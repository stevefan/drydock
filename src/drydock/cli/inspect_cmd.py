"""ws inspect — show workspace details."""

import click

from drydock.core.errors import WsError


@click.command()
@click.argument("name")
@click.pass_context
def inspect_cmd(ctx, name):
    """Show full details for a workspace."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_workspace(name)
    if not ws:
        out.error(
            WsError(
                f"Workspace '{name}' not found",
                fix=f"Run 'ws list' to see available workspaces",
            )
        )
        return

    out.success(
        ws.to_dict(),
        human_lines=[
            f"Workspace: {ws.name}",
            f"  id:         {ws.id}",
            f"  project:    {ws.project}",
            f"  branch:     {ws.branch}",
            f"  base_ref:   {ws.base_ref}",
            f"  state:      {ws.state}",
            f"  owner:      {ws.owner or '(none)'}",
            f"  repo:       {ws.repo_path}",
            f"  worktree:   {ws.worktree_path or '(none)'}",
            f"  container:  {ws.container_id or '(none)'}",
            f"  hostname:   {ws.hostname or '(none)'}",
            f"  image:      {ws.image or '(default)'}",
            f"  created:    {ws.created_at}",
            f"  updated:    {ws.updated_at}",
        ],
    )
