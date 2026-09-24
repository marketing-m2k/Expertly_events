"""Priority-2 extraction source: labelled details on an event's own page
("Date:", "Venue:", "Mode: Online", a <time> tag, the page's main heading).

The value is only ever taken from next to a label that means what the column
means. Labels that look like dates but aren't the event date (registration
deadline, posted on, early bird...) are recorded as ignored and never used.
No AI, no cost.
"""

import re

from bs4 import BeautifulSoup

from scraper.date_utils import format_display_date, parse_strict_date_range
from scraper.evidence import field, normalize_ws
from scraper.heuristic_extract import GENERIC_HEADINGS

_DATE_LABEL = re.compile(
    r"^(event\s+|start\s+)?dates?(\s*(&|and|/)\s*times?)?$|^when$|^date\s*&\s*time$|^event\s+dates?$", re.I)
_NOT_EVENT_DATE = re.compile(
    r"regist|deadline|last\s+date|early\s*bird|posted|publish|updated|modified|clos(e|ing)|"
    r"\bdue\b|submi|cut-?off|abstract|nominat", re.I)
_VENUE_LABEL = re.compile(r"^(event\s+)?(venue|location|where|address|place)$", re.I)
_MODE_LABEL = re.compile(
    r"^(event\s+)?(mode|format|attendance|delivery)(\s+of\s+(event|delivery|attendance))?$|^mode\s+of\s+\w+$", re.I)

_INLINE_PAIR = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z &/]{1,30}?)\s*[:：]\s*(?P<value>\S.{0,250})$")
_NOT_STATED = re.compile(r"^(tba|tbd|tbc|to be (announced|confirmed|decided)|n/?a|-+)$", re.I)
_BOILERPLATE = re.compile(
    r"cookie|privacy policy|subscribe|newsletter|all rights reserved|javascript|sign in|log in|"
    r"terms (of|and) (use|conditions)", re.I)


def _clean_scope(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside", "form", "noscript", "iframe"]):
        tag.decompose()
    return soup


def _main_scope(soup: BeautifulSoup):
    return soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article") or soup.body or soup


def _collect_pairs(scope) -> list[tuple[str, str]]:
    pairs = []
    for dt in scope.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.append((normalize_ws(dt.get_text(" ", strip=True)).rstrip(":"), normalize_ws(dd.get_text(" ", strip=True))))
    for row in scope.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            pairs.append((normalize_ws(cells[0].get_text(" ", strip=True)).rstrip(":"),
                          normalize_ws(cells[1].get_text(" ", strip=True))))
    for el in scope.find_all(["p", "li", "div", "td", "dd", "span"]):
        if el.find(["p", "li", "div", "table", "ul", "ol"]):
            continue
        m = _INLINE_PAIR.match(normalize_ws(el.get_text(" ", strip=True)))
        if m:
            pairs.append((m.group("label").strip(), m.group("value").strip()))
    seen, out = set(), []
    for label, value in pairs:
        key = (label.lower(), value.lower())
        if label and value and key not in seen:
            seen.add(key)
            out.append((label, value))
    return out


def _mode_from_text(text: str) -> str:
    t = text.lower()
    if "hybrid" in t:
        return "Hybrid"
    if re.search(r"\b(online|virtual|webinar|zoom|teams|webex|livestream)\b", t):
        return "Virtual"
    if re.search(r"\b(in[\s-]?person|physical|on[\s-]?site|offline)\b", t):
        return "In Person"
    return ""


def is_generic_title(title: str, org_name: str = "") -> bool:
    t = normalize_ws(title).lower()
    if len(t) < 4 or t.isdigit() or t in GENERIC_HEADINGS:
        return True
    return bool(org_name) and t == normalize_ws(org_name).lower()


def _title_from_page(soup: BeautifulSoup, scope, org_name: str) -> dict | None:
    h1 = scope.find("h1")
    if h1:
        text = normalize_ws(h1.get_text(" ", strip=True))
        if text and not is_generic_title(text, org_name):
            return field(text, text, "Title from the page's main heading (h1)", "heading")
    og = soup.find("meta", attrs={"property": "og:title"})
    for source, raw, confidence in (
        ("meta", og.get("content", "") if og else "", "medium"),
        ("meta", soup.title.get_text() if soup.title else "", "low"),
    ):
        text = normalize_ws(raw)
        parts = re.split(r"\s+[|\-–—]\s+", text)
        if parts and len(parts[0]) >= 8:
            text = parts[0]
        if text and not is_generic_title(text, org_name):
            return field(text, text, "Title from the page's title tag (no main heading found)", source, confidence)
    return None


def _description_from_page(scope) -> dict | None:
    for p in scope.find_all("p"):
        text = normalize_ws(p.get_text(" ", strip=True))
        if len(text) >= 60 and not _BOILERPLATE.search(text):
            return field(text[:600], text[:60], "First full paragraph of the event page, copied word for word", "paragraph")
    return None


def extract_labelled(html: str, page_url: str, org_name: str = "") -> dict:
    """Returns {"fields": {column: evidence-field}, "ignored_labels": [...],
    "date_without_year": raw text or ""}."""
    soup = _clean_scope(html)
    scope = _main_scope(soup)
    fields: dict[str, dict] = {}
    ignored: list[str] = []
    date_without_year = ""

    title = _title_from_page(soup, scope, org_name)
    if title:
        fields["name"] = title

    for label, value in _collect_pairs(scope):
        if _DATE_LABEL.match(label) and "date" not in fields:
            start, end = parse_strict_date_range(value)
            if start:
                fields["date"] = field(format_display_date(start), value,
                                       f"Date taken from the '{label}' label", "label")
                if end and end.date() != start.date():
                    fields["end_date"] = field(format_display_date(end), value,
                                               f"End date taken from the '{label}' label", "label")
            elif not date_without_year:
                date_without_year = value
        elif _NOT_EVENT_DATE.search(label) and re.search(r"date|deadline|by|until|clos", label, re.I):
            ignored.append(label)
        elif _VENUE_LABEL.match(label) and "location" not in fields and not _NOT_STATED.match(value):
            mode = _mode_from_text(value)
            if mode == "Virtual" and len(value) < 40:
                fields.setdefault("format", field("Virtual", value, f"Format from the '{label}' label ({value})", "label"))
            elif not value.lower().startswith("http") and len(value) <= 150:
                fields["location"] = field(value, value, f"Venue taken from the '{label}' label", "label")
        elif _MODE_LABEL.match(label) and "format" not in fields:
            mode = _mode_from_text(value)
            if mode:
                fields["format"] = field(mode, value, f"Format taken from the '{label}' label", "label")

    if "date" not in fields:
        time_tag = scope.find("time", attrs={"datetime": True})
        if time_tag:
            raw = time_tag["datetime"]
            start, _ = parse_strict_date_range(raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw) else raw)
            if start:
                fields["date"] = field(format_display_date(start), raw,
                                       "Date from the page's <time> tag (no 'Date:' label found)", "time_tag", "medium")

    description = _description_from_page(scope)
    if description:
        fields["description"] = description

    return {"fields": fields, "ignored_labels": ignored, "date_without_year": date_without_year}


def visible_text(html: str) -> str:
    """All human-visible text on the page (navigation and footer removed)."""
    return normalize_ws(_clean_scope(html).get_text(" ", strip=True))


def main_text(html: str) -> str:
    """The text of the page's main content -- used to notice when an event's
    page has really changed (a footer clock or cookie banner doesn't count)."""
    soup = _clean_scope(html)
    return normalize_ws(_main_scope(soup).get_text(" ", strip=True))
