"""ws secret — manage workspace secrets (file-backed)."""

import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from drydock.core import WsError


def _secrets_root() -> Path:
    return Path.home() / ".drydock" / "secrets"


def _ws_id_for(name: str, registry) -> str:
    ws = registry.get_workspace(name)
    if ws:
        return ws.id
    name_slug = name.replace("-", "_").replace(" ", "_")
    return f"ws_{name_slug}"


def _ws_secret_dir(ws_id: str) -> Path:
    return _secrets_root() / ws_id


@click.group()
def secret():
    """Manage workspace secrets."""


@secret.command("set")
@click.argument("workspace")
@click.argument("key_name")
@click.pass_context
def secret_set(ctx, workspace, key_name):
    """Store a secret for a workspace (value read from stdin)."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    if sys.stdin.isatty():
        click.echo("Enter secret value (then EOF / Ctrl-D):", err=True)

    value = sys.stdin.buffer.read()
    if not value:
        out.error(WsError("No value provided on stdin", fix="Pipe a value: echo -n val | ws secret set WS KEY"))

    ws = registry.get_workspace(workspace)
    if not ws:
        click.echo(f"warning: workspace '{workspace}' not in registry (pre-populating)", err=True)

    ws_id = _ws_id_for(workspace, registry)
    secret_dir = _ws_secret_dir(ws_id)
    secret_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secret_dir, 0o700)

    secret_path = secret_dir / key_name
    secret_path.write_bytes(value)
    os.chmod(secret_path, 0o400)

    out.success(
        {"workspace": workspace, "key": key_name, "path": str(secret_path), "bytes": len(value)},
        human_lines=[f"Secret '{key_name}' stored ({len(value)} bytes)"],
    )


@secret.command("list")
@click.argument("workspace")
@click.pass_context
def secret_list(ctx, workspace):
    """List secret key names for a workspace (never shows values)."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws_id = _ws_id_for(workspace, registry)
    secret_dir = _ws_secret_dir(ws_id)

    if not secret_dir.is_dir():
        out.success(
            {"workspace": workspace, "keys": []},
            human_lines=[
                f"No secrets for workspace '{workspace}'.",
                f"  fix: Run 'ws secret set {workspace} <key>' to add one.",
            ],
        )
        return

    keys = []
    for entry in sorted(secret_dir.iterdir()):
        if entry.is_file():
            st = entry.stat()
            keys.append({
                "name": entry.name,
                "mode": oct(stat.S_IMODE(st.st_mode)),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })

    out.success(
        {"workspace": workspace, "keys": keys},
        human_lines=[f"  {k['name']}  {k['mode']}  {k['size']}B" for k in keys]
        or [f"No secrets for workspace '{workspace}'."],
    )


@secret.command("rm")
@click.argument("workspace")
@click.argument("key_name")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def secret_rm(ctx, workspace, key_name, force):
    """Remove a secret from a workspace."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws_id = _ws_id_for(workspace, registry)
    secret_path = _ws_secret_dir(ws_id) / key_name

    if not secret_path.exists():
        out.success(
            {"workspace": workspace, "key": key_name, "removed": False},
            human_lines=[f"Secret '{key_name}' not found (no-op)."],
        )
        return

    if not force and sys.stdin.isatty():
        click.confirm(f"Remove secret '{key_name}' from workspace '{workspace}'?", abort=True)

    secret_path.unlink()
    out.success(
        {"workspace": workspace, "key": key_name, "removed": True},
        human_lines=[f"Secret '{key_name}' removed."],
    )


@secret.command("push")
@click.argument("workspace")
@click.option("--to", "ssh_host", required=True, help="SSH host to push secrets to")
@click.pass_context
def secret_push(ctx, workspace, ssh_host):
    """Push workspace secrets to a remote host via rsync over SSH."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj.get("dry_run", False)

    ws_id = _ws_id_for(workspace, registry)
    secret_dir = _ws_secret_dir(ws_id)

    if not secret_dir.is_dir():
        out.error(WsError(
            f"No secrets directory for workspace '{workspace}'",
            fix=f"Run 'ws secret set {workspace} <key>' first",
        ))

    remote_path = f"~/.drydock/secrets/{ws_id}/"
    cmd = [
        "rsync", "-a",
        "-e", "ssh",
        f"{secret_dir}/",
        f"{ssh_host}:{remote_path}",
    ]

    mkdir_cmd = ["ssh", ssh_host, f"mkdir -p -m 700 ~/.drydock/secrets/{ws_id}"]

    if dry_run:
        out.success(
            {"workspace": workspace, "ssh_host": ssh_host, "mkdir_cmd": mkdir_cmd, "rsync_cmd": cmd, "dry_run": True},
            human_lines=[f"[dry-run] Would run:", f"  {' '.join(mkdir_cmd)}", f"  {' '.join(cmd)}"],
        )
        return

    subprocess.run(mkdir_cmd, check=True)
    subprocess.run(cmd, check=True)

    out.success(
        {"workspace": workspace, "ssh_host": ssh_host, "synced": True},
        human_lines=[f"Secrets for '{workspace}' pushed to {ssh_host}."],
    )
