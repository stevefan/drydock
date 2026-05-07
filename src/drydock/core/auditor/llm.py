"""LLM client abstraction for the Auditor (Phase PA1+).

Pluggable client interface so the watch loop and deep analysis paths can
be tested with mocks. Production uses ``AnthropicHttpClient`` (urllib-
based, no SDK dependency to keep drydock's install footprint minimal).

Credential lookup follows the auditor design (port-auditor.md):
- Today: API key primary at ``~/.drydock/daemon-secrets/anthropic_api_key``
- Future (after auth-broker lands): swap to OAuth-token primary, API as fallback

For now we only implement the API-key path. The OAuth swap is a config
change at credential-resolution time, not a code change at the call site.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

DAEMON_SECRETS_DIR = Path.home() / ".drydock" / "daemon-secrets"
# Per-drydock secrets path (mounted into containerized auditor at
# create-time via the standard secrets bind-mount). This is the
# preferred source — per-service keys mean spend is attributable per
# Anthropic console line item; revoking one key doesn't disrupt others.
PER_DRYDOCK_API_KEY_PATH = Path("/run/secrets/anthropic_api_key")
# Harbor-admin fallback for ad-hoc invocations (`drydock auditor
# watch-once` run on the Harbor host outside any container).
DAEMON_SECRETS_API_KEY_PATH = DAEMON_SECRETS_DIR / "anthropic_api_key"
DEFAULT_API_KEY_PATH = DAEMON_SECRETS_API_KEY_PATH  # back-compat alias
ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = 30


def resolve_api_key_path() -> Path:
    """Resolution order: per-drydock secret first, daemon-secrets fallback.

    Per-drydock is preferred because it lets the principal attribute
    Anthropic spend per service in the console — one key per drydock,
    revocable independently. Harbor-admin daemon-secrets is the
    fallback for Harbor-side invocations where /run/secrets isn't
    mounted (ad-hoc snapshots, `drydock auditor watch-once`).

    Returns the first readable, non-empty path. Returns the per-drydock
    path even if missing so error messages point users at the
    principled location first.
    """
    if PER_DRYDOCK_API_KEY_PATH.is_file():
        try:
            if PER_DRYDOCK_API_KEY_PATH.read_text(encoding="utf-8").strip():
                return PER_DRYDOCK_API_KEY_PATH
        except (PermissionError, OSError):
            pass
    return DAEMON_SECRETS_API_KEY_PATH


@dataclass
class LLMResponse:
    """Structured response from a single LLM call."""
    text: str
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "stop_reason": self.stop_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
        }


class LLMClient(Protocol):
    """The interface watch + deep-analysis call against. Mockable."""
    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
    ) -> LLMResponse:
        ...


class LLMUnavailableError(Exception):
    """Raised when no credential is available. Watch loop catches + skips."""


class AnthropicHttpClient:
    """Production client — calls Anthropic API via stdlib urllib."""

    def __init__(self, api_key_path: Path | None = None, timeout: int = DEFAULT_TIMEOUT):
        # When no explicit path given, resolve at construction time so
        # tests can monkeypatch resolve_api_key_path. Per-drydock path
        # wins if present + readable; falls back to daemon-secrets.
        self.api_key_path = api_key_path or resolve_api_key_path()
        self.timeout = timeout

    def _api_key(self) -> str:
        try:
            key = self.api_key_path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise LLMUnavailableError(
                f"Anthropic API key not readable at {self.api_key_path}: {exc}"
            )
        if not key:
            raise LLMUnavailableError(f"Anthropic API key at {self.api_key_path} is empty")
        return key

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
    ) -> LLMResponse:
        api_key = self._api_key()
        url = f"{ANTHROPIC_API_BASE}/v1/messages"
        body = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise LLMUnavailableError(
                f"Anthropic API HTTP {exc.code}: {err_body}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LLMUnavailableError(f"Anthropic API connection failed: {exc}") from exc

        # Parse response. The shape is:
        #   {"id": "...", "type": "message", "role": "assistant",
        #    "content": [{"type": "text", "text": "..."}],
        #    "model": "claude-...", "stop_reason": "end_turn",
        #    "usage": {"input_tokens": N, "output_tokens": N}}
        text_chunks = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_chunks.append(block.get("text", ""))
        usage = data.get("usage", {})
        return LLMResponse(
            text="".join(text_chunks),
            stop_reason=data.get("stop_reason", ""),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=data.get("model", model),
        )


# NOTE: the test-double `MockLLMClient` was previously defined here. It
# moved to `tests/core/auditor_helpers.py` because nothing in production
# uses it — it's a test-only construct, and shipping it in production
# code obscured the actual production LLMClient surface (just the
# protocol + AnthropicHttpClient).
