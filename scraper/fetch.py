"""Render a page with a headless browser, expanding 'load more' and
following basic pagination so events aren't missed past the first screen."""

from playwright.sync_api import sync_playwright

LOAD_MORE_TEXTS = ["load more", "show more", "view more", "more events", "load additional events"]
NEXT_PAGE_TEXTS = ["next", "next page", "older events", "»"]  # » = »


def fetch_html(url: str, wait_ms: int = 3000, max_pages: int = 3) -> list[str]:
    """Return HTML snapshots: the initial page, then up to `max_pages` - 1
    more page loads reached via 'load more' clicks or pagination links.
    Best-effort — failures to expand/paginate are swallowed, not raised."""
    htmls = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(ignore_https_errors=True)
        try:
            page.goto(url, wait_until="load", timeout=20000)
        except Exception:
            page.goto(url, wait_until="commit", timeout=20000)
        page.wait_for_timeout(wait_ms)

        # expand "load more" a couple of times if present
        for _ in range(2):
            clicked = False
            for text in LOAD_MORE_TEXTS:
                try:
                    page.get_by_text(text, exact=False).first.click(timeout=2000)
                    page.wait_for_timeout(1500)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                break

        htmls.append(page.content())

        # follow numbered/next pagination for a couple more pages
        while len(htmls) < max_pages:
            navigated = False
            try:
                next_link = page.query_selector("a[rel='next']")
                if next_link:
                    next_link.click(timeout=3000)
                    page.wait_for_timeout(wait_ms)
                    htmls.append(page.content())
                    navigated = True
            except Exception:
                pass

            if not navigated:
                for text in NEXT_PAGE_TEXTS:
                    try:
                        page.get_by_text(text, exact=False).first.click(timeout=2000)
                        page.wait_for_timeout(wait_ms)
                        htmls.append(page.content())
                        navigated = True
                        break
                    except Exception:
                        continue

            if not navigated:
                break

        browser.close()
    return htmls
