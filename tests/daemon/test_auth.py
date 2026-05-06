"""Bearer-token auth contract tests for the drydock daemon."""

from __future__ import annotations

import sqlite3
import stat
import subprocess
from pathlib import Path

from drydock.daemon.auth import hash_token


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


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_desk_and_read_token(daemon, *, name: str, request_id: str) -> tuple[dict, str]:
    repo = daemon.home / f"repo-{name}"
    _init_repo(repo)
    result = daemon.call_rpc(
        "CreateDesk",
        params={"project": "proj", "name": name, "repo_path": str(repo)},
        request_id=request_id,
    )["result"]
    token = (daemon.secrets_root / result["drydock_id"] / "drydock-token").read_text(encoding="utf-8").strip()
    return result, token


def test_create_desk_issues_token(daemon):
    result, token = _create_desk_and_read_token(daemon, name="desk-auth", request_id="req-auth")

    conn = _connect(daemon.registry_path)
    token_row = conn.execute(
        "SELECT drydock_id, token_sha256 FROM tokens WHERE drydock_id = ?",
        (result["drydock_id"],),
    ).fetchone()
    conn.close()

    assert token_row is not None
    assert token_row["drydock_id"] == result["drydock_id"]
    assert token_row["token_sha256"]

    token_path = daemon.secrets_root / result["drydock_id"] / "drydock-token"
    assert token_path.exists()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o400
    assert token
    assert hash_token(token) == token_row["token_sha256"]


def test_create_desk_idempotent_does_not_reissue_token(daemon):
    repo = daemon.home / "repo-idem-token"
    _init_repo(repo)
    params = {"project": "proj", "name": "desk-idem-token", "repo_path": str(repo)}

    first = daemon.call_rpc("CreateDesk", params=params, request_id="r1")["result"]
    token_path = daemon.secrets_root / first["drydock_id"] / "drydock-token"
    first_token = token_path.read_text(encoding="utf-8").strip()

    second = daemon.call_rpc("CreateDesk", params=params, request_id="r1")["result"]
    second_token = token_path.read_text(encoding="utf-8").strip()

    conn = _connect(daemon.registry_path)
    token_count = conn.execute("SELECT COUNT(*) AS n FROM tokens").fetchone()["n"]
    conn.close()

    assert second == first
    assert second_token == first_token
    assert token_count == 1


def test_whoami_returns_caller_desk_id_with_valid_token(daemon):
    result, token = _create_desk_and_read_token(daemon, name="desk-whoami", request_id="req-whoami-create")

    response = daemon.call_rpc("daemon.whoami", request_id="req-whoami", auth=token)

    assert response == {"result": {"drydock_id": result["drydock_id"]}}


def test_whoami_unauthenticated_without_token(daemon):
    response = daemon.call_rpc("daemon.whoami", request_id="req-no-token")

    assert response["error"]["code"] == -32004
    assert response["error"]["message"] == "unauthenticated"
    assert response["error"]["data"]["reason"] == "no_token"


def test_whoami_unauthenticated_with_invalid_token(daemon):
    response = daemon.call_rpc("daemon.whoami", request_id="req-bad-token", auth="garbage-not-a-real-token")

    assert response["error"]["code"] == -32004
    assert response["error"]["message"] == "unauthenticated"
    assert response["error"]["data"]["reason"] == "invalid_token"


def test_health_works_without_auth(daemon):
    response = daemon.call_rpc("daemon.health", request_id="req-health-no-auth")

    assert response["result"]["ok"] is True


def test_health_ignores_provided_token(daemon):
    response = daemon.call_rpc("daemon.health", request_id="req-health-junk-auth", auth="garbage")

    assert response["result"]["ok"] is True
