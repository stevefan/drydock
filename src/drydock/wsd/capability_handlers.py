"""RequestCapability + ReleaseCapability JSON-RPC handlers.

Per docs/v2-design-capability-broker.md and docs/v2-design-protocol.md:

- Subject desk is derived from the bearer token (caller_desk_id) — never
  taken as an RPC argument. Mitigates a confused-deputy class of bugs.
- V2.0 ships type=SECRET. V2.1 added cross-drydock secret delegation via
  `source_desk_id`. V4 Phase 1 adds type=STORAGE_MOUNT (scoped AWS STS
  credentials). Phase 1c adds type=NETWORK_REACH (live firewall opens
  via add-allowed-domain.sh; per-Dock delegatable_network_reach +
  network_reach_ports policy). Only COMPUTE_QUOTA remains reserved-but-
  unsupported.
- Entitlement check for SECRET is a trivial subset lookup against the
  desk's `delegatable_secrets` (which doubles as the Dock's own
  entitlements in the V2 model — see capability-broker.md §4 closing
  note "Post-spawn narrowness is a trivial lookup").
- STORAGE_MOUNT gate in Phase 1 is coarse: the capability
  `request_storage_leases` must be granted. Per-bucket narrowness
  (delegatable_storage_scopes) is deferred to Phase 1b.
- Lease materialization is daemon-owned, not backend-specific
  (capability-broker.md §7). SECRET writes a single file named by
  scope.secret_name; STORAGE_MOUNT writes four `aws_*` files following
  the drydock-base sync-aws-auth.sh convention.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from drydock.core import CONTAINER_REMOTE_GID, CONTAINER_REMOTE_UID, chown_to_container
from drydock.core.audit import emit_audit
from drydock.core.capability import CapabilityLease, CapabilityType
from drydock.core.policy import (
    CapabilityKind,
    matches_network_reach,
    matches_provision_actions,
    matches_storage_scope,
)
from drydock.core.registry import Registry
from drydock.core.secrets import (
    BackendPermissionDenied,
    BackendUnavailable,
    FileBackend,
    SecretsBackend,
    build_backend,
)
from drydock.core.storage import (
    StorageBackend,
    StorageBackendConfigError,
    StorageBackendPermissionDenied,
    StorageBackendUnavailable,
    StorageCredential,
)
from drydock.wsd.server import _RpcError

logger = logging.getLogger(__name__)

# Matches the existing `ws secret set` Phase-1 hardening (cli/secret.py).
# Stricter than the design doc's [a-z0-9_]{1,64} for compatibility with
# secret names users have already stored on disk.
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_DOCKER_EXEC_TIMEOUT = 10


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Phase A1 (conservative): every capability request creates a side-effect
# amendment record so the principal can review the audit trail via
# `ws amendment list`. Successful grants → status='auto_approved'.
# Policy-failures (narrowness, capability-not-granted) → status='escalated'.
# Infrastructure failures (backend down, materialization OS error) do
# NOT emit amendments — those are operational issues, not policy decisions.
#
# This is a side-effect only — the caller's response shape is unchanged.
# Phase A1-full (envelope-as-request, callers get amendment_id back) is
# a deferred follow-up; Phase A1-conservative gives the principal
# visibility without breaking any caller.
def _emit_capability_amendment(
    registry: Registry,
    *,
    kind: str,
    request: dict,
    caller_desk_id: str,
    status: str,
    reason: str | None = None,
) -> None:
    """Side-effect: record a capability-request amendment. Never raises."""
    try:
        registry.create_amendment(
            kind=kind,
            request=request,
            proposed_by_type="dockworker",
            proposed_by_id=caller_desk_id,
            drydock_id=caller_desk_id,
            reason=reason,
            status=status,
        )
    except Exception:
        # Amendment recording is observability — must not crash the
        # capability handler. Log but don't escalate.
        logger.exception(
            "wsd: failed to emit capability amendment "
            "(caller=%s kind=%s status=%s) — capability path continues",
            caller_desk_id, kind, status,
        )


def _policy_list(policy_row: dict, key: str) -> list:
    """Pull a JSON-encoded list out of a desk_policy row, defaulting to [].

    Every policy column we read in this module is stored as JSON-text and
    represents a list (or set, treated as list at the wire boundary). This
    is the single place that knows about that encoding.
    """
    return json.loads(policy_row.get(key) or "[]")


def _check_capability(
    policy_row: dict, kind: "CapabilityKind", *, fix_hint: str,
) -> None:
    """Enforce that the Dock has been granted `kind` in its capabilities list.

    Raises _RpcError(capability_not_granted) on failure. The fix_hint is the
    user-facing remediation string surfaced in the error data — usually
    "Grant <kind.value> in the Dock's project YAML capabilities" but worded
    per call site so it can name the right capability.
    """
    capabilities = {CapabilityKind(v) for v in _policy_list(policy_row, "capabilities")}
    if kind not in capabilities:
        raise _RpcError(
            code=-32005, message="capability_not_granted",
            data={"missing": kind.value, "fix": fix_hint},
        )


def request_capability(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    backend_name: str = "file",
    backend: SecretsBackend | None = None,
    storage_backend: StorageBackend | None = None,
) -> dict:
    if caller_desk_id is None:
        # The dispatcher should have rejected this already (requires_auth=True),
        # but defense-in-depth: handler must not run without a subject.
        raise _RpcError(code=-32004, message="unauthenticated", data={"reason": "no_caller"})

    spec = _validate_request_params(params)
    request_type = spec["type"]

    if request_type == "SECRET":
        return _handle_secret_request(
            spec,
            caller_desk_id=caller_desk_id,
            request_id=request_id,
            registry_path=registry_path,
            secrets_root=secrets_root,
            backend_name=backend_name,
            backend=backend,
        )
    if request_type == "STORAGE_MOUNT":
        return _handle_storage_request(
            spec,
            caller_desk_id=caller_desk_id,
            request_id=request_id,
            registry_path=registry_path,
            secrets_root=secrets_root,
            storage_backend=storage_backend,
        )
    if request_type == "INFRA_PROVISION":
        return _handle_provision_request(
            spec,
            caller_desk_id=caller_desk_id,
            request_id=request_id,
            registry_path=registry_path,
            secrets_root=secrets_root,
            storage_backend=storage_backend,
        )
    if request_type == "NETWORK_REACH":
        return _handle_network_reach_request(
            spec,
            caller_desk_id=caller_desk_id,
            request_id=request_id,
            registry_path=registry_path,
        )
    # _validate_request_params already rejects unsupported types; defense in depth.
    raise _RpcError(code=-32013, message="capability_unsupported",
                    data={"type": request_type})


def _handle_network_reach_request(
    spec: dict,
    *,
    caller_desk_id: str,
    request_id: str | int | None,
    registry_path: Path,
) -> dict:
    """Issue a NETWORK_REACH lease: open the Dock's container firewall to
    one (domain, port) pair via the in-container add-allowed-domain.sh
    helper. Additive-only (V1) per docs/design/network-reach.md.

    Defaults committed in network-reach.md §entitlement-model:
    - Empty `delegatable_network_reach` = no dynamic opens (deny-all).
    - Wildcard "*" allowed; audited explicitly.
    - Default port allowlist [80, 443] when `network_reach_ports` is empty.
    """
    registry = Registry(db_path=registry_path)
    try:
        policy_row = registry.load_desk_policy(caller_desk_id)
        if policy_row is None:
            raise _RpcError(code=-32001, message="desk_not_found",
                            data={"desk_id": caller_desk_id})

        _check_capability(
            policy_row, CapabilityKind.REQUEST_NETWORK_REACH,
            fix_hint="Grant request_network_reach in the Dock's project YAML capabilities",
        )

        granted_domains = _policy_list(policy_row, "delegatable_network_reach")
        granted_ports = _policy_list(policy_row, "network_reach_ports")
        allowed, reason = matches_network_reach(
            spec["domain"], spec["port"], granted_domains, granted_ports,
        )
        if not allowed:
            audit_detail = {
                "rule": "network_reach",
                "subreason": reason,
                "requested": {"domain": spec["domain"], "port": spec["port"]},
                "granted_domains": granted_domains,
                "granted_ports": granted_ports or [80, 443],
            }
            emit_audit(
                "lease.denied",
                principal=caller_desk_id,
                request_id=request_id,
                method="RequestCapability",
                result="denied",
                details={"type": "NETWORK_REACH", **audit_detail},
            )
            # Suggest a covering glob only when the request has 3+ labels —
            # for 2-label FQDNs like "github.com" the glob "*.com" is far too
            # broad to ever be a sensible suggestion. Surface the bare domain
            # as the safe default in that case.
            labels = spec["domain"].split(".")
            if len(labels) >= 3:
                glob_hint = f"or a covering glob like '*.{'.'.join(labels[1:])}'"
            else:
                glob_hint = "(no covering glob suggested for short FQDNs — add the bare domain)"
            fix_map = {
                "no_entitlement":
                    "Add at least one entry to delegatable_network_reach in the project YAML",
                "domain_not_entitled":
                    f"Add '{spec['domain']}' {glob_hint} to delegatable_network_reach",
                "port_not_entitled":
                    f"Add port {spec['port']} to network_reach_ports (default allowlist is [80, 443])",
            }
            _emit_capability_amendment(
                registry, kind="network_reach",
                request={"domain": spec["domain"], "port": spec["port"]},
                caller_desk_id=caller_desk_id, status="escalated",
                reason=f"narrowness_violated: {reason}",
            )
            raise _RpcError(
                code=-32006, message="narrowness_violated",
                data={**audit_detail, "fix": fix_map.get(reason, "Tighten/widen the Dock's network_reach policy")},
            )

        # Wildcard grants are audited at INFO with explicit flag so
        # ungated desks don't go dark in operation.
        is_wildcard_grant = "*" in granted_domains

        workspace = _lookup_workspace(registry, caller_desk_id)
        if workspace is None or not workspace.container_id:
            raise _RpcError(code=-32010, message="desk_not_running",
                            data={"desk_id": caller_desk_id})

        try:
            add_result = _materialize_network_reach(
                workspace.container_id, spec["domain"], spec["port"],
            )
        except RuntimeError as exc:
            raise _RpcError(code=-32011, message="materialization_failed",
                            data={"detail": str(exc)})

        if not add_result.get("ok"):
            err = add_result.get("error", "add_failed")
            emit_audit(
                "lease.denied",
                principal=caller_desk_id,
                request_id=request_id,
                method="RequestCapability",
                result="denied",
                details={"type": "NETWORK_REACH", "error": err, "domain": spec["domain"]},
            )
            fix = ("DNS resolution failed — check that the domain is reachable from the Harbor"
                   if err == "dns_resolution_failed"
                   else "Check container logs at /tmp/firewall-add.log for ipset/iptables errors")
            raise _RpcError(
                code=-32011, message="materialization_failed",
                data={"detail": add_result, "fix": fix},
            )

        lease_scope: dict = {
            "domain": spec["domain"],
            "port": spec["port"],
            "resolved_ips": list(add_result.get("added", [])) + list(add_result.get("already_present", [])),
        }
        lease = CapabilityLease(
            lease_id=f"nr_{uuid4().hex}",
            desk_id=caller_desk_id,
            type=CapabilityType.NETWORK_REACH,
            scope=lease_scope,
            issued_at=_utc_now(),
            expiry=None,    # additive-only V1; container restart wipes
            issuer="wsd",
        )
        registry.insert_lease(lease)
        _emit_capability_amendment(
            registry, kind="network_reach",
            request={"domain": spec["domain"], "port": spec["port"]},
            caller_desk_id=caller_desk_id, status="auto_approved",
            reason="within_policy" + (" (wildcard grant)" if is_wildcard_grant else ""),
        )
        logger.info(
            "wsd: NETWORK_REACH lease issued lease_id=%s desk_id=%s domain=%s port=%s wildcard=%s",
            lease.lease_id, caller_desk_id, spec["domain"], spec["port"], is_wildcard_grant,
        )
        emit_audit(
            "lease.issued",
            principal=caller_desk_id,
            request_id=request_id,
            method="RequestCapability",
            result="ok",
            details={
                "lease_id": lease.lease_id,
                "desk_id": caller_desk_id,
                "type": lease.type.value,
                "scope": dict(lease.scope),
                "expiry": None,
                "wildcard_grant": is_wildcard_grant,
            },
        )
        return lease.to_wire()
    finally:
        registry.close()


def _materialize_network_reach(
    container_id: str, domain: str, port: int,
) -> dict:
    """Invoke add-allowed-domain.sh inside the container and parse its
    JSON output. Helper script ships in drydock-base; runs as root for
    sudo access to ipset/iptables.
    """
    proc = subprocess.run(
        ["docker", "exec", "--user", "root", container_id,
         "/usr/local/bin/add-allowed-domain.sh", domain, str(port)],
        capture_output=True, text=True, timeout=_DOCKER_EXEC_TIMEOUT,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        # Helper missing entirely (old base image) or hard exec failure.
        raise RuntimeError(
            f"add-allowed-domain.sh failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or 'no output'}"
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"add-allowed-domain.sh returned non-JSON: {exc}; "
            f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
        )
    return result


def _handle_secret_request(
    spec: dict,
    *,
    caller_desk_id: str,
    request_id: str | int | None,
    registry_path: Path,
    secrets_root: Path,
    backend_name: str,
    backend: SecretsBackend | None,
) -> dict:
    backend = backend or build_backend(backend_name, secrets_root=secrets_root)
    registry = Registry(db_path=registry_path)
    try:
        policy_row = registry.load_desk_policy(caller_desk_id)
        if policy_row is None:
            raise _RpcError(code=-32001, message="desk_not_found",
                            data={"desk_id": caller_desk_id})

        _check_capability(
            policy_row, CapabilityKind.REQUEST_SECRET_LEASES,
            fix_hint="Grant request_secret_leases in the Dock's project YAML capabilities",
        )

        entitlements = set(_policy_list(policy_row, "delegatable_secrets"))
        if spec["secret_name"] not in entitlements:
            _emit_capability_amendment(
                registry, kind="secret_grant",
                request={"secret_name": spec["secret_name"],
                         "source_desk_id": spec.get("source_desk_id")},
                caller_desk_id=caller_desk_id, status="escalated",
                reason="narrowness_violated: secret_not_entitled",
            )
            raise _RpcError(
                code=-32006, message="narrowness_violated",
                data={
                    "rule": "secret_entitlement",
                    "requested": spec["secret_name"],
                    "fix": "Add the secret to delegatable_secrets in the project YAML",
                },
            )

        # Resolve which desk's secret dir to read from.
        source_desk_id = spec.get("source_desk_id")
        fetch_desk_id = source_desk_id or caller_desk_id
        is_cross_desk = source_desk_id is not None and source_desk_id != caller_desk_id

        if is_cross_desk:
            # Validate source desk exists.
            source_policy = registry.load_desk_policy(source_desk_id)
            if source_policy is None:
                raise _RpcError(
                    code=-32001, message="source_desk_not_found",
                    data={"source_desk_id": source_desk_id},
                )

        try:
            payload = backend.fetch(spec["secret_name"], fetch_desk_id)
        except BackendPermissionDenied as exc:
            raise _RpcError(code=-32007, message="backend_permission_denied",
                            data={"detail": str(exc)})
        except BackendUnavailable as exc:
            raise _RpcError(code=-32008, message="backend_unavailable",
                            data={"detail": str(exc), "retry": True})

        if payload is None:
            fix_desk = fetch_desk_id
            raise _RpcError(
                code=-32009, message="backend_missing_secret",
                data={
                    "secret_name": spec["secret_name"],
                    "fix": f"ws secret set {fix_desk} {spec['secret_name']} < value",
                },
            )

        # Materialization: make the secret bytes available at
        # /run/secrets/<name> inside the caller's container.
        #
        # Same-Dock + file-backed (V2.0): the overlay bind-mounts
        # ~/.drydock/secrets/<caller_desk_id>/ at /run/secrets/ read-only.
        # The file is already visible. No materialization needed.
        #
        # Cross-Dock + file-backed (V2.1): the source desk's file is NOT
        # in the caller's bind mount. The daemon writes the bytes into
        # the CALLER's host secret dir (on the host filesystem). The bind
        # mount makes it visible in the caller's container immediately.
        # On release, daemon removes the file from the caller's dir.
        #
        # Non-file backends: active docker-exec materialization.
        workspace = _lookup_workspace(registry, caller_desk_id)
        if workspace is None or not workspace.container_id:
            raise _RpcError(code=-32010, message="desk_not_running",
                            data={"desk_id": caller_desk_id})

        if is_cross_desk and isinstance(backend, FileBackend):
            # Cross-Dock file-backed: write source bytes into caller's
            # host secret dir so the bind mount picks them up.
            try:
                _materialize_to_host_secret_dir(
                    secrets_root, caller_desk_id, spec["secret_name"], payload,
                )
            except OSError as exc:
                raise _RpcError(code=-32011, message="materialization_failed",
                                data={"detail": str(exc)})
        elif not isinstance(backend, FileBackend):
            # Non-file backends: active docker-exec materialization.
            try:
                _materialize_secret(workspace.container_id, spec["secret_name"], payload)
            except RuntimeError as exc:
                raise _RpcError(code=-32011, message="materialization_failed",
                                data={"detail": str(exc)})
        # else: same-desk file-backed — already visible via bind mount.

        lease_scope: dict = {"secret_name": spec["secret_name"]}
        if source_desk_id:
            lease_scope["source_desk_id"] = source_desk_id

        lease = CapabilityLease(
            lease_id=f"ls_{uuid4().hex}",
            desk_id=caller_desk_id,
            type=CapabilityType.SECRET,
            scope=lease_scope,
            issued_at=_utc_now(),
            expiry=None,
            issuer="wsd",
        )
        registry.insert_lease(lease)
        _emit_capability_amendment(
            registry, kind="secret_grant",
            request={"secret_name": spec["secret_name"],
                     "source_desk_id": source_desk_id},
            caller_desk_id=caller_desk_id, status="auto_approved",
            reason="within_policy",
        )
        logger.info(
            "wsd: capability lease issued lease_id=%s desk_id=%s secret=%s",
            lease.lease_id, caller_desk_id, spec["secret_name"],
        )
        emit_audit(
            "lease.issued",
            principal=caller_desk_id,
            request_id=request_id,
            method="RequestCapability",
            result="ok",
            details={
                "lease_id": lease.lease_id,
                "desk_id": caller_desk_id,
                "type": lease.type.value,
                "scope": dict(lease.scope),
                "expiry": None,
            },
        )
        return lease.to_wire()
    finally:
        registry.close()


def _handle_storage_request(
    spec: dict,
    *,
    caller_desk_id: str,
    request_id: str | int | None,
    registry_path: Path,
    secrets_root: Path,
    storage_backend: StorageBackend | None,
) -> dict:
    """Issue a STORAGE_MOUNT lease: scoped AWS STS credentials materialized
    into the caller's secret dir as aws_* files.

    Phase 1 gate: coarse REQUEST_STORAGE_LEASES capability. Per-bucket
    narrowness (delegatable_storage_scopes) is Phase 1b.
    """
    if storage_backend is None:
        raise _RpcError(
            code=-32015, message="storage_backend_not_configured",
            data={
                "fix": "Set [storage] backend = 'sts' and role_arn = '...' in ~/.drydock/wsd.toml",
            },
        )

    registry = Registry(db_path=registry_path)
    try:
        # One active AWS lease per drydock (STORAGE_MOUNT + INFRA_PROVISION
        # share the same aws_* materialization slots — issuing either
        # overwrites prior creds, so stale "active" rows would make release
        # cleanup unreliable). Auto-revoke prior on new issue.
        while True:
            prior = registry.find_active_aws_lease(caller_desk_id)
            if prior is None:
                break
            registry.revoke_lease(prior.lease_id, "superseded")
            emit_audit(
                "lease.released",
                principal=caller_desk_id,
                request_id=request_id,
                method="RequestCapability",
                result="ok",
                details={
                    "lease_id": prior.lease_id,
                    "reason": "superseded_by_new_storage_lease",
                },
            )
            logger.info(
                "wsd: superseded prior storage lease %s on new issue", prior.lease_id,
            )

        policy_row = registry.load_desk_policy(caller_desk_id)
        if policy_row is None:
            raise _RpcError(code=-32001, message="desk_not_found",
                            data={"desk_id": caller_desk_id})

        _check_capability(
            policy_row, CapabilityKind.REQUEST_STORAGE_LEASES,
            fix_hint="Grant request_storage_leases in the Dock's project YAML capabilities",
        )

        # Phase 1b: per-bucket narrowness. Empty list in the registry
        # means "no narrowness declared" — capability gate alone governs,
        # matching pre-Phase-1b behavior. Once declared, every request
        # must match at least one scope.
        granted_scopes = _policy_list(policy_row, "delegatable_storage_scopes")
        if not matches_storage_scope(
            {"bucket": spec["bucket"], "prefix": spec["prefix"], "mode": spec["mode"]},
            granted_scopes,
        ):
            _emit_capability_amendment(
                registry, kind="storage_grant",
                request={"bucket": spec["bucket"], "prefix": spec["prefix"],
                         "mode": spec["mode"]},
                caller_desk_id=caller_desk_id, status="escalated",
                reason="narrowness_violated: storage_scope_not_entitled",
            )
            raise _RpcError(
                code=-32006, message="narrowness_violated",
                data={
                    "rule": "storage_scope",
                    "requested": {
                        "bucket": spec["bucket"],
                        "prefix": spec["prefix"],
                        "mode": spec["mode"],
                    },
                    "granted": granted_scopes,
                    "fix": "Add a matching entry to delegatable_storage_scopes in the project YAML "
                           "(e.g. 's3://bucket/prefix/*' or 'rw:s3://bucket/prefix/*')",
                },
            )

        workspace = _lookup_workspace(registry, caller_desk_id)
        if workspace is None or not workspace.container_id:
            raise _RpcError(code=-32010, message="desk_not_running",
                            data={"desk_id": caller_desk_id})

        try:
            cred = storage_backend.mint(
                desk_id=caller_desk_id,
                bucket=spec["bucket"],
                prefix=spec["prefix"],
                mode=spec["mode"],
            )
        except StorageBackendConfigError as exc:
            raise _RpcError(code=-32016, message="storage_backend_config_error",
                            data={"detail": str(exc)})
        except StorageBackendPermissionDenied as exc:
            raise _RpcError(code=-32007, message="backend_permission_denied",
                            data={"detail": str(exc)})
        except StorageBackendUnavailable as exc:
            raise _RpcError(code=-32008, message="backend_unavailable",
                            data={"detail": str(exc), "retry": True})

        try:
            _materialize_storage_credentials(secrets_root, caller_desk_id, cred)
        except OSError as exc:
            raise _RpcError(code=-32011, message="materialization_failed",
                            data={"detail": str(exc)})

        lease_scope: dict = {
            "bucket": spec["bucket"],
            "prefix": spec["prefix"],
            "mode": spec["mode"],
            "expiration": cred.expiration.isoformat(),
        }
        lease = CapabilityLease(
            lease_id=f"ls_{uuid4().hex}",
            desk_id=caller_desk_id,
            type=CapabilityType.STORAGE_MOUNT,
            scope=lease_scope,
            issued_at=_utc_now(),
            expiry=cred.expiration,
            issuer="wsd",
        )
        registry.insert_lease(lease)
        _emit_capability_amendment(
            registry, kind="storage_grant",
            request={"bucket": spec["bucket"], "prefix": spec["prefix"],
                     "mode": spec["mode"]},
            caller_desk_id=caller_desk_id, status="auto_approved",
            reason="within_policy",
        )
        logger.info(
            "wsd: storage lease issued lease_id=%s desk_id=%s bucket=%s prefix=%s mode=%s",
            lease.lease_id, caller_desk_id, spec["bucket"], spec["prefix"], spec["mode"],
        )
        emit_audit(
            "lease.issued",
            principal=caller_desk_id,
            request_id=request_id,
            method="RequestCapability",
            result="ok",
            details={
                "lease_id": lease.lease_id,
                "desk_id": caller_desk_id,
                "type": lease.type.value,
                "scope": dict(lease.scope),
                "expiry": cred.expiration.isoformat(),
            },
        )
        return lease.to_wire()
    finally:
        registry.close()


def _handle_provision_request(
    spec: dict,
    *,
    caller_desk_id: str,
    request_id: str | int | None,
    registry_path: Path,
    secrets_root: Path,
    storage_backend: StorageBackend | None,
) -> dict:
    """Issue an INFRA_PROVISION lease: AWS STS credentials narrowed to an
    IAM action allow-list, materialized as aws_* files.

    Reuses the STS assume-role path (StorageBackend.mint_provision).
    Materializes to the same aws_* filenames as STORAGE_MOUNT — a drydock
    holds at most one active AWS lease at a time (prior is superseded).
    """
    if storage_backend is None:
        raise _RpcError(
            code=-32015, message="storage_backend_not_configured",
            data={"fix": "Set [storage] backend = 'sts' and role_arn = '...' in ~/.drydock/wsd.toml"},
        )

    registry = Registry(db_path=registry_path)
    try:
        while True:
            prior = registry.find_active_aws_lease(caller_desk_id)
            if prior is None:
                break
            registry.revoke_lease(prior.lease_id, "superseded")
            emit_audit(
                "lease.released",
                principal=caller_desk_id,
                request_id=request_id,
                method="RequestCapability",
                result="ok",
                details={"lease_id": prior.lease_id, "reason": "superseded_by_new_provision_lease"},
            )

        policy_row = registry.load_desk_policy(caller_desk_id)
        if policy_row is None:
            raise _RpcError(code=-32001, message="desk_not_found",
                            data={"desk_id": caller_desk_id})

        _check_capability(
            policy_row, CapabilityKind.REQUEST_PROVISION_LEASES,
            fix_hint="Grant request_provision_leases in the Dock's project YAML capabilities",
        )

        granted = _policy_list(policy_row, "delegatable_provision_scopes")
        if not matches_provision_actions(spec["actions"], granted):
            _emit_capability_amendment(
                registry, kind="provision_grant",
                request={"actions": list(spec["actions"])},
                caller_desk_id=caller_desk_id, status="escalated",
                reason="narrowness_violated: provision_actions_not_entitled",
            )
            raise _RpcError(
                code=-32006, message="narrowness_violated",
                data={
                    "rule": "provision_actions",
                    "requested": spec["actions"],
                    "granted": granted,
                    "fix": "Add matching IAM action globs to delegatable_provision_scopes in the project YAML",
                },
            )

        workspace = _lookup_workspace(registry, caller_desk_id)
        if workspace is None or not workspace.container_id:
            raise _RpcError(code=-32010, message="desk_not_running",
                            data={"desk_id": caller_desk_id})

        try:
            cred = storage_backend.mint_provision(
                desk_id=caller_desk_id,
                actions=spec["actions"],
            )
        except StorageBackendConfigError as exc:
            raise _RpcError(code=-32016, message="storage_backend_config_error",
                            data={"detail": str(exc)})
        except StorageBackendPermissionDenied as exc:
            raise _RpcError(code=-32007, message="backend_permission_denied",
                            data={"detail": str(exc)})
        except StorageBackendUnavailable as exc:
            raise _RpcError(code=-32008, message="backend_unavailable",
                            data={"detail": str(exc), "retry": True})

        try:
            _materialize_storage_credentials(secrets_root, caller_desk_id, cred)
        except OSError as exc:
            raise _RpcError(code=-32011, message="materialization_failed",
                            data={"detail": str(exc)})

        lease_scope = {
            "actions": list(spec["actions"]),
            "expiration": cred.expiration.isoformat(),
        }
        lease = CapabilityLease(
            lease_id=f"ls_{uuid4().hex}",
            desk_id=caller_desk_id,
            type=CapabilityType.INFRA_PROVISION,
            scope=lease_scope,
            issued_at=_utc_now(),
            expiry=cred.expiration,
            issuer="wsd",
        )
        registry.insert_lease(lease)
        _emit_capability_amendment(
            registry, kind="provision_grant",
            request={"actions": list(spec["actions"])},
            caller_desk_id=caller_desk_id, status="auto_approved",
            reason="within_policy",
        )
        logger.info(
            "wsd: provision lease issued lease_id=%s desk_id=%s actions=%s",
            lease.lease_id, caller_desk_id, spec["actions"],
        )
        emit_audit(
            "lease.issued",
            principal=caller_desk_id,
            request_id=request_id,
            method="RequestCapability",
            result="ok",
            details={
                "lease_id": lease.lease_id,
                "desk_id": caller_desk_id,
                "type": lease.type.value,
                "scope": dict(lease.scope),
                "expiry": cred.expiration.isoformat(),
            },
        )
        return lease.to_wire()
    finally:
        registry.close()


def release_capability(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
) -> dict:
    if caller_desk_id is None:
        raise _RpcError(code=-32004, message="unauthenticated", data={"reason": "no_caller"})

    if not isinstance(params, dict):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "params must be an object"})
    lease_id = params.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "lease_id required (string)"})

    registry = Registry(db_path=registry_path)
    try:
        lease = registry.get_lease(lease_id)
        if lease is None:
            raise _RpcError(code=-32012, message="lease_not_found",
                            data={"lease_id": lease_id})
        if lease.desk_id != caller_desk_id:
            raise _RpcError(code=-32012, message="lease_not_found",
                            data={"lease_id": lease_id})

        revoked = registry.revoke_lease(lease_id, "released")

        # Cleanup materialized cross-Dock secrets.
        # Same-Dock file-backed: secret lives in the Dock's own bind mount,
        # daemon doesn't own it (ws secret set does). Leave it.
        # Cross-Dock file-backed: daemon copied bytes into the caller's
        # secret dir. On release, remove the copy so the caller loses
        # access through the bind mount.
        if lease.type == CapabilityType.SECRET and revoked:
            source_desk_id = lease.scope.get("source_desk_id")
            secret_name = lease.scope.get("secret_name")
            is_cross_desk = (
                isinstance(source_desk_id, str)
                and source_desk_id != caller_desk_id
            )
            if is_cross_desk and isinstance(secret_name, str):
                # Only remove if no other active lease still grants the
                # same cross-Dock secret to this caller.
                still_active = registry.find_active_secret_lease(
                    caller_desk_id, secret_name,
                )
                if still_active is None:
                    _remove_from_host_secret_dir(
                        secrets_root, caller_desk_id, secret_name,
                    )

        # Storage lease cleanup. The daemon materialized aws_* files into
        # the caller's host secret dir; remove them so the worker's AWS
        # SDK can't keep using expired-or-revoked creds.
        #
        # Single-lease-at-a-time semantic today: one active STORAGE_MOUNT
        # per drydock overwrites prior creds in place. On release of the
        # last active lease we drop the files entirely.
        if lease.type in (CapabilityType.STORAGE_MOUNT, CapabilityType.INFRA_PROVISION) and revoked:
            still_active_aws = registry.find_active_aws_lease(caller_desk_id)
            if still_active_aws is None:
                _remove_storage_credentials(secrets_root, caller_desk_id)

        # NETWORK_REACH release is bookkeeping-only in V1 (additive-only
        # firewall model per network-reach.md). The ipset entry stays put
        # until container restart wipes it; calling ReleaseCapability
        # marks the lease revoked and emits an audit event but does NOT
        # close the firewall. Per-IP reaper is a resource-ceilings.md
        # Phase C deliverable.

        if revoked:
            emit_audit(
                "lease.released",
                principal=caller_desk_id,
                request_id=request_id,
                method="ReleaseCapability",
                result="ok",
                details={
                    "lease_id": lease_id,
                    "reason": "client_release",
                    "source_desk_id": lease.scope.get("source_desk_id"),
                },
            )
        return {"lease_id": lease_id, "revoked": revoked}
    finally:
        registry.close()


_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.\-/]{0,256}$")
_STORAGE_MODES = {"ro", "rw"}
_SUPPORTED_CAPABILITY_TYPES = {"SECRET", "STORAGE_MOUNT", "INFRA_PROVISION", "NETWORK_REACH"}
# Domain shape for NETWORK_REACH scope. Mirrors add-allowed-domain.sh's
# in-container check; we validate before invoking docker exec so a malformed
# request never reaches a privileged shell.
_NETWORK_REACH_DOMAIN_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)+$"
)
_IAM_ACTION_RE = re.compile(r"^[A-Za-z0-9*]+:[A-Za-z0-9*]+$|^\*$")


def _validate_request_params(params: object) -> dict:
    if not isinstance(params, dict):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "params must be an object"})

    type_str = params.get("type")
    if type_str not in _SUPPORTED_CAPABILITY_TYPES:
        if type_str in {ct.value for ct in CapabilityType}:
            raise _RpcError(code=-32013, message="capability_unsupported",
                            data={"type": type_str,
                                  "fix": f"Supported types: {sorted(_SUPPORTED_CAPABILITY_TYPES)}"})
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "type must be one of CapabilityType",
                              "got": type_str})

    scope = params.get("scope")
    if not isinstance(scope, dict):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope must be an object"})

    if type_str == "SECRET":
        return _validate_secret_scope(scope)
    if type_str == "STORAGE_MOUNT":
        return _validate_storage_scope(scope)
    if type_str == "INFRA_PROVISION":
        return _validate_provision_scope(scope)
    if type_str == "NETWORK_REACH":
        return _validate_network_reach_scope(scope)
    # Unreachable — _SUPPORTED_CAPABILITY_TYPES exhausted above.
    raise _RpcError(code=-32013, message="capability_unsupported",
                    data={"type": type_str})


def _validate_network_reach_scope(scope: dict) -> dict:
    """NETWORK_REACH scope shape: {domain, port?}.

    Domain must be a lowercased FQDN with no wildcards or shell metachars
    (the entitlement *patterns* on the Dock policy may include `*.x.com`
    or `*`; the *request* is always a concrete domain). Port defaults to
    443 and must be in 1..65535.
    """
    domain = scope.get("domain")
    if not isinstance(domain, str):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.domain must be a string"})
    canonical = domain.strip().lower().rstrip(".")
    if not _NETWORK_REACH_DOMAIN_RE.match(canonical):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.domain must be a valid lowercase FQDN",
                              "got": domain})

    port = scope.get("port", 443)
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.port must be an integer in 1..65535",
                              "got": port})

    return {"type": "NETWORK_REACH", "domain": canonical, "port": port}


def _validate_secret_scope(scope: dict) -> dict:
    secret_name = scope.get("secret_name")
    if not isinstance(secret_name, str) or not _SECRET_NAME_RE.match(secret_name):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.secret_name must match [A-Za-z0-9_.-]{1,64}"})

    # V2.1: optional source_desk_id for cross-Dock secret delegation.
    # When present, the daemon reads from the source desk's secret dir
    # instead of the caller's. The caller must still have the secret in
    # its own entitlements (the capability broker gate is on the CALLER's
    # policy, not the source's).
    source_desk_id = scope.get("source_desk_id")
    if source_desk_id is not None and not isinstance(source_desk_id, str):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.source_desk_id must be a string"})

    return {"type": "SECRET", "secret_name": secret_name, "source_desk_id": source_desk_id}


def _validate_storage_scope(scope: dict) -> dict:
    """V4 Phase 1 scope shape: {bucket, prefix, mode}.

    bucket: S3 bucket-name rules (lowercase, 3-63 chars, no dots-surrounded-by-dashes-strict-form).
    prefix: optional path within the bucket; empty means whole bucket.
    mode: 'ro' or 'rw'. Finer-grained modes reserved.
    """
    bucket = scope.get("bucket")
    if not isinstance(bucket, str) or not _BUCKET_RE.match(bucket):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.bucket must be a valid S3 bucket name (3-63 chars, [a-z0-9.-])"})

    prefix = scope.get("prefix", "")
    if prefix is None:
        prefix = ""
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.prefix must match [A-Za-z0-9_.-/]{0,256}"})

    mode = scope.get("mode", "ro")
    if mode not in _STORAGE_MODES:
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": f"scope.mode must be one of {sorted(_STORAGE_MODES)}",
                              "got": mode})

    return {
        "type": "STORAGE_MOUNT",
        "bucket": bucket,
        "prefix": prefix.strip("/"),
        "mode": mode,
    }


def _validate_provision_scope(scope: dict) -> dict:
    """INFRA_PROVISION scope shape: {actions: [str, ...]}.

    Each action must match `SERVICE:ACTION` with optional `*` in either
    segment, or be the bare wildcard `*`. Bounded at 64 entries per
    request to keep the session policy under AWS's 2048-byte inline limit.
    """
    actions = scope.get("actions")
    if not isinstance(actions, list) or not actions:
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.actions must be a non-empty list"})
    if len(actions) > 64:
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.actions must be <= 64 entries"})
    cleaned: list[str] = []
    for a in actions:
        if not isinstance(a, str) or not _IAM_ACTION_RE.match(a):
            raise _RpcError(code=-32602, message="invalid_params",
                            data={"reason": "scope.actions entries must match 'service:action' (e.g. 's3:CreateBucket', 'iam:*', '*')",
                                  "got": a})
        cleaned.append(a)
    return {"type": "INFRA_PROVISION", "actions": cleaned}


_STORAGE_CRED_FILENAMES = (
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "aws_session_expiration",
)


def _materialize_storage_credentials(
    secrets_root: Path, desk_id: str, cred: "StorageCredential",
) -> None:
    """Write storage lease bytes into the caller's host secret dir.

    Writes 4 files: aws_access_key_id, aws_secret_access_key,
    aws_session_token, aws_session_expiration. The overlay bind-mounts
    ~/.drydock/secrets/<desk_id>/ read-only at /run/secrets/ inside the
    container, so these become immediately visible to the worker.

    Files are chowned to the container's node uid (1000) with mode 0400
    — the daemon runs as root on the Harbor, but the worker inside the
    drydock is uid 1000. Writing as root + mode 0400 would block the
    worker from reading its own leased credential (learned the hard way
    during the first in-desk STORAGE_MOUNT test).

    Writes are not atomic across the 4 files — a worker reading
    mid-write could see inconsistent state. Acceptable today: workers
    poll on aws_session_expiration to check freshness.
    """
    desk_dir = secrets_root / desk_id
    desk_dir.mkdir(parents=True, exist_ok=True)
    payload = cred.to_files()
    for name, value in payload.items():
        target = desk_dir / name
        # Unlink first: the prior lease left files at 0400, which blocks
        # in-place rewrite on non-root Harbors (root bypasses perms; tests
        # and future non-root Harbors don't).
        target.unlink(missing_ok=True)
        target.write_bytes(value)
        chown_to_container(target)
        os.chmod(target, 0o400)


def _remove_storage_credentials(secrets_root: Path, desk_id: str) -> None:
    """Remove the 4 aws_* storage-lease files from the Dock's secret dir."""
    desk_dir = secrets_root / desk_id
    for name in _STORAGE_CRED_FILENAMES:
        target = desk_dir / name
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("wsd: failed to remove %s: %s", target, exc)


