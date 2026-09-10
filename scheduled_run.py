"""Entry point for the Coolify Scheduled Task: run a full weekly re-scrape
of every country in weekly_full_run.py's COUNTRIES list, then email a
short digest. Meant to be invoked as
`python3 scheduled_run.py` on a weekly cron schedule — the schedule itself
lives in Coolify, not here (see DEPLOYMENT.md).

This always does a FULL re-scrape of every organization (not an incremental
resume), so that events published since last week are picked up and any
previously incomplete/missing dates get filled in as sites update them —
see weekly_full_run.py for the reconciliation logic.

Email digest is optional: it only fires if SMTP_HOST/SMTP_USER/SMTP_PASS/
DIGEST_TO are all set as environment variables. Without them, this just runs
the scrape and logs the summary to stdout (visible in Coolify's task logs).
"""

import json
import os
import smtplib
import subprocess
import sys
from email.mime.text import MIMEText

from weekly_full_run import COUNTRIES

SUMMARY_PATH = "output/weekly_summary.json"
# Derived from COUNTRIES instead of hardcoded -- adding a country to
# weekly_full_run.py's list is picked up here automatically.
FAILURES_LOGS = {c["label"]: c["failures_log"] for c in COUNTRIES}


def run_scrape():
    result = subprocess.run(
        [sys.executable, "weekly_full_run.py", "--engine", "free"],
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
    )
    return result.returncode


def build_digest() -> str:
    try:
        with open(SUMMARY_PATH, encoding="utf-8") as f:
            summary = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return "Scrape ran but output/weekly_summary.json was not found/readable."

    lines = [
        f"Expertly Event Scraper — weekly run summary",
        f"Started:  {summary.get('started_at', 'unknown')}",
        f"Finished: {summary.get('finished_at', 'unknown')}",
        f"Status:   {summary.get('status', 'unknown')}",
        f"Master.xlsx verified events (all countries): {summary.get('master_verified_total', 'unknown')}",
    ]

    for label, counts in summary.get("countries", {}).items():
        lines.append("")
        lines.append(f"--- {label} ---")
        if counts.get("scrape_status") != "done":
            lines.append("  Scrape FAILED this run — see container logs.")
            continue
        lines.append(f"  Orgs checked:            {counts.get('total_orgs')}")
        lines.append(f"  Orgs with >=1 event:     {counts.get('orgs_seen')}")
        lines.append(f"  Upcoming (verified):     {counts.get('verified')}")
        lines.append(f"  Upcoming (incomplete):   {counts.get('incomplete')}")
        lines.append(f"  Past events:             {counts.get('past')}")
        lines.append(f"  Flagged (irrelevant):    {counts.get('flagged')}")

        fail_log = FAILURES_LOGS.get(label)
        if fail_log and os.path.exists(fail_log):
            with open(fail_log, encoding="utf-8") as f:
                fail_lines = f.readlines()
            if len(fail_lines) > 1:
                lines.append(f"  Sites that failed to load ({len(fail_lines) - 1}):")
                for row in fail_lines[1:11]:
                    lines.append(f"    - {row.split(',')[0]}")

    return "\n".join(lines)


def send_email(body: str):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("DIGEST_TO")

    if not all([host, user, password, to_addr]):
        print("SMTP_HOST/SMTP_USER/SMTP_PASS/DIGEST_TO not fully set — skipping email, printing digest instead:\n")
        print(body)
        return

    # An unset GitHub Actions secret still comes through as an empty string,
    # not a missing env var -- os.environ.get's default only kicks in when
    # the key is absent entirely, so int("") would otherwise crash here even
    # though the `all([...])` check above already treats an empty SMTP_PORT
    # as "not configured" for the other fields.
    port = int(os.environ.get("SMTP_PORT") or "587")

    msg = MIMEText(body)
    msg["Subject"] = "Expertly Event Scraper — run digest"
    msg["From"] = os.environ.get("DIGEST_FROM", user)
    msg["To"] = to_addr

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(msg["From"], [to_addr], msg.as_string())
    print(f"Digest emailed to {to_addr}")


if __name__ == "__main__":
    exit_code = run_scrape()
    digest = build_digest()
    send_email(digest)
    sys.exit(exit_code)
