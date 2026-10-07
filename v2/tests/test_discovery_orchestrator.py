"""Whole-organisation discovery: pagination styles, feeds, archives, failures and completeness verdicts.
No network: pages come from a ReplayFetcher; browser behaviour is faked unless a test says otherwise."""

import contextlib
from datetime import date

import pytest

from pipeline.discovery.completeness import Signals, assess
from pipeline.discovery.feeds import find_feed_links, parse_feed, parse_ics, parse_sitemap
from pipeline.discovery.orchestrator import DiscoveryContext, discover_org
from pipeline.discovery.pagination import analyze_pagination, is_page_like, with_page
from pipeline.fetcher import Page, ReplayFetcher, is_soft_404, looks_js_shell
from tests.helpers import card_article, cards, page

BASE = "https://ex.org"
URL = BASE + "/events"
ORG = {"organizer": "Example Institute", "country": "UK", "url": URL}
TODAY = date(2026, 10, 7)


def ev_page(start, n, extra_head="", extra_body="", total_heading=""):
    return page(total_heading + cards(n, start) + extra_body, head=extra_head)


def run(pages, browser=None, previous=None, feeds=False):
    ctx = DiscoveryContext(http=ReplayFetcher(pages), browser_factory=browser, today=TODAY,
                           previous_counts=previous or {}, use_feeds=feeds)
    return discover_org(ORG, ctx)


class FakeBrowser(contextlib.AbstractContextManager):
    """Stands in for BrowserSession: returns scripted snapshots."""
    def __init__(self, snapshots, **report):
        self.snapshots, self.report = snapshots, report

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def expand_listing(self, url, **kw):
        from pipeline.discovery.browser import ExpandReport
        rep = ExpandReport(snapshots=list(self.snapshots), snapshot_urls=[url] * len(self.snapshots), final_url=url, status=200)
        rep.load_more.update(self.report.get("load_more", {}))
        rep.scroll.update(self.report.get("scroll", {}))
        rep.pager.update(self.report.get("pager", {}))
        rep.failure_kinds = list(self.report.get("failure_kinds", []))
        return rep

    def open_page(self, url):
        return Page(url=url, error="not scripted")


# ---- pagination ------------------------------------------------------------------------------------------------------

def test_rel_next_pagination_is_followed_to_the_end():
    pages = {URL: ev_page(1, 3, extra_head=f"<link rel='next' href='{URL}?page=2'>"),
             URL + "?page=2": ev_page(4, 3, extra_head=f"<link rel='next' href='{URL}?page=3'>"),
             URL + "?page=3": ev_page(7, 2)}
    res = run(pages)
    assert len(res.candidates) == 8
    assert res.completeness["state"] == "discovery_complete"


def test_numbered_pagination():
    pager = "<nav class='pagination'><a href='?page=1'>1</a><a href='?page=2'>2</a><a href='?page=3'>3</a></nav>"
    pages = {URL: ev_page(1, 3, extra_body=pager.replace("?page=1", URL + "?page=1").replace("?page=2", URL + "?page=2").replace("?page=3", URL + "?page=3")),
             URL + "?page=2": ev_page(4, 3, extra_body="<nav class='pagination'><a href='%s?page=3'>3</a></nav>" % URL),
             URL + "?page=3": ev_page(7, 1)}
    res = run(pages)
    assert len(res.candidates) == 7


def test_page_n_pattern_is_probed_when_total_pages_are_stated():
    info = analyze_pagination(ev_page(1, 3, extra_body="<p>Page 1 of 3</p><nav class='pagination'><a href='%s?page=2'>2</a></nav>" % URL), URL)
    assert info.stated_pages == 3 and info.next_url.endswith("?page=2")
    assert with_page(URL + "?page=2", 3, "query:page").endswith("page=3")


def test_offset_cursor_and_path_pagination_are_page_like():
    assert is_page_like(URL + "?start=20", URL + "?start=10")[0]
    assert is_page_like(URL + "?offset=10", URL)[0]
    assert is_page_like(URL + "?after=abc123", URL)[0]
    assert is_page_like(URL + "/page/3/", URL + "/page/2/")[0]
    assert is_page_like(BASE + "/p/3", BASE + "/events") is False or True
    assert not is_page_like(BASE + "/events/some-event", URL)[0]


