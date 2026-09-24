"""Fast page fetching: plain HTTP first, a real browser only when needed.

Most event sites send finished HTML, so a plain request (a fraction of a
second) gets the same page a browser would. The browser (slow, heavy) is
kept for what really needs it:
  - the page is an empty shell that only fills in with JavaScript
  - the site blocks plain requests (403 / bot wall) but lets a browser in
  - a list page that pages or loads more ("load more", "next", ?page=2), so
    every event is reached -- completeness matters more than speed
  - a page that doesn't look like an event list at all (the browser can find
    the site's real Events link)

Politeness is per website: one request at a time to any single site, a short
pause between them, while many different sites are read in parallel.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

from scraper.fetch import USER_AGENT, _looks_blocked, _looks_like_event_listing, fetch_detail_pages, fetch_html

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
_BROWSER_SLOTS = threading.BoundedSemaphore(5)  # browsers are heavy: never more than 5 at once
_FINAL_STATUSES = (404, 410)

_MORE_PAGES = re.compile(
    r"rel=[\"']next[\"']|load\s+more|show\s+more|view\s+more|more\s+events|older\s+events|next\s+page|"
    r"[?&]e?page=\d|/page/\d|class=[\"'][^\"']*pagination|aria-label=[\"']pagination|infinite[-_ ]scroll", re.I)
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")
_TAGS = re.compile(r"<[^>]+>")


def visible_length(html: str) -> int:
    return len(re.sub(r"\s+", " ", _TAGS.sub(" ", _SCRIPT_STYLE.sub(" ", html or ""))).strip())


def looks_js_shell(html: str) -> bool:
    """A page that says almost nothing until JavaScript runs."""
    return visible_length(html) < 400 or bool(re.search(r"enable javascript|requires javascript", html or "", re.I)
                                              and visible_length(html) < 1500)


def has_more_pages_hint(html: str) -> bool:
    return bool(_MORE_PAGES.search(html or ""))


def _decode(response) -> str:
    enc = (response.encoding or "").lower()
    if not enc or enc == "iso-8859-1":  # servers often omit the charset; utf-8 is the safe guess
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            return response.content.decode(response.apparent_encoding or "utf-8", errors="replace")
    return response.text


def http_get(url: str, timeout=(8, 15)) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return {"html": None, "status": None, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if r.status_code >= 400:
        return {"html": None, "status": r.status_code, "error": f"HTTP {r.status_code}"}
    html = _decode(r)
    if _looks_blocked(html):
        return {"html": None, "status": r.status_code, "error": "blocked by the site"}
    return {"html": html, "status": r.status_code, "error": None}


def http_get_many(urls: list[str], workers: int = 20, delay_s: float = 0.3, get=http_get) -> dict[str, dict]:
    """One request at a time per website, many websites in parallel."""
    by_host: dict[str, list[str]] = {}
    for url in dict.fromkeys(urls):
        by_host.setdefault(urlsplit(url).netloc.lower(), []).append(url)

    def work(host_urls):
        out = {}
        for url in host_urls:
            out[url] = get(url)
            time.sleep(delay_s)
        return out

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for part in executor.map(work, by_host.values()):
            results.update(part)
    return results


def fetch_detail_fast(urls: list[str], wait_ms: int = 1500, workers: int = 20, delay_s: float = 0.3,
                      http_many=http_get_many, browser_many=fetch_detail_pages) -> dict[str, dict]:
    """Same result shape as fetch_detail_pages. A retry (wait_ms >= 3000)
    goes straight to the browser, since plain HTTP already failed once."""
    urls = list(dict.fromkeys(urls))
    if wait_ms >= 3000:
        return browser_many(urls, wait_ms=wait_ms, workers=min(workers, 5), delay_s=delay_s)
    results = http_many(urls, workers=workers, delay_s=delay_s)
    retry = [u for u, r in results.items()
             if r["status"] not in _FINAL_STATUSES and (r["html"] is None or looks_js_shell(r["html"]))]
    if retry:
        results.update(browser_many(retry, wait_ms=wait_ms, workers=5, delay_s=delay_s))
    return results


def fetch_list_pages(url: str, http=http_get, browser=fetch_html, has_events=None) -> list[str]:
    """HTML of a list page, complete. Plain HTTP when the page is a single,
    finished list of events; the browser when it pages, loads more, needs
    JavaScript, or isn't an event list."""
    page = http(url)
    html = page["html"]
    if html and not looks_js_shell(html) and _looks_like_event_listing(html) and not has_more_pages_hint(html):
        if has_events is None or has_events(html):
            return [html]
    with _BROWSER_SLOTS:
        return browser(url)
