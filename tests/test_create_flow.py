"""Tests for the create command's devcontainer lifecycle and state transitions."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.main import cli


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
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
