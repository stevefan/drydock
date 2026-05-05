"""ws fleet — central observer for peer Harbors.

V1 surface (one-shot probes only; polling/storage/alerts come later):

* ``ws fleet status``                       — probe every peer, roll up health.
* ``ws fleet probe <host> [<desk>]``        — probe one peer (one desk if given).

Peer config: ``~/.drydock/fleet/peers.yaml``. See docs/design/fleet-monitor.md.

Channel: SSH-shell to each peer's ``ws`` CLI. Auth: existing key-based SSH.
Probe set: daemon ping → desk listing → per-desk deskwatch + Claude-Code
liveness. Exits 1 if any peer or desk is unhealthy (cron-friendly).
"""

from __future__ import annotations

import click

from drydock.core import WsError
from drydock.core.fleet import (
    PeerSpec,
    load_peers,
    probe_cc_liveness,
    probe_daemon,
    probe_desks,
    probe_deskwatch,
    probe_peer,
    resolve_desks,
    rollup,
)


def _format_status_human(payload: dict) -> list[str]:
    lines: list[str] = []
    for p in payload["peers"]:
        mark = "✓" if p["healthy"] else ("⚠" if p["reachable"] else "✗")
        lines.append(f"{mark} {p['peer']}  ({'reachable' if p['reachable'] else 'UNREACHABLE'})")
        # peer-level probes (daemon, desks)
        for probe in p["probes"]:
            if probe["desk"] is None:
                m = "✓" if probe["status"] == "ok" else "✗"
                lines.append(
                    f"    [{m}] {probe['kind']}  {probe['detail']}  ({probe['elapsed_ms']}ms)"
                )
        for d in p["desks"].values():
            dmark = "✓" if d["healthy"] else "✗"
            lines.append(f"  {dmark} {d['desk']}")
            for c in d["checks"]:
                m = "✓" if c["status"] == "ok" else "✗"
                lines.append(f"      [{m}] {c['kind']}: {c['detail']}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    lines.append(f"overall: {'HEALTHY' if payload['healthy'] else 'UNHEALTHY'}")
    return lines


@click.group()
def fleet():
    """Cross-Harbor health observer."""


@fleet.command(name="status")
@click.pass_context
def status(ctx):
    """Probe every configured peer and roll up health. Exit 1 if any unhealthy."""
    out = ctx.obj["output"]
    try:
        peers = load_peers()
    except WsError as e:
        out.error(e)
        return

    if not peers:
        out.success(
            {"healthy": True, "peers": []},
            human_lines=["(no peers configured)"],
        )
        return

    all_results = []
    for peer in peers:
        all_results.extend(probe_peer(peer))

    payload = rollup(all_results)
    out.success(payload, human_lines=_format_status_human(payload))
    if not payload["healthy"]:
        ctx.exit(1)


@fleet.command(name="probe")
@click.argument("host")
@click.argument("desk", required=False)
@click.option("--ssh-user", default=None,
              help="SSH user; falls back to peers.yaml or .ssh/config.")
@click.pass_context
def probe(ctx, host, desk, ssh_user):
    """Probe one peer (or one desk on that peer) for debugging.

    HOST may be either an entry in peers.yaml (matched on host field) or a
    raw SSH target. DESK, if omitted, runs the full per-peer probe set.
    """
    out = ctx.obj["output"]

    peer: PeerSpec | None = None
    try:
        for p in load_peers():
            if p.host == host:
                peer = p
                break
    except WsError:
        # No peers config is fine for ad-hoc probing
        pass

    if peer is None:
        peer = PeerSpec(host=host, ssh_user=ssh_user, desks=[desk] if desk else ["*"])
    elif ssh_user:
        peer.ssh_user = ssh_user

    if desk:
        # Targeted: just deskwatch + cc_liveness on the named desk.
        d = probe_daemon(peer)
        if d.status == "unreachable":
            results = [d]
        else:
            results = [d, probe_deskwatch(peer, desk), probe_cc_liveness(peer, desk)]
    else:
        results = probe_peer(peer)

    payload = rollup(results)
    out.success(payload, human_lines=_format_status_human(payload))
    if not payload["healthy"]:
        ctx.exit(1)
