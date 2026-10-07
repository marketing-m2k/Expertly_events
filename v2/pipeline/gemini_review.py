"""Stages: GEMINI REVIEW and GEMINI EVIDENCE VALIDATION.

Gemini's job: REVIEW + INTERPRET + CORRECT. It is not the source of truth, it does
not guess, and it cannot reject anything just because the page was empty.

What Gemini gets: the official URL, the discovery card, every extracted field with its
evidence and verification, JSON-LD (validated and ignored), the event's structured
page text (label/value pairs, headings), quality flags and classification signals --
not v1's `name | host | tag | location`.

What Gemini must return: a decision (VERIFIED / CORRECTED / NEEDS_REVIEW / REJECTED) and,
for every correction, {field, old_value, new_value, evidence_quote, source_url, reason}.

What CODE then does -- nothing from Gemini is applied on trust:
  * the evidence quote must be found in the event's OWN content (never in a related /
    contact / footer block); a quote that is not there fails the correction;
  * the new value must be derivable from the quote BY CODE (dates are re-parsed from the
    quote and need a year; places must be words of the quote; formats are re-derived);
  * the source URL must be the official page's site;
  * one failed correction sends the event to NEEDS_REVIEW (valid ones are still applied
    and logged);
  * a rejection needs a readable page, a quoted reason, a non-scope category and no strong
    Tax/Finance/Legal topic in the title -- otherwise it becomes NEEDS_REVIEW;
  * an unreadable / empty / JS-only page is NEVER sent for rejection;
  * budget / API failures leave the event NEEDS_AI_REVIEW, never rejected.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date

from pipeline.classify import ACCEPTED, taxonomy_text
from pipeline.common import normalize_ws, same_site, sha, tokens
from pipeline.dates import date_context_is_deadline, find_dates, find_times, find_timezone, parse_iso
from pipeline.extraction.detail import ExtractedEvent
from pipeline.extraction.fields import mode_from_text
from pipeline.llm import BudgetExceeded, LLMBadResponse, LLMUnavailable
from pipeline.models import REVIEW_PROMPT_VERSION, FieldValue
from pipeline.verify import SourceText, loose

CORRECTABLE = ("name", "start_date", "end_date", "start_time", "end_time", "timezone", "venue", "address", "city", "region", "country",
               "format", "description", "organiser")
DECISIONS = ("VERIFIED", "CORRECTED", "NEEDS_REVIEW", "REJECTED")
# a failed correction to one of these sends the event to NEEDS_REVIEW; a failed guess at the others (city, timezone, ...) is just discarded
CRITICAL_FIELDS = ("name", "start_date", "end_date", "venue", "address", "format")

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "category": {"type": "string", "enum": ["Tax", "Finance", "Legal", "None"]},
        "category_evidence": {"type": "string"},
        "reason": {"type": "string"},
        "corrections": {"type": "array", "items": {"type": "object", "properties": {
            "field": {"type": "string", "enum": list(CORRECTABLE)}, "old_value": {"type": "string"}, "new_value": {"type": "string"},
            "evidence_quote": {"type": "string"}, "source_url": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["field", "new_value", "evidence_quote"]}},
        "rejection": {"type": "object", "properties": {"reason": {"type": "string"}, "evidence_quote": {"type": "string"},
                                                       "source_url": {"type": "string"}}},
    },
    "required": ["decision", "reason"],
}

PROMPT = """You are the reviewer in an events pipeline for a professional Tax / Finance / Legal events directory.
You are given ONE event: what the scraper extracted (each value with its evidence), the event's own page text, and quality flags.
Today is {today}.

