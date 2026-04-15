"""CreateDesk daemon contract tests."""

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


def test_create_desk_happy_path(wsd):
    repo = wsd.home / "repo"
    _init_repo(repo)

    response = wsd.call_rpc(
        "CreateDesk",
        params={"project": "proj", "name": "desk-ok", "repo_path": str(repo)},
        request_id="req-happy",
    )
    result = response["result"]

    assert result["desk_id"] == "ws_desk_ok"
    assert result["name"] == "desk-ok"
    assert result["state"] == "running"
    assert result["container_id"].startswith("dry-run-")

    conn = _connect(wsd.registry_path)
    workspace = conn.execute(
        "SELECT * FROM workspaces WHERE name = ?",
        ("desk-ok",),
    ).fetchone()
    assert workspace is not None
    assert workspace["state"] == "running"
    assert workspace["container_id"]

    task = conn.execute(
        "SELECT * FROM task_log WHERE request_id = ?",
        ("req-happy",),
    ).fetchone()
    assert task is not None
    assert task["method"] == "CreateDesk"
    assert task["status"] == "completed"
    assert json.loads(task["outcome_json"]) == result
    conn.close()


def test_create_desk_idempotent_by_request_id(wsd):
    repo = wsd.home / "repo-idempotent"
    _init_repo(repo)

    params = {"project": "proj", "name": "desk-idem", "repo_path": str(repo)}
    first = wsd.call_rpc("CreateDesk", params=params, request_id="req-1")["result"]
    second = wsd.call_rpc("CreateDesk", params=params, request_id="req-1")["result"]

    assert second == first

    conn = _connect(wsd.registry_path)
    workspace_count = conn.execute(
        "SELECT COUNT(*) AS n FROM workspaces WHERE name = ?",
        ("desk-idem",),
    ).fetchone()["n"]
    task_count = conn.execute(
        "SELECT COUNT(*) AS n FROM task_log WHERE request_id = ?",
        ("req-1",),
    ).fetchone()["n"]
    conn.close()

    assert workspace_count == 1
    assert task_count == 1


def test_create_desk_already_running(wsd):
    repo = wsd.home / "repo-running"
    _init_repo(repo)

    params = {"project": "proj", "name": "x", "repo_path": str(repo)}
    first = wsd.call_rpc("CreateDesk", params=params, request_id="req-1")
    assert "result" in first

    second = wsd.call_rpc("CreateDesk", params=params, request_id="req-2")
    error = second["error"]
    assert error["code"] == -32001
    assert error["message"] == "workspace_already_running"
    assert "--force" in error["data"]["fix"]


def test_create_desk_invalid_params(wsd):
    response = wsd.call_rpc("CreateDesk", params=None, request_id="req-invalid")
    error = response["error"]
    assert error["code"] == -32602
    assert error["message"] == "invalid_params"
    assert error["data"]["missing"] == ["project", "name"]


def test_create_desk_persists_task_log_failure(wsd):
    repo = wsd.home / "repo-fail"
    _init_repo(repo, with_devcontainer=False)

    response = wsd.call_rpc(
        "CreateDesk",
        params={"project": "proj", "name": "desk-fail", "repo_path": str(repo)},
        request_id="req-fail",
    )
    error = response["error"]
    assert error["code"] == -32000
    assert error["message"] == "create_desk_failed"

    conn = _connect(wsd.registry_path)
    task = conn.execute(
        "SELECT * FROM task_log WHERE request_id = ?",
        ("req-fail",),
    ).fetchone()
    assert task is not None
    assert task["status"] == "failed"
    cached_error = json.loads(task["outcome_json"])
    assert cached_error["code"] == -32000
    assert cached_error["message"] == "create_desk_failed"
    assert "fix" in cached_error["data"]
    conn.close()


def test_create_desk_with_devcontainer_subpath(wsd):
    repo = wsd.home / "repo-subpath"
    _init_repo(repo, with_devcontainer=False)
    _add_devcontainer_variant(repo, ".devcontainer/drydock")

    response = wsd.call_rpc(
        "CreateDesk",
        params={
            "project": "proj",
            "name": "desk-subpath",
            "repo_path": str(repo),
            "devcontainer_subpath": ".devcontainer/drydock",
        },
        request_id="req-subpath",
    )
    result = response["result"]

    assert result["state"] == "running"

    conn = _connect(wsd.registry_path)
    workspace = conn.execute(
        "SELECT config, worktree_path FROM workspaces WHERE name = ?",
        ("desk-subpath",),
    ).fetchone()
    conn.close()

    assert workspace is not None
    config = json.loads(workspace["config"])
    assert config["devcontainer_subpath"] == ".devcontainer/drydock"
    expected_path = Path(workspace["worktree_path"]) / ".devcontainer" / "drydock" / "devcontainer.json"
    assert expected_path.exists()


def test_create_desk_rejects_absolute_devcontainer_subpath(wsd):
    repo = wsd.home / "repo-absolute"
    _init_repo(repo)

    response = wsd.call_rpc(
        "CreateDesk",
        params={
            "project": "proj",
            "name": "desk-absolute",
            "repo_path": str(repo),
            "devcontainer_subpath": "/etc",
        },
        request_id="req-absolute",
    )
    error = response["error"]

    assert error["code"] == -32602
    assert error["message"] == "invalid_params"
    assert error["data"]["reason"] == "devcontainer_subpath must be relative and contain no .."


def test_create_desk_rejects_dotdot_in_devcontainer_subpath(wsd):
    repo = wsd.home / "repo-dotdot"
    _init_repo(repo)

    response = wsd.call_rpc(
        "CreateDesk",
        params={
            "project": "proj",
            "name": "desk-dotdot",
            "repo_path": str(repo),
            "devcontainer_subpath": "../escape",
        },
        request_id="req-dotdot",
    )
    error = response["error"]

    assert error["code"] == -32602
    assert error["message"] == "invalid_params"
    assert error["data"]["reason"] == "devcontainer_subpath must be relative and contain no .."
