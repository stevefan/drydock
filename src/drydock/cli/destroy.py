"""ws destroy — remove a workspace."""

import click

from drydock.core.errors import WsError


@click.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Required for destructive operation")
@click.pass_context
def destroy(ctx, name, force):
    """Destroy a workspace and remove its registry entry.

    Requires --force flag. Use --dry-run to preview.
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

    if dry_run:
        out.success(
            {"dry_run": True, "action": "destroy", "workspace": ws.to_dict()},
            human_lines=[
                f"Would destroy workspace '{name}':",
                f"  Remove registry entry",
                f"  Remove worktree at {ws.worktree_path}" if ws.worktree_path else "",
                f"  Stop container {ws.container_id}" if ws.container_id else "",
            ],
        )
        return

    if not force:
        out.error(
            WsError(
                f"Refusing to destroy '{name}' without --force",
                fix=f"Run: ws destroy {name} --force",
            )
        )
        return

    # TODO: stop container if running
    # TODO: remove git worktree
    # TODO: remove state directory
    registry.delete_workspace(name)

    out.success(
        {"destroyed": name},
        human_lines=[f"workspace '{name}' destroyed"],
    )
