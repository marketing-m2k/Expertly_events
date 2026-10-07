"""Phases 4-5: quality flags (never rejections), field-level re-scrape, browser fallback, versioned reprocessing."""

from datetime import date

import pytest

from pipeline.extraction.detail import extract_event
from pipeline.fetcher import Page
from pipeline.models import Flag, Status
from pipeline.quality import assess_quality
from pipeline.rescrape import rescrape, targets_for
from tests.pages import RELATED, TODAY, URL, event_page, jsonld_event


def ex(html, **kw):
    return extract_event(html, URL, org_name="Example Institute", today=TODAY, **kw)


def flags(html, **kw):
    ev = ex(html, **kw)
    return assess_quality(ev, today=TODAY, card_date_hint=kw.get("card_date_hint", "")).flags


def test_clean_event_has_no_blocking_flags():
    q = assess_quality(ex(event_page(fmt="In person", address="2 Mount Road, Chennai", desc="<p>" + "Practical GST audit readiness for practitioners. " * 4 + "</p>")), today=TODAY)
    assert not q.hard_blocks


def test_missing_fields_are_flagged():
    f = flags(event_page(date_line=None, venue=None))
    assert Flag.MISSING_DATE in f and Flag.MISSING_LOCATION in f and Flag.MISSING_FORMAT in f


def test_missing_name():
    f = flags("<html><body><main><p>" + "Some text about nothing in particular, long enough to be readable by the extractor. " * 4 + "</p></main></body></html>")
    assert Flag.MISSING_NAME in f


def test_year_missing_flag_and_missing_date():
    f = flags(event_page(date_line="12 October"))
    assert Flag.YEAR_MISSING in f and Flag.MISSING_DATE in f


def test_conflicting_date_flag():
    f = flags(event_page(jsonld=jsonld_event(start="2026-11-20"), date_line="12 October 2026"))
    assert Flag.CONFLICTING_DATE in f


def test_conflicting_location_flag():
    html = event_page(jsonld=jsonld_event(venue="Grand Hotel", city="Pune"), venue="Hilton Chennai")
    assert Flag.CONFLICTING_LOCATION in flags(html)


def test_weak_name_flag_for_generic_titles():
    f = flags(event_page(title="Webinar", h1="Webinar", title_tag="Webinar"))
    assert Flag.WEAK_NAME in f


def test_weak_date_when_only_the_card_states_it():
    f = flags(event_page(date_line=None), card_date_hint="12 October 2026")
    assert Flag.WEAK_DATE in f


def test_card_date_conflicting_with_page_date_is_recorded_page_wins():
    ev = ex(event_page(date_line="12 October 2026"))
    q = assess_quality(ev, today=TODAY, card_date_hint="14 October 2026")
    fv = ev.fields["start_date"]
    assert fv.value == "2026-10-12" and any(a["source"] == "card" for a in fv.alternatives) and "page used" in fv.note


def test_description_flags():
    assert Flag.WEAK_DESCRIPTION in flags(event_page(desc="<p>Short.</p>"))


def test_suspicious_dates():
    assert Flag.SUSPICIOUS_VALUE in flags(event_page(date_line="12 October 2035"))
    assert Flag.SUSPICIOUS_VALUE in flags(event_page(date_line="12 October 2021"))


def test_unreadable_page_flag_and_no_detail_page_flag():
    ev = ex("<html><body><div id='root'></div></body></html>")
    assert Flag.PAGE_UNREADABLE in assess_quality(ev, today=TODAY).flags
    assert Flag.NO_DETAIL_PAGE in assess_quality(ex(event_page()), today=TODAY, has_own_page=False).flags


def test_foreign_jsonld_is_recorded_as_a_flag_not_used():
    html = event_page(jsonld=jsonld_event(name="Other Thing", url="https://inst.org/events/other", start="2027-01-01"))
    q = assess_quality(ex(html), today=TODAY)
    assert Flag.JSONLD_FOREIGN in q.flags and Flag.JSONLD_FOREIGN not in Flag.HARD_BLOCKS


