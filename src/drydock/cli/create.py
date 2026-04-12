"""ws create — provision a new workspace."""

import os
from pathlib import Path

import click

from drydock.core.devcontainer import DevcontainerCLI
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

    workspace_subdir = (proj_cfg.workspace_subdir if proj_cfg and proj_cfg.workspace_subdir else "")

    ws = Workspace(
        name=name,
        project=project,
        repo_path=repo_path,
        branch=branch,
        base_ref=base_ref,
        image=image,
        workspace_subdir=workspace_subdir,
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

    # Launch devcontainer
    devc = DevcontainerCLI(dry_run=dry_run)
    try:
        devc.check_available()
    except WsError as e:
        out.error(e)

    workspace_folder = (
        os.path.join(ws.worktree_path, ws.workspace_subdir)
        if ws.workspace_subdir
        else ws.worktree_path
    )

    # Preflight: verify devcontainer.json exists before invoking devcontainer CLI
    devcontainer_json = Path(workspace_folder) / ".devcontainer" / "devcontainer.json"
    if not devcontainer_json.exists():
        registry.update_state(ws.name, "error")
        raise WsError(
            f"devcontainer.json not found at {devcontainer_json}",
            fix=f"Create {workspace_folder}/.devcontainer/devcontainer.json, or set a different workspace_subdir in the project YAML",
        )

    ws = registry.update_state(ws.name, "provisioning")
    out.success(
        {},
        human_lines=[f"launching container for '{ws.name}'..."],
    )

    try:
        up_result = devc.up(
            workspace_folder=workspace_folder,
            override_config=str(overlay_path),
        )
        container_id = up_result.get("container_id", "")
        ws = registry.update_workspace(
            ws.name, container_id=container_id, state="running",
        )
    except WsError as e:
        registry.update_state(ws.name, "error")
        raise

    out.success(
        ws.to_dict(),
        human_lines=[
            f"workspace '{ws.name}' created",
            f"  id:           {ws.id}",
            f"  project:      {ws.project}",
            f"  branch:       {ws.branch}",
            f"  state:        {ws.state}",
            f"  container_id: {ws.container_id}",
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
