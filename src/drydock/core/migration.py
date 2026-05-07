"""Migration primitive — atomic structural transitions for a Drydock.

Phase 2a.4 of make-the-harness-live.md. Tonight's hetzner deploy was
a bespoke shell sequence of "drain → snapshot → stop → mutate →
restore → start → verify → rollback." This module turns that pattern
into a daemon-driven state machine that any structural change to a
drydock can flow through.

What's M1 (this commit): types, planner (computes the delta), and
``--dry-run`` mode. The state machine runs forward through stages,
each emitting an audit event. M1 ships only the Plan + Pre-check
+ Cleanup stages; Snapshot/Stop/Mutate/Restore/Start/Verify/Rollback
land in subsequent commits.

The design contract: state captures (registry row, secrets, worktree,
volumes) live behind an interface so cross-host migration (M5) can
plug in a different ``StateBackend`` without touching the state machine.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage taxonomy — pinned so the audit-event vocabulary is stable
# ---------------------------------------------------------------------------


class MigrationStage(str, Enum):
    """Each stage of the migration state machine.

    The string values are the tail of the audit-event names emitted at
    each transition (``drydock.migration_stage_<value>``). Keep them
    stable — they're consumer contract.
    """
    PLAN = "plan"
    PRECHECK = "precheck"
    DRAIN = "drain"
    SNAPSHOT = "snapshot"
    STOP = "stop"
    MUTATE = "mutate"
    RESTORE = "restore"
    START = "start"
    VERIFY = "verify"
    ROLLBACK = "rollback"
    CLEANUP = "cleanup"


class MigrationStatus(str, Enum):
    """Terminal outcomes for a migration record."""
    PLANNED = "planned"           # dry-run finished
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"             # rollback also failed; manual intervention


# ---------------------------------------------------------------------------
# Target types — what kind of structural change is being migrated
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageBumpTarget:
    """Bump the desk's image tag and recreate. The high-frequency case.

    Most migrations on a stable Harbor are this shape: drydock-base
    rolled to a new tag, project's `image:` field updated, container
    needs to be recreated against the new image with worktree + secrets
    + volumes intact.
    """
    new_image: str
    kind: str = "image_bump"


@dataclass(frozen=True)
class ProjectReloadTarget:
    """Re-pin the desk's policy from the (potentially-edited) project YAML.

    Non-image structural changes: firewall, mounts, env, port forwards.
    Today this is `drydock project reload && drydock stop && drydock
    create`. The migration primitive lets us do all three in one
    audited transaction with rollback.
    """
    kind: str = "project_reload"


@dataclass(frozen=True)
class SchemaMigrationTarget:
    """Daemon registry version bump.

    Tonight's hetzner deploy was this shape. Schema migrations need
    coordination across all desks (stop everything, run the migration,
    restart everything) — this target wraps that orchestration.
    """
    target_schema_version: int
    kind: str = "schema_migration"


MigrationTarget = ImageBumpTarget | ProjectReloadTarget | SchemaMigrationTarget


# ---------------------------------------------------------------------------
# Plan — the structured delta produced by the planner
# ---------------------------------------------------------------------------


@dataclass
class MigrationPlan:
    """What the migration will do, what's at risk, and what to expect.

    Produced by ``plan_migration`` from a source state + target spec.
    Printed by ``--dry-run``. Persisted to the migrations table as
    the head record for the in-flight migration.
    """
    migration_id: str
    drydock_id: str
    drydock_name: str
    source_harbor: str
    target_harbor: str
    target_kind: str             # ImageBumpTarget.kind etc.
    target_summary: dict          # serialized target — what's actually changing
    changes: dict                 # human-readable diff: {"image": "v1.0.18 → v1.0.19"}
    estimated_downtime_seconds: int
    in_flight_lease_warnings: list[str]
    rollback_strategy: str = "snapshot-and-restore"
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    def human_summary(self) -> list[str]:
        """Multi-line human-readable summary for `--dry-run` output."""
        lines = [
            f"Migration plan {self.migration_id}",
            f"  drydock: {self.drydock_name} ({self.drydock_id})",
            f"  harbor:  {self.source_harbor} → {self.target_harbor}",
            f"  target:  {self.target_kind}",
        ]
        if self.changes:
            lines.append("  changes:")
            for key, value in self.changes.items():
                lines.append(f"    {key}: {value}")
        lines.append(f"  estimated downtime: {self.estimated_downtime_seconds}s")
        if self.in_flight_lease_warnings:
            lines.append("  warnings:")
            for w in self.in_flight_lease_warnings:
                lines.append(f"    ⚠ {w}")
        lines.append(f"  rollback: {self.rollback_strategy}")
        return lines


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class MigrationPlanError(ValueError):
    """Plan refused outright — target is invalid or incompatible with source."""


def new_migration_id() -> str:
    return f"mig_{uuid.uuid4().hex[:16]}"


def plan_migration(
    *,
    drydock,                      # core.runtime.Drydock
    target: MigrationTarget,
    source_harbor: str,
    target_harbor: Optional[str] = None,
    in_flight_workload_leases: Optional[list[dict]] = None,
) -> MigrationPlan:
    """Produce a structured plan from source state + target spec.

    Raises MigrationPlanError if the target is invalid (e.g.,
    ImageBumpTarget with the same image as current — no-op refused
    explicitly; schema migration to an older version refused).

    Same-host migrations leave ``target_harbor`` as ``source_harbor``.
    Cross-host (M5+) sets a different value.
    """
    target_harbor = target_harbor or source_harbor

    changes: dict = {}
    estimated_downtime = 30  # default for stop+recreate cycle
    warnings: list[str] = []

    if isinstance(target, ImageBumpTarget):
        current_image = drydock.image or "(none)"
        if current_image == target.new_image:
            raise MigrationPlanError(
                f"image bump no-op: drydock '{drydock.name}' already on "
                f"{target.new_image!r}; nothing to do"
            )
        changes["image"] = f"{current_image} → {target.new_image}"
        changes["overlay"] = "regenerate (image change)"

    elif isinstance(target, ProjectReloadTarget):
        changes["overlay"] = "regenerate from current project YAML"
        changes["registry_config"] = "re-pin policy"

    elif isinstance(target, SchemaMigrationTarget):
        # Schema migrations don't touch the container per se; they touch
        # the daemon's registry. Estimated downtime is "long" because
        # all desks pause during the daemon restart.
        changes["registry_schema"] = f"→ V{target.target_schema_version}"
        estimated_downtime = 120
    else:
        raise MigrationPlanError(f"unknown target type: {type(target).__name__}")

    # In-flight workload leases — warn if any expire after a normal
    # drain TTL (60s default). M1 doesn't auto-cancel them; that's
    # the principal's choice.
    leases = in_flight_workload_leases or []
    for lease in leases:
        if lease.get("expires_at"):
            warnings.append(
                f"active WorkloadLease {lease['id']} (expires {lease['expires_at']})"
            )

    return MigrationPlan(
        migration_id=new_migration_id(),
        drydock_id=drydock.id,
        drydock_name=drydock.name,
        source_harbor=source_harbor,
        target_harbor=target_harbor,
        target_kind=target.kind,
        target_summary=_serialize_target(target),
        changes=changes,
        estimated_downtime_seconds=estimated_downtime,
        in_flight_lease_warnings=warnings,
    )


def _serialize_target(target: MigrationTarget) -> dict:
    """Capture the target as a dict for persistence + audit.

    Each target type is a frozen dataclass; we serialize all its fields
    so the migrations table row carries the full structured intent.
    """
    return {**asdict(target)}


# ---------------------------------------------------------------------------
# Pre-check
# ---------------------------------------------------------------------------


@dataclass
class PreCheckResult:
    """Outcome of stage 2 (Pre-check). Hard refuses block the migration;
    warnings can be bypassed with ``--force``."""
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals

    def to_dict(self) -> dict:
        return asdict(self)


def precheck_migration(
    *,
    drydock,
    plan: MigrationPlan,
    disk_free_bytes: Optional[int] = None,
    daemon_healthy: bool = True,
    target_image_present: Optional[bool] = None,
) -> PreCheckResult:
    """Run hard checks against the source state before executing.

    Hard refuses (block migration entirely):
    - Daemon not healthy on the target Harbor.
    - Drydock not in a recoverable state (currently in another migration).
    - Target image declared but missing locally (image bump only).

    Warnings (proceed with --force):
    - In-flight workload leases that would be cut short.
    - Disk space tight for the snapshot tarball.
    """
    result = PreCheckResult()

    if not daemon_healthy:
        result.refusals.append("daemon health check failed on target Harbor")

    if drydock.state == "migrating":
        result.refusals.append(
            f"drydock '{drydock.name}' has state='migrating' — finish or "
            f"rollback the prior migration before starting another"
        )

    if isinstance_image_bump(plan.target_kind) and target_image_present is False:
        result.refusals.append(
            f"target image not present locally ({plan.target_summary.get('new_image')!r}); "
            f"docker pull it first or unpin to fetch on demand"
        )

    # Disk-space check is a soft warning by default — exact tarball size
    # depends on volume contents we haven't measured yet.
    if disk_free_bytes is not None and disk_free_bytes < 1_000_000_000:
        result.warnings.append(
            f"low disk free ({disk_free_bytes // 1_000_000} MB); snapshot may not fit"
        )

    if plan.in_flight_lease_warnings:
        result.warnings.extend(plan.in_flight_lease_warnings)

    return result


def isinstance_image_bump(target_kind: str) -> bool:
    return target_kind == "image_bump"