RULES -- these are strict:
1. The event's OFFICIAL PAGE is the only source of truth. Use ONLY what its text states. Never use general knowledge.
2. NEVER guess dates, years, locations, venues or formats. If the page does not state something, do not supply it.
3. A correction is allowed ONLY if the official page states the new value. For every correction give "evidence_quote": the EXACT words copied from
   the page text (structured_page_text / extracted evidence) that state it. A correction without a verbatim quote is discarded by code.
   - dates: the quote must contain the full date WITH its year. The event date is when the event happens -- never a registration deadline,
     early-bird, "posted on" or "last updated" date, and never another (related) event's date.
   - place: only the event's own venue/address, never the organiser's head-office or contact address.
4. If the page is empty, unreadable, a cookie wall, JavaScript-only or too short to judge: decision NEEDS_REVIEW. NEVER REJECTED.
5. decision meanings:
   VERIFIED     - the extracted values are right and the event is a real attendable professional event inside Tax / Finance / Legal. No corrections needed.
   CORRECTED    - the event is in scope but one or more extracted fields are wrong or missing and the page states the right value (give corrections).
   NEEDS_REVIEW - you cannot tell, the page lacks the information, sources conflict and the page does not settle it, or you are unsure.
   REJECTED     - ONLY when the page positively shows this is NOT an event we want: not a real event (a news item, job ad, article, newsletter, membership notice,
                  corporate notice), or purely social/networking with no professional subject, or its subject is clearly outside Tax/Finance/Legal.
                  Give "rejection": {{reason, evidence_quote (verbatim from the page showing why), source_url}}. A keyword like "dinner" in the title does NOT make an
                  event out of scope (e.g. "Annual Tax Dinner & Update" is a tax event).
6. Tax, Finance and Legal are BROAD professional fields, not three keywords. Examples of what belongs (not exhaustive):
{taxonomy}
   category = the field the event belongs to, or "None" if outside all three.
7. "old_value" is the value the scraper extracted (empty if none). source_url is the official event page URL.

