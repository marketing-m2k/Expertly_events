"""Quick structural profile of listing URLs (HTTP only): used to pick a varied baseline set."""
import json, re, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from scraper.fast_fetch import http_get, looks_js_shell, has_more_pages_hint

URLS = sys.argv[1:]

def prof(u):
    r = http_get(u)
    h = r["html"] or ""
    return dict(url=u, status=r["status"], err=r["error"], size=len(h), shell=looks_js_shell(h) if h else None,
                jsonld_event=len(re.findall(r'"@type"\s*:\s*"[A-Za-z]*Event"', h)),
                more=has_more_pages_hint(h), ics=bool(re.search(r"\.ics|ical|webcal", h, re.I)),
                loadmore=bool(re.search(r"load\s*more|show\s*more", h, re.I)),
                aspx=bool(re.search(r"__VIEWSTATE", h)), evlinks=len(set(re.findall(r'href="([^"]*event[^"]*)"', h, re.I))))
with ThreadPoolExecutor(8) as ex:
    for p in ex.map(prof, URLS):
        print(json.dumps(p))
