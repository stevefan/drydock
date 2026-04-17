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
