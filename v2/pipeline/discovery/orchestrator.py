"""Discovery of one organisation: every mechanism, then a completeness verdict.

    configured URL
      -> HTTP fetch (browser if blocked / JavaScript shell / needs Load more / scroll / button pager)
      -> cards + JSON-LD on every page of the list (rel=next, numbered, ?page=N, offset, cursor,
         Load more, infinite scroll, JS pager) -- archives are never crawled
      -> RSS / ICS / sitemap as second opinions (and extra candidates)
      -> merge by event URL -> Candidates, with every drop recorded
      -> completeness check (complete / incomplete / failed)

Nothing here raises for one bad page, node or feed: failures are recorded on the
DiscoveryResult and make the organisation *incomplete or failed*, never "empty".
"""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable
from urllib.parse import urlsplit

from pipeline.common import norm_text, norm_url, normalize_ws, same_page, sha, similarity
from pipeline.dates import find_dates, parse_iso
from pipeline.discovery import cards as cards_mod
from pipeline.discovery import feeds as feeds_mod
from pipeline.discovery import jsonld as jsonld_mod
from pipeline.discovery.completeness import Signals, assess
from pipeline.discovery.listing import find_events_page_link, has_empty_state, is_archive_page, listing_signals
from pipeline.discovery.pagination import TYPICAL_PAGE_SIZES, PagerInfo, analyze_pagination, with_page
from pipeline.fetcher import Fetcher, Page, has_app_root, is_soft_404, looks_js_shell, site_root
from pipeline.models import Candidate, DiscoveryResult, Dropped

MAX_HTTP_PAGES = 40
MAX_SITEMAP_ADD = 150


@dataclass
class DiscoveryContext:
    http: Fetcher
    browser_factory: Callable[[], AbstractContextManager] | None = None
    today: date = field(default_factory=date.today)
    previous_counts: dict[str, int] = field(default_factory=dict)
    use_feeds: bool = True
    max_pages: int = MAX_HTTP_PAGES
    deadline_s: float = 150


def _locale(country: str) -> str:
    return "mdy" if (country or "").upper() in ("USA", "US") else "dmy"


def _cand_key(url: str, title: str, date_hint: str, shared: bool = False) -> str:
    if url and not shared:
        return "u:" + norm_url(url)
    return "t:" + norm_text(title) + "|" + norm_text(date_hint)


class _Acc:
    """Accumulates candidates across pages / mechanisms, merging by identity and recording duplicates."""

    def __init__(self, res: DiscoveryResult, org: dict):
        self.res, self.org = res, org
        self.by_key: dict[str, Candidate] = {}
        self.archive_keys: set[str] = set()

    def add(self, *, url: str, title: str, tconf: str, card_text: str, date_hint: str, loc_hint: str, methods: list[str],
            listing_url: str, notes: list[str], in_archive: bool = False, shared: bool = False, section: str = "",
            jsonld: dict | None = None, feed: dict | None = None, ext_id: str = "") -> Candidate | None:
        key = _cand_key(url, title, date_hint, shared)
        if in_archive:
            if key not in self.archive_keys and key not in self.by_key:
                self.archive_keys.add(key)
                self.res.dropped.append(Dropped(self.org["organizer"], listing_url, "archive_section", title, url,
                                                f"under '{section}'" if section else "in a past-events section"))
            return None
        cur = self.by_key.get(key)
        if cur is None and not shared and url:
            # a JSON-LD / feed item without a URL may match a card by title
            pass
        if cur is not None:
            for m in methods:
                if m not in cur.methods:
                    cur.methods.append(m)
            if jsonld and not cur.jsonld:
                cur.jsonld = jsonld
            if feed and not getattr(cur, "feed", None):
                cur.notes.append("also listed in a feed")
            if (not cur.title or cur.title_confidence == "low") and title and tconf != "low":
                cur.title, cur.title_confidence = title, tconf
            if not cur.date_hint and date_hint:
                cur.date_hint = date_hint
            if not cur.location_hint and loc_hint:
                cur.location_hint = loc_hint
            if not cur.url and url:
                cur.url = url
            self.res.dropped.append(Dropped(self.org["organizer"], listing_url, "merged_duplicate", title, url,
                                            "same event seen again (" + "+".join(methods) + ")"))
            return cur
        cand = Candidate(candidate_id="c_" + sha(f"{self.org['organizer']}|{key}", 14), org=self.org["organizer"],
                         country=self.org.get("country", ""), source_url=self.org["url"], listing_url=listing_url,
                         url="" if shared else url, title=title, title_confidence=tconf, card_text=card_text,
                         date_hint=date_hint, location_hint=loc_hint, methods=list(methods), ext_id=ext_id,
                         jsonld=jsonld, feed=feed, notes=list(notes))
        if shared and url:
            cand.notes.append(f"its link {url} is shared with other events, so it is not used as this event's own page")
        self.by_key[key] = cand
        return cand

    def match_by_title(self, title: str, thresh: float = 0.85) -> Candidate | None:
        for c in self.by_key.values():
            if c.title and title and similarity(c.title, title) >= thresh:
                return c
        return None


