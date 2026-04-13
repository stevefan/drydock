"""ws list — show all workspaces."""

import click


@click.command()
@click.option("--project", default=None, help="Filter by project")
@click.option("--state", default=None, help="Filter by state")
@click.pass_context
def list(ctx, project, state):
    """List workspaces."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    workspaces = registry.list_workspaces(project=project, state=state)

    out.table(
        [ws.to_dict() for ws in workspaces],
        columns=["name", "project", "branch", "state", "owner", "created_at"],
    )
