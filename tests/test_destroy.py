"""Tests for ws destroy cleanup logic."""

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from drydock.cli.destroy import destroy
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _git(repo: Path, *args: str):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def env(tmp_path):
    repo = _init_repo(tmp_path)
    db_path = tmp_path / "registry.db"
    registry = Registry(db_path=db_path)

    wt_path = tmp_path / "worktrees" / "wt"
    wt_path.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", str(wt_path), "-b", "ws/test", "HEAD")

    overlay_dir = tmp_path / "overlays"
    overlay_dir.mkdir()
    overlay_file = overlay_dir / "test.json"
    overlay_file.write_text("{}")

    ws = Workspace(
        name="test-ws",
        project="proj",
        repo_path=str(repo),
        worktree_path=str(wt_path),
        branch="ws/test",
        config={"overlay_path": str(overlay_file)},
    )
    registry.create_workspace(ws)

    out = Output(force_json=True)
    return {
        "registry": registry,
        "ws": ws,
        "repo": repo,
        "wt_path": wt_path,
        "overlay_file": overlay_file,
        "out": out,
    }


def _invoke_destroy(env, name="test-ws"):
    runner = CliRunner()
    return runner.invoke(
        destroy,
        ["--force", name],
        obj={"registry": env["registry"], "output": env["out"], "dry_run": False},
    )


class TestDestroyWorktree:
    def test_removes_worktree(self, env):
        assert env["wt_path"].exists()
        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert not env["wt_path"].exists()

    def test_removes_overlay(self, env):
        assert env["overlay_file"].exists()
        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert not env["overlay_file"].exists()

    def test_idempotent_when_already_gone(self, env, tmp_path):
        import shutil
        shutil.rmtree(env["wt_path"])
        env["overlay_file"].unlink()

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert env["registry"].get_workspace("test-ws") is None

    def test_registry_row_removed_on_cleanup_failure(self, env, monkeypatch):
        def fail_remove(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("drydock.cli.destroy.remove_worktree", fail_remove)
        monkeypatch.setattr("drydock.cli.destroy.remove_overlay", fail_remove)

        # Make paths exist so cleanup is attempted
        assert env["wt_path"].exists()
        assert env["overlay_file"].exists()

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert env["registry"].get_workspace("test-ws") is None
