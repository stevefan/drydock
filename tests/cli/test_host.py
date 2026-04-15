"""Tests for ws host (init + check)."""

import os
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.main import cli


def test_host_init_creates_state_dirs(tmp_path, monkeypatch):
    """Idempotent setup: missing state dirs are created with the right modes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["host", "init"])
    assert result.exit_code == 0, result.output

    for name, expected_mode in [
        ("projects", 0o755),
        ("secrets", 0o700),
        ("worktrees", 0o755),
        ("overlays", 0o755),
        ("daemon-secrets", 0o700),
        ("logs", 0o755),
    ]:
        d = tmp_path / ".drydock" / name
        assert d.is_dir(), f"missing {d}"
        assert d.stat().st_mode & 0o777 == expected_mode, f"wrong mode for {name}"

    # gitconfig stub touched
    assert (tmp_path / ".gitconfig").is_file()


def test_host_init_is_idempotent(tmp_path, monkeypatch):
    """Second invocation is a no-op (no actions reported)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(cli, ["host", "init"])  # first run creates everything
    result = runner.invoke(cli, ["--json", "host", "init"])
    assert result.exit_code == 0, result.output
    # Second-run JSON should report noop=true
    import json
    data = json.loads(result.output)
    assert data["noop"] is True, f"expected noop on second run, got {data}"


def test_host_init_repairs_wrong_mode(tmp_path, monkeypatch):
    """If a state dir exists with wrong mode, init chmods it back."""
    monkeypatch.setenv("HOME", str(tmp_path))
    secrets = tmp_path / ".drydock" / "secrets"
    secrets.mkdir(parents=True)
    os.chmod(secrets, 0o755)  # wrong — should be 0o700

    runner = CliRunner()
    result = runner.invoke(cli, ["host", "init"])
    assert result.exit_code == 0, result.output
    assert secrets.stat().st_mode & 0o777 == 0o700


def test_host_check_fails_when_docker_missing(tmp_path, monkeypatch):
    """Required check failure → exit 1. Docker absence is the canonical fail."""
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    # Patch _check_docker to return fail; leaves the rest realistic.
    with patch("drydock.cli.host._check_docker", return_value=("fail", "docker not installed")):
        with patch("drydock.cli.host._check_devcontainer", return_value=("ok", "0.86.0")):
            result = runner.invoke(cli, ["--json", "host", "check"])
    assert result.exit_code == 1, result.output
    import json
    data = json.loads(result.output)
    assert data["passed"] is False
    assert data["summary"]["fail"] >= 1
    docker_check = next(c for c in data["checks"] if c["check"] == "docker")
    assert docker_check["status"] == "fail"


def test_host_check_passes_with_warnings(tmp_path, monkeypatch):
    """Warnings (missing tailscale, gh) do not fail the check; exit 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Initialize state dirs so those checks pass
    runner = CliRunner()
    runner.invoke(cli, ["host", "init"])

    with patch("drydock.cli.host._check_docker", return_value=("ok", "server 27.0")):
        with patch("drydock.cli.host._check_devcontainer", return_value=("ok", "0.86.0")):
            with patch("drydock.cli.host._check_tailscale", return_value=("warn", "not installed")):
                with patch("drydock.cli.host._check_gh_auth", return_value=("warn", "not installed")):
                    result = runner.invoke(cli, ["--json", "host", "check"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert data["passed"] is True
    assert data["summary"]["warn"] >= 2
    assert data["summary"]["fail"] == 0
