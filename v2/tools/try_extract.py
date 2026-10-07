import sys, json
sys.path.insert(0, ".")
from pipeline.extraction.detail import extract_event
DATE_HINTS = {"ciot_conf": "Tuesday 6 October 2026"}
names = sys.argv[1:] or ["tei_annual","tei_chapter","icaew_digital","taxinst_gst","taxinst_sa","cfa_vips","ciot_conf","fta_hub","isca_conf"]
for n in names:
    meta = json.load(open(f"tests/fixtures/live/{n}.json"))
    html = open(f"tests/fixtures/live/{n}.html", encoding="utf-8").read()
    ev = extract_event(html, meta["url"], final_url=meta["final_url"], org_name="Org", card_date_hint=DATE_HINTS.get(n, ""), today=__import__("datetime").date(2026,10,7))
    print("=" * 100); print(n, meta["url"], "| kind:", ev.page_kind, "| readable:", ev.readable, ev.unreadable_reason)
    c = ev.ctx
    print("  h1:", c.h1[:80], "| jsonld:", bool(c.jsonld), "foreign:", len(c.jsonld_foreign), "| pairs:", [(p.canon, p.value[:30], "C" if p.contact else "") for p in c.pairs][:10], "| excluded blocks:", len(c.excluded_texts))
    for k, v in ev.fields.items():
        if k in ("description",): print(f"  {k:16} [{v.confidence[:1]}/{v.verification[:4]}] {v.source:11} {(v.value or '')[:110]!r}  {v.note[:90]}")
        else: print(f"  {k:16} [{v.confidence[:1]}/{v.verification[:4]}] {v.source:11} {(v.value or '')[:80]!r}  {v.note[:90]}")
