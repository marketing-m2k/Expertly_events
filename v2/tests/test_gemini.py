"""Phase 8: Gemini reviews and corrects, but only code-verified evidence changes anything, an unreadable page is never
rejected, and API trouble never becomes a rejection."""

import pytest

from pipeline.fetcher import ReplayFetcher
from pipeline.gemini_review import ReviewOutcome, SCHEMA, input_hash_of, interpret_response
from pipeline.llm import BudgetExceeded, LLMBadResponse, LLMUnavailable
from pipeline.models import Candidate, Status
from pipeline.process import Deps, process_candidate
from pipeline.verify import SourceText
from tests.fakes import FakeLLM, corrected, correction, rejected, verified
from tests.pages import RELATED, TODAY, URL, event_page

GOOD_DESC = "<p>" + "A practical workshop on GST audit readiness for tax practitioners and finance teams, covering notices and reconciliations. " * 2 + "</p>"


def cand(title="GST Audit Workshop", date_hint="12 October 2026"):
    return Candidate(candidate_id="c1", org="Example Institute", country="UK", source_url="https://inst.org/events", listing_url="https://inst.org/events",
                     url=URL, title=title, card_text=f"{title} {date_hint}", date_hint=date_hint)


def run(html, llm, mode="all", title="GST Audit Workshop", date_hint="12 October 2026"):
    deps = Deps(http=ReplayFetcher({URL: html}), today=TODAY, run_id="r", review_mode=mode, llm=llm)
    return process_candidate(cand(title, date_hint), deps)


COMPLETE = event_page(fmt="In person", address="2 Mount Road, Chennai", desc=GOOD_DESC)


def test_valid_confirmation_verifies():
    llm = FakeLLM(verified())
    r = run(COMPLETE, llm)
    assert r.status == Status.VERIFIED and r.decision.category == "Tax" and len(llm.calls) == 1
    assert r.review.ai_status == "reviewed"


def test_gemini_receives_the_full_source_packet_not_just_name_host_tag():
    llm = FakeLLM(verified())
    run(COMPLETE, llm)
    prompt = llm.calls[0]
    for needle in ("official_url", "extracted_fields", "structured_page_text", "quality", "classification_signals", "jsonld", "discovery",
                   "evidence_quote", "Hilton Chennai"):
        assert needle in prompt, needle


def test_correction_with_valid_evidence_is_applied_and_logged():
    html = event_page(venue=None, date_line="12 October 2026", fmt="In person",
                      desc="<p>Held at the Hotel Taj Coromandel in Chennai. " + "A practical GST workshop for practitioners. " * 6 + "</p>")
    llm = FakeLLM(corrected(correction("venue", "Hotel Taj Coromandel", "Held at the Hotel Taj Coromandel in Chennai", old_value="")))
    r = run(html, llm)
    fv = r.ev.fields["venue"]
    assert fv.value == "Hotel Taj Coromandel" and fv.source == "gemini" and fv.verification == "verified"
    assert r.status == Status.CORRECTED and r.corrections[0]["field"] == "venue" and r.corrections[0]["evidence_quote"]


def test_correction_with_invalid_evidence_is_not_applied_and_goes_to_review():
    llm = FakeLLM(corrected(correction("venue", "Grand Palace Hotel", "The event is held at the Grand Palace Hotel")))
    r = run(event_page(venue=None, fmt="In person", desc=GOOD_DESC), llm)
    assert r.ev.fields["venue"].value is None
    assert r.status == Status.NEEDS_REVIEW and r.review.evidence_failures == 1
    assert "not on the official page" in r.review.corrections[0].failure


def test_a_quote_that_only_exists_in_a_related_event_block_fails():
    html = event_page(date_line=None, related=True, fmt="In person", venue="Hilton Chennai", desc=GOOD_DESC)
    llm = FakeLLM(corrected(correction("start_date", "2026-12-03", "VAT Update Seminar 3 December 2026")))
    r = run(html, llm, date_hint="")
    assert r.ev.fields["start_date"].value is None
    assert r.status == Status.NEEDS_REVIEW
    assert "related/contact/footer" in r.review.corrections[0].failure


def test_date_quote_without_a_year_fails_missing_year_is_not_guessed():
    html = event_page(date_line="12 October", fmt="In person", desc=GOOD_DESC)
    llm = FakeLLM(corrected(correction("start_date", "2026-10-12", "Date: 12 October")))
    r = run(html, llm, date_hint="")
    assert r.ev.fields["start_date"].value is None and r.status == Status.NEEDS_REVIEW
    assert "year" in r.review.corrections[0].failure


def test_deadline_date_cannot_be_used_as_the_event_date():
    html = event_page(date_line=None, extra_body="<p>Registration closes 5 October 2026</p>", fmt="In person", desc=GOOD_DESC)
    llm = FakeLLM(corrected(correction("start_date", "2026-10-05", "Registration closes 5 October 2026")))
    r = run(html, llm, date_hint="")
    assert r.ev.fields["start_date"].value is None and r.status == Status.NEEDS_REVIEW
    assert "deadline" in r.review.corrections[0].failure


