import sys,re
slug,page,start,end=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4] if len(sys.argv)>4 else None
t=open(f"baseline/raw/{slug}/page_{int(page):02d}.txt",encoding="utf-8").read()
i=t.find(start); i=0 if i<0 else i
j=t.find(end,i+1) if end else -1
print(t[i:(j if j>0 else len(t))])
