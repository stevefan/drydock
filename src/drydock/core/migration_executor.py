"""Migration state machine — executes a planned migration.

Phase 2a.4 M1 of make-the-harness-live.md. Composes plan, precheck,
snapshot, stop, mutate, start, verify (with rollback from snapshot
on failure) into one auditable transaction.

Stages, in forward order:
  PRECHECK → DRAIN → SNAPSHOT → STOP → MUTATE → START → VERIFY → CLEANUP

On any stage failure after SNAPSHOT, the executor walks the rollback
path: restore from snapshot, restart container, log failure mode.
A rollback that itself fails leaves the migration in 'failed' status
for principal-level intervention.

Mutate strategies are pluggable by target type. M1 ships:
  - image_bump:     update drydocks.image, regenerate overlay
  - project_reload: re-pin policy from YAML, regenerate overlay
  - schema_migration: NotImplementedError (different shape — needs
    multi-drydock orchestration; lands in M2)

Drain is V0 — `docker stop -t <ttl>` and trust the worker's signal
handling. The structured-drain contract V1 (worker writes status to
a side-channel file) lands in M3.

Each stage emits exactly one audit event:
  drydock.migration_stage  (with details.stage and details.outcome)

Plus the bookend events:
  drydock.migration_started   (at the top of execute())
  drydock.migrated            (success terminal)
  drydock.migration_rolled_back (rollback succeeded)
  drydock.migration_failed    (rollback failed; manual intervention)

The state machine writes ``migrations.current_stage`` at every
transition so the daemon-restart recovery path can detect a stalled
migration and either resume or roll back.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from drydock.core.audit import emit_audit
from drydock.core.migration import (
    ImageBumpTarget,
    MigrationStage,
    MigrationStatus,
    ProjectReloadTarget,
    SchemaMigrationTarget,
)
from drydock.core.snapshot import (
    SnapshotError,
    cleanup_snapshot,
    restore_drydock,
    snapshot_drydock,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class StageOutcome:
    """Per-stage record for the executor's structured return."""
    stage: str
    status: str  # "ok" | "skipped" | "failed"
    detail: dict = field(default_factory=dict)


