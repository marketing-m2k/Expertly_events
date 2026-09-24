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
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

import openpyxl
from openpyxl.styles import Font, PatternFill

import main as scraper_main
from scraper.api_budget import BudgetExceededError, get_total_cost
from scraper.clean_events import JUNK_TITLES, clean
from scraper.date_utils import date_conflicts_with_text
from scraper.excel_writer import FINAL_COLUMNS, format_sheet
from scraper.load_sites import load_organizations

# A handful of organizations run one global calendar but got entered into
# several different country tabs of the Sources workbook (e.g. the
# Association of Corporate Treasurers under both UK and UAE, ISDA under
# both UK and USA) -- each tab re-scrapes the SAME page, so the same event
# shows up once per tab it's listed under, tagged with whichever country
# that tab happens to be, regardless of where the event is actually
# happening. Separately, a genuinely single-tab org whose own calendar
# covers events worldwide (e.g. LCIA under India, listing a Beijing summit)
# gets every one of its events mislabeled with that one tab's country too.
# These hints let build_master() correct the Country field from the
# event's own Location text when it clearly disagrees, and only these --
# no full country-name-as-substring matching, which false-positives on
# things like "Indianapolis, IN" (contains "india") or "Dublin, OH".
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_COUNTRY_LOCATION_HINTS = {
    "USA": [r"\busa\b", r"\bunited states\b"] + [rf",\s*{s.lower()}\b" for s in _US_STATES],
    "UK": [r"\bunited kingdom\b", r"\buk\b", r"\blondon\b"],
    "India": [r"\bindia\b", r"\bmumbai\b", r"\bdelhi\b", r"\bbengaluru\b",
              r"\bbangalore\b", r"\bkolkata\b", r"\bchennai\b", r"\bhyderabad\b",
              r"\bpune\b"],
    "UAE": [r"\bdubai\b", r"\babu dhabi\b", r"\buae\b", r"\bunited arab emirates\b"],
    "AUS": [r"\baustralia\b", r"\bsydney\b", r"\bmelbourne\b", r"\bbrisbane\b", r"\bperth\b"],
    "SG": [r"\bsingapore\b"],
}
# A confident location signal that names a country we don't even track --
# not corrected to one of the 6 tracked labels (there's nothing to correct
# it TO), but not left silently mislabeled either: routed to Needs Review
# so a human decides whether it belongs on that country's list at all.
_UNTRACKED_COUNTRY_HINTS = [
    r"\bchina\b", r"\bbeijing\b", r"\bshanghai\b", r"\bhong kong\b",
    r"\bjapan\b", r"\btokyo\b", r"\bosaka\b", r"\bcanada\b", r"\btoronto\b",
    r"\bcalgary\b", r"\bgermany\b", r"\bfrance\b", r"\bparis\b",
    r"\bswitzerland\b", r"\bgeneva\b", r"\bzurich\b", r"\bireland\b",
    r"\bdublin,\s*ireland\b", r"\bnew zealand\b", r"\bsouth africa\b",
]


def _infer_country_from_location(location: str) -> str | None:
    """A tracked country label if `location` unambiguously names it, else
    None (including when it's blank, or names more than one -- ambiguous
    beats wrong)."""
    loc = (location or "").lower()
    if not loc:
        return None
    matches = {country for country, patterns in _COUNTRY_LOCATION_HINTS.items()
               if any(re.search(p, loc) for p in patterns)}
    return matches.pop() if len(matches) == 1 else None


def _location_names_untracked_country(location: str) -> bool:
    loc = (location or "").lower()
    return bool(loc) and any(re.search(p, loc) for p in _UNTRACKED_COUNTRY_HINTS)


def _row_fullness(row: list) -> int:
    return sum(1 for v in row if v)


SOURCE = "Sources/Event_scrapper_-_Website_completed.xlsx"
SUMMARY_PATH = "output/summaries/weekly_summary.json"
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
        "final_output": "output/final/Events_2026.xlsx",
        "failures_log": "output/failures/failures.csv",
    },
    {
        "label": "USA",
        "source_sheet": "USA",
        "raw_sheet": "USA",
        "raw_output": "output/raw/Events_USA.xlsx",
        "final_output": "output/final/Events_USA_2026.xlsx",
        "failures_log": "output/failures/failures_USA.csv",
    },
    {
        "label": "UK",
        "source_sheet": "UK",
        "raw_sheet": "UK",
        "raw_output": "output/raw/Events_UK.xlsx",
        "final_output": "output/final/Events_UK_2026.xlsx",
        "failures_log": "output/failures/failures_UK.csv",
    },
    {
        "label": "SG",
        "source_sheet": "SG",
        "raw_sheet": "SG",
        "raw_output": "output/raw/Events_SG.xlsx",
        "final_output": "output/final/Events_SG_2026.xlsx",
        "failures_log": "output/failures/failures_SG.csv",
    },
    {
        "label": "UAE",
        "source_sheet": "UAE",
        "raw_sheet": "UAE",
        "raw_output": "output/raw/Events_UAE.xlsx",
        "final_output": "output/final/Events_UAE_2026.xlsx",
        "failures_log": "output/failures/failures_UAE.csv",
    },
    {
        "label": "AUS",
        "source_sheet": "AUS",
        "raw_sheet": "AUS",
        "raw_output": "output/raw/Events_AUS.xlsx",
        "final_output": "output/final/Events_AUS_2026.xlsx",
        "failures_log": "output/failures/failures_AUS.csv",
    },
]


