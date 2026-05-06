"""drydock daemon — manage the local drydock.daemon process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click
from drydock.cli._daemon_client import DaemonRpcError, DaemonUnavailable, call_daemon


STARTUP_TIMEOUT_SECONDS = 5.0
HEALTH_TIMEOUT_SECONDS = 2.0
STOP_TIMEOUT_SECONDS = 5.0


def _state_root() -> Path:
    return Path.home() / ".drydock"


def _socket_path(value: str | Path | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    return Path(os.environ.get("DRYDOCK_DAEMON_SOCKET", "~/.drydock/run/daemon.sock")).expanduser()


def _registry_path(value: str | Path | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    return Path(os.environ.get("DRYDOCK_DAEMON_REGISTRY", "~/.drydock/registry.db")).expanduser()


def _log_path(value: str | Path | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    return Path(os.environ.get("DRYDOCK_DAEMON_LOG", "~/.drydock/daemon.log")).expanduser()


def _pid_path() -> Path:
    return _state_root() / "daemon.pid"


def _read_pid(pid_path: Path) -> int | None:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _daemon_command(socket_path: Path, registry_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "drydock.daemon",
        "--socket",
        str(socket_path),
        "--registry",
        str(registry_path),
    ]


def _wait_for_socket(socket_path: Path, proc: subprocess.Popen[bytes], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return socket_path.exists()


def _last_lines(path: Path, count: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    if count <= 0:
        return []
    return lines[-count:]


def _emit_start_failure(log_path: Path) -> None:
    lines = _last_lines(log_path, 20)
    if not lines:
        click.echo(f"(no log output at {log_path})", err=True)
    else:
        click.echo(f"startup failed; last 20 log lines from {log_path}:", err=True)
        for line in lines:
            click.echo(line, err=True)
    raise SystemExit(1)


def _health_call(socket_path: Path, timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    try:
        result = call_daemon(
            "daemon.health",
            {},
            socket_path=socket_path,
            request_id="daemon-status",
            timeout=timeout,
        )
    except (DaemonUnavailable, DaemonRpcError, OSError, TimeoutError):
        return False
    return result.get("ok") is True


def _daemon_status(socket_path: Path, log_path: Path) -> dict[str, object]:
    pid_path = _pid_path()
    pid = _read_pid(pid_path)
    proc_alive = _process_alive(pid)
    if not proc_alive and pid_path.exists():
        _remove_file(pid_path)
        pid = None

    socket_present = socket_path.exists()
    health_responsive = socket_present and _health_call(socket_path)
    # A daemon launched by systemd/launchd has no PID file under ~/.drydock.
    # Treat "serving on the socket" as running regardless of PID-file mode.
    running = proc_alive or health_responsive
    last_log_line = None
    lines = _last_lines(log_path, 1)
    if lines:
        last_log_line = lines[-1]

    return {
        "running": running,
        "pid": pid if proc_alive else None,
        "socket_present": socket_present,
        "socket_path": str(socket_path),
        "health_responsive": health_responsive,
        "log_path": str(log_path),
        "last_log_line": last_log_line,
    }


def _wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return not _process_alive(pid)


def _wait_for_socket_removal(socket_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not socket_path.exists():
            return
        time.sleep(0.05)


@click.group()
def daemon():
    """Manage the local drydock daemon."""


@daemon.command()
@click.option("--socket", "socket_override", type=click.Path(path_type=Path))
@click.option("--registry", "registry_override", type=click.Path(path_type=Path))
@click.option("--log", "log_override", type=click.Path(path_type=Path))
@click.option("--foreground", is_flag=True, help="Run in the foreground")
def start(socket_override: Path | None, registry_override: Path | None, log_override: Path | None, foreground: bool):
    """Start the daemon."""
    pid_path = _pid_path()
    pid = _read_pid(pid_path)
    if _process_alive(pid):
        click.echo(f"daemon already running (pid={pid})")
        return
    if pid_path.exists():
        _remove_file(pid_path)

    socket_path = _socket_path(socket_override)
    registry_path = _registry_path(registry_override)
    log_path = _log_path(log_override)
    command = _daemon_command(socket_path, registry_path)

    if foreground:
        os.execvpe(sys.executable, command, os.environ.copy())

    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        _remove_file(socket_path)

    with log_path.open("ab") as log_file, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            command,
            stdin=devnull,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    if not _wait_for_socket(socket_path, proc, timeout=STARTUP_TIMEOUT_SECONDS):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        _emit_start_failure(log_path)

    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    click.echo(f"daemon started (pid={proc.pid}, socket={socket_path}, log={log_path})")


@daemon.command()
@click.option("--timeout", type=float, default=STOP_TIMEOUT_SECONDS, show_default=True)
@click.option("--socket", "socket_override", type=click.Path(path_type=Path), hidden=True)
def stop(timeout: float, socket_override: Path | None):
    """Stop the daemon."""
    pid_path = _pid_path()
    pid = _read_pid(pid_path)
    socket_path = _socket_path(socket_override)
    if not _process_alive(pid):
        if pid_path.exists():
            _remove_file(pid_path)
        click.echo("daemon not running")
        return

    os.kill(pid, signal.SIGTERM)
    forced = False
    if not _wait_for_exit(pid, timeout=max(timeout, 0.0)):
        os.kill(pid, signal.SIGKILL)
        _wait_for_exit(pid, timeout=1.0)
        forced = True

    _remove_file(pid_path)
    _wait_for_socket_removal(socket_path, timeout=max(timeout, 0.0))
    if forced:
        click.echo(f"warning: daemon did not exit after {timeout:g}s; sent SIGKILL")
    click.echo(f"daemon stopped (pid={pid})")


@daemon.command()
@click.option("--timeout", type=float, default=STOP_TIMEOUT_SECONDS, show_default=True)
@click.option("--socket", "socket_override", type=click.Path(path_type=Path), hidden=True)
@click.option("--registry", "registry_override", type=click.Path(path_type=Path), hidden=True)
@click.option("--log", "log_override", type=click.Path(path_type=Path), hidden=True)
def reload(
    timeout: float,
    socket_override: Path | None,
    registry_override: Path | None,
    log_override: Path | None,
):
    """Restart the daemon so it picks up new code.

    The daemon is a long-lived Python process; editable-install code
    changes (pipx install --editable) don't reach it without a
    restart. `drydock daemon reload` stops + starts in one command. Equivalent
    to `systemctl restart drydock.service` where systemd is
    managing the unit.
    """
    pid_path = _pid_path()
    pid = _read_pid(pid_path)
    socket_path = _socket_path(socket_override)

    if _process_alive(pid):
        os.kill(pid, signal.SIGTERM)
        if not _wait_for_exit(pid, timeout=max(timeout, 0.0)):
            os.kill(pid, signal.SIGKILL)
            _wait_for_exit(pid, timeout=1.0)
        _remove_file(pid_path)
        _wait_for_socket_removal(socket_path, timeout=max(timeout, 0.0))
        click.echo(f"daemon stopped (pid={pid})")
    else:
        if pid_path.exists():
            _remove_file(pid_path)
        click.echo("daemon was not running; starting")

    registry_path = _registry_path(registry_override)
    log_path = _log_path(log_override)
    command = _daemon_command(socket_path, registry_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        _remove_file(socket_path)

    with log_path.open("ab") as log_file, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            command,
            stdin=devnull,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    if not _wait_for_socket(socket_path, proc, timeout=STARTUP_TIMEOUT_SECONDS):
        if proc.poll() is None:
            proc.terminate()
        raise click.ClickException(
            f"daemon failed to come up within {STARTUP_TIMEOUT_SECONDS}s; see {log_path}"
        )
    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    click.echo(f"daemon started (pid={proc.pid})")


@daemon.command()
@click.option("--socket", "socket_override", type=click.Path(path_type=Path), hidden=True)
@click.option("--log", "log_override", type=click.Path(path_type=Path), hidden=True)
@click.pass_context
def status(ctx, socket_override: Path | None, log_override: Path | None):
    """Show daemon status."""
    out = ctx.obj["output"]
    socket_path = _socket_path(socket_override)
    log_path = _log_path(log_override)
    data = _daemon_status(socket_path, log_path)

    out.success(
        data,
        human_lines=[
            f"running: {'yes' if data['running'] else 'no'}",
            f"pid: {data['pid'] if data['pid'] is not None else '(none)'}",
            f"socket: {'present' if data['socket_present'] else 'missing'} ({data['socket_path']})",
            f"health: {'responsive' if data['health_responsive'] else 'not responsive'}",
            f"log: {data['log_path']}",
            f"last log line: {data['last_log_line'] or '(none)'}",
        ],
    )
    raise SystemExit(0 if data["running"] and data["health_responsive"] else 1)


@daemon.command()
@click.option("-n", "num_lines", type=int, default=50, show_default=True)
@click.option("-f", "--follow", is_flag=True, help="Follow appended log output")
@click.option("--log", "log_override", type=click.Path(path_type=Path))
def logs(num_lines: int, follow: bool, log_override: Path | None):
    """Show daemon logs."""
    log_path = _log_path(log_override)
    if not log_path.exists():
        click.echo(f"(no log file at {log_path})")
        raise SystemExit(1)

    for line in _last_lines(log_path, num_lines):
        click.echo(line)

    if not follow:
        return

    proc = subprocess.Popen(["tail", "-n", "0", "-f", str(log_path)])
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
