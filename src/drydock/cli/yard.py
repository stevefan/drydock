"""ws yard — manage Yards (groupings of related Drydocks).

Phase Y0 surface (the primitive only — shared substrate features
are Phase Y1+):

* ``ws yard create <name> [--repo <path>]`` — register a new Yard.
* ``ws yard list``                          — list Yards on this Harbor.
* ``ws yard show <name>``                   — show Yard config + members.
* ``ws yard destroy <name> [--with-members]`` — remove Yard.

A Yard groups related Drydocks for shared concerns (budget, network,
secrets, metering). In Phase Y0, membership is just a foreign key —
no functional difference from standalone Drydocks. Phase Y1+ wire up
the actual shared substrate features.

See docs/design/yard.md for the full model.
"""

from __future__ import annotations

import click

from drydock.core import WsError


@click.group()
def yard():
    """Manage Yards — groupings of related Drydocks with shared substrate."""


@yard.command("create")
@click.argument("name")
@click.option("--repo", "repo_path", default=None,
              help="Path to the Yard's primary repo (e.g., the monorepo).")
@click.pass_context
def yard_create(ctx, name, repo_path):
    """Register a new Yard."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    try:
        result = registry.create_yard(name, repo_path=repo_path)
    except WsError as e:
        out.error(e)
        return
    out.success(
        result,
        human_lines=[
            f"yard '{name}' created (id={result['id']})",
            f"  repo: {repo_path or '(none)'}",
            f"  add Drydocks via the project YAML's `yard: {name}` field, then `ws create`",
        ],
    )


@yard.command("list")
@click.pass_context
def yard_list(ctx):
    """List Yards on this Harbor."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    yards = registry.list_yards()
    if not yards:
        out.success({"yards": []}, human_lines=["(no yards registered)"])
        return
    payload = {"count": len(yards), "yards": []}
    human_lines = [f"{len(yards)} yard(s):", ""]
    for y in yards:
        members = registry.list_yard_members(y["id"])
        entry = {
            "name": y["name"],
            "id": y["id"],
            "repo_path": y["repo_path"],
            "member_count": len(members),
            "members": [m.name for m in members],
        }
        payload["yards"].append(entry)
        human_lines.append(f"  {y['name']:<20} ({len(members)} member(s))")
        for m in members:
            human_lines.append(f"    · {m.name}")
    out.success(payload, human_lines=human_lines)


@yard.command("show")
@click.argument("name")
@click.pass_context
def yard_show(ctx, name):
    """Show Yard config + member Drydocks."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    y = registry.get_yard(name)
    if y is None:
        out.error(WsError(
            f"Yard '{name}' not found",
            fix="Check `ws yard list`; create with `ws yard create <name>`",
            code="yard_not_found",
        ))
        return
    members = registry.list_yard_members(y["id"])
    payload = {
        "name": y["name"],
        "id": y["id"],
        "repo_path": y["repo_path"],
        "config": y["config"],
        "created_at": y["created_at"],
        "updated_at": y["updated_at"],
        "members": [
            {"name": m.name, "id": m.id, "state": m.state, "project": m.project}
            for m in members
        ],
    }
    human_lines = [
        f"yard '{y['name']}' (id={y['id']})",
        f"  repo: {y['repo_path'] or '(none)'}",
        f"  config: {y['config'] or '(empty — Phase Y1+ adds shared-substrate fields)'}",
        f"  created: {y['created_at']}",
        f"  members: {len(members)}",
    ]
    for m in members:
        human_lines.append(f"    · {m.name:<20} state={m.state} project={m.project}")
    out.success(payload, human_lines=human_lines)


@yard.command("destroy")
@click.argument("name")
@click.option("--with-members", is_flag=True,
              help="Detach member Drydocks (sets their yard_id to NULL). "
                   "Does NOT destroy the Drydocks themselves.")
@click.pass_context
def yard_destroy(ctx, name, with_members):
    """Remove a Yard. Refuses if members exist unless --with-members."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    try:
        detached = registry.destroy_yard(name, with_members=with_members)
    except WsError as e:
        out.error(e)
        return
    msg = [f"yard '{name}' destroyed"]
    if detached:
        msg.append(f"  detached {detached} member drydock(s) (still running)")
    out.success({"destroyed": True, "name": name, "detached_members": detached},
                human_lines=msg)
