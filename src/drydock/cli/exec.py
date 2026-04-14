"""ws exec — shell into a running workspace container."""

import json
import os
import subprocess

import click

from drydock.core import WsError


def _find_container_id(worktree_path: str) -> str:
    result = subprocess.run(
        [
            "docker", "ps", "-q",
            "--filter", f"label=devcontainer.local_folder={worktree_path}",
        ],
        capture_output=True,
        text=True,
    )
    container_id = result.stdout.strip().split("\n")[0].strip()
    return container_id


def _read_workspace_folder(overlay_path: str) -> str:
    try:
        with open(overlay_path) as f:
            data = json.load(f)
        return data.get("workspaceFolder", "/workspace")
    except (OSError, json.JSONDecodeError):
        return "/workspace"


@click.command(name="exec", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def exec_cmd(ctx, name, cmd):
    """Execute a command in a running workspace container."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_workspace(name)
    if not ws:
        out.error(
            WsError(
                f"Workspace '{name}' not found",
                fix="Run 'ws list' to see available workspaces",
            )
        )
        return

    if ws.state != "running":
        out.error(
            WsError(
                f"Workspace '{name}' is not running (state: {ws.state})",
                fix=f"Run 'ws create {ws.project} {name}' to start it",
            )
        )
        return

    overlay_path = ws.config.get("overlay_path", "")
    if overlay_path:
        workdir = _read_workspace_folder(overlay_path)
    else:
        workdir = "/workspace"

    container_id = _find_container_id(ws.worktree_path)
    if not container_id:
        out.error(
            WsError(
                f"No running container found for workspace '{name}'",
                fix=f"The container may have stopped. Run 'ws inspect {name}' to check status",
            )
        )
        return

    command = list(cmd) if cmd else ["bash"]
    os.execvp("docker", ["docker", "exec", "-it", "-w", workdir, container_id] + command)
