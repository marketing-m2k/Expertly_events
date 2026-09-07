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
    r"^(\d{1,2}(?:st|nd|rd|th)?)\s*[-–—]\s*\d{1,2}(?:st|nd|rd|th)?\s+([A-Za-z].*)$"
)


def _range_start_candidate(text: str, year_match: re.Match | None) -> str | None:
    """Pull the start half out of a date-range string, handling the
    separator styles seen in the wild:
      - word-separated: "20th July to 24th July 2026", "5 Aug - 9 Aug" ("to"/
        "until"/"through", unambiguous — never appears inside a single date)
      - "day-day Month Year": "5-6 Sep 2026" (see _DAY_DAY_MONTH_YEAR above)
      - hyphen-separated full dates: "May 5-6, 2026", "24-07-2026 - 24-07-2026"

    The last case is the tricky one: a naive "split at the first hyphen"
    breaks full numeric dates like "24-07-2026" (splits inside the date
    itself, at "24" / "07-2026"), silently producing an unparseable
    fragment. So for hyphens, each occurrence is tried left-to-right and
    only accepted once the left-hand side already looks like a *complete*
    date token (contains a month name, or is itself a full D-M-Y token) —
    not just a bare day number.
    """
    m = re.search(r"\s+(?:to|until|through)\s+", text, re.I)
    dm = _DAY_DAY_MONTH_YEAR.match(text)
    if m:
        start = text[:m.start()].strip()
    elif dm:
        start = f"{dm.group(1)} {dm.group(2)}"
    else:
        start = None
        for hm in re.finditer(r"[-–—]", text):
            left = text[:hm.start()].strip()
            if _FULL_NUMERIC_DATE.fullmatch(left) or re.search(r"[A-Za-z]{3,}", left):
                start = left
                break
        if start is None:
            return None

    # the year usually trails the range ("May 5-6, 2026"), so the start half
    # loses it on split — reattach it before parsing
    if year_match and not re.search(r"\b(19|20)\d{2}\b", start):
        start = f"{start} {year_match.group(0)}"
    return start


def parse_event_date(raw: str, today: datetime | None = None) -> datetime | None:
    """Best-effort parse of a free-text date or date range.

    Dates with no year (e.g. "12 Aug") are assumed to mean the next
    upcoming occurrence, not one that already passed this year. Returns
    None if nothing in the text could be parsed as a date at all.
    """
    today = today or datetime.now()
    text = (raw or "").strip()
    if not text:
        return None

    year_match = re.search(r"\b(19|20)\d{2}\b", text)

    candidates = [text]
    start = _range_start_candidate(text, year_match)
    if start:
        candidates.insert(0, start)

    cutoff = today.replace(hour=0, minute=0, second=0, microsecond=0)
    for candidate in candidates:
        try:
            dt = date_parser.parse(candidate, fuzzy=True, default=today)
        except (ValueError, OverflowError, TypeError):
            continue

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
                continue

        # dateutil's fuzzy mode will occasionally misread an unrelated number
        # in the text as a year (e.g. "Accounting 101 for..." -> year 101,
        # or "Part 2" contributing digits) -- a real event for this tracker
        # is never more than a few years out, so anything wildly outside
        # that range is a misparse, not a real date.
        if not (today.year - 2 <= dt.year <= today.year + 6):
            continue

        return dt
    return None


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
