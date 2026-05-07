"""Shared RPC primitives — error type, idempotency wrapper, replay helper.

Lives here (not in server.py) so handler modules can import without
creating a circular dependency on the dispatcher. Per the design,
the daemon's RPC error model has exactly one shape; per V2 protocol
§3, state-mutating handlers wrap their work in task_log so client
retries replay the cached outcome instead of re-applying side effects.

This module is import-safe from anywhere; it doesn't touch the
daemon's module-level globals (_REGISTRY_PATH etc.) — callers pass
the registry-builder explicitly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from drydock.core.registry import Registry


@dataclass(frozen=True)
class _RpcError(ValueError):
    """JSON-RPC 2.0 error payload.

    code/message/data shape mirrors the wire protocol so handlers can
    raise this and let the dispatcher serialize it directly.

    **Application error code registry** (-32xxx range). Each code maps
    to exactly one error class; reusing a code for different meanings
    is a wire-protocol bug. When adding a new error, pick the next
    free code below.

    Standard JSON-RPC errors (don't reuse):
      -32700  parse_error
      -32600  invalid_request
      -32601  method_not_found
      -32602  invalid_params
      -32603  internal_error

    Application-defined (drydock-specific):
      -32000  generic application error (avoid; prefer specific code)
      -32001  reserved for narrowness/policy refusals (capability)
      -32002  request_in_progress (task_log replay)
      -32004  unauthenticated / forbidden
      -32005  reserved (storage scope refusal)
      -32006  narrowness_violated (capability narrowness gate)
      -32007  backend_permission_denied (capability backend)
      -32008  backend_unavailable (capability backend)
      -32009  backend_missing_secret (capability backend)
      -32010  desk_not_running (capability handler)
      -32011  materialization_failed (capability handler)
      -32012  lease_not_found (capability handler)
      -32013  capability_unsupported
      -32014  reserved (CreateDesk validation)
      -32015  storage_backend_not_configured
      -32016  storage_backend_config_error
      -32017  workload_lease_exists           (Phase 2a.3 WL1)
      -32018  workload_drydock_not_running    (Phase 2a.3 WL1)
      -32019  workload_apply_failed           (Phase 2a.3 WL1)
    """
    code: int
    message: str
    data: object | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finish_task_log(
    registry: Registry,
    request_id: str,
    status: str,
    outcome: object,
) -> None:
    registry._conn.execute(
        "UPDATE task_log SET status = ?, outcome_json = ?, completed_at = ? "
        "WHERE request_id = ?",
        (status, json.dumps(outcome), _utc_now(), request_id),
    )
    registry._conn.commit()


def _replay_cached_outcome(
    request_id: str,
    status: str,
    outcome_json: Optional[str],
) -> dict:
    """Replay a previously-cached task_log row.

    in_progress → caller is told to retry later (no double-apply).
    completed → return the cached success outcome.
    failed → re-raise the cached error (special-case: destroy
             outcomes that succeeded but report destroyed=true also
             return the dict, since destroy's contract).
    """
    if status == "in_progress":
        raise _RpcError(
            code=-32002,
            message="request_in_progress",
            data={"request_id": request_id},
        )
    outcome = json.loads(outcome_json) if outcome_json else None
    if status == "completed":
        return outcome
    if status == "failed" and isinstance(outcome, dict) and outcome.get("destroyed") is True:
        return outcome
    if status == "failed" and isinstance(outcome, dict):
        raise _RpcError(
            code=outcome["code"],
            message=outcome["message"],
            data=outcome.get("data"),
        )
    raise _RpcError(code=-32603, message="Internal error")


def with_task_log(
    *,
    method: str,
    params: object,
    request_id: str | int | None,
    registry_path: Optional[Path],
    fn: Callable[[Registry], dict],
    status_for: Optional[Callable[[dict], str]] = None,
) -> dict:
    """Run ``fn(registry)`` inside the task_log idempotency contract.

    Per docs/v2-design-protocol.md §3, state-mutating handlers cache
    outcomes by request_id so client retries replay the cached result
    instead of re-applying side effects. Was duplicated across handlers;
    this is the canonical implementation.

    The handler ``fn`` receives an open Registry and returns the
    success outcome. ``_RpcError`` raised inside fn is caught,
    persisted as the failed outcome, and re-raised so the dispatcher
    serializes the error response correctly.

    ``status_for`` is an optional callback invoked on the success
    outcome to compute the task_log row's terminal status. Defaults
    to "completed". DestroyDesk uses this to record the row as "failed"
    when ``result.partial_failures`` is set, so a retry of a
    half-destroyed desk surfaces the partial-failure shape directly
    via the cached-outcome replay path.
    """
    if registry_path is None:
        raise _RpcError(code=-32603, message="Internal error")
    if request_id is None:
        # Per protocol §3 — without request_id the call is not safe to
        # retry; daemon would issue duplicate side effects.
        raise _RpcError(
            code=-32600, message="Invalid Request",
            data={"reason": "request_id_required"},
        )

    request_key = str(request_id)
    registry = Registry(db_path=registry_path)
    try:
        cached = registry._conn.execute(
            "SELECT status, outcome_json FROM task_log WHERE request_id = ?",
            (request_key,),
        ).fetchone()
        if cached is not None:
            return _replay_cached_outcome(
                request_key, cached["status"], cached["outcome_json"],
            )

        registry._conn.execute(
            "INSERT INTO task_log "
            "(request_id, method, spec_json, status, outcome_json, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_key, method, json.dumps(params), "in_progress", None,
             _utc_now(), None),
        )
        registry._conn.commit()

        try:
            result = fn(registry)
        except _RpcError as exc:
            error: dict = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            _finish_task_log(registry, request_key, "failed", error)
            raise

        terminal = status_for(result) if status_for else "completed"
        _finish_task_log(registry, request_key, terminal, result)
        return result
    finally:
        registry.close()
