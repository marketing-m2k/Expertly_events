"""Runs the v2 discovery through the measurement harness (live, or offline on the verified final DOM)."""

from __future__ import annotations

import os
import time
from datetime import date

from pipeline.discovery.browser import BrowserSession
from pipeline.discovery.orchestrator import DiscoveryContext, discover_from_snapshots, discover_org
from pipeline.fetcher import HttpFetcher

TODAY = date(2026, 10, 7)
_HTTP = HttpFetcher()


def to_output(res, seconds: float) -> dict:
    return {
        "candidates": [{"title": c.title, "url": c.url, "date_hint": c.date_hint, "card_text": c.card_text,
                        "methods": c.methods} for c in res.candidates],
        "dropped": [d.to_dict() for d in res.dropped if d.reason not in ("merged_duplicate",)],
        "state": res.completeness.get("state", ""), "completeness": res.completeness,
        "failure_kinds": res.failure_kinds, "errors": res.errors, "pages": len(res.pages), "seconds": seconds,
    }


def run_v2(baseline: dict, mode: str) -> dict:
    org = {"organizer": baseline["org"], "country": baseline.get("country", ""), "url": baseline["url"], "category": ""}
    t0 = time.time()
    if mode == "ideal":
        d = os.path.join("baseline", "raw", baseline["slug"])
        snaps = [open(os.path.join(d, n), encoding="utf-8").read() for n in baseline.get("ideal_pages", ["page_01.html"])]
        res = discover_from_snapshots(org, snaps)
    else:
        ctx = DiscoveryContext(http=_HTTP, browser_factory=lambda: BrowserSession(), today=TODAY)
        res = discover_org(org, ctx)
    return to_output(res, time.time() - t0)
