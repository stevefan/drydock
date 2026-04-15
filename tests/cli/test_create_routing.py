"""Daemon-routing tests for `ws create`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core.registry import Registry

def _init_repo(path: Path, *, with_devcontainer: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    if with_devcontainer:
        (path / ".devcontainer").mkdir()
        (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


def _find_last_json_block(lines: list[str]) -> list[str]:
    brace_depth = 0
    block_lines: list[str] = []

    for line in reversed(lines):
        stripped = line.strip()
        brace_depth += stripped.count("}") - stripped.count("{")
        block_lines.append(line)
        if brace_depth <= 0 and "{" in stripped:
            break

    block_lines.reverse()
    return block_lines


def test_create_routes_through_daemon_when_socket_present(wsd, monkeypatch):
    monkeypatch.setenv("HOME", str(wsd.home))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(wsd.socket_path))

    repo = wsd.home / "repo-route"
    _init_repo(repo)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "create", "proj", "desk-route", "--repo-path", str(repo)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["desk_id"] == "ws_desk_route"
    assert payload["state"] == "running"

    registry = Registry(db_path=wsd.registry_path)
    try:
        workspace = registry.get_workspace("desk-route")
        token = registry.get_token_info("ws_desk_route")
    finally:
        registry.close()

    assert workspace is not None
    assert workspace.container_id.startswith("dry-run-")
    assert token is not None


@patch("drydock.cli.create.log_event")
@patch("drydock.cli.create.DevcontainerCLI")
def test_create_falls_back_to_direct_when_daemon_unreachable(MockCLI, _mock_log_event, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(tmp_path / "missing.sock"))

    repo = tmp_path / "repo"
    _init_repo(repo)

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "ctr-fallback", "containerId": "ctr-fallback"}

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "create", "proj", "desk-fallback", "--repo-path", str(repo)],
    )

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    payload = json.loads("\n".join(_find_last_json_block(lines)))
    assert payload["container_id"] == "ctr-fallback"
    assert payload["state"] == "running"


def test_create_propagates_daemon_rpc_error_without_fallback(wsd, monkeypatch):
    monkeypatch.setenv("HOME", str(wsd.home))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(wsd.socket_path))

    repo = wsd.home / "repo-rpc-error"
    _init_repo(repo)

    runner = CliRunner()
    first = runner.invoke(
        cli,
        ["--json", "create", "proj", "desk-rpc-error", "--repo-path", str(repo)],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        cli,
        ["--json", "create", "proj", "desk-rpc-error", "--repo-path", str(repo)],
    )
    assert second.exit_code != 0

    error = json.loads(second.output)
    assert error["error"] == "workspace_already_running"

    registry = Registry(db_path=wsd.registry_path)
    try:
        task_rows = registry._conn.execute(
            "SELECT method, status FROM task_log WHERE method = 'CreateDesk'"
        ).fetchall()
    finally:
        registry.close()

    assert len(task_rows) == 2
    assert {row["status"] for row in task_rows} == {"completed", "failed"}


@patch("drydock.cli.create.call_daemon")
def test_create_only_sends_explicit_devcontainer_subpath_to_daemon(mock_call_daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(tmp_path / "wsd.sock"))

    repo = tmp_path / "repo-subpath-route"
    _init_repo(repo)

    mock_call_daemon.return_value = {
        "desk_id": "ws_desk_subpath_route",
        "name": "desk-subpath-route",
        "project": "proj",
        "branch": "ws/desk-subpath-route",
        "state": "running",
        "container_id": "dry-run-1234",
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "create",
            "proj",
            "desk-subpath-route",
            "--repo-path",
            str(repo),
            "--devcontainer-subpath",
            ".devcontainer/drydock",
        ],
    )

    assert result.exit_code == 0, result.output
    assert mock_call_daemon.call_count == 1
    assert mock_call_daemon.call_args.args[0] == "CreateDesk"
    assert mock_call_daemon.call_args.args[1]["devcontainer_subpath"] == ".devcontainer/drydock"


@patch("drydock.cli.create.call_daemon")
def test_create_omits_default_devcontainer_subpath_from_daemon_request(mock_call_daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DRYDOCK_WSD_SOCKET", str(tmp_path / "wsd.sock"))

    repo = tmp_path / "repo-default-route"
    _init_repo(repo)

    mock_call_daemon.return_value = {
        "desk_id": "ws_desk_default_route",
        "name": "desk-default-route",
        "project": "proj",
        "branch": "ws/desk-default-route",
        "state": "running",
        "container_id": "dry-run-5678",
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "create", "proj", "desk-default-route", "--repo-path", str(repo)],
    )

    assert result.exit_code == 0, result.output
    assert mock_call_daemon.call_count == 1
    assert "devcontainer_subpath" not in mock_call_daemon.call_args.args[1]
