"""The persistent event store (SQLite). Master.xlsx is produced FROM this -- never the other way round, never rebuilt blind.

Kept for every event, so nothing useful is lost and nothing is silently overwritten:
  events              current best record (+ the full source-of-truth packet and field evidence as JSON)
  status_history      every status transition with reason, run, pipeline + extractor version
  correction_history  every AI correction with its evidence quote and source URL
  field_history       previous values of a field whenever re-extraction changed it
  ledger              the traceability table: every discovered candidate, every stage, every outcome
  org_runs            per organisation per run: discovery verdict and its signals
  raw_pages           compressed copies of the official pages that were read (the raw evidence)
  runs                what ran, with which versions
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from pipeline.common import now_iso, sha
from pipeline.models import EXTRACTOR_VERSION, PIPELINE_VERSION, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, pipeline_version TEXT, extractor_version TEXT,
    config_json TEXT, summary_json TEXT);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, org TEXT, country TEXT, source_url TEXT, url TEXT, url_key TEXT, ext_key TEXT, nal_key TEXT, nad_key TEXT,
    status TEXT, category TEXT, name TEXT, start_date TEXT, end_date TEXT, location TEXT, format TEXT, flags_json TEXT, reasons_json TEXT,
    fields_json TEXT, packet_json TEXT, extractor_version TEXT, classifier_version TEXT, prompt_version TEXT, pipeline_version TEXT,
    input_hash TEXT, ai_status TEXT, verified_by TEXT, first_discovered TEXT, last_discovered TEXT, last_changed TEXT, verified_at TEXT,
    locked INTEGER DEFAULT 0, dup_of TEXT, possible_dup_of TEXT, last_run_id TEXT, shared_url INTEGER DEFAULT 0, rejection_json TEXT,
    last_attempt_json TEXT, alt_urls_json TEXT);
CREATE INDEX IF NOT EXISTS ix_events_url ON events(url_key);
CREATE INDEX IF NOT EXISTS ix_events_nal ON events(nal_key);
CREATE INDEX IF NOT EXISTS ix_events_status ON events(status);
CREATE INDEX IF NOT EXISTS ix_events_org ON events(org);
CREATE TABLE IF NOT EXISTS status_history (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, ts TEXT, run_id TEXT, from_status TEXT,
    to_status TEXT, reason TEXT, actor TEXT, pipeline_version TEXT, extractor_version TEXT);
CREATE INDEX IF NOT EXISTS ix_sh_event ON status_history(event_id);
CREATE TABLE IF NOT EXISTS correction_history (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, ts TEXT, run_id TEXT, field TEXT,
    old_value TEXT, new_value TEXT, evidence_quote TEXT, source_url TEXT, reason TEXT, by TEXT);
CREATE TABLE IF NOT EXISTS field_history (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, ts TEXT, run_id TEXT, field TEXT, old_value TEXT,
    new_value TEXT, source TEXT, extractor_version TEXT);
CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, org TEXT, candidate_id TEXT, event_id TEXT,
    stage TEXT, outcome TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_ledger_run ON ledger(run_id);
CREATE INDEX IF NOT EXISTS ix_ledger_cand ON ledger(candidate_id);
CREATE TABLE IF NOT EXISTS org_runs (run_id TEXT, org TEXT, country TEXT, source_url TEXT, state TEXT, reasons_json TEXT, signals_json TEXT,
    n_candidates INTEGER, n_dropped INTEGER, failure_kinds TEXT, errors_json TEXT, pages_json TEXT, PRIMARY KEY (run_id, org, source_url));
CREATE TABLE IF NOT EXISTS raw_pages (event_id TEXT, sha TEXT, url TEXT, path TEXT, fetched_at TEXT, status INTEGER, method TEXT, bytes INTEGER,
    PRIMARY KEY (event_id, sha));
"""


