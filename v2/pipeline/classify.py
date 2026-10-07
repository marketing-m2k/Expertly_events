"""Stage: CLASSIFICATION SIGNALS.

The target scope is Tax / Finance / Legal -- broad professional fields defined by
scraper/taxonomy.json (edit that file to extend them; no code change needed).

Rules here produce SIGNALS, never verdicts. v1 returned "Reject" the moment the
title contained 'dinner', 'networking lunch', 'gala' ...: "Annual Tax Dinner & Update"
was thrown away on a keyword. In v2:

  * topic hits (taxonomy) give a category guess and a strength;
  * 'social / exam-prep / corporate-notice / newsletter / college' words are reject SIGNALS;
  * a strong topic hit together with a reject signal is a CONFLICT -> it goes to review;
  * only the AI review, with quoted evidence from the official page, can REJECT.
"""

from __future__ import annotations

import re

from scraper.classify_rules import (ACCEPT_AT, ORDER, STRONG_DESC, STRONG_TITLE, WEAK_DESC, WEAK_TITLE, _REJECT_PATTERNS,
                                    load_taxonomy, taxonomy_prompt_text)

ACCEPTED = ("Tax", "Finance", "Legal")


def classify_signals(name: str, description: str = "") -> dict:
    name, description = name or "", description or ""
    taxonomy = load_taxonomy()

    reject_signals = []
    for reason, pattern in _REJECT_PATTERNS.items():
        haystack = f"{name} {description}" if reason == "exam prep" else name
        m = re.search(pattern, haystack, re.I)
        if m:
            reject_signals.append({"type": reason, "match": m.group(0).strip()})

    scores, evidence, topics = {}, {}, {}
    for category in ORDER:
        block, score, hits, strong = taxonomy[category], 0.0, [], 0
        for topic, phrases in block["topics"]:
            for _, rx in phrases:
                if (m := rx.search(name)):
                    score += STRONG_TITLE
                    strong += 1
                    hits.append((topic, m.group(0).strip().lower(), "title"))
                elif (m := rx.search(description)):
                    score += STRONG_DESC
                    strong += 1
                    hits.append((topic, m.group(0).strip().lower(), "description"))
        for _, rx in block["weak"]:
            if (m := rx.search(name)):
                score += WEAK_TITLE
                hits.append(("generic word", m.group(0).strip().lower(), "title"))
            elif (m := rx.search(description)):
                score += WEAK_DESC
                hits.append(("generic word", m.group(0).strip().lower(), "description"))
        scores[category] = round(score if strong else min(score, ACCEPT_AT - 0.5), 2)
        seen, shown = set(), []
        for topic, matched, where in hits:
            if (topic, matched) not in seen:
                seen.add((topic, matched))
                shown.append({"topic": topic, "matched": matched, "in": where})
        evidence[category] = shown[:5]
        topics[category] = next((h["topic"] for h in shown if h["topic"] != "generic word"), "")

    top = max(scores.values())
    best = next((c for c in ORDER if scores[c] == top), "")
    if top >= ACCEPT_AT:
        strength = "strong"
    elif top > 0:
        strength = "weak"
    else:
        strength = "none"
    in_title = any(h["in"] == "title" and h["topic"] != "generic word" for h in evidence.get(best, []))
    if strength == "strong" and reject_signals:
        verdict = "conflict"
    elif strength == "strong":
        verdict = "in_scope_strong"
    elif reject_signals:
        verdict = "possibly_out_of_scope"
    elif strength == "weak":
        verdict = "unclear_weak"
    else:
        verdict = "unclear_none"
    return {"category_guess": best if strength != "none" else "", "scores": scores, "strength": strength, "topic": topics.get(best, ""),
            "evidence": evidence.get(best, []), "topic_in_title": in_title, "reject_signals": reject_signals, "verdict": verdict,
            "needs_review": verdict != "in_scope_strong",
            "note": "signals only: nothing here rejects an event"}


def taxonomy_text() -> str:
    return taxonomy_prompt_text()
