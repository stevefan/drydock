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
    tailscale_hostname: str = ""
    tailscale_authkey: str = ""
    tailscale_serve_port: int = 3000
    remote_control_name: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    extra_mounts: list[str] = field(default_factory=list)
    forward_ports: list[int] = field(default_factory=list)


def generate_overlay(ws: Workspace, config: OverlayConfig | None = None) -> dict:
    """Build a devcontainer override dict for a workspace.

    The returned dict is suitable for writing to a JSON file and passing
    to `devcontainer up --override-config`.
    """
    config = config or OverlayConfig()

    overlay: dict = {
        "name": ws.name,
    }

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
        else:
            composite[key] = value

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


def _build_container_env(ws: Workspace, config: OverlayConfig) -> dict[str, str]:
    env: dict[str, str] = {}

    env["TAILSCALE_HOSTNAME"] = config.tailscale_hostname or _default_identity(ws)

    if config.tailscale_authkey:
        env["TAILSCALE_AUTHKEY"] = config.tailscale_authkey

    env["TAILSCALE_SERVE_PORT"] = str(config.tailscale_serve_port)

    env["REMOTE_CONTROL_NAME"] = config.remote_control_name or _default_identity(ws)

    # TODO(v2): replace with daemon-enforced firewall policy; the string format
    # is a coupling to init-firewall.sh's arg parsing.
    if config.firewall_extra_domains:
        env["FIREWALL_EXTRA_DOMAINS"] = " ".join(config.firewall_extra_domains)

    if config.firewall_ipv6_hosts:
        env["FIREWALL_IPV6_HOSTS"] = " ".join(config.firewall_ipv6_hosts)

    # Workspace identity labels as env vars for container introspection
    env["DRYDOCK_WORKSPACE_ID"] = ws.id
    env["DRYDOCK_WORKSPACE_NAME"] = ws.name
    env["DRYDOCK_PROJECT"] = ws.project

    if ws.workspace_subdir:
        env["DRYDOCK_WORKSPACE_SUBDIR"] = ws.workspace_subdir

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

    mounts.append("source=claude-code-config,target=/home/node/.claude,type=volume")
    mounts.append("source=claude-code-bashhistory-${devcontainerId},target=/commandhistory,type=volume")
    mounts.append("source=tailscale-state-${devcontainerId},target=/tmp/tailscale,type=volume")

    mounts.append("source=drydock-vscode-server,target=/home/node/.vscode-server,type=volume")
    mounts.append("source=drydock-npm-cache,target=/home/node/.npm,type=volume")
    mounts.append("source=drydock-tool-cache,target=/home/node/.cache,type=volume")
    mounts.append("source=${localEnv:HOME}/.gitconfig,target=/home/node/.gitconfig,type=bind,readonly")

    mounts.extend(config.extra_mounts)

    return mounts
