"""Listing-page judgement: is this an event list? an archive? an empty-state? a dead URL?
Where is the site's real Events page when the configured URL is only a homepage?
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from pipeline.common import absolute_url, norm_url, normalize_ws, same_page, same_site
from pipeline.dates import find_dates

ARCHIVE_URL = re.compile(r"(?:^|[/_?&=-])(past|previous|archive[sd]?|completed|history|ended)(?:[-_/]?events?|[-_/]?webinars?|[-_/]|$|=)", re.I)
ARCHIVE_TITLE = re.compile(r"\b(past|previous|archived?|completed)\s+(events?|webinars?|seminars?|conferences?)\b|\bevent\s+archive\b", re.I)
EMPTY_STATE = re.compile(
    r"\b(no\s+(?:upcoming\s+|current\s+|scheduled\s+|forthcoming\s+)?(?:events?|webinars?|seminars?|courses?)\s*(?:found|scheduled|available|at this time|planned|listed|to show|currently)?|"
    r"there\s+are\s+no\s+(?:upcoming\s+)?events|nothing\s+(?:is\s+)?(?:scheduled|planned)|0\s+events?\s+found|check\s+back\s+(?:soon|later)|"
    r"no\s+results?\s+(?:found|match))\b", re.I)
EVENTS_NAV_TEXT = re.compile(
    r"^(?:(?:our\s+|all\s+|upcoming\s+|latest\s+|view\s+(?:all\s+)?)?(?:events?|webinars?|conferences?|seminars?|courses?)(?:\s*(?:&|and|/)\s*(?:events?|webinars?|training|conferences?|seminars?|courses?|cpd|programmes?|programs?))?|"
    r"events?\s+calendar|calendar(?:\s+of\s+events)?|what'?s\s+on|training\s*(?:&|and)\s*events?|events?\s*(?:&|and)\s*training|"
    r"upcoming\s+(?:events?|webinars?|seminars?|conferences?)|cpd\s+events?|events?\s+listing)$", re.I)


def is_archive_url(url: str) -> bool:
    p = urlsplit(url)
    return bool(ARCHIVE_URL.search(p.path + ("?" + p.query if p.query else "")))


def is_archive_page(url: str, html: str) -> bool:
    """The configured/followed URL itself is a past-events archive (not just a section of a current list)."""
    if is_archive_url(url):
        return True
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    title = normalize_ws(m.group(1)) if m else ""
    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html or "")
    h1t = normalize_ws(re.sub(r"<[^>]+>", " ", h1.group(1))) if h1 else ""
    return bool(ARCHIVE_TITLE.search(title) or ARCHIVE_TITLE.search(h1t)) and not re.search(r"\bupcoming\b", title + " " + h1t, re.I)


def has_empty_state(text: str) -> bool:
    return bool(EMPTY_STATE.search(text or ""))


def find_events_page_link(html: str, base_url: str) -> str:
    """When the configured URL is a homepage: the site's own Events/Calendar nav link, or ''."""
    soup = BeautifulSoup(html or "", "html.parser")
    best, best_score = "", 0
    for a in soup.find_all("a", href=True):
        text = normalize_ws(a.get_text(" ", strip=True))
        if not text or len(text) > 40 or not EVENTS_NAV_TEXT.match(text):
            continue
        url = absolute_url(base_url, a["href"])
        if not url or same_page(url, base_url) or not same_site(url, base_url):
            continue
        if re.search(r"/(category|tag)/|\.(pdf|jpg|png)$", url, re.I) or is_archive_url(url):
            continue
        score = 2 if re.match(r"^(upcoming\s+)?events?$", text, re.I) else 1
        if score > best_score:
            best, best_score = url, score
    return best


def listing_signals(html: str, page_url: str, locale: str = "dmy") -> dict:
    """Cheap listing-ness: how many dates (any common format) and how many event-looking links."""
    text = normalize_ws(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")))[:80000]
    dates = [h for h in find_dates(text, locale) if h.valid or h.partial]
    return {"dates": len(dates), "text_len": len(text)}
