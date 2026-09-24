"""Verification 1: automatic checks on one extracted event, using only rules.

Every check returns a plain-language failure reason. An empty list means the
event passed. Nothing here guesses or repairs data -- a failed check sends the
event back to be re-scraped, and after that to Needs Review.
"""

from datetime import datetime

from scraper.clean_events import _is_junk_title
from scraper.country_rules import _infer_country_from_location, _location_names_untracked_country
from scraper.date_utils import date_conflicts_with_text
from scraper.evidence import appears_on_page

PROOF_COLUMNS = ("name", "date", "end_date", "location", "format")
ACCEPTED_CATEGORIES = ("Tax", "Finance", "Legal")
MAX_YEARS_AHEAD = 3


def _parse_display(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d-%b-%Y")
    except (TypeError, ValueError):
        return None


def resolve_country(location: str, source_country: str) -> str:
    """The event's own Location wins over the Sources tab it was found under."""
    return _infer_country_from_location(location) or source_country


def verify_event(fields: dict, category: str, page_haystack: str, today: datetime | None = None) -> list[str]:
    today = (today or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    failures: list[str] = []

    name = fields.get("name")
    if not name:
        failures.append("no event title")
    else:
        if _is_junk_title(name["value"]):
            failures.append(f"title '{name['value']}' is a generic label, not an event name")
        if name["confidence"] != "high":
            failures.append(f"title only came from a {name['source']} ({name['confidence']} confidence)")

    for column in PROOF_COLUMNS:
        f = fields.get(column)
        if f and not appears_on_page(f["match"], page_haystack):
            failures.append(f"{column} '{f['value']}' could not be found on the event page")

    date = fields.get("date")
    start = end = None
    if not date:
        failures.append("no event date")
    else:
        start = _parse_display(date["value"])
        if start is None:
            failures.append(f"date '{date['value']}' is not a real date")
        elif start < today:
            failures.append(f"event date {date['value']} has already passed")
        elif start.year > today.year + MAX_YEARS_AHEAD:
            failures.append(f"event date {date['value']} is implausibly far ahead")
        if date["confidence"] != "high":
            failures.append(f"date only came from a {date['source']} ({date['confidence']} confidence)")
    if fields.get("end_date"):
        end = _parse_display(fields["end_date"]["value"])

    if start and name and date_conflicts_with_text(start, end, name["value"]):
        failures.append("the date in the title disagrees with the event date")

    location = fields.get("location")
    if location and _location_names_untracked_country(location["value"]) and not _infer_country_from_location(location["value"]):
        failures.append(f"location '{location['value']}' is in a country we don't track")

    if category not in ACCEPTED_CATEGORIES:
        failures.append("the rules could not place it in Tax, Finance or Legal (goes to the AI review, not rejected)")

    return failures
