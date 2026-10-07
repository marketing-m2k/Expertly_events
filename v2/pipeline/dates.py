"""Strict date/time reading. Never guesses.

A returned date always has a day, a month and a year that are WRITTEN in the
text. A day+month without a year comes back as a *partial* hit (year_stated =
False, start = None) so the caller can mark the year unresolved instead of
inventing one. Numeric dates like 03/04/2026 are only resolved when the text
itself disambiguates them (a part > 12) or the caller supplies the site's
locale -- and an ambiguous reading is flagged so it is reviewed, not trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_M = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Sept|Sep|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Oct|Nov|Dec)"
_D = r"(?P<{n}>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)"
_Y = r"(?:19|20)\d{2}"
_SEP = r"\s*(?:-|–|—|\bto\b|\buntil\b|\bthrough\b|\bthru\b)\s*"
_WD = r"(?:(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)(?:day)?\.?,?\s+)?"

# an endpoint: "12 October 2026" | "12 October" | "October 12, 2026" | "October 12" | ISO | numeric
def _end(i: str) -> str:
    return (rf"(?:(?P<d{i}a>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:\s+(?:of\s+)?|[-/.])(?P<m{i}a>{_M})\b\.?(?:[,\s/.-]*(?P<y{i}a>{_Y})(?!\d))?"
            rf"|(?P<m{i}b>{_M})\b\.?\s+(?P<d{i}b>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(?P<y{i}b>{_Y})(?!\d))?"
            rf"|(?P<iso{i}>(?:19|20)\d{{2}}-\d{{2}}-\d{{2}})"
            rf"|(?P<n{i}x>\d{{1,2}})[/.\-](?P<n{i}y>\d{{1,2}})[/.\-](?P<n{i}z>{_Y})(?!\d))")


_RANGE_SAME_MONTH_DF = re.compile(
    rf"{_WD}(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?(?!\d){_SEP}(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)\s+(?:of\s+)?(?P<m>{_M})\b\.?(?:,?\s*(?P<y>{_Y})(?!\d))?", re.I)
_RANGE_SAME_MONTH_MF = re.compile(
    rf"{_WD}(?P<m>{_M})\b\.?\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?(?!\d){_SEP}(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?!\s*(?:{_M})\b)(?:,?\s*(?P<y>{_Y})(?!\d))?", re.I)
_RANGE_TWO = re.compile(rf"{_WD}{_end('1')}{_SEP}{_WD}{_end('2')}", re.I)
_SINGLE = re.compile(rf"{_WD}{_end('1')}", re.I)
_MONTH_YEAR = re.compile(rf"(?<![\w])(?P<m>{_M})\b\.?,?\s+(?P<y>{_Y})(?!\d)", re.I)

DEADLINE_WORDS = re.compile(
    r"(regist\w*|deadline|clos(?:es|ing|ed|e)|early[\s-]*bird|submi\w*|due|last\s+date|cut-?off|abstracts?|nominat\w*|"
    r"book(?:ing)?s?\s+(?:by|close)|apply\s+by|rsvp\s+by|expires?|"
    r"posted|published|updated|modified|last\s+updated|released|issued|copyright|©)\W{0,16}(?:on|by|before|until|:|-|–)?\W{0,6}$", re.I)
_BY_UNTIL = re.compile(r"\b(?:by|until|before)\s*$", re.I)


@dataclass
class DateHit:
    start: date | None
    end: date | None
    raw: str
    span: tuple[int, int]
    year_stated: bool = True
    ambiguous: bool = False       # numeric day/month order was not self-evident
    partial: bool = False         # day+month known but no year (start is None)
    month: int | None = None
    day: int | None = None
    note: str = ""
    kind: str = "single"          # single | range

    @property
    def valid(self) -> bool:
        return self.start is not None

    @property
    def weekday(self) -> int | None:
        """0=Monday..6=Sunday when the date is written with a weekday name ('Tuesday 6 October')."""
        m = re.match(r"\s*(mon|tue|wed|thu|fri|sat|sun)", self.raw, re.I)
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(m.group(1).lower()) if m else None


def year_from_weekday(day: int, month: int, weekday: int, today: date, back: int = 1, ahead: int = 3) -> int | None:
    """The one year in [today.year-back, today.year+ahead] where day/month falls on that weekday -- or None when
    zero or several years fit. This ESTABLISHES a year from what the page wrote; it never guesses one."""
    fits = [y for y in range(today.year - back, today.year + ahead + 1)
            if (d := _mk(y, month, day)) is not None and d.weekday() == weekday]
    return fits[0] if len(fits) == 1 else None


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _month(name: str) -> int:
    return MONTHS[name.lower().rstrip(".")]


def _endpoint(m: re.Match, i: str, locale: str) -> tuple[int | None, int | None, int | None, bool]:
    """-> (day, month, year, ambiguous)"""
    g = m.groupdict()
    if g.get(f"d{i}a"):
        return int(g[f"d{i}a"]), _month(g[f"m{i}a"]), int(g[f"y{i}a"]) if g.get(f"y{i}a") else None, False
    if g.get(f"m{i}b"):
        return int(g[f"d{i}b"]), _month(g[f"m{i}b"]), int(g[f"y{i}b"]) if g.get(f"y{i}b") else None, False
    if g.get(f"iso{i}"):
        y, mo, d = g[f"iso{i}"].split("-")
        return int(d), int(mo), int(y), False
    if g.get(f"n{i}x"):
        a, b, y = int(g[f"n{i}x"]), int(g[f"n{i}y"]), int(g[f"n{i}z"])
        if a > 12 and b <= 12:
            return a, b, y, False
        if b > 12 and a <= 12:
            return b, a, y, False
        if a > 12 and b > 12:
            return None, None, None, False
        if a == b:
            return a, b, y, False
        # both <= 12 and different: genuinely ambiguous -> locale decides, and it is flagged
        return (a, b, y, True) if locale == "dmy" else (b, a, y, True) if locale == "mdy" else (None, None, y, True)
    return None, None, None, False


def _overlaps(span, taken):
    return any(span[0] < t[1] and t[0] < span[1] for t in taken)


def find_dates(text: str, locale: str = "dmy") -> list[DateHit]:
    """Every date or date range in `text`, in order of appearance."""
    text = text or ""
    hits: list[DateHit] = []
    taken: list[tuple[int, int]] = []

    def add(h: DateHit):
        if not _overlaps(h.span, taken):
            taken.append(h.span)
            hits.append(h)

    for rx, tag in ((_RANGE_TWO, "two"), (_RANGE_SAME_MONTH_DF, "same"), (_RANGE_SAME_MONTH_MF, "same")):
        for m in rx.finditer(text):
            if _overlaps(m.span(), taken):
                continue
            if tag == "two":
                d1, m1, y1, amb1 = _endpoint(m, "1", locale)
                d2, m2, y2, amb2 = _endpoint(m, "2", locale)
                if d1 is None or d2 is None:
                    if d1 is None and d2 is None:
                        continue
                    continue
                note, year_stated = "", True
                if y1 is None and y2 is not None:
                    y1 = y2 - 1 if m1 > m2 else y2   # a range is chronological: Dec..Jan can only start the year before
                    note = "start year taken from the range's stated end year"
                if y2 is None and y1 is not None:
                    y2 = y1 + 1 if m2 < m1 else y1
                    note = "end year taken from the range's stated start year"
                if y1 is None or y2 is None:
                    add(DateHit(None, None, m.group(0), m.span(), year_stated=False, partial=True, month=m1, day=d1,
                                kind="range"))
                    continue
                s, e = _mk(y1, m1, d1), _mk(y2, m2, d2)
                if s and e and e >= s:
                    add(DateHit(s, e if e != s else None, m.group(0), m.span(), ambiguous=amb1 or amb2, note=note,
                                kind="range" if e != s else "single"))
                continue
            d1, d2, mo = int(m.group("d1")), int(m.group("d2")), _month(m.group("m"))
            y = int(m.group("y")) if m.group("y") else None
            if y is None:
                add(DateHit(None, None, m.group(0), m.span(), year_stated=False, partial=True, month=mo, day=d1,
                            kind="range"))
                continue
            s, e = _mk(y, mo, d1), _mk(y, mo, d2)
            if s and e and e >= s:
                add(DateHit(s, e if e != s else None, m.group(0), m.span(), kind="range" if e != s else "single"))

    for m in _SINGLE.finditer(text):
        if _overlaps(m.span(), taken):
            continue
        d, mo, y, amb = _endpoint(m, "1", locale)
        if d is None or mo is None:
            if amb:  # ambiguous numeric date and no locale -> report it as unresolved, never pick
                add(DateHit(None, None, m.group(0), m.span(), ambiguous=True, note="numeric date order is ambiguous"))
            continue
        if y is None:
            add(DateHit(None, None, m.group(0), m.span(), year_stated=False, partial=True, month=mo, day=d))
            continue
        s = _mk(y, mo, d)
        if s:
            add(DateHit(s, None, m.group(0), m.span(), ambiguous=amb,
                        note="numeric date order taken from the site's locale" if amb else ""))

    for m in _MONTH_YEAR.finditer(text):
        if not _overlaps(m.span(), taken):
            add(DateHit(None, None, m.group(0), m.span(), year_stated=True, month=_month(m.group("m")),
                        note="month and year only, no day"))
    hits.sort(key=lambda h: h.span[0])
    return hits


def date_context_is_deadline(text: str, hit: DateHit, window: int = 40) -> bool:
    """True when the words just before the date say it is a deadline/posted-on
    date rather than the event date."""
    before = text[max(0, hit.span[0] - window):hit.span[0]]
    return bool(DEADLINE_WORDS.search(before)) or bool(re.search(
        r"(regist\w*|deadline|early[\s-]*bird|closes?|closing)[^.]{0,25}\b(?:by|until|before|on)\s*$", before, re.I))


def parse_one(text: str, locale: str = "dmy") -> DateHit | None:
    """The first complete date in `text`, skipping partial / month-only hits."""
    for h in find_dates(text, locale):
        if h.valid:
            return h
    return None


def parse_iso(value: str | None) -> date | None:
    m = re.match(r"\s*((?:19|20)\d{2})-(\d{2})-(\d{2})", value or "")
    return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def to_iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def display(d: date | str | None) -> str:
    if isinstance(d, str):
        d = parse_iso(d)
    return d.strftime("%d-%b-%Y") if d else ""


# ---- times ----------------------------------------------------------------------

_T = r"(?<![\d:.])(?P<h{i}>\d{{1,2}})(?::(?P<mi{i}>\d{{2}})|\.(?P<mj{i}>\d{{2}})(?=\s*(?:[ap]\.?m|[-–—]\s*\d|to\s+\d)))?\s*(?P<ap{i}>a\.?m\.?|p\.?m\.?)?(?![\d])"
_TIME_RANGE = re.compile(_T.format(i=1) + _SEP + _T.format(i=2), re.I)
_TIME_ONE = re.compile(_T.format(i=1), re.I)


def _hhmm(m: re.Match, i: str) -> str | None:
    g = m.groupdict()
    h = g.get(f"h{i}")
    if h is None:
        return None
    ap = (g.get(f"ap{i}") or "").lower().replace(".", "")
    minutes = g.get(f"mi{i}") or g.get(f"mj{i}")
    if not ap and not minutes:
        return None                      # a bare number is not a time
    h, mi = int(h), int(minutes or 0)
    if ap:
        if not (1 <= h <= 12):
            return None
        h = h % 12 + (12 if ap.startswith("p") else 0)
    elif h > 23:
        return None
    return f"{h:02d}:{mi:02d}"


def find_times(text: str) -> tuple[str | None, str | None, str]:
    """(start "HH:MM", end "HH:MM", evidence). Times are only ever read, never inferred.
    '5:30-7:30pm' -> 17:30-19:30 (the first time shares the end's meridiem when that keeps the range forward)."""
    text = text or ""
    for m in _TIME_RANGE.finditer(text):
        a, b = _hhmm(m, "1"), _hhmm(m, "2")
        g = m.groupdict()
        if g.get("h1") and not g.get("ap1") and g.get("ap2") and b and 1 <= int(g["h1"]) <= 12:
            g = dict(g, mi1=g.get("mi1") or g.get("mj1"))
            ap2 = g["ap2"].lower().replace(".", "")
            h1, mi1 = int(g["h1"]), int(g.get("mi1") or 0)
            if 1 <= h1 <= 12:
                same = f"{h1 % 12 + (12 if ap2.startswith('p') else 0):02d}:{mi1:02d}"
                other = f"{h1 % 12 + (0 if ap2.startswith('p') else 12):02d}:{mi1:02d}"
                a = same if same <= b else other if other <= b else None
        if a and b:
            return a, b, m.group(0)
    for m in _TIME_ONE.finditer(text):
        a = _hhmm(m, "1")
        if a:
            return a, None, m.group(0)
    return None, None, ""


_TZ = re.compile(
    r"\b(AEST|AEDT|ACST|ACDT|AWST|NZST|NZDT|IST|SGT|GST|GMT|BST|UTC|EST|EDT|CST|CDT|MST|MDT|PST|PDT|CET|CEST|HKT|JST|SAST)"
    r"(?:\s?[+-]\s?\d{1,2}(?::?\d{2})?)?\b|\bGMT\s?[+-]\s?\d{1,2}(?::?\d{2})?|\bUTC\s?[+-]\s?\d{1,2}(?::?\d{2})?"
    r"|\b(?:Singapore|Dubai|India|Sydney|Melbourne|London|Eastern|Central|Pacific|Mountain)\s+(?:Standard\s+|Daylight\s+)?Time\b")


def find_timezone(text: str) -> str:
    m = _TZ.search(text or "")
    return m.group(0).strip() if m else ""