def _build_stats_sheet(wb_out: openpyxl.Workbook, passed_rows: list, needs_review_rows: list) -> None:
    """Adds a "Stats" sheet to Master.xlsx summarizing every stage of the
    pipeline per country: orgs checked/found/failed, raw events extracted
    vs. filtered by Gemini, relevant events split by status, and this
    run's actual final Verified/Needs-Review counts. Rebuilt fresh every
    run (not carried over) so it always reflects exactly what's in the two
    other sheets right now."""
    per_country = []
    for country in COUNTRIES:
        label = country["label"]
        try:
            with open(_country_summary_path(label), encoding="utf-8") as f:
                counts = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if counts.get("scrape_status") != "done":
            continue
        verified = counts.get("verified", 0)
        incomplete = counts.get("incomplete", 0)
        past = counts.get("past", 0)
        flagged = counts.get("flagged", 0)
        orgs_seen = counts.get("orgs_seen", 0)
        total_orgs = counts.get("total_orgs", 0)

        failures_path = country["failures_log"]
        if os.path.exists(failures_path):
            with open(failures_path, encoding="utf-8") as f:
                failed = sum(1 for _ in csv.DictReader(f))
        else:
            failed = 0

        per_country.append({
            "label": label, "checked": total_orgs, "with_events": orgs_seen,
            "zero_events": total_orgs - orgs_seen, "failed": failed,
            "raw_extracted": verified + incomplete + past + flagged,
            "irrelevant": flagged, "relevant": verified + incomplete + past,
            "verified": verified, "incomplete": incomplete, "past": past,
        })

    master_verified = Counter(r[0] for r in passed_rows)
    master_needs_review = Counter(r[0] for r in needs_review_rows)

    ws = wb_out.create_sheet("Stats")
    header_fill = PatternFill("solid", fgColor="2F5496")
    header_font = Font(color="FFFFFF", bold=True)
    bold = Font(bold=True)

    row = [1]  # mutable so the nested helper can advance it

    def write_table(title: str, headers: list[str], data: list[list], totals: list) -> None:
        r = row[0]
        ws.cell(row=r, column=1, value=title).font = Font(bold=True, size=13)
        r += 1
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=r, column=c, value=h)
            cell.font = header_font
            cell.fill = header_fill
        r += 1
        for data_row in data:
            for c, v in enumerate(data_row, 1):
                ws.cell(row=r, column=c, value=v)
            r += 1
        for c, v in enumerate(totals, 1):
            ws.cell(row=r, column=c, value=v).font = bold
        row[0] = r + 2  # blank row after each table

    write_table(
        "Organizations",
        ["Country", "Checked", "Had >=1 event", "Had 0 events", "Failed to load"],
        [[c["label"], c["checked"], c["with_events"], c["zero_events"], c["failed"]] for c in per_country],
        ["TOTAL", sum(c["checked"] for c in per_country), sum(c["with_events"] for c in per_country),
         sum(c["zero_events"] for c in per_country), sum(c["failed"] for c in per_country)],
    )
    write_table(
        "Raw events extracted -> filtered by Gemini",
        ["Country", "Raw events extracted", "Irrelevant (filtered out)", "Relevant (kept)"],
        [[c["label"], c["raw_extracted"], c["irrelevant"], c["relevant"]] for c in per_country],
        ["TOTAL", sum(c["raw_extracted"] for c in per_country), sum(c["irrelevant"] for c in per_country),
         sum(c["relevant"] for c in per_country)],
    )
    write_table(
        "Relevant events -> split by status",
        ["Country", "Verified (complete)", "Incomplete (missing date/venue)", "Past"],
        [[c["label"], c["verified"], c["incomplete"], c["past"]] for c in per_country],
        ["TOTAL", sum(c["verified"] for c in per_country), sum(c["incomplete"] for c in per_country),
         sum(c["past"] for c in per_country)],
    )
    labels = [c["label"] for c in per_country]
    write_table(
        "Final Master.xlsx (this workbook)",
        ["Country", "Verified Events (final)", "Needs Review (held back)"],
        [[label, master_verified.get(label, 0), master_needs_review.get(label, 0)] for label in labels],
        ["TOTAL", sum(master_verified.values()), sum(master_needs_review.values())],
    )

    ws.column_dimensions["A"].width = 22
    for col in "BCDE":
        ws.column_dimensions[col].width = 20


