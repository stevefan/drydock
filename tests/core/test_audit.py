"""Tests for the audit log primitive."""

import json

from drydock.core.audit import log_event


class TestLogEvent:
    def test_writes_valid_json_line(self, tmp_path):
        log_path = tmp_path / "audit.log"
        log_event("drydock.created", "dock_test", log_path=log_path)
        line = log_path.read_text().strip()
        entry = json.loads(line)
        assert entry["event"] == "drydock.created"
        assert entry["drydock_id"] == "dock_test"
        assert "timestamp" in entry

    def test_extra_fields_merged(self, tmp_path):
        log_path = tmp_path / "audit.log"
        log_event("drydock.created", "dock_test", extra={"container_id": "abc123"}, log_path=log_path)
        entry = json.loads(log_path.read_text().strip())
        assert entry["container_id"] == "abc123"

    def test_appends_multiple_events(self, tmp_path):
        log_path = tmp_path / "audit.log"
        log_event("drydock.created", "dock_a", log_path=log_path)
        log_event("drydock.running", "dock_a", log_path=log_path)
        log_event("drydock.stopped", "dock_a", log_path=log_path)
        lines = [l for l in log_path.read_text().strip().split("\n") if l]
        assert len(lines) == 3
        events = [json.loads(l)["event"] for l in lines]
        assert events == ["drydock.created", "drydock.running", "drydock.stopped"]

    def test_creates_parent_directories(self, tmp_path):
        log_path = tmp_path / "nested" / "dir" / "audit.log"
        log_event("drydock.created", "dock_test", log_path=log_path)
        assert log_path.exists()

    def test_round_trip_all_events(self, tmp_path):
        log_path = tmp_path / "audit.log"
        events = [
            ("drydock.created", "ws_1", None),
            ("drydock.running", "ws_1", {"container_id": "ctr"}),
            ("drydock.error", "ws_2", {"reason": "boom"}),
            ("drydock.stopped", "ws_1", None),
            ("drydock.destroyed", "ws_1", None),
        ]
        for event, dock_id, extra in events:
            log_event(event, dock_id, extra=extra, log_path=log_path)

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 5
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["event"] == events[i][0]
            assert entry["drydock_id"] == events[i][1]
