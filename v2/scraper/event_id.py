"""A fixed ID per event, so the same event is recognised week to week even
when its title, date or wording changes on the site.

The event's own page URL is the most stable identity. When an event has no
page of its own (its "link" is just the listing page), fall back to the
organizer + name.
"""

import hashlib
import re
from urllib.parse import urlsplit

from scraper.evidence import normalize_ws


def _normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")


def make_event_id(organizer: str, name: str, link: str = "", source_url: str = "", when: str = "") -> str:
    """`when` (the event's date) only matters for an event with no page of its
    own: the same name can then legitimately recur on several dates."""
    own_page = link and _normalize_url(link) != _normalize_url(source_url)
    if own_page:
        basis = "url|" + _normalize_url(link)
    else:
        basis = ("name|" + normalize_ws(organizer).lower() + "|" + re.sub(r"\W+", " ", normalize_ws(name).lower()).strip()
                 + ("|" + normalize_ws(when) if when else ""))
    return "ev_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
