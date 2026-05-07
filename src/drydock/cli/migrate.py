"""drydock migrate — atomic structural transitions for a Drydock.

Phase 2a.4 M1 of make-the-harness-live.md. Today (M1): plan + dry-run
only. The full state machine (Snapshot → Stop → Mutate → Restore →
Start → Verify → Rollback → Cleanup) lands in subsequent commits.

Usage::

    drydock migrate <name> --target image=<tag> [--dry-run]
    drydock migrate <name> --target reload [--dry-run]
    drydock migrate <name> --target schema=<version> [--dry-run]

For now, every invocation behaves as if --dry-run is set: it computes
+ persists the plan, runs pre-checks, and exits. Real execution stages
ship in M1-followups.
"""
from __future__ import annotations

import json
import socket

import click

from drydock.core import WsError
from drydock.core.migration import (
    ImageBumpTarget,
    MigrationPlanError,
    ProjectReloadTarget,
    SchemaMigrationTarget,
    plan_migration,
    precheck_migration,
)


@click.command("migrate")
@click.argument("name")
@click.option(
    "--target",
    "target_spec",
    required=True,
    help=(
        "What to migrate to. Forms: 'image=<tag>', 'reload', "
        "'schema=<version>'."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help="Bypass pre-check warnings (does not bypass hard refusals).",
)
@click.pass_context
def migrate(ctx, name, target_spec, force):
    """Plan and execute an atomic structural transition for a drydock.

    M1: prints the plan + pre-check result and persists a 'planned'
    migration record. Execution stages land in follow-up commits.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj.get("dry_run", False)

    drydock = registry.get_drydock(name)
    if drydock is None:
        out.error(WsError(
            f"drydock_not_found: '{name}'",
            fix="Use `drydock list` to see what's registered.",
        ))
        return

    try:
        target = _parse_target(target_spec)
    except WsError as exc:
        out.error(exc)
        return

    # Existing in-flight migration on this drydock blocks a new plan.
    in_flight = registry.list_active_migrations(drydock_id=drydock.id)
    if in_flight:
        out.error(WsError(
            f"migration_in_progress: drydock '{name}' has migration "
            f"{in_flight[0]['id']} in_progress",
            fix="Resolve or rollback the prior migration first.",
        ))
        return

    # Look up active workload leases so the plan can warn about them.
    workload_leases = registry.list_active_workload_leases(drydock_id=drydock.id)

    try:
        plan = plan_migration(
            drydock=drydock,
            target=target,
            source_harbor=socket.gethostname(),
            in_flight_workload_leases=workload_leases,
        )
    except MigrationPlanError as exc:
        out.error(WsError(str(exc), fix=""))
        return

    precheck = precheck_migration(
        drydock=drydock,
        plan=plan,
        # M1: no real disk-space probe yet; daemon-health/image-presence
        # default to "assume ok" for the planning surface. Hard checks
        # land in M1-followup when the state machine actually executes.
        daemon_healthy=True,
        target_image_present=None,
    )

    registry.insert_migration(
        migration_id=plan.migration_id,
        drydock_id=drydock.id,
        plan_json=json.dumps(plan.to_dict()),
        status="planned",
    )

    response: dict = {
        "migration_id": plan.migration_id,
        "plan": plan.to_dict(),
        "precheck": precheck.to_dict(),
        "executed": False,
        "dry_run": dry_run,
    }

    human_lines = list(plan.human_summary())
    if precheck.refusals:
        human_lines.append("")
        human_lines.append("Pre-check REFUSED:")
        for r in precheck.refusals:
            human_lines.append(f"  ✗ {r}")
        out.success(response, human_lines=human_lines)
        return

    if precheck.warnings:
        human_lines.append("")
        if force or dry_run:
            human_lines.append("Pre-check warnings:")
        else:
            human_lines.append("Pre-check warnings (use --force to proceed):")
        for w in precheck.warnings:
            human_lines.append(f"  ⚠ {w}")

    # Block execution if warnings exist without --force.
    if precheck.warnings and not force and not dry_run:
        human_lines.append("")
        human_lines.append("Refusing to execute without --force.")
        out.success(response, human_lines=human_lines)
        return

    if dry_run:
        human_lines.append("")
        human_lines.append("[--dry-run] plan recorded; no execution.")
        out.success(response, human_lines=human_lines)
        return

    # Execute the planned migration.
    from pathlib import Path
    from drydock.core.migration_executor import (
        ExecutorConfig, execute_migration,
    )
    config = ExecutorConfig(
        secrets_root=Path.home() / ".drydock" / "secrets",
        overlays_root=Path.home() / ".drydock" / "overlays",
        migrations_root=Path.home() / ".drydock" / "migrations",
    )
    outcome = execute_migration(
        plan.migration_id, registry=registry, config=config,
    )
    response["executed"] = True
    response["outcome"] = outcome.to_dict()

    human_lines.append("")
    human_lines.append(f"Execution: {outcome.terminal_status}")
    for s in outcome.stages:
        marker = {"ok": "✓", "skipped": "·", "failed": "✗"}.get(s.status, "?")
        human_lines.append(f"  {marker} {s.stage}")
    if outcome.error:
        human_lines.append("")
        human_lines.append(f"Error: {json.dumps(outcome.error)}")
    if outcome.terminal_status == "completed":
        human_lines.append("")
        human_lines.append(
            "Container is stopped. Run `drydock create "
            f"{drydock.name}` to bring it back up under the new config."
        )

    out.success(response, human_lines=human_lines)


def _parse_target(spec: str):
    """Parse the --target string into a typed MigrationTarget.

    Forms:
    - image=<tag>       → ImageBumpTarget
    - reload            → ProjectReloadTarget
    - schema=<int>      → SchemaMigrationTarget
    """
    spec = spec.strip()
    if spec == "reload":
        return ProjectReloadTarget()
    if spec.startswith("image="):
        new_image = spec[len("image="):].strip()
        if not new_image:
            raise WsError(
                "invalid --target image: empty image tag",
                fix="Use --target image=<tag>, e.g. ghcr.io/stevefan/drydock-base:v1.0.20",
            )
        return ImageBumpTarget(new_image=new_image)
    if spec.startswith("schema="):
        try:
            version = int(spec[len("schema="):].strip())
        except ValueError:
            raise WsError(
                "invalid --target schema: not an integer version",
                fix="Use --target schema=<int>, e.g. --target schema=8",
            ) from None
        return SchemaMigrationTarget(target_schema_version=version)
    raise WsError(
        f"unknown --target form: {spec!r}",
        fix="Supported: 'image=<tag>', 'reload', 'schema=<int>'",
    )
