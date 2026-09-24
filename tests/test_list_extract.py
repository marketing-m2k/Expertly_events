"""Tests for reading events off a list page (stage 1) and merging with the
event's own page. Made-up pages only."""

from datetime import datetime

from scraper.enrich_lists import merge_fields, process_candidate
from scraper.evidence import field
from scraper.list_extract import extract_list_events
from scraper.master_store import MasterData

TODAY = datetime(2026, 9, 24)
LIST_URL = "https://example.org/events"


def _list(*cards: str) -> str:
    body = "".join(f'<div class="event-card">{c}</div>' for c in cards)
    return f"<html><body><main>{body}</main></body></html>"


def _card(title, link, text):
    return f'<h3><a href="{link}">{title}</a></h3><p>{text}</p>'


def _events(*cards):
    return extract_list_events(_list(*cards), LIST_URL, TODAY)


FILLER = _card("Second Tax Workshop", "/e/2", "Date: 3 November 2026 Venue: Sydney Town Hall")


def test_labelled_date_and_venue_are_read_from_the_card():
    ev = _events(_card("GST Workshop", "/e/1", "Date: 22 September 2026 Venue: RFDS Adelaide 1 Tower Rd"), FILLER)[0]
    assert ev["fields"]["date"]["value"] == "22-Sep-2026" and ev["fields"]["date"]["confidence"] == "high"
    assert ev["fields"]["location"]["value"].startswith("RFDS Adelaide")
    assert ev["link"] == "https://example.org/e/1"


def test_the_only_full_date_in_a_card_is_accepted_with_a_reason():
    ev = _events(_card("Global Investor Forum", "/e/1", "14 October 2026 Join us for a members update."), FILLER)[0]
    assert ev["fields"]["date"]["value"] == "14-Oct-2026"
    assert "only full date" in ev["fields"]["date"]["reason"]


def test_a_deadline_date_is_never_the_event_date():
    ev = _events(_card("Tax Summit", "/e/1", "Register by 1 October 2026. Summit on 20 October 2026."), FILLER)[0]
    assert ev["fields"]["date"]["value"] == "20-Oct-2026"


def test_card_without_a_year_gives_no_date_not_a_guess():
    ev = _events(_card("CPD Lecture", "/e/1", "Tuesday, 29 Sep Ethics for lawyers"), FILLER)[0]
    assert "date" not in ev["fields"] and not ev["conflict"]


def test_two_different_dates_in_one_card_hold_the_event_for_review():
    ev = _events(_card("Risk Course", "/e/1", "Day 1: 5 October 2026 Day 2: 12 November 2026"), FILLER)[0]
    assert "date" not in ev["fields"] and "several different dates" in ev["conflict"]


def test_format_only_from_an_explicit_tag():
    ev = _events(_card("Webinar on GST", "/e/1", '14 October 2026 <span>Virtual</span> Some description text here'), FILLER)[0]
    assert ev["fields"]["format"]["value"] == "Virtual"
    ev2 = _events(_card("GST Workshop", "/e/1", "14 October 2026 Recordings are shared on Zoom afterwards for members."), FILLER)[0]
    assert "format" not in ev2["fields"]


def test_generic_card_titles_are_skipped():
    events = _events(_card("Event Details", "/e/1", "14 October 2026 more"), FILLER)
    assert [e["fields"]["name"]["value"] for e in events] == ["Second Tax Workshop"]


def test_card_disagreeing_with_structured_data_drops_both():
    ld = ('<script type="application/ld+json">{"@type":"Event","name":"GST Workshop","startDate":"2026-10-14",'
          '"eventAttendanceMode":"https://schema.org/OfflineEventAttendanceMode","url":"https://example.org/e/1"}</script>')
    page = _list(_card("GST Workshop", "/e/1", '14 October 2026 <span>Virtual</span> Learn how GST applies today'), FILLER) + ld
    ev = [e for e in extract_list_events(page, LIST_URL, TODAY) if e["link"] == "https://example.org/e/1"][0]
    assert "format" not in ev["fields"] and "structured data" in ev["conflict"]
    assert ev["fields"]["date"]["value"] == "14-Oct-2026"  # they agree on the date


