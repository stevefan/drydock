"""Devcontainer override generator for per-workspace orchestration."""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import WsError

logger = logging.getLogger(__name__)
from .workspace import Workspace

DEFAULT_SECRETS_HOST_DIR = str(Path.home() / ".drydock" / "secrets")
DEFAULT_SECRETS_CONTAINER_DIR = "/run/secrets"
# Socket lives inside its own dedicated dir so the overlay can bind-mount
# the DIRECTORY (not the socket file) into drydock containers. File
# bind-mounts capture inodes at container-start and orphan on wsd
# restart; directory bind-mounts survive socket-file recreation.
DEFAULT_WSD_RUN_HOST_DIR = str(Path.home() / ".drydock" / "run")
DEFAULT_WSD_RUN_CONTAINER_DIR = "/run/drydock"
DEFAULT_WSD_SOCKET_CONTAINER_PATH = "/run/drydock/wsd.sock"
DEFAULT_DRYDOCK_RPC_HOST_PATH = str(Path.home() / ".drydock" / "bin" / "drydock-rpc")
DEFAULT_DRYDOCK_RPC_CONTAINER_PATH = "/usr/local/bin/drydock-rpc"
SHORT_ID_LENGTH = 6


def _short_id(ws: Workspace) -> str:
    return hashlib.sha256(ws.id.encode()).hexdigest()[:SHORT_ID_LENGTH]


def _default_identity(ws: Workspace) -> str:
    return f"{ws.name}-{_short_id(ws)}"


@dataclass
class OverlayConfig:
    secrets_host_dir: str = DEFAULT_SECRETS_HOST_DIR
    secrets_container_dir: str = DEFAULT_SECRETS_CONTAINER_DIR
    firewall_extra_domains: list[str] = field(default_factory=list)
    firewall_ipv6_hosts: list[str] = field(default_factory=list)
    firewall_aws_ip_ranges: list[str] = field(default_factory=list)
    tailscale_hostname: str = ""
    tailscale_authkey: str = ""
    tailscale_serve_port: int = 3000
    tailscale_advertise_tags: list[str] = field(default_factory=list)
    remote_control_name: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    extra_mounts: list[str] = field(default_factory=list)
    forward_ports: list[int] = field(default_factory=list)
    claude_profile: str = ""
    # In-desk RPC wiring. Set to None to disable either bind-mount
    # (e.g. running a drydock on a Harbor with no wsd daemon yet).
    wsd_run_host_dir: str | None = DEFAULT_WSD_RUN_HOST_DIR
    drydock_rpc_host_path: str | None = DEFAULT_DRYDOCK_RPC_HOST_PATH


def generate_overlay(ws: Workspace, config: OverlayConfig | None = None) -> dict:
    """Build a devcontainer override dict for a workspace.

    The returned dict is suitable for writing to a JSON file and passing
    to `devcontainer up --override-config`.
    """
    config = config or OverlayConfig()

    overlay: dict = {
        "name": ws.name,
    }

    # Pin the container hostname to the tailscale/desk identity so surfaces
    # that read hostname (like the Claude mobile "Choose environment" picker)
    # show a descriptive name instead of Docker's default container-id prefix.
    hostname = config.tailscale_hostname or _default_identity(ws)
    overlay["runArgs"] = [f"--hostname={hostname}"]

    container_env = _build_container_env(ws, config)
    if container_env:
        overlay["containerEnv"] = container_env

    mounts = _build_mounts(ws, config)
    if mounts:
        overlay["mounts"] = mounts

    if config.forward_ports:
        overlay["forwardPorts"] = config.forward_ports

    return overlay


