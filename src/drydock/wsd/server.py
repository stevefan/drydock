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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drydock.core.registry import Registry
from drydock.wsd.auth import validate_token
from drydock.wsd.recovery import recover_in_progress

logger = logging.getLogger(__name__)
_JSON_RPC_VERSION = "2.0"
_REGISTRY_PATH: Path | None = None
_SECRETS_ROOT: Path | None = None
_DRY_RUN = False


@dataclass(frozen=True)
class _RpcError(ValueError):
    code: int
    message: str
    data: object | None = None


@dataclass(frozen=True)
class MethodSpec:
    handler: Callable[[dict | list | None, str | int | None, str | None], Any]
    requires_auth: bool


def _health(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
) -> dict[str, object]:
    del params
    del request_id
    del caller_desk_id
    return {"ok": True, "pid": os.getpid(), "version": "v2-slice1b"}


def _create_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
) -> dict[str, object]:
    if _REGISTRY_PATH is None:
        raise _RpcError(code=-32603, message="Internal error")
    if _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")
    if request_id is None:
        raise _RpcError(
            code=-32600,
            message="Invalid Request",
            data={"reason": "request_id_required"},
        )
    del caller_desk_id

    request_key = str(request_id)
    registry = Registry(db_path=_REGISTRY_PATH)
    try:
        cached = registry._conn.execute(
            """
            SELECT status, outcome_json
            FROM task_log
            WHERE request_id = ?
            """,
            (request_key,),
        ).fetchone()
        if cached is not None:
            return _replay_cached_outcome(request_key, cached["status"], cached["outcome_json"])

        created_at = _utc_now()
        registry._conn.execute(
            """
            INSERT INTO task_log
                (request_id, method, spec_json, status, outcome_json, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_key,
                "CreateDesk",
                json.dumps(params),
                "in_progress",
                None,
                created_at,
                None,
            ),
        )
        registry._conn.commit()

        try:
            from drydock.wsd.handlers import create_desk
            result = create_desk(
                params,
                request_id,
                None,
                registry_path=_REGISTRY_PATH,
                secrets_root=_SECRETS_ROOT,
                dry_run=_DRY_RUN,
            )
        except _RpcError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            _finish_task_log(registry, request_key, "failed", error)
            raise

        _finish_task_log(registry, request_key, "completed", result)
        return result
    finally:
        registry.close()


def _spawn_child(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
) -> dict[str, object]:
    if _REGISTRY_PATH is None:
        raise _RpcError(code=-32603, message="Internal error")
    if _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")
    if request_id is None:
        raise _RpcError(
            code=-32600,
            message="Invalid Request",
            data={"reason": "request_id_required"},
        )

    request_key = str(request_id)
    registry = Registry(db_path=_REGISTRY_PATH)
    try:
        cached = registry._conn.execute(
            """
            SELECT status, outcome_json
            FROM task_log
            WHERE request_id = ?
            """,
            (request_key,),
        ).fetchone()
        if cached is not None:
            return _replay_cached_outcome(request_key, cached["status"], cached["outcome_json"])

        created_at = _utc_now()
        registry._conn.execute(
            """
            INSERT INTO task_log
                (request_id, method, spec_json, status, outcome_json, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_key,
                "SpawnChild",
                json.dumps(params),
                "in_progress",
                None,
                created_at,
                None,
            ),
        )
        registry._conn.commit()

        try:
            from drydock.wsd.handlers import spawn_child
            result = spawn_child(
                params,
                request_id,
                caller_desk_id,
                registry_path=_REGISTRY_PATH,
                secrets_root=_SECRETS_ROOT,
                dry_run=_DRY_RUN,
            )
        except _RpcError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            _finish_task_log(registry, request_key, "failed", error)
            raise

        _finish_task_log(registry, request_key, "completed", result)
        return result
    finally:
        registry.close()


def _whoami(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
) -> dict[str, object]:
    from drydock.wsd.handlers import whoami

    return whoami(params, request_id, caller_desk_id)


