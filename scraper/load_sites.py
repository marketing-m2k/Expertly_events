"""Read the target organizations/events-pages from the master workbook."""

import openpyxl


def load_organizations(path: str, sheet_name: str = "Organizations") -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name]

    orgs = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[4]:
            continue
        _, name, category, country, events_page = row[:5]
        orgs.append({
            "organizer": name,
            "category": category,
            "country": country,
            "url": events_page,
        })
    return orgs
