"""Tests for ws status command."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.status import status, _probe_workspace
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _make_ws(name="test-ws", state="running", worktree_path="/tmp/wt"):
    return Workspace(
        name=name,
        project="proj",
        repo_path="/tmp/repo",
        worktree_path=worktree_path,
        branch="ws/test",
        state=state,
        container_id="abc123",
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
    registry.list_workspaces.return_value = []
    result = _invoke(registry)
    assert result.exit_code == 0


@patch("drydock.cli.status._probe_firewall", return_value=True)
@patch("drydock.cli.status._probe_supervisor", return_value=True)
@patch("drydock.cli.status._probe_tailscale", return_value=True)
@patch("drydock.cli.status._docker_container_id", return_value="ctr-abc")
def test_probe_workspace_healthy(mock_docker, mock_ts, mock_sup, mock_fw):
    ws = _make_ws()
    row = _probe_workspace(ws)
    assert row["name"] == "test-ws"
    assert row["state"] == "running"
    assert row["container"] == "running"
    assert row["tailscale"] == "joined"
    assert row["supervisor"] == "alive"
    assert row["firewall"] == "active"


@patch("drydock.cli.status._probe_firewall", return_value=False)
@patch("drydock.cli.status._probe_supervisor", return_value=False)
@patch("drydock.cli.status._probe_tailscale", return_value=False)
@patch("drydock.cli.status._docker_container_id", return_value="ctr-abc")
def test_probe_workspace_unhealthy(mock_docker, mock_ts, mock_sup, mock_fw):
    ws = _make_ws()
    row = _probe_workspace(ws)
    assert row["container"] == "running"
    assert row["tailscale"] == "disconnected"
    assert row["supervisor"] == "dead"
    assert row["firewall"] == "inactive"


@patch("drydock.cli.status._docker_container_id", return_value="")
def test_probe_workspace_no_container(mock_docker):
    ws = _make_ws()
    row = _probe_workspace(ws)
    assert row["container"] == "not found"
    assert row["tailscale"] == "unknown"
    assert row["supervisor"] == "unknown"
    assert row["firewall"] == "unknown"


def test_probe_workspace_no_worktree():
    ws = _make_ws(worktree_path="")
    row = _probe_workspace(ws)
    assert row["container"] == "not found"


@patch("drydock.cli.status._probe_firewall", return_value=True)
@patch("drydock.cli.status._probe_supervisor", return_value=False)
@patch("drydock.cli.status._probe_tailscale", return_value=True)
@patch("drydock.cli.status._docker_container_id", return_value="ctr-abc")
def test_status_multiple_workspaces(mock_docker, mock_ts, mock_sup, mock_fw):
    registry = MagicMock()
    registry.list_workspaces.return_value = [
        _make_ws(name="ws-a"),
        _make_ws(name="ws-b", state="suspended", worktree_path=""),
    ]
    result = _invoke(registry)
    assert result.exit_code == 0
