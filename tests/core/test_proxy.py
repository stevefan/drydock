"""Tests for `drydock.core.proxy` — smokescreen ACL generation.

Pin the contract:
- Glob partition: "*.X" → allowed_domains, bare → allowed_hosts.
- Empty / whitespace entries skipped.
- Output is valid YAML readable by smokescreen's v1 schema.
- File write is atomic (tempfile + rename pattern).
- Round-trip: write → read YAML → check structure.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from drydock.core.proxy import (
    generate_smokescreen_acl,
    split_globs,
    write_smokescreen_acl,
)


class TestSplitGlobs:
    def test_wildcard_to_domain(self):
        hosts, domains = split_globs(["*.github.com"])
        assert hosts == []
        assert domains == ["github.com"]

    def test_bare_to_host(self):
        hosts, domains = split_globs(["api.anthropic.com"])
        assert hosts == ["api.anthropic.com"]
        assert domains == []

    def test_mixed(self):
        hosts, domains = split_globs([
            "api.anthropic.com",
            "*.github.com",
            "huggingface.co",
            "*.s3.us-west-2.amazonaws.com",
        ])
        # Sorted within each list.
        assert hosts == ["api.anthropic.com", "huggingface.co"]
        assert domains == ["github.com", "s3.us-west-2.amazonaws.com"]

    def test_dedupes(self):
        hosts, domains = split_globs(["github.com", "github.com", "*.github.com"])
        assert hosts == ["github.com"]
        assert domains == ["github.com"]

    def test_empty_and_whitespace_skipped(self):
        hosts, domains = split_globs(["", "  ", "\t", "github.com"])
        assert hosts == ["github.com"]
        assert domains == []

    def test_bare_wildcard_skipped(self):
        # "*" alone is not currently representable in smokescreen ACL —
        # we drop it rather than silently allowing everything.
        hosts, domains = split_globs(["*", "github.com"])
        assert hosts == ["github.com"]
        assert domains == []

    def test_empty_input(self):
        hosts, domains = split_globs([])
        assert hosts == []
        assert domains == []


class TestGenerateSmokescreenAcl:
    def test_basic_shape(self):
        acl = generate_smokescreen_acl("dock_collab", ["*.github.com", "api.anthropic.com"])
        assert acl["version"] == "v1"
        assert acl["services"] == []
        assert acl["default"]["action"] == "enforce"
        assert acl["default"]["project"] == "drydock-dock_collab"
        assert acl["default"]["allowed_hosts"] == ["api.anthropic.com"]
        assert acl["default"]["allowed_domains"] == ["github.com"]

    def test_empty_network_reach_produces_empty_lists(self):
        acl = generate_smokescreen_acl("dock_x", [])
        # Valid ACL with empty allowlists — smokescreen will deny everything.
        # That's correct for a desk that declared no network_reach.
        assert acl["default"]["allowed_hosts"] == []
        assert acl["default"]["allowed_domains"] == []
        assert acl["default"]["action"] == "enforce"


class TestWriteSmokescreenAcl:
    def test_writes_valid_yaml(self, tmp_path):
        target = write_smokescreen_acl(
            "dock_collab",
            ["*.github.com", "api.anthropic.com"],
            tmp_path,
        )
        assert target.exists()
        assert target.name == "dock_collab.yaml"

        # Round-trip through YAML to confirm valid structure.
        with open(target) as f:
            parsed = yaml.safe_load(f)
        assert parsed["version"] == "v1"
        assert parsed["default"]["allowed_hosts"] == ["api.anthropic.com"]
        assert parsed["default"]["allowed_domains"] == ["github.com"]

    def test_creates_proxy_root_if_missing(self, tmp_path):
        nested = tmp_path / "deeply" / "nested" / "proxy"
        target = write_smokescreen_acl("dock_x", ["github.com"], nested)
        assert target.exists()
        assert nested.is_dir()

    def test_overwrites_existing_atomically(self, tmp_path):
        # First write
        write_smokescreen_acl("dock_x", ["github.com"], tmp_path)
        # Second write with different content
        target = write_smokescreen_acl("dock_x", ["pypi.org"], tmp_path)
        with open(target) as f:
            parsed = yaml.safe_load(f)
        assert parsed["default"]["allowed_hosts"] == ["pypi.org"]
        # No leftover tempfile
        leftover = list(Path(tmp_path).glob(".*.tmp"))
        assert leftover == []

    def test_file_is_world_readable(self, tmp_path):
        target = write_smokescreen_acl("dock_x", ["github.com"], tmp_path)
        mode = target.stat().st_mode & 0o777
        # 0644: container's UID needs to read via bind mount.
        assert mode == 0o644