EVENT PACKET (JSON):
{packet}
"""


@dataclass
class Correction:
    field: str
    old_value: str
    new_value: str
    evidence_quote: str
    source_url: str
    reason: str
    accepted: bool = False
    failure: str = ""
    evidence_where: str = ""
    derived_value: str = ""        # what CODE derived from the quote (the value actually applied)


@dataclass
class ReviewOutcome:
    ai_status: str = "not_run"      # reviewed | unavailable | bad_response | skipped | not_run
    decision: str | None = None     # Gemini's decision after code validation: VERIFIED | CORRECTED | NEEDS_REVIEW | REJECTED
    category: str = ""
    category_evidence: str = ""
    reason: str = ""
    corrections: list[Correction] = field(default_factory=list)
    rejection: dict | None = None
    evidence_failures: int = 0
    notes: list[str] = field(default_factory=list)
    input_hash: str = ""
    prompt_version: str = REVIEW_PROMPT_VERSION
    raw_decision: str = ""

    @property
    def applied(self) -> list[Correction]:
        return [c for c in self.corrections if c.accepted]

    def to_dict(self) -> dict:
        return {**{k: v for k, v in asdict(self).items() if k != "corrections"}, "corrections": [asdict(c) for c in self.corrections]}


def packet_for_prompt(packet: dict, max_text: int = 3800) -> dict:
    p = json.loads(json.dumps(packet, default=str))
    st = p.get("structured_page_text", {})
    if st.get("text"):
        st["text"] = st["text"][:max_text]
    for k in ("rescrape_attempts",):
        p.pop(k, None)
    return p


def input_hash_of(packet: dict) -> str:
    keep = {"u": packet.get("official_url"), "f": {k: (v.get("value"), v.get("verification")) for k, v in packet.get("extracted_fields", {}).items()},
            "t": (packet.get("structured_page_text") or {}).get("text", "")[:3800], "q": (packet.get("quality") or {}).get("flags"),
            "s": (packet.get("classification_signals") or {}).get("verdict"), "pv": REVIEW_PROMPT_VERSION}
    return sha(json.dumps(keep, sort_keys=True, default=str), 16)


# ======================================================================================================================
# evidence verification
# ======================================================================================================================

def _iso_from_value(value: str, locale: str) -> date | None:
    d = parse_iso(value)
    if d:
        return d
    hits = [h for h in find_dates(value or "", locale) if h.valid]
    return hits[0].start if hits else None


def _derive(field_name: str, new_value: str, quote: str, ev: ExtractedEvent) -> tuple[str | None, str]:
    """-> (value derived BY CODE from the quote, failure reason). None means the quote does not support new_value."""
    locale = ev.ctx.locale if ev.ctx else "dmy"
    nv = normalize_ws(new_value)
    if field_name in ("start_date", "end_date"):
        want = _iso_from_value(nv, locale)
        hits = [h for h in find_dates(quote, locale) if h.valid]
        if not hits:
            partial = [h for h in find_dates(quote, locale) if h.partial]
            return None, "the quote contains no complete date with a year" + (" (a day and month without a year is not enough)" if partial else "")
        if want is None:
            return None, "the new value is not a readable date"
        for h in hits:
            if date_context_is_deadline(quote, h):
                continue
            dates = [h.start] + ([h.end] if h.end else [])
            if field_name == "start_date" and h.start == want:
                return want.isoformat(), ""
            if field_name == "end_date" and (h.end == want or (h.start == want and len(hits) >= 1)):
                return want.isoformat(), ""
        if len([h for h in hits if not date_context_is_deadline(quote, h)]) == 0:
            return None, "the only date in the quote is a deadline/registration/published date"
        return None, "the date in the quote does not match the new value"
    if field_name in ("start_time", "end_time"):
        a, b, _ = find_times(quote)
        wa, wb, _ = find_times(nv)
        want = wa
        if not want:
            return None, "the new value is not a readable time"
        got = a if field_name == "start_time" else (b or a)
        return (want, "") if want in (a, b) and got is not None else (None, "the time in the quote does not match the new value")
    if field_name == "timezone":
        tz = find_timezone(quote)
        return (nv, "") if tz and loose(tz) == loose(nv) else (None, "the time zone in the quote does not match")
    if field_name == "format":
        m = mode_from_text(quote)
        return (m, "") if m and m == nv else (None, f"the quote says '{m or 'nothing about format'}', not '{nv}'")
    if field_name in ("venue", "address", "city", "region", "country", "organiser", "name"):
        tq, tn = tokens(quote), tokens(nv)
        if not tn:
            return None, "the new value is empty"
        covered = len(tn & tq) / len(tn)
        if covered >= 0.9 or loose(nv) in loose(quote):
            if field_name == "name" and len(nv) < 4:
                return None, "title too short"
            return nv, ""
        return None, "the new value is not stated in the quote"
    if field_name == "description":
        if len(nv) >= 40 and loose(nv)[:120] in loose(quote):
            return nv, ""
        return None, "the description is not a verbatim part of the quote"
    return None, "field cannot be corrected"


def validate_correction(c: Correction, ev: ExtractedEvent, source: SourceText) -> Correction:
    if c.field not in CORRECTABLE:
        c.failure = f"'{c.field}' is not a correctable field"
        return c
    if not normalize_ws(c.new_value):
        c.failure = "the new value is empty"
        return c
    if not normalize_ws(c.evidence_quote):
        c.failure = "no evidence quote given"
        return c
    if c.source_url and not (c.source_url == ev.url or same_site(c.source_url, ev.url)):
        c.failure = f"the source URL ({c.source_url}) is not the official event page's site"
        return c
    found, where = source.contains(c.evidence_quote)
    c.evidence_where = where
    if not found:
        c.failure = f"the evidence quote is not on the official page ({where})"
        return c
    derived, why = _derive(c.field, c.new_value, c.evidence_quote, ev)
    if derived is None:
        c.failure = why
        return c
    c.derived_value, c.accepted = derived, True
    return c


def validate_rejection(rej: dict | None, category: str, ev: ExtractedEvent, source: SourceText, signals: dict, has_own_page: bool) -> tuple[bool, str]:
    """-> (rejection allowed, why not)."""
    if not ev.readable or ev.page_kind != "event" and has_own_page:
        return False, "the page is unreadable or is not a single event page: rejection is not allowed on missing information"
    if not ev.ctx or len(ev.ctx.container_text) < 250:
        return False, "too little page text to reject an event on"
    if not rej or not normalize_ws(rej.get("reason", "")):
        return False, "no rejection reason given"
    if not normalize_ws(rej.get("evidence_quote", "")):
        return False, "no evidence quote for the rejection"
    found, where = source.contains(rej["evidence_quote"])
    if not found:
        return False, f"the rejection's evidence quote is not on the official page ({where})"
    if len(normalize_ws(rej["evidence_quote"])) < 25 or loose(rej["evidence_quote"]) == loose(ev.fields["name"].value or ""):
        return False, "the evidence quote is just the title (or too short): it does not show why the event is out of scope"
    if category in ACCEPTED:
        return False, f"the model rejected the event but classified it as {category}"
    if re.search(r"already (passed|past|took place|ended)|date (has )?passed|in the past|expired", rej.get("reason", ""), re.I):
        return False, "'the date has passed' is decided by code from the verified date, not by the AI"
    if re.search(r"no information|not enough|insufficient|unreadable|empty|cookie|javascript|unable to (read|determine)|does not (state|provide|contain)", rej.get("reason", ""), re.I):
        return False, "a rejection reason that says information is missing is not a reason to reject"
    if signals.get("strength") == "strong" and signals.get("topic_in_title"):
        return False, "the title carries a strong Tax/Finance/Legal topic; a rejection against that needs a person"
    return True, ""


# ======================================================================================================================
# the review
# ======================================================================================================================

def review_event(ev: ExtractedEvent, packet: dict, *, source: SourceText, signals: dict, llm, today: date, has_own_page: bool = True) -> ReviewOutcome:
    out = ReviewOutcome(input_hash=input_hash_of(packet))
    if not ev.readable:
        out.ai_status, out.decision = "skipped", "NEEDS_REVIEW"
        out.reason = f"the official page could not be read ({ev.unreadable_reason}); it is not sent for rejection"
        return out
    if has_own_page and ev.page_kind != "event":
        out.ai_status, out.decision = "skipped", "NEEDS_REVIEW"
        out.reason = "the link is a list page, not this event's own page"
        return out
    prompt = PROMPT.format(today=today.isoformat(), taxonomy=taxonomy_text(), packet=json.dumps(packet_for_prompt(packet), ensure_ascii=False, indent=None))
    try:
        resp = llm.call(prompt, SCHEMA)
    except BudgetExceeded as exc:
        out.ai_status, out.reason = "unavailable", f"AI budget reached: {exc}"
        return out
    except LLMUnavailable as exc:
        out.ai_status, out.reason = "unavailable", f"AI unavailable: {exc}"
        return out
    except LLMBadResponse as exc:
        out.ai_status, out.reason = "bad_response", f"AI answer unusable: {exc}"
        return out
    return interpret_response(resp, ev, source, signals, out, has_own_page)


def interpret_response(resp: dict, ev: ExtractedEvent, source: SourceText, signals: dict, out: ReviewOutcome | None = None,
                       has_own_page: bool = True) -> ReviewOutcome:
    out = out or ReviewOutcome()
    out.ai_status = "reviewed"
    out.raw_decision = str(resp.get("decision", ""))
    decision = out.raw_decision if out.raw_decision in DECISIONS else "NEEDS_REVIEW"
    out.reason = normalize_ws(resp.get("reason", ""))
    cat = resp.get("category", "None")
    out.category = cat if cat in ACCEPTED else ""
    out.category_evidence = normalize_ws(resp.get("category_evidence", ""))[:300]
    if out.raw_decision not in DECISIONS:
        out.notes.append(f"unknown decision '{out.raw_decision}' treated as NEEDS_REVIEW")

    for raw in (resp.get("corrections") or []):
        if not isinstance(raw, dict):
            continue
        c = Correction(field=str(raw.get("field", "")), old_value=str(raw.get("old_value", "") or ""), new_value=str(raw.get("new_value", "") or ""),
                       evidence_quote=str(raw.get("evidence_quote", "") or ""), source_url=str(raw.get("source_url", "") or ev.url),
                       reason=str(raw.get("reason", "") or ""))
        out.corrections.append(validate_correction(c, ev, source))
    out.evidence_failures = sum(1 for c in out.corrections if not c.accepted)

    crit_fail = [c for c in out.corrections if not c.accepted and c.field in CRITICAL_FIELDS]
    supp_fail = [c for c in out.corrections if not c.accepted and c.field not in CRITICAL_FIELDS]
    if crit_fail:
        out.notes.append(f"{len(crit_fail)} correction(s) to key fields ({', '.join(c.field for c in crit_fail)}) failed evidence verification and were not applied")
    if supp_fail:
        out.notes.append(f"{len(supp_fail)} supplementary correction(s) ({', '.join(c.field for c in supp_fail)}) failed evidence verification and were discarded")
    if decision == "CORRECTED":
        if not out.corrections:
            decision = "NEEDS_REVIEW"
            out.notes.append("the model said CORRECTED but gave no corrections")
        elif crit_fail:
            decision = "NEEDS_REVIEW"
        elif not out.applied:
            decision = "VERIFIED"          # only unsupported supplementary guesses: nothing changed, the verified data stands
    elif decision in ("VERIFIED", "NEEDS_REVIEW") and crit_fail:
        decision = "NEEDS_REVIEW"
    elif decision == "VERIFIED" and out.applied:
        decision = "CORRECTED"                     # it corrected something after all, and the corrections held up

    if decision == "REJECTED":
        rej = resp.get("rejection") if isinstance(resp.get("rejection"), dict) else None
        ok, why = validate_rejection(rej, cat, ev, source, signals, has_own_page)
        if ok:
            out.rejection = {"reason": normalize_ws(rej["reason"]), "evidence_quote": normalize_ws(rej["evidence_quote"]),
                             "source_url": rej.get("source_url") or ev.url}
        else:
            decision = "NEEDS_REVIEW"
            out.notes.append("rejection not accepted: " + why)
    out.decision = decision
    return out


def apply_corrections(ev: ExtractedEvent, out: ReviewOutcome) -> list[dict]:
    """Write the accepted corrections into the event. -> history entries [{field, old, new, quote, source_url, reason}]."""
    history = []
    for c in out.applied:
        old = ev.fields.get(c.field)
        value = c.derived_value
        ev.fields[c.field] = FieldValue(value=value, source="gemini", evidence=c.evidence_quote[:300], confidence="medium", verification="verified",
                                        locator=f"AI correction, quote verified on the official page ({c.evidence_where})",
                                        note=f"corrected by the AI review: {c.reason[:150]}",
                                        alternatives=([{"value": old.value, "source": old.source, "evidence": old.evidence[:150]}] if old and old.value else []))
        if c.field == "start_date":
            e = ev.fields.get("end_date")
            hit = next((h for h in find_dates(c.evidence_quote, ev.ctx.locale if ev.ctx else "dmy") if h.valid and h.start == parse_iso(value)), None)
            if hit and hit.end and (not e or not e.usable or e.verification != "verified"):
                ev.fields["end_date"] = FieldValue(hit.end.isoformat(), "gemini", c.evidence_quote[:300], "medium", "verified",
                                                   "the end of the range in the verified quote", "derived from the verified date range")
        history.append({"field": c.field, "old": (old.value if old else "") or "", "new": value, "evidence_quote": c.evidence_quote,
                        "source_url": c.source_url or ev.url, "reason": c.reason})
    return history
