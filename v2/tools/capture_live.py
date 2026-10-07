"""Independent ground-truth capture for the baseline.

Deliberately NOT part of the discovery code under test: it renders the listing
in a real browser, expands everything it can (load more / scroll / next pages)
with generous patience, and dumps rendered HTML + visible text + link list so a
human (or a reviewing model) can read the page and write down the TRUE event
list. The output lives in baseline/raw/<slug>/ and is only an aid for writing
baseline/<slug>.json; it is never read by the pipeline.

    python tools/capture_live.py <slug> <url> [--max-pages 30]
"""

import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


def snapshot(page, out_dir: str, n: int, label: str):
    html = page.content()
    text = page.evaluate("document.body ? document.body.innerText : ''")
    links = page.evaluate("Array.from(document.querySelectorAll('a[href]')).map(a=>[(a.innerText||'').trim().replace(/\\s+/g,' ').slice(0,140), a.href])")
    with open(os.path.join(out_dir, f"page_{n:02d}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(out_dir, f"page_{n:02d}.txt"), "w", encoding="utf-8") as f:
        f.write(f"# {label}\n# url: {page.url}\n\n{text}")
    with open(os.path.join(out_dir, f"page_{n:02d}.links.json"), "w", encoding="utf-8") as f:
        json.dump(links, f, indent=0)
    return html, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--wait", type=int, default=3500)
    args = ap.parse_args()
    out_dir = os.path.join("baseline", "raw", args.slug)
    os.makedirs(out_dir, exist_ok=True)
    notes = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900}, ignore_https_errors=True)
        try:
            page.goto(args.url, wait_until="load", timeout=45000)
        except Exception as exc:
            notes.append(f"goto: {exc}")
            try:
                page.goto(args.url, wait_until="commit", timeout=45000)
            except Exception as exc2:
                notes.append(f"goto2: {exc2}")
        page.wait_for_timeout(args.wait)

        def expand():
            for rnd in range(40):
                grew = False
                before = page.evaluate("document.body.scrollHeight")
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(700)
                if page.evaluate("document.body.scrollHeight") > before:
                    grew = True
                for text in ("load more", "show more", "view more", "see more", "more events"):
                    loc = page.get_by_text(re.compile(rf"^\s*{text}", re.I)).first
                    try:
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=2500)
                            page.wait_for_timeout(1500)
                            grew = True
                            notes.append(f"clicked '{text}'")
                            break
                    except Exception:
                        pass
                if not grew:
                    break

        expand()
        html, text = snapshot(page, out_dir, 1, "first page (expanded)")
        n = 1
        seen = {hash(text)}
        while n < args.max_pages:
            clicked = False
            for sel in ("a[rel=next]", "a:has-text('Next')", "a[aria-label*='next' i]", "li.next a", "a.next", "button:has-text('Next')",
                        "a:text-is('›')", "a:text-is('»')", "a:text-is('>')"):
                loc = page.locator(sel).first
                try:
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=3000)
                        page.wait_for_timeout(args.wait)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break
            expand()
            html, text = snapshot(page, out_dir, n + 1, f"after next click {n}")
            if hash(text) in seen:
                notes.append("next click did not change the page; stopping")
                break
            seen.add(hash(text))
            n += 1
        notes.append(f"pages captured: {n}")
        browser.close()
    with open(os.path.join(out_dir, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(notes))
    print(args.slug, "->", out_dir, "|", "; ".join(notes[-4:]))


if __name__ == "__main__":
    main()
