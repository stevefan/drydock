"""Tests for the auditor role validator (Phase PA3.1).

The validator IS the gate that makes "Auditor is a drydock with role"
not collapse into "Auditor is whatever drydock you flag." So pinning
each constraint is the contract test surface.

For each violation kind: one failing case. Plus: one passing case
(the canonical example YAML in scripts/port-auditor/project.yaml).
And: a non-auditor role bypasses the validator entirely.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from drydock.core.project_config import (
    ProjectConfig,
    ROLE_AUDITOR,
    ROLE_WORKER,
    load_project_config,
)
from drydock.core.auditor.role_validator import validate_auditor_role


def _minimal_passing_cfg() -> ProjectConfig:
    """The smallest config that passes the auditor validator. Tests
    perturb one field at a time off this baseline to pin each
    violation."""
    return ProjectConfig(
        role=ROLE_AUDITOR,
        firewall_extra_domains=["api.anthropic.com", "api.telegram.org"],
        resources_hard={"cpu_max": 1.0, "memory_max": "1g"},
    )


class TestPassthroughForNonAuditor:
    def test_worker_role_skips_validation(self):
        """Validator returns ok for non-auditor configs even if they'd
        fail the auditor constraints — it's only the auditor's gate."""
        cfg = ProjectConfig(
            role=ROLE_WORKER,
            firewall_extra_domains=["evil.example.com"],
            firewall_aws_ip_ranges=["us-west-2:AMAZON"],
            capabilities=["request_secret_leases"],
        )
        result = validate_auditor_role(cfg)
        assert result.ok is True
        assert result.violations == ()


class TestPassingCase:
    def test_minimal_passing_config(self):
        result = validate_auditor_role(_minimal_passing_cfg())
        assert result.ok is True, result.violations
        assert result.violations == ()

    def test_bundled_template_yaml_passes(self, tmp_path):
        """The bundled template in src/drydock/cli/new.py
        (_auditor_project_yaml) is THE documented shape. If it stops
        passing the validator, either the validator changed
        (deliberate) or the template drifted (regression). Either way,
        the test surfaces it. This is also covered end-to-end by
        test_new.py::TestNewAuditor — keeping a unit-level pin here
        for fast feedback on validator drift specifically."""
        from drydock.cli.new import _auditor_project_yaml, AUDITOR_DEFAULT_BASE_TAG
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "port-auditor.yaml").write_text(
            _auditor_project_yaml(tmp_path, AUDITOR_DEFAULT_BASE_TAG)
        )
        cfg = load_project_config("port-auditor", base_dir=projects_dir)
        assert cfg is not None
        assert cfg.role == ROLE_AUDITOR
        result = validate_auditor_role(cfg)
        assert result.ok is True, [
            (v.code, v.message) for v in result.violations
        ]


class TestEgressViolations:
    def test_disallowed_firewall_domain(self):
        cfg = _minimal_passing_cfg()
        cfg.firewall_extra_domains = ["api.anthropic.com", "evil.example.com"]
        result = validate_auditor_role(cfg)
        assert result.ok is False
        codes = {v.code for v in result.violations}
        assert "egress-domain-not-allowed" in codes

    def test_aws_ranges_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.firewall_aws_ip_ranges = ["us-west-2:AMAZON"]
        result = validate_auditor_role(cfg)
        assert "aws-egress-forbidden" in {v.code for v in result.violations}

    def test_ipv6_hosts_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.firewall_ipv6_hosts = ["[2001:db8::1]:443"]
        result = validate_auditor_role(cfg)
        assert "ipv6-egress-forbidden" in {v.code for v in result.violations}

    def test_network_reach_delegation_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.delegatable_network_reach = ["*.example.com"]
        result = validate_auditor_role(cfg)
        assert "network-reach-delegation-forbidden" in {
            v.code for v in result.violations
        }


class TestBrokerCapabilityViolations:
    @pytest.mark.parametrize("cap", [
        "request_secret_leases",
        "request_storage_leases",
        "request_provision_leases",
        "request_workload_leases",
        "request_network_reach",
    ])
    def test_each_forbidden_capability_caught(self, cap):
        cfg = _minimal_passing_cfg()
        cfg.capabilities = [cap]
        result = validate_auditor_role(cfg)
        assert "broker-capability-forbidden" in {
            v.code for v in result.violations
        }


class TestDelegationViolations:
    def test_delegatable_secrets_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.delegatable_secrets = ["foo"]
        result = validate_auditor_role(cfg)
        assert "secret-delegation-forbidden" in {
            v.code for v in result.violations
        }

    def test_delegatable_storage_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.delegatable_storage_scopes = ["s3://bucket/*"]
        result = validate_auditor_role(cfg)
        assert "storage-delegation-forbidden" in {
            v.code for v in result.violations
        }

    def test_delegatable_provision_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.delegatable_provision_scopes = ["sts:GetCallerIdentity"]
        result = validate_auditor_role(cfg)
        assert "provision-delegation-forbidden" in {
            v.code for v in result.violations
        }

    def test_delegatable_firewall_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.delegatable_firewall_domains = ["*.example.com"]
        result = validate_auditor_role(cfg)
        assert "firewall-delegation-forbidden" in {
            v.code for v in result.violations
        }


class TestResourceCeilingViolations:
    def test_missing_resources_hard_rejected(self):
        cfg = _minimal_passing_cfg()
        cfg.resources_hard = {}
        result = validate_auditor_role(cfg)
        assert "resource-ceiling-required" in {
            v.code for v in result.violations
        }

    def test_cpu_above_max_rejected(self):
        cfg = _minimal_passing_cfg()
        cfg.resources_hard = {"cpu_max": 4.0, "memory_max": "1g"}
        result = validate_auditor_role(cfg)
        assert "cpu-ceiling-exceeded" in {v.code for v in result.violations}

    def test_memory_above_max_rejected(self):
        cfg = _minimal_passing_cfg()
        cfg.resources_hard = {"cpu_max": 1.0, "memory_max": "16g"}
        result = validate_auditor_role(cfg)
        assert "memory-ceiling-exceeded" in {
            v.code for v in result.violations
        }

    def test_memory_malformed_rejected(self):
        cfg = _minimal_passing_cfg()
        cfg.resources_hard = {"cpu_max": 1.0, "memory_max": "lots"}
        result = validate_auditor_role(cfg)
        assert "memory-ceiling-malformed" in {
            v.code for v in result.violations
        }


class TestImageViolations:
    def test_disallowed_image_rejected(self):
        cfg = _minimal_passing_cfg()
        cfg.image = "untrusted/random:latest"
        result = validate_auditor_role(cfg)
        assert "image-not-approved" in {v.code for v in result.violations}

    def test_drydock_base_image_allowed(self):
        cfg = _minimal_passing_cfg()
        cfg.image = "ghcr.io/stevefan/drydock-base:v0.5.0"
        result = validate_auditor_role(cfg)
        assert result.ok is True, result.violations

    def test_baked_auditor_image_allowed(self):
        cfg = _minimal_passing_cfg()
        cfg.image = "ghcr.io/stevefan/drydock-port-auditor:v0.1.0"
        result = validate_auditor_role(cfg)
        assert result.ok is True, result.violations


class TestMountAndPortViolations:
    def test_storage_mounts_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.storage_mounts = [{"source": "s3://b/p", "target": "/m"}]
        result = validate_auditor_role(cfg)
        assert "storage-mount-forbidden" in {
            v.code for v in result.violations
        }

    def test_extra_mounts_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.extra_mounts = ["source=foo,target=/foo,type=volume"]
        result = validate_auditor_role(cfg)
        assert "extra-mount-forbidden" in {
            v.code for v in result.violations
        }

    def test_forward_ports_forbidden(self):
        cfg = _minimal_passing_cfg()
        cfg.forward_ports = [3000]
        result = validate_auditor_role(cfg)
        assert "forward-port-forbidden" in {
            v.code for v in result.violations
        }


class TestCollectAllSemantics:
    def test_multiple_violations_all_returned(self):
        """Collect-all (not fail-fast) — operator can fix everything
        at once."""
        cfg = _minimal_passing_cfg()
        cfg.firewall_aws_ip_ranges = ["us-west-2:AMAZON"]
        cfg.capabilities = ["request_secret_leases"]
        cfg.storage_mounts = [{"source": "s3://b/p", "target": "/m"}]
        result = validate_auditor_role(cfg)
        assert result.ok is False
        codes = {v.code for v in result.violations}
        assert "aws-egress-forbidden" in codes
        assert "broker-capability-forbidden" in codes
        assert "storage-mount-forbidden" in codes
