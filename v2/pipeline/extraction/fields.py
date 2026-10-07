"""Field extractors and resolvers.

Each field has an ordered chain of extractors ("jsonld", "label", "time_tag",
"header_text", ...). An extractor returns zero or more CANDIDATES, each with its
source, tier and the literal evidence it was read from. A resolver then picks
the value -- or refuses to: conflicting primary candidates leave the value empty
and list the alternatives; a missing year leaves the date unresolved.

The same extractors are what the field-level re-scrape (rescrape.py) runs again,
one field at a time, against fresh page content.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import date

from pipeline.common import normalize_ws, similarity, tokens
from pipeline.dates import DateHit, date_context_is_deadline, find_dates, find_times, find_timezone, parse_iso, to_iso, year_from_weekday
from pipeline.discovery.cards import CTA_TEXT, is_generic_title, strip_date_affix
from pipeline.extraction.page_context import BOILER, PageContext
from pipeline.extraction.pairs import Pair
from pipeline.models import FieldValue, unresolved

TIER_NAMES = {5: "structured/labelled", 4: "time tag", 3: "header/meta", 2: "title", 1: "body/card"}
_VIRTUAL = re.compile(r"\b(online|virtual|webinar|zoom|teams|webex|livestream|live\s*stream|remote|web\s*conference|google\s*meet|goto\s*webinar)\b", re.I)
_INPERSON = re.compile(r"\b(in[\s-]?person|face[\s-]?to[\s-]?face|on[\s-]?site|physical|onsite|classroom)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b|\bin[\s-]?person\s*(?:&|and|\+|/)\s*(?:online|virtual)|\b(?:online|virtual)\s*(?:&|and|\+|/)\s*in[\s-]?person", re.I)
_ADDRESS_LIKE = re.compile(r"\b\d{1,5}\s+[A-Za-z][\w .'-]+\b(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|drive|dr\.?|boulevard|blvd|place|square|way|court|tower|floor|level|suite|building)\b|"
                           r"\b(?:level|floor|suite|unit)\s+\d+|\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b|\b\d{5}(?:-\d{4})?\b|\b\d{6}\b|\bP\.?O\.?\s*Box\b", re.I)


@dataclass
class Cand:
    value: str | None
    source: str
    tier: int
    evidence: str
    locator: str = ""
    start: date | None = None
    end: date | None = None
    partial: bool = False
    ambiguous: bool = False
    extra: dict = field(default_factory=dict)


def _fv(c: Cand, confidence: str, note: str = "") -> FieldValue:
    return FieldValue(value=c.value, source=c.source, evidence=c.evidence[:300], confidence=confidence, locator=c.locator, note=note)


def _alt(c: Cand) -> dict:
    return {"value": c.value, "source": c.source, "evidence": c.evidence[:200]}


# ======================================================================================================================
# NAME
# ======================================================================================================================

def _site_suffix_stripped(text: str, org_name: str, others: list[str]) -> str:
    parts = [p.strip() for p in re.split(r"\s+[|\-–—·»:]\s+|\s*\|\s*", normalize_ws(text)) if p.strip()]
    if len(parts) <= 1:
        return normalize_ws(text)
    def org_like(p: str) -> bool:
        return bool(org_name) and (similarity(p, org_name) >= 0.7 or norm_in(p, org_name))
    def norm_in(a, b):
        return normalize_ws(a).lower() in normalize_ws(b).lower() or normalize_ws(b).lower() in normalize_ws(a).lower()
    keep = [p for p in parts if not org_like(p) and not is_generic_title(p)]
    if not keep:
        return parts[0]
    ref = [o for o in others if o]
    if ref:
        keep.sort(key=lambda p: max(similarity(p, o) for o in ref), reverse=True)
        return keep[0]
    return max(keep, key=len) if len(keep) > 1 and len(keep[0]) < 8 else keep[0]


_WEEKDAY = r"(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)(?:day|sday|nesday|rsday|urday)?"


def scrub_title(text: str, org_name: str, locale: str) -> str:
    """Remove dates, times and the organisation suffix that sites append to og:title / <title>."""
    t = normalize_ws(text)
    for h in sorted(find_dates(t, locale), key=lambda h: -h.span[0]):
        if h.valid or h.partial:
            t = t[:h.span[0]] + " " + t[h.span[1]:]
    t = re.sub(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b|\b\d{1,2}:\d{2}\b", " ", t, flags=re.I)
    t = re.sub(rf"\b{_WEEKDAY}\b\.?,?", " ", t)
    t = re.sub(r"[@|]\s*", " ", t)
    for abbr in re.findall(r"\(([A-Za-z]{2,8})\)", org_name or ""):
        t = re.sub(rf"\b{re.escape(abbr)}\b\s*$", " ", t)
    return re.sub(r"\s+", " ", t).strip(" ,;:-–—")


def _agree(a: str, b: str) -> bool:
    na, nb = normalize_ws(a).lower(), normalize_ws(b).lower()
    if min(len(na), len(nb)) >= 8 and (na in nb or nb in na):
        return True
    return similarity(a, b) >= 0.75


def name_candidates(ctx: PageContext, card_title: str = "") -> list[Cand]:
    out: list[Cand] = []
    if ctx.jsonld and ctx.jsonld.get("name"):
        out.append(Cand(ctx.jsonld["name"], "jsonld", 5, ctx.jsonld["name"], "JSON-LD Event.name"))
    if ctx.h1:
        out.append(Cand(ctx.h1, "heading", 4, ctx.h1, "page h1"))
    others = [ctx.h1, (ctx.jsonld or {}).get("name", ""), card_title]
    for src, txt in (("meta", ctx.og_title), ("title_tag", ctx.title_tag)):
        if txt:
            t = _site_suffix_stripped(txt, ctx.org_name, others)
            t = scrub_title(t, ctx.org_name, ctx.locale) or t
            if t:
                out.append(Cand(t, src, 3, txt, src))
    if card_title:
        out.append(Cand(card_title, "card", 2, card_title, "discovery card"))
    for c in out:
        stripped = strip_date_affix(c.value or "", ctx.locale)
        if stripped and stripped != c.value:
            c.extra["raw"] = c.value
            c.value = stripped
    return out


def resolve_name(cands: list[Cand], ctx: PageContext) -> FieldValue:
    usable = [c for c in cands if c.value and not CTA_TEXT.match(c.value)]
    if not usable:
        return unresolved("no event title found on the page", source="")
    good = [c for c in usable if not is_generic_title(c.value, ctx.org_name)]
    pool = good or usable
    # agreement between independent sources
    best, best_score = None, -1.0
    for c in pool:
        agree = [o for o in pool if o is not c and _agree(o.value, c.value)]
        score = c.tier + 1.5 * len(agree)
        if score > best_score:
            best, best_score = c, score
    agree = [o for o in pool if o is not best and _agree(o.value, best.value)]
    disagree = [o for o in pool if o is not best and not _agree(o.value, best.value) and similarity(o.value, best.value) < 0.4 and o.source != "card"]
    alts = [_alt(o) for o in pool if o is not best]
    if not good:
        fv = _fv(best, "low", "only a generic title was found; it is not trusted as the event's name")
        fv.alternatives = alts
        return fv
    if agree and not disagree:
        fv = _fv(best, "high")
        fv.note = "agrees with " + ", ".join(sorted({a.source for a in agree}))
        fv.alternatives = alts
        return fv
    if disagree:
        fv = _fv(best, "low", "the page's sources disagree about the title: " + "; ".join(f"{d.source}: {d.value}" for d in disagree))
        fv.verification, fv.alternatives = "conflict", alts
        return fv
    conf = "high" if best.tier >= 5 else "medium"
    fv = _fv(best, conf, "only one source states the title" if len(pool) == 1 else "")
    fv.alternatives = alts
    return fv


# ======================================================================================================================
# DATES / TIMES
# ======================================================================================================================

def _hit_to_cand(h: DateHit, source: str, tier: int, evidence: str, locator: str) -> Cand:
    return Cand(to_iso(h.start) if h.valid else None, source, tier, evidence, locator, start=h.start, end=h.end,
                partial=h.partial or not h.valid, ambiguous=h.ambiguous,
                extra={"raw": h.raw, "note": h.note, "day": h.day, "month": h.month, "weekday": h.weekday})


def _establish_years(cands: list["Cand"], today: date) -> None:
    """A day+month written without a year becomes a real date ONLY when the page itself fixes the year:
    its weekday ('Tuesday 6 October' fits exactly one year in the plausible window), or the same site's listing
    card stating the same day and month WITH a year. Anything else stays unresolved."""
    cards = [c for c in cands if c.source == "card" and c.start]
    for c in cands:
        if c.start or not c.partial or not c.extra.get("day") or not c.extra.get("month"):
            continue
        d, m, wd = c.extra["day"], c.extra["month"], c.extra.get("weekday")
        y = year_from_weekday(d, m, wd, today) if wd is not None else None
        if y:
            c.start, c.value, c.partial = date(y, m, d), date(y, m, d).isoformat(), False
            c.extra["note"] = f"year {y} established from the weekday written on the page ('{c.extra.get('raw')}')"
            continue
        match = next((k for k in cards if (k.start.day, k.start.month) == (d, m)), None)
        if match is not None:
            c.start, c.value, c.partial = match.start, match.value, False
            if match.end and not c.end:
                c.end = match.end
            c.extra["note"] = f"year {match.start.year} taken from the listing card, which states the same day and month with a year"


def _valid_hits(text: str, locale: str, keep_deadline: bool = False) -> list[DateHit]:
    return [h for h in find_dates(text, locale) if (h.valid or h.partial) and (keep_deadline or not date_context_is_deadline(text, h))]


def x_date_jsonld(ctx: PageContext, **_) -> list[Cand]:
    r = ctx.jsonld
    if not r or not r.get("startDate"):
        return []
    s, e = parse_iso(r["startDate"]), parse_iso(r.get("endDate") or "")
    if not s:
        return []
    return [Cand(to_iso(s), "jsonld", 5, f"startDate {r['startDate']}" + (f" / endDate {r['endDate']}" if r.get("endDate") else ""),
                 "JSON-LD Event", start=s, end=e if e and e != s else None, extra={"raw": r["startDate"]})]


def x_date_label(ctx: PageContext, **_) -> list[Cand]:
    out, start_p, end_p = [], None, None
    for p in ctx.pairs:
        if p.contact or p.agenda or p.canon not in ("date", "start_date", "end_date"):
            continue
        hits = _valid_hits(p.value, ctx.locale)
        valid = [h for h in hits if h.valid]
        if p.canon == "date":
            if valid:
                out.append(_hit_to_cand(valid[0], "label", 5, p.evidence, f"label '{p.label}'" + (f" under '{p.section}'" if p.section else "")))
            elif hits:
                out.append(_hit_to_cand(hits[0], "label", 5, p.evidence, f"label '{p.label}'"))
        elif p.canon == "start_date" and valid and start_p is None:
            start_p = (valid[0], p)
        elif p.canon == "end_date" and valid and end_p is None:
            end_p = (valid[0], p)
    if start_p and not any(c.start == start_p[0].start for c in out):
        c = _hit_to_cand(start_p[0], "label", 5, start_p[1].evidence, f"label '{start_p[1].label}'")
        if end_p and end_p[0].start and end_p[0].start >= start_p[0].start and end_p[0].start != start_p[0].start:
            c.end = end_p[0].start
            c.evidence += " | " + end_p[1].evidence
        out.append(c)
    return out


def x_date_time_tag(ctx: PageContext, **_) -> list[Cand]:
    tags = []
    for i, t in enumerate(ctx.time_tags):
        d = parse_iso(t["datetime"])
        if not d or re.search(r"regist|deadline|publish|posted|updated|modified|closes?|early", t["near"], re.I):
            continue
        tags.append((i, d, t))
    out = []
    if len(tags) >= 2 and tags[0][2]["near"] == tags[1][2]["near"] and tags[1][1] > tags[0][1]:
        _, d0, t0 = tags[0]
        _, d1, t1 = tags[1]
        out.append(Cand(to_iso(d0), "time_tag", 4, f"{t0['text']} - {t1['text']} (<time datetime={t0['datetime']}> .. <time datetime={t1['datetime']}>)",
                        "time tags in the page header", start=d0, end=d1, extra={"raw": t0["datetime"]}))
        tags = tags[2:]
    for i, d, t in tags[:2]:
        out.append(Cand(to_iso(d), "time_tag", 4 if (i == 0 and not out) else 2, f"<time datetime={t['datetime']}> {t['text']}", "time tag", start=d,
                        extra={"raw": t["datetime"]}))
    return out


def _after_h1(ctx: PageContext, n: int = 900) -> str:
    text = ctx.container_text
    if ctx.h1 and ctx.h1 in text:
        text = text.split(ctx.h1, 1)[1]
    return text[:n]


def x_date_header_text(ctx: PageContext, **_) -> list[Cand]:
    seg = _after_h1(ctx)
    hits = [h for h in _valid_hits(seg, ctx.locale) if h.valid]
    if hits:
        return [_hit_to_cand(hits[0], "header_text", 3, seg[max(0, hits[0].span[0] - 30):hits[0].span[1] + 30], "text just below the main heading")]
    partial = [h for h in _valid_hits(seg, ctx.locale) if h.partial]
    return [_hit_to_cand(partial[0], "header_text", 3, partial[0].raw, "text just below the main heading")] if partial else []


def x_date_meta(ctx: PageContext, **_) -> list[Cand]:
    out = []
    for k, v in ctx.event_meta.items():
        if "start" in k.lower():
            d = parse_iso(v)
            if d:
                out.append(Cand(to_iso(d), "meta", 3, f"{k}={v}", "meta tag", start=d))
    return out


def x_date_title(ctx: PageContext, card_title: str = "", **_) -> list[Cand]:
    out = []
    for txt in (ctx.h1, ctx.og_title, ctx.title_tag):
        hits = [h for h in _valid_hits(txt or "", ctx.locale) if h.valid]
        if hits:
            out.append(_hit_to_cand(hits[0], "title", 2, txt, "date written in the title"))
            break
    return out


def x_date_body(ctx: PageContext, **_) -> list[Cand]:
    text = ctx.container_text[900:] if len(ctx.container_text) > 900 else ""
    hits = [h for h in _valid_hits(text, ctx.locale) if h.valid]
    return [_hit_to_cand(hits[0], "body_text", 1, text[max(0, hits[0].span[0] - 40):hits[0].span[1] + 40], "page body text")] if hits else []


def x_date_card(ctx: PageContext, card_date_hint: str = "", **_) -> list[Cand]:
    if not card_date_hint:
        return []
    hits = [h for h in _valid_hits(card_date_hint, ctx.locale) if h.valid]
    if hits:
        return [_hit_to_cand(hits[0], "card", 1, card_date_hint, "discovery card")]
    p = [h for h in find_dates(card_date_hint, ctx.locale) if h.partial]
    return [_hit_to_cand(p[0], "card", 1, card_date_hint, "discovery card")] if p else []


DATE_EXTRACTORS = [("jsonld", x_date_jsonld), ("label", x_date_label), ("time_tag", x_date_time_tag), ("header_text", x_date_header_text),
                   ("meta", x_date_meta), ("title", x_date_title), ("body_text", x_date_body), ("card", x_date_card)]


def resolve_dates(cands: list[Cand], ctx: PageContext) -> tuple[FieldValue, FieldValue]:
    """-> (start_date, end_date). Never guesses a year; never picks between conflicting primary sources."""
    _establish_years(cands, ctx.today)
    valid = [c for c in cands if c.start]
    partial = [c for c in cands if c.partial and not c.start]
    primary = [c for c in valid if c.tier >= 3]
    weak = [c for c in valid if c.tier < 3]
    end_fv = FieldValue(None, verification="unresolved", note="no end date stated")

    def pack(c: Cand, conf: str, note: str = "") -> tuple[FieldValue, FieldValue]:
        if c.extra.get("note"):
            note = (note + "; " if note else "") + c.extra["note"]
        fv = _fv(c, conf, note)
        if c.ambiguous:
            fv.note = (fv.note + "; " if fv.note else "") + "day/month order read from the site's locale"
            fv.confidence = "medium" if conf == "high" else conf
        end = FieldValue(None, verification="unresolved", note="no end date stated")
        if c.end and c.end != c.start:
            end = FieldValue(to_iso(c.end), c.source, c.evidence[:300], fv.confidence, "unverified", c.locator)
        return fv, end

    distinct = {c.start for c in primary}
    if len(distinct) > 1:
        # a multi-day range and its own end date are not a conflict
        ranges = [(c.start, c.end) for c in primary]
        starts = {s for s, _ in ranges}
        if len(starts) > 1:
            fv = unresolved("the page gives different event dates: " + "; ".join(f"{c.source}: {c.extra.get('raw', c.value)}" for c in primary),
                            alternatives=[_alt(c) for c in primary])
            fv.verification = "conflict"
            return fv, end_fv
    if primary:
        top = max(primary, key=lambda c: (c.tier, c.end is not None))
        conf = "high" if top.tier >= 4 or any(c.tier >= 5 for c in primary) else "medium"
        if len({c.source for c in primary}) == 1 and top.tier < 5:
            conf = "medium"
        fv, end = pack(top, conf, "agrees with " + ", ".join(sorted({c.source for c in primary if c is not top})) if len(primary) > 1 else "")
        if not end.usable:
            # the page states a start but no end, and the listing card gives a range from the SAME start: the end is the card's
            cr = next((c for c in cands if c.source == "card" and c.start == top.start and c.end and c.end > c.start), None)
            if cr is not None:
                end = FieldValue(to_iso(cr.end), "card", cr.evidence[:300], "medium", "unverified", "discovery card",
                                 note="the event page states no end date; the listing card's range with the same start gives it")
        # a date written in the title that disagrees with the chosen date is a conflict worth review
        title_dates = [c for c in weak if c.source == "title" and c.start != top.start and not (top.end and top.start <= c.start <= top.end)]
        if title_dates:
            fv.alternatives = [_alt(c) for c in title_dates]
            fv.note = (fv.note + "; " if fv.note else "") + f"the title states a different date ({title_dates[0].extra.get('raw')})"
            fv.verification = "conflict"
            fv.confidence = "low"
        else:
            fv.alternatives = [_alt(c) for c in weak if c.start != top.start][:3]
        return fv, end
    if weak and partial:
        # the page states a day+month without a year; a weaker source with a DIFFERENT day/month contradicts it -- no pick
        pm = {(c.extra.get("day"), c.extra.get("month")) for c in partial if c.extra.get("day")}
        if pm and any((c.start.day, c.start.month) not in pm for c in weak):
            fv = unresolved("the page's day/month and a weaker source disagree: " + "; ".join(f"{c.source}: {c.extra.get('raw', c.value)}" for c in partial + weak),
                            alternatives=[_alt(c) for c in partial + weak])
            fv.verification = "conflict"
            return fv, end_fv
    if weak:
        ds = {c.start for c in weak if c.source != "card"} or {c.start for c in weak}
        pool = [c for c in weak if c.source != "card"] or weak
        if len(ds) == 1:
            top = max(pool, key=lambda c: c.tier)
            fv, end = pack(top, "low", f"only a weak source ({top.source}) states the date")
            return fv, end
        fv = unresolved("only weak, disagreeing sources mention a date", alternatives=[_alt(c) for c in weak])
        fv.verification = "conflict"
        return fv, end_fv
    if partial:
        raws = ", ".join(sorted({c.extra.get("raw", "") for c in partial if c.extra.get("raw")}))
        fv = unresolved(f"a date is written without a year ({raws}); the year is not guessed", alternatives=[_alt(c) for c in partial])
        fv.note = "year_missing: " + fv.note
        return fv, end_fv
    return unresolved("no event date found on the page"), end_fv


def resolve_times(ctx: PageContext, start_fv: FieldValue) -> tuple[FieldValue, FieldValue, FieldValue]:
    """(start_time, end_time, timezone) -- read only. A time is used when ONE event-level time is stated; several different
    times (a programme/agenda) are left unresolved rather than picking a session's time."""
    none = lambda: FieldValue(None, verification="unresolved")
    r = ctx.jsonld or {}
    st = re.search(r"T(\d{2}):(\d{2})", r.get("startDate", "") or "")
    en = re.search(r"T(\d{2}):(\d{2})", r.get("endDate", "") or "")
    tzm = re.search(r"(Z|[+-]\d{2}:?\d{2})$", r.get("startDate", "") or "")
    tz_json = (tzm.group(1) if tzm else "")
    a = b = None
    ev, src, tier = "", "", "medium"
    midnight = ("00", "00")
    if st and (st.group(1), st.group(2)) != midnight:
        a = f"{st.group(1)}:{st.group(2)}"
        b = f"{en.group(1)}:{en.group(2)}" if en and (en.group(1), en.group(2)) not in (("23", "59"), midnight) else None
        ev, src, tier = f"startDate {r['startDate']}", "jsonld", "high"
    note = ""
    if not a:
        found = []
        for p in ctx.pairs_of("time", "date"):
            x, y, e = find_times(p.value)
            if x:
                found.append((x, y, p.evidence))
        distinct = {(x, y) for x, y, _ in found}
        if len(distinct) == 1:
            a, b, ev = found[0]
            src, tier = "label", "high"
        elif len(distinct) > 1:
            note = "several different times are stated on the page (programme/agenda); the event's own time is not guessed"
    if not a and not note and start_fv.source in ("header_text", "label", "title") and start_fv.evidence:
        x, y, e = find_times(start_fv.evidence)
        if x:
            a, b, ev, src, tier = x, y, e, start_fv.source, "medium"
    if not a and not note:
        x, y, e = find_times(_after_h1(ctx, 300))
        if x:
            a, b, ev, src, tier = x, y, e, "header_text", "medium"
    tz = ""
    for text in [ev] + [p.value for p in ctx.pairs_of("time", "date")] + [_after_h1(ctx, 300)]:
        if find_times(text or "")[0]:          # a zone abbreviation only counts next to a time ("GST" in a title is a tax)
            tz = find_timezone(text or "")
            if tz:
                break
    tz = tz or ("UTC" if tz_json == "Z" else tz_json)
    res = lambda v: FieldValue(v, src, ev, tier) if v else FieldValue(None, verification="unresolved", note=note)
    return (res(a), res(b), FieldValue(tz, "label" if tz and not tz_json else "jsonld", tz, "medium") if tz else none())