class Store:
    def __init__(self, path: str, evidence_dir: str | None = None):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.evidence_dir = evidence_dir or os.path.join(os.path.dirname(path) or ".", "evidence")
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
        self._ledger_buf: list[tuple] = []

    def close(self) -> None:
        self.flush()
        self.db.close()

    # ---- runs ---------------------------------------------------------------------------------------------------------------------
    def start_run(self, config: dict) -> str:
        run_id = datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                            (run_id, now_iso(), None, PIPELINE_VERSION, EXTRACTOR_VERSION, json.dumps(config, default=str), None))
            self.db.commit()
        return run_id

    def finish_run(self, run_id: str, summary: dict) -> None:
        self.flush()
        with self.lock:
            self.db.execute("UPDATE runs SET finished_at=?, summary_json=? WHERE run_id=?", (now_iso(), json.dumps(summary, default=str), run_id))
            self.db.commit()

    # ---- ledger (traceability) ------------------------------------------------------------------------------------------------------
    def ledger(self, run_id: str, org: str, candidate_id: str, event_id: str, stage: str, outcome: str, reason: str = "") -> None:
        self._ledger_buf.append((run_id, now_iso(), org, candidate_id, event_id, stage, outcome, (reason or "")[:500]))
        if len(self._ledger_buf) >= 500:
            self.flush()

    def flush(self) -> None:
        if self._ledger_buf:
            with self.lock:
                self.db.executemany("INSERT INTO ledger (run_id,ts,org,candidate_id,event_id,stage,outcome,reason) VALUES (?,?,?,?,?,?,?,?)", self._ledger_buf)
                self.db.commit()
            self._ledger_buf = []

    def save_org_run(self, run_id: str, res) -> None:
        c = res.completeness or {}
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO org_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, res.org, res.country, res.source_url, c.get("state", ""), json.dumps(c.get("reasons", [])),
                             json.dumps(c.get("signals", {}), default=str), len(res.candidates), len(res.dropped), ",".join(res.failure_kinds),
                             json.dumps(res.errors), json.dumps(res.pages[:30], default=str)))
            self.db.commit()

    # ---- raw evidence ---------------------------------------------------------------------------------------------------------------
    def save_page(self, event_id: str, url: str, html: str | None, status: int | None, method: str) -> str:
        if not html:
            return ""
        h = sha(html, 16)
        d = os.path.join(self.evidence_dir, h[:2])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, h + ".html.gz")
        if not os.path.exists(path):
            with gzip.open(path, "wt", encoding="utf-8") as f:
                f.write(html)
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO raw_pages VALUES (?,?,?,?,?,?,?,?)", (event_id, h, url, path, now_iso(), status, method, len(html)))
            self.db.commit()
        return h

    def load_page(self, event_id: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT path FROM raw_pages WHERE event_id=? ORDER BY fetched_at DESC LIMIT 1", (event_id,)).fetchone()
        if row and os.path.exists(row["path"]):
            with gzip.open(row["path"], "rt", encoding="utf-8") as f:
                return f.read()
        return None

    # ---- events ---------------------------------------------------------------------------------------------------------------------
    def get(self, event_id: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(r) if r else None

    def find_existing(self, keys: dict, org: str) -> tuple[dict | None, str, list[dict]]:
        """-> (existing row, how it matched, possible duplicates). Never merges on a name+date match with a missing location."""
        with self.lock:
            if keys.get("url_key"):
                r = self.db.execute("SELECT * FROM events WHERE url_key=? AND dup_of IS NULL", (keys["url_key"],)).fetchone()
                if r:
                    return dict(r), "url", []
            if keys.get("ext_key"):
                r = self.db.execute("SELECT * FROM events WHERE ext_key=? AND dup_of IS NULL", (keys["ext_key"],)).fetchone()
                if r:
                    return dict(r), "external_id", []
            if keys.get("nal_key"):
                r = self.db.execute("SELECT * FROM events WHERE nal_key=? AND dup_of IS NULL", (keys["nal_key"],)).fetchone()
                if r:
                    return dict(r), "name+date+location", []
            possible = []
            if keys.get("nad_key"):
                possible = [dict(x) for x in self.db.execute("SELECT * FROM events WHERE nad_key=? AND dup_of IS NULL", (keys["nad_key"],)).fetchall()]
        return None, "", possible

    def log_status(self, event_id: str, run_id: str, old: str | None, new: str, reason: str, actor: str = "pipeline") -> None:
        with self.lock:
            self.db.execute("INSERT INTO status_history (event_id,ts,run_id,from_status,to_status,reason,actor,pipeline_version,extractor_version) VALUES (?,?,?,?,?,?,?,?,?)",
                            (event_id, now_iso(), run_id, old, new, (reason or "")[:600], actor, PIPELINE_VERSION, EXTRACTOR_VERSION))
            self.db.commit()

    def log_correction(self, event_id: str, run_id: str, h: dict, by: str = "gemini") -> None:
        with self.lock:
            self.db.execute("INSERT INTO correction_history (event_id,ts,run_id,field,old_value,new_value,evidence_quote,source_url,reason,by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (event_id, now_iso(), run_id, h["field"], h["old"], h["new"], h["evidence_quote"], h["source_url"], h["reason"], by))
            self.db.commit()

    def log_field_change(self, event_id: str, run_id: str, fld: str, old: str, new: str, source: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO field_history (event_id,ts,run_id,field,old_value,new_value,source,extractor_version) VALUES (?,?,?,?,?,?,?,?)",
                            (event_id, now_iso(), run_id, fld, old, new, source, EXTRACTOR_VERSION))
            self.db.commit()

    def upsert(self, rec: dict, run_id: str) -> tuple[str, str]:
        """rec = a fully processed event (see process.EventResult.to_row). -> (action, event_id).
        action: added | updated | unchanged | kept_previous | duplicate_of | locked_skip"""
        now = now_iso()
        eid = rec["event_id"]
        keys = {k: rec.get(k, "") for k in ("url_key", "ext_key", "nal_key", "nad_key")}
        existing, how, possible = self.find_existing(keys, rec["org"])
        if existing is not None:
            eid = existing["event_id"]
            rec["event_id"] = eid
        elif self.get(eid):                  # same event id, different keys
            existing = self.get(eid)
        if existing is not None and existing.get("locked"):
            self.ledger(run_id, rec["org"], rec.get("candidate_id", ""), eid, "master", "locked_skip", "the row is locked: the scraper never touches it")
            return "locked_skip", eid

        if existing is None:
            dup = None
            row = self._row(rec, run_id, now)
            row["first_discovered"] = now
            row["possible_dup_of"] = ",".join(p["event_id"] for p in possible) or None
            row["dup_of"] = dup
            self._insert(row)
            prev = None
            for st, why in rec.get("lifecycle", []):           # DISCOVERED -> EXTRACTED happened in this run: the history shows them
                self.log_status(eid, run_id, prev, st, why, "pipeline")
                prev = st
            self.log_status(eid, run_id, prev, rec["status"], "; ".join(rec.get("reasons", []))[:500], rec.get("verified_by") or "pipeline")
            return ("duplicate_of" if dup else "added"), eid

        # ---- the same event seen under a different URL (matched by external id / name+date+location) ------------------------------------
        if how in ("external_id", "name+date+location") and rec.get("url_key") and existing.get("url_key") and rec["url_key"] != existing["url_key"]:
            publishable = (Status.VERIFIED, Status.CORRECTED)
            better = rec["status"] in publishable and existing["status"] not in publishable
            alts = json.loads(existing.get("alt_urls_json") or "[]")
            other = rec.get("url") if better is False else existing.get("url")
            if other and other not in alts:
                alts.append(other)
            with self.lock:
                self.db.execute("UPDATE events SET alt_urls_json=?, last_discovered=?, last_run_id=? WHERE event_id=?", (json.dumps(alts), now, run_id, eid))
                self.db.commit()
            self.log_status(eid, run_id, existing["status"], existing["status"],
                            f"the same event was also listed at {other} (matched by {how}); merged, not duplicated", "pipeline")
            if not better:
                return "duplicate_of", eid
            # the new listing is the better-verified copy: it becomes the record, the old URL is kept as an alternative

        # ---- existing record --------------------------------------------------------------------------------------------------------
        old_status, new_status = existing["status"], rec["status"]
        worse = new_status in (Status.FETCH_FAILED, Status.QUALITY_REVIEW, Status.NEEDS_AI_REVIEW) and old_status in (Status.VERIFIED, Status.CORRECTED, Status.PAST)
        attempt = {"run_id": run_id, "ts": now, "status": new_status, "reasons": rec.get("reasons", [])[:4], "flags": rec.get("flags", [])}
        with self.lock:
            self.db.execute("UPDATE events SET last_discovered=?, last_run_id=?, last_attempt_json=?, url=COALESCE(NULLIF(?, ''), url) WHERE event_id=?",
                            (now, run_id, json.dumps(attempt), rec.get("url", ""), eid))
            self.db.commit()
        if worse:
            self.log_status(eid, run_id, old_status, old_status, f"this run could not re-verify the event ({new_status}); the previous verified record was kept: "
                            + "; ".join(rec.get("reasons", []))[:300], "pipeline")
            return "kept_previous", eid

        old_fields = json.loads(existing.get("fields_json") or "{}")
        changed = []
        for k, v in (rec.get("fields") or {}).items():
            ov = (old_fields.get(k) or {}).get("value")
            nv = v.get("value")
            if k != "source_url" and (ov or "") != (nv or "") and (ov or nv):
                changed.append((k, ov or "", nv or "", v.get("source", "")))
        same_version = existing.get("extractor_version") == rec.get("extractor_version")
        if not changed and old_status == new_status and same_version and (existing.get("input_hash") or "") == (rec.get("input_hash") or ""):
            return "unchanged", eid
        for k, ov, nv, src in changed:
            self.log_field_change(eid, run_id, k, ov, nv, src)
        row = self._row(rec, run_id, now)
        row["first_discovered"] = existing["first_discovered"]
        row["alt_urls_json"] = existing.get("alt_urls_json")
        row["locked"] = existing.get("locked") or 0
        row["possible_dup_of"] = existing.get("possible_dup_of") or (",".join(p["event_id"] for p in possible if p["event_id"] != eid) or None)
        self._insert(row, replace=True)
        if old_status != new_status:
            self.log_status(eid, run_id, old_status, new_status, "; ".join(rec.get("reasons", []))[:500], rec.get("verified_by") or "pipeline")
        return "updated", eid

    @staticmethod
    def _row(rec: dict, run_id: str, now: str) -> dict:
        f = rec.get("fields") or {}
        g = lambda k: (f.get(k) or {}).get("value") or ""
        loc = ", ".join(x for x in (g("venue"), g("address") if not g("venue") else "", g("city"), g("region"), g("country")) if x)
        return {"event_id": rec["event_id"], "org": rec["org"], "country": rec.get("country", ""), "source_url": rec.get("source_url", ""),
                "url": rec.get("url", ""), "url_key": rec.get("url_key", ""), "ext_key": rec.get("ext_key", ""), "nal_key": rec.get("nal_key", ""),
                "nad_key": rec.get("nad_key", ""), "status": rec["status"], "category": rec.get("category", ""), "name": g("name"),
                "start_date": g("start_date"), "end_date": g("end_date"), "location": loc, "format": g("format"),
                "flags_json": json.dumps(rec.get("flags", [])), "reasons_json": json.dumps(rec.get("reasons", [])),
                "fields_json": json.dumps(f, default=str), "packet_json": json.dumps(rec.get("packet", {}), default=str)[:200000],
                "extractor_version": rec.get("extractor_version", EXTRACTOR_VERSION), "classifier_version": rec.get("classifier_version", ""),
                "prompt_version": rec.get("prompt_version", ""), "pipeline_version": PIPELINE_VERSION, "input_hash": rec.get("input_hash", ""),
                "ai_status": rec.get("ai_status", ""), "verified_by": rec.get("verified_by", ""), "last_discovered": now,
                "last_changed": now, "verified_at": now if rec["status"] in (Status.VERIFIED, Status.CORRECTED, Status.PAST, Status.REJECTED) else None,
                "locked": 0, "dup_of": None, "possible_dup_of": None, "last_run_id": run_id, "shared_url": 1 if rec.get("shared_url") else 0, "alt_urls_json": None,
                "rejection_json": json.dumps(rec["rejection"]) if rec.get("rejection") else None, "last_attempt_json": None}

    def _insert(self, row: dict, replace: bool = False) -> None:
        cols = ",".join(row)
        qs = ",".join("?" * len(row))
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO events ({cols}) VALUES ({qs})" if replace else f"INSERT INTO events ({cols}) VALUES ({qs})", tuple(row.values()))
            self.db.commit()

    # ---- queries ----------------------------------------------------------------------------------------------------------------------
    def rows(self, where: str = "", args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM events" + (f" WHERE {where}" if where else ""), args).fetchall()]

    def count_by_status(self) -> dict:
        with self.lock:
            return {r["status"]: r["n"] for r in self.db.execute("SELECT status, COUNT(*) n FROM events WHERE dup_of IS NULL GROUP BY status").fetchall()}

    def previous_org_counts(self, before_run: str | None = None) -> dict[str, int]:
        """source_url -> number of candidates the last COMPLETE discovery found (the 'previous baseline')."""
        with self.lock:
            rows = self.db.execute("SELECT source_url, n_candidates, state, run_id FROM org_runs ORDER BY run_id DESC").fetchall()
        out: dict[str, int] = {}
        for r in rows:
            if before_run and r["run_id"] >= before_run:
                continue
            if r["source_url"] not in out and r["state"] == "discovery_complete":
                out[r["source_url"]] = r["n_candidates"]
        return out

    def history(self, table: str, event_id: str | None = None) -> list[dict]:
        with self.lock:
            q = f"SELECT * FROM {table}" + (" WHERE event_id=?" if event_id else "") + " ORDER BY id"
            return [dict(r) for r in self.db.execute(q, (event_id,) if event_id else ()).fetchall()]

    def set_lock(self, event_id: str, locked: bool) -> None:
        with self.lock:
            self.db.execute("UPDATE events SET locked=? WHERE event_id=?", (1 if locked else 0, event_id))
            self.db.commit()
