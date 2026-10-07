"""Event identity and duplicate detection.

v1 de-duplicated Master by (name, date) alone, so "Tax Update - Chicago" and
"Tax Update - Dallas" on the same day collapsed into one row. The hierarchy here:

    1. the official event URL (normalised)                         -- exact
    2. a stable external id (JSON-LD @id, ICS UID)                 -- exact, same organisation
    3. normalised name + date + location                           -- exact on all three
    4. same organisation + name + date, location missing on a side -- NEVER merged: flagged 'possible duplicate'

A URL shared by several events of one run (a series / course page) is not an identity:
those events fall through to 3.
"""

from __future__ import annotations

import re

from pipeline.common import norm_text, norm_url, sha

_CITYISH = re.compile(r"\b(?:level|floor|suite|room|hall|centre|center|hotel|building|tower|street|st|road|rd|avenue|ave)\b", re.I)


def location_key(location: str) -> str:
    """Normalised location for identity: lower-case words with the building/room noise removed."""
    t = norm_text(location)
    return " ".join(w for w in t.split() if not _CITYISH.fullmatch(w) and not w.isdigit())[:80]


def name_key(name: str) -> str:
    return norm_text(name)


def make_event_id(org: str, url: str, *, shared_url: bool, name: str, start_date: str, location: str, ext_id: str = "") -> str:
    if url and not shared_url:
        return "ev_" + sha("url|" + norm_url(url), 12)
    if ext_id:
        return "ev_" + sha(f"ext|{norm_text(org)}|{ext_id}", 12)
    return "ev_" + sha(f"nal|{norm_text(org)}|{name_key(name)}|{start_date}|{location_key(location)}", 12)


def identity_keys(org: str, url: str, shared_url: bool, name: str, start_date: str, location: str, ext_id: str = "") -> dict:
    return {"url_key": norm_url(url) if url and not shared_url else "",
            "ext_key": f"{norm_text(org)}|{ext_id}" if ext_id else "",
            "nal_key": f"{norm_text(org)}|{name_key(name)}|{start_date}|{location_key(location)}" if name and start_date else "",
            "nad_key": f"{norm_text(org)}|{name_key(name)}|{start_date}" if name and start_date else ""}
