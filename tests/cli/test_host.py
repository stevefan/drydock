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


class TestEnsureDrydockSymlink:
    """Unit tests for the symlink helper. The full Linux+root CLI path
    is exercised via the smoke harness on Hetzner — these tests pin the
    branching logic in isolation."""

    def test_creates_symlink_when_target_missing(self, tmp_path):
        from drydock.cli.host import _ensure_drydock_symlink
        # Seed a fake pipx-installed drydock binary
        pipx_bin = tmp_path / ".local" / "bin"
        pipx_bin.mkdir(parents=True)
        source = pipx_bin / "drydock"
        source.write_text("#!/bin/sh\n")
        source.chmod(0o755)

        target = tmp_path / "system-bin" / "drydock"
        with patch("drydock.cli.host.shutil.which", return_value=None):
            action = _ensure_drydock_symlink(target=target, home=tmp_path)
        assert target.is_symlink()
        assert os.readlink(target) == str(source)
        assert action is not None
        assert "symlinked" in action

    def test_idempotent_when_symlink_already_correct(self, tmp_path):
        from drydock.cli.host import _ensure_drydock_symlink
        pipx_bin = tmp_path / ".local" / "bin"
        pipx_bin.mkdir(parents=True)
        source = pipx_bin / "drydock"
        source.write_text("#!/bin/sh\n")
        source.chmod(0o755)

        target = tmp_path / "system-bin" / "drydock"
        target.parent.mkdir()
        target.symlink_to(source)

        with patch("drydock.cli.host.shutil.which", return_value=None):
            action = _ensure_drydock_symlink(target=target, home=tmp_path)
        assert action is None

    def test_replaces_stale_symlink(self, tmp_path):
        from drydock.cli.host import _ensure_drydock_symlink
        pipx_bin = tmp_path / ".local" / "bin"
        pipx_bin.mkdir(parents=True)
        source = pipx_bin / "drydock"
        source.write_text("#!/bin/sh\n"); source.chmod(0o755)

        # Symlink already exists but points somewhere stale.
        target = tmp_path / "system-bin" / "drydock"
        target.parent.mkdir()
        stale = tmp_path / "stale" / "drydock"
        stale.parent.mkdir()
        stale.write_text("old"); stale.chmod(0o755)
        target.symlink_to(stale)

        with patch("drydock.cli.host.shutil.which", return_value=None):
            action = _ensure_drydock_symlink(target=target, home=tmp_path)
        assert os.readlink(target) == str(source)
        assert action is not None

    def test_refuses_to_clobber_regular_file(self, tmp_path):
        """Operator-installed binary at /usr/local/bin/drydock is
        respected; we don't overwrite it."""
        from drydock.cli.host import _ensure_drydock_symlink
        pipx_bin = tmp_path / ".local" / "bin"
        pipx_bin.mkdir(parents=True)
        source = pipx_bin / "drydock"
        source.write_text("#!/bin/sh\n"); source.chmod(0o755)

        target = tmp_path / "system-bin" / "drydock"
        target.parent.mkdir()
        target.write_text("operator's own binary")

        with patch("drydock.cli.host.shutil.which", return_value=None):
            action = _ensure_drydock_symlink(target=target, home=tmp_path)
        assert action is None
        # Original contents preserved
        assert target.read_text() == "operator's own binary"

    def test_returns_none_when_no_drydock_installed(self, tmp_path):
        """Bootstrap-script case: pipx hasn't installed yet."""
        from drydock.cli.host import _ensure_drydock_symlink
        target = tmp_path / "system-bin" / "drydock"
        with patch("drydock.cli.host.shutil.which", return_value=None):
            action = _ensure_drydock_symlink(target=target, home=tmp_path)
        assert action is None
        assert not target.exists()

    def test_uses_pipx_share_path_as_fallback(self, tmp_path):
        """Some pipx configs install only at ~/.local/share/pipx/venvs/...
        without the ~/.local/bin shortcut."""
        from drydock.cli.host import _ensure_drydock_symlink
        pipx_share = tmp_path / ".local" / "share" / "pipx" / "venvs" / "drydock" / "bin"
        pipx_share.mkdir(parents=True)
        source = pipx_share / "drydock"
        source.write_text("#!/bin/sh\n"); source.chmod(0o755)

        target = tmp_path / "system-bin" / "drydock"
        with patch("drydock.cli.host.shutil.which", return_value=None):
            action = _ensure_drydock_symlink(target=target, home=tmp_path)
        assert target.is_symlink()
        assert os.readlink(target) == str(source)


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
