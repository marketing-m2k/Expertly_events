"""Run v1's own list -> event page -> classify -> verify pipeline (no Gemini) on the baseline organisations, so the
integration test can compare OLD V1 vs NEW PIPELINE on the same events. Uses the unmodified copy of v1 in ./scraper
and writes ONLY to reports/ (never to v1's output folder)."""

import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, ".")
from measure.match import load_baselines
from scraper.enrich_lists import enrich_country_from_lists

today = datetime(2026, 10, 7)
orgs = [{"organizer": b["org"], "country": b.get("country", ""), "category": "", "url": b["url"]} for b in load_baselines("baseline")]
state_dir = tempfile.mkdtemp()
records, counts = enrich_country_from_lists("BASELINE", orgs, state_dir, today, workers=6)
out = {"counts": counts, "records": records}
os.makedirs("reports", exist_ok=True)
with open("reports/v1_pipeline_records.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=1, default=str)
from collections import Counter
print(Counter(r["status"] for r in records))
print(len(records), "records ->", "reports/v1_pipeline_records.json")
