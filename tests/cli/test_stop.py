"""Tests for ws stop — DevcontainerCLI integration."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.stop import stop
from drydock.core import WsError
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _make_ws(state="running"):
    return Workspace(
        name="test-ws",
        project="proj",
        repo_path="/tmp/repo",
        worktree_path="/tmp/wt",
        branch="ws/test",
        state=state,
        container_id="abc123",
    )


def _invoke(registry, dry_run=False):
    runner = CliRunner()
    out = Output(force_json=True)
    return runner.invoke(
        stop,
        ["test-ws"],
        obj={"registry": registry, "output": out, "dry_run": dry_run},
    )


@patch("drydock.cli.stop.DevcontainerCLI")
def test_stop_calls_devcontainer_stop(MockCLI):
    ws = _make_ws()
    registry = MagicMock()
    registry.get_workspace.return_value = ws
    registry.update_state.return_value = ws

    result = _invoke(registry)

    assert result.exit_code == 0
    mock_devc = MockCLI.return_value
    mock_devc.stop.assert_called_once_with(container_id="abc123")
    registry.update_state.assert_called_once_with("test-ws", "suspended")


@patch("drydock.cli.stop.DevcontainerCLI")
def test_stop_dry_run_skips_cli_call(MockCLI):
    ws = _make_ws()
    registry = MagicMock()
    registry.get_workspace.return_value = ws

    result = _invoke(registry, dry_run=True)

    assert result.exit_code == 0
    MockCLI.assert_not_called()
    registry.update_state.assert_not_called()


@patch("drydock.cli.stop.DevcontainerCLI")
def test_stop_propagates_wserror(MockCLI):
    ws = _make_ws()
    registry = MagicMock()
    registry.get_workspace.return_value = ws

    mock_devc = MockCLI.return_value
    mock_devc.stop.side_effect = WsError("devcontainer down failed: timeout")

    result = _invoke(registry)

    assert result.exit_code != 0
    registry.update_state.assert_not_called()


@patch("drydock.cli.stop.DevcontainerCLI")
def test_tailnet_logout_called_before_stop(MockCLI):
    ws = _make_ws()
    registry = MagicMock()
    registry.get_workspace.return_value = ws
    registry.update_state.return_value = ws

    call_order = []
    mock_devc = MockCLI.return_value
    mock_devc.tailnet_logout.side_effect = lambda **kw: call_order.append("logout")
    mock_devc.stop.side_effect = lambda **kw: call_order.append("stop")

    result = _invoke(registry)

    assert result.exit_code == 0
    assert call_order == ["logout", "stop"]
    mock_devc.tailnet_logout.assert_called_once_with(container_id="abc123")
