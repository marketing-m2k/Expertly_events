"""Shared shape for an extracted value that carries its own proof.

Every value the new extraction layer produces is a dict:
    value       what goes in the column
    match       the literal text that must appear on the page for this value
                to be trusted (verification re-checks it against the page)
    reason      one line saying why this value belongs in this column
    source      structured_data | microdata | label | time_tag | heading | meta | paragraph
    confidence  high | medium | low   (low never goes straight to Master)
"""

import re


def field(value: str, match: str, reason: str, source: str, confidence: str = "high") -> dict:
    return {"value": value, "match": match, "reason": reason, "source": source, "confidence": confidence}


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def appears_on_page(match: str, page_text: str) -> bool:
    """True if `match` occurs in `page_text`, ignoring whitespace and case.
    This is the check that stops a value the page never stated from being
    saved -- used by verification, and by the AI review to reject a quote
    that isn't really on the page."""
    needle = normalize_ws(match).lower()
    return bool(needle) and needle in normalize_ws(page_text).lower()
