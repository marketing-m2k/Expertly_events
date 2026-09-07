"""Weekly full re-scrape + clean for both India and USA, then rebuild
Master.xlsx from the results.

Unlike main.py's --resume mode (which only picks up where a partial run
stopped), this always re-scrapes EVERY organization in both tabs of
Sources/Event_scrapper_-_Website_completed.xlsx, so that:
  - events a site has newly published since last week are picked up, and
  - events we already have with an incomplete/missing date or (for an
    In Person event) an unannounced venue get updated -- and moved from
    "Upcoming - Incomplete" to "Upcoming - Verified" -- if the site has
    since published the missing info. If it still hasn't, the event stays
    Incomplete; this is a live re-check against the site every run, not a
    one-time judgment.

Re-visiting every organization's page is unavoidable -- it's the only way
to notice a brand-new event or a newly-completed one -- but nothing about
already-settled data gets duplicated: upsert_events (excel_writer.py)
skips an exact repeat and fixes an incomplete row in place rather than
appending beside it, and clean_events.py's own dedup does the same for
the same event scraped under two organizer labels. Verified events that
haven't changed are simply recomputed identically, not re-added.

After both countries are cleaned, output/Master.xlsx is rebuilt from the
union of every country's own Upcoming - Verified sheet (deduped by
country + event name + date) -- this is the one sheet meant for actual use
downstream (the Expertly website), so it only ever contains fully verified
events, tagged with which country each came from.

Meant to be run on a weekly cron schedule (see scheduled_run.py / Coolify's
Scheduled Task). Manual usage:

    python weekly_full_run.py
    python weekly_full_run.py --engine gemini   # higher-quality extraction, needs GEMINI_API_KEY
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import openpyxl

import main as scraper_main
from scraper.clean_events import clean
from scraper.excel_writer import FINAL_COLUMNS, format_sheet
from scraper.load_sites import load_organizations

SOURCE = "Sources/Event_scrapper_-_Website_completed.xlsx"
SUMMARY_PATH = "output/weekly_summary.json"
MASTER_PATH = "output/Master.xlsx"
MASTER_COLUMNS = ["Country"] + FINAL_COLUMNS

# Add a country here and it's picked up everywhere in this file automatically
# -- the scrape, the clean, and the Master rebuild all iterate this list.
COUNTRIES = [
    {
        "label": "India",
        "source_sheet": "India",
        "raw_sheet": "Events",
        "raw_output": "output/raw/Events.xlsx",
        "final_output": "output/Events_2026.xlsx",
        "failures_log": "output/failures.csv",
    },
    {
        "label": "USA",
        "source_sheet": "USA",
        "raw_sheet": "USA",
        "raw_output": "output/raw/Events_USA.xlsx",
        "final_output": "output/Events_USA_2026.xlsx",
        "failures_log": "output/failures_USA.csv",
    },
]


def build_master(labels_and_paths: list[tuple[str, str]]) -> int:
    """Rebuild Master.xlsx's one "Verified Events" sheet from the union of
    every country's own Upcoming - Verified sheet. Dedupes on (country,
    event name, date) so re-running this never doubles up a row -- each
    country file is itself the single source of truth per event; Master is
    just a read-only merge of them, not an accumulating log."""
    seen = set()
    all_rows = []
    for label, path in labels_and_paths:
        try:
            wb = openpyxl.load_workbook(path, data_only=True)
        except FileNotFoundError:
            continue
        ws = wb["Upcoming - Verified"]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not (row[3] or "").strip():  # Event Name blank
                continue
            key = (label, (row[3] or "").strip().lower(), (row[0] or "").strip().lower())
            if key in seen:
                continue
            seen.add(key)
            all_rows.append([label] + list(row))

    all_rows.sort(key=lambda r: (r[0], r[1] or ""))  # Country, then Date

    wb_out = openpyxl.Workbook()
    wb_out.remove(wb_out.active)
    ws_out = wb_out.create_sheet("Verified Events")
    ws_out.append(MASTER_COLUMNS)
    for r in all_rows:
        ws_out.append(r)
    format_sheet(ws_out, MASTER_COLUMNS)
    wb_out.save(MASTER_PATH)
    print(f"Wrote {MASTER_PATH}: {len(all_rows)} verified events across {len(labels_and_paths)} countries.")
    return len(all_rows)


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

    master_count = build_master([(c["label"], c["final_output"]) for c in COUNTRIES])
    summary["master_verified_total"] = master_count

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
