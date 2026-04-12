"""Devcontainer override generator for per-workspace orchestration."""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

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
    extra_labels: dict[str, str] = field(default_factory=dict)


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

    return overlay


def write_overlay(ws: Workspace, output_dir: Path, config: OverlayConfig | None = None) -> Path:
    """Generate and write the override JSON file. Returns the file path."""
    overlay = generate_overlay(ws, config)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{ws.id}.devcontainer.override.json"
    path.write_text(json.dumps(overlay, indent=2) + "\n")
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

    mounts.extend(config.extra_mounts)

    return mounts
