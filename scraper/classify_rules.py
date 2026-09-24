"""Rule-based Tax / Finance / Legal classification -- no AI, no cost.

"Tax", "Finance" and "Legal" are three BROAD professional fields, not three
keywords. What counts in each is defined in taxonomy.json (International Tax,
Transfer Pricing, Private Equity, Insolvency, Data Privacy...), and that file
is meant to grow: adding a topic or phrase there needs no code change.

Returns one of: Tax, Finance, Legal (accepted), Reject (definitely not a
professional Tax/Finance/Legal event) or Unclear (goes to Needs Review, where
the Gemini review decides using the same taxonomy). The category comes from
the event's OWN title and description, never from the hosting organisation.

Scoring: a topic phrase in the TITLE counts 3, in the description 1. A
"weak" generic word (risk, policy, sustainability...) counts 1 in the title,
0.5 in the description. An event is accepted at a score of 3 or more AND only
with at least one real topic phrase (generic words alone never accept), so a
single generic word or a single passing mention in the description is never
enough on its own -- those go to the Gemini review instead.
"""

import json
import os
import re
from functools import lru_cache

TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxonomy.json")
ORDER = ("Tax", "Legal", "Finance")  # a tie between fields is settled in this order
STRONG_TITLE, STRONG_DESC, WEAK_TITLE, WEAK_DESC = 3, 1, 1, 0.5
ACCEPT_AT = 3

_REJECT_PATTERNS = {
    "social event": r"\b(happy hour|mixer|golf|holiday party|christmas party|reception|social hour|cocktail|"
                    r"gala|networking (lunch|drinks|breakfast|evening)|yoga|wellness|fun run|5k|dinner|sundowner|trivia night|quiz night)\b",
    "exam prep": r"\b(exam (review|prep|preparation|training|bootcamp)|pre[- ]exam|pass (the|your) .{0,20}exam|"
                 r"examiner (school|training)|mock exam|exam registration|exam enrol+ment|study skills)\b",
    "corporate notice": r"\b(annual general meeting|\bagm\b|dividend (declaration|announcement|payment|record)|board meeting|"
                        r"intimation|record date|"
                        r"results? announcement|postal ballot)\b",
    "newsletter/publication": r"\b(newsletter|quarterly issue|journal issue|magazine issue|e-?bulletin)\b",
    "college event": r"\b(alumni|campus recruit|college (event|fair)|actec)\b",
}


def _phrase_regex(phrase: str) -> re.Pattern:
    """A taxonomy phrase as a regex: whole-word, hyphen/space tolerant, 'and'
    also matches '&', an optional plural, and a trailing * meaning any ending."""
    text = phrase.strip().lower()
    stem = text.endswith("*")
    parts = []
    for token in text.rstrip("*").split():
        if token in ("and", "&"):
            parts.append(r"(?:&|and)")
        else:
            parts.append(re.escape(token).replace(r"\-", r"[\s\-]"))
    body = r"[\s\-]+".join(parts)
    tail = "" if stem else r"(?:s|es)?(?![A-Za-z0-9])"
    return re.compile(r"(?<![A-Za-z0-9])" + body + tail, re.I)


@lru_cache(maxsize=1)
def load_taxonomy() -> dict:
    with open(TAXONOMY_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    compiled = {}
    for category in ORDER:
        block = raw[category]
        topics = []
        for topic, phrases in block["topics"].items():
            names = dict.fromkeys([topic] + list(phrases))  # a topic's own name is always a phrase
            topics.append((topic, [(p, _phrase_regex(p)) for p in names]))
        compiled[category] = {"topics": topics, "weak": [(p, _phrase_regex(p)) for p in block.get("weak", [])]}
    return compiled


def taxonomy_topics() -> dict[str, list[str]]:
    """{"Tax": [topic names], ...} -- used to tell Gemini what counts."""
    return {category: [topic for topic, _ in block["topics"] if "(general)" not in topic]
            for category, block in load_taxonomy().items()}


def taxonomy_prompt_text() -> str:
    return "\n".join(f"  {category}: " + "; ".join(topics) for category, topics in taxonomy_topics().items())


def classify_event(name: str, description: str = "") -> dict:
    name, description = name or "", description or ""

    for reason, pattern in _REJECT_PATTERNS.items():
        haystack = f"{name} {description}" if reason == "exam prep" else name
        m = re.search(pattern, haystack, re.I)
        if m:
            return {"category": "Reject", "reason": f"{reason}: '{m.group(0).strip()}' in the event text"}

    taxonomy = load_taxonomy()
    scores, evidence, topic_of = {}, {}, {}
    for category in ORDER:
        block, score, hits, strong = taxonomy[category], 0.0, [], 0
        for topic, phrases in block["topics"]:
            for _, rx in phrases:
                if (m := rx.search(name)):
                    score += STRONG_TITLE
                    strong += 1
                    hits.append((topic, m.group(0).strip().lower()))
                elif (m := rx.search(description)):
                    score += STRONG_DESC
                    strong += 1
                    hits.append((topic, m.group(0).strip().lower()))
        for _, rx in block["weak"]:
            if (m := rx.search(name)):
                score += WEAK_TITLE
                hits.append(("generic word", m.group(0).strip().lower()))
            elif (m := rx.search(description)):
                score += WEAK_DESC
                hits.append(("generic word", m.group(0).strip().lower()))
        scores[category] = score if strong else min(score, ACCEPT_AT - 0.5)  # generic words alone never accept
        seen, shown = set(), []
        for topic, matched in hits:
            if (topic, matched) not in seen:
                seen.add((topic, matched))
                shown.append(f"{topic} ('{matched}')")
        evidence[category] = shown[:4]
        topic_of[category] = next((t for t, _ in hits if t != "generic word"), "")

    top_score = max(scores.values())
    if top_score < ACCEPT_AT:
        if top_score == 0:
            return {"category": "Unclear", "reason": "no Tax, Finance or Legal topic found in the event's title or description"}
        best = next(c for c in ORDER if scores[c] == top_score)
        return {"category": "Unclear", "reason": "only a generic word or a single passing mention "
                f"({'; '.join(evidence[best])}) -- not enough to accept on rules alone"}
    tied = [c for c in ORDER if scores[c] == top_score]
    top = tied[0]
    reason = f"{top} topic in the event's own text: {'; '.join(evidence[top])}"
    if len(tied) > 1:
        reason += f" (also matches {' and '.join(tied[1:])}; {top} chosen by fixed order)"
    return {"category": top, "reason": reason, "topic": topic_of[top]}
