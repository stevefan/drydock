"""Tests for V2 audit emitter (Slice 4a).

These pin the stable contract from docs/v2-design-state.md §1a:
- Event-name vocabulary is fixed; unknown events raise.
- Required keys present with the spec'd names (`ts`, not `timestamp`;
  `principal`, `request_id`, `method`, `result`, `details`).
- v1 `log_event` and v2 `emit_audit` coexist in the same JSONL file
  without one corrupting the other's reader contract.
"""

import json
from datetime import datetime, timezone

import pytest

from drydock.core.audit import V2_EVENTS, emit_audit, log_event


class TestEmitAudit:
    def test_writes_v2_shape(self, tmp_path):
        log = tmp_path / "audit.log"
        emit_audit(
            "desk.created",
            principal="ws_alpha",
            request_id="req-1",
            method="CreateDesk",
            result="ok",
            details={"desk_id": "ws_alpha", "project": "p", "parent_desk_id": None},
            log_path=log,
            now=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
        )
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry == {
            "ts": "2026-04-16T12:00:00+00:00",
            "event": "desk.created",
            "principal": "ws_alpha",
            "request_id": "req-1",
            "method": "CreateDesk",
            "result": "ok",
            "details": {"desk_id": "ws_alpha", "project": "p", "parent_desk_id": None},
        }

    def test_appends_does_not_truncate(self, tmp_path):
        log = tmp_path / "audit.log"
        emit_audit("desk.created", principal=None, request_id=None,
                   method="CreateDesk", result="ok", log_path=log)
        emit_audit("desk.destroyed", principal=None, request_id=None,
                   method="DestroyDesk", result="ok", log_path=log)
        assert len(log.read_text().strip().splitlines()) == 2

    # Vocabulary is the consumer contract per §1a. A typo here is a bug
    # that ships rotten data — fail loud rather than silent-emit.
    def test_unknown_event_raises(self, tmp_path):
        log = tmp_path / "audit.log"
        with pytest.raises(ValueError, match="unknown audit event"):
            emit_audit("desk.exploded", principal=None, request_id=None,
                       method="X", result="ok", log_path=log)
        # No partial write
        assert not log.exists()

    def test_default_details_is_empty_dict_not_none(self, tmp_path):
        log = tmp_path / "audit.log"
        emit_audit("desk.created", principal=None, request_id=None,
                   method="CreateDesk", result="ok", log_path=log)
        entry = json.loads(log.read_text().strip())
        assert entry["details"] == {}

    def test_request_id_int_coerced_to_string(self, tmp_path):
        log = tmp_path / "audit.log"
        emit_audit("desk.created", principal=None, request_id=42,
                   method="CreateDesk", result="ok", log_path=log)
        entry = json.loads(log.read_text().strip())
        assert entry["request_id"] == "42"

    # The full V2 vocabulary must round-trip. Any future addition lands
    # here too — additive, not breaking.
    @pytest.mark.parametrize("event", sorted(V2_EVENTS))
    def test_every_v2_event_accepted(self, event, tmp_path):
        log = tmp_path / "audit.log"
        emit_audit(event, principal=None, request_id=None,
                   method="X", result="ok", log_path=log)
        assert json.loads(log.read_text().strip())["event"] == event


class TestV1Coexistence:
    def test_log_event_and_emit_audit_share_file(self, tmp_path):
        log = tmp_path / "audit.log"
        log_event("workspace.created", "ws_alpha", log_path=log)
        emit_audit("desk.created", principal="ws_alpha", request_id="r1",
                   method="CreateDesk", result="ok", log_path=log)

        lines = [json.loads(line) for line in log.read_text().strip().splitlines()]
        assert len(lines) == 2
        # v1 entry uses old keys; v2 entry uses new keys; consumer must
        # tolerate the union (the GetAudit handler in Slice 4c does).
        assert lines[0]["event"] == "workspace.created"
        assert "workspace_id" in lines[0]
        assert lines[1]["event"] == "desk.created"
        assert "principal" in lines[1] and "ts" in lines[1]
