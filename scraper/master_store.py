"""Master.xlsx as ONE persistent file, updated in place every run -- never
rebuilt from scratch.

Rules (all agreed with the user):
  - new verified event                     -> added
  - existing event, website unchanged      -> not touched
  - existing event, website changed and the new scrape verified
                                           -> only the changed fields updated, each one logged
  - existing event, new scrape failed/blank-> Master keeps its current values
  - row with Locked = Yes                  -> never touched
  - event date passed                      -> moved to Past Events
  - event gone from a healthy website      -> after 2 weeks moved to Needs Review
  - nothing is ever deleted without a record (Rejected sheet + Change Log)

A dated backup of the previous file is written before every save.
"""

import os
import shutil
from collections import Counter
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, PatternFill

from scraper.event_id import _normalize_url, make_event_id
from scraper.excel_writer import format_sheet

IDENTITY = ["Event ID", "Country", "Date", "End Date", "Status", "Event Name", "Organizer", "Category",
            "Format", "Location", "Description", "Register Link", "Source URL"]
BOOKKEEPING = ["Added On", "Last Updated", "Last Seen on Site", "Verified By", "Locked"]
VERIFIED_COLUMNS = IDENTITY + BOOKKEEPING
REVIEW_COLUMNS = VERIFIED_COLUMNS + ["Review Reason", "AI Review"]
REJECTED_COLUMNS = VERIFIED_COLUMNS + ["Reject Reason"]
CHANGE_LOG_COLUMNS = ["Timestamp", "Event ID", "Event Name", "Action", "Field", "Old Value", "New Value",
                      "Source", "Changed By"]
SHEET_COLUMNS = {"Verified Events": VERIFIED_COLUMNS, "Needs Review": REVIEW_COLUMNS,
                 "Past Events": VERIFIED_COLUMNS, "Rejected": REJECTED_COLUMNS}
UPDATABLE = ["Country", "Date", "End Date", "Event Name", "Category", "Format", "Location", "Description",
             "Register Link"]
