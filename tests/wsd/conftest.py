"""pytest fixtures for wsd daemon subprocess tests.

Spawns `python -m drydock.wsd` in a subprocess with a temp socket path.
Teardown terminates the process and waits. Per the daemon test strategy
in docs/v2-design-state.md §7.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


def _wait_for_socket(path: Path, proc: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            _, stderr = proc.communicate(timeout=1)
            raise RuntimeError(
                f"wsd exited before socket appeared (rc={proc.returncode}); "
                f"stderr: {stderr.decode('utf-8', errors='replace')}"
            )
        time.sleep(0.02)
    raise TimeoutError(f"wsd socket {path} did not appear within {timeout}s")


class WsdClient:
    """Minimal blocking client for newline-delimited JSON over a Unix socket."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    def call(self, payload: dict, timeout: float = 5.0) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(self.socket_path))
        try:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8").strip())
        finally:
            s.close()


@pytest.fixture
def wsd():
    # macOS AF_UNIX socket paths are capped at ~104 chars; pytest's tmp_path
    # blows past that. Use a short /tmp dir instead and clean up manually.
    tmp = Path(tempfile.mkdtemp(prefix="wsd-", dir="/tmp"))
    sock = tmp / "s"  # 1-char filename keeps path well under limit
    registry = tmp / "r.db"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "drydock.wsd",
            "--socket",
            str(sock),
            "--registry",
            str(registry),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_socket(sock, proc, timeout=5.0)
        yield WsdClient(sock)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)
