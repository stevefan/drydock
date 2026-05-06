"""ws auditor — the Port Auditor's CLI surface.

Phase PA0: snapshot capture + read. No LLM. Useful in isolation:

* ``ws auditor snapshot``                  — take a snapshot now; persist + display.
* ``ws auditor list [--limit N]``          — list stored snapshots (newest last).
* ``ws auditor show [<filename>|latest]``  — display a snapshot.
* ``ws auditor metrics <dock>``            — pull one Dock's most recent metrics.
* ``ws auditor prune --keep N``            — keep most-recent N snapshots; remove rest.

The (future) Auditor LLM consumes these snapshots as input context. For now,
the principal can run them manually for ad-hoc inspection.
"""

from __future__ import annotations

from pathlib import Path

import click

from drydock.core import WsError
from drydock.core.auditor.measurement import snapshot_harbor
from drydock.core.auditor.storage import (
    latest_snapshot,
    list_snapshots,
    prune_snapshots,
    read_snapshot,
    snapshot_dir,
    write_snapshot,
)


@click.group()
def auditor():
    """Port Auditor — observation, metrics, snapshots."""


@auditor.command("snapshot")
@click.option("--no-write", is_flag=True,
              help="Don't persist; just compute and display.")
@click.pass_context
def auditor_snapshot(ctx, no_write):
    """Take a Harbor snapshot now."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    snap = snapshot_harbor(registry)
    payload = snap.to_dict()
    path: Path | None = None
    if not no_write:
        path = write_snapshot(snap)
        payload["_persisted_to"] = str(path)
    out.success(payload, human_lines=_format_snapshot_summary(payload))


@auditor.command("list")
@click.option("--limit", default=20, show_default=True,
              help="Show only the most recent N snapshots.")
@click.pass_context
def auditor_list(ctx, limit):
    """List stored snapshots (newest last)."""
    out = ctx.obj["output"]
    snaps = list_snapshots()
    shown = snaps[-limit:] if limit > 0 else snaps
    payload = {
        "snapshot_dir": str(snapshot_dir()),
        "total": len(snaps),
        "shown": len(shown),
        "snapshots": [p.name for p in shown],
    }
    human = [
        f"{len(snaps)} snapshot(s) in {snapshot_dir()}",
        "",
    ]
    if not snaps:
        human.append("(none yet — run `ws auditor snapshot`)")
    else:
        human.extend(f"  {p.name}" for p in shown)
        if len(snaps) > limit > 0:
            human.append(f"  ... ({len(snaps) - limit} older not shown)")
    out.success(payload, human_lines=human)


@auditor.command("show")
@click.argument("which", default="latest")
@click.pass_context
def auditor_show(ctx, which):
    """Display a snapshot. Use 'latest' (default) or pass a filename."""
    out = ctx.obj["output"]
    if which == "latest":
        payload = latest_snapshot()
        if payload is None:
            out.error(WsError(
                "No snapshots stored",
                fix="Run `ws auditor snapshot` to take one",
                code="no_snapshots",
            ))
            return
    else:
        path = snapshot_dir() / which
        if not path.exists():
            out.error(WsError(
                f"Snapshot {which} not found",
                fix="Check `ws auditor list` for available snapshots",
                code="snapshot_not_found",
            ))
            return
        payload = read_snapshot(path)
    out.success(payload, human_lines=_format_snapshot_summary(payload))


@auditor.command("metrics")
@click.argument("dock")
@click.pass_context
def auditor_metrics(ctx, dock):
    """Show the most-recent metrics for one Dock from the latest snapshot."""
    out = ctx.obj["output"]
    snap = latest_snapshot()
    if snap is None:
        out.error(WsError(
            "No snapshots stored",
            fix="Run `ws auditor snapshot` to take one first",
            code="no_snapshots",
        ))
        return
    matches = [d for d in snap.get("drydocks", []) if d["name"] == dock]
    if not matches:
        out.error(WsError(
            f"Dock '{dock}' not found in latest snapshot ({snap['snapshot_at']})",
            fix="Check `ws list` for valid dock names",
            code="dock_not_in_snapshot",
        ))
        return
    payload = matches[0]
    payload["_snapshot_at"] = snap["snapshot_at"]
    out.success(payload, human_lines=_format_dock_summary(payload))


@auditor.command("prune")
@click.option("--keep", required=True, type=int,
              help="Number of most-recent snapshots to keep.")
@click.pass_context
def auditor_prune(ctx, keep):
    """Remove all but the N most-recent snapshots."""
    out = ctx.obj["output"]
    if keep < 0:
        out.error(WsError(
            "--keep must be >= 0",
            fix="Use --keep 0 to remove all, --keep 50 to retain the most recent 50",
            code="invalid_params",
        ))
        return
    removed = prune_snapshots(keep)
    out.success(
        {"removed": removed, "kept": len(list_snapshots())},
        human_lines=[f"removed {removed} snapshot(s); {len(list_snapshots())} remain"],
    )


# ---------------- formatters ----------------

def _format_snapshot_summary(payload: dict) -> list[str]:
    lines = [
        f"snapshot @ {payload.get('snapshot_at', '?')}",
        f"  harbor: {payload.get('harbor_hostname', '?')}",
        f"  drydocks: {payload.get('drydock_count', 0)}",
        "",
    ]
    for d in payload.get("drydocks", []):
        lines.extend(_format_dock_summary(d))
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _format_dock_summary(d: dict) -> list[str]:
    name = d.get("name", "?")
    state = d.get("state", "?")
    yard = d.get("yard_id") or "(no yard)"
    lines = [f"  {name}  state={state}  yard={yard}"]
    metrics = d.get("metrics")
    if metrics:
        cpu = metrics.get("cpu_pct")
        mem_used = metrics.get("mem_used_bytes")
        mem_limit = metrics.get("mem_limit_bytes")
        pids = metrics.get("pids")
        bits = []
        if cpu is not None:
            bits.append(f"cpu={cpu:.1f}%")
        if mem_used is not None and mem_limit is not None:
            bits.append(f"mem={_human_bytes(mem_used)}/{_human_bytes(mem_limit)}")
        if pids is not None:
            bits.append(f"pids={pids}")
        if bits:
            lines.append(f"      metrics: {', '.join(bits)}")
    else:
        lines.append("      metrics: (unavailable)")
    leases = d.get("leases", {})
    if leases.get("active_total", 0) > 0:
        bt = leases.get("by_type", {})
        bits = [f"{t}={n}" for t, n in bt.items()]
        lines.append(f"      leases: active={leases['active_total']} ({', '.join(bits)})")
    audit = d.get("audit_recent_1h")
    if audit and audit.get("events_total", 0) > 0:
        lines.append(f"      audit (last 1h): {audit['events_total']} events")
    drift = d.get("yaml_drift")
    if drift in ("drifted", "yaml_missing"):
        lines.append(f"      ⚠ yaml: {drift}")
    return lines


def _human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n = n / 1024
    return f"{n:.1f}PiB"
