"""Tests for paginated GetAudit handler (Slice 4c).

Pin contracts:
- Empty file → empty result (not error).
- Newest-first ordering.
- Pagination cursor (`next_before_ts`) when more events than limit.
- Filters: event, principal (matches v1 workspace_id too), before_ts.
- Filter validation: limit bounds, type checks.
- v1 + v2 entries coexist in the same response.
- Malformed JSONL lines are skipped, not fatal.
"""

import json
from pathlib import Path

import pytest

from drydock.wsd.audit_handlers import DEFAULT_LIMIT, MAX_LIMIT, get_audit
from drydock.wsd.server import _RpcError


def _write_log(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _v2(event, ts, principal=None, **details):
    return {
        "ts": ts,
        "event": event,
        "principal": principal,
        "request_id": None,
        "method": "X",
        "result": "ok",
        "details": details,
    }


def _v1(event, ts, workspace_id):
    return {"timestamp": ts, "event": event, "workspace_id": workspace_id}


class TestGetAudit:
    def test_empty_when_log_missing(self, tmp_path):
        result = get_audit(None, None, None, log_path=tmp_path / "nope.log")
        assert result == {"events": [], "next_before_ts": None}

    def test_empty_when_log_empty(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text("")
        result = get_audit(None, None, None, log_path=log)
        assert result == {"events": [], "next_before_ts": None}

    def test_returns_newest_first(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v2("desk.created", "2026-04-16T10:00:00+00:00"),
            _v2("desk.destroyed", "2026-04-16T11:00:00+00:00"),
            _v2("lease.issued", "2026-04-16T09:00:00+00:00"),
        ])
        result = get_audit(None, None, None, log_path=log)
        names = [e["event"] for e in result["events"]]
        assert names == ["desk.destroyed", "desk.created", "lease.issued"]

    def test_pagination_cursor_when_over_limit(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v2("desk.created", f"2026-04-16T10:0{i}:00+00:00") for i in range(5)
        ])
        result = get_audit({"limit": 3}, None, None, log_path=log)
        assert len(result["events"]) == 3
        # Newest-first: events[0]=10:04, [1]=10:03, [2]=10:02
        # Cursor is the last-returned entry's ts so caller passes
        # before_ts=cursor to get older events.
        assert result["next_before_ts"] == "2026-04-16T10:02:00+00:00"

    def test_no_cursor_when_within_limit(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [_v2("desk.created", "2026-04-16T10:00:00+00:00")])
        result = get_audit({"limit": 100}, None, None, log_path=log)
        assert result["next_before_ts"] is None

    def test_event_filter(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v2("desk.created", "2026-04-16T10:00:00+00:00"),
            _v2("lease.issued", "2026-04-16T11:00:00+00:00"),
            _v2("desk.destroyed", "2026-04-16T12:00:00+00:00"),
        ])
        result = get_audit({"event": "lease.issued"}, None, None, log_path=log)
        assert len(result["events"]) == 1
        assert result["events"][0]["event"] == "lease.issued"

    def test_principal_filter_matches_v2_principal(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v2("desk.created", "2026-04-16T10:00:00+00:00", principal="ws_alpha"),
            _v2("desk.created", "2026-04-16T11:00:00+00:00", principal="ws_beta"),
        ])
        result = get_audit({"principal": "ws_alpha"}, None, None, log_path=log)
        assert len(result["events"]) == 1
        assert result["events"][0]["principal"] == "ws_alpha"

    # Cross-shape compat: v1 entries use `workspace_id` not `principal`.
    # Filtering by principal should still match them — same conceptual field.
    def test_principal_filter_matches_v1_workspace_id(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v1("workspace.created", "2026-04-16T10:00:00+00:00", "ws_alpha"),
            _v1("workspace.created", "2026-04-16T11:00:00+00:00", "ws_beta"),
        ])
        result = get_audit({"principal": "ws_alpha"}, None, None, log_path=log)
        assert len(result["events"]) == 1
        assert result["events"][0]["workspace_id"] == "ws_alpha"

    def test_before_ts_filter(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v2("desk.created", "2026-04-16T10:00:00+00:00"),
            _v2("desk.created", "2026-04-16T11:00:00+00:00"),
            _v2("desk.created", "2026-04-16T12:00:00+00:00"),
        ])
        result = get_audit(
            {"before_ts": "2026-04-16T11:00:00+00:00"},
            None, None, log_path=log,
        )
        # Strict less-than — the 11:00 event is excluded
        assert len(result["events"]) == 1
        assert result["events"][0]["ts"] == "2026-04-16T10:00:00+00:00"

    def test_v1_and_v2_coexist_in_response(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v1("workspace.created", "2026-04-16T10:00:00+00:00", "ws_a"),
            _v2("desk.created", "2026-04-16T11:00:00+00:00"),
        ])
        result = get_audit(None, None, None, log_path=log)
        assert len(result["events"]) == 2
        # Both shapes preserved as-written; consumer reads union of keys.
        shapes = {("ts" in e, "timestamp" in e) for e in result["events"]}
        assert shapes == {(True, False), (False, True)}

    def test_malformed_json_skipped(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text(
            json.dumps(_v2("desk.created", "2026-04-16T10:00:00+00:00")) + "\n"
            + "not valid json\n"
            + json.dumps(_v2("desk.destroyed", "2026-04-16T11:00:00+00:00")) + "\n"
        )
        result = get_audit(None, None, None, log_path=log)
        assert len(result["events"]) == 2

    @pytest.mark.parametrize("bad", [0, -1, MAX_LIMIT + 1, "100", None])
    def test_invalid_limit_rejected(self, tmp_path, bad):
        log = tmp_path / "audit.log"
        log.write_text("")
        if bad is None:
            params = {"limit": None}  # explicit None should also reject
        else:
            params = {"limit": bad}
        with pytest.raises(_RpcError) as exc:
            get_audit(params, None, None, log_path=log)
        assert exc.value.message == "invalid_params"

    def test_default_limit_when_omitted(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_log(log, [
            _v2("desk.created", f"2026-04-16T10:00:0{i}+00:00") for i in range(150)
        ])
        result = get_audit(None, None, None, log_path=log)
        assert len(result["events"]) == DEFAULT_LIMIT
