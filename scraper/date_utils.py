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
    m = re.match(r"^(.*?)[-–—](.*)$", text)  # date ranges: "May 5-6, 2026"
    if m:
        start = m.group(1).strip()
        # the year usually trails the range ("May 5-6, 2026"), so the start
        # half loses it on split — reattach it before parsing
        if year_match and not re.search(r"\b(19|20)\d{2}\b", start):
            start = f"{start} {year_match.group(0)}"
        candidates.insert(0, start)

    cutoff = today.replace(hour=0, minute=0, second=0, microsecond=0)
    for candidate in candidates:
        try:
            dt = date_parser.parse(candidate, fuzzy=True, default=today)
        except (ValueError, OverflowError, TypeError):
            continue

        if dt < cutoff and not _has_explicit_year(candidate, today):
            try:
                dt = dt.replace(year=dt.year + 1)
            except ValueError:
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
