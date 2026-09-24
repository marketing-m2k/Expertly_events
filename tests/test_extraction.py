"""Regression tests for the no-AI extraction layer. Each one is a mistake
found in the September 2026 quality checks (or a rule from the redesign);
every code change must keep them all passing:

    python -m pytest tests/
"""

from datetime import datetime

from scraper.date_utils import parse_strict_date_range
from scraper.detail_extract import extract_detail_page
from scraper.evidence import appears_on_page
from scraper.label_extract import extract_labelled, is_generic_title
from scraper.structured_data import extract_structured_events, pick_event_for_page

URL = "https://example.org/events/gst-workshop"


def _page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body><main>{body}</main></body></html>"


# ---- dates are never guessed -------------------------------------------------

def test_no_year_means_no_date():
    today = datetime(2026, 9, 24)
    assert parse_strict_date_range("12 March", today) == (None, None)


def test_month_year_without_day_means_no_date():
    assert parse_strict_date_range("December 2026", datetime(2026, 9, 24)) == (None, None)


def test_full_date_parses():
    start, end = parse_strict_date_range("12 October 2026", datetime(2026, 9, 24))
    assert start == datetime(2026, 10, 12) and end is None


def test_date_range_parses():
    start, end = parse_strict_date_range("12-14 October 2026", datetime(2026, 9, 24))
    assert start == datetime(2026, 10, 12) and end == datetime(2026, 10, 14)


def test_past_2026_date_is_not_rolled_to_2027():
    # the "International Tax Conference 2026 -> 24-Apr-2027" bug
    start, _ = parse_strict_date_range("24 April 2026", datetime(2026, 9, 24))
    assert start == datetime(2026, 4, 24)


# ---- deadlines are never the event date --------------------------------------

def test_registration_deadline_is_not_the_event_date():
    html = _page("""
        <h1>Annual Transfer Pricing Summit</h1>
        <p><b>Registration closes:</b> 1 September 2026</p>
        <p><b>Event Date:</b> 20 October 2026</p>
    """)
    result = extract_labelled(html, URL)
    assert result["fields"]["date"]["value"] == "20-Oct-2026"
    assert any("Registration" in label for label in result["ignored_labels"])


def test_only_a_deadline_gives_no_date():
    html = _page("<h1>Annual Transfer Pricing Summit</h1><p>Last date to register: 1 September 2026</p>")
    assert "date" not in extract_labelled(html, URL)["fields"]


# ---- titles ------------------------------------------------------------------

def test_generic_headings_are_not_titles():
    for bad in ("THEME", "Masterclasses", "LISTING", "Webinar", "RESILIENCE", "42"):
        assert is_generic_title(bad)


def test_generic_h1_falls_back_to_a_specific_title():
    html = _page("<h1>Masterclasses</h1>",
                 head='<meta property="og:title" content="GST Audit Masterclass for Practitioners | ICAI">')
    name = extract_labelled(html, URL)["fields"]["name"]
    assert name["value"] == "GST Audit Masterclass for Practitioners"
    assert name["confidence"] == "medium"


# ---- structured data ---------------------------------------------------------

JSONLD = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"EducationEvent","name":"GST Workshop",
 "startDate":"2026-10-12T09:00:00+05:30","endDate":"2026-10-13",
 "eventAttendanceMode":"https://schema.org/OfflineEventAttendanceMode",
 "location":{"@type":"Place","name":"Taj Hotel","address":{"addressLocality":"Mumbai","addressCountry":"IN"}},
 "url":"https://example.org/events/gst-workshop"}
