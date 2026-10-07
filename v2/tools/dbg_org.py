import sys, json
sys.path.insert(0, ".")
from datetime import date
from pipeline.discovery.browser import BrowserSession
from pipeline.discovery.orchestrator import DiscoveryContext, discover_org
from pipeline.fetcher import HttpFetcher
url, country = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
ctx = DiscoveryContext(http=HttpFetcher(), browser_factory=lambda: BrowserSession(), today=date(2026, 10, 7), use_feeds=("--nofeeds" not in sys.argv))
res = discover_org({"organizer": "X", "country": country, "url": url}, ctx)
print("STATE", res.completeness["state"], res.completeness["reasons"])
print("FAIL", res.failure_kinds, "ERR", res.errors)
for p in res.pages: print("PAGE", {k: v for k, v in p.items() if k != "report"}, (p.get("report") or {}).get("notes"), {k: (p.get("report") or {}).get(k) for k in ("load_more", "scroll", "pager")} if p.get("report") else "")
print("CANDS", len(res.candidates))
for c in res.candidates[:int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 15]: print("  ", c.methods, c.title[:60], "|", c.date_hint, "|", c.url[-60:])
from collections import Counter
print("DROPS", Counter(d.reason for d in res.dropped))
