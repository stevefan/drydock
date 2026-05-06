"""ws stop — stop a running drydock."""

import click

from drydock.core.devcontainer import DevcontainerCLI
from drydock.core import WsError
from drydock.core.audit import log_event


@click.command()
@click.argument("name")
@click.pass_context
def stop(ctx, name):
    """Stop a running drydock.

    Stops the container and removes it so the next ws create rebuilds fresh.
    Volumes and checkout are preserved.
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj["dry_run"]

    ws = registry.get_drydock(name)
    if not ws:
        out.error(
            WsError(
                f"Drydock '{name}' not found",
                fix="Run 'ws list' to see available drydocks",
            )
        )
        return

    if ws.state != "running":
        out.error(
            WsError(
                f"Drydock '{name}' is in state '{ws.state}', cannot stop",
                fix="Only running drydocks can be stopped",
            )
        )
        return

    if dry_run:
        out.success(
            {"dry_run": True, "action": "stop", "drydock": ws.to_dict()},
            human_lines=[f"Would stop drydock '{name}'"],
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
    log_event("drydock.stopped", ws.id)

    out.success(
        ws.to_dict(),
        human_lines=[f"drydock '{name}' stopped"],
    )
