"""Per-project YAML configuration for workspace orchestration defaults."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import WsError

KNOWN_KEYS = {
    "repo_path",
    "image",
    "workspace_subdir",
    "devcontainer_subpath",
    "tailscale_hostname",
    "tailscale_serve_port",
    "tailscale_authkey_env_var",
    "remote_control_name",
    "firewall_extra_domains",
    "firewall_ipv6_hosts",
    "firewall_aws_ip_ranges",
    "forward_ports",
    "extra_mounts",
    "extra_env",
    "claude_profile",
    "capabilities",
    "secret_entitlements",
    "delegatable_secrets",
    "delegatable_firewall_domains",
    "delegatable_storage_scopes",
    "delegatable_provision_scopes",
    "storage_mounts",
    "deskwatch",
}


@dataclass
class ProjectConfig:
    repo_path: str | None = None
    image: str | None = None
    workspace_subdir: str | None = None
    devcontainer_subpath: str | None = None
    tailscale_hostname: str | None = None
    tailscale_serve_port: int | None = None
    tailscale_authkey_env_var: str | None = None
    remote_control_name: str | None = None
    firewall_extra_domains: list[str] = field(default_factory=list)
    firewall_ipv6_hosts: list[str] = field(default_factory=list)
    # AWS ip-ranges.json CIDR additions, declared as "REGION:SERVICE" strings
    # (e.g. "us-west-2:AMAZON"). init-firewall.sh fetches + filters +
    # adds CIDRs to the allowed-domains ipset at container start. Closes the
    # structural mismatch between hostname-based firewall and AWS's
    # virtual-host-per-bucket S3 DNS + rotating STS/IAM regional endpoints.
    firewall_aws_ip_ranges: list[str] = field(default_factory=list)
    forward_ports: list[int] = field(default_factory=list)
    extra_mounts: list[str] = field(default_factory=list)
    # containerEnv passthrough: declared env vars land in the devcontainer
    # overlay's containerEnv block alongside drydock-emitted ones
    # (DRYDOCK_WORKSPACE_ID, FIREWALL_EXTRA_DOMAINS, etc.). Useful for
    # pointing tools at specific config file paths — e.g. AWS_CONFIG_FILE
    # for drydocks that bind-mount a readonly AWS profile dir.
    extra_env: dict[str, str] = field(default_factory=dict)
    claude_profile: str | None = None
    capabilities: list[str] = field(default_factory=list)
    secret_entitlements: list[str] = field(default_factory=list)
    delegatable_secrets: list[str] = field(default_factory=list)
    delegatable_firewall_domains: list[str] = field(default_factory=list)
    delegatable_storage_scopes: list[str] = field(default_factory=list)
    delegatable_provision_scopes: list[str] = field(default_factory=list)
    # Declarative S3 mounts; expand_storage_mounts fills in the capability,
    # scope, and firewall entries each one implies. See storage-mount.md.
    storage_mounts: list[dict] = field(default_factory=list)
    # Deskwatch health expectations: jobs / outputs / probes. Parsed lazily
    # at evaluation time via deskwatch.parse_deskwatch_config, so YAML
    # reload doesn't crash on deskwatch typos — errors surface when the user
    # runs `ws deskwatch` and can see them.
    deskwatch: dict = field(default_factory=dict)


def default_projects_dir() -> Path:
    """Resolved lazily so tests can monkeypatch HOME after import."""
    return Path.home() / ".drydock" / "projects"


# Kept for import-compat; do not use for new code — pass base_dir explicitly
# or call default_projects_dir(). Resolving at import time bakes in $HOME
# before test setup can swap it.
DEFAULT_PROJECTS_DIR = Path.home() / ".drydock" / "projects"


def load_project_config(
    project: str, base_dir: Path | None = None,
) -> ProjectConfig | None:
    if base_dir is None:
        base_dir = default_projects_dir()
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

    cfg = ProjectConfig(
        repo_path=raw.get("repo_path"),
        image=raw.get("image"),
        workspace_subdir=raw.get("workspace_subdir"),
        devcontainer_subpath=raw.get("devcontainer_subpath"),
        tailscale_hostname=raw.get("tailscale_hostname"),
        tailscale_serve_port=raw.get("tailscale_serve_port"),
        tailscale_authkey_env_var=raw.get("tailscale_authkey_env_var"),
        remote_control_name=raw.get("remote_control_name"),
        firewall_extra_domains=raw.get("firewall_extra_domains", []),
        firewall_ipv6_hosts=raw.get("firewall_ipv6_hosts", []),
        firewall_aws_ip_ranges=raw.get("firewall_aws_ip_ranges", []),
        forward_ports=raw.get("forward_ports", []),
        extra_mounts=raw.get("extra_mounts", []),
        extra_env=raw.get("extra_env") or {},
        claude_profile=raw.get("claude_profile"),
        capabilities=raw.get("capabilities", []),
        secret_entitlements=raw.get("secret_entitlements", []),
        delegatable_secrets=raw.get("delegatable_secrets", []),
        delegatable_firewall_domains=raw.get("delegatable_firewall_domains", []),
        delegatable_storage_scopes=raw.get("delegatable_storage_scopes", []),
        delegatable_provision_scopes=raw.get("delegatable_provision_scopes", []),
        storage_mounts=raw.get("storage_mounts", []),
        deskwatch=raw.get("deskwatch") or {},
    )
    return expand_storage_mounts(cfg)


_DEFAULT_STORAGE_REGION = "us-west-2"


def expand_storage_mounts(cfg: ProjectConfig) -> ProjectConfig:
    """Derive capabilities + storage scopes + firewall ranges from storage_mounts.

    One declaration → full wiring. User-provided values on the dependent
    fields are preserved (additive); duplicates are de-dup'd.

    For each entry in `cfg.storage_mounts`:
      - adds `request_storage_leases` capability (idempotent)
      - adds `<rw:?>s3://bucket/prefix/*` to delegatable_storage_scopes
      - adds `<region>:AMAZON` to firewall_aws_ip_ranges
    """
    if not cfg.storage_mounts:
        return cfg

    caps = list(cfg.capabilities)
    scopes = list(cfg.delegatable_storage_scopes)
    fw = list(cfg.firewall_aws_ip_ranges)

    if "request_storage_leases" not in caps:
        caps.append("request_storage_leases")

    for entry in cfg.storage_mounts:
        if not isinstance(entry, dict):
            raise WsError(
                message=f"storage_mounts entries must be mappings, got {type(entry).__name__}",
                fix="Each entry needs at least 'source' and 'target' keys.",
            )
        source = entry.get("source", "")
        if not isinstance(source, str) or not source.startswith("s3://"):
            raise WsError(
                message=f"storage_mounts[].source must be an s3:// URL, got {source!r}",
                fix="Example: 'source: s3://my-bucket/my-prefix'",
            )
        mode = (entry.get("mode") or "ro").lower()
        if mode not in ("ro", "rw"):
            raise WsError(
                message=f"storage_mounts[].mode must be 'ro' or 'rw', got {mode!r}",
                fix="Use 'ro' for read-only (default) or 'rw' for read-write.",
            )
        target = entry.get("target", "")
        if not isinstance(target, str) or not target.startswith("/"):
            raise WsError(
                message=f"storage_mounts[].target must be an absolute container path, got {target!r}",
                fix="Example: 'target: /mnt/data'",
            )
        region = entry.get("region") or _DEFAULT_STORAGE_REGION

        body = source[len("s3://"):].rstrip("/")
        scope = f"s3://{body}/*" if body else source
        if mode == "rw":
            scope = f"rw:{scope}"
        if scope not in scopes:
            scopes.append(scope)

        fw_entry = f"{region}:AMAZON"
        if fw_entry not in fw:
            fw.append(fw_entry)

    cfg.capabilities = caps
    cfg.delegatable_storage_scopes = scopes
    cfg.firewall_aws_ip_ranges = fw
    return cfg
