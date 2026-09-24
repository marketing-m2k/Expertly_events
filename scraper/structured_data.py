"""Priority-1 extraction source: the labelled event data many sites embed for
Google and calendar apps (schema.org "Event" as JSON-LD or microdata).

Every value here is stated by the site itself in a named field (startDate,
location, eventAttendanceMode...), so nothing is inferred. No AI, no cost.
"""

import json
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.date_utils import format_display_date
from scraper.evidence import field, normalize_ws

_NOT_REAL_EVENTS = {"PublicationEvent", "BroadcastEvent", "SaleEvent", "DeliveryEvent"}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _is_event_type(node_type) -> bool:
    types = node_type if isinstance(node_type, list) else [node_type]
    for t in types:
        t = str(t or "").rsplit("/", 1)[-1]
        if t.endswith("Event") and t not in _NOT_REAL_EVENTS:
            return True
    return False


def _iter_nodes(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_nodes(item)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "subEvent", "event"):
            if key in obj:
                yield from _iter_nodes(obj[key])


def _iso_to_display(raw) -> str:
    m = _ISO_DATE.match(str(raw or "").strip())
    if not m:
        return ""
    try:
        return format_display_date(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    except ValueError:
        return ""


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return normalize_ws(str(value or ""))


def _location(loc) -> tuple[str, bool]:
    """(location text, is_virtual). Virtual locations carry no venue."""
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not loc:
        return "", False
    if isinstance(loc, str):
        return normalize_ws(loc), False
    if not isinstance(loc, dict):
        return "", False
    if "VirtualLocation" in str(loc.get("@type", "")):
        return "", True
    parts = [_text(loc.get("name"))]
    addr = loc.get("address")
    if isinstance(addr, str):
        parts.append(normalize_ws(addr))
    elif isinstance(addr, dict):
        for key in ("streetAddress", "addressLocality", "addressRegion", "addressCountry"):
            parts.append(_text(addr.get(key)))
    seen, out = set(), []
    for p in parts:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out), False


def _format_from_mode(mode: str, virtual_location: bool) -> str:
    mode = (mode or "").rsplit("/", 1)[-1].lower()
    if "mixed" in mode:
        return "Hybrid"
    if "online" in mode:
        return "Virtual"
    if "offline" in mode:
        return "In Person"
    return "Virtual" if virtual_location else ""


def _from_jsonld_node(node: dict, base_url: str) -> dict:
    out = {}
    name = _text(node.get("name"))
    if name:
        out["name"] = field(name, name, "Title from the site's structured event data (name)", "structured_data")

    start_raw = _text(node.get("startDate"))
    start = _iso_to_display(start_raw)
    if start:
        out["date"] = field(start, start_raw, "Date from the site's structured event data (startDate)", "structured_data")
    end_raw = _text(node.get("endDate"))
    end = _iso_to_display(end_raw)
    if end and end != start:
        out["end_date"] = field(end, end_raw, "End date from the site's structured event data (endDate)", "structured_data")

    location, virtual = _location(node.get("location"))
    if location:
        out["location"] = field(location, location.split(",")[0], "Venue from the site's structured event data (location)", "structured_data")
    mode_raw = _text(node.get("eventAttendanceMode"))
    fmt = _format_from_mode(mode_raw, virtual)
    if fmt:
        out["format"] = field(fmt, mode_raw or "VirtualLocation",
                              "Format from the site's structured event data (eventAttendanceMode)", "structured_data")

    description = _text(node.get("description"))
    if description:
        out["description"] = field(description[:600], description[:60],
                                   "Description from the site's structured event data (description)", "structured_data")
    url = _text(node.get("url"))
    if url:
        out["link"] = field(urljoin(base_url, url), url, "Link from the site's structured event data (url)", "structured_data")
    return out


def _itemprop_value(el) -> str:
    for attr in ("content", "datetime", "href"):
        if el.get(attr):
            return normalize_ws(el[attr])
    return normalize_ws(el.get_text(" ", strip=True))


def _from_microdata_scope(scope, base_url: str) -> dict:
    def prop(name):
        el = scope.find(attrs={"itemprop": name})
        return _itemprop_value(el) if el else ""

    out = {}
    name = prop("name")
    if name:
        out["name"] = field(name, name, "Title from the page's microdata (itemprop=name)", "microdata")
    start_raw = prop("startDate")
    start = _iso_to_display(start_raw)
    if start:
        out["date"] = field(start, start_raw, "Date from the page's microdata (itemprop=startDate)", "microdata")
    end_raw = prop("endDate")
    end = _iso_to_display(end_raw)
    if end and end != start:
        out["end_date"] = field(end, end_raw, "End date from the page's microdata (itemprop=endDate)", "microdata")
    loc_el = scope.find(attrs={"itemprop": "location"})
    if loc_el:
        loc = normalize_ws(loc_el.get_text(" ", strip=True))
        if loc:
            out["location"] = field(loc, loc, "Venue from the page's microdata (itemprop=location)", "microdata")
    url = prop("url")
    if url:
        out["link"] = field(urljoin(base_url, url), url, "Link from the page's microdata (itemprop=url)", "microdata")
    return out


def extract_structured_events(html: str, base_url: str) -> list[dict]:
    """Every schema.org Event on the page, each as {column: evidence-field}."""
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in _iter_nodes(data):
            if _is_event_type(node.get("@type")):
                record = _from_jsonld_node(node, base_url)
                if record:
                    events.append(record)

    for scope in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/\w*Event$", re.I)}):
        record = _from_microdata_scope(scope, base_url)
        if record:
            events.append(record)
    return events


def _same_page(a: str, b: str) -> bool:
    strip = lambda u: re.sub(r"[#?].*$", "", u or "").rstrip("/").lower()
    return bool(a) and strip(a) == strip(b)


def pick_event_for_page(events: list[dict], page_url: str) -> dict | None:
    """A single event's own page normally carries exactly one Event. If the
    page carries several (it's really a listing), only trust the one whose
    url is this page -- otherwise pick none rather than guess which it is."""
    if len(events) == 1:
        return events[0]
    for e in events:
        if "link" in e and _same_page(e["link"]["value"], page_url):
            return e
    return None