# ======================================================================================================================
# LOCATION / FORMAT
# ======================================================================================================================

def _is_virtual_value(v: str) -> bool:
    return bool(_VIRTUAL.search(v)) and not re.search(r"\b(hotel|centre|center|hall|tower|street|road|avenue|campus)\b", v, re.I)


def mode_from_text(text: str) -> str:
    t = text or ""
    if _HYBRID.search(t):
        return "Hybrid"
    v, i = bool(_VIRTUAL.search(t)), bool(_INPERSON.search(t))
    if v and i:
        return "Hybrid"
    if v:
        return "Virtual"
    if i:
        return "In Person"
    return ""


def location_candidates(ctx: PageContext) -> dict[str, list[Cand]]:
    out: dict[str, list[Cand]] = {k: [] for k in ("venue", "address", "city", "region", "country", "format")}
    r = ctx.jsonld or {}
    loc = r.get("location") or {}
    if loc.get("virtual") and not loc.get("name") and not loc.get("street"):
        out["format"].append(Cand("Virtual", "jsonld", 5, "VirtualLocation", "JSON-LD Event.location"))
    if loc.get("name") and not _is_virtual_value(loc["name"]):
        out["venue"].append(Cand(loc["name"], "jsonld", 5, loc["name"], "JSON-LD Event.location.name"))
    elif loc.get("name") and _is_virtual_value(loc["name"]):
        out["format"].append(Cand("Virtual", "jsonld", 5, loc["name"], "JSON-LD Event.location.name"))
    if loc.get("street"):
        addr = ", ".join(x for x in (loc.get("street"), loc.get("locality"), loc.get("region"), loc.get("postal"), loc.get("country")) if x)
        out["address"].append(Cand(addr, "jsonld", 5, addr, "JSON-LD Event.location.address"))
    for key, jk in (("city", "locality"), ("region", "region"), ("country", "country")):
        if loc.get(jk):
            out[key].append(Cand(loc[jk], "jsonld", 5, loc[jk], f"JSON-LD address.{jk}"))
    mode = (r.get("attendanceMode") or "").lower()
    if mode:
        fm = "Hybrid" if "mixed" in mode else "Virtual" if "online" in mode else "In Person" if "offline" in mode else ""
        if fm:
            out["format"].append(Cand(fm, "jsonld", 5, f"eventAttendanceMode {r['attendanceMode']}", "JSON-LD Event.eventAttendanceMode"))
    for p in ctx.pairs:
        if p.contact or p.agenda:
            continue
        sec = f"label '{p.label}'" + (f" under '{p.section}'" if p.section else "")
        if p.canon == "venue":
            if _is_virtual_value(p.value) and len(p.value) < 60:
                out["format"].append(Cand("Virtual" if not _HYBRID.search(p.value) else "Hybrid", "label", 5, p.evidence, sec))
                if not re.search(r"\bhybrid\b", p.value, re.I) and len(p.value) < 25:
                    continue
            if _ADDRESS_LIKE.search(p.value) and len(p.value) > 25:
                out["address"].append(Cand(p.value, "label", 5, p.evidence, sec))
            elif not p.value.lower().startswith("http") and len(p.value) <= 200:
                out["venue"].append(Cand(p.value, "label", 5, p.evidence, sec))
        elif p.canon == "address":
            out["address"].append(Cand(p.value, "label", 5, p.evidence, sec))
        elif p.canon in ("city", "region", "country"):
            out[p.canon].append(Cand(p.value, "label", 5, p.evidence, sec))
        elif p.canon == "format":
            m = mode_from_text(p.value)
            if m:
                out["format"].append(Cand(m, "label", 5, p.evidence, sec))
    # contact-block-only values are recorded as alternatives elsewhere, never used here
    for p in ctx.pairs:
        if p.source == "icon" and p.canon == "venue" and not p.contact and not p.agenda and not any(c.value == p.value for c in out["venue"]):
            out["venue"].append(Cand(p.value, "icon", 3, p.evidence, "icon row"))
    # header badges / text just below the heading
    seg = _after_h1(ctx, 500)
    m = mode_from_text(seg) if re.search(r"^(?:\W*(?:online|virtual|in[\s-]?person|hybrid|webinar)\W*)", seg, re.I) or \
        re.search(r"\b(?:online|virtual|hybrid|in[\s-]?person)\s+(?:event|webinar|conference|seminar|workshop|course)\b", seg, re.I) else ""
    if m:
        out["format"].append(Cand(m, "header_text", 3, seg[:120], "text just below the main heading"))
    t = mode_from_text(ctx.h1 or "")
    if t and not out["format"]:
        out["format"].append(Cand(t, "title", 2, ctx.h1, "mode word in the title"))
    return out


