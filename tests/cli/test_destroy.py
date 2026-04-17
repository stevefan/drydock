"""Tests for ws destroy cleanup logic."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from drydock.cli.destroy import destroy
from drydock.core import WsError
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
    subprocess.run(
        ["git", "clone", str(repo), str(wt_path)],
        capture_output=True, text=True, check=True,
    )

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
        container_id="cid_abc",
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


# All tests must bypass the daemon routing (tests run without wsd).
_DAEMON_UNAVAILABLE = patch(
    "drydock.cli.destroy.call_daemon",
    side_effect=__import__("drydock.cli._wsd_client", fromlist=["DaemonUnavailable"]).DaemonUnavailable("no_socket"),
)

_NO_TAILNET_CLEANUP = patch(
    "drydock.cli.destroy._delete_tailnet_device_best_effort",
)


@_DAEMON_UNAVAILABLE
@_NO_TAILNET_CLEANUP
@patch("drydock.cli.destroy.DevcontainerCLI")
class TestDestroyWorktree:
    def test_removes_worktree(self, _MockCLI, _mock_tn, _mock_daemon, env):
        assert env["wt_path"].exists()
        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert not env["wt_path"].exists()

    def test_removes_overlay(self, _MockCLI, _mock_tn, _mock_daemon, env):
        assert env["overlay_file"].exists()
        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert not env["overlay_file"].exists()

    def test_idempotent_when_already_gone(self, _MockCLI, _mock_tn, _mock_daemon, env, tmp_path):
        import shutil
        shutil.rmtree(env["wt_path"])
        env["overlay_file"].unlink()

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert env["registry"].get_workspace("test-ws") is None

    def test_registry_row_removed_on_cleanup_failure(self, _MockCLI, _mock_tn, _mock_daemon, env, monkeypatch):
        def fail_remove(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("drydock.cli.destroy.remove_checkout", fail_remove)
        monkeypatch.setattr("drydock.cli.destroy.remove_overlay", fail_remove)

        # Make paths exist so cleanup is attempted
        assert env["wt_path"].exists()
        assert env["overlay_file"].exists()

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert env["registry"].get_workspace("test-ws") is None


@_DAEMON_UNAVAILABLE
@_NO_TAILNET_CLEANUP
class TestDestroyContainerStop:
    @patch("drydock.cli.destroy.DevcontainerCLI")
    def test_calls_stop_when_running(self, MockCLI, _mock_tn, _mock_daemon, env):
        env["registry"].update_state("test-ws", "running")
        result = _invoke_destroy(env)
        assert result.exit_code == 0
        MockCLI.return_value.stop.assert_called_once_with(container_id="cid_abc")
        MockCLI.return_value.remove.assert_called_once_with(container_id="cid_abc")

    @patch("drydock.cli.destroy.DevcontainerCLI")
    def test_skips_stop_when_not_running(self, MockCLI, _mock_tn, _mock_daemon, env):
        # state is "defined" (default) — stop/logout skipped, but remove is called
        result = _invoke_destroy(env)
        assert result.exit_code == 0
        MockCLI.return_value.stop.assert_not_called()
        MockCLI.return_value.remove.assert_called_once_with(container_id="cid_abc")

    @patch("drydock.cli.destroy.DevcontainerCLI")
    def test_continues_cleanup_when_stop_raises(self, MockCLI, _mock_tn, _mock_daemon, env):
        env["registry"].update_state("test-ws", "running")
        MockCLI.return_value.stop.side_effect = WsError("down failed")

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert env["registry"].get_workspace("test-ws") is None
        assert not env["wt_path"].exists()

    @patch("drydock.cli.destroy.DevcontainerCLI")
    def test_tailnet_logout_called_before_stop(self, MockCLI, _mock_tn, _mock_daemon, env):
        env["registry"].update_state("test-ws", "running")
        call_order = []
        mock_devc = MockCLI.return_value
        mock_devc.tailnet_logout.side_effect = lambda **kw: call_order.append("logout")
        mock_devc.stop.side_effect = lambda **kw: call_order.append("stop")

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert call_order == ["logout", "stop"]
        mock_devc.tailnet_logout.assert_called_once_with(container_id="cid_abc")

    @patch("drydock.cli.destroy.DevcontainerCLI")
    def test_stop_called_even_if_logout_raises(self, MockCLI, _mock_tn, _mock_daemon, env):
        env["registry"].update_state("test-ws", "running")
        mock_devc = MockCLI.return_value
        mock_devc.tailnet_logout.side_effect = RuntimeError("boom")

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        mock_devc.stop.assert_called_once_with(container_id="cid_abc")


@_DAEMON_UNAVAILABLE
@patch("drydock.cli.destroy.DevcontainerCLI")
class TestDestroyTailnetDeviceDelete:
    # Contract: when admin credentials are configured, destroy calls the
    # tailnet API to remove the device record. This is the v1.x mechanism
    # for reclaiming ghost hostnames (e.g. `auction-crawl`) — the whole
    # reason this backport exists.
    @patch("drydock.cli.destroy.tailnet_api")
    def test_deletes_device_when_credentials_present(self, mock_tn, _MockCLI, _mock_daemon, env):
        mock_tn.load_admin_credentials.return_value = ("tok", "example.ts.net")
        mock_tn.find_devices.return_value = [{"id": "dev-1", "hostname": "ws_test_ws"}]
        mock_tn.find_device_by_hostname.return_value = {"id": "dev-1", "hostname": "ws_test_ws"}

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        mock_tn.delete_tailnet_device.assert_called_once_with("dev-1", "tok")

    # Contract: absence of credentials is a silent no-op, not an error.
    # This preserves v1 behavior for users who haven't opted into tailnet
    # identity cleanup (design §4, §7 "no token configured" row).
    @patch("drydock.cli.destroy.tailnet_api")
    def test_skips_delete_when_credentials_absent(self, mock_tn, _MockCLI, _mock_daemon, env):
        mock_tn.load_admin_credentials.return_value = None

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        mock_tn.delete_tailnet_device.assert_not_called()
        mock_tn.find_devices.assert_not_called()

    # Contract: tailnet API failure must NOT roll back destroy. The
    # workspace is already gone from drydock's registry; the orphan record
    # is recoverable via `ws tailnet prune`. Audit captures the failure.
    @patch("drydock.cli.destroy.tailnet_api")
    def test_tailnet_failure_does_not_block_destroy(self, mock_tn, _MockCLI, _mock_daemon, env):
        from drydock.core import WsError
        mock_tn.load_admin_credentials.return_value = ("tok", "example.ts.net")
        mock_tn.find_devices.side_effect = WsError("API down", fix="retry")

        result = _invoke_destroy(env)
        assert result.exit_code == 0
        assert env["registry"].get_workspace("test-ws") is None
