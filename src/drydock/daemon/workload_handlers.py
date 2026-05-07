"""RPC handlers for the workload-as-transaction primitive.

Phase 2a.3 WL1. Two RPCs land here:
- ``RegisterWorkload(spec)`` — validates, builds actions, applies
  atomically, persists the lease, returns the lease shape.
- ``ReleaseWorkload(lease_id)`` — looks up, reverts actions, marks
  the lease released.

Auth model: caller must hold a bearer token bound to the drydock
whose workload they're declaring. The lease's ``drydock_id`` is
derived from the caller's token, never from RPC args — same
confused-deputy mitigation as ``RequestCapability``.

Idempotency: same ``request_id`` replays the cached outcome via the
``task_log`` table the daemon already maintains. Critical for
workload registration because the action set has real side effects
(``docker update``); double-application would corrupt the lease record.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from drydock.core.audit import emit_audit
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.core.workload import (
    WorkloadApplyError,
    WorkloadSpec,
    WorkloadValidationError,
    apply_actions_atomically,
    assemble_lease,
    build_actions_for_spec,
    revert_lease_actions,
    validate_spec,
)
from drydock.daemon.rpc_common import _RpcError

logger = logging.getLogger(__name__)


def register_workload(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
) -> dict:
    """Validate spec → build actions → apply atomically → persist lease.

    Auth required (the caller's drydock_id IS the lease's drydock_id).
    """
    if caller_drydock_id is None:
        raise _RpcError(code=-32004, message="unauthenticated")

    spec_dict = _validated_register_params(params)
    spec = WorkloadSpec(**spec_dict)
    try:
        validate_spec(spec)
    except WorkloadValidationError as exc:
        raise _RpcError(
            code=-32602,
            message="invalid_workload_spec",
            data={"reason": str(exc)},
        ) from exc

    registry = Registry(db_path=registry_path)
    try:
        # Look up the desk to get container_id + original ceilings.
        # Caller's token already validated they own this drydock.
        ws = _drydock_by_id(registry, caller_drydock_id)
        if ws is None:
            raise _RpcError(
                code=-32603,
                message="caller_drydock_not_found",
                data={"drydock_id": caller_drydock_id},
            )
        if not ws.container_id:
            raise _RpcError(
                code=-32018,  # workload_drydock_not_running
                message="drydock_not_running",
                data={"fix": "Start the drydock first; cgroup lift requires a live container."},
            )

        # WL1 single-active-lease semantic: refuse if there's already
        # an active workload lease for this desk. WL5 will replace this
        # with stack-or-merge semantics; for now, simplicity wins.
        existing = registry.list_active_workload_leases(drydock_id=caller_drydock_id)
        if existing:
            raise _RpcError(
                code=-32017,  # workload_lease_exists
                message="workload_lease_exists",
                data={
                    "lease_id": existing[0]["id"],
                    "fix": "ReleaseWorkload the existing lease before registering another.",
                },
            )

        # Build the action list. May be empty if the spec doesn't
        # actually need any lift above standing — we still issue a
        # lease (zero-action) so the workload is recorded for audit.
        actions = build_actions_for_spec(
            spec,
            container_id=ws.container_id,
            original_resources_hard=ws.original_resources_hard or {},
        )

        # Apply atomically. On failure, the transaction script reverts
        # already-applied actions; we surface a structured error.
        try:
            applied = apply_actions_atomically(actions)
        except WorkloadApplyError as exc:
            emit_audit(
                "workload.lease_apply_failed",
                principal=caller_drydock_id,
                request_id=request_id,
                method="RegisterWorkload",
                result="error",
                details={
                    "kind": spec.kind,
                    "failed_at": exc.failed_at,
                    "cause": str(exc.cause),
                },
            )
            raise _RpcError(
                code=-32019,  # workload_apply_failed
                message="workload_apply_failed",
                data={"failed_at": exc.failed_at, "cause": str(exc.cause)},
            ) from exc

        lease = assemble_lease(
            drydock_id=caller_drydock_id,
            spec=spec,
            applied_actions=applied,
        )
        registry.insert_workload_lease(lease)
        emit_audit(
            "workload.lease_granted",
            principal=caller_drydock_id,
            request_id=request_id,
            method="RegisterWorkload",
            result="ok",
            details={
                "lease_id": lease.id,
                "kind": spec.kind,
                "duration_max_seconds": spec.duration_max_seconds,
                "actions": [a.get("kind") for a in applied],
                "expires_at": lease.expires_at,
            },
        )
        return {
            "lease_id": lease.id,
            "drydock_id": lease.drydock_id,
            "kind": spec.kind,
            "granted_at": lease.granted_at,
            "expires_at": lease.expires_at,
            "applied_actions": [
                {"kind": a.get("kind")} for a in applied
            ],
            "status": lease.status,
        }
    finally:
        registry.close()


def release_workload(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
) -> dict:
    """Revert a lease's applied actions and mark it released."""
    if caller_drydock_id is None:
        raise _RpcError(code=-32004, message="unauthenticated")

    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"reason": "expected object with lease_id"},
        )
    lease_id = params.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": "lease_id", "reason": "must be a non-empty string"},
        )

    registry = Registry(db_path=registry_path)
    try:
        row = registry.get_workload_lease(lease_id)
        if row is None:
            raise _RpcError(
                code=-32601,
                message="lease_not_found",
                data={"lease_id": lease_id},
            )
        # Auth scope: only the desk that owns the lease can release it.
        # (No principal-override yet; that's the Auditor's bucket-2 path
        # in 2a.3 WL5+.)
        if row["drydock_id"] != caller_drydock_id:
            raise _RpcError(
                code=-32004,
                message="forbidden",
                data={"reason": "lease_belongs_to_other_drydock"},
            )
        if row["status"] != "active":
            # Idempotent re-release is fine; just return the current shape.
            return _release_response(row, results=None)

        applied = json.loads(row["applied_actions_json"])
        results = revert_lease_actions(applied)

        any_failure = any(not r.get("ok") for r in results)
        terminal_status = "partial-revoked" if any_failure else "released"
        registry.mark_workload_lease_revoked(
            lease_id,
            revoke_results=results,
            terminal_status=terminal_status,
        )
        emit_audit(
            "workload.lease_released",
            principal=caller_drydock_id,
            request_id=request_id,
            method="ReleaseWorkload",
            result="ok" if not any_failure else "error",
            details={
                "lease_id": lease_id,
                "terminal_status": terminal_status,
                "results": results,
            },
        )
        # Re-read for response shape (status updated)
        row = registry.get_workload_lease(lease_id)
        return _release_response(row, results=results)
    finally:
        registry.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _validated_register_params(params: dict | list | None) -> dict:
    """Coerce + lightly validate raw RPC params into kwargs for WorkloadSpec.

    Heavy lifting (kind enum, duration bounds, cgroup shape) happens
    in validate_spec — this layer just ensures we have a dict with
    the right key shapes before constructing the dataclass.
    """
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"reason": "expected object"},
        )
    kind = params.get("kind")
    if not isinstance(kind, str):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "kind", "reason": "must be a string"},
        )
    description = params.get("description") or ""
    if not isinstance(description, str):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "description", "reason": "must be a string"},
        )
    expected = params.get("expected") or {}
    if not isinstance(expected, dict):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "expected", "reason": "must be an object"},
        )
    duration = params.get("duration_max_seconds", 3600)
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "duration_max_seconds", "reason": "must be an integer"},
        )
    return {
        "kind": kind,
        "description": description,
        "expected": dict(expected),
        "duration_max_seconds": duration,
    }


def _drydock_by_id(registry: Registry, drydock_id: str) -> Drydock | None:
    """Look up a drydock row by id (the caller's token gives us id, not name)."""
    row = registry._conn.execute(
        "SELECT * FROM drydocks WHERE id = ?", (drydock_id,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_drydock(row)


def _release_response(row: dict, *, results: list[dict] | None) -> dict:
    out = {
        "lease_id": row["id"],
        "drydock_id": row["drydock_id"],
        "status": row["status"],
        "revoked_at": row["revoked_at"],
    }
    if results is not None:
        out["results"] = results
    return out
