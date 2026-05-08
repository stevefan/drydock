"""Tests for Phase PA1 Auditor watch loop.

Pin the contracts:
- parse_verdict tolerates code fences, prose-around-JSON, returns
  'error' (not raise) on malformed input
- format_snapshot_for_llm produces valid JSON, prunes verbose fields
- watch_once with MockLLMClient: each verdict path works end-to-end
- watch_once on LLM-unavailable: returns error verdict, does NOT
  update heartbeat (deadman should fire)
- watch_once on LLM-malformed-response: returns error verdict, DOES
  update heartbeat (LLM was reachable, just confused — not a deadman case)
- write/read watch_log round-trip
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drydock.core.auditor.heartbeat import last_heartbeat
from drydock.core.auditor.llm import LLMResponse, LLMUnavailableError
from tests.core.auditor_helpers import MockLLMClient
from drydock.core.auditor.measurement import HarborSnapshot
from drydock.core.auditor.watch import (
    DEFAULT_WATCH_MODEL,
    format_snapshot_for_llm,
    parse_verdict,
    read_watch_log,
    watch_once,
)
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock


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


def _ws(name: str) -> Drydock:
    return Drydock(
        name=name, project=name, repo_path="/tmp/r",
        worktree_path=f"/tmp/{name}", branch=f"ws/{name}",
        state="running", container_id=f"cid_{name}",
    )


class TestParseVerdict:
    def test_clean_json(self):
        text = '{"verdict": "routine", "reason": "all good", "drydocks_of_concern": []}'
        assert parse_verdict(text) == ("routine", "all good", [])

    def test_with_code_fences(self):
        text = '```json\n{"verdict": "routine", "reason": "ok", "drydocks_of_concern": []}\n```'
        verdict, reason, docs = parse_verdict(text)
        assert verdict == "routine"
        assert reason == "ok"

    def test_with_prose_around(self):
        text = (
            "Looking at the snapshot, I see normal patterns.\n\n"
            '{"verdict": "routine", "reason": "no anomalies", "drydocks_of_concern": []}\n\n'
            "Everything checks out."
        )
        verdict, reason, docs = parse_verdict(text)
        assert verdict == "routine"

    def test_anomaly_with_drydocks(self):
        text = '{"verdict": "anomaly_suspected", "reason": "high mem", "drydocks_of_concern": ["a", "b"]}'
        assert parse_verdict(text) == ("anomaly_suspected", "high mem", ["a", "b"])

    def test_invalid_verdict_returns_error(self):
        text = '{"verdict": "totally_made_up", "reason": "x"}'
        verdict, reason, _ = parse_verdict(text)
        assert verdict == "error"
        assert "invalid verdict" in reason

    def test_no_json_returns_error(self):
        verdict, reason, _ = parse_verdict("just some prose, no JSON here")
        assert verdict == "error"
        assert "no JSON object" in reason

    def test_malformed_json_returns_error(self):
        verdict, reason, _ = parse_verdict('{"verdict": broken')
        assert verdict == "error"

    def test_non_object_returns_error(self):
        verdict, reason, _ = parse_verdict("[1, 2, 3]")
        assert verdict == "error"
        assert "no JSON object" in reason or "expected" in reason

    def test_missing_drydocks_field_defaults_to_empty(self):
        text = '{"verdict": "routine", "reason": "ok"}'
        _, _, docs = parse_verdict(text)
        assert docs == []

    def test_drydocks_field_strings_only(self):
        text = '{"verdict": "anomaly_suspected", "reason": "x", "drydocks_of_concern": ["a", null, "", "b"]}'
        _, _, docs = parse_verdict(text)
        assert docs == ["a", "b"]


class TestFormatSnapshot:
    def test_produces_valid_json(self, registry):
        registry.create_drydock(_ws("a"))
        from drydock.core.auditor.measurement import snapshot_harbor
        snap = snapshot_harbor(registry, hostname="test")
        result = format_snapshot_for_llm(snap)
        # Must parse as JSON
        parsed = json.loads(result)
        assert parsed["harbor"] == "test"
        assert len(parsed["drydocks"]) == 1
        assert parsed["drydocks"][0]["name"] == "a"


class TestWatchOnce:
    def test_routine_verdict_path(self, registry, isolated_home):
        client = MockLLMClient(responses=[LLMResponse(
            text='{"verdict": "routine", "reason": "all good", "drydocks_of_concern": []}',
            input_tokens=500, output_tokens=20, model="claude-haiku-4-5",
        )])
        verdict = watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                              write_to_log=False, write_snapshot_to_disk=False)
        assert verdict.verdict == "routine"
        assert verdict.reason == "all good"
        assert verdict.input_tokens == 500
        assert verdict.output_tokens == 20

    def test_anomaly_verdict_path(self, registry, isolated_home):
        registry.create_drydock(_ws("auction-crawl"))
        client = MockLLMClient(responses=[LLMResponse(
            text='{"verdict": "anomaly_suspected", "reason": "egress 8x", "drydocks_of_concern": ["auction-crawl"]}',
        )])
        verdict = watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                              write_to_log=False, write_snapshot_to_disk=False)
        assert verdict.verdict == "anomaly_suspected"
        assert verdict.drydocks_of_concern == ["auction-crawl"]

    def test_unsure_verdict_path(self, registry, isolated_home):
        client = MockLLMClient(responses=[LLMResponse(
            text='{"verdict": "unsure", "reason": "weird pattern, want second look", "drydocks_of_concern": ["x"]}',
        )])
        verdict = watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                              write_to_log=False, write_snapshot_to_disk=False)
        assert verdict.verdict == "unsure"

    def test_llm_unavailable_returns_error_no_heartbeat(self, registry, isolated_home):
        client = MockLLMClient(raise_on_call=LLMUnavailableError("no key"))
        verdict = watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                              write_to_log=False, write_snapshot_to_disk=False)
        assert verdict.verdict == "error"
        assert "llm_unavailable" in (verdict.error or "")
        # Critical contract: heartbeat NOT updated when LLM unavailable.
        # (Deadman should fire if this keeps happening.)
        assert last_heartbeat() is None

    def test_llm_malformed_response_updates_heartbeat(self, registry, isolated_home):
        # LLM was reachable but returned garbage. Heartbeat SHOULD update
        # — the watch loop is alive, just confused. Not a deadman case.
        client = MockLLMClient(responses=[LLMResponse(
            text="this is not json", input_tokens=100, output_tokens=5,
        )])
        verdict = watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                              write_to_log=False, write_snapshot_to_disk=False)
        assert verdict.verdict == "error"
        assert verdict.error is None  # Not a credential error
        # Heartbeat updated despite verdict='error'
        assert last_heartbeat() is not None

    def test_writes_to_watch_log_when_enabled(self, registry, isolated_home, tmp_path):
        log_path = tmp_path / "watch_log.jsonl"
        client = MockLLMClient(responses=[LLMResponse(
            text='{"verdict": "routine", "reason": "ok", "drydocks_of_concern": []}',
        )])
        watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                   write_to_log=True, write_snapshot_to_disk=False, log_path=log_path)
        watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                   write_to_log=True, write_snapshot_to_disk=False, log_path=log_path)
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert parsed["verdict"] == "routine"

    def test_uses_default_model(self, registry, isolated_home):
        client = MockLLMClient(responses=[LLMResponse(text='{"verdict": "routine", "reason": "x"}')])
        watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                   write_to_log=False, write_snapshot_to_disk=False)
        assert client.calls[0]["model"] == DEFAULT_WATCH_MODEL


class TestWatchLog:
    def test_empty_log_returns_empty_list(self, tmp_path):
        log = tmp_path / "wl.jsonl"
        assert read_watch_log(log_path=log) == []

    def test_round_trip(self, tmp_path, registry, isolated_home):
        log = tmp_path / "wl.jsonl"
        client = MockLLMClient(responses=[
            LLMResponse(text='{"verdict": "routine", "reason": "ok"}'),
            LLMResponse(text='{"verdict": "anomaly_suspected", "reason": "spike", "drydocks_of_concern": ["x"]}'),
        ])
        watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                   write_to_log=True, write_snapshot_to_disk=False, log_path=log)
        watch_once(registry=registry, llm_client=client, enable_signature_dedup=False,
                   write_to_log=True, write_snapshot_to_disk=False, log_path=log)

        verdicts = read_watch_log(log_path=log)
        assert len(verdicts) == 2
        assert verdicts[0]["verdict"] == "routine"
        assert verdicts[1]["verdict"] == "anomaly_suspected"

    def test_limit_respected(self, tmp_path):
        log = tmp_path / "wl.jsonl"
        log.write_text(
            '{"verdict": "routine", "tick_at": "1"}\n'
            '{"verdict": "routine", "tick_at": "2"}\n'
            '{"verdict": "routine", "tick_at": "3"}\n'
            '{"verdict": "routine", "tick_at": "4"}\n'
            '{"verdict": "routine", "tick_at": "5"}\n'
        )
        result = read_watch_log(limit=2, log_path=log)
        assert len(result) == 2
        # Newest last (chronological)
        assert result[0]["tick_at"] == "4"
        assert result[1]["tick_at"] == "5"

    def test_malformed_lines_skipped(self, tmp_path):
        log = tmp_path / "wl.jsonl"
        log.write_text(
            '{"verdict": "routine"}\n'
            'not json\n'
            '{"verdict": "anomaly_suspected"}\n'
        )
        result = read_watch_log(log_path=log)
        assert len(result) == 2
