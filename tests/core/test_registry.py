import pytest

from drydock.core import WsError
from drydock.core.runtime import Drydock


def test_create_and_get(registry):
    ws = Drydock(name="test-ws", project="myapp", repo_path="/srv/code/myapp")
    created = registry.create_drydock(ws)
    assert created.id == "dock_test_ws"
    assert created.state == "defined"

    fetched = registry.get_drydock("test-ws")
    assert fetched is not None
    assert fetched.name == "test-ws"
    assert fetched.project == "myapp"


def test_create_duplicate_fails(registry):
    ws = Drydock(name="dup", project="app", repo_path="/srv/code/app")
    registry.create_drydock(ws)

    with pytest.raises(WsError, match="already exists"):
        registry.create_drydock(
            Drydock(name="dup", project="app", repo_path="/srv/code/app")
        )


def test_list_drydocks(registry):
    registry.create_drydock(
        Drydock(name="a", project="p1", repo_path="/srv/code/p1")
    )
    registry.create_drydock(
        Drydock(name="b", project="p2", repo_path="/srv/code/p2")
    )
    registry.create_drydock(
        Drydock(name="c", project="p1", repo_path="/srv/code/p1")
    )

    all_ws = registry.list_drydocks()
    assert len(all_ws) == 3

    p1_ws = registry.list_drydocks(project="p1")
    assert len(p1_ws) == 2
    assert all(ws.project == "p1" for ws in p1_ws)


def test_update_state(registry):
    ws = Drydock(name="s", project="app", repo_path="/srv/code/app")
    registry.create_drydock(ws)

    updated = registry.update_state("s", "running")
    assert updated.state == "running"

    fetched = registry.get_drydock("s")
    assert fetched.state == "running"


def test_delete_drydock(registry):
    ws = Drydock(name="del", project="app", repo_path="/srv/code/app")
    registry.create_drydock(ws)

    registry.delete_drydock("del")
    assert registry.get_drydock("del") is None


def test_get_nonexistent_returns_none(registry):
    assert registry.get_drydock("nope") is None


def test_registry_honors_env_override(tmp_path, monkeypatch):
    """Phase PA3.7: DRYDOCK_DAEMON_REGISTRY env points Registry() at the
    bind-mounted host registry inside containers (auditor role). Without
    this, the watch loop opened a fresh empty DB at ~/.drydock/registry.db
    inside the container and reported "empty harbor" while the real
    Harbor had drydocks. Caught at first end-to-end auditor run."""
    from drydock.core.registry import Registry
    from drydock.core.runtime import Drydock

    # Pre-seed a "host" DB at a non-default path
    host_db = tmp_path / "host" / "registry.db"
    host_db.parent.mkdir(parents=True)
    seed = Registry(db_path=host_db)
    try:
        seed.create_drydock(Drydock(name="visible", project="x", repo_path="/r"))
    finally:
        seed.close()

    # Point env override at the same DB; default-construct
    monkeypatch.setenv("DRYDOCK_DAEMON_REGISTRY", str(host_db))
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    r = Registry()
    try:
        assert r.db_path == host_db
        assert r.get_drydock("visible") is not None
    finally:
        r.close()


def test_registry_accepts_string_db_path(tmp_path):
    """Defensive: callers that pass DRYDOCK_DAEMON_REGISTRY env directly
    (raw string, not Path) shouldn't trip the .parent attribute access
    that previously errored with `'str' object has no attribute 'parent'`."""
    from drydock.core.registry import Registry
    db_str = str(tmp_path / "reg.db")
    r = Registry(db_path=db_str)
    try:
        assert r.db_path == tmp_path / "reg.db"
    finally:
        r.close()
