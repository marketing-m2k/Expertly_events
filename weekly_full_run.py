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
from scraper.date_utils import date_conflicts_with_text
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
    """Rebuild Master.xlsx from the union of every country's own
    Upcoming - Verified sheet. Dedupes on (country, event name, date) so
    re-running this never doubles up a row -- each country file is itself
    the single source of truth per event; Master is just a read-only merge
    of them, not an accumulating log.

    Being in "Upcoming - Verified" is not enough on its own to reach
    Master: every row also goes through a cross-check gate here first --
    does the event's own name/description state an explicit date that
    DISAGREES with the resolved Date/End Date? (e.g. a historical
    "Bulletin" title stating 1908 while the resolved date drifted to some
    other year, or a description saying an event "was held ... 2023" while
    the Date column still shows something else). A row that fails this
    check is held back into a separate "Needs Review" sheet instead of
    silently reaching Master with a possibly-wrong date."""
    seen = set()
    passed_rows = []
    needs_review_rows = []

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
            full_row = [label] + list(row)

            date_disp, end_disp, name, description = full_row[1], full_row[2], full_row[4], full_row[9]
            try:
                resolved_start = datetime.strptime(date_disp, "%d-%b-%Y") if date_disp else None
            except ValueError:
                resolved_start = None
            resolved_end = None
            if end_disp:
                try:
                    resolved_end = datetime.strptime(end_disp, "%d-%b-%Y")
                except ValueError:
                    pass

            if resolved_start and date_conflicts_with_text(resolved_start, resolved_end,
                                                             f"{name or ''} {description or ''}"):
                needs_review_rows.append(full_row)
            else:
                passed_rows.append(full_row)

    passed_rows.sort(key=lambda r: (r[0], r[1] or ""))  # Country, then Date
    needs_review_rows.sort(key=lambda r: (r[0], r[1] or ""))

    wb_out = openpyxl.Workbook()
    wb_out.remove(wb_out.active)

    ws_v = wb_out.create_sheet("Verified Events")
    ws_v.append(MASTER_COLUMNS)
    for r in passed_rows:
        ws_v.append(r)
    format_sheet(ws_v, MASTER_COLUMNS)

    ws_r = wb_out.create_sheet("Needs Review")
    ws_r.append(MASTER_COLUMNS)
    for r in needs_review_rows:
        ws_r.append(r)
    format_sheet(ws_r, MASTER_COLUMNS)

    wb_out.save(MASTER_PATH)
    print(f"Wrote {MASTER_PATH}: {len(passed_rows)} verified events across {len(labels_and_paths)} countries "
          f"({len(needs_review_rows)} held back to Needs Review -- date conflicts with the event's own text).")
    return len(passed_rows)


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
