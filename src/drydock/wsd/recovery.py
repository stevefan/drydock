"""Crash recovery for `wsd` startup per `docs/v2-design-state.md` §3."""

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
from drydock.core.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    completed: int
    rolled_back: int
    unknown_method: int


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
                    "wsd: recovered request_id=%s method=%s status=failed",
                    request_id,
                    method,
                )
                continue

            spec = _load_spec(row["spec_json"])
            workspace_name = _expected_workspace_name(spec)
            if method == "DestroyDesk":
                from drydock.wsd.handlers import _destroy_tree

                workspace = _expected_destroy_workspace(registry, spec)
                if workspace is None:
                    outcome = {
                        "destroyed": True,
                        "desk_id": _expected_destroy_desk_id(spec),
                        "recovered": True,
                    }
                    _finish_task_log(registry, request_id, "completed", outcome)
                    completed += 1
                    logger.info(
                        "wsd: recovered request_id=%s method=%s status=completed workspace=%s",
                        request_id,
                        method,
                        _expected_destroy_desk_id(spec),
                    )
                    continue

                cascaded: list[str] = []
                partial_failures = _destroy_tree(
                    workspace,
                    registry=registry,
                    secrets_root=Path.home() / ".drydock" / "secrets",
                    dry_run=False,
                    cascaded=cascaded,
                    visited=set(),
                )
                outcome = {
                    "destroyed": True,
                    "desk_id": workspace.id,
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
                    "wsd: recovered request_id=%s method=%s status=%s workspace=%s",
                    request_id,
                    method,
                    status,
                    workspace.name,
                )
                continue

            workspace = registry.get_workspace(workspace_name) if workspace_name else None

            if workspace is not None and workspace.state == "running" and workspace.container_id:
                outcome = _desk_ref(registry, workspace, spec)
                _finish_task_log(registry, request_id, "completed", outcome)
                completed += 1
                logger.info(
                    "wsd: recovered request_id=%s method=%s status=completed workspace=%s",
                    request_id,
                    method,
                    workspace.name,
                )
                continue

            reason = _rollback_partial_create(registry, workspace_name, workspace)
            outcome = {
                "code": -32000,
                "message": "crashed_during_create",
                "data": {"reason": reason},
            }
            _finish_task_log(registry, request_id, "failed", outcome)
            rolled_back += 1
            logger.info(
                "wsd: recovered request_id=%s method=%s status=failed workspace=%s",
                request_id,
                method,
                workspace_name or "<unknown>",
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


def _expected_workspace_name(spec: dict[str, object]) -> str:
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
        return Workspace(name=name, project=name, repo_path="").id
    desk_id = spec.get("desk_id")
    if isinstance(desk_id, str) and desk_id:
        return desk_id
    return ""


def _expected_destroy_workspace(registry: Registry, spec: dict[str, object]) -> Workspace | None:
    name = spec.get("name")
    if isinstance(name, str) and name:
        return registry.get_workspace(name)

    desk_id = spec.get("desk_id")
    if not isinstance(desk_id, str) or not desk_id:
        return None
    row = registry._conn.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (desk_id,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_workspace(row)


def _workspace_id(workspace_name: str) -> str:
    return Workspace(name=workspace_name, project=workspace_name, repo_path="").id


def _desk_ref(registry: Registry, workspace: Workspace, spec: dict[str, object]) -> dict[str, object]:
    branch = workspace.branch or f"ws/{workspace.name}"
    project = workspace.project
    spec_project = spec.get("project")
    if isinstance(spec_project, str) and spec_project:
        project = spec_project
    result = {
        "desk_id": workspace.id,
        "name": workspace.name,
        "project": project,
        "branch": branch,
        "state": "running",
        "container_id": workspace.container_id,
        "worktree_path": workspace.worktree_path,
    }
    parent_desk_id = _workspace_parent_desk_id(registry, workspace.name)
    if parent_desk_id:
        result["parent_desk_id"] = parent_desk_id
    return result


def _rollback_partial_create(
    registry: Registry,
    workspace_name: str,
    workspace: Workspace | None,
) -> str:
    if workspace is None:
        if workspace_name:
            _remove_worktree_best_effort(DEFAULT_CHECKOUT_BASE / _workspace_id(workspace_name))
            _remove_overlay_best_effort(
                Path.home() / ".drydock" / "overlays" / f"{_workspace_id(workspace_name)}.devcontainer.json"
            )
        return "workspace_missing"

    if workspace.worktree_path:
        _remove_worktree_best_effort(Path(workspace.worktree_path))
    else:
        logger.warning("wsd: recovery found no worktree_path for workspace %s", workspace.name)

    overlay_path = workspace.config.get("overlay_path")
    if isinstance(overlay_path, str) and overlay_path:
        _remove_overlay_best_effort(Path(overlay_path))
    else:
        _remove_overlay_best_effort(
            Path.home() / ".drydock" / "overlays" / f"{workspace.id}.devcontainer.json"
        )

    registry.delete_workspace(workspace.name)
    return f"workspace_partial_state:{workspace.state or 'unknown'}"


def _workspace_parent_desk_id(registry: Registry, name: str) -> str | None:
    row = registry._conn.execute(
        "SELECT parent_desk_id FROM workspaces WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    value = row["parent_desk_id"]
    return value if isinstance(value, str) and value else None


def _remove_worktree_best_effort(path: Path) -> None:
    if not path.exists():
        logger.warning("wsd: recovery worktree path absent: %s", path)
        return
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        logger.warning("wsd: recovery failed to remove worktree path: %s", path)


def _remove_overlay_best_effort(path: Path) -> None:
    if not path.exists():
        logger.warning("wsd: recovery overlay path absent: %s", path)
        return
    try:
        remove_overlay(str(path))
    except FileNotFoundError:
        logger.warning("wsd: recovery overlay path absent: %s", path)
    except Exception as exc:
        logger.warning("wsd: recovery failed to remove overlay %s: %s", path, exc)