def resolve_choice(cands: list[Cand], fname: str, *, normalize=lambda v: normalize_ws(v).lower()) -> FieldValue:
    cands = [c for c in cands if c.value]
    if not cands:
        return unresolved(f"no {fname} found", source="")
    top_tier = max(c.tier for c in cands)
    top = [c for c in cands if c.tier == top_tier]
    distinct = {normalize(c.value) for c in top}
    best = top[0]
    # several different values from the same tier: only acceptable when one contains the other (venue vs venue+city)
    if len(distinct) > 1:
        vals = sorted(distinct, key=len)
        if all(vals[0] in v for v in vals[1:]):
            best = max(top, key=lambda c: len(c.value))
        else:
            fv = unresolved(f"the page gives different {fname} values: " + "; ".join(f"{c.source}: {c.value}" for c in top), alternatives=[_alt(c) for c in top])
            fv.verification = "conflict"
            return fv
    agree = [c for c in cands if c is not best and (normalize(c.value) == normalize(best.value) or normalize(c.value) in normalize(best.value))]
    conf = "high" if best.tier >= 5 else "medium" if best.tier >= 3 else "low"
    fv = _fv(best, conf, ("agrees with " + ", ".join(sorted({a.source for a in agree}))) if agree else "")
    fv.alternatives = [_alt(c) for c in cands if c is not best and normalize(c.value) != normalize(best.value)][:3]
    # a lower-tier value that CONTRADICTS the chosen one (e.g. JSON-LD venue vs label venue) is a conflict
    contradicting = [c for c in cands if c.tier >= 3 and c is not best and normalize(c.value) not in normalize(best.value)
                     and normalize(best.value) not in normalize(c.value) and similarity(c.value, best.value) < 0.3]
    if contradicting and top_tier < 5:
        fv.verification, fv.confidence = "conflict", "low"
        fv.note = "sources disagree: " + "; ".join(f"{c.source}: {c.value}" for c in contradicting)
    return fv