# ---- merging the card with the event's own page ---------------------------------

def _f(value, source="label", confidence="high"):
    return field(value, value, "test", source, confidence)


def test_disagreement_between_event_page_and_card_drops_the_value():
    merged, conflicts = merge_fields({"date": _f("12-Oct-2026")}, {"date": _f("14-Oct-2026", "card")})
    assert "date" not in merged and conflicts[0]["column"] == "date"


def test_event_page_fills_what_the_card_lacks():
    merged, conflicts = merge_fields({"location": _f("Taj Hotel")}, {"name": _f("GST Workshop", "card"), "date": _f("12-Oct-2026", "card")})
    assert set(merged) == {"name", "date", "location"} and not conflicts


# ---- one candidate end to end ----------------------------------------------------

def _cand(fields, own_page, conflict="", link="https://example.org/e/1"):
    text = "GST Workshop Date: 12 October 2026"
    return {"organizer": "ICAI", "source_url": LIST_URL, "link": link, "fields": fields, "conflict": conflict,
            "card_text": text, "haystack": text, "own_page": own_page}


def test_event_with_no_page_of_its_own_is_verified_from_its_card():
    fields = {"name": field("GST Workshop", "GST Workshop", "card", "card"),
              "date": field("12-Oct-2026", "12 October 2026", "card date", "card")}
    rec = process_candidate(_cand(fields, own_page=False, link=LIST_URL), None, lambda u, wait_ms: None, "AUS", TODAY)
    assert rec["status"] == "verified" and rec["category"] == "Tax"


def test_card_that_conflicts_is_held_for_review_with_the_reason():
    fields = {"name": field("GST Workshop", "GST Workshop", "card", "card")}
    rec = process_candidate(_cand(fields, own_page=False, conflict="the card shows several different dates: x, y"),
                            None, lambda u, wait_ms: None, "AUS", TODAY)
    assert rec["status"] == "needs_review" and any("several different dates" in r for r in rec["reasons"])


def test_unopenable_event_page_does_not_block_a_good_card():
    fields = {"name": field("GST Workshop", "GST Workshop", "card", "card"),
              "date": field("12-Oct-2026", "12 October 2026", "card date", "card")}
    rec = process_candidate(_cand(fields, own_page=True), {"html": None, "status": 403, "error": "HTTP 403"},
                            lambda u, wait_ms: {"html": None, "status": 403, "error": "HTTP 403"}, "AUS", TODAY)
    assert rec["status"] == "verified"


def test_events_with_no_page_get_distinct_ids_per_date():
    a = process_candidate(_cand({"name": _f("Tax Series", "card"), "date": _f("12-Oct-2026", "card")}, False, link=LIST_URL), None, None, "AUS", TODAY)
    b = process_candidate(_cand({"name": _f("Tax Series", "card"), "date": _f("19-Oct-2026", "card")}, False, link=LIST_URL), None, None, "AUS", TODAY)
    assert a["event_id"] != b["event_id"]


# ---- Master: an old unverified row is upgraded in place ---------------------------

def test_legacy_row_is_upgraded_in_place_not_duplicated(tmp_path):
    m = MasterData(str(tmp_path / "Master.xlsx"), TODAY).load()
    m.verified["ev_old"] = {"Event ID": "ev_old", "Event Name": "GST Workshop", "Date": "12-Oct-2026", "Country": "AUS",
                            "Verified By": "Legacy (before v2)", "Last Seen on Site": "24-Sep-2026", "Locked": "",
                            "Category": "Tax, Accounting & Finance", "Location": ""}
    rec = {"event_id": "ev_new", "status": "verified", "changed": True, "country": "AUS", "organizer": "ICAI",
           "category": "Tax", "link": LIST_URL, "source_url": LIST_URL, "reasons": [],
           "fields": {"name": _f("GST Workshop"), "date": _f("12-Oct-2026"), "location": _f("Sydney Town Hall")}}
    m.apply_record(rec)
    assert list(m.verified) == ["ev_new"] and m.stats["duplicates_skipped"] == 0
    row = m.verified["ev_new"]
    assert row["Verified By"] == "Rules" and row["Category"] == "Tax" and row["Location"] == "Sydney Town Hall"
