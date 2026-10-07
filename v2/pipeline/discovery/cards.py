"""Card discovery that does NOT depend on repeated sibling markup.

v1 only recognised an event list when >= 2 sibling elements shared the same tag
and CSS class, scored the group on dates/headings, then removed nested groups.
That loses single events, featured events, alternating layouts, cards styled
differently, and every event of a list whose first group was mis-scored.

v2 is link-driven instead:

  1. Every candidate event link on the page is found and filtered by its URL
     (event-ish path / id parameter / event-platform host; never navigation,
     categories, pagination, social, files, the page itself).
  2. Links to the same event (title link, image link, 'Register' button, '.ics')
     are grouped into ONE unit.
  3. Each unit's card is the LARGEST ancestor that contains no link of any other
     unit -- so the card boundary is found structurally, with no assumption
     about class names, sibling count or alternating layouts.
  4. Dates written outside the link (a <dt>, a day heading above a group of
     cards, a separate date column) are inherited from the card's own
     preceding siblings / ancestors' preceding siblings.
  5. Rows of text that carry a date but no link at all are found as 'text-date'
     candidates.
  6. Cards under a 'Past events' / 'Archive' heading (or class) are flagged as
     archive so current discovery is not contaminated by them.

A generic title ('Webinar', 'Event - ...') never discards a card: the best
title in the card is used and the title confidence is lowered, so detail
extraction can resolve the final title from the event's own page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from pipeline.common import absolute_url, norm_text, norm_url, normalize_ws, same_page, same_site
from pipeline.dates import DateHit, date_context_is_deadline, find_dates

# ---- vocab -------------------------------------------------------------------------------

CTA_TEXT = re.compile(
    r"^(read more|learn more|more info(?:rmation)?|find out more|view (?:the )?(?:event|details?|more|all)|event details?|details?|"
    r"register(?: now| here| today)?|book(?: now| here| a place| your place| tickets?)?|buy tickets?|sign[- ]?up|rsvp|explore|see more|"
    r"more|apply(?: now)?|enrol(?:l)?(?: now)?|get tickets?|click here|join(?: now)?|add to calendar|save the date|view|go|"
    r"read the article|continue reading|full details?|know more|show details?)\W*$", re.I)

HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
TITLE_CLASS = re.compile(r"(?:^|[\s_-])(?:event[-_]?)?(?:title|name|heading|headline|card[-_]title)(?:$|[\s_-])", re.I)

_CHROME = re.compile(
    r"cookiebot|cybot|onetrust|cookie|consent|gdpr|userway|accessibility-widget|skip-link|screen-reader|breadcrumb|"
    r"(?:^|[\s_-])(?:navbar|site-nav|main-nav|primary-nav|top-nav|mega-?menu|dropdown-menu|offcanvas|mobile-menu|social-links|share-buttons|"
    r"newsletter-signup|back-to-top|copyright|visitor-counter|last-updated)(?:$|[\s_-])|^(?:site-|page-|main-|global-)?footer$|(?:^|\s)(?:site|page|main|global)-footer(?:$|\s)", re.I)

EXTERNAL_EVENT_HOSTS = re.compile(
    r"(eventbrite\.|zoom\.us/(?:webinar|meeting)|lu\.ma|meetup\.com/.+/events|cvent\.com|hopin\.|livestorm\.|bigmarker\.|airmeet\.|"
    r"gotowebinar\.|teams\.microsoft\.com|events\.|eventsair\.|swoogo\.|splashthat\.|on24\.com|brighttalk\.|webex\.com|ticketsource\.|"
    r"humanitix\.|trybooking\.|oztix\.|eventzilla\.|konfhub\.|townscript\.|insider\.in|ticketleap\.)", re.I)
SOCIAL_HOSTS = re.compile(r"(facebook\.com|twitter\.com|x\.com|instagram\.com|youtube\.com|youtu\.be|tiktok\.com|pinterest\.|t\.me|wa\.me|whatsapp\.)", re.I)

_EVENTISH_SEG = re.compile(
    r"(events?|webinars?|seminars?|conferences?|workshops?|courses?|training|programmes?|programs?|summits?|forums?|sessions?|"
    r"masterclass(?:es)?|symposi\w+|roundtables?|lectures?|cpd|cpe|calendar|whats-on|meetings?|expo|congress|convention|retreats?|"
    r"intensives?|series|ceremony|dinner|lunch|breakfast|briefing|bootcamp|clinic|moot|competition|tax-day)", re.I)
_NOT_EVENT_SEG = re.compile(
    r"^(news|blog|articles?|press|media|insights?|publications?|resources?|about|about-us|contact|contact-us|login|log-in|signin|"
    r"sign-in|account|membership|join|careers?|jobs?|privacy|privacy-policy|terms|terms-and-conditions|cookies?|sitemap|search|tags?|"
    r"category|categories|topics?|types?|page|feed|rss|author|speakers?|sponsors?|exhibitors?|partners?|team|staff|people|faqs?|help|"
    r"support|shop|store|cart|checkout|wp-json|wp-admin|wp-content|donate|subscribe|unsubscribe|home|index|filters?|archive|archives|"
    r"user|users|logout|log-out|register-interest|become-.*|sponsor.*|exhibit.*|advertis.*|partner.*|.*-policy|.*-terms|accessibility|"
    r"disclaimer|copyright|complaints?|feedback|directory|find-a-.*|join-.*|member-login)$", re.I)
_LISTING_PLURAL = re.compile(r"(?:^|[-_])(events|webinars|seminars|conferences|workshops|courses|summits|forums|programmes|programs|sessions|"
                             r"meetings|trainings|masterclasses|calendar|conventions)$", re.I)
_ASSET_EXT = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|zip|mp3|mp4|woff2?|ttf|xml|json|rss)(?:\?|$)", re.I)
_DOC_EXT = re.compile(r"\.(?:pdf|docx?|pptx?|xlsx?)(?:\?|$)", re.I)
_PAGINATION_QS = re.compile(r"(?:^|[?&])(?:page|paged|pg|p|pagenum|pageno|pageindex|start|offset|skip|from|cursor|after|pn|epage|sheet)=\d*", re.I)
_PAGINATION_PATH = re.compile(r"/page/\d+/?$|/p/\d+/?$|[-_/]page[-_]?\d+/?$", re.I)
_EVENT_QS = re.compile(r"(?:^|[?&])(?:event_?id|eid|event|ev|e|eventkey|id|item|itemid|sessionid|course_?id|coursemasterid|activity|ref_event)=", re.I)
_ARCHIVE_HEAD = re.compile(r"\b(past|previous|archive[sd]?|completed|ended|older|recent(?:ly)?\s+(?:events?|webinars?)|prior|finished|on[\s-]?demand\s+archive)\b", re.I)
_UPCOMING_HEAD = re.compile(r"\b(upcoming|forthcoming|future|next|current|featured|new|coming\s+up|what'?s on)\b", re.I)
_ARCHIVE_ATTR = re.compile(r"(?:^|[\s_#-])(past|archive[sd]?|previous|completed|old)[-_ ]?(events?|list|items?|tab|pane|panel|section|webinars?)(?:$|[\s_-])", re.I)
_ROWLIKE = {"li", "tr", "article", "div", "p", "section", "dd", "dt", "td", "a", "span"}
_SKIP_TEXT_PARENTS = {"script", "style", "title", "head", "noscript", "select", "option", "textarea", "button", "footer", "nav"}
_SUFFIX_SEG = re.compile(r"/(register|registration|book|booking|tickets?|details?|info|ics|ical|calendar|speakers?|agenda|programme|program|"
                         r"enrol|enroll|apply|sponsors?|schedule)/?$", re.I)
MAX_CARD_TEXT = 1800


@dataclass
class Card:
    element: Tag | None
    anchors: list[Tag]
    url: str
    title: str
    title_confidence: str
    title_raw: str
    card_text: str
    date_hint: str
    date_inherited: bool
    location_hint: str
    in_archive: bool
    section: str
    methods: list[str]
    shared_url: bool = False
    link_score: int = 0
    notes: list[str] = field(default_factory=list)
    alt_urls: list[str] = field(default_factory=list)


@dataclass
class LinkDrop:
    reason: str
    url: str = ""
    title: str = ""
    detail: str = ""


# ---- helpers -----------------------------------------------------------------------------------

def prepare_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript", "template", "svg", "iframe", "nav", "footer", "select", "head"]):
        t.decompose()
    for t in soup.find_all(attrs={"hidden": True}):
        t.decompose()
    for t in soup.find_all(attrs={"aria-hidden": "true"}):
        if t.name in ("div", "span", "ul", "section") and not t.find("a", href=True):
            t.decompose()
    for t in soup.find_all(True):
        if t.attrs is None:
            continue
        ident = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        if ident.strip() and _CHROME.search(ident):
            t.decompose()
    return soup


def _text(el) -> str:
    return normalize_ws(el.get_text(" ", strip=True)) if el is not None else ""


def clean_title(text: str) -> str:
    t = normalize_ws(text)
    t = re.sub(r"\s*\|\s*$", "", t)
    return t


def strip_date_affix(title: str, locale: str = "dmy") -> str:
    """'24 September 2026: Farming Conference' -> 'Farming Conference' (only when real words remain)."""
    t = normalize_ws(title)
    hits = [h for h in find_dates(t, locale) if h.valid or h.partial]
    for h in hits:
        rest_before, rest_after = t[:h.span[0]], t[h.span[1]:]
        if not rest_before.strip(" \t-–—|:,()[]") and len(re.sub(r"^[\s\-–—|:,.)(\]]+", "", rest_after)) >= 6:
            return re.sub(r"^[\s\-–—|:,.)(\]]+", "", rest_after).strip()
        if not rest_after.strip(" \t-–—|:,()[]") and len(rest_before.strip(" -–—|:,([")) >= 6:
            return rest_before.strip(" -–—|:,([")
    return t


def is_generic_title(title: str, org_name: str = "") -> bool:
    from scraper.label_extract import is_generic_title as v1_generic
    t = norm_text(title)
    if not t or len(t) < 4 or CTA_TEXT.match(normalize_ws(title)):
        return True
    return v1_generic(title, org_name)


def classify_link(href_abs: str, page_url: str, anchor_text: str) -> tuple[int, str]:
    """-> (score, reject_reason). reject_reason != '' means never an event link."""
    p = urlsplit(href_abs)
    if p.scheme not in ("http", "https"):
        return 0, "non_http"
    if same_page(href_abs, page_url):
        return 0, "same_page"
    full = href_abs.lower()
    if _ASSET_EXT.search(p.path + ("?" + p.query if p.query else "")):
        return 0, "asset_link"
    if _DOC_EXT.search(p.path):
        return 0, "document_link"
    if SOCIAL_HOSTS.search(p.netloc.lower() + p.path.lower()) and "/events/" not in p.path.lower():
        return 0, "social_link"
    if re.search(r"(calendar\.google\.com|google\.com/calendar|outlook\.(live|office)\.com|outlook\.office365|yahoo\.com/calendar)", full)             or re.match(r"(google calendar|i?calendar|outlook|yahoo|export|subscribe|add to (my )?(google |outlook )?calendar|\.ics)", anchor_text or "", re.I):
        return 0, "calendar_feed_link"
    if _PAGINATION_QS.search("?" + p.query) and not _EVENT_QS.search("?" + p.query) and norm_url(href_abs).split("?")[0] == norm_url(page_url).split("?")[0]:
        return 0, "pagination_link"
    if _PAGINATION_PATH.search(p.path):
        return 0, "pagination_link"
    segs = [s for s in p.path.split("/") if s]
    if re.search(r"(?:^|[?&])(?:ical|ics|outlook-ical|eventdisplay|tribe-bar-date|tribe_events|post_type)=", "?" + p.query, re.I) and not _EVENT_QS.search("?" + p.query):
        return 0, "calendar_feed_link"
    if len(segs) >= 2 and re.fullmatch(r"(month|today|day|week|year|photo|map|list|summary|agenda|calendar-view|grid)", segs[-1], re.I):
        return 0, "calendar_view_link"
    ext_event_host = bool(EXTERNAL_EVENT_HOSTS.search(p.netloc.lower() + p.path.lower()))
    same_host = same_site(href_abs, page_url)
    # navigation / account / content-type pages: judged on the FIRST path segment(s)
    if segs and not ext_event_host:
        if _NOT_EVENT_SEG.match(segs[0]) and not (len(segs) >= 3 and _EVENTISH_SEG.search(" ".join(segs[1:]))):
            return 0, "non_event_url"
        if any(_NOT_EVENT_SEG.match(s) for s in segs[:-1]) and not _EVENTISH_SEG.search(segs[-1]):
            return 0, "non_event_url"
        if segs[-1].lower() in ("category", "categories", "tag", "tags", "feed", "rss") or "/category/" in full or "/tag/" in full:
            return 0, "non_event_url"
        if _NOT_EVENT_SEG.match(segs[-1]) and not _EVENT_QS.search("?" + p.query):
            return 0, "non_event_url"
    score = 0
    if segs:
        eventish_idx = [i for i, s in enumerate(segs) if _EVENTISH_SEG.search(s)]
        last = segs[-1]
        if eventish_idx:
            score += 2
            if len(segs) - 1 > eventish_idx[0]:       # something after the event-ish segment: an individual page
                score += 1
            if _EVENTISH_SEG.fullmatch(last.replace("-", " ").strip() or "x") or (len(segs) == 1 and _EVENTISH_SEG.search(last) and len(last) <= 12):
                score -= 2                             # '/events' itself, not an event
        if re.search(r"\d{3,}", last) or len([w for w in re.split(r"[-_]", last) if w]) >= 3:
            score += 1
        if _LISTING_PLURAL.search(last) and not re.search(r"\d{3,}", last):
            score -= 2                                 # '/branch-webinars', '/upcoming-events': a listing, not an event
        if len(segs) >= 2 and not eventish_idx:
            score += 0
    if _EVENT_QS.search("?" + p.query):
        score += 2
    if re.search(r"event[_-]?(?:display|detail|view|info)|eventdetails?|eventinfo", p.path, re.I):
        score += 2
    if ext_event_host:
        score += 3 if not same_host else 1
    if same_host and not segs:
        return 0, "site_root"
    if not same_host and not ext_event_host:
        score -= 1
    return score, ""


def group_key(href_abs: str) -> str:
    base, _, q = norm_url(href_abs).partition("?")
    base = _SUFFIX_SEG.sub("", base)
    return base + (f"?{q}" if q else "")


def _ancestors(el: Tag) -> list[Tag]:
    return [p for p in el.parents if isinstance(p, Tag) and p.name not in ("[document]",)]


def _lca(anchors: list[Tag]) -> Tag | None:
    chains = [[a] + _ancestors(a) for a in anchors]
    common = chains[0]
    for ch in chains[1:]:
        ids = {id(x) for x in ch}
        common = [x for x in common if id(x) in ids]
    return common[0] if common else None


# ---- card boundary ---------------------------------------------------------------------------------

def _grow_card(start: Tag, anchor_group: dict[int, int], gid: int) -> Tag:
    """Largest ancestor of `start` holding no link of another unit and still card-sized."""
    card = start
    while True:
        parent = card.parent
        if parent is None or not isinstance(parent, Tag) or parent.name in ("body", "html", "main", "form", "header", "[document]"):
            break
        if len(_text(parent)) > MAX_CARD_TEXT:
            break
        if parent.name in ("ul", "ol", "tbody", "table", "dl", "thead") and len(parent.find_all(["li", "tr", "dt"], recursive=False)) > 1:
            break
        if any(anchor_group.get(id(a), gid) != gid for a in parent.find_all("a", href=True)):
            break
        if parent.find("h1") is not None and card.find("h1") is None:
            break
        if len(parent.find_all(HEADINGS)) > max(4, len(card.find_all(HEADINGS)) + 2):
            break
        card = parent
    return card


def _title_candidates(card: Tag, anchors: list[Tag]) -> list[tuple[str, str]]:
    cands: list[tuple[str, str]] = []
    for h in card.find_all(HEADINGS):
        t = clean_title(_text(h))
        if t:
            cands.append((t, "heading"))
    for el in card.find_all(class_=TITLE_CLASS):
        if el.name in HEADINGS:
            continue
        t = clean_title(_text(el))
        if t and len(t) <= 220:
            cands.append((t, "class_title"))
    for a in anchors:
        for src, val in (("anchor", _text(a)), ("anchor_attr", a.get("aria-label") or ""), ("anchor_attr", a.get("title") or "")):
            val = clean_title(val)
            if val:
                cands.append((val, src))
        img = a.find("img", alt=True)
        if img and img["alt"].strip():
            cands.append((clean_title(img["alt"]), "img_alt"))
    # text-line fallback: the first substantial line that is not a date / CTA
    for s in card.stripped_strings:
        t = normalize_ws(s)
        if len(t) >= 8 and not CTA_TEXT.match(t) and not any(h.valid for h in find_dates(t)) and len(t) <= 200:
            cands.append((t, "line"))
            break
    return cands


def pick_title(card: Tag, anchors: list[Tag], locale: str = "dmy", org_name: str = "") -> tuple[str, str, str, list[str]]:
    """-> (title, confidence, raw, notes). A generic title lowers confidence; it never discards the card."""
    notes: list[str] = []
    cands = _title_candidates(card, anchors)
    generic_fallback = None
    for text, src in cands:
        if CTA_TEXT.match(text):
            continue
        cleaned = strip_date_affix(text, locale)
        if len(cleaned) > 200:
            cleaned = cleaned[:200].rsplit(" ", 1)[0]
        if not cleaned or CTA_TEXT.match(cleaned):
            continue
        if is_generic_title(cleaned, org_name):
            generic_fallback = generic_fallback or (cleaned, text, src)
            continue
        conf = "high" if src in ("heading", "class_title") and len(cleaned) >= 8 else "medium"
        if cleaned != text:
            notes.append(f"date removed from title ('{text}')")
        return cleaned, conf, text, notes
    if generic_fallback:
        cleaned, text, src = generic_fallback
        notes.append(f"only a generic title found ('{cleaned}' from {src}); detail page must supply the real one")
        return cleaned, "low", text, notes
    return "", "low", "", ["no title text in card"]


def _card_dates(text: str, locale: str) -> list[DateHit]:
    out = []
    for h in find_dates(text, locale):
        if not (h.valid or h.partial):
            continue
        if date_context_is_deadline(text, h):
            continue
        out.append(h)
    return out


_LOC_LABEL = re.compile(r"(?:venue|location|where|place|held at)\s*[:\-–]\s*([^|•\n]{3,120})", re.I)


def location_hint(text: str) -> str:
    m = _LOC_LABEL.search(text or "")
    return normalize_ws(m.group(1)) if m else ""


def section_of(card: Tag, claimed: set[int]) -> str:
    """Text of the nearest section heading above the card that is not itself a card title."""
    n = 0
    for h in card.find_all_previous(HEADINGS + ["legend", "summary", "caption"]):
        n += 1
        if n > 600:
            break
        if id(h) in claimed or any(id(p) in claimed for p in h.parents):
            continue
        t = _text(h)
        if t and len(t) <= 80:
            return t
    # class-labelled section titles ('tab-title', 'section-heading') that are not headings
    for el in card.find_all_previous(class_=re.compile(r"(?:section|list|block|tab|group)[-_ ]?(?:title|heading|header|label)|tab-title|nav-link", re.I), limit=12):
        if any(id(p) in claimed for p in el.parents):
            continue
        t = _text(el)
        if t and len(t) <= 60:
            return t
    return ""


def in_archive_section(card: Tag, section: str) -> bool:
    for anc in [card] + _ancestors(card)[:8]:
        ident = " ".join(anc.get("class") or []) + " " + (anc.get("id") or "")
        if ident.strip() and _ARCHIVE_ATTR.search(ident):
            return True
    if section and _ARCHIVE_HEAD.search(section) and not _UPCOMING_HEAD.search(section):
        return True
    return False


# ---- main entry --------------------------------------------------------------------------------------

STRONG_LINK = 2          # a link needs this URL score to define an event unit on its own


def _date_only_elements(soup, claimed: set[int], locale: str) -> list[tuple[Tag, str]]:
    """Short elements (a <dt>, a day heading, a date line) that are almost nothing but a date and sit outside every card."""
    out, seen = [], set()
    body = soup.body or soup
    for node in body.find_all(string=True):
        if not isinstance(node, NavigableString) or not str(node).strip():
            continue
        parent = node.parent
        if parent is None or parent.name in _SKIP_TEXT_PARENTS or id(parent) in seen:
            continue
        if any(id(p) in claimed for p in [parent] + _ancestors(parent)):
            continue
        txt = _text(parent)
        if not txt or len(txt) > 70:
            continue
        hits = [h for h in _card_dates(txt, locale) if h.valid or h.partial]
        if hits and (hits[0].span[1] - hits[0].span[0]) >= len(txt) * 0.45:
            seen.add(id(parent))
            out.append((parent, hits[0].raw))
    return out


def _assign_loose_dates(cards: list[Card], date_els: list[tuple[Tag, str]], pos: dict[int, int]) -> None:
    """Give dateless cards the date written next to them -- above OR below -- by learning, from the page itself,
    which side this site puts its dates on. A date line that follows each title (CIOT) would otherwise be
    attributed to the NEXT event; a day heading above a group of cards (calendar views) needs the opposite.

    pattern 'after'  : card, date, card, date ...        (each date belongs to the card before it)
    pattern 'before' : date, card, [card], date, card    (each date belongs to the card(s) after it)
    The pattern with more directly-adjacent pairs wins; a tie assigns nothing (no guessing)."""
    dateless = [c for c in cards if not c.date_hint and c.element is not None]
    if not dateless or not date_els:
        return
    seq = sorted([("c", pos.get(id(c.element), 0), c) for c in cards if c.element is not None] +
                 [("d", pos.get(id(e), 0), (e, raw)) for e, raw in date_els], key=lambda t: t[1])
    after_pairs, before_pairs = [], []
    for i, (kind, _, obj) in enumerate(seq):
        if kind == "c" and i + 1 < len(seq) and seq[i + 1][0] == "d" and not obj.date_hint:
            after_pairs.append((obj, seq[i + 1][2][1]))
        if kind == "d" and i + 1 < len(seq) and seq[i + 1][0] == "c" and not seq[i + 1][2].date_hint:
            before_pairs.append((seq[i + 1][2], obj[1]))
    if len(after_pairs) == len(before_pairs):
        return
    if len(after_pairs) > len(before_pairs):
        for card, raw in after_pairs:
            card.date_hint, card.date_inherited = raw, True
            card.notes.append("date taken from the line after the card (this page puts the date below the title)")
    else:
        last_date = None
        for kind, _, obj in seq:
            if kind == "d":
                last_date = obj[1]
            elif not obj.date_hint and last_date:
                obj.date_hint, obj.date_inherited = last_date, True
                obj.notes.append("date taken from the heading above the card (this page puts the date above the title)")


def _merge_stubs(cards: list[Card], page_url: str) -> None:
    """A second link inside one event's card (an external venue/registration link next to a 'Read more') becomes
    its own unit and, being tiny, would otherwise surface as a title-less duplicate. Fold such stubs into the
    neighbouring card they share a small ancestor with; prefer the site's own page as the event URL."""
    stubs = [c for c in cards if c.element is not None and not c.date_hint and not c.title and len(c.card_text) <= 40]
    for x in stubs:
        best, best_len = None, None
        for y in cards:
            if y is x or y.element is None or y in stubs:
                continue
            lca = _lca([x.element, y.element])
            if lca is None:
                continue
            n = len(_text(lca))
            if n <= MAX_CARD_TEXT and (best_len is None or n < best_len):
                inside = [z for z in cards if z.element is not None and (z.element is lca or lca in _ancestors(z.element))]
                if len(inside) <= 2:
                    best, best_len = y, n
        if best is None:
            continue
        if same_site(x.url, page_url) and not same_site(best.url, page_url):
            best.alt_urls.append(best.url)
            best.url = x.url
        else:
            best.alt_urls.append(x.url)
        best.notes.append(f"folded in the card's second link ({x.url})")
        x.title = ""
        x.methods = ["merged_stub"]
    cards[:] = [c for c in cards if c.methods != ["merged_stub"]]


