"""Crash recovery tests for `drydock daemon` startup reconciliation."""

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
from datetime import datetime, timezone
from pathlib import Path

from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.daemon.recovery import RecoveryReport, recover_in_progress


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


def _drydock_row(db_path: Path, name: str) -> sqlite3.Row | None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM drydocks WHERE name = ?",
        (name,),
    ).fetchone()
    conn.close()
    return row


def _make_drydock(
    registry_path: Path,
    *,
    name: str,
    project: str = "proj",
    state: str = "defined",
    container_id: str = "",
    worktree_path: str = "",
) -> Drydock:
    registry = Registry(db_path=registry_path)
    try:
        ws = registry.create_drydock(
            Drydock(
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
                f"daemon exited before socket appeared (rc={proc.returncode}); "
                f"stderr: {stderr.decode('utf-8', errors='replace')}"
            )
        time.sleep(0.02)
    raise TimeoutError(f"daemon socket {path} did not appear within {timeout}s")


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


def test_recovery_completes_in_progress_with_running_drydock(tmp_path):
    registry_path = tmp_path / "registry.db"
    ws = _make_drydock(
        registry_path,
        name="desk-ok",
        state="running",
        container_id="ctr-abc",
        worktree_path=str(tmp_path / "worktrees" / "dock_desk_ok"),
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
    assert outcome["drydock_id"] == ws.id
    assert outcome["container_id"] == "ctr-abc"
    assert outcome["state"] == "running"


def test_recovery_rolls_back_in_progress_without_drydock(tmp_path):
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


def test_recovery_rolls_back_partial_drydock(tmp_path):
    registry_path = tmp_path / "registry.db"
    worktree_path = tmp_path / "worktrees" / "dock_partial"
    worktree_path.mkdir(parents=True)
    (worktree_path / "marker.txt").write_text("partial")

    _make_drydock(
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
    assert _drydock_row(registry_path, "partial") is None
    assert not worktree_path.exists()
    task = _task_row(registry_path, "r1")
    assert task["status"] == "failed"


def test_recovery_rolls_back_partial_spawn_child(tmp_path):
    registry_path = tmp_path / "registry.db"
    worktree_path = tmp_path / "worktrees" / "dock_child_partial"
    worktree_path.mkdir(parents=True)
    (worktree_path / "marker.txt").write_text("partial")

    registry = Registry(db_path=registry_path)
    try:
        registry.create_drydock(
            Drydock(
                name="child-partial",
                project="proj",
                repo_path="/repos/proj",
                branch="ws/child-partial",
                state="provisioning",
                worktree_path=str(worktree_path),
            )
        )
        registry.update_drydock("child-partial", parent_drydock_id="dock_parent")
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
    assert _drydock_row(registry_path, "child-partial") is None
    assert not worktree_path.exists()
    task = _task_row(registry_path, "r-spawn")
    outcome = json.loads(task["outcome_json"])
    assert task["status"] == "failed"
    assert outcome["message"] == "crashed_during_create"


def test_recovery_is_idempotent(tmp_path):
    registry_path = tmp_path / "registry.db"
    _make_drydock(
        registry_path,
        name="desk-idem",
        state="running",
        container_id="ctr-abc",
        worktree_path=str(tmp_path / "worktrees" / "dock_desk_idem"),
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
    _make_drydock(
        registry_path,
        name="untouched",
        state="running",
        container_id="ctr-keep",
        worktree_path=str(tmp_path / "worktrees" / "dock_untouched"),
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
    assert _drydock_row(registry_path, "untouched") is not None


def test_recovery_completes_in_progress_destroy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    registry_path = tmp_path / "registry.db"
    worktree_path = tmp_path / "worktrees" / "dock_destroy_me"
    worktree_path.mkdir(parents=True)
    overlay_path = tmp_path / ".drydock" / "overlays" / "dock_destroy_me.devcontainer.json"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("{}")
    secrets_dir = tmp_path / ".drydock" / "secrets" / "dock_destroy_me"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "drydock-token").write_text("secret")

    registry = Registry(db_path=registry_path)
    try:
        registry.create_drydock(
            Drydock(
                name="destroy-me",
                project="proj",
                repo_path="/repos/proj",
                branch="ws/destroy-me",
                state="running",
                worktree_path=str(worktree_path),
                config={"overlay_path": str(overlay_path)},
            )
        )
        registry.insert_token(
            drydock_id="dock_destroy_me",
            token_sha256="deadbeef",
            issued_at=datetime.now(timezone.utc),
        )
    finally:
        registry.close()

    _insert_task(
        registry_path,
        request_id="r-destroy",
        method="DestroyDesk",
        spec={"name": "destroy-me"},
    )

    report = recover_in_progress(registry_path)

    assert report == RecoveryReport(completed=1, rolled_back=0, unknown_method=0)
    assert _drydock_row(registry_path, "destroy-me") is None
    assert not worktree_path.exists()
    assert not overlay_path.exists()
    assert not secrets_dir.exists()
    task = _task_row(registry_path, "r-destroy")
    outcome = json.loads(task["outcome_json"])
    assert task["status"] == "completed"
    assert outcome["destroyed"] is True
    assert outcome["drydock_id"] == "dock_destroy_me"
    assert outcome["recovered"] is True


def test_daemon_startup_invokes_recovery_and_then_serves():
    tmp = Path(tempfile.mkdtemp(prefix="daemon-recovery-", dir="/tmp"))
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
            "drydock.daemon",
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
            {"jsonrpc": "2.0", "method": "daemon.health", "id": "health-1"},
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
