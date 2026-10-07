"""Render a page with a headless browser, expanding 'load more', infinite
scroll, and pagination so events aren't missed past the first screen."""

import re
import time

from playwright.sync_api import sync_playwright

LOAD_MORE_TEXTS = ["load more", "show more", "view more", "more events", "load additional events"]
NEXT_PAGE_TEXTS = ["next", "next page", "older events", "»"]  # » = »

PAST_EVENTS_LINK_TEXT = re.compile(
    r"past\s+events?|previous\s+events?|event\s+archive|events?\s+archive|"
    r"past\s+webinars?|completed\s+events?|archived\s+events?|history\s+of\s+events?",
    re.I,
)

# A real browser UA — headless Chromium's default UA (or its lack of one)
# gets flagged by WAFs (Cloudflare, Sucuri, etc.) on a lot of these sites,
# so requests come back as a 403 page instead of the real content.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = ("403 forbidden", "access denied", "unexpected error", "are you a human",
                  "attention required", "just a moment")


def _looks_blocked(html: str) -> bool:
    if len(html) > 3000:
        return False  # real pages are rarely this short; long ones are never a block page
    lowered = html.lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def _click_first_match(page, text: str, timeout: int) -> bool:
    """Click the first element matching `text` if one already exists.
    Checking .count() first avoids Playwright's default behavior of waiting
    out the full timeout hoping a matching element appears dynamically —
    on the (common) case where no such element exists at all, this returns
    in milliseconds instead of eating the full timeout every time."""
    locator = page.get_by_text(text, exact=False).first
    try:
        if locator.count() == 0:
            return False
        locator.click(timeout=timeout)
        return True
    except Exception:
        return False


def _scroll_to_bottom(page, rounds: int = 4, wait_ms: int = 500) -> None:
    """Trigger infinite-scroll/lazy-loaded content by repeatedly scrolling
    down and waiting for the page height to stop growing."""
    last_height = None
    for _ in range(rounds):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(wait_ms)
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height


_OLD_YEAR = re.compile(r"\b(201\d|202[0-5])\b")
_CURRENT_YEAR = re.compile(r"\b2026\b")


def _archive_page_looks_too_old(html: str) -> bool:
    """True if a past-events page shows only pre-2026 years and no 2026 —
    used to stop paginating deeper into an archive once it's past our
    Jan-2026-onward window (archives are typically newest-first)."""
    return bool(_OLD_YEAR.search(html)) and not _CURRENT_YEAR.search(html)


def _advance_page(page, current_html: str, page_num_hint: int) -> str | None:
    """Try each pagination strategy in turn; return the new page HTML only if
    it actually changed. A click can report success in Playwright while the
    page silently doesn't navigate — common on postback-based pagers (e.g.
    ASP.NET .aspx sites, seen on several .gov.in / institutional sites here)
    where a stale/re-rendered control eats the click. Without this check,
    the loop would keep "succeeding" while capturing the same page N times,
    which looks like progress but adds zero new content."""
    try:
        next_link = page.query_selector("a[rel='next']")
        if next_link:
            next_link.click(timeout=3000)
            page.wait_for_timeout(2000)
            _scroll_to_bottom(page)
            after = page.content()
            if after != current_html:
                return after
    except Exception:
        pass

    for text in NEXT_PAGE_TEXTS:
        if _click_first_match(page, text, timeout=1500):
            page.wait_for_timeout(2000)
            _scroll_to_bottom(page)
            after = page.content()
            if after != current_html:
                return after
            break  # matched but didn't move the page — don't keep re-clicking it

    # last resort: numbered pagination links (2, 3, 4, ...) — role+exact
    # match, not loose text, so a bare "2" doesn't match unrelated content
    locator = page.get_by_role("link", name=str(page_num_hint), exact=True).first
    try:
        if locator.count() > 0:
            locator.scroll_into_view_if_needed(timeout=1500)
            locator.click(timeout=1500)
            page.wait_for_timeout(2000)
            _scroll_to_bottom(page)
            after = page.content()
            if after != current_html:
                return after
    except Exception:
        pass

    return None


EVENTS_LINK_TEXT = re.compile(
    r"^(upcoming\s+)?events?$|^events?\s*&\s*programs?$|^programs?\s*&\s*events?$|"
    r"^calendar(\s+of\s+events)?$|^events?\s+calendar$",
    re.I,
)

