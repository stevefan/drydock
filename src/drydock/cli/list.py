"""ws list — show all drydocks."""

import click


@click.command()
@click.option("--project", default=None, help="Filter by project")
@click.option("--state", default=None, help="Filter by state")
@click.pass_context
def list(ctx, project, state):
    """List drydocks."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    drydocks = registry.list_drydocks(project=project, state=state)

    out.table(
        [ws.to_dict() for ws in drydocks],
        columns=["name", "project", "branch", "state", "owner", "created_at"],
    )
