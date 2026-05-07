"""Workload-as-transaction primitive.

Phase 2a.3 WL1 of make-the-harness-live.md. A worker about to do
something heavier than its standing allocation declares the workload
ahead of time; the daemon issues a `WorkloadLease` that bundles all
the resource lifts (cgroup, egress shaping, additional reach,
storage) into one atomic grant. At lease expiry / explicit release,
all lifts revert atomically.

This module defines the *types* and the *atomic apply/revert pattern*.
Wiring to the daemon's RPC surface lives in `daemon/workload_handlers.py`.
The cgroup sub-action wraps `core/cgroup.py` (shipped in 2a.2);
future sub-actions (egress, network reach, storage) plug in via the
same `Action` protocol.

The action pattern is a standard transaction script. Each Action knows
how to apply and revert itself. The transaction runs forward through
the list; on any failure, it runs backward through the actions that
already succeeded. The lease persists the list of *applied* actions
so revoke/expiry can replay the inverse — even after a daemon restart.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from drydock.core.cgroup import (
    CgroupUpdateError,
    apply_cgroup_limits,
    revert_cgroup_limits,
)
from drydock.core.resource_ceilings import HardCeilings, ResourceCeilingError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec / Lease types
# ---------------------------------------------------------------------------


@dataclass
class WorkloadSpec:
    """What the worker declares it's about to do.

    `kind` is a short tag (training, crawl, batch, experiment, interactive).
    `expected` is a dict of resource → requested-peak. Recognized keys
    grow over time; WL1 understands cgroup keys (cpu_max, memory_max,
    pids_max). Unknown keys are preserved-but-ignored so future workers
    can include forward-compat fields.

    `duration_max` is an ISO-8601 duration-equivalent in seconds. The
    lease's `expires_at` is computed as `granted_at + duration_max`.
    Default 1h; max 12h (kept narrow until WL5 escalation lands).
    """
    kind: str
    description: str = ""
    expected: dict = field(default_factory=dict)
    duration_max_seconds: int = 3600  # 1h default

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorkloadLease:
    """The structured grant returned by RegisterWorkload."""
    id: str
    drydock_id: str
    spec_json: str           # serialized WorkloadSpec
    applied_actions_json: str  # serialized list of {kind, params, ...}
    granted_at: str          # ISO-8601 UTC
    expires_at: str          # ISO-8601 UTC
    status: str = "active"   # active | released | expired | partial-revoked

    def spec(self) -> WorkloadSpec:
        return WorkloadSpec(**json.loads(self.spec_json))

    def applied_actions(self) -> list[dict]:
        return json.loads(self.applied_actions_json)


class WorkloadValidationError(ValueError):
    """Spec rejected on shape grounds (bad cgroup, duration out of range)."""


class WorkloadApplyError(RuntimeError):
    """An action's `apply` failed; the lease was rolled back atomically.

    Carries the index of the failing action and its underlying error so
    the audit pipeline can record which sub-action broke without
    re-implementing exception parsing per call site.
    """
    def __init__(self, message: str, *, failed_at: int, cause: Exception):
        super().__init__(message)
        self.failed_at = failed_at
        self.cause = cause


# ---------------------------------------------------------------------------
# Action protocol + concrete actions
# ---------------------------------------------------------------------------


class Action(Protocol):
    """Reversible transaction-script step.

    Each concrete Action knows how to apply and revert itself, and how
    to serialize its parameters so a future revoke (after daemon restart)
    can rebuild and replay the inverse.
    """
    kind: str  # discriminator written to the persisted action list

    def apply(self) -> dict:
        """Perform the side effect. Return a dict of post-apply facts to
        persist (e.g., the previous-value the revert needs)."""
        ...

    def revert(self, persisted: dict) -> None:
        """Undo this action using the dict returned by apply()."""
        ...

    def serialize(self) -> dict:
        """Capture parameters for replay after a daemon restart."""
        ...


@dataclass
class CgroupLiftAction:
    """Lift a running container's hard cgroup ceilings.

    Reads the original ceilings from the registry (drydocks.original_resources_hard);
    applies the lifted values via docker update on apply(); restores
    the originals on revert().
    """
    container_id: str
    original: HardCeilings
    lifted: HardCeilings

    kind: str = "cgroup_lift"

    def apply(self) -> dict:
        # Refuse to lift fields that have no original cap to revert to.
        # Docker's update API refuses to clear a once-set memory/cpu/pids
        # limit, so the only way to "revert to unlimited" is container
        # recreate — which defeats the live-lift contract. Force the
        # principal to set standing caps in project YAML before workloads
        # can lift them.
        unsafe = []
        if self.lifted.memory_max is not None and self.original.memory_max is None:
            unsafe.append("memory_max")
        if self.lifted.cpu_max is not None and self.original.cpu_max is None:
            unsafe.append("cpu_max")
        if self.lifted.pids_max is not None and self.original.pids_max is None:
            unsafe.append("pids_max")
        if unsafe:
            raise CgroupUpdateError(
                f"refusing to lift cgroup field(s) {unsafe} on a drydock "
                f"with no original cap — docker can't revert to unlimited "
                f"in place. Set explicit standing caps in project YAML "
                f"resources_hard before lifting.",
                flags=[], stderr="",
            )
        applied_flags = apply_cgroup_limits(self.container_id, self.lifted)
        return {
            "container_id": self.container_id,
            "applied_flags": applied_flags,
            "original": self.original.to_dict(),
            "lifted": self.lifted.to_dict(),
        }

    def revert(self, persisted: dict) -> None:
        # Use the original from the persisted record, not self.original —
        # so a revert path that doesn't have the in-memory action object
        # (lease expiry sweeper after daemon restart) can still execute.
        # Pass `lifted` so revert_cgroup_limits knows to emit docker's
        # "unlimited" sentinels for fields that had no original cap.
        original = HardCeilings.from_dict(persisted.get("original") or {})
        lifted = HardCeilings.from_dict(persisted.get("lifted") or {})
        revert_cgroup_limits(
            persisted["container_id"], original, lifted=lifted,
        )

    def serialize(self) -> dict:
        return {
            "kind": self.kind,
            "container_id": self.container_id,
            "original": self.original.to_dict(),
            "lifted": self.lifted.to_dict(),
        }


def deserialize_action(payload: dict) -> Action:
    """Reconstruct an Action from its serialized form (for replay).

    Used by the lease-expiry sweeper after a daemon restart, where the
    in-memory action objects are gone but the lease record persists.
    """
    kind = payload.get("kind")
    if kind == "cgroup_lift":
        return CgroupLiftAction(
            container_id=payload["container_id"],
            original=HardCeilings.from_dict(payload.get("original") or {}),
            lifted=HardCeilings.from_dict(payload.get("lifted") or {}),
        )
    raise WorkloadValidationError(f"unknown action kind: {kind!r}")


# ---------------------------------------------------------------------------
# Spec validation + lease assembly
# ---------------------------------------------------------------------------


_VALID_KINDS = {"training", "crawl", "batch", "experiment", "interactive"}
_MAX_DURATION_SECONDS = 12 * 3600  # 12h, matches AWS STS cap


def validate_spec(spec: WorkloadSpec) -> WorkloadSpec:
    """Reject malformed specs early.

    Raises WorkloadValidationError on:
    - unknown kind
    - duration out of [1, 12h] range
    - malformed expected.cgroup ceilings (delegated to HardCeilings.from_dict)
    """
    if spec.kind not in _VALID_KINDS:
        raise WorkloadValidationError(
            f"unknown workload kind {spec.kind!r}; expected one of {sorted(_VALID_KINDS)}"
        )
    if not (1 <= spec.duration_max_seconds <= _MAX_DURATION_SECONDS):
        raise WorkloadValidationError(
            f"duration_max_seconds must be in [1, {_MAX_DURATION_SECONDS}]; got {spec.duration_max_seconds}"
        )

    # Expected cgroup keys validate via HardCeilings — same gate as
    # creation-time resources_hard, single source of truth.
    cgroup_keys = {"cpu_max", "memory_max", "pids_max"}
    cgroup_part = {k: v for k, v in spec.expected.items() if k in cgroup_keys}
    if cgroup_part:
        try:
            HardCeilings.from_dict(cgroup_part)
        except ResourceCeilingError as exc:
            raise WorkloadValidationError(
                f"invalid cgroup ceilings in expected: {exc}"
            ) from exc
    return spec


def build_actions_for_spec(
    spec: WorkloadSpec,
    *,
    container_id: str,
    original_resources_hard: dict,
) -> list[Action]:
    """Compose the Action list for a given spec.

    WL1 only knows about cgroup lift. Other sub-actions (egress shape,
    network reach, storage mount) plug in here as they land.

    Returns an empty list if the spec doesn't request any lifts above
    the desk's standing allocation — caller may still record a "lease"
    for audit purposes (workload registered for visibility) without
    needing to apply anything.
    """
    actions: list[Action] = []

    cgroup_keys = {"cpu_max", "memory_max", "pids_max"}
    cgroup_request = {k: spec.expected[k] for k in cgroup_keys if k in spec.expected}
    if cgroup_request:
        original = HardCeilings.from_dict(original_resources_hard or {})
        lifted = HardCeilings.from_dict({**original_resources_hard, **cgroup_request})
        if not _ceilings_equal(original, lifted):
            actions.append(CgroupLiftAction(
                container_id=container_id,
                original=original,
                lifted=lifted,
            ))

    return actions


def _ceilings_equal(a: HardCeilings, b: HardCeilings) -> bool:
    return a.to_dict() == b.to_dict()


# ---------------------------------------------------------------------------
# Atomic apply/revert
# ---------------------------------------------------------------------------


def apply_actions_atomically(actions: list[Action]) -> list[dict]:
    """Apply each action in order; on any failure, revert in reverse.

    Returns a list of {kind, params, persisted} dicts — one per action
    that ended up applied. Suitable for storage in the lease record.

    On any sub-action's apply() raising, this:
    - calls revert() on each previously-applied action in reverse order,
      using the persisted dict each one returned;
    - logs each revert failure (best-effort — we keep going so partial
      cleanup is better than no cleanup);
    - raises WorkloadApplyError naming the failing action.

    A clean apply leaves all actions in effect; the caller persists the
    returned action list to the lease record.
    """
    applied: list[tuple[Action, dict]] = []
    for idx, action in enumerate(actions):
        try:
            persisted = action.apply()
        except Exception as exc:  # noqa: BLE001 — re-raised below with context
            # Roll back any previously-applied actions.
            for prior_action, prior_persisted in reversed(applied):
                try:
                    prior_action.revert(prior_persisted)
                except Exception as revert_exc:  # noqa: BLE001
                    logger.error(
                        "rollback failed for action %s: %s",
                        prior_action.kind, revert_exc,
                    )
            raise WorkloadApplyError(
                f"action {idx} ({action.kind}) failed: {exc}",
                failed_at=idx, cause=exc,
            ) from exc
        applied.append((action, persisted))

    return [
        {**action.serialize(), "persisted": persisted}
        for action, persisted in applied
    ]


def revert_lease_actions(applied_actions: list[dict]) -> list[dict]:
    """Revert each action's effect, in reverse order.

    Best-effort: a failed revert is logged but does not stop subsequent
    reverts. Returns a list of {kind, ok, error?} per attempted revert
    so the audit event can capture the partial-revoke shape.
    """
    results: list[dict] = []
    for entry in reversed(applied_actions):
        try:
            action = deserialize_action(entry)
            action.revert(entry.get("persisted") or entry)
            results.append({"kind": entry.get("kind"), "ok": True})
        except Exception as exc:  # noqa: BLE001
            logger.error("revert failed for action %s: %s", entry.get("kind"), exc)
            results.append({
                "kind": entry.get("kind"),
                "ok": False,
                "error": str(exc),
            })
    return list(reversed(results))  # restore original order for readability


# ---------------------------------------------------------------------------
# Lease assembly
# ---------------------------------------------------------------------------


def new_lease_id() -> str:
    return f"wl_{uuid.uuid4().hex}"


def assemble_lease(
    *,
    drydock_id: str,
    spec: WorkloadSpec,
    applied_actions: list[dict],
    now: Optional[datetime] = None,
) -> WorkloadLease:
    granted = (now or datetime.now(timezone.utc))
    expires = granted + timedelta(seconds=spec.duration_max_seconds)
    return WorkloadLease(
        id=new_lease_id(),
        drydock_id=drydock_id,
        spec_json=json.dumps(spec.to_dict()),
        applied_actions_json=json.dumps(applied_actions),
        granted_at=granted.isoformat(),
        expires_at=expires.isoformat(),
        status="active",
    )


# ---------------------------------------------------------------------------
# Lease-expiry sweeper
# ---------------------------------------------------------------------------


def sweep_expired_leases(registry, *, now: Optional[datetime] = None) -> list[dict]:
    """Find leases past expires_at and revert them.

    For each lease whose ``expires_at`` is before ``now`` (default: utc
    now) and whose status is still ``active``:
    - deserialize applied_actions,
    - call revert_lease_actions (best-effort),
    - mark the lease ``expired`` (or ``partial-revoked`` on failure).

    Returns one summary dict per lease processed::

        [{"lease_id": ..., "drydock_id": ..., "terminal_status": ...,
          "results": [...]}]

    Designed for the daemon's periodic background thread; idempotent
    if no leases are due (returns empty list). Caller is expected to
    emit `workload.lease_expired` audit events from these summaries —
    we don't emit from this module to keep core/ test-friendly without
    needing audit-log fixtures.
    """
    moment = (now or datetime.now(timezone.utc))
    cutoff = moment.isoformat()

    # Direct query rather than going through list_active_workload_leases
    # so we filter by expires_at server-side.
    cur = registry._conn.execute(
        "SELECT * FROM workload_leases "
        "WHERE status = 'active' AND expires_at < ? "
        "ORDER BY expires_at ASC",
        (cutoff,),
    )
    rows = [dict(row) for row in cur.fetchall()]

    summaries: list[dict] = []
    for row in rows:
        try:
            applied = json.loads(row["applied_actions_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error(
                "sweep: lease %s has malformed applied_actions: %s",
                row["id"], exc,
            )
            applied = []

        results = revert_lease_actions(applied)
        any_failure = any(not r.get("ok") for r in results)
        terminal_status = "partial-revoked" if any_failure else "expired"

        registry.mark_workload_lease_revoked(
            row["id"],
            revoke_results=results,
            terminal_status=terminal_status,
        )
        summaries.append({
            "lease_id": row["id"],
            "drydock_id": row["drydock_id"],
            "terminal_status": terminal_status,
            "results": results,
        })

    return summaries
