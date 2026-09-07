"""Weekly full re-scrape + clean for both India and USA.

Unlike main.py's --resume mode (which only picks up where a partial run
stopped), this always re-scrapes EVERY organization in both tabs of
Sources/Event_scrapper_-_Website_completed.xlsx, so that:
  - events a site has newly published since last week are picked up, and
  - events we already have with an incomplete/missing date get their date
    filled in (and moved from "Upcoming - Incomplete" to "Upcoming -
    Verified") if the site has since published one.

Meant to be run on a weekly cron schedule (see scheduled_run.py / Coolify's
Scheduled Task). Manual usage:

    python weekly_full_run.py
    python weekly_full_run.py --engine gemini   # higher-quality extraction, needs GEMINI_API_KEY
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import main as scraper_main
from scraper.clean_events import clean
from scraper.load_sites import load_organizations

SOURCE = "Sources/Event_scrapper_-_Website_completed.xlsx"
SUMMARY_PATH = "output/weekly_summary.json"

COUNTRIES = [
    {
        "label": "India",
        "source_sheet": "India",
        "raw_sheet": "Events",
        "raw_output": "output/Events.xlsx",
        "final_output": "output/Events_2026.xlsx",
        "failures_log": "output/failures.csv",
    },
    {
        "label": "USA",
        "source_sheet": "USA",
        "raw_sheet": "USA",
        "raw_output": "output/Events_USA.xlsx",
        "final_output": "output/Events_USA_2026.xlsx",
        "failures_log": "output/failures_USA.csv",
    },
]


def run_all(engine: str = "free", llm_classify: bool = True) -> int:
    exit_code = 0
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "countries": {}}

    for country in COUNTRIES:
        label = country["label"]
        orgs = load_organizations(SOURCE, country["source_sheet"])
        print(f"\n=== {label}: full re-scrape of {len(orgs)} organizations ===")

        rc = scraper_main.run(
            source=SOURCE,
            output=country["raw_output"],
            sheet=country["raw_sheet"],
            limit=0,
            start=0,
            failures_log=country["failures_log"],
            engine=engine,
            resume=False,  # always scrape everything, not just where we left off
            source_sheet=country["source_sheet"],
        )
        if rc:
            exit_code = rc
            print(f"{label} scrape failed (exit {rc}) — skipping its cleaning step this run")
            summary["countries"][label] = {"scrape_status": "failed"}
            continue

        print(f"=== {label}: cleaning/classifying into {country['final_output']} ===")
        counts = clean(
            input_path=country["raw_output"],
            sheet=country["raw_sheet"],
            output_path=country["final_output"],
            label=label,
            total_orgs=len(orgs),
            llm_classify=llm_classify,
        )
        summary["countries"][label] = {"scrape_status": "done", **counts}

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["status"] = "done" if exit_code == 0 else "partial_failure"
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["free", "gemini"], default="free",
                         help="extraction engine for main.py's scrape step (default: free, no API cost)")
    parser.add_argument("--skip-llm", action="store_true",
                         help="skip the Gemini per-event relevance classification pass during cleaning")
    args = parser.parse_args()
    sys.exit(run_all(engine=args.engine, llm_classify=not args.skip_llm))
