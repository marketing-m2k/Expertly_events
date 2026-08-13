"""Turn cleaned page text into a structured list of events using Gemini."""

import json
import os

import google.generativeai as genai

MODEL = "gemini-2.0-flash"

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

_configured = False


def _client():
    global _configured
    if not _configured:
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        _configured = True
    return genai.GenerativeModel(
        MODEL,
        generation_config=genai.types.GenerationConfig(
            response_mime_type="application/json",
            response_schema=EVENT_SCHEMA,
        ),
    )


def extract_events(page_text: str, source_url: str, org_name: str = "") -> list[dict]:
    if not page_text.strip():
        return []

    model = _client()

    prompt = (
        f"Source URL: {source_url}\n"
        f"Organization: {org_name or 'unknown'}\n\n"
        "Extract every upcoming event from the page text below. Skip navigation "
        "links, past-events archives, and unrelated content. If a date has no "
        "year, assume the next upcoming occurrence. For 'format', infer "
        "In Person / Virtual / Hybrid from context; leave 'location_city' blank "
        "for virtual-only events. Return an empty events list if the page has "
        "no events.\n\n"
        f"PAGE TEXT:\n{page_text[:15000]}"
    )

    response = model.generate_content(prompt)
    try:
        data = json.loads(response.text)
    except (ValueError, AttributeError):
        return []
    return data.get("events", [])
