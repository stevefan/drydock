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


_PASSING_AUDITOR_YAML = """
role: auditor
firewall_extra_domains:
  - api.anthropic.com
  - api.telegram.org
resources_hard:
  cpu_max: 1.0
  memory_max: 1g
"""


def _seed(
    tmp_path,
    *,
    with_token: bool = True,
    auditor_yaml: str | None = _PASSING_AUDITOR_YAML,
    other_yaml: str | None = _PASSING_AUDITOR_YAML,
):
    drydock_home = tmp_path / ".drydock"
    drydock_home.mkdir(parents=True, exist_ok=True)
    projects_dir = drydock_home / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    if auditor_yaml is not None:
        (projects_dir / "a.yaml").write_text(auditor_yaml)
    if other_yaml is not None:
        (projects_dir / "b.yaml").write_text(other_yaml)
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


class TestValidatorGate:
    """The validator IS the gate that makes role-locking real. These
    tests pin the gate's behavior in the CLI surface."""

    def test_missing_project_yaml_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, auditor_yaml=None)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "project_yaml_missing" in err["error"]

    def test_role_not_auditor_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, auditor_yaml="role: worker\n")
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "role_not_auditor" in err["error"]

    def test_validator_violations_refused(self, tmp_path, monkeypatch):
        """role: auditor declared but with broad egress — the validator
        catches it. Several violations expected."""
        monkeypatch.setenv("HOME", str(tmp_path))
        bad_yaml = """
role: auditor
firewall_extra_domains:
  - evil.example.com
firewall_aws_ip_ranges:
  - us-west-2:AMAZON
resources_hard:
  cpu_max: 8.0
  memory_max: 16g
"""
        _seed(tmp_path, auditor_yaml=bad_yaml)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--json", "auditor", "designate", "auditor-desk"],
        )
        assert result.exit_code != 0
        err = json.loads(result.output.strip())
        assert "auditor_role_violations" in err["error"]
        violation_codes = {
            v["code"] for v in err["context"]["violations"]
        }
        assert "egress-domain-not-allowed" in violation_codes
        assert "aws-egress-forbidden" in violation_codes
        assert "cpu-ceiling-exceeded" in violation_codes
        assert "memory-ceiling-exceeded" in violation_codes


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
