"""Stage 9: Gemini reviews the events held in Needs Review -- and nothing else.

Gemini reads the event's own page and must give, for each detail, the value
AND the exact words from the page it relied on. Code then:
  - rejects any quote that is not really on the page,
  - reads the dates itself from the quote (Gemini's own date value is ignored),
  - runs the same verification checks every other event faces.
Gemini can only promote an event that passes all of them. It can also reject
an event (with a reason) or leave it for a person. Each event is reviewed
once; it is reviewed again only after its page changes.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from scraper.api_budget import BudgetExceededError
from scraper.classify_rules import taxonomy_prompt_text
from scraper.date_utils import format_display_date, parse_strict_date_range
from scraper.evidence import appears_on_page, field, normalize_ws
from scraper.enrich import _haystack
from scraper.fast_fetch import fetch_detail_fast
from scraper.label_extract import _mode_from_text, main_text
from scraper.verify_events import ACCEPTED_CATEGORIES, verify_event

PAGE_CHARS = 6000
_MISSING_INFO = re.compile(
    r"cookie|privacy|consent|no information|does not (contain|provide|state|include)|not provide|missing|"
    r"empty|unreadable|generic template|only (the )?(name|title|navigation)", re.I)

_FIELD = {"type": "object", "properties": {"value": {"type": "string"}, "quote": {"type": "string"}}}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["verified", "rejected", "unclear"]},
        "category": {"type": "string", "enum": ["Tax", "Finance", "Legal", "None"]},
        "reason": {"type": "string"},
        "name": _FIELD, "date": _FIELD, "end_date": _FIELD, "location": _FIELD, "format": _FIELD,
    },
    "required": ["decision", "reason"],
}

_PROMPT_TEMPLATE = """You are checking ONE event for a Tax / Finance / Legal professional events tracker.
Read the event page text below. Rules:
- Use ONLY what the page text states. Never use general knowledge. If the page does not state something, leave that field out.
- For every field you give, "quote" must be the EXACT words copied from the page text that state it.
- "date" is the date the event takes place -- never a registration deadline, posted-on, updated or early-bird date. It must include the year on the page.
- "format" is Virtual, In Person or Hybrid, only if the page says so for THIS event.
- "category" is Tax, Finance or Legal. These are BROAD professional fields, not just those three words. Everything professionally inside them counts. Topics that belong to each (these are examples, not a complete list -- any topic in the same spirit counts too):
{taxonomy}
  Use None only when the event's own subject is not in any of these fields.
- decision "verified": a real, attendable professional event whose own subject is within Tax, Finance or Legal (the whole of each field, as above), and the page states its title and date.
- decision "rejected": ONLY when the page positively shows it is not such an event. That includes: purely social or networking events, careers/student events, receptions; general leadership or industry-business events with no tax, finance or legal subject (corporate governance itself IS a legal topic and counts); membership administration; corporate notices (AGM, dividends); newsletters or publications; exam-prep courses; job vacancies; articles; pages that are not an event at all. Say why.
- decision "unclear": whenever the page text is empty, only cookie/consent/navigation text, or lacks the information you need. NEVER reject an event just because the page text is missing or unreadable.

Event as found on the list page: {name}
Held back because: {reasons}

PAGE TEXT:
{page}
"""
# the same taxonomy the rule-based classifier uses (scraper/taxonomy.json) is given to Gemini
PROMPT = _PROMPT_TEMPLATE.replace("{taxonomy}", taxonomy_prompt_text())


def gemini_call(prompt: str) -> dict:
    """One Gemini request returning parsed JSON. Spend is tracked against the
    run's budget cap (BudgetExceededError propagates to the caller)."""
    from google.genai import types
    from google.genai.errors import ClientError, ServerError

    from scraper.api_budget import record_usage
    from scraper.classify_relevance import MODEL, _get_client

    client = _get_client()
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=RESPONSE_SCHEMA)
    for attempt in range(4):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt, config=config)
            break
        except (ClientError, ServerError) as exc:
            retryable = isinstance(exc, ServerError) or getattr(exc, "code", None) == 429
            if retryable and attempt < 3:
                time.sleep(20)
                continue
            raise
    usage = getattr(response, "usage_metadata", None)
    record_usage(getattr(usage, "prompt_token_count", 0) or 0, getattr(usage, "candidates_token_count", 0) or 0)
    return json.loads(response.text)