def test_flags_are_quality_flags_not_rejection_reasons():
    """The flag vocabulary contains no verdicts: nothing in it maps to REJECTED."""
    from pipeline.status import resolve_status
    ev = ex(event_page(date_line=None))
    q = assess_quality(ev, today=TODAY)
    d = resolve_status(ev, q, {"verdict": "in_scope_strong"}, None, TODAY, review_wanted=False, ai_enabled=False)
    assert d.status in (Status.QUALITY_REVIEW,) and d.status != Status.REJECTED


# ---- field-level re-scrape ---------------------------------------------------------------------------------------------------

def test_targets_are_per_field():
    assert targets_for([Flag.MISSING_DATE]) == ["start_date"]
    assert set(targets_for([Flag.MISSING_NAME, Flag.MISSING_LOCATION, Flag.MISSING_FORMAT])) == {"name", "venue", "format"}


def test_alternate_extractor_resolves_a_venue_on_the_same_page():
    html = event_page(venue=None, date_line=None, desc="<p class='sub'>Wednesday, 11 November 2026 9.00am - 6.00pm Sands Expo &amp; Convention Centre</p>"
                      "<p>" + "Reimagining the future: resilience in a multipolar economy for finance leaders. " * 3 + "</p>")
    ev = ex(html)
    q = assess_quality(ev, today=TODAY)
    assert Flag.MISSING_LOCATION in q.flags
    ev, q = rescrape(ev, q, today=TODAY)
    assert ev.fields["venue"].value == "Sands Expo & Convention Centre"
    trail = [(a["field"], a["method"], a["outcome"]) for a in ev.attempts if a["field"] == "venue"]
    assert trail[0][1] == "extraction" and any(m == "existing_content" and o == "resolved" for _, m, o in trail)


def test_only_the_failed_field_is_retried():
    ev = ex(event_page(venue=None))
    q = assess_quality(ev, today=TODAY)
    before = ev.fields["start_date"].to_dict()
    ev, q = rescrape(ev, q, today=TODAY)
    assert ev.fields["start_date"].to_dict() == before
    assert {a["field"] for a in ev.attempts} <= {"venue", "format"}


def test_retry_ladder_http_then_browser_and_attempt_trail():
    shell = "<html><body><div id='root'></div><script>app()</script></body></html>"
    rendered = event_page()
    calls = []

    def http(url):
        calls.append("http")
        return Page(url=url, html=shell, status=200)

    def browser(url):
        calls.append("browser")
        return Page(url=url, final_url=url, html=rendered, status=200, method="browser")

    ev = ex(shell, card_title="GST Audit Workshop")
    q = assess_quality(ev, today=TODAY)
    assert Flag.PAGE_UNREADABLE in q.flags
    ev, q = rescrape(ev, q, fetch_http=http, fetch_browser=browser, today=TODAY)
    assert calls == ["http", "browser"] or calls == ["browser"] or "browser" in calls
    assert ev.fields["start_date"].value == "2026-10-12" and ev.readable
    methods = [a["method"] for a in ev.attempts if a["field"] == "start_date"]
    assert "browser_render" in methods and methods[0] == "extraction"


def test_unresolvable_field_ends_as_unresolved_with_a_full_trail():
    ev = ex(event_page(date_line=None))
    q = assess_quality(ev, today=TODAY)
    ev, q = rescrape(ev, q, fetch_http=lambda u: Page(url=u, html=event_page(date_line=None), status=200),
                     fetch_browser=lambda u: Page(url=u, html=event_page(date_line=None) + "<!-- rendered -->", status=200, method="browser"), today=TODAY)
    trail = [a for a in ev.attempts if a["field"] == "start_date"]
    assert trail[-1]["outcome"] == "unresolved" and trail[-1]["method"] == "final"
    assert ev.fields["start_date"].value is None and Flag.MISSING_DATE in q.flags
    assert len(trail) >= 3                                            # extraction, existing content, browser, final


def test_rescrape_does_nothing_for_events_without_a_page_of_their_own():
    ev = ex(event_page(date_line=None))
    q = assess_quality(ev, today=TODAY, has_own_page=False)
    ev2, q2 = rescrape(ev, q, today=TODAY, has_own_page=False)
    assert ev2.attempts == []


