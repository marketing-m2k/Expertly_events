"""Turn a raw scraped Events workbook (one flat sheet) into the reviewed,
5-sheet format (Summary / Upcoming - Verified / Upcoming - Incomplete / Past
events / Flagged for Review): normalized dates, a clean single-category
filter, junk-title filtering, dedup of the same event scraped under
different organizer labels, and a per-organizer summary table.

Usage:
    python -m scraper.clean_events --input output/Events_USA.xlsx \
        --sheet USA --output output/Events_USA_2026.xlsx --label USA \
        --total-orgs 215
"""

import argparse
from collections import defaultdict
from datetime import datetime

import openpyxl

from scraper.date_utils import (
    has_day_precision,
    has_explicit_year,
    parse_event_date,
    parse_event_date_range,
    stated_past_year,
)
from scraper.excel_writer import FINAL_COLUMNS, format_sheet

# An event is relevant if its category (or, when category is blank, its
# name/description) mentions any of these — not an exact-match check, since
# multi-focus orgs legitimately tag events "Legal / Tax" or "Finance /
# Accounting" and those are still real tax/finance/legal events, not noise.
RELEVANCE_KEYWORDS = (
    "tax", "finance", "financial", "legal", "law", "accounting", "audit",
    "bank", "securities", "insurance", "compliance", "insolvency", "actuari",
    "corporate governance", "cpa", "estate planning", "wealth management",
)

# Exact-match (case-insensitive, trimmed) titles seen across sites that are
# nav chrome / CTAs / placeholders picked up by the heuristic extractor, not
# real event names.
JUNK_TITLES = {
    "today", "more info", "upcoming events", "register", "register now",
    "events", "learn more", "learn more.", "google calendar", "conferences",
    "read more", "read more »", "login with facebook", "yourmembership",
    "view event", "home", "calendar", "view calendar", "view all events",
    "see all events", "details", "click here", "subscribe", "download",
    "sign in", "log in", "view program description",
    "learn more and secure your room at special rates.",
    "past events", "events calendar", "contact us", "quick links",
    "announcements", "empty heading", "no title", "untitled",
}
JUNK_SUBSTRINGS = ("check eligibility", "login with", "secure your room at special rates")


def _s(value) -> str:
    """Coerce any cell value to a string. openpyxl hands back whatever type
    Excel inferred for a cell -- a bare year typed into a Date column (e.g.
    "2026") gets auto-converted to an int, which would otherwise crash the
    string-only parsing/filtering below."""
    if value is None:
        return ""
    return str(value)