def _heading_link(info: dict) -> bool:
    """A link whose text IS a title (inside an h1-h6 / title-classed element) defines an event unit even when its
    URL says nothing ('/OMB-Private-Client-Tax-Update'); satellites (maps, calendar, speakers) are not like that."""
    if info["score"] < 0:
        return False
    text = info["text"]
    if len(text) < 6 or CTA_TEXT.match(text):
        return False
    a = info["a"]
    for anc in [a] + _ancestors(a)[:3]:
        if anc.name in HEADINGS:
            return True
        if anc.name not in ("html", "body") and TITLE_CLASS.search(" ".join(anc.get("class") or [])):
            return True
    return False


def find_cards(html: str, page_url: str, locale: str = "dmy", org_name: str = "", today: date | None = None) -> tuple[list[Card], list[LinkDrop], dict]:
    """-> (cards, dropped_links, stats). Never raises on odd markup; each unit is built independently."""
    soup = prepare_soup(html)
    stats = {"anchors": 0, "nav_links": 0, "groups": 0, "text_date_cards": 0, "weak_links": 0}
    drops: list[LinkDrop] = []
    pos = {id(t): i for i, t in enumerate(soup.find_all(True))}

    # 1. classify every anchor
    infos: list[dict] = []
    for a in soup.find_all("a", href=True):
        stats["anchors"] += 1
        try:
            href = absolute_url(page_url, a["href"])
            if not href:
                continue
            text = _text(a) or (a.get("aria-label") or "") or (a.get("title") or "")
            score, reject = classify_link(href, page_url, text)
            infos.append({"a": a, "href": href, "score": score, "reject": reject, "text": text})
        except Exception:  # noqa: BLE001 - one odd anchor never stops discovery
            continue
    by_id = {id(i["a"]): i for i in infos}
    stats["nav_links"] = sum(1 for i in infos if i["reject"])

    # 2. strong links define event units; weak links (score < 2) are satellites that never block a card from growing
    groups: dict[str, list[Tag]] = {}
    order: list[str] = []
    for info in infos:
        if info["reject"] or not (info["score"] >= STRONG_LINK or _heading_link(info)):
            if not info["reject"]:
                stats["weak_links"] += 1
            continue
        key = group_key(info["href"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(info["a"])
    anchor_group = {id(a): gid for gid, key in enumerate(order) for a in groups[key]}

    # 3. one card per unit
    claimed: set[int] = set()
    cards: list[Card] = []
    for gid, key in enumerate(order):
        anchors = groups[key]
        best = max(by_id[id(a)]["score"] for a in anchors)
        try:
            lca = _lca(anchors)
            if lca is None:
                continue
            card_el = _grow_card(lca, anchor_group, gid)
            shared = len(anchors) > 1 and (len(_text(card_el)) > MAX_CARD_TEXT or card_el.name in ("body", "html", "main"))
            units = [[a] for a in anchors] if shared else [anchors]
            for unit in units:
                el = _grow_card(unit[0], anchor_group, gid) if shared else card_el
                claimed.add(id(el))
                claimed.update(id(d) for d in el.descendants if isinstance(d, Tag))
                text = _text(el)
                title, tconf, traw, tnotes = pick_title(el, unit, locale, org_name)
                dates = _card_dates(text, locale)
                own_date = dates[0].raw if dates else ""
                href = by_id[id(unit[0])]["href"]
                cards.append(Card(
                    element=el, anchors=unit, url=href, title=title, title_confidence=tconf if title else "low",
                    title_raw=traw, card_text=text[:900], date_hint=own_date, date_inherited=False,
                    location_hint=location_hint(text), in_archive=False, section="",
                    methods=["card"], shared_url=shared, link_score=best, notes=list(tnotes)))
        except Exception as exc:  # noqa: BLE001
            drops.append(LinkDrop("card_error", anchors[0].get("href", ""), "", f"{type(exc).__name__}: {exc!s:.100}"))

    # sections/archive flags need EVERY card claimed first (a card title must not be mistaken for a section heading)
    for c in cards:
        c.section = section_of(c.element, claimed)
        c.in_archive = in_archive_section(c.element, c.section)

    # 4. dates written beside (not inside) cards, then fold title-less CTA stubs into their neighbour
    _assign_loose_dates(cards, _date_only_elements(soup, claimed, locale), pos)
    _merge_stubs(cards, page_url)

    # 5. acceptance: a link alone is not an event
    accepted: list[Card] = []
    for c in cards:
        has_heading = c.element is not None and c.element.find(HEADINGS) is not None
        good_title = bool(c.title) and c.title_confidence != "low"
        own_date = bool(c.date_hint) and not c.date_inherited
        if own_date and c.title:
            ok = True                                   # a date written in the card + a title
        elif c.date_inherited and c.link_score >= 1 and good_title:
            ok = True                                   # date learned from the page's own layout
        elif c.link_score >= 4 and good_title:
            ok = True                                   # no date shown, but the URL is clearly an individual event page
        else:
            ok = False
        if ok:
            accepted.append(c)
        else:
            drops.append(LinkDrop("weak_link_no_event_evidence", c.url, c.title,
                                  f"url score {c.link_score}, title confidence {c.title_confidence}, "
                                  f"{'inherited' if c.date_inherited else 'no'} date -- not enough to call it an event"))
    stats["groups"] = len(accepted)

    # 6. link-less rows that carry a date (their link, if any, is recovered from inside the row)
    for card in _text_date_cards(soup, set(claimed), locale, org_name, page_url, by_id):
        accepted.append(card)
        stats["text_date_cards"] += 1
    accepted.sort(key=lambda c: pos.get(id(c.element), 10**9) if c.element is not None else 10**9)
    return accepted, drops, stats


def _text_date_cards(soup: BeautifulSoup, claimed: set[int], locale: str, org_name: str, page_url: str, by_id: dict) -> list[Card]:
    out: list[Card] = []
    used: set[int] = set()
    body = soup.body or soup
    for node in body.find_all(string=True):
        if not isinstance(node, NavigableString) or not str(node).strip():
            continue
        parent = node.parent
        if parent is None or parent.name in _SKIP_TEXT_PARENTS:
            continue
        if any(id(p) in claimed for p in [parent] + _ancestors(parent)) or any(p.name in _SKIP_TEXT_PARENTS for p in parent.parents if isinstance(p, Tag)):
            continue
        txt = normalize_ws(str(node))
        if not [h for h in _card_dates(txt, locale) if h.valid]:
            continue
        el = parent
        while True:
            up = el.parent
            if up is None or not isinstance(up, Tag) or up.name in ("body", "html", "main", "form"):
                break
            t_up = _text(up)
            if len(t_up) > 500 or id(up) in claimed:
                break
            if len({(h.start, h.end) for h in _card_dates(t_up, locale) if h.valid}) > 1:
                break
            if any(id(x) in claimed for x in up.descendants if isinstance(x, Tag)):
                break
            el = up
            if len(_text(el)) >= 30 and el.name in ("li", "tr", "article", "dd"):
                break
        if id(el) in used or id(el) in claimed:
            continue
        text = _text(el)
        stripped = text
        for h in find_dates(text, locale):
            stripped = stripped.replace(h.raw, " ")
        if len(normalize_ws(stripped)) < 10 or len(text) > 500:
            continue
        used.add(id(el))
        title, tconf, traw, notes = pick_title(el, [], locale, org_name)
        if not title:
            continue
        url = ""
        for a in el.find_all("a", href=True):
            info = by_id.get(id(a))
            if info and not info["reject"]:
                url = info["href"]
                break
        dates = _card_dates(text, locale)
        section = section_of(el, claimed)
        out.append(Card(element=el, anchors=[], url=url, title=title, title_confidence="low" if tconf != "high" else "medium",
                        title_raw=traw, card_text=text[:900], date_hint=dates[0].raw if dates else "", date_inherited=False,
                        location_hint=location_hint(text), in_archive=in_archive_section(el, section), section=section,
                        methods=["text_date"], notes=notes + ["found from a dated text row" + ("" if url else " (no link in the row)")]))
    return out
