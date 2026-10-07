"""The per-event pipeline, as separate, named stages (no giant function):

    fetch_detail        -> open the official event page (HTTP, browser if needed)           [FETCH_FAILED on failure]
    extract_event       -> read every field with proof                                        (extraction/detail.py)
    assess_quality      -> quality flags                                                      (quality.py)
    rescrape            -> field-level retries                                                (rescrape.py)
    verify_event        -> source verification against the event's own content               (verify.py)
    classify_signals    -> Tax/Finance/Legal signals (never verdicts)                         (classify.py)
    review_event        -> Gemini review + evidence validation                                (gemini_review.py)
    resolve_status      -> the final status, with reasons                                     (status.py)

Every step writes to the ledger, so every candidate can be traced from discovery to its outcome.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from pipeline import classify, gemini_review, identity, quality as quality_mod, rescrape as rescrape_mod, status as status_mod, verify as verify_mod
from pipeline.common import norm_url, normalize_ws, same_page, same_site
from pipeline.dates import find_dates, parse_iso, to_iso
from pipeline.extraction.detail import ALL_FIELDS, ExtractedEvent, extract_event
from pipeline.fetcher import Fetcher, Page, has_app_root, looks_js_shell
from pipeline.models import (CLASSIFIER_VERSION, EXTRACTOR_VERSION, REVIEW_PROMPT_VERSION, Candidate, FieldValue, Flag, Status, field_dicts, unresolved)
from pipeline.quality import QualityReport, assess_quality
from pipeline.status import Decision, is_past, resolve_status
from pipeline.verify import SourceText, VerificationReport, build_packet, verify_event

REVIEW_MODES = ("all", "flagged", "off")


@dataclass
class Deps:
    http: Fetcher
    today: date
    run_id: str = ""
    browser_open: Callable[[str], Page] | None = None
    llm: object | None = None
    review_mode: str = "all"                  # all = review every processed event (the benchmark); flagged = only flagged/uncertain; off
    ledger: Callable[..., None] = lambda *a, **k: None
    store: object | None = None
    locale_by_country: dict = field(default_factory=lambda: {"USA": "mdy", "US": "mdy"})
    rescrape: bool = True
    save_pages: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def locale(self, country: str) -> str:
        return self.locale_by_country.get((country or "").upper(), "dmy")


@dataclass
class EventResult:
    cand: Candidate
    event_id: str = ""
    ev: ExtractedEvent | None = None
    quality: QualityReport | None = None
    verification: VerificationReport | None = None
    signals: dict = field(default_factory=dict)
    packet: dict = field(default_factory=dict)
    review: gemini_review.ReviewOutcome | None = None
    decision: Decision | None = None
    has_own_page: bool = True
    shared_url: bool = False
    fetch: dict = field(default_factory=dict)
    corrections: list[dict] = field(default_factory=list)
    keys: dict = field(default_factory=dict)
    initial_flags: list = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.decision.status if self.decision else Status.DISCOVERED

    def to_row(self) -> dict:
        ev, d = self.ev, self.decision
        fields = field_dicts(ev.fields) if ev else {}
        return {"event_id": self.event_id, "candidate_id": self.cand.candidate_id, "org": self.cand.org, "country": self.cand.country,
                "source_url": self.cand.source_url, "url": ev.url if ev else self.cand.url, "status": d.status, "category": d.category,
                "reasons": d.reasons, "flags": self.quality.flags if self.quality else [], "fields": fields, "packet": self.packet,
                "extractor_version": EXTRACTOR_VERSION, "classifier_version": CLASSIFIER_VERSION, "prompt_version": REVIEW_PROMPT_VERSION,
                "input_hash": self.review.input_hash if self.review else "", "ai_status": self.review.ai_status if self.review else "not_run",
                "verified_by": d.verified_by, "rejection": d.rejection, "shared_url": self.shared_url, **self.keys,
                "lifecycle": [(Status.DISCOVERED, f"found by {'+'.join(self.cand.methods) or 'discovery'} on {self.cand.listing_url or self.cand.source_url}"),
                              (Status.EXTRACTED, ("event page read" if self.ev and self.ev.readable and self.has_own_page else
                                                  "event page not readable" if self.ev and not self.ev.readable else "read from the listing card (no page of its own)")
                               + f"; {len(self.initial_flags)} quality flag(s)")]}


# ======================================================================================================================
# stage: plan (which candidates have a page of their own?)
# ======================================================================================================================

def plan_pages(cands: list[Candidate]) -> dict[str, tuple[bool, bool]]:
    """candidate_id -> (has_own_page, shared_url). A link shared by several events, the list page itself, or an external
    calendar link cannot be read as 'this event's page'."""
    from collections import Counter
    counts = Counter(norm_url(c.url) for c in cands if c.url)
    out = {}
    for c in cands:
        if not c.url:
            out[c.candidate_id] = (False, False)
            continue
        shared = counts[norm_url(c.url)] > 1
        own = not (same_page(c.url, c.source_url) or same_page(c.url, c.listing_url) or "calendar.google.com" in c.url or shared)
        out[c.candidate_id] = (own, shared)
    return out


