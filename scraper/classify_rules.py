"""Rule-based Tax / Finance / Legal classification -- no AI, no cost.

Returns one of: Tax, Finance, Legal (accepted), Reject (definitely not a
professional Tax/Finance/Legal event) or Unclear (goes to Needs Review, where
the Gemini review decides). The category comes from the event's OWN title and
description, never from the hosting organisation's general category.
"""

import re

_REJECT_PATTERNS = {
    "social event": r"\b(happy hour|mixer|golf|holiday party|christmas party|reception|social hour|cocktail|"
                    r"gala|networking (lunch|drinks|breakfast|evening)|yoga|wellness|fun run|5k)\b",
    "exam prep": r"\b(exam (review|prep|preparation|training|bootcamp)|pre[- ]exam|pass (the|your) .{0,20}exam|"
                 r"examiner (school|training)|mock exam|exam registration|exam enrol+ment|study skills)\b",
    "corporate notice": r"\b(annual general meeting|\bagm\b|dividend (declaration|announcement|payment|record)|board meeting|"
                        r"intimation|record date|"
                        r"results? announcement|postal ballot)\b",
    "newsletter/publication": r"\b(newsletter|quarterly issue|journal issue|magazine issue|e-?bulletin)\b",
    "college event": r"\b(alumni|campus recruit|college (event|fair)|actec)\b",
}

_CATEGORY_KEYWORDS = {
    "Tax": [r"\btax", r"\bgst\b", r"\bvat\b", r"\bcustoms\b", r"transfer pricing", r"\btds\b", r"\bbeps\b",
            r"pillar (two|2)", r"withholding", r"\bexcise\b", r"\bfbt\b", r"capital gains"],
    "Finance": [r"\bfinance\b", r"\bfinancial\b", r"\baudit", r"\baccounting\b", r"\bifrs\b", r"\bind as\b",
                r"\bgaap\b", r"\bbanking\b", r"\btreasury\b", r"capital markets?", r"\bvaluation\b",
                r"\binvestment", r"wealth", r"\binsurance\b", r"\bactuar", r"\bfintech\b", r"\bcredit\b",
                r"\blending\b", r"\bsebi\b", r"superannuation", r"\bsmsf\b", r"\bcfo\b"],
    "Legal": [r"\blaw\b", r"\blegal\b", r"arbitration", r"litigation", r"\bcounsel\b", r"\bcourts?\b",
              r"\bcompliance\b", r"\bregulat", r"insolvency", r"bankruptcy", r"\bcontracts?\b", r"\bdisputes?\b",
              r"corporate governance", r"\bjudicia", r"mediation", r"estate planning", r"\baml\b", r"\bfiduciary\b"],
}


def _hits(text: str, patterns: list[str]) -> list[str]:
    found = []
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            found.append(m.group(0).strip().lower())
    return found


def classify_event(name: str, description: str = "") -> dict:
    name, description = name or "", description or ""

    for reason, pattern in _REJECT_PATTERNS.items():
        haystack = f"{name} {description}" if reason == "exam prep" else name
        m = re.search(pattern, haystack, re.I)
        if m:
            return {"category": "Reject", "reason": f"{reason}: '{m.group(0).strip()}' in the event text"}

    scores, evidence = {}, {}
    for category, patterns in _CATEGORY_KEYWORDS.items():
        in_name = _hits(name, patterns)
        in_desc = [h for h in _hits(description, patterns) if h not in in_name]
        scores[category] = 3 * len(in_name) + len(in_desc)
        evidence[category] = in_name + in_desc

    # highest score wins; a tie between categories (e.g. "GST Audit Workshop"
    # is both Tax and Finance) is still clearly on-topic, so it is settled by
    # a fixed order rather than sent for review -- which of the three labels
    # it gets matters far less than whether it is one of them at all.
    order = ("Tax", "Legal", "Finance")
    top_score = max(scores.values())
    if top_score == 0:
        return {"category": "Unclear", "reason": "no Tax, Finance or Legal keywords in the event's title or description"}
    tied = [c for c in order if scores[c] == top_score]
    top = tied[0]
    reason = f"keywords in the event's own text: {', '.join(evidence[top])}"
    if len(tied) > 1:
        reason += f" (also matches {' and '.join(tied[1:])}; {top} chosen by fixed order)"
    return {"category": top, "reason": reason}
