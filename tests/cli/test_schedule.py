"""Tests for ws schedule CLI — sync idempotency, remove cleanup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.schedule import schedule
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _make_obj(registry):
    return {"registry": registry, "output": Output(force_json=True), "dry_run": False}


def _register_desk(registry, name="testdesk", worktree="/tmp/ws", subdir=""):
    ws = Workspace(name=name, project="proj", repo_path="/tmp/repo", worktree_path=worktree, workspace_subdir=subdir)
    registry.create_workspace(ws)
    return ws


def _write_schedule(base: Path, subdir: str = ""):
    root = base / subdir if subdir else base
    deploy = root / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / "schedule.yaml").write_text(
        "jobs:\n"
        "  daily-crawl:\n"
        "    cron: '0 13 * * *'\n"
        "    command: bash deploy/run-daily.sh\n"
        "    log: /var/log/drydock/crawl.log\n"
    )


def test_schedule_sync_launchd_idempotent(tmp_path, registry):
    """Sync writes plists; second sync rewrites identically (idempotent)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_schedule(worktree)
    _register_desk(registry, worktree=str(worktree))

    launch_dir = tmp_path / "LaunchAgents"
    with patch("drydock.core.schedule._launchd_dir", return_value=launch_dir):
        with patch("drydock.core.schedule.detect_backend", return_value="launchd"):
            runner = CliRunner()
            obj = _make_obj(registry)
            r1 = runner.invoke(schedule, ["sync", "testdesk"], obj=obj)
            assert r1.exit_code == 0, r1.output

            plist_files = list(launch_dir.glob("*.plist"))
            assert len(plist_files) == 1
            content_first = plist_files[0].read_bytes()

            # Second sync — idempotent
            r2 = runner.invoke(schedule, ["sync", "testdesk"], obj=obj)
            assert r2.exit_code == 0, r2.output
            assert plist_files[0].read_bytes() == content_first


def test_schedule_sync_unknown_desk(registry):
    """Sync with unknown desk exits with error."""
    runner = CliRunner()
    result = runner.invoke(schedule, ["sync", "nope"], obj=_make_obj(registry))
    assert result.exit_code == 1


def test_schedule_remove_launchd(tmp_path, registry):
    """Remove deletes installed plists."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_schedule(worktree)
    _register_desk(registry, worktree=str(worktree))

    launch_dir = tmp_path / "LaunchAgents"
    with patch("drydock.core.schedule._launchd_dir", return_value=launch_dir):
        with patch("drydock.core.schedule.detect_backend", return_value="launchd"):
            runner = CliRunner()
            obj = _make_obj(registry)
            # Install first
            runner.invoke(schedule, ["sync", "testdesk"], obj=obj)
            assert len(list(launch_dir.glob("*.plist"))) == 1

            # Remove
            result = runner.invoke(schedule, ["remove", "testdesk"], obj=obj)
            assert result.exit_code == 0, result.output
            assert len(list(launch_dir.glob("*.plist"))) == 0


def test_schedule_sync_with_workspace_subdir(tmp_path, registry):
    """Sync resolves schedule.yaml under workspace_subdir."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_schedule(worktree, subdir="myapp")
    _register_desk(registry, worktree=str(worktree), subdir="myapp")

    launch_dir = tmp_path / "LaunchAgents"
    with patch("drydock.core.schedule._launchd_dir", return_value=launch_dir):
        with patch("drydock.core.schedule.detect_backend", return_value="launchd"):
            runner = CliRunner()
            result = runner.invoke(schedule, ["sync", "testdesk"], obj=_make_obj(registry))
            assert result.exit_code == 0, result.output
            assert len(list(launch_dir.glob("*.plist"))) == 1


def test_schedule_sync_stale_plist_removed(tmp_path, registry):
    """Sync removes plists for jobs no longer in schedule.yaml."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_schedule(worktree)
    _register_desk(registry, worktree=str(worktree))

    launch_dir = tmp_path / "LaunchAgents"
    with patch("drydock.core.schedule._launchd_dir", return_value=launch_dir):
        with patch("drydock.core.schedule.detect_backend", return_value="launchd"):
            runner = CliRunner()
            obj = _make_obj(registry)
            runner.invoke(schedule, ["sync", "testdesk"], obj=obj)

            # Plant a stale plist
            stale = launch_dir / "com.drydock.testdesk.old-job.plist"
            stale.write_text("<fake/>")
            assert stale.exists()

            # Re-sync should remove the stale one
            runner.invoke(schedule, ["sync", "testdesk"], obj=obj)
            assert not stale.exists()
            # But the valid one still exists
            assert len(list(launch_dir.glob("*.plist"))) == 1
