"""Label/value pairs from an event page, with the label->value association preserved.

v1 took the FIRST "Date"/"Venue"/"Address" label anywhere in <main>, so another
event's date, a sidebar block or the organisation's contact address became the
event's own field. Here every pair carries where it came from (its section
heading, whether it sits in a contact/organiser block) and its position, so
later stages can choose by association -- never by 'first label found'.

Pair sources: dl/dt+dd, table rows, 'Label: value' inline text (several labels
in one line are split), label element + value sibling (<strong>Date</strong>
<span>..</span>, <div class=label>/<div class=value>, <h4>Venue</h4><p>..), and
icon rows (<i class="fa-calendar"> + text) which carry only a class hint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

from pipeline.common import normalize_ws

HEADS = ["h1", "h2", "h3", "h4", "h5", "h6"]

# canonical label families
_LABELS = [
    ("registration_deadline", r"regist\w*(\s+(?:closes?|close|deadline|closing|by|ends?|opens?))?|deadline|early[\s-]*bird(\s+\w+){0,3}|last\s+date\s+to\s+(?:register|apply)|booking\s+(?:closes?|deadline)|rsvp(\s+by)?|cut-?off(\s+date)?|abstract\w*(\s+\w+)?|nominat\w+(\s+\w+)?|applications?\s+(?:close|due)|due\s+date"),
    ("start_date", r"(?:event\s+|course\s+)?start(?:s|ing)?(?:\s+(?:date|on|day))?|begins?(?:\s+on)?|starts\s+on|from(?:\s+date)?"),
    ("end_date", r"(?:event\s+|course\s+)?end(?:s|ing)?(?:\s+(?:date|on|day))?|finish(?:es)?(?:\s+on)?|ends\s+on|until|to\s+date"),
    ("date", r"(?:event\s+|session\s+|course\s+|conference\s+)?dates?(?:\s*(?:&|and|/)\s*times?)?|date\s*(?:and|&)\s*time|when|day(?:s)?|schedule|dates?\s+and\s+venue"),
    ("time", r"(?:event\s+|start\s+)?times?|timings?|hours|session\s+times?"),
    ("venue", r"(?:event\s+)?(?:venue|location|place|where|held\s+at|meeting\s+(?:point|place)|location\s+details?)"),
    ("address", r"(?:venue\s+|street\s+|full\s+|postal\s+)?address|location\s+address"),
    ("city", r"city|town|suburb"),
    ("region", r"state|province|region|county"),
    ("country", r"country"),
    ("format", r"(?:event\s+)?(?:mode|format|attendance|delivery(?:\s+(?:mode|method))?|event\s+type|type\s+of\s+event|how|participation)(?:\s+of\s+(?:event|delivery|attendance))?|mode\s+of\s+\w+"),
    ("organiser", r"organi[sz]ers?|organi[sz]ed\s+by|hosted\s+by|presented\s+by|host|presenter|convenor|event\s+host|conducted\s+by"),
    ("cost", r"(?:cost|fees?|price|pricing|ticket(?:s)?|admission|registration\s+fee)"),
    ("speaker", r"speakers?|faculty|presenters?|panel(?:l?ists)?|trainers?"),
    ("cpd", r"cpd|cpe|credits?|points?|hours\s+of\s+cpd"),
]
_LABEL_RES = [(name, re.compile(rf"^(?:{pat})$", re.I)) for name, pat in _LABELS]
# known labels inside a longer run of text:  "Date: 12 Oct 2026 Venue: Hilton"
_RUN_LABEL = re.compile(
    r"(?<![A-Za-z])(?P<label>(?:event\s+|start\s+|end\s+|course\s+)?dates?(?:\s*(?:&|and)\s*times?)?|when|times?|venue|location|where|address|"
    r"city|country|mode|format|organi[sz]er|hosted\s+by|presented\s+by|registration\s+(?:closes?|deadline|closing|opens?)|deadline|"
    r"early[\s-]*bird[^:]{0,20}|last\s+date\s+to\s+register|cost|fees?|price)\s*:", re.I)
_INLINE = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z &/']{1,32}?)\s*[:：]\s*(?P<value>\S.{0,400})$")
_NOT_STATED = re.compile(r"^(tba|tbd|tbc|to be (announced|confirmed|decided|advised)|n/?a|-+|not (yet )?(announced|available))$", re.I)

CONTACT_CTX = re.compile(
    r"contact|enquir|inquir|get\s+in\s+touch|find\s+us|head\s+office|our\s+office|reach\s+us|organi[sz]er'?s?\s+(details|contact)|"
    r"about\s+(us|the\s+organi[sz]er|the\s+institute|the\s+society|the\s+association)|footer|copyright|follow\s+us|newsletter|subscribe|"
    r"office\s+hours|registered\s+office|postal", re.I)
AGENDA_CTX = re.compile(r"programme?|agenda|schedule|timetable|itinerary|sessions?|speakers?|day\s*\d|workshops?\s+\d|streams?|breakout|keynote", re.I)
AGENDA_ATTR = re.compile(r"(?:^|[\s_-])(agenda|programme?|schedule|timetable|sessions?|speakers?|itinerary)(?:$|[\s_-])", re.I)
CONTACT_ATTR = re.compile(r"(?:^|[\s_-])(contact|enquir\w*|inquir\w*|footer|office|organi[sz]er-(?:details|contact)|address-block|vcard|org-info)(?:$|[\s_-])", re.I)
ICON_HINTS = [("date", r"calendar|date|event-date|fa-calendar|icon-calendar|cal\b"), ("time", r"clock|time|fa-clock"),
              ("venue", r"map-marker|location|pin|venue|fa-map|marker|geo")]


def canon_label(label: str) -> str:
    l = normalize_ws(label).rstrip(":：").strip()
    for name, rx in _LABEL_RES:
        if rx.match(l):
            return name
    return "other"


@dataclass
class Pair:
    label: str
    canon: str
    value: str
    source: str            # dl | table | inline | sibling | icon
    section: str = ""      # nearest heading above
    contact: bool = False  # sits in a contact / organiser-details / footer block
    idx: int = 0
    evidence: str = ""
    near_title: bool = False   # appears within the first block after the page's main heading
    agenda: bool = False       # sits in a programme / agenda / speaker block: session-level info, not the event's own

    def to_dict(self) -> dict:
        return {"label": self.label, "canon": self.canon, "value": self.value, "source": self.source, "section": self.section,
                "contact": self.contact, "agenda": self.agenda, "idx": self.idx}


def _text(el) -> str:
    return normalize_ws(el.get_text(" ", strip=True)) if el is not None else ""


def _section_of(el: Tag) -> str:
    h = el.find_previous(HEADS)
    return _text(h)[:80] if h is not None else ""


_SESSION_MARK = re.compile(r"\b(?:speakers?|panell?ists?|moderators?|chair(?:ed)?|presenters?|keynote|session\s*\d+|workshop\s*\d+|breakout)\s*(?::|-|–)", re.I)


def _session_block(el: Tag) -> bool:
    """True when the pair sits in a small block that also names speakers/sessions: a programme entry, not the event itself."""
    cur = el
    for _ in range(4):
        if cur.parent is None or not isinstance(cur.parent, Tag) or cur.parent.name in ("body", "main", "html", "article"):
            break
        cur = cur.parent
        t = normalize_ws(cur.get_text(" ", strip=True))
        if len(t) > 1500:
            break
        if _SESSION_MARK.search(t):
            return True
    return False


_PANEL_ITEM = re.compile(r"accordion[-_]*item|accordion__item|tab-?pane|tabpanel|collaps|panel-?body|toggle-?content|cmp-accordion__panel", re.I)
_EVENT_PANEL_TITLE = re.compile(r"detail|info|overview|when|where|date|venue|location|about|general|summary|logistic|key\s+facts", re.I)


def _in_secondary_panel(el: Tag) -> bool:
    """Inside an accordion/tab panel whose title is not an 'event details'-type title (e.g. 'Networking function',
    'Day 2', 'Workshop A'): that panel describes a part of the event, not the event."""
    for anc in [p for p in el.parents if isinstance(p, Tag)][:10]:
        ident = " ".join(anc.get("class") or []) + " " + (anc.get("id") or "")
        if not _PANEL_ITEM.search(ident):
            continue
        item = anc
        # climb to the accordion ITEM (the element that holds both the header and the panel)
        for up in [anc] + [p for p in anc.parents if isinstance(p, Tag)][:3]:
            if up.find(["h2", "h3", "h4", "h5", "h6", "button", "summary"]) is not None and \
                    re.search(r"item|accordion|tab|panel", " ".join(up.get("class") or []), re.I):
                item = up
                break
        head = item.find(["h2", "h3", "h4", "h5", "h6", "button", "summary"])
        title = normalize_ws(head.get_text(" ", strip=True)) if head is not None else ""
        if title and not _EVENT_PANEL_TITLE.search(title):
            return True
    return False


def _agenda_context(el: Tag, section: str) -> bool:
    if _session_block(el) or _in_secondary_panel(el):
        return True
    if section and AGENDA_CTX.search(section):
        return True
    for anc in [el] + [p for p in el.parents if isinstance(p, Tag)][:6]:
        ident = " ".join(anc.get("class") or []) + " " + (anc.get("id") or "")
        if ident.strip() and AGENDA_ATTR.search(ident):
            return True
    return False


def _contact_context(el: Tag, section: str) -> bool:
    if section and CONTACT_CTX.search(section):
        return True
    for anc in [el] + [p for p in el.parents if isinstance(p, Tag)][:6]:
        ident = " ".join(anc.get("class") or []) + " " + (anc.get("id") or "")
        if ident.strip() and CONTACT_ATTR.search(ident):
            return True
        if anc.name in ("address", "footer"):
            return True
        if anc.get("itemtype") and re.search(r"Organization|LocalBusiness|PostalAddress", anc.get("itemtype", "")):
            return True
    return False


def _split_run(text: str) -> list[tuple[str, str]]:
    marks = list(_RUN_LABEL.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        value = text[m.end():end].strip(" ,;|-–—•")
        if value:
            out.append((m.group("label").strip(), value))
    return out


def collect_pairs(container: Tag) -> list[Pair]:
    """Every label/value pair in the container, in document order, de-duplicated by (label, value)."""
    raw: list[tuple[int, str, str, str, Tag]] = []
    pos = {id(t): i for i, t in enumerate(container.find_all(True))}

    def add(el: Tag, label: str, value: str, source: str):
        label = normalize_ws(label).rstrip(":：").strip()
        value = re.sub(r"^[\s\-–—:：•|]+|[\s\-–—:：•|]+$", "", normalize_ws(value))
        if not label or not value or len(label) > 40 or len(value) > 500 or _NOT_STATED.match(value):
            return
        raw.append((pos.get(id(el), 0), label, value, source, el))

    for dt in container.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            add(dt, _text(dt), _text(dd), "dl")
    for tr in container.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2 and len(_text(cells[0])) <= 40:
            add(tr, _text(cells[0]), _text(cells[1]), "table")
    leafish = [e for e in container.find_all(["p", "li", "div", "span", "td", "dd", "section", "h2", "h3", "h4", "h5", "h6", "label", "strong", "b"])
               if not e.find(["p", "li", "div", "table", "ul", "ol", "section", "h2", "h3", "h4", "h5", "h6", "dl"])]
    for el in leafish:
        text = _text(el)
        if not text:
            continue
        run = _split_run(text)
        if run:
            for lab, val in run:
                add(el, lab, val, "inline")
            continue
        m = _INLINE.match(text)
        if m and canon_label(m.group("label")) != "other":
            add(el, m.group("label"), m.group("value"), "inline")
            continue
        # label element followed by its value: <strong>Date</strong><span>..</span>, <h4>Venue</h4><p>..</p>, label/value divs
        label_txt = text.rstrip(":：").strip()
        if len(label_txt) <= 32 and canon_label(label_txt) != "other" and not re.search(r"\d{3,}", label_txt):
            nxt = el.next_sibling
            while nxt is not None and isinstance(nxt, NavigableString) and not nxt.strip():
                nxt = nxt.next_sibling
            value = ""
            if isinstance(nxt, Tag):
                value = _text(nxt)
            elif isinstance(nxt, NavigableString):
                value = normalize_ws(str(nxt))
            if not value and isinstance(el.parent, Tag):          # label inside a wrapper whose next sibling holds the value
                par_next = el.parent.find_next_sibling()
                if par_next is not None and len(_text(el.parent)) <= 40:
                    value = _text(par_next)
            if value and canon_label(value.rstrip(":：")) == "other":
                add(el, label_txt, value, "sibling")
    # icon rows: <i class="fa fa-calendar"></i> 12 October 2026
    for icon in container.find_all(["i", "svg", "span", "img"]):
        cls = " ".join(icon.get("class") or []) + " " + (icon.get("aria-label") or "") + " " + (icon.get("alt") or "")
        if not cls.strip():
            continue
        for canon, pat in ICON_HINTS:
            if re.search(pat, cls, re.I):
                holder = icon.parent
                value = _text(holder)
                if holder is not None and 3 <= len(value) <= 200 and not (canon == "date" and not re.search(r"\d", value)):
                    add(icon, canon, value, "icon")
                break

    raw.sort(key=lambda r: r[0])
    pairs: list[Pair] = []
    seen = set()
    first_h = container.find(HEADS)
    for idx, (_, label, value, source, el) in enumerate(raw):
        key = (label.lower(), value.lower())
        if key in seen:
            continue
        seen.add(key)
        section = _section_of(el)
        near = False
        if first_h is not None:
            # within ~12 elements after the first heading counts as the event's header block
            near = 0 <= pos.get(id(el), 0) - pos.get(id(first_h), 0) <= 40
        pairs.append(Pair(label=label, canon=canon_label(label), value=value, source=source, section=section,
                          contact=_contact_context(el, section), idx=idx, evidence=f"{label}: {value}", near_title=near,
                          agenda=_agenda_context(el, section)))
    return pairs
