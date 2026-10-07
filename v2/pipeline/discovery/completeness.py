"""Discovery completeness: did we really see every event this organisation lists?

Three outcomes (the only ones that exist):

    discovery_complete    every signal agrees with what was found
    discovery_incomplete  something says there is more (or we could not finish looking)
    discovery_failed      the list could not be read at all

Signals compared: stated total, stated page count, cards, event links, JSON-LD
events, sitemap events, RSS/ICS events, pagination / load-more / scroll end
states and the previous run's count.

An organisation that is incomplete or failed is NEVER treated as "has no
events", and downstream "not found on website" demotions are suppressed for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.models import DISCOVERY_COMPLETE, DISCOVERY_FAILED, DISCOVERY_INCOMPLETE_STATE

BAD_END_STATES = ("capped", "stuck", "error", "loop")


@dataclass
class Signals:
    fetch_ok: bool = True
    fetch_error: str = ""
    soft_404: bool = False
    blocked: bool = False
    js_unrendered: bool = False           # page needs JavaScript and the browser could not render it
    pages_walked: int = 1
    candidates_current: int = 0           # unique non-archive candidates kept
    candidates_all: int = 0               # incl. archive/dropped-as-archive (what the page truly listed)
    cards: int = 0
    event_links: int = 0
    jsonld_events: int = 0
    sitemap_events: int = 0               # upcoming event pages found in the sitemap that the list did not show
    feed_events: int = 0                  # current ICS events the list did not show
    stated_total: int | None = None
    stated_pages: int | None = None
    pagination_state: str = "none"        # none|complete|capped|stuck|error|loop
    load_more_state: str = "none"
    scroll_state: str = "none"
    archive_pager_ignored: bool = False
    previous_count: int | None = None
    empty_state_text: bool = False
    page_errors: list[str] = field(default_factory=list)
    source_url_is_archive: bool = False
    extra: dict = field(default_factory=dict)


def assess(sig: Signals) -> dict:
    reasons: list[str] = []
    state = DISCOVERY_COMPLETE
    found = sig.candidates_current

    rank = {DISCOVERY_COMPLETE: 0, DISCOVERY_INCOMPLETE_STATE: 1, DISCOVERY_FAILED: 2}

    def worsen(to: str, why: str):
        nonlocal state
        reasons.append(why)
        if rank[to] > rank[state]:
            state = to

    if not sig.fetch_ok:
        why = f"the list page could not be fetched ({sig.fetch_error or 'no response'})"
        if found:
            worsen(DISCOVERY_INCOMPLETE_STATE, why + " -- only feed/sitemap events were found")
        else:
            worsen(DISCOVERY_FAILED, why)
    if sig.soft_404:
        why = "the configured URL returns a 'page not found' view (HTTP 200 soft-404): the source URL looks broken"
        if found:
            worsen(DISCOVERY_INCOMPLETE_STATE, why)
        else:
            worsen(DISCOVERY_FAILED, why)
    if sig.blocked and not found:
        worsen(DISCOVERY_FAILED, "the site blocked the request and no browser result is available")
    if sig.js_unrendered:
        worsen(DISCOVERY_INCOMPLETE_STATE if found else DISCOVERY_FAILED,
               "the page needs JavaScript to show its events and browser rendering did not succeed")
    if sig.source_url_is_archive:
        worsen(DISCOVERY_INCOMPLETE_STATE, "the configured URL is a past-events archive, not a current list")

    for name, st in (("pagination", sig.pagination_state), ("load-more", sig.load_more_state), ("infinite-scroll", sig.scroll_state)):
        if st in BAD_END_STATES:
            worsen(DISCOVERY_INCOMPLETE_STATE, f"{name} did not finish cleanly ({st})")
    for e in sig.page_errors:
        worsen(DISCOVERY_INCOMPLETE_STATE, f"a list page failed to load: {e}")

    if sig.stated_total is not None and sig.candidates_all < sig.stated_total and not sig.archive_pager_ignored:
        worsen(DISCOVERY_INCOMPLETE_STATE, f"the page states {sig.stated_total} events but only {sig.candidates_all} were found")
    if sig.stated_pages and sig.pages_walked < sig.stated_pages and sig.pagination_state != "complete" and not sig.archive_pager_ignored:
        worsen(DISCOVERY_INCOMPLETE_STATE, f"the pager states {sig.stated_pages} pages but only {sig.pages_walked} were read")
    if sig.sitemap_events:
        worsen(DISCOVERY_INCOMPLETE_STATE, f"{sig.sitemap_events} upcoming event page(s) are in the sitemap but were not on the list")
    if sig.feed_events:
        worsen(DISCOVERY_INCOMPLETE_STATE, f"{sig.feed_events} current event(s) are in the ICS calendar but were not on the list")
    if sig.previous_count and sig.previous_count >= 6 and found < sig.previous_count * 0.5:
        worsen(DISCOVERY_INCOMPLETE_STATE, f"{found} events found vs {sig.previous_count} last run (big drop)")
    elif sig.previous_count and found == 0 and not sig.empty_state_text:
        worsen(DISCOVERY_INCOMPLETE_STATE, f"0 events found but {sig.previous_count} last run and the page does not say it has none")

    if found == 0 and state == DISCOVERY_COMPLETE:
        if sig.empty_state_text or sig.jsonld_events == 0 and sig.sitemap_events == 0 and sig.feed_events == 0 and sig.extra.get("empty_listing_confirmed"):
            reasons.append("0 events and the page itself says there are none")
        else:
            worsen(DISCOVERY_INCOMPLETE_STATE, "0 events found and the page does not say it has none -- not assumed empty")

    if state == DISCOVERY_COMPLETE:
        reasons.append("all signals agree with what was found")
    return {"state": state, "reasons": reasons,
            "signals": {k: v for k, v in sig.__dict__.items() if k != "extra"} | sig.extra}
