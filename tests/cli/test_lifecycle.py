"""Tests for ephemeral-container lifecycle: stop-removes-container, suspended
resume, running-error-with-fix, --force rebuild, and pre-sweep."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core import WsError
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _init_repo(path, devcontainer=True):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    if devcontainer:
        (path / ".devcontainer").mkdir()
        (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


def _find_last_json_block(lines):
    brace_depth = 0
    block_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        brace_depth += stripped.count("}") - stripped.count("{")
        block_lines.append(line)
        if brace_depth <= 0 and "{" in stripped:
            break
    block_lines.reverse()
    return block_lines


# Regression: container lingers after stop → silently restarted without postStartCommand
@patch("drydock.cli.stop.DevcontainerCLI")
def test_stop_removes_container(MockCLI):
    """Catches regression where stopped container gets silently restarted by devcontainer CLI."""
    ws = Workspace(
        name="test-ws", project="proj", repo_path="/tmp/repo",
        worktree_path="/tmp/wt", branch="ws/test", state="running",
        container_id="abc123",
    )
    registry = MagicMock()
    registry.get_workspace.return_value = ws
    registry.update_state.return_value = ws

    runner = CliRunner()
    from drydock.cli.stop import stop
    result = runner.invoke(
        stop, ["test-ws"],
        obj={"registry": registry, "output": Output(force_json=True), "dry_run": False},
    )

    assert result.exit_code == 0
    mock_devc = MockCLI.return_value
    mock_devc.stop.assert_called_once_with(container_id="abc123")
    mock_devc.remove.assert_called_once_with(container_id="abc123")


# Regression: suspended workspaces couldn't be resumed — WorkspaceExistsError
@patch("drydock.cli.create.DevcontainerCLI")
def test_create_on_suspended_workspace_resumes(MockCLI, tmp_path, monkeypatch):
    """Catches failure where suspended workspaces can't be resumed via ws create."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    # Set up a suspended workspace with an existing checkout
    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    checkout_dir = tmp_path / ".drydock" / "worktrees" / "ws_myws"
    subprocess.run(
        ["git", "clone", str(repo), str(checkout_dir)],
        capture_output=True, text=True, check=True,
    )
    ws = Workspace(
        name="myws", project="proj", repo_path=str(repo),
        worktree_path=str(checkout_dir), branch="ws/myws",
        state="suspended", container_id="old-ctr-dead",
    )
    registry.create_workspace(ws)
    registry.update_state("myws", "suspended")

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "new-ctr-123", "containerId": "new-ctr-123"}

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "myws", "--repo-path", str(repo)],
    )
    assert result.exit_code == 0, result.output

    refreshed = registry.get_workspace("myws")
    assert refreshed.state == "running"
    assert refreshed.container_id == "new-ctr-123"
    registry.close()


# LLM-usability contract: JSON error must include executable fix field
def test_create_on_running_workspace_errors_with_fix(tmp_path, monkeypatch):
    """Catches LLM-usability failure: caller must parse fix from JSON and re-run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    ws = Workspace(
        name="active", project="proj", repo_path="/tmp/repo",
        state="running", container_id="ctr-live",
    )
    registry.create_workspace(ws)
    registry.update_state("active", "running")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "active"],
        obj={"registry": registry, "output": Output(force_json=True), "dry_run": False},
    )

    assert result.exit_code != 0
    err = json.loads(result.output.strip())
    assert err["error"] == "workspace_already_running"
    assert "fix" in err
    assert "--force" in err["fix"]
    registry.close()


# Scariest regression in this lifecycle change: --force silently wipes user data
@patch("drydock.cli.create.DevcontainerCLI")
def test_force_rebuild_preserves_checkout(MockCLI, tmp_path, monkeypatch):
    """Catches --force silently destroying checkout (volumes are the container's
    concern; checkout is what we control and must preserve across rebuild)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    checkout_dir = tmp_path / ".drydock" / "worktrees" / "ws_forcews"
    subprocess.run(
        ["git", "clone", str(repo), str(checkout_dir)],
        capture_output=True, text=True, check=True,
    )
    # Seed a user file in the checkout — this must survive --force
    sentinel = checkout_dir / "user-work.txt"
    sentinel.write_text("precious user data")

    ws = Workspace(
        name="forcews", project="proj", repo_path=str(repo),
        worktree_path=str(checkout_dir), branch="ws/forcews",
        state="running", container_id="old-ctr",
    )
    registry.create_workspace(ws)
    registry.update_state("forcews", "running")

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "new-ctr-456", "containerId": "new-ctr-456"}

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "forcews", "--repo-path", str(repo), "--force"],
    )
    assert result.exit_code == 0, result.output

    assert sentinel.exists(), "User data in checkout was destroyed by --force"
    assert sentinel.read_text() == "precious user data"

    refreshed = registry.get_workspace("forcews")
    assert refreshed.state == "running"
    assert refreshed.container_id == "new-ctr-456"
    registry.close()


# Original Bug 2 root cause: stale stopped container confuses devcontainer CLI
@patch("drydock.cli.create.DevcontainerCLI")
def test_presweep_removes_stale_containers(MockCLI, tmp_path, monkeypatch):
    """Catches the bug where a stale stopped container with matching
    devcontainer.local_folder label gets silently restarted without postStartCommand."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "fresh-ctr", "containerId": "fresh-ctr"}
    mock_devc.remove_stale_containers.return_value = ["stale-ctr-999"]

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "sweeptest", "--repo-path", str(repo)],
    )
    assert result.exit_code == 0, result.output

    mock_devc.remove_stale_containers.assert_called_once()
    call_args = mock_devc.remove_stale_containers.call_args
    workspace_folder = call_args[0][0] if call_args[0] else call_args[1].get("workspace_folder")
    assert workspace_folder is not None
