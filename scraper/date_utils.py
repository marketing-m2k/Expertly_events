"""Shared date parsing: turn a free-text event date into a real datetime,
and decide whether an event is in the past.
"""

import re
from datetime import datetime

from dateutil import parser as date_parser


def has_explicit_year(candidate: str, today: datetime) -> bool:
    try:
        d1 = date_parser.parse(candidate, fuzzy=True, default=today.replace(year=1901))
        d2 = date_parser.parse(candidate, fuzzy=True, default=today.replace(year=2099))
    except (ValueError, OverflowError, TypeError):
        return True  # unknown -> don't guess, treat as explicit so we don't shift it
    return d1.year == d2.year


# An archive-style listing (e.g. a "past events" page) sometimes gives a
# date with no year at all in its own field ("December 1") while the
# event's own title/description states, in the past tense, which year it
# actually happened ("...programme was held from December 1 to 9 2023").
# Without this, a yearless date gets the "assume next upcoming occurrence"
# treatment below and a 2023 event silently becomes "upcoming in 2026".
# Deliberately narrow to past-tense phrasing describing THIS event
# happening -- not organizational history ("founded in 1998") -- to avoid
# misreading an unrelated year mention as the event's own date.
_STATED_PAST_YEAR = re.compile(
    r"(?:was held|held (?:on|from|in)|took place|was conducted|conducted (?:on|in)|"
    r"concluded on|was organi[sz]ed|was convened)"
    # up to 60 chars of anything can sit between the phrase and the year
    # (e.g. "held from December 1 to 9 2023") -- must be `.`, not `\D`,
    # since day numbers in that gap are themselves digits.
    r".{0,60}?(\d{4})\b",
    re.I,
)


def stated_past_year(text: str, today: datetime) -> int | None:
    """If `text` explicitly says (in the past tense) which year an event
    happened, and that year is actually in the past, return it -- else
    None."""
    match = _STATED_PAST_YEAR.search(text or "")
    if not match:
        return None
    year = int(match.group(1))
    return year if year < today.year else None


_FULL_NUMERIC_DATE = re.compile(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}")

# "5-6 Sep 2026", "01st-02nd September 2026", "3rd - 5th December 2026": the
# day-day pair comes BEFORE the month, so the month info the left half needs
# lives on the *right* of the hyphen — the generic left-side-looks-complete
# check below can't handle that, and worse, dateutil's fuzzy parser mangles
# a bare leading "5-6" into a nonsense year (e.g. 2005) instead of raising,
# so this must be caught explicitly rather than left to fall through.
_DAY_DAY_MONTH_YEAR = re.compile(
    r"^(\d{1,2}(?:st|nd|rd|th)?)\s*[-–—]\s*(\d{1,2}(?:st|nd|rd|th)?)\s+([A-Za-z].*)$"
)

# Same "day comes before month" shape as above, but "to"/"until"/"through"
# instead of a hyphen, and NOT anchored to the start of the string -- this
# form shows up embedded in a longer event title, e.g. "12 DAYS TRANSFER
# PRICING SERIES - 1st to 18th September, 2026". Naively splitting on " to "
# (the word-separated branch below) would keep everything before "to",
# including "12 DAYS ... - 1st" -- and dateutil's fuzzy parser then grabs the
# unrelated "12" as a month, producing a wrong date instead of raising. This
# pattern captures ONLY the bare day immediately before "to" plus the month
# that follows the second day, discarding any leading junk.
_DAY_TO_DAY_MONTH_YEAR = re.compile(
    r"(\d{1,2}(?:st|nd|rd|th)?)\s+(?:to|until|through)\s+(\d{1,2}(?:st|nd|rd|th)?)\s+([A-Za-z].*)$",
    re.I,
)

