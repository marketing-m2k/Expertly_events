"""JSON-LD discovery and validation.

Rules:
  * every <script type="application/ld+json"> is parsed on its own; a broken
    script never affects another one;
  * every Event node is turned into a record inside its own try/except -- a
    malformed node (no name, wrong types, nested junk) is reported as a
    problem and the remaining nodes of the same page, and the whole
    organisation's scrape, carry on;
  * a node with neither a usable name nor URL nor date is recorded as dropped
    with a reason, never silently skipped.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline.common import absolute_url, normalize_ws, same_page, similarity

_NOT_EVENTS = {"PublicationEvent", "BroadcastEvent", "SaleEvent", "DeliveryEvent", "OnDemandEvent"}
_EVENT_TYPES_EXTRA = {"CourseInstance"}


def _is_event_type(t) -> bool:
    types = t if isinstance(t, list) else [t]
    for x in types:
        x = str(x or "").rsplit("/", 1)[-1]
        if x in _EVENT_TYPES_EXTRA or (x.endswith("Event") and x not in _NOT_EVENTS):
            return True
    return False


def _loads(raw: str):
    raw = (raw or "").strip()
    raw = re.sub(r"^\s*<!--|-->\s*$", "", raw).strip()
    raw = re.sub(r"^\s*//\s*<!\[CDATA\[|//\s*\]\]>\s*$", "", raw).strip()
    for attempt in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            return json.loads(attempt, strict=False)
        except (ValueError, TypeError):
            continue
    return None


def _text(v) -> str:
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or v.get("text") or ""
    if isinstance(v, list):
        v = v[0] if v else ""
    return normalize_ws(html_lib.unescape(str(v or "")))


def _iter_nodes(obj, depth=0):
    if depth > 6:
        return
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_nodes(x, depth + 1)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "subEvent", "event", "itemListElement", "item", "mainEntity", "hasPart"):
            if key in obj:
                yield from _iter_nodes(obj[key], depth + 1)


def _offer_url(node: dict) -> str:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    return _text(offers.get("url")) if isinstance(offers, dict) else ""


def location_parts(loc) -> dict:
    """{name, street, locality, region, postal, country, text, virtual} from a JSON-LD location."""
    out = {"name": "", "street": "", "locality": "", "region": "", "postal": "", "country": "", "text": "", "virtual": False}
    if isinstance(loc, list):
        locs = [l for l in loc if l]
        virtual = any(isinstance(l, dict) and "VirtualLocation" in str(l.get("@type", "")) for l in locs)
        physical = [l for l in locs if not (isinstance(l, dict) and "VirtualLocation" in str(l.get("@type", "")))]
        loc = physical[0] if physical else (locs[0] if locs else None)
        out["virtual"] = virtual
    if not loc:
        return out
    if isinstance(loc, str):
        out["text"] = out["name"] = normalize_ws(loc)
        return out
    if not isinstance(loc, dict):
        return out
    if "VirtualLocation" in str(loc.get("@type", "")):
        out["virtual"] = True
        return out
    out["name"] = _text(loc.get("name"))
    addr = loc.get("address")
    if isinstance(addr, str):
        out["street"] = normalize_ws(addr)
    elif isinstance(addr, dict):
        out["street"] = _text(addr.get("streetAddress"))
        out["locality"] = _text(addr.get("addressLocality"))
        out["region"] = _text(addr.get("addressRegion"))
        out["postal"] = _text(addr.get("postalCode"))
        out["country"] = _text(addr.get("addressCountry"))
    parts, seen = [], set()
    for p in (out["name"], out["street"], out["locality"], out["region"], out["postal"], out["country"]):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            parts.append(p)
    out["text"] = ", ".join(parts)
    return out


def node_to_record(node: dict, base_url: str) -> dict:
    """Plain dict from one Event node. Raises nothing for odd shapes -- callers still wrap it."""
    ident = _text(node.get("@id"))
    url = absolute_url(base_url, _text(node.get("url"))) or (ident if ident.startswith("http") else "") \
        or absolute_url(base_url, _offer_url(node)) or absolute_url(base_url, _text(node.get("mainEntityOfPage")))
    organizer = node.get("organizer")
    if isinstance(organizer, list):
        organizer = organizer[0] if organizer else None
    return {
        "name": _text(node.get("name")) or _text(node.get("headline")),
        "url": url,
        "startDate": _text(node.get("startDate")),
        "endDate": _text(node.get("endDate")),
        "doorTime": _text(node.get("doorTime")),
        "location": location_parts(node.get("location")),
        "attendanceMode": _text(node.get("eventAttendanceMode")).rsplit("/", 1)[-1],
        "status": _text(node.get("eventStatus")).rsplit("/", 1)[-1],
        "description": _text(node.get("description")),
        "organizer": _text(organizer) if organizer else "",
        "registration_url": absolute_url(base_url, _offer_url(node)),
        "ext_id": ident or _text(node.get("identifier")),
        "type": _text(node.get("@type")),
    }


def parse_jsonld_events(html: str, base_url: str) -> tuple[list[dict], list[dict]]:
    """-> (event records, problems). problems = [{reason, detail}] -- one bad node/script costs only itself."""
    events: list[dict] = []
    problems: list[dict] = []
    if not html or "ld+json" not in html.lower():
        return events, problems
    try:
        soup = BeautifulSoup(html, "html.parser")
        scripts = soup.find_all("script", type=re.compile(r"ld\+json", re.I))
    except Exception as exc:  # noqa: BLE001
        return events, [{"reason": "jsonld_scan_failed", "detail": str(exc)[:120]}]
    for i, script in enumerate(scripts):
        data = _loads(script.string or script.get_text() or "")
        if data is None:
            problems.append({"reason": "jsonld_unparseable", "detail": f"script #{i}"})
            continue
        try:
            nodes = list(_iter_nodes(data))
        except Exception as exc:  # noqa: BLE001
            problems.append({"reason": "jsonld_unreadable", "detail": f"script #{i}: {exc!s:.100}"})
            continue
        for node in nodes:
            try:
                if not isinstance(node, dict) or not _is_event_type(node.get("@type")):
                    continue
                rec = node_to_record(node, base_url)
                if not (rec["name"] or rec["url"] or rec["startDate"]):
                    problems.append({"reason": "jsonld_empty_event", "detail": f"script #{i}: no name, url or date"})
                    continue
                if not rec["name"]:
                    rec["problem"] = "jsonld_missing_name"
                    problems.append({"reason": "jsonld_missing_name", "detail": rec["url"] or rec["startDate"]})
                events.append(rec)
            except Exception as exc:  # noqa: BLE001 - one malformed node must never kill the organisation
                problems.append({"reason": "jsonld_node_error", "detail": f"script #{i}: {type(exc).__name__}: {exc!s:.100}"})
    return events, problems


def validate_for_page(records: list[dict], page_url: str, page_title_candidates: list[str]) -> list[dict]:
    """Annotate each record with `belongs` (True/False) and `why`.

    A JSON-LD Event only counts for THIS page when its URL is this page, or its
    name matches the page's own heading/title. An Event that is neither is a
    widget for some other event (related/next-event/sidebar) and is never used.
    """
    out = []
    for r in records:
        why, belongs = [], False
        if r.get("url") and same_page(r["url"], page_url):
            belongs, why = True, ["url is this page"]
        elif r.get("name") and any(similarity(r["name"], t) >= 0.7 for t in page_title_candidates if t):
            belongs, why = True, ["name matches the page heading/title"]
        elif not r.get("url") and len(records) == 1 and r.get("name") is not None:
            belongs, why = None, ["only Event on page but not tied to this page's URL or heading"]
        else:
            why = ["url and name belong to a different event"]
        out.append({**r, "belongs": belongs, "why": why})
    return out
