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
            if method != "CreateDesk":
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
            workspace = registry.get_workspace(workspace_name) if workspace_name else None

            if workspace is not None and workspace.state == "running" and workspace.container_id:
                outcome = _desk_ref(workspace, spec)
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


def _workspace_id(workspace_name: str) -> str:
    return Workspace(name=workspace_name, project=workspace_name, repo_path="").id


def _desk_ref(workspace: Workspace, spec: dict[str, object]) -> dict[str, object]:
    branch = workspace.branch or f"ws/{workspace.name}"
    project = workspace.project
    spec_project = spec.get("project")
    if isinstance(spec_project, str) and spec_project:
        project = spec_project
    return {
        "desk_id": workspace.id,
        "name": workspace.name,
        "project": project,
        "branch": branch,
        "state": "running",
        "container_id": workspace.container_id,
        "worktree_path": workspace.worktree_path,
    }


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
