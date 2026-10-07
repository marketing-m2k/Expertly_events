"""Shared test helpers: tiny HTML builders and a local HTTP site for browser tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TODAY_ISO = "2026-10-07"


FILLER = ("<p class='intro'>" + "The Example Institute supports professionals in tax, finance and law through training and networking. " * 5 + "</p>")


def page(body: str, title: str = "Events", head: str = "", filler: bool = True) -> str:
    return (f"<!doctype html><html><head><title>{title}</title>{head}</head><body>"
            f"<nav><a href='/'>Home</a><a href='/about'>About us</a><a href='/news'>News</a><a href='/contact'>Contact</a></nav>"
            f"<main>{FILLER if filler else ''}{body}</main><footer><a href='/privacy'>Privacy</a><p>Copyright 2026</p></footer></body></html>")


def card_article(i: int, title: str | None = None, date: str | None = None, base: str = "https://ex.org", cls: str = "event-card") -> str:
    title = title or f"Tax Workshop {i}"
    date = date or f"{10 + i} November 2026"
    return (f"<article class='{cls}'><h3><a href='{base}/events/{title.lower().replace(' ', '-')}-{i}'>{title}</a></h3>"
            f"<p class='date'>{date}</p><p>Venue: Hall {i}</p></article>")


def cards(n: int, start: int = 1, base: str = "https://ex.org") -> str:
    return "\n".join(card_article(i, base=base) for i in range(start, start + n))


# ---- local site for browser tests ------------------------------------------------------------------------------------

def _ev(i: int, base: str) -> str:
    return card_article(i, f"Finance Seminar {i}", f"{(i % 27) + 1} December 2026", base)


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, *a):  # noqa: D401 - silence
        pass

    def do_GET(self):
        p = urlsplit(self.path)
        qs = parse_qs(p.query)
        base = f"http://127.0.0.1:{self.server.server_port}"
        status, ctype, body = 200, "text/html; charset=utf-8", ""
        path = p.path
        if path == "/api/events":
            off, n, total = int(qs.get("offset", ["0"])[0]), int(qs.get("n", ["5"])[0]), int(qs.get("total", ["14"])[0])
            items = [_ev(i, base) for i in range(off + 1, min(off + n, total) + 1)]
            body, ctype = json.dumps({"html": "".join(items), "more": off + n < total}), "application/json"
        elif path == "/loadmore":
            body = page(f"""<h1>Events</h1><div id='list'>{''.join(_ev(i, base) for i in range(1, 6))}</div>
            <button id='more' class='load-more'>Load more events</button>
            <script>let off=5; const b=document.getElementById('more');
            b.onclick=async()=>{{const r=await fetch('/api/events?offset='+off+'&n=5&total=14');const j=await r.json();
            document.getElementById('list').insertAdjacentHTML('beforeend', j.html); off+=5; if(!j.more) b.remove();}};</script>""")
        elif path == "/loadmore_stuck":
            body = page(f"<h1>Events</h1><div id='list'>{''.join(_ev(i, base) for i in range(1, 4))}</div><button class='load-more'>Load more</button>")
        elif path == "/infinite":
            body = page(f"""<h1>Events</h1><div id='list'>{''.join(_ev(i, base) for i in range(1, 6))}</div>
            <script>let off=5, busy=false, done=false;
            window.addEventListener('scroll', async()=>{{ if(busy||done) return;
              if(window.innerHeight+window.scrollY >= document.body.scrollHeight-200){{ busy=true;
                const r=await fetch('/api/events?offset='+off+'&n=5&total=17'); const j=await r.json();
                document.getElementById('list').insertAdjacentHTML('beforeend', j.html); off+=5; done=!j.more; busy=false; }} }});</script>""")
        elif path == "/endless":
            body = page(f"""<h1>Events</h1><div id='list'>{''.join(_ev(i, base) for i in range(1, 4))}</div>
            <script>let off=3; window.addEventListener('scroll', async()=>{{ if(window.innerHeight+window.scrollY >= document.body.scrollHeight-200){{
              const r=await fetch('/api/events?offset='+off+'&n=5&total=100000'); const j=await r.json();
              document.getElementById('list').insertAdjacentHTML('beforeend', j.html); off+=5; }} }});</script>""")
        elif path == "/jspager":
            pages = {1: range(1, 4), 2: range(4, 7), 3: range(7, 9)}
            tricky = f"<article class='event-card'><h3><a href='{base}/events/next-gen-tax-discussion-group-99'>Next Gen Tax Discussion Group</a></h3><p>5 January 2027</p></article>"
            body = page(f"""<h1>Events</h1><div id='list'>{''.join(_ev(i, base) for i in pages[1])}{tricky}</div>
            <div class='pagination'><button class='prev' disabled>Previous</button><span id='cur'>1</span>
            <button class='next' id='nextbtn'>Next</button></div>
            <script>const data={json.dumps({k: ''.join(_ev(i, base) for i in v) for k, v in pages.items()})}; let cur=1;
            document.getElementById('nextbtn').onclick=()=>{{ cur++; document.getElementById('list').innerHTML=data[cur];
              document.getElementById('cur').textContent=cur; if(cur>=3) document.getElementById('nextbtn').disabled=true; }};</script>""")
        elif path == "/jspager_stuck":
            body = page(f"<h1>Events</h1><div id='list'>{''.join(_ev(i, base) for i in range(1, 4))}</div>"
                        "<div class='pagination'><button class='next'>Next</button></div>")
        elif path == "/shell":
            body = ("<!doctype html><html><head><title>Events</title></head><body><div id='root'></div>"
                    f"<script>setTimeout(()=>{{document.getElementById('root').innerHTML=`<main><h1>Events</h1>{''.join(_ev(i, base) for i in range(1, 5))}</main>`}}, 300);</script></body></html>")
        elif path == "/pages/1" or path == "/pages/2" or path == "/pages/3":
            n = int(path.rsplit("/", 1)[1])
            nxt = f"<link rel='next' href='{base}/pages/{n + 1}'>" if n < 3 else ""
            body = page("<h1>Events</h1>" + "".join(_ev(i, base) for i in range((n - 1) * 3 + 1, n * 3 + 1)), head=nxt)
        else:
            status, body = 404, page("<h1>Not found</h1>")
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LocalSite:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"
