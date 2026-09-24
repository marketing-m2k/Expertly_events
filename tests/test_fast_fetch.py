"""Tests for the fast-fetch decisions: plain HTTP when safe, browser when the
page pages / needs JavaScript / is blocked. No network is used."""

from scraper.fast_fetch import (
    fetch_detail_fast, fetch_list_pages, has_more_pages_hint, http_get_many, looks_js_shell,
)

DATES = " ".join(f"<li>Event {i}: Sep {i + 10} 2026</li>" for i in range(5))
GOOD_LIST = f"<html><body><main><ul>{DATES}</ul>" + "<p>filler text about events and calendars</p>" * 30 + "</main></body></html>"


def test_empty_javascript_shell_is_detected():
    assert looks_js_shell("<html><body><div id='app'></div><script>x</script></body></html>")
    assert not looks_js_shell(GOOD_LIST)


def test_pagination_and_load_more_are_detected():
    assert has_more_pages_hint('<a rel="next" href="?page=2">Next</a>')
    assert has_more_pages_hint("<button>Load more events</button>")
    assert has_more_pages_hint('<a href="/events/page/2/">2</a>')
    assert not has_more_pages_hint(GOOD_LIST)


def test_a_single_finished_list_page_is_read_over_plain_http():
    calls = []
    out = fetch_list_pages("https://x.org/events", http=lambda u: {"html": GOOD_LIST, "status": 200, "error": None},
                           browser=lambda u: calls.append(u) or ["BROWSER"])
    assert out == [GOOD_LIST] and not calls


def test_a_list_that_pages_goes_to_the_browser_so_no_event_is_missed():
    paged = GOOD_LIST + '<a rel="next" href="?page=2">Next</a>'
    out = fetch_list_pages("https://x.org/events", http=lambda u: {"html": paged, "status": 200, "error": None},
                           browser=lambda u: ["BROWSER"])
    assert out == ["BROWSER"]


def test_blocked_or_shell_or_non_list_pages_go_to_the_browser():
    for page in ({"html": None, "status": 403, "error": "HTTP 403"},
                 {"html": "<html><body><div id='app'></div></body></html>", "status": 200, "error": None},
                 {"html": "<html><body>" + "<p>About our company and its long history</p>" * 60 + "</body></html>",
                  "status": 200, "error": None}):
        assert fetch_list_pages("https://x.org/", http=lambda u, p=page: p, browser=lambda u: ["BROWSER"]) == ["BROWSER"]


def test_page_with_no_events_found_goes_to_the_browser():
    out = fetch_list_pages("https://x.org/events", http=lambda u: {"html": GOOD_LIST, "status": 200, "error": None},
                           browser=lambda u: ["BROWSER"], has_events=lambda h: False)
    assert out == ["BROWSER"]


def test_detail_pages_use_the_browser_only_for_the_ones_http_could_not_read():
    seen_browser = []

    def http_many(urls, workers, delay_s):
        return {"https://a.org/1": {"html": GOOD_LIST, "status": 200, "error": None},
                "https://a.org/2": {"html": None, "status": 403, "error": "HTTP 403"},
                "https://a.org/3": {"html": None, "status": 404, "error": "HTTP 404"},
                "https://a.org/4": {"html": "<html><body><div id='r'></div></body></html>", "status": 200, "error": None}}

    def browser_many(urls, wait_ms, workers, delay_s):
        seen_browser.extend(urls)
        return {u: {"html": GOOD_LIST, "status": 200, "error": None} for u in urls}

    out = fetch_detail_fast(["https://a.org/1", "https://a.org/2", "https://a.org/3", "https://a.org/4"],
                            http_many=http_many, browser_many=browser_many)
    assert sorted(seen_browser) == ["https://a.org/2", "https://a.org/4"]  # a 404 is final, a good page needs no browser
    assert out["https://a.org/3"]["status"] == 404 and out["https://a.org/1"]["html"] == GOOD_LIST


def test_a_retry_goes_straight_to_the_browser():
    out = fetch_detail_fast(["https://a.org/1"], wait_ms=3000,
                            http_many=lambda *a, **k: (_ for _ in ()).throw(AssertionError("http must not be used")),
                            browser_many=lambda urls, wait_ms, workers, delay_s: {u: {"html": "B", "status": 200, "error": None} for u in urls})
    assert out["https://a.org/1"]["html"] == "B"


def test_one_request_at_a_time_per_website():
    import threading
    active, worst = {}, {"n": 0}
    lock = threading.Lock()

    def get(url):
        host = url.split("/")[2]
        with lock:
            active[host] = active.get(host, 0) + 1
            worst["n"] = max(worst["n"], active[host])
        import time
        time.sleep(0.02)
        with lock:
            active[host] -= 1
        return {"html": "x", "status": 200, "error": None}

    urls = [f"https://{h}.org/{i}" for h in ("a", "b", "c") for i in range(4)]
    out = http_get_many(urls, workers=6, delay_s=0.0, get=get)
    assert len(out) == 12 and worst["n"] == 1