def _lookup_workspace(registry: Registry, desk_id: str):
    for ws in registry.list_workspaces():
        if ws.id == desk_id:
            return ws
    return None


def _materialize_secret(container_id: str, secret_name: str, payload: bytes) -> None:
    """Write secret bytes to /run/secrets/<name> in a running container.

    Mode 0400, owned by the container's node user. Overwrites if present.
    Raises RuntimeError on docker-exec failure.
    """
    # Ensure the directory exists (drydock-base creates /run/secrets but
    # we don't assume — defense against base-image divergence). Run as root
    # because /run/secrets is typically root-owned.
    mk = subprocess.run(
        ["docker", "exec", "--user", "root", container_id,
         "mkdir", "-p", "/run/secrets"],
        capture_output=True, text=True, timeout=_DOCKER_EXEC_TIMEOUT,
    )
    if mk.returncode != 0:
        raise RuntimeError(f"mkdir failed: {mk.stderr.strip()}")

    target = f"/run/secrets/{secret_name}"
    write = subprocess.run(
        ["docker", "exec", "-i", "--user", "root", container_id,
         "sh", "-c", f"cat > {target} && chmod 0400 {target} && "
                     f"chown {CONTAINER_REMOTE_UID}:{CONTAINER_REMOTE_GID} {target}"],
        input=payload, capture_output=True, timeout=_DOCKER_EXEC_TIMEOUT,
    )
    if write.returncode != 0:
        stderr = write.stderr.decode("utf-8", errors="replace") if isinstance(write.stderr, bytes) else write.stderr
        raise RuntimeError(f"write failed: {stderr.strip()}")


