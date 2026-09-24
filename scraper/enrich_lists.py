"""Stages 1-6 for one country, list page first.

  1. open every organisation's list page (all pages / load-more) and read each
     event card with proof (list_extract.py)
  2. for an event that has a page of its own, open it and read it too
     (detail_extract.py); the two are merged, and if they disagree about the
     date or format NEITHER is used
  3. classify, verify (re-scraping the event page on failure), and return one
     record per event

Events with no page of their own (the link is the list page, or one page is
shared by several events) are verified against their card on the list page.
"""

import hashlib
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from scraper.classify_rules import classify_event
from scraper.detail_extract import COLUMNS, extract_detail_page
from scraper.enrich import MAX_RETRIES, _haystack, _is_past, has_own_page
from scraper.event_id import _normalize_url, make_event_id
from scraper.evidence import normalize_ws
from scraper.fast_fetch import _BROWSER_SLOTS, fetch_detail_fast, fetch_list_pages, looks_js_shell
from scraper.label_extract import main_text
from scraper.list_extract import extract_list_events
from scraper.site_health import load_json, save_json
from scraper.verify_events import resolve_country, verify_event

_CONFLICT_COLUMNS = ("date", "end_date", "format")
_RANK = {"high": 3, "medium": 2, "low": 1}


def merge_fields(detail: dict, listing: dict) -> tuple[dict, list[dict]]:
    """Event-page values and list-card values, merged. A disagreement about
    the date/end date/format drops both and is reported as a conflict."""
    merged, conflicts = {}, []
    for column in COLUMNS:
        d, l = detail.get(column), listing.get(column)
        if d and l and column in _CONFLICT_COLUMNS and normalize_ws(d["value"]) != normalize_ws(l["value"]):
            conflicts.append({"column": column, "event_page": d["value"], "list_page": l["value"]})
            continue
        candidates = [x for x in (d, l) if x]
        if candidates:
            merged[column] = max(candidates, key=lambda f: _RANK[f["confidence"]])
    return merged, conflicts


def _scrape_list(org: dict, fetch_list_fn, today: datetime) -> dict:
    try:
        htmls = fetch_list_fn(org["url"])
        events, haystack, seen = [], [], set()
        for html in htmls:
            haystack.append(_haystack(html))
            for e in extract_list_events(html, org["url"], today):
                key = (e["link"], e["fields"]["name"]["value"], e["fields"].get("date", {}).get("value", ""))
                if key not in seen:
                    seen.add(key)
                    events.append(e)
        return {"org": org, "events": events, "haystack": " ".join(haystack), "error": None}
    except Exception as exc:  # noqa: BLE001 - one bad site must never stop the run
        return {"org": org, "events": [], "haystack": "", "error": str(exc)[:200]}


def process_candidate(cand: dict, detail: dict | None, refetch, source_country: str, today: datetime,
                      max_retries: int = MAX_RETRIES) -> dict:
    """One event: merge its card and (if it has one) its own page, classify,
    verify, and re-open the event page up to `max_retries` times on failure."""
    listing, org = cand["fields"], cand["organizer"]
    own_page = cand["own_page"]
    attempts, page_html, extraction = 0, None, None
    merged, conflicts = dict(listing), []
    haystack = cand["haystack"]

    while True:
        if own_page:
            result = detail if attempts == 0 else refetch(cand["link"], wait_ms=3000 * attempts)
            page_html = (result or {}).get("html")
            if page_html is None:
                if (result or {}).get("status") in (404, 410) or attempts >= max_retries:
                    break
                attempts += 1
                continue
            extraction = extract_detail_page(page_html, cand["link"], org)
            merged, conflicts = merge_fields(extraction["fields"], listing)
            haystack = cand["haystack"] + " " + _haystack(page_html)
        name = merged.get("name", {}).get("value", "")
        classification = classify_event(name, merged.get("description", {}).get("value", ""))
        failures = verify_event(merged, classification["category"], haystack, today)
        if (classification["category"] == "Reject" or _is_past(merged, today)
                or (not failures and not conflicts and not cand["conflict"]) or not own_page or attempts >= max_retries
                or not looks_js_shell(page_html)):  # a page that loaded fully will not say more the second time
            break
        attempts += 1

    name = merged.get("name", {}).get("value", "")
    classification = classify_event(name, merged.get("description", {}).get("value", ""))
    failures = verify_event(merged, classification["category"], haystack, today)
    reasons = list(failures)
    if cand["conflict"]:
        reasons.append(f"list page: {cand['conflict']}")
    for c in conflicts:
        reasons.append(f"conflict on {c['column']}: event page says '{c['event_page']}', list page says '{c['list_page']}'")
    if extraction:
        reasons += [r for r in extraction["review_reasons"]
                    if r not in reasons and "no event date" not in r and " came from a " not in r and "conflict on" not in r]
    if classification["category"] == "Reject":
        status, reasons = "rejected", [classification["reason"]]
    elif _is_past(merged, today) and not cand["conflict"] and not conflicts:
        status, reasons = "past", []
    elif reasons:
        status = "needs_review"
    else:
        status = "verified"

    location = merged.get("location", {}).get("value", "")
    when = merged.get("date", {}).get("value", "")
    event_id = make_event_id(org, name, cand["link"] if own_page else "", cand["source_url"], when)
    basis = cand["card_text"] + "|" + (main_text(page_html) if page_html else "")
    return {"event_id": event_id, "status": status, "fields": merged, "category": classification["category"],
            "classification_reason": classification["reason"], "reasons": reasons, "attempts": attempts,
            "organizer": org, "source_url": cand["source_url"], "link": cand["link"], "source_country": source_country,
            "country": resolve_country(location, source_country),
            "fingerprint": hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]}


