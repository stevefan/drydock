"""SpawnChild daemon contract tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path


def _init_repo(path: Path, *, with_devcontainer: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    if with_devcontainer:
        (path / ".devcontainer").mkdir()
        (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


def _add_devcontainer_variant(path: Path, subpath: str) -> None:
    variant_dir = path / subpath
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "devcontainer.json").write_text("{}")
    # Must commit so create_checkout's clone picks it up — same gotcha
    # surfaced by the asi repo's untracked .devcontainer/.
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"add {subpath}"], cwd=path, capture_output=True, check=True)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_parent(wsd, *, name: str, request_id: str, capabilities: list[str], domains: list[str]) -> tuple[dict, str]:
    repo = wsd.home / f"repo-{name}"
    _init_repo(repo)
    result = wsd.call_rpc(
        "CreateDesk",
        params={
            "project": "proj",
            "name": name,
            "repo_path": str(repo),
            "delegatable_firewall_domains": domains,
            "capabilities": capabilities,
        },
        request_id=request_id,
    )["result"]
    token = (wsd.secrets_root / result["desk_id"] / "drydock-token").read_text(encoding="utf-8").strip()
    return result, token


def test_spawn_child_happy_path_with_narrow_subset(wsd):
    parent, token = _create_parent(
        wsd,
        name="parent-happy",
        request_id="parent-happy",
        capabilities=["spawn_children"],
        domains=["api.example.com", "example.com"],
    )

    response = wsd.call_rpc(
        "SpawnChild",
        params={
            "project": "proj",
            "name": "child-happy",
            "repo_path": str(wsd.home / "repo-parent-happy"),
            "firewall_extra_domains": ["api.example.com"],
            "capabilities": ["spawn_children"],
        },
        request_id="spawn-happy",
        auth=token,
    )
    result = response["result"]

    assert result["desk_id"] == "ws_child_happy"
    assert result["parent_desk_id"] == parent["desk_id"]
    assert result["state"] == "running"
    assert result["container_id"].startswith("dry-run-")

    conn = _connect(wsd.registry_path)
    row = conn.execute(
        "SELECT parent_desk_id FROM workspaces WHERE name = ?",
        ("child-happy",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["parent_desk_id"] == parent["desk_id"]


def test_spawn_child_narrowness_violated_firewall(wsd):
    _, token = _create_parent(
        wsd,
        name="parent-firewall",
        request_id="parent-firewall",
        capabilities=["spawn_children"],
        domains=["a.com"],
    )

    response = wsd.call_rpc(
        "SpawnChild",
        params={
            "project": "proj",
            "name": "child-firewall",
            "repo_path": str(wsd.home / "repo-parent-firewall"),
            "firewall_extra_domains": ["b.com"],
        },
        request_id="spawn-firewall",
        auth=token,
    )
    error = response["error"]

    assert error["code"] == -32001
    assert error["message"] == "narrowness_violated"
    assert error["data"]["reject"]["rule"] == "firewall_narrowness"
    assert error["data"]["reject"]["offending_item"] == "b.com"

    conn = _connect(wsd.registry_path)
    row = conn.execute(
        "SELECT * FROM workspaces WHERE name = ?",
        ("child-firewall",),
    ).fetchone()
    conn.close()

    assert row is None


def test_spawn_child_narrowness_violated_capability(wsd):
    _, token = _create_parent(
        wsd,
        name="parent-capability",
        request_id="parent-capability",
        capabilities=[],
        domains=["example.com"],
    )

    response = wsd.call_rpc(
        "SpawnChild",
        params={
            "project": "proj",
            "name": "child-capability",
            "repo_path": str(wsd.home / "repo-parent-capability"),
            "capabilities": ["spawn_children"],
        },
        request_id="spawn-capability",
        auth=token,
    )
    error = response["error"]

    assert error["code"] == -32001
    assert error["message"] == "narrowness_violated"
    assert error["data"]["reject"]["rule"] == "capability_narrowness"
    assert error["data"]["reject"]["offending_item"] == "spawn_children"


def test_spawn_child_unauthenticated_no_token(wsd):
    repo = wsd.home / "repo-no-token"
    _init_repo(repo)

    response = wsd.call_rpc(
        "SpawnChild",
        params={"project": "proj", "name": "child-no-token", "repo_path": str(repo)},
        request_id="spawn-no-token",
    )

    assert response["error"]["code"] == -32004
    assert response["error"]["message"] == "unauthenticated"
    assert response["error"]["data"]["reason"] == "no_token"


def test_spawn_child_parent_not_found(wsd):
    parent, token = _create_parent(
        wsd,
        name="parent-gone",
        request_id="parent-gone",
        capabilities=["spawn_children"],
        domains=["example.com"],
    )

    conn = _connect(wsd.registry_path)
    conn.execute("DELETE FROM workspaces WHERE id = ?", (parent["desk_id"],))
    conn.commit()
    conn.close()

    response = wsd.call_rpc(
        "SpawnChild",
        params={
            "project": "proj",
            "name": "child-gone",
            "repo_path": str(wsd.home / "repo-parent-gone"),
        },
        request_id="spawn-gone",
        auth=token,
    )

    assert response["error"]["code"] == -32001
    assert response["error"]["message"] == "parent_not_found"
    assert response["error"]["data"]["parent_desk_id"] == parent["desk_id"]


def test_spawn_child_idempotent_by_request_id(wsd):
    parent, token = _create_parent(
        wsd,
        name="parent-idem",
        request_id="parent-idem",
        capabilities=["spawn_children"],
        domains=["api.example.com", "example.com"],
    )

    params = {
        "project": "proj",
        "name": "child-idem",
        "repo_path": str(wsd.home / "repo-parent-idem"),
        "firewall_extra_domains": ["api.example.com"],
        "capabilities": ["spawn_children"],
    }
    first = wsd.call_rpc("SpawnChild", params=params, request_id="r1", auth=token)["result"]
    second = wsd.call_rpc("SpawnChild", params=params, request_id="r1", auth=token)["result"]

    assert second == first
    assert second["parent_desk_id"] == parent["desk_id"]

    conn = _connect(wsd.registry_path)
    workspace_count = conn.execute(
        "SELECT COUNT(*) AS n FROM workspaces WHERE name = ?",
        ("child-idem",),
    ).fetchone()["n"]
    task_count = conn.execute(
        "SELECT COUNT(*) AS n FROM task_log WHERE request_id = ?",
        ("r1",),
    ).fetchone()["n"]
    task = conn.execute(
        "SELECT * FROM task_log WHERE request_id = ?",
        ("r1",),
    ).fetchone()
    conn.close()

    assert workspace_count == 1
    assert task_count == 1
    assert task["method"] == "SpawnChild"
    assert json.loads(task["outcome_json"]) == first


def test_spawn_child_with_devcontainer_subpath(wsd):
    parent, token = _create_parent(
        wsd,
        name="parent-subpath",
        request_id="parent-subpath",
        capabilities=["spawn_children"],
        domains=["example.com"],
    )
    del parent

    child_repo = wsd.home / "repo-parent-subpath"
    (child_repo / ".devcontainer" / "devcontainer.json").unlink()
    _add_devcontainer_variant(child_repo, ".devcontainer/drydock")

    response = wsd.call_rpc(
        "SpawnChild",
        params={
            "project": "proj",
            "name": "child-subpath",
            "repo_path": str(child_repo),
            "devcontainer_subpath": ".devcontainer/drydock",
        },
        request_id="spawn-subpath",
        auth=token,
    )
    result = response["result"]

    assert result["state"] == "running"

    conn = _connect(wsd.registry_path)
    workspace = conn.execute(
        "SELECT config, worktree_path FROM workspaces WHERE name = ?",
        ("child-subpath",),
    ).fetchone()
    conn.close()

    assert workspace is not None
    config = json.loads(workspace["config"])
    assert config["devcontainer_subpath"] == ".devcontainer/drydock"
    expected_path = Path(workspace["worktree_path"]) / ".devcontainer" / "drydock" / "devcontainer.json"
    assert expected_path.exists()
