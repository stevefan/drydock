"""ws destroy — remove a drydock."""

import logging
from pathlib import Path

import click

from drydock.cli._daemon_client import DaemonRpcError, DaemonUnavailable, call_daemon
from drydock.core.devcontainer import DevcontainerCLI
from drydock.core import WsError
from drydock.core.audit import log_event
from drydock.core.overlay import remove_overlay
from drydock.core.checkout import remove_checkout
from drydock.core import tailnet as tailnet_api

logger = logging.getLogger(__name__)


def _delete_tailnet_device_best_effort(ws):
    creds = tailnet_api.load_admin_credentials()
    if creds is None:
        logger.info("No tailscale admin token configured; skipping device delete for %s", ws.id)
        return
    token, tnet = creds
    hostname = ws.config.get("tailscale_hostname") or ws.id
    try:
        devices = tailnet_api.find_devices(tnet, token)
        device = tailnet_api.find_device_by_hostname(hostname, devices)
        if device is None:
            logger.info("No tailnet device matches hostname %s; nothing to delete", hostname)
            return
        device_id = device.get("id", "")
        tailnet_api.delete_tailnet_device(device_id, token)
        log_event(
            "tailnet.device_deleted",
            ws.id,
            extra={"hostname": hostname, "device_id": device_id},
        )
    except WsError as e:
        logger.warning("Tailnet device delete failed for %s: %s", ws.id, e.message)
        log_event(
            "tailnet.device_delete_failed",
            ws.id,
            extra={"hostname": hostname, "error": e.message},
        )
    except Exception as e:
        logger.warning("Unexpected tailnet delete error for %s: %s", ws.id, e)
        log_event(
            "tailnet.device_delete_failed",
            ws.id,
            extra={"hostname": hostname, "error": str(e)},
        )


@click.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Required for destructive operation")
@click.pass_context
def destroy(ctx, name, force):
    """Destroy a drydock and remove its registry entry.

    Requires --force flag. Use --dry-run to preview.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj["dry_run"]

    if dry_run:
        ws = registry.get_drydock(name)
        if not ws:
            out.error(
                WsError(
                    f"Drydock '{name}' not found",
                    fix="Run 'ws list' to see available drydocks",
                )
            )
            return
        out.success(
            {"dry_run": True, "action": "destroy", "drydock": ws.to_dict()},
            human_lines=[
                f"Would destroy drydock '{name}':",
                "  Remove registry entry",
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

    try:
        logger.info("cli: routing via daemon")
        call_daemon("DestroyDesk", {"name": name, "force": force})
    except DaemonUnavailable:
        logger.info("cli.destroy: daemon unavailable, falling back to direct")
    except DaemonRpcError as exc:
        out.error(_ws_error_from_daemon_error(exc))
    else:
        out.success(
            {"destroyed": name},
            human_lines=[f"drydock '{name}' destroyed"],
        )
        return

    ws = registry.get_drydock(name)
    if not ws:
        out.error(
            WsError(
                f"Drydock '{name}' not found",
                fix="Run 'ws list' to see available drydocks",
            )
        )
        return

    if ws.container_id:
        devc = DevcontainerCLI()
        if ws.state == "running":
            try:
                devc.tailnet_logout(container_id=ws.container_id)
            except Exception as exc:
                logger.warning("Failed tailnet logout for %s: %s", name, exc)
            try:
                devc.stop(container_id=ws.container_id)
            except Exception as exc:
                logger.warning("Failed to stop container for %s: %s", name, exc)
        # Remove even when state is error/suspended/defined — otherwise the
        # next ws create will reuse the stopped container and ignore overlay
        # updates.
        try:
            devc.remove(container_id=ws.container_id)
        except Exception as exc:
            logger.warning("Failed to remove container for %s: %s", name, exc)

    if ws.worktree_path and Path(ws.worktree_path).exists():
        try:
            remove_checkout(ws.repo_path, ws.worktree_path)
        except Exception as exc:
            logger.warning("Failed to remove checkout %s: %s", ws.worktree_path, exc)

    overlay_path = ws.config.get("overlay_path")
    if overlay_path and Path(overlay_path).exists():
        try:
            remove_overlay(overlay_path)
        except Exception as exc:
            logger.warning("Failed to remove overlay %s: %s", overlay_path, exc)

    log_event("drydock.destroyed", ws.id)
    registry.delete_drydock(name)

    # Best-effort tailnet device-record cleanup. Failure here does not roll
    # back destroy — drydock is gone from drydock's state regardless; an
    # orphan tailnet record is recoverable via `ws tailnet prune --apply`.
    # See docs/v2-design-tailnet-identity.md §5.1.
    _delete_tailnet_device_best_effort(ws)

    out.success(
        {"destroyed": name},
        human_lines=[f"drydock '{name}' destroyed"],
    )


def _ws_error_from_daemon_error(err: DaemonRpcError) -> WsError:
    fix = None
    context = {}
    if err.data:
        fix_value = err.data.get("fix")
        if isinstance(fix_value, str):
            fix = fix_value
        context = {key: value for key, value in err.data.items() if key != "fix"}
    return WsError(
        err.message,
        fix=fix,
        context=context,
        code=err.message,
    )
