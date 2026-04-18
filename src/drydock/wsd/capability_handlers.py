"""RequestCapability + ReleaseCapability JSON-RPC handlers.

Per docs/v2-design-capability-broker.md and docs/v2-design-protocol.md:

- Subject desk is derived from the bearer token (caller_desk_id) — never
  taken as an RPC argument. Mitigates a confused-deputy class of bugs.
- V2.0 ships type=SECRET. V2.1 added cross-drydock secret delegation via
  `source_desk_id`. V4 Phase 1 adds type=STORAGE_MOUNT (scoped AWS STS
  credentials); reserved COMPUTE_QUOTA/NETWORK_REACH still reject.
- Entitlement check for SECRET is a trivial subset lookup against the
  desk's `delegatable_secrets` (which doubles as the desk's own
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

from drydock.core import CONTAINER_REMOTE_GID, CONTAINER_REMOTE_UID
from drydock.core.audit import emit_audit
from drydock.core.capability import CapabilityLease, CapabilityType
from drydock.core.policy import CapabilityKind
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
    # _validate_request_params already rejects unsupported types; defense in depth.
    raise _RpcError(code=-32013, message="capability_unsupported",
                    data={"type": request_type})


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

        capabilities_raw = json.loads(policy_row.get("capabilities") or "[]")
        capabilities = {CapabilityKind(value) for value in capabilities_raw}
        if CapabilityKind.REQUEST_SECRET_LEASES not in capabilities:
            raise _RpcError(
                code=-32005, message="capability_not_granted",
                data={
                    "missing": CapabilityKind.REQUEST_SECRET_LEASES.value,
                    "fix": "Grant request_secret_leases in the desk's project YAML capabilities",
                },
            )

        entitlements = set(json.loads(policy_row.get("delegatable_secrets") or "[]"))
        if spec["secret_name"] not in entitlements:
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
        # Same-desk + file-backed (V2.0): the overlay bind-mounts
        # ~/.drydock/secrets/<caller_desk_id>/ at /run/secrets/ read-only.
        # The file is already visible. No materialization needed.
        #
        # Cross-desk + file-backed (V2.1): the source desk's file is NOT
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
            # Cross-desk file-backed: write source bytes into caller's
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
        policy_row = registry.load_desk_policy(caller_desk_id)
        if policy_row is None:
            raise _RpcError(code=-32001, message="desk_not_found",
                            data={"desk_id": caller_desk_id})

        capabilities_raw = json.loads(policy_row.get("capabilities") or "[]")
        capabilities = {CapabilityKind(value) for value in capabilities_raw}
        if CapabilityKind.REQUEST_STORAGE_LEASES not in capabilities:
            raise _RpcError(
                code=-32005, message="capability_not_granted",
                data={
                    "missing": CapabilityKind.REQUEST_STORAGE_LEASES.value,
                    "fix": "Grant request_storage_leases in the desk's project YAML capabilities",
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

        # Cleanup materialized cross-desk secrets.
        # Same-desk file-backed: secret lives in the desk's own bind mount,
        # daemon doesn't own it (ws secret set does). Leave it.
        # Cross-desk file-backed: daemon copied bytes into the caller's
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
                # same cross-desk secret to this caller.
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
        if lease.type == CapabilityType.STORAGE_MOUNT and revoked:
            still_active_storage = registry.find_active_storage_lease(caller_desk_id)
            if still_active_storage is None:
                _remove_storage_credentials(secrets_root, caller_desk_id)

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
_SUPPORTED_CAPABILITY_TYPES = {"SECRET", "STORAGE_MOUNT"}


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
    # Unreachable — _SUPPORTED_CAPABILITY_TYPES exhausted above.
    raise _RpcError(code=-32013, message="capability_unsupported",
                    data={"type": type_str})


def _validate_secret_scope(scope: dict) -> dict:
    secret_name = scope.get("secret_name")
    if not isinstance(secret_name, str) or not _SECRET_NAME_RE.match(secret_name):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.secret_name must match [A-Za-z0-9_.-]{1,64}"})

    # V2.1: optional source_desk_id for cross-desk secret delegation.
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
        target.write_bytes(value)
        _chown_for_container(target)
        os.chmod(target, 0o400)


def _chown_for_container(path: Path) -> None:
    """chown to the drydock-base container's node uid/gid. No-op if not root.

    Non-root Harbor (dev Mac) cannot chown; the file stays owned by the
    Harbor user, which happens to align with the container user only on
    Linux Harbors running as root (Hetzner convention).
    """
    try:
        os.chown(path, CONTAINER_REMOTE_UID, CONTAINER_REMOTE_GID)
    except PermissionError:
        # Mac Harbor / non-root tests — chown needs root. Fine; file
        # ownership only matters when a real container mounts and reads.
        logger.debug("skipped chown on %s (not root)", path)


def _remove_storage_credentials(secrets_root: Path, desk_id: str) -> None:
    """Remove the 4 aws_* storage-lease files from the desk's secret dir."""
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
    """Write secret bytes into a desk's host secret directory.

    For cross-desk file-backed delegation: the source desk's bytes get
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
    _chown_for_container(target)
    os.chmod(target, 0o400)
    logger.info(
        "wsd: cross-desk secret materialized at %s (%d bytes)",
        target, len(payload),
    )


def _remove_from_host_secret_dir(
    secrets_root: Path, desk_id: str, secret_name: str,
) -> None:
    """Remove a cross-desk materialized secret from the host secret dir.

    Best-effort: logs on failure, does not raise. The lease is already
    revoked in the registry; the file is a convenience copy.
    """
    target = secrets_root / desk_id / secret_name
    try:
        target.unlink(missing_ok=True)
        logger.info("wsd: cross-desk secret removed from %s", target)
    except OSError as exc:
        logger.warning("wsd: failed to remove cross-desk secret %s: %s", target, exc)


def _remove_materialized_secret(container_id: str, secret_name: str) -> None:
    """Best-effort: remove /run/secrets/<name> in the desk container.

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
