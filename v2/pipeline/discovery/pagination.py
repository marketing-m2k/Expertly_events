"""Pagination analysis of one listing page (HTTP level).

What v1 did: click the first element whose TEXT contained "next" -- which on real
sites is an event called "The Next Era of Wealth Management" or a group called
"Next Gen Tax Discussion Group" (both seen in the 2026-10-07 baseline), then
silently stop when a timeout hit.

What v2 does: a "next" control only counts when it is
  * rel="next" (link or anchor), or
  * inside a pagination container (class/id/role/aria-label says so) and says
    next / > / > / older / next events..., or
  * an anchor whose text is exactly such a symbol AND whose URL differs from
    this page's only by a page-like parameter (page, paged, offset, start,
    /page/N, cursor...).
Carousels (slick/swiper/owl/flickity) are never pagination. Numbered pages,
`?page=N`, offset and cursor URLs are all recognised. A pager that sits under a
'Past events' heading is flagged as an archive pager and ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from pipeline.common import absolute_url, norm_url, normalize_ws, same_page

PAGER_CONTAINER = re.compile(r"pagin|pager|page-?nav|paging|nav-links|page-numbers|wp-pagenavi|pagelist|page-list|page_list|paginat", re.I)
CAROUSEL = re.compile(r"slick|swiper|owl-|flickity|carousel|splide|glide|slider|tns-|keen-slider", re.I)
NEXT_WORDS = re.compile(
    r"^(?:next(?:\s+(?:page|events?|results?|»|›|→|>))?|older(?:\s+events?)?|more\s+events|next\s*[»›→>]+|[»›→>]+|[»›→>]\s*next|"
    r"siguiente|suivant|weiter)\W*$", re.I)
NEXT_SYMBOLS = {"›", "»", "→", ">", ">>", "»»", "next ›", "next »", "next →", "next >"}
LOAD_MORE = re.compile(
    r"^(?:load|show|view|see|get)\s+(?:more|additional)(?:\s+(?:events?|results?|items?|posts?|articles?|webinars?|courses?))?\W*$|"
    r"^more\s+(?:events?|results?|webinars?)\W*$|^load\s+additional\s+events\W*$", re.I)
LOAD_MORE_CLASS = re.compile(r"load[-_]?more|show[-_]?more|more[-_]?btn|btn[-_]?more|view[-_]?more|loadmore", re.I)
INFINITE_HINT = re.compile(
    r"infinite[-_ ]?scroll|data-infinite|IntersectionObserver|loadMoreOnScroll|waypoint|scroll-trigger|lazy[-_]?load[-_]?(?:items|posts)|"
    r"addEventListener\(\s*['\"]scroll|onscroll\s*=|\.on\(\s*['\"]scroll|admin-ajax\.php|[\"']load_?more[\"']|data-next-page|data-load-more", re.I)
TYPICAL_PAGE_SIZES = {10, 12, 15, 20, 24, 25, 30, 40, 50, 100}
PAGE_KEYS = ("page", "paged", "pg", "p", "pagenum", "pageno", "pageindex", "pagenumber", "pn", "epage", "sheet", "currentpage")
OFFSET_KEYS = ("start", "offset", "skip", "from", "first", "startindex", "b_start")
CURSOR_KEYS = ("cursor", "after", "next", "next_token", "page_token", "continuation", "pagetoken", "since")
_PATH_PAGE = re.compile(r"(?P<pre>.*?)(?P<sep>/page/|/p/|[-_/]page[-_]?)(?P<n>\d+)(?P<post>/?)$", re.I)
_STATED = [
    re.compile(r"(?:showing|displaying|results?)\s+(\d+)\s*[-–to]+\s*(\d+)\s+of\s+(\d+)", re.I),
    re.compile(r"\b(\d{1,5})\s+(?:events?|results?|items?|webinars?|courses?)\s+(?:found|available|total|match)", re.I),
    re.compile(r"\btotal(?:\s+of)?\s*[:\-]?\s*(\d{1,5})\s+(?:events?|results?|items?)", re.I),
    re.compile(r"\bof\s+(\d{1,5})\s+(?:events?|results?|items?)\b", re.I),
]
_PAGE_OF = re.compile(r"\bpage\s+(\d+)\s+of\s+(\d+)\b", re.I)


@dataclass
class PagerInfo:
    next_url: str = ""
    page_urls: dict[int, str] = field(default_factory=dict)
    current: int | None = None
    pattern: str = ""                 # query:page | path:/page/N | offset:start | cursor:after | ""
    load_more: bool = False
    load_more_text: str = ""
    infinite_hint: bool = False
    js_pager: bool = False            # a pager exists but its controls are not URL-addressable
    archive_pager: bool = False       # the only pager belongs to a past-events section
    stated_total: int | None = None
    stated_pages: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def addressable(self) -> bool:
        return bool(self.next_url or self.page_urls)


def _is_carousel(el: Tag) -> bool:
    for anc in [el] + [p for p in el.parents if isinstance(p, Tag)][:5]:
        if CAROUSEL.search(" ".join(anc.get("class") or []) + " " + (anc.get("id") or "")):
            return True
    return False


def _disabled(el: Tag) -> bool:
    cls = " ".join(el.get("class") or [])
    parent_cls = " ".join(el.parent.get("class") or []) if isinstance(el.parent, Tag) else ""
    return (el.has_attr("disabled") or el.get("aria-disabled") == "true" or re.search(r"\bdisabled\b", cls + " " + parent_cls) is not None)


def _label(a: Tag) -> str:
    return normalize_ws(a.get_text(" ", strip=True) or a.get("aria-label") or a.get("title") or "")


def _qs(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def is_page_like(candidate: str, current: str) -> tuple[bool, str]:
    """Does `candidate` differ from `current` only in a paging parameter / /page/N segment?"""
    cp, cu = urlsplit(candidate), urlsplit(current)
    if cp.netloc.lower().removeprefix("www.") != cu.netloc.lower().removeprefix("www."):
        return False, ""
    mq, cq = _qs(candidate), _qs(current)
    if cp.path.rstrip("/") == cu.path.rstrip("/"):
        diff = {k for k in set(mq) | set(cq) if mq.get(k) != cq.get(k)}
        for kind, keys in (("query:page", PAGE_KEYS), ("offset", OFFSET_KEYS), ("cursor", CURSOR_KEYS)):
            if diff and all(k.lower() in keys for k in diff):
                return True, f"{kind}"
    m1, m2 = _PATH_PAGE.match(cp.path), _PATH_PAGE.match(cu.path)
    if m1 and (cu.path.rstrip("/") == m1.group("pre").rstrip("/") or (m2 and m1.group("pre") == m2.group("pre"))) and mq == cq:
        return True, "path:/page/N"
    return False, ""


def _stated(text: str) -> tuple[int | None, int | None]:
    total = pages = None
    for rx in _STATED:
        m = rx.search(text)
        if m:
            total = int(m.groups()[-1])
            break
    m = _PAGE_OF.search(text)
    if m:
        pages = int(m.group(2))
    return total, pages


def _archive_container(el: Tag) -> bool:
    from pipeline.discovery.cards import _ARCHIVE_ATTR, _ARCHIVE_HEAD, _UPCOMING_HEAD
    for anc in [el] + [p for p in el.parents if isinstance(p, Tag)][:8]:
        if _ARCHIVE_ATTR.search(" ".join(anc.get("class") or []) + " " + (anc.get("id") or "")):
            return True
        # a heading among the previous siblings of this ancestor
        sib, n = anc.previous_sibling, 0
        while sib is not None and n < 6:
            if isinstance(sib, Tag) and (sib.name in ("h1", "h2", "h3", "h4", "h5", "h6")):
                t = normalize_ws(sib.get_text(" ", strip=True))
                if t:
                    return bool(_ARCHIVE_HEAD.search(t)) and not _UPCOMING_HEAD.search(t)
            if isinstance(sib, Tag):
                n += 1
            sib = sib.previous_sibling
    return False


def analyze_pagination(html: str, page_url: str, archive_url_hint: bool = False) -> PagerInfo:
    info = PagerInfo()
    soup = BeautifulSoup(html or "", "html.parser")
    text = normalize_ws(soup.get_text(" ", strip=True))[:60000]
    info.stated_total, info.stated_pages = _stated(text)
    info.infinite_hint = bool(INFINITE_HINT.search(html or ""))

    # <link rel="next"> in the head is the most reliable signal there is
    link = soup.find("link", rel=lambda r: r and "next" in (r if isinstance(r, list) else [r]), href=True)
    candidates: list[tuple[str, Tag | None, str]] = []   # (url, element, why)
    if link:
        candidates.append((absolute_url(page_url, link["href"]), None, "link rel=next"))

    containers = [c for c in soup.find_all(["nav", "ul", "ol", "div", "section", "span", "p"])
                  if PAGER_CONTAINER.search(" ".join(c.get("class") or []) + " " + (c.get("id") or "") + " " + (c.get("aria-label") or ""))
                  and not _is_carousel(c)]
    # innermost containers only
    containers = [c for c in containers if not any(o is not c and c in o.parents for o in containers)] or containers

    def consider(a: Tag, in_container: bool, container_archive: bool):
        href = (a.get("href") or "").strip()
        label = _label(a)
        low = label.lower()
        rel = " ".join(a.get("rel") or []).lower() if a.has_attr("rel") else ""
        cls = " ".join(a.get("class") or []).lower()
        is_next = ("next" in rel) or bool(NEXT_WORDS.match(low)) or low in NEXT_SYMBOLS or \
                  (in_container and ("next" in cls or "next" in (a.get("aria-label") or "").lower() or (a.get("title") or "").lower().startswith("next")))
        if is_next and not _is_carousel(a) and not _disabled(a):
            if not href or href.startswith(("#", "javascript:")):
                info.js_pager = True
                return
            url = absolute_url(page_url, href)
            ok, kind = is_page_like(url, page_url)
            if "next" in rel or in_container or ok:
                if container_archive:
                    info.archive_pager = True
                    return
                candidates.append((url, a, kind or "pager-container"))
                if kind and not info.pattern:
                    info.pattern = kind
            return
        # numbered page link
        if in_container and re.fullmatch(r"\d{1,4}", low) and href and not href.startswith(("#", "javascript:")):
            if container_archive:
                info.archive_pager = True
                return
            url = absolute_url(page_url, href)
            info.page_urls[int(low)] = url
            ok, kind = is_page_like(url, page_url)
            if kind and not info.pattern:
                info.pattern = kind
        elif in_container and re.fullmatch(r"\d{1,4}", low) and (not href or href.startswith(("#", "javascript:"))):
            info.js_pager = True

    for c in containers:
        arch = _archive_container(c)
        for a in c.find_all("a"):
            consider(a, True, arch)
        for b in c.find_all(["button"]):
            lab = _label(b).lower()
            if (NEXT_WORDS.match(lab) or lab in NEXT_SYMBOLS or "next" in " ".join(b.get("class") or []).lower()) and not _disabled(b) and not _is_carousel(b):
                if arch:
                    info.archive_pager = True
                else:
                    info.js_pager = True
        cur = c.find(attrs={"aria-current": True}) or c.find(class_=re.compile(r"\b(current|active|selected)\b"))
        if cur and not arch:
            m = re.search(r"\d+", normalize_ws(cur.get_text(" ", strip=True)))
            if m:
                info.current = int(m.group(0))
    # anchors outside any container: rel=next, or exactly a next-symbol with a page-like URL
    inside = {id(a) for c in containers for a in c.find_all("a")}
    for a in soup.find_all("a", href=True):
        if id(a) in inside:
            continue
        label = _label(a).lower()
        rel = " ".join(a.get("rel") or []).lower() if a.has_attr("rel") else ""
        if "next" in rel or NEXT_WORDS.match(label) or label in NEXT_SYMBOLS:
            consider(a, False, False)

    # choose the next URL: prefer rel=next, then pager container, never the page itself
    for url, el, why in candidates:
        if url and not same_page(url, page_url):
            info.next_url = url
            info.notes.append(f"next from {why}")
            break
    if not info.current:
        qs = _qs(page_url)
        for k in PAGE_KEYS:
            if k in qs and qs[k].isdigit():
                info.current = int(qs[k])
                break
        else:
            m = _PATH_PAGE.match(urlsplit(page_url).path)
            info.current = int(m.group("n")) if m else (1 if info.page_urls or info.next_url else None)
    if not info.next_url and info.page_urls and info.current:
        higher = sorted(n for n in info.page_urls if n > info.current)
        if higher:
            info.next_url = info.page_urls[higher[0]]
            info.notes.append(f"next from numbered page {higher[0]}")

    # load more
    for el in soup.find_all(["button", "a", "input"]):
        label = _label(el) if el.name != "input" else (el.get("value") or "")
        cls = " ".join(el.get("class") or [])
        if (LOAD_MORE.match(label.strip()) or LOAD_MORE_CLASS.search(cls + " " + (el.get("id") or ""))) and not _disabled(el) and not _is_carousel(el):
            if _archive_container(el):
                info.notes.append("load-more belongs to a past-events section: ignored")
                continue
            info.load_more, info.load_more_text = True, label.strip()[:40]
            break
    return info


def with_page(url: str, n: int, pattern: str) -> str:
    """URL of page n using the pattern learned from the page ('query:page' style)."""
    p = urlsplit(url)
    if pattern == "path:/page/N":
        m = _PATH_PAGE.match(p.path)
        path = (m.group("pre") + m.group("sep") + str(n) + m.group("post")) if m else p.path.rstrip("/") + f"/page/{n}/"
        return urlunsplit((p.scheme, p.netloc, path, p.query, ""))
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    key = next((k for k in q if k.lower() in PAGE_KEYS), "page")
    q[key] = str(n)
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))
