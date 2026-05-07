"""Tests for `drydock.core.migration_executor` (Phase 2a.4 M1).

Pin the contract:
- execute_migration walks PRECHECK → DRAIN → SNAPSHOT → STOP →
  MUTATE → START → VERIFY → CLEANUP for clean image bumps.
- Migration record's status walks: planned → in_progress → completed.
- A snapshot is captured (file exists) and recorded in migrations.snapshot_path.
- Mutate updates the registry's image field for image_bump targets.
- A failure post-snapshot triggers rollback; status='rolled_back'.
- A failure pre-snapshot fails fast; status='failed'.
- A rollback that itself fails ends in status='failed'.
- schema_migration target NotImplemented → fails (with rollback).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from drydock.core.migration import (
    ImageBumpTarget,
    MigrationStatus,
    ProjectReloadTarget,
    SchemaMigrationTarget,
    plan_migration,
)
from drydock.core.migration_executor import (
    ExecutorConfig,
    MigrationOutcome,
    StageFailure,
    execute_migration,
)
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock


def _ok_subprocess():
    """Mock a successful docker stop / etc."""
    res = MagicMock()
    res.returncode = 0
    res.stdout = ""
    res.stderr = ""
    return res


def _fail_subprocess(stderr="Error"):
    res = MagicMock()
    res.returncode = 1
    res.stdout = ""
    res.stderr = stderr
    return res


def _seed(tmp_path):
    """Set up a registry + drydock + a planned migration ready to execute."""
    secrets_root = tmp_path / "secrets"
    overlays_root = tmp_path / "overlays"
    migrations_root = tmp_path / "migrations"
    audit_log = tmp_path / "audit.log"
    for p in (secrets_root, overlays_root, migrations_root):
        p.mkdir(parents=True)

    # Redirect emit_audit's default log path so this test doesn't write
    # to ~/.drydock/audit.log.
    import drydock.core.audit as audit_mod
    audit_mod.DEFAULT_LOG_PATH = audit_log

    r = Registry(db_path=tmp_path / "r.db")
    ws = Drydock(
        name="test", project="test", repo_path="/r",
        image="img:v1", state="running",
        container_id="cid_test",
    )
    r.create_drydock(ws)
    r.update_drydock("test", state="running", container_id="cid_test")

    # Seed secrets dir + overlay file
    (secrets_root / ws.id).mkdir()
    (secrets_root / ws.id / "drydock-token").write_text("tok")
    (overlays_root / f"{ws.id}.devcontainer.json").write_text("{}")

    return {
        "registry": r,
        "ws": ws,
        "config": ExecutorConfig(
            secrets_root=secrets_root,
            overlays_root=overlays_root,
            migrations_root=migrations_root,
        ),
        "audit_log": audit_log,
    }


def _plan_image_bump(env, new_image="img:v2") -> str:
    """Plan an image-bump migration and return the migration_id."""
    plan = plan_migration(
        drydock=env["ws"],
        target=ImageBumpTarget(new_image=new_image),
        source_harbor="testharbor",
    )
    env["registry"].insert_migration(
        migration_id=plan.migration_id,
        drydock_id=env["ws"].id,
        plan_json=json.dumps(plan.to_dict()),
        status="planned",
    )
    return plan.migration_id


# ---------------------------------------------------------------------------
# Happy path: image bump
# ---------------------------------------------------------------------------


class TestImageBumpHappyPath:
    def test_walks_all_stages_to_completion(self, tmp_path):
        env = _seed(tmp_path)
        mid = _plan_image_bump(env, new_image="img:v2")

        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            outcome = execute_migration(
                mid, registry=env["registry"], config=env["config"],
            )

        assert outcome.terminal_status == MigrationStatus.COMPLETED.value
        stages = [s.stage for s in outcome.stages]
        # All forward stages ran in order.
        assert stages == [
            "precheck", "drain", "snapshot", "stop",
            "mutate", "start", "verify", "cleanup",
        ]
        # All ok.
        assert all(s.status == "ok" for s in outcome.stages)

    def test_status_walks_planned_to_completed(self, tmp_path):
        env = _seed(tmp_path)
        mid = _plan_image_bump(env)
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            execute_migration(mid, registry=env["registry"], config=env["config"])
        row = env["registry"].get_migration(mid)
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["current_stage"] == "cleanup"

    def test_snapshot_file_recorded(self, tmp_path):
        env = _seed(tmp_path)
        mid = _plan_image_bump(env)
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            outcome = execute_migration(mid, registry=env["registry"], config=env["config"])
        # Snapshot path persisted to the migrations row.
        row = env["registry"].get_migration(mid)
        assert row["snapshot_path"]
        snap_dir = Path(row["snapshot_path"])
        assert (snap_dir / "snapshot.tgz").is_file()
        assert (snap_dir / "manifest.json").is_file()
        assert outcome.snapshot_path == str(snap_dir)

    def test_image_field_updated_in_registry(self, tmp_path):
        env = _seed(tmp_path)
        mid = _plan_image_bump(env, new_image="img:v9")
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            execute_migration(mid, registry=env["registry"], config=env["config"])
        ws_after = env["registry"].get_drydock("test")
        assert ws_after.image == "img:v9"


# ---------------------------------------------------------------------------
# Project reload
# ---------------------------------------------------------------------------


class TestProjectReload:
    def test_project_reload_target_executes(self, tmp_path, monkeypatch):
        env = _seed(tmp_path)
        # Seed a project YAML so load_project_config finds it
        monkeypatch.setenv("HOME", str(tmp_path))
        projects_dir = tmp_path / ".drydock" / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "test.yaml").write_text(
            "repo_path: /r\n"
            "capabilities: [request_secret_leases]\n"
            "delegatable_secrets: [some-secret]\n"
        )

        plan = plan_migration(
            drydock=env["ws"],
            target=ProjectReloadTarget(),
            source_harbor="testharbor",
        )
        env["registry"].insert_migration(
            migration_id=plan.migration_id,
            drydock_id=env["ws"].id,
            plan_json=json.dumps(plan.to_dict()),
            status="planned",
        )
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            outcome = execute_migration(
                plan.migration_id, registry=env["registry"], config=env["config"],
            )
        assert outcome.terminal_status == "completed"
        # Mutate stage detail captures what got reloaded
        mutate_stage = next(s for s in outcome.stages if s.stage == "mutate")
        assert "project" in mutate_stage.detail
        assert "delegations_updated" in mutate_stage.detail


# ---------------------------------------------------------------------------
# Failure paths — pre-snapshot fail, post-snapshot rollback
# ---------------------------------------------------------------------------


class TestPreSnapshotFailure:
    def test_drydock_already_migrating_fails_fast_no_rollback(self, tmp_path):
        env = _seed(tmp_path)
        # Mark the desk as migrating to fail PRECHECK.
        env["registry"].update_drydock("test", state="migrating")
        mid = _plan_image_bump(env)

        outcome = execute_migration(mid, registry=env["registry"], config=env["config"])
        assert outcome.terminal_status == MigrationStatus.FAILED.value
        assert outcome.error["failed_stage"] == "precheck"
        # No snapshot should have been written (pre-snapshot failure).
        row = env["registry"].get_migration(mid)
        assert row["snapshot_path"] is None
        assert row["status"] == "failed"


class TestPostSnapshotRollback:
    def test_stop_failure_triggers_rollback(self, tmp_path):
        env = _seed(tmp_path)
        mid = _plan_image_bump(env, new_image="img:v2")

        # First N subprocess calls (snapshot's volume capture is skipped,
        # so the first real subprocess call is `docker stop` in STOP).
        # Configure docker stop to fail.
        call_log = []

        def _maybe_fail(cmd, **kw):
            call_log.append(cmd)
            if len(cmd) >= 2 and cmd[1] == "stop":
                return _fail_subprocess(stderr="containerd lost it")
            return _ok_subprocess()

        with patch(
            "drydock.core.migration_executor.subprocess.run",
            side_effect=_maybe_fail,
        ):
            outcome = execute_migration(
                mid, registry=env["registry"], config=env["config"],
            )

        # STOP failure → rollback succeeded → terminal=rolled_back
        assert outcome.terminal_status == MigrationStatus.ROLLED_BACK.value
        assert outcome.error["failed_stage"] == "stop"
        # Image should be reverted to the original via rollback
        ws_after = env["registry"].get_drydock("test")
        assert ws_after.image == "img:v1"
        # Migration row reflects rolled_back
        row = env["registry"].get_migration(mid)
        assert row["status"] == "rolled_back"

    def test_schema_migration_target_fails_with_rollback(self, tmp_path):
        env = _seed(tmp_path)
        plan = plan_migration(
            drydock=env["ws"],
            target=SchemaMigrationTarget(target_schema_version=99),
            source_harbor="testharbor",
        )
        env["registry"].insert_migration(
            migration_id=plan.migration_id,
            drydock_id=env["ws"].id,
            plan_json=json.dumps(plan.to_dict()),
            status="planned",
        )
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            outcome = execute_migration(
                plan.migration_id, registry=env["registry"], config=env["config"],
            )
        # MUTATE refuses schema_migration → rollback restores
        assert outcome.terminal_status == MigrationStatus.ROLLED_BACK.value
        assert outcome.error["failed_stage"] == "mutate"
        assert outcome.error.get("reason") == "schema_migration_not_implemented"


# ---------------------------------------------------------------------------
# Audit emissions
# ---------------------------------------------------------------------------


class TestAuditEvents:
    def test_started_and_per_stage_events_emitted(self, tmp_path):
        env = _seed(tmp_path)
        mid = _plan_image_bump(env)
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            execute_migration(mid, registry=env["registry"], config=env["config"])
        events = [
            json.loads(line)
            for line in env["audit_log"].read_text().splitlines()
            if line.strip()
        ]
        kinds = [e["event"] for e in events]
        assert "drydock.migration_started" in kinds
        # 8 forward stages: precheck, drain, snapshot, stop, mutate,
        # start, verify, cleanup
        assert kinds.count("drydock.migration_stage") == 8
        assert "drydock.migrated" in kinds


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestStartVerifyStages:
    """Pin the M1-followup START + VERIFY behavior."""

    def test_start_skipped_when_no_worktree_path(self, tmp_path):
        """Tests in this file create desks without worktree_path; the
        executor should treat START as a no-op rather than crash. This
        is the existing-test invariant; documenting it explicitly."""
        env = _seed(tmp_path)
        mid = _plan_image_bump(env)
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            outcome = execute_migration(mid, registry=env["registry"], config=env["config"])
        start_stage = next(s for s in outcome.stages if s.stage == "start")
        assert start_stage.detail.get("skipped") is True
        assert start_stage.detail.get("reason") == "no_worktree_path"

    def test_verify_skips_when_start_skipped(self, tmp_path):
        """Caught by smoke: when START skips (no worktree), VERIFY must
        skip too rather than refuse with 'not_running_after_start'.
        Otherwise the synthetic-desk smoke path always rolls back."""
        env = _seed(tmp_path)
        mid = _plan_image_bump(env)
        with patch(
            "drydock.core.migration_executor.subprocess.run",
            return_value=_ok_subprocess(),
        ):
            outcome = execute_migration(mid, registry=env["registry"], config=env["config"])
        verify_stage = next(s for s in outcome.stages if s.stage == "verify")
        assert verify_stage.detail.get("skipped") is True
        assert verify_stage.detail.get("reason") == "start_skipped"
        # And the migration completes cleanly, no rollback.
        assert outcome.terminal_status == "completed"

    def test_start_calls_resume_when_worktree_present(self, tmp_path):
        env = _seed(tmp_path)
        # Set worktree_path on the desk so START attempts the resume path.
        env["registry"].update_drydock("test", worktree_path="/tmp/fake-worktree")
        mid = _plan_image_bump(env, new_image="img:v2")

        # Patch _resume_desk to verify it's called and to return a
        # synthetic success without actually running devcontainer up.
        from unittest.mock import patch as _patch
        resumed_payload = {
            "drydock_id": "dock_test",
            "name": "test",
            "project": "test",
            "branch": "ws/test",
            "state": "running",
            "container_id": "cid_after_resume",
            "worktree_path": "/tmp/fake-worktree",
        }

        def _fake_resume(existing, *, registry, dry_run):
            # Real _resume_desk updates the registry; mirror that here.
            registry.update_drydock(existing.name, container_id="cid_after_resume", state="running")
            return resumed_payload

        with _patch("drydock.daemon.handlers._resume_desk", side_effect=_fake_resume), \
             _patch("drydock.core.migration_executor.subprocess.run",
                    return_value=_ok_subprocess()):
            outcome = execute_migration(mid, registry=env["registry"], config=env["config"])

        assert outcome.terminal_status == "completed"
        start_stage = next(s for s in outcome.stages if s.stage == "start")
        assert start_stage.detail.get("started") is True
        assert start_stage.detail.get("container_id") == "cid_after_resume"
        # VERIFY confirms state=running
        verify_stage = next(s for s in outcome.stages if s.stage == "verify")
        assert verify_stage.detail.get("verified") is True
        assert verify_stage.detail.get("drydock_state") == "running"

    def test_start_failure_triggers_rollback(self, tmp_path):
        """If _resume_desk raises (e.g., devcontainer up failed), the
        post-snapshot rollback path runs and restores the original image."""
        env = _seed(tmp_path)
        env["registry"].update_drydock("test", worktree_path="/tmp/fake-worktree")
        mid = _plan_image_bump(env, new_image="img:v2")

        from unittest.mock import patch as _patch
        with _patch("drydock.daemon.handlers._resume_desk",
                    side_effect=RuntimeError("devcontainer up died")), \
             _patch("drydock.core.migration_executor.subprocess.run",
                    return_value=_ok_subprocess()):
            outcome = execute_migration(mid, registry=env["registry"], config=env["config"])

        assert outcome.terminal_status == "rolled_back"
        assert outcome.error["failed_stage"] == "start"
        # Image reverted by rollback
        ws_after = env["registry"].get_drydock("test")
        assert ws_after.image == "img:v1"


class TestValidation:
    def test_unknown_migration_id_raises(self, tmp_path):
        env = _seed(tmp_path)
        with pytest.raises(ValueError) as exc:
            execute_migration(
                "mig_does_not_exist",
                registry=env["registry"], config=env["config"],
            )
        assert "not found" in str(exc.value)

    def test_already_executed_migration_refused(self, tmp_path):
        env = _seed(tmp_path)
        mid = _plan_image_bump(env)
        # Manually mark it already in_progress
        env["registry"].update_migration(mid, status="in_progress")
        with pytest.raises(ValueError) as exc:
            execute_migration(mid, registry=env["registry"], config=env["config"])
        assert "expects 'planned'" in str(exc.value)
