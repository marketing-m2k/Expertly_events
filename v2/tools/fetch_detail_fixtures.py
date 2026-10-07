"""Save real event detail pages as test fixtures (HTTP first, browser if needed)."""
import sys, os, json
sys.path.insert(0, ".")
from pipeline.fetcher import HttpFetcher, looks_js_shell
from pipeline.discovery.browser import BrowserSession
PAGES = {
 "arita_vic": "https://www.arita.com.au/Web/Web/Events/Event_Display.aspx?EventKey=3VICCON26",
 "tei_annual": "https://www.tei.org/events-education/events/2026-annual-conference",
 "tei_chapter": "https://www.tei.org/events-education/events/chicago-chapter-season-pass",
 "icaew_digital": "https://www.icaew.com/learning-and-development/events/digital-assets-conference",
 "taxinst_gst": "https://www.taxinstitute.com.au/events/conferences-forums/national-gst",
 "taxinst_sa": "https://www.taxinstitute.com.au/events/conferences-forums/SA-Tax-Forum",
 "cfa_vips": "https://cfasocietyindia.org/events/10th-value-investing-pioneers-summit-delhi/",
 "ciot_conf": "https://www.tax.org.uk/conservative-party-conference-2026",
 "fta_hub": "https://hub.taxadmin.org/e/2026-tobacco-basic-training",
 "isca_conf": "https://www.isca.org.sg/member-support/events/isca-conference-2026",
}
http = HttpFetcher()
for k, u in PAGES.items():
    p = http.get(u)
    how = "http"
    if not p.ok or looks_js_shell(p.html):
        with BrowserSession() as br:
            p = br.open_page(u)
        how = "browser"
    path = f"tests/fixtures/live/{k}.html"
    if p.html:
        open(path, "w", encoding="utf-8").write(p.html)
    print(k, how, p.status, p.error, len(p.html or ""))
    json.dump({"url": u, "final_url": p.final_url or u, "how": how}, open(f"tests/fixtures/live/{k}.json", "w"))
