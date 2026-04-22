"""ws deskwatch — workload health evaluation.

Two commands:

* ``ws deskwatch [name]`` — evaluate one desk (or all), print the
  result, exit 0 if healthy and 1 if any desk has violations.
* ``ws deskwatch-record <desk> <kind> <name> <status> [detail]`` —
  internal helper that scheduler-wrapped cron lines call to log a
  job_run / probe_result event. Kept as a separate command so cron
  wrappers don't have to know about Python packaging.

See docs/design/deskwatch.md for the model.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from drydock.core import WsError
from drydock.core.deskwatch import (
    evaluate_desk,
    parse_deskwatch_config,
)
from drydock.core.project_config import load_project_config


def _effective_workspace_folder(ws) -> str:
    return str(
        Path(ws.worktree_path) / ws.workspace_subdir
        if ws.workspace_subdir
        else Path(ws.worktree_path)
    )


def _find_container_id(*candidate_paths: str) -> str:
    """Find running container by devcontainer.local_folder label.

    Mirrors ws exec / ws status: containers built before workspace_subdir
    was added carry the bare worktree as their label, so we try both.
    """
    for path in candidate_paths:
        if not path:
            continue
        try:
            result = subprocess.run(
                ["docker", "ps", "-q",
                 "--filter", f"label=devcontainer.local_folder={path}"],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        cid = result.stdout.strip().split("\n")[0].strip()
        if cid:
            return cid
    return ""


def _evaluate_one(registry, ws):
    """Evaluate a single desk; returns (result dict, exit-contribution)."""
    proj = load_project_config(ws.project)
    raw = (proj.deskwatch if proj else None) or ws.config.get("deskwatch") or {}
    config = parse_deskwatch_config(raw)

    if config.is_empty:
        return {
            "desk": ws.name,
            "desk_id": ws.id,
            "checks": [],
            "violations": 0,
            "healthy": True,
            "note": "no deskwatch: block declared in project YAML",
        }, 0

    cid = _find_container_id(_effective_workspace_folder(ws), ws.worktree_path)
    result = evaluate_desk(registry, ws, cid, config)
    return result, (0 if result["healthy"] else 1)


def _format_human(result: dict) -> list[str]:
    lines = [f"{result['desk']}:"]
    if result.get("note"):
        lines.append(f"  ({result['note']})")
        return lines
    if not result["checks"]:
        lines.append("  no checks configured")
        return lines
    for c in result["checks"]:
        mark = "✓" if c["healthy"] else "✗"
        lines.append(f"  [{mark}] {c['kind']}:{c['name']}  {c['detail']}")
    lines.append(
        f"  overall: {'HEALTHY' if result['healthy'] else 'UNHEALTHY'} "
        f"({result['violations']} violation{'s' if result['violations'] != 1 else ''})"
    )
    return lines


@click.command()
@click.argument("name", required=False)
@click.pass_context
def deskwatch(ctx, name):
    """Evaluate workload health for one desk (or all)."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    if name:
        ws = registry.get_workspace(name)
        if ws is None:
            out.error(WsError(
                f"Drydock '{name}' not found",
                fix="Check `ws list` for the name",
                code="desk_not_found",
            ))
            return
        desks = [ws]
    else:
        desks = registry.list_workspaces()

    all_results = []
    exit_code = 0
    for ws in desks:
        try:
            result, contrib = _evaluate_one(registry, ws)
        except WsError as e:
            result = {
                "desk": ws.name, "desk_id": ws.id,
                "checks": [], "violations": 1, "healthy": False,
                "error": e.message, "fix": e.fix,
            }
            contrib = 1
        all_results.append(result)
        exit_code = max(exit_code, contrib)

    if len(all_results) == 1:
        payload = all_results[0]
    else:
        payload = {
            "desks": all_results,
            "healthy": all(r["healthy"] for r in all_results),
            "total_violations": sum(r["violations"] for r in all_results),
        }

    human_lines: list[str] = []
    for r in all_results:
        human_lines.extend(_format_human(r))
        human_lines.append("")
    if human_lines and human_lines[-1] == "":
        human_lines.pop()

    out.success(payload, human_lines=human_lines)
    if exit_code != 0:
        ctx.exit(exit_code)


@click.command(name="deskwatch-record")
@click.argument("desk")
@click.argument("kind", type=click.Choice(["job_run", "probe_result", "output_check"]))
@click.argument("event_name")
@click.argument("status", type=click.Choice(["ok", "failed", "missing"]))
@click.option("--detail", default=None, help="Free-form detail (exit code, stderr tail, etc).")
@click.pass_context
def deskwatch_record(ctx, desk, kind, event_name, status, detail):
    """Record a deskwatch event. Invoked by scheduler wrappers.

    Example (cron wrapper generated by `ws schedule sync`):

        /usr/local/bin/ws exec my-desk -- bash run.sh ; ec=$? ; \\
            /usr/local/bin/ws deskwatch-record my-desk job_run run-daily \\
                $([ $ec -eq 0 ] && echo ok || echo failed) --detail "exit $ec"
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_workspace(desk)
    if ws is None:
        out.error(WsError(
            f"Drydock '{desk}' not found",
            fix="Check `ws list` for the name",
            code="desk_not_found",
        ))
        return

    rowid = registry.record_deskwatch_event(
        ws.id, kind, event_name, status, detail=detail,
    )
    out.success(
        {"desk": desk, "kind": kind, "name": event_name, "status": status,
         "detail": detail, "row_id": rowid},
        human_lines=[f"recorded {kind}:{event_name} = {status} for {desk}"],
    )
