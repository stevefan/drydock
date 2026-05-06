"""Root pytest fixtures.

Shared fixtures (registry, output, daemon) live here so any test can use them
via fixture lookup without import games. Sub-directory conftests must NOT
declare these via `pytest_plugins = ("tests.daemon.conftest",)` etc. — that
collides with pytest's auto-discovery and produces a duplicate-plugin
registration error.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from drydock.core.registry import Registry
from drydock.output.formatter import Output


@pytest.fixture
def registry(tmp_path):
    return Registry(db_path=tmp_path / "test.db")


@pytest.fixture
def output():
    return Output(force_json=True)


# --- drydock daemon fixtures (per docs/v2-design-state.md §7) ---------------------


def _wait_for_socket(path: Path, proc: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            _, stderr = proc.communicate(timeout=1)
            raise RuntimeError(
                f"daemon exited before socket appeared (rc={proc.returncode}); "
                f"stderr: {stderr.decode('utf-8', errors='replace')}"
            )
        time.sleep(0.02)
    raise TimeoutError(f"daemon socket {path} did not appear within {timeout}s")


class WsdClient:
    """Minimal blocking client for newline-delimited JSON over a Unix socket."""

    def __init__(self, socket_path: Path, registry_path: Path, home: Path, secrets_root: Path):
        self.socket_path = socket_path
        self.registry_path = registry_path
        self.home = home
        self.secrets_root = secrets_root

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

    def call_rpc(
        self,
        method: str,
        params: dict | list | None = None,
        request_id: str | int | None = "test-1",
        auth: str | None = None,
    ) -> dict:
        payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if request_id is not None:
            payload["id"] = request_id
        if auth is not None:
            payload["auth"] = auth
        response = self.call(payload)
        if "result" in response:
            return {"result": response["result"]}
        if "error" in response:
            return {"error": response["error"]}
        return response


@pytest.fixture
def daemon():
    # macOS AF_UNIX socket paths are capped at ~104 chars; pytest's tmp_path
    # blows past that. Use a short /tmp dir instead and clean up manually.
    tmp = Path(tempfile.mkdtemp(prefix="daemon-", dir="/tmp"))
    sock = tmp / "s"  # 1-char filename keeps path well under limit
    registry = tmp / "r.db"
    secrets_root = tmp / "secrets"
    Registry(db_path=registry).close()
    env = os.environ.copy()
    env["DRYDOCK_WSD_DRY_RUN"] = "1"
    env["DRYDOCK_SECRETS_ROOT"] = str(secrets_root)
    env["HOME"] = str(tmp)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "drydock.daemon",
            "--socket",
            str(sock),
            "--registry",
            str(registry),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_socket(sock, proc, timeout=5.0)
        yield WsdClient(sock, registry, tmp, secrets_root)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)
