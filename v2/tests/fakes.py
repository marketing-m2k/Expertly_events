"""Test doubles."""

from __future__ import annotations

from pipeline.llm import BudgetExceeded, LLMBadResponse, LLMUnavailable


class FakeLLM:
    """Returns scripted responses in order (or a callable's result). An Exception instance in the script is raised."""

    def __init__(self, *responses, default=None):
        self.responses = list(responses)
        self.default = default
        self.calls: list[str] = []

    def call(self, prompt: str, schema: dict) -> dict:
        self.calls.append(prompt)
        r = self.responses.pop(0) if self.responses else self.default
        if callable(r):
            r = r(prompt)
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise AssertionError("FakeLLM was called but has no scripted response")
        return r


def verified(category="Tax", reason="a real tax workshop"):
    return {"decision": "VERIFIED", "category": category, "reason": reason, "corrections": []}


def corrected(*corrections, category="Tax", reason="fields corrected from the page"):
    return {"decision": "CORRECTED", "category": category, "reason": reason, "corrections": list(corrections)}


def correction(field, new_value, quote, old_value="", source_url="https://inst.org/events/gst-audit-workshop", reason="stated on the page"):
    return {"field": field, "old_value": old_value, "new_value": new_value, "evidence_quote": quote, "source_url": source_url, "reason": reason}


def rejected(reason, quote, category="None", source_url="https://inst.org/events/gst-audit-workshop"):
    return {"decision": "REJECTED", "category": category, "reason": reason, "corrections": [],
            "rejection": {"reason": reason, "evidence_quote": quote, "source_url": source_url}}
