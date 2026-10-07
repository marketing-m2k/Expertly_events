"""Master.xlsx -- generated FROM the event store.

  Verified Events    VERIFIED + CORRECTED, upcoming. Only properly verified results are ever here.
  Past Events        PAST (verified date has passed)
  Needs Review       QUALITY_REVIEW + NEEDS_AI_REVIEW + NEEDS_REVIEW  (with the reasons and flags)
  Rejected           REJECTED, each with its reason, evidence quote, official URL, timestamp and pipeline version
  Fetch Failed       FETCH_FAILED (the official page could not be opened; retried next run, never rejected)
  Duplicates         records merged into another event, with what they duplicate
  Org Health         per organisation: discovery_complete / incomplete / failed and why
  Status History / Correction History   the audit trail
  QC Summary         the run's QC numbers

The first 13 columns match v1's Master so downstream consumers keep working. Rows with Locked = Yes are never touched.
Existing locks are read back from the previous file before it is overwritten (a dated backup is kept).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime

import openpyxl
from openpyxl.styles import Font, PatternFill

from pipeline.dates import display, parse_iso
from pipeline.models import Status
from pipeline.store import Store
from scraper.country_rules import _infer_country_from_location

CORE = ["Event ID", "Country", "Date", "End Date", "Status", "Event Name", "Organizer", "Category", "Format", "Location", "Description", "Register Link", "Source URL"]
EXTRA = ["Start Time", "End Time", "Timezone", "Venue", "Address", "City", "Region", "Event URL", "Verified By", "Extractor Version", "Pipeline Version",
         "First Discovered", "Last Discovered", "Verified At", "Quality Flags", "Locked"]
VERIFIED_COLS = CORE + EXTRA
REVIEW_COLS = CORE + ["Review Reasons", "Quality Flags", "AI Status", "Start Time", "Venue", "Address", "Event URL", "Extractor Version", "First Discovered", "Last Discovered", "Locked"]
REJECTED_COLS = CORE + ["Reject Reason", "Evidence Quote", "Official URL", "Rejected At", "Pipeline Version", "Extractor Version", "Locked"]
FAILED_COLS = ["Event ID", "Country", "Organizer", "Event Name", "Event URL", "Failure", "Last Discovered", "Extractor Version"]
DUP_COLS = ["Event ID", "Duplicate Of", "Organizer", "Event Name", "Date", "Location", "Event URL"]   # Event URL = the other listing merged into the kept event

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")


def _f(fields: dict, k: str) -> str:
    return (fields.get(k) or {}).get("value") or ""


def row_dict(r: dict) -> dict:
    fields = json.loads(r.get("fields_json") or "{}")
    reasons = json.loads(r.get("reasons_json") or "[]")
    flags = json.loads(r.get("flags_json") or "[]")
    loc = r.get("location") or ""
    country = _infer_country_from_location(_f(fields, "address") + " " + loc + " " + _f(fields, "country")) or r.get("country", "")
    rej = json.loads(r["rejection_json"]) if r.get("rejection_json") else {}
    return {
        "Event ID": r["event_id"], "Country": country, "Date": display(r.get("start_date")), "End Date": display(r.get("end_date")),
        "Status": r["status"], "Event Name": r.get("name") or "", "Organizer": r.get("org") or "", "Category": r.get("category") or "",
        "Format": r.get("format") or "", "Location": loc, "Description": (_f(fields, "description") or "")[:600],
        "Register Link": _f(fields, "registration_url") or r.get("url") or "", "Source URL": r.get("source_url") or "",
        "Start Time": _f(fields, "start_time"), "End Time": _f(fields, "end_time"), "Timezone": _f(fields, "timezone"),
        "Venue": _f(fields, "venue"), "Address": _f(fields, "address"), "City": _f(fields, "city"), "Region": _f(fields, "region"),
        "Event URL": r.get("url") or "", "Verified By": r.get("verified_by") or "", "Extractor Version": r.get("extractor_version") or "",
        "Pipeline Version": r.get("pipeline_version") or "", "First Discovered": (r.get("first_discovered") or "")[:10],
        "Last Discovered": (r.get("last_discovered") or "")[:10], "Verified At": (r.get("verified_at") or "")[:10],
        "Quality Flags": ", ".join(flags), "Locked": "Yes" if r.get("locked") else "",
        "Review Reasons": " | ".join(reasons)[:900], "AI Status": r.get("ai_status") or "",
        "Reject Reason": rej.get("reason", " | ".join(reasons)[:500]), "Evidence Quote": rej.get("evidence_quote", ""),
        "Official URL": rej.get("source_url") or r.get("url") or "", "Rejected At": (r.get("verified_at") or "")[:19],
        "Failure": " | ".join(reasons)[:400],
    }


def import_locks(store: Store, master_path: str) -> int:
    """Rows locked by a person in the existing Master stay locked."""
    if not os.path.exists(master_path):
        return 0
    n = 0
    try:
        wb = openpyxl.load_workbook(master_path, read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        return 0
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = [str(h or "") for h in next(rows, [])]
        if "Locked" not in header or "Event ID" not in header:
            continue
        li, ei = header.index("Locked"), header.index("Event ID")
        for r in rows:
            if r and len(r) > max(li, ei) and str(r[li] or "").strip().lower() in ("yes", "y", "true", "1") and r[ei]:
                store.set_lock(str(r[ei]), True)
                n += 1
    wb.close()
    return n


def _sheet(wb, title: str, cols: list[str], rows: list[dict]) -> None:
    ws = wb.create_sheet(title)
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font, cell.fill = Font(color="FFFFFF", bold=True), HEADER_FILL
    for r in rows:
        ws.append([r.get(c, "") for c in cols])
    ws.freeze_panes = "A2"
    for i, c in enumerate(cols, 1):
        width = 14
        if c in ("Event Name", "Organizer", "Location", "Review Reasons", "Reject Reason", "Evidence Quote", "Failure"):
            width = 42
        if c in ("Description",):
            width = 60
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width


def export_master(store: Store, path: str, today: date, qc: dict | None = None, run_id: str | None = None) -> dict:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        arch = os.path.join(os.path.dirname(path) or ".", "archive")
        os.makedirs(arch, exist_ok=True)
        shutil.copy2(path, os.path.join(arch, f"Master_v2_{today:%Y-%m-%d}_{datetime.now():%H%M%S}.xlsx"))
    rows = [r for r in store.rows("dup_of IS NULL")]
    by = lambda *sts: sorted((row_dict(r) for r in rows if r["status"] in sts), key=lambda d: (d["Country"], parse_iso(_to_iso(d["Date"])) or date.max, d["Event Name"]))
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    verified = by(Status.VERIFIED, Status.CORRECTED)
    _sheet(wb, "Verified Events", VERIFIED_COLS, verified)
    _sheet(wb, "Past Events", VERIFIED_COLS, by(Status.PAST))
    _sheet(wb, "Needs Review", REVIEW_COLS, by(Status.NEEDS_REVIEW, Status.QUALITY_REVIEW, Status.NEEDS_AI_REVIEW))
    _sheet(wb, "Rejected", REJECTED_COLS, by(Status.REJECTED))
    _sheet(wb, "Fetch Failed", FAILED_COLS, by(Status.FETCH_FAILED))
    dups = []
    for r in store.rows("alt_urls_json IS NOT NULL"):
        d = row_dict(r)
        for u in json.loads(r["alt_urls_json"] or "[]"):
            dups.append({"Event ID": r["event_id"], "Duplicate Of": f"{r['event_id']} (kept)", "Organizer": d["Organizer"], "Event Name": d["Event Name"],
                         "Date": d["Date"], "Location": d["Location"], "Event URL": u})
    _sheet(wb, "Duplicates", DUP_COLS, dups)
    org_rows = []
    with store.lock:
        last = store.db.execute("SELECT MAX(run_id) m FROM org_runs").fetchone()["m"]
        for r in store.db.execute("SELECT * FROM org_runs WHERE run_id=?", (last,)).fetchall() if last else []:
            org_rows.append({"Organizer": r["org"], "Country": r["country"], "Source URL": r["source_url"], "State": r["state"], "Why": " | ".join(json.loads(r["reasons_json"] or "[]"))[:500],
                             "Candidates": r["n_candidates"], "Dropped": r["n_dropped"], "Failure Kinds": r["failure_kinds"], "Errors": " | ".join(json.loads(r["errors_json"] or "[]"))[:300]})
    _sheet(wb, "Org Health", ["Organizer", "Country", "Source URL", "State", "Why", "Candidates", "Dropped", "Failure Kinds", "Errors"], org_rows)
    _sheet(wb, "Status History", ["event_id", "ts", "run_id", "from_status", "to_status", "reason", "actor", "pipeline_version", "extractor_version"], store.history("status_history"))
    _sheet(wb, "Correction History", ["event_id", "ts", "run_id", "field", "old_value", "new_value", "evidence_quote", "source_url", "reason", "by"], store.history("correction_history"))
    if qc:
        ws = wb.create_sheet("QC Summary", 0)
        ws.append(["Measure", "Value"])
        for k, v in qc.get("headline", {}).items():
            ws.append([k, v if not isinstance(v, (dict, list)) else json.dumps(v)])
        ws.column_dimensions["A"].width = 52
        ws.column_dimensions["B"].width = 40
    tmp = path + ".tmp"
    wb.save(tmp)
    os.replace(tmp, path)
    return {"verified": len(verified), "path": path}


def _to_iso(disp: str) -> str:
    try:
        return datetime.strptime(disp, "%d-%b-%Y").date().isoformat() if disp else ""
    except ValueError:
        return ""
