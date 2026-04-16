"""ws audit — paginated query over the daemon audit log (Slice 4d).

Routes through the wsd daemon's GetAudit RPC when available; falls back
to reading ~/.drydock/audit.log directly when the daemon socket is
absent (read-only operation, file is the same artifact).
"""

from pathlib import Path

import click

from drydock.cli._wsd_client import DaemonRpcError, DaemonUnavailable, call_daemon
from drydock.core import WsError
from drydock.core import audit as _audit_module


@click.command()
@click.option("--limit", default=20, type=int, help="Max events (1..1000)")
@click.option("--before-ts", default=None,
              help="Pagination cursor: only events with ts < this ISO8601 string")
@click.option("--event", default=None,
              help="Filter to one event name (e.g. desk.created, lease.issued)")
@click.option("--principal", default=None,
              help="Filter to events where principal/workspace_id matches")
@click.pass_context
def audit(ctx, limit, before_ts, event, principal):
    """Show recent audit events (newest first)."""
    out = ctx.obj["output"]

    params: dict[str, object] = {"limit": limit}
    if before_ts is not None:
        params["before_ts"] = before_ts
    if event is not None:
        params["event"] = event
    if principal is not None:
        params["principal"] = principal

    try:
        result = call_daemon("GetAudit", params, timeout=30.0)
    except DaemonUnavailable:
        # Fall back to direct file read — GetAudit is read-only and the
        # file is the authoritative artifact whether or not the daemon
        # is up.
        from drydock.wsd.audit_handlers import get_audit
        # Look up DEFAULT_LOG_PATH on the module at call time so test
        # monkeypatching of audit.DEFAULT_LOG_PATH takes effect.
        log_path = _audit_module.DEFAULT_LOG_PATH
        try:
            result = get_audit(params, None, None, log_path=log_path)
        except Exception as exc:
            out.error(WsError(
                f"Failed to read audit log directly: {exc}",
                fix=f"Check {log_path}",
            ))
            return
    except DaemonRpcError as exc:
        out.error(WsError(exc.message, fix=(exc.data or {}).get("fix")))
        return

    events = result.get("events", [])
    next_before_ts = result.get("next_before_ts")

    human_lines = [_format_event(e) for e in events]
    if next_before_ts:
        human_lines.append("")
        human_lines.append(f"more events: ws audit --before-ts {next_before_ts}")
    elif not events:
        human_lines = ["(no events match)"]

    out.success(result, human_lines=human_lines)


def _format_event(entry: dict) -> str:
    """One-line summary for human mode. Tolerates v1 + v2 shapes."""
    ts = entry.get("ts") or entry.get("timestamp") or "?"
    event = entry.get("event", "?")
    principal = entry.get("principal") or entry.get("workspace_id") or "-"
    method = entry.get("method", "")
    detail_str = ""
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    if details:
        # Show 1-2 most informative keys — desk_id / lease_id / cascaded
        # are the common ones. Skip dumping the whole dict.
        keys = [k for k in ("desk_id", "lease_id", "reason", "cascaded_children")
                if k in details]
        if keys:
            detail_str = " " + " ".join(f"{k}={details[k]}" for k in keys)
    method_str = f" [{method}]" if method else ""
    return f"{ts}  {event:<22} {principal}{method_str}{detail_str}"
