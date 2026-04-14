"""Tests for the audit log primitive."""

import json

from drydock.core.audit import log_event


class TestLogEvent:
    def test_writes_valid_json_line(self, tmp_path):
        log_path = tmp_path / "audit.log"
        log_event("workspace.created", "ws_test", log_path=log_path)
        line = log_path.read_text().strip()
        entry = json.loads(line)
        assert entry["event"] == "workspace.created"
        assert entry["workspace_id"] == "ws_test"
        assert "timestamp" in entry

    def test_extra_fields_merged(self, tmp_path):
        log_path = tmp_path / "audit.log"
        log_event("workspace.created", "ws_test", extra={"container_id": "abc123"}, log_path=log_path)
        entry = json.loads(log_path.read_text().strip())
        assert entry["container_id"] == "abc123"

    def test_appends_multiple_events(self, tmp_path):
        log_path = tmp_path / "audit.log"
        log_event("workspace.created", "ws_a", log_path=log_path)
        log_event("workspace.running", "ws_a", log_path=log_path)
        log_event("workspace.stopped", "ws_a", log_path=log_path)
        lines = [l for l in log_path.read_text().strip().split("\n") if l]
        assert len(lines) == 3
        events = [json.loads(l)["event"] for l in lines]
        assert events == ["workspace.created", "workspace.running", "workspace.stopped"]

    def test_creates_parent_directories(self, tmp_path):
        log_path = tmp_path / "nested" / "dir" / "audit.log"
        log_event("workspace.created", "ws_test", log_path=log_path)
        assert log_path.exists()

    def test_round_trip_all_events(self, tmp_path):
        log_path = tmp_path / "audit.log"
        events = [
            ("workspace.created", "ws_1", None),
            ("workspace.running", "ws_1", {"container_id": "ctr"}),
            ("workspace.error", "ws_2", {"reason": "boom"}),
            ("workspace.stopped", "ws_1", None),
            ("workspace.destroyed", "ws_1", None),
        ]
        for event, ws_id, extra in events:
            log_event(event, ws_id, extra=extra, log_path=log_path)

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 5
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["event"] == events[i][0]
            assert entry["workspace_id"] == events[i][1]
