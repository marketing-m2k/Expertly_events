"""Tests for classification, verification, Master updates, site health and
the AI review checks. All use made-up pages and rows -- no website is
contacted and no real data is read or written.

    python -m pytest tests/
"""

from datetime import datetime

import openpyxl

from scraper.ai_review import evaluate_response, review_needs_review
from scraper.classify_rules import classify_event
from scraper.enrich import process_event
from scraper.event_id import make_event_id
from scraper.master_store import MasterData, VERIFIED_COLUMNS
from scraper.site_health import assess_sites, healthy_sources
from scraper.verify_events import resolve_country, verify_event
from scraper.evidence import field

TODAY = datetime(2026, 9, 24)
URL = "https://example.org/events/gst-workshop"

PAGE = """<html><head>
<script type="application/ld+json">
{"@type":"EducationEvent","name":"GST Audit Workshop","startDate":"2026-10-12","endDate":"2026-10-13",
 "eventAttendanceMode":"https://schema.org/OnlineEventAttendanceMode","url":"https://example.org/events/gst-workshop"}
</script></head><body><main><h1>GST Audit Workshop</h1>
<p><b>Date:</b> 12 October 2026 - 13 October 2026</p>
<p>A practical two-day workshop on GST audit for tax practitioners and finance teams.</p></main></body></html>"""


# ---- classification ------------------------------------------------------------

def test_clear_categories():
    assert classify_event("GST Audit Workshop")["category"] == "Tax"  # ties are settled by a fixed order
    assert classify_event("Transfer Pricing Masterclass")["category"] == "Tax"
    assert classify_event("International Arbitration Conference")["category"] == "Legal"
    assert classify_event("Treasury and Banking Summit")["category"] == "Finance"


def test_exam_prep_social_and_notices_are_rejected():
    assert classify_event("CFE Exam Review Course")["category"] == "Reject"
    assert classify_event("Annual Golf Outing")["category"] == "Reject"
    assert classify_event("Notice of Annual General Meeting")["category"] == "Reject"
    assert classify_event("Quarterly Issue #63 Newsletter")["category"] == "Reject"


def test_dividend_taxation_webinar_is_not_a_corporate_notice():
    assert classify_event("Dividend Taxation Webinar")["category"] == "Tax"


def test_vague_titles_are_unclear_not_guessed():
    assert classify_event("Annual Conference 2026")["category"] == "Unclear"


# ---- event ids -----------------------------------------------------------------

def test_event_id_follows_the_event_page_not_the_title():
    a = make_event_id("ICAI", "GST Workshop", "https://www.example.org/e/1/", "https://example.org/events")
    b = make_event_id("ICAI", "GST Workshop (Updated title)", "https://example.org/e/1", "https://example.org/events")
    assert a == b


def test_event_id_without_own_page_uses_organizer_and_name():
    a = make_event_id("ICAI", "GST Workshop", "https://example.org/events", "https://example.org/events")
    b = make_event_id("ICAI", "gst  workshop", "", "https://example.org/events")
    assert a == b


# ---- verification --------------------------------------------------------------

def _f(value, source="label", confidence="high", match=None):
    return field(value, match or value, "test", source, confidence)


HAY = "GST Audit Workshop. Date: 12-Oct-2026. Venue: Taj Hotel, Mumbai."


def test_a_clean_event_passes():
    fields = {"name": _f("GST Audit Workshop"), "date": _f("12-Oct-2026")}
    assert verify_event(fields, "Tax", HAY, TODAY) == []


def test_past_date_fails():
    fields = {"name": _f("GST Audit Workshop"), "date": _f("12-Apr-2026", match="12-Apr-2026")}
    assert any("already passed" in f for f in verify_event(fields, "Tax", HAY + " 12-Apr-2026", TODAY))


def test_value_not_on_the_page_fails():
    fields = {"name": _f("GST Audit Workshop"), "date": _f("14-Oct-2026")}
    assert any("could not be found" in f for f in verify_event(fields, "Tax", HAY, TODAY))


