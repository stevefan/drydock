"""Tests for git worktree creation."""

import subprocess
from pathlib import Path

import pytest

from drydock.core.errors import WsError
from drydock.core.worktree import create_worktree
from drydock.core.workspace import Workspace


def _git(repo: Path, *args: str):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit."""
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
def repo(tmp_path):
    return _init_repo(tmp_path)


@pytest.fixture
def ws(repo):
    return Workspace(
        name="my-feature",
        project="app",
        repo_path=str(repo),
        branch="ws/my-feature",
        base_ref="HEAD",
    )


class TestCreateWorktree:
    def test_happy_path(self, ws, repo, tmp_path):
        wt_base = tmp_path / "worktrees"
        wt_path = create_worktree(ws, base_dir=wt_base)

        assert wt_path == wt_base / ws.id
        assert wt_path.is_dir()
        assert (wt_path / "README.md").exists()

    def test_creates_branch(self, ws, repo, tmp_path):
        wt_base = tmp_path / "worktrees"
        create_worktree(ws, base_dir=wt_base)

        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{ws.branch}"],
            cwd=repo, capture_output=True,
        )
        assert result.returncode == 0

    def test_repo_missing_raises(self, tmp_path):
        ws = Workspace(
            name="bad", project="x",
            repo_path=str(tmp_path / "nonexistent"),
            branch="ws/bad",
        )
        with pytest.raises(WsError, match="Not a git repository"):
            create_worktree(ws, base_dir=tmp_path / "wt")

    def test_repo_missing_error_has_fix(self, tmp_path):
        ws = Workspace(
            name="bad", project="x",
            repo_path=str(tmp_path / "nonexistent"),
            branch="ws/bad",
        )
        with pytest.raises(WsError) as exc_info:
            create_worktree(ws, base_dir=tmp_path / "wt")
        assert exc_info.value.fix is not None
        assert "--repo-path" in exc_info.value.fix

    def test_branch_already_exists(self, ws, repo, tmp_path):
        _git(repo, "branch", ws.branch)

        wt_base = tmp_path / "worktrees"
        wt_path = create_worktree(ws, base_dir=wt_base)

        assert wt_path.is_dir()
        assert (wt_path / "README.md").exists()

    def test_creates_base_dir_if_missing(self, ws, tmp_path):
        wt_base = tmp_path / "a" / "b" / "c"
        wt_path = create_worktree(ws, base_dir=wt_base)
        assert wt_path.exists()

    def test_invalid_base_ref_raises(self, repo, tmp_path):
        ws = Workspace(
            name="bad-ref", project="app",
            repo_path=str(repo),
            branch="ws/bad-ref",
            base_ref="nonexistent-ref-abc123",
        )
        with pytest.raises(WsError, match="git failed"):
            create_worktree(ws, base_dir=tmp_path / "wt")

    def test_worktree_path_uses_ws_id(self, ws, tmp_path):
        wt_base = tmp_path / "worktrees"
        wt_path = create_worktree(ws, base_dir=wt_base)
        assert wt_path.name == ws.id
