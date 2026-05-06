"""Tests for Phase PA2 deep analysis layer.

Pin the contracts:
- parse_deep_verdict tolerates fences, prose, malformed JSON; returns
  DeepAnalysis with verdict='error' (not raise)
- VALID_VERDICTS / VALID_ACTIONS are stable contract enums
- LLM unavailable returns error verdict, no Telegram attempt
- LLM malformed returns error verdict, no Telegram attempt
- should_send_telegram=True triggers Telegram attempt; respects
  send_telegram=False parameter
- telegram_sent reflects actual send outcome
- daemon wiring: only triggers deep_analyze when verdict in
  ('anomaly_suspected', 'unsure'); routine/error don't trigger
- read_deep_log round-trips
- TELEGRAM_PROXY_PATH is touched on successful Telegram (signals
  scheduler to use responsive cadence)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drydock.core.auditor.daemon import DaemonStats, run as run_daemon
from drydock.core.auditor.deep import (
    VALID_ACTIONS,
    VALID_VERDICTS,
    DeepAnalysis,
    deep_analyze,
    parse_deep_verdict,
    read_deep_log,
)
from drydock.core.auditor.llm import LLMResponse, LLMUnavailableError, MockLLMClient
from drydock.core.auditor.measurement import HarborSnapshot
from drydock.core.auditor.watch import WatchVerdict
from drydock.core.registry import Registry


@pytest.fixture
def registry(tmp_path):
    db = tmp_path / "registry.db"
    r = Registry(db_path=db)
    yield r
    r.close()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Override HOME to keep tests from touching real ~/.drydock state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _watch_verdict(v: str = "anomaly_suspected", docs: list[str] | None = None) -> WatchVerdict:
    return WatchVerdict(
        verdict=v, reason="test reason",
        drydocks_of_concern=docs or ["auction-crawl"],
        snapshot_at="2026-05-05T12:00:00+00:00",
        tick_at="2026-05-05T12:00:01+00:00",
    )


def _snapshot() -> HarborSnapshot:
    return HarborSnapshot(
        snapshot_at="2026-05-05T12:00:00+00:00",
        harbor_hostname="test-harbor",
        drydock_count=1,
        drydocks=[{
            "name": "auction-crawl",
            "id": "ws_auction_crawl",
            "state": "running",
            "metrics": {"cpu_pct": 5.0, "mem_used_bytes": 100_000_000},
            "leases": {"active_total": 0, "by_type": {}},
            "audit_recent_1h": {"events_total": 0, "by_event_class": {}},
            "yaml_drift": "in_sync",
        }],
    )


class TestParseDeepVerdict:
    def test_clean_action_recommended(self):
        text = json.dumps({
            "verdict": "action_recommended",
            "confidence": "high",
            "reasoning": "egress 8x normal",
            "recommended_action": "throttle_egress",
            "target_drydock": "auction-crawl",
            "target_lease_id": None,
            "should_send_telegram": True,
            "escalation_message": "🟡 throttled auction-crawl",
        })
        result = parse_deep_verdict(text)
        assert result.verdict == "action_recommended"
        assert result.confidence == "high"
        assert result.recommended_action == "throttle_egress"
        assert result.target_drydock == "auction-crawl"
        assert result.should_send_telegram is True

    def test_with_code_fences(self):
        text = '```json\n{"verdict": "false_alarm", "confidence": "medium", "reasoning": "ok"}\n```'
        result = parse_deep_verdict(text)
        assert result.verdict == "false_alarm"

    def test_with_prose_around(self):
        text = (
            "Looking at the watch flag, I see this was unusual but actually fine.\n\n"
            '{"verdict": "false_alarm", "confidence": "high", "reasoning": "explained by registered workload", "should_send_telegram": false, "escalation_message": ""}\n\n'
            "No action needed."
        )
        result = parse_deep_verdict(text)
        assert result.verdict == "false_alarm"
        assert result.should_send_telegram is False

    def test_invalid_verdict(self):
        text = '{"verdict": "made_up_verdict"}'
        result = parse_deep_verdict(text)
        assert result.verdict == "error"

    def test_no_json(self):
        result = parse_deep_verdict("just prose")
        assert result.verdict == "error"

    def test_action_normalization_handles_variation(self):
        # LLM might output "throttle egress" with space — should normalize
        text = json.dumps({
            "verdict": "action_recommended",
            "confidence": "medium",
            "reasoning": "x",
            "recommended_action": "throttle egress",
            "target_drydock": "x",
            "should_send_telegram": False,
            "escalation_message": "",
        })
        result = parse_deep_verdict(text)
        assert result.recommended_action == "throttle_egress"

    def test_invalid_action_becomes_none(self):
        text = json.dumps({
            "verdict": "action_recommended",
            "confidence": "low",
            "reasoning": "x",
            "recommended_action": "totally_made_up_action",
            "target_drydock": "x",
            "should_send_telegram": False,
            "escalation_message": "",
        })
        result = parse_deep_verdict(text)
        # parsing succeeded; action couldn't be normalized → None
        assert result.verdict == "action_recommended"
        assert result.recommended_action is None

    def test_valid_verdicts_contract(self):
        # The verdicts the deep prompt produces are a stable contract
        # the daemon + log readers + UIs depend on. Renaming silently breaks consumers.
        assert VALID_VERDICTS == ("action_recommended", "escalate_only", "informational", "false_alarm")

    def test_valid_actions_contract(self):
        assert VALID_ACTIONS == ("throttle_egress", "stop_dock", "revoke_lease", "freeze_storage", None)


class TestDeepAnalyze:
    def test_llm_unavailable_returns_error_no_telegram(self, isolated_home):
        client = MockLLMClient(raise_on_call=LLMUnavailableError("no key"))
        sent_messages = []
        result = deep_analyze(
            watch_verdict=_watch_verdict(), snapshot=_snapshot(),
            llm_client=client, write_to_log=False,
            telegram_send_fn=lambda msg, **kw: sent_messages.append(msg) or True,
        )
        assert result.verdict == "error"
        assert "llm_unavailable" in (result.error or "")
        assert result.telegram_sent is False
        assert sent_messages == []  # Telegram not called when LLM errored

    def test_should_send_telegram_calls_send_fn(self, isolated_home):
        sent_messages = []
        client = MockLLMClient(responses=[LLMResponse(text=json.dumps({
            "verdict": "escalate_only",
            "confidence": "high",
            "reasoning": "real anomaly",
            "should_send_telegram": True,
            "escalation_message": "🔴 alert",
        }), input_tokens=200, output_tokens=50)])

        result = deep_analyze(
            watch_verdict=_watch_verdict(), snapshot=_snapshot(),
            llm_client=client, write_to_log=False,
            telegram_send_fn=lambda msg, **kw: sent_messages.append(msg) or True,
        )
        assert result.should_send_telegram is True
        assert result.telegram_sent is True
        assert sent_messages == ["🔴 alert"]

    def test_should_send_telegram_false_skips_send(self, isolated_home):
        sent_messages = []
        client = MockLLMClient(responses=[LLMResponse(text=json.dumps({
            "verdict": "informational",
            "confidence": "medium",
            "reasoning": "fyi only",
            "should_send_telegram": False,
            "escalation_message": "",
        }))])
        result = deep_analyze(
            watch_verdict=_watch_verdict(), snapshot=_snapshot(),
            llm_client=client, write_to_log=False,
            telegram_send_fn=lambda msg, **kw: sent_messages.append(msg) or True,
        )
        assert result.should_send_telegram is False
        assert result.telegram_sent is False
        assert sent_messages == []

    def test_send_telegram_param_overrides_to_off(self, isolated_home):
        sent_messages = []
        client = MockLLMClient(responses=[LLMResponse(text=json.dumps({
            "verdict": "escalate_only", "confidence": "high",
            "reasoning": "x", "should_send_telegram": True,
            "escalation_message": "🔴 x",
        }))])
        result = deep_analyze(
            watch_verdict=_watch_verdict(), snapshot=_snapshot(),
            llm_client=client, write_to_log=False, send_telegram=False,
            telegram_send_fn=lambda msg, **kw: sent_messages.append(msg) or True,
        )
        # LLM said yes, but caller said skip
        assert result.should_send_telegram is True
        assert result.telegram_sent is False
        assert sent_messages == []

    def test_telegram_send_failure_recorded(self, isolated_home):
        client = MockLLMClient(responses=[LLMResponse(text=json.dumps({
            "verdict": "escalate_only", "confidence": "high",
            "reasoning": "x", "should_send_telegram": True,
            "escalation_message": "🔴 x",
        }))])
        result = deep_analyze(
            watch_verdict=_watch_verdict(), snapshot=_snapshot(),
            llm_client=client, write_to_log=False,
            telegram_send_fn=lambda msg, **kw: False,  # send fails
        )
        assert result.telegram_sent is False

    def test_proxy_file_touched_on_send(self, isolated_home, monkeypatch):
        # When telegram successfully sends, the scheduler-proxy file
        # should be touched so next watch tick uses responsive cadence.
        from drydock.core.auditor import deep as deep_module
        proxy_path = isolated_home / ".drydock" / "auditor" / "last_telegram_send"
        monkeypatch.setattr(deep_module, "TELEGRAM_PROXY_PATH", proxy_path)

        client = MockLLMClient(responses=[LLMResponse(text=json.dumps({
            "verdict": "escalate_only", "confidence": "high",
            "reasoning": "x", "should_send_telegram": True,
            "escalation_message": "🔴 x",
        }))])
        deep_analyze(
            watch_verdict=_watch_verdict(), snapshot=_snapshot(),
            llm_client=client, write_to_log=False,
            telegram_send_fn=lambda msg, **kw: True,
        )
        assert proxy_path.exists()


class TestReadDeepLog:
    def test_empty(self, tmp_path):
        assert read_deep_log(log_path=tmp_path / "no-log") == []

    def test_round_trip(self, tmp_path):
        log = tmp_path / "deep.jsonl"
        log.write_text(
            json.dumps({"verdict": "false_alarm", "analyzed_at": "1"}) + "\n"
            + json.dumps({"verdict": "escalate_only", "analyzed_at": "2"}) + "\n"
        )
        items = read_deep_log(log_path=log)
        assert len(items) == 2
        assert items[0]["verdict"] == "false_alarm"
        assert items[1]["verdict"] == "escalate_only"

    def test_limit(self, tmp_path):
        log = tmp_path / "deep.jsonl"
        log.write_text(
            "\n".join(
                json.dumps({"verdict": "false_alarm", "analyzed_at": str(i)})
                for i in range(5)
            ) + "\n"
        )
        items = read_deep_log(limit=2, log_path=log)
        assert len(items) == 2
        # Newest last
        assert items[0]["analyzed_at"] == "3"
        assert items[1]["analyzed_at"] == "4"


class TestDaemonDeepIntegration:
    def test_daemon_triggers_deep_on_anomaly(self, registry, isolated_home):
        deep_calls = []
        watch_verdict = WatchVerdict(verdict="anomaly_suspected", reason="x",
                                     drydocks_of_concern=[])

        def fake_deep(*, watch_verdict, snapshot):
            deep_calls.append(watch_verdict)
            return DeepAnalysis(verdict="false_alarm", reasoning="ok")

        run_daemon(
            registry=registry, max_iterations=1, sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=lambda **k: watch_verdict,
            deep_analyze_fn=fake_deep,
        )
        assert len(deep_calls) == 1

    def test_daemon_triggers_deep_on_unsure(self, registry, isolated_home):
        deep_calls = []
        watch_verdict = WatchVerdict(verdict="unsure", reason="x")

        def fake_deep(*, watch_verdict, snapshot):
            deep_calls.append(watch_verdict)
            return DeepAnalysis(verdict="false_alarm", reasoning="ok")

        run_daemon(
            registry=registry, max_iterations=1, sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=lambda **k: watch_verdict,
            deep_analyze_fn=fake_deep,
        )
        assert len(deep_calls) == 1

    def test_daemon_skips_deep_on_routine(self, registry, isolated_home):
        deep_calls = []
        watch_verdict = WatchVerdict(verdict="routine", reason="ok")

        def fake_deep(*, watch_verdict, snapshot):
            deep_calls.append(watch_verdict)
            return DeepAnalysis(verdict="false_alarm")

        run_daemon(
            registry=registry, max_iterations=2, sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=lambda **k: watch_verdict,
            deep_analyze_fn=fake_deep,
        )
        assert deep_calls == []

    def test_daemon_skips_deep_on_error(self, registry, isolated_home):
        deep_calls = []
        watch_verdict = WatchVerdict(verdict="error", reason="x", error="llm_unavailable")

        def fake_deep(*, watch_verdict, snapshot):
            deep_calls.append(watch_verdict)
            return DeepAnalysis(verdict="false_alarm")

        run_daemon(
            registry=registry, max_iterations=1, sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=lambda **k: watch_verdict,
            deep_analyze_fn=fake_deep,
        )
        assert deep_calls == []

    def test_daemon_stats_tracks_deep_and_telegram(self, registry, isolated_home):
        watch_verdict = WatchVerdict(verdict="anomaly_suspected", reason="x")

        def fake_deep(*, watch_verdict, snapshot):
            return DeepAnalysis(verdict="escalate_only", telegram_sent=True)

        stats = run_daemon(
            registry=registry, max_iterations=2, sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=lambda **k: watch_verdict,
            deep_analyze_fn=fake_deep,
        )
        assert stats.deep_analyses == 2
        assert stats.telegram_escalations == 2

    def test_daemon_continues_when_deep_raises(self, registry, isolated_home):
        watch_verdict = WatchVerdict(verdict="anomaly_suspected", reason="x")

        def bad_deep(*, watch_verdict, snapshot):
            raise RuntimeError("deep blew up")

        # Daemon should NOT crash; just log + continue
        stats = run_daemon(
            registry=registry, max_iterations=2, sleep_fn=lambda _: None,
            next_cadence_fn=lambda **k: 1,
            watch_once_fn=lambda **k: watch_verdict,
            deep_analyze_fn=bad_deep,
        )
        assert stats.iterations == 2  # Both iterations completed despite deep raising
