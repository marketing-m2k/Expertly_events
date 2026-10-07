"""Feed-based discovery: ICS/iCalendar, RSS/Atom and XML sitemaps.

All parsers are tolerant (a bad item costs only itself) and return plain
dicts. Feeds are a *second opinion* on completeness as much as a source of
candidates: a sitemap or calendar that lists far more event URLs than the
list page showed is a signal that discovery is incomplete.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from bs4 import BeautifulSoup

from pipeline.common import absolute_url, norm_url, normalize_ws
from pipeline.dates import parse_iso

EVENT_URL_WORDS = re.compile(
    r"(?:^|[/_-])(events?|webinars?|seminars?|conferences?|workshops?|courses?|training|summits?|forums?|masterclass(?:es)?|"
    r"symposi\w+|roundtables?|cpd|cpe|programmes?|programs?|lectures?|convention|congress|expo|tribe[-_]events?|whats-on)(?:[/_-]|\.|$)", re.I)
NOT_EVENT_URL_WORDS = re.compile(
    r"(?:^|/)(?:category|categories|tag|tags|topic|topics|type|types|page|feed|rss|author|search|login|signin|sign-in|account|"
    r"membership|join|careers|jobs|privacy|terms|cookies?|sitemap|speakers?|sponsors?|exhibitors?|wp-json|wp-admin|cart|checkout)(?:/|$|\.)", re.I)


def find_feed_links(html: str, base_url: str) -> dict[str, list[str]]:
    out = {"rss": [], "ics": [], "sitemap": []}
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return out
    for link in soup.find_all("link", href=True):
        t = (link.get("type") or "").lower()
        rel = " ".join(link.get("rel") or []).lower()
        href = absolute_url(base_url, link["href"])
        if not href:
            continue
        if "rss" in t or "atom" in t:
            out["rss"].append(href)
        elif "calendar" in t or href.lower().split("?")[0].endswith(".ics"):
            out["ics"].append(href)
        elif "sitemap" in rel:
            out["sitemap"].append(href)
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        low = h.lower()
        if low.startswith("webcal:"):
            out["ics"].append("https:" + h[len("webcal:"):])
        elif low.split("?")[0].endswith(".ics") or re.search(r"[?&](ical|ics|outlook-ical)=", low):
            if re.search(r"(subscribe|calendar|ical|export|all)", a.get_text(" ", strip=True) + low, re.I):
                out["ics"].append(absolute_url(base_url, h))
    for k in out:
        out[k] = list(dict.fromkeys(out[k]))
    return out


# ---- ICS ----------------------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _ics_date(value: str) -> str:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", value or "")
    if not m:
        return ""
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return ""


def parse_ics(text: str, base_url: str = "") -> list[dict]:
    """VEVENT blocks -> [{name, startDate, endDate, location, description, url, ext_id}]. Bad blocks are skipped individually."""
    events, cur = [], None
    for line in _unfold(text or ""):
        up = line.upper()
        if up.startswith("BEGIN:VEVENT"):
            cur = {}
        elif up.startswith("END:VEVENT"):
            if cur is not None:
                try:
                    start, end = cur.get("DTSTART", ""), cur.get("DTEND", "")
                    rec = {"name": _ics_unescape(cur.get("SUMMARY", "")), "startDate": _ics_date(start),
                           "endDate": _ics_date(end), "location": _ics_unescape(cur.get("LOCATION", "")),
                           "description": _ics_unescape(cur.get("DESCRIPTION", ""))[:600],
                           "url": absolute_url(base_url, cur.get("URL", "")), "ext_id": cur.get("UID", ""),
                           "raw_start": start}
                    # an all-day DTEND is exclusive: 20261012..20261013 is a one-day event
                    if rec["endDate"] and re.fullmatch(r"\d{8}", end or "") and rec["startDate"]:
                        e = parse_iso(rec["endDate"])
                        s = parse_iso(rec["startDate"])
                        if e and s and (e - s).days >= 1:
                            from datetime import timedelta
                            rec["endDate"] = (e - timedelta(days=1)).isoformat()
                            if rec["endDate"] == rec["startDate"]:
                                rec["endDate"] = ""
                    if rec["name"] or rec["url"]:
                        events.append(rec)
                except Exception:  # noqa: BLE001
                    pass
            cur = None
        elif cur is not None and ":" in line:
            key, _, val = line.partition(":")
            cur[key.split(";")[0].upper()] = val.strip()
    return events


# ---- RSS / Atom ----------------------------------------------------------------------

def parse_feed(xml_text: str, base_url: str = "") -> list[dict]:
    """Items -> [{name, url, published, description}]. NOTE: `published` is the post date, not the event date."""
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return items
    ns_strip = lambda tag: tag.rsplit("}", 1)[-1]
    for el in root.iter():
        if ns_strip(el.tag) not in ("item", "entry"):
            continue
        rec = {"name": "", "url": "", "published": "", "description": ""}
        for ch in el:
            t = ns_strip(ch.tag)
            if t == "title":
                rec["name"] = normalize_ws("".join(ch.itertext()))
            elif t == "link":
                rec["url"] = absolute_url(base_url, ch.attrib.get("href") or (ch.text or "").strip())
            elif t in ("pubDate", "published", "updated") and not rec["published"]:
                rec["published"] = normalize_ws(ch.text)
            elif t in ("description", "summary") and not rec["description"]:
                rec["description"] = normalize_ws(re.sub(r"<[^>]+>", " ", "".join(ch.itertext())))[:400]
        if rec["name"] or rec["url"]:
            items.append(rec)
    return items


# ---- sitemaps ---------------------------------------------------------------------------

def parse_sitemap(xml_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """-> ([(url, lastmod)], [child sitemap urls])"""
    urls, children = [], []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return urls, children
    ns_strip = lambda tag: tag.rsplit("}", 1)[-1]
    for el in root:
        kind = ns_strip(el.tag)
        loc = lastmod = ""
        for ch in el:
            t = ns_strip(ch.tag)
            if t == "loc":
                loc = normalize_ws(ch.text)
            elif t == "lastmod":
                lastmod = normalize_ws(ch.text)
        if not loc:
            continue
        (children if kind == "sitemap" else urls).append(loc if kind == "sitemap" else (loc, lastmod))
    return urls, children


def event_like_sitemap_urls(urls: list[tuple[str, str]], listing_url: str) -> list[tuple[str, str]]:
    """Sitemap URLs that look like individual event pages under the listing's section of the site."""
    from urllib.parse import urlsplit
    lp = urlsplit(listing_url)
    base_seg = [s for s in lp.path.split("/") if s][:1]
    out = []
    for u, lm in urls:
        p = urlsplit(u)
        if p.netloc.lower().removeprefix("www.") != lp.netloc.lower().removeprefix("www."):
            continue
        path = p.path
        if NOT_EVENT_URL_WORDS.search(path) or norm_url(u) == norm_url(listing_url):
            continue
        segs = [s for s in path.split("/") if s]
        if len(segs) < 2:
            continue
        if EVENT_URL_WORDS.search(path) or (base_seg and segs[0] == base_seg[0] and len(segs) >= 2):
            out.append((u, lm))
    return out