def resolve_location_fields(cands: dict[str, list[Cand]], ctx: PageContext) -> dict[str, FieldValue]:
    out: dict[str, FieldValue] = {}
    for k in ("venue", "address", "city", "region", "country", "format"):
        out[k] = resolve_choice(cands[k], k)
    # JSON-LD vs label venue/address disagree -> conflicting_location, not a silent pick
    for k in ("venue", "address"):
        tiers = {c.source for c in cands[k]}
        if {"jsonld", "label"} <= tiers:
            j = next(c for c in cands[k] if c.source == "jsonld")
            l = next(c for c in cands[k] if c.source == "label")
            if similarity(j.value, l.value) < 0.3 and normalize_ws(j.value).lower() not in normalize_ws(l.value).lower() \
                    and normalize_ws(l.value).lower() not in normalize_ws(j.value).lower():
                fv = unresolved(f"JSON-LD and the page's own label disagree about the {k}: '{j.value}' vs '{l.value}'",
                                alternatives=[_alt(j), _alt(l)])
                fv.verification = "conflict"
                out[k] = fv
    # country by rule from the location text (explicit names only), only when no structured country exists
    if not out["country"].usable:
        from scraper.country_rules import _infer_country_from_location
        text = " ".join(filter(None, (out["address"].value, out["venue"].value, out["city"].value)))
        code = _infer_country_from_location(text)
        names = {"USA": "United States", "UK": "United Kingdom", "India": "India", "UAE": "United Arab Emirates", "AUS": "Australia", "SG": "Singapore"}
        if code:
            out["country"] = FieldValue(names[code], "rule", text[:200], "medium", "unverified", "country named in the location text")
    return out