# "May 5-6, 2026": month comes FIRST, then a day-day pair -- the mirror
# image of _DAY_DAY_MONTH_YEAR. Without this, the generic hyphen-scan
# fallback below finds the hyphen between the two days, sees "May 5" as a
# complete-looking left side, and leaves the end half as a bare "6, 2026"
# with no month -- dateutil then fills the missing month in from `today`
# instead of "May", silently producing a wrong end date.
_MONTH_DAY_DAY = re.compile(
    r"^([A-Za-z]+\.?)\s+(\d{1,2}(?:st|nd|rd|th)?)\s*[-–—]\s*(\d{1,2}(?:st|nd|rd|th)?)\b"
)


def _range_bounds(text: str) -> tuple[str | None, str | None]:
    """Pull the (start, end) halves out of a date-range string, handling
    the separator styles seen in the wild:
      - "day to day Month Year": "1st to 18th September, 2026", including
        with junk text before it, e.g. "12 DAYS ... - 1st to 18th
        September, 2026" (see _DAY_TO_DAY_MONTH_YEAR above)
      - "day-day Month Year": "5-6 Sep 2026" (see _DAY_DAY_MONTH_YEAR above)
      - word-separated, each side already a complete date: "20th July to
        24th July 2026"
      - hyphen-separated full dates: "May 5-6, 2026", "24-07-2026 - 24-07-2026"

    Returns (None, None) if `text` isn't recognized as a range at all --
    callers fall back to treating the whole text as a single date. The end
    half may still come back None even when start doesn't, if no end could
    be identified.

    The hyphen case is the tricky one: a naive "split at the first hyphen"
    breaks full numeric dates like "24-07-2026" (splits inside the date
    itself, at "24" / "07-2026"), silently producing an unparseable
    fragment. So for hyphens, each occurrence is tried left-to-right and
    only accepted once the left-hand side already looks like a *complete*
    date token (contains a month name, or is itself a full D-M-Y token) —
    not just a bare day number.
    """
    dtm = _DAY_TO_DAY_MONTH_YEAR.search(text)
    if dtm:
        day1, day2, tail = dtm.group(1), dtm.group(2), dtm.group(3)
        return f"{day1} {tail}", f"{day2} {tail}"

    dm = _DAY_DAY_MONTH_YEAR.match(text)
    if dm:
        day1, day2, tail = dm.group(1), dm.group(2), dm.group(3)
        return f"{day1} {tail}", f"{day2} {tail}"

    mdd = _MONTH_DAY_DAY.match(text)
    if mdd:
        month, day1, day2 = mdd.group(1), mdd.group(2), mdd.group(3)
        return f"{month} {day1}", f"{month} {day2}"

    m = re.search(r"\s+(?:to|until|through)\s+", text, re.I)
    if m:
        return text[:m.start()].strip(), text[m.end():].strip()

    for hm in re.finditer(r"[-–—]", text):
        left = text[:hm.start()].strip()
        if _FULL_NUMERIC_DATE.fullmatch(left) or re.search(r"[A-Za-z]{3,}", left):
            return left, text[hm.end():].strip()

    return None, None


def _reattach_year(candidate: str, year_match: re.Match | None) -> str:
    # the year usually trails the range ("May 5-6, 2026"), so a split-off
    # half loses it -- reattach it before parsing
    if year_match and not re.search(r"\b(19|20)\d{2}\b", candidate):
        return f"{candidate} {year_match.group(0)}"
    return candidate


# Returned by _parse_candidate instead of None when the candidate DID
# contain an explicit, unambiguous year and it's just implausible (e.g. a
# 1908 historical bulletin) -- as opposed to genuinely failing to parse.
# The distinction matters to parse_event_date_range's fallback loop below:
# an implausible-but-explicit year means "this text names a real date and
# it's simply out of scope," so trying a weaker/fuzzier fallback parse of
# the same text next would only risk fabricating some OTHER date out of
# unrelated digits in the surrounding text (this is exactly how a 1908
# bulletin title was previously turning into a fabricated "2030" event).
# An unparseable candidate, by contrast, has no year to trust either way,
# so falling back to a different candidate is still worth trying.
REJECTED_IMPLAUSIBLE_YEAR = object()


