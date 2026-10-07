"""Stage: FIELD-LEVEL RE-SCRAPE.

When one field fails, only that field is retried -- not the whole event. The
retry ladder, in order:

    1. existing page content, ALTERNATE extractor (different strategy, same page)
    2. HTTP retry (fresh request) -- when the page looked unreadable / JS-shell
    3. browser render
    4. targeted alternate extractor on the rendered page
    5. unresolved -> human review (the final state, recorded, never guessed)

Every attempt is logged, per field: which method, which extractor, what came
back. 'date: attempt 1 = JSON-LD, attempt 2 = labelled field, attempt 3 =
browser, final = unresolved' is exactly what ends up in `ev.attempts`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable

from pipeline.common import normalize_ws, sha
from pipeline.dates import date_context_is_deadline, find_dates, parse_iso, to_iso
from pipeline.extraction import fields as F
from pipeline.extraction.detail import ExtractedEvent, extract_fields
from pipeline.extraction.fields import Cand
from pipeline.extraction.page_context import build_context
from pipeline.fetcher import Page, has_app_root, looks_js_shell
from pipeline.models import Flag
from pipeline.quality import QualityReport, assess_quality

# flag -> the fields a retry should target
TARGETS = {
    Flag.MISSING_NAME: ("name",), Flag.WEAK_NAME: ("name",),
    Flag.MISSING_DATE: ("start_date",), Flag.YEAR_MISSING: ("start_date",), Flag.CONFLICTING_DATE: ("start_date",), Flag.WEAK_DATE: ("start_date",),
    Flag.MISSING_LOCATION: ("venue",), Flag.WEAK_LOCATION: ("venue",), Flag.CONFLICTING_LOCATION: ("venue",),
    Flag.MISSING_FORMAT: ("format",), Flag.WEAK_DESCRIPTION: ("description",), Flag.UNRELATED_CONTENT: ("description",),
}
GROUP = {"name": "name", "start_date": "date", "venue": "location", "format": "location", "description": "description"}
FIELD_SETS = {"name": ("name",), "start_date": ("start_date", "end_date", "start_time", "end_time", "timezone"),
              "venue": ("venue", "address", "city", "region", "country", "format"), "format": ("format",), "description": ("description",)}
NEEDS_BROWSER = {"name", "start_date", "venue"}
_VENUE_WORDS = re.compile(r"\b(centre|center|hotel|hall|tower|convention|campus|club|theatre|theater|university|house|park|resort|expo|room|court|institute|"
                          r"library|building|auditorium|gardens|square|street|road|level|floor|suite|stadium|arena|ballroom|college|school|city)\b", re.I)


@dataclass
class Attempt:
    field: str
    n: int
    level: int
    method: str
    extractor: str
    outcome: str            # resolved | unchanged | failed | skipped
    value: str = ""
    note: str = ""


# ---- alternate extractors (strategy B on the same content) -----------------------------------------------------------------

def alt_date_wide(ctx, **_) -> list[Cand]:
    """Exactly one distinct, non-deadline date anywhere in the event's own content."""
    hits = [h for h in find_dates(ctx.container_text, ctx.locale) if h.valid and not date_context_is_deadline(ctx.container_text, h)]
    distinct = {(h.start, h.end) for h in hits}
    if len(distinct) == 1:
        h = hits[0]
        return [Cand(to_iso(h.start), "body_wide", 2, ctx.container_text[max(0, h.span[0] - 40):h.span[1] + 40], "the only date in the event content",
                     start=h.start, end=h.end, extra={"raw": h.raw})]
    return []


def alt_date_attributes(ctx, **_) -> list[Cand]:
    out = []
    if ctx.container is None:
        return out
    for el in ctx.container.find_all(True):
        for attr in ("data-date", "data-start", "data-startdate", "data-event-date", "data-start-date", "content", "datetime"):
            v = el.get(attr)
            if v and el.get("itemprop", "").lower() in ("", "startdate", "datepublished") or attr.startswith("data-"):
                d = parse_iso(v or "")
                if d and (attr.startswith("data-") or el.get("itemprop", "").lower() == "startdate"):
                    out.append(Cand(to_iso(d), "attribute", 3, f'{attr}="{v}"', f"{el.name} attribute", start=d, extra={"raw": v}))
    return out[:2]


