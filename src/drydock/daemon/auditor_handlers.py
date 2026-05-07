"""Bucket-2 action RPCs the Port Auditor invokes.

Phase PA3 of port-auditor.md. The Auditor's deep-analysis pipeline
emits ``recommended_action`` strings (``stop_dock``, ``revoke_lease``,
``throttle_egress``, ``freeze_storage``) — until now those were just
strings written to the deep-analysis log. PA3 lights up the path
that actually invokes them.

The structural shape:
- The Auditor desk's bearer token has scope='auditor' (set by
  ``drydock auditor designate <name>``).
- This module's RPC handlers REQUIRE that scope; a dock-scoped token
  raises -32004 forbidden. The check is structural — Bucket-3 RPCs
  (DestroyDesk, anything that writes to daemon-secrets/) are simply
  never registered with auditor-scope acceptance, so an Auditor token
  cannot reach them at all.
- Each handler runs in **dry-run mode by default**. The call is
  validated, audited (``auditor.action_dry_run`` event with full
  context), and returns success — but the underlying primitive
  (``stop_desk``, ``release_capability``, ``tc qdisc add``) is not
  invoked. Flip ``AUDITOR_LIVE_ACTIONS=1`` in the daemon env to
  enable live execution.

The dry-run-by-default stance is the trust ramp: judgment quality
gets validated against real recommendations + observed actuals
before we let the Auditor pull triggers. When confidence is high,
flip the flag.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from drydock.core.audit import emit_audit
from drydock.core.registry import Registry
from drydock.daemon.rpc_common import _RpcError

logger = logging.getLogger(__name__)


# Action-kind taxonomy. Adding a new Bucket-2 action means appending
# a kind here + handling it in _execute_live (when live-actions enabled).
VALID_ACTION_KINDS = (
    "stop_dock",
    "revoke_lease",
    "throttle_egress",
    "freeze_storage",
)


def auditor_action(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict:
    """Single entry point for all Bucket-2 actions.

    Wire shape::

        AuditorAction {
          kind: "stop_dock" | "revoke_lease" | "throttle_egress" | "freeze_storage"
          target_drydock_id: <id>            # for stop_dock, throttle, freeze
          lease_id: <id>                     # for revoke_lease
          reason: <free-form prose>          # principal-readable rationale
          evidence: { ... }                  # structured observations
        }

    The ``reason`` and ``evidence`` are persisted in audit so the
    principal can review the Auditor's judgment after the fact. This
    is the audit-asymmetry property from harbor-authority.md §5: the
    Auditor's actions are structurally observable to the principal.
    """
    if caller_drydock_id is None:
        raise _RpcError(code=-32004, message="unauthenticated")

    # Validate scope: caller's token must be auditor-scoped.
    registry = Registry(db_path=registry_path)
    try:
        auditor_id = registry.get_auditor_drydock_id()
        if auditor_id is None or auditor_id != caller_drydock_id:
            emit_audit(
                "auditor.action_refused",
                principal=caller_drydock_id,
                request_id=request_id,
                method="AuditorAction",
                result="error",
                details={
                    "reason": "caller_not_auditor",
                    "caller_drydock_id": caller_drydock_id,
                    "designated_auditor_id": auditor_id,
                },
            )
            raise _RpcError(
                code=-32020,  # auditor_scope_required
                message="auditor_scope_required",
                data={
                    "fix": "This RPC requires an auditor-scoped bearer "
                           "token. Run `drydock auditor designate <name>` "
                           "to grant the scope to a drydock.",
                },
            )

        spec = _validate_params(params)

        # Compose the structured "what this would have done" record,
        # used identically by both dry-run and live paths so audit
        # consumers see one consistent shape.
        outcome: dict = {
            "kind": spec["kind"],
            "target_drydock_id": spec.get("target_drydock_id"),
            "lease_id": spec.get("lease_id"),
            "reason": spec["reason"],
            "evidence": spec.get("evidence", {}),
            "execution_mode": "dry_run" if dry_run else "live",
        }

        if dry_run:
            emit_audit(
                "auditor.action_dry_run",
                principal=caller_drydock_id,
                request_id=request_id,
                method="AuditorAction",
                result="ok",
                details=outcome,
            )
            outcome["executed"] = False
            outcome["note"] = (
                "AUDITOR_LIVE_ACTIONS not set — call audited, primitive "
                "not invoked. Set the env var to enable live execution."
            )
            return outcome

        # Live mode — invoke the underlying primitive.
        live_result = _execute_live(
            spec,
            registry=registry,
            secrets_root=secrets_root,
        )
        outcome.update(live_result)
        emit_audit(
            "auditor.action_executed",
            principal=caller_drydock_id,
            request_id=request_id,
            method="AuditorAction",
            result="ok",
            details=outcome,
        )
        outcome["executed"] = True
        return outcome
    finally:
        registry.close()


def is_live_actions_enabled() -> bool:
    """Read AUDITOR_LIVE_ACTIONS at call time so daemon restarts pick up
    env changes without code redeploy."""
    return os.getenv("AUDITOR_LIVE_ACTIONS", "0") == "1"


def _validate_params(params: object) -> dict:
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"reason": "expected object"},
        )
    kind = params.get("kind")
    if not isinstance(kind, str) or kind not in VALID_ACTION_KINDS:
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={
                "field": "kind",
                "expected_one_of": list(VALID_ACTION_KINDS),
            },
        )
    reason = params.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "reason", "reason": "must be a non-empty string"},
        )

    target = params.get("target_drydock_id")
    lease_id = params.get("lease_id")
    # stop_dock / throttle_egress / freeze_storage need a target drydock.
    # revoke_lease needs a lease_id.
    if kind in ("stop_dock", "throttle_egress", "freeze_storage"):
        if not isinstance(target, str) or not target:
            raise _RpcError(
                code=-32602, message="invalid_params",
                data={"field": "target_drydock_id",
                      "reason": f"required for kind={kind}"},
            )
    if kind == "revoke_lease":
        if not isinstance(lease_id, str) or not lease_id:
            raise _RpcError(
                code=-32602, message="invalid_params",
                data={"field": "lease_id",
                      "reason": "required for kind=revoke_lease"},
            )

    evidence = params.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "evidence", "reason": "must be an object"},
        )

    return {
        "kind": kind,
        "target_drydock_id": target,
        "lease_id": lease_id,
        "reason": reason,
        "evidence": evidence,
    }


def _execute_live(
    spec: dict,
    *,
    registry: Registry,
    secrets_root: Path,
) -> dict:
    """Invoke the underlying primitive for ``spec``.

    Each kind dispatches to the existing primitive (stop_desk,
    release_capability, etc.) or raises NotImplementedError if that
    primitive doesn't yet exist (throttle_egress + freeze_storage are
    sketched in port-auditor.md but the underlying mechanism hasn't
    landed; PA4+).
    """
    kind = spec["kind"]
    if kind == "stop_dock":
        return _live_stop_dock(spec, registry=registry, secrets_root=secrets_root)
    if kind == "revoke_lease":
        return _live_revoke_lease(spec, registry=registry, secrets_root=secrets_root)
    if kind in ("throttle_egress", "freeze_storage"):
        # Primitive not built yet. Refuse loudly rather than no-op silently.
        raise _RpcError(
            code=-32021,  # auditor_action_unsupported
            message="auditor_action_unsupported",
            data={
                "kind": kind,
                "fix": "The underlying primitive isn't built yet. "
                       "Stop and revoke_lease work in live mode; "
                       "throttle and freeze land in PA4+.",
            },
        )
    raise _RpcError(
        code=-32602, message="invalid_params",
        data={"field": "kind", "reason": f"unhandled kind {kind!r}"},
    )


def _live_stop_dock(spec, *, registry, secrets_root) -> dict:
    """Wrap daemon.handlers.stop_desk for the auditor's stop action.

    The Auditor doesn't pass through the normal stop_desk RPC because
    that takes a request_id + caller_drydock_id — neither makes sense
    when the daemon is invoking on the Auditor's behalf. We call the
    underlying lifecycle primitive directly.
    """
    target = spec["target_drydock_id"]
    row = registry._conn.execute(
        "SELECT name FROM drydocks WHERE id = ?", (target,),
    ).fetchone()
    if row is None:
        raise _RpcError(
            code=-32601, message="drydock_not_found",
            data={"target_drydock_id": target},
        )
    drydock_name = row["name"]
    from drydock.daemon.handlers import stop_desk
    result = stop_desk(
        {"name": drydock_name},
        request_id=None,  # internal call
        caller_drydock_id=None,
        registry_path=Path(registry.db_path),
        dry_run=False,
    )
    return {"primitive": "stop_desk", "primitive_result": result}


def _live_revoke_lease(spec, *, registry, secrets_root) -> dict:
    """Wrap capability_handlers.release_capability for the auditor's
    revoke_lease action."""
    lease_id = spec["lease_id"]
    from drydock.daemon.capability_handlers import release_capability
    result = release_capability(
        {"lease_id": lease_id},
        request_id=None,
        caller_drydock_id=None,  # auditor scope; bypass owner check
        registry_path=Path(registry.db_path),
        secrets_root=secrets_root,
    )
    return {"primitive": "release_capability", "primitive_result": result}
