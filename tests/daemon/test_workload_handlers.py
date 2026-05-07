"""Tests for the daemon's workload RPC handlers (Phase 2a.3 WL1).

The contract these tests pin:
- Auth required (caller_drydock_id is the lease's drydock_id).
- Spec validation surfaces structured errors (-32602 invalid_workload_spec).
- Drydock-not-running surfaces a clear -32018 with fix hint.
- Single-active-lease semantic (-32017 workload_lease_exists).
- Happy path returns a lease id, applies cgroup, persists row.
- Release reverts and marks the row.
- Release of foreign drydock's lease forbidden.
- Idempotent re-release returns the released shape, no-error.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.daemon.workload_handlers import (
    _RpcError,
    register_workload,
    release_workload,
)


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    """Redirect emit_audit's default log path so tests don't write to ~."""
    audit_log = tmp_path / "audit.log"
    import drydock.core.audit as audit_mod
    monkeypatch.setattr(audit_mod, "DEFAULT_LOG_PATH", audit_log)
    return audit_log


@pytest.fixture
def registry_with_running_desk(tmp_path, isolated_audit):
    """Registry with one running drydock (id=dock_x, container_id=cid_x)
    capped at memory_max=4g."""
    db = tmp_path / "r.db"
    r = Registry(db_path=db)
    ws = Drydock(
        name="test", project="test", repo_path="/r",
        state="running", container_id="cid_x",
        original_resources_hard={"memory_max": "4g"},
    )
    r.create_drydock(ws)
    r.update_drydock("test", state="running", container_id="cid_x")
    r.close()
    return db


def _ok_subprocess():
    """Mock a successful docker update call."""
    res = MagicMock()
    res.returncode = 0
    res.stdout = ""
    res.stderr = ""
    return res


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


class TestRegisterWorkloadAuth:
    def test_unauthenticated_rejected(self, registry_with_running_desk):
        with pytest.raises(_RpcError) as exc:
            register_workload(
                {"kind": "batch"}, "req-1", caller_drydock_id=None,
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32004

    def test_missing_kind_rejected(self, registry_with_running_desk):
        with pytest.raises(_RpcError) as exc:
            register_workload(
                {}, "req-1", caller_drydock_id="dock_x",
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32602

    def test_invalid_kind_rejected_with_reason(self, registry_with_running_desk):
        with pytest.raises(_RpcError) as exc:
            register_workload(
                {"kind": "totally-fake"}, "req-1", caller_drydock_id="dock_x",
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32602
        assert exc.value.message == "invalid_workload_spec"

    def test_caller_not_in_registry_rejected(self, registry_with_running_desk):
        with pytest.raises(_RpcError) as exc:
            register_workload(
                {"kind": "batch"}, "req-1", caller_drydock_id="dock_unknown",
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32603

    def test_drydock_not_running_rejected_with_fix(self, tmp_path, isolated_audit):
        # Build a registry where the desk has no container_id (suspended).
        db = tmp_path / "r.db"
        r = Registry(db_path=db)
        ws = Drydock(
            name="test", project="test", repo_path="/r",
            state="suspended",
        )
        r.create_drydock(ws)
        r.close()
        with pytest.raises(_RpcError) as exc:
            register_workload(
                {"kind": "batch"}, "req-1", caller_drydock_id="dock_test",
                registry_path=db,
            )
        assert exc.value.code == -32018  # workload_drydock_not_running
        assert "fix" in exc.value.data


class TestRegisterWorkloadHappyPath:
    def test_zero_action_spec_still_returns_lease(self, registry_with_running_desk):
        """Spec that doesn't ask for any lift above original — still
        creates a lease record for audit visibility."""
        result = register_workload(
            {"kind": "interactive", "duration_max_seconds": 600},
            "req-1", caller_drydock_id="dock_test",
            registry_path=registry_with_running_desk,
        )
        assert result["lease_id"].startswith("wl_")
        assert result["status"] == "active"
        assert result["applied_actions"] == []
        # Persisted in registry
        r = Registry(db_path=registry_with_running_desk)
        try:
            row = r.get_workload_lease(result["lease_id"])
            assert row is not None
            assert row["status"] == "active"
        finally:
            r.close()

    def test_cgroup_lift_applies_via_docker_update(self, registry_with_running_desk):
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_subprocess()) as run:
            result = register_workload(
                {
                    "kind": "training",
                    "duration_max_seconds": 7200,
                    "expected": {"memory_max": "8g"},
                },
                "req-1", caller_drydock_id="dock_test",
                registry_path=registry_with_running_desk,
            )
        assert result["status"] == "active"
        # docker update was called once
        run.assert_called_once()
        cmd = run.call_args[0][0]
        assert "update" in cmd
        assert "--memory=8g" in cmd
        # Lease persisted with applied actions
        assert len(result["applied_actions"]) == 1
        assert result["applied_actions"][0]["kind"] == "cgroup_lift"


class TestRegisterWorkloadSingleActive:
    def test_second_concurrent_lease_refused(self, registry_with_running_desk):
        # First lease succeeds
        register_workload(
            {"kind": "batch"}, "req-1", caller_drydock_id="dock_test",
            registry_path=registry_with_running_desk,
        )
        # Second is refused
        with pytest.raises(_RpcError) as exc:
            register_workload(
                {"kind": "batch"}, "req-2", caller_drydock_id="dock_test",
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32017  # workload_lease_exists
        assert exc.value.message == "workload_lease_exists"
        assert "lease_id" in exc.value.data


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


class TestReleaseWorkload:
    def test_unauthenticated_rejected(self, registry_with_running_desk):
        with pytest.raises(_RpcError) as exc:
            release_workload(
                {"lease_id": "wl_xxx"}, "req-1", caller_drydock_id=None,
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32004

    def test_lease_not_found_rejected(self, registry_with_running_desk):
        with pytest.raises(_RpcError) as exc:
            release_workload(
                {"lease_id": "wl_does_not_exist"}, "req-1",
                caller_drydock_id="dock_test",
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32601

    def test_other_drydock_release_forbidden(self, registry_with_running_desk):
        # Register a lease under dock_test
        granted = register_workload(
            {"kind": "batch"}, "req-1", caller_drydock_id="dock_test",
            registry_path=registry_with_running_desk,
        )
        # Try to release as a different drydock
        with pytest.raises(_RpcError) as exc:
            release_workload(
                {"lease_id": granted["lease_id"]}, "req-2",
                caller_drydock_id="dock_other",
                registry_path=registry_with_running_desk,
            )
        assert exc.value.code == -32004
        assert exc.value.data["reason"] == "lease_belongs_to_other_drydock"

    def test_release_reverts_cgroup_and_marks_released(self, registry_with_running_desk):
        # Register with cgroup lift
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_subprocess()):
            granted = register_workload(
                {"kind": "training", "expected": {"memory_max": "8g"}},
                "req-1", caller_drydock_id="dock_test",
                registry_path=registry_with_running_desk,
            )
        # Release
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_subprocess()) as run:
            result = release_workload(
                {"lease_id": granted["lease_id"]}, "req-2",
                caller_drydock_id="dock_test",
                registry_path=registry_with_running_desk,
            )
        assert result["status"] == "released"
        # docker update called for the revert
        assert run.called
        cmd = run.call_args[0][0]
        assert "--memory=4g" in cmd  # reverts to original
        # Persisted state
        r = Registry(db_path=registry_with_running_desk)
        try:
            row = r.get_workload_lease(granted["lease_id"])
            assert row["status"] == "released"
            assert row["revoked_at"] is not None
        finally:
            r.close()

    def test_idempotent_re_release_returns_released(self, registry_with_running_desk):
        granted = register_workload(
            {"kind": "batch"}, "req-1", caller_drydock_id="dock_test",
            registry_path=registry_with_running_desk,
        )
        release_workload(
            {"lease_id": granted["lease_id"]}, "req-2",
            caller_drydock_id="dock_test",
            registry_path=registry_with_running_desk,
        )
        # Second release returns the released shape, no error
        result = release_workload(
            {"lease_id": granted["lease_id"]}, "req-3",
            caller_drydock_id="dock_test",
            registry_path=registry_with_running_desk,
        )
        assert result["status"] == "released"

    def test_partial_revoke_when_action_fails(self, registry_with_running_desk):
        """If revert fails on one action, lease ends up partial-revoked."""
        # Register with cgroup lift (apply succeeds)
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_subprocess()):
            granted = register_workload(
                {"kind": "training", "expected": {"memory_max": "8g"}},
                "req-1", caller_drydock_id="dock_test",
                registry_path=registry_with_running_desk,
            )
        # Release: docker update fails on revert
        bad = MagicMock()
        bad.returncode = 1
        bad.stderr = "container disappeared"
        bad.stdout = ""
        with patch("drydock.core.cgroup.subprocess.run", return_value=bad):
            result = release_workload(
                {"lease_id": granted["lease_id"]}, "req-2",
                caller_drydock_id="dock_test",
                registry_path=registry_with_running_desk,
            )
        assert result["status"] == "partial-revoked"
        # results carries the per-action error
        assert any(not r.get("ok") for r in result["results"])
