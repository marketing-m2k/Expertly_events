"""Browser rendering and listing expansion (Playwright, headless Chromium).

Used when plain HTTP does not give the whole list: JavaScript-rendered pages,
'Load more' buttons, infinite scroll, and pagers that are buttons rather than
links. Every mechanism reports an explicit END STATE -- complete, capped, stuck
or error -- so a timeout can never masquerade as "no more events":

    complete  the control disappeared / was disabled / nothing new loaded for several rounds
    capped    a safety limit (rounds, pages, deadline) was hit while still progressing -> discovery_incomplete
    stuck     the control was clicked but the page did not change -> a pagination/load-more FAILURE
    error     the browser itself failed

Safety: a 'next' control is only ever picked inside a pagination container /
rel=next / aria-label; carousel controls and controls under 'Past events' are
skipped; consent banners are dismissed with the most privacy-preserving button
(reject / necessary only / close), never 'accept all'.
"""

from __future__ import annotations

import os

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from pipeline.fetcher import USER_AGENT, Page, looks_blocked

_BROWSER_SLOTS = threading.BoundedSemaphore(4)

_JS_SIGNATURE = """() => {
  const hrefs = Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
  const text = document.body ? document.body.innerText : '';
  return {anchors: hrefs.length, hrefs: Array.from(new Set(hrefs)), text_len: text.length,
          height: document.body ? document.body.scrollHeight : 0, head: text.slice(0, 400)};
}"""

_JS_FIND_LOAD_MORE = r"""() => {
  const RX = /^(?:load|show|view|see|get)\s+(?:more|additional)(?:\s+(?:events?|results?|items?|posts?|articles?|webinars?|courses?))?\W*$|^more\s+(?:events?|results?|webinars?)\W*$|^load\s+additional\s+events\W*$/i;
  const CL = /load[-_]?more|show[-_]?more|more[-_]?btn|btn[-_]?more|view[-_]?more|loadmore/i;
  const ARCH = /\b(past|previous|archive[sd]?|completed|ended|older)\b/i, UP = /\b(upcoming|forthcoming|future|next|current)\b/i;
  document.querySelectorAll('[data-lm-mark]').forEach(e => e.removeAttribute('data-lm-mark'));
  const archived = (el) => {
    for (let a = el, n = 0; a && n < 8; a = a.parentElement, n++) {
      if (/(^|[\s_-])(past|archive|previous|completed)[-_ ]?(events?|list|items?|tab|pane|panel|section)/i.test((a.className||'').toString() + ' ' + (a.id||''))) return true;
      let s = a.previousElementSibling, k = 0;
      while (s && k < 6) { if (/^H[1-6]$/.test(s.tagName)) { const t = s.innerText || ''; return ARCH.test(t) && !UP.test(t); } s = s.previousElementSibling; k++; }
    }
    return false;
  };
  for (const el of document.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')) {
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    const cls = (el.className && el.className.toString()) || '';
    if (!(RX.test(label) || CL.test(cls + ' ' + (el.id || '')))) continue;
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || (r.width === 0 && r.height === 0)) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true' || /\bdisabled\b/.test(cls)) continue;
    if (el.closest('[class*=carousel],[class*=slick],[class*=swiper],[class*=owl-],[class*=splide]')) continue;
    if (archived(el)) continue;
    el.setAttribute('data-lm-mark', '1');
    return {label: label.slice(0, 40), tag: el.tagName};
  }
  return null;
}"""