# ======================================================================================================================
# stage: fetch the official page
# ======================================================================================================================

def fetch_detail(deps: Deps, url: str) -> tuple[Page, dict]:
    info = {"attempts": []}
    page = deps.http.get(url)
    info["attempts"].append({"method": "http", "status": page.status, "error": page.error})
    final = page
    if (not page.ok or looks_js_shell(page.html) or has_app_root(page.html or "") and len(page.html or "") < 30000) and page.status not in (404, 410) and deps.browser_open:
        bp = deps.browser_open(url)
        info["attempts"].append({"method": "browser", "status": bp.status, "error": bp.error})
        if bp.ok and (not page.ok or len(bp.html or "") > len(page.html or "")):
            final = bp
    info["final"] = {"method": final.method, "status": final.status, "error": final.error, "url": final.final_url or url}
    return final, info


# ======================================================================================================================
# stage: card-only extraction (events without a page of their own)
# ======================================================================================================================

def extract_from_card(cand: Candidate, org_name: str, locale: str, today: date, reason: str) -> ExtractedEvent:
    """The listing card IS the official source for an event with no page of its own. Labelled 'listing_card', never invented."""
    from pipeline.extraction.page_context import PageContext
    ctx = PageContext(url=cand.url or cand.listing_url, final_url=cand.listing_url or cand.url, html="", locale=locale, org_name=org_name, today=today)
    ctx.container_text = normalize_ws(cand.card_text)
    ev = ExtractedEvent(url=cand.url or cand.listing_url or cand.source_url, ctx=ctx, page_kind="listing", readable=True, org_name=org_name,
                        card_title=cand.title, card_date_hint=cand.date_hint, notes=[reason])
    for n in ALL_FIELDS:
        ev.fields[n] = unresolved("not stated on the listing card")
    if cand.title:
        ev.fields["name"] = FieldValue(cand.title, "listing_card", cand.title, "medium" if cand.title_confidence != "low" else "low", "unverified", "listing card")
    hits = [h for h in find_dates(" ".join([cand.date_hint, cand.card_text]), locale) if h.valid]
    if hits:
        h = hits[0]
        ev.fields["start_date"] = FieldValue(to_iso(h.start), "listing_card", cand.date_hint or h.raw, "medium", "unverified", "listing card")
        if h.end:
            ev.fields["end_date"] = FieldValue(to_iso(h.end), "listing_card", h.raw, "medium", "unverified", "listing card")
    if cand.location_hint:
        ev.fields["venue"] = FieldValue(cand.location_hint, "listing_card", cand.location_hint, "medium", "unverified", "listing card")
    ev.fields["organiser"] = FieldValue(org_name, "sources_sheet", org_name, "high", "verified", "") if org_name else unresolved("")
    desc = normalize_ws(cand.card_text.replace(cand.title, "", 1)) if cand.title else ""
    if len(desc) >= 60:
        ev.fields["description"] = FieldValue(desc[:600], "listing_card", desc[:100], "low", "unverified", "listing card text")
    ev.fields["source_url"] = FieldValue(cand.listing_url or cand.source_url, "page_url", cand.listing_url, "high", "verified", "the list page this card was read from")
    return ev


# ======================================================================================================================
# the event pipeline
# ======================================================================================================================

def _led(deps: Deps, cand: Candidate, event_id: str, stage: str, outcome: str, reason: str = "") -> None:
    deps.ledger(deps.run_id, cand.org, cand.candidate_id, event_id, stage, outcome, reason)


