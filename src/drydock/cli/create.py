"""ws create — provision a new workspace."""

import logging
import os
from pathlib import Path

import click

from drydock.cli._wsd_client import DaemonRpcError, DaemonUnavailable, call_daemon
from drydock.core import WsError
from drydock.core.audit import log_event
from drydock.core.checkout import create_checkout
from drydock.core.devcontainer import DevcontainerCLI
from drydock.core.overlay import OverlayConfig, write_overlay
from drydock.core.project_config import ProjectConfig, load_project_config
from drydock.core.trust import _read_workspace_folder_from_overlay, seed_workspace_trust
from drydock.core.workspace import Workspace

logger = logging.getLogger(__name__)
DEFAULT_DEVCONTAINER_SUBPATH = ".devcontainer"


def _ensure_gitconfig_stub() -> None:
    """Touch ~/.gitconfig if missing so the devcontainer bind-mount succeeds.

    The base devcontainer.json bind-mounts ${HOME}/.gitconfig into the
    container so in-container git inherits the user's name/email. On a
    fresh host where git was never configured for this user (common on
    Linux servers), the file may not exist, and `docker run` hard-fails
    with "bind source path does not exist". An empty stub is enough to
    satisfy the mount; users can populate later.
    """
    gitconfig = Path.home() / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.touch(mode=0o644)
        logger.info("Created empty %s for devcontainer bind-mount", gitconfig)


def _daemon_overlay_params(proj_cfg: ProjectConfig | None) -> dict[str, object]:
    if proj_cfg is None:
        return {}

    params: dict[str, object] = {}
    if proj_cfg.tailscale_hostname is not None:
        params["tailscale_hostname"] = proj_cfg.tailscale_hostname
    if proj_cfg.tailscale_serve_port is not None:
        params["tailscale_serve_port"] = proj_cfg.tailscale_serve_port
    if proj_cfg.tailscale_authkey_env_var is not None:
        params["tailscale_authkey_env_var"] = proj_cfg.tailscale_authkey_env_var
    if proj_cfg.remote_control_name is not None:
        params["remote_control_name"] = proj_cfg.remote_control_name
    if proj_cfg.firewall_extra_domains:
        params["firewall_extra_domains"] = proj_cfg.firewall_extra_domains
    if proj_cfg.firewall_ipv6_hosts:
        params["firewall_ipv6_hosts"] = proj_cfg.firewall_ipv6_hosts
    if proj_cfg.forward_ports:
        params["forward_ports"] = proj_cfg.forward_ports
    if proj_cfg.claude_profile is not None:
        params["claude_profile"] = proj_cfg.claude_profile
    if proj_cfg.capabilities:
        params["capabilities"] = proj_cfg.capabilities
    if proj_cfg.secret_entitlements:
        params["secret_entitlements"] = proj_cfg.secret_entitlements
    if proj_cfg.delegatable_secrets:
        params["delegatable_secrets"] = proj_cfg.delegatable_secrets
    if proj_cfg.delegatable_firewall_domains:
        params["delegatable_firewall_domains"] = proj_cfg.delegatable_firewall_domains
    if proj_cfg.delegatable_storage_scopes:
        params["delegatable_storage_scopes"] = proj_cfg.delegatable_storage_scopes
    if proj_cfg.extra_env:
        params["extra_env"] = proj_cfg.extra_env
    return params


