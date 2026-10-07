"""Turn cleaned page text into a structured list of events using Gemini."""

import json
import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError

load_dotenv()

# gemini-2.0-flash (used here previously) was shut down 2026-06-01. Flash-Lite
# is the cheapest currently-supported model and is plenty for structured
# extraction like this.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_name": {"type": "string"},
                    "date": {"type": "string"},
                    "format": {"type": "string", "enum": ["In Person", "Virtual", "Hybrid"]},
                    "location_city": {"type": "string"},
                    "description": {"type": "string"},
                    "link": {"type": "string"},
                },
                "required": ["event_name"],
            },
        }
    },
    "required": ["events"],
}

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to a .env file in the project "
                "root (see .env.example) or set it as an environment variable."
            )
        _client = genai.Client(api_key=api_key)
    return _client


def extract_events(page_text: str, source_url: str, org_name: str = "") -> list[dict]:
    if not page_text.strip():
        return []

    client = _get_client()

    prompt = (
        f"Source URL: {source_url}\n"
        f"Organization: {org_name or 'unknown'}\n\n"
        "Extract EVERY event mentioned in the page text below — both upcoming "
        "events and past/completed events (including any 'past events', "
        "'event archive', or 'previous events' section) — as long as the event "
        "date is on or after 1 January 2026. Do not skip past events; this page "
        "may be a past-events archive on purpose. Skip navigation links, menus, "
        "and unrelated content that isn't an actual event listing. Keep each "
        "date exactly as written on the page (including the year if shown) so "
        "past vs. upcoming can be determined later — do not shift or invent a "
        "year. For 'format', infer In Person / Virtual / Hybrid from context; "
        "leave 'location_city' blank for virtual-only events.\n\n"
        "IMPORTANT for 'link': links in the page text below appear inline as "
        "[link text](URL) right after the text they belong to. For each event, "
        "find the [text](URL) closest to that event's own title/details — "
        "prefer a URL whose link text says things like 'Register', 'RSVP', "
        "'Details', 'Read more', 'Enroll', or that IS the event title itself — "
        "and put that exact URL in 'link'. Do NOT put a generic top-of-page "
        "navigation link (e.g. a bare 'Events' or 'Home' menu link) there. "
        "If you genuinely cannot find any event-specific URL for that event, "
        f"leave 'link' blank rather than guessing — it will fall back to {source_url} "
        "automatically. Return an empty events list if the page has no events.\n\n"
        f"PAGE TEXT:\n{page_text[:45000]}"
    )

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=EVENT_SCHEMA,
    )

    for attempt in range(3):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt, config=config)
            break
        except ClientError as exc:
            if exc.code == 429 and attempt < 2:  # rate limit — back off and retry
                time.sleep(20)
                continue
            raise
    else:
        return []

    try:
        data = json.loads(response.text)
    except (ValueError, AttributeError, TypeError):
        return []
    return data.get("events", [])
