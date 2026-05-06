"""ws tailnet — tailnet device-record admin commands.

`ws tailnet prune` enumerates devices on the configured tailnet, matches
them against the drydock drydock registry by hostname, and lists (or
deletes) those that don't correspond to a live drydock.

Useful after force-removed containers, crashes mid-destroy, or to clean
up V1-era ghost records (the original forcing function — see
docs/v2-design-tailnet-identity.md §5.3, §8).
"""

from __future__ import annotations

import logging

import click

from drydock.core import WsError
from drydock.core import tailnet as tailnet_api
from drydock.core.audit import log_event

logger = logging.getLogger(__name__)


def _live_hostnames(registry) -> set[str]:
    """Hostnames currently associated with registry drydocks.

    Each drydock may contribute up to two candidate hostnames: the explicit
    `config.tailscale_hostname` (if set) and `ws.id`. We consider both so a
    device matching either is preserved — prune's job is to remove only
    confirmed orphans.
    """
    hostnames: set[str] = set()
    for ws in registry.list_drydocks():
        hostnames.add(ws.id)
        hostnames.add(ws.name)
        ts = ws.config.get("tailscale_hostname")
        if ts:
            hostnames.add(ts)
    return hostnames


def _classify_candidates(devices: list[dict], live: set[str]) -> list[dict]:
    candidates = []
    for dev in devices:
        hostname = dev.get("hostname", "")
        if hostname in live:
            continue
        if not tailnet_api.DRYDOCK_HOSTNAME_PATTERN.match(hostname):
            continue
        candidates.append(
            {
                "device_id": dev.get("id", ""),
                "hostname": hostname,
                "last_seen": dev.get("lastSeen", ""),
                "reason": "no_matching_desk",
            }
        )
    return candidates


@click.group()
def tailnet():
    """Tailnet device-record admin."""


@tailnet.command("prune")
@click.option("--apply", "apply_", is_flag=True, help="Delete candidates (default: dry-run)")
@click.pass_context
def prune(ctx, apply_):
    """List (or delete) orphan drydock-style tailnet device records."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    creds = tailnet_api.load_admin_credentials()
    if creds is None:
        out.error(
            WsError(
                "No tailscale admin credentials configured",
                fix=(
                    "Generate a Tailscale API token at login.tailscale.com -> "
                    "Settings -> Keys -> Generate API access token, then write "
                    "to ~/.drydock/daemon-secrets/tailscale_admin_token (0400) "
                    "and the tailnet name to ~/.drydock/daemon-secrets/tailscale_tailnet"
                ),
            )
        )
        return
    token, tnet = creds

    try:
        devices = tailnet_api.find_devices(tnet, token)
    except WsError as e:
        out.error(e)
        return

    live = _live_hostnames(registry)
    candidates = _classify_candidates(devices, live)

    deleted: list[str] = []
    errors: list[dict] = []
    if apply_:
        for c in candidates:
            try:
                tailnet_api.delete_tailnet_device(c["device_id"], token)
                deleted.append(c["device_id"])
                log_event(
                    "tailnet.device_deleted",
                    "",
                    extra={"hostname": c["hostname"], "device_id": c["device_id"]},
                )
            except WsError as e:
                errors.append({"device_id": c["device_id"], "hostname": c["hostname"], "error": e.message})
                log_event(
                    "tailnet.device_delete_failed",
                    "",
                    extra={"hostname": c["hostname"], "device_id": c["device_id"], "error": e.message},
                )

    data = {
        "dry_run": not apply_,
        "tailnet": tnet,
        "candidates": candidates,
        "deleted": deleted,
        "errors": errors,
    }
    if apply_:
        human = [f"Deleted {len(deleted)}/{len(candidates)} candidate devices from tailnet {tnet}:"]
        for c in candidates:
            status = "DELETED" if c["device_id"] in deleted else "FAILED"
            human.append(f"  [{status}] {c['hostname']}  id={c['device_id']}  last_seen={c['last_seen']}")
    else:
        human = [f"[dry-run] {len(candidates)} orphan candidate(s) on tailnet {tnet}:"]
        for c in candidates:
            human.append(f"  {c['hostname']}  id={c['device_id']}  last_seen={c['last_seen']}")
        if candidates:
            human.append("Re-run with --apply to delete.")

    out.success(data, human_lines=human)
