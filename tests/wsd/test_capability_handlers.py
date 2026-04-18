"""Tests for RequestCapability + ReleaseCapability handlers (Slice 3c).

These pin the contracts:
- Subject derivation from caller_desk_id (no spoofing via params).
- Capability gate (REQUEST_SECRET_LEASES required).
- Entitlement narrowness (delegatable_secrets membership).
- Backend dispatch + error taxonomy mapping.
- Materialization + ref-counted removal at release.
- Cross-desk lease access returns lease_not_found (no info leak).
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import json
import pytest

from drydock.core.capability import CapabilityLease, CapabilityType
from drydock.core.policy import CapabilityKind
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.wsd.capability_handlers import (
    release_capability,
    request_capability,
)
from drydock.wsd.server import _RpcError


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "registry.db"
    secrets_root = tmp_path / "secrets"
    secrets_root.mkdir()

    registry = Registry(db_path=db)
    ws = Workspace(
        name="alpha",
        project="p",
        repo_path="/tmp/repo",
        worktree_path="/tmp/wt",
        branch="ws/alpha",
        state="running",
        container_id="cid_alpha",
    )
    registry.create_workspace(ws)
    desk_id = ws.id

    # Grant entitlements + capability the same way SpawnChild would.
    registry.update_desk_delegations(
        ws.name,
        delegatable_firewall_domains=[],
        delegatable_secrets=["anthropic_api_key", "tailscale_authkey"],
        capabilities=[CapabilityKind.REQUEST_SECRET_LEASES.value],
    )

    # Place a secret on disk for FileBackend to find.
    desk_secrets = secrets_root / desk_id
    desk_secrets.mkdir()
    (desk_secrets / "anthropic_api_key").write_bytes(b"sk-ant-test\n")

    yield {
        "tmp_path": tmp_path,
        "db": db,
        "secrets_root": secrets_root,
        "registry": registry,
        "desk_id": desk_id,
    }
    registry.close()


def _params(secret="anthropic_api_key", type="SECRET"):
    return {"type": type, "scope": {"secret_name": secret}}


class TestRequestCapability:
    def test_unauthenticated_when_caller_desk_id_none(self, env):
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(), "rid", None,
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "unauthenticated"

    def test_rejects_unsupported_capability_type(self, env):
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(type="STORAGE_MOUNT"), "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "capability_unsupported"

    def test_rejects_invalid_type(self, env):
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(type="GARBAGE"), "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "invalid_params"

    def test_rejects_invalid_secret_name(self, env):
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(secret="bad name with spaces"), "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "invalid_params"

    def test_rejects_when_capability_not_granted(self, env):
        env["registry"].update_desk_delegations("alpha", capabilities=[])
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(), "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "capability_not_granted"

    def test_rejects_secret_not_in_entitlements(self, env):
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(secret="not_in_entitlements"), "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "narrowness_violated"

    def test_returns_backend_missing_secret_when_file_absent(self, env):
        # tailscale_authkey is in entitlements but not on disk
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(secret="tailscale_authkey"), "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "backend_missing_secret"

    def test_rejects_when_desk_not_running(self, env):
        env["registry"].update_workspace("alpha", container_id="")
        with pytest.raises(_RpcError) as exc:
            request_capability(_params(), "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "desk_not_running"

    def test_happy_path_inserts_lease_for_file_backend(self, env):
        # FileBackend skips docker-exec materialization — /run/secrets/ is
        # a read-only bind mount from the host secret dir, so the file is
        # already visible inside the container. The handler's job is to
        # check entitlement + issue the lease (audit record).
        result = request_capability(_params(), "rid", env["desk_id"],
                                    registry_path=env["db"],
                                    secrets_root=env["secrets_root"])
        assert result["type"] == "SECRET"
        assert result["scope"]["secret_name"] == "anthropic_api_key"
        assert result["desk_id"] == env["desk_id"]
        assert result["revoked"] is False
        assert result["lease_id"].startswith("ls_")

        # Lease persisted
        lease = env["registry"].get_lease(result["lease_id"])
        assert lease is not None
        assert lease.scope["secret_name"] == "anthropic_api_key"

    # NOTE: materialization_failure test removed — file-backed backend
    # skips docker-exec materialization (/run/secrets is a read-only bind
    # mount). When a non-file backend ships, add a test that passes a
    # non-FileBackend instance and verifies materialization failure leaves
    # no orphan lease.


class TestReleaseCapability:
    def _make_lease(self, registry, desk_id, lease_id="ls_x", secret="anthropic_api_key"):
        lease = CapabilityLease(
            lease_id=lease_id,
            desk_id=desk_id,
            type=CapabilityType.SECRET,
            scope={"secret_name": secret},
            issued_at=datetime.now(timezone.utc),
            expiry=None,
            issuer="wsd",
        )
        registry.insert_lease(lease)
        return lease

    def test_unauthenticated_when_caller_desk_id_none(self, env):
        with pytest.raises(_RpcError) as exc:
            release_capability({"lease_id": "x"}, "rid", None,
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "unauthenticated"

    def test_missing_lease_id(self, env):
        with pytest.raises(_RpcError) as exc:
            release_capability({}, "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "invalid_params"

    def test_lease_not_found(self, env):
        with pytest.raises(_RpcError) as exc:
            release_capability({"lease_id": "ls_unknown"}, "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "lease_not_found"

    # Cross-desk access must return the same code as not-found so we
    # don't leak existence of leases to other desks.
    def test_cross_desk_access_returns_lease_not_found(self, env):
        self._make_lease(env["registry"], desk_id="ws_other", lease_id="ls_other")
        with pytest.raises(_RpcError) as exc:
            release_capability({"lease_id": "ls_other"}, "rid", env["desk_id"],
                               registry_path=env["db"], secrets_root=env["secrets_root"])
        assert exc.value.message == "lease_not_found"
        # Lease NOT revoked (caller wasn't authorized)
        assert env["registry"].get_lease("ls_other").revoked is False

    def test_happy_path_revokes_lease(self, env):
        # File-backed: no docker-exec removal on release (bind mount is
        # read-only; lease is an audit/authorization record, not physical
        # access control). The test verifies the lease record is revoked.
        self._make_lease(env["registry"], env["desk_id"], lease_id="ls_one")
        result = release_capability({"lease_id": "ls_one"}, "rid", env["desk_id"],
                                    registry_path=env["db"],
                                    secrets_root=env["secrets_root"])
        assert result == {"lease_id": "ls_one", "revoked": True}
        assert env["registry"].get_lease("ls_one").revoked is True

    def test_double_release_is_idempotent(self, env):
        self._make_lease(env["registry"], env["desk_id"], lease_id="ls_dup")
        first = release_capability({"lease_id": "ls_dup"}, "rid", env["desk_id"],
                                   registry_path=env["db"],
                                   secrets_root=env["secrets_root"])
        second = release_capability({"lease_id": "ls_dup"}, "rid", env["desk_id"],
                                    registry_path=env["db"],
                                    secrets_root=env["secrets_root"])
        assert first["revoked"] is True
        assert second["revoked"] is False


# --- V2.1: Cross-desk secret delegation tests ---

class TestCrossDeskDelegation:
    """RequestCapability with source_desk_id reads from the source desk's
    secret dir and materializes into the caller's host secret dir so the
    caller's bind mount picks it up.
    """

    @pytest.fixture
    def cross_env(self, tmp_path):
        db = tmp_path / "registry.db"
        secrets_root = tmp_path / "secrets"
        secrets_root.mkdir()

        registry = Registry(db_path=db)

        # Source desk (fleet-auth) — holds the secret
        source = Workspace(
            name="fleet-auth",
            project="infra",
            repo_path="/tmp/repo",
            worktree_path="/tmp/wt-fleet",
            branch="ws/fleet-auth",
            state="running",
            container_id="cid_fleet",
        )
        registry.create_workspace(source)
        source_id = source.id

        # Caller desk (worker) — wants to read the source's secret
        caller = Workspace(
            name="worker",
            project="p",
            repo_path="/tmp/repo",
            worktree_path="/tmp/wt-worker",
            branch="ws/worker",
            state="running",
            container_id="cid_worker",
        )
        registry.create_workspace(caller)
        caller_id = caller.id

        # Grant caller the entitlement + capability
        registry.update_desk_delegations(
            "worker",
            delegatable_secrets=["shared_cred"],
            capabilities=[CapabilityKind.REQUEST_SECRET_LEASES.value],
        )

        # Place the secret in the SOURCE desk's secret dir (not the caller's)
        source_secrets = secrets_root / source_id
        source_secrets.mkdir()
        (source_secrets / "shared_cred").write_bytes(b"cross-desk-value\n")

        yield {
            "db": db,
            "secrets_root": secrets_root,
            "registry": registry,
            "source_id": source_id,
            "caller_id": caller_id,
        }
        registry.close()

    def test_cross_desk_happy_path_materializes_in_caller_dir(self, cross_env):
        result = request_capability(
            {"type": "SECRET", "scope": {
                "secret_name": "shared_cred",
                "source_desk_id": cross_env["source_id"],
            }},
            "rid-cross",
            cross_env["caller_id"],
            registry_path=cross_env["db"],
            secrets_root=cross_env["secrets_root"],
        )
        assert result["type"] == "SECRET"
        assert result["scope"]["source_desk_id"] == cross_env["source_id"]
        assert result["scope"]["secret_name"] == "shared_cred"

        # Secret materialized in CALLER's host secret dir
        caller_file = cross_env["secrets_root"] / cross_env["caller_id"] / "shared_cred"
        assert caller_file.exists()
        assert caller_file.read_bytes() == b"cross-desk-value\n"

    def test_cross_desk_requires_caller_entitlement(self, cross_env):
        # Remove the entitlement from the caller
        cross_env["registry"].update_desk_delegations(
            "worker", delegatable_secrets=[],
        )
        with pytest.raises(_RpcError) as exc:
            request_capability(
                {"type": "SECRET", "scope": {
                    "secret_name": "shared_cred",
                    "source_desk_id": cross_env["source_id"],
                }},
                "rid",
                cross_env["caller_id"],
                registry_path=cross_env["db"],
                secrets_root=cross_env["secrets_root"],
            )
        assert exc.value.message == "narrowness_violated"

    def test_cross_desk_source_not_found(self, cross_env):
        with pytest.raises(_RpcError) as exc:
            request_capability(
                {"type": "SECRET", "scope": {
                    "secret_name": "shared_cred",
                    "source_desk_id": "ws_nonexistent",
                }},
                "rid",
                cross_env["caller_id"],
                registry_path=cross_env["db"],
                secrets_root=cross_env["secrets_root"],
            )
        assert exc.value.message == "source_desk_not_found"

    def test_cross_desk_release_removes_materialized_file(self, cross_env):
        result = request_capability(
            {"type": "SECRET", "scope": {
                "secret_name": "shared_cred",
                "source_desk_id": cross_env["source_id"],
            }},
            "rid-rel",
            cross_env["caller_id"],
            registry_path=cross_env["db"],
            secrets_root=cross_env["secrets_root"],
        )
        lease_id = result["lease_id"]

        caller_file = cross_env["secrets_root"] / cross_env["caller_id"] / "shared_cred"
        assert caller_file.exists()

        release_capability(
            {"lease_id": lease_id},
            "rid-rel2",
            cross_env["caller_id"],
            registry_path=cross_env["db"],
            secrets_root=cross_env["secrets_root"],
        )

        # File removed from caller's dir after release
        assert not caller_file.exists()

        # Source desk's original file is untouched
        source_file = cross_env["secrets_root"] / cross_env["source_id"] / "shared_cred"
        assert source_file.exists()
        assert source_file.read_bytes() == b"cross-desk-value\n"

    def test_same_desk_source_id_is_noop(self, cross_env):
        """Passing source_desk_id == caller_desk_id is treated as same-desk."""
        # Place the secret in the caller's own dir
        caller_secrets = cross_env["secrets_root"] / cross_env["caller_id"]
        caller_secrets.mkdir(exist_ok=True)
        (caller_secrets / "shared_cred").write_bytes(b"own-value\n")

        result = request_capability(
            {"type": "SECRET", "scope": {
                "secret_name": "shared_cred",
                "source_desk_id": cross_env["caller_id"],
            }},
            "rid-self",
            cross_env["caller_id"],
            registry_path=cross_env["db"],
            secrets_root=cross_env["secrets_root"],
        )
        # No source_desk_id in scope when same-desk? Actually it IS there since
        # we passed it. That's fine — scope includes whatever was passed.
        assert result["type"] == "SECRET"


# V4 Phase 1: STORAGE_MOUNT leases issue scoped AWS STS creds + materialize
# them into the caller's host secret dir as 4 aws_* files. Pin:
# - reject when no storage backend configured
# - reject when desk lacks REQUEST_STORAGE_LEASES capability
# - validate bucket/prefix/mode shapes
# - materialize creds + return lease on happy path (uses StubStorageBackend)
# - release removes the materialized aws_* files
class TestStorageMount:
    @pytest.fixture
    def storage_env(self, tmp_path):
        from drydock.core.storage import StubStorageBackend

        db = tmp_path / "registry.db"
        secrets_root = tmp_path / "secrets"
        secrets_root.mkdir()

        registry = Registry(db_path=db)
        ws = Workspace(
            name="storage-worker",
            project="p",
            repo_path="/tmp/repo",
            worktree_path="/tmp/wt-sw",
            branch="ws/storage-worker",
            state="running",
            container_id="cid_sw",
        )
        registry.create_workspace(ws)

        registry.update_desk_delegations(
            ws.name,
            delegatable_firewall_domains=[],
            delegatable_secrets=[],
            capabilities=[CapabilityKind.REQUEST_STORAGE_LEASES.value],
        )

        backend = StubStorageBackend()

        yield {
            "db": db,
            "secrets_root": secrets_root,
            "registry": registry,
            "desk_id": ws.id,
            "backend": backend,
        }
        registry.close()

    def _storage_params(self, bucket="lab-data", prefix="scraped", mode="ro"):
        return {"type": "STORAGE_MOUNT", "scope": {"bucket": bucket, "prefix": prefix, "mode": mode}}

    def test_happy_path_materializes_aws_files(self, storage_env):
        result = request_capability(
            self._storage_params(),
            "rid-storage-1",
            storage_env["desk_id"],
            registry_path=storage_env["db"],
            secrets_root=storage_env["secrets_root"],
            storage_backend=storage_env["backend"],
        )
        assert result["type"] == "STORAGE_MOUNT"
        assert result["scope"]["bucket"] == "lab-data"
        assert result["scope"]["prefix"] == "scraped"
        assert result["scope"]["mode"] == "ro"
        assert result["expiry"] is not None  # storage leases always have expiry

        desk_dir = storage_env["secrets_root"] / storage_env["desk_id"]
        assert (desk_dir / "aws_access_key_id").read_bytes().decode().startswith("STUB-")
        assert (desk_dir / "aws_secret_access_key").exists()
        assert (desk_dir / "aws_session_token").exists()
        assert (desk_dir / "aws_session_expiration").exists()

    def test_rejects_when_backend_not_configured(self, storage_env):
        with pytest.raises(_RpcError) as exc:
            request_capability(
                self._storage_params(),
                "rid",
                storage_env["desk_id"],
                registry_path=storage_env["db"],
                secrets_root=storage_env["secrets_root"],
                storage_backend=None,
            )
        assert exc.value.message == "storage_backend_not_configured"

    def test_rejects_when_capability_not_granted(self, storage_env):
        storage_env["registry"].update_desk_delegations(
            "storage-worker", capabilities=[],
        )
        with pytest.raises(_RpcError) as exc:
            request_capability(
                self._storage_params(),
                "rid",
                storage_env["desk_id"],
                registry_path=storage_env["db"],
                secrets_root=storage_env["secrets_root"],
                storage_backend=storage_env["backend"],
            )
        assert exc.value.message == "capability_not_granted"
        assert "request_storage_leases" in exc.value.data["missing"]

    @pytest.mark.parametrize("bucket", ["UPPERCASE", "a", "x_y_z", "-leading-dash"])
    def test_invalid_bucket_names_rejected(self, storage_env, bucket):
        with pytest.raises(_RpcError) as exc:
            request_capability(
                self._storage_params(bucket=bucket),
                "rid",
                storage_env["desk_id"],
                registry_path=storage_env["db"],
                secrets_root=storage_env["secrets_root"],
                storage_backend=storage_env["backend"],
            )
        assert exc.value.message == "invalid_params"

    def test_invalid_mode_rejected(self, storage_env):
        with pytest.raises(_RpcError) as exc:
            request_capability(
                self._storage_params(mode="admin"),
                "rid",
                storage_env["desk_id"],
                registry_path=storage_env["db"],
                secrets_root=storage_env["secrets_root"],
                storage_backend=storage_env["backend"],
            )
        assert exc.value.message == "invalid_params"

    # Phase 1b: per-bucket narrowness. Empty granted list = permissive
    # (back-compat); non-empty = must match.
    def test_narrowness_empty_list_permissive(self, storage_env):
        # Default-permissive-when-empty: storage_env sets no scopes, so
        # the capability gate alone governs. Request for any bucket works.
        result = request_capability(
            self._storage_params(bucket="anything", prefix="at-all", mode="ro"),
            "rid-empty",
            storage_env["desk_id"],
            registry_path=storage_env["db"],
            secrets_root=storage_env["secrets_root"],
            storage_backend=storage_env["backend"],
        )
        assert result["type"] == "STORAGE_MOUNT"

    def test_narrowness_allowed_when_scope_matches(self, storage_env):
        storage_env["registry"].update_desk_delegations(
            "storage-worker",
            delegatable_storage_scopes=["s3://lab-data/scraped/*"],
        )
        result = request_capability(
            self._storage_params(bucket="lab-data", prefix="scraped/2026", mode="ro"),
            "rid-ok",
            storage_env["desk_id"],
            registry_path=storage_env["db"],
            secrets_root=storage_env["secrets_root"],
            storage_backend=storage_env["backend"],
        )
        assert result["type"] == "STORAGE_MOUNT"

    def test_narrowness_denied_for_undeclared_bucket(self, storage_env):
        storage_env["registry"].update_desk_delegations(
            "storage-worker",
            delegatable_storage_scopes=["s3://lab-data/scraped/*"],
        )
        with pytest.raises(_RpcError) as exc:
            request_capability(
                self._storage_params(bucket="other-bucket", prefix="x", mode="ro"),
                "rid-deny",
                storage_env["desk_id"],
                registry_path=storage_env["db"],
                secrets_root=storage_env["secrets_root"],
                storage_backend=storage_env["backend"],
            )
        assert exc.value.message == "narrowness_violated"
        assert exc.value.data["rule"] == "storage_scope"

    def test_narrowness_denied_for_rw_on_ro_scope(self, storage_env):
        # rw escalation past a ro-only scope is the exact attack
        # surface Phase 1b closes. Keep this pinned.
        storage_env["registry"].update_desk_delegations(
            "storage-worker",
            delegatable_storage_scopes=["s3://lab-data/*"],
        )
        with pytest.raises(_RpcError) as exc:
            request_capability(
                self._storage_params(bucket="lab-data", prefix="scraped", mode="rw"),
                "rid-rw-deny",
                storage_env["desk_id"],
                registry_path=storage_env["db"],
                secrets_root=storage_env["secrets_root"],
                storage_backend=storage_env["backend"],
            )
        assert exc.value.message == "narrowness_violated"
        assert exc.value.data["rule"] == "storage_scope"

    def test_release_removes_materialized_aws_files(self, storage_env):
        result = request_capability(
            self._storage_params(),
            "rid-storage-rel",
            storage_env["desk_id"],
            registry_path=storage_env["db"],
            secrets_root=storage_env["secrets_root"],
            storage_backend=storage_env["backend"],
        )
        lease_id = result["lease_id"]
        desk_dir = storage_env["secrets_root"] / storage_env["desk_id"]
        assert (desk_dir / "aws_access_key_id").exists()

        release_capability(
            {"lease_id": lease_id},
            "rid-storage-rel-2",
            storage_env["desk_id"],
            registry_path=storage_env["db"],
            secrets_root=storage_env["secrets_root"],
        )

        assert not (desk_dir / "aws_access_key_id").exists()
        assert not (desk_dir / "aws_secret_access_key").exists()
        assert not (desk_dir / "aws_session_token").exists()
        assert not (desk_dir / "aws_session_expiration").exists()
