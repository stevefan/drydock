"""Tests for project YAML SHA-256 computation + drift classification.

Phase 0 of project-dock-ontology.md: pin a hash at create/reload, compare
on audit. The contracts pinned here are:

- compute_project_yaml_sha returns hex SHA-256 of YAML bytes
- missing/unreadable file returns empty string (NOT raises)
- yaml_drift_status's 5 outcomes are stable contract strings used by audit
- a different YAML produces a different hash (obvious but pin)
- the same YAML produces the same hash across calls (idempotent)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from drydock.core.project_yaml_sha import (
    compute_project_yaml_sha,
    project_yaml_path,
    yaml_drift_status,
)


class TestComputeProjectYamlSha:
    def test_returns_sha256_hex_of_yaml_bytes(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        content = b"repo_path: /tmp/x\ncapabilities: [request_secret_leases]\n"
        (projects / "demo.yaml").write_bytes(content)

        result = compute_project_yaml_sha("demo", base_dir=projects)

        assert result == hashlib.sha256(content).hexdigest()
        assert len(result) == 64  # SHA-256 hex is 64 chars

    def test_missing_file_returns_empty_string(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        # No demo.yaml exists.

        result = compute_project_yaml_sha("demo", base_dir=projects)

        assert result == ""

    def test_idempotent_for_same_content(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        (projects / "demo.yaml").write_bytes(b"key: value\n")

        a = compute_project_yaml_sha("demo", base_dir=projects)
        b = compute_project_yaml_sha("demo", base_dir=projects)

        assert a == b
        assert a != ""

    def test_different_content_produces_different_hash(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        path = projects / "demo.yaml"

        path.write_bytes(b"key: value1\n")
        a = compute_project_yaml_sha("demo", base_dir=projects)

        path.write_bytes(b"key: value2\n")
        b = compute_project_yaml_sha("demo", base_dir=projects)

        assert a != b

    def test_whitespace_change_changes_hash(self, tmp_path):
        # The hash is over RAW BYTES, so semantically-equivalent YAML
        # with different whitespace produces a different hash. This is
        # intentional — operationally the principal probably wants to
        # know the file was edited, even if semantics didn't change.
        projects = tmp_path / "projects"
        projects.mkdir()
        path = projects / "demo.yaml"

        path.write_bytes(b"key: value\n")
        a = compute_project_yaml_sha("demo", base_dir=projects)

        path.write_bytes(b"key:  value\n")  # extra space
        b = compute_project_yaml_sha("demo", base_dir=projects)

        assert a != b

    def test_project_yaml_path_resolution(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        path = project_yaml_path("auction-crawl", base_dir=projects)
        assert path == projects / "auction-crawl.yaml"


class TestYamlDriftStatus:
    """The 5 outcomes are the stable contract that ws host audit consumes."""

    def test_in_sync(self):
        assert yaml_drift_status("abc123", "abc123") == "in_sync"

    def test_drifted(self):
        assert yaml_drift_status("abc123", "def456") == "drifted"

    def test_yaml_missing_when_pinned_but_current_empty(self):
        # The YAML was pinned at some point, but the file is gone now.
        assert yaml_drift_status("abc123", "") == "yaml_missing"

    def test_unpinned_when_pinned_empty(self):
        # Legacy row that predates the pinned_yaml_sha256 column —
        # surface as unpinned, not as drift.
        assert yaml_drift_status("", "abc123") == "unpinned"

    def test_unknown_when_both_empty(self):
        # Degenerate case: no pin AND no current.
        assert yaml_drift_status("", "") == "unknown"

    def test_outcomes_are_stable_contract(self):
        # `ws host audit` formats based on these strings. Renaming any
        # of them would silently break the audit's drift display.
        valid_outcomes = {"in_sync", "drifted", "yaml_missing", "unpinned", "unknown"}
        for pinned, current in [
            ("a", "a"), ("a", "b"), ("a", ""), ("", "a"), ("", ""),
        ]:
            assert yaml_drift_status(pinned, current) in valid_outcomes