def _finish_task_log(
    registry: Registry,
    request_id: str,
    status: str,
    outcome: object,
) -> None:
    registry._conn.execute(
        """
        UPDATE task_log
        SET status = ?, outcome_json = ?, completed_at = ?
        WHERE request_id = ?
        """,
        (status, json.dumps(outcome), _utc_now(), request_id),
    )
    registry._conn.commit()


def _replay_cached_outcome(request_id: str, status: str, outcome_json: str | None) -> dict[str, object]:
    if status == "in_progress":
        raise _RpcError(
            code=-32002,
            message="request_in_progress",
            data={"request_id": request_id},
        )
    outcome = json.loads(outcome_json) if outcome_json else None
    if status == "completed":
        return outcome
    if status == "failed" and isinstance(outcome, dict):
        raise _RpcError(
            code=outcome["code"],
            message=outcome["message"],
            data=outcome.get("data"),
        )
    raise _RpcError(code=-32603, message="Internal error")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_METHODS: dict[str, MethodSpec] = {
    "CreateDesk": MethodSpec(handler=_create_desk, requires_auth=False),
    "SpawnChild": MethodSpec(handler=_spawn_child, requires_auth=True),
    "wsd.health": MethodSpec(handler=_health, requires_auth=False),
    "wsd.whoami": MethodSpec(handler=_whoami, requires_auth=True),
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


def _parse_request(
    payload: object,
) -> tuple[str, dict | list | None, str | int | None, str | None, bool]:
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

    auth = payload.get("auth")
    if auth is not None and not isinstance(auth, str):
        raise _RpcError(code=-32600, message="Invalid Request")

    is_notification = "id" not in payload
    return method, params, request_id, auth, is_notification


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
            method_name, params, request_id, auth, is_notification = _parse_request(payload)
        except _RpcError as exc:
            return _error_response(
                None,
                code=exc.code,
                message=exc.message,
                data=exc.data,
            )

        if is_notification:
            return None

        spec = _METHODS.get(method_name)
        if spec is None:
            return _error_response(
                request_id,
                code=-32601,
                message=f"Method not found: {method_name}",
            )

        try:
            caller_desk_id = self._resolve_caller(spec, auth)
            result = spec.handler(params, request_id, caller_desk_id)
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

    def _resolve_caller(self, spec: MethodSpec, auth: str | None) -> str | None:
        if _REGISTRY_PATH is None:
            raise _RpcError(code=-32603, message="Internal error")
        if auth is None:
            if spec.requires_auth:
                raise _RpcError(
                    code=-32004,
                    message="unauthenticated",
                    data={"reason": "no_token"},
                )
            return None

        registry = Registry(db_path=_REGISTRY_PATH)
        try:
            caller_desk_id = validate_token(auth, registry)
        finally:
            registry.close()
        if caller_desk_id is None and spec.requires_auth:
            raise _RpcError(
                code=-32004,
                message="unauthenticated",
                data={"reason": "invalid_token"},
            )
        return caller_desk_id


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(socket_path: Path, registry_path: Path | None, secrets_root: Path, dry_run: bool) -> None:
    """Bind the Unix socket and serve until interrupted.

    Removes a stale socket file if one exists at `socket_path`. Creates
    parent directories if missing. Cleans up the socket file on exit.
    """
    global _REGISTRY_PATH, _SECRETS_ROOT, _DRY_RUN
    socket_path = Path(socket_path)
    _REGISTRY_PATH = registry_path or (Path.home() / ".drydock" / "registry.db")
    _SECRETS_ROOT = Path(secrets_root)
    _DRY_RUN = dry_run
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    if _REGISTRY_PATH is not None:
        try:
            report = recover_in_progress(_REGISTRY_PATH)
        except Exception:
            logger.exception("wsd: startup recovery failed for %s", _REGISTRY_PATH)
            raise
        logger.info(
            "wsd: recovery report — completed=%d rolled_back=%d unknown_method=%d",
            report.completed,
            report.rolled_back,
            report.unknown_method,
        )
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
