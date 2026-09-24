"""Read events straight from a list/calendar page, without AI, with proof.

The old list-page reader (heuristic_extract.py) kept only a guessed date and
threw the rest of each event card away. Most cards actually state the date,
time, venue and format in plain view (or in a <time> tag, or in structured
event data on the page). This reader keeps all of that, each value with its
proof text and reason, and refuses to guess:

  - a date needs a day, month AND year in the card (no year -> not stated)
  - dates near "register by / closes / deadline" wording are never the event date
  - two different dates in one card, or a card that disagrees with the page's
    structured data, means neither is used (the event goes to Needs Review)
  - format only from an explicit "Virtual"/"Online"/"In person" tag or label
"""

import re
from datetime import datetime

from bs4 import BeautifulSoup

from scraper.date_utils import format_display_date, parse_strict_date_range
from scraper.evidence import field, normalize_ws
from scraper.heuristic_extract import (
    _dedupe_nested, _find_repeated_card_groups, _find_title_and_link, _score_group, _strip_page_chrome,
)
from scraper.label_extract import _NOT_STATED, _mode_from_text, _split_labelled_run, is_generic_title
from scraper.structured_data import extract_structured_events

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_FULL_DATE = re.compile(
    rf"(?:{_DAY}\s*(?:[-–—]|to)\s*)?{_DAY}\s+{_MONTH}\.?,?\s+20\d\d"
    rf"|{_MONTH}\.?\s+{_DAY}(?:\s*[-–—]\s*{_DAY})?,?\s+20\d\d"
    r"|20\d\d-\d{2}-\d{2}", re.I)
_DEADLINE_BEFORE = re.compile(
    r"(regist\w*|deadline|clos(?:es|ing|ed)|early\s*bird|submit\w*|due|last\s+date|by|until|before)\W{0,12}$", re.I)
_VIRTUAL_TOKEN = re.compile(r"^(virtual|virtual event|online|online event|webinar|live webinar)$", re.I)
_INPERSON_TOKEN = re.compile(r"^(in[\s-]?person|in[\s-]?person event)$", re.I)
_HYBRID_TOKEN = re.compile(r"^hybrid( event)?$", re.I)


def _dates_in_text(text: str, today: datetime) -> list[tuple[datetime, datetime | None, str]]:
    """Every full (day+month+year) date in `text` that isn't next to deadline wording."""
    found = []
    for m in _FULL_DATE.finditer(text):
        if _DEADLINE_BEFORE.search(text[max(0, m.start() - 24):m.start()]):
            continue
        start, end = parse_strict_date_range(m.group(0), today)
        if start:
            found.append((start, end, m.group(0)))
    return found


def _card_date(card, card_text: str, today: datetime) -> tuple[dict, dict]:
    """(fields, info). fields may hold 'date'/'end_date'; info['conflict'] is
    set when the card's own dates disagree."""
    candidates: list[tuple[str, datetime, datetime | None, str]] = []  # (tier, start, end, proof)

    for label, value in _split_labelled_run(card_text):
        if re.fullmatch(r"(event\s+|start\s+)?dates?(\s*(&|and)\s*times?)?|when|date\s*&\s*time", label, re.I):
            start, end = parse_strict_date_range(value, today)
            if start:
                candidates.append(("label", start, end, value))
                break

    iso_days = {}
    for t in card.find_all("time", attrs={"datetime": True}):
        raw = t["datetime"].strip()
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            try:
                iso_days[datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))] = raw
            except ValueError:
                pass
    if iso_days:
        days = sorted(iso_days)
        candidates.append(("time", days[0], days[-1] if len(days) > 1 else None, iso_days[days[0]]))

    text_dates = _dates_in_text(card_text, today)
    if text_dates:
        distinct = {(s, e) for s, e, _ in text_dates}
        if len(distinct) == 1:
            s, e, raw = text_dates[0]
            candidates.append(("text", s, e, raw))
        else:
            return {}, {"conflict": "the card shows several different dates: " + ", ".join(t[2] for t in text_dates[:3])}

    if not candidates:
        return {}, {}
    starts = {c[1].date() for c in candidates}
    if len(starts) > 1:
        return {}, {"conflict": "the card's date sources disagree: " + ", ".join(f"{c[0]} {c[3]}" for c in candidates)}

    tiers = {c[0] for c in candidates}
    _, start, end, proof = next(c for c in candidates if c[0] == "label") if "label" in tiers else candidates[0]
    end = end or next((c[2] for c in candidates if c[2]), None)
    confident = "label" in tiers or len(tiers) > 1 or "text" in tiers
    reason = ("Date from the card's 'Date:' label" if "label" in tiers else
              f"Date agreed by {' and '.join(sorted(tiers))} in the event's card" if len(tiers) > 1 else
              "The only full date in the event's card, with no deadline wording" if "text" in tiers else
              "Date from the card's <time> tag")
    fields = {"date": field(format_display_date(start), proof, reason, "card", "high" if confident else "medium")}
    if end and end.date() != start.date():
        fields["end_date"] = field(format_display_date(end), proof, reason.replace("Date", "End date", 1), "card",
                                   "high" if confident else "medium")
    return fields, {}