# ---- per-page parsing ----------------------------------------------------------------------------------------

def _ingest_page(acc: _Acc, res: DiscoveryResult, sig: Signals, html: str, page_url: str, org: dict, locale: str,
                 kind: str) -> dict:
    """Cards + JSON-LD of one rendered page into the accumulator. Returns per-page stats."""
    stat = {"url": page_url, "kind": kind, "cards": 0, "archive_cards": 0, "jsonld": 0, "new": 0, "past_only": False}
    before = len(acc.by_key)
    try:
        cards, drops, cstats = cards_mod.find_cards(html, page_url, locale, org["organizer"])
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"card discovery failed on {page_url}: {type(exc).__name__}: {exc!s:.100}")
        res.failure_kinds.append("extraction")
        cards, drops, cstats = [], [], {}
    stat["cards"] = len(cards)
    sig.cards += len(cards)
    for d in drops:
        res.dropped.append(Dropped(org["organizer"], page_url, d.reason, d.title, d.url, d.detail))
    current_cards = 0
    for c in cards:
        if c.in_archive:
            stat["archive_cards"] += 1
        else:
            current_cards += 1
        acc.add(url=c.url, title=c.title, tconf=c.title_confidence, card_text=c.card_text, date_hint=c.date_hint,
                loc_hint=c.location_hint, methods=c.methods, listing_url=page_url, notes=c.notes, in_archive=c.in_archive,
                shared=c.shared_url, section=c.section)
        if c.alt_urls:
            cand = acc.by_key.get(_cand_key(c.url, c.title, c.date_hint, c.shared_url))
            if cand:
                cand.alt_urls = list(dict.fromkeys(cand.alt_urls + c.alt_urls))
                cand.notes.append("other links in its card: " + ", ".join(c.alt_urls[:3]))
    sig.event_links += sum(1 for c in cards if c.url)
    if cards and current_cards == 0:
        stat["past_only"] = True

    events, problems = jsonld_mod.parse_jsonld_events(html, page_url)
    for pr in problems:
        res.dropped.append(Dropped(org["organizer"], page_url, pr["reason"], "", "", pr.get("detail", "")))
    stat["jsonld"] = len(events)
    sig.jsonld_events += len(events)
    for ev in events:
        try:
            _merge_jsonld(acc, ev, page_url, org, kind)
        except Exception as exc:  # noqa: BLE001
            res.dropped.append(Dropped(org["organizer"], page_url, "jsonld_merge_error", ev.get("name", ""), ev.get("url", ""), str(exc)[:100]))
    stat["new"] = len(acc.by_key) - before
    return stat