def location_display(f: dict[str, FieldValue]) -> str:
    parts, seen = [], set()
    for k in ("venue", "address", "city", "region", "country"):
        v = f.get(k)
        if v and v.usable:
            # skip an address that merely repeats the venue / city already shown
            low = normalize_ws(v.value).lower()
            if low in seen or any(low in s for s in seen):
                continue
            seen.add(low)
            parts.append(v.value)
    return ", ".join(parts)


# ======================================================================================================================
# ORGANISER / DESCRIPTION / REGISTRATION
# ======================================================================================================================

def resolve_organiser(ctx: PageContext, org_name: str) -> FieldValue:
    r = ctx.jsonld or {}
    if r.get("organizer"):
        return FieldValue(r["organizer"], "jsonld", r["organizer"], "high", locator="JSON-LD Event.organizer")
    for p in ctx.pairs_of("organiser", include_contact=True):
        return FieldValue(p.value, "label", p.evidence, "high", locator=f"label '{p.label}'")
    if org_name:
        return FieldValue(org_name, "sources_sheet", org_name, "high", locator="the organisation whose events page listed this event")
    return unresolved("no organiser found")


def _clean_desc(text: str) -> str:
    t = html_lib.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return normalize_ws(t)


def description_candidates(ctx: PageContext) -> list[Cand]:
    out: list[Cand] = []
    r = ctx.jsonld or {}
    d = _clean_desc(r.get("description", ""))
    if len(d) >= 40 and not BOILER.search(d):
        out.append(Cand(d[:900], "jsonld", 5, d[:120], "JSON-LD Event.description"))
    root = ctx.container
    pair_texts = {normalize_ws(p.evidence).lower() for p in ctx.pairs} | {normalize_ws(p.value).lower() for p in ctx.pairs}
    blocks: list[str] = []
    if root is not None:
        seen_h1 = False
        for el in root.find_all(["p", "li", "div", "td", "blockquote"]):
            if el.find(["p", "li", "div", "table", "ul", "ol", "h1", "h2", "h3", "h4"]):
                continue
            if ctx.h1 and not seen_h1:
                pass
            t = normalize_ws(el.get_text(" ", strip=True))
            if len(t) < 50 or BOILER.search(t) or CTA_TEXT.match(t):
                continue
            tl = t.lower()
            if tl in pair_texts or any(pt and pt in tl and len(pt) > 20 and len(tl) < len(pt) + 20 for pt in pair_texts):
                continue
            if re.match(r"^(date|time|venue|location|address|when|where|cost|fee|price|registration)s?\s*[:\-]", t, re.I):
                continue
            if t in blocks:
                continue
            blocks.append(t)
            if len(blocks) >= 3:
                break
    if blocks:
        joined = "\n".join(blocks)[:900]
        out.append(Cand(joined, "paragraph", 3, blocks[0][:120], "first descriptive paragraphs of the event content"))
    for src, v in (("meta", ctx.og_description), ("meta", ctx.meta_description)):
        v = _clean_desc(v)
        if len(v) >= 50 and not BOILER.search(v):
            out.append(Cand(v[:600], src, 2, v[:120], "meta description"))
            break
    return out


