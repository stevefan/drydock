"""ws status — per-workspace health overview."""

import subprocess

import click


PROBE_TIMEOUT = 5


def _docker_container_id(worktree_path: str) -> str:
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-q",
                "--filter", f"label=devcontainer.local_folder={worktree_path}",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        return result.stdout.strip().split("\n")[0].strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _probe_tailscale(container_id: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "tailscale", "status"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _probe_supervisor(container_id: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "pgrep", "-f", "start-remote-control"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _probe_firewall(container_id: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "sudo", "iptables", "-L", "OUTPUT"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        return result.returncode == 0 and "DROP" in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def _probe_workspace(ws) -> dict:
    row = {
        "name": ws.name,
        "state": ws.state,
        "container": "not found",
        "tailscale": "unknown",
        "supervisor": "unknown",
        "firewall": "unknown",
    }

    if not ws.worktree_path:
        return row

    # devcontainer CLI labels with the workspace-folder it was given, which is
    # worktree_path + workspace_subdir for sub-project desks.
    from pathlib import Path as _P
    effective_folder = str(
        _P(ws.worktree_path) / ws.workspace_subdir if ws.workspace_subdir else _P(ws.worktree_path)
    )
    cid = _docker_container_id(effective_folder)
    if not cid:
        row["container"] = "not found"
        return row

    row["container"] = "running"
    row["tailscale"] = "joined" if _probe_tailscale(cid) else "disconnected"
    row["supervisor"] = "alive" if _probe_supervisor(cid) else "dead"
    row["firewall"] = "active" if _probe_firewall(cid) else "inactive"
    return row


@click.command()
@click.pass_context
def status(ctx):
    """Show per-workspace health status."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    workspaces = registry.list_workspaces()
    rows = [_probe_workspace(ws) for ws in workspaces]

    out.table(
        rows,
        columns=["name", "state", "container", "tailscale", "supervisor", "firewall"],
    )
