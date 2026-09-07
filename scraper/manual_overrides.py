"""Manually-verified events persist here so they survive the weekly
automated regeneration of each country's final workbook (which otherwise
rebuilds Upcoming - Incomplete / Upcoming - Verified from scratch every
run, discarding any Status edit made directly in last week's file).

Workflow:
  1. A human reviews a country's "Upcoming - Incomplete" sheet, fills in
     the real Date (and End Date, if it's a multi-day event) by hand, and
     types "Verified" into the Status column for that row.
  2. Running `python -m scraper.build_master` captures every such row
     across all countries' current workbooks into this file (skipping any
     row marked Verified without an actual date filled in), then rebuilds
     Master.xlsx from every country's Verified sheet plus every recorded
     override.
  3. clean_events.py also reads this file on each weekly run, so a
     manually-verified event's corrected row is substituted back in before
     classification -- it lands in that country's own Upcoming - Verified
     sheet again next Monday instead of falling back to Incomplete.
"""

import json
import os

PATH = "output/manual_verifications.json"


def _key(country: str, name: str) -> str:
    return f"{(country or '').strip().lower()}|{(name or '').strip().lower()}"


def load() -> dict:
    if not os.path.exists(PATH):
        return {}
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)


def save(overrides: dict) -> None:
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, sort_keys=True)


def record(overrides: dict, country: str, name: str, row: list) -> None:
    overrides[_key(country, name)] = {"country": country, "name": name, "row": row}


def get(overrides: dict, country: str, name: str) -> dict | None:
    return overrides.get(_key(country, name))
