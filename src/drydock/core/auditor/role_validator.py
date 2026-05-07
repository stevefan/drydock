"""Role validator for the Port Auditor's drydock (Phase PA3.1).

Per docs/design/port-auditor.md + memory/project_auditor_isolation_principles.md:
the Auditor *is* a drydock (bootstraps from existing infra), distinguished
by `role: auditor` in project YAML and a validator that constrains what
such a YAML is allowed to declare.

Without this validator, `role: auditor` would be decorative — anyone could
declare it and inherit the asymmetric scope. The validator is the gate.

The Auditor's deployment shape, structurally enforced:

- Egress narrow to {Anthropic API, Telegram, daemon socket} — no broad
  internet, no AWS, no fan-out via the broker
- No broker-capability requests (`request_*_leases`) — Auditor has direct
  scope, not indirect access via the capability broker
- No delegation (`delegatable_*`) — Auditor doesn't sublease; it observes
- Resource caps bounded — Auditor is small; reject elastic compute
- No storage mounts, no extra bind-mounts beyond approved set
- Image from approved list (drydock-base for now; baked port-auditor
  image when that lands)

Loosening the validator requires editing this module — a deliberate
principal-side action, not a YAML knob.
"""
from __future__ import annotations

from dataclasses import dataclass

from drydock.core.project_config import ProjectConfig, ROLE_AUDITOR


# Hostname allowlist. Extend deliberately when the Auditor genuinely
# needs to reach somewhere new — never as a YAML override.
_ALLOWED_FIREWALL_DOMAINS = frozenset({
    "api.anthropic.com",
    "api.telegram.org",
})

# Approved image prefixes. Initially drydock-base; the curated
# drydock-port-auditor image is a later, deliberate addition.
_APPROVED_IMAGE_PREFIXES = (
    "ghcr.io/stevefan/drydock-base",
    "ghcr.io/stevefan/drydock-port-auditor",
)

# Hard ceilings for the Auditor's container. The watch loop is small
# (Haiku calls + light measurement); bursting beyond this is an
# anomaly, not a feature.
_MAX_CPUS = 2.0
_MAX_MEMORY_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB

# Forbidden capabilities — Auditor uses direct scope, not the broker.
_FORBIDDEN_CAPABILITIES = frozenset({
    "request_secret_leases",
    "request_storage_leases",
    "request_provision_leases",
    "request_workload_leases",
    "request_network_reach",
})


@dataclass(frozen=True)
class Violation:
    """A single role-validator failure. Code is a stable kebab-case
    identifier so callers (and tests) can match on it; message is the
    human-readable explanation."""
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    violations: tuple[Violation, ...]

    @classmethod
    def passing(cls) -> "ValidationResult":
        return cls(ok=True, violations=())

    @classmethod
    def failing(cls, violations: list[Violation]) -> "ValidationResult":
        return cls(ok=False, violations=tuple(violations))


