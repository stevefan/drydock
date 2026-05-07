"""ws attach — open an editor attached to a running drydock."""

import json
import shutil
import subprocess

import click

from drydock.core import WsError


def _find_container(worktree_path: str) -> str:
    # devcontainer CLI labels containers with devcontainer.local_folder=<workspace-folder>,
    # which is the path passed via --workspace-folder — i.e. the worktree path in drydock's
    # case (not the overlay path).
    result = subprocess.run(
        [
            "docker", "ps", "-q",
            "--filter", f"label=devcontainer.local_folder={worktree_path}",
        ],
        capture_output=True,
        text=True,
    )
    container_id = result.stdout.strip().split("\n")[0].strip()
    if not container_id:
        return ""
    name_result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Name}}", container_id],
        capture_output=True,
        text=True,
    )
    return name_result.stdout.strip().lstrip("/")


def _read_workspace_folder(overlay_path: str) -> str:
    try:
        with open(overlay_path) as f:
            data = json.load(f)
        return data.get("workspaceFolder", "/drydock")
    except (OSError, json.JSONDecodeError):
        return "/drydock"


def _hex_encode(name: str) -> str:
    return "".join(f"{b:02x}" for b in name.encode("utf-8"))


@click.command()
@click.argument("name")
@click.option("--editor", default="code", help="Editor binary (code, cursor, code-insiders)")
@click.pass_context
def attach(ctx, name, editor):
    """Attach an editor to a running drydock."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_drydock(name)
    if not ws:
        out.error(
            WsError(
                f"Drydock '{name}' not found",
                fix="Run 'ws list' to see available drydocks",
            )
        )
        return

    if ws.state != "running":
        out.error(
            WsError(
                f"Drydock '{name}' is not running (state: {ws.state})",
                fix=f"Run 'ws create {ws.project} {name}' to start it",
            )
        )
        return

    overlay_path = ws.config.get("overlay_path", "")
    if not overlay_path:
        out.error(
            WsError(
                f"Drydock '{name}' has no overlay_path in config",
                fix="Drydock may have been created with an older version of ws",
            )
        )
        return

    folder = _read_workspace_folder(overlay_path)
    # devcontainer CLI labels containers with the workspace-folder it was given
    # (worktree_path + workspace_subdir for sub-project desks).
    from pathlib import Path as _P
    effective_workspace_folder = str(
        _P(ws.worktree_path) / ws.workspace_subdir if ws.workspace_subdir else _P(ws.worktree_path)
    )
    container_name = _find_container(effective_workspace_folder)
    if not container_name:
        out.error(
            WsError(
                f"No running container found for drydock '{name}'",
                fix="The container may have stopped. Run 'ws inspect {name}' to check status",
            )
        )
        return

    hex_name = _hex_encode(container_name)
    uri = f"vscode-remote://attached-container+{hex_name}{folder}"

    if not shutil.which(editor):
        out.error(
            WsError(
                f"Editor '{editor}' not found on PATH",
                fix="Install the shell command: VS Code Command Palette -> "
                    "'Shell Command: Install code command in PATH'. "
                    "Or pass --editor <your-binary>.",
            )
        )
        return

    subprocess.Popen([editor, "--folder-uri", uri])

    out.success(
        {"uri": uri, "editor": editor, "drydock": name, "container": container_name},
        human_lines=[f"Opening {name} in {editor}..."],
    )
