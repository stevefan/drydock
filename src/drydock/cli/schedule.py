"""ws schedule — sync project schedules to host-native cron/launchd."""

from __future__ import annotations

from pathlib import Path

import click

from drydock.core import WsError
from drydock.core.schedule import (
    detect_backend,
    install_cron,
    install_launchd,
    list_installed_cron,
    list_installed_launchd,
    load_schedule,
    remove_cron,
    remove_launchd,
)


@click.group()
def schedule():
    """Manage host-native scheduled jobs for workspaces."""


@schedule.command("sync")
@click.argument("desk")
@click.pass_context
def schedule_sync(ctx, desk):
    """Sync deploy/schedule.yaml from a workspace worktree to host scheduler."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_workspace(desk)
    if ws is None:
        out.error(WsError(
            message=f"Workspace '{desk}' not found in registry",
            fix="Run 'ws list' to see available workspaces.",
        ))
        return

    worktree = Path(ws.worktree_path) if ws.worktree_path else Path(ws.repo_path)
    subdir = ws.workspace_subdir
    if subdir:
        schedule_path = worktree / subdir / "deploy" / "schedule.yaml"
    else:
        schedule_path = worktree / "deploy" / "schedule.yaml"

    try:
        jobs = load_schedule(schedule_path)
    except WsError as e:
        out.error(e)
        return

    backend = detect_backend()
    try:
        if backend == "launchd":
            written = install_launchd(desk, jobs)
            paths = [str(p) for p in written]
        else:
            path = install_cron(desk, jobs)
            paths = [str(path)]
    except WsError as e:
        out.error(e)
        return

    out.success(
        {"desk": desk, "backend": backend, "jobs": len(jobs), "paths": paths},
        human_lines=[
            f"Synced {len(jobs)} job(s) for desk '{desk}' via {backend}:",
            *[f"  {p}" for p in paths],
        ],
    )


@schedule.command("list")
@click.argument("desk")
@click.pass_context
def schedule_list(ctx, desk):
    """List installed schedule entries for a workspace."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_workspace(desk)
    if ws is None:
        out.error(WsError(
            message=f"Workspace '{desk}' not found in registry",
            fix="Run 'ws list' to see available workspaces.",
        ))
        return

    backend = detect_backend()
    if backend == "launchd":
        labels = list_installed_launchd(desk)
        entries = [{"label": l, "type": "launchd"} for l in labels]
    else:
        lines = list_installed_cron(desk)
        entries = [{"line": l, "type": "cron"} for l in lines]

    # Cross-reference with schedule.yaml if available
    worktree = Path(ws.worktree_path) if ws.worktree_path else Path(ws.repo_path)
    subdir = ws.workspace_subdir
    if subdir:
        schedule_path = worktree / subdir / "deploy" / "schedule.yaml"
    else:
        schedule_path = worktree / "deploy" / "schedule.yaml"

    yaml_jobs: list[str] = []
    if schedule_path.exists():
        try:
            jobs = load_schedule(schedule_path)
            yaml_jobs = [j.name for j in jobs]
        except WsError:
            pass  # yaml parse error — just report installed state

    out.success(
        {"desk": desk, "backend": backend, "installed": entries, "yaml_jobs": yaml_jobs},
        human_lines=[
            f"Desk '{desk}' ({backend}):",
            f"  Installed: {len(entries)} entry/entries",
            f"  schedule.yaml: {len(yaml_jobs)} job(s)" + (f" ({', '.join(yaml_jobs)})" if yaml_jobs else ""),
        ],
    )


@schedule.command("remove")
@click.argument("desk")
@click.pass_context
def schedule_remove(ctx, desk):
    """Remove all host-native schedule entries for a workspace."""
    out = ctx.obj["output"]

    backend = detect_backend()
    try:
        if backend == "launchd":
            removed = remove_launchd(desk)
            paths = [str(p) for p in removed]
        else:
            result = remove_cron(desk)
            paths = [str(result)] if result else []
    except WsError as e:
        out.error(e)
        return

    out.success(
        {"desk": desk, "backend": backend, "removed": paths},
        human_lines=[
            f"Removed {len(paths)} schedule entry/entries for desk '{desk}':",
            *[f"  {p}" for p in paths],
        ] if paths else [f"No schedule entries found for desk '{desk}'."],
    )