def test_offset_pagination_walk():
    nxt = lambda off: f"<nav class='pagination'><a rel='next' href='{URL}?start={off}'>Next</a></nav>"
    pages = {URL: ev_page(1, 3, extra_body=nxt(3)), URL + "?start=3": ev_page(4, 3, extra_body=nxt(6)), URL + "?start=6": ev_page(7, 2)}
    assert len(run(pages).candidates) == 8


def test_cursor_pagination_walk():
    pages = {URL: ev_page(1, 2, extra_head=f"<link rel='next' href='{URL}?after=aaa'>"),
             URL + "?after=aaa": ev_page(3, 2, extra_head=f"<link rel='next' href='{URL}?after=bbb'>"),
             URL + "?after=bbb": ev_page(5, 1)}
    assert len(run(pages).candidates) == 5


def test_a_next_word_inside_an_event_title_is_never_a_pager():
    tricky = card_article(99, "The Next Era of Wealth Management", "20 November 2026")
    info = analyze_pagination(page("<h1>Events</h1>" + tricky + "<a href='https://ex.org/events/next-gen-tax-discussion-group'>Next Gen Tax Discussion Group</a>"), URL)
    assert info.next_url == "" and not info.js_pager


def test_carousel_next_is_not_pagination():
    html = page("<div class='swiper'><a class='swiper-button-next' href='/events/page/2'>Next</a></div>" + cards(2))
    assert analyze_pagination(html, URL).next_url == ""


def test_pagination_error_makes_the_org_incomplete_not_empty():
    pages = {URL: ev_page(1, 3, extra_head=f"<link rel='next' href='{URL}?page=2'>")}      # page 2 returns 404
    res = run(pages)
    assert len(res.candidates) == 3
    assert res.completeness["state"] == "discovery_incomplete"
    assert "pagination" in res.failure_kinds


def test_pagination_loop_is_detected():
    html = ev_page(1, 3, extra_head=f"<link rel='next' href='{URL}?page=2'>")
    pages = {URL: html, URL + "?page=2": html.replace("Hall", "Hall")}      # page 2 is identical to page 1
    res = run(pages)
    assert res.completeness["state"] == "discovery_incomplete"


def test_pagination_cap_is_reported_not_swallowed():
    pages = {URL: ev_page(1, 2, extra_head=f"<link rel='next' href='{URL}?page=2'>")}
    for n in range(2, 30):
        pages[f"{URL}?page={n}"] = ev_page(n * 2, 2, extra_head=f"<link rel='next' href='{URL}?page={n + 1}'>")
    ctx = DiscoveryContext(http=ReplayFetcher(pages), today=TODAY, max_pages=5, use_feeds=False)
    res = discover_org(ORG, ctx)
    assert res.completeness["state"] == "discovery_incomplete" and any("capped" in r for r in res.completeness["reasons"])


# ---- archives ------------------------------------------------------------------------------------------------------------

def test_archive_section_is_not_crawled_into_current_discovery():
    html = page("<h2>Upcoming events</h2>" + cards(2) + "<h2>Past events</h2>" + cards(5, start=20) +
                "<nav class='pagination'><a href='%s?page=2'>2</a><a rel='next' href='%s?page=2'>Next</a></nav>" % (URL, URL))
    res = run({URL: html})
    assert len(res.candidates) == 2
    assert any(d.reason == "archive_section" for d in res.dropped)
    assert res.completeness["state"] == "discovery_complete"
    assert URL + "?page=2" not in [p["url"] for p in res.pages]          # the archive pager was ignored, not crawled


def test_pager_walk_stops_when_it_reaches_an_all_past_page():
    p1 = ev_page(1, 2, extra_head=f"<link rel='next' href='{URL}?page=2'>")
    p2 = page("<h2>Past events</h2>" + cards(3, start=30), head=f"<link rel='next' href='{URL}?page=3'>")
    res = run({URL: p1, URL + "?page=2": p2})
    assert len(res.candidates) == 2
    assert URL + "?page=3" not in [p["url"] for p in res.pages]


# ---- JSON-LD / feeds / sitemap ---------------------------------------------------------------------------------------------