_LISTING_DATE_PATTERN = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
    r"\b\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}\b",
    re.I,
)


def _looks_like_event_listing(html: str) -> bool:
    """Cheap heuristic: does this page already contain several date-like
    strings? A real events/calendar page almost always does; an org's plain
    homepage usually doesn't. Used to decide whether it's worth looking for
    a dedicated Events nav link instead of scraping the homepage as-is."""
    return len(_LISTING_DATE_PATTERN.findall(html[:20000])) >= 3


def _find_events_page_url(page, base_url: str) -> str | None:
    """When the loaded page doesn't look like an event listing itself (e.g.
    the master list's URL is just the org's homepage), look for a nav link
    to a dedicated Events/Calendar page and return it so that gets scraped
    instead of the homepage."""
    try:
        for a in page.query_selector_all("a[href]"):
            text = (a.inner_text() or "").strip()
            if text and len(text) <= 40 and EVENTS_LINK_TEXT.match(text):
                href = a.get_attribute("href")
                if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    from urllib.parse import urljoin
                    full = urljoin(base_url, href)
                    if full.rstrip("/") != base_url.rstrip("/"):
                        return full
    except Exception:
        pass
    return None


def _resolve_frameset(page) -> bool:
    """Old-style <frameset> pages (still used by a handful of legacy .aspx
    sites here) have no real content in <body> — it lives in one of the
    <frame src> URLs. Navigate straight to the most likely content frame
    (skip small utility frames like hidden-variable/nav/banner frames).
    Returns True if it navigated somewhere new."""
    try:
        frames = page.query_selector_all("frame")
        if not frames:
            return False
        candidates = [
            f.get_attribute("src") for f in frames
            if f.get_attribute("src")
            and not re.search(r"hidden|blank|nav|menu|header|footer|banner", f.get_attribute("src"), re.I)
        ]
        if not candidates:
            return False
        target = max(candidates, key=len)  # longest src is the best guess at the main content frame
        from urllib.parse import urljoin
        page.goto(urljoin(page.url, target), wait_until="load", timeout=20000)
        return True
    except Exception:
        return False


def _find_past_events_url(page, base_url: str) -> str | None:
    """Look for a 'past events' / 'event archive' link on the page so sites
    that only list upcoming events on their main page still get their
    history scraped. Returns an absolute URL, or None if no such link exists."""
    try:
        for a in page.query_selector_all("a[href]"):
            text = (a.inner_text() or "").strip()
            if text and PAST_EVENTS_LINK_TEXT.search(text):
                href = a.get_attribute("href")
                if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    from urllib.parse import urljoin
                    return urljoin(base_url, href)
    except Exception:
        pass
    return None


