"""Stage: DETAIL EXTRACTION. One event page in, one ExtractedEvent out.

The page is first reduced to the event's own content (page_context.py), then
every field is read by its chain of extractors and resolved by its resolver
(fields.py). Nothing here classifies or rejects; it only reads, with proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.extraction import fields as F
from pipeline.extraction.page_context import PageContext, build_context
from pipeline.models import EXTRACTOR_VERSION, FieldValue, field_dicts, unresolved

DATE_FIELDS = ("start_date", "end_date", "start_time", "end_time", "timezone")
LOCATION_FIELDS = ("venue", "address", "city", "region", "country", "format")
ALL_FIELDS = ("name", "start_date", "end_date", "start_time", "end_time", "timezone", "venue", "address", "city", "region", "country",
              "format", "organiser", "description", "registration_url", "source_url")


@dataclass
class ExtractedEvent:
    url: str
    fields: dict[str, FieldValue] = field(default_factory=dict)
    ctx: PageContext | None = None
    page_kind: str = "event"
    readable: bool = True
    unreadable_reason: str = ""
    notes: list[str] = field(default_factory=list)
    extractor_version: str = EXTRACTOR_VERSION
    card_title: str = ""
    card_date_hint: str = ""
    org_name: str = ""
    attempts: list[dict] = field(default_factory=list)       # field-level re-scrape log

    def structured_page_text(self) -> dict:
        """Label/value relationships and headings of the event's own content (what the AI and the verifier read)."""
        c = self.ctx
        if not c:
            return {}
        return {"h1": c.h1, "headings": c.headings[:15],
                "pairs": [p.to_dict() for p in c.pairs][:40],
                "time_tags": c.time_tags[:5],
                "text": c.container_text[:6000]}

    def to_record(self) -> dict:
        return {"url": self.url, "fields": field_dicts(self.fields), "page_kind": self.page_kind, "readable": self.readable,
                "unreadable_reason": self.unreadable_reason, "notes": self.notes, "extractor_version": self.extractor_version,
                "attempts": self.attempts}


def extract_fields(ctx: PageContext, names: tuple[str, ...] | list[str], *, card_title: str = "", card_date_hint: str = "",
                   org_name: str = "", page_url: str = "", only_extractors: list[str] | None = None,
                   extra_cands: dict | None = None) -> dict[str, FieldValue]:
    """Run the extractor chains for `names` against `ctx`. `only_extractors` limits the date/name chains and
    `extra_cands` ({field-group: [Cand]}) adds candidates from alternate extractors (both used by the re-scrape)."""
    extra_cands = extra_cands or {}
    out: dict[str, FieldValue] = {}
    names = set(names)

    def want(*ns):
        return bool(names & set(ns))

    if want("name"):
        cands = F.name_candidates(ctx, card_title)
        if only_extractors:
            cands = [c for c in cands if c.source in only_extractors]
        out["name"] = F.resolve_name(cands + extra_cands.get("name", []), ctx)
    if want(*DATE_FIELDS):
        cands = []
        for ename, fn in F.DATE_EXTRACTORS:
            if only_extractors and ename not in only_extractors:
                continue
            cands += fn(ctx, card_title=card_title, card_date_hint=card_date_hint)
        start, end = F.resolve_dates(cands + extra_cands.get("date", []), ctx)
        out["start_date"], out["end_date"] = start, end
        st, en, tz = F.resolve_times(ctx, start)
        out["start_time"], out["end_time"], out["timezone"] = st, en, tz
    if want(*LOCATION_FIELDS):
        lc = F.location_candidates(ctx)
        for k, extra in extra_cands.get("location", {}).items():
            lc[k] = lc.get(k, []) + extra
        out.update(F.resolve_location_fields(lc, ctx))
    if want("organiser"):
        out["organiser"] = F.resolve_organiser(ctx, org_name)
    if want("description"):
        out["description"] = F.resolve_description(F.description_candidates(ctx) + extra_cands.get("description", []), ctx)
    if want("registration_url"):
        out["registration_url"] = F.resolve_registration(ctx, page_url or ctx.final_url)
    if want("source_url"):
        out["source_url"] = FieldValue(page_url or ctx.final_url, "page_url", page_url or ctx.final_url, "high", "verified", "the official event page that was read")
    return out


def extract_event(html: str | None, url: str, *, final_url: str = "", card_title: str = "", card_date_hint: str = "",
                  org_name: str = "", locale: str = "dmy", today=None) -> ExtractedEvent:
    ctx = build_context(html or "", url, final_url or url, locale=locale, org_name=org_name, card_title=card_title, today=today)
    ev = ExtractedEvent(url=final_url or url, ctx=ctx, page_kind=ctx.page_kind, readable=ctx.readable,
                        unreadable_reason=ctx.unreadable_reason, card_title=card_title, card_date_hint=card_date_hint,
                        org_name=org_name, notes=list(ctx.notes))
    if not ctx.readable:
        for n in ALL_FIELDS:
            ev.fields[n] = unresolved(f"page not readable: {ctx.unreadable_reason}")
        ev.fields["source_url"] = FieldValue(final_url or url, "page_url", url, "high", "verified", "")
        if card_title:
            ev.fields["name"] = FieldValue(card_title, "card", card_title, "low", "unverified", "discovery card only; page unreadable")
        return ev
    ev.fields = extract_fields(ctx, ALL_FIELDS, card_title=card_title, card_date_hint=card_date_hint, org_name=org_name,
                               page_url=final_url or url)
    if ctx.page_kind == "listing":
        ev.notes.append("the link leads to a list of events, so its values cannot be attributed to this one event")
    return ev
