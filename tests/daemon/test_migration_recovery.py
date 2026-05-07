"""Tests for daemon-restart recovery of in-progress migrations.

Phase 2a.4 M1. If the daemon dies mid-migration, the migrations row
stays `status='in_progress'` with the last-completed stage in
`current_stage`. Recovery's job is to either roll back from the
snapshot (if one was captured) or mark failed (no safe rollback).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.daemon.recovery import recover_in_progress_migrations


def _seed_audit(tmp_path):
    import drydock.core.audit as audit_mod
    audit_mod.DEFAULT_LOG_PATH = tmp_path / "audit.log"


def _seed_registry_with_drydock(tmp_path, image="img:v1"):
    db = tmp_path / "registry.db"
    r = Registry(db_path=db)
    ws = Drydock(
        name="test", project="test", repo_path="/r",
        image=image, state="running",
    )
    r.create_drydock(ws)
    r.close()
    return db


class TestRecoveryNoSnapshot:
    def test_pre_snapshot_in_progress_marked_failed(self, tmp_path):
        """A migration that died at PRECHECK or DRAIN has no snapshot;
        nothing to roll back from. Mark failed."""
        _seed_audit(tmp_path)
        db = _seed_registry_with_drydock(tmp_path)
        # Insert a stuck migration with no snapshot_path
        r = Registry(db_path=db)
        try:
            r.insert_migration(
                migration_id="mig_stuck",
                drydock_id="dock_test",
                plan_json="{}",
                status="planned",
            )
            r.update_migration("mig_stuck", status="in_progress",
                               current_stage="precheck")
        finally:
            r.close()

        rolled_back, failed = recover_in_progress_migrations(db)
        assert rolled_back == 0
        assert failed == 1

        r = Registry(db_path=db)
        try:
            row = r.get_migration("mig_stuck")
            assert row["status"] == "failed"
            err = json.loads(row["error_json"])
            assert err["reason"] == "daemon_restart_recovery"
            assert err["current_stage"] == "precheck"
        finally:
            r.close()

    def test_no_in_progress_migrations_is_noop(self, tmp_path):
        _seed_audit(tmp_path)
        db = _seed_registry_with_drydock(tmp_path)
        rolled_back, failed = recover_in_progress_migrations(db)
        assert rolled_back == 0
        assert failed == 0


class TestRecoveryWithSnapshot:
    def test_post_snapshot_rolled_back(self, tmp_path):
        """A migration with a captured snapshot gets restored from it."""
        _seed_audit(tmp_path)
        # Set up a desk with image:v1.
        db = _seed_registry_with_drydock(tmp_path, image="img:v1")
        secrets_root = tmp_path / "secrets"
        overlays_root = tmp_path / "overlays"
        secrets_root.mkdir()
        overlays_root.mkdir()
        # Seed secrets dir + overlay file so snapshot has something to capture.
        (secrets_root / "dock_test").mkdir()
        (secrets_root / "dock_test" / "drydock-token").write_text("tok")
        (overlays_root / "dock_test.devcontainer.json").write_text("{}")

        r = Registry(db_path=db)
        try:
            ws = r.get_drydock("test")
        finally:
            r.close()

        # Capture a snapshot
        from drydock.core.snapshot import snapshot_drydock
        r = Registry(db_path=db)
        try:
            snapshot_dir, _ = snapshot_drydock(
                ws, migration_id="mig_postsnap", registry=r,
                secrets_root=secrets_root, overlays_root=overlays_root,
                migrations_root=tmp_path / "migrations",
                capture_volumes=False,
            )
        finally:
            r.close()

        # Now mutate the desk's image to img:v2 (simulating that MUTATE
        # ran before the daemon died).
        r = Registry(db_path=db)
        try:
            r.update_drydock("test", image="img:v2")
            r.insert_migration(
                migration_id="mig_postsnap",
                drydock_id="dock_test",
                plan_json="{}",
                status="planned",
            )
            r.update_migration(
                "mig_postsnap", status="in_progress",
                current_stage="mutate",
                snapshot_path=str(snapshot_dir),
            )
        finally:
            r.close()

        # Recovery should restore image to v1 and mark rolled_back.
        # Need to point Path.home() at tmp_path so the recovery's
        # secrets_root + overlays_root resolve correctly.
        from unittest.mock import patch
        with patch("drydock.daemon.recovery.Path.home", return_value=tmp_path):
            rolled_back, failed = recover_in_progress_migrations(db)

        assert rolled_back == 1
        assert failed == 0

        r = Registry(db_path=db)
        try:
            ws_after = r.get_drydock("test")
            assert ws_after.image == "img:v1"  # reverted from v2
            row = r.get_migration("mig_postsnap")
            assert row["status"] == "rolled_back"
        finally:
            r.close()

    def test_missing_snapshot_path_marked_failed(self, tmp_path):
        """If snapshot_path is set but the file is gone, mark failed
        rather than crash."""
        _seed_audit(tmp_path)
        db = _seed_registry_with_drydock(tmp_path)
        r = Registry(db_path=db)
        try:
            r.insert_migration(
                migration_id="mig_lost",
                drydock_id="dock_test",
                plan_json="{}",
                status="planned",
            )
            r.update_migration(
                "mig_lost", status="in_progress",
                current_stage="restore",
                snapshot_path=str(tmp_path / "does-not-exist"),
            )
        finally:
            r.close()

        rolled_back, failed = recover_in_progress_migrations(db)
        assert rolled_back == 0
        assert failed == 1

        r = Registry(db_path=db)
        try:
            row = r.get_migration("mig_lost")
            assert row["status"] == "failed"
        finally:
            r.close()
