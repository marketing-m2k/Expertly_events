"""v2 pipeline command line.

    # small integration test on the 11 baseline organisations
    python run_v2.py --baseline-orgs --out data/integration --review all

    # a few organisations from a country tab
    python run_v2.py --countries AUS --limit-orgs 3 --max-events-per-org 5 --out data/trial

    # reprocess everything affected by an extractor / prompt version change (uses the saved page copies, no network)
    python run_v2.py --reprocess --out data/run_full

    # THE FULL PRODUCTION RUN -- refuses to start without the explicit approval phrase
    python run_v2.py --countries India USA UK SG UAE AUS --review all --gemini-budget <usd> --out data/run_full --confirm-full "RUN FULL"

v1 is untouched: this writes only inside its own --out folder (store.sqlite, Master_v2.xlsx, qc_report.*, evidence/).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from dotenv import load_dotenv
    for _p in (os.path.join(HERE, "..", ".env"), os.path.join(HERE, ".env")):
        if os.path.exists(_p):
            load_dotenv(_p)
except ImportError:
    pass

import faulthandler  # noqa: E402
try:
    faulthandler.dump_traceback_later(1200, repeat=True, file=open(os.path.join(HERE, 'data', 'watchdog_stacks.txt'), 'a') if os.path.isdir(os.path.join(HERE, 'data')) else sys.stderr)
except (OSError, ValueError, AttributeError):
    pass

from pipeline.run import RunConfig, load_orgs, run_pipeline  # noqa: E402

DEFAULT_SOURCES = os.path.join(HERE, "..", "Sources", "Event_scrapper_-_Website_completed.xlsx")
FULL_RUN_PHRASE = "RUN FULL"
FULL_RUN_ORG_THRESHOLD = 40


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default=DEFAULT_SOURCES)
    ap.add_argument("--countries", nargs="*", default=[], help="Sources tabs: India USA UK SG UAE AUS")
    ap.add_argument("--baseline-orgs", action="store_true", help="use the organisations of the manually verified baseline")
    ap.add_argument("--limit-orgs", type=int, default=0, help="organisations per country tab (0 = all)")
    ap.add_argument("--max-events-per-org", type=int, default=0)
    ap.add_argument("--review", choices=["all", "flagged", "off"], default="all")
    ap.add_argument("--gemini-budget", type=float, default=2.0, help="USD cap for this run (v1's $2 default; raise deliberately)")
    ap.add_argument("--out", default=os.path.join(HERE, "data", "run"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--discovery-workers", type=int, default=3)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-feeds", action="store_true")
    ap.add_argument("--no-rescrape", action="store_true")
    ap.add_argument("--today", default="", help="override today's date (YYYY-MM-DD), for reproducible test runs")
    ap.add_argument("--reprocess", action="store_true", help="re-run affected stored events (new extractor/prompt version, flagged, not final)")
    ap.add_argument("--refetch", action="store_true", help="with --reprocess: re-open the live pages instead of the saved copies")
    ap.add_argument("--confirm-full", default="", help=f'must be exactly "{FULL_RUN_PHRASE}" to run more than {FULL_RUN_ORG_THRESHOLD} organisations')
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    cfg = RunConfig(out_dir=args.out, review_mode=args.review, gemini_budget=args.gemini_budget, workers=args.workers,
                    discovery_workers=args.discovery_workers, max_events_per_org=args.max_events_per_org, today=today,
                    use_browser=not args.no_browser, use_feeds=not args.no_feeds, rescrape=not args.no_rescrape)

    if args.reprocess:
        from pipeline.fetcher import HttpFetcher
        from pipeline.llm import Budget, GeminiClient
        from pipeline.process import Deps
        from pipeline.reprocess import reprocess, select_for_reprocess
        from pipeline.store import Store
        store = Store(os.path.join(args.out, "store.sqlite"))
        affected = select_for_reprocess(store)
        print(f"{len(affected)} stored events are affected")
        run_id = store.start_run({"reprocess": True, "refetch": args.refetch})
        llm = None if args.review == "off" else GeminiClient(Budget(args.gemini_budget))
        deps = Deps(http=HttpFetcher(), today=today, run_id=run_id, llm=llm, review_mode=args.review, ledger=store.ledger, store=store)
        stats = reprocess(store, deps, affected, run_id, refetch=args.refetch, live_fetcher=HttpFetcher())
        store.finish_run(run_id, stats)
        print(json.dumps(stats, indent=1))
        return 0

    if args.baseline_orgs:
        from measure.match import load_baselines
        orgs = [{"organizer": b["org"], "country": b.get("country", ""), "url": b["url"], "category": ""} for b in load_baselines(os.path.join(HERE, "baseline"))]
    elif args.countries:
        orgs = load_orgs(args.sources, args.countries, args.limit_orgs)
    else:
        ap.error("choose --baseline-orgs or --countries")
        return 2

    if len(orgs) > FULL_RUN_ORG_THRESHOLD and args.confirm_full != FULL_RUN_PHRASE:
        print(f"REFUSED: {len(orgs)} organisations is a full production run. It needs the explicit approval phrase:\n"
              f'    --confirm-full "{FULL_RUN_PHRASE}"\nNothing was started.')
        return 3
    result = run_pipeline(orgs, cfg)
    print(f"\nMaster: {result['master']}\nQC:     {os.path.join(args.out, 'qc_report.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
