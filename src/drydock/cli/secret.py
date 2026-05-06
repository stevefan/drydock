"""ws secret — manage drydock secrets (file-backed)."""

import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import click

from drydock.core import CONTAINER_REMOTE_GID, CONTAINER_REMOTE_UID, WsError

# dock_id flows into ssh/rsync remote-command strings; anything outside this
# character set would enable command injection on the remote host.
_DOCK_ID_RE = re.compile(r"^dock_[a-zA-Z0-9_]+$")
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _secrets_root() -> Path:
    return Path.home() / ".drydock" / "secrets"


def _ws_id_for(name: str, registry) -> str:
    ws = registry.get_drydock(name)
    if ws:
        dock_id = ws.id
    else:
        name_slug = name.replace("-", "_").replace(" ", "_")
        dock_id = f"dock_{name_slug}"
    if not _DOCK_ID_RE.match(dock_id):
        raise WsError(
            f"Unsafe drydock name {name!r} (derived id {dock_id!r} has characters outside [A-Za-z0-9_])",
            fix="Use a drydock name matching [A-Za-z0-9_-] with no whitespace or shell metacharacters",
        )
    return dock_id


def _validate_key_name(key_name: str) -> None:
    if not _KEY_NAME_RE.match(key_name):
        raise WsError(
            f"Invalid secret key name {key_name!r}",
            fix="Use characters from [A-Za-z0-9_.-] only (no slashes, whitespace, or shell metacharacters)",
        )


def _ws_secret_dir(dock_id: str) -> Path:
    return _secrets_root() / dock_id


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
        if os.geteuid() == 0:
            os.chown(target, CONTAINER_REMOTE_UID, CONTAINER_REMOTE_GID)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return target


@click.group()
def secret():
    """Manage drydock secrets."""


@secret.command("set")
@click.argument("drydock")
@click.argument("key_name")
@click.pass_context
def secret_set(ctx, drydock, key_name):
    """Store a secret for a drydock (value read from stdin)."""
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

    ws = registry.get_drydock(drydock)
    if not ws:
        click.echo(f"warning: drydock '{drydock}' not in registry (pre-populating)", err=True)

    try:
        dock_id = _ws_id_for(drydock, registry)
    except WsError as e:
        out.error(e)

    secret_dir = _ws_secret_dir(dock_id)
    secret_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secret_dir, 0o700)
    if os.geteuid() == 0:
        os.chown(secret_dir, CONTAINER_REMOTE_UID, CONTAINER_REMOTE_GID)

    secret_path = _write_secret_atomic(secret_dir, key_name, value)

    out.success(
        {"drydock": drydock, "key": key_name, "path": str(secret_path), "bytes": len(value)},
        human_lines=[f"Secret '{key_name}' stored ({len(value)} bytes)"],
    )


@secret.command("list")
@click.argument("drydock")
@click.pass_context
def secret_list(ctx, drydock):
    """List secret key names for a drydock (never shows values)."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    try:
        dock_id = _ws_id_for(drydock, registry)
    except WsError as e:
        out.error(e)
    secret_dir = _ws_secret_dir(dock_id)

    if not secret_dir.is_dir():
        out.success(
            {"drydock": drydock, "keys": []},
            human_lines=[
                f"No secrets for drydock '{drydock}'.",
                f"  fix: Run 'ws secret set {drydock} <key>' to add one.",
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
        {"drydock": drydock, "keys": keys},
        human_lines=[f"  {k['name']}  {k['mode']}  {k['size']}B" for k in keys]
        or [f"No secrets for drydock '{drydock}'."],
    )


@secret.command("rm")
@click.argument("drydock")
@click.argument("key_name")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def secret_rm(ctx, drydock, key_name, force):
    """Remove a secret from a drydock."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    try:
        _validate_key_name(key_name)
        dock_id = _ws_id_for(drydock, registry)
    except WsError as e:
        out.error(e)
    secret_path = _ws_secret_dir(dock_id) / key_name

    if secret_path.exists() and not force and sys.stdin.isatty():
        click.confirm(f"Remove secret '{key_name}' from drydock '{drydock}'?", abort=True)

    try:
        secret_path.unlink()
        removed = True
    except FileNotFoundError:
        removed = False

    out.success(
        {"drydock": drydock, "key": key_name, "removed": removed},
        human_lines=[f"Secret '{key_name}' {'removed' if removed else 'not found (no-op)'}."],
    )


@secret.command("push")
@click.argument("drydock")
@click.option("--to", "ssh_host", required=True, help="SSH host to push secrets to")
@click.pass_context
def secret_push(ctx, drydock, ssh_host):
    """Push drydock secrets to a remote host via rsync over SSH."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj.get("dry_run", False)

    try:
        dock_id = _ws_id_for(drydock, registry)
    except WsError as e:
        out.error(e)
    secret_dir = _ws_secret_dir(dock_id)

    if not secret_dir.is_dir():
        out.error(WsError(
            f"No secrets directory for drydock '{drydock}'",
            fix=f"Run 'ws secret set {drydock} <key>' first",
        ))

    remote_path = f"~/.drydock/secrets/{dock_id}/"
    cmd = [
        "rsync", "-a",
        "-e", "ssh",
        f"{secret_dir}/",
        f"{ssh_host}:{remote_path}",
    ]

    mkdir_cmd = ["ssh", ssh_host, f"mkdir -p -m 700 ~/.drydock/secrets/{dock_id}"]
    # Receiver-side chown after rsync: rsync from a non-root sender can't set
    # remote ownership, so we do it explicitly. Required on Linux receivers
    # where bind-mounts preserve uid; harmless if the receiver is macOS.
    chown_cmd = [
        "ssh", ssh_host,
        f"chown -R {CONTAINER_REMOTE_UID}:{CONTAINER_REMOTE_GID} ~/.drydock/secrets/{dock_id}",
    ]

    if dry_run:
        out.success(
            {"drydock": drydock, "ssh_host": ssh_host, "mkdir_cmd": mkdir_cmd, "rsync_cmd": cmd, "chown_cmd": chown_cmd, "dry_run": True},
            human_lines=[f"[dry-run] Would run:", f"  {' '.join(mkdir_cmd)}", f"  {' '.join(cmd)}", f"  {' '.join(chown_cmd)}"],
        )
        return

    subprocess.run(mkdir_cmd, check=True)
    subprocess.run(cmd, check=True)
    subprocess.run(chown_cmd, check=True)

    out.success(
        {"drydock": drydock, "ssh_host": ssh_host, "synced": True},
        human_lines=[f"Secrets for '{drydock}' pushed to {ssh_host}."],
    )