# ---- reprocessing when the extractor changes ---------------------------------------------------------------------------------------

def test_extractor_version_change_makes_old_results_eligible(tmp_path):
    from pipeline.models import EXTRACTOR_VERSION
    from pipeline.reprocess import select_for_reprocess
    from pipeline.store import Store
    st = Store(str(tmp_path / "s.sqlite"))
    rec = {"event_id": "ev_1", "org": "O", "country": "UK", "source_url": "https://o/e", "url": "https://o/e/1", "status": Status.VERIFIED, "category": "Tax",
           "reasons": [], "flags": [], "fields": {"name": {"value": "X"}}, "packet": {}, "extractor_version": "1.0.0", "url_key": "o/e/1"}
    st.upsert(rec, "r1")
    rec2 = {**rec, "event_id": "ev_2", "url_key": "o/e/2", "url": "https://o/e/2", "extractor_version": EXTRACTOR_VERSION}
    st.upsert(rec2, "r1")
    ids = {a.event_id: a.reasons for a in select_for_reprocess(st)}
    assert "ev_1" in ids and "ev_2" not in ids
    assert "extractor 1.0.0" in ids["ev_1"][0]


def test_previously_rejected_or_flagged_records_are_reconsidered(tmp_path):
    from pipeline.reprocess import select_for_reprocess
    from pipeline.store import Store
    st = Store(str(tmp_path / "s.sqlite"))
    base = {"org": "O", "country": "UK", "source_url": "https://o/e", "category": "", "reasons": ["x"], "fields": {}, "packet": {}, "extractor_version": "2.0.0"}
    st.upsert({**base, "event_id": "ev_r", "url": "https://o/e/r", "url_key": "o/e/r", "status": Status.REJECTED, "flags": []}, "r1")
    st.upsert({**base, "event_id": "ev_n", "url": "https://o/e/n", "url_key": "o/e/n", "status": Status.NEEDS_REVIEW, "flags": ["missing_date"]}, "r1")
    st.upsert({**base, "event_id": "ev_v", "url": "https://o/e/v", "url_key": "o/e/v", "status": Status.VERIFIED, "flags": []}, "r1")
    ids = {a.event_id for a in select_for_reprocess(st)}
    assert ids == {"ev_r", "ev_n"}


def test_reprocess_uses_the_saved_page_and_keeps_history(tmp_path):
    from pipeline.fetcher import ReplayFetcher
    from pipeline.process import Deps, process_candidate
    from pipeline.models import Candidate
    from pipeline.reprocess import reprocess, select_for_reprocess
    from pipeline.store import Store
    st = Store(str(tmp_path / "s.sqlite"))
    cand = Candidate(candidate_id="c1", org="Example Institute", country="UK", source_url="https://inst.org/events", listing_url="https://inst.org/events",
                     url=URL, title="GST Audit Workshop", card_text="GST Audit Workshop 12 October 2026", date_hint="12 October 2026")
    deps = Deps(http=ReplayFetcher({URL: event_page(fmt="In person")}), today=TODAY, run_id="r1", review_mode="off", ledger=st.ledger, store=st)
    res = process_candidate(cand, deps)
    row = res.to_row()
    row["extractor_version"] = "1.0.0"                                  # pretend it was extracted by an older extractor
    st.upsert(row, "r1")
    assert st.load_page(res.event_id)                                    # the raw page evidence was preserved
    deps2 = Deps(http=ReplayFetcher({}), today=TODAY, run_id="r2", review_mode="off", ledger=st.ledger, store=st)
    affected = select_for_reprocess(st)
    assert [a.event_id for a in affected] == [res.event_id]
    stats = reprocess(st, deps2, affected, "r2")
    assert stats["reprocessed"] == 1 and stats["errors"] == 0           # served from the stored copy: the fetcher knew nothing
    assert st.get(res.event_id)["extractor_version"] != "1.0.0"
    assert len(st.history("status_history", res.event_id)) >= 1
