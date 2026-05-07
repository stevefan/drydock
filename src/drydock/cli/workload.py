"""drydock workload — Harbor-side view of declared workloads.

Phase 2a.3 WL1 ships RegisterWorkload + ReleaseWorkload as in-container
RPCs (workers call them via drydock-rpc using their bearer token). This
CLI is the *Harbor-side* read view: list active leases, inspect their
declared specs, see how long they've got. No mutation — that goes
through the in-container RPC path, bound to the worker's token.

Why list-only here:
- The daemon's RegisterWorkload requires the caller's bearer token
  (carries the drydock_id). Harbor-side execution would need a
  different auth path (admin-mode RPCs); not built yet, deferred.
- For operator visibility ("which desks have active workloads, and
  what did they declare?"), direct registry read is enough.
- Auditor will want this view too; landing it as CLI now makes the
  registry method already-extracted.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import click


@click.group()
def workload():
    """Inspect declared workloads and their leases."""


@workload.command("list")
@click.option(
    "--drydock", "drydock_name", default=None,
    help="Filter to one drydock (by name).",
)
@click.option(
    "--all", "include_inactive", is_flag=True, default=False,
    help="Include released/expired/partial-revoked leases too.",
)
@click.pass_context
def list_workloads(ctx, drydock_name, include_inactive):
    """List workload leases.

    Default: only active leases. ``--all`` includes terminal-state rows
    (released, expired, partial-revoked) so you can see recent history.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    drydock_id = None
    if drydock_name:
        ws = registry.get_drydock(drydock_name)
        if ws is None:
            from drydock.core import WsError
            out.error(WsError(
                f"drydock_not_found: {drydock_name!r}",
                fix="Use `drydock list` to see registered drydocks.",
            ))
            return
        drydock_id = ws.id

    if include_inactive:
        rows = _list_all_leases(registry, drydock_id=drydock_id)
    else:
        rows = registry.list_active_workload_leases(drydock_id=drydock_id)

    enriched = [_enrich(r) for r in rows]
    out.table(
        enriched,
        columns=[
            "id", "drydock_id", "kind", "status",
            "granted_at", "expires_at", "time_remaining",
        ],
    )


def _list_all_leases(registry, *, drydock_id: str | None) -> list[dict]:
    """All leases (any status), most-recent first. Tiny helper —
    the canonical-active-only path is already on Registry."""
    if drydock_id is None:
        cur = registry._conn.execute(
            "SELECT * FROM workload_leases ORDER BY granted_at DESC"
        )
    else:
        cur = registry._conn.execute(
            "SELECT * FROM workload_leases WHERE drydock_id = ? "
            "ORDER BY granted_at DESC",
            (drydock_id,),
        )
    return [dict(r) for r in cur.fetchall()]


def _enrich(row: dict) -> dict:
    """Add display-only fields: kind from spec, time_remaining."""
    try:
        spec = json.loads(row["spec_json"])
        kind = spec.get("kind", "?")
    except Exception:
        kind = "?"
    time_remaining = _time_remaining(row.get("status"), row.get("expires_at"))
    return {
        "id": row["id"],
        "drydock_id": row["drydock_id"],
        "kind": kind,
        "status": row["status"],
        "granted_at": row["granted_at"],
        "expires_at": row["expires_at"],
        "time_remaining": time_remaining,
    }


def _time_remaining(status: str | None, expires_at: str | None) -> str:
    if status != "active" or not expires_at:
        return "—"
    try:
        expires = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return "?"
    delta = (expires - datetime.now(timezone.utc)).total_seconds()
    if delta < 0:
        return "EXPIRED"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    return f"{int(delta // 3600)}h{int((delta % 3600) // 60)}m"


@workload.command("inspect")
@click.argument("lease_id")
@click.pass_context
def inspect_workload(ctx, lease_id):
    """Show full structured detail for one workload lease, including
    spec and applied actions."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    row = registry.get_workload_lease(lease_id)
    if row is None:
        from drydock.core import WsError
        out.error(WsError(
            f"lease_not_found: {lease_id!r}",
            fix="Use `drydock workload list --all` to see all leases.",
        ))
        return

    detail = dict(row)
    # Pretty-print embedded JSON for human consumers.
    for field in ("spec_json", "applied_actions_json", "revoke_results_json"):
        if detail.get(field):
            try:
                detail[field.removesuffix("_json")] = json.loads(detail[field])
                detail.pop(field)
            except Exception:
                pass
    out.success(detail)
