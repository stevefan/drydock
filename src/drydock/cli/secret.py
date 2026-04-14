"""ws secret — manage workspace secrets (file-backed)."""

import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import click

from drydock.core import WsError

# ws_id flows into ssh/rsync remote-command strings; anything outside this
# character set would enable command injection on the remote host.
_WS_ID_RE = re.compile(r"^ws_[a-zA-Z0-9_]+$")
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _secrets_root() -> Path:
    return Path.home() / ".drydock" / "secrets"


def _ws_id_for(name: str, registry) -> str:
    ws = registry.get_workspace(name)
    if ws:
        ws_id = ws.id
    else:
        name_slug = name.replace("-", "_").replace(" ", "_")
        ws_id = f"ws_{name_slug}"
    if not _WS_ID_RE.match(ws_id):
        raise WsError(
            f"Unsafe workspace name {name!r} (derived id {ws_id!r} has characters outside [A-Za-z0-9_])",
            fix="Use a workspace name matching [A-Za-z0-9_-] with no whitespace or shell metacharacters",
        )
    return ws_id


def _validate_key_name(key_name: str) -> None:
    if not _KEY_NAME_RE.match(key_name):
        raise WsError(
            f"Invalid secret key name {key_name!r}",
            fix="Use characters from [A-Za-z0-9_.-] only (no slashes, whitespace, or shell metacharacters)",
        )


def _ws_secret_dir(ws_id: str) -> Path:
    return _secrets_root() / ws_id


def _write_secret_atomic(secret_dir: Path, key_name: str, value: bytes) -> Path:
    """Write `value` into `secret_dir/key_name` atomically with mode 0400.

    tempfile.mkstemp creates with mode 0600 + O_EXCL, so nothing ever appears
    at the target path with wider permissions. We chmod to 0400 on the temp
    file and rename into place.
    """
    target = secret_dir / key_name
    fd, tmp_name = tempfile.mkstemp(dir=str(secret_dir), prefix=f".{key_name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
        os.chmod(tmp_name, 0o400)
        os.rename(tmp_name, str(target))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return target


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

    try:
        _validate_key_name(key_name)
    except WsError as e:
        out.error(e)

    if sys.stdin.isatty():
        click.echo("Enter secret value (then EOF / Ctrl-D):", err=True)

    value = sys.stdin.buffer.read()
    if not value:
        out.error(WsError("No value provided on stdin", fix="Pipe a value: echo -n val | ws secret set WS KEY"))

    ws = registry.get_workspace(workspace)
    if not ws:
        click.echo(f"warning: workspace '{workspace}' not in registry (pre-populating)", err=True)

    try:
        ws_id = _ws_id_for(workspace, registry)
    except WsError as e:
        out.error(e)

    secret_dir = _ws_secret_dir(ws_id)
    secret_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secret_dir, 0o700)

    secret_path = _write_secret_atomic(secret_dir, key_name, value)

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

    try:
        ws_id = _ws_id_for(workspace, registry)
    except WsError as e:
        out.error(e)
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

    try:
        _validate_key_name(key_name)
        ws_id = _ws_id_for(workspace, registry)
    except WsError as e:
        out.error(e)
    secret_path = _ws_secret_dir(ws_id) / key_name

    if secret_path.exists() and not force and sys.stdin.isatty():
        click.confirm(f"Remove secret '{key_name}' from workspace '{workspace}'?", abort=True)

    try:
        secret_path.unlink()
        removed = True
    except FileNotFoundError:
        removed = False

    out.success(
        {"workspace": workspace, "key": key_name, "removed": removed},
        human_lines=[f"Secret '{key_name}' {'removed' if removed else 'not found (no-op)'}."],
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

    try:
        ws_id = _ws_id_for(workspace, registry)
    except WsError as e:
        out.error(e)
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
