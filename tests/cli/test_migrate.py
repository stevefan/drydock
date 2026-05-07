"""Tests for `drydock migrate` CLI (Phase 2a.4 M1).

Pin the contract:
- Target parsing: image=<tag>, reload, schema=<version>; bad forms surface clear errors.
- Unknown drydock surfaces drydock_not_found.
- Plan + pre-check executed; migration record persisted with status='planned'.
- In-flight migration on the same drydock blocks a new plan.
- Output shape: human + JSON, plan + precheck both included.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock


def _seed(tmp_path, *, image="img:v1", state="running"):
    drydock_home = tmp_path / ".drydock"
    drydock_home.mkdir(parents=True, exist_ok=True)
    r = Registry(db_path=drydock_home / "registry.db")
    ws = Drydock(
        name="test",
        project="test",
        repo_path="/r",
        image=image,
        state=state,
    )
    r.create_drydock(ws)
    r.close()


class TestTargetParsing:
    def test_image_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "--dry-run", "migrate", "test", "--target", "image=img:v2"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["plan"]["target_kind"] == "image_bump"
        assert "img:v2" in data["plan"]["target_summary"]["new_image"]

    def test_reload_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "--dry-run", "migrate", "test", "--target", "reload"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["plan"]["target_kind"] == "project_reload"

    def test_schema_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "--dry-run", "migrate", "test", "--target", "schema=8"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["plan"]["target_kind"] == "schema_migration"
        assert data["plan"]["target_summary"]["target_schema_version"] == 8

    def test_unknown_target_form_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "migrate", "test", "--target", "what-even"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "unknown --target form" in err["error"]

    def test_empty_image_tag_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "migrate", "test", "--target", "image="],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "empty image tag" in err["error"]

    def test_non_integer_schema_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "migrate", "test", "--target", "schema=abc"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "not an integer" in err["error"]


class TestUnknownDrydock:
    def test_drydock_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Initialize empty registry
        (tmp_path / ".drydock").mkdir()
        Registry(db_path=tmp_path / ".drydock" / "registry.db").close()
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "migrate", "nonexistent", "--target", "reload"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "drydock_not_found" in err["error"]


class TestPersistence:
    def test_migration_record_persisted_as_planned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "--dry-run", "migrate", "test", "--target", "reload"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Migration record exists in registry
        r = Registry(db_path=tmp_path / ".drydock" / "registry.db")
        try:
            row = r.get_migration(data["migration_id"])
            assert row is not None
            assert row["status"] == "planned"
            plan = json.loads(row["plan_json"])
            assert plan["target_kind"] == "project_reload"
        finally:
            r.close()


class TestInFlightBlocking:
    def test_existing_in_progress_migration_blocks_new_plan(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        # Manually insert an in-progress migration for this drydock
        r = Registry(db_path=tmp_path / ".drydock" / "registry.db")
        try:
            r.insert_migration(
                migration_id="mig_already_active",
                drydock_id="dock_test",
                plan_json="{}",
                status="in_progress",
            )
        finally:
            r.close()

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "migrate", "test", "--target", "reload"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "migration_in_progress" in err["error"]


class TestNoOpRefusal:
    def test_same_image_bump_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, image="img:v5")
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "migrate", "test", "--target", "image=img:v5"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "no-op" in err["error"]


class TestOutputShape:
    def test_dry_run_response_includes_plan_precheck_executed_flags(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "--dry-run", "migrate", "test", "--target", "reload"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "migration_id" in data
        assert "plan" in data
        assert "precheck" in data
        assert data["executed"] is False
        assert data["plan"]["drydock_name"] == "test"
        assert data["plan"]["target_kind"] == "project_reload"
        assert "estimated_downtime_seconds" in data["plan"]