def _is_past_iso(value: str, today: date) -> bool:
    d = parse_iso(value or "")
    return bool(d and d < today)


def _flag_unverified(q: QualityReport, ev: ExtractedEvent) -> None:
    for k in ("name", "start_date"):
        fv = ev.fields.get(k)
        if fv and fv.value and fv.verification in ("unverified", "contradicted") and fv.source not in ("card", "listing_card") \
                and (ev.ctx is not None and ev.ctx.html):
            q.add(Flag.UNVERIFIED_EVIDENCE, f"{k}: its evidence could not be located in the event's own content")


def process_candidate(cand: Candidate, deps: Deps, *, has_own_page: bool = True, shared_url: bool = False) -> EventResult:
    res = EventResult(cand=cand, has_own_page=has_own_page, shared_url=shared_url)
    org_name, locale, today = cand.org, deps.locale(cand.country), deps.today
    ext_event: ExtractedEvent | None = None
    fetch_failed = ""
    cid = cand.candidate_id

    # 1. fetch + extract ------------------------------------------------------------------------------------------------------------
    if has_own_page and cand.url:
        page, finfo = fetch_detail(deps, cand.url)
        res.fetch = finfo
        _led(deps, cand, "", "fetch", "ok" if page.ok else "failed", json.dumps(finfo["attempts"])[:300])
        if not page.ok:
            fetch_failed = page.error or "no response"
            ev = extract_from_card(cand, org_name, locale, today, "the official event page could not be opened")
            ev.fields["source_url"] = FieldValue(cand.url, "page_url", cand.url, "high", "verified", "the page that could not be opened")
            ev.readable = True
        else:
            ev = extract_event(page.html, cand.url, final_url=page.final_url or cand.url, card_title=cand.title, card_date_hint=cand.date_hint,
                               org_name=org_name, locale=locale, today=today)
            if ev.page_kind == "listing":
                has_own_page = res.has_own_page = False
                ev = extract_from_card(cand, org_name, locale, today, "the link leads to a list of events, not to this event's own page")
    else:
        ev = extract_from_card(cand, org_name, locale, today, "the event has no page of its own (its link is the list page, a shared page or missing)")
        res.fetch = {"attempts": [], "final": {"method": "none"}}
    res.ev = ev
    _led(deps, cand, "", "extract", f"{ev.page_kind}/{'readable' if ev.readable else 'unreadable'}", ev.unreadable_reason or "")

    # 2. quality -> field-level re-scrape ---------------------------------------------------------------------------------------------
    q = assess_quality(ev, card_date_hint=cand.date_hint, has_own_page=has_own_page, today=today, candidate_jsonld=cand.jsonld)
    res.initial_flags = list(q.flags)
    _led(deps, cand, "", "quality", f"{len(q.flags)} flags", ",".join(q.flags))
    if not fetch_failed and has_own_page and deps.rescrape and q.triggers_review and ev.page_kind == "event":
        before = len(q.flags)
        ev, q = rescrape_mod.rescrape(ev, q, fetch_http=deps.http.get, fetch_browser=deps.browser_open, today=today,
                                      card_date_hint=cand.date_hint, has_own_page=has_own_page)
        res.ev = ev
        _led(deps, cand, "", "rescrape", f"{len(ev.attempts)} attempts", f"flags {before} -> {len(q.flags)}: {','.join(q.flags)}")

    # 3. verification, classification, identity ----------------------------------------------------------------------------------------
    card_text = cand.card_text
    ver = verify_event(ev, card_text=card_text, has_own_page=has_own_page)
    _flag_unverified(q, ev)
    res.verification, res.quality = ver, q
    _led(deps, cand, "", "verify", f"{sum(1 for v in ver.results.values() if v == 'verified')} verified", "; ".join(ver.failures)[:300])
    name = ev.fields["name"].value or cand.title or ""
    desc = ev.fields["description"].value or ""
    res.signals = classify.classify_signals(name, desc)
    _led(deps, cand, "", "classify", res.signals["verdict"], f"{res.signals.get('category_guess', '')} {res.signals.get('topic', '')}")
    sd = ev.fields["start_date"].value or ""
    loc = ", ".join(x for x in (ev.fields["venue"].value, ev.fields["city"].value, ev.fields["country"].value) if x)
    res.event_id = identity.make_event_id(cand.org, ev.url if has_own_page else "", shared_url=shared_url or not has_own_page, name=name,
                                          start_date=sd, location=loc, ext_id=cand.ext_id)
    ik = identity.identity_keys(cand.org, ev.url if has_own_page else "", shared_url or not has_own_page, name, sd, loc, cand.ext_id)
    res.keys = {"url_key": ik["url_key"], "ext_key": ik["ext_key"], "nal_key": ik["nal_key"], "nad_key": ik["nad_key"]}

    if deps.save_pages and deps.store is not None and ev.ctx is not None and ev.ctx.html:
        deps.store.save_page(res.event_id, ev.url, ev.ctx.html, res.fetch.get("final", {}).get("status"), res.fetch.get("final", {}).get("method", ""))

    # 4. AI review -------------------------------------------------------------------------------------------------------------------------
    review_wanted = deps.review_mode == "all" or (deps.review_mode == "flagged" and (q.triggers_review or res.signals.get("needs_review")))
    review = None
    if fetch_failed:
        review_wanted = False
    packet = build_packet(ev, candidate=cand.to_dict(), quality=q.to_dict(), signals=res.signals, verification=ver.to_dict(), today=today, fetch=res.fetch)
    res.packet = packet
    if review_wanted and deps.llm is not None and deps.review_mode != "off":
        source = SourceText.from_event(ev, card_text, allow_card=not has_own_page)
        review = gemini_review.review_event(ev, packet, source=source, signals=res.signals, llm=deps.llm, today=today, has_own_page=has_own_page)
        res.review = review
        _led(deps, cand, res.event_id, "review", f"{review.ai_status}/{review.decision or '-'}",
             f"{review.reason[:200]} | corrections accepted {len(review.applied)} failed {review.evidence_failures}")
        if review.applied:
            res.corrections = gemini_review.apply_corrections(ev, review)
            # an AI date correction that moves a future event into the past never decides PAST by itself
            moved = [h for h in res.corrections if h["field"] in ("start_date", "end_date") and _is_past_iso(h["new"], today) and h["old"] and not _is_past_iso(h["old"], today)]
            if moved:
                review.decision = "NEEDS_REVIEW"
                review.notes.append(f"the AI's date correction ({moved[0]['old']} -> {moved[0]['new']}) would make a future event past: a person must confirm")
            ver = verify_event(ev, card_text=card_text, has_own_page=has_own_page)
            q = assess_quality(ev, card_date_hint=cand.date_hint, has_own_page=has_own_page, today=today, candidate_jsonld=cand.jsonld)
            _flag_unverified(q, ev)
            res.verification, res.quality = ver, q
            res.packet = build_packet(ev, candidate=cand.to_dict(), quality=q.to_dict(), signals=res.signals, verification=ver.to_dict(), today=today, fetch=res.fetch)
            res.packet["ai_review"] = review.to_dict()
            # identity may change once the corrected date/location is known
            sd = ev.fields["start_date"].value or ""
            loc = ", ".join(x for x in (ev.fields["venue"].value, ev.fields["city"].value, ev.fields["country"].value) if x)
            if not has_own_page:
                res.event_id = identity.make_event_id(cand.org, "", shared_url=True, name=ev.fields["name"].value or name, start_date=sd, location=loc, ext_id=cand.ext_id)
                ik = identity.identity_keys(cand.org, "", True, ev.fields["name"].value or name, sd, loc, cand.ext_id)
                res.keys = {"url_key": ik["url_key"], "ext_key": ik["ext_key"], "nal_key": ik["nal_key"], "nad_key": ik["nad_key"]}
        else:
            res.packet["ai_review"] = review.to_dict()
    elif review_wanted and deps.llm is None and deps.review_mode != "off":
        from pipeline.gemini_review import ReviewOutcome
        review = res.review = ReviewOutcome(ai_status="unavailable", reason="no AI client is configured")

    # 5. final status ------------------------------------------------------------------------------------------------------------------------
    d = resolve_status(ev, res.quality, res.signals, review, today, fetch_failed=fetch_failed, review_wanted=review_wanted,
                       ai_enabled=deps.review_mode != "off")
    res.decision = d
    _led(deps, cand, res.event_id, "final", d.status, "; ".join(d.reasons)[:300])
    return res
