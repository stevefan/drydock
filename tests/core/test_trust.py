"""Tests for workspace-trust seeding."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from drydock.core.trust import (
    _already_trusted,
    _read_workspace_folder_from_overlay,
    seed_workspace_trust,
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class TestAlreadyTrusted:
    def test_dict_form_match(self):
        assert _already_trusted({"trustedWorkspaces": {"/workspace": {"trusted": True}}}, "/workspace")

    def test_list_form_dict_entry(self):
        assert _already_trusted({"trustedWorkspaces": [{"path": "/workspace"}]}, "/workspace")

    def test_no_match(self):
        assert not _already_trusted({"trustedWorkspaces": {"/other": {}}}, "/workspace")

    def test_no_trusted_field(self):
        assert not _already_trusted({}, "/workspace")


class TestReadWorkspaceFolder:
    def test_returns_default_when_overlay_missing(self):
        assert _read_workspace_folder_from_overlay(None) == "/workspace"
        assert _read_workspace_folder_from_overlay("") == "/workspace"

    def test_returns_default_for_unreadable(self, tmp_path):
        assert _read_workspace_folder_from_overlay(tmp_path / "nope.json") == "/workspace"

    def test_returns_overlay_value(self, tmp_path):
        overlay = tmp_path / "overlay.json"
        overlay.write_text(json.dumps({"workspaceFolder": "/workspaces/asi"}))
        assert _read_workspace_folder_from_overlay(str(overlay)) == "/workspaces/asi"


class TestSeedWorkspaceTrust:
    @patch("drydock.core.trust.subprocess.run")
    def test_idempotent_when_already_trusted(self, mock_run):
        # Existing claude.json already has the trust entry.
        mock_run.return_value = _completed(
            stdout=json.dumps({"trustedWorkspaces": {"/workspace": {"trusted": True}}}),
        )
        assert seed_workspace_trust("cid", "/workspace") is True
        # Only the read happened — no mkdir, no write
        assert mock_run.call_count == 1

    @patch("drydock.core.trust.subprocess.run")
    def test_writes_when_missing_entry_preserves_other_fields(self, mock_run):
        existing = {
            "userID": "abc",
            "trustedWorkspaces": {"/other": {"trusted": True}},
        }
        mock_run.side_effect = [
            _completed(stdout=json.dumps(existing)),  # cat
            _completed(),  # mkdir
            _completed(),  # write
        ]
        assert seed_workspace_trust("cid", "/workspace") is True

        # Capture the JSON payload sent to the write call
        write_call = mock_run.call_args_list[2]
        payload = json.loads(write_call.kwargs["input"])
        assert payload["userID"] == "abc"
        assert "/other" in payload["trustedWorkspaces"]
        assert payload["trustedWorkspaces"]["/workspace"] == {"trusted": True}

    @patch("drydock.core.trust.subprocess.run")
    def test_starts_from_empty_when_file_missing(self, mock_run):
        mock_run.side_effect = [
            _completed(returncode=1, stderr="No such file"),  # cat
            _completed(),  # mkdir
            _completed(),  # write
        ]
        assert seed_workspace_trust("cid", "/workspace") is True
        write_call = mock_run.call_args_list[2]
        payload = json.loads(write_call.kwargs["input"])
        assert payload == {"trustedWorkspaces": {"/workspace": {"trusted": True}}}

    @patch("drydock.core.trust.subprocess.run")
    def test_returns_false_on_write_failure(self, mock_run, caplog):
        mock_run.side_effect = [
            _completed(returncode=1),  # cat fails -> empty
            _completed(),  # mkdir ok
            _completed(returncode=1, stderr="docker write boom"),  # write fails
        ]
        assert seed_workspace_trust("cid", "/workspace") is False

    def test_returns_false_for_empty_args(self):
        assert seed_workspace_trust("", "/workspace") is False
        assert seed_workspace_trust("cid", "") is False
