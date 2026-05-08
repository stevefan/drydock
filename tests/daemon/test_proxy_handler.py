"""Tests for the UpdateProxyAllowlist RPC handler (Phase 2)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.daemon.proxy_handlers import update_proxy_allowlist
from drydock.daemon.rpc_common import _RpcError


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Two drydocks: 'caller' (dock-scoped) + 'target'. Audit log
    redirected. Daemon-secrets and proxy roots in tmp_path."""
    audit_log = tmp_path / "audit.log"
    proxy_root = tmp_path / "proxy"
    proxy_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    import drydock.core.audit as audit_mod
    monkeypatch.setattr(audit_mod, "DEFAULT_LOG_PATH", audit_log)

    db = tmp_path / "r.db"
    r = Registry(db_path=db)
    caller = Drydock(name="caller", project="c", repo_path="/r")
    # Caller has narrowness allowing api.github.com only
    r.create_drydock(caller)
    r.update_desk_delegations("caller", delegatable_network_reach=["api.github.com"])
    r.insert_token(caller.id, "h_caller", datetime.now(timezone.utc))

    target = Drydock(name="target", project="t", repo_path="/r")
    r.create_drydock(target)
    r.update_desk_delegations("target", delegatable_network_reach=["api.example.com"])
    r.insert_token(target.id, "h_target", datetime.now(timezone.utc))
    r.close()
    return {
        "db": db,
        "caller_id": caller.id,
        "target_id": target.id,
        "proxy_root": proxy_root,
        "audit_log": audit_log,
    }


class TestAuth:
    def test_unauthenticated_rejected(self, env):
        with pytest.raises(_RpcError) as exc:
            update_proxy_allowlist(
                {"domains": ["api.github.com"]},
                "req-1", caller_drydock_id=None,
                registry_path=env["db"],
            )
        assert exc.value.code == -32004

    def test_self_update_dock_scope_passes(self, env):
        """Caller updating own desk with allowed domain — fine."""
        result = update_proxy_allowlist(
            {"domains": ["api.github.com"]},
            "req-1", caller_drydock_id=env["caller_id"],
            registry_path=env["db"],
        )
        assert result["drydock_id"] == env["caller_id"]
        assert result["host_count"] == 1

    def test_cross_desk_dock_scope_refused(self, env):
        """Caller (dock-scope) tries to update target — refused."""
        with pytest.raises(_RpcError) as exc:
            update_proxy_allowlist(
                {
                    "target_drydock_id": env["target_id"],
                    "domains": ["api.example.com"],
                },
                "req-1", caller_drydock_id=env["caller_id"],
                registry_path=env["db"],
            )
        assert exc.value.code == -32020
        assert "auditor_scope_required" in exc.value.message


class TestNarrowness:
    def test_self_update_outside_narrowness_refused(self, env):
        """Caller's narrowness allows api.github.com but they ask for
        evil.example.com — refused with -32006."""
        with pytest.raises(_RpcError) as exc:
            update_proxy_allowlist(
                {"domains": ["api.github.com", "evil.example.com"]},
                "req-1", caller_drydock_id=env["caller_id"],
                registry_path=env["db"],
            )
        assert exc.value.code == -32006
        assert "evil.example.com" in exc.value.data["disallowed"]

    def test_auditor_bypasses_narrowness(self, env):
        """Promote caller to auditor scope; cross-desk update with any
        domains succeeds — auditor authority IS the gate."""
        r = Registry(db_path=env["db"])
        try:
            r.designate_auditor(env["caller_id"])
        finally:
            r.close()

        result = update_proxy_allowlist(
            {
                "target_drydock_id": env["target_id"],
                "domains": ["anything-i-want.com", "api.github.com"],
                "reason": "incident response",
            },
            "req-1", caller_drydock_id=env["caller_id"],
            registry_path=env["db"],
        )
        assert result["drydock_id"] == env["target_id"]
        # File written somewhere — the result tells us exactly where
        assert Path(result["written_path"]).exists()
        assert "anything-i-want.com" in Path(result["written_path"]).read_text()


class TestPersistence:
    def test_acl_file_written(self, env):
        result = update_proxy_allowlist(
            {"domains": ["api.github.com"]},
            "req-1", caller_drydock_id=env["caller_id"],
            registry_path=env["db"],
        )
        path = Path(result["written_path"])
        assert path.exists()
        content = path.read_text()
        assert "api.github.com" in content


class TestAudit:
    def test_audit_event_emitted(self, env):
        update_proxy_allowlist(
            {"domains": ["api.github.com"], "reason": "user added new endpoint"},
            "req-1", caller_drydock_id=env["caller_id"],
            registry_path=env["db"],
        )
        log = env["audit_log"].read_text()
        assert "egress.allowlist_updated" in log
        assert env["caller_id"] in log
        assert "user added new endpoint" in log
