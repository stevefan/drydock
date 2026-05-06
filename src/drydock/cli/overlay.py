"""ws overlay — regenerate a drydock's devcontainer override from registry config.

Explicit surface for the regen path that `ws create` on a suspended desk
runs implicitly. Useful when you want to pick up overlay-code changes
(e.g. a new bind-mount shipped in `core/overlay.py`) without touching
the drydock's lifecycle — the file is rewritten; the running container
picks it up on the next `ws stop && ws create` cycle.

Pairs with `ws project reload <name>`, which does the same thing PLUS
reconciles the project YAML into the registry first. Use `ws overlay
regenerate` when the YAML hasn't changed; use `ws project reload` when
it has.
"""

from __future__ import annotations

from pathlib import Path

import click

from drydock.core import WsError
from drydock.core.overlay import regenerate_overlay_from_drydock


@click.group()
def overlay():
    """Manage per-drydock devcontainer overlay files."""


@overlay.command("regenerate")
@click.argument("name")
@click.pass_context
def overlay_regenerate(ctx, name):
    """Rewrite <name>'s overlay JSON from its current registry config.

    Worktree, registry policy, secrets, and container are untouched. The
    container picks up the new overlay on the next `ws stop && ws create`
    cycle; if you want to apply immediately, follow this with stop+create.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_drydock(name)
    if ws is None:
        out.error(
            WsError(
                f"Drydock '{name}' not found",
                fix="Check `ws list` for the name",
                code="desk_not_found",
            )
        )
        return

    try:
        path = regenerate_overlay_from_drydock(ws)
    except WsError as e:
        out.error(e)
        return

    out.success(
        {"name": name, "overlay_path": str(path), "applied": False},
        human_lines=[
            f"overlay regenerated for '{name}'",
            f"  path: {path}",
            f"  next: `ws stop {name} && ws create {name}` to apply to the running container",
        ],
    )
