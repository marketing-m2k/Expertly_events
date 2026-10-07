import sys, json, os
sys.path.insert(0,".")
from pipeline.discovery.cards import find_cards
slug=sys.argv[1]
b=json.load(open(f"baseline/{slug}.json",encoding="utf-8"))
tot=0
for n in b.get("ideal_pages",["page_01.html"]):
    html=open(f"baseline/raw/{slug}/{n}",encoding="utf-8").read()
    cards,drops,stats=find_cards(html,b["url"])
    print(slug,n,"cards",len(cards),"archive",sum(c.in_archive for c in cards),stats, "drops",len(drops))
    for c in cards[:int(sys.argv[2]) if len(sys.argv)>2 else 60]:
        print("  ",("A" if c.in_archive else " "),c.title_confidence[0],c.methods[0][:4], c.title[:60].ljust(60), "|", c.date_hint[:22].ljust(22),"|", c.url[-50:], "|", c.section[:25])
    for d in drops[:8]: print("  DROP",d.reason,d.url[-50:],d.title[:30],d.detail[:60])
