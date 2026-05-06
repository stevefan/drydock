"""Tests for Phase PA1 adaptive cadence + daemon loop.

Pin the contracts:
- next_cadence: highest-priority signal wins; correct cadence per signal
- has_recent_broker_activity: scans audit log tail; cutoff window respected
- has_sustained_quiet: opposite of activity; conservative when log absent
- is_night: handles wraps (e.g. 22:00 → 06:00)
- has_open_telegram_thread: reads proxy file mtime
- daemon.run: respects max_iterations, handles errors without crashing,
  cadences_chosen accumulates, consecutive_errors tracking is correct
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from drydock.core.auditor.daemon import DaemonStats, run as run_daemon
from drydock.core.auditor.scheduler import (
    CADENCE_DEFAULT,
    CADENCE_NEAR_RESPONSIVE,
    CADENCE_QUIET,
    CADENCE_RESPONSIVE,
    has_open_telegram_thread,
    has_recent_broker_activity,
    has_sustained_quiet,
    is_night,
    next_cadence,
)
from drydock.core.auditor.watch import WatchVerdict
from drydock.core.registry import Registry


@pytest.fixture
def registry(tmp_path):
    db = tmp_path / "registry.db"
    r = Registry(db_path=db)
    yield r
    r.close()


def _audit_log_with_events(path: Path, events: list[dict]):
    """Write a sequence of audit events to a log file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


class TestIsNight:
    def test_in_window(self):
        # 03:00 local during default 02:00-06:00 window
        # We use astimezone() in the function, so we have to construct
        # a datetime that lands at the right local hour. For test
        # determinism, fix to a known UTC offset by constructing with
        # local timezone.
        # Easiest: just confirm the simple range case works.
        now = datetime(2026, 5, 5, 3, 0, tzinfo=timezone.utc)
        # If local matches UTC, will be in window. Test the logic with
        # explicit start/end.
        assert is_night(now, start=time(2, 0), end=time(6, 0)) == (
            now.astimezone().time().hour in (2, 3, 4, 5)
        )

    def test_outside_window(self):
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        # Set window to definitely-not-now
        assert is_night(now, start=time(2, 0), end=time(3, 0)) is False

    def test_wraps_midnight(self):
        # Window 22:00 → 06:00 wraps midnight; verify both halves work
        now_late = datetime(2026, 5, 5, 23, 0, tzinfo=timezone.utc)
        now_early = datetime(2026, 5, 5, 5, 0, tzinfo=timezone.utc)
        # Use UTC-time as local-time approximation
        if now_late.astimezone().time().hour == 23:
            assert is_night(now_late, start=time(22, 0), end=time(6, 0)) is True
        if now_early.astimezone().time().hour == 5:
            assert is_night(now_early, start=time(22, 0), end=time(6, 0)) is True


