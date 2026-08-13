"""Strip a rendered page down to readable text before handing it to Claude."""

import trafilatura


def extract_text(html: str, url: str) -> str:
    text = trafilatura.extract(html, url=url, include_links=True, include_tables=True)
    return text or ""
