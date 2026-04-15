"""Crash recovery tests for `wsd` startup reconciliation."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.wsd.recovery import RecoveryReport, recover_in_progress


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_task(
    db_path: Path,
    *,
    request_id: str,
    method: str,
    spec: dict[str, object],
    status: str = "in_progress",
) -> None:
    conn = _connect(db_path)
    conn.execute(
        """
        INSERT INTO task_log
            (request_id, method, spec_json, status, outcome_json, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            method,
            json.dumps(spec),
            status,
            None,
            "2026-04-15T00:00:00+00:00",
            None,
        ),
    )
    conn.commit()
    conn.close()


def _task_row(db_path: Path, request_id: str) -> sqlite3.Row:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM task_log WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return row


def _workspace_row(db_path: Path, name: str) -> sqlite3.Row | None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM workspaces WHERE name = ?",
        (name,),
    ).fetchone()
    conn.close()
    return row


def _make_workspace(
    registry_path: Path,
    *,
    name: str,
    project: str = "proj",
    state: str = "defined",
    container_id: str = "",
    worktree_path: str = "",
) -> Workspace:
    registry = Registry(db_path=registry_path)
    try:
        ws = registry.create_workspace(
            Workspace(
                name=name,
                project=project,
                repo_path=f"/repos/{project}",
                branch=f"ws/{name}",
                state=state,
                container_id=container_id,
                worktree_path=worktree_path,
            )
        )
        return ws
    finally:
        registry.close()


def _wait_for_socket(path: Path, proc: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            _, stderr = proc.communicate(timeout=1)
            raise RuntimeError(
                f"wsd exited before socket appeared (rc={proc.returncode}); "
                f"stderr: {stderr.decode('utf-8', errors='replace')}"
            )
        time.sleep(0.02)
    raise TimeoutError(f"wsd socket {path} did not appear within {timeout}s")


def _call_rpc(socket_path: Path, payload: dict[str, object], timeout: float = 5.0) -> dict[str, object]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(socket_path))
    try:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8").strip())
    finally:
        sock.close()


def test_recovery_completes_in_progress_with_running_workspace(tmp_path):
    registry_path = tmp_path / "registry.db"
    ws = _make_workspace(
        registry_path,
        name="desk-ok",
        state="running",
        container_id="ctr-abc",
        worktree_path=str(tmp_path / "worktrees" / "ws_desk_ok"),
    )
    _insert_task(
        registry_path,
        request_id="r1",
        method="CreateDesk",
        spec={"project": "proj", "name": "desk-ok"},
    )

    report = recover_in_progress(registry_path)

    assert report == RecoveryReport(completed=1, rolled_back=0, unknown_method=0)
    task = _task_row(registry_path, "r1")
    assert task["status"] == "completed"
    outcome = json.loads(task["outcome_json"])
    assert outcome["desk_id"] == ws.id
    assert outcome["container_id"] == "ctr-abc"
    assert outcome["state"] == "running"


def test_recovery_rolls_back_in_progress_without_workspace(tmp_path):
    registry_path = tmp_path / "registry.db"
    Registry(db_path=registry_path).close()
    _insert_task(
        registry_path,
        request_id="r1",
        method="CreateDesk",
        spec={"project": "proj", "name": "ghost"},
    )

    report = recover_in_progress(registry_path)

    assert report == RecoveryReport(completed=0, rolled_back=1, unknown_method=0)
    task = _task_row(registry_path, "r1")
    outcome = json.loads(task["outcome_json"])
    assert task["status"] == "failed"
    assert outcome["code"] == -32000
    assert outcome["message"] == "crashed_during_create"


def test_recovery_rolls_back_partial_workspace(tmp_path):
    registry_path = tmp_path / "registry.db"
    worktree_path = tmp_path / "worktrees" / "ws_partial"
    worktree_path.mkdir(parents=True)
    (worktree_path / "marker.txt").write_text("partial")

    _make_workspace(
        registry_path,
        name="partial",
        state="provisioning",
        worktree_path=str(worktree_path),
    )
    _insert_task(
        registry_path,
        request_id="r1",
        method="CreateDesk",
        spec={"project": "proj", "name": "partial"},
    )

    report = recover_in_progress(registry_path)

    assert report == RecoveryReport(completed=0, rolled_back=1, unknown_method=0)
    assert _workspace_row(registry_path, "partial") is None
    assert not worktree_path.exists()
    task = _task_row(registry_path, "r1")
    assert task["status"] == "failed"


