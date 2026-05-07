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


@auditor.command("watch-once")
@click.option("--no-log", is_flag=True, help="Don't append to watch_log.jsonl")
@click.option("--no-snapshot", is_flag=True,
              help="Don't persist the snapshot (still uses it for the LLM call)")
@click.pass_context
def auditor_watch_once(ctx, no_log, no_snapshot):
    """Run one watch-loop tick: snapshot, LLM classify, record verdict.

    Phase PA1 — single-tick classification by a cheap-class LLM (Haiku).
    Decides routine / anomaly_suspected / unsure. Does not take action;
    does not escalate to principal. Output is the input to the (later)
    deep-analysis tier.

    Requires anthropic API key at ~/.drydock/daemon-secrets/anthropic_api_key.
    Without it, returns an error verdict; deadman switch will fire if
    repeated calls fail (heartbeat won't update on error).
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    from drydock.core.auditor.watch import watch_once

    verdict = watch_once(
        registry=registry,
        write_to_log=not no_log,
        write_snapshot_to_disk=not no_snapshot,
    )
    payload = verdict.to_dict()
    human = [
        f"verdict: {verdict.verdict}",
        f"  reason: {verdict.reason or '(none)'}",
    ]
    if verdict.drydocks_of_concern:
        human.append(f"  drydocks_of_concern: {', '.join(verdict.drydocks_of_concern)}")
    if verdict.input_tokens or verdict.output_tokens:
        human.append(f"  tokens: in={verdict.input_tokens} out={verdict.output_tokens}")
    if verdict.error:
        human.append(f"  ⚠ error: {verdict.error}")
    out.success(payload, human_lines=human)


@auditor.command("deep-log")
@click.option("--limit", default=10, show_default=True)
@click.pass_context
def auditor_deep_log(ctx, limit):
    """Show recent deep-analysis results (Phase PA2)."""
    out = ctx.obj["output"]
    from drydock.core.auditor.deep import read_deep_log
    items = read_deep_log(limit=limit)
    payload = {"count": len(items), "deep_analyses": items}
    if not items:
        out.success(payload, human_lines=["(no deep analyses logged yet)"])
        return
    human = [f"{len(items)} deep analysis result(s):", ""]
    for d in items:
        marker = {
            "action_recommended": "→",
            "escalate_only": "⚠",
            "informational": "·",
            "false_alarm": "·",
            "error": "✗",
        }.get(d.get("verdict", ""), "?")
        sent = " [tg-sent]" if d.get("telegram_sent") else ""
        human.append(
            f"  [{marker}] {d.get('analyzed_at', '?')[:19]}  "
            f"{d.get('verdict', '?'):<20} conf={d.get('confidence', '?')}{sent}"
        )
        if d.get("reasoning"):
            human.append(f"      {d['reasoning'][:120]}")
    out.success(payload, human_lines=human)


@auditor.command("watch-loop")
@click.option("--max-iterations", type=int, default=None,
              help="Run at most N iterations (default: unbounded)")
@click.pass_context
def auditor_watch_loop(ctx, max_iterations):
    """Run the watch loop daemon (foreground).

    Adaptive cadence per scheduler.next_cadence:
      1 min  — open Telegram thread, active workload
      2 min  — recent broker activity (last 10 min)
      5 min  — default
      15 min — night-time (02:00-06:00 local) OR sustained quiet (1h+)

    Each iteration: snapshot Harbor → call Haiku → record verdict.
    The verdict is appended to ~/.drydock/auditor/watch_log.jsonl;
    heartbeat updates on LLM-reachable (any verdict including 'error'
    from malformed-response).

    Stops on SIGTERM/SIGINT/KeyboardInterrupt. For production, run
    under systemd / launchd with Restart=on-failure.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    from drydock.core.auditor.daemon import run as run_daemon
    try:
        stats = run_daemon(registry=registry, max_iterations=max_iterations)
    except KeyboardInterrupt:
        out.success({"interrupted": True}, human_lines=["watch loop interrupted"])
        return
    out.success(
        {
            "iterations": stats.iterations,
            "last_verdict": stats.last_verdict,
            "last_tick_at": stats.last_tick_at,
            "consecutive_errors": stats.consecutive_errors,
            "cadences_chosen": stats.cadences_chosen,
            "deep_analyses": stats.deep_analyses,
            "telegram_escalations": stats.telegram_escalations,
        },
        human_lines=[
            f"watch loop completed: {stats.iterations} iteration(s)",
            f"  last verdict:        {stats.last_verdict}",
            f"  last tick:           {stats.last_tick_at}",
            f"  consecutive errors:  {stats.consecutive_errors}",
            f"  deep analyses fired: {stats.deep_analyses}",
            f"  telegram escalations: {stats.telegram_escalations}",
        ],
    )


@auditor.command("watch-log")
@click.option("--limit", default=20, show_default=True,
              help="Show only the most recent N verdicts.")
