"""Per-EVENT (not per-organizer) relevance classification via Gemini.

The rule-based pass in clean_events.py only knows the *organizer's* category
tag, so it can't tell "Motor Fuel Annual Conference" (a real state-tax
conference hosted by tax administrators) apart from "Back to School Happy
Hour" (a purely social mixer hosted by a bar association) — both come from
orgs tagged relevantly. This does the actual content judgment per event.
"""

import json
import os
import time

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError

from scraper.api_budget import BudgetExceededError, record_usage

load_dotenv()

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
BATCH_SIZE = 60

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {"type": "array", "items": {"type": "boolean"}},
    },
    "required": ["results"],
}

PROMPT_HEADER = (
    "You are cleaning a directory of events for a Tax / Finance / Legal "
    "professional events tracker. For each numbered event below, decide "
    "whether the EVENT ITSELF is substantively about tax, finance, or legal "
    "professional subject matter — not just because the hosting "
    "organization generally works in that space.\n\n"
    "Mark true for: CLE/CPE educational programs; conferences, seminars, "
    "symposiums or webinars on tax, accounting, banking, financial "
    "regulation, corporate/securities law, litigation, compliance, "
    "insolvency, audit, or estate planning topics; regulatory/policy "
    "briefings; professional certification courses. A niche-sounding "
    "conference (e.g. a motor-fuel-tax conference run by a tax "
    "administrators' association) still counts if the substance is tax, "
    "finance, or legal.\n\n"
    "Mark false for: purely social/networking events (happy hours, mixers, "
    "holiday parties, golf outings, receptions, socials); general "
    "leadership/board-governance or industry-vertical business events with "
    "no specific tax/finance/legal content; membership-administrative "
    "events (elections, orientations); wellness/sports content; corporate "
    "disclosures and stock-exchange filings that aren't attendable events at "
    "all (Annual General Meeting notices, dividend/results announcements, "
    "board-meeting intimations, and similar corporate-action filings, e.g. "
    "from a stock exchange like MSEI/BSE/NSE); recurring newsletter or "
    "publication issues with no session to attend (e.g. 'Quarterly Issue "
    "#63', a journal/magazine release); any navigation artifact or "
    "placeholder that slipped through (e.g. a bare number, 'Learn more'); "
    "and exam-prep/exam-review content — courses, webinars, or bootcamps "
    "whose entire purpose is helping someone pass a professional "
    "certification exam or training internal examiners/inspectors for their "
    "own agency (e.g. 'CFE Exam Review Course', 'RO1 Pre Exam Training', "
    "'BSA/AML Examiner School', 'Nonbank Cyber Examination Training', "
    "'Study Skills Masterclass - How to pass an exam', a bar/CPA/CMI exam "
    "registration or enrollment notice) — those are exam-prep logistics, not "
    "substantive tax/finance/legal educational content, even though they're "
    "hosted by an on-topic organization and styled as a 'course' or "
    "'training'.\n\n"
    "Do mark true for genuine CLE/CPE courses, certification programs, and "
    "training workshops on tax/finance/legal SUBJECT MATTER (e.g. a course "
    "actually teaching tax law, estate planning, or securities compliance) "
    "— those are real events even when styled as a 'course' or 'batch'. The "
    "distinction from the exam-prep exclusion above is substance vs. exam "
    "logistics: teaching the subject is true; drilling someone to pass a "
    "test about it, or training an agency's own staff to conduct "
    "examinations, is false.\n\n"
    "Return exactly one boolean per event, in the same order, via the "
    "'results' array.\n\nEVENTS:\n"
)


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    return genai.Client(api_key=api_key)


def _format_event(i: int, row: dict) -> str:
    bits = [f"{i}. \"{row['name']}\"", f"host: {row['organizer']}", f"tag: {row['category']}"]
    if row.get("location"):
        bits.append(f"location: {row['location']}")
    return " | ".join(bits)


def classify_batch(client: genai.Client, rows: list[dict]) -> list[bool]:
    prompt = PROMPT_HEADER + "\n".join(_format_event(i, r) for i, r in enumerate(rows))
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=RESULT_SCHEMA)

    for attempt in range(4):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt, config=config)
            break
        except ClientError as exc:
            if exc.code == 429 and attempt < 3:  # rate limit -- back off and retry
                time.sleep(20)
                continue
            raise
        except ServerError as exc:
            # transient 5xx (e.g. 503 UNAVAILABLE) -- Gemini's side, not ours;
            # this runs unattended weekly, so it must ride these out rather
            # than crashing the whole classification pass over one batch.
            if attempt < 3:
                time.sleep(20)
                continue
            raise
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
            # a local network blip (Wi-Fi drop, DNS hiccup) -- not Gemini's
            # fault and not a permanent failure; ride it out the same as a
            # transient server error rather than killing the whole run over
            # a few seconds of dropped connectivity.
            if attempt < 3:
                time.sleep(20)
                continue
            raise
    else:
        return [True] * len(rows)  # give up gracefully: keep, don't silently drop

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0
    record_usage(prompt_tokens, output_tokens)  # raises BudgetExceededError past the cap

    try:
        data = json.loads(response.text)
        results = data.get("results", [])
    except (ValueError, AttributeError, TypeError):
        return [True] * len(rows)

    if len(results) != len(rows):
        # length mismatch -- don't misalign booleans to the wrong events
        return [True] * len(rows)
    return results


def classify_all(rows: list[dict]) -> list[bool]:
    """rows: list of {"name","organizer","category","location"}. Returns a
    parallel list of bools. Batches to keep prompts small and calls cheap."""
    client = _get_client()
    out = []
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        out.extend(classify_batch(client, batch))
        print(f"  classified {min(start + BATCH_SIZE, len(rows))}/{len(rows)}")
    return out