def test_recovery_rolls_back_partial_spawn_child(tmp_path):
    registry_path = tmp_path / "registry.db"
    worktree_path = tmp_path / "worktrees" / "ws_child_partial"
    worktree_path.mkdir(parents=True)
    (worktree_path / "marker.txt").write_text("partial")

    registry = Registry(db_path=registry_path)
    try:
        registry.create_workspace(
            Workspace(
                name="child-partial",
                project="proj",
                repo_path="/repos/proj",
                branch="ws/child-partial",
                state="provisioning",
                worktree_path=str(worktree_path),
            )
        )
        registry.update_workspace("child-partial", parent_desk_id="ws_parent")
    finally:
        registry.close()
    _insert_task(
        registry_path,
        request_id="r-spawn",
        method="SpawnChild",
        spec={"project": "proj", "name": "child-partial"},
    )

    report = recover_in_progress(registry_path)

    assert report == RecoveryReport(completed=0, rolled_back=1, unknown_method=0)
    assert _workspace_row(registry_path, "child-partial") is None
    assert not worktree_path.exists()
    task = _task_row(registry_path, "r-spawn")
    outcome = json.loads(task["outcome_json"])
    assert task["status"] == "failed"
    assert outcome["message"] == "crashed_during_create"


def test_recovery_is_idempotent(tmp_path):
    registry_path = tmp_path / "registry.db"
    _make_workspace(
        registry_path,
        name="desk-idem",
        state="running",
        container_id="ctr-abc",
        worktree_path=str(tmp_path / "worktrees" / "ws_desk_idem"),
    )
    _insert_task(
        registry_path,
        request_id="r1",
        method="CreateDesk",
        spec={"project": "proj", "name": "desk-idem"},
    )

    first = recover_in_progress(registry_path)
    second = recover_in_progress(registry_path)

    assert first == RecoveryReport(completed=1, rolled_back=0, unknown_method=0)
    assert second == RecoveryReport(completed=0, rolled_back=0, unknown_method=0)
    assert _task_row(registry_path, "r1")["status"] == "completed"


def test_recovery_handles_unknown_method(tmp_path):
    registry_path = tmp_path / "registry.db"
    _make_workspace(
        registry_path,
        name="untouched",
        state="running",
        container_id="ctr-keep",
        worktree_path=str(tmp_path / "worktrees" / "ws_untouched"),
    )
    _insert_task(
        registry_path,
        request_id="r1",
        method="SomeFutureMethod",
        spec={"project": "proj", "name": "untouched"},
    )

    report = recover_in_progress(registry_path)

    assert report == RecoveryReport(completed=0, rolled_back=0, unknown_method=1)
    task = _task_row(registry_path, "r1")
    outcome = json.loads(task["outcome_json"])
    assert task["status"] == "failed"
    assert outcome["message"] == "unknown_method_during_recovery"
    assert outcome["data"]["method"] == "SomeFutureMethod"
    assert _workspace_row(registry_path, "untouched") is not None


def test_daemon_startup_invokes_recovery_and_then_serves():
    tmp = Path(tempfile.mkdtemp(prefix="wsd-recovery-", dir="/tmp"))
    sock = tmp / "s"
    registry_path = tmp / "r.db"
    Registry(db_path=registry_path).close()
    _insert_task(
        registry_path,
        request_id="req-recovery",
        method="CreateDesk",
        spec={"project": "proj", "name": "ghost"},
    )

    env = os.environ.copy()
    env["DRYDOCK_WSD_DRY_RUN"] = "1"
    env["HOME"] = str(tmp)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "drydock.wsd",
            "--socket",
            str(sock),
            "--registry",
            str(registry_path),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_socket(sock, proc, timeout=5.0)
        response = _call_rpc(
            sock,
            {"jsonrpc": "2.0", "method": "wsd.health", "id": "health-1"},
        )
        assert response["result"]["ok"] is True

        task = _task_row(registry_path, "req-recovery")
        outcome = json.loads(task["outcome_json"])
        assert task["status"] == "failed"
        assert outcome["message"] == "crashed_during_create"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)