def test_new_value_must_follow_from_the_quote():
    html = event_page(date_line="12 October", extra_body="<p>The conference runs on 14 November 2026 in Chennai.</p>", fmt="In person", desc=GOOD_DESC)
    llm = FakeLLM(corrected(correction("start_date", "2026-11-15", "The conference runs on 14 November 2026")))
    r = run(html, llm, date_hint="")
    assert r.ev.fields["start_date"].value is None and "does not match" in r.review.corrections[0].failure


def test_a_correct_date_correction_from_the_page_resolves_a_missing_year():
    html = event_page(date_line="12 October", extra_body="<p>The conference runs on 12 October 2026 in Chennai.</p>", fmt="In person", desc=GOOD_DESC)
    # the label says '12 October' (no year) but page text states it fully
    llm = FakeLLM(corrected(correction("start_date", "2026-10-12", "The conference runs on 12 October 2026")))
    r = run(html, llm, date_hint="")
    assert r.ev.fields["start_date"].value == "2026-10-12" and r.status == Status.CORRECTED


def test_source_url_must_be_the_official_site():
    llm = FakeLLM(corrected(correction("venue", "Hilton Chennai", "Venue: Hilton Chennai", source_url="https://evil.example.com/page")))
    r = run(event_page(fmt="In person", desc=GOOD_DESC), llm)
    assert "source URL" in r.review.corrections[0].failure and r.status == Status.NEEDS_REVIEW


def test_unreadable_page_is_never_sent_for_rejection():
    llm = FakeLLM(rejected("page has no information", "anything"))
    r = run("<html><body><div id='root'></div></body></html>", llm)
    assert r.status != Status.REJECTED and not llm.calls                 # not even asked
    assert r.status in (Status.QUALITY_REVIEW, Status.NEEDS_REVIEW)
    # and even if a model WERE to answer REJECTED about an unreadable page, the answer is refused
    ev = r.ev
    out = interpret_response(rejected("empty", "anything"), ev, SourceText.from_event(ev), {"strength": "none"})
    assert out.decision == "NEEDS_REVIEW" and out.rejection is None


def test_false_rejection_refused_when_the_reason_is_missing_information():
    llm = FakeLLM(rejected("The page does not provide enough information to judge", "GST Audit Workshop"))
    r = run(COMPLETE, llm)
    assert r.status == Status.NEEDS_REVIEW and "rejection not accepted" in " ".join(r.review.notes)


def test_false_rejection_refused_when_the_model_also_says_tax():
    llm = FakeLLM(rejected("It is a dinner", "GST Audit Workshop", category="Tax"))
    assert run(COMPLETE, llm).status == Status.NEEDS_REVIEW


def test_rejection_against_a_strong_topic_title_needs_a_person():
    llm = FakeLLM(rejected("It is a social event", "A practical workshop on GST audit readiness for tax practitioners and finance teams"))
    r = run(COMPLETE, llm)
    assert r.status == Status.NEEDS_REVIEW and "strong Tax/Finance/Legal topic" in " ".join(r.review.notes)


def test_rejection_without_a_verifiable_quote_is_refused():
    llm = FakeLLM(rejected("This is a golf day", "Join us for a round of golf on the course"))
    r = run(event_page(title="Annual Meeting", h1="Annual Meeting", date_line="12 October 2026", venue="Hilton Chennai",
                       desc="<p>" + "Our members come together once a year to catch up and hear from the board about the year ahead. " * 4 + "</p>"), llm,
            title="Annual Meeting")
    assert r.status == Status.NEEDS_REVIEW


def test_valid_rejection_with_page_evidence_is_accepted_and_stored():
    html = event_page(title="Annual Members Golf Day", h1="Annual Members Golf Day", date_line="12 October 2026", venue="Royal Golf Club",
                      desc="<p>Join us for a round of golf and lunch at the club. " + "A relaxed social day for our members and their guests with prizes and a barbecue. " * 4 + "</p>")
    llm = FakeLLM(rejected("A social golf day with no professional programme", "Join us for a round of golf and lunch at the club"))
    r = run(html, llm, title="Annual Members Golf Day")
    assert r.status == Status.REJECTED
    rej = r.decision.rejection
    assert rej["reason"] and "round of golf" in rej["evidence_quote"] and rej["source_url"] == URL
    row = r.to_row()
    assert row["rejection"]["evidence_quote"] and row["extractor_version"] and row["prompt_version"]


def test_verified_by_gemini_but_a_hard_block_remains_is_needs_review():
    r = run(event_page(date_line=None, fmt="In person", desc=GOOD_DESC), FakeLLM(verified()), date_hint="")
    assert r.status == Status.NEEDS_REVIEW and "missing_date" in r.quality.flags


def test_gemini_category_none_without_a_rejection_is_not_verified():
    r = run(COMPLETE, FakeLLM({"decision": "VERIFIED", "category": "None", "reason": "unsure"}))
    assert r.status == Status.NEEDS_REVIEW


