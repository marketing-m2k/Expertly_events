import sys, json
sys.path.insert(0, ".")
from pipeline.discovery.cards import find_cards
from measure.match import load_baselines, match_org
rows=[]
for b in load_baselines("baseline", sys.argv[1:] or None):
    cands=[]
    for n in b.get("ideal_pages",["page_01.html"]):
        html=open(f"baseline/raw/{b['slug']}/{n}",encoding="utf-8").read()
        cards,drops,stats=find_cards(html,b["url"])
        for c in cards:
            if c.in_archive: continue
            cands.append({"title":c.title,"url":c.url,"date_hint":c.date_hint,"card_text":c.card_text})
    m=match_org(b,{"candidates":cands,"state":"x"})
    print(f"{b['slug']:22} true={m.true_count:3} found={m.discovered_count:3} matched={m.matched_valid:3} recall={m.recall} miss={len(m.missed)} dup={len(m.duplicates)} extra={len(m.extras)}")
    for x in m.missed[:6]: print("     MISS",x["title"][:70],x["date_text"])
    for x in m.extras[:6]: print("     EXTRA",x["title"][:60],"|",x["date_hint"],"|",x["url"][-45:])
    for x in m.duplicates[:3]: print("     DUP",x["title"][:60])
