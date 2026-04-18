"""Pure desk-policy validation per docs/v2-design-capability-broker.md §4."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os.path
from typing import Any


MountTuple = tuple[str, str, str]


class InvalidDomainFormat(ValueError):
    """Raised when a domain cannot be canonicalized safely."""


class InvalidMountFormat(ValueError):
    """Raised when a mount cannot be canonicalized safely."""


class CapabilityKind(str, Enum):
    """Bare capability grants enforced by the pure spawn validator."""

    SPAWN_CHILDREN = "spawn_children"
    REQUEST_SECRET_LEASES = "request_secret_leases"
    # V4 Phase 1: coarse gate for type=STORAGE_MOUNT lease requests.
    # Per-bucket narrowness is deferred to Phase 1b (delegatable_storage_scopes).
    REQUEST_STORAGE_LEASES = "request_storage_leases"


@dataclass(frozen=True)
class DeskPolicy:
    """Delegatable parent policy pinned at desk-spawn time."""

    delegatable_firewall_domains: frozenset[str] = field(default_factory=frozenset)
    delegatable_secrets: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[CapabilityKind] = field(default_factory=frozenset)
    extra_mounts: frozenset[MountTuple] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DeskSpec:
    """Requested child desk scope subject to narrowness checks."""

    firewall_extra_domains: frozenset[str] = field(default_factory=frozenset)
    secret_entitlements: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[CapabilityKind] = field(default_factory=frozenset)
    extra_mounts: frozenset[MountTuple] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Allow:
    """Marker result for an allowed spawn request."""


@dataclass(frozen=True)
class Reject:
    """Structured rejection result for a failed spawn validation."""

    rule: str
    parent_value: object
    requested_value: object
    offending_item: object
    fix_hint: str


def canonicalize_domain(raw: str) -> str:
    """Canonicalize a firewall domain string or raise InvalidDomainFormat."""

    if not isinstance(raw, str):
        raise InvalidDomainFormat(f"invalid domain: {raw!r}")

    value = raw.strip()
    if not value:
        raise InvalidDomainFormat(f"invalid domain: {raw!r}")
    if "*" in value:
        raise InvalidDomainFormat(f"invalid domain: {raw!r}")
    if ":" in value:
        raise InvalidDomainFormat(f"invalid domain: {raw!r}")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidDomainFormat(f"invalid domain: {raw!r}") from exc

    value = value.rstrip(".").lower()
    if not value:
        raise InvalidDomainFormat(f"invalid domain: {raw!r}")

    labels = value.split(".")
    if any(not label for label in labels):
        raise InvalidDomainFormat(f"invalid domain: {raw!r}")

    try:
        canonical = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidDomainFormat(f"invalid domain: {raw!r}") from exc

    return canonical.lower()


def canonicalize_mount(raw: str) -> MountTuple:
    """Canonicalize an extra-mount string or raise InvalidMountFormat."""

    if not isinstance(raw, str):
        raise InvalidMountFormat(f"invalid mount: {raw!r}")

    fields: dict[str, str] = {}
    for item in raw.split(","):
        part = item.strip()
        if not part or "=" not in part:
            raise InvalidMountFormat(f"invalid mount: {raw!r}")
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value or key in fields:
            raise InvalidMountFormat(f"invalid mount: {raw!r}")
        fields[key] = value

    if set(fields) != {"source", "target", "type"}:
        raise InvalidMountFormat(f"invalid mount: {raw!r}")

    return _canonicalize_mount_tuple(
        (fields["source"], fields["target"], fields["type"]),
        raw=raw,
    )


def validate_spawn(parent: DeskPolicy, requested_child: DeskSpec) -> Allow | Reject:
    """Validate child desk narrowness against the parent desk policy."""

    try:
        parent_domains = _canonicalize_domains(parent.delegatable_firewall_domains)
        child_domains = _canonicalize_domains(requested_child.firewall_extra_domains)
        parent_mounts = _canonicalize_mounts(parent.extra_mounts)
        child_mounts = _canonicalize_mounts(requested_child.extra_mounts)
    except (InvalidDomainFormat, InvalidMountFormat) as exc:
        return Reject(
            rule="invalid_input_format",
            parent_value=parent,
            requested_value=requested_child,
            offending_item=str(exc),
            fix_hint="Canonicalize firewall domains and mounts before calling validate_spawn.",
        )

    firewall_diff = sorted(child_domains - parent_domains)
    if firewall_diff:
        return Reject(
            rule="firewall_narrowness",
            parent_value=parent_domains,
            requested_value=child_domains,
            offending_item=firewall_diff[0],
            fix_hint="Request a subset of the parent's delegatable firewall domains.",
        )

    secret_diff = sorted(requested_child.secret_entitlements - parent.delegatable_secrets)
    if secret_diff:
        return Reject(
            rule="secret_narrowness",
            parent_value=parent.delegatable_secrets,
            requested_value=requested_child.secret_entitlements,
            offending_item=secret_diff[0],
            fix_hint="Request a subset of the parent's delegatable secrets.",
        )

    capability_diff = sorted(
        requested_child.capabilities - parent.capabilities,
        key=lambda item: item.value,
    )
    if capability_diff:
        return Reject(
            rule="capability_narrowness",
            parent_value=parent.capabilities,
            requested_value=requested_child.capabilities,
            offending_item=capability_diff[0],
            fix_hint="Request a subset of the parent's capabilities.",
        )

    mount_diff = sorted(child_mounts - parent_mounts)
    if mount_diff:
        return Reject(
            rule="mount_narrowness",
            parent_value=parent_mounts,
            requested_value=child_mounts,
            offending_item=mount_diff[0],
            fix_hint="Request only mounts already present on the parent desk.",
        )

    return Allow()


def _canonicalize_domains(values: frozenset[str]) -> frozenset[str]:
    return frozenset(canonicalize_domain(value) for value in values)


def _canonicalize_mounts(values: frozenset[MountTuple]) -> frozenset[MountTuple]:
    return frozenset(_canonicalize_mount_tuple(value) for value in values)


def _canonicalize_mount_tuple(value: Any, raw: str | None = None) -> MountTuple:
    if not isinstance(value, tuple) or len(value) != 3:
        raise InvalidMountFormat(f"invalid mount: {raw if raw is not None else value!r}")

    source, target, mode = value
    if not all(isinstance(part, str) and part for part in (source, target, mode)):
        raise InvalidMountFormat(f"invalid mount: {raw if raw is not None else value!r}")

    normalized_source = os.path.normpath(source)
    normalized_target = os.path.normpath(target)
    normalized_mode = mode.strip().lower()

    if normalized_mode not in {"bind", "volume"}:
        raise InvalidMountFormat(f"invalid mount: {raw if raw is not None else value!r}")
    if not os.path.isabs(normalized_target):
        raise InvalidMountFormat(f"invalid mount: {raw if raw is not None else value!r}")
    if _has_parent_reference(normalized_source) or _has_parent_reference(normalized_target):
        raise InvalidMountFormat(f"invalid mount: {raw if raw is not None else value!r}")

    return (normalized_source, normalized_target, normalized_mode)


def _has_parent_reference(path: str) -> bool:
    return any(segment == ".." for segment in path.split(os.sep))
