"""Entry point for the Coolify Scheduled Task: run a full scrape, then email
a short digest. Meant to be invoked as `python3 scheduled_run.py` on a cron
schedule (e.g. every 3 days) — the schedule itself lives in Coolify, not here.

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

PROGRESS_PATH = "output/progress.json"
FAILURES_LOG = "output/failures.csv"


def run_scrape():
    result = subprocess.run(
        [sys.executable, "main.py", "--engine", "free"],
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
    )
    return result.returncode


def build_digest() -> str:
    try:
        with open(PROGRESS_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return "Scrape ran but output/progress.json was not found/readable."

    lines = [
        f"Expertly Event Scraper — run summary ({state.get('updated_at', 'unknown time')})",
        "",
        f"Sites processed: {state.get('processed')}/{state.get('total_sites')}",
        f"Events found:    {state.get('events_found')}",
        f"Events added:    {state.get('events_added')}",
        f"Links fixed:     {state.get('links_fixed')}",
        f"Failures:        {state.get('failures')}",
        f"Status:          {state.get('status')}",
    ]

    zero_event_orgs = [
        entry.split("]")[1].split(":")[0].strip()
        for entry in state.get("recent_log", [])
        if ": found 0, added 0" in entry
    ]
    if zero_event_orgs:
        lines.append("")
        lines.append(f"Sites returning 0 events this run (most recent {len(zero_event_orgs)}):")
        for name in zero_event_orgs[:15]:
            lines.append(f"  - {name}")

    if os.path.exists(FAILURES_LOG):
        with open(FAILURES_LOG, encoding="utf-8") as f:
            fail_lines = f.readlines()
        if len(fail_lines) > 1:
            lines.append("")
            lines.append(f"Sites that failed to load ({len(fail_lines) - 1}):")
            for row in fail_lines[1:11]:
                lines.append(f"  - {row.split(',')[0]}")

    return "\n".join(lines)


def send_email(body: str):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("DIGEST_TO")
    port = int(os.environ.get("SMTP_PORT", "587"))

    if not all([host, user, password, to_addr]):
        print("SMTP_HOST/SMTP_USER/SMTP_PASS/DIGEST_TO not fully set — skipping email, printing digest instead:\n")
        print(body)
        return

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