def test_date_in_title_disagreeing_fails():
    fields = {"name": _f("Credit Group [10/11/2026]", match="Credit Group"), "date": _f("26-Dec-2026", match="26-Dec-2026")}
    hay = "Credit Group [10/11/2026] 26-Dec-2026"
    assert any("title disagrees" in f for f in verify_event(fields, "Finance", hay, TODAY))


def test_low_confidence_title_and_unclear_category_fail():
    fields = {"name": _f("GST Audit Workshop", source="meta", confidence="low"), "date": _f("12-Oct-2026")}
    failures = verify_event(fields, "Unclear", HAY, TODAY)
    assert any("low confidence" in f for f in failures)
    assert any("Tax, Finance or Legal" in f for f in failures)


def test_untracked_country_fails_and_location_beats_source_tab():
    fields = {"name": _f("GST Audit Workshop"), "date": _f("12-Oct-2026"), "location": _f("Beijing, China")}
    assert any("don't track" in f for f in verify_event(fields, "Tax", HAY + " Beijing, China", TODAY))
    assert resolve_country("Melbourne, Victoria", "India") == "AUS"
    assert resolve_country("", "India") == "India"


# ---- one event end to end, with re-scrape --------------------------------------

def test_process_event_verifies_a_good_page():
    row = {"name": "GST Audit Workshop", "organizer": "ICAI", "link": URL, "source_url": "https://example.org/events"}
    rec = process_event(row, {"html": PAGE, "status": 200, "error": None}, "India", lambda u, wait_ms: None, TODAY)
    assert rec["status"] == "verified" and rec["fields"]["date"]["value"] == "12-Oct-2026"
    assert rec["category"] in ("Tax", "Finance")


def test_failed_check_triggers_rescrape_then_review():
    row = {"name": "GST Audit Workshop", "organizer": "ICAI", "link": URL, "source_url": "https://example.org/events"}
    bare = "<html><body><main><h1>GST Audit Workshop</h1></main></body></html>"
    calls = []

    def refetch(url, wait_ms):
        calls.append(wait_ms)
        return {"html": bare, "status": 200, "error": None}

    rec = process_event(row, {"html": bare, "status": 200, "error": None}, "India", refetch, TODAY)
    assert calls == [3000, 6000]  # two more attempts, each waiting longer
    assert rec["status"] == "needs_review" and any("no event date" in r for r in rec["reasons"])


def test_rescrape_can_fix_a_slow_page():
    row = {"name": "GST Audit Workshop", "organizer": "ICAI", "link": URL, "source_url": "https://example.org/events"}
    bare = "<html><body><main><h1>GST Audit Workshop</h1></main></body></html>"
    rec = process_event(row, {"html": bare, "status": 200, "error": None}, "India",
                        lambda u, wait_ms: {"html": PAGE, "status": 200, "error": None}, TODAY)
    assert rec["status"] == "verified" and rec["attempts"] == 1


def test_unreachable_page_is_reported_not_guessed():
    row = {"name": "GST Audit Workshop", "organizer": "ICAI", "link": URL, "source_url": "https://example.org/events"}
    rec = process_event(row, {"html": None, "status": 404, "error": "HTTP 404"}, "India", lambda u, wait_ms: None, TODAY)
    assert rec["status"] == "unreachable" and rec["fields"] == {}


# ---- site health ---------------------------------------------------------------

def test_site_health_flags_failed_empty_and_big_drop():
    prev = {"a": {"events": 20}, "b": {"events": 20}, "c": {"events": 20}, "d": {"events": 20}}
    now = {"a": {"events": 0, "error": "timeout"}, "b": {"events": 0, "error": None},
           "c": {"events": 4, "error": None}, "d": {"events": 19, "error": None}}
    health = assess_sites(now, prev)
    assert [health[k]["status"] for k in "abcd"] == ["failed", "empty", "big_drop", "ok"]
    assert healthy_sources(health) == {"d"}


