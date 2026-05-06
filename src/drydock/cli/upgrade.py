"""ws upgrade — bump a drydock's drydock-base tag and recreate the desk."""

import logging
import re
import subprocess
from pathlib import Path

import click

from drydock.cli._daemon_client import DaemonRpcError, DaemonUnavailable, call_daemon
from drydock.core import WsError
from drydock.core.project_config import ProjectConfig, load_project_config

logger = logging.getLogger(__name__)

DEFAULT_DEVCONTAINER_SUBPATH = ".devcontainer"
BASE_IMAGE_REF = "ghcr.io/stevefan/drydock-base"
FROM_LINE_RE = re.compile(
    r"^(\s*FROM\s+)(" + re.escape(BASE_IMAGE_REF) + r"):(\S+)(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def _resolve_dockerfile_path(ws, proj_cfg: ProjectConfig | None) -> Path:
    workspace_subdir = (proj_cfg.workspace_subdir if proj_cfg and proj_cfg.workspace_subdir else "")
    devcontainer_subpath = (
        proj_cfg.devcontainer_subpath
        if proj_cfg and proj_cfg.devcontainer_subpath is not None
        else DEFAULT_DEVCONTAINER_SUBPATH
    )
    parts = [ws.repo_path]
    if workspace_subdir:
        parts.append(workspace_subdir)
    parts.append(devcontainer_subpath)
    parts.append("Dockerfile")
    return Path(*parts)


def _git_commit_in(repo_path: str, message: str, target: Path) -> None:
    rel = target.resolve().relative_to(Path(repo_path).resolve())
    subprocess.run(
        ["git", "-C", repo_path, "add", "--", str(rel)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", repo_path, "commit", "-m", message],
        check=True, capture_output=True, text=True,
    )


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
    if proj_cfg.devcontainer_subpath is not None and proj_cfg.devcontainer_subpath != DEFAULT_DEVCONTAINER_SUBPATH:
        params["devcontainer_subpath"] = proj_cfg.devcontainer_subpath
    return params


@click.command()
@click.argument("name")
@click.option("--to", "to_tag", default=None, help="Target drydock-base tag (e.g. v1.0.7)")
@click.pass_context
def upgrade(ctx, name, to_tag):
    """Bump a drydock's drydock-base tag and recreate the desk."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]
    dry_run = ctx.obj["dry_run"]

    if not to_tag:
        out.error(WsError("--to <tag> is required", fix="ws upgrade <name> --to v1.0.7"))
        return

    ws = registry.get_drydock(name)
    if not ws:
        out.error(WsError(f"Drydock '{name}' not found", fix="ws list"))
        return

    try:
        proj_cfg = load_project_config(ws.project)
    except WsError as e:
        out.error(e)
        return

    dockerfile = _resolve_dockerfile_path(ws, proj_cfg)
    if not dockerfile.exists():
        out.error(WsError(
            f"Dockerfile not found at {dockerfile}",
            fix=f"Check {ws.project}.yaml repo_path/devcontainer_subpath",
        ))
        return

    contents = dockerfile.read_text()
    match = FROM_LINE_RE.search(contents)
    if not match:
        out.error(WsError(
            f"No FROM ghcr.io/stevefan/drydock-base: line in {dockerfile}",
            fix=f"Project '{ws.project}' is not on drydock-base; nothing to upgrade",
        ))
        return

    old_tag = match.group(3)
    if old_tag == to_tag:
        out.success(
            {"name": name, "tag": to_tag, "changed": False},
            human_lines=[f"drydock '{name}' already on drydock-base:{to_tag}"],
        )
        return

    new_contents = FROM_LINE_RE.sub(rf"\g<1>\g<2>:{to_tag}\g<4>", contents, count=1)

    if dry_run:
        out.success(
            {"dry_run": True, "name": name, "from_tag": old_tag, "to_tag": to_tag,
             "dockerfile": str(dockerfile)},
            human_lines=[
                f"Would bump {dockerfile} from :{old_tag} to :{to_tag}",
                f"Would commit in {ws.repo_path}",
                f"Would destroy + recreate '{name}'",
            ],
        )
        return

    dockerfile.write_text(new_contents)
    try:
        _git_commit_in(ws.repo_path, f"drydock-base: bump to {to_tag}", dockerfile)
    except subprocess.CalledProcessError as exc:
        out.error(WsError(
            f"git commit failed in {ws.repo_path}: {exc.stderr.strip() if exc.stderr else exc}",
            fix=f"Inspect repo state: git -C {ws.repo_path} status",
        ))
        return

    try:
        call_daemon("DestroyDesk", {"name": name, "force": True}, timeout=120.0)
    except DaemonUnavailable:
        out.error(WsError(
            "drydock daemon required for ws upgrade",
            fix="Start the daemon: drydock daemon start",
        ))
        return
    except DaemonRpcError as exc:
        if exc.message != "desk_not_found":
            out.error(_ws_error_from_daemon_error(exc))
            return

    daemon_params = {
        "project": ws.project,
        "name": name,
        "base_ref": ws.base_ref or "HEAD",
        "branch": ws.branch or f"ws/{name}",
        "repo_path": ws.repo_path,
        "image": ws.image or "",
        "owner": ws.owner or "",
    }
    daemon_params.update(_daemon_overlay_params(proj_cfg))

    try:
        result = call_daemon("CreateDesk", daemon_params, timeout=900.0)
    except DaemonUnavailable:
        out.error(WsError(
            "drydock daemon went away mid-upgrade",
            fix=f"Inspect: drydock daemon status; recreate manually: ws create {ws.project} {name}",
        ))
        return
    except DaemonRpcError as exc:
        out.error(_ws_error_from_daemon_error(exc))
        return

    out.success(
        {**result, "from_tag": old_tag, "to_tag": to_tag},
        human_lines=[
            f"drydock '{name}' upgraded :{old_tag} → :{to_tag}",
            f"  container_id: {result.get('container_id', '?')}",
        ],
    )


def _ws_error_from_daemon_error(err: DaemonRpcError) -> WsError:
    fix = None
    context = {}
    if err.data:
        fix_value = err.data.get("fix")
        if isinstance(fix_value, str):
            fix = fix_value
        context = {key: value for key, value in err.data.items() if key != "fix"}
    return WsError(err.message, fix=fix, context=context, code=err.message)
