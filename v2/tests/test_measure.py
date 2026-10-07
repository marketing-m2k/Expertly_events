"""The measurement system itself: matching, recall formula, duplicate/extra accounting, miss diagnosis."""

import json
import os

from measure.match import aggregate, load_baselines, match_org
from measure.report import render_markdown
from measure.v1_adapter import diagnose_miss, run_v1
from tests.helpers import card_article, cards, page


def base(events, **kw):
    return {"slug": "t", "org": "T", "url": "https://ex.org/events", "events": events, "expected_state": "discovery_complete", **kw}


def ev(title, url="", valid=True):
    return {"title": title, "url": url, "valid": valid, "date_text": ""}


def test_recall_formula_is_found_valid_over_true_valid():
    b = base([ev("Alpha", "https://ex.org/a"), ev("Beta", "https://ex.org/b"), ev("Gamma", "https://ex.org/c"), ev("Series", valid=False)])
    out = {"candidates": [{"title": "Alpha", "url": "https://ex.org/a"}, {"title": "Beta event", "url": "https://ex.org/b"}], "state": "discovery_complete"}
    m = match_org(b, out)
    assert m.true_count == 3 and m.matched_valid == 2 and m.recall == 66.7
    assert [x["title"] for x in m.missed] == ["Gamma"]


def test_matching_uses_url_first_and_does_not_confuse_same_titled_events():
    b = base([ev("Ethics Training", "https://ex.org/ne"), ev("Ethics Training", "https://ex.org/dc")])
    out = {"candidates": [{"title": "Ethics Training", "url": "https://ex.org/dc"}, {"title": "Ethics Training", "url": "https://ex.org/ne"}]}
    m = match_org(b, out)
    assert m.matched_valid == 2 and not m.duplicates and not m.extras


def test_duplicates_and_extras_are_counted_separately():
    b = base([ev("Alpha", "https://ex.org/a")])
    out = {"candidates": [{"title": "Alpha", "url": "https://ex.org/a"}, {"title": "Alpha", "url": "https://ex.org/a"},
                          {"title": "Contact us", "url": "https://ex.org/contact"}]}
    m = match_org(b, out)
    assert m.matched_valid == 1 and len(m.duplicates) == 1 and len(m.extras) == 1 and m.discovered_count == 3


def test_neutral_baseline_entries_are_neither_missed_nor_extra():
    b = base([ev("Alpha", "https://ex.org/a"), ev("On demand recording", "https://ex.org/od", valid=False)])
    m = match_org(b, {"candidates": [{"title": "Alpha", "url": "https://ex.org/a"}, {"title": "On demand recording", "url": "https://ex.org/od"}]})
    assert m.matched_valid == 1 and m.matched_neutral == 1 and not m.extras and m.recall == 100.0


def test_past_extras_measure_archive_leakage():
    b = base([ev("Alpha", "https://ex.org/a")])
    out = {"candidates": [{"title": "Alpha", "url": "https://ex.org/a"},
                          {"title": "Old one", "url": "https://ex.org/o", "date_hint": "12 March 2025"},
                          {"title": "Newer old one", "url": "https://ex.org/o2", "card_text": "Held on 3 May 2026"}]}
    assert match_org(b, out).past_extras == 2


def test_silent_empty_and_failed_state_are_detected():
    b = base([ev("Alpha", "https://ex.org/a")])
    assert match_org(b, {"candidates": [], "state": ""}).silently_empty                    # nothing found, nothing reported
    assert not match_org(b, {"candidates": [], "state": "discovery_failed", "errors": ["x"]}).silently_empty
    broken = base([], expected_state="discovery_failed")
    assert match_org(broken, {"candidates": [], "state": "discovery_complete"}).silently_empty
    assert match_org(broken, {"candidates": [], "state": "discovery_failed"}).state_correct is True


def test_dropped_reasons_are_tallied():
    b = base([ev("Alpha", "https://ex.org/a")])
    out = {"candidates": [], "dropped": [{"reason": "archive_section"}, {"reason": "archive_section"}, {"reason": "weak_link_no_event_evidence"}]}
    assert match_org(b, out).drop_reasons == {"archive_section": 2, "weak_link_no_event_evidence": 1}


def test_aggregate_and_report_render():
    b = base([ev("Alpha", "https://ex.org/a"), ev("Beta", "https://ex.org/b")])
    m = match_org(b, {"candidates": [{"title": "Alpha", "url": "https://ex.org/a"}], "state": "discovery_incomplete", "failure_kinds": ["pagination"]})
    agg = aggregate([m])
    assert agg["recall_pct"] == 50.0 and agg["missed"] == 1 and agg["failure_kinds"] == {"pagination": 1}
    md = render_markdown({"engine": "x", "mode": "y", "aggregate": agg, "orgs": [m.to_dict()]})
    assert "Discovery recall: 50.0%" in md and "Beta" in md


def test_v1_adapter_diagnoses_why_it_missed_an_event():
    """v1 needs >= 2 identical sibling cards: a single event page is lost, a generic-titled card is discarded."""
    single = page("<h1>Events</h1>" + card_article(1))
    out = run_v1("https://ex.org/events", htmls=[single])
    assert out["candidates"] == [] or len(out["candidates"]) <= 1
    miss = {"title": "Tax Workshop 1", "url": "https://ex.org/events/tax-workshop-1-1"}
    assert diagnose_miss([single], "https://ex.org/events", miss) in ("no_repeated_card_group", "in_page_but_not_extracted", "card_group_low_score")
    assert diagnose_miss([], "https://ex.org/events", miss) == "fetch_failed"
    assert diagnose_miss([single], "https://ex.org/events", {"title": "Totally absent event", "url": ""}) == "not_in_fetched_pages"
    generic = page("".join(f"<article><h3>Webinar</h3><p>{i + 10} November 2026</p><a href='https://ex.org/e/w{i}'>Webinar</a></article>" for i in range(3)))
    assert diagnose_miss([generic], "https://ex.org/events", {"title": "Webinar", "url": "https://ex.org/e/w1"}) in ("generic_title_discarded", "in_page_but_not_extracted")


def test_baseline_files_are_internally_consistent():
    baselines = load_baselines("baseline")
    assert len(baselines) >= 10
    for b in baselines:
        valid = [e for e in b["events"] if e["valid"]]
        assert b["true_event_count"] == len(valid), b["slug"]
        assert b["expected_state"] in ("discovery_complete", "discovery_incomplete", "discovery_failed")
        assert b.get("verified_by") and b.get("snapshot_date")
        for e in valid:
            assert e["title"]
    assert next(b for b in baselines if b["slug"] == "icsi")["expected_state"] == "discovery_failed"