_JS_FIND_NEXT = r"""(want_number) => {
  const NEXT = /^(?:next(?:\s+(?:page|events?|results?))?|older(?:\s+events?)?|more\s+events|[»›→>]+|next\s*[»›→>]+)\W*$/i;
  const CONT = '[class*=pagin],[class*=pager],[id*=pagin],[id*=pager],nav[aria-label*=pag i],[role=navigation][aria-label*=pag i],.nav-links,.page-numbers,.wp-pagenavi,[class*=page-nav]';
  const ARCH = /\b(past|previous|archive[sd]?|completed|ended|older)\b/i, UP = /\b(upcoming|forthcoming|future|next|current)\b/i;
  document.querySelectorAll('[data-pg-mark]').forEach(e => e.removeAttribute('data-pg-mark'));
  const carousel = el => !!el.closest('[class*=carousel],[class*=slick],[class*=swiper],[class*=owl-],[class*=splide],[class*=slider]');
  const archived = (el) => {
    for (let a = el, n = 0; a && n < 8; a = a.parentElement, n++) {
      if (/(^|[\s_-])(past|archive|previous|completed)[-_ ]?(events?|list|items?|tab|pane|panel|section)/i.test((a.className||'').toString() + ' ' + (a.id||''))) return true;
      let s = a.previousElementSibling, k = 0;
      while (s && k < 6) { if (/^H[1-6]$/.test(s.tagName)) { const t = s.innerText || ''; return ARCH.test(t) && !UP.test(t); } s = s.previousElementSibling; k++; }
    }
    return false;
  };
  const visible = el => { const r = el.getBoundingClientRect(), st = getComputedStyle(el); return !(st.display === 'none' || st.visibility === 'hidden' || (r.width === 0 && r.height === 0)); };
  const disabled = el => el.disabled || el.getAttribute('aria-disabled') === 'true' || /\bdisabled\b/.test(((el.className||'').toString()) + ' ' + (el.parentElement ? (el.parentElement.className||'').toString() : ''));
  for (const c of document.querySelectorAll(CONT)) {
    if (carousel(c) || archived(c)) continue;
    const els = Array.from(c.querySelectorAll('a, button, [role=button], li'));
    // numbered target first when asked
    if (want_number) {
      for (const el of els) { if ((el.innerText || '').trim() === String(want_number) && visible(el) && !disabled(el)) { const t = el.tagName === 'LI' ? (el.querySelector('a,button') || el) : el; t.setAttribute('data-pg-mark', '1'); return {label: String(want_number), kind: 'number'}; } }
      continue;
    }
    for (const el of els) {
      const label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
      const cls = ((el.className || '').toString()).toLowerCase();
      const rel = (el.getAttribute('rel') || '').toLowerCase();
      const aria = (el.getAttribute('aria-label') || '').toLowerCase();
      if (!(NEXT.test(label) || rel.includes('next') || /\bnext\b/.test(aria) || /(^|[\s_-])next($|[\s_-])/.test(cls))) continue;
      const t = el.tagName === 'LI' ? (el.querySelector('a,button') || el) : el;
      if (t.tagName === 'LI' && !t.onclick) continue;
      if (!visible(t) || disabled(t) || disabled(el) || carousel(el)) continue;
      t.setAttribute('data-pg-mark', '1');
      return {label: label.slice(0, 30), kind: 'next'};
    }
  }
  return null;
}"""

_JS_CURRENT_PAGE = r"""() => {
  const CONT = '[class*=pagin],[class*=pager],[id*=pagin],nav[aria-label*=pag i],.nav-links,.page-numbers,.wp-pagenavi';
  for (const c of document.querySelectorAll(CONT)) {
    const cur = c.querySelector('[aria-current], .current, .active, .selected');
    if (cur) { const m = (cur.innerText || '').match(/\d+/); if (m) return parseInt(m[0]); }
  }
  return null;
}"""

_JS_DISMISS_COOKIES = r"""() => {
  const CONT = /cookie|consent|gdpr|cmp|onetrust|cookiebot|cc-window|privacy-banner/i;
  const BTN = /^(reject all|decline all|decline|reject|only necessary|necessary only|use necessary cookies only|use necessary only|no thanks|close|dismiss|got it|x|×)$/i;
  for (const box of document.querySelectorAll('div,section,aside,dialog')) {
    const id = (box.id || '') + ' ' + ((box.className || '').toString());
    if (!CONT.test(id)) continue;
    for (const b of box.querySelectorAll('button, a, [role=button]')) {
      const t = (b.innerText || b.getAttribute('aria-label') || '').trim();
      if (BTN.test(t)) { try { b.click(); return t; } catch (e) {} }
    }
  }
  return null;
}"""


@dataclass
class ExpandReport:
    snapshots: list[str] = field(default_factory=list)       # rendered HTML: initial (expanded) page, then each further pager page
    snapshot_urls: list[str] = field(default_factory=list)
    load_more: dict = field(default_factory=lambda: {"state": "none", "clicks": 0})
    scroll: dict = field(default_factory=lambda: {"state": "none", "rounds": 0})
    pager: dict = field(default_factory=lambda: {"state": "none", "pages": 1})
    notes: list[str] = field(default_factory=list)
    failure_kinds: list[str] = field(default_factory=list)
    error: str | None = None
    status: int | None = None
    final_url: str = ""
    cookie_banner: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("snapshots",)} | {"snapshot_count": len(self.snapshots)}


