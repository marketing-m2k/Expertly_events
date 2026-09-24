"""Extract one event from its own page, without AI, with proof for every value.

Sources in priority order: structured event data (schema.org) > labelled
fields > (the old pattern matcher is NOT used here; it stays a listing-page
fallback and its output is always low confidence).

Rules that keep this honest:
  - two sources disagreeing on the date/end date/format -> neither is used;
    both are reported as a conflict for review
  - a date without a year is reported as "date not stated", never guessed
  - only high-confidence values count as verified; a title from the <title>
    tag or a date from a <time> tag is kept but flags the event for review
"""

from scraper.evidence import field, normalize_ws
from scraper.label_extract import extract_labelled
from scraper.structured_data import extract_structured_events, pick_event_for_page

COLUMNS = ("name", "date", "end_date", "location", "format", "description", "link")
_CONFLICT_COLUMNS = ("date", "end_date", "format")


def extract_detail_page(html: str, page_url: str, org_name: str = "") -> dict:
    structured = pick_event_for_page(extract_structured_events(html, page_url), page_url) or {}
    labelled_result = extract_labelled(html, page_url, org_name)
    labelled = labelled_result["fields"]

    record: dict[str, dict] = {}
    conflicts: list[dict] = []
    review: list[str] = []

    for column in COLUMNS:
        s, l = structured.get(column), labelled.get(column)
        if s and l and column in _CONFLICT_COLUMNS and normalize_ws(s["value"]) != normalize_ws(l["value"]):
            conflicts.append({"column": column, "structured_data": s["value"], "page_label": l["value"]})
            continue  # nothing picked when the page contradicts itself
        chosen = s or l
        if chosen:
            record[column] = chosen

    if "link" not in record:
        # the page we opened is the event's own page -- but that's stated by
        # us, not by the site, so it is medium confidence
        record["link"] = field(page_url, page_url, "The event page this data was read from", "page_url", "medium")

    for c in conflicts:
        review.append(f"conflict on {c['column']}: structured data says '{c['structured_data']}', "
                      f"page label says '{c['page_label']}'")
    if "name" not in record:
        review.append("event title not found on the event page")
    if "date" not in record and not any(c["column"] == "date" for c in conflicts):
        raw = labelled_result["date_without_year"]
        review.append(f"date has no year on the page ('{raw}'), not guessed" if raw
                      else "no event date found on the event page")
    for column, f in record.items():
        if column in ("name", "date") and f["confidence"] != "high":
            review.append(f"{column} came from a {f['source']} ({f['confidence']} confidence)")

    return {
        "fields": record,
        "conflicts": conflicts,
        "review_reasons": review,
        "ignored_labels": labelled_result["ignored_labels"],
    }
