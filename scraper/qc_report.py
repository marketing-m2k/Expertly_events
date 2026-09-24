"""Stage 10: the weekly QC report -- what was checked, what changed, what
went wrong and what needs a person. Written to output/summaries/ and
included in the emailed digest."""

import json
import os
from collections import Counter


def build_qc_report(run: dict) -> str:
    """run: {"started_at", "countries": {label: {...}}, "master": {stats...},
    "master_totals": {...}, "ai": {...}, "gemini_cost": float}"""
    lines = ["EXPERTLY EVENTS: WEEKLY QC REPORT", f"Run started: {run.get('started_at', '')}", ""]

    for label, c in run.get("countries", {}).items():
        lines.append(f"--- {label} ---")
        if c.get("scrape_status") != "done":
            lines.append("  Scrape FAILED: this country's existing Master events were left untouched.")
            continue
        health = c.get("site_health", {})
        counts = Counter(h["status"] for h in health.values())
        lines.append(f"  Sites checked: {len(health)}  (ok {counts['ok']}, failed {counts['failed']}, "
                     f"empty {counts['empty']}, big drop {counts['big_drop']})")
        for url, h in health.items():
            if h["status"] != "ok":
                lines.append(f"    ! {h['status']}: {url}  {h['detail']}")
        st = c.get("event_status", {})
        lines.append(f"  Events checked: {sum(st.values())}  (verified {st.get('verified', 0)}, "
                     f"needs review {st.get('needs_review', 0)}, rejected {st.get('rejected', 0)}, "
                     f"already past {st.get('past', 0)}, page unreachable {st.get('unreachable', 0)}, "
                     f"no page of its own {st.get('shared_page', 0)})")
        lines.append(f"  Fixed by re-scraping: {c.get('fixed_by_rescrape', 0)}")
        reasons = c.get("review_reasons", {})
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"    - {n} x {reason}")
        lines.append("")

    m = run.get("master", {})
    t = run.get("master_totals", {})
    lines += ["--- MASTER ---",
              f"  Added {m.get('added', 0)}, updated {m.get('updated_events', 0)}, unchanged {m.get('unchanged', 0)}, "
              f"moved to Past {m.get('moved_to_past', 0)}, promoted from review {m.get('promoted', 0)}",
              f"  Sent to Needs Review {m.get('sent_to_review', 0)}, not found on website {m.get('not_found_to_review', 0)}, "
              f"rejected {m.get('rejected', 0)}",
              f"  Kept existing values after a failed re-scrape: {m.get('kept_after_failed_rescrape', 0)}; "
              f"locked rows skipped: {m.get('locked_skipped', 0)}",
              f"  Totals: Verified {t.get('verified', 0)}, Needs Review {t.get('review', 0)}, "
              f"Past {t.get('past', 0)}, Rejected {t.get('rejected', 0)}", ""]

    ai = run.get("ai")
    if ai:
        lines += ["--- AI REVIEW (Needs Review only) ---",
                  f"  Reviewed {ai['reviewed']}: verified {ai['verified']}, rejected {ai['rejected']}, "
                  f"left for a person {ai['unclear']}"
                  + ("  (STOPPED: Gemini budget reached)" if ai.get("stopped_by_budget") else ""), ""]
    lines.append(f"Gemini spend this run: ${run.get('gemini_cost', 0):.4f}")
    return "\n".join(lines)


def write_qc_report(run: dict, out_dir: str = "output/summaries") -> str:
    os.makedirs(out_dir, exist_ok=True)
    text = build_qc_report(run)
    with open(os.path.join(out_dir, "qc_report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(out_dir, "qc_report.json"), "w", encoding="utf-8") as f:
        json.dump(run, f, indent=1, default=str)
    return text
