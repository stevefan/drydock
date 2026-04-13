"""Tests for DevcontainerCLI — all subprocess calls are mocked."""

import json
import subprocess
from unittest.mock import patch

import pytest

from drydock.core.devcontainer import DevcontainerCLI, _parse_devcontainer_output
from drydock.core import WsError


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_includes_log_format_json(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"containerId": "abc123", "outcome": "success"}),
        stderr="",
    )
    cli = DevcontainerCLI()
    cli.up(workspace_folder="/tmp/ws")

    cmd = mock_run.call_args[0][0]
    assert "--log-format" in cmd
    idx = cmd.index("--log-format")
    assert cmd[idx + 1] == "json"


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_includes_override_config(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"containerId": "abc123"}),
        stderr="",
    )
    cli = DevcontainerCLI()
    cli.up(workspace_folder="/tmp/ws", override_config="/tmp/override.json")

    cmd = mock_run.call_args[0][0]
    assert "--override-config" in cmd
    assert "/tmp/override.json" in cmd


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_extracts_container_id(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"containerId": "deadbeef", "outcome": "success"}),
        stderr="",
    )
    cli = DevcontainerCLI()
    result = cli.up(workspace_folder="/tmp/ws")

    assert result["container_id"] == "deadbeef"
    assert result["containerId"] == "deadbeef"
    assert result["exit_code"] == 0
    assert "warning" not in result


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_returns_full_dict_when_no_container_id(mock_run):
    payload = {"outcome": "success", "remoteUser": "node"}
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )
    cli = DevcontainerCLI()
    result = cli.up(workspace_folder="/tmp/ws")

    assert "container_id" not in result
    assert result["outcome"] == "success"


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_error_no_container_id_raises_wserror(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout="",
        stderr="build failed: Dockerfile syntax error",
    )
    cli = DevcontainerCLI()
    with pytest.raises(WsError, match="devcontainer up failed"):
        cli.up(workspace_folder="/tmp/ws")


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_nonzero_with_container_id_returns_warning(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout=json.dumps({"containerId": "ctr-ok", "outcome": "success"}),
        stderr="postAttachCommand failed",
    )
    cli = DevcontainerCLI()
    result = cli.up(workspace_folder="/tmp/ws")

    assert result["container_id"] == "ctr-ok"
    assert result["exit_code"] == 1
    assert "warning" in result
    assert "postAttachCommand failed" in result["warning"]


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_nonzero_no_container_id_raises(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout=json.dumps({"outcome": "error"}),
        stderr="total failure",
    )
    cli = DevcontainerCLI()
    with pytest.raises(WsError, match="devcontainer up failed"):
        cli.up(workspace_folder="/tmp/ws")


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_zero_exit_with_container_id_no_warning(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"containerId": "ctr-clean", "outcome": "success"}),
        stderr="",
    )
    cli = DevcontainerCLI()
    result = cli.up(workspace_folder="/tmp/ws")

    assert result["container_id"] == "ctr-clean"
    assert result["exit_code"] == 0
    assert "warning" not in result


def test_up_dry_run_returns_command():
    cli = DevcontainerCLI(dry_run=True)
    result = cli.up(workspace_folder="/tmp/ws", override_config="/tmp/o.json")

    assert result["dry_run"] is True
    assert "--log-format" in result["command"]
    assert "--override-config" in result["command"]


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_non_json_stdout(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="not json at all",
        stderr="",
    )
    cli = DevcontainerCLI()
    result = cli.up(workspace_folder="/tmp/ws")

    assert result["stdout"] == "not json at all"
    assert result["exit_code"] == 0


def test_parse_ndjson_extracts_last_object():
    lines = [
        json.dumps({"type": "log", "text": "building..."}),
        json.dumps({"type": "log", "text": "starting..."}),
        json.dumps({"containerId": "abc123", "outcome": "success"}),
    ]
    stdout = "\n".join(lines)
    parsed = _parse_devcontainer_output(stdout)
    assert parsed["containerId"] == "abc123"


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_ndjson_stdout_extracts_container_id(mock_run):
    lines = [
        json.dumps({"type": "log", "text": "building..."}),
        json.dumps({"containerId": "ndjson-ctr", "outcome": "success"}),
    ]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="\n".join(lines),
        stderr="",
    )
    cli = DevcontainerCLI()
    result = cli.up(workspace_folder="/tmp/ws")

    assert result["container_id"] == "ndjson-ctr"


@patch("drydock.core.devcontainer.subprocess.run")
def test_tailnet_logout_calls_docker_exec(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    cli = DevcontainerCLI()
    cli.tailnet_logout("ctr-abc")

    mock_run.assert_called_once_with(
        ["docker", "exec", "ctr-abc", "sudo", "tailscale", "logout"],
        capture_output=True,
        text=True,
        timeout=15,
    )


@patch("drydock.core.devcontainer.subprocess.run")
def test_tailnet_logout_tolerates_failure(mock_run):
    mock_run.side_effect = OSError("container gone")
    cli = DevcontainerCLI()
    cli.tailnet_logout("ctr-gone")


def test_tailnet_logout_skipped_in_dry_run():
    cli = DevcontainerCLI(dry_run=True)
    cli.tailnet_logout("ctr-abc")


@patch("drydock.core.devcontainer.subprocess.run")
def test_up_container_id_in_nested_outcome(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"outcome": {"containerId": "nested-ctr"}}),
        stderr="",
    )
    cli = DevcontainerCLI()
    result = cli.up(workspace_folder="/tmp/ws")

    assert result["container_id"] == "nested-ctr"
