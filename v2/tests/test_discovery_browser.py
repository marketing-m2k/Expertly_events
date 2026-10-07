"""Real headless Chromium against a local test site: Load more, infinite scroll, JS pagers, JS-rendered pages.
These are the mechanisms v1 swallowed errors for; here every one must end in an explicit state."""

from datetime import date

import pytest

from pipeline.discovery.browser import BrowserSession
from pipeline.discovery.orchestrator import DiscoveryContext, discover_org
from pipeline.fetcher import HttpFetcher
from tests.helpers import LocalSite

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def site():
    with LocalSite() as s:
        yield s


def disco(site, path, **kw):
    org = {"organizer": "Local Org", "country": "UK", "url": site.base + path}
    ctx = DiscoveryContext(http=HttpFetcher(delay_s=0), browser_factory=lambda: BrowserSession(wait_ms=300), today=date(2026, 10, 7),
                           use_feeds=False, deadline_s=60, **kw)
    return discover_org(org, ctx)


def test_load_more_is_clicked_until_the_button_disappears(site):
    res = disco(site, "/loadmore")
    assert len(res.candidates) == 14
    assert res.completeness["state"] == "discovery_complete"
    rep = next(p["report"] for p in res.pages if p["kind"] == "browser")
    assert rep["load_more"]["state"] == "complete" and rep["load_more"]["clicks"] == 2


def test_load_more_that_does_nothing_is_a_reported_failure_not_silence(site):
    res = disco(site, "/loadmore_stuck")
    assert len(res.candidates) == 3
    assert "load_more" in res.failure_kinds
    assert res.completeness["state"] == "discovery_incomplete"


def test_infinite_scroll_loads_everything(site):
    res = disco(site, "/infinite")
    assert len(res.candidates) == 17
    rep = next(p["report"] for p in res.pages if p["kind"] == "browser")
    assert rep["scroll"]["state"] == "complete"
    assert res.completeness["state"] == "discovery_complete"


def test_endless_scroll_hits_the_cap_and_says_so(site):
    with BrowserSession(wait_ms=200) as br:
        br.goto(site.base + "/endless")
        from pipeline.discovery.browser import ExpandReport
        rep = ExpandReport()
        import time
        br.expand_scroll(rep, time.monotonic() + 60, max_rounds=3)
    assert rep.scroll["state"] == "capped" and "infinite_scroll" in rep.failure_kinds


def test_js_pager_walks_all_pages_and_never_clicks_an_event_titled_next(site):
    res = disco(site, "/jspager")
    titles = {c.title for c in res.candidates}
    assert "Next Gen Tax Discussion Group" in titles                       # visible on page 1, event not navigated into
    assert {f"Finance Seminar {i}" for i in range(1, 9)} <= titles       # all 3 pages
    assert res.completeness["state"] == "discovery_complete"
    rep = next(p["report"] for p in res.pages if p["kind"] == "browser")
    assert rep["pager"]["state"] == "complete" and rep["pager"]["pages"] == 3


def test_js_pager_that_does_not_change_the_page_is_a_pagination_failure(site):
    res = disco(site, "/jspager_stuck")
    assert "pagination" in res.failure_kinds and res.completeness["state"] == "discovery_incomplete"


def test_javascript_rendered_page_is_rendered_not_declared_empty(site):
    res = disco(site, "/shell")
    assert len(res.candidates) == 4 and res.completeness["state"] == "discovery_complete"


def test_url_pagination_through_the_real_http_fetcher(site):
    res = disco(site, "/pages/1")
    assert len(res.candidates) == 9 and res.completeness["state"] == "discovery_complete"


def test_missing_page_is_failed(site):
    res = disco(site, "/does-not-exist")
    assert res.completeness["state"] == "discovery_failed"
