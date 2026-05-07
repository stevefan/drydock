"""daemon Unix-socket server — Slice 1b JSON-RPC dispatcher.

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
from drydock.daemon.auth import validate_token
from drydock.daemon.recovery import recover_in_progress, recover_in_progress_migrations

logger = logging.getLogger(__name__)
_JSON_RPC_VERSION = "2.0"
_REGISTRY_PATH: Path | None = None
_SECRETS_ROOT: Path | None = None
_DRY_RUN = False
_SECRETS_BACKEND_NAME = "file"
# V4 Phase 1: pre-built storage backend (StsAssumeRoleBackend or StubStorageBackend).
# None = storage not configured; STORAGE_MOUNT leases reject with
# `storage_backend_not_configured`. Built once in serve() — AWS CLI setup
# is stable for the daemon's lifetime.
_STORAGE_BACKEND: Any = None


# _RpcError + task_log helpers moved to drydock.daemon.rpc_common so
# handler modules (workload_handlers, etc.) can import without circular
# dependency on the dispatcher.
from drydock.daemon.rpc_common import _RpcError, with_task_log  # noqa: E402


@dataclass(frozen=True)
class MethodSpec:
    handler: Callable[[dict | list | None, str | int | None, str | None], Any]
    requires_auth: bool


def _health(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    del params
    del request_id
    del caller_drydock_id
    return {"ok": True, "pid": os.getpid(), "version": "v2-slice1b"}


def _create_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")
    del caller_drydock_id

    def _impl(_registry):
        from drydock.daemon.handlers import create_desk
        return create_desk(
            params, request_id, None,
            registry_path=_REGISTRY_PATH,
            secrets_root=_SECRETS_ROOT,
            dry_run=_DRY_RUN,
        )
    return with_task_log(
        method="CreateDesk", params=params, request_id=request_id,
        registry_path=_REGISTRY_PATH, fn=_impl,
    )


def _spawn_child(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")

    def _impl(_registry):
        from drydock.daemon.handlers import spawn_child
        return spawn_child(
            params, request_id, caller_drydock_id,
            registry_path=_REGISTRY_PATH,
            secrets_root=_SECRETS_ROOT,
            dry_run=_DRY_RUN,
        )
    return with_task_log(
        method="SpawnChild", params=params, request_id=request_id,
        registry_path=_REGISTRY_PATH, fn=_impl,
    )


def _destroy_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")

    def _impl(_registry):
        from drydock.daemon.handlers import destroy_desk
        return destroy_desk(
            params, request_id, caller_drydock_id,
            registry_path=_REGISTRY_PATH,
            secrets_root=_SECRETS_ROOT,
            dry_run=_DRY_RUN,
        )
    # Destroy returns its result even on partial_failures so the caller
    # sees the structured shape; the task_log row is recorded as "failed"
    # so a retry surfaces the partial-failure outcome via _replay_cached.
    def _status(result: dict) -> str:
        return "failed" if result.get("partial_failures") else "completed"

    return with_task_log(
        method="DestroyDesk", params=params, request_id=request_id,
        registry_path=_REGISTRY_PATH, fn=_impl, status_for=_status,
    )


def _whoami(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    from drydock.daemon.handlers import whoami

    return whoami(params, request_id, caller_drydock_id)


def _request_capability(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")

    def _impl(_registry):
        from drydock.daemon.capability_handlers import request_capability
        return request_capability(
            params, request_id, caller_drydock_id,
            registry_path=_REGISTRY_PATH,
            secrets_root=_SECRETS_ROOT,
            backend_name=_SECRETS_BACKEND_NAME,
            storage_backend=_STORAGE_BACKEND,
        )

    return with_task_log(
        method="RequestCapability", params=params, request_id=request_id,
        registry_path=_REGISTRY_PATH, fn=_impl,
    )


def _release_capability(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _REGISTRY_PATH is None or _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")
    # Naturally idempotent per §3 — repeats by lease_id are safe; no
    # task_log entry needed.
    from drydock.daemon.capability_handlers import release_capability
    return release_capability(
        params,
        request_id,
        caller_drydock_id,
        registry_path=_REGISTRY_PATH,
        secrets_root=_SECRETS_ROOT,
    )


def _stop_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _REGISTRY_PATH is None:
        raise _RpcError(code=-32603, message="Internal error")
    from drydock.daemon.handlers import stop_desk
    return stop_desk(
        params,
        request_id,
        caller_drydock_id,
        registry_path=_REGISTRY_PATH,
        dry_run=_DRY_RUN,
    )


def _list_desks(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _REGISTRY_PATH is None:
        raise _RpcError(code=-32603, message="Internal error")
    from drydock.daemon.handlers import list_desks
    return list_desks(
        params,
        request_id,
        caller_drydock_id,
        registry_path=_REGISTRY_PATH,
    )


def _list_children(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _REGISTRY_PATH is None:
        raise _RpcError(code=-32603, message="Internal error")
    from drydock.daemon.handlers import list_children
    return list_children(
        params,
        request_id,
        caller_drydock_id,
        registry_path=_REGISTRY_PATH,
    )


def _inspect_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _REGISTRY_PATH is None:
        raise _RpcError(code=-32603, message="Internal error")
    from drydock.daemon.handlers import inspect_desk
    return inspect_desk(
        params,
        request_id,
        caller_drydock_id,
        registry_path=_REGISTRY_PATH,
    )


def _get_audit(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    # Read-only introspection per protocol §2; no task_log idempotency
    # needed. Reads the audit.log file directly. requires_auth=False.
    from drydock.core.audit import DEFAULT_LOG_PATH
    from drydock.daemon.audit_handlers import get_audit
    return get_audit(
        params,
        request_id,
        caller_drydock_id,
        log_path=DEFAULT_LOG_PATH,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_METHODS: dict[str, MethodSpec] = {
    "CreateDesk": MethodSpec(handler=_create_desk, requires_auth=False),
    "DestroyDesk": MethodSpec(handler=_destroy_desk, requires_auth=False),
    "SpawnChild": MethodSpec(handler=_spawn_child, requires_auth=True),
    "RequestCapability": MethodSpec(handler=_request_capability, requires_auth=True),
    "ReleaseCapability": MethodSpec(handler=_release_capability, requires_auth=True),
    "StopDesk": MethodSpec(handler=_stop_desk, requires_auth=False),
    "ListDesks": MethodSpec(handler=_list_desks, requires_auth=False),
    "ListChildren": MethodSpec(handler=_list_children, requires_auth=True),
    "InspectDesk": MethodSpec(handler=_inspect_desk, requires_auth=False),
    "GetAudit": MethodSpec(handler=_get_audit, requires_auth=False),
    "daemon.health": MethodSpec(handler=_health, requires_auth=False),
    "daemon.whoami": MethodSpec(handler=_whoami, requires_auth=True),
}


# ---------------------------------------------------------------------------
# Phase 2a.3 WL1: workload RPCs registered separately so the dispatch
# table reads cleanly. Wrapped in task_log for idempotency on retry.
# ---------------------------------------------------------------------------


def _register_workload(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    def _impl(_registry):
        from drydock.daemon.workload_handlers import register_workload
        return register_workload(
            params, request_id, caller_drydock_id,
            registry_path=_REGISTRY_PATH,
        )
    return with_task_log(
        method="RegisterWorkload", params=params, request_id=request_id,
        registry_path=_REGISTRY_PATH, fn=_impl,
    )


def _release_workload(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    def _impl(_registry):
        from drydock.daemon.workload_handlers import release_workload
        return release_workload(
            params, request_id, caller_drydock_id,
            registry_path=_REGISTRY_PATH,
        )
    return with_task_log(
        method="ReleaseWorkload", params=params, request_id=request_id,
        registry_path=_REGISTRY_PATH, fn=_impl,
    )


_METHODS["RegisterWorkload"] = MethodSpec(handler=_register_workload, requires_auth=True)
_METHODS["ReleaseWorkload"] = MethodSpec(handler=_release_workload, requires_auth=True)


# ---------------------------------------------------------------------------
# Phase PA3: Auditor action authority. Single AuditorAction RPC dispatches
# to specific Bucket-2 primitives (stop_dock, revoke_lease, etc.). The
# auditor-scope check is INSIDE the handler — requires_auth=True only
# gates "is the token valid"; the scope gate ensures it's the auditor.
# ---------------------------------------------------------------------------


def _auditor_action(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, object]:
    if _SECRETS_ROOT is None:
        raise _RpcError(code=-32603, message="Internal error")

    def _impl(_registry):
        from drydock.daemon.auditor_handlers import (
            auditor_action, is_live_actions_enabled,
        )
        return auditor_action(
            params, request_id, caller_drydock_id,
            registry_path=_REGISTRY_PATH,
            secrets_root=_SECRETS_ROOT,
            dry_run=not is_live_actions_enabled(),
        )
    return with_task_log(
        method="AuditorAction", params=params, request_id=request_id,
        registry_path=_REGISTRY_PATH, fn=_impl,
    )


_METHODS["AuditorAction"] = MethodSpec(handler=_auditor_action, requires_auth=True)


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
            caller_drydock_id = self._resolve_caller(spec, auth)
            result = spec.handler(params, request_id, caller_drydock_id)
        except _RpcError as exc:
            return _error_response(
                request_id,
                code=exc.code,
                message=exc.message,
                data=exc.data,
            )
        except Exception:
            logger.exception("daemon: internal error handling %s", method_name)
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
            caller_drydock_id = validate_token(auth, registry)
        finally:
            registry.close()
        if caller_drydock_id is None and spec.requires_auth:
            raise _RpcError(
                code=-32004,
                message="unauthenticated",
                data={"reason": "invalid_token"},
            )
        return caller_drydock_id


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(
    socket_path: Path,
    registry_path: Path | None,
    secrets_root: Path,
    dry_run: bool,
    secrets_backend: str = "file",
    storage_backend: str | None = None,
    storage_role_arn: str | None = None,
    storage_source_profile: str = "drydock-runner",
    storage_session_duration_seconds: int = 14400,
) -> None:
    """Bind the Unix socket and serve until interrupted.

    Removes a stale socket file if one exists at `socket_path`. Creates
    parent directories if missing. Cleans up the socket file on exit.

    `secrets_backend` selects the SecretsBackend used by RequestCapability
    (Slice 3). Default is "file"; the daemon.toml [secrets] loader resolves
    the value before serve() is called and rejects unknown names with
    `unknown_secrets_backend` so the daemon never starts misconfigured.

    `storage_backend` selects the StorageBackend used by
    RequestCapability(type=STORAGE_MOUNT). None = STORAGE_MOUNT
    unavailable. "sts" = real AWS STS AssumeRole; "stub" = test backend.
    """
    global _REGISTRY_PATH, _SECRETS_ROOT, _DRY_RUN, _SECRETS_BACKEND_NAME, _STORAGE_BACKEND
    socket_path = Path(socket_path)
    _REGISTRY_PATH = registry_path or (Path.home() / ".drydock" / "registry.db")
    _SECRETS_ROOT = Path(secrets_root)
    _DRY_RUN = dry_run
    _SECRETS_BACKEND_NAME = secrets_backend

    if storage_backend:
        from drydock.core.storage import build_storage_backend
        _STORAGE_BACKEND = build_storage_backend(
            storage_backend,
            role_arn=storage_role_arn,
            source_profile=storage_source_profile,
            session_duration_seconds=storage_session_duration_seconds,
        )
        logger.info("daemon: storage backend = %s", storage_backend)
    else:
        _STORAGE_BACKEND = None
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    if _REGISTRY_PATH is not None:
        try:
            report = recover_in_progress(_REGISTRY_PATH)
        except Exception:
            logger.exception("daemon: startup recovery failed for %s", _REGISTRY_PATH)
            raise
        logger.info(
            "daemon: recovery report — completed=%d rolled_back=%d unknown_method=%d",
            report.completed,
            report.rolled_back,
            report.unknown_method,
        )
        # Phase 2a.4 M1: migration recovery. If the daemon died mid-walk,
        # any migrations row stuck at status='in_progress' gets rolled
        # back from its snapshot (if one exists) or marked failed (if
        # pre-snapshot or snapshot missing).
        try:
            mig_rolled, mig_failed = recover_in_progress_migrations(_REGISTRY_PATH)
        except Exception:
            logger.exception("daemon: migration recovery failed for %s", _REGISTRY_PATH)
            raise
        if mig_rolled or mig_failed:
            logger.info(
                "daemon: migration recovery — rolled_back=%d failed=%d",
                mig_rolled, mig_failed,
            )
        # Per docs/v2-design-protocol.md §3: bounded task log. One sweep
        # at boot; in-progress rows are never evicted.
        registry = Registry(db_path=_REGISTRY_PATH)
        try:
            evicted = registry.evict_old_task_log()
            if evicted:
                logger.info("daemon: evicted %d old task_log entries", evicted)
        finally:
            registry.close()
    # Phase 2a.3 WL1: lease-expiry sweeper. Background daemon thread that
    # wakes every WORKLOAD_SWEEP_INTERVAL_SECONDS and reverts expired
    # workload leases. Daemon-thread so KeyboardInterrupt during
    # serve_forever() drops it cleanly without join.
    import threading
    sweep_stop = threading.Event()
    def _sweep_loop():
        from drydock.core.workload import sweep_expired_leases
        from drydock.core.audit import emit_audit
        interval = float(os.getenv("WORKLOAD_SWEEP_INTERVAL_SECONDS", "60"))
        while not sweep_stop.wait(interval):
            try:
                reg = Registry(db_path=_REGISTRY_PATH)
                try:
                    summaries = sweep_expired_leases(reg)
                finally:
                    reg.close()
            except Exception:
                logger.exception("daemon: workload sweep failed")
                continue
            for s in summaries:
                try:
                    emit_audit(
                        "workload.lease_expired"
                        if s["terminal_status"] == "expired"
                        else "workload.lease_partial_revoked",
                        principal=s["drydock_id"],
                        request_id=None,
                        method="WorkloadSweeper",
                        result="ok" if s["terminal_status"] == "expired" else "error",
                        details=s,
                    )
                except Exception:
                    logger.exception("daemon: failed to audit sweep result for %s", s.get("lease_id"))
    sweeper_thread = threading.Thread(target=_sweep_loop, daemon=True, name="workload-sweeper")
    sweeper_thread.start()
    logger.info("daemon: workload sweeper started (interval=%ss)",
                os.getenv("WORKLOAD_SWEEP_INTERVAL_SECONDS", "60"))

    with _Server(str(socket_path), _Handler) as server:
        # Socket must be connect()-able by workers inside drydock containers,
        # which run as uid 1000 (node). daemon runs as Harbor root. Unix-socket
        # connect() requires write permission on the socket file; the default
        # umask leaves it at 0755, which blocks non-root callers.
        #
        # 0o666 is not the security boundary — the bearer token (checked by
        # the auth middleware) is. The socket permission just gates transport
        # reachability from inside drydocks. See docs/v2-design-protocol.md §5.
        try:
            os.chmod(socket_path, 0o666)
        except OSError as exc:
            logger.warning("daemon: failed to chmod socket to 0o666: %s", exc)
        logger.info("daemon: listening on %s (mode 0o666)", socket_path)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("daemon: interrupted, shutting down")
        finally:
            sweep_stop.set()
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
