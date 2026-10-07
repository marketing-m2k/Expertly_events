"""Stage: REPROCESSING / VERSIONING.

Nothing is frozen. Every stored record remembers the extractor / classifier / prompt version that produced it.
When extraction logic changes (bump EXTRACTOR_VERSION in models.py) or a record has quality flags or is waiting
for review, it is reprocessed -- from the raw page copy saved at the time (no network) or, with refetch, from the
live page. Previously REJECTED or NEEDS_REVIEW events are reconsidered too, so a bad early result is never permanent.

    affected = select_for_reprocess(store)          # which records, and why
    reprocess(store, deps, affected)                # run them through the pipeline again; history is preserved
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pipeline.fetcher import Fetcher, Page
from pipeline.models import CLASSIFIER_VERSION, EXTRACTOR_VERSION, REVIEW_PROMPT_VERSION, Candidate, Status
from pipeline.process import Deps, process_candidate
from pipeline.store import Store


@dataclass
class Affected:
    event_id: str
    reasons: list[str]


def select_for_reprocess(store: Store, *, include_flagged: bool = True, statuses: tuple[str, ...] = Status.RETRYABLE,
                         extractor_version: str = EXTRACTOR_VERSION, classifier_version: str = CLASSIFIER_VERSION,
                         prompt_version: str = REVIEW_PROMPT_VERSION) -> list[Affected]:
    out = []
    for r in store.rows("dup_of IS NULL AND locked=0"):
        why = []
        if r["extractor_version"] != extractor_version:
            why.append(f"extracted with extractor {r['extractor_version']} (current {extractor_version})")
        if r.get("classifier_version") and r["classifier_version"] != classifier_version:
            why.append(f"classified with classifier {r['classifier_version']} (current {classifier_version})")
        if r.get("prompt_version") and r["prompt_version"] != prompt_version and r["status"] in (Status.REJECTED, Status.NEEDS_REVIEW, Status.NEEDS_AI_REVIEW):
            why.append(f"reviewed with prompt {r['prompt_version']} (current {prompt_version})")
        if r["status"] in statuses and r["status"] not in (Status.VERIFIED, Status.CORRECTED, Status.PAST):
            why.append(f"status {r['status']} is not final")
        if include_flagged and json.loads(r["flags_json"] or "[]") and r["status"] not in (Status.REJECTED,):
            why.append("has quality flags")
        if why:
            out.append(Affected(r["event_id"], why))
    return out


class StoredPages:
    """A fetcher that serves the page saved when the event was last read (no network)."""

    def __init__(self, store: Store, mapping: dict[str, str], fallback: Fetcher | None = None, refetch: bool = False):
        self.store, self.mapping, self.fallback, self.refetch = store, mapping, fallback, refetch

    def get(self, url: str) -> Page:
        eid = self.mapping.get(url)
        if eid and not self.refetch:
            html = self.store.load_page(eid)
            if html:
                return Page(url=url, final_url=url, html=html, status=200, method="stored")
        if self.fallback is not None:
            return self.fallback.get(url)
        return Page(url=url, error="no stored copy of this page and refetching is off")


def candidate_from_row(row: dict) -> Candidate:
    packet = json.loads(row.get("packet_json") or "{}")
    d = packet.get("discovery", {})
    return Candidate(candidate_id="c_" + row["event_id"], org=row["org"], country=row.get("country", ""), source_url=row["source_url"],
                     listing_url=d.get("listing_url", row["source_url"]), url=row["url"] or "", title=d.get("title") or row.get("name") or "",
                     title_confidence=d.get("title_confidence") or "medium", card_text=d.get("card_text", ""), date_hint=d.get("date_hint", ""),
                     location_hint=d.get("location_hint", ""), methods=d.get("methods", []), notes=d.get("notes", []))


def reprocess(store: Store, deps: Deps, affected: list[Affected], run_id: str, *, refetch: bool = False, live_fetcher: Fetcher | None = None) -> dict:
    """Run affected records through the full per-event pipeline again and write the results back (history preserved)."""
    stats = {"reprocessed": 0, "status_changed": 0, "unchanged": 0, "errors": 0, "from_stored_page": 0}
    rows = {a.event_id: store.get(a.event_id) for a in affected}
    mapping = {r["url"]: r["event_id"] for r in rows.values() if r and r.get("url")}
    deps.http = StoredPages(store, mapping, fallback=live_fetcher, refetch=refetch)
    deps.save_pages = refetch
    for a in affected:
        row = rows[a.event_id]
        if not row:
            continue
        cand = candidate_from_row(row)
        own = bool(row["url"]) and not row.get("shared_url") and row["url"] != row["source_url"]
        try:
            res = process_candidate(cand, deps, has_own_page=own, shared_url=bool(row.get("shared_url")))
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            store.ledger(run_id, row["org"], cand.candidate_id, a.event_id, "final", "ERROR", f"reprocess: {exc!s:.150}")
            continue
        new = res.to_row()
        new["event_id"] = a.event_id            # keep identity: reprocessing never creates a second event
        action, eid = store.upsert(new, run_id)
        store.log_status(a.event_id, run_id, row["status"], new["status"], "reprocessed: " + "; ".join(a.reasons)[:300], "reprocess")
        for h in res.corrections:
            store.log_correction(a.event_id, run_id, h)
        stats["reprocessed"] += 1
        stats["from_stored_page"] += 0 if refetch else 1
        stats["status_changed" if row["status"] != new["status"] else "unchanged"] += 1
    return stats
