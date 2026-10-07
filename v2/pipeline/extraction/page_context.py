"""PageContext: one event page, prepared so that extraction can only ever see THIS event.

v1 stripped every <form> (an ASP.NET WebForms page wraps the WHOLE page in one),
took the first label found anywhere in <main>, and trusted a single JSON-LD Event
even when it belonged to a 'next event' widget. Here:

  * <form> elements are UNWRAPPED (their content kept); only form controls go;
  * related / recommended / upcoming-events widgets, newsletter and share blocks,
    sidebar event lists and navigation are removed from the extraction scope --
    and their text is KEPT SEPARATELY (`excluded_texts`) so verification can tell
    when a value came from outside the event;
  * the event container is chosen around the page's own main heading;
  * JSON-LD Events are validated against the page (URL / heading) before use;
  * label/value pairs keep their section and contact-context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup, Tag

from pipeline.common import absolute_url, normalize_ws, same_page, similarity
from pipeline.dates import find_dates
from pipeline.discovery import jsonld as jsonld_mod
from pipeline.discovery.cards import CTA_TEXT, is_generic_title
from pipeline.extraction.pairs import HEADS, Pair, collect_pairs
from pipeline.fetcher import is_soft_404, looks_blocked, visible_text

RELATED_HEAD = re.compile(
    r"\b(?:related|similar|more|other|upcoming|recent|latest|next|previous|featured|popular|recommended|suggested|trending|explore|"
    r"you\s+(?:may|might)\s+(?:also\s+)?(?:like|be\s+interested)|also\s+(?:of\s+)?interest)\s+"
    r"(?:events?|webinars?|courses?|posts?|articles?|news|resources?|stories|seminars?|conferences?|programmes?|programs?|training|sessions?|workshops?|reading)\b|"
    r"\b(?:events?|webinars?|courses?)\s+you\s+(?:may|might)\b|^(?:upcoming|latest|recent)\s+(?:news|events?)\b|"
    r"^(?:follow\s+us|subscribe|newsletter|share(?:\s+this)?|stay\s+(?:connected|informed|up\s+to\s+date)|join\s+our|sign\s+up\s+for|"
    r"get\s+in\s+touch|contact\s+us|our\s+(?:partners|sponsors)|sponsors?|partners?|latest\s+from|from\s+the\s+blog|related)\b", re.I)
NOISE_CLASS = re.compile(
    r"(?:^|[\s_-])(related|recommended|similar|more-events|other-events|upcoming-events|latest-events|next-event|prev-event|sidebar-events|"
    r"widget[-_]?recent|recent-posts|newsletter|subscribe|social|share|sharing|breadcrumbs?|cookie\w*|consent|comments?|"
    r"sponsor-?logos?|site-footer|site-header|masthead|mega-?menu|navbar|skip-link|back-to-top|print-only|visually-hidden|screen-reader-text)(?:$|[\s_-])", re.I)
BOILER = re.compile(
    r"cookie|privacy policy|subscribe|newsletter|all rights reserved|copyright|follow us|share this|sign in|log in|log-in|terms (?:of|and) (?:use|conditions)|"
    r"register now|book now|click here|back to events|add to (?:my )?calendar|download the|view all|you may also|javascript|skip to|"
    r"learn more about|read more|find out more|enable (?:cookies|javascript)", re.I)


@dataclass
class PageContext:
    url: str
    final_url: str
    html: str
    locale: str = "dmy"
    org_name: str = ""
    today: date = field(default_factory=date.today)
    # meta
    title_tag: str = ""
    og_title: str = ""
    og_description: str = ""
    meta_description: str = ""
    canonical: str = ""
    lang: str = ""
    event_meta: dict = field(default_factory=dict)
    # structured
    jsonld_all: list[dict] = field(default_factory=list)
    jsonld: dict | None = None            # the validated Event for THIS page
    jsonld_foreign: list[dict] = field(default_factory=list)
    jsonld_problems: list[dict] = field(default_factory=list)
    # scoped content
    container: Tag | None = None
    soup: BeautifulSoup | None = None
    container_text: str = ""
    full_text: str = ""
    h1: str = ""
    headings: list[tuple[int, str]] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)
    time_tags: list[dict] = field(default_factory=list)
    excluded_texts: list[str] = field(default_factory=list)
    excluded_titles: list[str] = field(default_factory=list)
    page_kind: str = "event"              # event | listing | unreadable
    readable: bool = True
    unreadable_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def pairs_of(self, *canons: str, include_contact: bool = False, include_agenda: bool = False) -> list[Pair]:
        return [p for p in self.pairs if p.canon in canons and (include_contact or not p.contact) and (include_agenda or not p.agenda)]


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return normalize_ws(tag["content"])
    return ""


def _safe_decompose(el: Tag) -> None:
    try:
        el.decompose()
    except Exception:  # noqa: BLE001
        pass


def _heading_rank(t: Tag) -> int:
    return int(t.name[1]) if t.name in HEADS else 9


def _is_ancestor(a: Tag, b: Tag | None) -> bool:
    return b is not None and (a is b or a in b.parents)


def _text(el) -> str:
    return normalize_ws(el.get_text(" ", strip=True)) if el is not None else ""


def _choose_h1(soup: BeautifulSoup, title_hints: list[str]) -> Tag | None:
    h1s = [h for h in soup.find_all("h1") if _text(h)]
    if not h1s:
        return None
    if len(h1s) == 1:
        return h1s[0]
    best, best_s = h1s[0], -1.0
    for h in h1s:
        t = _text(h)
        s = max([similarity(t, hint) for hint in title_hints if hint] or [0.0])
        if is_generic_title(t):
            s -= 0.5
        if s > best_s:
            best, best_s = h, s
    return best


def _note_titles(el: Tag, titles: list[str] | None) -> None:
    if titles is None:
        return
    for a in el.find_all("a"):
        t = _text(a)
        if t and len(t) > 6 and t not in titles:
            titles.append(t)


def _remove_section(h: Tag, excluded: list[str], protect: Tag | None, titles: list[str] | None = None) -> None:
    """Drop a heading and the content that belongs to it (its wrapper if it is the wrapper's lead, else following siblings)."""
    if protect is not None and (_is_ancestor(h, protect) or h is protect):
        return
    parent = h.parent
    if isinstance(parent, Tag) and parent.name in ("section", "aside", "div", "ul", "nav") and parent is not protect \
            and not (protect is not None and _is_ancestor(parent, protect)):
        first = next((c for c in parent.children if isinstance(c, Tag)), None)
        if first is h and len(_text(parent)) <= 3500:
            excluded.append(_text(parent))
            _note_titles(parent, titles)
            _safe_decompose(parent)
            return
    rank = _heading_rank(h)
    block = [h]
    sib = h.next_sibling
    while sib is not None:
        nxt = sib.next_sibling
        if isinstance(sib, Tag):
            if sib.name in HEADS and _heading_rank(sib) <= rank:
                break
            if protect is not None and (_is_ancestor(sib, protect) or sib is protect):
                break
            block.append(sib)
        sib = nxt
    excluded.append(normalize_ws(" ".join(_text(b) for b in block)))
    for b in block:
        _note_titles(b, titles) if isinstance(b, Tag) else None
        _safe_decompose(b)


def _list_widgets(soup: BeautifulSoup, protect: Tag | None, url: str, locale: str) -> list[Tag]:
    """Containers that are lists of OTHER events (>= 2 items each with a link elsewhere and a date)."""
    found: list[Tag] = []
    for el in soup.find_all(["ul", "ol", "div", "section", "aside", "table", "tbody"]):
        if protect is not None and _is_ancestor(el, protect):
            continue
        if any(f in el.parents for f in found):
            continue
        items = [c for c in el.children if isinstance(c, Tag) and c.name in ("li", "article", "div", "tr", "section", "a")]
        if len(items) < 2:
            continue
        good = 0
        for it in items:
            a = it.find("a", href=True) if it.name != "a" else it
            if a is None:
                continue
            href = absolute_url(url, a.get("href"))
            if not href or same_page(href, url):
                continue
            t = _text(it)
            if len(t) <= 700 and any(h.valid or h.partial for h in find_dates(t, locale)):
                good += 1
        if good >= 2 and len(_text(el)) <= 4000:
            found.append(el)
    return found


def build_context(html: str, url: str, final_url: str = "", locale: str = "dmy", org_name: str = "",
                  card_title: str = "", today: date | None = None) -> PageContext:
    ctx = PageContext(url=url, final_url=final_url or url, html=html or "", locale=locale, org_name=org_name,
                      today=today or date.today())
    if not html or not html.strip():
        ctx.readable, ctx.page_kind, ctx.unreadable_reason = False, "unreadable", "empty page"
        return ctx
    if looks_blocked(html):
        ctx.readable, ctx.page_kind, ctx.unreadable_reason = False, "unreadable", "blocked by the site"
        return ctx
    soup = BeautifulSoup(html, "html.parser")
    ctx.title_tag = normalize_ws(soup.title.get_text()) if soup.title else ""
    ctx.og_title = _meta(soup, "og:title", "twitter:title")
    ctx.og_description = _meta(soup, "og:description", "twitter:description")
    ctx.meta_description = _meta(soup, "description")
    link = soup.find("link", rel="canonical")
    ctx.canonical = absolute_url(url, link.get("href")) if link and link.get("href") else ""
    ctx.lang = (soup.html.get("lang") if soup.html else "") or ""
    for key in ("event:start_time", "og:event:start_time", "event:end_time", "article:published_time", "startDate", "endDate"):
        v = _meta(soup, key)
        if v:
            ctx.event_meta[key] = v

    # ---- JSON-LD, validated against THIS page ------------------------------------------------------------------------------------
    records, problems = jsonld_mod.parse_jsonld_events(html, ctx.final_url)
    ctx.jsonld_problems = problems
    hints = [t for t in (card_title, ctx.og_title, ctx.title_tag) if t]
    h1_pre = _choose_h1(soup, hints)
    h1_text_pre = _text(h1_pre)
    validated = jsonld_mod.validate_for_page(records, ctx.final_url, [h1_text_pre, ctx.og_title, ctx.title_tag, card_title] + ([ctx.canonical] if False else []))
    ctx.jsonld_all = validated
    if ctx.canonical:
        for r in validated:
            if r.get("belongs") is not True and r.get("url") and same_page(r["url"], ctx.canonical):
                r["belongs"], r["why"] = True, ["url is this page's canonical URL"]
    ok = [r for r in validated if r.get("belongs") is True]
    ctx.jsonld = ok[0] if len(ok) == 1 else (max(ok, key=lambda r: len(r.get("name", ""))) if ok else None)
    ctx.jsonld_foreign = [r for r in validated if r.get("belongs") is not True]

    # ---- scope the document ------------------------------------------------------------------------------------------------------
    for t in soup(["script", "style", "noscript", "template", "svg", "iframe", "link", "meta", "object", "embed", "canvas", "select",
                   "input", "textarea", "head"]):
        _safe_decompose(t)
    for b in soup.find_all("button"):       # accordion/tab titles live inside <button>: keep their text
        b.unwrap()
    for f in soup.find_all("form"):           # ASP.NET WebForms: the whole page is inside ONE form -- keep its content
        f.unwrap()
    h1 = _choose_h1(soup, hints)
    protect = h1
    for t in soup.find_all(["nav", "footer"]):
        if not (protect is not None and _is_ancestor(t, protect)):
            ctx.excluded_texts.append(_text(t)[:1500])
            _safe_decompose(t)
    for t in soup.find_all("header"):
        # a <header> is only site chrome when it is mostly links and says nothing dated; an event 'hero' banner
        # (title, date, venue) is also a <header> on many themes and must stay
        txt = _text(t)
        ident = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        hero = any(h.valid or h.partial for h in find_dates(txt, locale))
        chrome = bool(re.search(r"site-header|masthead|navbar|top-?bar|main-header|global-header|page-header-nav", ident, re.I)) \
            or (len(t.find_all("a")) >= 4 and not hero)
        if t.find("h1") is None and chrome and not hero:
            ctx.excluded_texts.append(txt[:1500])
            _safe_decompose(t)
    for t in soup.find_all(attrs={"role": re.compile(r"navigation|contentinfo|search|complementary", re.I)}):
        if not (protect is not None and _is_ancestor(t, protect)):
            ctx.excluded_texts.append(_text(t)[:1500])
            _safe_decompose(t)
    for t in list(soup.find_all(True)):
        if t.attrs is None:
            continue
        ident = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        if ident.strip() and NOISE_CLASS.search(ident) and not (protect is not None and (_is_ancestor(t, protect) or t is protect)):
            txt = _text(t)
            if len(txt) <= 3000:
                ctx.excluded_texts.append(txt)
                _note_titles(t, ctx.excluded_titles)
                _safe_decompose(t)
    for h in list(soup.find_all(HEADS + ["strong", "b", "p"])):
        if h.attrs is None or (h.name in ("strong", "b", "p") and len(_text(h)) > 60):
            continue
        t = _text(h)
        if t and RELATED_HEAD.search(t) and h is not protect:
            if h.name in ("strong", "b", "p") and (h.parent is None or h.parent.name in ("body", "main", "article")):
                continue
            _remove_section(h, ctx.excluded_texts, protect, ctx.excluded_titles)
    total_before = len(_text(soup.body or soup))
    widgets = _list_widgets(soup, protect, ctx.final_url, locale)
    removed_len = 0
    for w in widgets:
        t = _text(w)
        removed_len += len(t)
        ctx.excluded_texts.append(t)
        for a in w.find_all("a"):
            if _text(a) and len(_text(a)) > 6:
                ctx.excluded_titles.append(_text(a))
        _safe_decompose(w)

    # ---- the event container -------------------------------------------------------------------------------------------------------------
    root = None
    ev_scope = soup.find(attrs={"itemtype": re.compile(r"schema\.org/\w*Event$", re.I)})
    if ev_scope is not None:
        root = ev_scope
    if root is None and h1 is not None:
        art = h1.find_parent("article")
        main = h1.find_parent("main") or h1.find_parent(attrs={"role": "main"})
        root = art if art is not None and len(_text(art)) >= 200 else main
    if root is None:
        root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article")
    if root is None and h1 is not None:
        cur = h1
        while cur.parent is not None and isinstance(cur.parent, Tag) and cur.parent.name not in ("body", "html", "[document]") and len(_text(cur)) < 700:
            cur = cur.parent
        root = cur
    root = root or soup.body or soup
    if h1 is not None and h1 is not root and root not in h1.parents:
        # the page's own heading (and its date/venue sub-heading) sit in a hero banner OUTSIDE <main>: widen the scope
        # to the nearest ancestor that holds both the heading and the main content
        for anc in h1.parents:
            if isinstance(anc, Tag) and (root is anc or root in anc.descendants):
                root = anc
                break
    ctx.soup, ctx.container = soup, root
    ctx.container_text = _text(root)
    ctx.full_text = _text(soup.body or soup)
    ctx.h1 = _text(h1) if h1 is not None else ""
    ctx.headings = [(_heading_rank(h), _text(h)) for h in root.find_all(HEADS) if _text(h)][:30]
    ctx.pairs = collect_pairs(root)
    for t in root.find_all("time"):
        dt = t.get("datetime") or ""
        txt = _text(t)
        if dt or txt:
            ctx.time_tags.append({"datetime": dt.strip(), "text": txt, "near": _text(t.parent)[:200]})

    # ---- readability / page kind ---------------------------------------------------------------------------------------------------------------
    if is_soft_404(html, None):
        ctx.readable, ctx.page_kind, ctx.unreadable_reason = False, "unreadable", "the page is a 'not found' view"
    elif len(ctx.container_text) < 120 and len(ctx.full_text) < 200:
        ctx.readable, ctx.page_kind = False, "unreadable"
        ctx.unreadable_reason = "almost no readable text (JavaScript-only page or an empty shell)"
    elif widgets and (not ctx.h1 or is_generic_title(ctx.h1, org_name)) and (
            removed_len > 0.6 * max(total_before, 1) or not any(h.valid or h.partial for h in find_dates(ctx.container_text, locale))):
        ctx.page_kind = "listing"
        ctx.notes.append("the page is a list of several events, not one event's page")
    return ctx