@click.pass_context
def auditor_watch_log(ctx, limit):
    """Show recent watch-loop verdicts."""
    out = ctx.obj["output"]
    from drydock.core.auditor.watch import read_watch_log
    verdicts = read_watch_log(limit=limit)
    payload = {"count": len(verdicts), "verdicts": verdicts}
    if not verdicts:
        out.success(payload, human_lines=["(no watch verdicts logged yet — run `ws auditor watch-once`)"])
        return
    human = [f"{len(verdicts)} verdict(s):", ""]
    for v in verdicts:
        marker = {"routine": "·", "anomaly_suspected": "⚠",
                  "unsure": "?", "error": "✗"}.get(v.get("verdict"), "?")
        human.append(
            f"  [{marker}] {v.get('tick_at', '?')[:19]}  "
            f"{v.get('verdict', '?'):<18} {v.get('reason', '')[:80]}"
        )
    out.success(payload, human_lines=human)


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


@auditor.command("designate")
@click.argument("drydock_name")
@click.option(
    "--revoke", is_flag=True, default=False,
    help="Revoke an existing auditor designation (revert this drydock's "
         "token to dock scope).",
)
@click.pass_context
def auditor_designate(ctx, drydock_name, revoke):
    """Designate a drydock as the Port Auditor on this Harbor.

    Phase PA3 — Auditor action authority. Marks the named drydock's
    bearer token as 'auditor'-scoped, which is the structural gate
    that lets it invoke Bucket-2 RPCs (StopDock, RevokeLease,
    ThrottleEgress, FreezeStorage). One Auditor per Harbor by design.

    Idempotent: re-designating the same drydock is a no-op. Refuses
    if a *different* drydock already holds the auditor scope —
    revoke that one first with ``--revoke``.

    Today (V1): the action surface ships in dry-run mode. The Auditor
    can call the Bucket-2 RPCs and the daemon validates scope, audits
    the call, and logs "would have done X" — but doesn't actually
    invoke the underlying primitive. Flip to live execution by setting
    AUDITOR_LIVE_ACTIONS=1 in the daemon environment.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_drydock(drydock_name)
    if ws is None:
        from drydock.core import WsError
        out.error(WsError(
            f"drydock_not_found: {drydock_name!r}",
            fix="Use `drydock list` to see registered drydocks.",
        ))
        return

    token_info = registry.get_token_info(ws.id)
    if token_info is None:
        from drydock.core import WsError
        out.error(WsError(
            f"no_token_issued: drydock '{drydock_name}' has no bearer token",
            fix="The token is issued at create-time. Recreate the drydock "
                "via `drydock destroy --force` then `drydock create` if "
                "this is a legacy desk.",
        ))
        return

    if not revoke:
        # Run the role validator on the drydock's project YAML before
        # granting the auditor scope. Without this gate, `role: auditor`
        # is decorative — anyone could declare it and inherit the
        # asymmetric scope. The validator IS the gate.
        from drydock.core.project_config import load_project_config
        from drydock.core.auditor.role_validator import validate_auditor_role
        from drydock.core import WsError

        cfg = load_project_config(ws.project)
        if cfg is None:
            out.error(WsError(
                f"project_yaml_missing: no project YAML found for "
                f"project {ws.project!r}",
                fix=f"Create ~/.drydock/projects/{ws.project}.yaml with "
                    f"`role: auditor` and the constraints checked by "
                    f"role_validator (narrow egress, resource caps, "
                    f"approved image, etc.).",
            ))
            return
        if cfg.role != "auditor":
            out.error(WsError(
                f"role_not_auditor: project {ws.project!r} declares "
                f"role={cfg.role!r}, not 'auditor'",
                fix="Add `role: auditor` to the project YAML, plus the "
                    "narrow-egress + resource-cap constraints required "
                    "by the role validator. See "
                    "docs/design/port-auditor.md.",
            ))
            return
        result = validate_auditor_role(cfg)
        if not result.ok:
            violations_payload = [
                {"code": v.code, "message": v.message}
                for v in result.violations
            ]
            out.error(WsError(
                f"auditor_role_violations: project {ws.project!r} declares "
                f"role: auditor but fails the role validator "
                f"({len(result.violations)} violation(s))",
                fix="\n".join(f"  - [{v.code}] {v.message}"
                              for v in result.violations),
                context={"violations": violations_payload},
            ))
            return

    if revoke:
        changed = registry.revoke_auditor_scope(ws.id)
        out.success(
            {
                "drydock_id": ws.id,
                "drydock_name": ws.name,
                "revoked": changed,
                "scope_after": "dock",
            },
            human_lines=[
                f"revoked auditor scope from {ws.name}" if changed
                else f"{ws.name} was not auditor-scoped (no-op)",
            ],
        )
        return

    try:
        changed = registry.designate_auditor(ws.id)
    except ValueError as exc:
        from drydock.core import WsError
        out.error(WsError(str(exc), fix=""))
        return

    out.success(
        {
            "drydock_id": ws.id,
            "drydock_name": ws.name,
            "designated": changed,
            "scope_after": "auditor",
            "execution_mode": "dry-run (default)",
            "note": "Bucket-2 RPCs validate scope + audit the call but "
                    "don't yet execute the underlying primitive. Flip "
                    "AUDITOR_LIVE_ACTIONS=1 in the daemon env to enable.",
        },
        human_lines=[
            f"designated {ws.name} as Port Auditor on this Harbor" if changed
            else f"{ws.name} was already auditor-scoped (no-op)",
            "execution mode: DRY-RUN (Bucket-2 calls audit but don't fire)",
        ],
    )


def _human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n = n / 1024
    return f"{n:.1f}PiB"
