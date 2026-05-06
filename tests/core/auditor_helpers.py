"""Test helpers for the Auditor (Phase PA0/PA1/PA2).

MockLLMClient lives here (NOT in production code). Production code only
needs the LLMClient protocol + AnthropicHttpClient; the mock is for
tests only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from drydock.core.auditor.llm import LLMResponse


@dataclass
class MockLLMClient:
    """Test double — returns pre-canned responses without network calls.

    Use in tests that exercise watch-loop / deep-analysis logic without
    needing a real API key. Configure with `responses` (returned in order)
    or `raise_on_call` to simulate LLM unavailability.
    """
    responses: list[LLMResponse] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    raise_on_call: Exception | None = None

    def call(
        self, *, model: str, system: str, user: str, max_tokens: int,
    ) -> LLMResponse:
        self.calls.append({
            "model": model, "system": system, "user": user, "max_tokens": max_tokens,
        })
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if not self.responses:
            return LLMResponse(text='{"verdict": "routine"}', model=model)
        return self.responses.pop(0)
