"""Stage: FINAL STATUS.

One function decides an event's status from everything known about it, and writes
down WHY. The rules that matter:

  FETCH_FAILED     the official page could not be opened (retryable). Never a rejection.
  QUALITY_REVIEW   quality flags remain and no AI review has been (or can be) done -- unreadable page,
                   AI disabled by configuration, list page instead of an event page.
  NEEDS_AI_REVIEW  the event wants an AI review and Gemini was unavailable / out of budget / answered unusably.
  NEEDS_REVIEW     the AI reviewed it and the answer is still uncertain, or a correction failed evidence checks,
                   or hard quality blocks remain. A person decides.
  VERIFIED         readable official page, no hard quality block, source verification passed, in scope.
  CORRECTED        like VERIFIED, after evidence-checked AI corrections.
  PAST             the (verified) end/start date is before today.
  REJECTED         ONLY a validated AI rejection: official-page evidence that the event is genuinely out of scope / not an event.

Scraper failure, a missing date, an unreadable page, uncertainty and an exhausted API budget can never produce REJECTED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from pipeline.dates import parse_iso
from pipeline.extraction.detail import ExtractedEvent
from pipeline.gemini_review import ReviewOutcome
from pipeline.models import Flag, Status
from pipeline.quality import QualityReport


@dataclass
class Decision:
    status: str
    reasons: list[str] = field(default_factory=list)
    category: str = ""
    rejection: dict | None = None
    verified_by: str = ""


def is_past(ev: ExtractedEvent, today: date) -> bool:
    for k in ("end_date", "start_date"):
        fv = ev.fields.get(k)
        if fv and fv.usable:
            d = parse_iso(fv.value)
            if d:
                return d < today
    return False


def resolve_status(ev: ExtractedEvent | None, q: QualityReport | None, signals: dict, review: ReviewOutcome | None, today: date, *,
                   fetch_failed: str = "", review_wanted: bool = True, ai_enabled: bool = True) -> Decision:
    if fetch_failed:
        return Decision(Status.FETCH_FAILED, [f"the official event page could not be opened: {fetch_failed}"])
    assert ev is not None and q is not None
    reasons: list[str] = []
    ai_done = bool(review and review.ai_status == "reviewed")
    category = (review.category if ai_done and review.category else "") or (signals.get("category_guess", "") if signals.get("strength") == "strong" else "")

    # ---- a validated rejection is the only road to REJECTED -----------------------------------------------------------------------------------
    if ai_done and review.decision == "REJECTED" and review.rejection:
        return Decision(Status.REJECTED, [f"AI review, evidence verified on the official page: {review.rejection['reason']}"], category="",
                        rejection=review.rejection, verified_by="AI review (evidence checked)")

    hard = q.hard_blocks
    if not ev.readable:
        return Decision(Status.QUALITY_REVIEW, ["the official page is not readable: " + (ev.unreadable_reason or "no content"),
                                                 *(review.reason and [review.reason] or [])], category=category)
    if hard:
        why = [f"{f}: {'; '.join(q.details.get(f, []))[:200]}" for f in hard]
        if ai_done:
            return Decision(Status.NEEDS_REVIEW, ["after the AI review these problems remain"] + why + ([review.reason] if review.reason else []),
                            category=category)
        if review_wanted and ai_enabled and review and review.ai_status in ("unavailable", "bad_response"):
            return Decision(Status.NEEDS_AI_REVIEW, [review.reason or "AI review unavailable"] + why, category=category)
        return Decision(Status.QUALITY_REVIEW, ["quality problems not cleared and no AI review was done"] + why, category=category)

    # ---- no hard blocks -------------------------------------------------------------------------------------------------------------------------
    if ai_done and review.decision == "NEEDS_REVIEW":
        return Decision(Status.NEEDS_REVIEW, [review.reason or "the AI review could not decide", *review.notes], category=category)
    if is_past(ev, today):
        return Decision(Status.PAST, ["the verified event date has passed"], category=category, verified_by="rules + source verification")

    if review_wanted and ai_enabled and not ai_done:
        if review and review.ai_status in ("unavailable", "bad_response"):
            return Decision(Status.NEEDS_AI_REVIEW, [review.reason or "AI review unavailable"], category=category)
        if review and review.ai_status == "skipped":
            return Decision(Status.QUALITY_REVIEW, [review.reason], category=category)
        return Decision(Status.NEEDS_AI_REVIEW, ["waiting for the AI review"], category=category)

    if ai_done:
        if review.decision == "NEEDS_REVIEW":
            return Decision(Status.NEEDS_REVIEW, [review.reason or "the AI review could not decide", *review.notes], category=category)
        if review.decision in ("VERIFIED", "CORRECTED"):
            if not review.category:
                return Decision(Status.NEEDS_REVIEW, ["the AI review found no Tax/Finance/Legal category but did not reject with evidence"] + review.notes, category="")
            st = Status.CORRECTED if review.applied else Status.VERIFIED
            return Decision(st, [review.reason] if review.reason else [], category=review.category, verified_by="AI review (evidence checked)" if review.applied else "rules + AI review")

    # no AI review required: code alone may only verify when scope is clear from the title/description topics
    if signals.get("verdict") == "in_scope_strong":
        return Decision(Status.VERIFIED, ["clear Tax/Finance/Legal topic and every field verified against the event's own page"],
                        category=signals.get("category_guess", ""), verified_by="rules + source verification")
    return Decision(Status.NEEDS_AI_REVIEW if ai_enabled else Status.QUALITY_REVIEW,
                    ["scope is not clear from the rules and no AI review was done"], category=category)
