"""Minimal daemon JSON-RPC client per docs/v2-design-protocol.md §1."""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path


def _default_socket_path() -> Path:
    value = os.environ.get("DRYDOCK_DAEMON_SOCKET")
    if value:
        return Path(value)
    return Path.home() / ".drydock" / "run" / "daemon.sock"


class DaemonUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DaemonRpcError(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message
        self.data = data

    def __str__(self) -> str:
        return f"{self.code} {self.message}"


def call_daemon(
    method: str,
    params: dict,
    *,
    socket_path: Path | None = None,
    request_id: str | None = None,
    auth: str | None = None,
    timeout: float = 30.0,
) -> dict:
    socket_path = socket_path or _default_socket_path()
    if not socket_path.exists():
        raise DaemonUnavailable("socket_missing")

    payload: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    if auth is not None:
        payload["auth"] = auth

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall((json.dumps(payload) + "\n").encode("utf-8"))

            response_bytes = b""
            while not response_bytes.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                response_bytes += chunk
    except ConnectionRefusedError as exc:
        raise DaemonUnavailable("connection_refused") from exc
    except FileNotFoundError as exc:
        raise DaemonUnavailable("socket_missing") from exc
    except PermissionError as exc:
        raise DaemonUnavailable("permission_denied") from exc

    response = json.loads(response_bytes.decode("utf-8").strip())
    if "result" in response:
        result = response["result"]
        if isinstance(result, dict):
            return result
        raise DaemonRpcError(-32603, "invalid_response")
    if "error" in response:
        error = response["error"]
        if not isinstance(error, dict):
            raise DaemonRpcError(-32603, "invalid_response")
        raise DaemonRpcError(
            int(error.get("code", -32603)),
            str(error.get("message", "internal_error")),
            error.get("data") if isinstance(error.get("data"), dict) else None,
        )
    raise DaemonRpcError(-32603, "invalid_response")
