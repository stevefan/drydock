"""Pure desk-policy validation per docs/v2-design-capability-broker.md §4."""

from __future__ import annotations

import fnmatch
import os.path
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


MountTuple = tuple[str, str, str]


class InvalidDomainFormat(ValueError):
    """Raised when a domain cannot be canonicalized safely."""


class InvalidMountFormat(ValueError):
    """Raised when a mount cannot be canonicalized safely."""


class InvalidStorageScopeFormat(ValueError):
    """Raised when a storage-scope string cannot be parsed."""


class CapabilityKind(str, Enum):
    """Bare capability grants enforced by the pure spawn validator."""

    SPAWN_CHILDREN = "spawn_children"
    REQUEST_SECRET_LEASES = "request_secret_leases"
    # V4 Phase 1: coarse gate for type=STORAGE_MOUNT lease requests.
    # Per-bucket narrowness is deferred to Phase 1b (delegatable_storage_scopes).
    REQUEST_STORAGE_LEASES = "request_storage_leases"
    # Provisioner drydocks: coarse gate for type=INFRA_PROVISION lease
    # requests. Per-action narrowness via delegatable_provision_scopes.
    REQUEST_PROVISION_LEASES = "request_provision_leases"


@dataclass(frozen=True)
class DeskPolicy:
    """Delegatable parent policy pinned at desk-spawn time."""

    delegatable_firewall_domains: frozenset[str] = field(default_factory=frozenset)
    delegatable_secrets: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[CapabilityKind] = field(default_factory=frozenset)
    extra_mounts: frozenset[MountTuple] = field(default_factory=frozenset)
    # Phase 1b (V4): per-bucket narrowness for STORAGE_MOUNT leases.
    # Stored as the raw YAML strings; parsed on match.
    # Format: "s3://bucket/prefix/*" (ro-only) or "rw:s3://bucket/prefix/*".
    # Default-permissive-when-empty: an empty tuple means "no narrowness
    # declared yet; capability gate alone governs" — preserves pre-1b
    # behavior for existing drydocks. A non-empty tuple enables narrowness
    # matching: requests must match at least one scope. Stricter
    # empty=deny-all was considered and rejected because it would break
    # every existing request_storage_leases user immediately.
    delegatable_storage_scopes: tuple[str, ...] = ()
    # INFRA_PROVISION narrowness: allow-list of IAM action strings
    # (bare AWS actions, optionally globbed: "s3:CreateBucket", "iam:*",
    # "*"). Default-permissive-when-empty preserves the same "declare
    # something to narrow, else capability gate alone" invariant used
    # for storage scopes above. A request's action list must be a subset
    # of the union of matches against granted globs.
    delegatable_provision_scopes: tuple[str, ...] = ()


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


def parse_storage_scope(raw: str) -> dict:
    """Parse a YAML storage-scope string into {bucket, prefix, mode_max}.

    Accepted forms:
      "s3://bucket/prefix/*"       -> ro-only
      "s3://bucket/*"              -> ro-only, whole-bucket
      "rw:s3://bucket/prefix/*"    -> rw allowed (ro also matches)
      "s3://bucket"                -> ro-only, whole-bucket, no prefix

    Trailing "/*" is optional sugar meaning "everything under this prefix".
    The bucket is always required; empty prefix means whole-bucket access.

    Raises InvalidStorageScopeFormat on a malformed string.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidStorageScopeFormat(f"invalid storage scope: {raw!r}")

    value = raw.strip()
    mode_max = "ro"
    if value.startswith("rw:"):
        mode_max = "rw"
        value = value[3:]
    elif value.startswith("ro:"):
        # Tolerated for symmetry; equivalent to no prefix.
        value = value[3:]

    if not value.startswith("s3://"):
        raise InvalidStorageScopeFormat(f"invalid storage scope: {raw!r}")
    body = value[len("s3://"):]
    if not body:
        raise InvalidStorageScopeFormat(f"invalid storage scope: {raw!r}")

    # Strip trailing "/*" sugar; it just means "match any suffix".
    if body.endswith("/*"):
        body = body[:-2]
    elif body == "*":
        raise InvalidStorageScopeFormat(f"invalid storage scope: {raw!r}")

    if "/" in body:
        bucket, prefix = body.split("/", 1)
    else:
        bucket, prefix = body, ""

    bucket = bucket.strip()
    prefix = prefix.strip("/")
    if not bucket:
        raise InvalidStorageScopeFormat(f"invalid storage scope: {raw!r}")

    return {"bucket": bucket, "prefix": prefix, "mode_max": mode_max}


def matches_storage_scope(requested: dict, granted: list[str] | tuple[str, ...]) -> bool:
    """Return True iff `requested` matches at least one `granted` scope.

    `requested` has shape {bucket, prefix, mode}. `granted` is the raw
    YAML-level list (strings). See parse_storage_scope for format.

    Default-permissive-when-empty: an empty `granted` list returns True
    unconditionally. This preserves pre-Phase-1b behavior for existing
    drydocks that were granted request_storage_leases without declaring
    specific scopes. Once a drydock declares any scope, every request
    must match one.

    Match rules per scope:
      - bucket must equal exactly
      - requested.prefix must equal granted.prefix OR start with
        granted.prefix + "/" (so scope "data" matches requested "data"
        and "data/foo" but not "data2"). Empty granted.prefix matches
        any requested prefix.
      - requested.mode must be <= granted.mode_max ("ro" always OK;
        "rw" requires explicit "rw:" prefix on the scope)
    """
    if not granted:
        return True

    req_bucket = requested.get("bucket", "")
    req_prefix = (requested.get("prefix") or "").strip("/")
    req_mode = requested.get("mode", "ro")

    for raw in granted:
        try:
            scope = parse_storage_scope(raw)
        except InvalidStorageScopeFormat:
            # Malformed scopes never match. The YAML loader does not
            # validate shape today; a typo in one entry must not silently
            # allow everything, so we skip it rather than raise.
            continue

        if scope["bucket"] != req_bucket:
            continue
        if not _prefix_matches(req_prefix, scope["prefix"]):
            continue
        if req_mode == "rw" and scope["mode_max"] != "rw":
            continue
        return True
    return False


def matches_provision_actions(
    requested: list[str] | tuple[str, ...],
    granted: list[str] | tuple[str, ...],
) -> bool:
    """Return True iff every requested IAM action is matched by some grant.

    Grants are glob patterns on bare IAM action strings — "s3:CreateBucket"
    (exact), "s3:*" (prefix wildcard), "*" (any). The wildcard syntax is
    deliberately limited to match AWS's own IAM policy grammar (where `*`
    can appear at end of segments or as the whole value).

    Default-permissive-when-empty: an empty `granted` list permits any
    request, matching the pre-narrowness behavior so existing drydocks
    keep working once they've been granted request_provision_leases.
    """
    if not granted:
        return True
    if not requested:
        return True
    for action in requested:
        if not isinstance(action, str) or not action:
            return False
        if not any(_iam_glob_match(action, pat) for pat in granted):
            return False
    return True


def _iam_glob_match(action: str, pattern: str) -> bool:
    if not isinstance(pattern, str) or not pattern:
        return False
    if pattern == "*" or pattern == action:
        return True
    return fnmatch.fnmatchcase(action, pattern)


def _prefix_matches(requested: str, granted: str) -> bool:
    if granted == "":
        return True
    if requested == granted:
        return True
    return requested.startswith(granted + "/")