# ---- Master: in-place updates --------------------------------------------------

def _rec(eid="ev_1", status="verified", name="GST Audit Workshop", date="12-Oct-2026", changed=True, **extra):
    fields = {"name": _f(name), "date": _f(date)}
    fields.update(extra.pop("fields", {}))
    return {"event_id": eid, "status": status, "fields": fields, "country": "India", "organizer": "ICAI",
            "category": "Tax", "link": URL, "source_url": "https://example.org/events", "reasons": extra.pop("reasons", []),
            "changed": changed, **extra}


def _master(tmp_path):
    return MasterData(str(tmp_path / "Master.xlsx"), TODAY).load()


def test_new_event_is_added_and_logged(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    assert "ev_1" in m.verified and m.stats["added"] == 1
    assert m.log[-1][3] == "added"


def test_changed_website_updates_only_changed_fields(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    m.apply_record(_rec(date="19-Oct-2026"))
    row = m.verified["ev_1"]
    assert row["Date"] == "19-Oct-2026" and row["Event Name"] == "GST Audit Workshop"
    assert [e[4] for e in m.log if e[3] == "updated"] == ["Date"]


def test_blank_scrape_never_overwrites_a_value(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec(fields={"location": _f("Taj Hotel, Mumbai")}))
    m.apply_record(_rec())  # this week's scrape found no location
    assert m.verified["ev_1"]["Location"] == "Taj Hotel, Mumbai"


def test_unchanged_page_is_not_touched(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    m.apply_record(_rec(date="19-Oct-2026", changed=False))
    assert m.verified["ev_1"]["Date"] == "12-Oct-2026" and m.stats["unchanged"] == 1


def test_failed_rescrape_keeps_the_good_row(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    m.apply_record(_rec(status="needs_review", fields={}, reasons=["no event date"]))
    assert m.verified["ev_1"]["Date"] == "12-Oct-2026" and "ev_1" not in m.review
    assert m.stats["kept_after_failed_rescrape"] == 1


def test_locked_row_is_never_changed(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    m.verified["ev_1"]["Locked"] = "Yes"
    m.apply_record(_rec(date="19-Oct-2026"))
    assert m.verified["ev_1"]["Date"] == "12-Oct-2026" and m.stats["locked_skipped"] == 1


def test_review_event_is_promoted_when_it_later_verifies(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec(status="needs_review", reasons=["no event date"]))
    assert "ev_1" in m.review
    m.apply_record(_rec())
    assert "ev_1" in m.verified and "ev_1" not in m.review


def test_rejected_event_moves_with_a_record(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    m.apply_record(_rec(status="rejected", reasons=["exam prep: 'exam review'"]))
    assert "ev_1" not in m.verified and "ev_1" in m.rejected
    assert any(e[3] == "rejected" for e in m.log)


def test_cross_country_duplicate_is_not_added_twice(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec(eid="ev_1"))
    m.apply_record(_rec(eid="ev_2"))
    assert list(m.verified) == ["ev_1"] and m.stats["duplicates_skipped"] == 1


def test_past_event_moves_to_past_events(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec(date="01-Sep-2026"))
    m.apply_missing_and_past(set(), {"ev_1"})
    assert "ev_1" in m.past and "ev_1" not in m.verified


def test_missing_event_waits_two_weeks_then_goes_to_review(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    row = m.verified["ev_1"]
    m.apply_missing_and_past({"https://example.org/events"}, set())
    assert "ev_1" in m.verified  # just seen: still inside the grace period
    row["Last Seen on Site"] = "01-Sep-2026"
    m.apply_missing_and_past({"https://example.org/events"}, set())
    assert "ev_1" in m.review and "Not found" in m.review["ev_1"]["Review Reason"]


def test_event_on_an_unhealthy_site_is_never_marked_missing(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    m.verified["ev_1"]["Last Seen on Site"] = "01-Jan-2026"
    m.apply_missing_and_past(set(), set())  # the site is not in the healthy set
    assert "ev_1" in m.verified


def test_save_and_reload_round_trip_with_backup(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec())
    m.save()
    m2 = _master(tmp_path)
    assert m2.verified["ev_1"]["Event Name"] == "GST Audit Workshop"
    assert [e[3] for e in m2.log] == ["added"]
    m2.save()
    assert list((tmp_path / "archive").glob("Master_*.xlsx"))  # the previous file was backed up first


def test_legacy_master_rows_get_event_ids_without_data_loss(tmp_path):
    path = tmp_path / "Master.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Verified Events"
    ws.append(["Country", "Date", "End Date", "Status", "Event Name", "Organizer", "Category", "Format", "Location",
               "Description", "Register Link", "Source URL"])
    ws.append(["AUS", "01-Dec-2026", None, "Upcoming", "Melbourne Tax Discussion Group", "IFPA", "Tax, Accounting & Finance",
               None, None, None, "https://ifpa.com.au/event/melbourne-tax-dg-dec-2026/", "https://ifpa.com.au/events/"])
    wb.save(path)
    m = MasterData(str(path), TODAY).load()
    (eid, row), = m.verified.items()
    assert eid == make_event_id("IFPA", "Melbourne Tax Discussion Group",
                                "https://ifpa.com.au/event/melbourne-tax-dg-dec-2026/", "https://ifpa.com.au/events/")
    assert row["Date"] == "01-Dec-2026" and row["Verified By"].startswith("Legacy")
    assert set(VERIFIED_COLUMNS) <= set(row)


# ---- AI review: Gemini can't slip anything past the checks -----------------------

def _review_master(tmp_path):
    m = _master(tmp_path)
    m.apply_record(_rec(status="needs_review", fields={}, reasons=["no event date"]))
    m.review["ev_1"]["Register Link"] = URL
    return m


def _fetch(urls, workers=5):
    return {u: {"html": PAGE, "status": 200, "error": None} for u in urls}


GOOD = {"decision": "verified", "category": "Tax", "reason": "GST audit workshop for tax practitioners",
        "name": {"value": "GST Audit Workshop", "quote": "GST Audit Workshop"},
        "date": {"value": "12 October 2026", "quote": "12 October 2026 - 13 October 2026"}}


def test_ai_review_promotes_only_a_fully_quoted_answer(tmp_path):
    m = _review_master(tmp_path)
    stats = review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: GOOD)
    assert stats["verified"] == 1 and m.verified["ev_1"]["Verified By"] == "AI review"
    assert m.verified["ev_1"]["Date"] == "12-Oct-2026" and m.verified["ev_1"]["End Date"] == "13-Oct-2026"


def test_ai_quote_that_is_not_on_the_page_is_not_trusted(tmp_path):
    bad = {**GOOD, "date": {"value": "20 October 2026", "quote": "20 October 2026"}}
    m = _review_master(tmp_path)
    review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: bad)
    assert "ev_1" in m.review and "not on the page" in m.review["ev_1"]["AI Review"]


def test_ai_date_value_is_ignored_the_quote_decides(tmp_path):
    sneaky = {**GOOD, "date": {"value": "1 January 2030", "quote": "12 October 2026 - 13 October 2026"}}
    decision, fields, problems, _ = evaluate_response(sneaky, PAGE, TODAY)
    assert fields["date"]["value"] == "12-Oct-2026"


def test_ai_cannot_promote_an_event_that_fails_the_checks(tmp_path):
    no_category = {**GOOD, "category": "None"}
    m = _review_master(tmp_path)
    review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: no_category)
    assert "ev_1" in m.review and "Tax, Finance or Legal" in m.review["ev_1"]["AI Review"]


def test_ai_reject_needs_a_reason_and_is_recorded(tmp_path):
    m = _review_master(tmp_path)
    review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: {"decision": "rejected", "reason": "It is a social mixer"})
    assert "ev_1" in m.rejected and m.rejected["ev_1"]["Reject Reason"] == "It is a social mixer"


def test_ai_reviews_each_event_once(tmp_path):
    m = _review_master(tmp_path)
    calls = []
    unclear = {"decision": "unclear", "reason": "page is vague"}
    review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: calls.append(p) or unclear)
    review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: calls.append(p) or unclear)
    assert len(calls) == 1


def test_ai_review_stops_cleanly_when_the_budget_is_reached(tmp_path):
    from scraper.api_budget import BudgetExceededError

    def over_budget(prompt):
        raise BudgetExceededError("cap reached")

    m = _review_master(tmp_path)
    stats = review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=over_budget)
    assert stats["stopped_by_budget"] and "ev_1" in m.review


# ---- found by the first AUS trial (24 Sep 2026) ----------------------------------

def test_events_sharing_one_page_all_survive_loading_master(tmp_path):
    # 125 rows of the real Master shared a page with another row and overwrote each other
    path = tmp_path / "Master.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Verified Events"
    ws.append(["Country", "Date", "Event Name", "Organizer", "Register Link", "Source URL"])
    ws.append(["AUS", "16-Sep-2026", "FAAA Series", "FAAA", "https://faaa.au/series/x/", "https://faaa.au/events/"])
    ws.append(["AUS", "28-Oct-2026", "FAAA Series", "FAAA", "https://faaa.au/series/x/", "https://faaa.au/events/"])
    wb.save(path)
    m = MasterData(str(path), TODAY).load()
    assert len(m.verified) == 2 and sorted(r["Date"] for r in m.verified.values()) == ["16-Sep-2026", "28-Oct-2026"]
    m.save()
    assert len(MasterData(str(path), TODAY).load().verified) == 2  # ids are stable after saving


def test_html_entities_in_structured_titles_are_decoded():
    from scraper.structured_data import extract_structured_events
    page = ('<script type="application/ld+json">{"@type":"Event","name":"Series &#8211; Session 2",'
            '"startDate":"2026-10-12"}</script>')
    assert extract_structured_events(page, URL)[0]["name"]["value"] == "Series – Session 2"


def test_event_that_already_happened_is_past_not_needs_review():
    row = {"name": "GST Audit Workshop", "organizer": "ICAI", "link": URL, "source_url": "https://example.org/events"}
    rec = process_event(row, {"html": PAGE, "status": 200, "error": None}, "India", lambda u, wait_ms: None,
                        datetime(2026, 12, 1))
    assert rec["status"] == "past"


def test_page_that_fails_to_open_is_retried_but_a_404_is_final():
    row = {"name": "GST Audit Workshop", "organizer": "ICAI", "link": URL, "source_url": "https://example.org/events"}
    ok = {"html": PAGE, "status": 200, "error": None}
    rec = process_event(row, {"html": None, "status": None, "error": "timeout"}, "India", lambda u, wait_ms: ok, TODAY)
    assert rec["status"] == "verified"
    calls = []
    rec = process_event(row, {"html": None, "status": 404, "error": "HTTP 404"}, "India",
                        lambda u, wait_ms: calls.append(1), TODAY)
    assert rec["status"] == "unreachable" and not calls


def test_list_pages_and_shared_pages_are_not_treated_as_one_events_page():
    from collections import Counter
    from scraper.enrich import has_own_page
    counts = Counter({"example.org/e/1": 1, "faaa.au/series/x": 2})
    assert has_own_page("https://example.org/e/1", "https://example.org/events", counts)
    assert not has_own_page("https://faaa.au/series/x", "https://faaa.au/events", counts)   # shared by two events
    assert not has_own_page("https://example.org/events", "https://example.org/events/", counts)  # the list page
    assert not has_own_page("http://www.google.com/calendar/event?action=TEMPLATE", "https://a.org/e", counts)
    assert not has_own_page("", "https://a.org/e", counts)


def test_events_on_shared_pages_are_never_marked_missing(tmp_path):
    m = _master(tmp_path)
    for eid, date in (("ev_a", "20-Oct-2026"), ("ev_b", "27-Oct-2026")):
        m.verified[eid] = {"Event ID": eid, "Event Name": "Series", "Date": date, "Register Link": "https://x.org/series/1",
                           "Source URL": "https://x.org/events", "Last Seen on Site": "01-Jan-2026", "Locked": ""}
    m.apply_missing_and_past({"https://x.org/events"}, set())
    assert set(m.verified) == {"ev_a", "ev_b"}


def test_listing_style_titles_are_not_event_names():
    from scraper.label_extract import is_generic_title
    for bad in ("Events Archive", "Events Calendar", "Event Display", "Conferences and events", "Events & CPD",
                "Events - Energy & Resources Law Association"):
        assert is_generic_title(bad), bad
    assert not is_generic_title("China Asset Management Forum in Australia 2026")


def test_og_title_shown_in_the_page_content_is_high_confidence():
    from scraper.label_extract import extract_labelled
    html = ('<html><head><meta property="og:title" content="AIMA Australia Annual Forum 2026"></head>'
            '<body><main><div>AIMA Australia Annual Forum 2026</div></main></body></html>')
    name = extract_labelled(html, URL)["fields"]["name"]
    assert name["confidence"] == "high"
    unseen = ('<html><head><meta property="og:title" content="AIMA Australia Annual Forum 2026"></head>'
              '<body><main><div>Something else entirely</div></main></body></html>')
    assert extract_labelled(unseen, URL)["fields"]["name"]["confidence"] == "medium"


# ---- AI review fixes from the first AUS trial ------------------------------------

def test_missing_page_text_is_never_a_reason_to_reject(tmp_path):
    m = _review_master(tmp_path)
    review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: {
        "decision": "rejected", "reason": "The page text is only a cookie consent notice with no information about the event"})
    assert "ev_1" in m.review and "ev_1" not in m.rejected
    assert "could not be read" in m.review["ev_1"]["AI Review"]


