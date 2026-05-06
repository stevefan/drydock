"""ws amendment — manage IaC amendment proposals.

Phase A0 of amendment-contract.md: schema + envelope + manual review.
NO auto-approval logic yet — every amendment lands as 'pending' and
requires explicit `ws amendment approve <id>`. A1 adds capability-
handler integration where in-policy proposals auto-apply.

Three classes of amendment author:
- principal: edits YAML directly OR via `ws amendment file --as-principal`
- dockworker: agents inside Drydocks file via the broker (RequestCapability
  wraps amendments in A1+)
- For A0, manual `ws amendment file` is the only authoring path

Surface:
- ws amendment list [--status STATUS] [--dock NAME] [--limit N]
- ws amendment show <id>
- ws amendment file --kind KIND --request JSON [--as-principal | --as DOCK]
- ws amendment approve <id> [--note NOTE]
- ws amendment deny <id> --note NOTE
- ws amendment expire <id>
- ws amendment expire-old (sweep all expired)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import click

from drydock.core import WsError


@click.group()
def amendment():
    """Manage IaC amendment proposals (Phase A0 — manual review)."""


@amendment.command("list")
@click.option("--status", default=None,
              type=click.Choice(["pending", "auto_approved", "escalated",
                                  "approved", "denied", "applied", "expired"]),
              help="Filter by status")
@click.option("--dock", default=None, help="Filter by drydock name")
@click.option("--limit", default=20, show_default=True)
@click.pass_context
def amendment_list(ctx, status, dock, limit):
    """List amendments, newest first."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    drydock_id = None
    if dock:
        ws = registry.get_drydock(dock)
        if ws is None:
            out.error(WsError(
                f"Drydock '{dock}' not found",
                fix="Check `ws list`",
                code="dock_not_found",
            ))
            return
        drydock_id = ws.id
    items = registry.list_amendments(status=status, drydock_id=drydock_id, limit=limit)
    payload = {"count": len(items), "amendments": items}
    if not items:
        out.success(payload, human_lines=["(no amendments matching filters)"])
        return
    human = [f"{len(items)} amendment(s):", ""]
    for a in items:
        marker = _status_marker(a["status"])
        target = a.get("drydock_id") or a.get("yard_id") or "harbor"
        human.append(
            f"  [{marker}] {a['id']}  {a['kind']:<22} "
            f"target={target:<20} status={a['status']}"
        )
        if a.get("reason"):
            human.append(f"      reason: {a['reason'][:80]}")
    out.success(payload, human_lines=human)


@amendment.command("show")
@click.argument("amendment_id")
@click.pass_context
def amendment_show(ctx, amendment_id):
    """Show full amendment record."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    a = registry.get_amendment(amendment_id)
    if a is None:
        out.error(WsError(
            f"Amendment {amendment_id} not found",
            fix="Check `ws amendment list`",
            code="amendment_not_found",
        ))
        return
    out.success(a, human_lines=_format_amendment(a))


@amendment.command("file")
@click.option("--kind", required=True,
              help="Amendment kind (e.g., network_reach, secret_grant, "
                   "workload_register, narrowness_widen)")
@click.option("--request", "request_json", required=True,
              help="JSON object with kind-specific request fields")
@click.option("--as-principal", is_flag=True,
              help="File as principal (auto-approved on create)")
@click.option("--as", "as_dock", default=None,
              help="File as the named Drydock's Dockworker (default: principal)")
@click.option("--reason", default=None, help="Free-form prose justification")
@click.option("--tos-notes", default=None, help="Third-party ToS notes (optional)")
@click.option("--dock", default=None,
              help="Target Drydock for the amendment (default: same as --as)")
@click.option("--yard", default=None, help="Target Yard (alternative to --dock)")
@click.pass_context
def amendment_file(ctx, kind, request_json, as_principal, as_dock, reason,
                   tos_notes, dock, yard):
    """File a new amendment proposal."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    try:
        request = json.loads(request_json)
    except json.JSONDecodeError as e:
        out.error(WsError(
            f"--request must be valid JSON: {e}",
            fix="Example: --request '{\"domain\": \"github.com\", \"port\": 443}'",
            code="invalid_request_json",
        ))
        return
    if not isinstance(request, dict):
        out.error(WsError(
            "--request must be a JSON object",
            fix="Wrap in {} — e.g., '{\"key\": \"value\"}'",
            code="invalid_request_json",
        ))
        return

    drydock_id = None
    yard_id = None
    if dock:
        ws = registry.get_drydock(dock)
        if ws is None:
            out.error(WsError(f"Drydock '{dock}' not found", fix="Check `ws list`"))
            return
        drydock_id = ws.id
    elif as_dock:
        ws = registry.get_drydock(as_dock)
        if ws is None:
            out.error(WsError(f"Drydock '{as_dock}' not found", fix="Check `ws list`"))
            return
        drydock_id = ws.id
    if yard:
        y = registry.get_yard(yard)
        if y is None:
            out.error(WsError(f"Yard '{yard}' not found", fix="Check `ws yard list`"))
            return
        yard_id = y["id"]

    # Determine proposer
    if as_principal or not as_dock:
        proposed_by_type = "principal"
        proposed_by_id = "principal"
        # Principal-authored amendments default to 'approved' (no review gate)
        initial_status = "approved" if as_principal else "pending"
    else:
        ws = registry.get_drydock(as_dock)
        proposed_by_type = "dockworker"
        proposed_by_id = ws.id
        initial_status = "pending"  # Per A0: no auto-approval yet

    record = registry.create_amendment(
        kind=kind,
        request=request,
        proposed_by_type=proposed_by_type,
        proposed_by_id=proposed_by_id,
        drydock_id=drydock_id,
        yard_id=yard_id,
        reason=reason,
        tos_notes=tos_notes,
        status=initial_status,
    )
    out.success(record, human_lines=[
        f"amendment filed: {record['id']}",
        f"  kind:      {record['kind']}",
        f"  proposer:  {proposed_by_type}={proposed_by_id}",
        f"  target:    {drydock_id or yard_id or 'harbor'}",
        f"  status:    {initial_status}",
        "",
        f"review with: ws amendment show {record['id']}",
    ])