def _parse_candidate(candidate: str, today: datetime, cutoff: datetime):
    try:
        dt = date_parser.parse(candidate, fuzzy=True, default=today)
    except (ValueError, OverflowError, TypeError):
        return None

    if dt.tzinfo is not None:
        # some sites embed a UTC offset in the date string (e.g. an ISO
        # timestamp) — drop it, since we only ever compare at day
        # granularity and a tz-aware dt can't be compared to the
        # tz-naive `cutoff` below.
        dt = dt.replace(tzinfo=None)

    candidate_had_explicit_year = has_explicit_year(candidate, today)

    if dt < cutoff and not candidate_had_explicit_year:
        try:
            dt = dt.replace(year=dt.year + 1)
        except ValueError:
            return None

    # dateutil's fuzzy mode will occasionally misread an unrelated number
    # in the text as a year (e.g. "Accounting 101 for..." -> year 101,
    # or "Part 2" contributing digits) -- reject those obvious misparses.
    # The floor is deliberately generous (not just today.year - 2): an
    # archive-style "past events" listing can genuinely reference events
    # several years old (stated_past_year() above corrects a yearless date
    # to that real year), and this same bound would otherwise reject that
    # correction right back into looking like a misparse.
    if not (today.year - 10 <= dt.year <= today.year + 6):
        return REJECTED_IMPLAUSIBLE_YEAR if candidate_had_explicit_year else None

    return dt


def parse_event_date(raw: str, today: datetime | None = None) -> datetime | None:
    """Best-effort parse of a free-text date or date range, returning its
    START date.

    Dates with no year (e.g. "12 Aug") are assumed to mean the next
    upcoming occurrence, not one that already passed this year. Returns
    None if nothing in the text could be parsed as a date at all.
    """
    start, _ = parse_event_date_range(raw, today)
    return start


def parse_event_date_range(raw: str, today: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    """Like parse_event_date, but also returns the range's END date, e.g.
    "1st to 18th September, 2026" -> (1-Sep-2026, 18-Sep-2026). The end
    date is None for a single-day event, or when no end could be
    confidently parsed (including when it would fall before the start --
    a sign `raw` wasn't really a date range after all).

    `raw` is usually a string, but callers sometimes hand back whatever an
    Excel cell held (a bare year like "2026" typed into a Date column gets
    auto-typed as an int by openpyxl) -- coerce defensively rather than
    crash on `.strip()`."""
    today = today or datetime.now()
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None, None

    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    cutoff = today.replace(hour=0, minute=0, second=0, microsecond=0)

    start_half, end_half = _range_bounds(text)
    start_candidates = [text]
    if start_half:
        start_candidates.insert(0, _reattach_year(start_half, year_match))

    start_dt = None
    for candidate in start_candidates:
        result = _parse_candidate(candidate, today, cutoff)
        if result is REJECTED_IMPLAUSIBLE_YEAR:
            # this candidate named a real, unambiguous year and it's just
            # out of scope (e.g. a 1908 historical bulletin) -- don't keep
            # trying weaker/fuzzier fallback candidates on the same text,
            # since that risks fabricating an unrelated date out of other
            # digits nearby instead of correctly giving up.
            return None, None
        if result is not None:
            start_dt = result
            break
    if start_dt is None:
        return None, None

    end_dt = None
    if end_half:
        end_result = _parse_candidate(_reattach_year(end_half, year_match), today, cutoff)
        end_dt = end_result if isinstance(end_result, datetime) else None
        if end_dt is not None and end_dt < start_dt:
            end_dt = None

    return start_dt, end_dt


def is_past_event(raw: str, today: datetime | None = None) -> bool:
    """True only when the date is confidently in the past. Unparseable
    dates are treated as NOT past (we'd rather keep an event we can't
    date than silently drop it)."""
    today = today or datetime.now()
    dt = parse_event_date(raw, today)
    if dt is None:
        return False
    return dt.date() < today.replace(hour=0, minute=0, second=0, microsecond=0).date()


_DAY_NUMBER = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\b")
_FULL_NUMERIC = re.compile(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}")


def has_day_precision(raw) -> bool:
    """True only if the text names an actual day-of-month, not just a bare
    month/year (e.g. "APR", "Q3 2026", "December 2026"). dateutil's fuzzy
    parser will happily fill in a missing day from `today`/`default`, which
    silently manufactures a plausible-looking but fabricated date — this
    catches that case so it can be routed to the Incomplete sheet instead of
    trusted as a real event date.

    `raw` is usually a string, but callers sometimes hand back whatever an
    Excel cell held (a bare year like "2026" typed into a Date column gets
    auto-typed as an int by openpyxl) -- coerce defensively rather than
    crash on `.strip()`."""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return False
    if _FULL_NUMERIC.search(text):
        return True
    return bool(_DAY_NUMBER.search(text))


def is_before_cutoff(raw: str, cutoff: datetime, today: datetime | None = None) -> bool:
    """True only when the date is confidently before `cutoff`. Parses using
    `today` as the ambiguity-resolution anchor (so a yearless date like
    "15 Feb" still resolves to its most sensible real occurrence), then
    compares the result to `cutoff`. Unparseable dates are treated as NOT
    before cutoff (kept rather than dropped)."""
    today = today or datetime.now()
    dt = parse_event_date(raw, today)
    if dt is None:
        return False
    return dt.date() < cutoff.date()


_MONTH_NAMES = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)
_STATED_FULL_DATE = re.compile(
    rf"(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\.?,?\s+(\d{{4}}))"
    rf"|(?:({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}}))",
    re.I,
)
# Range forms: "October 6-8, 1908", "December 1 to 9, 2023" -- extract the
# START day of the range as the comparison point.
_STATED_RANGE_DATE = re.compile(
    rf"(?:({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:[-–—]|to|through)\s*\d{{1,2}}(?:st|nd|rd|th)?,?\s+(\d{{4}}))"
    rf"|(?:(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:[-–—]|to|through)\s*\d{{1,2}}(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\.?,?\s+(\d{{4}}))",
    re.I,
)
_MONTH_NUM = {name.lower(): i for i, names in enumerate([
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"), ("december", "dec"),
], start=1) for name in names}


