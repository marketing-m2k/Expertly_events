"""Stage: EXTRACTION QUALITY CHECK.

Every extracted field is judged on its own: missing, conflicting, weak, suspicious
or unrelated. The result is a set of QUALITY FLAGS. A flag describes the
extraction -- it is never a rejection reason and never removes an event. Flags
decide what happens NEXT (re-scrape that field, then AI review), nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from pipeline.common import normalize_ws, similarity
from pipeline.dates import parse_iso
from pipeline.discovery.cards import is_generic_title
from pipeline.extraction.detail import ExtractedEvent
from pipeline.models import Flag

MAX_YEARS_AHEAD = 3


@dataclass
class QualityReport:
    flags: list[str] = field(default_factory=list)
    details: dict[str, list[str]] = field(default_factory=dict)     # flag -> reasons
    per_field: dict[str, str] = field(default_factory=dict)         # field -> ok | weak | conflict | missing

    def add(self, flag: str, why: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)
        self.details.setdefault(flag, []).append(why)

    @property
    def triggers_review(self) -> bool:
        return bool(set(self.flags) & Flag.REVIEW_TRIGGERS)

    @property
    def hard_blocks(self) -> list[str]:
        return [f for f in self.flags if f in Flag.HARD_BLOCKS]

    def to_dict(self) -> dict:
        return {"flags": self.flags, "details": self.details, "per_field": self.per_field}


def assess_quality(ev: ExtractedEvent, *, card_date_hint: str = "", has_own_page: bool = True, today: date | None = None,
                   candidate_jsonld: dict | None = None) -> QualityReport:
    today = today or date.today()
    q = QualityReport()
    f = ev.fields

    if not ev.readable:
        q.add(Flag.PAGE_UNREADABLE, ev.unreadable_reason or "the event page could not be read")
    if ev.page_kind == "listing" or not has_own_page:
        q.add(Flag.NO_DETAIL_PAGE, "the link is not a page of its own for this event (a list page or a page shared by several events)"
              if has_own_page is False or ev.page_kind == "listing" else "no detail page")
    ctx = ev.ctx

    # ---- name ------------------------------------------------------------------------------------------------------
    name = f.get("name")
    if not name or not name.value:
        q.add(Flag.MISSING_NAME, name.note if name else "no title")
        q.per_field["name"] = "missing"
    elif name.verification == "conflict":
        q.add(Flag.WEAK_NAME, name.note)
        q.add(Flag.SOURCE_CONFLICT, name.note)
        q.per_field["name"] = "conflict"
    elif name.confidence == "low" or is_generic_title(name.value, ev.org_name):
        q.add(Flag.WEAK_NAME, name.note or "the title is generic or only weakly sourced")
        q.per_field["name"] = "weak"
    else:
        q.per_field["name"] = "ok"

    # ---- dates -----------------------------------------------------------------------------------------------------
    sd, ed = f.get("start_date"), f.get("end_date")
    if not sd or not sd.value:
        if sd and sd.verification == "conflict":
            q.add(Flag.CONFLICTING_DATE, sd.note)
            q.per_field["start_date"] = "conflict"
        elif sd and "year" in (sd.note or "").lower() and "without a year" in (sd.note or ""):
            q.add(Flag.YEAR_MISSING, sd.note)
            q.add(Flag.MISSING_DATE, "no complete date (the year is missing and is not guessed)")
            q.per_field["start_date"] = "missing"
        else:
            q.add(Flag.MISSING_DATE, sd.note if sd else "no date")
            q.per_field["start_date"] = "missing"
    else:
        d = parse_iso(sd.value)
        q.per_field["start_date"] = "ok"
        if sd.verification == "conflict":
            q.add(Flag.SOURCE_CONFLICT, sd.note)
            q.per_field["start_date"] = "conflict"
        if sd.confidence == "low" or sd.source in ("body_text", "card", "title"):
            q.add(Flag.WEAK_DATE, f"the date only comes from {sd.source} ({sd.confidence} confidence)")
            q.per_field["start_date"] = "weak"
        if d and d.year > today.year + MAX_YEARS_AHEAD:
            q.add(Flag.SUSPICIOUS_VALUE, f"the date {sd.value} is more than {MAX_YEARS_AHEAD} years ahead")
        if d and d.year < today.year - 2:
            q.add(Flag.SUSPICIOUS_VALUE, f"the date {sd.value} is more than two years in the past")
        if ed and ed.value:
            e = parse_iso(ed.value)
            if d and e and e < d:
                q.add(Flag.SUSPICIOUS_VALUE, "the end date is before the start date")
            elif d and e and (e - d).days > 400 and "season" not in (name.value if name and name.value else "").lower() \
                    and "course" not in (name.value if name and name.value else "").lower():
                q.add(Flag.SUSPICIOUS_VALUE, f"the event is said to last {(e - d).days} days")
        # the discovery card disagreeing with the event page: the page wins (it is the source), but it is recorded
        if card_date_hint and d and sd.source != "card":
            from pipeline.discovery.cards import _card_dates
            cd = [h for h in _card_dates(card_date_hint, ev.ctx.locale if ev.ctx else "dmy") if h.valid]
            if cd and cd[0].start != d and not (cd[0].end and cd[0].start <= d <= cd[0].end):
                sd.alternatives = (sd.alternatives or []) + [{"value": cd[0].start.isoformat(), "source": "card", "evidence": card_date_hint}]
                sd.note = (sd.note + "; " if sd.note else "") + f"the discovery card said {cd[0].start.isoformat()} but the event page says {sd.value}: page used"
                if sd.confidence != "high":
                    q.add(Flag.SOURCE_CONFLICT, f"card date {cd[0].start.isoformat()} vs page date {sd.value} and the page value is only {sd.confidence} confidence")
    if candidate_jsonld and candidate_jsonld.get("startDate") and sd and sd.value:
        lj = parse_iso(candidate_jsonld["startDate"])
        if lj and lj != parse_iso(sd.value):
            sd.alternatives = (sd.alternatives or []) + [{"value": lj.isoformat(), "source": "listing JSON-LD", "evidence": candidate_jsonld["startDate"]}]
            sd.note = (sd.note + "; " if sd.note else "") + f"the list page's structured data said {lj.isoformat()}: event page used"

    # ---- location --------------------------------------------------------------------------------------------------
    venue, addr, fmt = f.get("venue"), f.get("address"), f.get("format")
    has_place = bool((venue and venue.value) or (addr and addr.value))
    virtual = bool(fmt and fmt.value == "Virtual")
    conflicts = [x for x in (venue, addr, f.get("city"), f.get("country")) if x and x.verification == "conflict"]
    if conflicts:
        q.add(Flag.CONFLICTING_LOCATION, "; ".join(c.note for c in conflicts))
        q.per_field["location"] = "conflict"
    elif not has_place and not virtual:
        q.add(Flag.MISSING_LOCATION, "no venue or address found on the event page")
        q.per_field["location"] = "missing"
    elif has_place and any(x and x.value and x.confidence == "low" for x in (venue, addr)):
        q.add(Flag.WEAK_LOCATION, "the venue/address is only weakly sourced")
        q.per_field["location"] = "weak"
    else:
        q.per_field["location"] = "ok"
    if not (fmt and fmt.value):
        q.add(Flag.MISSING_FORMAT, "the page does not say whether the event is virtual, in person or hybrid")
        q.per_field["format"] = "missing"
    else:
        q.per_field["format"] = "weak" if fmt.confidence == "low" else "ok"

    # ---- description -----------------------------------------------------------------------------------------------
    desc = f.get("description")
    if not desc or not desc.value or len(desc.value) < 60:
        q.add(Flag.WEAK_DESCRIPTION, "no real description found (or very short)")
        q.per_field["description"] = "missing" if not (desc and desc.value) else "weak"
    elif desc.verification == "conflict":
        q.add(Flag.UNRELATED_CONTENT, desc.note)
        q.per_field["description"] = "conflict"
    else:
        q.per_field["description"] = "ok"

    # ---- structured data that is not this page's -----------------------------------------------------------------------
    if ctx and ctx.jsonld_foreign and not ctx.jsonld:
        q.add(Flag.JSONLD_FOREIGN, f"{len(ctx.jsonld_foreign)} JSON-LD Event(s) on the page belong to other events and were ignored")
    return q
