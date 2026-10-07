"""Phase 7: classification produces SIGNALS; a keyword can never reject an event."""

import pytest

from pipeline.classify import classify_signals
from pipeline.extraction.detail import extract_event
from pipeline.models import Status
from pipeline.quality import assess_quality
from pipeline.status import resolve_status
from tests.pages import TODAY, URL, event_page


def test_valid_tax_event():
    s = classify_signals("GST Audit Workshop", "A practical session on GST returns and audits for tax practitioners")
    assert s["category_guess"] == "Tax" and s["verdict"] == "in_scope_strong" and not s["reject_signals"]


def test_valid_finance_event():
    s = classify_signals("Treasury and Banking Summit")
    assert s["category_guess"] == "Finance" and s["verdict"] == "in_scope_strong"


def test_valid_legal_event():
    s = classify_signals("International Arbitration Conference")
    assert s["category_guess"] == "Legal" and s["verdict"] == "in_scope_strong"


def test_false_keyword_rejection_annual_tax_dinner_is_a_conflict_not_a_reject():
    s = classify_signals("Annual Tax Dinner & Update")
    assert s["verdict"] == "conflict" and s["needs_review"]
    assert {r["match"].lower() for r in s["reject_signals"]} == {"dinner"}
    assert s["category_guess"] == "Tax"                                  # the topic is still seen


def test_signals_never_reject_by_themselves():
    for title in ("Annual Golf Outing", "Notice of Annual General Meeting", "CFE Exam Review Course", "Quarterly Issue #63 Newsletter",
                  "Annual Tax Dinner & Update"):
        s = classify_signals(title)
        assert "category" not in s or s.get("verdict") != "rejected"
        assert s["verdict"] in ("conflict", "possibly_out_of_scope", "unclear_none", "unclear_weak", "in_scope_strong")
        assert s["needs_review"] or s["verdict"] == "in_scope_strong"


def test_irrelevant_event_is_flagged_for_review_with_its_signals():
    s = classify_signals("Annual Golf Outing")
    assert s["verdict"] == "possibly_out_of_scope" and s["reject_signals"][0]["type"] == "social event"


def test_unclear_event_is_unclear_not_rejected():
    assert classify_signals("Annual Conference 2026")["verdict"] in ("unclear_none", "unclear_weak")


def test_status_resolution_cannot_reject_on_signals_alone():
    ev = extract_event(event_page(title="Annual Golf Outing", date_line="12 October 2026", fmt="In person", address="2 Mount Road, Chennai"), URL, org_name="X", today=TODAY)
    q = assess_quality(ev, today=TODAY)
    sig = classify_signals("Annual Golf Outing")
    for review_wanted, ai in ((False, False), (True, False), (False, True)):
        d = resolve_status(ev, q, sig, None, TODAY, review_wanted=review_wanted, ai_enabled=ai)
        assert d.status != Status.REJECTED
    assert d.status in (Status.NEEDS_AI_REVIEW, Status.QUALITY_REVIEW)


def test_in_scope_event_with_clean_extraction_is_verified_by_rules_when_no_ai_is_needed():
    ev = extract_event(event_page(fmt="In person", address="2 Mount Road, Chennai", desc="<p>" + "Practical GST audit readiness for tax practitioners and finance teams. " * 3 + "</p>"),
                       URL, org_name="X", today=TODAY, card_title="GST Audit Workshop")
    from pipeline.verify import verify_event
    verify_event(ev)
    q = assess_quality(ev, today=TODAY)
    d = resolve_status(ev, q, classify_signals("GST Audit Workshop", ev.fields["description"].value), None, TODAY, review_wanted=False, ai_enabled=True)
    assert d.status == Status.VERIFIED and d.category == "Tax"


def test_taxonomy_is_extendable_from_the_file(tmp_path, monkeypatch):
    """Adding a topic to taxonomy.json needs no code change."""
    import json
    from scraper import classify_rules
    raw = json.load(open(classify_rules.TAXONOMY_PATH, encoding="utf-8"))
    raw["Legal"]["topics"]["Space Law"] = ["orbital debris liability"]
    p = tmp_path / "t.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(classify_rules, "TAXONOMY_PATH", str(p))
    classify_rules.load_taxonomy.cache_clear()
    try:
        assert classify_signals("Orbital Debris Liability Forum")["category_guess"] == "Legal"
    finally:
        monkeypatch.undo()
        classify_rules.load_taxonomy.cache_clear()