def enrich_country_from_lists(label: str, orgs: list[dict], state_dir: str, today: datetime | None = None,
                              fetch_list_fn=None, fetch_detail_fn=fetch_detail_fast, workers: int = 20,
                              max_events: int = 0) -> tuple[list[dict], dict]:
    """Returns (records, org_counts). org_counts is {list_url: {"organizer",
    "events", "error"}} for the site health check."""
    today = today or datetime.now()
    fetch_list_fn = fetch_list_fn or (lambda url: fetch_list_pages(url, has_events=lambda h: bool(extract_list_events(h, url, today))))
    scraped = [None] * len(orgs)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(_scrape_list, o, fetch_list_fn, today): i for i, o in enumerate(orgs)}
        for done, future in enumerate(as_completed(futures), 1):
            result = scraped[futures[future]] = future.result()
            note = f" FAILED: {result['error']}" if result["error"] else ""
            print(f"  list pages {done}/{len(orgs)}: {result['org']['organizer'][:45]} -> {len(result['events'])} events{note}", flush=True)

    org_counts = {s["org"]["url"]: {"organizer": s["org"]["organizer"], "events": len(s["events"]), "error": s["error"]}
                  for s in scraped}

    candidates = []
    for s in scraped:
        for e in s["events"]:
            candidates.append({"organizer": s["org"]["organizer"], "source_url": s["org"]["url"], "link": e["link"],
                               "fields": e["fields"], "conflict": e["conflict"], "card_text": e["card_text"],
                               "haystack": s["haystack"]})
    if max_events:
        candidates = candidates[:max_events]

    link_counts = Counter(_normalize_url(c["link"]) for c in candidates if c["link"])
    for c in candidates:
        c["own_page"] = has_own_page(c["link"], c["source_url"], link_counts)

    detail_links = [c["link"] for c in candidates if c["own_page"]]
    print(f"  opening {len(detail_links)} event pages ({len(candidates) - len(detail_links)} events have no page of their own)", flush=True)
    detail = fetch_detail_fn(detail_links, workers=workers)
    print("  event pages done; checking every event", flush=True)
    def refetch(url, wait_ms):
        with _BROWSER_SLOTS:
            return fetch_detail_fn([url], wait_ms=wait_ms, workers=1).get(url)

    state_path = os.path.join(state_dir, f"lists_{label}.json")
    state = load_json(state_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        processed = list(executor.map(
            lambda c: process_candidate(c, detail.get(c["link"]) if c["own_page"] else None, refetch, label, today),
            candidates))
    records, seen_ids = [], set()
    for record in processed:
        if record["event_id"] in seen_ids:
            continue  # the same event on two pages of one list
        seen_ids.add(record["event_id"])
        previous = state.get(record["event_id"])
        if previous and previous.get("fingerprint") == record["fingerprint"]:
            record = {**previous["record"], "changed": False}
        else:
            record["changed"] = True
        state[record["event_id"]] = {"fingerprint": record["fingerprint"],
                                     "record": {k: v for k, v in record.items() if k != "changed"}}
        records.append(record)
    save_json(state_path, state)
    return records, org_counts
