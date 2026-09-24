"""Scrape events from every organization's events page and write them to Excel.

Usage:
    python main.py --source-sheet India --output "output/raw/Events.xlsx" --limit 5 --engine free

    python main.py --resume   # picks up wherever the last run stopped

For a full weekly re-scrape of both India and USA plus cleaning/classification,
use weekly_full_run.py instead of calling this directly.
"""

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
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

    CHUNK_SIZE = 40000  # chars per Gemini call

    def _chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
        """Split into <= size chunks on line boundaries so a big page (e.g. a
        200-row events table) never gets silently truncated — every chunk
        gets its own full Gemini call instead of losing whatever fell past
        a single fixed cutoff."""
        if len(text) <= size:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                split_at = text.rfind("\n", start, end)
                if split_at <= start:
                    split_at = end
            else:
                split_at = end
            chunks.append(text[start:split_at])
            start = split_at
        return chunks

    def extract(htmls, url, org_name):
        # One Gemini call per org for typical pages (combine all distinct page
        # snapshots' text into a single request, cutting per-call prompt/schema
        # overhead by up to ~9x on paginated sites) — but split into multiple
        # calls, not truncate, when the combined text is large enough that a
        # single call would silently drop content (e.g. CII's ~180-row table).
        texts = []
        seen_fingerprints = set()
        for html in htmls:
            text = extract_text(html, url)
            if not text.strip():
                continue
            fingerprint = text[:500]
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)
            texts.append(text)
        if not texts:
            return []
        combined = "\n\n=== NEXT PAGE SNAPSHOT ===\n\n".join(texts)
        chunks = _chunk_text(combined)
        return _merge([gemini_extract(chunk, url, org_name) for chunk in chunks])

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


STALE_RUN_AFTER_SECONDS = 20