def _card_format_and_location(card, card_text: str) -> dict:
    out = {}
    for label, value in _split_labelled_run(card_text):
        if re.fullmatch(r"(event\s+)?(venue|location|where|address|place)", label, re.I) and not _NOT_STATED.match(value):
            if _mode_from_text(value) == "Virtual" and len(value) < 40:
                out.setdefault("format", field("Virtual", value, f"Format from the card's '{label}' label", "card"))
            elif len(value) <= 150 and not value.lower().startswith("http") and "location" not in out:
                out["location"] = field(value, value, f"Venue from the card's '{label}' label", "card")
        elif re.fullmatch(r"(event\s+)?(mode|format|attendance)", label, re.I):
            mode = _mode_from_text(value)
            if mode:
                out.setdefault("format", field(mode, value, f"Format from the card's '{label}' label", "card"))
    if "format" not in out:
        for el in card.find_all(["span", "div", "p", "li", "small", "em", "strong"]):
            if el.find(["span", "div", "p", "li"]):
                continue
            token = normalize_ws(el.get_text(" ", strip=True))
            if len(token) <= 20:
                mode = ("Hybrid" if _HYBRID_TOKEN.match(token) else "Virtual" if _VIRTUAL_TOKEN.match(token)
                        else "In Person" if _INPERSON_TOKEN.match(token) else "")
                if mode:
                    out["format"] = field(mode, token, f"Format from the card's '{token}' tag", "card")
                    break
    return out


def extract_list_events(html: str, source_url: str, today: datetime | None = None) -> list[dict]:
    """Every event on a list page as {"fields", "link", "card_text", "conflict"}."""
    today = today or datetime.now()
    soup = BeautifulSoup(html, "html.parser")
    _strip_page_chrome(soup)

    events: list[dict] = []
    groups = [g for g in _find_repeated_card_groups(soup) if _score_group(g) >= 0.3]
    for group in _dedupe_nested(groups):
        for card in group:
            card_text = normalize_ws(card.get_text(" ", strip=True))
            title, link = _find_title_and_link(card, source_url)
            title = normalize_ws(title)
            if not title or is_generic_title(title):
                continue
            fields = {"name": field(title, title, "Title from the event's card heading/link on the list page", "card")}
            date_fields, info = _card_date(card, card_text, today)
            fields.update(date_fields)
            fields.update(_card_format_and_location(card, card_text))
            events.append({"fields": fields, "link": link, "card_text": card_text[:600],
                           "conflict": info.get("conflict", "")})

    # structured events the page itself lists (the site's own labelled data)
    structured = extract_structured_events(html, source_url)
    by_link = {e["link"].rstrip("/"): e for e in events if e["link"]}
    for s in structured:
        url = s.get("link", {}).get("value", "").rstrip("/")
        card_event = by_link.get(url)
        if card_event is None:
            events.append({"fields": s, "link": url, "card_text": "", "conflict": ""})
            continue
        for column, value in s.items():
            mine = card_event["fields"].get(column)
            if mine and column in ("date", "end_date", "format") and mine["value"] != value["value"]:
                card_event["conflict"] = card_event["conflict"] or (
                    f"{column}: the card says '{mine['value']}', the page's structured data says '{value['value']}'")
                card_event["fields"].pop(column, None)
            elif not mine or (mine["confidence"] != "high" and value["confidence"] == "high"):
                card_event["fields"][column] = value
    return events