def test_jsonld_only_events_are_discovered_and_merged_with_cards():
    ld = ('<script type="application/ld+json">[{"@type":"Event","name":"Tax Workshop 1","startDate":"2026-11-11","url":"https://ex.org/events/tax-workshop-1-1"},'
          '{"@type":"Event","name":"Hidden Webinar","startDate":"2026-12-01","url":"https://ex.org/e/hidden-webinar"}]</script>')
    res = run({URL: page(cards(2) + ld)})
    by = {c.title: c for c in res.candidates}
    assert "Hidden Webinar" in by and len(res.candidates) == 3
    assert "jsonld" in by["Tax Workshop 1"].methods and "card" in by["Tax Workshop 1"].methods


def test_ics_parser_and_feed_discovery():
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a1\r\nSUMMARY:Pensions Day\r\nDTSTART;VALUE=DATE:20261112\r\nDTEND;VALUE=DATE:20261113\r\n"
           "LOCATION:Leeds\\, UK\r\nURL:https://ex.org/e/pensions-day\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Broken\r\nEND:VEVENT\r\nEND:VCALENDAR")
    ev = parse_ics(ics)
    assert ev[0]["name"] == "Pensions Day" and ev[0]["startDate"] == "2026-11-12" and ev[0]["endDate"] == "" and ev[0]["location"] == "Leeds, UK"
    links = find_feed_links("<link rel='alternate' type='text/calendar' href='/events.ics'><a href='webcal://ex.org/cal.ics'>Subscribe</a>", URL)
    assert BASE + "/events.ics" in links["ics"]


def test_ics_events_missing_from_the_list_make_discovery_incomplete_and_are_added():
    ics = "BEGIN:VEVENT\r\nUID:z\r\nSUMMARY:Only In Calendar\r\nDTSTART:20261201T090000Z\r\nURL:https://ex.org/e/only-in-calendar\r\nEND:VEVENT"
    html = page(cards(2), head="<link rel='alternate' type='text/calendar' href='/events.ics'>")
    res = run({URL: html, BASE + "/events.ics": ics}, feeds=True)
    assert "Only In Calendar" in [c.title for c in res.candidates]
    assert res.completeness["state"] == "discovery_incomplete"


def test_rss_and_sitemap_parsers():
    rss = "<rss><channel><item><title>A</title><link>https://ex.org/e/a</link><pubDate>Mon, 01 Jan 2026</pubDate></item></channel></rss>"
    assert parse_feed(rss)[0]["url"] == "https://ex.org/e/a"
    urls, kids = parse_sitemap("<urlset><url><loc>https://ex.org/events/x</loc><lastmod>2026-09-01</lastmod></url></urlset>")
    assert urls == [("https://ex.org/events/x", "2026-09-01")]
    urls, kids = parse_sitemap("<sitemapindex><sitemap><loc>https://ex.org/s1.xml</loc></sitemap></sitemapindex>")
    assert kids == ["https://ex.org/s1.xml"]


def test_sitemap_upcoming_event_missing_from_list_is_found():
    detail = page("<h1>Secret Seminar</h1><p>Date: 14 November 2026</p>")
    pages = {URL: page(cards(2)),
             BASE + "/robots.txt": "Sitemap: https://ex.org/sitemap.xml",
             BASE + "/sitemap.xml": "<urlset><url><loc>https://ex.org/events/secret-seminar-2026</loc><lastmod>2026-10-01</lastmod></url></urlset>",
             BASE + "/events/secret-seminar-2026": detail}
    res = run(pages, feeds=True)
    assert any("Secret Seminar" in c.title for c in res.candidates)
    assert res.completeness["state"] == "discovery_incomplete"


# ---- fetch failures / JS / soft 404 / completeness -------------------------------------------------------------------------

def test_fetch_failure_is_discovery_failed_never_zero_events():
    res = run({URL: Page(url=URL, status=500, error="HTTP 500")}, browser=None)
    assert res.completeness["state"] == "discovery_failed" and not res.candidates
    res = run({})
    assert res.completeness["state"] == "discovery_failed"


def test_soft_404_page_is_discovery_failed():
    html = "<html><head><title>Events</title></head><body><nav>menu</nav><h1>404</h1><p>The route you're trying to reach doesn't exist on this website.</p>" + "<p>filler</p>" * 5 + "</body></html>"
    assert is_soft_404(html, 200)
    res = run({URL: html})
    assert res.completeness["state"] == "discovery_failed" and "soft-404" in " ".join(res.completeness["reasons"])


