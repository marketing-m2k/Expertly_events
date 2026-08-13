"""Append scraped events to an Excel workbook, skipping duplicates."""

from datetime import datetime

import openpyxl

from scraper.date_utils import is_past_event, parse_event_date

COLUMNS = [
    "Date",
    "Category",
    "Format",
    "Event Name",
    "Description",
    "Location",
    "Country",
    "Organizer",
    "Register Link",
    "Source URL",
]


def _dedupe_key(row: dict) -> tuple:
    return (
        (row.get("event_name") or "").strip().lower(),
        (row.get("date") or "").strip().lower(),
        (row.get("organizer") or "").strip().lower(),
    )


def upsert_events(path: str, sheet_name: str, events: list[dict]) -> int:
    try:
        wb = openpyxl.load_workbook(path)
    except FileNotFoundError:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)

    if sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
    else:
        ws = wb.create_sheet(sheet_name)
        ws.append(COLUMNS)

    existing_keys = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row:
            continue
        existing_keys.add((str(row[3] or "").strip().lower(),
                            str(row[0] or "").strip().lower(),
                            str(row[7] or "").strip().lower()))

    added = 0
    for event in events:
        if is_past_event(event.get("date", "")):
            continue
        key = _dedupe_key(event)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        ws.append([
            event.get("date", ""),
            event.get("category", ""),
            event.get("format", ""),
            event.get("event_name", ""),
            event.get("description", ""),
            event.get("location_city", ""),
            event.get("country", ""),
            event.get("organizer", ""),
            event.get("link", ""),
            event.get("source_url", ""),
        ])
        added += 1

    wb.save(path)
    return added


def sort_and_link(path: str, sheet_name: str):
    """Sort the sheet soonest-upcoming-first, drop any past-dated rows left
    over from earlier runs, and turn URL columns into clickable hyperlinks."""
    wb = openpyxl.load_workbook(path)
    if sheet_name not in wb.sheetnames:
        return
    ws = wb[sheet_name]

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    if not rows:
        return

    today = datetime.now()
    rows = [r for r in rows if not is_past_event(r[0], today)]

    def sort_key(row):
        dt = parse_event_date(row[0], today)
        return dt if dt is not None else datetime.max

    rows.sort(key=sort_key)

    ws.delete_rows(2, ws.max_row)
    for row in rows:
        ws.append(row)

    link_col_idx = {name: i + 1 for i, name in enumerate(COLUMNS)}
    for col_name in ("Register Link", "Source URL"):
        col = link_col_idx[col_name]
        for row_idx in range(2, ws.max_row + 1):
            cell = ws.cell(row=row_idx, column=col)
            url = cell.value
            if url and str(url).startswith("http"):
                cell.hyperlink = url
                cell.style = "Hyperlink"

    wb.save(path)
