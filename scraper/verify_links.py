"""Verify that a scraped event link actually resolves before it goes in the sheet."""

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ExpertlyEventScraper/1.0)"}


def link_is_reachable(url: str, timeout: float = 6.0) -> bool:
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
    (possibly-adjusted) events and a count of how many links were replaced."""
    replaced = 0
    for e in events:
        link = e.get("link", "")
        if link and not link_is_reachable(link):
            e["link"] = fallback_url
            replaced += 1
        elif not link:
            e["link"] = fallback_url
    return events, replaced
