"""Tests for V2 audit-event emission from daemon handlers (Slice 4b).

Pin the contract that each handler emits the spec'd events with the
required `details` keys. Doesn't exhaustively test every error path —
just the happy/reject paths the spec calls out as required emissions
(docs/v2-design-state.md §1a).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from drydock.core.capability import CapabilityLease, CapabilityType
from drydock.core.policy import CapabilityKind
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.wsd import handlers
from drydock.wsd.capability_handlers import release_capability, request_capability


def _read_events(audit_log: Path) -> list[dict]:
    if not audit_log.exists():
        return []
    return [json.loads(line) for line in audit_log.read_text().strip().splitlines() if line]


def _v2_events(audit_log: Path) -> list[dict]:
    """Filter to V2-shape events only — they have `ts`+`principal`+`method`."""
    return [e for e in _read_events(audit_log) if "ts" in e and "method" in e]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from drydock.core import audit, checkout
    monkeypatch.setattr(audit, "DEFAULT_LOG_PATH", tmp_path / ".drydock" / "audit.log")
    monkeypatch.setattr(checkout, "DEFAULT_CHECKOUT_BASE", tmp_path / ".drydock" / "worktrees")
    db = tmp_path / "registry.db"
    Registry(db_path=db).close()
    return {
        "tmp_path": tmp_path,
        "db": db,
        "secrets_root": tmp_path / "secrets",
        "audit_log": tmp_path / ".drydock" / "audit.log",
    }


def _init_repo(path: Path):
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / ".devcontainer").mkdir()
    (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)


class TestCreateDeskEmission:
    def test_emits_desk_created_and_token_issued(self, env):
        repo = env["tmp_path"] / "repo"
        _init_repo(repo)
        result = handlers.create_desk(
            {"project": "proj", "name": "alpha", "repo_path": str(repo)},
            "req-create-1",
            None,
            registry_path=env["db"],
            secrets_root=env["secrets_root"],
            dry_run=True,
        )

        events = _v2_events(env["audit_log"])
        # token.issued emitted from _perform_create AND desk.created from
        # the handler — order matters (token first, then desk).
        names = [e["event"] for e in events]
        assert "token.issued" in names
        assert "desk.created" in names

        created = next(e for e in events if e["event"] == "desk.created")
        assert created["principal"] is None
        assert created["request_id"] == "req-create-1"
        assert created["method"] == "CreateDesk"
        assert created["result"] == "ok"
        assert created["details"]["desk_id"] == result["desk_id"]
        assert created["details"]["parent_desk_id"] is None


class TestDestroyDeskEmission:
    def test_emits_desk_destroyed_and_token_revoked(self, env):
        repo = env["tmp_path"] / "repo"
        _init_repo(repo)
        created = handlers.create_desk(
            {"project": "p", "name": "destroy-me", "repo_path": str(repo)},
            "req-c",
            None,
            registry_path=env["db"],
            secrets_root=env["secrets_root"],
            dry_run=True,
        )
        # Clear pre-destroy events for an easier assertion
        env["audit_log"].parent.mkdir(parents=True, exist_ok=True)
        env["audit_log"].write_text("")

        handlers.destroy_desk(
            {"name": "destroy-me"},
            "req-d",
            None,
            registry_path=env["db"],
            secrets_root=env["secrets_root"],
            dry_run=True,
        )

        names = [e["event"] for e in _v2_events(env["audit_log"])]
        assert "token.revoked" in names
        assert "desk.destroyed" in names

        destroyed = next(e for e in _v2_events(env["audit_log"])
                         if e["event"] == "desk.destroyed")
        assert destroyed["request_id"] == "req-d"
        assert destroyed["details"]["desk_id"] == created["desk_id"]
        assert destroyed["details"]["cascaded_children"] == []


class TestLeaseEmission:
    @pytest.fixture
    def lease_env(self, env):
        # Set up a desk + entitlement + secret on disk
        registry = Registry(db_path=env["db"])
        ws = Workspace(
            name="alpha",
            project="p",
            repo_path="/tmp/repo",
            worktree_path="/tmp/wt",
            branch="ws/alpha",
            state="running",
            container_id="cid_alpha",
        )
        registry.create_workspace(ws)
        registry.update_desk_delegations(
            "alpha",
            delegatable_secrets=["k"],
            capabilities=[CapabilityKind.REQUEST_SECRET_LEASES.value],
        )
        desk_id = ws.id
        registry.close()

        secret_dir = env["secrets_root"] / desk_id
        secret_dir.mkdir(parents=True)
        (secret_dir / "k").write_bytes(b"value")

        env["audit_log"].parent.mkdir(parents=True, exist_ok=True)
        env["audit_log"].parent.mkdir(parents=True, exist_ok=True)
        env["audit_log"].write_text("")  # clear setup noise
        return {**env, "desk_id": desk_id}

    @patch("drydock.wsd.capability_handlers._materialize_secret")
    def test_emits_lease_issued(self, _mat, lease_env):
        result = request_capability(
            {"type": "SECRET", "scope": {"secret_name": "k"}},
            "req-req",
            lease_env["desk_id"],
            registry_path=lease_env["db"],
            secrets_root=lease_env["secrets_root"],
        )
        events = [e for e in _v2_events(lease_env["audit_log"])
                  if e["event"] == "lease.issued"]
        assert len(events) == 1
        ev = events[0]
        assert ev["principal"] == lease_env["desk_id"]
        assert ev["request_id"] == "req-req"
        assert ev["method"] == "RequestCapability"
        assert ev["details"]["lease_id"] == result["lease_id"]
        assert ev["details"]["scope"] == {"secret_name": "k"}

    @patch("drydock.wsd.capability_handlers._remove_materialized_secret")
    @patch("drydock.wsd.capability_handlers._materialize_secret")
    def test_emits_lease_released_with_client_release_reason(self, _mat, _rm, lease_env):
        result = request_capability(
            {"type": "SECRET", "scope": {"secret_name": "k"}},
            "req-req",
            lease_env["desk_id"],
            registry_path=lease_env["db"],
            secrets_root=lease_env["secrets_root"],
        )
        lease_env["audit_log"].parent.mkdir(parents=True, exist_ok=True)
        lease_env["audit_log"].parent.mkdir(parents=True, exist_ok=True)
        lease_env["audit_log"].write_text("")  # clear lease.issued

        release_capability(
            {"lease_id": result["lease_id"]},
            "req-rel",
            lease_env["desk_id"],
            registry_path=lease_env["db"],
            secrets_root=lease_env["secrets_root"],
        )

        events = [e for e in _v2_events(lease_env["audit_log"])
                  if e["event"] == "lease.released"]
        assert len(events) == 1
        assert events[0]["details"]["reason"] == "client_release"

    @patch("drydock.wsd.capability_handlers._materialize_secret")
    def test_destroy_cascade_emits_lease_released_with_desk_destroyed_reason(
        self, _mat, lease_env
    ):
        request_capability(
            {"type": "SECRET", "scope": {"secret_name": "k"}},
            "req-req",
            lease_env["desk_id"],
            registry_path=lease_env["db"],
            secrets_root=lease_env["secrets_root"],
        )
        lease_env["audit_log"].parent.mkdir(parents=True, exist_ok=True)
        lease_env["audit_log"].write_text("")

        handlers.destroy_desk(
            {"desk_id": lease_env["desk_id"]},
            "req-d",
            None,
            registry_path=lease_env["db"],
            secrets_root=lease_env["secrets_root"],
            dry_run=True,
        )

        cascade_events = [e for e in _v2_events(lease_env["audit_log"])
                          if e["event"] == "lease.released"
                          and e["details"]["reason"] == "desk_destroyed"]
        assert len(cascade_events) == 1