def fetch_detail_pages(urls: list[str], wait_ms: int = 1500, workers: int = 5,
                        delay_s: float = 0.7) -> dict[str, dict]:
    """Open each event's own page. Returns {url: {"html", "status", "error"}}.

    Polite by design: URLs are grouped by website and each website is worked
    through one page at a time with a short pause, so no site is ever hit by
    several requests at once; up to `workers` different websites run in
    parallel. A failure on one page never stops the others."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlsplit
    import time

    by_host: dict[str, list[str]] = {}
    for url in dict.fromkeys(urls):  # de-duplicated, order kept
        by_host.setdefault(urlsplit(url).netloc.lower(), []).append(url)

    def work(host_urls: list[str]) -> dict[str, dict]:
        out = {}
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(ignore_https_errors=True, user_agent=USER_AGENT,
                                     viewport={"width": 1366, "height": 900})
            page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
            for url in host_urls:
                try:
                    try:
                        response = page.goto(url, wait_until="load", timeout=20000)
                    except Exception:
                        response = page.goto(url, wait_until="commit", timeout=20000)
                    page.wait_for_timeout(wait_ms)
                    html = page.content()
                    status = response.status if response else None
                    if _looks_blocked(html):
                        out[url] = {"html": None, "status": status, "error": "blocked by the site"}
                    elif status is not None and status >= 400:
                        out[url] = {"html": None, "status": status, "error": f"HTTP {status}"}
                    else:
                        out[url] = {"html": html, "status": status, "error": None}
                except Exception as exc:  # noqa: BLE001 - one bad page must never stop the rest
                    out[url] = {"html": None, "status": None, "error": str(exc)[:200]}
                time.sleep(delay_s)
            browser.close()
        return out

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for partial in executor.map(work, by_host.values()):
            results.update(partial)
    return results


def fetch_html(url: str, wait_ms: int = 3000, max_pages: int = 12,
                follow_past_events_link: bool = True, deadline_s: float = 90) -> list[str]:
    """Return HTML snapshots: the initial page, then up to `max_pages` - 1
    more page loads reached via 'load more' clicks or pagination links.
    If `follow_past_events_link` and the page links to a separate past
    events/archive page, that page (with its own pagination) is fetched too
    so past events aren't missed just because they live on another URL.
    Best-effort — failures to expand/paginate are swallowed, not raised."""
    htmls = []
    past_events_url = None
    deadline = time.monotonic() + deadline_s  # a slow site returns what it has, it never stalls the run
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(ignore_https_errors=True, user_agent=USER_AGENT,
                                 viewport={"width": 1366, "height": 900})
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        try:
            page.goto(url, wait_until="load", timeout=20000)
        except Exception:
            page.goto(url, wait_until="commit", timeout=20000)
        page.wait_for_timeout(wait_ms)

        if _looks_blocked(page.content()):
            # one retry — some WAFs pass a second request after a JS challenge
            page.wait_for_timeout(3000)
            try:
                page.reload(wait_until="load", timeout=20000)
                page.wait_for_timeout(wait_ms)
            except Exception:
                pass

        if _resolve_frameset(page):
            page.wait_for_timeout(wait_ms)

        # the master list's URL is sometimes just the org's homepage, not an
        # actual events page — if this page doesn't look like a listing,
        # try to find and follow a dedicated Events/Calendar nav link first
        if not _looks_like_event_listing(page.content()):
            events_url = _find_events_page_url(page, page.url)
            if events_url:
                try:
                    page.goto(events_url, wait_until="load", timeout=20000)
                    page.wait_for_timeout(wait_ms)
                    if _resolve_frameset(page):
                        page.wait_for_timeout(wait_ms)
                except Exception:
                    pass

        _scroll_to_bottom(page)

        # expand "load more" repeatedly if present (bounded so we don't spin forever)
        for _ in range(8):
            if time.monotonic() > deadline:
                break
            clicked = False
            for text in LOAD_MORE_TEXTS:
                if _click_first_match(page, text, timeout=1500):
                    page.wait_for_timeout(1200)
                    _scroll_to_bottom(page, rounds=2)
                    clicked = True
                    break
            if not clicked:
                break

        htmls.append(page.content())

        if follow_past_events_link:
            past_events_url = _find_past_events_url(page, page.url)

        # follow numbered/next pagination for a few more pages
        while len(htmls) < max_pages and time.monotonic() < deadline:
            new_html = _advance_page(page, htmls[-1], len(htmls) + 1)
            if new_html is None:
                break
            htmls.append(new_html)

        if past_events_url and past_events_url.rstrip("/") != page.url.rstrip("/"):
            try:
                page.goto(past_events_url, wait_until="load", timeout=20000)
                page.wait_for_timeout(wait_ms)
                _scroll_to_bottom(page)
                for _ in range(8):
                    if time.monotonic() > deadline:
                        break
                    clicked = False
                    for text in LOAD_MORE_TEXTS:
                        if _click_first_match(page, text, timeout=1500):
                            page.wait_for_timeout(1200)
                            _scroll_to_bottom(page, rounds=2)
                            clicked = True
                            break
                    if not clicked:
                        break
                htmls.append(page.content())

                while len(htmls) < max_pages + 3 and time.monotonic() < deadline:
                    if _archive_page_looks_too_old(htmls[-1]):
                        # past-events archives are almost always newest-first;
                        # once a page has no 2026 dates left, older pages are
                        # outside our Jan-2026-onward window too — stop paying
                        # for more fetches/tokens on content we'd discard anyway
                        break
                    new_html = _advance_page(page, htmls[-1], len(htmls) + 1)
                    if new_html is None:
                        break
                    htmls.append(new_html)
            except Exception:
                pass

        browser.close()
    return htmls