class BrowserSession:
    """One headless Chromium for one organisation. Use as a context manager; thread-confined."""

    def __init__(self, wait_ms: int = 2500, block_media: bool = True):
        self.wait_ms, self.block_media = wait_ms, block_media
        self._pw = self._browser = self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        _BROWSER_SLOTS.acquire()
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or None)
            ctx = self._browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900}, ignore_https_errors=True,
                                            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
            self.page = ctx.new_page()
            if self.block_media:
                self.page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._browser = self._pw = self.page = None
            try:
                _BROWSER_SLOTS.release()
            except ValueError:
                pass
        return False

    # ---- primitives --------------------------------------------------------------------------
    def goto(self, url: str) -> Page:
        status = None
        try:
            try:
                resp = self.page.goto(url, wait_until="load", timeout=30000)
            except Exception:
                resp = self.page.goto(url, wait_until="commit", timeout=30000)
            status = resp.status if resp else None
            self._settle()
            html = self.page.content()
        except Exception as exc:  # noqa: BLE001
            return Page(url=url, error=f"{type(exc).__name__}: {str(exc)[:140]}", method="browser")
        err = None
        if looks_blocked(html):
            err = "blocked by the site"
        elif status and status >= 400:
            err = f"HTTP {status}"
        return Page(url=url, final_url=self.page.url, html=html, status=status, error=err, method="browser")

    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        self.page.wait_for_timeout(self.wait_ms)

    def _sig(self) -> dict:
        try:
            return self.page.evaluate(_JS_SIGNATURE)
        except Exception:
            return {"anchors": 0, "hrefs": [], "text_len": 0, "height": 0, "head": ""}

    @staticmethod
    def _grew(before: dict, after: dict) -> bool:
        return (after["anchors"] > before["anchors"] or after["text_len"] > before["text_len"] + 20
                or len(set(after["hrefs"]) - set(before["hrefs"])) > 0)

    @staticmethod
    def _fingerprint(sig: dict):
        return (len(sig["hrefs"]), hash(frozenset(sig["hrefs"])))

    @staticmethod
    def _changed(before: dict, after: dict) -> bool:
        return (set(after["hrefs"]) != set(before["hrefs"])) or after["head"] != before["head"]

    def dismiss_cookies(self) -> str:
        try:
            r = self.page.evaluate(_JS_DISMISS_COOKIES)
            if r:
                self.page.wait_for_timeout(600)
            return r or ""
        except Exception:
            return ""

    # ---- mechanisms ------------------------------------------------------------------------------
    def expand_load_more(self, rep: ExpandReport, deadline: float, max_clicks: int = 60) -> None:
        clicks, stuck_rounds = 0, 0
        while time.monotonic() < deadline:
            try:
                found = self.page.evaluate(_JS_FIND_LOAD_MORE)
            except Exception as exc:  # noqa: BLE001
                rep.load_more.update(state="error")
                rep.notes.append(f"load-more probe failed: {exc!s:.80}")
                rep.failure_kinds.append("load_more")
                return
            if not found:
                if clicks:
                    rep.load_more.update(state="complete", clicks=clicks)
                return
            if clicks >= max_clicks:
                rep.load_more.update(state="capped", clicks=clicks)
                rep.failure_kinds.append("load_more")
                rep.notes.append(f"load more still present after {clicks} clicks (safety cap)")
                return
            before = self._sig()
            try:
                self.page.locator('[data-lm-mark="1"]').first.scroll_into_view_if_needed(timeout=2000)
                self.page.locator('[data-lm-mark="1"]').first.click(timeout=3000)
            except Exception as exc:  # noqa: BLE001
                rep.load_more.update(state="stuck", clicks=clicks)
                rep.failure_kinds.append("load_more")
                rep.notes.append(f"load more could not be clicked: {exc!s:.80}")
                return
            clicks += 1
            grew = False
            for _ in range(8):                       # up to ~4s for the new items to arrive
                self.page.wait_for_timeout(500)
                if self._grew(before, self._sig()):
                    grew = True
                    break
            if grew:
                stuck_rounds = 0
            else:
                stuck_rounds += 1
                if stuck_rounds >= 2:
                    rep.load_more.update(state="stuck", clicks=clicks)
                    rep.failure_kinds.append("load_more")
                    rep.notes.append("load more clicked but nothing new appeared")
                    return
        rep.load_more.update(state="capped", clicks=clicks)
        rep.failure_kinds.append("load_more")
        rep.notes.append("deadline reached while loading more")

    def expand_scroll(self, rep: ExpandReport, deadline: float, max_rounds: int = 40, probe_only: bool = False) -> None:
        still, rounds, grown_ever = 0, 0, False
        while rounds < max_rounds and time.monotonic() < deadline:
            before = self._sig()
            try:
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight); window.dispatchEvent(new Event('scroll'))")
                self.page.mouse.wheel(0, 3000)
            except Exception:
                break
            self.page.wait_for_timeout(900)
            after = self._sig()
            rounds += 1
            if self._grew(before, after) or after["height"] > before["height"]:
                grown_ever, still = True, 0
            else:
                still += 1
                if still >= (2 if not grown_ever else 3):
                    rep.scroll.update(state="complete" if grown_ever else "none", rounds=rounds)
                    return
            if probe_only and not grown_ever and rounds >= 2:
                rep.scroll.update(state="none", rounds=rounds)
                return
        rep.scroll.update(state="capped", rounds=rounds)
        rep.failure_kinds.append("infinite_scroll")
        rep.notes.append(f"scrolling still produced new content after {rounds} rounds (safety cap)")

    def expand_pager(self, rep: ExpandReport, deadline: float, max_pages: int = 40,
                     page_is_archive: Callable[[str], bool] | None = None) -> None:
        pages, no_new = 1, 0
        seen_heads = {self._fingerprint(self._sig())}
        while pages < max_pages and time.monotonic() < deadline:
            try:
                nxt = self.page.evaluate(_JS_FIND_NEXT, 0)
            except Exception as exc:  # noqa: BLE001
                rep.pager.update(state="error", pages=pages)
                rep.failure_kinds.append("pagination")
                rep.notes.append(f"pager probe failed: {exc!s:.80}")
                return
            tried_number = False
            before = self._sig()
            moved = False
            if nxt:
                try:
                    loc = self.page.locator('[data-pg-mark="1"]').first
                    if not (loc.is_visible() and loc.is_enabled()):
                        rep.notes.append("pager control present but not actionable (disabled): treated as the last page")
                        nxt = None
                    else:
                        loc.scroll_into_view_if_needed(timeout=2000)
                        loc.click(timeout=3000)
                        moved = self._wait_changed(before)
                except Exception as exc:  # noqa: BLE001
                    rep.notes.append(f"next click failed: {exc!s:.80}")
            if not moved:
                cur = None
                try:
                    cur = self.page.evaluate(_JS_CURRENT_PAGE)
                except Exception:
                    pass
                target = (cur or pages) + 1
                try:
                    num = self.page.evaluate(_JS_FIND_NEXT, target)
                except Exception:
                    num = None
                if num:
                    tried_number = True
                    try:
                        self.page.locator('[data-pg-mark="1"]').first.click(timeout=3000)
                        moved = self._wait_changed(before)
                    except Exception as exc:  # noqa: BLE001
                        rep.notes.append(f"page {target} click failed: {exc!s:.80}")
            if not moved:
                if nxt or tried_number:
                    rep.pager.update(state="stuck", pages=pages)
                    rep.failure_kinds.append("pagination")
                    rep.notes.append("pager control clicked but the page did not change")
                else:
                    rep.pager.update(state="complete" if pages > 1 else "none", pages=pages)
                return
            self.expand_load_more(rep, deadline)
            html = self.page.content()
            head = self._fingerprint(self._sig())
            if head in seen_heads:
                rep.pager.update(state="stuck", pages=pages)
                rep.failure_kinds.append("pagination")
                rep.notes.append("pager returned a page already seen (loop)")
                return
            seen_heads.add(head)
            pages += 1
            rep.snapshots.append(html)
            rep.snapshot_urls.append(self.page.url)
            if page_is_archive and page_is_archive(html):
                rep.pager.update(state="complete", pages=pages)
                rep.notes.append("pager reached past-events pages: stopped (archive)")
                return
        rep.pager.update(state="capped", pages=pages)
        rep.failure_kinds.append("pagination")
        rep.notes.append(f"stopped at {pages} pages (safety cap or deadline) while more pages remained")

    def _wait_changed(self, before: dict) -> bool:
        for _ in range(14):
            self.page.wait_for_timeout(500)
            if self._changed(before, self._sig()):
                self.page.wait_for_timeout(500)
                return True
        return False

    # ---- the whole thing ---------------------------------------------------------------------------
    def expand_listing(self, url: str, want_load_more: bool = True, want_scroll: bool = True, want_pager: bool = True,
                       deadline_s: float = 150, page_is_archive: Callable[[str], bool] | None = None,
                       max_pages: int = 40) -> ExpandReport:
        rep = ExpandReport()
        deadline = time.monotonic() + deadline_s
        page = self.goto(url)
        rep.error, rep.status, rep.final_url = page.error, page.status, page.final_url
        if page.html is None:
            rep.failure_kinds.append("fetch")
            return rep
        rep.cookie_banner = self.dismiss_cookies()
        if want_load_more:
            self.expand_load_more(rep, deadline)
        if want_scroll:
            self.expand_scroll(rep, deadline, probe_only=True)   # keeps scrolling only while the page keeps growing
        rep.snapshots.insert(0, self.page.content())
        rep.snapshot_urls.insert(0, self.page.url)
        if want_pager:
            self.expand_pager(rep, deadline, max_pages=max_pages, page_is_archive=page_is_archive)
        return rep

    def open_page(self, url: str) -> Page:
        p = self.goto(url)
        if p.html:
            self.dismiss_cookies()
            try:
                p.html = self.page.content()
            except Exception:
                pass
        return p
