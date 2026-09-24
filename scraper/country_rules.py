"""Country inference from an event's own Location text.

Shared by the Master rebuild (weekly_full_run.py) and the verification
checks (verify_events.py), so both apply exactly the same rule.
"""

import re

# A handful of organizations run one global calendar but got entered into
# several different country tabs of the Sources workbook (e.g. the
# Association of Corporate Treasurers under both UK and UAE, ISDA under
# both UK and USA) -- each tab re-scrapes the SAME page, so the same event
# shows up once per tab it's listed under, tagged with whichever country
# that tab happens to be, regardless of where the event is actually
# happening. Separately, a genuinely single-tab org whose own calendar
# covers events worldwide (e.g. LCIA under India, listing a Beijing summit)
# gets every one of its events mislabeled with that one tab's country too.
# These hints let build_master() correct the Country field from the
# event's own Location text when it clearly disagrees, and only these --
# no full country-name-as-substring matching, which false-positives on
# things like "Indianapolis, IN" (contains "india") or "Dublin, OH".
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_COUNTRY_LOCATION_HINTS = {
    "USA": [r"\busa\b", r"\bunited states\b"] + [rf",\s*{s.lower()}\b" for s in _US_STATES],
    "UK": [r"\bunited kingdom\b", r"\buk\b", r"\blondon\b"],
    "India": [r"\bindia\b", r"\bmumbai\b", r"\bdelhi\b", r"\bbengaluru\b",
              r"\bbangalore\b", r"\bkolkata\b", r"\bchennai\b", r"\bhyderabad\b",
              r"\bpune\b"],
    "UAE": [r"\bdubai\b", r"\babu dhabi\b", r"\buae\b", r"\bunited arab emirates\b"],
    "AUS": [r"\baustralia\b", r"\bsydney\b", r"\bmelbourne\b", r"\bbrisbane\b", r"\bperth\b"],
    "SG": [r"\bsingapore\b"],
}
# A confident location signal that names a country we don't even track --
# not corrected to one of the 6 tracked labels (there's nothing to correct
# it TO), but not left silently mislabeled either: routed to Needs Review
# so a human decides whether it belongs on that country's list at all.
_UNTRACKED_COUNTRY_HINTS = [
    r"\bchina\b", r"\bbeijing\b", r"\bshanghai\b", r"\bhong kong\b",
    r"\bjapan\b", r"\btokyo\b", r"\bosaka\b", r"\bcanada\b", r"\btoronto\b",
    r"\bcalgary\b", r"\bgermany\b", r"\bfrance\b", r"\bparis\b",
    r"\bswitzerland\b", r"\bgeneva\b", r"\bzurich\b", r"\bireland\b",
    r"\bdublin,\s*ireland\b", r"\bnew zealand\b", r"\bsouth africa\b",
]


def _infer_country_from_location(location: str) -> str | None:
    """A tracked country label if `location` unambiguously names it, else
    None (including when it's blank, or names more than one -- ambiguous
    beats wrong)."""
    loc = (location or "").lower()
    if not loc:
        return None
    matches = {country for country, patterns in _COUNTRY_LOCATION_HINTS.items()
               if any(re.search(p, loc) for p in patterns)}
    return matches.pop() if len(matches) == 1 else None


def _location_names_untracked_country(location: str) -> bool:
    loc = (location or "").lower()
    return bool(loc) and any(re.search(p, loc) for p in _UNTRACKED_COUNTRY_HINTS)