def validate_auditor_role(cfg: ProjectConfig) -> ValidationResult:
    """Validate that ``cfg`` is shaped like a legitimate Port Auditor.

    Returns ``ok=True`` if the config declares ``role: auditor`` AND
    every constraint above holds. Otherwise returns the full list of
    violations (collect-all, not fail-fast — easier for the operator
    to fix everything at once).

    Configs with ``role != auditor`` are *not* this validator's
    concern — it returns ok in that case (caller should use a separate
    gate to decide whether the auditor scope applies).
    """
    if cfg.role != ROLE_AUDITOR:
        return ValidationResult.passing()

    violations: list[Violation] = []

    # 1. Egress narrow
    bad_domains = [d for d in cfg.firewall_extra_domains
                   if d not in _ALLOWED_FIREWALL_DOMAINS]
    if bad_domains:
        violations.append(Violation(
            code="egress-domain-not-allowed",
            message=(f"Auditor firewall_extra_domains must be subset of "
                     f"{sorted(_ALLOWED_FIREWALL_DOMAINS)}; got disallowed: "
                     f"{sorted(bad_domains)}"),
        ))
    if cfg.firewall_aws_ip_ranges:
        violations.append(Violation(
            code="aws-egress-forbidden",
            message=("Auditor must not declare firewall_aws_ip_ranges; "
                     "Auditor does not reach AWS."),
        ))
    if cfg.firewall_ipv6_hosts:
        violations.append(Violation(
            code="ipv6-egress-forbidden",
            message=("Auditor must not declare firewall_ipv6_hosts; "
                     "narrow egress is the principle."),
        ))
    if cfg.delegatable_network_reach:
        violations.append(Violation(
            code="network-reach-delegation-forbidden",
            message=("Auditor must not declare delegatable_network_reach; "
                     "the Auditor does not sublease network reach."),
        ))

    # 2. No broker-capability requests
    forbidden = [c for c in cfg.capabilities if c in _FORBIDDEN_CAPABILITIES]
    if forbidden:
        violations.append(Violation(
            code="broker-capability-forbidden",
            message=(f"Auditor must not declare capabilities {sorted(forbidden)}; "
                     "Auditor uses direct scope, not the broker."),
        ))

    # 3. No delegation of any kind
    if cfg.delegatable_secrets:
        violations.append(Violation(
            code="secret-delegation-forbidden",
            message="Auditor must not declare delegatable_secrets.",
        ))
    if cfg.delegatable_storage_scopes:
        violations.append(Violation(
            code="storage-delegation-forbidden",
            message="Auditor must not declare delegatable_storage_scopes.",
        ))
    if cfg.delegatable_provision_scopes:
        violations.append(Violation(
            code="provision-delegation-forbidden",
            message="Auditor must not declare delegatable_provision_scopes.",
        ))
    if cfg.delegatable_firewall_domains:
        violations.append(Violation(
            code="firewall-delegation-forbidden",
            message="Auditor must not declare delegatable_firewall_domains.",
        ))

    # 4. Resource caps
    cpus = cfg.resources_hard.get("cpus")
    memory = cfg.resources_hard.get("memory")
    if cpus is None or memory is None:
        violations.append(Violation(
            code="resource-ceiling-required",
            message=("Auditor must declare resources_hard.cpus and "
                     "resources_hard.memory; the Auditor is small and "
                     "should not run unbounded."),
        ))
    else:
        try:
            if float(cpus) > _MAX_CPUS:
                violations.append(Violation(
                    code="cpu-ceiling-exceeded",
                    message=(f"Auditor resources_hard.cpus={cpus} exceeds "
                             f"max {_MAX_CPUS}."),
                ))
        except (TypeError, ValueError):
            violations.append(Violation(
                code="cpu-ceiling-malformed",
                message=f"Auditor resources_hard.cpus must be numeric; got {cpus!r}.",
            ))
        memory_bytes = _parse_memory_to_bytes(memory)
        if memory_bytes is None:
            violations.append(Violation(
                code="memory-ceiling-malformed",
                message=(f"Auditor resources_hard.memory must be a docker "
                         f"size string (e.g. '2g', '512m'); got {memory!r}."),
            ))
        elif memory_bytes > _MAX_MEMORY_BYTES:
            violations.append(Violation(
                code="memory-ceiling-exceeded",
                message=(f"Auditor resources_hard.memory={memory} exceeds "
                         f"max {_MAX_MEMORY_BYTES} bytes (4g)."),
            ))

    # 5. Image from approved list
    if cfg.image is not None:
        if not any(cfg.image.startswith(p) for p in _APPROVED_IMAGE_PREFIXES):
            violations.append(Violation(
                code="image-not-approved",
                message=(f"Auditor image {cfg.image!r} not in approved set; "
                         f"valid prefixes: {list(_APPROVED_IMAGE_PREFIXES)}."),
            ))

    # 6. No storage mounts, no extra bind-mounts, no forwarded ports
    if cfg.storage_mounts:
        violations.append(Violation(
            code="storage-mount-forbidden",
            message="Auditor must not declare storage_mounts.",
        ))
    if cfg.extra_mounts:
        violations.append(Violation(
            code="extra-mount-forbidden",
            message=("Auditor must not declare extra_mounts; the Auditor's "
                     "bind-mounts are baked at create time."),
        ))
    if cfg.forward_ports:
        violations.append(Violation(
            code="forward-port-forbidden",
            message=("Auditor must not declare forward_ports; the Auditor "
                     "is observation-only and exposes no listening service."),
        ))

    if violations:
        return ValidationResult.failing(violations)
    return ValidationResult.passing()


def _parse_memory_to_bytes(value) -> int | None:
    """Parse a docker-style memory string ('512m', '2g', '1024') to bytes.

    Returns None if unparseable. Accepts plain ints (treated as bytes).
    """
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    if s[-1] in units:
        try:
            return int(float(s[:-1]) * units[s[-1]])
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None
