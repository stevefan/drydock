"""ws stop — stop a running workspace."""

import click

from drydock.core.devcontainer import DevcontainerCLI
from drydock.core import WsError
from drydock.core.audit import log_event


@click.command()
@click.argument("name")
@click.pass_context
def stop(ctx, name):
    """Stop a running workspace.

    Stops the container and removes it so the next ws create rebuilds fresh.
    Volumes and checkout are preserved.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj["dry_run"]

    ws = registry.get_workspace(name)
    if not ws:
        out.error(
            WsError(
                f"Workspace '{name}' not found",
                fix="Run 'ws list' to see available workspaces",
            )
        )
        return

    if ws.state != "running":
        out.error(
            WsError(
                f"Workspace '{name}' is in state '{ws.state}', cannot stop",
                fix="Only running workspaces can be stopped",
            )
        )
        return

    if dry_run:
        out.success(
            {"dry_run": True, "action": "stop", "workspace": ws.to_dict()},
            human_lines=[f"Would stop workspace '{name}'"],
        )
        return

    devc = DevcontainerCLI(dry_run=dry_run)
    devc.tailnet_logout(container_id=ws.container_id)
    try:
        devc.stop(container_id=ws.container_id)
        devc.remove(container_id=ws.container_id)
    except WsError:
        registry.update_state(name, "error")
        raise

    ws = registry.update_state(name, "suspended")
    log_event("workspace.stopped", ws.id)

    out.success(
        ws.to_dict(),
        human_lines=[f"workspace '{name}' stopped"],
    )