def alt_venue_header(ctx, **_) -> list[Cand]:
    """The venue-looking phrase that follows the date/time line right under the heading
    ('... 9.00am - 6.00pm Sands Expo & Convention Centre', 'Friday 13 November 2026, Chartered Accountants' Hall')."""
    seg = F._after_h1(ctx, 500)
    anchors = list(re.finditer(r"(?:\d{1,2}[.:]\d{2}\s*(?:am|pm)|\b(?:19|20)\d{2}\b)", seg, re.I))
    for m in reversed(anchors):
        tail = seg[m.end():m.end() + 140].lstrip(" 	|,•·-–—")
        run = re.match(r"((?:[A-Z][\w'’.\-]*|&|of|the|at|and|de|la)(?:\s+(?:[A-Z][\w'’.\-]*|&|of|the|at|and|de|la)){0,8})", tail)
        if not run:
            continue
        words = run.group(1).split()
        last = max((i for i, w in enumerate(words) if _VENUE_WORDS.fullmatch(w.strip(".,'’"))), default=-1)
        if last < 0:
            continue
        venue = " ".join(words[:last + 1]).strip(" ,.-")
        if F._VIRTUAL.search(venue) or len(venue) < 5:
            continue
        return [Cand(venue, "header_text", 2, seg[max(0, m.start() - 30):m.end() + len(venue) + 8], "text just below the main heading")]
    return []


def alt_location_microdata(ctx, **_) -> list[Cand]:
    out = []
    if ctx.container is not None:
        el = ctx.container.find(attrs={"itemprop": "location"})
        if el is not None:
            t = normalize_ws(el.get_text(" ", strip=True))
            if 3 <= len(t) <= 200:
                out.append(Cand(t, "microdata", 3, t, "itemprop=location"))
    return out


def alt_name_h2(ctx, **_) -> list[Cand]:
    from pipeline.discovery.cards import is_generic_title
    if ctx.container is not None:
        for h in ctx.container.find_all(["h2", "h3"]):
            t = normalize_ws(h.get_text(" ", strip=True))
            if t and not is_generic_title(t, ctx.org_name) and 8 <= len(t) <= 160:
                return [Cand(t, "heading2", 3, t, "first h2 (no usable h1)")]
    return []


def alt_description_any(ctx, **_) -> list[Cand]:
    if ctx.container is None:
        return []
    ps = [normalize_ws(p.get_text(" ", strip=True)) for p in ctx.container.find_all("p")]
    ps = [p for p in ps if len(p) >= 40 and not F.BOILER.search(p)]
    return [Cand("\n".join(ps[:3])[:900], "paragraph_any", 2, ps[0][:120], "any paragraphs in the event content")] if ps else []


def alt_format_text(ctx, **_) -> list[Cand]:
    m = F.mode_from_text(ctx.container_text[:1500])
    return [Cand(m, "text", 2, ctx.container_text[:160], "mode words in the first part of the event content")] if m else []


ALTERNATES: dict[str, list[tuple[str, Callable]]] = {
    "name": [("alt_name_h2", alt_name_h2)],
    "start_date": [("alt_date_attributes", alt_date_attributes), ("alt_date_wide", alt_date_wide)],
    "venue": [("alt_location_microdata", alt_location_microdata), ("alt_venue_header", alt_venue_header)],
    "format": [("alt_format_text", alt_format_text)],
    "description": [("alt_description_any", alt_description_any)],
}


def _extra(target: str, ctx) -> tuple[dict, list[str]]:
    group = GROUP[target]
    names = []
    if group == "location":
        out: dict = {}
        for n, fn in ALTERNATES.get(target, []):
            for c in fn(ctx):
                key = "format" if target == "format" else ("address" if re.search(r"\d", c.value or "") and len(c.value) > 25 and F._ADDRESS_LIKE.search(c.value) else "venue")
                out.setdefault(key, []).append(c)
                names.append(n)
        return {"location": out} if out else {}, names
    cands = []
    for n, fn in ALTERNATES.get(target, []):
        got = fn(ctx)
        if got:
            names.append(n)
        cands += got
    return ({group: cands} if cands else {}), names