def _merge_jsonld(acc: _Acc, ev: dict, page_url: str, org: dict, kind: str) -> None:
    name, url = ev.get("name", ""), ev.get("url", "")
    start = ev.get("startDate", "")
    cur = acc.by_key.get(_cand_key(url, name, "")) if url else None
    if cur is None and name:
        cur = acc.match_by_title(name)
    notes = [ev["problem"]] if ev.get("problem") else []
    if cur is not None:
        if not cur.jsonld:
            cur.jsonld = ev
        if "jsonld" not in cur.methods:
            cur.methods.append("jsonld")
        if not cur.url and url:
            cur.url = url
        if not cur.date_hint and start:
            cur.date_hint = start[:10]
        if ev.get("ext_id") and not cur.ext_id:
            cur.ext_id = ev["ext_id"]
        return
    loc = ev.get("location", {}).get("text", "")
    acc.add(url=url, title=name, tconf="high" if name else "low", card_text=normalize_ws(f"{name} {start} {loc} {ev.get('description', '')[:200]}"),
            date_hint=start[:10] if start else "", loc_hint=loc, methods=["jsonld"], listing_url=page_url, notes=notes,
            jsonld=ev, ext_id=ev.get("ext_id", ""))


# ---- feeds & sitemaps ---------------------------------------------------------------------------------------------

def _current(iso_start: str, iso_end: str, today: date) -> bool:
    s, e = parse_iso(iso_start), parse_iso(iso_end)
    last = e or s
    return last is None or last >= today - timedelta(days=1)


def _use_feeds(ctx: DiscoveryContext, acc: _Acc, res: DiscoveryResult, sig: Signals, org: dict, html: str, url: str) -> None:
    """ICS calendars list real, dated events: any current one the list did not show is a candidate AND proof that
    discovery was incomplete. RSS is only a hint (its dates are post dates), so it never adds candidates."""
    links = feeds_mod.find_feed_links(html, url)
    missing = 0
    for ics_url in links["ics"][:2]:
        p = ctx.http.get(ics_url)
        res.pages.append({"url": ics_url, "kind": "ics", "status": p.status, "error": p.error})
        if not p.ok:
            continue
        for ev in feeds_mod.parse_ics(p.html, ics_url):
            if not _current(ev["startDate"], ev["endDate"], ctx.today):
                continue
            cur = acc.by_key.get(_cand_key(ev["url"], ev["name"], "")) if ev["url"] else None
            cur = cur or (acc.match_by_title(ev["name"]) if ev["name"] else None)
            if cur is not None:
                if "ics" not in cur.methods:
                    cur.methods.append("ics")
                if not cur.date_hint and ev["startDate"]:
                    cur.date_hint = ev["startDate"]
                continue
            missing += 1
            acc.add(url=ev["url"], title=ev["name"], tconf="medium", card_text=normalize_ws(f"{ev['name']} {ev['startDate']} {ev['location']}"),
                    date_hint=ev["startDate"], loc_hint=ev["location"], methods=["ics"], listing_url=ics_url,
                    notes=["found in the ICS calendar but not on the list page"], feed=ev, ext_id=ev["ext_id"])
    sig.feed_events = missing
    for rss_url in links["rss"][:1]:
        p = ctx.http.get(rss_url)
        res.pages.append({"url": rss_url, "kind": "rss", "status": p.status, "error": p.error})


