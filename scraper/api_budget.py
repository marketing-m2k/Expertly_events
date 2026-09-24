"""Tracks cumulative Gemini API spend across a run and stops classification
before it goes past a hard dollar cap, instead of finding out after the
bill arrives.

Pricing is gemini-3.1-flash-lite's published per-token rate (as of Sep 2026):
$0.25 / 1M input tokens, $1.50 / 1M output tokens.
"""

INPUT_COST_PER_TOKEN = 0.25 / 1_000_000
OUTPUT_COST_PER_TOKEN = 1.50 / 1_000_000

BUDGET_USD = 2.00

_total_cost = 0.0
_call_count = 0


class BudgetExceededError(RuntimeError):
    """Raised when recording a call's usage would push spend past BUDGET_USD."""


def record_usage(prompt_tokens: int, output_tokens: int) -> float:
    """Add one API call's token usage to the running total. Raises
    BudgetExceededError if the cap is now exceeded. Returns the new total."""
    global _total_cost, _call_count
    cost = prompt_tokens * INPUT_COST_PER_TOKEN + output_tokens * OUTPUT_COST_PER_TOKEN
    _total_cost += cost
    _call_count += 1
    if _total_cost >= BUDGET_USD:
        raise BudgetExceededError(
            f"Gemini API spend hit ${_total_cost:.4f} (cap ${BUDGET_USD:.2f}) "
            f"after {_call_count} calls -- stopping."
        )
    return _total_cost


def get_total_cost() -> float:
    return _total_cost


def get_call_count() -> int:
    return _call_count
