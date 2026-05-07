"""Tests for `drydock.core.workload` — the workload-as-transaction primitive.

Pin the WL1 contract:
- WorkloadSpec validation (kind enum, duration bounds, cgroup shape).
- build_actions_for_spec generates a CgroupLiftAction only when the
  spec actually requests a lift (no-op specs produce no actions).
- apply_actions_atomically: clean apply persists serialization;
  failure rolls back already-applied actions in reverse.
- revert_lease_actions: best-effort, returns per-action ok/error.
- assemble_lease: ID format, granted_at + duration → expires_at.
- Round-trip through registry: insert + get + list + mark revoked.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from drydock.core.cgroup import CgroupUpdateError
from drydock.core.registry import Registry
from drydock.core.resource_ceilings import HardCeilings
from drydock.core.runtime import Drydock
from drydock.core.workload import (
    Action,
    CgroupLiftAction,
    WorkloadApplyError,
    WorkloadSpec,
    WorkloadValidationError,
    apply_actions_atomically,
    assemble_lease,
    build_actions_for_spec,
    deserialize_action,
    new_lease_id,
    revert_lease_actions,
    validate_spec,
)


def _ok_run():
    """Stub a successful subprocess.run for cgroup apply paths."""
    from unittest.mock import MagicMock
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


class TestValidateSpec:
    def test_valid_minimal_spec(self):
        spec = WorkloadSpec(kind="batch", duration_max_seconds=600)
        assert validate_spec(spec) is spec

    def test_unknown_kind_rejected(self):
        with pytest.raises(WorkloadValidationError) as exc:
            validate_spec(WorkloadSpec(kind="not-a-kind"))
        assert "unknown workload kind" in str(exc.value)

    def test_zero_duration_rejected(self):
        with pytest.raises(WorkloadValidationError):
            validate_spec(WorkloadSpec(kind="batch", duration_max_seconds=0))

    def test_excessive_duration_rejected(self):
        with pytest.raises(WorkloadValidationError):
            validate_spec(WorkloadSpec(kind="batch", duration_max_seconds=24 * 3600))

    def test_max_12h_accepted(self):
        validate_spec(WorkloadSpec(kind="batch", duration_max_seconds=12 * 3600))

    def test_malformed_cgroup_in_expected_rejected(self):
        with pytest.raises(WorkloadValidationError) as exc:
            validate_spec(WorkloadSpec(
                kind="training",
                expected={"memory_max": "garbage-value"},
            ))
        assert "invalid cgroup" in str(exc.value)

    def test_unknown_keys_in_expected_ignored(self):
        # Forward-compat: workers might include fields that aren't yet
        # understood; we shouldn't reject.
        validate_spec(WorkloadSpec(
            kind="experiment",
            expected={"egress_bytes": 1024 ** 3, "future_field": "x"},
        ))


# ---------------------------------------------------------------------------
# Action assembly
# ---------------------------------------------------------------------------


class TestBuildActions:
    def test_no_lift_above_original_no_actions(self):
        # Spec asks for the same caps as already in original.
        actions = build_actions_for_spec(
            WorkloadSpec(kind="batch", expected={"memory_max": "4g"}),
            container_id="cid_x",
            original_resources_hard={"memory_max": "4g"},
        )
        assert actions == []

    def test_actual_lift_produces_cgroup_action(self):
        actions = build_actions_for_spec(
            WorkloadSpec(kind="training", expected={"memory_max": "8g"}),
            container_id="cid_x",
            original_resources_hard={"memory_max": "4g"},
        )
        assert len(actions) == 1
        assert isinstance(actions[0], CgroupLiftAction)
        assert actions[0].lifted.memory_max == "8g"
        assert actions[0].original.memory_max == "4g"

    def test_no_cgroup_keys_no_action(self):
        actions = build_actions_for_spec(
            WorkloadSpec(kind="experiment", expected={"future_field": 42}),
            container_id="cid_x",
            original_resources_hard={},
        )
        assert actions == []


# ---------------------------------------------------------------------------
# Atomic apply / rollback
# ---------------------------------------------------------------------------


class _FakeAction:
    """Test-double Action: configurable apply success/failure + revert."""
    def __init__(self, name: str, *, fail_on_apply: bool = False, fail_on_revert: bool = False):
        self.kind = name
        self.applied = False
        self.reverted = False
        self.fail_on_apply = fail_on_apply
        self.fail_on_revert = fail_on_revert

    def apply(self) -> dict:
        if self.fail_on_apply:
            raise RuntimeError(f"{self.kind} apply boom")
        self.applied = True
        return {"name": self.kind, "ok": True}

    def revert(self, persisted: dict) -> None:
        if self.fail_on_revert:
            raise RuntimeError(f"{self.kind} revert boom")
        self.reverted = True

    def serialize(self) -> dict:
        return {"kind": self.kind}


class TestApplyAtomically:
    def test_clean_apply_persists_each_action(self):
        a1 = _FakeAction("action1")
        a2 = _FakeAction("action2")
        applied = apply_actions_atomically([a1, a2])
        assert a1.applied
        assert a2.applied
        assert len(applied) == 2
        assert applied[0]["kind"] == "action1"
        assert applied[1]["kind"] == "action2"
        # Persisted carries the apply()-returned dict
        assert applied[0]["persisted"] == {"name": "action1", "ok": True}

    def test_failure_rolls_back_in_reverse(self):
        a1 = _FakeAction("first")
        a2 = _FakeAction("second")
        a3 = _FakeAction("third", fail_on_apply=True)
        with pytest.raises(WorkloadApplyError) as exc_info:
            apply_actions_atomically([a1, a2, a3])
        assert exc_info.value.failed_at == 2
        # First two were applied then reverted.
        assert a1.applied and a1.reverted
        assert a2.applied and a2.reverted
        assert not a3.applied  # never succeeded
        assert not a3.reverted

    def test_revert_failure_does_not_block_subsequent_reverts(self):
        # Even if one revert fails during rollback, the others still run.
        a1 = _FakeAction("clean")
        a2 = _FakeAction("dirty-revert", fail_on_revert=True)
        a3 = _FakeAction("third", fail_on_apply=True)
        with pytest.raises(WorkloadApplyError):
            apply_actions_atomically([a1, a2, a3])
        # a1 still got reverted despite a2's revert failing
        assert a1.reverted
        # a2.revert was attempted but raised; record state is implicit


# ---------------------------------------------------------------------------
# revert_lease_actions (the sweeper / release path)
# ---------------------------------------------------------------------------


class TestRevertLeaseActions:
    def test_clean_revert_returns_ok_per_action(self):
        # Build an actual cgroup action and persist its serialization.
        action = CgroupLiftAction(
            container_id="cid_x",
            original=HardCeilings(memory_max="4g"),
            lifted=HardCeilings(memory_max="8g"),
        )
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_run()):
            persisted = action.apply()
        applied = [{**action.serialize(), "persisted": persisted}]
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_run()):
            results = revert_lease_actions(applied)
        assert results == [{"kind": "cgroup_lift", "ok": True}]

    def test_revert_failure_recorded(self):
        action = CgroupLiftAction(
            container_id="cid_x",
            original=HardCeilings(memory_max="4g"),
            lifted=HardCeilings(memory_max="8g"),
        )
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_run()):
            persisted = action.apply()
        applied = [{**action.serialize(), "persisted": persisted}]
        # docker update fails on revert
        from unittest.mock import MagicMock
        bad = MagicMock(); bad.returncode = 1; bad.stderr = "boom"; bad.stdout = ""
        with patch("drydock.core.cgroup.subprocess.run", return_value=bad):
            results = revert_lease_actions(applied)
        assert results[0]["ok"] is False
        assert "boom" in results[0]["error"]


class TestSerializeRoundTrip:
    def test_cgroup_action_roundtrip(self):
        action = CgroupLiftAction(
            container_id="cid_x",
            original=HardCeilings(memory_max="4g", cpu_max=2.0),
            lifted=HardCeilings(memory_max="8g", cpu_max=4.0),
        )
        serialized = action.serialize()
        restored = deserialize_action(serialized)
        assert isinstance(restored, CgroupLiftAction)
        assert restored.container_id == "cid_x"
        assert restored.original.memory_max == "4g"
        assert restored.lifted.cpu_max == 4.0

    def test_unknown_kind_raises(self):
        with pytest.raises(WorkloadValidationError):
            deserialize_action({"kind": "bogus"})


# ---------------------------------------------------------------------------
# Lease assembly
# ---------------------------------------------------------------------------


class TestAssembleLease:
    def test_id_format(self):
        spec = WorkloadSpec(kind="batch", duration_max_seconds=600)
        lease = assemble_lease(drydock_id="dock_x", spec=spec, applied_actions=[])
        assert lease.id.startswith("wl_")
        assert len(lease.id) > len("wl_")

    def test_expires_at_is_granted_plus_duration(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        spec = WorkloadSpec(kind="training", duration_max_seconds=7200)
        lease = assemble_lease(
            drydock_id="dock_x", spec=spec, applied_actions=[], now=now,
        )
        granted = datetime.fromisoformat(lease.granted_at)
        expires = datetime.fromisoformat(lease.expires_at)
        assert expires - granted == timedelta(seconds=7200)


# ---------------------------------------------------------------------------
# Registry round-trip
# ---------------------------------------------------------------------------


class TestRegistryWorkloadMethods:
    def test_insert_get_round_trip(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        spec = WorkloadSpec(kind="batch", duration_max_seconds=600)
        lease = assemble_lease(drydock_id="dock_x", spec=spec, applied_actions=[])
        r.insert_workload_lease(lease)
        row = r.get_workload_lease(lease.id)
        assert row is not None
        assert row["drydock_id"] == "dock_x"
        assert row["status"] == "active"
        assert json.loads(row["spec_json"])["kind"] == "batch"

    def test_list_active_filters_by_drydock(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        for desk_id in ("dock_a", "dock_b", "dock_a"):
            spec = WorkloadSpec(kind="batch", duration_max_seconds=600)
            r.insert_workload_lease(assemble_lease(drydock_id=desk_id, spec=spec, applied_actions=[]))
        all_active = r.list_active_workload_leases()
        only_a = r.list_active_workload_leases(drydock_id="dock_a")
        assert len(all_active) == 3
        assert len(only_a) == 2
        assert all(row["drydock_id"] == "dock_a" for row in only_a)

    def test_mark_revoked_persists_terminal_status_and_results(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        spec = WorkloadSpec(kind="batch", duration_max_seconds=600)
        lease = assemble_lease(drydock_id="dock_x", spec=spec, applied_actions=[])
        r.insert_workload_lease(lease)
        r.mark_workload_lease_revoked(
            lease.id,
            revoke_results=[{"kind": "cgroup_lift", "ok": True}],
            terminal_status="released",
        )
        row = r.get_workload_lease(lease.id)
        assert row["status"] == "released"
        assert row["revoked_at"]
        results = json.loads(row["revoke_results_json"])
        assert results == [{"kind": "cgroup_lift", "ok": True}]

    def test_active_query_excludes_released(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        spec = WorkloadSpec(kind="batch", duration_max_seconds=600)
        lease = assemble_lease(drydock_id="dock_x", spec=spec, applied_actions=[])
        r.insert_workload_lease(lease)
        r.mark_workload_lease_revoked(lease.id, revoke_results=[])
        assert r.list_active_workload_leases() == []


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


class TestSweeper:
    def test_sweep_no_expired_returns_empty(self, tmp_path):
        from drydock.core.workload import sweep_expired_leases
        r = Registry(db_path=tmp_path / "r.db")
        # Insert a lease that expires far in the future
        spec = WorkloadSpec(kind="batch", duration_max_seconds=3600)
        lease = assemble_lease(drydock_id="dock_x", spec=spec, applied_actions=[])
        r.insert_workload_lease(lease)
        assert sweep_expired_leases(r) == []
        # And it stays active
        row = r.get_workload_lease(lease.id)
        assert row["status"] == "active"

    def test_sweep_expires_past_due_lease(self, tmp_path):
        from drydock.core.workload import sweep_expired_leases
        from datetime import datetime, timezone, timedelta
        r = Registry(db_path=tmp_path / "r.db")
        # Insert a lease that's already expired.
        # Use a past `now` to assemble it.
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        spec = WorkloadSpec(kind="batch", duration_max_seconds=600)  # 10 min
        lease = assemble_lease(
            drydock_id="dock_x", spec=spec, applied_actions=[], now=past,
        )
        r.insert_workload_lease(lease)
        summaries = sweep_expired_leases(r)
        assert len(summaries) == 1
        assert summaries[0]["lease_id"] == lease.id
        assert summaries[0]["terminal_status"] == "expired"
        # Persisted state
        row = r.get_workload_lease(lease.id)
        assert row["status"] == "expired"
        assert row["revoked_at"]

    def test_sweep_runs_action_revert(self, tmp_path):
        """Expired lease with a real action: sweep calls revert_cgroup_limits."""
        from drydock.core.workload import sweep_expired_leases
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch
        from drydock.core.resource_ceilings import HardCeilings
        r = Registry(db_path=tmp_path / "r.db")
        # Build a lease with a CgroupLiftAction in its applied_actions
        action = CgroupLiftAction(
            container_id="cid_x",
            original=HardCeilings(memory_max="4g"),
            lifted=HardCeilings(memory_max="8g"),
        )
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_run()):
            persisted = action.apply()
        applied = [{**action.serialize(), "persisted": persisted}]
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        spec = WorkloadSpec(kind="training", duration_max_seconds=600,
                            expected={"memory_max": "8g"})
        lease = assemble_lease(
            drydock_id="dock_x", spec=spec,
            applied_actions=applied, now=past,
        )
        r.insert_workload_lease(lease)
        # Sweep — docker update mocked, succeeds
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_run()) as run:
            summaries = sweep_expired_leases(r)
        assert summaries[0]["terminal_status"] == "expired"
        # docker update was called for the revert
        assert run.called

    def test_sweep_partial_revoke_when_action_fails(self, tmp_path):
        """A failed revert during sweep yields partial-revoked terminal state."""
        from drydock.core.workload import sweep_expired_leases
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch, MagicMock
        from drydock.core.resource_ceilings import HardCeilings
        r = Registry(db_path=tmp_path / "r.db")
        action = CgroupLiftAction(
            container_id="cid_x",
            original=HardCeilings(memory_max="4g"),
            lifted=HardCeilings(memory_max="8g"),
        )
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok_run()):
            persisted = action.apply()
        applied = [{**action.serialize(), "persisted": persisted}]
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        spec = WorkloadSpec(kind="training", duration_max_seconds=600,
                            expected={"memory_max": "8g"})
        lease = assemble_lease(
            drydock_id="dock_x", spec=spec,
            applied_actions=applied, now=past,
        )
        r.insert_workload_lease(lease)
        # docker update fails on revert
        bad = MagicMock(); bad.returncode = 1; bad.stderr = "boom"; bad.stdout = ""
        with patch("drydock.core.cgroup.subprocess.run", return_value=bad):
            summaries = sweep_expired_leases(r)
        assert summaries[0]["terminal_status"] == "partial-revoked"
        row = r.get_workload_lease(lease.id)
        assert row["status"] == "partial-revoked"
