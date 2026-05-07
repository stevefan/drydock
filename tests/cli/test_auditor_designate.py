"""Tests for `drydock auditor designate` (Phase PA3)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock


def _seed(tmp_path, *, with_token: bool = True):
    drydock_home = tmp_path / ".drydock"
    drydock_home.mkdir(parents=True, exist_ok=True)
    r = Registry(db_path=drydock_home / "registry.db")
    a = Drydock(name="auditor-desk", project="a", repo_path="/r")
    b = Drydock(name="other-desk", project="b", repo_path="/r")
    r.create_drydock(a)
    r.create_drydock(b)
    if with_token:
        r.insert_token(a.id, "hash_a", datetime.now(timezone.utc))
        r.insert_token(b.id, "hash_b", datetime.now(timezone.utc))
    r.close()


class TestDesignate:
    def test_designate_first_drydock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["designated"] is True
        assert data["scope_after"] == "auditor"

        # Persisted in registry
        r = Registry(db_path=tmp_path / ".drydock" / "registry.db")
        try:
            assert r.get_auditor_drydock_id() == "dock_auditor_desk"
        finally:
            r.close()

    def test_designate_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        runner.invoke(cli, ["--json", "auditor", "designate", "auditor-desk"])
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["designated"] is False  # no-op

    def test_designate_second_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        runner.invoke(cli, ["--json", "auditor", "designate", "auditor-desk"])
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "other-desk"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "already has the auditor scope" in err["error"]

    def test_designate_unknown_drydock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "nonexistent"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "drydock_not_found" in err["error"]

    def test_designate_without_token_refuses(self, tmp_path, monkeypatch):
        """Legacy desks without an issued token can't be designated;
        we'd have nothing to scope."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, with_token=False)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "no_token_issued" in err["error"]


class TestRevoke:
    def test_revoke_removes_auditor_scope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        runner.invoke(cli, ["--json", "auditor", "designate", "auditor-desk"])
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk", "--revoke"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["revoked"] is True
        assert data["scope_after"] == "dock"

        # And another drydock can now be designated
        result2 = runner.invoke(
            cli, ["--json", "auditor", "designate", "other-desk"],
        )
        assert result2.exit_code == 0
        data2 = json.loads(result2.output)
        assert data2["designated"] is True

    def test_revoke_idempotent(self, tmp_path, monkeypatch):
        """Revoking a non-auditor drydock is a no-op."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk", "--revoke"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["revoked"] is False