# ---- the ladder ----------------------------------------------------------------------------------------------------------------

def targets_for(flags: list[str]) -> list[str]:
    out: list[str] = []
    for fl in flags:
        for t in TARGETS.get(fl, ()):
            if t not in out:
                out.append(t)
    return out


def _cleared(target: str, q: QualityReport) -> bool:
    mapping = {"name": (Flag.MISSING_NAME, Flag.WEAK_NAME), "start_date": (Flag.MISSING_DATE, Flag.YEAR_MISSING, Flag.CONFLICTING_DATE, Flag.WEAK_DATE),
               "venue": (Flag.MISSING_LOCATION, Flag.WEAK_LOCATION, Flag.CONFLICTING_LOCATION), "format": (Flag.MISSING_FORMAT,),
               "description": (Flag.WEAK_DESCRIPTION, Flag.UNRELATED_CONTENT)}
    return not (set(mapping[target]) & set(q.flags))


def _apply(ev: ExtractedEvent, new: dict, target: str) -> bool:
    """Replace fields of one target group when the retry produced a usable (or better) value. Returns True if anything changed."""
    changed = False
    for k in FIELD_SETS[target]:
        nv = new.get(k)
        if nv is None:
            continue
        old = ev.fields.get(k)
        better = (nv.usable and (not old or not old.usable)) or \
                 (nv.usable and old and old.usable and nv.value != old.value and (nv.confidence in ("high", "medium") and old.confidence == "low")) or \
                 (nv.usable and old and old.usable and old.verification == "conflict" and nv.verification != "conflict")
        if better:
            ev.fields[k] = nv
            changed = True
        elif not nv.usable and old and not old.usable and nv.note and nv.note != old.note:
            ev.fields[k] = nv       # a clearer 'why unresolved'
    return changed


