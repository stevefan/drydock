"""Tests for the auditor LLM-key credential resolution (Phase PA3.5).

Per-drydock keys at /run/secrets/anthropic_api_key (containerized
auditor) win over Harbor-admin keys at ~/.drydock/daemon-secrets/.
This is what makes per-service Anthropic accounting work — each
drydock's spend is attributable to its own key in the console.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from drydock.core.auditor.llm import (
    AnthropicHttpClient,
    DAEMON_SECRETS_API_KEY_PATH,
    PER_DRYDOCK_API_KEY_PATH,
    resolve_api_key_path,
)


class TestResolution:
    def test_per_drydock_wins_when_present(self, tmp_path, monkeypatch):
        """When /run/secrets/anthropic_api_key is readable + non-empty,
        it's preferred over the Harbor-admin path."""
        per_drydock = tmp_path / "run_secrets_anthropic"
        per_drydock.write_text("per-drydock-key")
        daemon_admin = tmp_path / "daemon_secrets_anthropic"
        daemon_admin.write_text("admin-fallback-key")
        monkeypatch.setattr(
            "drydock.core.auditor.llm.PER_DRYDOCK_API_KEY_PATH", per_drydock,
        )
        monkeypatch.setattr(
            "drydock.core.auditor.llm.DAEMON_SECRETS_API_KEY_PATH", daemon_admin,
        )
        assert resolve_api_key_path() == per_drydock

    def test_falls_back_to_daemon_secrets_when_per_drydock_missing(
        self, tmp_path, monkeypatch,
    ):
        """No /run/secrets/anthropic_api_key — typical for Harbor-side
        ad-hoc `drydock auditor watch-once`. Use admin path."""
        per_drydock = tmp_path / "missing"  # doesn't exist
        daemon_admin = tmp_path / "daemon_secrets_anthropic"
        daemon_admin.write_text("admin-key")
        monkeypatch.setattr(
            "drydock.core.auditor.llm.PER_DRYDOCK_API_KEY_PATH", per_drydock,
        )
        monkeypatch.setattr(
            "drydock.core.auditor.llm.DAEMON_SECRETS_API_KEY_PATH", daemon_admin,
        )
        assert resolve_api_key_path() == daemon_admin

    def test_falls_back_when_per_drydock_empty(self, tmp_path, monkeypatch):
        """Empty per-drydock file — treat as missing. (Catches the case
        where `drydock secret set` was run but the key didn't get piped
        in correctly — the empty-file footgun we hit on first try.)"""
        per_drydock = tmp_path / "empty"
        per_drydock.write_text("")
        daemon_admin = tmp_path / "admin"
        daemon_admin.write_text("real-key")
        monkeypatch.setattr(
            "drydock.core.auditor.llm.PER_DRYDOCK_API_KEY_PATH", per_drydock,
        )
        monkeypatch.setattr(
            "drydock.core.auditor.llm.DAEMON_SECRETS_API_KEY_PATH", daemon_admin,
        )
        assert resolve_api_key_path() == daemon_admin


class TestClientUsesResolution:
    def test_default_construction_uses_resolver(self, tmp_path, monkeypatch):
        """Constructing AnthropicHttpClient without an explicit
        api_key_path picks whatever resolve_api_key_path() returns."""
        per_drydock = tmp_path / "per_drydock"
        per_drydock.write_text("per-drydock-key")
        monkeypatch.setattr(
            "drydock.core.auditor.llm.PER_DRYDOCK_API_KEY_PATH", per_drydock,
        )
        client = AnthropicHttpClient()
        assert client.api_key_path == per_drydock

    def test_explicit_path_overrides_resolver(self, tmp_path):
        """Caller can still override (used by tests + future callers
        that resolve credentials some other way)."""
        explicit = tmp_path / "custom"
        explicit.write_text("custom-key")
        client = AnthropicHttpClient(api_key_path=explicit)
        assert client.api_key_path == explicit