def _strip_jsonc_comments(text: str) -> str:
    """Strip // line comments and /* */ block comments from JSONC text."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'//[^\n]*', '', text)
    return text


def _mount_target(mount_str: str) -> str | None:
    for part in mount_str.split(","):
        if part.startswith("target="):
            return part.split("=", 1)[1]
    return None


def _dedup_mounts(base: list[str], overlay: list[str]) -> list[str]:
    """Concatenate mounts, deduplicating by target. Overlay wins on conflict."""
    overlay_targets = {_mount_target(m) for m in overlay}
    result = [m for m in base if _mount_target(m) not in overlay_targets]
    result.extend(overlay)
    return result


def merge_into_base(base_path: Path, overlay: dict) -> dict:
    """Read base devcontainer.json (JSONC) and deep-merge overlay onto it."""
    if not base_path.exists():
        raise WsError(
            f"Base devcontainer.json not found at {base_path}",
            fix=f"Create {base_path}, or check workspace_subdir in the project YAML",
        )
    raw = base_path.read_text()
    base = json.loads(_strip_jsonc_comments(raw))

    composite = dict(base)
    for key, value in overlay.items():
        if key == "containerEnv":
            composite["containerEnv"] = {**composite.get("containerEnv", {}), **value}
        elif key == "mounts":
            composite["mounts"] = _dedup_mounts(
                list(composite.get("mounts", [])), list(value)
            )
        elif key == "forwardPorts":
            seen = set()
            merged: list[int] = []
            for port in list(composite.get("forwardPorts", [])) + list(value):
                if port not in seen:
                    seen.add(port)
                    merged.append(port)
            composite["forwardPorts"] = merged
        elif key == "runArgs":
            # Concatenate, not replace — base often has required flags like
            # --cap-add=NET_ADMIN, --device=/dev/net/tun. Overlay appends its
            # own (--hostname=...) without dropping the base's.
            composite["runArgs"] = list(composite.get("runArgs", [])) + list(value)
        else:
            composite[key] = value

    # Rewrite relative build.dockerfile + build.context to absolute paths
    # anchored at the source devcontainer.json's directory. Necessary when the
    # source devcontainer.json is NOT at the conventional .devcontainer/
    # location (e.g., .devcontainer/drydock/ via devcontainer_subpath), because
    # devcontainer CLI resolves relative build paths against a synthetic
    # <workspace>/.devcontainer/ regardless of where the override-config sits.
    build = composite.get("build")
    if isinstance(build, dict):
        dockerfile = build.get("dockerfile")
        if isinstance(dockerfile, str) and not Path(dockerfile).is_absolute():
            build["dockerfile"] = str((base_path.parent / dockerfile).resolve())
        context = build.get("context")
        if isinstance(context, str) and not Path(context).is_absolute():
            build["context"] = str((base_path.parent / context).resolve())

    return composite


def write_overlay(
    ws: Workspace,
    output_dir: Path,
    config: OverlayConfig | None = None,
    *,
    base_devcontainer_path: Path,
) -> Path:
    """Generate overlay, merge with base, and write composite JSON. Returns file path."""
    overlay = generate_overlay(ws, config)
    composite = merge_into_base(base_devcontainer_path, overlay)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{ws.id}.devcontainer.json"
    path.write_text(json.dumps(composite, indent=2) + "\n")
    return path


def remove_overlay(overlay_path: str) -> None:
    """Remove an overlay file. Raises on failure."""
    Path(overlay_path).unlink()


def regenerate_overlay_from_workspace(
    ws: Workspace,
    *,
    overlay_dir: Path | None = None,
) -> Path:
    """Re-derive a drydock's overlay file from its registry config snapshot.

    Used by three callers:
    - `_resume_desk` on `ws create` of a suspended drydock
    - `ws overlay regenerate <name>` as an explicit CLI entry point
    - `ws project reload <name>` after reconciling YAML into the registry

    Pulls persistent fields (tailscale_*, firewall_*, extra_mounts, etc.)
    from `ws.config` — the registry's stored snapshot. Project-YAML drift
    isn't reconciled here; `ws project reload` is the path for that.

    `overlay_dir` defaults to `~/.drydock/overlays/` but callers can
    override (e.g. tests). Returns the path of the rewritten file.
    """
    cfg = ws.config if isinstance(ws.config, dict) else {}
    kwargs: dict[str, object] = {}
    if cfg.get("tailscale_hostname"):
        kwargs["tailscale_hostname"] = cfg["tailscale_hostname"]
    if cfg.get("tailscale_serve_port"):
        kwargs["tailscale_serve_port"] = cfg["tailscale_serve_port"]
    if cfg.get("remote_control_name"):
        kwargs["remote_control_name"] = cfg["remote_control_name"]
    if cfg.get("firewall_extra_domains"):
        kwargs["firewall_extra_domains"] = list(cfg["firewall_extra_domains"])
    if cfg.get("firewall_ipv6_hosts"):
        kwargs["firewall_ipv6_hosts"] = list(cfg["firewall_ipv6_hosts"])
    if cfg.get("firewall_aws_ip_ranges"):
        kwargs["firewall_aws_ip_ranges"] = list(cfg["firewall_aws_ip_ranges"])
    if cfg.get("forward_ports"):
        kwargs["forward_ports"] = list(cfg["forward_ports"])
    if cfg.get("claude_profile"):
        kwargs["claude_profile"] = cfg["claude_profile"]
    if cfg.get("extra_mounts"):
        kwargs["extra_mounts"] = list(cfg["extra_mounts"])
    if cfg.get("extra_env"):
        kwargs["extra_env"] = dict(cfg["extra_env"])

    overlay_config = OverlayConfig(**kwargs)
    devcontainer_subpath = cfg.get("devcontainer_subpath") or ".devcontainer"
    worktree_path = ws.worktree_path
    if not worktree_path:
        raise WsError(
            f"Cannot regenerate overlay for '{ws.name}': worktree_path unset in registry",
            fix=f"Rebuild from clean state: ws create {ws.project} {ws.name} --force",
        )
    base_devcontainer = Path(worktree_path) / devcontainer_subpath / "devcontainer.json"

    if overlay_dir is None:
        stored = cfg.get("overlay_path") if isinstance(cfg, dict) else None
        overlay_dir = Path(stored).parent if stored else Path.home() / ".drydock" / "overlays"

    return write_overlay(
        ws,
        overlay_dir,
        overlay_config,
        base_devcontainer_path=base_devcontainer,
    )


def _build_container_env(ws: Workspace, config: OverlayConfig) -> dict[str, str]:
    env: dict[str, str] = {}

    env["TAILSCALE_HOSTNAME"] = config.tailscale_hostname or _default_identity(ws)

    if config.tailscale_authkey:
        env["TAILSCALE_AUTHKEY"] = config.tailscale_authkey

    env["TAILSCALE_SERVE_PORT"] = str(config.tailscale_serve_port)

    if config.tailscale_advertise_tags:
        env["TAILSCALE_ADVERTISE_TAGS"] = ",".join(config.tailscale_advertise_tags)

    env["REMOTE_CONTROL_NAME"] = config.remote_control_name or _default_identity(ws)

    # TODO(v2): replace with daemon-enforced firewall policy; the string format
    # is a coupling to init-firewall.sh's arg parsing.
    if config.firewall_extra_domains:
        env["FIREWALL_EXTRA_DOMAINS"] = " ".join(config.firewall_extra_domains)

    if config.firewall_ipv6_hosts:
        env["FIREWALL_IPV6_HOSTS"] = " ".join(config.firewall_ipv6_hosts)

    if config.firewall_aws_ip_ranges:
        env["FIREWALL_AWS_IP_RANGES"] = " ".join(config.firewall_aws_ip_ranges)

    # Workspace identity labels as env vars for container introspection
    env["DRYDOCK_WORKSPACE_ID"] = ws.id
    env["DRYDOCK_WORKSPACE_NAME"] = ws.name
    env["DRYDOCK_PROJECT"] = ws.project

    if ws.workspace_subdir:
        env["DRYDOCK_WORKSPACE_SUBDIR"] = ws.workspace_subdir

    # In-desk RPC — tell any worker (ws CLI, drydock-rpc, direct clients)
    # where to reach the daemon inside the container.
    if config.wsd_run_host_dir:
        env["DRYDOCK_WSD_SOCKET"] = DEFAULT_WSD_SOCKET_CONTAINER_PATH

    env.update(config.extra_env)

    return env


def _build_mounts(ws: Workspace, config: OverlayConfig) -> list[str]:
    mounts: list[str] = []

    # Per-workspace secrets directory. Operator populates it before container
    # start; docker auto-creates an empty dir if missing. v2 will replace this
    # with daemon-brokered time-bounded credential leases.
    workspace_secrets = Path(config.secrets_host_dir) / ws.id
    mounts.append(
        f"source={workspace_secrets},target={config.secrets_container_dir},type=bind,readonly"
    )

    # In-desk RPC: bind-mount the wsd *run directory* + the drydock-rpc
    # client script. A worker inside the container reaches the daemon
    # by connect()-ing to /run/drydock/wsd.sock.
    #
    # We bind-mount the DIRECTORY (~/.drydock/run → /run/drydock)
    # rather than the socket file so a wsd restart — which unlinks and
    # recreates the socket inode — doesn't orphan the container's
    # bind-mount. The container sees whatever socket file is currently
    # live in the directory. Resilient across `systemctl restart
    # drydock-wsd.service` without needing per-drydock recreate.
    if config.wsd_run_host_dir:
        mounts.append(
            f"source={config.wsd_run_host_dir},"
            f"target={DEFAULT_WSD_RUN_CONTAINER_DIR},type=bind"
        )
    if config.drydock_rpc_host_path:
        mounts.append(
            f"source={config.drydock_rpc_host_path},"
            f"target={DEFAULT_DRYDOCK_RPC_CONTAINER_PATH},type=bind,readonly"
        )

    claude_vol = (
        f"claude-code-config-{config.claude_profile}"
        if config.claude_profile
        else "claude-code-config"
    )
    mounts.append(f"source={claude_vol},target=/home/node/.claude,type=volume")
    mounts.append("source=claude-code-bashhistory-${devcontainerId},target=/commandhistory,type=volume")
    mounts.append("source=tailscale-state-${devcontainerId},target=/tmp/tailscale,type=volume")

    mounts.append("source=drydock-vscode-server,target=/home/node/.vscode-server,type=volume")
    mounts.append("source=drydock-npm-cache,target=/home/node/.npm,type=volume")
    # Narrow: only share pip cache. Umbrella ~/.cache would shadow project-baked
    # subdirs (e.g. Playwright browsers at ~/.cache/ms-playwright) at runtime.
    mounts.append("source=drydock-pip-cache,target=/home/node/.cache/pip,type=volume")
    mounts.append("source=${localEnv:HOME}/.gitconfig,target=/home/node/.gitconfig,type=bind,readonly")

    mounts.extend(config.extra_mounts)

    return mounts