@pytest.mark.parametrize("exc,kind", [(BudgetExceeded("cap"), "budget"), (LLMUnavailable("503"), "down"), (LLMBadResponse("not json"), "bad")])
def test_api_trouble_never_becomes_a_rejection(exc, kind):
    r = run(COMPLETE, FakeLLM(exc))
    assert r.status == Status.NEEDS_AI_REVIEW and r.status != Status.REJECTED
    assert r.review.ai_status in ("unavailable", "bad_response")


def test_review_mode_off_and_flagged():
    clean = run(COMPLETE, None, mode="off")
    assert clean.status == Status.VERIFIED and clean.review is None
    llm = FakeLLM()
    r = run(COMPLETE, llm, mode="flagged")                               # clean + strong topic: no AI needed
    assert not llm.calls and r.status in (Status.VERIFIED, Status.QUALITY_REVIEW, Status.NEEDS_AI_REVIEW)


def test_input_hash_changes_when_the_page_changes_enabling_re_review():
    a = run(COMPLETE, FakeLLM(verified())).packet
    b = run(COMPLETE, FakeLLM(verified())).packet
    c = run(event_page(fmt="In person", address="9 Other Road, Chennai", desc=GOOD_DESC), FakeLLM(verified())).packet
    assert input_hash_of(a) == input_hash_of(b) != input_hash_of(c)


def test_schema_has_the_required_structured_output():
    props = SCHEMA["properties"]
    assert set(props["decision"]["enum"]) == {"VERIFIED", "CORRECTED", "NEEDS_REVIEW", "REJECTED"}
    c = props["corrections"]["items"]["properties"]
    assert {"field", "old_value", "new_value", "evidence_quote", "source_url", "reason"} <= set(c)


def test_evidence_search_is_scoped_to_the_events_own_content():
    from pipeline.extraction.detail import extract_event
    ev = extract_event(event_page(related=True, desc=GOOD_DESC), URL, org_name="X", today=TODAY)
    src = SourceText.from_event(ev)
    assert src.contains("Date: 12 October 2026")[0]
    ok, where = src.contains("VAT Update Seminar 3 December 2026")
    assert not ok and "related" in where
    assert not src.contains("something that is not on the page at all")[0]


def test_an_ai_date_correction_cannot_push_a_future_event_into_the_past_by_itself():
    html = event_page(date_line="12 October 2026", extra_body="<p>The previous edition took place on 1 March 2026 in Chennai.</p>", fmt="In person", desc=GOOD_DESC)
    llm = FakeLLM(corrected(correction("start_date", "2026-03-01", "The previous edition took place on 1 March 2026", old_value="2026-10-12")))
    r = run(html, llm)
    assert r.status == Status.NEEDS_REVIEW and r.status != Status.PAST


def test_past_status_comes_from_the_code_verified_date():
    r = run(event_page(date_line="1 March 2026", fmt="In person", desc=GOOD_DESC), FakeLLM(verified()), date_hint="1 March 2026")
    assert r.status == Status.PAST


def test_needs_review_events_are_re_reviewed_when_the_prompt_version_changes(tmp_path):
    from pipeline import models
    from pipeline.reprocess import reprocess, select_for_reprocess
    from pipeline.store import Store
    st = Store(str(tmp_path / "s.sqlite"))
    deps = Deps(http=ReplayFetcher({URL: COMPLETE}), today=TODAY, run_id="r1", review_mode="all", llm=FakeLLM({"decision": "NEEDS_REVIEW", "category": "Tax", "reason": "not sure"}),
                ledger=st.ledger, store=st)
    r = process_candidate(cand(), deps)
    assert r.status == Status.NEEDS_REVIEW
    st.upsert(r.to_row(), "r1")
    # nothing changed: a NEEDS_REVIEW event is still not final, so it is offered again; the answer can now differ
    affected = select_for_reprocess(st)
    assert [a.event_id for a in affected] == [r.event_id]
    deps2 = Deps(http=ReplayFetcher({}), today=TODAY, run_id="r2", review_mode="all", llm=FakeLLM(verified()), ledger=st.ledger, store=st)
    reprocess(st, deps2, affected, "r2")
    assert st.get(r.event_id)["status"] == Status.VERIFIED
    hist = [(h["from_status"], h["to_status"]) for h in st.history("status_history", r.event_id)]
    assert ("NEEDS_REVIEW", "VERIFIED") in hist and hist[0][1] == "DISCOVERED"


def test_a_rejection_whose_only_evidence_is_the_title_is_refused():
    r = run(COMPLETE, FakeLLM(rejected("sports festival", "GST Audit Workshop")))
    assert r.status == Status.NEEDS_REVIEW and "just the title" in " ".join(r.review.notes)


def test_failed_guesses_at_supplementary_fields_do_not_block_an_otherwise_verified_event():
    llm = FakeLLM(corrected(correction("city", "Chennai", "Chennai is lovely in October"), correction("timezone", "IST", "Indian Standard Time prevails")))
    r = run(COMPLETE, llm)
    assert r.status == Status.VERIFIED and r.review.evidence_failures == 2 and r.ev.fields["city"].value is None
    assert "supplementary" in " ".join(r.review.notes)
