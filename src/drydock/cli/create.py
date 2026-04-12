"""ws create — provision a new workspace."""

from pathlib import Path

import click

from drydock.core.errors import WsError
from drydock.core.overlay import OverlayConfig, generate_overlay, write_overlay
from drydock.core.workspace import Workspace


@click.command()
@click.argument("project")
@click.argument("name", required=False, default=None)
@click.option("--from", "base_ref", default="HEAD", help="Base ref to branch from")
@click.option("--branch", default=None, help="Branch name (default: derived from name)")
@click.option("--repo-path", default=None, help="Path to project repo")
@click.option("--image", default=None, help="Container image override")
@click.option("--owner", default=None, help="Workspace owner (user profile name)")
@click.pass_context
def create(ctx, project, name, base_ref, branch, repo_path, image, owner):
    """Create a new workspace.

    PROJECT is the project name. NAME is an optional workspace name
    (defaults to PROJECT).
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj["dry_run"]

    if name is None:
        name = project

    if branch is None:
        branch = f"ws/{name}"

    if repo_path is None:
        # TODO: look up from project config in drydock/projects/{project}.yaml
        repo_path = f"/srv/code/{project}"

    ws = Workspace(
        name=name,
        project=project,
        repo_path=repo_path,
        branch=branch,
        base_ref=base_ref,
        image=image or "",
        owner=owner or "",
    )

    if dry_run:
        out.success(
            {"dry_run": True, "workspace": ws.to_dict()},
            human_lines=[
                f"Would create workspace '{name}':",
                f"  project:  {project}",
                f"  branch:   {branch}",
                f"  base_ref: {base_ref}",
                f"  repo:     {repo_path}",
            ],
        )
        return

    try:
        ws = registry.create_workspace(ws)
    except WsError as e:
        out.error(e)

    # TODO: create git worktree

    # Generate devcontainer override
    overlay_dir = Path.home() / ".drydock" / "overlays"
    overlay_config = OverlayConfig()
    overlay_path = write_overlay(ws, overlay_dir, overlay_config)
    ws = registry.update_workspace(
        ws.name, config={"overlay_path": str(overlay_path)}
    )

    # TODO: devcontainer up (using overlay_path as --override-config)
    # TODO: update state to 'running' with container_id

    out.success(
        ws.to_dict(),
        human_lines=[
            f"workspace '{ws.name}' created",
            f"  id:       {ws.id}",
            f"  project:  {ws.project}",
            f"  branch:   {ws.branch}",
            f"  state:    {ws.state}",
        ],
    )
