"""Per-project YAML configuration for workspace orchestration defaults."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import WsError

KNOWN_KEYS = {
    "repo_path",
    "image",
    "tailscale_hostname",
    "tailscale_serve_port",
    "tailscale_authkey_env_var",
    "remote_control_name",
    "firewall_extra_domains",
    "firewall_ipv6_hosts",
    "secrets_source",
}


@dataclass
class ProjectConfig:
    repo_path: str | None = None
    image: str | None = None
    tailscale_hostname: str | None = None
    tailscale_serve_port: int | None = None
    tailscale_authkey_env_var: str | None = None
    remote_control_name: str | None = None
    firewall_extra_domains: list[str] = field(default_factory=list)
    firewall_ipv6_hosts: list[str] = field(default_factory=list)
    secrets_source: str | None = None


def load_project_config(
    project: str, base_dir: Path = Path("drydock/projects")
) -> ProjectConfig | None:
    path = base_dir / f"{project}.yaml"
    if not path.exists():
        return None

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise WsError(
            message=f"Invalid YAML in {path}: {e}",
            fix=f"Check {path} for syntax errors — run 'python -c \"import yaml; yaml.safe_load(open(\"{path}\"))\"' to diagnose.",
        )

    if raw is None:
        return ProjectConfig()

    if not isinstance(raw, dict):
        raise WsError(
            message=f"Project config {path} must be a YAML mapping, got {type(raw).__name__}",
            fix=f"Rewrite {path} as key-value pairs (e.g. 'repo_path: /srv/code/myproject').",
        )

    # Unknown keys are rejected so typos don't silently become no-ops
    unknown = set(raw.keys()) - KNOWN_KEYS
    if unknown:
        raise WsError(
            message=f"Unknown keys in {path}: {', '.join(sorted(unknown))}",
            fix=f"Valid keys: {', '.join(sorted(KNOWN_KEYS))}",
        )

    return ProjectConfig(
        repo_path=raw.get("repo_path"),
        image=raw.get("image"),
        tailscale_hostname=raw.get("tailscale_hostname"),
        tailscale_serve_port=raw.get("tailscale_serve_port"),
        tailscale_authkey_env_var=raw.get("tailscale_authkey_env_var"),
        remote_control_name=raw.get("remote_control_name"),
        firewall_extra_domains=raw.get("firewall_extra_domains", []),
        firewall_ipv6_hosts=raw.get("firewall_ipv6_hosts", []),
        secrets_source=raw.get("secrets_source"),
    )
