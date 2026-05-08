"""Tests for snapshot signature dedup (Phase PA3.9)."""
from __future__ import annotations

import time

import pytest

from drydock.core.auditor.signature import (
    FORCE_REFRESH_SECONDS,
    SignatureState,
    compute_signature,
    load_state,
    save_state,
    should_skip_llm,
)


def _snap(name="d1", state="running", cpu=10.0, mem=2 * 1024**3, leases=0):
    return {
        "harbor_hostname": "test-harbor",
        "drydock_count": 1,
        "drydocks": [{
            "name": name,
            "state": state,
            "yard_id": None,
            "metrics": {
                "cpu_pct": cpu,
                "mem_used_bytes": mem,
                "pids": 5,
            },
            "leases": {"active_total": leases, "by_type": {}},
            "audit_recent_1h": {"events_total": 0},
            "yaml_drift": "ok",
        }],
    }


class TestStability:
    def test_same_input_same_signature(self):
        s1 = compute_signature(_snap())
        s2 = compute_signature(_snap())
        assert s1 == s2

    def test_timestamp_excluded(self):
        """snapshot_at IS NOT in the signature payload — timestamps
        change every tick but state hasn't."""
        a = _snap()
        a["snapshot_at"] = "2026-05-08T00:00:00Z"
        b = _snap()
        b["snapshot_at"] = "2026-05-08T00:05:00Z"
        assert compute_signature(a) == compute_signature(b)

    def test_drydock_order_irrelevant(self):
        """Sorted by name internally so list ordering doesn't change sig."""
        a = {
            "harbor_hostname": "h", "drydock_count": 2,
            "drydocks": [_snap("a")["drydocks"][0], _snap("b")["drydocks"][0]],
        }
        b = {
            "harbor_hostname": "h", "drydock_count": 2,
            "drydocks": [_snap("b")["drydocks"][0], _snap("a")["drydocks"][0]],
        }
        assert compute_signature(a) == compute_signature(b)


class TestSensitivity:
    def test_state_change_changes_sig(self):
        s1 = compute_signature(_snap(state="running"))
        s2 = compute_signature(_snap(state="suspended"))
        assert s1 != s2

    def test_lease_change_changes_sig(self):
        s1 = compute_signature(_snap(leases=0))
        s2 = compute_signature(_snap(leases=1))
        assert s1 != s2

    def test_yaml_drift_change_changes_sig(self):
        a = _snap()
        b = _snap()
        b["drydocks"][0]["yaml_drift"] = "drifted"
        assert compute_signature(a) != compute_signature(b)


class TestBucketing:
    def test_small_cpu_wiggle_same_sig(self):
        """5% CPU vs 9% CPU both fall in the 0-10 bucket."""
        s1 = compute_signature(_snap(cpu=5.0))
        s2 = compute_signature(_snap(cpu=9.5))
        assert s1 == s2

    def test_cpu_bucket_jump_changes_sig(self):
        """5% vs 15% are different buckets."""
        s1 = compute_signature(_snap(cpu=5.0))
        s2 = compute_signature(_snap(cpu=15.0))
        assert s1 != s2

    def test_small_mem_wiggle_same_sig(self):
        """50MB delta well under the 100MB bucket."""
        s1 = compute_signature(_snap(mem=2 * 1024**3))
        s2 = compute_signature(_snap(mem=2 * 1024**3 + 50 * 1024**2))
        assert s1 == s2


class TestClarificationsInfluence:
    def test_clarifications_change_sig(self):
        """Adding a clarification should re-trigger LLM eval."""
        snap = _snap()
        s1 = compute_signature(snap, clarifications=None)
        s2 = compute_signature(snap, clarifications=[
            {"id": 1, "drydock_id": "dock_x", "kind": "workload_intent"},
        ])
        assert s1 != s2


