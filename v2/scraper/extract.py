"""Strip a rendered page down to readable text before handing it to Gemini.

Does NOT use trafilatura's content-selection heuristics. trafilatura is
tuned for news/blog articles and proved unreliable on event-listing pages in
two different, unpredictable ways: on one site it kept only the cookie-consent
banner and discarded every event; on another (a table-based listing) it kept
the table but silently dropped just the Title column's link text, producing
events with real dates/locations but no names. Neither failure is reliably
detectable after the fact (the second one still "looks" like reasonable
output — right length, right structure, just missing one field), so rather
than keep patching heuristics to catch each new failure mode, we skip
trafilatura entirely and always send the raw visible DOM text instead.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

CHROME_ID_CLASS = re.compile(
    r"cookiebot|cybot|onetrust|cookie-consent|cookie-banner|gdpr|userway|accessibility-widget",
    re.I,
)


def _valid_href(href: str) -> bool:
    href = (href or "").strip()
    return bool(href) and not href.startswith(("#", "javascript:", "mailto:", "tel:"))


def extract_text(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "iframe", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(id=CHROME_ID_CLASS):
        tag.decompose()
    for tag in soup.find_all(class_=CHROME_ID_CLASS):
        tag.decompose()

    # get_text() alone discards every href — Gemini would have no way to
    # recover each event's actual registration link and would fall back to
    # the org's generic events-page URL for nearly every row. Inline each
    # link's absolute URL next to its text (markdown-link style) so the URL
    # survives into the plain text Gemini reads.
    for a in soup.find_all("a", href=True):
        if _valid_href(a["href"]):
            absolute = urljoin(url, a["href"].strip())
            a.replace_with(f"[{a.get_text(strip=True)}]({absolute})")

    return soup.get_text("\n", strip=True)
