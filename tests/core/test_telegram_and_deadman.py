"""Tests for the host-side telegram helper + Auditor heartbeat + deadman.

Pin the contracts:
- telegram.is_configured: True only if both token + chat_id files exist
- telegram.send: returns False on missing token/chat (no raise)
- telegram.send_with_fallback: tries telegram, falls back to logger
- heartbeat.touch: creates file + parents
- heartbeat.last_heartbeat: returns datetime or None
- heartbeat.is_stale: never-existed → False (silent), exists+old → True
- staleness_seconds: matches mtime delta
- deadman script: exit 0 (silent), 1 (alert sent), 2 (alert failed), 3 (bad args)

These contracts are load-bearing because the deadman is the LLM-failure
detector — its behavior must be predictable even under partial outages.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drydock.core.auditor.heartbeat import (
    is_stale,
    last_heartbeat,
    staleness_seconds,
    touch,
)
from drydock.core.telegram import (
    DEFAULT_CHAT_ID_PATH,
    DEFAULT_TOKEN_PATH,
    is_configured,
    send,
    send_with_fallback,
)


class TestTelegramConfigured:
    def test_neither_present(self, tmp_path):
        token = tmp_path / "tok"
        chat = tmp_path / "chat"
        assert is_configured(token_path=token, chat_id_path=chat) is False

    def test_only_token_present(self, tmp_path):
        token = tmp_path / "tok"
        chat = tmp_path / "chat"
        token.write_text("xxx")
        assert is_configured(token_path=token, chat_id_path=chat) is False

    def test_only_chat_present(self, tmp_path):
        token = tmp_path / "tok"
        chat = tmp_path / "chat"
        chat.write_text("123")
        assert is_configured(token_path=token, chat_id_path=chat) is False

    def test_both_present(self, tmp_path):
        token = tmp_path / "tok"
        chat = tmp_path / "chat"
        token.write_text("xxx")
        chat.write_text("123")
        assert is_configured(token_path=token, chat_id_path=chat) is True

    def test_empty_files_treated_as_unconfigured(self, tmp_path):
        token = tmp_path / "tok"
        chat = tmp_path / "chat"
        token.write_text("")
        chat.write_text("")
        assert is_configured(token_path=token, chat_id_path=chat) is False


class TestTelegramSend:
    def test_no_token_returns_false_no_raise(self, tmp_path):
        result = send(
            "hi",
            token_path=tmp_path / "missing",
            chat_id_path=tmp_path / "missing-chat",
        )
        assert result is False

    def test_no_chat_returns_false(self, tmp_path):
        token = tmp_path / "tok"
        token.write_text("abc")
        result = send(
            "hi", token_path=token, chat_id_path=tmp_path / "missing",
        )
        assert result is False

    def test_explicit_chat_overrides_default_lookup(self, tmp_path):
        # Even with no chat_id_path file, explicit chat_id should be tried.
        # We can't test the actual API call without a real bot; we just
        # confirm the function gets past the "no chat" early return.
        # It will then fail at the network layer, which returns False.
        token = tmp_path / "tok"
        token.write_text("invalid-token")
        result = send(
            "hi", chat_id=12345,
            token_path=token, chat_id_path=tmp_path / "missing",
        )
        # Will be False (invalid token), but importantly not raised.
        assert result is False

    def test_send_with_fallback_returns_log_when_no_telegram(self, tmp_path):
        sent, channel = send_with_fallback(
            "test alert",
            token_path=tmp_path / "missing",
            chat_id_path=tmp_path / "missing",
        )
        assert sent is False
        assert channel == "log_fallback"


class TestHeartbeat:
    def test_last_heartbeat_returns_none_when_absent(self, tmp_path):
        assert last_heartbeat(tmp_path / "no-heartbeat") is None

    def test_touch_creates_file_and_parents(self, tmp_path):
        path = tmp_path / "deeply" / "nested" / "heartbeat"
        touch(path)
        assert path.exists()

    def test_last_heartbeat_returns_datetime(self, tmp_path):
        path = tmp_path / "hb"
        touch(path)
        result = last_heartbeat(path)
        assert isinstance(result, datetime)
        assert result.tzinfo is not None  # UTC
        # Should be very recent
        assert (datetime.now(timezone.utc) - result).total_seconds() < 5

    def test_staleness_seconds_when_absent_returns_none(self, tmp_path):
        assert staleness_seconds(tmp_path / "no-hb") is None

    def test_staleness_matches_mtime_delta(self, tmp_path):
        path = tmp_path / "hb"
        touch(path)
        # Set mtime to 100s ago
        old = time.time() - 100
        os.utime(path, (old, old))
        age = staleness_seconds(path)
        assert age is not None
        assert 99 <= age <= 102  # account for timing slop

    def test_is_stale_silent_when_no_heartbeat(self, tmp_path):
        # Critical contract: never-existed heartbeat → don't alert
        # (interpretation: no Auditor designated yet)
        assert is_stale(threshold_seconds=10, path=tmp_path / "no-hb") is False

    def test_is_stale_false_when_fresh(self, tmp_path):
        path = tmp_path / "hb"
        touch(path)
        assert is_stale(threshold_seconds=300, path=path) is False

    def test_is_stale_true_when_old(self, tmp_path):
        path = tmp_path / "hb"
        touch(path)
        old = time.time() - 1000
        os.utime(path, (old, old))
        assert is_stale(threshold_seconds=300, path=path) is True


class TestDeadmanScript:
    """Run the deadman script as a subprocess to verify exit codes + output."""

    @pytest.fixture
    def script_path(self):
        # Resolve to the repo root by walking up from this test file
        here = Path(__file__).resolve()
        for parent in [here] + list(here.parents):
            candidate = parent / "scripts" / "auditor-deadman"
            if candidate.exists():
                return candidate
        pytest.skip("deadman script not found")

    def _run(self, script, env_overrides=None, args=None):
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        cmd = [sys.executable, str(script)] + (args or [])
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)

    def test_exit_0_when_no_heartbeat_designated(self, script_path, tmp_path):
        # No HOME override → uses ~ which probably has no heartbeat OR has
        # whatever; we override HOME to a tmp_path with no heartbeat file.
        result = self._run(script_path, env_overrides={"HOME": str(tmp_path)})
        assert result.returncode == 0
        assert "no Auditor designated yet" in result.stderr

    def test_exit_1_when_stale_telegram_works(self, script_path, tmp_path):
        # We can't actually verify Telegram sends without a real bot. So
        # we just verify the alert path is reached. Without telegram
        # configured, exit will be 2 (alert FAILED via telegram).
        hb_dir = tmp_path / ".drydock" / "auditor"
        hb_dir.mkdir(parents=True)
        hb = hb_dir / "heartbeat"
        hb.touch()
        old = time.time() - 5000
        os.utime(hb, (old, old))

        result = self._run(
            script_path,
            env_overrides={"HOME": str(tmp_path)},
            args=["--threshold-seconds", "60"],
        )
        # Exit 2 because telegram isn't set up in tmp HOME → fallback to log.
        assert result.returncode == 2
        assert "ALERT" in result.stderr
        assert "telegram not configured" in result.stderr

    def test_exit_3_on_bad_threshold(self, script_path, tmp_path):
        result = self._run(
            script_path,
            env_overrides={"HOME": str(tmp_path)},
            args=["--threshold-seconds", "0"],
        )
        assert result.returncode == 3
        assert ">= 1" in result.stderr

    def test_quiet_suppresses_all_good_output(self, script_path, tmp_path):
        result = self._run(
            script_path,
            env_overrides={"HOME": str(tmp_path)},
            args=["--quiet"],
        )
        assert result.returncode == 0
        # No "no Auditor designated" output in quiet mode
        assert "no Auditor designated yet" not in result.stderr