def _quick_upcoming(html: str, today: date, locale: str) -> str:
    """ISO date of an upcoming event on this page, or '' (JSON-LD startDate first, else a full date near the top)."""
    events, _ = jsonld_mod.parse_jsonld_events(html, "")
    for ev in events:
        d = parse_iso(ev.get("endDate") or ev.get("startDate"))
        if d and d >= today:
            return d.isoformat()
    text = normalize_ws(re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", re.sub(r"<[^>]+>", " ", html)))[:3000]
    from pipeline.dates import date_context_is_deadline
    for h in find_dates(text, locale):
        if h.valid and not date_context_is_deadline(text, h) and (h.end or h.start) >= today:
            return (h.end or h.start).isoformat()
    return ""


def _use_sitemap(ctx: DiscoveryContext, acc: _Acc, res: DiscoveryResult, sig: Signals, org: dict, url: str, locale: str,
                 probe: int = 6) -> None:
    """The sitemap lists past events too, so its COUNT proves nothing. What it can prove: an event page, absent from the
    list, that is dated in the future. Probe the newest few; any upcoming one is added and makes discovery incomplete."""
    from collections import Counter
    root = site_root(url)
    sm_urls: list[str] = []
    robots = ctx.http.get(root + "/robots.txt")
    if robots.ok:
        sm_urls = [m.group(1).strip() for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", robots.html)]
    if not sm_urls:
        sm_urls = [root + "/sitemap.xml"]
    found: list[tuple[str, str]] = []
    queue, fetched = list(sm_urls[:3]), 0
    while queue and fetched < 5:
        sm = queue.pop(0)
        p = ctx.http.get(sm)
        fetched += 1
        res.pages.append({"url": sm, "kind": "sitemap", "status": p.status, "error": p.error})
        if not p.ok:
            continue
        urls, children = feeds_mod.parse_sitemap(p.html)
        found += urls
        for ch in children:
            if re.search(r"event|webinar|seminar|course|training|calendar", ch, re.I) and len(queue) < 4:
                queue.append(ch)
    have = {norm_url(u) for c in acc.by_key.values() for u in ([c.url] + c.alt_urls) if u}
    prefixes = [pre for pre, n in Counter(urlsplit(c.url).path.rsplit("/", 1)[0] + "/" for c in acc.by_key.values() if c.url).most_common(3)]
    pool = [(u, lm) for u, lm in feeds_mod.event_like_sitemap_urls(found, url)
            if norm_url(u) not in have and (not prefixes or any(urlsplit(u).path.startswith(pre) for pre in prefixes))]
    pool.sort(key=lambda t: t[1] or "", reverse=True)
    sig.extra["sitemap_event_like_not_found"] = len(pool)
    missing = []
    for u, lm in pool[:probe]:
        pg = ctx.http.get(u)
        if pg.ok and (d := _quick_upcoming(pg.html, ctx.today, locale)):
            missing.append((u, d))
            slug = urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1]
            acc.add(url=u, title=normalize_ws(re.sub(r"[-_]+", " ", slug)).title(), tconf="low", card_text="", date_hint=d,
                    loc_hint="", methods=["sitemap"], listing_url=u,
                    notes=[f"upcoming event page ({d}) found in the sitemap but not on the list page; title is the URL slug until the page is read"])
    sig.sitemap_events = len(missing)
    sig.extra["sitemap_probed"] = min(probe, len(pool))


# ---- the walk ----------------------------------------------------------------------------------------------------------

def _page_is_archive_only(html: str, page_url: str, locale: str) -> bool:
    try:
        cards, _, _ = cards_mod.find_cards(html, page_url, locale)
    except Exception:  # noqa: BLE001
        return False
    return bool(cards) and all(c.in_archive for c in cards)


def _walk_urls(first_html: str, first_url: str, info: PagerInfo, get: Callable[[str], Page], acc: _Acc, res: DiscoveryResult,
               sig: Signals, org: dict, locale: str, max_pages: int, kind: str) -> str:
    """Follow addressable pagination. Returns the end state: none | complete | capped | stuck | error | loop."""
    seen = {norm_url(first_url)}
    last_sig = norm_text(first_html)[:2000]
    pages = 1
    while pages < max_pages:
        nxt = info.next_url
        if not nxt and info.pattern and sig.stated_pages and pages < sig.stated_pages:
            nxt = with_page(first_url, pages + 1, info.pattern)
        if not nxt:
            return "complete" if pages > 1 else "none"
        if norm_url(nxt) in seen:
            res.failure_kinds.append("pagination")
            return "loop"
        seen.add(norm_url(nxt))
        pg = get(nxt)
        res.pages.append({"url": nxt, "kind": kind, "status": pg.status, "error": pg.error, "method": pg.method})
        if not pg.ok:
            sig.page_errors.append(f"{nxt}: {pg.error}")
            res.failure_kinds.append("pagination")
            return "error"
        body_sig = norm_text(pg.html)[:2000]
        if body_sig == last_sig:
            res.failure_kinds.append("pagination")
            return "loop"
        last_sig = body_sig
        before = len(acc.by_key)
        stat = _ingest_page(acc, res, sig, pg.html, pg.final_url or nxt, org, locale, kind)
        pages += 1
        sig.pages_walked = max(sig.pages_walked, pages)
        if stat["past_only"] or (stat["cards"] == 0 and stat["jsonld"] == 0):
            return "complete"          # archive reached / list ran out: stop without crawling it
        if len(acc.by_key) == before and stat["cards"] > 0:
            res.failure_kinds.append("pagination")
            return "loop"              # a whole page of events we already had
        info = analyze_pagination(pg.html, pg.final_url or nxt)
    res.failure_kinds.append("pagination")
    return "capped"


def discover_org(org: dict, ctx: DiscoveryContext) -> DiscoveryResult:
    url = org["url"]
    res = DiscoveryResult(org=org["organizer"], country=org.get("country", ""), source_url=url)
    sig = Signals(previous_count=ctx.previous_counts.get(url))
    locale = _locale(org.get("country", ""))
    try:
        _discover(org, ctx, res, sig, locale)
    except Exception as exc:  # noqa: BLE001 - discovery of one org never raises into the run
        res.errors.append(f"discovery crashed: {type(exc).__name__}: {exc!s:.160}")
        res.failure_kinds.append("crash")
        sig.fetch_ok, sig.fetch_error = False, f"crash: {exc!s:.80}"
    res.completeness = assess(sig)
    res.failure_kinds = list(dict.fromkeys(res.failure_kinds))
    return res


def _browser_pass(ctx: DiscoveryContext, acc: _Acc, res: DiscoveryResult, sig: Signals, org: dict, locale: str, open_url: str,
                  base_url: str, info: PagerInfo, why: list[str], http_ok: bool) -> tuple[bool, str]:
    """Render `open_url`, expand it (load more / scroll / pager) and ingest every snapshot. -> (page_ok, soft_404_seen)"""
    try:
        with ctx.browser_factory() as br:
            rep = br.expand_listing(open_url, deadline_s=ctx.deadline_s, want_pager=not info.addressable, max_pages=ctx.max_pages,
                                    page_is_archive=lambda h: _page_is_archive_only(h, base_url, locale))
            res.pages.append({"url": open_url, "kind": "browser", "status": rep.status, "error": rep.error, "method": "browser",
                              "why": why, "report": rep.to_dict()})
            res.failure_kinds += rep.failure_kinds
            if not rep.snapshots:
                sig.fetch_error = rep.error or "browser returned nothing"
                sig.blocked = sig.blocked or bool(rep.error and "block" in rep.error)
                return False, ""
            first = rep.snapshots[0]
            if is_soft_404(first, rep.status):
                sig.soft_404 = True
                res.pages[-1]["note"] = "soft-404 after rendering: its text is not read for events"
                return True, "soft_404"
            sig.load_more_state, sig.scroll_state = rep.load_more["state"], rep.scroll["state"]
            if rep.pager["state"] != "none":
                sig.pagination_state = rep.pager["state"]
            sig.pages_walked = max(sig.pages_walked, rep.pager.get("pages", 1))
            rinfo = analyze_pagination(first, rep.final_url or base_url)
            sig.stated_total = sig.stated_total or rinfo.stated_total
            sig.stated_pages = sig.stated_pages or rinfo.stated_pages
            sig.archive_pager_ignored = sig.archive_pager_ignored or rinfo.archive_pager
            sig.empty_state_text = sig.empty_state_text or has_empty_state(re.sub(r"<[^>]+>", " ", first))
            for i, (snap, su) in enumerate(zip(rep.snapshots, rep.snapshot_urls)):
                _ingest_page(acc, res, sig, snap, su or base_url, org, locale, "browser_page" if i else "browser_list")
            if rinfo.addressable and len(rep.snapshots) == 1:
                sig.pagination_state = _walk_urls(first, rep.final_url or base_url, rinfo, br.open_page, acc, res, sig, org, locale,
                                                  ctx.max_pages, "browser_url_page")
            return True, ""
    except Exception as exc:  # noqa: BLE001 - the browser failing is an incomplete discovery, never an empty one
        res.errors.append(f"browser failed: {type(exc).__name__}: {exc!s:.140}")
        res.failure_kinds.append("js")
        sig.fetch_error = f"browser error: {exc!s:.80}"
        return False, ""


def _discover(org: dict, ctx: DiscoveryContext, res: DiscoveryResult, sig: Signals, locale: str) -> None:
    url = org["url"]
    acc = _Acc(res, org)
    browser = ctx.browser_factory is not None
    p0 = ctx.http.get(url)
    res.pages.append({"url": url, "kind": "list", "status": p0.status, "error": p0.error, "method": p0.method})
    base = p0
    needs_browser, why = False, []

    if not p0.ok:
        if p0.status in (404, 410):
            sig.fetch_ok, sig.fetch_error = False, p0.error or "HTTP 404"
            res.failure_kinds.append("fetch")
        else:
            needs_browser = True
            why.append(f"HTTP fetch failed ({p0.error})")
            sig.blocked = bool(p0.error and "block" in p0.error)
    elif is_soft_404(p0.html, p0.status):
        sig.soft_404 = True                       # a 'not found' view served with HTTP 200: never read for events
    elif looks_js_shell(p0.html):
        needs_browser = True
        why.append("page is a JavaScript shell")

    def first_pass(html: str, purl: str) -> tuple[PagerInfo, int]:
        pinfo = analyze_pagination(html, purl)
        try:
            cs, _, _ = cards_mod.find_cards(html, purl, locale, org["organizer"])
        except Exception:  # noqa: BLE001
            cs = []
        return pinfo, len([c for c in cs if not c.in_archive])

    info, n_cards = PagerInfo(), 0
    if p0.ok and not looks_js_shell(p0.html):
        info, n_cards = first_pass(p0.html, p0.final_url or url)
        events0, _ = jsonld_mod.parse_jsonld_events(p0.html, url)
        sig.soft_404 = is_soft_404(p0.html, p0.status)
        sig.empty_state_text = has_empty_state(re.sub(r"<[^>]+>", " ", p0.html))
        sig.source_url_is_archive = is_archive_page(url, p0.html)
        if not sig.soft_404 and n_cards < 2 and not events0 and listing_signals(p0.html, url, locale)["dates"] < 2:
            ev_url = find_events_page_link(p0.html, url)       # the configured URL is a homepage: follow its Events link once
            if ev_url:
                p1 = ctx.http.get(ev_url)
                res.pages.append({"url": ev_url, "kind": "events_link_followed", "status": p1.status, "error": p1.error})
                if p1.ok and not looks_js_shell(p1.html):
                    base = p1
                    info, n_cards = first_pass(p1.html, p1.final_url or ev_url)
                    sig.soft_404 = is_soft_404(p1.html, p1.status)
                    sig.empty_state_text = has_empty_state(re.sub(r"<[^>]+>", " ", p1.html))
        sig.stated_total, sig.stated_pages = info.stated_total, info.stated_pages
        for flag, msg in ((info.load_more, f"'{info.load_more_text}' button"), (info.infinite_hint, "infinite-scroll hint"),
                          (info.js_pager and not info.addressable, "JavaScript pager"),
                          (has_app_root(base.html) and n_cards == 0, "empty app shell"),
                          (bool(sig.stated_total) and sig.stated_total > n_cards and not info.addressable and not sig.soft_404,
                           f"page states {sig.stated_total} events, {n_cards} found"),
                          (n_cards in TYPICAL_PAGE_SIZES and not info.addressable and not sig.stated_total and not info.archive_pager,
                           f"{n_cards} events is a typical page size: checking for more in the browser")):
            if flag:
                needs_browser, why = True, why + [msg]
        sig.archive_pager_ignored = info.archive_pager
        if info.notes:
            res.pages[-1]["pager_notes"] = info.notes

    base_url = base.final_url or base.url
    http_ok = base.ok and (not looks_js_shell(base.html or "") or sig.soft_404)
    browser_done = False

    if http_ok and sig.soft_404 and not needs_browser:
        res.pages[-1]["note"] = "soft-404: the page is a 'not found' view, its text is not read for events"
    elif http_ok and not (needs_browser and browser):
        stat = _ingest_page(acc, res, sig, base.html, base_url, org, locale, "list")
        sig.pagination_state = _walk_urls(base.html, base_url, info, ctx.http.get, acc, res, sig, org, locale, ctx.max_pages, "list_page")
        if needs_browser and not browser:
            sig.js_unrendered = n_cards == 0
            res.failure_kinds.append("js")
            res.errors.append("browser rendering was needed but is not available: " + "; ".join(why))
            if info.load_more:
                sig.load_more_state = "error"
    elif not http_ok and not browser:
        sig.fetch_ok = False
        sig.fetch_error = base.error or "page needs JavaScript and no browser is available"
        res.failure_kinds.append("fetch" if base.error else "js")
    else:
        if http_ok:
            _ingest_page(acc, res, sig, base.html, base_url, org, locale, "list_http")
        ok, _ = _browser_pass(ctx, acc, res, sig, org, locale, base_url if http_ok else url, base_url, info, why, http_ok)
        browser_done = True
        if not ok and not http_ok:
            sig.fetch_ok = False
            res.failure_kinds.append("fetch")

    # HTTP gave a page with NO events at all and no 'there are none' text: plain HTTP is not trusted to be complete -> render it
    if (browser and not browser_done and not acc.by_key and not sig.soft_404 and not sig.empty_state_text
            and (base.ok or not sig.fetch_ok is False)):
        ok, _ = _browser_pass(ctx, acc, res, sig, org, locale, url, base_url, info,
                              ["plain HTTP returned no events and no 'no events' message; rendering the page to check"], http_ok)
        browser_done = True

    if sig.soft_404 and not acc.by_key:
        res.failure_kinds.append("fetch")

    # ---- second opinions -------------------------------------------------------------------------------------------
    list_found = len([c for c in acc.by_key.values() if any(m in ("card", "text_date", "jsonld") for m in c.methods)])
    sig.candidates_current = list_found
    sig.candidates_all = list_found + len(acc.archive_keys)
    sig.cards = max(sig.cards, 0)
    if ctx.use_feeds and (base.html or "") and not sig.soft_404:
        try:
            _use_feeds(ctx, acc, res, sig, org, base.html, base_url)
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"feed parsing failed: {exc!s:.100}")
    if ctx.use_feeds and not sig.soft_404:
        try:
            _use_sitemap(ctx, acc, res, sig, org, url, locale)
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"sitemap parsing failed: {exc!s:.100}")
    res.candidates = list(acc.by_key.values())


