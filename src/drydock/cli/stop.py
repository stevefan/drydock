"""ws stop — stop a running workspace."""

import click

from drydock.core.devcontainer import DevcontainerCLI
from drydock.core.errors import WsError


@click.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Skip confirmation")
@click.pass_context
def stop(ctx, name, force):
    """Stop a running workspace."""
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

    if ws.state not in ("running", "idle", "ready"):
        out.error(
            WsError(
                f"Workspace '{name}' is in state '{ws.state}', cannot stop",
                fix=f"Only running/idle/ready workspaces can be stopped",
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
    devc.stop(container_id=ws.container_id)

    ws = registry.update_state(name, "suspended")

    out.success(
        ws.to_dict(),
        human_lines=[f"workspace '{name}' stopped"],
    )
