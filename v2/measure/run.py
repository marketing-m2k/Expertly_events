"""Measurement runner.

    python -m measure.run --engine v1 --mode live  --out reports/baseline_v1_live
    python -m measure.run --engine v1 --mode ideal --out reports/baseline_v1_ideal     # card parsing on the verified final DOM
    python -m measure.run --engine v2 --mode live  --out reports/baseline_v2_live

`ideal` mode feeds the engine the final, fully-expanded DOM captured for the
baseline, so it isolates CARD PARSING from FETCH/PAGINATION problems. `live`
mode is end to end. Nothing here touches Master or any v1 output file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from measure.match import OrgMetrics, aggregate, load_baselines, match_org
from measure.report import render_markdown

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_engine(engine: str, mode: str, baseline: dict) -> dict:
    slug, url = baseline["slug"], baseline["url"]
    if engine == "v1":
        from measure.v1_adapter import ideal_pages, run_v1
        return run_v1(url, htmls=ideal_pages(slug, baseline) if mode == "ideal" else None)
    if engine == "v2":
        from measure.v2_adapter import run_v2
        return run_v2(baseline, mode)
    raise SystemExit(f"unknown engine {engine}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["v1", "v2"], required=True)
    ap.add_argument("--mode", choices=["live", "ideal"], default="live")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args(argv)

    baselines = load_baselines("baseline", args.only)

    def one(b):
        t0 = time.time()
        try:
            out = run_engine(args.engine, args.mode, b)
        except Exception as exc:  # noqa: BLE001 - a crash is a result, not an excuse to stop the measurement
            out = {"candidates": [], "dropped": [], "state": "", "failure_kinds": ["crash"],
                   "errors": [f"{type(exc).__name__}: {exc!s:.200}"], "pages": 0, "seconds": time.time() - t0}
        m = match_org(b, out)
        print(f"  {b['slug']:22} true={m.true_count:3} found={m.discovered_count:3} matched={m.matched_valid:3} "
              f"recall={m.recall if m.recall is not None else '-':>6} missed={len(m.missed):3} dup={len(m.duplicates):3} "
              f"extra={len(m.extras):3} state={m.state_reported or '-'} {m.seconds}s", flush=True)
        return m

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows: list[OrgMetrics] = list(ex.map(one, baselines))
    agg = aggregate(rows)
    print("\nAGGREGATE:", json.dumps(agg, indent=1))
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        payload = {"engine": args.engine, "mode": args.mode, "aggregate": agg, "orgs": [r.to_dict() for r in rows]}
        with open(args.out + ".json", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, ensure_ascii=False)
        with open(args.out + ".md", "w", encoding="utf-8") as f:
            f.write(render_markdown(payload))
        print(f"wrote {args.out}.json / .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