def discover_from_snapshots(org: dict, snapshots: list[str], ctx: DiscoveryContext | None = None) -> DiscoveryResult:
    """Offline entry: run card + JSON-LD discovery over already-rendered pages (no fetching, no completeness
    claims beyond what the pages say). Used by the measurement harness ('ideal DOM' mode) and by tests."""
    res = DiscoveryResult(org=org["organizer"], country=org.get("country", ""), source_url=org["url"])
    sig = Signals()
    acc = _Acc(res, org)
    locale = _locale(org.get("country", ""))
    for i, html in enumerate(snapshots):
        if i == 0 and is_soft_404(html, 200):
            break
        _ingest_page(acc, res, sig, html, org["url"], org, locale, "snapshot")
        res.pages.append({"url": org["url"], "kind": "snapshot", "n": i + 1})
    sig.pages_walked = len(snapshots)
    sig.soft_404 = bool(snapshots) and is_soft_404(snapshots[0], 200)
    if sig.soft_404:
        res.candidates, sig.candidates_current = [], 0
        res.completeness = assess(sig)
        return res
    res.candidates = list(acc.by_key.values())
    sig.candidates_current = len(res.candidates)
    sig.candidates_all = len(res.candidates) + len(acc.archive_keys)
    res.completeness = assess(sig)
    return res
