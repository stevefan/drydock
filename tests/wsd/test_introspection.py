"""Introspection RPC contract tests: ListDesks, ListChildren, InspectDesk, StopDesk."""

from __future__ import annotations

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


def _create_desk(wsd, repo: Path, name: str, request_id: str) -> dict:
    response = wsd.call_rpc(
        "CreateDesk",
        params={"project": "proj", "name": name, "repo_path": str(repo)},
        request_id=request_id,
    )
    return response["result"]


# Contract: ListDesks returns desks that were created via CreateDesk.
# Fails if registry reads are wired incorrectly or serialization drifts.
def test_list_desks_returns_created_desks(wsd):
    repo = wsd.home / "repo"
    _init_repo(repo)
    _create_desk(wsd, repo, "desk-a", "req-a")
    _create_desk(wsd, repo, "desk-b", "req-b")

    response = wsd.call_rpc("ListDesks", request_id="req-list")
    result = response["result"]

    assert "desks" in result
    names = {d["name"] for d in result["desks"]}
    assert "desk-a" in names
    assert "desk-b" in names


# Contract: ListChildren returns only children of the specified parent,
# not all desks. Fails if parent_desk_id filtering is broken.
def test_list_children_returns_only_children(wsd):
    repo = wsd.home / "repo"
    _init_repo(repo)

    # Create a parent desk and get its token for auth
    parent = _create_desk(wsd, repo, "parent-desk", "req-parent")
    parent_id = parent["desk_id"]

    # Read the token from the secrets dir
    token_path = wsd.secrets_root / parent_id / "drydock-token"
    token = token_path.read_text()

    # Spawn a child via SpawnChild (authenticated)
    child_resp = wsd.call_rpc(
        "SpawnChild",
        params={"project": "proj", "name": "child-desk", "repo_path": str(repo)},
        request_id="req-child",
        auth=token,
    )
    assert "result" in child_resp

    # Create an unrelated desk (not a child of parent)
    _create_desk(wsd, repo, "unrelated-desk", "req-unrelated")

    # ListChildren with explicit parent_id
    response = wsd.call_rpc(
        "ListChildren",
        params={"parent_id": parent_id},
        request_id="req-list-children",
        auth=token,
    )
    result = response["result"]

    assert "children" in result
    child_names = {c["name"] for c in result["children"]}
    assert "child-desk" in child_names
    assert "unrelated-desk" not in child_names


# Contract: InspectDesk returns the full workspace dict for an existing desk.
# Fails if to_dict() wiring or lookup logic drifts.
def test_inspect_desk_returns_full_dict(wsd):
    repo = wsd.home / "repo"
    _init_repo(repo)
    created = _create_desk(wsd, repo, "desk-inspect", "req-inspect-create")

    response = wsd.call_rpc(
        "InspectDesk",
        params={"name": "desk-inspect"},
        request_id="req-inspect",
    )
    result = response["result"]

    assert result["name"] == "desk-inspect"
    assert result["project"] == "proj"
    assert result["id"] == created["desk_id"]
    # to_dict() includes these structural fields
    assert "state" in result
    assert "config" in result


# Contract: InspectDesk returns desk_not_found for a missing desk.
# Fails if the error code or shape drifts from the protocol.
def test_inspect_desk_not_found(wsd):
    response = wsd.call_rpc(
        "InspectDesk",
        params={"name": "no-such-desk"},
        request_id="req-missing",
    )
    assert response["error"]["code"] == -32001
    assert response["error"]["message"] == "desk_not_found"


# Contract: StopDesk on a running desk returns state=suspended.
# Uses the wsd fixture which runs in dry_run mode (no real container stop).
def test_stop_desk_returns_suspended(wsd):
    repo = wsd.home / "repo"
    _init_repo(repo)
    created = _create_desk(wsd, repo, "desk-stop", "req-stop-create")

    response = wsd.call_rpc(
        "StopDesk",
        params={"name": "desk-stop"},
        request_id="req-stop",
    )
    result = response["result"]

    assert result["desk_id"] == created["desk_id"]
    assert result["name"] == "desk-stop"
    assert result["state"] == "suspended"
