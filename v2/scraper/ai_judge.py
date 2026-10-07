"""Gemini as the JUDGE of every event the rules could not settle on their own.

The scraper collects every event it can find. Rules only do what is objective
(is the date already past? is the page a duplicate?). Everything else -- is this
a real Tax / Finance / Legal professional event, what is its date, where is it,
is it virtual or in person -- is decided by Gemini reading the page and the
list-page card and using its own judgement. It is not made to quote the page or
to follow a checklist. Code only guards what is objective: the answer must give
a real date that is not already past, and a title.

Covers Needs Review AND events an earlier, stricter AI pass rejected, so nothing
is lost to the old rules. Each event is judged once (marker "AI judge").
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from scraper.ai_review import gemini_call
from scraper.api_budget import BudgetExceededError
from scraper.classify_rules import taxonomy_prompt_text
from scraper.date_utils import format_display_date
from scraper.fast_fetch import fetch_detail_fast
from scraper.label_extract import main_text

PAGE_CHARS = 9000
MARK = "AI judge"

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["verified", "rejected", "unclear"]},
        "reason": {"type": "string"},
        "category": {"type": "string", "enum": ["Tax", "Finance", "Legal", "None"]},
        "name": {"type": "string"},
        "date": {"type": "string", "description": "start date YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "end date YYYY-MM-DD, only if multi-day"},
        "location": {"type": "string"},
        "format": {"type": "string", "enum": ["Virtual", "In Person", "Hybrid", "Unknown"]},
    },
    "required": ["decision", "reason"],
}

_TEMPLATE = """You are the expert reviewer for a professional events tracker covering Tax, Finance and Legal.
Today is {today}. Decide about ONE event using your own judgement, like a knowledgeable person would.

What counts: a real, attendable professional event (conference, seminar, webinar, workshop, training,
summit, roundtable, moot/competition for professionals or law/finance students) whose subject falls in
Tax, Finance or Legal in the broad professional sense. Examples of the fields (not a complete list):
{taxonomy}

Decide "verified" if it is such an event and you can tell when it takes place. Fill name, date (YYYY-MM-DD),
end_date if multi-day, location, format (Virtual / In Person / Hybrid), category.
Decide "rejected" if it is clearly not such an event (social/networking only, job ad, article, newsletter,
membership or admin notice, exam-prep course, unrelated subject) or its date is already past. Say why.
Decide "unclear" only if you truly cannot tell even after using everything below.

Use everything given: the page text AND the list-page card. The date is when the event happens, never a
registration deadline or posted-on date. If the year is not written but is obvious from context (the page
is clearly about an upcoming edition), use your judgement. Do not invent a date you have no basis for.

EVENT FROM THE LIST PAGE
Title: {name}
Organizer: {organizer}
Date shown: {date}   Location: {location}   Format: {format}
Card text: {card}
Link: {link}
Earlier automatic checks said: {reasons}

EVENT PAGE TEXT:
{page}
"""
PROMPT = _TEMPLATE.replace("{taxonomy}", taxonomy_prompt_text())


def _iso(value: str):
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def apply_judgement(resp: dict, today: datetime) -> tuple[str, dict, str]:
    """(decision, row updates, reason). Only objective guards: a title, a real date, not past."""
    decision = resp.get("decision", "unclear")
    reason = re.sub(r"\s+", " ", resp.get("reason", "")).strip()
    if decision != "verified":
        return decision, {}, reason
    start, end = _iso(resp.get("date")), _iso(resp.get("end_date"))
    name = (resp.get("name") or "").strip()
    if not name or start is None:
        return "unclear", {}, f"judged as an event but gave no usable title/date ({reason})"
    if (end or start).date() < today.date():
        return "rejected", {}, "event date has already passed"
    if start.year > today.year + 3:
        return "unclear", {}, f"date {start:%Y-%m-%d} looks implausible ({reason})"
    fmt = resp.get("format")
    updates = {"Event Name": name, "Date": format_display_date(start),
               "End Date": format_display_date(end) if end and end.date() != start.date() else "",
               "Category": resp.get("category") if resp.get("category") in ("Tax", "Finance", "Legal") else "",
               "Location": (resp.get("location") or "").strip(),
               "Format": fmt if fmt in ("Virtual", "In Person", "Hybrid") else ""}
    if not updates["Category"]:
        return "rejected", {}, reason or "not in Tax, Finance or Legal"
    return "verified", updates, reason


def judge_events(master, today: datetime | None = None, fetch_fn=fetch_detail_fast, call_fn=None,
                 workers: int = 8) -> dict:
    today = today or datetime.now()
    call_fn = call_fn or (lambda prompt: gemini_call(prompt, SCHEMA))
    stats = {"reviewed": 0, "verified": 0, "rejected": 0, "unclear": 0, "stopped_by_budget": False}

    def pending(row):
        return not str(row.get("AI Review", "")).startswith(MARK) and not master._locked(row)

    todo = {eid: row for eid, row in master.review.items() if pending(row)}
    todo.update({eid: row for eid, row in master.rejected.items() if pending(row)
                 and not str(row.get("Reject Reason", "")).startswith("event date")})
    if not todo:
        return stats

    links = sorted({r["Register Link"] for r in todo.values()
                    if r.get("Register Link") and r["Register Link"] != r.get("Source URL")})
    print(f"  AI judge: opening {len(links)} event pages for {len(todo)} events", flush=True)
    pages = fetch_fn(links, workers=20) if links else {}

    jobs = []
    for eid, row in todo.items():
        page = pages.get(row.get("Register Link")) or {}
        text = main_text(page["html"])[:PAGE_CHARS] if page.get("html") else "(the event's own page could not be opened)"
        prompt = PROMPT.format(
            today=f"{today:%d-%b-%Y}", name=row.get("Event Name", ""), organizer=row.get("Organizer", ""),
            date=row.get("Date") or "none", location=row.get("Location") or "none", format=row.get("Format") or "none",
            card=(row.get("Description") or "")[:1500], link=row.get("Register Link", ""),
            reasons=row.get("Review Reason") or row.get("Reject Reason") or "none", page=text)
        jobs.append((eid, prompt))

    def ask(job):
        try:
            return job, call_fn(job[1]), None
        except BudgetExceededError:
            return job, None, "budget"
        except Exception as exc:  # noqa: BLE001 - one failed call must not stop the rest
            return job, None, str(exc)[:150]

    print(f"  AI judge: asking Gemini about {len(jobs)} events", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        results = list(executor.map(ask, jobs))

    for (eid, _), resp, err in results:
        if err == "budget":
            stats["stopped_by_budget"] = True
            continue
        if err:
            continue  # stays as it is; judged again next run
        stats["reviewed"] += 1
        decision, updates, reason = apply_judgement(resp, today)
        if eid in master.rejected:  # give an earlier rejection a second look
            master.review[eid] = master.rejected.pop(eid)
            master.review[eid].pop("Reject Reason", None)
        if decision == "verified":
            master.promote_from_review(eid, updates, reason)
            stats["verified"] += 1
        elif decision == "rejected":
            master.reject_from_review(eid, reason or "judged not relevant")
            master.rejected[eid]["AI Review"] = f"{MARK} {today:%d-%b-%Y}: rejected"
            stats["rejected"] += 1
        else:
            master.note_ai_review(eid, f"{MARK} {today:%d-%b-%Y}: still unclear: {reason}")
            stats["unclear"] += 1
    return stats
