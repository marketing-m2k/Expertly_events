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


# A handful of sites use a section/category label -- not the event's own
# name -- as every single one of their event cards' heading: TVED tags
# every masterclass card "Masterclasses", the Australian Competition
# Tribunal's template literally headings each listing "LISTING", Institute
# of Directors India uses "THEME", the NY Fed's own program names
# ("RESILIENCE") get picked up instead of that session's actual topic.
# Trusting the heading blindly there produces a sheet full of events all
# named "Masterclasses" -- worse than no heading at all, since the card
# usually has the real, event-specific title sitting in a different link.
GENERIC_HEADINGS = {
    "masterclasses", "masterclass", "lunchtime series", "listing", "theme",
    "webinar", "webinars", "conference", "conferences", "seminar", "seminars",
    "news letter", "newsletter", "event", "events", "details", "register",
    "training", "workshop", "workshops", "programme", "program", "session",
    "sessions", "meeting", "meetings", "series", "resilience",
    "economic education", "update", "updates",
}


def _find_title_and_link(card: Tag, base_url: str) -> tuple[str, str]:
    """Find the event title and, preferentially, the link that actually
    points at *that* event's own page — not just the first link in the card.

    Priority: (1) the link the heading text itself is wrapped in or contains
    -- unless that heading text is a generic section label rather than the
    event's own name, in which case it's set aside in favor of (2) a link
    whose own text is register/details/etc., (3) a link whose text looks
    like a title, (4) the first valid link as a last resort -- falling back
    to the generic heading text only if nothing more specific turns up
    anywhere in the card, since a vague name still beats silently dropping
    the event.
    """
    heading = card.find(re.compile(r"^h[1-4]$"))
    heading_text = heading.get_text(strip=True) if heading else ""
    usable_heading = "" if heading_text.strip().lower() in GENERIC_HEADINGS else heading_text
    title = usable_heading

    # (1) the heading's own anchor — most reliable per-event link, but only
    # trusted for the title text when the heading itself wasn't generic
    if heading and usable_heading:
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
        return title or heading_text, ""

    # (2) explicit "register/details/..." link text
    for a in candidates:
        if LINK_KEYWORDS.search(a.get_text(strip=True)):
            link = urljoin(base_url, a["href"].strip())
            if not title:
                title = a.get_text(strip=True)
            return title, link

    # (3) a link whose text reads like a title -- skip another generic label
    # here too (e.g. a second nav-ish link in the same card), so a genuinely
    # generic heading doesn't just get replaced by an equally generic link
    for a in candidates:
        text = a.get_text(strip=True)
        if 8 <= len(text) <= 140 and text.strip().lower() not in GENERIC_HEADINGS:
            if not title:
                title = text
            return title, urljoin(base_url, a["href"].strip())

    # (4) fall back to the first link in the card, or the generic heading
    # text if even that has nothing -- still better than losing the event
    link = urljoin(base_url, candidates[0]["href"].strip())
    if not title:
        title = candidates[0].get_text(strip=True) or heading_text
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


NON_EVENT_CLASS = re.compile(
    r"\b(nav|menu|footer|header|breadcrumb|sidebar|cookie|social|share|pagination|"
    r"pager|widget|search|filter|tag|categor)", re.I,
)


def _card_to_event(card: Tag, source_url: str) -> dict | None:
    card_text = card.get_text(" ", strip=True)
    title, link = _find_title_and_link(card, source_url)
    if not title:
        return None
    return {
        "event_name": title,
        "date": _find_date(card_text),
        "format": _find_format(card_text),
        "location_city": _find_location(card_text),
        "description": "",
        "link": link,
    }


def _find_repeated_card_groups(soup: BeautifulSoup) -> list[list[Tag]]:
    """Find groups of sibling elements that repeat under the same parent with
    the same tag+class — the structural signature of a list of event cards.
    This catches events whose date lives in a different sentence/node than
    the one a naive per-text-node walk would start from, or that have no
    date at all on the listing page.
    """
    groups: dict[tuple, list[Tag]] = {}
    for el in soup.find_all(["article", "li", "div", "tr"]):
        classes = tuple(sorted(el.get("class") or []))
        key = (id(el.parent), el.name, classes)
        groups.setdefault(key, []).append(el)

    candidates = []
    for (_, _tag, classes), els in groups.items():
        if not (2 <= len(els) <= 300):
            continue
        class_str = " ".join(classes)
        if NON_EVENT_CLASS.search(class_str):
            continue

        good = []
        for el in els:
            if not el.find("a", href=True):
                continue
            text_len = len(el.get_text(strip=True))
            if not (15 <= text_len <= 5000):
                continue
            good.append(el)
        if len(good) < 2:
            continue
        candidates.append(good)
    return candidates


def _score_group(els: list[Tag]) -> float:
    with_date = sum(1 for el in els if _find_date(el.get_text(" ", strip=True)))
    with_heading = sum(1 for el in els if el.find(re.compile(r"^h[1-4]$")))
    return (with_date + with_heading) / len(els)


def _dedupe_nested(groups: list[list[Tag]]) -> list[list[Tag]]:
    """Drop a group if its elements are ancestors of elements in a
    higher-scoring group — otherwise the same events get captured twice,
    once as an outer wrapper and once as the inner card."""
    scored = sorted(groups, key=_score_group, reverse=True)
    kept: list[list[Tag]] = []
    kept_descendant_ids: set[int] = set()
    for group in scored:
        if any(id(el) in kept_descendant_ids for el in group):
            continue
        kept.append(group)
        for ke in group:
            kept_descendant_ids.update(id(d) for d in ke.descendants if isinstance(d, Tag))
    return kept


CHROME_ID_CLASS = re.compile(
    r"cookiebot|cybot|onetrust|cookie-consent|cookie-banner|gdpr|userway|accessibility-widget",
    re.I,
)


def _strip_page_chrome(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "nav", "footer", "iframe", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(id=CHROME_ID_CLASS):
        tag.decompose()
    for tag in soup.find_all(class_=CHROME_ID_CLASS):
        tag.decompose()


def extract_events(html: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    _strip_page_chrome(soup)

    events = []

    groups = _find_repeated_card_groups(soup)
    groups = [g for g in groups if _score_group(g) >= 0.3]
    groups = _dedupe_nested(groups)

    covered_cards: set[int] = set()
    for group in groups:
        for card in group:
            covered_cards.add(id(card))
            event = _card_to_event(card, source_url)
            if event:
                events.append(event)

    # fallback for pages that don't have a clean repeated-card structure:
    # the original date-driven per-text-node walk, skipping anything
    # already covered by the structural pass above.
    seen_cards = set(covered_cards)
    for node in soup.find_all(string=True):
        date = _find_date(str(node))
        if not date:
            continue
        card = _find_card(node.parent if node.parent else soup)
        card_id = id(card)
        if card_id in seen_cards:
            continue
        seen_cards.add(card_id)
        event = _card_to_event(card, source_url)
        if event:
            events.append(event)

    # dedupe by (title, date, link)
    deduped = {}
    for e in events:
        key = (e["event_name"].lower(), e["date"].lower(), e["link"].lower())
        deduped.setdefault(key, e)
    return list(deduped.values())
