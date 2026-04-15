"""DestroyDesk in-process daemon contract tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from drydock.core.registry import Registry
from drydock.core import checkout
from drydock.wsd import handlers, server
from drydock.wsd.auth import validate_token


def _init_repo(path: Path, *, with_devcontainer: bool = True) -> None:
    if (path / ".git").exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    if with_devcontainer:
        (path / ".devcontainer").mkdir(exist_ok=True)
        (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _setup_env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(checkout, "DEFAULT_CHECKOUT_BASE", tmp_path / ".drydock" / "worktrees")
    registry_path = tmp_path / "registry.db"
    secrets_root = tmp_path / "secrets"
    Registry(db_path=registry_path).close()
    return registry_path, secrets_root


def _create_desk(tmp_path: Path, registry_path: Path, secrets_root: Path, *, name: str, request_id: str) -> tuple[dict[str, object], str]:
    repo = tmp_path / f"repo-{name}"
    _init_repo(repo)
    result = handlers.create_desk(
        {"project": "proj", "name": name, "repo_path": str(repo)},
        request_id,
        None,
        registry_path=registry_path,
        secrets_root=secrets_root,
        dry_run=True,
    )
    token = (secrets_root / result["desk_id"] / "drydock-token").read_text(encoding="utf-8").strip()
    return result, token


def _create_parent_with_child(tmp_path: Path, registry_path: Path, secrets_root: Path) -> tuple[dict[str, object], dict[str, object]]:
    repo = tmp_path / "repo-parent"
    _init_repo(repo)
    parent = handlers.create_desk(
        {
            "project": "proj",
            "name": "parent-destroy",
            "repo_path": str(repo),
            "delegatable_firewall_domains": ["example.com"],
            "capabilities": ["spawn_children"],
        },
        "create-parent",
        None,
        registry_path=registry_path,
        secrets_root=secrets_root,
        dry_run=True,
    )
    child = handlers.spawn_child(
        {
            "project": "proj",
            "name": "child-destroy",
            "repo_path": str(repo),
            "firewall_extra_domains": ["example.com"],
        },
        "spawn-child",
        str(parent["desk_id"]),
        registry_path=registry_path,
        secrets_root=secrets_root,
        dry_run=True,
    )
    return parent, child


def _call_destroy(request_id: str, *, params: dict[str, object], registry_path: Path, secrets_root: Path, monkeypatch):
    monkeypatch.setattr(server, "_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(server, "_SECRETS_ROOT", secrets_root)
    monkeypatch.setattr(server, "_DRY_RUN", True)
    return server._destroy_desk(params, request_id, None)


def test_destroy_desk_happy_path(tmp_path, monkeypatch):
    registry_path, secrets_root = _setup_env(tmp_path, monkeypatch)
    result, _ = _create_desk(tmp_path, registry_path, secrets_root, name="desk-destroy", request_id="create-destroy")

    response = _call_destroy(
        "destroy-destroy",
        params={"name": "desk-destroy"},
        registry_path=registry_path,
        secrets_root=secrets_root,
        monkeypatch=monkeypatch,
    )

    assert response == {"destroyed": True, "desk_id": result["desk_id"], "cascaded": []}

    conn = _connect(registry_path)
    workspace = conn.execute("SELECT * FROM workspaces WHERE name = ?", ("desk-destroy",)).fetchone()
    token_row = conn.execute("SELECT * FROM tokens WHERE desk_id = ?", (result["desk_id"],)).fetchone()
    conn.close()

    assert workspace is None
    assert token_row is None

    token_path = secrets_root / result["desk_id"] / "drydock-token"
    assert not token_path.exists()
    assert not token_path.parent.exists() or not any(token_path.parent.iterdir())

    worktree_path = Path(str(result["worktree_path"]))
    assert not worktree_path.exists()


def test_destroy_desk_cascades_to_children(tmp_path, monkeypatch):
    registry_path, secrets_root = _setup_env(tmp_path, monkeypatch)
    parent, child = _create_parent_with_child(tmp_path, registry_path, secrets_root)

    order: list[str] = []
    original = handlers._destroy_one

    def record_order(workspace, registry, secrets_root, dry_run):
        order.append(workspace.id)
        return original(workspace, registry, secrets_root, dry_run)

    monkeypatch.setattr(handlers, "_destroy_one", record_order)
    response = handlers.destroy_desk(
        {"name": "parent-destroy"},
        "destroy-cascade",
        None,
        registry_path=registry_path,
        secrets_root=secrets_root,
        dry_run=True,
    )

    assert response["destroyed"] is True
    assert response["desk_id"] == parent["desk_id"]
    assert response["cascaded"] == [child["desk_id"]]
    assert order == [child["desk_id"], parent["desk_id"]]

    conn = _connect(registry_path)
    parent_row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (parent["desk_id"],)).fetchone()
    child_row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (child["desk_id"],)).fetchone()
    parent_token = conn.execute("SELECT * FROM tokens WHERE desk_id = ?", (parent["desk_id"],)).fetchone()
    child_token = conn.execute("SELECT * FROM tokens WHERE desk_id = ?", (child["desk_id"],)).fetchone()
    conn.close()

    assert parent_row is None
    assert child_row is None
    assert parent_token is None
    assert child_token is None


def test_destroy_desk_not_found(tmp_path, monkeypatch):
    registry_path, secrets_root = _setup_env(tmp_path, monkeypatch)

    try:
        _call_destroy(
            "destroy-missing",
            params={"name": "missing-destroy"},
            registry_path=registry_path,
            secrets_root=secrets_root,
            monkeypatch=monkeypatch,
        )
    except server._RpcError as exc:
        assert exc.code == -32001
        assert exc.message == "desk_not_found"
        assert exc.data == {"desk_id": "ws_missing_destroy"}
    else:
        assert False, "expected desk_not_found"


def test_destroy_desk_idempotent_by_request_id(tmp_path, monkeypatch):
    registry_path, secrets_root = _setup_env(tmp_path, monkeypatch)
    result, _ = _create_desk(tmp_path, registry_path, secrets_root, name="desk-idem-destroy", request_id="create-idem-destroy")

    params = {"name": "desk-idem-destroy"}
    first = _call_destroy("r1", params=params, registry_path=registry_path, secrets_root=secrets_root, monkeypatch=monkeypatch)
    second = _call_destroy("r1", params=params, registry_path=registry_path, secrets_root=secrets_root, monkeypatch=monkeypatch)

    assert second == first
    assert first["desk_id"] == result["desk_id"]

    conn = _connect(registry_path)
    workspace_count = conn.execute(
        "SELECT COUNT(*) AS n FROM workspaces WHERE name = ?",
        ("desk-idem-destroy",),
    ).fetchone()["n"]
    task_count = conn.execute(
        "SELECT COUNT(*) AS n FROM task_log WHERE request_id = ?",
        ("r1",),
    ).fetchone()["n"]
    conn.close()

    assert workspace_count == 0
    assert task_count == 1


def test_destroy_desk_revokes_token_and_removes_secret_file(tmp_path, monkeypatch):
    registry_path, secrets_root = _setup_env(tmp_path, monkeypatch)
    result, token = _create_desk(tmp_path, registry_path, secrets_root, name="desk-revoke", request_id="create-revoke")
    token_path = secrets_root / result["desk_id"] / "drydock-token"

    _call_destroy(
        "destroy-revoke",
        params={"name": "desk-revoke"},
        registry_path=registry_path,
        secrets_root=secrets_root,
        monkeypatch=monkeypatch,
    )

    registry = Registry(db_path=registry_path)
    try:
        assert registry.get_token_info(str(result["desk_id"])) is None
        assert validate_token(token, registry) is None
    finally:
        registry.close()
    assert not token_path.exists()


def test_destroy_desk_replayed_request_after_target_gone(tmp_path, monkeypatch):
    registry_path, secrets_root = _setup_env(tmp_path, monkeypatch)
    first, _ = _create_desk(tmp_path, registry_path, secrets_root, name="desk-recreate", request_id="create-recreate-1")
    destroy1 = _call_destroy("r1", params={"name": "desk-recreate"}, registry_path=registry_path, secrets_root=secrets_root, monkeypatch=monkeypatch)

    second, _ = _create_desk(tmp_path, registry_path, secrets_root, name="desk-recreate", request_id="create-recreate-2")
    destroy2 = _call_destroy("r2", params={"name": "desk-recreate"}, registry_path=registry_path, secrets_root=secrets_root, monkeypatch=monkeypatch)

    assert destroy1["desk_id"] == first["desk_id"]
    assert destroy2["desk_id"] == second["desk_id"]

    conn = _connect(registry_path)
    tasks = conn.execute(
        "SELECT request_id, status, outcome_json FROM task_log WHERE request_id IN ('r1', 'r2') ORDER BY request_id",
    ).fetchall()
    conn.close()

    assert [row["request_id"] for row in tasks] == ["r1", "r2"]
    assert [row["status"] for row in tasks] == ["completed", "completed"]
    assert json.loads(tasks[0]["outcome_json"]) == destroy1
    assert json.loads(tasks[1]["outcome_json"]) == destroy2
