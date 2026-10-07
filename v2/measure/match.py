"""Matching discovered candidates against the manually verified baseline, and
the metrics that follow from it.

Discovery Recall = discovered valid events / manually verified valid events x 100

Baseline events carry `valid`: false for things that are on the page but are not
scheduled events (on-demand recordings, standing series). Finding one of those
is neutral -- never a miss, never a false positive -- but they are reported so
nothing is ignored silently.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date

from pipeline.common import norm_text, norm_url, similarity
from pipeline.dates import find_dates

TITLE_MATCH = 0.82


def load_baselines(directory: str = "baseline", only: list[str] | None = None) -> list[dict]:
    out = []
    for fn in sorted(os.listdir(directory)):
        if fn.endswith(".json"):
            with open(os.path.join(directory, fn), encoding="utf-8") as f:
                b = json.load(f)
            if not only or b["slug"] in only:
                out.append(b)
    return out


def _title_match(a: str, b: str) -> bool:
    na, nb = norm_text(a), norm_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 12 and (na in nb or nb in na):
        return True
    return similarity(a, b) >= TITLE_MATCH


@dataclass
class OrgMetrics:
    slug: str
    org: str
    true_count: int = 0
    discovered_count: int = 0           # raw candidates returned
    unique_count: int = 0
    matched_valid: int = 0
    matched_neutral: int = 0
    missed: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)          # returned, matched nothing in the baseline
    past_extras: int = 0                                       # extras whose own date text is already past (archive leakage)
    recall: float | None = None
    state_reported: str = ""
    state_expected: str = ""
    state_correct: bool | None = None
    silently_empty: bool = False                               # returned nothing, reported no problem, but events existed / source broken
    failure_kinds: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    drop_reasons: dict = field(default_factory=dict)
    miss_reasons: dict = field(default_factory=dict)
    extraction_failures: int = 0                               # matched candidates lacking a usable title / link
    pages_walked: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _cand_date_past(c: dict, today: date) -> bool:
    text = c.get("date_hint") or c.get("card_text") or ""
    hits = [h for h in find_dates(text) if h.valid]
    if not hits:
        return False
    last = hits[-1].end or hits[-1].start
    return last < today


def match_org(baseline: dict, output: dict, today: date | None = None) -> OrgMetrics:
    """`output` = {"candidates": [{title,url,date_hint,card_text,...}], "dropped": [...], "state": str,
    "failure_kinds": [...], "errors": [...], "pages": int, "seconds": float, "miss_diagnoser": callable|None}"""
    today = today or date(2026, 10, 7)
    m = OrgMetrics(slug=baseline["slug"], org=baseline["org"])
    base_all = baseline["events"]
    valid = [e for e in base_all if e["valid"]]
    m.true_count = len(valid)
    cands = list(output.get("candidates", []))
    m.discovered_count = len(cands)
    m.state_reported = output.get("state", "")
    m.state_expected = baseline.get("expected_state", "")
    m.failure_kinds = list(output.get("failure_kinds", []))
    m.errors = list(output.get("errors", []))
    m.pages_walked = output.get("pages", 0)
    m.seconds = round(output.get("seconds", 0.0), 1)
    m.dropped = list(output.get("dropped", []))
    for d in m.dropped:
        m.drop_reasons[d.get("reason", "?")] = m.drop_reasons.get(d.get("reason", "?"), 0) + 1

    used_cand: set[int] = set()
    base_hit: dict[int, int] = {}      # base index -> cand index
    # pass 1: URL identity; pass 2: title
    for passno in (1, 2):
        for bi, b in enumerate(base_all):
            if bi in base_hit:
                continue
            for ci, c in enumerate(cands):
                if ci in used_cand:
                    continue
                if passno == 1:
                    ok = bool(b.get("url")) and bool(c.get("url")) and norm_url(b["url"]) == norm_url(c["url"])
                else:
                    ok = _title_match(b["title"], c.get("title", ""))
                    # same title, different known URLs -> a different event (e.g. two chapters' "Ethics Training")
                    if ok and b.get("url") and c.get("url") and norm_url(b["url"]) != norm_url(c["url"]) \
                            and any(norm_url(x.get("url", "")) == norm_url(c["url"]) for x in base_all if x is not b):
                        ok = False
                if ok:
                    base_hit[bi] = ci
                    used_cand.add(ci)
                    break

    for bi, b in enumerate(base_all):
        if bi in base_hit:
            if b["valid"]:
                m.matched_valid += 1
                c = cands[base_hit[bi]]
                if not (c.get("title") or "").strip() or not (c.get("url") or "").strip() and b.get("url"):
                    m.extraction_failures += 1
            else:
                m.matched_neutral += 1
        elif b["valid"]:
            m.missed.append({"title": b["title"], "url": b.get("url", ""), "date_text": b.get("date_text", "")})

    # remaining candidates: duplicates of something already matched, or extras
    seen_keys = {}
    for bi, ci in base_hit.items():
        c = cands[ci]
        seen_keys[norm_url(c.get("url", "")) or norm_text(c.get("title", ""))] = ci
    for ci, c in enumerate(cands):
        if ci in used_cand:
            continue
        key = norm_url(c.get("url", "")) or norm_text(c.get("title", ""))
        info = {"title": c.get("title", ""), "url": c.get("url", ""), "date_hint": c.get("date_hint", "")}
        if key in seen_keys or any(_title_match(c.get("title", ""), cands[j].get("title", "")) and
                                   norm_url(c.get("url", "")) == norm_url(cands[j].get("url", "")) for j in used_cand):
            m.duplicates.append(info)
        else:
            m.extras.append(info)
            if _cand_date_past(c, today):
                m.past_extras += 1
    keys = {norm_url(c.get("url", "")) or norm_text(c.get("title", "")) for c in cands}
    m.unique_count = len(keys)

    if m.true_count:
        m.recall = round(100.0 * m.matched_valid / m.true_count, 1)
    if m.state_expected:
        m.state_correct = (m.state_reported == m.state_expected) if m.state_reported else None
    if m.state_expected == "discovery_failed":
        m.silently_empty = not cands and m.state_reported in ("", "discovery_complete") and not m.errors
    elif m.true_count and not cands and m.state_reported in ("", "discovery_complete") and not m.errors:
        m.silently_empty = True

    diag = output.get("miss_diagnoser")
    if diag:
        for miss in m.missed:
            miss["reason"] = diag(miss)
            m.miss_reasons[miss["reason"]] = m.miss_reasons.get(miss["reason"], 0) + 1
    return m


def aggregate(rows: list[OrgMetrics]) -> dict:
    t = sum(r.true_count for r in rows)
    got = sum(r.matched_valid for r in rows)
    reasons: dict = {}
    drops: dict = {}
    kinds: dict = {}
    for r in rows:
        for k, v in r.miss_reasons.items():
            reasons[k] = reasons.get(k, 0) + v
        for k, v in r.drop_reasons.items():
            drops[k] = drops.get(k, 0) + v
        for k in r.failure_kinds:
            kinds[k] = kinds.get(k, 0) + 1
    return {
        "orgs": len(rows),
        "true_events": t,
        "discovered_total": sum(r.discovered_count for r in rows),
        "matched_valid": got,
        "missed": sum(len(r.missed) for r in rows),
        "duplicates": sum(len(r.duplicates) for r in rows),
        "extras": sum(len(r.extras) for r in rows),
        "past_extras": sum(r.past_extras for r in rows),
        "recall_pct": round(100.0 * got / t, 1) if t else None,
        "silent_empty_orgs": sum(1 for r in rows if r.silently_empty),
        "states_correct": sum(1 for r in rows if r.state_correct),
        "states_checked": sum(1 for r in rows if r.state_correct is not None),
        "failure_kinds": kinds,
        "miss_reasons": reasons,
        "drop_reasons": drops,
        "extraction_failures": sum(r.extraction_failures for r in rows),
        "orgs_with_errors": sum(1 for r in rows if r.errors),
    }
