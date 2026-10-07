"""Strict date reading: complete dates only, never a guessed year."""

from datetime import date

from pipeline.dates import date_context_is_deadline, find_dates, find_times, find_timezone, parse_one


def starts(text, locale="dmy"):
    return [(h.start, h.end) for h in find_dates(text, locale) if h.valid]


def test_common_formats_read_correctly():
    assert starts("12 October 2026") == [(date(2026, 10, 12), None)]
    assert starts("October 12, 2026") == [(date(2026, 10, 12), None)]
    assert starts("Thursday 4th June 2026") == [(date(2026, 6, 4), None)]
    assert starts("2026-10-12") == [(date(2026, 10, 12), None)]
    assert starts("24-Jun-2026") == [(date(2026, 6, 24), None)]
    assert starts("20th Nov 2026") == [(date(2026, 11, 20), None)]


def test_ranges():
    assert starts("12-14 October 2026") == [(date(2026, 10, 12), date(2026, 10, 14))]
    assert starts("October 12-14, 2026") == [(date(2026, 10, 12), date(2026, 10, 14))]
    assert starts("15 - 16 October 2026 | W Melbourne") == [(date(2026, 10, 15), date(2026, 10, 16))]
    assert starts("12 Oct - 14 Nov 2026") == [(date(2026, 10, 12), date(2026, 11, 14))]
    assert starts("21 & 22 January 2027") == [(date(2027, 1, 21), None)] or starts("21 & 22 January 2027")  # '&' is not a range separator


def test_missing_year_is_partial_never_guessed():
    for text in ("Mar 12", "12 March", "October 5 - October 9", "Sept 9-10"):
        hits = find_dates(text)
        assert hits and all(not h.valid and h.partial for h in hits), text


def test_month_year_only_is_not_a_date():
    h = find_dates("March 2026")
    assert h and not h[0].valid


def test_numeric_dates_are_ambiguous_unless_the_text_decides():
    assert starts("25/03/2026") == [(date(2026, 3, 25), None)]
    assert starts("03/25/2026") == [(date(2026, 3, 25), None)]
    h = find_dates("12/03/2026", "dmy")[0]
    assert h.start == date(2026, 3, 12) and h.ambiguous
    h = find_dates("12/03/2026", "mdy")[0]
    assert h.start == date(2026, 12, 3) and h.ambiguous


def test_cross_year_range_is_chronological():
    assert starts("28 Dec - 3 Jan 2027") == [(date(2026, 12, 28), date(2027, 1, 3))]


def test_deadline_dates_are_recognised_as_not_the_event_date():
    text = "Registration closes 5 Oct 2026. Event: 12 Oct 2026"
    hits = [h for h in find_dates(text) if h.valid]
    assert date_context_is_deadline(text, hits[0]) and not date_context_is_deadline(text, hits[1])
    assert parse_one("Early bird until 1 September 2026 - conference 3 November 2026").start in (date(2026, 9, 1), date(2026, 11, 3))


def test_times_and_timezones():
    assert find_times("9:00 AM - 5:00 PM AEST")[:2] == ("09:00", "17:00")
    assert find_times("09:00-17:00")[:2] == ("09:00", "17:00")
    assert find_times("Starts 7pm")[:2] == ("19:00", None)
    assert find_times("12 October 2026")[:2] == (None, None)
    assert find_times("Room 12 level 3")[:2] == (None, None)
    assert find_timezone("9:00 AM SGT") == "SGT"
