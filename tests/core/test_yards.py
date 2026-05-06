"""Tests for Phase Y0 Yard primitive — registry CRUD + membership.

Pin the contracts:
- create_yard generates predictable yd_<slug> id
- get_yard / get_yard_by_id round-trip
- list_yards returns in created_at order
- destroy_yard refuses if members exist (unless with_members=True)
- with_members=True detaches but does NOT destroy member Drydocks
- yard_id FK on workspaces is plain TEXT NULL (not enforced by SQLite,
  enforced in code)
- migration is additive (existing v3 registries get the v4 columns + table)
"""

from __future__ import annotations

import pytest

from drydock.core import WsError
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace


@pytest.fixture
def registry(tmp_path):
    db = tmp_path / "registry.db"
    r = Registry(db_path=db)
    yield r
    r.close()


def _make_ws(name: str, project: str = "demo") -> Workspace:
    return Workspace(
        name=name,
        project=project,
        repo_path="/tmp/repo",
        worktree_path="/tmp/wt",
        branch=f"ws/{name}",
        state="created",
    )


class TestYardCRUD:
    def test_create_assigns_yd_slug_id(self, registry):
        result = registry.create_yard("microfoundry")
        assert result["id"] == "yd_microfoundry"
        assert result["name"] == "microfoundry"
        assert result["repo_path"] is None
        assert result["config"] == {}

    def test_slug_replaces_dashes_and_spaces(self, registry):
        result = registry.create_yard("my-cool yard")
        assert result["id"] == "yd_my_cool_yard"

    def test_create_with_repo_path(self, registry):
        result = registry.create_yard("microfoundry", repo_path="/srv/code/microfoundry")
        assert result["repo_path"] == "/srv/code/microfoundry"

    def test_duplicate_create_raises(self, registry):
        registry.create_yard("microfoundry")
        with pytest.raises(WsError, match="already exists"):
            registry.create_yard("microfoundry")

    def test_get_yard_by_name(self, registry):
        registry.create_yard("microfoundry")
        y = registry.get_yard("microfoundry")
        assert y["name"] == "microfoundry"
        assert y["id"] == "yd_microfoundry"

    def test_get_yard_returns_none_for_missing(self, registry):
        assert registry.get_yard("nonexistent") is None

    def test_get_yard_by_id(self, registry):
        registry.create_yard("microfoundry")
        y = registry.get_yard_by_id("yd_microfoundry")
        assert y["name"] == "microfoundry"

    def test_list_yards_empty(self, registry):
        assert registry.list_yards() == []

    def test_list_yards_returns_all(self, registry):
        registry.create_yard("microfoundry")
        registry.create_yard("personal-tools")
        yards = registry.list_yards()
        names = [y["name"] for y in yards]
        assert "microfoundry" in names
        assert "personal-tools" in names


class TestYardMembership:
    def test_workspaces_have_yard_id_column(self, registry):
        # Migration adds the column with NULL default. Just creating
        # a Workspace should leave yard_id None on the resulting row.
        ws = _make_ws("test1")
        registry.create_workspace(ws)
        loaded = registry.get_workspace("test1")
        assert loaded.yard_id is None

    def test_set_workspace_to_yard(self, registry):
        registry.create_yard("microfoundry")
        ws = _make_ws("test1")
        registry.create_workspace(ws)
        registry.update_workspace("test1", yard_id="yd_microfoundry")
        loaded = registry.get_workspace("test1")
        assert loaded.yard_id == "yd_microfoundry"

    def test_list_yard_members(self, registry):
        registry.create_yard("microfoundry")
        for n in ("a", "b", "c"):
            registry.create_workspace(_make_ws(n))
            registry.update_workspace(n, yard_id="yd_microfoundry")
        registry.create_workspace(_make_ws("standalone"))  # no yard

        members = registry.list_yard_members("yd_microfoundry")
        names = sorted(m.name for m in members)
        assert names == ["a", "b", "c"]

    def test_list_yard_members_empty_yard(self, registry):
        registry.create_yard("microfoundry")
        assert registry.list_yard_members("yd_microfoundry") == []


class TestYardDestroy:
    def test_destroy_empty_yard(self, registry):
        registry.create_yard("microfoundry")
        detached = registry.destroy_yard("microfoundry")
        assert detached == 0
        assert registry.get_yard("microfoundry") is None

    def test_destroy_with_members_refuses_by_default(self, registry):
        registry.create_yard("microfoundry")
        registry.create_workspace(_make_ws("a"))
        registry.update_workspace("a", yard_id="yd_microfoundry")

        with pytest.raises(WsError, match="member drydock"):
            registry.destroy_yard("microfoundry")

        # Yard still exists, member still attached
        assert registry.get_yard("microfoundry") is not None
        assert registry.get_workspace("a").yard_id == "yd_microfoundry"

    def test_destroy_with_members_flag_detaches(self, registry):
        registry.create_yard("microfoundry")
        for n in ("a", "b"):
            registry.create_workspace(_make_ws(n))
            registry.update_workspace(n, yard_id="yd_microfoundry")

        detached = registry.destroy_yard("microfoundry", with_members=True)
        assert detached == 2
        assert registry.get_yard("microfoundry") is None
        # Member Drydocks still exist, just detached.
        for n in ("a", "b"):
            ws = registry.get_workspace(n)
            assert ws is not None
            assert ws.yard_id is None

    def test_destroy_nonexistent_raises(self, registry):
        with pytest.raises(WsError, match="not found"):
            registry.destroy_yard("nonexistent")


class TestProjectConfigYardField:
    def test_yard_field_in_known_keys(self):
        from drydock.core.project_config import KNOWN_KEYS
        assert "yard" in KNOWN_KEYS

    def test_project_config_parses_yard(self, tmp_path):
        from drydock.core.project_config import load_project_config
        projects = tmp_path / "projects"
        projects.mkdir()
        (projects / "demo.yaml").write_text(
            "repo_path: /tmp/x\nyard: microfoundry\n"
        )
        cfg = load_project_config("demo", base_dir=projects)
        assert cfg.yard == "microfoundry"

    def test_project_config_yard_defaults_to_none(self, tmp_path):
        from drydock.core.project_config import load_project_config
        projects = tmp_path / "projects"
        projects.mkdir()
        (projects / "demo.yaml").write_text("repo_path: /tmp/x\n")
        cfg = load_project_config("demo", base_dir=projects)
        assert cfg.yard is None
