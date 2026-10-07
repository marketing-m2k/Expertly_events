"""Stage: QC / AUDIT REPORT.

The question this answers is not "did the script finish?" but "can we account for every
discovered event and every failure at every stage?". So it reports counts at each stage
AND runs an audit over the ledger: every candidate discovery found must have a terminal
outcome (a final status, or a recorded drop/merge). Anything else is listed as LOST --
and must be empty.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from pipeline.models import Status


@dataclass
class RunStats:
    """Counters the runner updates as it goes (cheaper and clearer than re-deriving them)."""
    orgs: Counter = field(default_factory=Counter)             # state -> n
    candidates: int = 0
    dropped: Counter = field(default_factory=Counter)          # reason -> n
    extracted: int = 0
    readable: int = 0
    unreadable: int = 0
    fetch_failed: int = 0
    quality_flagged_first: int = 0                             # flagged right after extraction
    flag_counts: Counter = field(default_factory=Counter)      # flag -> n (final)
    rescrape_events: int = 0
    rescrape_attempts: int = 0
    rescrape_resolved_fields: int = 0
    statuses: Counter = field(default_factory=Counter)
    upsert: Counter = field(default_factory=Counter)           # added/updated/unchanged/kept_previous/duplicate_of/locked_skip
    ai_reviewed: int = 0
    ai_unavailable: int = 0
    ai_skipped: int = 0
    ai_corrections_accepted: int = 0
    ai_evidence_failures: int = 0
    ai_rejections_accepted: int = 0
    ai_rejections_refused: int = 0
    reject_reasons: Counter = field(default_factory=Counter)
    review_reasons: Counter = field(default_factory=Counter)
    gemini_spent: float = 0.0
    gemini_calls: int = 0

    def add_result(self, res) -> None:
        d = res.decision
        self.extracted += 1
        ev = res.ev
        if ev is not None:
            if ev.readable and d.status != Status.FETCH_FAILED:
                self.readable += 1
            elif d.status == Status.FETCH_FAILED:
                self.fetch_failed += 1
            else:
                self.unreadable += 1
            if ev.attempts:
                self.rescrape_events += 1
                self.rescrape_attempts += len(ev.attempts)
                self.rescrape_resolved_fields += sum(1 for a in ev.attempts if a["outcome"] == "resolved")
        self.statuses[d.status] += 1
        for f in (res.quality.flags if res.quality else []):
            self.flag_counts[f] += 1
        r = res.review
        if r is not None:
            if r.ai_status == "reviewed":
                self.ai_reviewed += 1
            elif r.ai_status == "skipped":
                self.ai_skipped += 1
            else:
                self.ai_unavailable += 1
            self.ai_corrections_accepted += len(r.applied)
            self.ai_evidence_failures += r.evidence_failures
            if r.raw_decision == "REJECTED":
                if r.rejection:
                    self.ai_rejections_accepted += 1
                else:
                    self.ai_rejections_refused += 1
        if d.status == Status.REJECTED:
            self.reject_reasons[(d.reasons[0] if d.reasons else "")[:120]] += 1
        elif d.status in (Status.NEEDS_REVIEW, Status.QUALITY_REVIEW, Status.NEEDS_AI_REVIEW):
            for x in d.reasons[1:3] or d.reasons[:1]:
                self.review_reasons[x[:110]] += 1


def audit_ledger(store, run_id: str) -> dict:
    """Every discovered candidate must have a final outcome. -> {discovered, with_final, lost: [...]}"""
    store.flush()
    with store.lock:
        disc = {r["candidate_id"]: dict(r) for r in store.db.execute(
            "SELECT candidate_id, org, reason FROM ledger WHERE run_id=? AND stage='discovery' AND outcome='candidate'", (run_id,)).fetchall()}
        final = {r["candidate_id"] for r in store.db.execute("SELECT candidate_id FROM ledger WHERE run_id=? AND stage='final'", (run_id,)).fetchall()}
        merged = {r["candidate_id"] for r in store.db.execute("SELECT candidate_id FROM ledger WHERE run_id=? AND stage IN ('discovery_drop','not_processed')", (run_id,)).fetchall()}
    lost = [{"candidate_id": c, "org": d["org"], "title": d["reason"]} for c, d in disc.items() if c not in final and c not in merged]
    return {"discovered_candidates": len(disc), "with_final_status": len(disc.keys() & final), "recorded_not_processed": len(disc.keys() & merged),
            "lost": lost}


def build_qc(store, run_id: str, stats: RunStats, org_results: list, *, benchmark: dict | None = None, started: str = "", finished: str = "",
             config: dict | None = None, gemini: dict | None = None) -> dict:
    audit = audit_ledger(store, run_id)
    states = Counter(r.completeness.get("state", "?") for r in org_results)
    drop_reasons = Counter()
    for r in org_results:
        for d in r.dropped:
            drop_reasons[d.reason] += 1
    totals = store.count_by_status()
    headline = {
        "Run": run_id, "Started": started, "Finished": finished,
        "Organisations processed": len(org_results),
        "  discovery complete": states.get("discovery_complete", 0),
        "  discovery incomplete": states.get("discovery_incomplete", 0),
        "  discovery failed": states.get("discovery_failed", 0),
        "Events discovered (candidates)": sum(len(r.candidates) for r in org_results),
        "Dropped at discovery (with reason)": sum(drop_reasons.values()),
        "Events extracted (pages read)": stats.readable,
        "Extraction failures (page unreadable)": stats.unreadable,
        "Fetch failures (official page could not be opened)": stats.fetch_failed,
        "Events quality-flagged (after extraction)": stats.quality_flagged_first,
        "Events with field re-scrapes": stats.rescrape_events,
        "  re-scrape attempts": stats.rescrape_attempts,
        "  fields fixed by a re-scrape": stats.rescrape_resolved_fields,
        "VERIFIED": stats.statuses.get(Status.VERIFIED, 0),
        "CORRECTED": stats.statuses.get(Status.CORRECTED, 0),
        "NEEDS_REVIEW": stats.statuses.get(Status.NEEDS_REVIEW, 0),
        "QUALITY_REVIEW": stats.statuses.get(Status.QUALITY_REVIEW, 0),
        "NEEDS_AI_REVIEW": stats.statuses.get(Status.NEEDS_AI_REVIEW, 0),
        "REJECTED": stats.statuses.get(Status.REJECTED, 0),
        "PAST": stats.statuses.get(Status.PAST, 0),
        "FETCH_FAILED": stats.statuses.get(Status.FETCH_FAILED, 0),
        "Duplicates (merged into another event)": stats.upsert.get("duplicate_of", 0),
        "Gemini: events reviewed": stats.ai_reviewed,
        "Gemini: corrections applied (evidence verified)": stats.ai_corrections_accepted,
        "Gemini: corrections refused (evidence failed)": stats.ai_evidence_failures,
        "Gemini: rejections accepted / refused": f"{stats.ai_rejections_accepted} / {stats.ai_rejections_refused}",
        "Gemini: unavailable / skipped": f"{stats.ai_unavailable} / {stats.ai_skipped}",
        "Gemini spend": f"${(gemini or {}).get('spent', 0):.4f} over {(gemini or {}).get('calls', 0)} calls",
        "TRACEABILITY: candidates without a final outcome": len(audit["lost"]),
    }
    if benchmark:
        headline["Discovery recall (benchmark orgs)"] = f"{benchmark.get('recall_pct')}% ({benchmark.get('matched_valid')} of {benchmark.get('true_events')})"
    return {"headline": headline, "stats": {"statuses": dict(stats.statuses), "flags": dict(stats.flag_counts), "upsert": dict(stats.upsert),
                                            "drop_reasons": dict(drop_reasons), "orgs": dict(states)},
            "store_totals": totals, "audit": audit, "benchmark": benchmark, "review_reasons": dict(stats.review_reasons.most_common(15)),
            "reject_reasons": dict(stats.reject_reasons.most_common(15)), "config": config or {},
            "problem_orgs": [{"org": r.org, "url": r.source_url, "state": r.completeness.get("state"), "why": r.completeness.get("reasons", [])[:3],
                              "failure_kinds": r.failure_kinds} for r in org_results if r.completeness.get("state") != "discovery_complete"]}


def render_markdown(qc: dict) -> str:
    L = ["# QC report", ""]
    L += ["| Measure | Value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in qc["headline"].items()] + [""]
    a = qc["audit"]
    L += ["## Traceability audit", "",
          f"- candidates discovered: **{a['discovered_candidates']}**",
          f"- with a final status: **{a['with_final_status']}**",
          f"- recorded as not processed (with reason): {a['recorded_not_processed']}",
          f"- **lost (no outcome): {len(a['lost'])}**" + ("  <- must be 0" if a["lost"] else "  (none: nothing disappeared silently)"), ""]
    for l in a["lost"][:20]:
        L.append(f"  - LOST: {l['org']} / {l['title']} ({l['candidate_id']})")
    if qc["stats"]["drop_reasons"]:
        L += ["", "## Dropped at discovery (every one has a reason)", "", "| Reason | Count |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in sorted(qc["stats"]["drop_reasons"].items(), key=lambda kv: -kv[1])]
    if qc["stats"]["flags"]:
        L += ["", "## Quality flags (final)", "", "| Flag | Events |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in sorted(qc["stats"]["flags"].items(), key=lambda kv: -kv[1])]
    if qc["review_reasons"]:
        L += ["", "## Why events are waiting for review", "", "| Reason | Events |", "|---|---|"] + [f"| {k} | {v} |" for k, v in qc["review_reasons"].items()]
    if qc["reject_reasons"]:
        L += ["", "## Rejections (all evidence-backed)", "", "| Reason | Events |", "|---|---|"] + [f"| {k} | {v} |" for k, v in qc["reject_reasons"].items()]
    if qc["problem_orgs"]:
        L += ["", "## Organisations that are not discovery_complete", ""]
        for p in qc["problem_orgs"][:60]:
            L.append(f"- **{p['state']}** {p['org']} ({p['url']}): {'; '.join(p['why'])} [{','.join(p['failure_kinds'])}]")
    L += ["", "## Store totals (all runs)", "", "| Status | Events |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(qc["store_totals"].items())]
    return "\n".join(L)