MISSING_GRACE_DAYS = 14


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _parse(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d-%b-%Y")
    except (TypeError, ValueError):
        return None


def _fullness(row: dict) -> int:
    return sum(1 for c in UPDATABLE if row.get(c))


def record_to_row(rec: dict) -> dict:
    f = rec.get("fields", {})
    val = lambda k: f.get(k, {}).get("value", "")
    return {
        "Event ID": rec["event_id"], "Country": rec.get("country", ""), "Date": val("date"),
        "End Date": val("end_date"), "Status": "Upcoming", "Event Name": val("name"),
        "Organizer": rec.get("organizer", ""), "Category": rec.get("category", "") if rec.get("category") in
        ("Tax", "Finance", "Legal") else "", "Format": val("format"), "Location": val("location"),
        "Description": val("description"), "Register Link": val("link") or rec.get("link", ""),
        "Source URL": rec.get("source_url", ""),
    }


class MasterData:
    def __init__(self, path: str, today: datetime):
        self.path = path
        self.today = today.replace(hour=0, minute=0, second=0, microsecond=0)
        self.stamp = today.strftime("%Y-%m-%d %H:%M")
        self.day = today.strftime("%d-%b-%Y")
        self.verified: dict[str, dict] = {}
        self.review: dict[str, dict] = {}
        self.past: dict[str, dict] = {}
        self.rejected: dict[str, dict] = {}
        self.log: list[list] = []
        self.stats = {k: 0 for k in ("added", "updated_events", "unchanged", "kept_after_failed_rescrape",
                                     "promoted", "sent_to_review", "rejected", "moved_to_past",
                                     "not_found_to_review", "duplicates_skipped", "locked_skipped")}

    # ---- loading -----------------------------------------------------------
    def load(self) -> "MasterData":
        try:
            wb = openpyxl.load_workbook(self.path, data_only=True)
        except FileNotFoundError:
            return self
        for name, target in (("Verified Events", self.verified), ("Needs Review", self.review),
                             ("Past Events", self.past), ("Rejected", self.rejected)):
            for row in self._read_sheet(wb, name):
                self._add_unique(target, row)
        if "Change Log" in wb.sheetnames:
            self.log = [[_s(c) for c in r] for r in wb["Change Log"].iter_rows(min_row=2, values_only=True) if any(r)]
        return self

    def _read_sheet(self, wb, name: str) -> list[dict]:
        if name not in wb.sheetnames:
            return []
        rows = wb[name].iter_rows(values_only=True)
        header = [_s(h) for h in next(rows, [])]
        out = []
        for values in rows:
            if not values or not any(values):
                continue
            row = {h: _s(values[i]) for i, h in enumerate(header) if h and i < len(values)}
            if not row.get("Event Name"):
                continue
            if not row.get("Event ID"):  # a row from before Event IDs existed
                row["Event ID"] = make_event_id(row.get("Organizer", ""), row["Event Name"],
                                                row.get("Register Link", ""), row.get("Source URL", ""))
                row.setdefault("Verified By", "Legacy (before v2)")
                row["Verified By"] = row["Verified By"] or "Legacy (before v2)"
            for column in BOOKKEEPING:
                row.setdefault(column, "")
            row["Added On"] = row.get("Added On") or self.day
            row["Last Seen on Site"] = row.get("Last Seen on Site") or self.day  # starts the 2-week grace period
            out.append(row)
        return out

    @staticmethod
    def _add_unique(target: dict, row: dict) -> None:
        """Several events can share one page (a series or course page), so
        they share an Event ID. Every row is kept: later ones get a ~2, ~3
        suffix instead of overwriting the first."""
        base, eid, n = row["Event ID"], row["Event ID"], 2
        while eid in target:
            eid = f"{base}~{n}"
            n += 1
        row["Event ID"] = eid
        target[eid] = row

    # ---- helpers -----------------------------------------------------------
    def _log(self, row: dict, action: str, field: str = "", old: str = "", new: str = "", source: str = "",
             by: str = "Scraper") -> None:
        self.log.append([self.stamp, row.get("Event ID", ""), row.get("Event Name", ""), action, field, old, new,
                         source, by])

    @staticmethod
    def _locked(row: dict) -> bool:
        return _s(row.get("Locked")).lower() in ("yes", "y", "true", "1")

    def _new_row(self, row: dict, verified_by: str) -> dict:
        row = dict(row)
        row.update({"Added On": self.day, "Last Updated": self.day, "Last Seen on Site": self.day,
                    "Verified By": verified_by, "Locked": ""})
        return row

    def _find_duplicate(self, row: dict, own_id: str) -> str | None:
        key = (row["Event Name"].lower(), row["Date"])
        for eid, other in self.verified.items():
            if eid != own_id and (other["Event Name"].lower(), other["Date"]) == key:
                return eid
        return None

    # ---- applying one scraped record ----------------------------------------
    def apply_record(self, rec: dict) -> None:
        eid, status = rec["event_id"], rec["status"]
        existing = self.verified.get(eid)
        if status == "past" and existing:
            existing["Last Seen on Site"] = self.day  # still listed; its own date moves it to Past Events
        if status not in ("verified", "needs_review", "rejected"):
            return  # unreachable / shared page / past: nothing to apply, existing rows stay as they are
        new = record_to_row(rec)
        if existing and self._locked(existing):
            self.stats["locked_skipped"] += 1
            return

        if status == "verified":
            if existing:
                self._update_existing(existing, new, rec)
            elif eid in self.review:
                row = self.review.pop(eid)
                merged = self._new_row({**row, **{k: v for k, v in new.items() if v}}, "Rules")
                merged["Added On"] = row.get("Added On") or self.day
                self.verified[eid] = merged
                self.stats["promoted"] += 1
                self._log(merged, "promoted to Verified", source=rec.get("link", ""))
            elif (dup := self._find_duplicate(new, eid)):
                row = self.verified[dup]
                if str(row.get("Verified By", "")).startswith("Legacy") and not self._locked(row):
                    # an old, never-checked row: the verified event replaces it in place
                    del self.verified[dup]
                    row["Event ID"] = eid
                    self.verified[eid] = row
                    self._update_existing(row, new, {**rec, "changed": True})
                    row["Verified By"] = "Rules"
                    self._log(row, "legacy row upgraded to verified", source=rec.get("link", ""))
                else:
                    self.stats["duplicates_skipped"] += 1
            else:
                self.verified[eid] = self._new_row(new, "Rules")
                self.stats["added"] += 1
                self._log(self.verified[eid], "added", source=rec.get("link", ""))

        elif status == "needs_review":
            if existing:
                self.stats["kept_after_failed_rescrape"] += 1  # a worse scrape never overwrites a good row
                existing["Last Seen on Site"] = self.day
                return
            reason = "; ".join(rec.get("reasons", []))
            prior = self.review.get(eid)
            row = self._new_row({**(prior or {}), **{k: v for k, v in new.items() if v}}, "")
            if prior:
                row["Added On"] = prior.get("Added On") or self.day
            row["Review Reason"] = reason
            row["AI Review"] = prior.get("AI Review", "") if prior and not rec.get("changed", True) else ""
            if not prior:
                self.stats["sent_to_review"] += 1
                self._log(row, "sent to Needs Review", new=reason, source=rec.get("link", ""))
            self.review[eid] = row

        elif status == "rejected":
            reason = "; ".join(rec.get("reasons", []))
            source_row = self.verified.pop(eid, None) or self.review.pop(eid, None) or new
            row = self._new_row({**source_row, **{k: v for k, v in new.items() if v}}, "")
            row["Reject Reason"] = reason
            if eid not in self.rejected:
                self.stats["rejected"] += 1
                self._log(row, "rejected", new=reason, source=rec.get("link", ""))
            self.rejected[eid] = row

    def _update_existing(self, existing: dict, new: dict, rec: dict) -> None:
        existing["Last Seen on Site"] = self.day
        if not rec.get("changed", True):
            self.stats["unchanged"] += 1
            return
        changed = False
        for col in UPDATABLE:
            nv, ov = new.get(col, ""), existing.get(col, "")
            if nv and nv != ov:  # a blank never overwrites a value
                self._log(existing, "updated", col, ov, nv, rec.get("link", ""))
                existing[col] = nv
                changed = True
        if changed:
            existing["Last Updated"] = self.day
            existing["Verified By"] = "Rules"
            self.stats["updated_events"] += 1
        else:
            self.stats["unchanged"] += 1

    # ---- after all records ---------------------------------------------------
    def apply_missing_and_past(self, healthy_sources: set[str], seen_ids: set[str]) -> None:
        link_counts = Counter(_normalize_url(r.get("Register Link", "")) for r in self.verified.values()
                              if r.get("Register Link"))
        for eid, row in list(self.verified.items()):
            if self._locked(row):
                continue
            end = _parse(row.get("End Date", "")) or _parse(row.get("Date", ""))
            if end and end < self.today:
                row["Status"] = "Past"
                self.past[eid] = self.verified.pop(eid)
                self.stats["moved_to_past"] += 1
                self._log(row, "moved to Past Events", source="date passed")
                continue
            if eid in seen_ids or row.get("Source URL") not in healthy_sources:
                continue  # seen this run, or its website is unwell -- not the event's fault
            link = _normalize_url(row.get("Register Link", ""))
            if not link or link == _normalize_url(row.get("Source URL", "")) or link_counts[link] > 1:
                continue  # its page covers several events, so "not found" can't be judged for this one
            last_seen = _parse(row.get("Last Seen on Site", ""))
            if last_seen and (self.today - last_seen).days > MISSING_GRACE_DAYS:
                reason = f"Not found on its website since {row['Last Seen on Site']}"
                moved = self.verified.pop(eid)
                moved["Review Reason"] = reason
                moved["AI Review"] = ""
                self.review[eid] = moved
                self.stats["not_found_to_review"] += 1
                self._log(moved, "sent to Needs Review", new=reason, source="not on website")

    # ---- AI review outcomes --------------------------------------------------
    def promote_from_review(self, eid: str, updates: dict, reason: str) -> None:
        row = self.review.pop(eid)
        row.update({k: v for k, v in updates.items() if v})
        row.pop("Review Reason", None)
        row.pop("AI Review", None)
        promoted = self._new_row(row, "AI review")
        promoted["Added On"] = row.get("Added On") or self.day
        self.verified[eid] = promoted
        self.stats["promoted"] += 1
        self._log(promoted, "promoted to Verified", new=reason, by="AI review")

    def reject_from_review(self, eid: str, reason: str) -> None:
        row = self.review.pop(eid)
        row["Reject Reason"] = reason
        self.rejected[eid] = row
        self.stats["rejected"] += 1
        self._log(row, "rejected", new=reason, by="AI review")

    def note_ai_review(self, eid: str, note: str) -> None:
        self.review[eid]["AI Review"] = note

    # ---- saving --------------------------------------------------------------
    def save(self, archive_dir: str | None = None) -> None:
        if os.path.exists(self.path):
            archive_dir = archive_dir or os.path.join(os.path.dirname(self.path) or ".", "archive")
            os.makedirs(archive_dir, exist_ok=True)
            shutil.copy2(self.path, os.path.join(archive_dir, f"Master_{self.today:%Y-%m-%d}_{datetime.now():%H%M%S}.xlsx"))

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        def sort_key(r):
            return (r.get("Country", ""), _parse(r.get("Date", "")) or datetime.max)

        ws = wb.create_sheet("Stats")
        for c, h in enumerate(("Measure", "This run"), 1):
            cell = ws.cell(row=1, column=c, value=h)
            cell.font, cell.fill = Font(color="FFFFFF", bold=True), PatternFill("solid", fgColor="1F4E78")
        rows = [("Run", self.stamp), ("Verified Events (total)", len(self.verified)),
                ("Needs Review (total)", len(self.review)), ("Past Events (total)", len(self.past)),
                ("Rejected (total)", len(self.rejected))] + [(k.replace("_", " ").capitalize(), v)
                                                            for k, v in self.stats.items()]
        for r, (k, v) in enumerate(rows, 2):
            ws.cell(row=r, column=1, value=k)
            ws.cell(row=r, column=2, value=v)
        ws.column_dimensions["A"].width = 36
        ws.column_dimensions["B"].width = 22

        for name, data in (("Verified Events", self.verified), ("Needs Review", self.review),
                           ("Past Events", self.past), ("Rejected", self.rejected)):
            columns = SHEET_COLUMNS[name]
            sheet = wb.create_sheet(name)
            sheet.append(columns)
            for row in sorted(data.values(), key=sort_key):
                sheet.append([row.get(c, "") for c in columns])
            format_sheet(sheet, columns)

        log_sheet = wb.create_sheet("Change Log")
        log_sheet.append(CHANGE_LOG_COLUMNS)
        for entry in self.log:
            log_sheet.append(entry)
        for c, h in enumerate(CHANGE_LOG_COLUMNS, 1):
            cell = log_sheet.cell(row=1, column=c)
            cell.font, cell.fill = Font(color="FFFFFF", bold=True), PatternFill("solid", fgColor="1F4E78")
        log_sheet.freeze_panes = "A2"

        tmp = self.path + ".tmp"
        wb.save(tmp)
        os.replace(tmp, self.path)  # a crash mid-save never leaves a half-written Master
