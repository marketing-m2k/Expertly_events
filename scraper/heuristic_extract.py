"""Extract events from a rendered page using pattern matching only — no API, no cost.

Looks for elements containing a date-like string, then walks up the DOM to
find the surrounding "card" and pulls a title, link, and location out of it.
Works well on typical event-listing layouts; misses unusual ones. Free but
lower recall/precision than an LLM-based extractor.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

LINK_KEYWORDS = re.compile(r"\b(register|rsvp|details|learn more|read more|view event|event details|more info)\b", re.I)

MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

DATE_PATTERNS = [
    re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:\s*[-–—]\s*(?:(?:{MONTHS})\.?\s+)?\d{{1,2}})?(?:,?\s*\d{{4}})?\b", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(rf"\b\d{{1,2}}\s+(?:{MONTHS})\.?(?:,?\s*\d{{4}})?\b", re.I),
]

VIRTUAL_WORDS = re.compile(r"\b(virtual|online|webinar|zoom|livestream|web conference)\b", re.I)
IN_PERSON_WORDS = re.compile(r"\b(in[\s-]?person|on[\s-]?site)\b", re.I)
LOCATION_PATTERN = re.compile(r"\b[A-Z][a-zA-Z.]+(?:\s[A-Z][a-zA-Z.]+)*,\s*[A-Z]{2}\b")


def _find_date(text: str) -> str:
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return ""


def _find_card(node: Tag, max_up: int = 5) -> Tag:
    """Walk up from a date-bearing node to the element that likely wraps one event."""
    current = node
    for _ in range(max_up):
        if current.parent is None:
            break
        parent = current.parent
        # heuristic: stop climbing once the parent has multiple links or headings,
        # meaning it probably wraps more than one event
        links = parent.find_all("a", href=True)
        headings = parent.find_all(re.compile(r"^h[1-4]$"))
        if len(links) > 3 or len(headings) > 2:
            return current
        current = parent
    return current


def _valid_href(href: str) -> bool:
    href = (href or "").strip()
    return bool(href) and not href.startswith(("#", "javascript:", "mailto:", "tel:"))


def _find_title_and_link(card: Tag, base_url: str) -> tuple[str, str]:
    """Find the event title and, preferentially, the link that actually
    points at *that* event's own page — not just the first link in the card.

    Priority: (1) the link the heading text itself is wrapped in or contains,
    (2) a link whose own text is register/details/etc., (3) a link whose
    text looks like a title, (4) the first valid link as a last resort.
    """
    heading = card.find(re.compile(r"^h[1-4]$"))
    title = heading.get_text(strip=True) if heading else ""

    # (1) the heading's own anchor — most reliable per-event link
    if heading:
        anchor = heading if heading.name == "a" else heading.find("a", href=True)
        if not anchor:
            parent_a = heading.find_parent("a", href=True)
            anchor = parent_a
        if anchor and _valid_href(anchor.get("href", "")):
            link = urljoin(base_url, anchor["href"].strip())
            if not title:
                title = anchor.get_text(strip=True)
            return title, link

    candidates = [a for a in card.find_all("a", href=True) if _valid_href(a["href"])]
    if not candidates:
        return title, ""

    # (2) explicit "register/details/..." link text
    for a in candidates:
        if LINK_KEYWORDS.search(a.get_text(strip=True)):
            link = urljoin(base_url, a["href"].strip())
            if not title:
                title = a.get_text(strip=True)
            return title, link

    # (3) a link whose text reads like a title
    for a in candidates:
        text = a.get_text(strip=True)
        if 8 <= len(text) <= 140:
            if not title:
                title = text
            return title, urljoin(base_url, a["href"].strip())

    # (4) fall back to the first link in the card
    link = urljoin(base_url, candidates[0]["href"].strip())
    if not title:
        title = candidates[0].get_text(strip=True)
    return title, link


def _find_location(text: str) -> str:
    m = LOCATION_PATTERN.search(text)
    return m.group(0) if m else ""


def _find_format(text: str) -> str:
    if VIRTUAL_WORDS.search(text):
        return "Virtual"
    if IN_PERSON_WORDS.search(text):
        return "In Person"
    return ""


def extract_events(html: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    seen_cards = set()
    events = []

    for node in soup.find_all(string=True):
        text = str(node)
        date = _find_date(text)
        if not date:
            continue

        card = _find_card(node.parent if node.parent else soup)
        card_id = id(card)
        if card_id in seen_cards:
            continue
        seen_cards.add(card_id)

        card_text = card.get_text(" ", strip=True)
        title, link = _find_title_and_link(card, source_url)
        if not title:
            continue

        events.append({
            "event_name": title,
            "date": date,
            "format": _find_format(card_text),
            "location_city": _find_location(card_text),
            "description": "",
            "link": link,
        })

    # dedupe by (title, date)
    deduped = {}
    for e in events:
        key = (e["event_name"].lower(), e["date"].lower())
        deduped.setdefault(key, e)
    return list(deduped.values())
