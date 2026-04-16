"""Tests for `ws audit` host CLI (Slice 4d)."""

import json
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.audit import audit
from drydock.output.formatter import Output


def _invoke(args, force_json=True):
    runner = CliRunner()
    return runner.invoke(
        audit, args,
        obj={"output": Output(force_json=force_json), "dry_run": False},
    )


class TestAuditCli:
    @patch("drydock.cli.audit.call_daemon")
    def test_routes_to_daemon_with_filter_params(self, mock_call):
        mock_call.return_value = {
            "events": [
                {"ts": "2026-04-16T10:00:00+00:00", "event": "desk.created",
                 "principal": "ws_a", "method": "CreateDesk",
                 "result": "ok", "details": {"desk_id": "ws_a"}},
            ],
            "next_before_ts": None,
        }
        result = _invoke(["--limit", "5", "--event", "desk.created", "--principal", "ws_a"])
        assert result.exit_code == 0
        called_with = mock_call.call_args.args
        assert called_with[0] == "GetAudit"
        params = called_with[1]
        assert params["limit"] == 5
        assert params["event"] == "desk.created"
        assert params["principal"] == "ws_a"

    @patch("drydock.cli.audit.call_daemon")
    def test_omits_unset_filters_from_params(self, mock_call):
        mock_call.return_value = {"events": [], "next_before_ts": None}
        _invoke(["--limit", "10"])
        params = mock_call.call_args.args[1]
        # Only `limit` should be in params — clean omission of None
        # filters keeps daemon validation simple.
        assert params == {"limit": 10}

    @patch("drydock.cli.audit.call_daemon")
    def test_falls_back_to_direct_read_when_daemon_unavailable(
        self, mock_call, tmp_path, monkeypatch
    ):
        from drydock.cli._wsd_client import DaemonUnavailable
        from drydock.core import audit as audit_module

        mock_call.side_effect = DaemonUnavailable("socket_missing")

        log = tmp_path / "audit.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({
            "ts": "2026-04-16T10:00:00+00:00",
            "event": "desk.created",
            "principal": "ws_a",
            "request_id": None,
            "method": "CreateDesk",
            "result": "ok",
            "details": {"desk_id": "ws_a"},
        }) + "\n")
        monkeypatch.setattr(audit_module, "DEFAULT_LOG_PATH", log)

        result = _invoke(["--limit", "10"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["events"]) == 1
        assert data["events"][0]["event"] == "desk.created"

    @patch("drydock.cli.audit.call_daemon")
    def test_passes_through_pagination_cursor(self, mock_call):
        mock_call.return_value = {
            "events": [],
            "next_before_ts": "2026-04-15T00:00:00+00:00",
        }
        result = _invoke(["--before-ts", "2026-04-16T00:00:00+00:00"])
        params = mock_call.call_args.args[1]
        assert params["before_ts"] == "2026-04-16T00:00:00+00:00"
        assert result.exit_code == 0