def evaluate_response(resp: dict, html: str, today: datetime) -> tuple[str, dict, list[str], str]:
    """Turn Gemini's answer into (decision, fields, problems, category), with
    every claim checked against the page. Returns decision "unclear" whenever
    the answer could not be trusted."""
    haystack = _haystack(html)
    problems: list[str] = []
    fields: dict[str, dict] = {}

    def quoted(key):
        item = resp.get(key) or {}
        quote = normalize_ws(item.get("quote", ""))
        if not quote:
            return None
        if not appears_on_page(quote, haystack):
            problems.append(f"the quote given for {key} is not on the page")
            return None
        return item, quote

    if (got := quoted("name")):
        item, quote = got
        value = normalize_ws(item.get("value", "")) or quote
        if appears_on_page(value, haystack):
            fields["name"] = field(value, value, "Title confirmed by AI review from the page text", "ai_review")
        else:
            problems.append("the title given is not on the page")
    if (got := quoted("date")):
        start, end = parse_strict_date_range(got[1], today)
        if start:
            fields["date"] = field(format_display_date(start), got[1],
                                   "Date read by code from the quote AI review found on the page", "ai_review")
            if end and end.date() != start.date():
                fields["end_date"] = field(format_display_date(end), got[1],
                                           "End date read by code from the AI review quote", "ai_review")
        else:
            problems.append("the date quote does not contain a full date with a year")
    if (got := quoted("location")):
        value = normalize_ws(got[0].get("value", "")) or got[1]
        fields["location"] = field(value, got[1], "Venue confirmed by AI review from the page text", "ai_review")
    if (got := quoted("format")):
        mode = _mode_from_text(got[1])
        if mode:
            fields["format"] = field(mode, got[1], "Format read by code from the AI review quote", "ai_review")

    category = resp.get("category", "None")
    decision = resp.get("decision", "unclear")
    if decision == "verified":
        failures = verify_event(fields, category if category in ACCEPTED_CATEGORIES else "", haystack, today)
        if failures:
            problems += failures
            decision = "unclear"
    return decision, fields, problems, category


def review_needs_review(master, today: datetime | None = None, fetch_fn=fetch_detail_fast,
                        call_fn=gemini_call, workers: int = 6) -> dict:
    today = today or datetime.now()
    stats = {"reviewed": 0, "verified": 0, "rejected": 0, "unclear": 0, "stopped_by_budget": False}
    # each event is reviewed once -- except when its page couldn't be opened last time
    todo = {eid: row for eid, row in master.review.items()
            if (not row.get("AI Review") or row["AI Review"].startswith("AI review skipped"))
            and row.get("Register Link") and not master._locked(row)}
    if not todo:
        return stats

    print(f"  AI review: opening {len(todo)} event pages", flush=True)
    pages = fetch_fn([row["Register Link"] for row in todo.values()], workers=20)

    jobs = []
    for eid, row in todo.items():
        page = pages.get(row["Register Link"]) or {}
        if not page.get("html"):
            master.note_ai_review(eid, f"AI review skipped: page could not be opened ({page.get('error') or 'no result'})")
            continue
        prompt = PROMPT.format(name=row["Event Name"], reasons=row.get("Review Reason", ""),
                               page=main_text(page["html"])[:PAGE_CHARS])
        jobs.append((eid, page["html"], prompt))

    def ask(job):
        try:
            return job, call_fn(job[2]), None
        except BudgetExceededError:
            return job, None, "budget"
        except Exception as exc:  # noqa: BLE001 - one failed call must not stop the review
            return job, None, str(exc)[:150]

    print(f"  AI review: asking Gemini about {len(jobs)} events", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        results = list(executor.map(ask, jobs))

    for (eid, html, _), resp, err in results:  # applied one at a time, in order
        if err == "budget":
            stats["stopped_by_budget"] = True
            continue
        if err:
            master.note_ai_review(eid, f"AI review skipped: {err}")
            continue
        stats["reviewed"] += 1
        decision, fields, problems, category = evaluate_response(resp, html, today)
        reason = normalize_ws(resp.get("reason", ""))
        if decision == "rejected" and _MISSING_INFO.search(reason):
            decision = "unclear"  # "no information on the page" is not a reason to reject an event
            problems.append(f"the page could not be read properly: {reason}")
        if resp.get("decision") == "verified" and any("already passed" in p for p in problems):
            master.reject_from_review(eid, "event date has already passed")
            stats["rejected"] += 1
            continue
        if decision == "verified":
            updates = {"Event Name": fields.get("name", {}).get("value", ""), "Date": fields["date"]["value"],
                       "End Date": fields.get("end_date", {}).get("value", ""), "Category": category,
                       "Format": fields.get("format", {}).get("value", ""),
                       "Location": fields.get("location", {}).get("value", "")}
            master.promote_from_review(eid, updates, reason)
            stats["verified"] += 1
        elif decision == "rejected" and reason:
            master.reject_from_review(eid, reason)
            stats["rejected"] += 1
        else:
            note = "; ".join(problems) or reason or "could not decide"
            master.note_ai_review(eid, f"AI review {today:%d-%b-%Y}: still unclear: {note}")
            stats["unclear"] += 1
    return stats
