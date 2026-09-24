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
import os
import time
from datetime import datetime

from scraper.api_budget import BudgetExceededError
from scraper.date_utils import format_display_date, parse_strict_date_range
from scraper.evidence import appears_on_page, field, normalize_ws
from scraper.enrich import _haystack
from scraper.fetch import fetch_detail_pages
from scraper.label_extract import _mode_from_text, main_text
from scraper.verify_events import ACCEPTED_CATEGORIES, verify_event

PAGE_CHARS = 6000

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

PROMPT = """You are checking ONE event for a Tax / Finance / Legal professional events tracker.
Read the event page text below. Rules:
- Use ONLY what the page text states. Never use general knowledge. If the page does not state something, leave that field out.
- For every field you give, "quote" must be the EXACT words copied from the page text that state it.
- "date" is the date the event takes place -- never a registration deadline, posted-on, updated or early-bird date. It must include the year on the page.
- "format" is Virtual, In Person or Hybrid, only if the page says so for THIS event.
- "category" is Tax, Finance or Legal, only if the event's own subject is that. Otherwise None.
- decision: "verified" only if this is a real, attendable Tax/Finance/Legal professional event and the page states its title and date. "rejected" if it is clearly not (say why). "unclear" if you cannot tell from the page.

Event as found on the list page: {name}
Held back because: {reasons}

PAGE TEXT:
{page}
"""


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


def review_needs_review(master, today: datetime | None = None, fetch_fn=fetch_detail_pages,
                        call_fn=gemini_call, workers: int = 5) -> dict:
    today = today or datetime.now()
    stats = {"reviewed": 0, "verified": 0, "rejected": 0, "unclear": 0, "stopped_by_budget": False}
    todo = {eid: row for eid, row in master.review.items()
            if not row.get("AI Review") and row.get("Register Link") and not master._locked(row)}
    if not todo:
        return stats

    pages = fetch_fn([row["Register Link"] for row in todo.values()], workers=workers)
    for eid, row in todo.items():
        page = pages.get(row["Register Link"]) or {}
        if not page.get("html"):
            master.note_ai_review(eid, f"AI review skipped: page could not be opened ({page.get('error') or 'no result'})")
            continue
        prompt = PROMPT.format(name=row["Event Name"], reasons=row.get("Review Reason", ""),
                               page=main_text(page["html"])[:PAGE_CHARS])
        try:
            resp = call_fn(prompt)
        except BudgetExceededError:
            stats["stopped_by_budget"] = True
            break
        stats["reviewed"] += 1

        decision, fields, problems, category = evaluate_response(resp, page["html"], today)
        reason = normalize_ws(resp.get("reason", ""))
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
