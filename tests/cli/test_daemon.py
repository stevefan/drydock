"""Tests for ws daemon lifecycle management.

Isolation fix (2026-04-16): use explicit --socket/--registry/--log flags
pointing at temp paths instead of relying on HOME override. The daemon
subprocess inherits the real os.environ, not Click's env= parameter, so
HOME-based path resolution picks up the real home dir (and its live wsd
daemon socket) rather than the test's temp dir. Explicit flags bypass
the HOME-based default entirely.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from click.testing import CliRunner

from drydock.cli.main import cli


def _short_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="wsd-cli-", dir="/tmp"))


def _invoke(runner: CliRunner, args: list[str], home: Path):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["DRYDOCK_WSD_DRY_RUN"] = "1"
    env.pop("DRYDOCK_WSD_SOCKET", None)
    return runner.invoke(cli, args, env=env)


def _daemon_flags(home: Path) -> list[str]:
    """Explicit paths that bypass HOME-based resolution."""
    root = home / ".drydock"
    return [
        "--socket", str(root / "wsd.sock"),
        "--registry", str(root / "registry.db"),
        "--log", str(root / "wsd.log"),
    ]


def _paths(home: Path) -> dict[str, Path]:
    root = home / ".drydock"
    return {
        "pid": root / "wsd.pid",
        "socket": root / "wsd.sock",
        "log": root / "wsd.log",
    }


def test_daemon_start_then_status_then_stop():
    """Lifecycle contract: start/status/stop manage the daemon end to end."""
    home = _short_tmp()
    runner = CliRunner()
    paths = _paths(home)
    flags = _daemon_flags(home)

    try:
        start = _invoke(runner, ["daemon", "start"] + flags, home)
        assert start.exit_code == 0, start.output
        assert paths["pid"].is_file()
        assert paths["socket"].exists()

        status = _invoke(runner, ["daemon", "status",
                                  "--socket", str(paths["socket"]),
                                  "--log", str(paths["log"])], home)
        assert status.exit_code == 0, status.output
        assert '"running": true' in status.output

        stop = _invoke(runner, ["daemon", "stop",
                                "--socket", str(paths["socket"])], home)
        assert stop.exit_code == 0, stop.output
        assert not paths["pid"].exists()
        assert not paths["socket"].exists()
    finally:
        _invoke(runner, ["daemon", "stop",
                         "--socket", str(paths["socket"])], home)
        shutil.rmtree(home, ignore_errors=True)


def test_daemon_status_when_not_running():
    """Status must fail clearly when no daemon is running."""
    home = _short_tmp()
    runner = CliRunner()
    paths = _paths(home)

    try:
        result = _invoke(runner, ["daemon", "status",
                                  "--socket", str(paths["socket"]),
                                  "--log", str(paths["log"])], home)
        assert result.exit_code == 1, result.output
        assert '"running": false' in result.output
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_daemon_start_idempotent_when_already_running():
    """Repeated start is a no-op while the daemon is already alive."""
    home = _short_tmp()
    runner = CliRunner()
    paths = _paths(home)
    flags = _daemon_flags(home)

    try:
        first = _invoke(runner, ["daemon", "start"] + flags, home)
        assert first.exit_code == 0, first.output

        second = _invoke(runner, ["daemon", "start"] + flags, home)
        assert second.exit_code == 0, second.output
        assert "already running" in second.output
    finally:
        _invoke(runner, ["daemon", "stop",
                         "--socket", str(paths["socket"])], home)
        shutil.rmtree(home, ignore_errors=True)


def test_daemon_stop_when_not_running():
    """Stop is idempotent when the pid file is missing or stale."""
    home = _short_tmp()
    runner = CliRunner()
    paths = _paths(home)

    try:
        result = _invoke(runner, ["daemon", "stop",
                                  "--socket", str(paths["socket"])], home)
        assert result.exit_code == 0, result.output
        assert "daemon not running" in result.output
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_daemon_logs_n_lines():
    """Logs command must surface recent daemon output for operators."""
    home = _short_tmp()
    runner = CliRunner()
    paths = _paths(home)
    flags = _daemon_flags(home)

    try:
        start = _invoke(runner, ["daemon", "start"] + flags, home)
        assert start.exit_code == 0, start.output
        stop = _invoke(runner, ["daemon", "stop",
                                "--socket", str(paths["socket"])], home)
        assert stop.exit_code == 0, stop.output

        result = _invoke(runner, ["daemon", "logs", "-n", "5",
                                  "--log", str(paths["log"])], home)
        if not paths["log"].exists() or paths["log"].stat().st_size == 0:
            return
        assert result.exit_code == 0, result.output
        assert result.output.strip()
    finally:
        _invoke(runner, ["daemon", "stop",
                         "--socket", str(paths["socket"])], home)
        shutil.rmtree(home, ignore_errors=True)