def extract_stated_dates(text: str) -> list[datetime]:
    """Find every explicit, fully-specified date (day + month-name + year)
    mentioned anywhere in `text`, in either "1 January 2026" or "January 1,
    2026" order. Used to cross-check a resolved Date against what the
    event's own name/description actually says -- unlike the parsing
    functions above, this doesn't guess or roll years forward; it only
    returns dates that were spelled out completely and unambiguously."""
    found = []
    for m in _STATED_FULL_DATE.finditer(text or ""):
        if m.group(1):
            day, month_name, year = m.group(1), m.group(2), m.group(3)
        else:
            month_name, day, year = m.group(4), m.group(5), m.group(6)
        month = _MONTH_NUM.get(month_name.lower().rstrip("."))
        if not month:
            continue
        try:
            found.append(datetime(int(year), month, int(day)))
        except ValueError:
            continue
    for m in _STATED_RANGE_DATE.finditer(text or ""):
        if m.group(1):
            month_name, day, year = m.group(1), m.group(2), m.group(3)
        else:
            day, month_name, year = m.group(4), m.group(5), m.group(6)
        month = _MONTH_NUM.get(month_name.lower().rstrip("."))
        if not month:
            continue
        try:
            found.append(datetime(int(year), month, int(day)))
        except ValueError:
            continue
    return found


def date_conflicts_with_text(resolved_start: datetime, resolved_end: datetime | None,
                              text: str) -> bool:
    """The verification gate before Master.xlsx: True only when `text`
    (an event's own name + description) explicitly states a date that
    DISAGREES with the resolved start/end. If the text states no date at
    all, or states one that matches (or falls inside a multi-day range),
    this returns False -- an unconfirmed date is not the same as a wrong
    one, so it doesn't get flagged just for lack of corroboration."""
    stated = extract_stated_dates(text)
    if not stated:
        return False
    for s in stated:
        if s.date() == resolved_start.date():
            return False
        if resolved_end and resolved_start.date() <= s.date() <= resolved_end.date():
            return False
    return True
