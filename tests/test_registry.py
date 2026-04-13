import pytest

from drydock.core.errors import WsError
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace


def test_create_and_get(registry):
    ws = Workspace(name="test-ws", project="myapp", repo_path="/srv/code/myapp")
    created = registry.create_workspace(ws)
    assert created.id == "ws_test_ws"
    assert created.state == "defined"

    fetched = registry.get_workspace("test-ws")
    assert fetched is not None
    assert fetched.name == "test-ws"
    assert fetched.project == "myapp"


def test_create_duplicate_fails(registry):
    ws = Workspace(name="dup", project="app", repo_path="/srv/code/app")
    registry.create_workspace(ws)

    with pytest.raises(WsError, match="already exists"):
        registry.create_workspace(
            Workspace(name="dup", project="app", repo_path="/srv/code/app")
        )


def test_list_workspaces(registry):
    registry.create_workspace(
        Workspace(name="a", project="p1", repo_path="/srv/code/p1")
    )
    registry.create_workspace(
        Workspace(name="b", project="p2", repo_path="/srv/code/p2")
    )
    registry.create_workspace(
        Workspace(name="c", project="p1", repo_path="/srv/code/p1")
    )

    all_ws = registry.list_workspaces()
    assert len(all_ws) == 3

    p1_ws = registry.list_workspaces(project="p1")
    assert len(p1_ws) == 2
    assert all(ws.project == "p1" for ws in p1_ws)


def test_update_state(registry):
    ws = Workspace(name="s", project="app", repo_path="/srv/code/app")
    registry.create_workspace(ws)

    updated = registry.update_state("s", "running")
    assert updated.state == "running"

    fetched = registry.get_workspace("s")
    assert fetched.state == "running"


def test_delete_workspace(registry):
    ws = Workspace(name="del", project="app", repo_path="/srv/code/app")
    registry.create_workspace(ws)

    registry.delete_workspace("del")
    assert registry.get_workspace("del") is None


def test_get_nonexistent_returns_none(registry):
    assert registry.get_workspace("nope") is None