def build_master(labels_and_paths: list[tuple[str, str]]) -> int:
    """Rebuild Master.xlsx from the union of every country's own
    Upcoming - Verified sheet.

    Two corrections happen here, on top of the per-country files
    themselves, because they can only be caught once every country's data
    is sitting side by side:

    1. Country correction from the event's own Location -- some
       organizations are global (their one Sources-tab listing covers
       events worldwide) and some are simply entered into several country
       tabs at once (each re-scraping the same global calendar page under
       a different label). Both produce a Country tag that's wrong for at
       least some of that org's events. _infer_country_from_location()
       trusts a confident Location signal over the source tab it happened
       to be scraped from.
    2. Cross-country dedup -- an org entered into multiple tabs produces
       the exact same event (same name, same date) once per tab, each
       under a different -- usually wrong per (1) -- country. Deduping key
       is (event name, date) alone, not (country, name, date), so the same
       globally-listed webinar can't survive as 3-5 near-identical rows.
       When duplicates collide, the most complete row wins.

    Being in "Upcoming - Verified" is not enough on its own to reach
    Master: every row also goes through a cross-check gate here first --
    does the event's own name/description state an explicit date that
    DISAGREES with the resolved Date/End Date? (e.g. a historical
    "Bulletin" title stating 1908 while the resolved date drifted to some
    other year, a description saying an event "was held ... 2023" while
    the Date column still shows something else, or a title with an
    embedded "[10/11/2026]" that disagrees with the resolved Date -- some
    sites put a different date, e.g. a registration cutoff, in their own
    structured date field). A row that fails this check, or whose Location
    names a country we don't even track, is held back into a separate
    "Needs Review" sheet instead of silently reaching Master with a
    possibly-wrong date or country."""
    by_key: dict[tuple[str, str], list] = {}
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
            if (row[3] or "").strip().lower() in JUNK_TITLES:
                # a generic section-label name (e.g. "Masterclasses",
                # "THEME") that reached this per-country file before the
                # extractor/JUNK_TITLES fix existed -- filtered out here too
                # so already-scraped data benefits immediately, not just
                # whatever gets scraped on the next run.
                continue
            full_row = [label] + list(row)

            date_disp, end_disp, name, location, description = (
                full_row[1], full_row[2], full_row[4], full_row[8], full_row[9])

            inferred_country = _infer_country_from_location(location)
            if inferred_country and inferred_country != label:
                full_row[0] = inferred_country

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

            date_conflict = resolved_start and date_conflicts_with_text(
                resolved_start, resolved_end, f"{name or ''} {description or ''}")
            location_conflict = not inferred_country and _location_names_untracked_country(location)

            if date_conflict or location_conflict:
                needs_review_rows.append(full_row)
                continue

            key = (name.strip().lower(), (date_disp or "").strip().lower())
            existing = by_key.get(key)
            if existing is None or _row_fullness(full_row) > _row_fullness(existing):
                by_key[key] = full_row

    passed_rows = list(by_key.values())
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

    _build_stats_sheet(wb_out, passed_rows, needs_review_rows)
    wb_out.move_sheet("Stats", offset=-(len(wb_out.sheetnames) - 1))  # put it first

    wb_out.save(MASTER_PATH)
    print(f"Wrote {MASTER_PATH}: {len(passed_rows)} verified events across {len(labels_and_paths)} countries "
          f"({len(needs_review_rows)} held back to Needs Review -- date conflicts with the event's own text).")
    return len(passed_rows)


def _country_summary_path(label: str) -> str:
    return f"output/summaries/summary_{label}.json"


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
        try:
            rc, _ = run_one_country(country, engine=engine, llm_classify=llm_classify)
        except BudgetExceededError as exc:
            print(f"\n*** STOPPING: {exc} ***")
            print(f"*** {country['label']} was mid-run when the cap hit -- its output may be partial. "
                  f"Countries not yet started this run were skipped. ***")
            exit_code = 1
            break
        if rc:
            exit_code = rc

    print(f"\nGemini API spend this run: ${get_total_cost():.4f}")
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
                              "useful for re-running one country or running countries in parallel")
    parser.add_argument("--merge-only", action="store_true",
                         help="skip scraping entirely and just rebuild Master.xlsx + weekly_summary.json "
                              "from each country's output/summaries/summary_<label>.json -- run this "
                              "after one or more --country runs")
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