@amendment.command("approve")
@click.argument("amendment_id")
@click.option("--note", default=None, help="Approval note (optional)")
@click.pass_context
def amendment_approve(ctx, amendment_id, note):
    """Approve a pending or escalated amendment."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    a = registry.get_amendment(amendment_id)
    if a is None:
        out.error(WsError(f"Amendment {amendment_id} not found",
                          fix="Check `ws amendment list`"))
        return
    if a["status"] not in ("pending", "escalated"):
        out.error(WsError(
            f"Amendment is in status '{a['status']}'; can only approve "
            f"from 'pending' or 'escalated'",
            fix=f"Current status: {a['status']}",
            code="invalid_state_transition",
        ))
        return
    updated = registry.update_amendment_status(
        amendment_id, status="approved", reviewed_by="principal",
        review_note=note,
    )
    out.success(updated, human_lines=[
        f"amendment {amendment_id} approved",
        f"  kind:    {updated['kind']}",
        f"  applied: {updated.get('applied_at') or '(pending — A1+ wires the apply step)'}",
    ])


@amendment.command("deny")
@click.argument("amendment_id")
@click.option("--note", required=True, help="Denial reason (required)")
@click.pass_context
def amendment_deny(ctx, amendment_id, note):
    """Deny a pending or escalated amendment."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    a = registry.get_amendment(amendment_id)
    if a is None:
        out.error(WsError(f"Amendment {amendment_id} not found",
                          fix="Check `ws amendment list`"))
        return
    if a["status"] not in ("pending", "escalated"):
        out.error(WsError(
            f"Amendment is in status '{a['status']}'; can only deny "
            f"from 'pending' or 'escalated'",
            code="invalid_state_transition",
        ))
        return
    updated = registry.update_amendment_status(
        amendment_id, status="denied", reviewed_by="principal", review_note=note,
    )
    out.success(updated, human_lines=[f"amendment {amendment_id} denied: {note}"])


@amendment.command("expire")
@click.argument("amendment_id")
@click.pass_context
def amendment_expire(ctx, amendment_id):
    """Mark an amendment expired (terminates pending/escalated state)."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    a = registry.get_amendment(amendment_id)
    if a is None:
        out.error(WsError(f"Amendment {amendment_id} not found",
                          fix="Check `ws amendment list`"))
        return
    updated = registry.update_amendment_status(amendment_id, status="expired")
    out.success(updated, human_lines=[f"amendment {amendment_id} expired"])


@amendment.command("expire-old")
@click.pass_context
def amendment_expire_old(ctx):
    """Sweep all expired amendments (status='pending' or 'escalated' past expires_at)."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    n = registry.expire_old_pending_amendments()
    out.success({"expired_count": n}, human_lines=[
        f"expired {n} amendment(s) past their expires_at"
    ])


# ---------------- formatters ----------------

def _status_marker(status: str) -> str:
    return {
        "pending": "?", "escalated": "!",
        "auto_approved": "·", "approved": "✓",
        "denied": "✗", "applied": "→",
        "expired": "·",
    }.get(status, "?")


def _format_amendment(a: dict) -> list[str]:
    lines = [
        f"amendment {a['id']}",
        f"  kind:        {a['kind']}",
        f"  status:      {a['status']}",
        f"  proposed by: {a['proposed_by_type']}={a['proposed_by_id']}",
        f"  proposed at: {a['proposed_at']}",
    ]
    if a.get("drydock_id"):
        lines.append(f"  target dock: {a['drydock_id']}")
    if a.get("yard_id"):
        lines.append(f"  target yard: {a['yard_id']}")
    lines.append(f"  request:")
    for k, v in a["request"].items():
        lines.append(f"    {k}: {v}")
    if a.get("reason"):
        lines.append(f"  reason:")
        for ln in str(a["reason"]).splitlines() or [""]:
            lines.append(f"    {ln}")
    if a.get("tos_notes"):
        lines.append(f"  tos_notes:    {a['tos_notes']}")
    if a.get("reviewed_by"):
        lines.append(f"  reviewed by: {a['reviewed_by']} at {a.get('reviewed_at', '?')}")
    if a.get("review_note"):
        lines.append(f"  review note: {a['review_note']}")
    if a.get("applied_at"):
        lines.append(f"  applied at:  {a['applied_at']}")
    if a.get("expires_at"):
        lines.append(f"  expires at:  {a['expires_at']}")
    return lines
