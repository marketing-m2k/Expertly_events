"""Verify that a scraped event link actually resolves before it goes in the sheet."""

from concurrent.futures import ThreadPoolExecutor

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ExpertlyEventScraper/1.0)"}
MAX_CHECK_WORKERS = 15


def link_is_reachable(url: str, timeout: float = 4.0) -> bool:
    if not url or not url.startswith("http"):
        return False
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=HEADERS)
        if r.status_code >= 400:
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=HEADERS, stream=True)
        return r.status_code < 400
    except requests.RequestException:
        return False


def verify_events(events: list[dict], fallback_url: str) -> tuple[list[dict], int]:
    """Check each event's link; if it doesn't resolve, fall back to the
    org's events-page URL so the row still has a usable link. Returns the
    (possibly-adjusted) events and a count of how many links were replaced.

    Checks run concurrently — these are independent I/O-bound HTTP requests,
    and a page with 20-30 events checked one at a time (each up to ~8s on a
    dead link) can otherwise take minutes for a single organization.
    """
    links = [e.get("link", "") for e in events]
    with ThreadPoolExecutor(max_workers=MAX_CHECK_WORKERS) as executor:
        reachable = list(executor.map(lambda link: bool(link) and link_is_reachable(link), links))

    replaced = 0
    for e, link, ok in zip(events, links, reachable):
        if link and not ok:
            e["link"] = fallback_url
            replaced += 1
        elif not link:
            e["link"] = fallback_url
    return events, replaced