def rescrape(ev: ExtractedEvent, q: QualityReport, *, fetch_http: Callable[[str], Page] | None = None,
             fetch_browser: Callable[[str], Page] | None = None, today: date | None = None, card_date_hint: str = "",
             has_own_page: bool = True, max_levels: int = 5) -> tuple[ExtractedEvent, QualityReport]:
    """Retry only the flagged fields. Returns the (updated) event and its fresh quality report; attempts go to ev.attempts."""
    today = today or date.today()
    if not has_own_page or ev.page_kind == "listing":
        return ev, q                                   # nothing to re-scrape: there is no page of its own
    pending = targets_for(q.flags)
    if not pending:
        return ev, q
    counters: dict[str, int] = {}

    def log(target, level, method, extractor, outcome, value="", note=""):
        counters[target] = counters.get(target, 0) + 1
        ev.attempts.append(asdict(Attempt(target, counters[target], level, method, extractor, outcome, value or "", note)))

    # attempt 1 is always what the first extraction already did -- recorded so the trail reads from the start
    for t in pending:
        first = ev.fields.get(FIELD_SETS[t][0])
        src = (first.source if first and first.usable else "") or "default chain"
        log(t, 0, "extraction", "default extractor chain", "unresolved" if not (first and first.usable) else "weak", first.value if first and first.value else "", (first.note if first else "")[:200])

    def settle(level_label: str) -> None:
        nonlocal q, pending
        q = assess_quality(ev, card_date_hint=card_date_hint, has_own_page=has_own_page, today=today)
        pending = [t for t in pending if not _cleared(t, q)]

    # ---- level 1: alternate extractor on the same content ----------------------------------------------------------------------------------
    if ev.ctx is not None and ev.ctx.readable:
        for t in list(pending):
            extra, used = _extra(t, ev.ctx)
            if not extra:
                log(t, 1, "existing_content", "alternate extractors", "unchanged", note="no alternate extractor found anything")
                continue
            new = extract_fields(ev.ctx, FIELD_SETS[t], card_title=ev.card_title, card_date_hint=card_date_hint, org_name=ev.org_name,
                                 page_url=ev.url, extra_cands=extra)
            changed = _apply(ev, new, t)
            top = ev.fields.get(FIELD_SETS[t][0])
            log(t, 1, "existing_content", "+".join(used), "resolved" if changed else "unchanged", (top.value if top else "") or "")
        settle("alt")
    if not pending or max_levels < 2:
        return ev, q

    # ---- level 2: HTTP retry (page unreadable / empty shell / transient failure) ------------------------------------------------------------------
    html_now = ev.ctx.html if ev.ctx else ""
    suspicious_page = (not ev.readable) or looks_js_shell(html_now) or has_app_root(html_now or "")
    if fetch_http and suspicious_page:
        page = fetch_http(ev.url)
        if page.ok and sha(page.html or "") != sha(html_now):
            new_ctx = build_context(page.html, ev.url, page.final_url or ev.url, locale=ev.ctx.locale if ev.ctx else "dmy",
                                    org_name=ev.org_name, card_title=ev.card_title, today=today)
            if new_ctx.readable:
                ev.ctx, ev.readable, ev.unreadable_reason, ev.page_kind = new_ctx, True, "", new_ctx.page_kind
                fresh = extract_fields(new_ctx, [n for t in pending for n in FIELD_SETS[t]], card_title=ev.card_title,
                                       card_date_hint=card_date_hint, org_name=ev.org_name, page_url=ev.url)
                for t in list(pending):
                    log(t, 2, "http_retry", "default extractor chain", "resolved" if _apply(ev, fresh, t) else "unchanged")
                settle("http")
            else:
                for t in pending:
                    log(t, 2, "http_retry", "default extractor chain", "failed", note=new_ctx.unreadable_reason)
        else:
            for t in pending:
                log(t, 2, "http_retry", "default extractor chain", "failed" if not page.ok else "unchanged", note=page.error or "same page content")
    if not pending or max_levels < 3:
        return ev, q

    # ---- level 3/4: browser render, then the alternates on the rendered page --------------------------------------------------------------------------
    need_browser = [t for t in pending if t in NEEDS_BROWSER]
    if fetch_browser and need_browser:
        page = fetch_browser(ev.url)
        if page.ok and page.html and sha(page.html) != sha(ev.ctx.html if ev.ctx else ""):
            new_ctx = build_context(page.html, ev.url, page.final_url or ev.url, locale=ev.ctx.locale if ev.ctx else "dmy",
                                    org_name=ev.org_name, card_title=ev.card_title, today=today)
            if new_ctx.readable:
                ev.ctx, ev.readable, ev.unreadable_reason, ev.page_kind = new_ctx, True, "", new_ctx.page_kind
                fresh = extract_fields(new_ctx, [n for t in need_browser for n in FIELD_SETS[t]], card_title=ev.card_title,
                                       card_date_hint=card_date_hint, org_name=ev.org_name, page_url=ev.url)
                for t in need_browser:
                    log(t, 3, "browser_render", "default extractor chain", "resolved" if _apply(ev, fresh, t) else "unchanged")
                settle("browser")
                for t in [x for x in need_browser if x in pending]:
                    extra, used = _extra(t, new_ctx)
                    if extra:
                        new = extract_fields(new_ctx, FIELD_SETS[t], card_title=ev.card_title, card_date_hint=card_date_hint,
                                             org_name=ev.org_name, page_url=ev.url, extra_cands=extra)
                        log(t, 4, "browser_render", "+".join(used), "resolved" if _apply(ev, new, t) else "unchanged")
                    else:
                        log(t, 4, "browser_render", "alternate extractors", "unchanged", note="no alternate extractor found anything")
                settle("browser-alt")
            else:
                for t in need_browser:
                    log(t, 3, "browser_render", "default extractor chain", "failed", note=new_ctx.unreadable_reason)
        else:
            for t in need_browser:
                log(t, 3, "browser_render", "default extractor chain", "failed" if not page.ok else "unchanged", note=page.error or "same content as the HTTP page")

    # ---- level 5: whatever is still pending is unresolved: human review ---------------------------------------------------------------------------------------
    for t in pending:
        log(t, 5, "final", "-", "unresolved", note="could not be established from the official page; sent for review, never guessed")
    return ev, q
