"""Read the target organizations/events-pages from the master workbook.

Column layout varies between master files (some have a Country column, some
don't; header names differ: "Organization" vs "Organisation", "Category" vs
"Focus"), so columns are matched by header name instead of hardcoded index.
"""

import openpyxl

HEADER_ALIASES = {
    "organizer": {"organization", "organisation", "org", "name"},
    "category": {"category", "focus"},
    "country": {"country"},
    "url": {"events page", "event page", "url", "link", "official event / calendar"},
}


def _map_headers(header_row: tuple) -> dict[str, int]:
    mapping = {}
    for idx, cell in enumerate(header_row):
        key = str(cell or "").strip().lower()
        for field, aliases in HEADER_ALIASES.items():
            if field in mapping:
                continue  # first (leftmost) matching column wins for a field
            # substring match, not exact -- a source tab's header is
            # sometimes phrased as "Organisation / Website" or similar
            # rather than a bare "Organisation", and an exact-match lookup
            # would silently fail to find the organizer/url columns at all.
            if any(alias in key for alias in aliases):
                mapping[field] = idx
    return mapping


def load_organizations(path: str, sheet_name: str | None = None) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb[wb.sheetnames[0]]

    rows = ws.iter_rows(min_row=1, values_only=True)
    header = next(rows, None)
    if header is None:
        return []
    cols = _map_headers(header)
    if "organizer" not in cols or "url" not in cols:
        raise ValueError(f"Could not find organizer/url columns in {path}!{ws.title} header: {header}")

    orgs = []
    for row in rows:
        if not row:
            continue
        url = row[cols["url"]] if cols["url"] < len(row) else None
        if not url:
            continue
        orgs.append({
            "organizer": row[cols["organizer"]] if cols["organizer"] < len(row) else "",
            "category": row[cols["category"]] if "category" in cols and cols["category"] < len(row) else "",
            "country": row[cols["country"]] if "country" in cols and cols["country"] < len(row) else "",
            "url": url,
        })
    return orgs