def test_ai_marks_an_already_past_event_as_past_not_left_in_review(tmp_path):
    past_page = PAGE.replace("2026-10-12", "2026-08-12").replace("2026-10-13", "2026-08-13") \
                    .replace("12 October 2026 - 13 October 2026", "12 August 2026 - 13 August 2026")
    resp = {**GOOD, "date": {"value": "12 August 2026", "quote": "12 August 2026 - 13 August 2026"}}
    m = _review_master(tmp_path)
    review_needs_review(m, TODAY, fetch_fn=lambda urls, workers=5: {u: {"html": past_page, "status": 200, "error": None} for u in urls},
                        call_fn=lambda p: resp)
    assert "ev_1" in m.rejected and "already passed" in m.rejected["ev_1"]["Reject Reason"]


def test_a_page_that_could_not_be_opened_is_retried_next_run(tmp_path):
    m = _review_master(tmp_path)
    down = lambda urls, workers=5: {u: {"html": None, "status": 405, "error": "HTTP 405"} for u in urls}
    review_needs_review(m, TODAY, fetch_fn=down, call_fn=lambda p: GOOD)
    assert m.review["ev_1"]["AI Review"].startswith("AI review skipped")
    stats = review_needs_review(m, TODAY, fetch_fn=_fetch, call_fn=lambda p: GOOD)  # the page works this time
    assert stats["verified"] == 1 and "ev_1" in m.verified


def test_prompt_carries_the_exclusion_rules_from_the_qc_comments():
    from scraper.ai_review import PROMPT
    for phrase in ("board-governance", "social or networking", "exam-prep", "NEVER reject"):
        assert phrase in PROMPT
