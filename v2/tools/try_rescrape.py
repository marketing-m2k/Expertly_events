import sys, json
sys.path.insert(0, ".")
from datetime import date
from pipeline.extraction.detail import extract_event
from pipeline.quality import assess_quality
from pipeline.rescrape import rescrape
from pipeline.fetcher import HttpFetcher
from pipeline.discovery.browser import BrowserSession
TODAY = date(2026, 10, 7)
for n in sys.argv[1:]:
    meta = json.load(open(f"tests/fixtures/live/{n}.json")); html = open(f"tests/fixtures/live/{n}.html", encoding="utf-8").read()
    ev = extract_event(html, meta["url"], final_url=meta["final_url"], org_name="Org", today=TODAY)
    q = assess_quality(ev, today=TODAY)
    print("==", n, "flags before:", q.flags)
    http = HttpFetcher()
    def browser(url):
        with BrowserSession() as br:
            return br.open_page(url)
    ev, q = rescrape(ev, q, fetch_http=http.get, fetch_browser=browser, today=TODAY)
    print("   flags after:", q.flags)
    for a in ev.attempts: print("   ", a["field"], a["n"], a["method"], a["extractor"], a["outcome"], a["value"][:50], a["note"][:70])
    for k in ("name", "start_date", "end_date", "venue", "address", "format"):
        v = ev.fields[k]; print("   ", k, repr(v.value)[:70], v.source, v.confidence)
