"""Tests for capability-lease persistence (Slice 3b).

Contracts these tests pin:
- Insert/get round-trip preserves every field including JSON scope and
  optional expiry.
- revoke_lease is idempotent (second call returns False; original
  revocation_reason preserved).
- revoke_leases_for_desk cascades to every active lease for the Dock
  and ignores already-revoked rows. This is the destroy-cascade
  contract (capability-broker.md §6a).
- find_active_secret_lease distinguishes per-(desk, secret_name) so the
  release handler can ref-count materialized files.
"""

from datetime import datetime, timezone

import pytest

from drydock.core.capability import CapabilityLease, CapabilityType
from drydock.core.registry import Registry


@pytest.fixture
def registry(tmp_path):
    db = tmp_path / "registry.db"
    reg = Registry(db_path=db)
    yield reg
    reg.close()


def _lease(lease_id="ls_a", desk_id="ws_alpha", secret="sk", revoked=False, reason=None):
    return CapabilityLease(
        lease_id=lease_id,
        desk_id=desk_id,
        type=CapabilityType.SECRET,
        scope={"secret_name": secret},
        issued_at=datetime(2026, 4, 16, 10, tzinfo=timezone.utc),
        expiry=None,
        issuer="wsd",
        revoked=revoked,
        revocation_reason=reason,
    )


class TestLeasePersistence:
    def test_insert_then_get_roundtrip(self, registry):
        original = _lease(lease_id="ls_1", desk_id="ws_a", secret="anthropic_api_key")
        registry.insert_lease(original)
        loaded = registry.get_lease("ls_1")
        assert loaded == original

    def test_get_missing_returns_none(self, registry):
        assert registry.get_lease("nope") is None

    def test_revoke_lease_marks_revoked(self, registry):
        registry.insert_lease(_lease(lease_id="ls_2"))
        assert registry.revoke_lease("ls_2", "manual") is True
        loaded = registry.get_lease("ls_2")
        assert loaded.revoked is True
        assert loaded.revocation_reason == "manual"

    # Idempotency contract: second revocation is a no-op and does NOT
    # clobber the original reason. This matters for the destroy cascade
    # racing with an explicit ReleaseCapability — we want first-writer-wins
    # so audit retains the actual cause.
    def test_revoke_lease_second_call_noop_preserves_reason(self, registry):
        registry.insert_lease(_lease(lease_id="ls_3"))
        registry.revoke_lease("ls_3", "manual")
        assert registry.revoke_lease("ls_3", "destroy_cascade") is False
        loaded = registry.get_lease("ls_3")
        assert loaded.revocation_reason == "manual"

    def test_revoke_leases_for_desk_cascades(self, registry):
        registry.insert_lease(_lease(lease_id="ls_a", desk_id="ws_alpha", secret="k1"))
        registry.insert_lease(_lease(lease_id="ls_b", desk_id="ws_alpha", secret="k2"))
        registry.insert_lease(_lease(lease_id="ls_c", desk_id="ws_beta", secret="k3"))

        revoked = registry.revoke_leases_for_desk("ws_alpha", "desk_destroyed")
        assert revoked == 2
        assert registry.get_lease("ls_a").revoked is True
        assert registry.get_lease("ls_b").revoked is True
        # Other desk's lease is untouched
        assert registry.get_lease("ls_c").revoked is False

    def test_find_active_secret_lease_matches_per_secret(self, registry):
        registry.insert_lease(_lease(lease_id="ls_a", desk_id="ws_alpha", secret="anthropic"))
        registry.insert_lease(_lease(lease_id="ls_b", desk_id="ws_alpha", secret="tailscale"))

        assert registry.find_active_secret_lease("ws_alpha", "anthropic").lease_id == "ls_a"
        assert registry.find_active_secret_lease("ws_alpha", "tailscale").lease_id == "ls_b"
        assert registry.find_active_secret_lease("ws_alpha", "missing") is None

    def test_find_active_secret_lease_ignores_revoked(self, registry):
        registry.insert_lease(_lease(lease_id="ls_a", desk_id="ws_alpha", secret="k"))
        registry.revoke_lease("ls_a", "released")
        assert registry.find_active_secret_lease("ws_alpha", "k") is None
