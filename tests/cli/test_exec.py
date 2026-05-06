"""Tests for ws exec command."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.exec import exec_cmd
from drydock.core import WsError
from drydock.core.runtime import Drydock
from drydock.output.formatter import Output


def _make_ws(state="running", overlay_path=""):
    return Drydock(
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


def test_exec_drydock_not_found():
    registry = MagicMock()
    registry.get_drydock.return_value = None
    result = _invoke(registry)
    assert result.exit_code == 1


def test_exec_drydock_not_running():
    registry = MagicMock()
    registry.get_drydock.return_value = _make_ws(state="suspended")
    result = _invoke(registry)
    assert result.exit_code == 1


@patch("drydock.cli.exec._find_container_id", return_value="")
def test_exec_no_container(mock_find):
    registry = MagicMock()
    registry.get_drydock.return_value = _make_ws()
    result = _invoke(registry)
    assert result.exit_code == 1


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_default_bash(mock_find, mock_execvp):
    registry = MagicMock()
    registry.get_drydock.return_value = _make_ws()
    _invoke(registry)
    mock_execvp.assert_called_once_with(
        "docker", ["docker", "exec", "-i", "-w", "/drydock", "ctr-abc", "bash"]
    )


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_custom_command(mock_find, mock_execvp):
    registry = MagicMock()
    registry.get_drydock.return_value = _make_ws()
    runner = CliRunner()
    out = Output(force_json=True)
    runner.invoke(
        exec_cmd,
        ["test-ws", "ls", "-la"],
        obj={"registry": registry, "output": out, "dry_run": False},
    )
    mock_execvp.assert_called_once_with(
        "docker", ["docker", "exec", "-i", "-w", "/drydock", "ctr-abc", "ls", "-la"]
    )


@patch("drydock.cli.exec._stdin_is_tty", return_value=True)
@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_passes_tty_when_stdin_is_terminal(mock_find, mock_execvp, mock_isatty):
    """Regression: hardcoded -it broke non-TTY callers (cron, ssh -T). Now -t
    is added only when stdin is an actual terminal. This test pins the TTY
    case; the other tests in this module exercise the no-TTY case (CliRunner
    provides no real terminal)."""
    registry = MagicMock()
    registry.get_drydock.return_value = _make_ws()
    _invoke(registry)
    mock_execvp.assert_called_once_with(
        "docker", ["docker", "exec", "-it", "-w", "/drydock", "ctr-abc", "bash"]
    )


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_lookup_tries_subdir_then_worktree(mock_find, mock_execvp):
    """ws exec must locate the container whether its
    devcontainer.local_folder label was set to worktree+subdir (fresh
    create with workspace_subdir) or bare worktree (container built
    before workspace_subdir was added via ws project reload). Pass both
    candidates and let _find_container_id pick the first match."""
    registry = MagicMock()
    ws = Drydock(
        name="test-ws",
        project="proj",
        repo_path="/tmp/repo",
        worktree_path="/tmp/wt",
        branch="ws/test",
        state="running",
        container_id="abc",
        workspace_subdir="subproj",
    )
    registry.get_drydock.return_value = ws
    _invoke(registry)
    mock_find.assert_called_once_with("/tmp/wt/subproj", "/tmp/wt")


@patch("drydock.cli.exec.os.execvp")
@patch("drydock.cli.exec._find_container_id", return_value="ctr-abc")
def test_exec_reads_workspace_folder_from_overlay(mock_find, mock_execvp, tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"drydockFolder": "/custom/path"}))

    registry = MagicMock()
    registry.get_drydock.return_value = _make_ws(overlay_path=str(overlay))
    _invoke(registry)
    mock_execvp.assert_called_once_with(
        "docker", ["docker", "exec", "-i", "-w", "/custom/path", "ctr-abc", "bash"]
    )


