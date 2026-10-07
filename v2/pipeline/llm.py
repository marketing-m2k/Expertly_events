"""Gemini access with TYPED failures and a spend cap.

Why typed: v1 turned "the API budget ran out" and "the model said nothing" into
rejections or silent defaults. Here a failure is always one of

    LLMUnavailable   no key / network / server / rate limit exhausted   -> event stays NEEDS_AI_REVIEW
    BudgetExceeded   the run's dollar cap was reached                    -> event stays NEEDS_AI_REVIEW
    LLMBadResponse   the model answered with something unusable          -> event stays NEEDS_AI_REVIEW

and none of them can ever become a REJECTED event.

The default model is the same flash-lite model v1 used; the $2 per-run cap is
kept (raise it only deliberately).
"""

from __future__ import annotations

import json
import os
import threading
import time

INPUT_COST_PER_TOKEN = 0.25 / 1_000_000
OUTPUT_COST_PER_TOKEN = 1.50 / 1_000_000
DEFAULT_BUDGET_USD = 2.00
DEFAULT_MODEL = "gemini-3.1-flash-lite"


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    pass


class BudgetExceeded(LLMError):
    pass


class LLMBadResponse(LLMError):
    pass


class Budget:
    def __init__(self, cap_usd: float = DEFAULT_BUDGET_USD):
        self.cap, self.spent, self.calls = cap_usd, 0.0, 0
        self.input_tokens = self.output_tokens = 0
        self._lock = threading.Lock()

    def check(self) -> None:
        if self.spent >= self.cap:
            raise BudgetExceeded(f"Gemini spend ${self.spent:.4f} reached the ${self.cap:.2f} cap after {self.calls} calls")

    def record(self, prompt_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.spent += prompt_tokens * INPUT_COST_PER_TOKEN + output_tokens * OUTPUT_COST_PER_TOKEN
            self.calls += 1
            self.input_tokens += prompt_tokens
            self.output_tokens += output_tokens


class GeminiClient:
    """call(prompt, schema) -> parsed JSON dict."""

    def __init__(self, budget: Budget | None = None, model: str | None = None, api_key: str | None = None, retries: int = 3):
        self.budget = budget or Budget()
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.retries = retries
        self._key = api_key
        self._client = None

    def _get(self):
        if self._client is None:
            key = self._key or os.environ.get("GEMINI_API_KEY")
            if not key:
                try:
                    from dotenv import load_dotenv
                    for p in (".env", "../.env"):
                        if os.path.exists(p):
                            load_dotenv(p)
                    key = os.environ.get("GEMINI_API_KEY")
                except ImportError:
                    pass
            if not key:
                raise LLMUnavailable("GEMINI_API_KEY is not set")
            from google import genai
            from google.genai import types
            self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=90_000))   # a hung request must fail, never block the run
        return self._client

    def call(self, prompt: str, schema: dict) -> dict:
        self.budget.check()
        client = self._get()
        from google.genai import types
        config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)
        last = None
        for attempt in range(self.retries + 1):
            try:
                response = client.models.generate_content(model=self.model, contents=prompt, config=config)
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                code = getattr(exc, "code", None)
                retryable = code in (429, 500, 502, 503, 504) or exc.__class__.__name__ in ("ServerError", "ConnectError", "ReadTimeout", "ConnectTimeout", "NetworkError")
                if retryable and attempt < self.retries:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise LLMUnavailable(f"{exc.__class__.__name__}: {str(exc)[:160]}") from exc
        else:
            raise LLMUnavailable(f"gave up: {last!s:.160}")
        usage = getattr(response, "usage_metadata", None)
        self.budget.record(getattr(usage, "prompt_token_count", 0) or 0, getattr(usage, "candidates_token_count", 0) or 0)
        try:
            data = json.loads(response.text)
        except (ValueError, TypeError, AttributeError) as exc:
            raise LLMBadResponse(f"the model did not return JSON: {str(exc)[:100]}") from exc
        if not isinstance(data, dict):
            raise LLMBadResponse("the model returned JSON that is not an object")
        return data
