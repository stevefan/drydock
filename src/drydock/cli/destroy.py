"""ws destroy — remove a workspace."""

import logging
from pathlib import Path

import click

from drydock.core.devcontainer import DevcontainerCLI
from drydock.core.errors import WsError
from drydock.core.overlay import remove_overlay
from drydock.core.worktree import remove_worktree

logger = logging.getLogger(__name__)


@click.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Required for destructive operation")
@click.pass_context
def destroy(ctx, name, force):
    """Destroy a workspace and remove its registry entry.

    Requires --force flag. Use --dry-run to preview.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj["dry_run"]

    ws = registry.get_workspace(name)
    if not ws:
        out.error(
            WsError(
                f"Workspace '{name}' not found",
                fix="Run 'ws list' to see available workspaces",
            )
        )
        return

    if dry_run:
        out.success(
            {"dry_run": True, "action": "destroy", "workspace": ws.to_dict()},
            human_lines=[
                f"Would destroy workspace '{name}':",
                f"  Remove registry entry",
                f"  Remove worktree at {ws.worktree_path}" if ws.worktree_path else "",
                f"  Stop container {ws.container_id}" if ws.container_id else "",
            ],
        )
        return

    if not force:
        out.error(
            WsError(
                f"Refusing to destroy '{name}' without --force",
                fix=f"Run: ws destroy {name} --force",
            )
        )
        return

    if ws.state in ("running", "idle", "ready") and ws.container_id:
        devc = DevcontainerCLI()
        try:
            devc.tailnet_logout(container_id=ws.container_id)
        except Exception as exc:
            logger.warning("Failed tailnet logout for %s: %s", name, exc)
        try:
            devc.stop(container_id=ws.container_id)
        except Exception as exc:
            logger.warning("Failed to stop container for %s: %s", name, exc)

    if ws.worktree_path and Path(ws.worktree_path).exists():
        try:
            remove_worktree(ws.repo_path, ws.worktree_path)
        except Exception as exc:
            logger.warning("Failed to remove worktree %s: %s", ws.worktree_path, exc)

    overlay_path = ws.config.get("overlay_path")
    if overlay_path and Path(overlay_path).exists():
        try:
            remove_overlay(overlay_path)
        except Exception as exc:
            logger.warning("Failed to remove overlay %s: %s", overlay_path, exc)

    registry.delete_workspace(name)

    out.success(
        {"destroyed": name},
        human_lines=[f"workspace '{name}' destroyed"],
    )
