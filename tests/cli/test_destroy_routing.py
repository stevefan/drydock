"""Daemon-routing tests for `ws destroy`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _init_repo(path: Path, *, with_devcontainer: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "T")
    (path / "README.md").write_text("init")
    if with_devcontainer:
        (path / ".devcontainer").mkdir()
        (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")


def test_destroy_routes_through_daemon(wsd, monkeypatch):
    monkeypatch.setenv("HOME", str(wsd.home))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(wsd.socket_path))

    repo = wsd.home / "repo-destroy-route"
    _init_repo(repo)
    created = wsd.call_rpc(
        "CreateDesk",
        params={"project": "proj", "name": "desk-destroy-route", "repo_path": str(repo)},
        request_id="create-destroy-route",
    )["result"]

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "destroy", "desk-destroy-route", "--force"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"destroyed": "desk-destroy-route"}

    registry = Registry(db_path=wsd.registry_path)
    try:
        workspace = registry.get_workspace("desk-destroy-route")
        token = registry.get_token_info(created["desk_id"])
        destroy_tasks = registry._conn.execute(
            "SELECT outcome_json FROM task_log WHERE method = 'DestroyDesk'"
        ).fetchall()
    finally:
        registry.close()

    assert workspace is None
    assert token is None
    assert destroy_tasks
    destroy_result = json.loads(destroy_tasks[-1]["outcome_json"])
    assert destroy_result["cascaded"] == []


@patch("drydock.cli.destroy.log_event")
@patch("drydock.cli.destroy.DevcontainerCLI")
@patch("drydock.cli.destroy.tailnet_api")
def test_destroy_falls_back_to_direct_when_daemon_unreachable(mock_tailnet, MockCLI, _mock_log_event, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(tmp_path / "missing.sock"))

    repo = tmp_path / "repo"
    _init_repo(repo, with_devcontainer=False)

    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    worktree = tmp_path / ".drydock" / "worktrees" / "desk-fallback-destroy"
    worktree.mkdir(parents=True)
    overlay = tmp_path / ".drydock" / "overlays" / "desk-fallback-destroy.json"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("{}")
    registry.create_workspace(
        Workspace(
            name="desk-fallback-destroy",
            project="proj",
            repo_path=str(repo),
            worktree_path=str(worktree),
            branch="ws/desk-fallback-destroy",
            state="running",
            container_id="ctr-destroy-fallback",
            config={"overlay_path": str(overlay)},
        )
    )
    registry.close()
    mock_tailnet.load_admin_credentials.return_value = None

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "destroy", "desk-fallback-destroy", "--force"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"destroyed": "desk-fallback-destroy"}
    MockCLI.return_value.stop.assert_called_once_with(container_id="ctr-destroy-fallback")


def test_destroy_propagates_daemon_rpc_error(wsd, monkeypatch):
    monkeypatch.setenv("HOME", str(wsd.home))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(wsd.socket_path))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "destroy", "missing-destroy-route", "--force"],
    )

    assert result.exit_code != 0
    error = json.loads(result.output)
    assert error["error"] == "desk_not_found"

    registry = Registry(db_path=wsd.registry_path)
    try:
        destroy_tasks = registry._conn.execute(
            "SELECT COUNT(*) AS n FROM task_log WHERE method = 'DestroyDesk'"
        ).fetchone()
    finally:
        registry.close()

    assert destroy_tasks["n"] == 1
