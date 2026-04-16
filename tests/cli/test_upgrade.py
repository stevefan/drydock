"""Tests for ws upgrade — Dockerfile bump + destroy/recreate."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from drydock.cli.upgrade import upgrade
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _git(repo: Path, *args: str):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _init_project_repo(tmp_path: Path, dockerfile_tag: str) -> Path:
    repo = tmp_path / "proj-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    devc = repo / ".devcontainer"
    devc.mkdir()
    (devc / "Dockerfile").write_text(
        f"FROM ghcr.io/stevefan/drydock-base:{dockerfile_tag}\n"
        "ARG TZ\n"
        "USER node\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def env(tmp_path):
    repo = _init_project_repo(tmp_path, "v1.0.5")
    db_path = tmp_path / "registry.db"
    registry = Registry(db_path=db_path)
    ws = Workspace(
        name="myws",
        project="myproj",
        repo_path=str(repo),
        branch="ws/myws",
        base_ref="HEAD",
    )
    registry.create_workspace(ws)
    out = Output(force_json=True)
    return {"registry": registry, "repo": repo, "out": out, "ws": ws}


def _invoke(env, *args):
    runner = CliRunner()
    return runner.invoke(
        upgrade,
        list(args),
        obj={"registry": env["registry"], "output": env["out"], "dry_run": False},
    )


class TestUpgrade:
    def test_workspace_not_found(self, env):
        result = _invoke(env, "nonexistent", "--to", "v1.0.7")
        assert result.exit_code == 1
        assert "not found" in (result.output + (result.stderr_bytes or b"").decode()).lower()

    def test_to_required(self, env):
        result = _invoke(env, "myws")
        assert result.exit_code == 1
        combined = result.output + (result.stderr_bytes or b"").decode()
        assert "--to" in combined

    def test_dockerfile_missing(self, env, tmp_path):
        env["registry"].update_workspace("myws", repo_path=str(tmp_path / "nope"))
        result = _invoke(env, "myws", "--to", "v1.0.7")
        assert result.exit_code == 1
        combined = result.output + (result.stderr_bytes or b"").decode()
        assert "Dockerfile not found" in combined

    def test_no_drydock_base_line(self, env):
        df = env["repo"] / ".devcontainer" / "Dockerfile"
        df.write_text("FROM debian:bookworm\nUSER node\n")
        _git(env["repo"], "add", ".")
        _git(env["repo"], "commit", "-m", "switch base")

        result = _invoke(env, "myws", "--to", "v1.0.7")
        assert result.exit_code == 1
        combined = result.output + (result.stderr_bytes or b"").decode()
        assert "not on drydock-base" in combined

    def test_already_on_target_tag_is_noop(self, env):
        df = env["repo"] / ".devcontainer" / "Dockerfile"
        df.write_text("FROM ghcr.io/stevefan/drydock-base:v1.0.7\nUSER node\n")
        _git(env["repo"], "add", ".")
        _git(env["repo"], "commit", "-m", "already on target")

        # Even with no daemon mock, this must not invoke call_daemon
        with patch("drydock.cli.upgrade.call_daemon") as mock_call:
            result = _invoke(env, "myws", "--to", "v1.0.7")
            assert result.exit_code == 0
            mock_call.assert_not_called()
        assert '"changed": false' in result.output

    @patch("drydock.cli.upgrade.call_daemon")
    def test_happy_path_bumps_commits_destroys_creates(self, mock_call, env):
        # First call (DestroyDesk) -> {}, second (CreateDesk) -> result dict
        mock_call.side_effect = [
            {},
            {"name": "myws", "container_id": "newcid", "desk_id": "d1",
             "project": "myproj", "branch": "ws/myws", "state": "running"},
        ]

        result = _invoke(env, "myws", "--to", "v1.0.7")
        assert result.exit_code == 0, result.output

        # Dockerfile rewritten
        df_text = (env["repo"] / ".devcontainer" / "Dockerfile").read_text()
        assert "drydock-base:v1.0.7" in df_text
        assert "drydock-base:v1.0.5" not in df_text

        # New commit landed
        log = subprocess.run(
            ["git", "-C", str(env["repo"]), "log", "--format=%s", "-1"],
            capture_output=True, text=True, check=True,
        )
        assert "drydock-base: bump to v1.0.7" in log.stdout

        # Both daemon calls in order: DestroyDesk then CreateDesk
        assert mock_call.call_count == 2
        first_args = mock_call.call_args_list[0]
        second_args = mock_call.call_args_list[1]
        assert first_args.args[0] == "DestroyDesk"
        assert second_args.args[0] == "CreateDesk"
        assert second_args.args[1]["name"] == "myws"
        assert second_args.args[1]["project"] == "myproj"
