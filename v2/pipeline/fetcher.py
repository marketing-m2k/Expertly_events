"""Fetching: plain HTTP with honest error reporting, plus page-health detection.

Every fetch returns a Page whose `error` says what went wrong (never an
exception, never a silent empty string). The pipeline decides what a failure
means; a fetch failure is never an event rejection.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import requests

from pipeline.common import host_of, normalize_ws

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

BLOCK_MARKERS = ("403 forbidden", "access denied", "unexpected error", "are you a human", "attention required",
                 "just a moment", "enable javascript and cookies", "request blocked", "bot detection", "captcha")
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1>")
_TAGS = re.compile(r"<[^>]+>")
_SOFT_404 = re.compile(
    r"(page|route|url|address)\s+(you('|’)?re (trying to reach|looking for)\s+)?(was\s+)?(not found|doesn('|’)?t exist|does not exist|could not be found|can('|’)?t be found|cannot be found)"
    r"|\b404\b.{0,40}(not found|error|doesn('|’)?t exist)|(not found|error)\s*[-–:]?\s*404|no longer (available|exists)|"
    r"this page (is|has been) (unavailable|removed|moved)|the page you (requested|are looking for)", re.I)


@dataclass
class Page:
    url: str
    final_url: str = ""
    html: str | None = None
    status: int | None = None
    error: str | None = None
    method: str = "http"          # http | browser | replay

    @property
    def ok(self) -> bool:
        return self.html is not None and not self.error


class Fetcher(Protocol):
    def get(self, url: str) -> Page: ...


def visible_text(html: str) -> str:
    return normalize_ws(_TAGS.sub(" ", _SCRIPT_STYLE.sub(" ", html or "")))


def looks_blocked(html: str | None) -> bool:
    if not html or len(html) > 4000:
        return False
    low = html.lower()
    return any(m in low for m in BLOCK_MARKERS)


def looks_js_shell(html: str | None) -> bool:
    """A page that says (almost) nothing until JavaScript runs."""
    if not html:
        return True
    n = len(visible_text(html))
    if n < 400:
        return True
    return n < 1500 and bool(re.search(r"enable javascript|requires javascript|javascript is (required|disabled)", html, re.I))


def has_app_root(html: str) -> bool:
    return bool(re.search(r'<div[^>]+id=["\'](?:root|app|__next|__nuxt|app-root)["\']', html or "", re.I))


def is_soft_404(html: str | None, status: int | None = None) -> bool:
    """HTTP 200 pages that are really 'not found' views (very common on SPA sites)."""
    if not html:
        return False
    if status in (404, 410):
        return True
    text = visible_text(html)
    head = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = normalize_ws(head.group(1)) if head else ""
    if _SOFT_404.search(title):
        return True
    h1s = re.findall(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    if any(_SOFT_404.search(normalize_ws(_TAGS.sub(" ", h))) for h in h1s):
        return True
    # a short page dominated by a not-found message
    m = _SOFT_404.search(text)
    if m and (len(text) < 1500 or text.find(m.group(0)) < 600):
        return True
    return False


def _decode(response) -> str:
    enc = (response.encoding or "").lower()
    if not enc or enc == "iso-8859-1":
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            return response.content.decode(response.apparent_encoding or "utf-8", errors="replace")
    return response.text


class HttpFetcher:
    """requests-based fetcher. One request at a time per host with a short pause."""

    def __init__(self, timeout=(8, 20), delay_s: float = 0.25, retries: int = 1):
        self.timeout, self.delay_s, self.retries = timeout, delay_s, retries
        self._locks: dict[str, threading.Lock] = {}
        self._glock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _lock(self, host: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(host, threading.Lock())

    def get(self, url: str) -> Page:
        host = host_of(url)
        last = Page(url=url, error="not attempted")
        for attempt in range(self.retries + 1):
            with self._lock(host):
                try:
                    r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                except requests.RequestException as exc:
                    last = Page(url=url, error=f"{type(exc).__name__}: {str(exc)[:120]}")
                else:
                    html = _decode(r)
                    if r.status_code >= 400:
                        last = Page(url=url, final_url=r.url, status=r.status_code, error=f"HTTP {r.status_code}",
                                    html=html if len(html) > 0 else None)
                        if r.status_code in (404, 410):
                            return last
                    elif looks_blocked(html):
                        last = Page(url=url, final_url=r.url, status=r.status_code, error="blocked by the site", html=html)
                    else:
                        return Page(url=url, final_url=r.url, html=html, status=r.status_code)
                time.sleep(self.delay_s)
            if attempt < self.retries:
                time.sleep(1.0 + attempt)
        return last


class ReplayFetcher:
    """Serves pages from a dict -- used by tests and by fixture-mode measurements."""

    def __init__(self, pages: dict[str, str | Page], default_404: bool = True):
        self.pages = pages
        self.calls: list[str] = []
        self.default_404 = default_404

    def get(self, url: str) -> Page:
        self.calls.append(url)
        v = self.pages.get(url)
        if v is None:
            v = self.pages.get(url.rstrip("/")) or self.pages.get(url + "/")
        if v is None:
            return Page(url=url, status=404, error="HTTP 404") if self.default_404 else Page(url=url, error="not in replay")
        if isinstance(v, Page):
            return v
        return Page(url=url, final_url=url, html=v, status=200, method="replay")


def site_root(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"
