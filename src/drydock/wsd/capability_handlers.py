"""RequestCapability + ReleaseCapability JSON-RPC handlers (Slice 3c).

Per docs/v2-design-capability-broker.md and docs/v2-design-protocol.md:

- Subject desk is derived from the bearer token (caller_desk_id) — never
  taken as an RPC argument. Mitigates a confused-deputy class of bugs.
- V2 implements only `type=SECRET`; reserved types reject with
  `capability_unsupported`.
- Entitlement check post-spawn is a trivial subset lookup against the
  desk's `delegatable_secrets` (which doubles as the desk's own
  entitlements in the V2 model — see capability-broker.md §4 closing
  note "Post-spawn narrowness is a trivial lookup").
- Lease materialization at /run/secrets/<name> is daemon-owned, not
  backend-specific (capability-broker.md §7).
"""

from __future__ import annotations

import json
import logging
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
) -> dict:
    if caller_desk_id is None:
        # The dispatcher should have rejected this already (requires_auth=True),
        # but defense-in-depth: handler must not run without a subject.
        raise _RpcError(code=-32004, message="unauthenticated", data={"reason": "no_caller"})

    spec = _validate_request_params(params)

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

        try:
            payload = backend.fetch(spec["secret_name"], caller_desk_id)
        except BackendPermissionDenied as exc:
            raise _RpcError(code=-32007, message="backend_permission_denied",
                            data={"detail": str(exc)})
        except BackendUnavailable as exc:
            raise _RpcError(code=-32008, message="backend_unavailable",
                            data={"detail": str(exc), "retry": True})

        if payload is None:
            raise _RpcError(
                code=-32009, message="backend_missing_secret",
                data={
                    "secret_name": spec["secret_name"],
                    "fix": f"ws secret set {caller_desk_id} {spec['secret_name']} < value",
                },
            )

        # Materialization: make the secret bytes available at
        # /run/secrets/<name> inside the running container.
        #
        # For the file-backed backend (V2.0): the overlay already bind-
        # mounts ~/.drydock/secrets/<desk_id>/ at /run/secrets/ (read-only).
        # The FileBackend.fetch() reading from that host path succeeding
        # means the file IS ALREADY VISIBLE inside the container via the
        # bind mount. No docker exec needed — and docker exec would FAIL
        # because the mount is read-only.
        #
        # For future network backends (1Password, Vault, cloud SMs):
        # bytes come from the network, not the host filesystem. Those
        # backends will need active materialization — either writing to
        # a tmpfs overlay at /run/secrets, or changing the bind mount
        # to read-write. That's additive when those backends ship.
        #
        # We still verify the desk is running (container exists) because
        # issuing a lease for a stopped desk is confusing — the caller
        # expects to read /run/secrets/<name> immediately after.
        workspace = _lookup_workspace(registry, caller_desk_id)
        if workspace is None or not workspace.container_id:
            raise _RpcError(code=-32010, message="desk_not_running",
                            data={"desk_id": caller_desk_id})

        if not isinstance(backend, FileBackend):
            # Non-file backends: active materialization into container.
            try:
                _materialize_secret(workspace.container_id, spec["secret_name"], payload)
            except RuntimeError as exc:
                raise _RpcError(code=-32011, message="materialization_failed",
                                data={"detail": str(exc)})

        lease = CapabilityLease(
            lease_id=f"ls_{uuid4().hex}",
            desk_id=caller_desk_id,
            type=CapabilityType.SECRET,
            scope={"secret_name": spec["secret_name"]},
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


def release_capability(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
) -> dict:
    del secrets_root
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
            # Cross-desk lease access — confused-deputy guard. Same code as
            # an unknown lease so we don't leak existence to other desks.
            raise _RpcError(code=-32012, message="lease_not_found",
                            data={"lease_id": lease_id})

        revoked = registry.revoke_lease(lease_id, "released")

        # File-backed note: /run/secrets/ is a read-only bind mount from
        # the host secret dir. The file exists as long as the host file
        # exists — lease revocation doesn't (and can't) remove it from
        # the container. The lease is an audit/authorization record, not
        # a physical access-control mechanism for file-backed secrets.
        #
        # When a non-file backend ships (1P, Vault), it will need active
        # removal here — the materialized file would be on a writable
        # tmpfs, and revoking the lease should clean it up. That's
        # additive when those backends land.
        #
        # For now: revoke the lease record + emit audit. No docker exec.

        if revoked:
            emit_audit(
                "lease.released",
                principal=caller_desk_id,
                request_id=request_id,
                method="ReleaseCapability",
                result="ok",
                details={"lease_id": lease_id, "reason": "client_release"},
            )
        return {"lease_id": lease_id, "revoked": revoked}
    finally:
        registry.close()


def _validate_request_params(params: object) -> dict:
    if not isinstance(params, dict):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "params must be an object"})

    type_str = params.get("type")
    if type_str != "SECRET":
        if type_str in {ct.value for ct in CapabilityType}:
            raise _RpcError(code=-32013, message="capability_unsupported",
                            data={"type": type_str,
                                  "fix": "Only SECRET is implemented in V2.0"})
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "type must be one of CapabilityType",
                              "got": type_str})

    scope = params.get("scope")
    if not isinstance(scope, dict):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope must be an object"})
    secret_name = scope.get("secret_name")
    if not isinstance(secret_name, str) or not _SECRET_NAME_RE.match(secret_name):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "scope.secret_name must match [A-Za-z0-9_.-]{1,64}"})

    return {"secret_name": secret_name}


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