def _is_junk_title(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return True
    if n in JUNK_TITLES:
        return True
    if n.isdigit():
        return True
    return any(s in n for s in JUNK_SUBSTRINGS)


_FINAL_SHEET_NAMES = ("Upcoming - Verified", "Upcoming - Incomplete", "Past events", "Flagged for Review")


def _existing_key(row: list) -> tuple[str, str]:
    return ((row[3] or "").strip().lower(), (row[0] or "").strip().lower())


def _data_unchanged(fresh_row: list, existing_row: list) -> bool:
    """True if every field except Status (index 2, which depends on
    today's date and is recomputed separately) is identical.

    Compares via _s() on both sides -- openpyxl round-trips an empty
    string as None (write "" to a cell, save, reload: you get back None,
    not ""), so a freshly-built row holding "" for a blank field would
    otherwise never match the same row loaded back from a previous run's
    saved file, making nearly everything look "changed" when nothing was.
    """
    fresh = [_s(v).strip() for v in [fresh_row[0], fresh_row[1], *fresh_row[3:]]]
    existing = [_s(v).strip() for v in [existing_row[0], existing_row[1], *existing_row[3:]]]
    return fresh == existing


def _load_existing_final(path: str) -> tuple[dict, dict]:
    """Load a previously-generated final workbook, if one exists, into:
      - by_key: (name, date) -> (sheet_name, row) for every row currently
        on any of the 4 data sheets
      - incomplete_by_name: name -> (name, date) key, for rows currently
        sitting in Upcoming - Incomplete -- used to notice when a
        previously dateless/venueless event has since been completed,
        even though its (name, date) key itself has therefore changed.
    Returns two empty dicts if `path` doesn't exist yet (first-ever run)."""
    by_key: dict[tuple[str, str], tuple[str, list]] = {}
    incomplete_by_name: dict[str, tuple[str, str]] = {}
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except FileNotFoundError:
        return by_key, incomplete_by_name

    for sheet_name in _FINAL_SHEET_NAMES:
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[3]:
                continue
            row = list(row)
            key = _existing_key(row)
            by_key[key] = (sheet_name, row)
            if sheet_name == "Upcoming - Incomplete":
                incomplete_by_name[key[0]] = key
    return by_key, incomplete_by_name


def clean(input_path: str, sheet: str, output_path: str, label: str, total_orgs: int | None,
          llm_classify: bool = True):
    """Incremental by design: an event whose data hasn't changed since the
    last run keeps its existing row and sheet placement untouched (not
    re-classified, immune to any run-to-run LLM non-determinism) -- only a
    genuinely new event, or one whose underlying data actually changed
    (e.g. a date/venue that was missing is now filled in), goes through
    fresh relevance/completeness classification. An unchanged row's
    Upcoming/Past Status is still re-derived every run, though, since an
    event can become Past purely because time passed, with no site change
    at all.

    First run ever (no existing output_path) classifies everything, same
    as before -- there's nothing to diff against yet.
    """
    wb_in = openpyxl.load_workbook(input_path, data_only=True)
    ws_in = wb_in[sheet] if sheet in wb_in.sheetnames else wb_in[wb_in.sheetnames[0]]
    header = next(ws_in.iter_rows(min_row=1, max_row=1, values_only=True))
    idx = {name: i for i, name in enumerate(header)}

    today = datetime.now()
    events, past_events, flagged, incomplete = [], [], [], []
    orgs_seen = set()

    existing_by_key, existing_incomplete_by_name = _load_existing_final(output_path)

    # The same event is often pulled from two different sub-pages of the same
    # institution (e.g. ICAI's CPE Directorate page and its Research
    # committee page both mention the same webinar), each tagging it with a
    # different organizer label. Collapse those into one row per (name,
    # resolved date) instead of listing the event 2-3 times, preferring
    # whichever duplicate has more fields filled in (description/link).
    dedup_rows: dict[tuple[str, str], tuple] = {}

    def _fullness(row) -> int:
        return sum(1 for v in row if v)

    for row in ws_in.iter_rows(min_row=2, values_only=True):
        if not row or not _s(row[idx["Event Name"]]).strip():
            continue
        date_raw = _s(row[idx["Date"]])
        name = _s(row[idx["Event Name"]])
        organizer = _s(row[idx["Organizer"]])
        category = _s(row[idx["Category"]]).strip()
        fmt = row[idx.get("Format", -1)] if "Format" in idx else None
        location = row[idx.get("Location", -1)] if "Location" in idx else None
        description = row[idx.get("Description", -1)] if "Description" in idx else None
        link = row[idx.get("Register Link", -1)] if "Register Link" in idx else None
        source_url = row[idx.get("Source URL", -1)] if "Source URL" in idx else None

        orgs_seen.add(organizer)

        date_source = date_raw
        dt, end_dt = parse_event_date_range(date_raw, today)
        if dt is None:
            # some sites never put the date in a structured field at all --
            # it's only visible embedded in the title itself, e.g. "56th
            # Annual Spring Symposium, 2026"
            dt, end_dt = parse_event_date_range(name, today)
            date_source = name

        if dt is not None and not has_explicit_year(date_source, today):
            # a yearless date ("December 1") defaults to "next upcoming
            # occurrence" -- but an archive-style listing's own title/
            # description sometimes states, in the past tense, which year
            # it actually happened ("...programme was held from December 1
            # to 9 2023"). Trust that over the rollover guess so a 2023
            # event doesn't silently become "upcoming in 2026".
            real_year = stated_past_year(f"{name} {_s(description)}", today)
            if real_year:
                dt = dt.replace(year=real_year)
                if end_dt is not None:
                    end_dt = end_dt.replace(year=real_year)

        # dateutil's fuzzy parser will happily invent a day for a bare
        # "APR" or "December 2026" by borrowing it from `today` -- that's a
        # fabricated date, not a real one, so don't format/trust it unless
        # the source text actually named a day.
        has_real_date = dt is not None and has_day_precision(date_source)
        date_display = dt.strftime("%d-%b-%Y") if has_real_date else date_raw
        # only a genuine multi-day range gets an End Date -- a one-day event
        # keeps End Date blank rather than repeating the same date twice.
        end_date_display = ""
        if has_real_date and end_dt is not None and end_dt.date() != dt.date():
            end_date_display = end_dt.strftime("%d-%b-%Y")

        out_row = [date_display, end_date_display, None, name, organizer, category, fmt,
                   location, description, link, source_url]

        dedup_key = (name.strip().lower(), date_display.strip().lower())
        prior = dedup_rows.get(dedup_key)
        if prior is None or _fullness(out_row) > _fullness(prior):
            dedup_rows[dedup_key] = out_row

    def _place_unchanged(sheet_name: str, row: list) -> None:
        """An event carried over from the previous run untouched. Its
        Status/bucket can still shift purely because time passed (an
        Upcoming event whose date has now arrived moves to Past events)
        even though nothing about the event itself changed -- everything
        else about the row is preserved exactly as it was."""
        row = list(row)
        if sheet_name == "Upcoming - Verified":
            try:
                dt = datetime.strptime(row[0] or "", "%d-%b-%Y")
            except ValueError:
                dt = None
            if dt is not None and dt.date() < today.date():
                row[2] = "Past"
                past_events.append(row)
                return
            row[2] = "Upcoming"
            events.append(row)
        elif sheet_name == "Past events":
            row[2] = "Past"
            past_events.append(row)
        elif sheet_name == "Upcoming - Incomplete":
            incomplete.append(row)
        else:  # Flagged for Review
            flagged.append(row)

    # Split fresh rows into "unchanged since last run" (carried over as-is,
    # skipping classification entirely) vs "new or actually changed" (needs
    # fresh relevance/completeness classification below).
    to_classify = []
    consumed_existing_keys = set()

    for out_row in dedup_rows.values():
        name = out_row[3] or ""
        name_norm = name.strip().lower()
        fresh_key = _existing_key(out_row)
        existing_entry = existing_by_key.get(fresh_key)

        if existing_entry is None and name_norm in existing_incomplete_by_name:
            # this event was previously sitting in Upcoming - Incomplete
            # under the same name but a different (incomplete) date key.
            old_key = existing_incomplete_by_name[name_norm]
            old_sheet_name, old_row = existing_by_key[old_key]
            consumed_existing_keys.add(old_key)
            if has_day_precision(out_row[0] or ""):
                # the fresh scrape now has a real date for it -- this IS
                # that same event, newly completed, so treat it as changed
                # rather than brand new.
                to_classify.append(out_row)
            elif _data_unchanged(out_row, old_row):
                # still incomplete, and nothing else about it changed
                # either -- leave it exactly as it was rather than
                # re-classifying (and re-spending an LLM call on) the same
                # still-unresolved event every single run.
                _place_unchanged(old_sheet_name, old_row)
            else:
                # still incomplete, but some other field changed (e.g. a
                # new description) -- worth a fresh look.
                to_classify.append(out_row)
            continue

        if existing_entry is not None:
            existing_sheet_name, existing_row = existing_entry
            consumed_existing_keys.add(fresh_key)
            if _data_unchanged(out_row, existing_row):
                _place_unchanged(existing_sheet_name, existing_row)
                continue
            to_classify.append(out_row)
            continue

        to_classify.append(out_row)  # brand new event

    if existing_by_key:
        print(f"Incremental update: {len(to_classify)} new/changed event(s) to (re)classify, "
              f"{len(dedup_rows) - len(to_classify)} unchanged event(s) carried over as-is.")

    # Anything already in the previous output that this run's fresh scrape
    # didn't touch at all (e.g. the site removed that listing, or a
    # transient fetch failure) is left exactly as it was rather than
    # dropped -- "skip it and leave it," not "delete it."
    for key, (existing_sheet_name, existing_row) in existing_by_key.items():
        if key in consumed_existing_keys:
            continue
        _place_unchanged(existing_sheet_name, existing_row)

    # Separate from events/past_events/incomplete/flagged, which already
    # hold the carried-over unchanged rows -- these only get merged in
    # (via .extend, never overwritten) once classification below is done,
    # so an unchanged row is never at risk of being reset or re-judged.
    new_events, new_past_events, new_incomplete, new_flagged = [], [], [], []

    for out_row in to_classify:
        name = out_row[3] or ""
        category = out_row[5] or ""
        fmt = out_row[6] or ""
        location = out_row[7]
        description = out_row[8]
        date_display = out_row[0] or ""
        # date_display is already a validated "%d-%b-%Y" string whenever
        # the first pass found a real date (including a genuinely old
        # historical year corrected via stated_past_year) -- parse it
        # directly rather than routing it back through parse_event_date's
        # heuristics, which include a sanity-bound rejection of implausibly
        # old years meant for messy RAW text, not an already-normalized
        # date. Re-running that here would reject a correctly-resolved
        # 1914 event right back into looking unparseable. Only fall back
        # to the heuristic parser for a still-raw, unformatted date string.
        try:
            dt = datetime.strptime(date_display, "%d-%b-%Y")
        except ValueError:
            dt = parse_event_date(date_display, today)
        is_dated = dt is not None and has_day_precision(date_display)
        is_past = is_dated and dt.date() < today.date()
        # a virtual/hybrid event has nowhere to announce -- Location is
        # legitimately blank there. An In Person event with no venue yet
        # is the "date and venue not yet announced" case: real, but not
        # something an attendee could act on yet.
        missing_venue = _s(fmt).strip().lower() == "in person" and not _s(location).strip()

        if category:
            is_relevant = any(kw in category.lower() for kw in RELEVANCE_KEYWORDS)
        else:
            # no category tag at all -- fall back to judging the event itself
            haystack = f"{name} {_s(description)}".lower()
            is_relevant = any(kw in haystack for kw in RELEVANCE_KEYWORDS)

        if not is_relevant or _is_junk_title(name):
            out_row[2] = "Undated" if not is_dated else ("Past" if is_past else "Upcoming")
            new_flagged.append(out_row)
            continue

        if not is_dated or missing_venue:
            # a real, on-topic event whose date is missing/too vague to
            # trust (e.g. "APR" with no day), or an in-person event with no
            # venue announced yet -- it's verified as relevant, just not
            # complete enough to publish as a firm upcoming event.
            out_row[2] = "Incomplete"
            new_incomplete.append(out_row)
            continue

        if is_past:
            out_row[2] = "Past"
            new_past_events.append(out_row)
        else:
            out_row[2] = "Upcoming"
            new_events.append(out_row)

    if llm_classify and (new_events or new_past_events or new_incomplete):
        from scraper.classify_relevance import classify_all

        candidates = new_events + new_past_events + new_incomplete
        print(f"Classifying {len(candidates)} new/changed events with Gemini for actual topical "
              f"relevance (an org's category tag doesn't mean every event it hosts is on-topic; "
              f"unchanged events from the previous run are not re-classified)...")
        payload = [{"name": r[3], "organizer": r[4], "category": r[5], "location": r[7]} for r in candidates]
        verdicts = classify_all(payload)
        new_events, new_past_events, new_incomplete = [], [], []
        for row, is_relevant in zip(candidates, verdicts):
            if not is_relevant:
                new_flagged.append(row)
            elif row[2] == "Upcoming":
                new_events.append(row)
            elif row[2] == "Past":
                new_past_events.append(row)
            else:
                new_incomplete.append(row)

    events.extend(new_events)
    past_events.extend(new_past_events)
    incomplete.extend(new_incomplete)
    flagged.extend(new_flagged)

    def sort_key(row):
        # same reasoning as the is_dated re-parse above: row[0] is already
        # a validated "%d-%b-%Y" string for every dated row, so parse it
        # directly rather than through parse_event_date's sanity-bound
        # rejection of implausibly old years (which would otherwise sort
        # a correctly-resolved 1914 event to the very end instead of into
        # its real chronological place).
        try:
            d = datetime.strptime(row[0] or "", "%d-%b-%Y")
        except ValueError:
            d = parse_event_date(row[0], today)
        return d if d is not None else datetime.max

    events.sort(key=sort_key)
    past_events.sort(key=sort_key)
    incomplete.sort(key=sort_key)
    flagged.sort(key=sort_key)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws_summary = wb.create_sheet("Summary")
    orgs_with_upcoming = {r[4] for r in events}
    orgs_with_past = {r[4] for r in past_events}
    total_checked = total_orgs if total_orgs is not None else len(orgs_seen)
    relevant_total = len(events) + len(past_events) + len(incomplete)

    org_totals = defaultdict(lambda: [0, 0])
    for r in events:
        org_totals[r[4]][0] += 1
    for r in past_events:
        org_totals[r[4]][1] += 1

    summary_rows = [
        [f"{label} Tax / Finance / Legal Events — Summary"],
        [],
        ["Metric", "Count"],
        ["Total organizations/sources checked", total_checked],
        ["Organizations with at least one event found", len(orgs_seen)],
        ["Organizations with zero events found (dead link / no listing)",
         max(total_checked - len(orgs_seen), 0)],
        [],
        ["Total events on sheet", relevant_total, "Relevant", None, "Irrelevant", len(flagged)],
        ["  – Upcoming events (verified, complete)", len(events)],
        ["  – Upcoming events (verified, incomplete date/venue)", len(incomplete)],
        ["  – Past events (since Jan 2026)", len(past_events)],
        [],
        ["Organizations with Upcoming events", len(orgs_with_upcoming)],
        ["Organizations with Past events", len(orgs_with_past)],
        ["Organizations with BOTH Past and Upcoming", len(orgs_with_upcoming & orgs_with_past)],
        [],
        [],
        ["Organizer", "Upcoming", "Past", "Total"],
    ]
    for organizer, (up, past) in sorted(org_totals.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        summary_rows.append([organizer, up, past, up + past])

    for row in summary_rows:
        ws_summary.append(row)

    for name, rows in (
        ("Upcoming - Verified", events),
        ("Upcoming - Incomplete", incomplete),
        ("Past events", past_events),
        ("Flagged for Review", flagged),
    ):
        ws = wb.create_sheet(name)
        ws.append(FINAL_COLUMNS)
        for r in rows:
            ws.append(r)
        format_sheet(ws, FINAL_COLUMNS)

    wb.save(output_path)
    print(f"Wrote {output_path}: {len(events)} upcoming verified, {len(incomplete)} upcoming incomplete, "
          f"{len(past_events)} past, {len(flagged)} flagged. {len(orgs_seen)}/{total_checked} orgs had >=1 event.")

    return {
        "verified": len(events),
        "incomplete": len(incomplete),
        "past": len(past_events),
        "flagged": len(flagged),
        "orgs_seen": len(orgs_seen),
        "total_orgs": total_checked,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--sheet", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", default="Events")
    parser.add_argument("--total-orgs", type=int, default=None)
    parser.add_argument("--skip-llm", action="store_true",
                         help="skip the Gemini per-event relevance pass (rule-based filtering only)")
    args = parser.parse_args()
    clean(args.input, args.sheet, args.output, args.label, args.total_orgs, llm_classify=not args.skip_llm)
