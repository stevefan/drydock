"""ws inspect — show drydock details."""

import click

from drydock.core import WsError


@click.command()
@click.argument("name")
@click.pass_context
def inspect(ctx, name):
    """Show full details for a drydock."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_drydock(name)
    if not ws:
        out.error(
            WsError(
                f"Drydock '{name}' not found",
                fix="Run 'ws list' to see available drydocks",
            )
        )
        return

    out.success(
        ws.to_dict(),
        human_lines=[
            f"Drydock: {ws.name}",
            f"  id:         {ws.id}",
            f"  project:    {ws.project}",
            f"  branch:     {ws.branch}",
            f"  base_ref:   {ws.base_ref}",
            f"  state:      {ws.state}",
            f"  owner:      {ws.owner or '(none)'}",
            f"  repo:       {ws.repo_path}",
            f"  worktree:   {ws.worktree_path or '(none)'}",
            f"  container:  {ws.container_id or '(none)'}",
            f"  image:      {ws.image or '(default)'}",
            f"  created:    {ws.created_at}",
            f"  updated:    {ws.updated_at}",
        ],
    )