def resolve_description(cands: list[Cand], ctx: PageContext) -> FieldValue:
    if not cands:
        return unresolved("no event description found on the page")
    best = max(cands, key=lambda c: (c.tier, len(c.value or "")))
    conf = "high" if best.tier >= 5 and len(best.value) >= 100 else "medium" if len(best.value) >= 60 else "low"
    fv = _fv(best, conf)
    fv.alternatives = [_alt(c) for c in cands if c is not best][:2]
    # contamination: a related event's title inside the description means it was read from the wrong block
    for t in ctx.excluded_titles:
        if len(t) > 12 and t.lower() in (best.value or "").lower() and t.lower() not in (ctx.h1 or "").lower():
            fv.verification = "conflict"
            fv.note = f"the description contains the title of another event ('{t}')"
            break
    return fv


_REG_CTA = re.compile(r"\b(register|registration|book|booking|buy\s+tickets?|get\s+tickets?|tickets?|enrol(?:l)?|sign[\s-]?up|rsvp|apply|reserve)\b", re.I)


def resolve_registration(ctx: PageContext, page_url: str) -> FieldValue:
    from pipeline.common import absolute_url, same_page
    r = ctx.jsonld or {}
    if r.get("registration_url"):
        return FieldValue(r["registration_url"], "jsonld", r["registration_url"], "high", locator="JSON-LD Event.offers.url")
    root = ctx.container
    if root is not None:
        for a in root.find_all("a", href=True):
            t = normalize_ws(a.get_text(" ", strip=True))
            if t and len(t) <= 40 and _REG_CTA.search(t):
                href = absolute_url(page_url, a["href"])
                if href and not href.lower().startswith(("mailto:", "tel:")):
                    return FieldValue(href, "link", f"{t} -> {href}", "high", locator="register/book link in the event content")
    return FieldValue(page_url, "page_url", page_url, "medium", locator="no separate registration link: the event page itself")
