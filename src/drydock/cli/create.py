"""ws create — provision a new workspace."""

import logging
import os
from pathlib import Path

import click

logger = logging.getLogger(__name__)

from drydock.core.devcontainer import DevcontainerCLI
from drydock.core import WsError
from drydock.core.audit import log_event
from drydock.core.overlay import OverlayConfig, write_overlay
from drydock.core.project_config import load_project_config
from drydock.core.checkout import create_checkout
from drydock.core.workspace import Workspace


@click.command()
@click.argument("project")
@click.argument("name", required=False, default=None)
@click.option("--from", "base_ref", default="HEAD", help="Base ref to branch from")
@click.option("--branch", default=None, help="Branch name (default: derived from name)")
@click.option("--repo-path", default=None, help="Path to project repo")
@click.option("--image", default=None, help="Container image override")
@click.option("--owner", default=None, help="Workspace owner (user profile name)")
@click.option("--force", is_flag=True, help="Destroy existing container and rebuild fresh")
@click.pass_context
def create(ctx, project, name, base_ref, branch, repo_path, image, owner, force):
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

    existing = registry.get_workspace(name)

    if existing and existing.state == "running" and not force:
        out.error(
            WsError(
                f"Workspace '{name}' is already running",
                fix=f"ws create {name} --force",
                code="workspace_already_running",
            )
        )
        return

    if existing and existing.state == "error" and not force:
        out.error(
            WsError(
                f"Workspace '{name}' is in state 'error'",
                fix=f"Rebuild from clean state: ws create {project} {name} --force",
                code="workspace_in_error_state",
            )
        )
        return

    if existing and existing.state == "provisioning":
        out.error(
            WsError(
                f"Workspace '{name}' is currently provisioning",
                fix=f"Wait for the in-flight operation, or investigate: ws inspect {name}",
                code="workspace_provisioning",
            )
        )
        return

    if existing and existing.state in ("running", "error") and force:
        if not dry_run:
            if existing.container_id:
                devc = DevcontainerCLI()
                try:
                    devc.tailnet_logout(container_id=existing.container_id)
                except Exception as exc:
                    logger.warning("Failed tailnet logout for %s: %s", name, exc)
                try:
                    devc.stop(container_id=existing.container_id)
                    devc.remove(container_id=existing.container_id)
                except WsError:
                    registry.update_state(name, "error")
                    raise
            registry.update_state(name, "suspended")
            log_event("workspace.force_recreate", existing.id)
            existing = registry.get_workspace(name)

    if existing and existing.state in ("suspended", "defined"):
        ws = existing
    elif existing:
        out.error(
            WsError(
                f"Workspace '{name}' is in unexpected state '{existing.state}'",
                fix=f"Investigate: ws inspect {name}",
            )
        )
        return
    else:
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
            log_event("workspace.created", ws.id)
        except WsError as e:
            out.error(e)

        try:
            checkout_dir = Path.home() / ".drydock" / "worktrees"
            checkout_path = create_checkout(ws, base_dir=checkout_dir)
            ws = registry.update_workspace(ws.name, worktree_path=str(checkout_path))
        except WsError as e:
            out.error(e)

    if dry_run:
        out.success(
            {"dry_run": True, "action": "resume", "workspace": ws.to_dict()},
            human_lines=[f"Would resume workspace '{name}'"],
        )
        return

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

    # Generate composite devcontainer config (base + overlay merged)
    overlay_dir = Path.home() / ".drydock" / "overlays"
    overlay_config = _overlay_from_project(proj_cfg)
    overlay_path = write_overlay(ws, overlay_dir, overlay_config, base_devcontainer_path=devcontainer_json)
    ws = registry.update_workspace(
        ws.name, config={"overlay_path": str(overlay_path)}
    )

    # Launch devcontainer
    devc = DevcontainerCLI(dry_run=dry_run)
    try:
        devc.check_available()
    except WsError as e:
        out.error(e)

    # Pre-sweep: remove stale stopped containers that would confuse devcontainer CLI
    devc.remove_stale_containers(workspace_folder)

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
        lifecycle_warning = up_result.get("warning")

        update_kwargs = dict(container_id=container_id, state="running")
        if lifecycle_warning:
            merged_config = {**ws.config, "lifecycle_warning": lifecycle_warning}
            update_kwargs["config"] = merged_config
        ws = registry.update_workspace(ws.name, **update_kwargs)
        log_event("workspace.running", ws.id)
    except WsError:
        registry.update_state(ws.name, "error")
        log_event("workspace.error", ws.id)
        raise

    human_lines = [
        f"workspace '{ws.name}' created",
        f"  id:           {ws.id}",
        f"  project:      {ws.project}",
        f"  branch:       {ws.branch}",
        f"  state:        {ws.state}",
        f"  container_id: {ws.container_id}",
    ]
    if lifecycle_warning:
        human_lines.append(f"  WARNING: {lifecycle_warning}")

    out.success(ws.to_dict(), human_lines=human_lines)


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
    if proj_cfg.forward_ports:
        kwargs["forward_ports"] = proj_cfg.forward_ports
    if proj_cfg.extra_mounts:
        kwargs["extra_mounts"] = proj_cfg.extra_mounts
    if proj_cfg.claude_profile is not None:
        kwargs["claude_profile"] = proj_cfg.claude_profile
    return OverlayConfig(**kwargs)
