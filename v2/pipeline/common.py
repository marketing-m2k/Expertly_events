"""Small shared helpers: text normalisation, URL identity, similarity, hashing.

Nothing here knows about events; every pipeline stage imports from here so
that "same URL" and "same title" mean exactly one thing everywhere.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

_WS = re.compile(r"\s+")
_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|_ga|ref|source|campaign|cmp|trk|igshid)$", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ws(text: str | None) -> str:
    return _WS.sub(" ", html_lib.unescape(text or "").replace("\xa0", " ")).strip()


def norm_text(text: str | None) -> str:
    """Lower-case, punctuation-free, single-spaced: for comparisons only."""
    return _NON_ALNUM.sub(" ", normalize_ws(text).lower()).strip()


def sha(text: str, n: int = 12) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:n]


def slugify(text: str) -> str:
    return _NON_ALNUM.sub("-", (text or "").lower()).strip("-")[:60] or "x"


# ---- URLs -------------------------------------------------------------------

def is_http_url(url: str | None) -> bool:
    return bool(url) and urlsplit(url).scheme in ("http", "https") and bool(urlsplit(url).netloc)


def clean_href(href: str | None) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "sms:")):
        return ""
    return href


def absolute_url(base: str, href: str | None) -> str:
    href = clean_href(href)
    return urljoin(base, href) if href else ""


def norm_url(url: str | None) -> str:
    """Identity form of a URL: no scheme/www/fragment/trailing slash/tracking
    params, query params sorted. Two links to the same page compare equal."""
    if not url:
        return ""
    p = urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                             if not _TRACKING_PARAMS.match(k)))
    path = re.sub(r"/index\.(html?|php|aspx?)$", "", p.path.rstrip("/"), flags=re.I)
    return f"{host}{path}" + (f"?{query}" if query else "")


def same_page(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and norm_url(a) == norm_url(b)


def host_of(url: str | None) -> str:
    return urlsplit(url or "").netloc.lower().removeprefix("www.")


def registered_domain(url_or_host: str | None) -> str:
    host = host_of(url_or_host) if "/" in (url_or_host or "") else (url_or_host or "").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(a: str | None, b: str | None) -> bool:
    ra, rb = registered_domain(a), registered_domain(b)
    return bool(ra) and ra == rb


# ---- similarity ---------------------------------------------------------------

_STOP = {"the", "a", "an", "of", "and", "for", "in", "on", "to", "at", "by", "with", "2025", "2026", "2027"}


def tokens(text: str | None) -> set[str]:
    return {t for t in norm_text(text).split() if t not in _STOP}


def similarity(a: str | None, b: str | None) -> float:
    """0..1 token overlap (Jaccard over the smaller side's coverage, so a title
    and the same title with a site suffix still score high)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb)) * 0.5 + inter / len(ta | tb) * 0.5


def contains_loosely(haystack: str | None, needle: str | None) -> bool:
    n = norm_text(needle)
    return bool(n) and n in norm_text(haystack)
