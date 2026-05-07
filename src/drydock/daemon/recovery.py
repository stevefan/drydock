"""Crash recovery for `drydock daemon` startup per `docs/v2-design-state.md` §3."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from drydock.core.checkout import DEFAULT_CHECKOUT_BASE
from drydock.core.overlay import remove_overlay
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    completed: int
    rolled_back: int
    unknown_method: int
    migrations_rolled_back: int = 0
    migrations_failed: int = 0


def recover_in_progress(registry_path: Path) -> RecoveryReport:
    registry_path = Path(registry_path)
    if not registry_path.exists():
        raise FileNotFoundError(f"Registry does not exist: {registry_path}")

    registry = Registry(db_path=registry_path)
    completed = 0
    rolled_back = 0
    unknown_method = 0

    try:
        rows = registry._conn.execute(
            """
            SELECT request_id, method, spec_json
            FROM task_log
            WHERE status = 'in_progress'
            ORDER BY created_at ASC
            """
        ).fetchall()

        for row in rows:
            request_id = row["request_id"]
            method = row["method"]
            if method not in {"CreateDesk", "DestroyDesk", "SpawnChild"}:
                outcome = {
                    "code": -32000,
                    "message": "unknown_method_during_recovery",
                    "data": {"method": method},
                }
                _finish_task_log(registry, request_id, "failed", outcome)
                unknown_method += 1
                logger.info(
                    "daemon: recovered request_id=%s method=%s status=failed",
                    request_id,
                    method,
                )
                continue

            spec = _load_spec(row["spec_json"])
            drydock_name = _expected_drydock_name(spec)
            if method == "DestroyDesk":
                from drydock.daemon.handlers import _destroy_tree

                drydock = _expected_destroy_drydock(registry, spec)
                if drydock is None:
                    outcome = {
                        "destroyed": True,
                        "drydock_id": _expected_destroy_desk_id(spec),
                        "recovered": True,
                    }
                    _finish_task_log(registry, request_id, "completed", outcome)
                    completed += 1
                    logger.info(
                        "daemon: recovered request_id=%s method=%s status=completed drydock=%s",
                        request_id,
                        method,
                        _expected_destroy_desk_id(spec),
                    )
                    continue

                cascaded: list[str] = []
                partial_failures = _destroy_tree(
                    drydock,
                    registry=registry,
                    secrets_root=Path.home() / ".drydock" / "secrets",
                    dry_run=False,
                    cascaded=cascaded,
                    visited=set(),
                )
                outcome = {
                    "destroyed": True,
                    "drydock_id": drydock.id,
                    "cascaded": cascaded,
                    "recovered": True,
                }
                status = "completed"
                if partial_failures:
                    outcome["partial_failures"] = partial_failures
                    status = "failed"
                _finish_task_log(registry, request_id, status, outcome)
                if status == "completed":
                    completed += 1
                else:
                    rolled_back += 1
                logger.info(
                    "daemon: recovered request_id=%s method=%s status=%s drydock=%s",
                    request_id,
                    method,
                    status,
                    drydock.name,
                )
                continue

            drydock = registry.get_drydock(drydock_name) if drydock_name else None

            if drydock is not None and drydock.state == "running" and drydock.container_id:
                outcome = _desk_ref(registry, drydock, spec)
                _finish_task_log(registry, request_id, "completed", outcome)
                completed += 1
                logger.info(
                    "daemon: recovered request_id=%s method=%s status=completed drydock=%s",
                    request_id,
                    method,
                    drydock.name,
                )
                continue

            reason = _rollback_partial_create(registry, drydock_name, drydock)
            outcome = {
                "code": -32000,
                "message": "crashed_during_create",
                "data": {"reason": reason},
            }
            _finish_task_log(registry, request_id, "failed", outcome)
            rolled_back += 1
            logger.info(
                "daemon: recovered request_id=%s method=%s status=failed drydock=%s",
                request_id,
                method,
                drydock_name or "<unknown>",
            )
    finally:
        registry.close()

    return RecoveryReport(
        completed=completed,
        rolled_back=rolled_back,
        unknown_method=unknown_method,
    )


def _finish_task_log(registry: Registry, request_id: str, status: str, outcome: object) -> None:
    registry._conn.execute(
        """
        UPDATE task_log
        SET status = ?, outcome_json = ?, completed_at = ?
        WHERE request_id = ?
        """,
        (status, json.dumps(outcome), _utc_now(), request_id),
    )
    registry._conn.commit()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_spec(spec_json: str) -> dict[str, object]:
    try:
        spec = json.loads(spec_json)
    except json.JSONDecodeError:
        return {}
    return spec if isinstance(spec, dict) else {}


def _expected_drydock_name(spec: dict[str, object]) -> str:
    name = spec.get("name")
    if isinstance(name, str) and name:
        return name
    project = spec.get("project")
    if isinstance(project, str) and project:
        return project
    return ""


def _expected_destroy_desk_id(spec: dict[str, object]) -> str:
    name = spec.get("name")
    if isinstance(name, str) and name:
        return Drydock(name=name, project=name, repo_path="").id
    drydock_id = spec.get("drydock_id")
    if isinstance(drydock_id, str) and drydock_id:
        return drydock_id
    return ""


def _expected_destroy_drydock(registry: Registry, spec: dict[str, object]) -> Drydock | None:
    name = spec.get("name")
    if isinstance(name, str) and name:
        return registry.get_drydock(name)

    drydock_id = spec.get("drydock_id")
    if not isinstance(drydock_id, str) or not drydock_id:
        return None
    row = registry._conn.execute(
        "SELECT * FROM drydocks WHERE id = ?",
        (drydock_id,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_drydock(row)


def _workspace_id(drydock_name: str) -> str:
    return Drydock(name=drydock_name, project=drydock_name, repo_path="").id


def _desk_ref(registry: Registry, drydock: Drydock, spec: dict[str, object]) -> dict[str, object]:
    branch = drydock.branch or f"ws/{drydock.name}"
    project = drydock.project
    spec_project = spec.get("project")
    if isinstance(spec_project, str) and spec_project:
        project = spec_project
    result = {
        "drydock_id": drydock.id,
        "name": drydock.name,
        "project": project,
        "branch": branch,
        "state": "running",
        "container_id": drydock.container_id,
        "worktree_path": drydock.worktree_path,
    }
    parent_drydock_id = _drydock_parent_desk_id(registry, drydock.name)
    if parent_drydock_id:
        result["parent_drydock_id"] = parent_drydock_id
    return result


def _rollback_partial_create(
    registry: Registry,
    drydock_name: str,
    drydock: Drydock | None,
) -> str:
    if drydock is None:
        if drydock_name:
            _remove_worktree_best_effort(DEFAULT_CHECKOUT_BASE / _workspace_id(drydock_name))
            _remove_overlay_best_effort(
                Path.home() / ".drydock" / "overlays" / f"{_workspace_id(drydock_name)}.devcontainer.json"
            )
        return "drydock_missing"

    if drydock.worktree_path:
        _remove_worktree_best_effort(Path(drydock.worktree_path))
    else:
        logger.warning("daemon: recovery found no worktree_path for drydock %s", drydock.name)

    overlay_path = drydock.config.get("overlay_path")
    if isinstance(overlay_path, str) and overlay_path:
        _remove_overlay_best_effort(Path(overlay_path))
    else:
        _remove_overlay_best_effort(
            Path.home() / ".drydock" / "overlays" / f"{drydock.id}.devcontainer.json"
        )

    registry.delete_drydock(drydock.name)
    return f"drydock_partial_state:{drydock.state or 'unknown'}"


def _drydock_parent_desk_id(registry: Registry, name: str) -> str | None:
    row = registry._conn.execute(
        "SELECT parent_drydock_id FROM drydocks WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    value = row["parent_drydock_id"]
    return value if isinstance(value, str) and value else None


def recover_in_progress_migrations(registry_path: Path) -> tuple[int, int]:
    """Resolve any migrations stuck in_progress at daemon startup.

    Phase 2a.4 M1 — daemon-restart recovery. If the daemon dies while
    a migration's state machine is mid-walk, the migrations row stays
    `status='in_progress'` with the last-completed stage in
    `current_stage`. The desk could be in any state — half-mutated,
    pre-snapshot, post-stop. Recovery's job is to restore a clean
    end-state.

    V1 strategy:
    - If a snapshot exists at `snapshot_path`: attempt rollback via
      restore_drydock. On success, mark 'rolled_back'. On failure,
      mark 'failed' (operator inspects manually).
    - If no snapshot (failure was pre-SNAPSHOT or snapshot file gone):
      mark 'failed'. There's nothing safe to roll back from; operator
      decides whether the desk's current state is acceptable.

    No "resume in place" path. Resuming a half-stopped state machine
    after an indeterminate time crash is genuinely hard; failing loud
    is the correct conservative behavior.

    Returns ``(rolled_back_count, failed_count)``.
    """
    rolled_back = 0
    failed = 0
    registry = Registry(db_path=registry_path)
    try:
        rows = registry._conn.execute(
            "SELECT id, drydock_id, plan_json, current_stage, snapshot_path "
            "FROM migrations WHERE status = 'in_progress' "
            "ORDER BY created_at ASC"
        ).fetchall()

        for row in rows:
            migration_id = row["id"]
            drydock_id = row["drydock_id"]
            current_stage = row["current_stage"]
            snapshot_path = row["snapshot_path"]

            error_payload: dict = {
                "reason": "daemon_restart_recovery",
                "current_stage": current_stage,
            }

            if snapshot_path and Path(snapshot_path).exists():
                try:
                    _rollback_migration_from_snapshot(
                        registry=registry,
                        drydock_id=drydock_id,
                        snapshot_path=Path(snapshot_path),
                    )
                    registry.update_migration(
                        migration_id,
                        status="rolled_back",
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        error_json=json.dumps(error_payload),
                    )
                    rolled_back += 1
                    logger.info(
                        "daemon: migration %s rolled back on recovery (stage=%s)",
                        migration_id, current_stage,
                    )
                    continue
                except Exception as exc:
                    error_payload["rollback_error"] = str(exc)
                    logger.warning(
                        "daemon: migration %s rollback failed on recovery: %s",
                        migration_id, exc,
                    )

            registry.update_migration(
                migration_id,
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_json=json.dumps(error_payload),
            )
            failed += 1
            logger.info(
                "daemon: migration %s marked failed on recovery (stage=%s, snapshot=%s)",
                migration_id, current_stage, snapshot_path or "none",
            )
    finally:
        registry.close()
    return rolled_back, failed


def _rollback_migration_from_snapshot(
    *,
    registry: Registry,
    drydock_id: str,
    snapshot_path: Path,
) -> None:
    """Restore a migration's pre-mutate state from its snapshot.

    Mirrors the executor's _rollback path. Restores secrets dir +
    overlay file from the snapshot, and reapplies the original
    image field on the registry row. Volume restore is skipped (M1
    semantic — image bumps don't move volumes; cross-host (M5) will
    add volume restore).
    """
    from drydock.core.snapshot import restore_drydock
    secrets_root = Path.home() / ".drydock" / "secrets"
    overlays_root = Path.home() / ".drydock" / "overlays"
    manifest = restore_drydock(
        snapshot_path,
        secrets_root=secrets_root,
        overlays_root=overlays_root,
        registry=registry,
        restore_volumes=False,
    )
    # Restore the registry row's mutable fields from the snapshot's row.
    prior = manifest.registry_row.get("drydocks") or {}
    if "image" in prior:
        # Look up by id since the snapshot stores id, not name.
        row = registry._conn.execute(
            "SELECT name FROM drydocks WHERE id = ?", (drydock_id,),
        ).fetchone()
        if row:
            registry.update_drydock(row["name"], image=prior["image"])


def _remove_worktree_best_effort(path: Path) -> None:
    if not path.exists():
        logger.warning("daemon: recovery worktree path absent: %s", path)
        return
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        logger.warning("daemon: recovery failed to remove worktree path: %s", path)


def _remove_overlay_best_effort(path: Path) -> None:
    if not path.exists():
        logger.warning("daemon: recovery overlay path absent: %s", path)
        return
    try:
        remove_overlay(str(path))
    except FileNotFoundError:
        logger.warning("daemon: recovery overlay path absent: %s", path)
    except Exception as exc:
        logger.warning("daemon: recovery failed to remove overlay %s: %s", path, exc)