@click.command()
@click.argument("project")
@click.argument("name", required=False, default=None)
@click.option("--from", "base_ref", default="HEAD", help="Base ref to branch from")
@click.option("--branch", default=None, help="Branch name (default: derived from name)")
@click.option("--repo-path", default=None, help="Path to project repo")
@click.option("--image", default=None, help="Container image override")
@click.option("--devcontainer-subpath", default=None, help="Relative path to the devcontainer config directory")
@click.option("--owner", default=None, help="Workspace owner (user profile name)")
@click.option("--force", is_flag=True, help="Destroy existing container and rebuild fresh")
@click.pass_context
def create(ctx, project, name, base_ref, branch, repo_path, image, devcontainer_subpath, owner, force):
    """Create a new workspace.

    PROJECT is the project name. NAME is an optional workspace name
    (defaults to PROJECT).
    """
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj["dry_run"]

    if name is None:
        name = project

    _ensure_gitconfig_stub()

    if branch is None:
        branch = f"ws/{name}"

    try:
        try:
            proj_cfg = load_project_config(
                project,
                base_dir=Path.home() / ".drydock" / "projects",
            )
        except TypeError as exc:
            if "unexpected keyword argument 'base_dir'" not in str(exc):
                raise
            proj_cfg = load_project_config(project)
    except WsError as e:
        out.error(e)
        return

    if repo_path is None:
        repo_path = (proj_cfg.repo_path if proj_cfg and proj_cfg.repo_path else f"/srv/code/{project}")

    if image is None:
        image = (proj_cfg.image if proj_cfg and proj_cfg.image else "")

    workspace_subdir = (proj_cfg.workspace_subdir if proj_cfg and proj_cfg.workspace_subdir else "")
    devcontainer_subpath = (
        devcontainer_subpath
        if devcontainer_subpath is not None
        else (
            proj_cfg.devcontainer_subpath
            if proj_cfg and proj_cfg.devcontainer_subpath is not None
            else DEFAULT_DEVCONTAINER_SUBPATH
        )
    )
    _validate_devcontainer_subpath(devcontainer_subpath)

    if dry_run:
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

        if existing and existing.state in ("suspended", "defined"):
            ws = existing
            out.success(
                {"dry_run": True, "action": "resume", "workspace": ws.to_dict()},
                human_lines=[f"Would resume workspace '{name}'"],
            )
            return

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

    daemon_params = {
        "project": project,
        "name": name,
        "base_ref": base_ref,
        "branch": branch,
        "repo_path": repo_path,
        "image": image,
        "owner": owner or "",
    }
    overlay_params = _daemon_overlay_params(proj_cfg)
    daemon_params.update(overlay_params)
    # Send devcontainer_subpath to the daemon whenever it's been overridden
    # from the default (via CLI flag OR project YAML). Previously this only
    # checked the CLI source, which meant a project YAML setting was silently
    # dropped on the daemon path.
    if devcontainer_subpath != DEFAULT_DEVCONTAINER_SUBPATH:
        daemon_params["devcontainer_subpath"] = devcontainer_subpath
    if force:
        try:
            if overlay_params:
                logger.info(
                    "cli: routing via daemon with overlay fields: %s",
                    sorted(overlay_params),
                )
            else:
                logger.info("cli: routing via daemon")
            # DestroyDesk can stop+remove a large container and cascade; give
            # it a 2-min budget (default 30s too short for real teardown).
            call_daemon("DestroyDesk", {"name": name, "force": True}, timeout=120.0)
        except DaemonUnavailable:
            logger.info("cli.create: daemon unavailable, falling back to direct")
        except DaemonRpcError as exc:
            if exc.message != "desk_not_found":
                out.error(_ws_error_from_daemon_error(exc))
    try:
        if overlay_params:
            logger.info(
                "cli: routing via daemon with overlay fields: %s",
                sorted(overlay_params),
            )
        else:
            logger.info("cli: routing via daemon")
        # Fresh devcontainer builds can take several minutes (apt installs,
        # multi-stage compiles). 30s default is too short; fall-through
        # timeouts leave the daemon doing work nobody's waiting on, and the
        # user sees "timed out" on a call that would have succeeded. 15min
        # accommodates realistic first-builds; cached rebuilds finish in
        # seconds either way.
        daemon_result = call_daemon("CreateDesk", daemon_params, timeout=900.0)
    except DaemonUnavailable:
        logger.info("cli.create: daemon unavailable, falling back to direct")
    except DaemonRpcError as exc:
        out.error(_ws_error_from_daemon_error(exc))
        return
    else:
        # Daemon is the source of truth in the routing case. The DeskRef in
        # `daemon_result` is what the host CLI returns to the user. We do NOT
        # re-query a local Registry — the daemon may be using a different
        # registry path (for V2 it shares ~/.drydock/registry.db by default,
        # but tests and ops setups can configure otherwise via --registry).
        out.success(daemon_result, human_lines=_daemon_result_human_lines(daemon_result))
        return

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

    workspace_folder = (
        os.path.join(ws.worktree_path, ws.workspace_subdir)
        if ws.workspace_subdir
        else ws.worktree_path
    )

    # Preflight: verify devcontainer.json exists before invoking devcontainer CLI
    devcontainer_json = Path(workspace_folder) / devcontainer_subpath / "devcontainer.json"
    if not devcontainer_json.exists():
        registry.update_state(ws.name, "error")
        raise WsError(
            f"devcontainer.json not found at {devcontainer_json}",
            fix=(
                f"Create {devcontainer_json}, or set a different workspace_subdir "
                "or devcontainer_subpath in the project YAML"
            ),
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

    if container_id and not dry_run:
        in_container_folder = _read_workspace_folder_from_overlay(str(overlay_path))
        seed_workspace_trust(container_id, in_container_folder)

    human_lines = _workspace_human_lines(ws)
    if lifecycle_warning:
        human_lines.append(f"  WARNING: {lifecycle_warning}")

    out.success(_workspace_output(ws), human_lines=human_lines)


def _overlay_from_project(proj_cfg) -> OverlayConfig:
    if proj_cfg is None:
        return OverlayConfig()
    kwargs: dict = {}
    if proj_cfg.tailscale_hostname is not None:
        kwargs["tailscale_hostname"] = proj_cfg.tailscale_hostname
    if proj_cfg.tailscale_serve_port is not None:
        kwargs["tailscale_serve_port"] = proj_cfg.tailscale_serve_port
    if proj_cfg.tailscale_authkey_env_var is not None:
        kwargs["tailscale_authkey"] = os.getenv(proj_cfg.tailscale_authkey_env_var, "")
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


def _validate_devcontainer_subpath(devcontainer_subpath: str) -> None:
    subpath = Path(devcontainer_subpath)
    if subpath.is_absolute() or ".." in subpath.parts:
        raise WsError(
            "devcontainer_subpath must be relative and contain no ..",
            fix="Pass a relative path like '.devcontainer' or '.devcontainer/drydock'.",
        )


def _workspace_output(ws: Workspace) -> dict:
    return ws.to_dict()


def _workspace_human_lines(ws: Workspace) -> list[str]:
    return [
        f"workspace '{ws.name}' created",
        f"  id:           {ws.id}",
        f"  project:      {ws.project}",
        f"  branch:       {ws.branch}",
        f"  state:        {ws.state}",
        f"  container_id: {ws.container_id}",
    ]


def _daemon_result_human_lines(result: dict) -> list[str]:
    """Format a daemon CreateDesk DeskRef for human output. Mirrors
    _workspace_human_lines so the user sees the same shape regardless of
    which path produced the result."""
    return [
        f"workspace '{result.get('name', '?')}' created (via daemon)",
        f"  id:           {result.get('desk_id', '?')}",
        f"  project:      {result.get('project', '?')}",
        f"  branch:       {result.get('branch', '?')}",
        f"  state:        {result.get('state', '?')}",
        f"  container_id: {result.get('container_id', '?')}",
    ]


def _ws_error_from_daemon_error(err: DaemonRpcError) -> WsError:
    fix = None
    context = {}
    if err.data:
        fix_value = err.data.get("fix")
        if isinstance(fix_value, str):
            fix = fix_value
        context = {key: value for key, value in err.data.items() if key != "fix"}
    return WsError(
        err.message,
        fix=fix,
        context=context,
        code=err.message,
    )
