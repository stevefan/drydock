"""Tests for `drydock workload list/inspect` CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.core.workload import WorkloadSpec, assemble_lease


def _seed_lease(tmp_path, *, drydock_name="test", kind="batch",
                duration_seconds=3600):
    """Create a registry + a drydock + an active workload lease."""
    drydock_home = tmp_path / ".drydock"
    drydock_home.mkdir(parents=True, exist_ok=True)
    r = Registry(db_path=drydock_home / "registry.db")
    ws = Drydock(name=drydock_name, project=drydock_name, repo_path="/r")
    r.create_drydock(ws)
    spec = WorkloadSpec(kind=kind, duration_max_seconds=duration_seconds)
    lease = assemble_lease(drydock_id=ws.id, spec=spec, applied_actions=[])
    r.insert_workload_lease(lease)
    r.close()
    return lease.id


class TestWorkloadList:
    def test_list_active_only_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        lease_id = _seed_lease(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "workload", "list"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert any(row["id"] == lease_id for row in data)
        assert all(row["status"] == "active" for row in data)

    def test_list_filters_by_drydock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Two desks with leases each
        _seed_lease(tmp_path, drydock_name="desk-a")
        _seed_lease(tmp_path, drydock_name="desk-b")
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "workload", "list", "--drydock", "desk-a"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["drydock_id"] == "dock_desk_a"

    def test_list_unknown_drydock_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".drydock").mkdir()
        Registry(db_path=tmp_path / ".drydock" / "registry.db").close()
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "workload", "list", "--drydock", "ghost"])
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "drydock_not_found" in err["error"]

    def test_all_includes_released(self, tmp_path, monkeypatch):
        """`--all` shows leases that are no longer active."""
        monkeypatch.setenv("HOME", str(tmp_path))
        lease_id = _seed_lease(tmp_path)
        # Mark it released
        r = Registry(db_path=tmp_path / ".drydock" / "registry.db")
        try:
            r.mark_workload_lease_revoked(lease_id, revoke_results=[])
        finally:
            r.close()

        runner = CliRunner()
        # Without --all, should be empty
        result = runner.invoke(cli, ["--json", "workload", "list"])
        assert json.loads(result.output) == []
        # With --all, should include the released lease
        result = runner.invoke(cli, ["--json", "workload", "list", "--all"])
        data = json.loads(result.output)
        assert any(row["id"] == lease_id and row["status"] == "released" for row in data)

    def test_kind_extracted_from_spec(self, tmp_path, monkeypatch):
        """The 'kind' column reads from spec_json — confirms the
        embedded-JSON unwrap works for display."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_lease(tmp_path, kind="experiment")
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "workload", "list"])
        data = json.loads(result.output)
        assert data[0]["kind"] == "experiment"


class TestWorkloadInspect:
    def test_inspect_unwraps_embedded_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        lease_id = _seed_lease(tmp_path, kind="training")
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "workload", "inspect", lease_id])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Embedded JSON fields are unwrapped to nested objects
        assert "spec" in data
        assert data["spec"]["kind"] == "training"
        assert "applied_actions" in data
        # Original *_json fields are removed
        assert "spec_json" not in data
        assert "applied_actions_json" not in data

    def test_inspect_unknown_lease_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".drydock").mkdir()
        Registry(db_path=tmp_path / ".drydock" / "registry.db").close()
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "workload", "inspect", "wl_does_not_exist"])
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "lease_not_found" in err["error"]
