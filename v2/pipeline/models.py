"""Core vocabulary of the v2 pipeline: versions, statuses, quality flags and the
shapes that travel between stages.

Versions matter: every stored result records the extractor/classifier/prompt
version that produced it. Bumping one makes the affected records eligible for
reprocessing (see reprocess.py) -- no bad historical result is ever frozen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field as dc_field
from typing import Any

PIPELINE_VERSION = "2.0.0"
DISCOVERY_VERSION = "2.0.0"
EXTRACTOR_VERSION = "2.0.0"      # bump when extraction logic changes -> records reprocessed
CLASSIFIER_VERSION = "2.0.0"     # bump when taxonomy/signal logic changes
REVIEW_PROMPT_VERSION = "2.0.0"  # bump when the Gemini prompt/schema changes


class Status:
    DISCOVERED = "DISCOVERED"
    EXTRACTED = "EXTRACTED"
    QUALITY_REVIEW = "QUALITY_REVIEW"          # quality flags the code could not clear; waits for AI/human
    NEEDS_AI_REVIEW = "NEEDS_AI_REVIEW"        # waiting for Gemini (not yet reviewed, or Gemini unavailable)
    NEEDS_REVIEW = "NEEDS_REVIEW"              # after review the answer is still uncertain -> a person decides
    VERIFIED = "VERIFIED"
    CORRECTED = "CORRECTED"                    # verified after an evidence-checked Gemini correction
    REJECTED = "REJECTED"                      # evidence says genuinely out of scope / invalid. Nothing else.
    PAST = "PAST"
    DISCOVERY_INCOMPLETE = "DISCOVERY_INCOMPLETE"  # org-level: list could not be read completely
    FETCH_FAILED = "FETCH_FAILED"

    ALL = (DISCOVERED, EXTRACTED, QUALITY_REVIEW, NEEDS_AI_REVIEW, NEEDS_REVIEW, VERIFIED, CORRECTED, REJECTED,
           PAST, DISCOVERY_INCOMPLETE, FETCH_FAILED)
    # statuses a Master "Verified Events" row may have
    PUBLISHABLE = (VERIFIED, CORRECTED)
    # statuses that must be re-examined when the extractor version changes
    RETRYABLE = (QUALITY_REVIEW, NEEDS_AI_REVIEW, NEEDS_REVIEW, REJECTED, FETCH_FAILED, DISCOVERED, EXTRACTED)


class Flag:
    """Quality flags. They describe the extraction, they never reject an event."""
    MISSING_NAME = "missing_name"
    WEAK_NAME = "weak_name"
    MISSING_DATE = "missing_date"
    CONFLICTING_DATE = "conflicting_date"
    WEAK_DATE = "weak_date"
    YEAR_MISSING = "year_missing"
    MISSING_LOCATION = "missing_location"
    CONFLICTING_LOCATION = "conflicting_location"
    WEAK_LOCATION = "weak_location"
    MISSING_FORMAT = "missing_format"
    WEAK_DESCRIPTION = "weak_description"
    SOURCE_CONFLICT = "source_conflict"
    PAGE_UNREADABLE = "page_unreadable"
    NO_DETAIL_PAGE = "no_detail_page"
    JSONLD_FOREIGN = "jsonld_foreign"          # the page's JSON-LD Event belongs to another widget/event
    UNRELATED_CONTENT = "unrelated_content"    # extracted text looks like it came from a related-events/contact block
    SUSPICIOUS_VALUE = "suspicious_value"
    UNVERIFIED_EVIDENCE = "unverified_evidence"

    # Flags that send an event to the AI review (it must be looked at before it can be trusted)
    REVIEW_TRIGGERS = frozenset({MISSING_NAME, WEAK_NAME, MISSING_DATE, CONFLICTING_DATE, WEAK_DATE, YEAR_MISSING, MISSING_LOCATION,
                                 CONFLICTING_LOCATION, WEAK_LOCATION, MISSING_FORMAT, WEAK_DESCRIPTION, SOURCE_CONFLICT,
                                 PAGE_UNREADABLE, UNRELATED_CONTENT, SUSPICIOUS_VALUE, UNVERIFIED_EVIDENCE, NO_DETAIL_PAGE})
    # Flags that stop an event being VERIFIED even after review, unless a verified correction clears them
    HARD_BLOCKS = frozenset({MISSING_NAME, WEAK_NAME, MISSING_DATE, CONFLICTING_DATE, WEAK_DATE, YEAR_MISSING, PAGE_UNREADABLE,
                             UNRELATED_CONTENT, SUSPICIOUS_VALUE, UNVERIFIED_EVIDENCE, SOURCE_CONFLICT})
    BLOCKING = HARD_BLOCKS            # kept for older callers


# discovery completeness (per organisation / listing)
DISCOVERY_COMPLETE = "discovery_complete"
DISCOVERY_INCOMPLETE_STATE = "discovery_incomplete"
DISCOVERY_FAILED = "discovery_failed"

FIELD_NAMES = ("name", "start_date", "end_date", "start_time", "end_time", "timezone", "venue", "address", "city",
               "region", "country", "format", "organiser", "description", "registration_url", "source_url")
CORE_FIELDS = ("name", "start_date", "venue", "format", "description")


@dataclass
class FieldValue:
    """One extracted value together with the proof for it."""
    value: str | None
    source: str = ""           # jsonld | label | heading | time_tag | header_text | card | meta | text | sources_sheet | page_url | gemini
    evidence: str = ""         # literal text the value was read from
    confidence: str = "medium"  # high | medium | low
    verification: str = "unverified"  # verified | unverified | conflict | unresolved
    locator: str = ""          # where on the page (section / label / dom path)
    alternatives: list[dict] = dc_field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FieldValue":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    @property
    def usable(self) -> bool:
        return bool(self.value) and self.verification != "unresolved"


def unresolved(note: str, alternatives: list[dict] | None = None, source: str = "") -> FieldValue:
    return FieldValue(value=None, source=source, confidence="low", verification="unresolved", note=note,
                      alternatives=alternatives or [])


@dataclass
class Candidate:
    """Something discovery believes may be an event. Created before any detail page is opened."""
    candidate_id: str
    org: str
    country: str
    source_url: str                # the organisation's configured events page
    listing_url: str = ""          # the concrete page/feed it was found on
    url: str = ""                  # the event's own page (may be empty / shared)
    title: str = ""
    title_confidence: str = "medium"   # high | medium | low (generic/unknown)
    card_text: str = ""
    date_hint: str = ""
    location_hint: str = ""
    methods: list[str] = dc_field(default_factory=list)   # card | jsonld | sitemap | rss | ics | link
    ext_id: str = ""               # platform/feed id (ICS UID, JSON-LD @id, ...)
    jsonld: dict | None = None
    feed: dict | None = None
    alt_urls: list[str] = dc_field(default_factory=list)
    from_archive: bool = False
    notes: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Dropped:
    """A discovered thing that did not become a candidate -- with the reason. Nothing disappears silently."""
    org: str
    listing_url: str
    reason: str
    title: str = ""
    url: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiscoveryResult:
    org: str
    country: str
    source_url: str
    candidates: list[Candidate] = dc_field(default_factory=list)
    dropped: list[Dropped] = dc_field(default_factory=list)
    pages: list[dict] = dc_field(default_factory=list)       # [{url, kind, status, error, method, n_cards}]
    completeness: dict = dc_field(default_factory=dict)      # {state, signals, reasons}
    errors: list[str] = dc_field(default_factory=list)
    failure_kinds: list[str] = dc_field(default_factory=list)  # pagination|load_more|infinite_scroll|js|fetch|extraction

    def to_dict(self) -> dict:
        return {"org": self.org, "country": self.country, "source_url": self.source_url,
                "candidates": [c.to_dict() for c in self.candidates], "dropped": [d.to_dict() for d in self.dropped],
                "pages": self.pages, "completeness": self.completeness, "errors": self.errors,
                "failure_kinds": self.failure_kinds}


def field_dicts(fields: dict[str, FieldValue]) -> dict[str, dict]:
    return {k: v.to_dict() for k, v in fields.items()}


def fields_from_dicts(d: dict[str, Any]) -> dict[str, FieldValue]:
    return {k: FieldValue.from_dict(v) for k, v in (d or {}).items()}