</script>
"""


def test_structured_data_is_read_field_by_field():
    events = extract_structured_events(_page("<h1>GST Workshop</h1>", head=JSONLD), URL)
    assert len(events) == 1
    e = events[0]
    assert e["date"]["value"] == "12-Oct-2026"
    assert e["end_date"]["value"] == "13-Oct-2026"
    assert e["format"]["value"] == "In Person"
    assert "Mumbai" in e["location"]["value"]
    assert e["date"]["source"] == "structured_data" and "startDate" in e["date"]["reason"]


def test_online_attendance_mode_is_virtual():
    ld = JSONLD.replace("OfflineEventAttendanceMode", "OnlineEventAttendanceMode")
    e = extract_structured_events(_page("", head=ld), URL)[0]
    assert e["format"]["value"] == "Virtual"


def test_listing_page_with_many_events_picks_none_unless_url_matches():
    two = JSONLD + JSONLD.replace("gst-workshop", "other-event").replace("GST Workshop", "Other")
    events = extract_structured_events(_page("", head=two), "https://example.org/events")
    assert pick_event_for_page(events, "https://example.org/events") is None
    assert pick_event_for_page(events, URL)["name"]["value"] == "GST Workshop"


# ---- combining sources -------------------------------------------------------

def test_two_sources_disagreeing_pick_neither():
    html = _page("<h1>GST Workshop</h1><p><b>Date:</b> 14 October 2026</p>", head=JSONLD)
    result = extract_detail_page(html, URL)
    assert "date" not in result["fields"]
    assert result["conflicts"][0]["column"] == "date"
    assert any("conflict on date" in r for r in result["review_reasons"])


def test_agreeing_sources_are_accepted():
    html = _page("<h1>GST Workshop</h1><p><b>Date:</b> 12 October 2026 - 13 October 2026</p>", head=JSONLD)
    result = extract_detail_page(html, URL)
    assert result["fields"]["date"]["value"] == "12-Oct-2026"
    assert not result["conflicts"]


def test_missing_year_goes_to_review_not_guessed():
    html = _page("<h1>GST Workshop</h1><p><b>Date:</b> 12 October</p>")
    result = extract_detail_page(html, URL)
    assert "date" not in result["fields"]
    assert any("no year" in r for r in result["review_reasons"])


def test_low_confidence_title_flags_review():
    html = "<html><head><title>GST Workshop for Tax Professionals</title></head><body><p><b>Date:</b> 12 October 2026</p></body></html>"
    result = extract_detail_page(html, URL)
    assert any("name came from" in r for r in result["review_reasons"])


# ---- format and venue --------------------------------------------------------

def test_format_only_from_an_explicit_label():
    html = _page("<h1>GST Workshop</h1><p><b>Mode:</b> Online (Zoom)</p><p><b>Date:</b> 12 October 2026</p>")
    assert extract_labelled(html, URL)["fields"]["format"]["value"] == "Virtual"


def test_zoom_in_body_text_does_not_make_an_event_virtual():
    html = _page("<h1>GST Workshop</h1><p>Recordings are also shared later on Zoom for members who could not attend in person.</p>")
    assert "format" not in extract_labelled(html, URL)["fields"]


def test_tba_venue_is_not_a_location():
    html = _page("<h1>GST Workshop</h1><p><b>Venue:</b> TBA</p>")
    assert "location" not in extract_labelled(html, URL)["fields"]


# ---- proof -------------------------------------------------------------------

def test_a_value_the_page_never_stated_is_detected():
    page_text = "GST Workshop. Date: 12 October 2026. Venue: Taj Hotel, Mumbai."
    assert appears_on_page("12 October 2026", page_text)
    assert appears_on_page("taj hotel,  mumbai", page_text)
    assert not appears_on_page("14 October 2026", page_text)


def test_several_labels_on_one_line_are_split_at_each_label():
    # seen on an Arlo events page (AUS trial): "Event: X Date: ... Venue: ..." in one block
    html = _page("<h1>20th Lawtech Summit 2026</h1>"
                 "<div>Event: 20th Lawtech Summit 2026 Date: 17–18 September 2026 Venue: JW Marriott Gold Coast Resort</div>")
    fields = extract_labelled(html, URL)["fields"]
    assert fields["date"]["value"] == "17-Sep-2026" and fields["end_date"]["value"] == "18-Sep-2026"
    assert fields["location"]["value"] == "JW Marriott Gold Coast Resort"


def test_a_deadline_on_the_same_line_does_not_leak_into_the_date():
    html = _page("<h1>Tax Summit</h1><div>Date: 20 October 2026 Registration closes: 1 October 2026</div>")
    result = extract_labelled(html, URL)
    assert result["fields"]["date"]["value"] == "20-Oct-2026"
    assert any("Registration" in label for label in result["ignored_labels"])
