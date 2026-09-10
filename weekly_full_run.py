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
    {
        "label": "UK",
        "source_sheet": "UK",
        "raw_sheet": "UK",
        "raw_output": "output/raw/Events_UK.xlsx",
        "final_output": "output/Events_UK_2026.xlsx",
        "failures_log": "output/failures_UK.csv",
    },
    {
        "label": "SG",
        "source_sheet": "SG",
        "raw_sheet": "SG",
        "raw_output": "output/raw/Events_SG.xlsx",
        "final_output": "output/Events_SG_2026.xlsx",
        "failures_log": "output/failures_SG.csv",
    },
    {
        "label": "UAE",
        "source_sheet": "UAE",
        "raw_sheet": "UAE",
        "raw_output": "output/raw/Events_UAE.xlsx",
        "final_output": "output/Events_UAE_2026.xlsx",
        "failures_log": "output/failures_UAE.csv",
    },
    {
        "label": "AUS",
        "source_sheet": "AUS",
        "raw_sheet": "AUS",
        "raw_output": "output/raw/Events_AUS.xlsx",
        "final_output": "output/Events_AUS_2026.xlsx",
        "failures_log": "output/failures_AUS.csv",
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


def _country_summary_path(label: str) -> str:
    return f"output/summary_{label}.json"


def run_one_country(country: dict, engine: str = "free", llm_classify: bool = True) -> tuple[int, dict]:
    """Scrape + clean exactly one country. Returns (exit_code, result_dict).
    Also writes output/summary_<label>.json -- this is what lets the
    parallel GitHub Actions matrix run every country as an independent job
    (each on its own runner, no shared state) and have a later merge step
    reassemble one combined summary afterward, without needing every job to
    somehow write to the same file at once."""
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
        print(f"{label} scrape failed (exit {rc}) — skipping its cleaning step this run")
        result = {"scrape_status": "failed"}
        with open(_country_summary_path(label), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return rc, result

    print(f"=== {label}: cleaning/classifying into {country['final_output']} ===")
    counts = clean(
        input_path=country["raw_output"],
        sheet=country["raw_sheet"],
        output_path=country["final_output"],
        label=label,
        total_orgs=len(orgs),
        llm_classify=llm_classify,
    )
    result = {"scrape_status": "done", **counts}
    with open(_country_summary_path(label), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return 0, result


def merge_and_finalize(started_at: str | None = None) -> int:
    """The second half of a parallel run: read every country's
    output/summary_<label>.json (written independently by run_one_country,
    possibly on a different runner entirely), rebuild Master.xlsx from
    whichever final workbooks are actually present, and write the combined
    output/weekly_summary.json. Safe to call even if a country's own
    summary file is missing (its scrape/clean job failed or never ran) --
    that country is just recorded as failed rather than crashing the merge."""
    summary = {"started_at": started_at or datetime.now(timezone.utc).isoformat(), "countries": {}}
    exit_code = 0

    for country in COUNTRIES:
        label = country["label"]
        try:
            with open(_country_summary_path(label), encoding="utf-8") as f:
                summary["countries"][label] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            summary["countries"][label] = {"scrape_status": "failed"}
        if summary["countries"][label].get("scrape_status") != "done":
            exit_code = 1

    master_count = build_master([(c["label"], c["final_output"]) for c in COUNTRIES])
    summary["master_verified_total"] = master_count

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["status"] = "done" if exit_code == 0 else "partial_failure"
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return exit_code


def run_all(engine: str = "free", llm_classify: bool = True) -> int:
    """Sequential fallback: every country, one after another, in a single
    process. Used for local/manual runs and Coolify -- the parallel GitHub
    Actions matrix uses run_one_country() + merge_and_finalize() instead,
    each country as its own job."""
    exit_code = 0
    started_at = datetime.now(timezone.utc).isoformat()

    for country in COUNTRIES:
        rc, _ = run_one_country(country, engine=engine, llm_classify=llm_classify)
        if rc:
            exit_code = rc

    merge_rc = merge_and_finalize(started_at=started_at)
    return exit_code or merge_rc


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["free", "gemini"], default="free",
                         help="extraction engine for main.py's scrape step (default: free, no API cost)")
    parser.add_argument("--skip-llm", action="store_true",
                         help="skip the Gemini per-event relevance classification pass during cleaning")
    parser.add_argument("--country", default=None,
                         help="run only this one country's label (e.g. India) instead of all of them -- "
                              "used by the parallel GitHub Actions matrix, one job per country")
    parser.add_argument("--merge-only", action="store_true",
                         help="skip scraping entirely and just rebuild Master.xlsx + weekly_summary.json "
                              "from each country's output/summary_<label>.json -- used by the matrix's "
                              "final merge job, after every per-country job has already run")
    args = parser.parse_args()

    if args.merge_only:
        sys.exit(merge_and_finalize())
    elif args.country:
        matches = [c for c in COUNTRIES if c["label"] == args.country]
        if not matches:
            print(f"Unknown country label {args.country!r}. Known labels: {[c['label'] for c in COUNTRIES]}")
            sys.exit(1)
        rc, _ = run_one_country(matches[0], engine=args.engine, llm_classify=not args.skip_llm)
        sys.exit(rc)
    else:
        sys.exit(run_all(engine=args.engine, llm_classify=not args.skip_llm))
