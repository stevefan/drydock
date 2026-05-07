"""Tests for the AuditorAction RPC (Phase PA3).

Pin the contract:
- Caller's bearer token must be auditor-scoped; otherwise -32004/-32020.
- One Auditor per Harbor — designating a second drydock raises.
- Dry-run mode (default) audits + returns success without invoking
  the underlying primitive.
- Live mode dispatches to stop_desk / release_capability for the
  supported kinds, refuses unsupported kinds with -32021.
- Spec validation surfaces structured errors.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.daemon.auditor_handlers import auditor_action
from drydock.daemon.rpc_common import _RpcError


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Registry with two drydocks: 'auditor' (designated) + 'target'.
    Audit log redirected so tests don't write to ~."""
    audit_log = tmp_path / "audit.log"
    import drydock.core.audit as audit_mod
    monkeypatch.setattr(audit_mod, "DEFAULT_LOG_PATH", audit_log)
    db = tmp_path / "r.db"
    r = Registry(db_path=db)
    a = Drydock(name="a-auditor", project="a", repo_path="/r")
    t = Drydock(name="target", project="t", repo_path="/r")
    r.create_drydock(a)
    r.create_drydock(t)
    # Issue tokens so designate has something to flip
    from datetime import datetime, timezone
    r.insert_token(a.id, "hash_a", datetime.now(timezone.utc))
    r.insert_token(t.id, "hash_t", datetime.now(timezone.utc))
    # Designate the auditor
    r.designate_auditor(a.id)
    r.close()
    return {
        "db": db,
        "auditor_id": a.id,
        "target_id": t.id,
        "secrets_root": tmp_path / "secrets",
        "audit_log": audit_log,
    }


class TestScopeGate:
    def test_unauthenticated_rejected(self, env):
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "stop_dock", "target_drydock_id": env["target_id"],
                 "reason": "test"},
                "req-1", caller_drydock_id=None,
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=True,
            )
        assert exc.value.code == -32004

    def test_non_auditor_caller_rejected(self, env):
        """Caller is the target drydock (dock-scoped), not the
        designated Auditor. Must refuse with -32020."""
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "stop_dock", "target_drydock_id": env["target_id"],
                 "reason": "test"},
                "req-1", caller_drydock_id=env["target_id"],
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=True,
            )
        assert exc.value.code == -32020
        assert "auditor_scope_required" in exc.value.message

    def test_auditor_caller_passes_scope_check(self, env):
        """Designated Auditor invokes; scope check passes."""
        result = auditor_action(
            {"kind": "stop_dock", "target_drydock_id": env["target_id"],
             "reason": "test"},
            "req-1", caller_drydock_id=env["auditor_id"],
            registry_path=env["db"], secrets_root=env["secrets_root"],
            dry_run=True,
        )
        assert result["execution_mode"] == "dry_run"
        assert result["executed"] is False


class TestSpecValidation:
    def test_unknown_kind_rejected(self, env):
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "delete_universe", "target_drydock_id": env["target_id"],
                 "reason": "test"},
                "req-1", caller_drydock_id=env["auditor_id"],
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=True,
            )
        assert exc.value.code == -32602
        assert "kind" in str(exc.value.data)

    def test_missing_reason_rejected(self, env):
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "stop_dock", "target_drydock_id": env["target_id"]},
                "req-1", caller_drydock_id=env["auditor_id"],
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=True,
            )
        assert exc.value.code == -32602
        assert exc.value.data["field"] == "reason"

    def test_stop_dock_without_target_rejected(self, env):
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "stop_dock", "reason": "test"},
                "req-1", caller_drydock_id=env["auditor_id"],
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=True,
            )
        assert exc.value.code == -32602
        assert exc.value.data["field"] == "target_drydock_id"

    def test_revoke_lease_without_lease_id_rejected(self, env):
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "revoke_lease", "reason": "test"},
                "req-1", caller_drydock_id=env["auditor_id"],
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=True,
            )
        assert exc.value.code == -32602
        assert exc.value.data["field"] == "lease_id"


class TestDryRunMode:
    def test_dry_run_audits_without_executing(self, env):
        result = auditor_action(
            {"kind": "stop_dock", "target_drydock_id": env["target_id"],
             "reason": "spending too much memory",
             "evidence": {"memory_max": "8g", "observed": "12g"}},
            "req-1", caller_drydock_id=env["auditor_id"],
            registry_path=env["db"], secrets_root=env["secrets_root"],
            dry_run=True,
        )
        assert result["executed"] is False
        assert result["execution_mode"] == "dry_run"
        assert result["reason"] == "spending too much memory"
        # Audit log captures the call
        log_text = env["audit_log"].read_text()
        assert "auditor.action_dry_run" in log_text
        assert env["auditor_id"] in log_text


class TestLiveMode:
    def test_throttle_egress_unsupported_in_live(self, env):
        """The primitive isn't built; live mode refuses."""
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "throttle_egress", "target_drydock_id": env["target_id"],
                 "reason": "burst"},
                "req-1", caller_drydock_id=env["auditor_id"],
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=False,
            )
        assert exc.value.code == -32021
        assert "throttle_egress" in str(exc.value.data)

    def test_freeze_storage_unsupported_in_live(self, env):
        with pytest.raises(_RpcError) as exc:
            auditor_action(
                {"kind": "freeze_storage", "target_drydock_id": env["target_id"],
                 "reason": "exfil"},
                "req-1", caller_drydock_id=env["auditor_id"],
                registry_path=env["db"], secrets_root=env["secrets_root"],
                dry_run=False,
            )
        assert exc.value.code == -32021


class TestRegistryDesignation:
    def test_one_auditor_per_harbor_invariant(self, env):
        """Designating a second drydock raises ValueError."""
        r = Registry(db_path=env["db"])
        try:
            with pytest.raises(ValueError) as exc:
                r.designate_auditor(env["target_id"])
            assert "already has the auditor scope" in str(exc.value)
        finally:
            r.close()

    def test_designate_idempotent(self, env):
        r = Registry(db_path=env["db"])
        try:
            # Second call on the already-auditor desk — idempotent no-op
            changed = r.designate_auditor(env["auditor_id"])
            assert changed is False
        finally:
            r.close()

    def test_revoke_then_redesignate(self, env):
        r = Registry(db_path=env["db"])
        try:
            assert r.revoke_auditor_scope(env["auditor_id"]) is True
            assert r.get_auditor_drydock_id() is None
            # Now we can designate the other one
            assert r.designate_auditor(env["target_id"]) is True
            assert r.get_auditor_drydock_id() == env["target_id"]
        finally:
            r.close()


class TestLiveActionsEnv:
    def test_is_live_actions_enabled_default_off(self, monkeypatch):
        from drydock.daemon.auditor_handlers import is_live_actions_enabled
        monkeypatch.delenv("AUDITOR_LIVE_ACTIONS", raising=False)
        assert is_live_actions_enabled() is False

    def test_is_live_actions_enabled_with_flag(self, monkeypatch):
        from drydock.daemon.auditor_handlers import is_live_actions_enabled
        monkeypatch.setenv("AUDITOR_LIVE_ACTIONS", "1")
        assert is_live_actions_enabled() is True

    def test_is_live_actions_enabled_other_values_off(self, monkeypatch):
        """Only "1" enables; "true", "yes", etc. don't (deliberately
        strict to avoid accidentally-enabling)."""
        from drydock.daemon.auditor_handlers import is_live_actions_enabled
        for val in ("0", "true", "yes", "TRUE", ""):
            monkeypatch.setenv("AUDITOR_LIVE_ACTIONS", val)
            assert is_live_actions_enabled() is False
