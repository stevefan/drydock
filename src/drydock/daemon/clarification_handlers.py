"""RegisterClarification RPC handler (Phase PA3.8).

The single sanctioned channel from worker drydocks to the Auditor's
judgment context. Per memory/project_auditor_isolation_principles
(2026-05-07):

    - Workers should rarely use this — the contract is the primary
      surface; clarification is for the rare contract-gap case.
    - Sanitization happens HERE, at the wire boundary, not at the
      LLM. The Auditor reads only the sanitized record.
    - Every use is a high-weight audit event. Frequent clarifications
      from a single desk are themselves anomalous.

This handler:
  1. Requires a valid bearer token (any scope — workers, not Auditors)
  2. Sanitizes via core/auditor/clarifier.sanitize
  3. Persists the SANITIZED record to the registry's clarifications
     table with a TTL (default 1 hour, max 24 hours)
  4. Emits an audit event with caller + kind + summary

The Auditor itself does NOT call this method — it READS clarifications
from the registry's bind-mounted RO view. One direction only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from drydock.core.audit import emit_audit
from drydock.core.auditor.clarifier import sanitize
from drydock.core.registry import Registry
from drydock.daemon.rpc_common import _RpcError


# TTLs in seconds. Workers ask for an expires_in; we clamp.
_DEFAULT_TTL_SECONDS = 60 * 60       # 1 hour
_MAX_TTL_SECONDS = 60 * 60 * 24      # 24 hours
_MIN_TTL_SECONDS = 60                # 1 minute


def register_clarification(
    params: dict | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
) -> dict:
    """Validate + sanitize + persist a clarification record.

    Returns a small ack dict; doesn't echo the sanitized payload back
    on the wire (caller already has it). The audit event is the
    visible artifact.
    """
    del request_id  # state-mutating but the table is append-only by id;
    # task_log idempotency would only matter if the caller retries with
    # the same request_id. For v1, accept duplicates — clarifications
    # are advisory, not financial.

    if caller_drydock_id is None:
        raise _RpcError(
            code=-32004,
            message="unauthenticated",
            data={"reason": "RegisterClarification requires bearer token"},
        )
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"reason": "params must be an object"},
        )

    result = sanitize(
        kind=params.get("kind"),
        summary=params.get("summary"),
        evidence=params.get("evidence"),
    )
    if not result.ok:
        raise _RpcError(
            code=-32022,
            message="clarification_rejected",
            data={
                "violations": [
                    {"code": v.code, "message": v.message}
                    for v in result.violations
                ],
            },
        )

    # TTL clamp
    expires_in_raw = params.get("expires_in_seconds", _DEFAULT_TTL_SECONDS)
    try:
        expires_in = int(expires_in_raw)
    except (TypeError, ValueError):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "expires_in_seconds", "reason": "must be integer"},
        )
    expires_in = max(_MIN_TTL_SECONDS, min(_MAX_TTL_SECONDS, expires_in))

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)

    sanitized = result.sanitized
    assert sanitized is not None  # mypy hint; ok=True implies sanitized is set

    registry = Registry(db_path=registry_path)
    try:
        clarification_id = registry.insert_clarification(
            drydock_id=caller_drydock_id,
            kind=sanitized.kind,
            summary=sanitized.summary,
            evidence_json=json.dumps(sanitized.evidence) if sanitized.evidence else None,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
        )
    finally:
        registry.close()

    # The audit event is the principle in action — every use is visible.
    emit_audit(
        "auditor.clarification_registered",
        principal=caller_drydock_id,
        request_id=None,
        method="RegisterClarification",
        result="ok",
        details={
            "drydock_id": caller_drydock_id,
            "kind": sanitized.kind,
            "summary": sanitized.summary,
            "expires_at": expires_at.isoformat(),
            "evidence_keys": sorted(sanitized.evidence.keys()),
        },
    )

    return {
        "clarification_id": clarification_id,
        "expires_at": expires_at.isoformat(),
        "expires_in_seconds": expires_in,
    }
