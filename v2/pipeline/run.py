"""The pipeline runner: stages in order, each separate, every candidate traced.

    SOURCE -> DISCOVERY -> COMPLETENESS CHECK -> (per event) DETAIL EXTRACTION -> QUALITY CHECK -> FIELD RE-SCRAPE
    -> SOURCE VERIFICATION -> CLASSIFICATION SIGNALS -> GEMINI REVIEW -> EVIDENCE VALIDATION -> FINAL STATUS
    -> STORE / MASTER -> QC REPORT

This module only orchestrates; the logic of each stage lives in its own module.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pipeline import master as master_mod, qc as qc_mod
from pipeline.common import now_iso, sha
from pipeline.discovery.browser import BrowserSession
from pipeline.discovery.orchestrator import DiscoveryContext, discover_org
from pipeline.fetcher import HttpFetcher
from pipeline.llm import Budget, GeminiClient
from pipeline.models import PIPELINE_VERSION, EXTRACTOR_VERSION, Candidate, Status
from pipeline.process import Deps, plan_pages, process_candidate
from pipeline.store import Store


@dataclass
class RunConfig:
    out_dir: str = "data/run"
    review_mode: str = "all"                 # all | flagged | off
    gemini_budget: float = 2.0
    workers: int = 8
    discovery_workers: int = 3
    max_events_per_org: int = 0              # 0 = no limit (test runs set this)
    today: date = field(default_factory=date.today)
    use_browser: bool = True
    use_feeds: bool = True
    rescrape: bool = True
    master_path: str = ""
    benchmark_dir: str = "baseline"
    missing_grace_days: int = 14
    label: str = ""


def _log(msg: str) -> None:
    print(msg, flush=True)


def load_orgs(sources_path: str, sheets: list[str], limit: int = 0) -> list[dict]:
    from scraper.load_sites import load_organizations
    out = []
    for s in sheets:
        orgs = load_organizations(sources_path, s)
        for o in orgs[:limit] if limit else orgs:
            o["country"] = o.get("country") or s
            out.append(o)
    return out


def apply_missing(store: Store, org_results: list, run_id: str, seen_event_ids: set[str], today: date, grace_days: int) -> int:
    """Events of a COMPLETELY read organisation that have not been seen for `grace_days` go to NEEDS_REVIEW with the reason.
    Organisations whose discovery was incomplete/failed are never used to judge an event 'not found'."""
    n = 0
    complete = {r.source_url for r in org_results if r.completeness.get("state") == "discovery_complete"}
    cutoff = (datetime.combine(today, datetime.min.time()) - timedelta(days=grace_days)).strftime("%Y-%m-%dT")
    for r in store.rows("status IN ('VERIFIED','CORRECTED') AND dup_of IS NULL AND locked=0"):
        if r["source_url"] not in complete or r["event_id"] in seen_event_ids or (r["last_discovered"] or "9") >= cutoff:
            continue
        store.log_status(r["event_id"], run_id, r["status"], Status.NEEDS_REVIEW,
                         f"not found on its website since {(r['last_discovered'] or '')[:10]} although the organisation's list was read completely", "pipeline")
        with store.lock:
            store.db.execute("UPDATE events SET status=? WHERE event_id=?", (Status.NEEDS_REVIEW, r["event_id"]))
            store.db.commit()
        n += 1
    return n


def run_pipeline(orgs: list[dict], cfg: RunConfig, *, llm=None, http=None, browser_factory=None, store: Store | None = None,
                 benchmark_slugs: dict | None = None) -> dict:
    os.makedirs(cfg.out_dir, exist_ok=True)
    store = store or Store(os.path.join(cfg.out_dir, "store.sqlite"))
    http = http or HttpFetcher()
    if browser_factory is None and cfg.use_browser:
        browser_factory = lambda: BrowserSession()
    if llm is None and cfg.review_mode != "off":
        llm = GeminiClient(Budget(cfg.gemini_budget))
    started = now_iso()
    run_id = store.start_run({k: str(v) for k, v in cfg.__dict__.items()} | {"orgs": len(orgs)})
    master_path = cfg.master_path or os.path.join(cfg.out_dir, "Master_v2.xlsx")
    locks = master_mod.import_locks(store, master_path)
    _log(f"[{run_id}] pipeline {PIPELINE_VERSION} / extractor {EXTRACTOR_VERSION}: {len(orgs)} organisations, review={cfg.review_mode}, locks restored: {locks}")
    stats = qc_mod.RunStats()

    def browser_open(url):
        with browser_factory() as br:
            return br.open_page(url)

    # ---- stage: discovery (+ completeness) --------------------------------------------------------------------------------------------------------
    previous = store.previous_org_counts(run_id)
    dctx = DiscoveryContext(http=http, browser_factory=browser_factory, today=cfg.today, previous_counts=previous, use_feeds=cfg.use_feeds)
    results: list = [None] * len(orgs)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, cfg.discovery_workers)) as ex:
        futs = {ex.submit(discover_org, o, dctx): i for i, o in enumerate(orgs)}
        for n, fut in enumerate(as_completed(futs), 1):
            res = results[futs[fut]] = fut.result()
            store.save_org_run(run_id, res)
            for c in res.candidates:
                store.ledger(run_id, res.org, c.candidate_id, "", "discovery", "candidate", c.title)
            for d in res.dropped:
                store.ledger(run_id, res.org, "", "", "discovery_drop", d.reason, f"{d.title} {d.url}"[:300])
                stats.dropped[d.reason] += 1
            stats.orgs[res.completeness.get("state", "?")] += 1
            _log(f"  discovery {n}/{len(orgs)} {res.org[:42]:42} {res.completeness.get('state', '?'):21} {len(res.candidates):3} events, {len(res.dropped)} dropped"
                 + (f"  [{','.join(res.failure_kinds)}]" if res.failure_kinds else ""))
    stats.candidates = sum(len(r.candidates) for r in results)
    _log(f"discovery done in {time.time() - t0:.0f}s: {stats.candidates} candidates; {dict(stats.orgs)}")

    # ---- per-event stages ------------------------------------------------------------------------------------------------------------------------
    deps = Deps(http=http, today=cfg.today, run_id=run_id, browser_open=browser_open if browser_factory else None, llm=llm,
                review_mode=cfg.review_mode, ledger=store.ledger, store=store, rescrape=cfg.rescrape)
    jobs: list[tuple[Candidate, bool, bool]] = []
    for res in results:
        plan = plan_pages(res.candidates)
        for i, c in enumerate(res.candidates):
            if cfg.max_events_per_org and i >= cfg.max_events_per_org:
                store.ledger(run_id, c.org, c.candidate_id, "", "not_processed", "limit", "test limit: max events per organisation")
                continue
            own, shared = plan[c.candidate_id]
            jobs.append((c, own, shared))
    _log(f"processing {len(jobs)} events with {cfg.workers} workers")
    seen_ids: set[str] = set()
    t1 = time.time()
    errors = []

    def work(job):
        c, own, shared = job
        try:
            return process_candidate(c, deps, has_own_page=own, shared_url=shared)
        except Exception as exc:  # noqa: BLE001 - one event failing must not stop the run, and must not vanish
            return ("error", c, f"{type(exc).__name__}: {exc!s:.200}")

    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for n, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if isinstance(r, tuple):                      # unexpected exception: traced, event kept as FETCH_FAILED-like error record
                _, c, msg = r
                errors.append((c.candidate_id, msg))
                store.ledger(run_id, c.org, c.candidate_id, "", "final", "ERROR", msg)
                stats.statuses["ERROR"] += 1
                continue
            row = r.to_row()
            action, eid = store.upsert(row, run_id)
            seen_ids.add(eid)
            stats.upsert[action] += 1
            store.ledger(run_id, r.cand.org, r.cand.candidate_id, eid, "master", action, "")
            for h in r.corrections:
                store.log_correction(eid, run_id, h)
            if r.initial_flags:
                stats.quality_flagged_first += 1
            stats.add_result(r)
            if n % 25 == 0 or n == len(futs):
                _log(f"  events {n}/{len(futs)}  {dict(stats.statuses)}  ({time.time() - t1:.0f}s)")
    store.flush()

    missing = apply_missing(store, results, run_id, seen_ids, cfg.today, cfg.missing_grace_days)
    if missing:
        _log(f"{missing} previously verified events of completely-read organisations were not found: sent to NEEDS_REVIEW")

    # ---- benchmark recall (where baseline data exists) ----------------------------------------------------------------------------------------------
    benchmark = None
    try:
        from measure.match import aggregate, load_baselines, match_org
        base = {b["url"]: b for b in load_baselines(cfg.benchmark_dir)} if os.path.isdir(cfg.benchmark_dir) else {}
        rows = []
        for r in results:
            b = base.get(r.source_url)
            if b:
                out = {"candidates": [{"title": c.title, "url": c.url, "date_hint": c.date_hint, "card_text": c.card_text} for c in r.candidates],
                       "state": r.completeness.get("state", ""), "failure_kinds": r.failure_kinds, "errors": r.errors, "pages": len(r.pages), "dropped": [d.to_dict() for d in r.dropped]}
                rows.append(match_org(b, out, cfg.today))
        if rows:
            benchmark = aggregate(rows)
    except Exception as exc:  # noqa: BLE001
        _log(f"benchmark recall skipped: {exc}")

    finished = now_iso()
    g = {"spent": getattr(getattr(llm, "budget", None), "spent", 0.0), "calls": getattr(getattr(llm, "budget", None), "calls", 0)}
    qc = qc_mod.build_qc(store, run_id, stats, results, benchmark=benchmark, started=started, finished=finished, config={k: str(v) for k, v in cfg.__dict__.items()}, gemini=g)
    qc["errors"] = errors
    qc["missing_from_complete_orgs"] = missing
    qc["headline"]["Events moved to review: not found on a completely-read website"] = missing
    master_mod.export_master(store, master_path, cfg.today, qc, run_id)
    with open(os.path.join(cfg.out_dir, "qc_report.json"), "w", encoding="utf-8") as f:
        json.dump(qc, f, indent=1, default=str)
    with open(os.path.join(cfg.out_dir, "qc_report.md"), "w", encoding="utf-8") as f:
        f.write(qc_mod.render_markdown(qc))
    with open(os.path.join(cfg.out_dir, "discovery_results.json"), "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in results], f, default=str)
    store.finish_run(run_id, {"headline": qc["headline"], "errors": errors})
    _log("\n" + qc_mod.render_markdown(qc)[:3500])
    return {"run_id": run_id, "qc": qc, "store": store, "discovery": results, "stats": stats, "master": master_path}
