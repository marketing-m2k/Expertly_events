"""Shared date parsing: turn a free-text event date into a real datetime,
and decide whether an event is in the past.
"""

import re
from datetime import datetime

from dateutil import parser as date_parser


def _has_explicit_year(candidate: str, today: datetime) -> bool:
    try:
        d1 = date_parser.parse(candidate, fuzzy=True, default=today.replace(year=1901))
        d2 = date_parser.parse(candidate, fuzzy=True, default=today.replace(year=2099))
    except (ValueError, OverflowError, TypeError):
        return True  # unknown -> don't guess, treat as explicit so we don't shift it
    return d1.year == d2.year


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


def _parse_candidate(candidate: str, today: datetime, cutoff: datetime) -> datetime | None:
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

    if dt < cutoff and not _has_explicit_year(candidate, today):
        try:
            dt = dt.replace(year=dt.year + 1)
        except ValueError:
            return None

    # dateutil's fuzzy mode will occasionally misread an unrelated number
    # in the text as a year (e.g. "Accounting 101 for..." -> year 101,
    # or "Part 2" contributing digits) -- a real event for this tracker
    # is never more than a few years out, so anything wildly outside
    # that range is a misparse, not a real date.
    if not (today.year - 2 <= dt.year <= today.year + 6):
        return None

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
    a sign `raw` wasn't really a date range after all)."""
    today = today or datetime.now()
    text = (raw or "").strip()
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
        start_dt = _parse_candidate(candidate, today, cutoff)
        if start_dt is not None:
            break
    if start_dt is None:
        return None, None

    end_dt = None
    if end_half:
        end_dt = _parse_candidate(_reattach_year(end_half, year_match), today, cutoff)
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


def has_day_precision(raw: str) -> bool:
    """True only if the text names an actual day-of-month, not just a bare
    month/year (e.g. "APR", "Q3 2026", "December 2026"). dateutil's fuzzy
    parser will happily fill in a missing day from `today`/`default`, which
    silently manufactures a plausible-looking but fabricated date — this
    catches that case so it can be routed to the Incomplete sheet instead of
    trusted as a real event date."""
    text = (raw or "").strip()
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
