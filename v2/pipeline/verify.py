"""Stage: SOURCE VERIFICATION and the source-of-truth packet.

v1 verified with "does the value appear somewhere on the page?" -- which a related
event, a sidebar or the footer happily satisfies. Here a value is verified against
the EVENT'S OWN CONTENT, by association:

  * a labelled value must still be a (non-contact, non-programme) label/value pair of this page;
  * a structured (JSON-LD) value needs a JSON-LD Event validated as THIS page's;
  * a header / time-tag / text value must have its evidence located inside the event's own content;
  * a date or place that exists only in a removed related-events / contact / footer block is CONTRADICTED.

`SourceText` is also what the AI review's quotes are checked against: a quote only counts if it is in the
event's own content (never merely 'somewhere on the page').
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from pipeline.common import norm_text, normalize_ws
from pipeline.dates import date_context_is_deadline, find_dates, find_times, parse_iso
from pipeline.extraction.detail import ExtractedEvent
from pipeline.models import EXTRACTOR_VERSION, PIPELINE_VERSION, FieldValue

_PUNCT = re.compile(r"[\s ]+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "‑": "-", " ": " "})


def loose(text: str | None) -> str:
    """Whitespace/case/dash/quote-insensitive form used to find a quote inside source text."""
    t = normalize_ws((text or "").translate(_QUOTES)).lower()
    t = re.sub(r"[\"'`*_|•·]+", "", t)
    return _PUNCT.sub(" ", t).strip()


@dataclass
class SourceText:
    """The text a quote may legitimately come from, for ONE event."""
    own: str                   # the event's own content (container) -- loose form
    jsonld: str                # validated JSON-LD values -- loose form
    pairs: str                 # label/value pair evidence -- loose form
    card: str                  # discovery card text (only usable when the event has no page of its own)
    excluded: str              # related / contact / footer blocks -- NEVER a legitimate source
    allow_card: bool = False

    @classmethod
    def from_event(cls, ev: ExtractedEvent, card_text: str = "", allow_card: bool = False) -> "SourceText":
        ctx = ev.ctx
        j = ""
        if ctx is not None and ctx.jsonld:
            r = ctx.jsonld
            loc = r.get("location") or {}
            j = " ".join(str(x) for x in (r.get("name"), r.get("startDate"), r.get("endDate"), r.get("description"), loc.get("text"),
                                           loc.get("name"), r.get("organizer"), r.get("attendanceMode")) if x)
        return cls(own=loose(ctx.container_text) if ctx else "", jsonld=loose(j),
                   pairs=loose(" ".join(p.evidence for p in ctx.pairs if not p.contact)) if ctx else "",
                   card=loose(card_text), excluded=loose(" ".join(ctx.excluded_texts)) if ctx else "", allow_card=allow_card)

    def contains(self, quote: str) -> tuple[bool, str]:
        """-> (found in a legitimate source, where)"""
        q = loose(quote)
        if len(q) < 3:
            return False, "quote too short"
        if q in self.own:
            return True, "event content"
        if self.jsonld and q in self.jsonld:
            return True, "structured data"
        if self.pairs and q in self.pairs:
            return True, "label/value pairs"
        if self.allow_card and q in self.card:
            return True, "listing card"
        if q in self.excluded:
            return False, "only in a related/contact/footer block of the page, not in the event's own content"
        return False, "not found on the page"


@dataclass
class VerificationReport:
    results: dict[str, str] = field(default_factory=dict)       # field -> verified | unverified | contradicted | unresolved
    failures: list[str] = field(default_factory=list)
    contaminated: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"results": self.results, "failures": self.failures, "contaminated": self.contaminated}


def _dates_in(text: str, locale: str) -> set[date]:
    out: set[date] = set()
    for h in find_dates(text or "", locale):
        if h.valid and not date_context_is_deadline(text, h):
            out.add(h.start)
            if h.end:
                out.add(h.end)
    return out


def verify_event(ev: ExtractedEvent, *, card_text: str = "", has_own_page: bool = True) -> VerificationReport:
    """Set `verification` on every extracted field and report what failed."""
    rep = VerificationReport()
    ctx = ev.ctx
    src = SourceText.from_event(ev, card_text, allow_card=not has_own_page)
    excluded_dates = _dates_in(" ".join(ctx.excluded_texts), ctx.locale) if ctx else set()
    own_dates = _dates_in(ctx.container_text, ctx.locale) if ctx else set()
    own_pairs = [p for p in (ctx.pairs if ctx else []) if not p.contact and not p.agenda]

    for name, fv in ev.fields.items():
        if not fv.value:
            rep.results[name] = "unresolved"
            continue
        if fv.verification == "conflict":
            rep.results[name] = "contradicted"
            continue
        if name == "source_url":
            fv.verification = "verified"
            rep.results[name] = "verified"
            continue
        s = fv.source
        ok, why = False, ""
        if s == "sources_sheet":
            ok = True
        elif s == "jsonld":
            ok = bool(ctx and ctx.jsonld)
            why = "no JSON-LD Event validated for this page"
        elif s == "label":
            ev_norm = loose(fv.evidence)
            ok = any(loose(p.evidence) in ev_norm or ev_norm in loose(p.evidence) for p in own_pairs) if ev_norm else False
            why = "its label/value pair is not part of this event's own content (contact, programme or another block)"
        elif s in ("card",):
            ok = src.allow_card and loose(fv.evidence) in src.card
            why = "the listing card is not the event's official page"
        elif s in ("link", "page_url"):
            ok = True
        else:
            found, where = src.contains(fv.evidence) if fv.evidence else (False, "no evidence")
            if not found and fv.evidence:
                # evidence snippets are windows around the value: accept when the VALUE-bearing core is in the content
                core = loose(re.sub(r"<[^>]+>", " ", fv.evidence))
                found = core in src.own or (len(core) > 40 and core[:40] in src.own)
            ok, why = found, where
        # dates must belong to this event, not to a block that was removed from it
        if ok and name in ("start_date", "end_date"):
            d = parse_iso(fv.value)
            if d and d in excluded_dates and d not in own_dates and s not in ("jsonld",):
                ok, why = False, f"the date {fv.value} appears only in a related/contact/footer block"
                rep.contaminated.append(name)
            evid_dates = _dates_in(fv.evidence or "", ctx.locale if ctx else "dmy")
            raw_iso = re.search(r"(\d{4}-\d{2}-\d{2})", fv.evidence or "")
            if ok and d and (evid_dates or raw_iso) and d not in evid_dates and not (raw_iso and parse_iso(raw_iso.group(1)) == d):
                # a derived date (year established from weekday / card) legitimately differs from the evidence text
                if "established" not in (fv.note or "") and "taken from the listing card" not in (fv.note or ""):
                    ok, why = False, "the date value does not match the date in its evidence text"
        if ok and name in ("venue", "address") and ctx:
            ev_l = loose(fv.value)
            if ev_l and ev_l not in src.own and s not in ("jsonld",) and ev_l[: max(8, len(ev_l) // 2)] not in src.own:
                ok, why = False, "the value is not in the event's own content"
        fv.verification = "verified" if ok else "unverified"
        if not ok:
            rep.failures.append(f"{name}: {why or 'could not be verified against the event page'}")
        rep.results[name] = fv.verification
    return rep


# ======================================================================================================================
# the source-of-truth packet
# ======================================================================================================================

def build_packet(ev: ExtractedEvent, *, candidate: dict | None, quality: dict, signals: dict, verification: dict, today: date,
                 fetch: dict | None = None) -> dict:
    """Everything a person or the AI needs to judge ONE event, with label/value relationships preserved."""
    ctx = ev.ctx
    cand = candidate or {}
    return {
        "official_url": ev.url,
        "organiser": ev.org_name,
        "today": today.isoformat(),
        "discovery": {"title": cand.get("title", ""), "title_confidence": cand.get("title_confidence", ""), "card_text": cand.get("card_text", "")[:700],
                      "date_hint": cand.get("date_hint", ""), "location_hint": cand.get("location_hint", ""),
                      "methods": cand.get("methods", []), "listing_url": cand.get("listing_url", ""), "notes": cand.get("notes", [])[:4]},
        "extracted_fields": {k: {"value": v.value, "source": v.source, "evidence": v.evidence[:200], "confidence": v.confidence,
                                 "verification": v.verification, "locator": v.locator, "note": v.note[:200],
                                 "alternatives": v.alternatives[:3]} for k, v in ev.fields.items() if k != "source_url"},
        "jsonld": {"validated": _small(ctx.jsonld) if ctx else None,
                   "ignored_other_events": [{"name": r.get("name"), "url": r.get("url"), "startDate": r.get("startDate"), "why": r.get("why")}
                                            for r in (ctx.jsonld_foreign if ctx else [])][:4]},
        "structured_page_text": ev.structured_page_text(),
        "page": {"kind": ev.page_kind, "readable": ev.readable, "unreadable_reason": ev.unreadable_reason, "title_tag": ctx.title_tag if ctx else "",
                 "og_title": ctx.og_title if ctx else "", "container_chars": len(ctx.container_text) if ctx else 0,
                 "excluded_blocks": len(ctx.excluded_texts) if ctx else 0},
        "quality": quality,
        "verification": verification,
        "classification_signals": signals,
        "rescrape_attempts": ev.attempts,
        "fetch": fetch or {},
        "versions": {"pipeline": PIPELINE_VERSION, "extractor": EXTRACTOR_VERSION},
    }


def _small(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {k: (v if not isinstance(v, str) else v[:300]) for k, v in rec.items() if k in ("name", "url", "startDate", "endDate", "location", "attendanceMode", "organizer", "description", "why")}