class TestSkipLogic:
    def test_first_tick_never_skips(self):
        """Empty state → always do a real call."""
        state = SignatureState()
        assert should_skip_llm(state, "any_sig") is False

    def test_matching_sig_within_floor_skips(self):
        state = SignatureState(
            last_signature="abc",
            last_real_call_unix=time.time() - 60,
        )
        assert should_skip_llm(state, "abc") is True

    def test_matching_sig_past_floor_does_not_skip(self):
        state = SignatureState(
            last_signature="abc",
            last_real_call_unix=time.time() - (FORCE_REFRESH_SECONDS + 60),
        )
        assert should_skip_llm(state, "abc") is False

    def test_changed_sig_does_not_skip(self):
        state = SignatureState(
            last_signature="abc",
            last_real_call_unix=time.time() - 5,
        )
        assert should_skip_llm(state, "different") is False

    def test_explicit_now_used_when_passed(self):
        """now_unix override lets tests not depend on real wall-clock."""
        state = SignatureState(last_signature="x", last_real_call_unix=1000.0)
        # 200s after last real call, well within floor → skip
        assert should_skip_llm(state, "x", now_unix=1200.0) is True
        # 30min+ after → don't skip
        assert should_skip_llm(state, "x",
                                now_unix=1000.0 + FORCE_REFRESH_SECONDS + 1) is False


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "sig.json"
        save_state(path, SignatureState(
            last_signature="hash123",
            last_real_call_unix=1700000000.0,
        ))
        state = load_state(path)
        assert state.last_signature == "hash123"
        assert state.last_real_call_unix == 1700000000.0

    def test_missing_file_returns_empty_state(self, tmp_path):
        state = load_state(tmp_path / "missing.json")
        assert state.last_signature == ""
        assert state.last_real_call_unix == 0.0

    def test_corrupt_file_returns_empty_state(self, tmp_path):
        """Corrupted JSON shouldn't crash the watch loop — fall through
        to empty state and the next tick will be a real call."""
        path = tmp_path / "sig.json"
        path.write_text("not json {{{")
        state = load_state(path)
        assert state.last_signature == ""


class TestWatchOnceIntegration:
    """End-to-end pin: watch_once with dedup ON. First tick is real,
    second tick (with same registry state) is deduplicated, no LLM
    call made."""

    def test_second_identical_tick_dedups(self, tmp_path):
        from drydock.core.registry import Registry
        from drydock.core.runtime import Drydock
        from drydock.core.auditor.watch import watch_once
        from drydock.core.auditor.llm import LLMResponse

        class CountingClient:
            def __init__(self):
                self.call_count = 0

            def call(self, *, model, system, user, max_tokens):
                self.call_count += 1
                return LLMResponse(
                    text='{"verdict": "routine", "reason": "ok", "drydocks_of_concern": []}',
                    input_tokens=100, output_tokens=20, model=model,
                )

        db = tmp_path / "r.db"
        r = Registry(db_path=db)
        try:
            r.create_drydock(Drydock(name="x", project="p", repo_path="/r"))
            client = CountingClient()
            sig_path = tmp_path / "sig.json"

            v1 = watch_once(
                registry=r, llm_client=client,
                write_to_log=False, write_snapshot_to_disk=False,
                signature_state_path=sig_path,
                update_heartbeat=False,
            )
            assert v1.verdict == "routine"
            assert client.call_count == 1

            # Second call: same state → dedup, no second LLM call
            v2 = watch_once(
                registry=r, llm_client=client,
                write_to_log=False, write_snapshot_to_disk=False,
                signature_state_path=sig_path,
                update_heartbeat=False,
            )
            assert v2.verdict == "deduplicated"
            assert client.call_count == 1  # unchanged

            # Mutate the registry — third call should fire again
            r.create_drydock(Drydock(name="y", project="p", repo_path="/r"))
            v3 = watch_once(
                registry=r, llm_client=client,
                write_to_log=False, write_snapshot_to_disk=False,
                signature_state_path=sig_path,
                update_heartbeat=False,
            )
            assert v3.verdict == "routine"
            assert client.call_count == 2  # incremented
        finally:
            r.close()