def _materialize_to_host_secret_dir(
    secrets_root: Path, desk_id: str, secret_name: str, payload: bytes,
) -> None:
    """Write secret bytes into a Dock's host secret directory.

    For cross-Dock file-backed delegation: the source desk's bytes get
    written into the CALLER's secret dir on the host. The overlay's bind
    mount at /run/secrets/ picks them up immediately inside the container.

    File is chowned to the container's node uid (1000) with mode 0400.
    Without chown the worker inside the drydock can't read a root-owned
    0400 file — same class as the STORAGE_MOUNT materialization gotcha.
    """
    desk_dir = secrets_root / desk_id
    desk_dir.mkdir(parents=True, exist_ok=True)
    target = desk_dir / secret_name
    target.write_bytes(payload)
    chown_to_container(target)
    os.chmod(target, 0o400)
    logger.info(
        "wsd: cross-Dock secret materialized at %s (%d bytes)",
        target, len(payload),
    )


def _remove_from_host_secret_dir(
    secrets_root: Path, desk_id: str, secret_name: str,
) -> None:
    """Remove a cross-Dock materialized secret from the host secret dir.

    Best-effort: logs on failure, does not raise. The lease is already
    revoked in the registry; the file is a convenience copy.
    """
    target = secrets_root / desk_id / secret_name
    try:
        target.unlink(missing_ok=True)
        logger.info("wsd: cross-Dock secret removed from %s", target)
    except OSError as exc:
        logger.warning("wsd: failed to remove cross-Dock secret %s: %s", target, exc)


def _remove_materialized_secret(container_id: str, secret_name: str) -> None:
    """Best-effort: remove /run/secrets/<name> in the Dock container.

    Failures are logged, not raised — the lease is already revoked in
    the registry, so the daemon's authoritative grant state is correct
    even if the file removal misses.
    """
    target = f"/run/secrets/{secret_name}"
    try:
        result = subprocess.run(
            ["docker", "exec", "--user", "root", container_id,
             "rm", "-f", target],
            capture_output=True, text=True, timeout=_DOCKER_EXEC_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "wsd: failed to remove %s in %s: %s",
                target, container_id, result.stderr.strip(),
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("wsd: docker exec rm failed for %s: %s", target, exc)