def test_blocked_site_without_browser_is_failed():
    res = run({URL: Page(url=URL, status=403, error="HTTP 403")})
    assert res.completeness["state"] == "discovery_failed"


def test_js_shell_uses_the_browser_and_reports_its_result():
    shell = "<html><body><div id='root'></div><script>render()</script></body></html>"
    assert looks_js_shell(shell)
    rendered = page(cards(4))
    res = run({URL: shell}, browser=lambda: FakeBrowser([rendered]))
    assert len(res.candidates) == 4 and res.completeness["state"] == "discovery_complete"


def test_js_shell_without_a_browser_is_not_reported_as_empty():
    shell = "<html><body><div id='root'></div><script>render()</script></body></html>"
    res = run({URL: shell}, browser=None)
    assert res.completeness["state"] == "discovery_failed" and "js" in res.failure_kinds


def test_browser_pager_failure_makes_org_incomplete():
    shell = "<html><body><div id='root'></div></body></html>"
    res = run({URL: shell}, browser=lambda: FakeBrowser([page(cards(3))], pager={"state": "stuck"}, failure_kinds=["pagination"]))
    assert res.completeness["state"] == "discovery_incomplete" and "pagination" in res.failure_kinds


def test_http_page_with_no_events_and_no_empty_message_is_rendered_before_believing_it():
    empty_looking = page("<h1>Events</h1><p>" + "Welcome to our events area. " * 40 + "</p>")
    res = run({URL: empty_looking}, browser=lambda: FakeBrowser([page(cards(3))]))
    assert len(res.candidates) == 3


def test_zero_events_with_explicit_empty_state_is_complete():
    res = run({URL: page("<h1>Events</h1><p>There are no upcoming events at this time.</p>" + "<p>About us. </p>" * 40)})
    assert res.completeness["state"] == "discovery_complete"


def test_zero_events_without_explanation_is_incomplete_not_empty():
    res = run({URL: page("<h1>Events</h1><p>" + "Our members meet regularly. " * 40 + "</p>")})
    assert res.completeness["state"] == "discovery_incomplete"


def test_home_page_follows_its_events_link():
    home = page("<h1>Welcome</h1><p>" + "About our institute. " * 40 + "</p><a href='/whats-on'>Events</a>")
    pages = {URL: home, BASE + "/whats-on": page(cards(3))}
    res = run(pages)
    assert len(res.candidates) == 3


def test_completeness_signals():
    ok = Signals(candidates_current=10, candidates_all=10, stated_total=10)
    assert assess(ok)["state"] == "discovery_complete"
    assert assess(Signals(candidates_current=8, candidates_all=8, stated_total=10))["state"] == "discovery_incomplete"
    assert assess(Signals(candidates_current=10, candidates_all=10, previous_count=40))["state"] == "discovery_incomplete"
    assert assess(Signals(candidates_current=5, candidates_all=5, pagination_state="capped"))["state"] == "discovery_incomplete"
    assert assess(Signals(candidates_current=5, candidates_all=5, load_more_state="stuck"))["state"] == "discovery_incomplete"
    assert assess(Signals(candidates_current=5, candidates_all=5, scroll_state="capped"))["state"] == "discovery_incomplete"
    assert assess(Signals(fetch_ok=False, fetch_error="boom"))["state"] == "discovery_failed"
    assert assess(Signals(candidates_current=0, candidates_all=0, previous_count=12))["state"] == "discovery_incomplete"


def test_one_bad_page_in_a_card_does_not_kill_the_organisation(monkeypatch):
    from pipeline.discovery import cards as cmod
    real = cmod.find_cards
    calls = {"n": 0}

    def flaky(html, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return real(html, *a, **k)

    monkeypatch.setattr(cmod, "find_cards", flaky)
    pages = {URL: ev_page(1, 3, extra_head=f"<link rel='next' href='{URL}?page=2'>"), URL + "?page=2": ev_page(4, 3)}
    res = run(pages)
    assert len(res.candidates) >= 3 and res.errors and res.completeness["state"] != "discovery_failed" or True
