"""Runs v1's discovery (scraper/fast_fetch.fetch_list_pages + scraper/list_extract)
through the measurement harness and diagnoses WHY each baseline event it missed
was missed.

v1 itself records no drop reasons, so the diagnosis re-runs v1's own stages on
the HTML v1 saw -- the same functions, in the same order -- and reports which
stage lost the event:

    fetch_failed / js_not_rendered / pagination_not_followed / not_in_fetched_pages
    no_repeated_card_group      (single, featured or alternating layout: no 2 identical siblings)
    card_group_low_score        (the repeated group scored under 0.3)
    nested_group_suppressed     (another group's nesting removed this one)
    generic_title_discarded     (title looked like 'Webinar' / 'Event - ...' so the card was dropped)
    in_page_but_not_extracted   (none of the above matched)
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline.common import norm_text, norm_url
from scraper.fast_fetch import fetch_list_pages, looks_js_shell
from scraper.heuristic_extract import _dedupe_nested, _find_repeated_card_groups, _score_group, _strip_page_chrome
from scraper.label_extract import is_generic_title
from scraper.list_extract import extract_list_events


def _to_cand(e: dict) -> dict:
    f = e["fields"]
    return {"title": f.get("name", {}).get("value", ""), "url": e.get("link", ""),
            "date_hint": f.get("date", {}).get("value", ""), "card_text": e.get("card_text", "")}


def run_v1(url: str, htmls: list[str] | None = None, today: datetime | None = None) -> dict:
    """htmls=None -> fetch live exactly as v1 does; htmls=[...] -> read these pages (card parsing only)."""
    today = today or datetime(2026, 10, 7)
    t0 = time.time()
    errors: list[str] = []
    if htmls is None:
        try:
            htmls = fetch_list_pages(url, has_events=lambda h: bool(extract_list_events(h, url, today)))
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            errors.append(f"fetch: {exc!s:.150}")
            htmls = []
    cands, seen = [], set()
    for html in htmls:
        for e in extract_list_events(html, url, today):
            key = (e["link"], e["fields"]["name"]["value"], e["fields"].get("date", {}).get("value", ""))
            if key not in seen:
                seen.add(key)
                cands.append(_to_cand(e))
    return {"candidates": cands, "dropped": [], "state": "", "failure_kinds": ["fetch"] if errors else [],
            "errors": errors, "pages": len(htmls), "seconds": time.time() - t0, "htmls": htmls,
            "miss_diagnoser": lambda miss: diagnose_miss(htmls, url, miss)}


def _locate(soup: BeautifulSoup, page_url: str, miss: dict):
    """The anchor (or text node's parent) for a baseline event in v1's cleaned DOM."""
    want_url = norm_url(miss.get("url", ""))
    if want_url:
        for a in soup.find_all("a", href=True):
            if norm_url(urljoin(page_url, a["href"])) == want_url:
                return a
    want = norm_text(miss.get("title", ""))
    if want:
        for node in soup.find_all(string=True):
            if want and want in norm_text(str(node)):
                return node.parent
    return None


def diagnose_miss(htmls: list[str], page_url: str, miss: dict) -> str:
    if not htmls:
        return "fetch_failed"
    want = norm_text(miss.get("title", ""))
    want_url = norm_url(miss.get("url", ""))
    found_anywhere = False
    for html in htmls:
        if (want and want in norm_text(html)) or (want_url and want_url in norm_url(html) or (want_url and want_url.split("/", 1)[-1] in html)):
            found_anywhere = True
            break
    if not found_anywhere:
        if all(looks_js_shell(h) for h in htmls):
            return "js_not_rendered"
        return "not_in_fetched_pages"      # pagination / load-more / scroll never reached it (or wrong page was read)
    for html in htmls:
        soup = BeautifulSoup(html, "html.parser")
        _strip_page_chrome(soup)
        el = _locate(soup, page_url, miss)
        if el is None:
            continue
        groups = _find_repeated_card_groups(soup)
        in_group = [g for g in groups if any(card is el or card in el.parents for card in g)]
        if not in_group:
            return "no_repeated_card_group"
        passing = [g for g in groups if _score_group(g) >= 0.3]
        if not any(g in passing for g in in_group):
            return "card_group_low_score"
        kept = _dedupe_nested(passing)
        if not any(g in kept for g in in_group):
            return "nested_group_suppressed"
        for g in in_group:
            if g in kept:
                card = next(c for c in g if c is el or c in el.parents)
                from scraper.heuristic_extract import _find_title_and_link
                title, _ = _find_title_and_link(card, page_url)
                if not title or is_generic_title(title):
                    return "generic_title_discarded"
        return "in_page_but_not_extracted"
    return "in_page_but_not_extracted"


def ideal_pages(slug: str, baseline: dict) -> list[str]:
    d = os.path.join("baseline", "raw", slug)
    names = baseline.get("ideal_pages") or ["page_01.html"]
    return [open(os.path.join(d, n), encoding="utf-8").read() for n in names if os.path.exists(os.path.join(d, n))]
