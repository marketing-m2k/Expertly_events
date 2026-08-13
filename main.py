"""Scrape events from every organization's events page and write them to Excel.

Usage:
    python main.py --source "../Tax_Legal_Finance_Events_Master_100_Organizations.xlsx" ^
                    --output "output/Events.xlsx" --limit 5 --engine free

    python main.py --resume   # picks up wherever the last run stopped
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scraper.excel_writer import sort_and_link, upsert_events
from scraper.fetch import fetch_html
from scraper.load_sites import load_organizations
from scraper.verify_links import verify_events

PROGRESS_PATH = "output/progress.json"
STATE_PATH = "output/run_state.json"
STOP_FLAG_PATH = "output/stop.flag"


def _merge(event_lists: list[list[dict]]) -> list[dict]:
    """Merge events found across multiple page snapshots, deduping by (name, date)."""
    merged = {}
    for events in event_lists:
        for e in events:
            key = ((e.get("event_name") or "").strip().lower(), (e.get("date") or "").strip().lower())
            merged.setdefault(key, e)
    return list(merged.values())


def get_extractor(engine: str):
    if engine == "free":
        from scraper.heuristic_extract import extract_events as heuristic_extract

        def extract(htmls, url, org_name):
            return _merge([heuristic_extract(html, url) for html in htmls])

        return extract

    from scraper.extract import extract_text
    from scraper.gemini_extract import extract_events as gemini_extract

    def extract(htmls, url, org_name):
        return _merge([gemini_extract(extract_text(html, url), url, org_name) for html in htmls])

    return extract


def write_progress(path: str, state: dict):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_next_index() -> int:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f).get("next_index", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def save_next_index(index: int):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"next_index": index}, f)


def clear_run_state():
    try:
        os.remove(STATE_PATH)
    except FileNotFoundError:
        pass


def stop_requested() -> bool:
    return os.path.exists(STOP_FLAG_PATH)


def clear_stop_flag():
    try:
        os.remove(STOP_FLAG_PATH)
    except FileNotFoundError:
        pass


def run(source: str, output: str, sheet: str, limit: int, start: int, failures_log: str, engine: str, resume: bool):
    orgs_all = load_organizations(source)

    if resume:
        start = load_next_index()

    orgs = orgs_all[start:start + limit] if limit else orgs_all[start:]
    extract_events = get_extractor(engine)

    total_added = 0
    total_links_fixed = 0
    failures = []
    recent_events = []
    recent_log = []
    state = {
        "status": "running",
        "engine": engine,
        "total_sites": len(orgs_all),
        "processed": start,
        "current_organizer": "",
        "current_url": "",
        "events_found": 0,
        "events_added": 0,
        "links_fixed": 0,
        "failures": 0,
        "recent_events": recent_events,
        "recent_log": recent_log,
    }
    write_progress(PROGRESS_PATH, state)

    clear_stop_flag()
    was_stopped = False

    for i, org in enumerate(orgs, start=1 + start):
        if stop_requested():
            print("Stop requested — pausing.")
            clear_stop_flag()
            save_next_index(i - 1)  # resume from this org next time
            was_stopped = True
            break

        state["current_organizer"] = org["organizer"]
        state["current_url"] = org["url"]
        print(f"[{i}] {org['organizer']} -> {org['url']}")

        try:
            htmls = fetch_html(org["url"])  # initial page + load-more/pagination snapshots
            events = extract_events(htmls, org["url"], org["organizer"])
            events, links_fixed = verify_events(events, org["url"])
            total_links_fixed += links_fixed

            for e in events:
                e["organizer"] = org["organizer"]
                e["category"] = org["category"]
                e["country"] = org["country"]
                e["source_url"] = org["url"]

            added = upsert_events(output, sheet, events)
            total_added += added
            state["events_found"] += len(events)
            state["events_added"] = total_added
            state["links_fixed"] = total_links_fixed
            print(f"    {len(htmls)} page(s), found {len(events)} event(s), added {added} new"
                  f"{f', fixed {links_fixed} broken link(s)' if links_fixed else ''}")
            recent_log.insert(0, f"[{i}/{len(orgs_all)}] {org['organizer']}: found {len(events)}, added {added}"
                                  f"{f', fixed {links_fixed} link(s)' if links_fixed else ''}")

            for e in events[:5]:
                recent_events.insert(0, {
                    "organizer": org["organizer"],
                    "event_name": e.get("event_name", ""),
                    "date": e.get("date", ""),
                    "location": e.get("location_city", ""),
                })

        except Exception as exc:  # noqa: BLE001 - log and keep going across 100+ sites
            print(f"    FAILED: {exc}")
            failures.append({"organizer": org["organizer"], "url": org["url"], "error": str(exc)})
            state["failures"] = len(failures)
            recent_log.insert(0, f"[{i}/{len(orgs_all)}] {org['organizer']}: FAILED - {exc}")

        state["processed"] = i
        del recent_log[50:]
        del recent_events[50:]
        write_progress(PROGRESS_PATH, state)
        save_next_index(i)  # in case of a hard crash, resume from here too

        time.sleep(1)  # be polite to target sites

    if failures:
        with open(failures_log, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["organizer", "url", "error"])
            writer.writeheader()
            writer.writerows(failures)
        print(f"\n{len(failures)} site(s) failed, logged to {failures_log}")

    sort_and_link(output, sheet)

    if was_stopped:
        state["status"] = "stopped"
        print(f"\nPaused at {state['processed']}/{len(orgs_all)}. {total_added} new events added to {output}")
    else:
        clear_run_state()  # full run completed — next Start begins from the top again
        state["status"] = "done"
        print(f"\nDone. {total_added} new events added to {output}")

    write_progress(PROGRESS_PATH, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="Tax_Legal_Finance_Events_Master.xlsx")
    parser.add_argument("--output", default="output/Events.xlsx")
    parser.add_argument("--sheet", default="Events")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit, process all organizations")
    parser.add_argument("--start", type=int, default=0, help="index to start from")
    parser.add_argument("--failures-log", default="output/failures.csv")
    parser.add_argument("--engine", choices=["free", "gemini"], default="free",
                         help="'free' = pattern-matching only, no API/cost. 'gemini' = LLM-based, needs GEMINI_API_KEY")
    parser.add_argument("--resume", action="store_true",
                         help="ignore --start and pick up from output/run_state.json instead")
    args = parser.parse_args()

    sys.exit(run(args.source, args.output, args.sheet, args.limit, args.start,
                  args.failures_log, args.engine, args.resume) or 0)
