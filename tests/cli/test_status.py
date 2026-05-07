"""Tests for ws status command."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.status import (
    _probe_base_image,
    _probe_compliance,
    _probe_ipset,
    _probe_refresh_supervisor,
    _probe_trust_accepted,
    _probe_drydock,
    status,
)
from drydock.core.runtime import Drydock
from drydock.output.formatter import Output


def _make_ws(name="test-ws", state="running", worktree_path="/tmp/wt", overlay_path=""):
    return Drydock(
        name=name,
        project="proj",
        repo_path="/tmp/repo",
        worktree_path=worktree_path,
        branch="ws/test",
        state=state,
        container_id="abc123",
        config={"overlay_path": overlay_path} if overlay_path else {},
    )


def _invoke(registry):
    runner = CliRunner()
    out = Output(force_json=True)
    return runner.invoke(
        status,
        [],
        obj={"registry": registry, "output": out, "dry_run": False},
    )


def test_status_empty_registry():
    registry = MagicMock()
    registry.list_drydocks.return_value = []
    result = _invoke(registry)
    assert result.exit_code == 0


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_refresh_supervisor_alive_when_pgrep_finds_process(MockCLI):
    ws = _make_ws()
    devc = MockCLI.return_value
    devc.exec_command.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="123\n", stderr=""
    )

    assert _probe_refresh_supervisor(ws, "active", devc) == "alive"


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_refresh_supervisor_dead_when_firewall_active_but_pgrep_empty(MockCLI):
    ws = _make_ws()
    devc = MockCLI.return_value
    devc.exec_command.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=""
    )

    assert _probe_refresh_supervisor(ws, "active", devc) == "dead"


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_refresh_supervisor_not_applicable_when_firewall_inactive(MockCLI):
    ws = _make_ws()
    devc = MockCLI.return_value

    assert _probe_refresh_supervisor(ws, "inactive", devc) == "not_applicable"
    devc.exec_command.assert_not_called()


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_ipset_returns_size_and_max_when_present(MockCLI):
    ws = _make_ws()
    devc = MockCLI.return_value
    devc.exec_command.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Name: allowed-domains\nType: hash:ip\nHeader: family inet hashsize 1024 maxelem 65536\nSize in memory: 16584\nReferences: 1\nNumber of entries: 17\n",
        stderr="",
    )

    assert _probe_ipset(ws, devc) == {"size": 17, "max": 65536}


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_ipset_null_when_absent(MockCLI):
    ws = _make_ws()
    devc = MockCLI.return_value
    devc.exec_command.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="ipset v7.19: The set with the given name does not exist"
    )

    assert _probe_ipset(ws, devc) is None


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_trust_accepted_true_when_claude_json_has_entry(MockCLI, tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"workspaceFolder": "/drydock"}))
    ws = _make_ws(overlay_path=str(overlay))
    devc = MockCLI.return_value
    devc.exec_command.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"trustedWorkspaces": {"/drydock": {"trusted": True}}}),
        stderr="",
    )

    assert _probe_trust_accepted(ws, devc) is True


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_trust_accepted_false_when_claude_json_missing_entry(MockCLI, tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"workspaceFolder": "/drydock"}))
    ws = _make_ws(overlay_path=str(overlay))
    devc = MockCLI.return_value
    devc.exec_command.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"trustedWorkspaces": {"/other": {"trusted": True}}}),
        stderr="",
    )

    assert _probe_trust_accepted(ws, devc) is False


@patch("drydock.cli.status.DevcontainerCLI")
def test_status_trust_accepted_null_when_claude_json_absent(MockCLI):
    ws = _make_ws()
    devc = MockCLI.return_value
    devc.exec_command.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="cat: /home/node/.claude/.claude.json: No such file or directory"
    )

    assert _probe_trust_accepted(ws, devc) is None


@patch("drydock.cli.status._docker_inspect_value")
def test_status_base_image_from_container_inspect(mock_inspect):
    ws = _make_ws()
    mock_inspect.side_effect = ["", "ghcr.io/stevefan/drydock-base:v1.0.7"]

    assert _probe_base_image(ws, "ctr-abc") == "ghcr.io/stevefan/drydock-base:v1.0.7"


def test_status_base_image_null_when_container_absent():
    ws = _make_ws()
    assert _probe_base_image(ws, "") is None


@patch("drydock.cli.status._docker_container_id", return_value="")
def test_status_gracefully_handles_container_not_running(mock_docker):
    ws = _make_ws(state="suspended")
    row = _probe_drydock(ws)

    assert row["state"] == "suspended"
    assert row["container"] == "not found"
    assert row["tailscale"] == "unknown"
    assert row["supervisor"] == "unknown"
    assert row["firewall"] == "unknown"
    assert row["refresh_supervisor"] == "not_applicable"
    assert row["ipset"] is None
    assert row["trust_accepted"] is None
    assert row["base_image"] is None


@patch("drydock.cli.status._probe_base_image", return_value="ghcr.io/stevefan/drydock-base:v1.0.7")
@patch("drydock.cli.status._probe_trust_accepted", return_value=True)
@patch("drydock.cli.status._probe_ipset", return_value={"size": 12, "max": 65536})
@patch("drydock.cli.status._probe_refresh_supervisor", return_value="alive")
@patch("drydock.cli.status._probe_firewall", return_value=True)
@patch("drydock.cli.status._probe_supervisor", return_value=True)
@patch("drydock.cli.status._probe_tailscale", return_value=True)
@patch("drydock.cli.status._docker_container_id", return_value="ctr-abc")
def test_status_preserves_existing_fields(
    mock_docker,
    mock_ts,
    mock_sup,
    mock_fw,
    mock_refresh,
    mock_ipset,
    mock_trust,
    mock_base,
):
    ws = _make_ws()
    row = _probe_drydock(ws)

    assert row["name"] == "test-ws"
    assert row["state"] == "running"
    assert row["container"] == "running"
    assert row["tailscale"] == "joined"
    assert row["supervisor"] == "alive"
    assert row["firewall"] == "active"
    assert row["refresh_supervisor"] == "alive"
    assert row["ipset"] == {"size": 12, "max": 65536}
    assert row["trust_accepted"] is True
    assert row["base_image"] == "ghcr.io/stevefan/drydock-base:v1.0.7"


@patch("drydock.cli.status._probe_base_image", return_value="ghcr.io/stevefan/drydock-base:v1.0.7")
@patch("drydock.cli.status._probe_trust_accepted", return_value=False)
@patch("drydock.cli.status._probe_ipset", return_value={"size": 3, "max": 32})
@patch("drydock.cli.status._probe_refresh_supervisor", return_value="dead")
@patch("drydock.cli.status._probe_firewall", return_value=False)
@patch("drydock.cli.status._probe_supervisor", return_value=False)
@patch("drydock.cli.status._probe_tailscale", return_value=True)
@patch("drydock.cli.status._docker_container_id", return_value="ctr-abc")
def test_status_multiple_drydocks(
    mock_docker,
    mock_ts,
    mock_sup,
    mock_fw,
    mock_refresh,
    mock_ipset,
    mock_trust,
    mock_base,
):
    registry = MagicMock()
    registry.list_drydocks.return_value = [
        _make_ws(name="ws-a"),
        _make_ws(name="ws-b", state="suspended", worktree_path=""),
    ]
    result = _invoke(registry)
    assert result.exit_code == 0


def test_probe_compliance_returns_none_when_file_missing(tmp_path):
    ws = _make_ws(worktree_path=str(tmp_path))
    assert _probe_compliance(ws) is None


def test_probe_compliance_flags_overdue_review(tmp_path):
    (tmp_path / "compliance.yaml").write_text(
        "last_reviewed: 2020-01-01\n"
        "review_cadence_days: 30\n"
    )
    ws = _make_ws(worktree_path=str(tmp_path))
    result = _probe_compliance(ws)
    assert result is not None
    assert result.startswith("stale (")
    assert "days overdue" in result
