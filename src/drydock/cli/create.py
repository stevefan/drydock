"""ws create — provision a new workspace."""

from pathlib import Path

import click

from drydock.core.errors import WsError
from drydock.core.overlay import OverlayConfig, generate_overlay, write_overlay
from drydock.core.project_config import load_project_config
from drydock.core.worktree import create_worktree
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

    try:
        proj_cfg = load_project_config(project)
    except WsError as e:
        out.error(e)
        return

    if repo_path is None:
        repo_path = (proj_cfg.repo_path if proj_cfg and proj_cfg.repo_path else f"/srv/code/{project}")

    if image is None:
        image = (proj_cfg.image if proj_cfg and proj_cfg.image else "")

    ws = Workspace(
        name=name,
        project=project,
        repo_path=repo_path,
        branch=branch,
        base_ref=base_ref,
        image=image,
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

    # Create git worktree for isolated workspace checkout
    try:
        worktree_dir = Path.home() / ".drydock" / "worktrees"
        worktree_path = create_worktree(ws, base_dir=worktree_dir)
        ws = registry.update_workspace(ws.name, worktree_path=str(worktree_path))
    except WsError as e:
        out.error(e)

    # Generate devcontainer override
    overlay_dir = Path.home() / ".drydock" / "overlays"
    overlay_config = _overlay_from_project(proj_cfg)
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


def _overlay_from_project(proj_cfg) -> OverlayConfig:
    if proj_cfg is None:
        return OverlayConfig()
    kwargs: dict = {}
    if proj_cfg.tailscale_hostname is not None:
        kwargs["tailscale_hostname"] = proj_cfg.tailscale_hostname
    if proj_cfg.tailscale_serve_port is not None:
        kwargs["tailscale_serve_port"] = proj_cfg.tailscale_serve_port
    if proj_cfg.remote_control_name is not None:
        kwargs["remote_control_name"] = proj_cfg.remote_control_name
    if proj_cfg.firewall_extra_domains:
        kwargs["firewall_extra_domains"] = proj_cfg.firewall_extra_domains
    if proj_cfg.firewall_ipv6_hosts:
        kwargs["firewall_ipv6_hosts"] = proj_cfg.firewall_ipv6_hosts
    return OverlayConfig(**kwargs)
