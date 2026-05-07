"""Tests for `drydock.core.migration` — the M1 planner + pre-check.

Pin the contract:
- Target parsing surfaces clear errors for malformed specs.
- plan_migration produces structured deltas per target type.
- No-op image bump (same tag) refused outright.
- Pre-check distinguishes refusals (block) from warnings (force-bypassable).
- MigrationPlan round-trips through to_dict / human_summary cleanly.
- Registry CRUD: insert / get / update / list_active migration records.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from drydock.core.migration import (
    ImageBumpTarget,
    MigrationPlanError,
    MigrationStage,
    MigrationStatus,
    ProjectReloadTarget,
    SchemaMigrationTarget,
    new_migration_id,
    plan_migration,
    precheck_migration,
)
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock


# ---------------------------------------------------------------------------
# Stage / status enums — pin the wire contract
# ---------------------------------------------------------------------------


class TestStageVocabulary:
    def test_stage_values_are_stable_strings(self):
        # The enum values feed audit-event names; renaming breaks consumers.
        assert MigrationStage.PLAN.value == "plan"
        assert MigrationStage.SNAPSHOT.value == "snapshot"
        assert MigrationStage.ROLLBACK.value == "rollback"
        assert MigrationStage.CLEANUP.value == "cleanup"

    def test_status_values_match_schema_check(self):
        # The CHECK constraint on migrations.status pins these exact strings.
        expected = {"planned", "in_progress", "completed", "rolled_back", "failed"}
        assert {s.value for s in MigrationStatus} == expected


# ---------------------------------------------------------------------------
# Migration ID
# ---------------------------------------------------------------------------


class TestMigrationId:
    def test_id_format(self):
        mid = new_migration_id()
        assert mid.startswith("mig_")
        assert len(mid) == len("mig_") + 16

    def test_id_uniqueness(self):
        ids = {new_migration_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _drydock(name="test", image="ghcr.io/stevefan/drydock-base:v1.0.18"):
    return Drydock(name=name, project=name, repo_path="/r", image=image)


class TestPlanImageBump:
    def test_basic_bump(self):
        ws = _drydock(image="ghcr.io/stevefan/drydock-base:v1.0.18")
        plan = plan_migration(
            drydock=ws,
            target=ImageBumpTarget(new_image="ghcr.io/stevefan/drydock-base:v1.0.19"),
            source_harbor="hetzner",
        )
        assert plan.target_kind == "image_bump"
        assert "v1.0.18" in plan.changes["image"]
        assert "v1.0.19" in plan.changes["image"]
        assert plan.changes["overlay"] == "regenerate (image change)"
        assert plan.estimated_downtime_seconds == 30

    def test_same_image_refused(self):
        ws = _drydock(image="img:v1")
        with pytest.raises(MigrationPlanError) as exc:
            plan_migration(
                drydock=ws,
                target=ImageBumpTarget(new_image="img:v1"),
                source_harbor="hetzner",
            )
        assert "no-op" in str(exc.value)


class TestPlanProjectReload:
    def test_reload_target_summary(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws,
            target=ProjectReloadTarget(),
            source_harbor="hetzner",
        )
        assert plan.target_kind == "project_reload"
        assert "regenerate" in plan.changes["overlay"]
        assert plan.changes["registry_config"] == "re-pin policy"


class TestPlanSchemaMigration:
    def test_schema_target_includes_version(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws,
            target=SchemaMigrationTarget(target_schema_version=9),
            source_harbor="hetzner",
        )
        assert plan.target_kind == "schema_migration"
        assert plan.changes["registry_schema"] == "→ V9"
        # Schema migrations expect longer downtime
        assert plan.estimated_downtime_seconds == 120


class TestPlanInFlightLeases:
    def test_warns_about_active_workload_leases(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws,
            target=ProjectReloadTarget(),
            source_harbor="hetzner",
            in_flight_workload_leases=[
                {"id": "wl_abc", "expires_at": "2026-05-07T12:00:00Z"},
            ],
        )
        assert len(plan.in_flight_lease_warnings) == 1
        assert "wl_abc" in plan.in_flight_lease_warnings[0]


class TestPlanCrossHarbor:
    def test_cross_harbor_target_recorded(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws,
            target=ProjectReloadTarget(),
            source_harbor="hetzner",
            target_harbor="mac",
        )
        assert plan.source_harbor == "hetzner"
        assert plan.target_harbor == "mac"


# ---------------------------------------------------------------------------
# Plan formatting
# ---------------------------------------------------------------------------


class TestPlanHumanSummary:
    def test_summary_lists_basics(self):
        ws = _drydock(name="auction-crawl")
        plan = plan_migration(
            drydock=ws,
            target=ImageBumpTarget(new_image="img:v2"),
            source_harbor="hetzner",
        )
        summary = plan.human_summary()
        assert any("auction-crawl" in line for line in summary)
        assert any("image_bump" in line for line in summary)
        assert any("hetzner" in line for line in summary)

    def test_summary_includes_warnings(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws,
            target=ProjectReloadTarget(),
            source_harbor="hetzner",
            in_flight_workload_leases=[{"id": "wl_x", "expires_at": "future"}],
        )
        summary = plan.human_summary()
        assert any("warnings" in line.lower() for line in summary)
        assert any("wl_x" in line for line in summary)


# ---------------------------------------------------------------------------
# Pre-check
# ---------------------------------------------------------------------------


class TestPreCheck:
    def test_clean_state_passes(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws,
            target=ProjectReloadTarget(),
            source_harbor="hetzner",
        )
        result = precheck_migration(drydock=ws, plan=plan)
        assert result.ok
        assert result.refusals == []

    def test_unhealthy_daemon_refuses(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws, target=ProjectReloadTarget(), source_harbor="hetzner",
        )
        result = precheck_migration(drydock=ws, plan=plan, daemon_healthy=False)
        assert not result.ok
        assert any("daemon" in r for r in result.refusals)

    def test_drydock_already_migrating_refused(self):
        ws = Drydock(name="x", project="x", repo_path="/r", state="migrating")
        plan = plan_migration(
            drydock=ws, target=ProjectReloadTarget(), source_harbor="hetzner",
        )
        result = precheck_migration(drydock=ws, plan=plan)
        assert not result.ok
        assert any("migrating" in r for r in result.refusals)

    def test_image_bump_missing_image_refused(self):
        ws = _drydock(image="img:old")
        plan = plan_migration(
            drydock=ws,
            target=ImageBumpTarget(new_image="img:new"),
            source_harbor="hetzner",
        )
        result = precheck_migration(
            drydock=ws, plan=plan, target_image_present=False,
        )
        assert not result.ok
        assert any("target image not present" in r for r in result.refusals)

    def test_low_disk_warns_but_does_not_refuse(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws, target=ProjectReloadTarget(), source_harbor="hetzner",
        )
        result = precheck_migration(
            drydock=ws, plan=plan, disk_free_bytes=500_000_000,
        )
        assert result.ok  # warning, not refusal
        assert any("disk" in w for w in result.warnings)

    def test_workload_warnings_propagate_from_plan(self):
        ws = _drydock()
        plan = plan_migration(
            drydock=ws,
            target=ProjectReloadTarget(),
            source_harbor="hetzner",
            in_flight_workload_leases=[{"id": "wl_x", "expires_at": "later"}],
        )
        result = precheck_migration(drydock=ws, plan=plan)
        assert result.ok
        assert any("wl_x" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Registry CRUD
# ---------------------------------------------------------------------------


class TestMigrationRegistry:
    def test_insert_get_round_trip(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        ws = _drydock()
        plan = plan_migration(
            drydock=ws, target=ProjectReloadTarget(), source_harbor="hetzner",
        )
        r.insert_migration(
            migration_id=plan.migration_id,
            drydock_id=ws.id,
            plan_json=json.dumps(plan.to_dict()),
        )
        row = r.get_migration(plan.migration_id)
        assert row is not None
        assert row["drydock_id"] == ws.id
        assert row["status"] == "planned"
        # plan_json round-trips
        round_tripped = json.loads(row["plan_json"])
        assert round_tripped["drydock_name"] == ws.name

    def test_update_walks_status_through_states(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        ws = _drydock()
        plan = plan_migration(
            drydock=ws, target=ProjectReloadTarget(), source_harbor="hetzner",
        )
        r.insert_migration(
            migration_id=plan.migration_id,
            drydock_id=ws.id,
            plan_json=json.dumps(plan.to_dict()),
        )
        # Walk: planned → in_progress → completed
        r.update_migration(plan.migration_id, status="in_progress",
                           current_stage="snapshot")
        row = r.get_migration(plan.migration_id)
        assert row["status"] == "in_progress"
        assert row["current_stage"] == "snapshot"

        r.update_migration(plan.migration_id, status="completed",
                           completed_at="2026-05-07T12:00:00Z")
        row = r.get_migration(plan.migration_id)
        assert row["status"] == "completed"
        assert row["completed_at"]

    def test_invalid_status_rejected_by_check_constraint(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        ws = _drydock()
        plan = plan_migration(
            drydock=ws, target=ProjectReloadTarget(), source_harbor="hetzner",
        )
        # Insert with valid status, then try to update to a bogus one.
        r.insert_migration(
            migration_id=plan.migration_id,
            drydock_id=ws.id,
            plan_json="{}",
        )
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            r.update_migration(plan.migration_id, status="bogus")

    def test_list_active_filters_by_drydock(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        for desk_id in ("dock_a", "dock_b", "dock_a"):
            mid = new_migration_id()
            r.insert_migration(
                migration_id=mid,
                drydock_id=desk_id,
                plan_json="{}",
                status="in_progress",
            )
        all_active = r.list_active_migrations()
        only_a = r.list_active_migrations(drydock_id="dock_a")
        assert len(all_active) == 3
        assert len(only_a) == 2

    def test_planned_excluded_from_active(self, tmp_path):
        r = Registry(db_path=tmp_path / "r.db")
        r.insert_migration(
            migration_id="mig_1",
            drydock_id="dock_x",
            plan_json="{}",
            status="planned",
        )
        # 'planned' (dry-run) is not 'in_progress'
        assert r.list_active_migrations() == []


class TestProbes:
    """Phase 2a.4 M4: live probes that compute precheck inputs."""

    def test_probe_disk_free_returns_int_for_existing_path(self, tmp_path):
        from drydock.core.migration import probe_disk_free
        result = probe_disk_free(tmp_path)
        assert isinstance(result, int)
        assert result > 0

    def test_probe_disk_free_returns_none_for_missing_path(self, tmp_path):
        from drydock.core.migration import probe_disk_free
        result = probe_disk_free(tmp_path / "does-not-exist")
        assert result is None

    def test_probe_target_image_present_true_when_inspect_succeeds(self):
        from unittest.mock import patch, MagicMock
        from drydock.core.migration import probe_target_image_present
        ok = MagicMock(); ok.returncode = 0; ok.stdout = "[]"; ok.stderr = ""
        with patch("subprocess.run", return_value=ok):
            assert probe_target_image_present("img:v1") is True

    def test_probe_target_image_present_false_when_inspect_fails(self):
        from unittest.mock import patch, MagicMock
        from drydock.core.migration import probe_target_image_present
        bad = MagicMock(); bad.returncode = 1; bad.stdout = ""; bad.stderr = "no such image"
        with patch("subprocess.run", return_value=bad):
            assert probe_target_image_present("img:v1") is False

    def test_probe_target_image_present_none_when_docker_unavailable(self):
        from unittest.mock import patch
        from drydock.core.migration import probe_target_image_present
        with patch("subprocess.run", side_effect=FileNotFoundError("no docker")):
            assert probe_target_image_present("img:v1") is None

    def test_probe_daemon_healthy_false_when_socket_missing(self, tmp_path):
        from drydock.core.migration import probe_daemon_healthy
        assert probe_daemon_healthy(socket_path=str(tmp_path / "no-sock")) is False
