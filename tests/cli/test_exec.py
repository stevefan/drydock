"""Tests for ws exec command."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.exec import exec_cmd
from drydock.core import WsError
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _make_ws(state="running", overlay_path=""):
    return Workspace(
        name="test-ws",
        project="proj",
        repo_path="/tmp/repo",
        worktree_path="/tmp/wt",
        branch="ws/test",
        state=state,
        container_id="abc123",
        config={"overlay_path": overlay_path} if overlay_path else {},
    )


def _invoke(registry, args=None):
    runner = CliRunner()
    out = Output(force_json=True)
    return runner.invoke(
        exec_cmd,
        args or ["test-ws"],
        obj={"registry": registry, "output": out, "dry_run": False},
    )


def test_exec_workspace_not_found():
    registry = MagicMock()
    registry.get_workspace.return_value = None
    result = _invoke(registry)
    assert result.exit_code == 1


def test_exec_workspace_not_running():
    registry = MagicMock()
    registry.get_workspace.return_value = _make_ws(state="suspended")
    result = _invoke(registry)
    assert result.exit_code == 1


@patch("drydock.cli.exec._find_container_id", return_value="")
def test_exec_no_container(mock_find):
    registry = MagicMock()
    registry.get_workspace.return_value = _make_ws()
    result = _invoke(registry)
    assert result.exit_code == 1


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_default_bash(mock_find, mock_execvp):
    registry = MagicMock()
    registry.get_workspace.return_value = _make_ws()
    _invoke(registry)
    mock_execvp.assert_called_once_with(
        "docker", ["docker", "exec", "-it", "-w", "/workspace", "ctr-abc", "bash"]
    )


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_custom_command(mock_find, mock_execvp):
    registry = MagicMock()
    registry.get_workspace.return_value = _make_ws()
    runner = CliRunner()
    out = Output(force_json=True)
    runner.invoke(
        exec_cmd,
        ["test-ws", "ls", "-la"],
        obj={"registry": registry, "output": out, "dry_run": False},
    )
    mock_execvp.assert_called_once_with(
        "docker", ["docker", "exec", "-it", "-w", "/workspace", "ctr-abc", "ls", "-la"]
    )


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_reads_workspace_folder_from_overlay(mock_find, mock_execvp, tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"workspaceFolder": "/custom/path"}))

    registry = MagicMock()
    registry.get_workspace.return_value = _make_ws(overlay_path=str(overlay))
    _invoke(registry)
    mock_execvp.assert_called_once_with(
        "docker", ["docker", "exec", "-it", "-w", "/custom/path", "ctr-abc", "bash"]
    )


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_defaults_workspace_when_no_overlay(mock_find, mock_execvp):
    registry = MagicMock()
    registry.get_workspace.return_value = _make_ws()
    _invoke(registry)
    args = mock_execvp.call_args[0][1]
    assert "-w" in args
    idx = args.index("-w")
    assert args[idx + 1] == "/workspace"