class TestHasRecentBrokerActivity:
    def test_no_log_returns_false(self, tmp_path):
        result = has_recent_broker_activity(
            now=datetime.now(timezone.utc),
            audit_path=tmp_path / "missing",
        )
        assert result is False

    def test_empty_log_returns_false(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text("")
        result = has_recent_broker_activity(
            now=datetime.now(timezone.utc), audit_path=log,
        )
        assert result is False

    def test_event_in_window_returns_true(self, tmp_path):
        log = tmp_path / "audit.log"
        recent = datetime.now(timezone.utc).isoformat()
        _audit_log_with_events(log, [{"timestamp": recent, "event": "x"}])
        result = has_recent_broker_activity(
            now=datetime.now(timezone.utc), audit_path=log,
        )
        assert result is True

    def test_old_event_outside_window_returns_false(self, tmp_path):
        log = tmp_path / "audit.log"
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        _audit_log_with_events(log, [{"timestamp": old, "event": "x"}])
        result = has_recent_broker_activity(
            now=datetime.now(timezone.utc),
            window=timedelta(minutes=10),
            audit_path=log,
        )
        assert result is False

    def test_malformed_lines_skipped(self, tmp_path):
        log = tmp_path / "audit.log"
        recent = datetime.now(timezone.utc).isoformat()
        log.write_text(
            "not json\n"
            + json.dumps({"timestamp": recent, "event": "x"}) + "\n"
            + "\n"
        )
        result = has_recent_broker_activity(
            now=datetime.now(timezone.utc), audit_path=log,
        )
        assert result is True


class TestHasSustainedQuiet:
    def test_no_log_returns_false_conservative(self, tmp_path):
        # No log = "we don't have data" — don't claim quiet
        result = has_sustained_quiet(
            now=datetime.now(timezone.utc),
            audit_path=tmp_path / "missing",
        )
        assert result is False

    def test_empty_log_returns_true(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text("")
        # Log exists but no events — quiet
        assert has_sustained_quiet(
            now=datetime.now(timezone.utc), audit_path=log,
        ) is True

    def test_recent_event_returns_false(self, tmp_path):
        log = tmp_path / "audit.log"
        recent = datetime.now(timezone.utc).isoformat()
        _audit_log_with_events(log, [{"timestamp": recent, "event": "x"}])
        assert has_sustained_quiet(
            now=datetime.now(timezone.utc), audit_path=log,
        ) is False


class TestHasOpenTelegramThread:
    def test_no_proxy_file_returns_false(self, tmp_path):
        result = has_open_telegram_thread(
            now=datetime.now(timezone.utc),
            proxy_path=tmp_path / "missing",
        )
        assert result is False

    def test_recent_proxy_returns_true(self, tmp_path):
        proxy = tmp_path / "last_send"
        proxy.touch()
        assert has_open_telegram_thread(
            now=datetime.now(timezone.utc), proxy_path=proxy,
        ) is True

    def test_old_proxy_returns_false(self, tmp_path):
        import os, time as _time
        proxy = tmp_path / "last_send"
        proxy.touch()
        old = _time.time() - 1000
        os.utime(proxy, (old, old))
        result = has_open_telegram_thread(
            now=datetime.now(timezone.utc),
            window=timedelta(minutes=10),
            proxy_path=proxy,
        )
        assert result is False


class TestNextCadence:
    def test_default_when_no_signals(self, tmp_path):
        # No audit log, no telegram proxy — goes to default OR quiet
        # depending on time of day. Force a known not-night time.
        now = datetime(2026, 5, 5, 14, 0, tzinfo=timezone.utc)
        cadence = next_cadence(
            now=now,
            audit_path=tmp_path / "missing-audit",
            telegram_proxy_path=tmp_path / "missing-proxy",
        )
        # Local clock at noon UTC may or may not be night locally; if
        # not night and no audit log, falls to DEFAULT.
        # When audit log is missing, has_sustained_quiet returns False.
        # So should be DEFAULT, unless local time is in night window.
        assert cadence in (CADENCE_DEFAULT, CADENCE_QUIET)

    def test_telegram_thread_overrides_to_responsive(self, tmp_path):
        proxy = tmp_path / "last_send"
        proxy.touch()  # very recent
        cadence = next_cadence(
            now=datetime.now(timezone.utc),
            audit_path=tmp_path / "missing-audit",
            telegram_proxy_path=proxy,
        )
        assert cadence == CADENCE_RESPONSIVE

    def test_recent_broker_activity_to_near_responsive(self, tmp_path):
        log = tmp_path / "audit.log"
        recent = datetime.now(timezone.utc).isoformat()
        _audit_log_with_events(log, [{"timestamp": recent, "event": "x"}])
        cadence = next_cadence(
            now=datetime.now(timezone.utc),
            audit_path=log,
            telegram_proxy_path=tmp_path / "missing-proxy",
        )
        assert cadence == CADENCE_NEAR_RESPONSIVE

    def test_sustained_quiet_to_quiet_cadence(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text("")  # exists, empty → quiet
        cadence = next_cadence(
            now=datetime.now(timezone.utc),
            audit_path=log,
            telegram_proxy_path=tmp_path / "missing-proxy",
        )
        assert cadence == CADENCE_QUIET

    def test_signal_priority_telegram_beats_activity(self, tmp_path):
        # Both signals present; telegram is highest priority.
        log = tmp_path / "audit.log"
        recent = datetime.now(timezone.utc).isoformat()
        _audit_log_with_events(log, [{"timestamp": recent, "event": "x"}])
        proxy = tmp_path / "last_send"
        proxy.touch()
        cadence = next_cadence(
            now=datetime.now(timezone.utc),
            audit_path=log, telegram_proxy_path=proxy,
        )
        assert cadence == CADENCE_RESPONSIVE


class TestDaemon:
    def test_runs_max_iterations(self, registry):
        verdict = WatchVerdict(verdict="routine", reason="ok",
                                tick_at="2026-05-05T12:00:00+00:00")
        sleeps = []
        stats = run_daemon(
            registry=registry,
            max_iterations=3,
            sleep_fn=sleeps.append,
            next_cadence_fn=lambda **k: 60,
            watch_once_fn=lambda **k: verdict,
        )
        assert stats.iterations == 3
        assert stats.last_verdict == "routine"
        assert stats.consecutive_errors == 0
        # max_iterations check happens BEFORE the final sleep — so 3
        # iterations produce 2 sleeps (no sleep after the last tick).
        assert len(sleeps) == 2
        # But all 3 cadence decisions were recorded (sleep just isn't
        # called for the last one).
        assert stats.cadences_chosen == [60, 60, 60]

    def test_consecutive_errors_track_and_reset(self, registry):
        # First two ticks error; third recovers
        responses = [
            WatchVerdict(verdict="error", reason="x", error="llm_unavailable"),
            WatchVerdict(verdict="error", reason="x", error="llm_unavailable"),
            WatchVerdict(verdict="routine", reason="ok"),
        ]

        def watch_fn(**kwargs):
            return responses.pop(0)

        stats = run_daemon(
            registry=registry,
            max_iterations=3,
            sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=watch_fn,
        )
        assert stats.iterations == 3
        # Consecutive errors reset on the recovery tick
        assert stats.consecutive_errors == 0

    def test_consecutive_errors_keep_climbing(self, registry):
        verdict = WatchVerdict(verdict="error", reason="x",
                                error="llm_unavailable")
        stats = run_daemon(
            registry=registry,
            max_iterations=5,
            sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=lambda **k: verdict,
        )
        assert stats.iterations == 5
        assert stats.consecutive_errors == 5

    def test_on_iteration_complete_called(self, registry):
        verdict = WatchVerdict(verdict="routine", reason="ok")
        callbacks = []
        run_daemon(
            registry=registry,
            max_iterations=2,
            sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 60,
            watch_once_fn=lambda **k: verdict,
            on_iteration_complete=lambda v, c: callbacks.append((v.verdict, c)),
        )
        assert callbacks == [("routine", 60), ("routine", 60)]

    def test_unexpected_exception_reraises(self, registry):
        def bad_watch(**kwargs):
            raise RuntimeError("totally unexpected")
        with pytest.raises(RuntimeError, match="totally unexpected"):
            run_daemon(
                registry=registry,
                max_iterations=1,
                sleep_fn=lambda _: None,
                next_cadence_fn=lambda **k: 1,
                watch_once_fn=bad_watch,
            )