@dataclass
class MigrationOutcome:
    """What the executor returns after walking the state machine."""
    migration_id: str
    drydock_id: str
    terminal_status: str         # MigrationStatus value
    stages: list[StageOutcome] = field(default_factory=list)
    snapshot_path: Optional[str] = None
    error: Optional[dict] = None  # populated on failure / partial-rollback

    def to_dict(self) -> dict:
        return {
            "migration_id": self.migration_id,
            "drydock_id": self.drydock_id,
            "terminal_status": self.terminal_status,
            "stages": [
                {"stage": s.stage, "status": s.status, "detail": s.detail}
                for s in self.stages
            ],
            "snapshot_path": self.snapshot_path,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StageFailure(RuntimeError):
    """One stage failed — caller decides whether to roll back."""
    def __init__(self, stage: MigrationStage, detail: dict):
        super().__init__(f"stage {stage.value} failed: {detail}")
        self.stage = stage
        self.detail = detail


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@dataclass
class ExecutorConfig:
    """Plumbing the executor needs from its caller (CLI or daemon)."""
    secrets_root: Path
    overlays_root: Path
    migrations_root: Path
    drain_ttl_seconds: int = 60
    docker_bin: Optional[str] = None
    # Optional: caller can provide a custom mutate dispatcher to inject
    # alternative strategies (e.g., for tests). Default uses the M1 set.
    mutate_dispatcher: Optional[Callable] = None


def execute_migration(
    migration_id: str,
    *,
    registry,
    config: ExecutorConfig,
) -> MigrationOutcome:
    """Walk the state machine for one planned migration.

    Reads the migration record, executes stages in order, and writes
    status + current_stage to the migrations table at each transition.
    Returns a MigrationOutcome describing what happened.

    Caller (CLI) handles converting the outcome into user-facing output.
    """
    migration_row = registry.get_migration(migration_id)
    if migration_row is None:
        raise ValueError(f"migration {migration_id!r} not found")
    if migration_row["status"] != "planned":
        raise ValueError(
            f"migration {migration_id!r} has status {migration_row['status']!r}; "
            f"executor expects 'planned'"
        )

    plan = json.loads(migration_row["plan_json"])
    drydock_id = plan["drydock_id"]
    drydock = _drydock_by_id(registry, drydock_id)
    if drydock is None:
        raise ValueError(f"plan references missing drydock {drydock_id!r}")

    # Top-of-execution: flip status, emit started.
    registry.update_migration(migration_id, status="in_progress",
                              current_stage=MigrationStage.PRECHECK.value)
    emit_audit(
        "drydock.migration_started",
        principal=drydock_id,
        request_id=migration_id,
        method="MigrationExecutor",
        result="ok",
        details={"plan": plan},
    )

    outcome = MigrationOutcome(
        migration_id=migration_id,
        drydock_id=drydock_id,
        terminal_status=MigrationStatus.IN_PROGRESS.value,
    )
    snapshot_dir: Optional[Path] = None
    snapshot_taken = False

    try:
        # ---- forward path ----

        _stage(outcome, registry, migration_id, MigrationStage.PRECHECK,
               lambda: _do_precheck(drydock, plan))

        _stage(outcome, registry, migration_id, MigrationStage.DRAIN,
               lambda: _do_drain(drydock, config))

        # SNAPSHOT — capture the rollback target. Sets snapshot_taken so
        # later failures know to roll back.
        snap_result = _stage(
            outcome, registry, migration_id, MigrationStage.SNAPSHOT,
            lambda: _do_snapshot(drydock, migration_id, registry, config),
        )
        snapshot_dir_str = snap_result.detail.get("snapshot_dir")
        snapshot_dir = Path(snapshot_dir_str) if snapshot_dir_str else None
        snapshot_taken = True
        outcome.snapshot_path = snapshot_dir_str
        if snapshot_dir:
            registry.update_migration(migration_id, snapshot_path=str(snapshot_dir))

        _stage(outcome, registry, migration_id, MigrationStage.STOP,
               lambda: _do_stop(drydock, config))

        _stage(outcome, registry, migration_id, MigrationStage.MUTATE,
               lambda: _do_mutate(drydock, plan, registry, config))

        start_result = _stage(
            outcome, registry, migration_id, MigrationStage.START,
            lambda: _do_start(drydock, registry, config),
        )

        # VERIFY semantics depend on what START did. If START skipped
        # (no worktree → no devcontainer up to perform), there's no
        # running container to verify; VERIFY skips too. If START
        # actually resumed, VERIFY confirms post-resume state.
        start_skipped = bool(start_result.detail.get("skipped"))
        _stage(
            outcome, registry, migration_id, MigrationStage.VERIFY,
            lambda: _do_verify(
                drydock, registry, config, start_skipped=start_skipped,
            ),
        )

        _stage(outcome, registry, migration_id, MigrationStage.CLEANUP,
               lambda: _do_cleanup(snapshot_dir))

        # Success terminal.
        outcome.terminal_status = MigrationStatus.COMPLETED.value
        registry.update_migration(
            migration_id,
            status=MigrationStatus.COMPLETED.value,
            current_stage=MigrationStage.CLEANUP.value,
            completed_at=_utc_now(),
        )
        emit_audit(
            "drydock.migrated",
            principal=drydock_id,
            request_id=migration_id,
            method="MigrationExecutor",
            result="ok",
            details={"stages": [s.stage for s in outcome.stages]},
        )
        return outcome

    except StageFailure as exc:
        # Stage failed. If we already took the snapshot, walk rollback.
        outcome.error = {"failed_stage": exc.stage.value, **exc.detail}
        if not snapshot_taken or snapshot_dir is None:
            # Pre-snapshot failure: nothing to roll back, just mark failed.
            outcome.terminal_status = MigrationStatus.FAILED.value
            registry.update_migration(
                migration_id,
                status=MigrationStatus.FAILED.value,
                error_json=json.dumps(outcome.error),
                completed_at=_utc_now(),
            )
            emit_audit(
                "drydock.migration_failed",
                principal=drydock_id,
                request_id=migration_id,
                method="MigrationExecutor",
                result="error",
                details=outcome.error,
            )
            return outcome
        # Post-snapshot failure: walk rollback.
        return _rollback(
            outcome=outcome,
            migration_id=migration_id,
            drydock=drydock,
            registry=registry,
            config=config,
            snapshot_dir=snapshot_dir,
        )


# ---------------------------------------------------------------------------
# Stage runner — uniform audit + status update wrapper
# ---------------------------------------------------------------------------


def _stage(
    outcome: MigrationOutcome,
    registry,
    migration_id: str,
    stage: MigrationStage,
    fn: Callable[[], dict],
) -> StageOutcome:
    """Run one stage, audit the outcome, update current_stage.

    Returns the StageOutcome so callers can read detail (e.g., the
    SNAPSHOT stage returns the snapshot dir path).

    Re-raises StageFailure on stage error so the executor's outer
    try/except can decide rollback vs. fail-fast.
    """
    registry.update_migration(migration_id, current_stage=stage.value)
    try:
        detail = fn() or {}
    except StageFailure:
        raise
    except Exception as exc:
        # Coerce unexpected errors into StageFailure with structured detail.
        logger.exception("migration: stage %s raised", stage.value)
        detail = {"error": str(exc), "error_type": type(exc).__name__}
        emit_audit(
            "drydock.migration_stage",
            principal=outcome.drydock_id,
            request_id=migration_id,
            method="MigrationExecutor",
            result="error",
            details={"stage": stage.value, "outcome": "failed", **detail},
        )
        record = StageOutcome(stage=stage.value, status="failed", detail=detail)
        outcome.stages.append(record)
        raise StageFailure(stage, detail) from exc

    record = StageOutcome(stage=stage.value, status="ok", detail=detail)
    outcome.stages.append(record)
    emit_audit(
        "drydock.migration_stage",
        principal=outcome.drydock_id,
        request_id=migration_id,
        method="MigrationExecutor",
        result="ok",
        details={"stage": stage.value, "outcome": "ok", **detail},
    )
    return record


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


def _do_precheck(drydock, plan: dict) -> dict:
    # M1 precheck is light — the planner already ran the heavy checks.
    # Re-validate that the drydock is in a state we can migrate.
    if drydock.state == "migrating":
        raise StageFailure(
            MigrationStage.PRECHECK,
            {"reason": "drydock_state=migrating"},
        )
    return {"drydock_state": drydock.state}


def _do_drain(drydock, config: ExecutorConfig) -> dict:
    # Drain V0: no worker-side contract yet; rely on docker stop's
    # SIGTERM-with-grace. The container's own signal handlers (or
    # absence thereof) decide what happens. M3 lands the structured
    # drain V1.
    return {"drain_version": "v0", "ttl_seconds": config.drain_ttl_seconds}


def _do_snapshot(drydock, migration_id, registry, config) -> dict:
    try:
        snapshot_dir, manifest = snapshot_drydock(
            drydock,
            migration_id=migration_id,
            registry=registry,
            secrets_root=config.secrets_root,
            overlays_root=config.overlays_root,
            migrations_root=config.migrations_root,
            # M1: skip volume capture by default — image_bump and
            # project_reload don't move volumes; they reattach by name.
            # Volume capture lands when M5 cross-host needs it.
            capture_volumes=False,
            docker_bin=config.docker_bin,
        )
    except SnapshotError as exc:
        raise StageFailure(
            MigrationStage.SNAPSHOT,
            {"reason": "snapshot_failed", "error": str(exc)},
        ) from exc
    return {
        "snapshot_dir": str(snapshot_dir),
        "bytes_total": manifest.bytes_total,
        "manifest_summary": {
            "drydock_id": manifest.drydock_id,
            "captured_at": manifest.captured_at,
            "secrets_present": manifest.secrets_path_in_tarball is not None,
            "overlay_present": manifest.overlay_path_in_tarball is not None,
            "worktree_dirty": (manifest.worktree.is_dirty
                                if manifest.worktree else False),
        },
    }


def _do_stop(drydock, config: ExecutorConfig) -> dict:
    if not drydock.container_id:
        return {"skipped": True, "reason": "no_container_id"}
    docker = config.docker_bin or shutil.which("docker") or "docker"
    res = subprocess.run(
        [docker, "stop", "-t", str(config.drain_ttl_seconds), drydock.container_id],
        capture_output=True, text=True, timeout=config.drain_ttl_seconds + 30,
    )
    if res.returncode != 0:
        # If the container is already gone, that's success-equivalent.
        if "No such container" in (res.stderr or ""):
            return {"skipped": True, "reason": "container_already_gone"}
        raise StageFailure(
            MigrationStage.STOP,
            {"reason": "docker_stop_failed",
             "exit_code": res.returncode,
             "stderr": (res.stderr or "").strip()[:500]},
        )
    return {"container_id": drydock.container_id, "stopped": True}


def _do_mutate(drydock, plan: dict, registry, config: ExecutorConfig) -> dict:
    """Dispatch on target type.

    M1 ships image_bump and project_reload. schema_migration raises
    explicitly so the caller knows it's not yet supported.
    """
    if config.mutate_dispatcher is not None:
        return config.mutate_dispatcher(drydock, plan, registry, config)

    target_kind = plan.get("target_kind")
    target_summary = plan.get("target_summary") or {}

    if target_kind == ImageBumpTarget.kind:
        return _mutate_image_bump(drydock, target_summary, registry, config)
    if target_kind == ProjectReloadTarget.kind:
        return _mutate_project_reload(drydock, registry, config)
    if target_kind == SchemaMigrationTarget.kind:
        raise StageFailure(
            MigrationStage.MUTATE,
            {"reason": "schema_migration_not_implemented",
             "fix": "Schema migrations need multi-drydock orchestration; M2."},
        )
    raise StageFailure(
        MigrationStage.MUTATE,
        {"reason": "unknown_target_kind", "target_kind": target_kind},
    )


def _mutate_image_bump(drydock, target_summary: dict, registry, config) -> dict:
    new_image = target_summary.get("new_image")
    if not new_image:
        raise StageFailure(
            MigrationStage.MUTATE,
            {"reason": "image_bump_missing_new_image"},
        )
    old_image = drydock.image
    # Update registry's image field. Overlay regen happens at START
    # (devcontainer up reads the registry's current image at that point).
    registry.update_drydock(drydock.name, image=new_image)
    return {"image_old": old_image, "image_new": new_image}


def _mutate_project_reload(drydock, registry, config) -> dict:
    """Re-pin policy from the project YAML.

    M1: thin wrapper — the existing `drydock project reload` CLI does
    the heavy lifting; here we mirror its policy-update side without
    invoking the CLI.
    """
    from drydock.core.project_config import load_project_config
    proj = load_project_config(drydock.project)
    if proj is None:
        raise StageFailure(
            MigrationStage.MUTATE,
            {"reason": "project_yaml_missing", "project": drydock.project},
        )
    # Update delegations / policy columns
    delegation_kwargs = {}
    if proj.capabilities:
        delegation_kwargs["capabilities"] = list(proj.capabilities)
    if proj.delegatable_secrets:
        delegation_kwargs["delegatable_secrets"] = list(proj.delegatable_secrets)
    if proj.delegatable_firewall_domains:
        delegation_kwargs["delegatable_firewall_domains"] = list(proj.delegatable_firewall_domains)
    if proj.delegatable_storage_scopes:
        delegation_kwargs["delegatable_storage_scopes"] = list(proj.delegatable_storage_scopes)
    if proj.delegatable_provision_scopes:
        delegation_kwargs["delegatable_provision_scopes"] = list(proj.delegatable_provision_scopes)
    if delegation_kwargs:
        registry.update_desk_delegations(drydock.name, **delegation_kwargs)
    return {
        "project": drydock.project,
        "delegations_updated": list(delegation_kwargs.keys()),
    }


def _do_start(drydock, registry, config: ExecutorConfig) -> dict:
    """Bring the (now-mutated) drydock back up.

    Reuses ``daemon.handlers._resume_desk`` — the same function that
    powers the resume path of ``drydock create <name>`` for a
    suspended desk. It regenerates the overlay (so it picks up
    whatever Mutate just wrote — image tag, policy, etc.), runs
    ``devcontainer up``, and updates the registry to state='running'.

    For migrations that are no-op-on-start (e.g., schema_migration
    that mutates only daemon-level state, not container state) the
    drydock has no container to bring up; we skip cleanly.
    """
    if not drydock.worktree_path:
        return {"skipped": True, "reason": "no_worktree_path"}
    # Re-fetch from the registry to pick up any state changes from MUTATE.
    fresh = registry.get_drydock(drydock.name)
    if fresh is None:
        raise StageFailure(
            MigrationStage.START,
            {"reason": "drydock_disappeared_after_mutate"},
        )

    # Defer the import — daemon.handlers depends on much that's
    # initialized only when the daemon is running. core/ modules avoid
    # the daemon import at module load.
    from drydock.daemon.handlers import _resume_desk
    try:
        result = _resume_desk(fresh, registry=registry, dry_run=False)
    except Exception as exc:  # noqa: BLE001 — coerced to StageFailure below
        raise StageFailure(
            MigrationStage.START,
            {"reason": "resume_failed", "error": str(exc)},
        ) from exc
    return {
        "started": True,
        "container_id": result.get("container_id"),
        "state": result.get("state"),
    }


def _do_verify(
    drydock,
    registry,
    config: ExecutorConfig,
    *,
    start_skipped: bool = False,
) -> dict:
    """Verify the resumed drydock is reachable + in state=running.

    If START was skipped (e.g., no worktree on a synthetic desk used
    in tests/smoke), VERIFY skips too — there's nothing to verify.
    Otherwise, confirm the registry's post-START state is 'running';
    devcontainer up's own health check is the underlying probe.

    For M1, that's the verification surface. A future Auditor-driven
    verification could probe deskwatch + a custom `verification_probes`
    field in project YAML; that's M4.
    """
    if start_skipped:
        return {
            "verified": False,
            "skipped": True,
            "reason": "start_skipped",
        }
    fresh = registry.get_drydock(drydock.name)
    if fresh is None:
        raise StageFailure(
            MigrationStage.VERIFY,
            {"reason": "drydock_disappeared_after_start"},
        )
    if fresh.state != "running":
        raise StageFailure(
            MigrationStage.VERIFY,
            {"reason": "not_running_after_start", "state": fresh.state},
        )
    return {
        "verified": True,
        "drydock_state": fresh.state,
        "container_id": fresh.container_id,
    }


def _do_cleanup(snapshot_dir: Optional[Path]) -> dict:
    """Schedule the snapshot for retention / immediate delete.

    M1 keeps the snapshot in place — operator decides when to delete.
    Future: a retention sweeper deletes after N days, optionally
    archiving to S3 first.
    """
    return {
        "snapshot_retained": snapshot_dir is not None,
        "snapshot_dir": str(snapshot_dir) if snapshot_dir else None,
    }


# ---------------------------------------------------------------------------
# Rollback path
# ---------------------------------------------------------------------------


def _rollback(
    *,
    outcome: MigrationOutcome,
    migration_id: str,
    drydock,
    registry,
    config: ExecutorConfig,
    snapshot_dir: Path,
) -> MigrationOutcome:
    """Restore from snapshot. Walks ROLLBACK stage; updates terminal status."""
    registry.update_migration(migration_id, current_stage=MigrationStage.ROLLBACK.value)
    try:
        manifest = restore_drydock(
            snapshot_dir,
            secrets_root=config.secrets_root,
            overlays_root=config.overlays_root,
            registry=registry,
            docker_bin=config.docker_bin,
            restore_volumes=False,
        )
        # Restore the registry row's mutable fields from the snapshot's row.
        # M1: just the image field (matches what image_bump mutates).
        prior = manifest.registry_row.get("drydocks") or {}
        if "image" in prior:
            registry.update_drydock(drydock.name, image=prior["image"])
    except Exception as exc:
        # Rollback itself failed — terminal status is FAILED.
        logger.exception("migration: rollback failed")
        outcome.terminal_status = MigrationStatus.FAILED.value
        outcome.error = {
            **(outcome.error or {}),
            "rollback_error": str(exc),
            "rollback_error_type": type(exc).__name__,
        }
        registry.update_migration(
            migration_id,
            status=MigrationStatus.FAILED.value,
            error_json=json.dumps(outcome.error),
            completed_at=_utc_now(),
        )
        emit_audit(
            "drydock.migration_failed",
            principal=outcome.drydock_id,
            request_id=migration_id,
            method="MigrationExecutor",
            result="error",
            details=outcome.error,
        )
        outcome.stages.append(StageOutcome(
            stage=MigrationStage.ROLLBACK.value, status="failed",
            detail={"error": str(exc)},
        ))
        return outcome

    # Rollback succeeded.
    outcome.stages.append(StageOutcome(
        stage=MigrationStage.ROLLBACK.value, status="ok", detail={},
    ))
    outcome.terminal_status = MigrationStatus.ROLLED_BACK.value
    registry.update_migration(
        migration_id,
        status=MigrationStatus.ROLLED_BACK.value,
        error_json=json.dumps(outcome.error) if outcome.error else None,
        completed_at=_utc_now(),
    )
    emit_audit(
        "drydock.migration_rolled_back",
        principal=outcome.drydock_id,
        request_id=migration_id,
        method="MigrationExecutor",
        result="ok",
        details={"failed_stage": outcome.error.get("failed_stage")
                 if outcome.error else None},
    )
    return outcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drydock_by_id(registry, drydock_id: str):
    row = registry._conn.execute(
        "SELECT * FROM drydocks WHERE id = ?", (drydock_id,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_drydock(row)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
