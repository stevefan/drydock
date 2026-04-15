"""wsd Unix-socket server — Slice 1b JSON-RPC dispatcher.

Binds a Unix stream socket at the configured path, accepts connections
(threaded, one per client), reads a single newline-delimited JSON-RPC 2.0
request per connection, returns a single JSON-RPC response, and closes.

Implements the wire/error contracts from docs/v2-design-protocol.md §2,
§3, and §8 while keeping the socket lifecycle + threading model unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from pathlib import Path

logger = logging.getLogger(__name__)
_JSON_RPC_VERSION = "2.0"


@dataclass(frozen=True)
class _RpcError(ValueError):
    code: int
    message: str
    data: object | None = None


def _health(params: dict | list | None) -> dict[str, object]:
    del params
    return {"ok": True, "pid": os.getpid(), "version": "v2-slice1b"}


_METHODS: dict[str, Callable[[dict | list | None], Any]] = {
    "wsd.health": _health,
}


def _error_response(
    request_id: str | int | None,
    *,
    code: int,
    message: str,
    data: object | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": _JSON_RPC_VERSION, "id": request_id, "error": error}


def _success_response(request_id: str | int | None, result: Any) -> dict[str, object]:
    return {"jsonrpc": _JSON_RPC_VERSION, "id": request_id, "result": result}


def _parse_request(payload: object) -> tuple[str, dict | list | None, str | int | None, bool]:
    if not isinstance(payload, dict):
        raise _RpcError(code=-32600, message="Invalid Request")

    request_id = payload.get("id")
    if "id" in payload and not isinstance(request_id, (str, int)) and request_id is not None:
        raise _RpcError(code=-32600, message="Invalid Request")

    method = payload.get("method")
    if payload.get("jsonrpc") != _JSON_RPC_VERSION or not isinstance(method, str) or not method:
        raise _RpcError(code=-32600, message="Invalid Request")

    params = payload.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        raise _RpcError(code=-32600, message="Invalid Request")

    is_notification = "id" not in payload
    return method, params, request_id, is_notification


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            response = _error_response(None, code=-32700, message=f"Parse error: {exc.msg}")
        else:
            response = self._dispatch(payload)
        if response is None:
            return
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))

    def _dispatch(self, payload: object) -> dict[str, object] | None:
        try:
            method_name, params, request_id, is_notification = _parse_request(payload)
        except _RpcError as exc:
            return _error_response(
                None,
                code=exc.code,
                message=exc.message,
                data=exc.data,
            )

        if is_notification:
            return None

        handler = _METHODS.get(method_name)
        if handler is None:
            return _error_response(
                request_id,
                code=-32601,
                message=f"Method not found: {method_name}",
            )

        try:
            result = handler(params)
        except _RpcError as exc:
            return _error_response(
                request_id,
                code=exc.code,
                message=exc.message,
                data=exc.data,
            )
        except Exception:
            logger.exception("wsd: internal error handling %s", method_name)
            return _error_response(
                request_id,
                code=-32603,
                message="Internal error",
            )
        return _success_response(request_id, result)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(socket_path: Path) -> None:
    """Bind the Unix socket and serve until interrupted.

    Removes a stale socket file if one exists at `socket_path`. Creates
    parent directories if missing. Cleans up the socket file on exit.
    """
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    with _Server(str(socket_path), _Handler) as server:
        logger.info("wsd: listening on %s", socket_path)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("wsd: interrupted, shutting down")
        finally:
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