def another_run_is_active() -> bool:
    """True if progress.json says a run is already in flight and was
    updated recently. Two processes upserting into the same Events.xlsx
    concurrently silently lose data (last save wins) — refuse to start a
    second run rather than race one, no matter which interface (CLI, GUI,
    dashboard) tries to launch it."""
    try:
        with open(PROGRESS_PATH, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("status") != "running":
            return False
        updated = datetime.fromisoformat(state["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age < STALE_RUN_AFTER_SECONDS
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return False


def _scrape_one(org: dict, extract_events) -> dict:
    """Fetch + extract + verify for a single org. Runs in a worker thread —
    must not touch shared state (Excel file, progress dict); returns
    everything the caller needs to apply those side effects itself."""
    try:
        htmls = fetch_html(org["url"])  # initial page + load-more/pagination snapshots
        events = extract_events(htmls, org["url"], org["organizer"])
        events, links_fixed = verify_events(events, org["url"])
        for e in events:
            e["organizer"] = org["organizer"]
            e["category"] = org["category"]
            e["country"] = org["country"]
            e["source_url"] = org["url"]
        return {"org": org, "pages": len(htmls), "events": events, "links_fixed": links_fixed, "error": None}
    except Exception as exc:  # noqa: BLE001 - log and keep going across 100+ sites
        return {"org": org, "pages": 0, "events": [], "links_fixed": 0, "error": str(exc)}


def run(source: str, output: str, sheet: str, limit: int, start: int, failures_log: str, engine: str, resume: bool,
        workers: int = 5, source_sheet: str | None = None):
    if another_run_is_active():
        print("Another scrape is already running (progress.json shows an active run) — refusing to start "
              "a second one, since two processes writing to the same Excel file at once silently lose data. "
              "Stop the other run first.")
        return 1

    orgs_all = load_organizations(source, source_sheet or sheet)

    if resume:
        start = load_next_index()

    orgs = orgs_all[start:start + limit] if limit else orgs_all[start:]
    extract_events = get_extractor(engine)

    total_added = 0
    total_updated = 0
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
        "events_updated": 0,
        "links_fixed": 0,
        "failures": 0,
        "recent_events": recent_events,
        "recent_log": recent_log,
    }
    write_progress(PROGRESS_PATH, state)

    clear_stop_flag()
    was_stopped = False

    try:
        # Sites are independent — process `workers` of them concurrently (each
        # gets its own browser + own extraction call) and apply results to the
        # shared Excel file / progress state serially once the batch finishes,
        # so there's no write-contention on output or progress.json.
        for batch_start in range(0, len(orgs), workers):
            if stop_requested():
                print("Stop requested — pausing.")
                clear_stop_flag()
                save_next_index(start + batch_start)  # resume from this batch next time
                was_stopped = True
                break

            batch = orgs[batch_start:batch_start + workers]
            state["current_organizer"] = ", ".join(o["organizer"] for o in batch)
            state["current_url"] = ""
            print(f"[{start + batch_start + 1}-{start + batch_start + len(batch)}] "
                  f"{', '.join(o['organizer'] for o in batch)}")

            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(lambda org: _scrape_one(org, extract_events), batch))

            for offset, result in enumerate(results):
                i = start + batch_start + offset + 1
                org = result["org"]

                try:
                    if result["error"] is not None:
                        raise RuntimeError(result["error"])

                    events = result["events"]
                    links_fixed = result["links_fixed"]
                    total_links_fixed += links_fixed

                    added, updated = upsert_events(output, sheet, events)
                    total_added += added
                    total_updated += updated
                    state["events_found"] += len(events)
                    state["events_added"] = total_added
                    state["events_updated"] = total_updated
                    state["links_fixed"] = total_links_fixed
                    print(f"    {org['organizer']}: {result['pages']} page(s), found {len(events)} event(s), "
                          f"added {added} new, fixed {updated} previously-incomplete date(s)"
                          f"{f', fixed {links_fixed} broken link(s)' if links_fixed else ''}")
                    recent_log.insert(0, f"[{i}/{len(orgs_all)}] {org['organizer']}: found {len(events)}, "
                                          f"added {added}, date-fixed {updated}"
                                          f"{f', fixed {links_fixed} link(s)' if links_fixed else ''}")

                    for e in events[:5]:
                        recent_events.insert(0, {
                            "organizer": org["organizer"],
                            "event_name": e.get("event_name", ""),
                            "date": e.get("date", ""),
                            "location": e.get("location_city", ""),
                        })

                except Exception as exc:  # noqa: BLE001 - one bad org/event must never take down a 300+ site run
                    print(f"    FAILED: {org['organizer']}: {exc}")
                    failures.append({"organizer": org["organizer"], "url": org["url"], "error": str(exc)})
                    state["failures"] = len(failures)
                    recent_log.insert(0, f"[{i}/{len(orgs_all)}] {org['organizer']}: FAILED - {exc}")

                state["processed"] = i

            del recent_log[50:]
            del recent_events[50:]
            write_progress(PROGRESS_PATH, state)
            save_next_index(start + batch_start + len(batch))  # in case of a hard crash, resume from here too

    except Exception:
        # Whatever this is, it's a bug we didn't anticipate — but progress.json
        # must never be left claiming "running" forever after the process has
        # actually died, or a stalled run becomes indistinguishable from a live
        # one (exactly what happened before this fix).
        import traceback
        traceback.print_exc()
        state["status"] = "crashed"
        write_progress(PROGRESS_PATH, state)
        return 1

    if failures:
        with open(failures_log, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["organizer", "url", "error"])
            writer.writeheader()
            writer.writerows(failures)
        print(f"\n{len(failures)} site(s) failed, logged to {failures_log}")

    sort_and_link(output, sheet)

    if was_stopped:
        state["status"] = "stopped"
        print(f"\nPaused at {state['processed']}/{len(orgs_all)}. {total_added} new events added, "
              f"{total_updated} incomplete dates fixed in {output}")
    else:
        clear_run_state()  # full run completed — next Start begins from the top again
        state["status"] = "done"
        print(f"\nDone. {total_added} new events added, {total_updated} incomplete dates fixed in {output}")

    write_progress(PROGRESS_PATH, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="Sources/Event_scrapper_-_Website_completed.xlsx")
    parser.add_argument("--source-sheet", default=None,
                         help="tab to read organizations from in --source (e.g. 'India' or 'USA'). "
                              "Defaults to --sheet if not given.")
    parser.add_argument("--output", default="output/raw/Events.xlsx")
    parser.add_argument("--sheet", default="Events",
                         help="tab name to write raw scraped events into, in --output")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit, process all organizations")
    parser.add_argument("--start", type=int, default=0, help="index to start from")
    parser.add_argument("--failures-log", default="output/failures/failures.csv")
    parser.add_argument("--engine", choices=["free", "gemini"], default="free",
                         help="'free' = pattern-matching only, no API/cost. 'gemini' = LLM-based, needs GEMINI_API_KEY")
    parser.add_argument("--resume", action="store_true",
                         help="ignore --start and pick up from output/run_state.json instead")
    parser.add_argument("--workers", type=int, default=5,
                         help="organizations to scrape concurrently (default 5). For --engine gemini, "
                              "keep this <= your API tier's requests-per-minute limit / avg pages per site.")
    args = parser.parse_args()

    sys.exit(run(args.source, args.output, args.sheet, args.limit, args.start,
                  args.failures_log, args.engine, args.resume, args.workers,
                  source_sheet=args.source_sheet) or 0)
