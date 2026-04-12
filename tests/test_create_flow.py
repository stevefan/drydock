"""Tests for the create command's devcontainer lifecycle and state transitions."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.main import cli


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


@patch("drydock.cli.create.DevcontainerCLI")
def test_create_success_transitions_to_running(MockCLI, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "ctr-abc", "containerId": "ctr-abc"}

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "ws-ok", "--repo-path", str(repo)]
    )
    assert result.exit_code == 0, result.output

    lines = [l for l in result.output.strip().split("\n") if l.strip()]
    last_json = json.loads("\n".join(_find_last_json_block(lines)))

    assert last_json["state"] == "running"
    assert last_json["container_id"] == "ctr-abc"

    mock_devc.check_available.assert_called_once()
    mock_devc.up.assert_called_once()
    call_kwargs = mock_devc.up.call_args
    assert "--override-config" not in str(call_kwargs) or call_kwargs[1].get("override_config")


@patch("drydock.cli.create.DevcontainerCLI")
def test_create_failure_transitions_to_error(MockCLI, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    from drydock.core.errors import WsError
    mock_devc = MockCLI.return_value
    mock_devc.up.side_effect = WsError("devcontainer up failed: boom")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "ws-fail", "--repo-path", str(repo)]
    )

    assert result.exit_code != 0

    from drydock.core.registry import Registry
    reg = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    ws = reg.get_workspace("ws-fail")
    assert ws is not None
    assert ws.state == "error"
    reg.close()


@patch("drydock.cli.create.DevcontainerCLI")
def test_create_sets_provisioning_before_up(MockCLI, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    states_seen = []

    from drydock.core.registry import Registry
    orig_update_state = Registry.update_state

    def spy_update_state(self, name, state):
        states_seen.append(state)
        return orig_update_state(self, name, state)

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "ctr-xyz", "containerId": "ctr-xyz"}

    with patch.object(Registry, "update_state", spy_update_state):
        runner = CliRunner()
        runner.invoke(
            cli, ["--json", "create", "proj", "ws-prov", "--repo-path", str(repo)]
        )

    assert "provisioning" in states_seen


@patch("drydock.cli.create.DevcontainerCLI")
def test_workspace_subdir_threads_to_workspace_folder(MockCLI, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    (projects_dir / "mono.yaml").write_text(
        f"repo_path: {repo}\nworkspace_subdir: apps/frontend\n"
    )

    from drydock.core.project_config import load_project_config as real_load
    monkeypatch.setattr(
        "drydock.cli.create.load_project_config",
        lambda project: real_load(project, base_dir=projects_dir),
    )

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "ctr-sub", "containerId": "ctr-sub"}

    from drydock.core.worktree import create_worktree as real_create_worktree

    def patched_create_worktree(ws, base_dir=None):
        result = real_create_worktree(ws, base_dir=base_dir)
        devc_dir = result / "apps" / "frontend" / ".devcontainer"
        devc_dir.mkdir(parents=True, exist_ok=True)
        (devc_dir / "devcontainer.json").write_text("{}")
        return result

    monkeypatch.setattr("drydock.cli.create.create_worktree", patched_create_worktree)

    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "create", "mono"])
    assert result.exit_code == 0, result.output

    mock_devc.up.assert_called_once()
    call_kwargs = mock_devc.up.call_args
    workspace_folder = call_kwargs[1].get("workspace_folder") or call_kwargs[0][0]
    assert workspace_folder.endswith("apps/frontend")


@patch("drydock.cli.create.DevcontainerCLI")
def test_preflight_raises_when_devcontainer_json_missing(MockCLI, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo, devcontainer=False)

    mock_devc = MockCLI.return_value

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "ws-ndc", "--repo-path", str(repo)]
    )
    assert result.exit_code != 0

    from drydock.core.registry import Registry
    reg = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    ws = reg.get_workspace("ws-ndc")
    assert ws is not None
    assert ws.state == "error"
    reg.close()


@patch("drydock.cli.create.DevcontainerCLI")
def test_preflight_passes_when_devcontainer_json_exists(MockCLI, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "ctr-ok", "containerId": "ctr-ok"}

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "ws-ok2", "--repo-path", str(repo)]
    )
    assert result.exit_code == 0, result.output

    lines = [l for l in result.output.strip().split("\n") if l.strip()]
    last_json = json.loads("\n".join(_find_last_json_block(lines)))
    assert last_json["state"] == "running"


def _find_last_json_block(lines):
    """Find the last JSON object in output (may have multiple JSON outputs)."""
    brace_depth = 0
    block_lines = []
    in_block = False

    for line in reversed(lines):
        stripped = line.strip()
        brace_depth += stripped.count("}") - stripped.count("{")
        block_lines.append(line)
        if brace_depth <= 0 and "{" in stripped:
            break

    block_lines.reverse()
    return block_lines
