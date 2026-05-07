"""Tests for `drydock.core.cgroup` — live cgroup ceiling adjustment.

These tests pin the contract:
- empty HardCeilings → no `docker update` invoked, no flags, no error.
- non-empty HardCeilings → `docker update` called with the right flags.
- docker failure → CgroupUpdateError with structured fields for audit.
- memory flag emits both --memory and --memory-swap (no swap allowed).

We don't actually exec docker; we patch subprocess.run and assert the
exact invocation. The integration test that hits a real docker daemon
lives in scripts/smoke/ — out of unit scope.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from drydock.core.cgroup import (
    CgroupUpdateError,
    apply_cgroup_limits,
    revert_cgroup_limits,
)
from drydock.core.resource_ceilings import HardCeilings


def _ok():
    """Stub a successful subprocess.run result."""
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _fail(rc: int = 1, stderr: str = "Error: container not found"):
    r = MagicMock()
    r.returncode = rc
    r.stdout = ""
    r.stderr = stderr
    return r


class TestApplyCgroupLimits:
    def test_empty_limits_is_noop(self):
        with patch("drydock.core.cgroup.subprocess.run") as run:
            flags = apply_cgroup_limits("cid_abc", HardCeilings())
        assert flags == []
        run.assert_not_called()

    def test_memory_only_emits_memory_and_swap(self):
        limits = HardCeilings(memory_max="4g")
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()) as run:
            flags = apply_cgroup_limits("cid_abc", limits)
        # Memory must come paired with --memory-swap to avoid docker's
        # default 2x-memory swap allowance.
        assert flags == ["--memory=4g", "--memory-swap=4g"]
        cmd = run.call_args[0][0]
        assert cmd[1] == "update"
        assert "--memory=4g" in cmd
        assert "--memory-swap=4g" in cmd
        assert cmd[-1] == "cid_abc"

    def test_cpu_only(self):
        limits = HardCeilings(cpu_max=2.5)
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()):
            flags = apply_cgroup_limits("cid_x", limits)
        assert flags == ["--cpus=2.5"]

    def test_pids_only(self):
        limits = HardCeilings(pids_max=1024)
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()):
            flags = apply_cgroup_limits("cid_x", limits)
        assert flags == ["--pids-limit=1024"]

    def test_full_set(self):
        limits = HardCeilings(cpu_max=4.0, memory_max="8g", pids_max=2048)
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()):
            flags = apply_cgroup_limits("cid_x", limits)
        assert flags == ["--cpus=4.0", "--memory=8g", "--memory-swap=8g", "--pids-limit=2048"]

    def test_missing_container_id_raises(self):
        with pytest.raises(CgroupUpdateError) as exc_info:
            apply_cgroup_limits("", HardCeilings(cpu_max=1.0))
        assert "container_id is required" in str(exc_info.value)

    def test_docker_nonzero_exit_raises_with_stderr(self):
        with patch("drydock.core.cgroup.subprocess.run", return_value=_fail()):
            with pytest.raises(CgroupUpdateError) as exc_info:
                apply_cgroup_limits("cid_x", HardCeilings(cpu_max=1.0))
        assert "exit 1" in str(exc_info.value)
        assert exc_info.value.flags == ["--cpus=1.0"]
        assert "container not found" in exc_info.value.stderr

    def test_docker_binary_missing_raises(self):
        with patch("drydock.core.cgroup.subprocess.run", side_effect=FileNotFoundError("docker not found")):
            with pytest.raises(CgroupUpdateError) as exc_info:
                apply_cgroup_limits("cid_x", HardCeilings(cpu_max=1.0))
        assert "docker binary not found" in str(exc_info.value)

    def test_docker_timeout_raises(self):
        timeout = subprocess.TimeoutExpired(cmd=["docker"], timeout=15)
        with patch("drydock.core.cgroup.subprocess.run", side_effect=timeout):
            with pytest.raises(CgroupUpdateError) as exc_info:
                apply_cgroup_limits("cid_x", HardCeilings(cpu_max=1.0))
        assert "timed out" in str(exc_info.value)


class TestRevertCgroupLimits:
    def test_revert_with_original_emits_originals(self):
        original = HardCeilings(cpu_max=1.0, memory_max="2g")
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()) as run:
            flags = revert_cgroup_limits("cid_x", original)
        assert flags == ["--memory=2g", "--memory-swap=2g", "--cpus=1.0"]
        run.assert_called_once()

    def test_revert_to_empty_with_no_lifted_is_noop(self):
        # If a desk had no original ceilings AND nothing was lifted,
        # there's nothing to revert.
        with patch("drydock.core.cgroup.subprocess.run") as run:
            flags = revert_cgroup_limits("cid_x", HardCeilings())
        assert flags == []
        run.assert_not_called()

    def test_revert_emits_unlimited_when_lifted_field_had_no_original(self):
        # The smoke-caught bug: a desk with no original memory cap
        # gets a workload lift to 8g; revert needs to emit --memory=-1
        # to clear the kernel-level cap. Without this, the lifted
        # 8g would persist forever.
        original = HardCeilings()  # no caps
        lifted = HardCeilings(memory_max="8g")
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()) as run:
            flags = revert_cgroup_limits("cid_x", original, lifted=lifted)
        assert "--memory=-1" in flags
        assert "--memory-swap=-1" in flags
        run.assert_called_once()

    def test_revert_emits_cpu_unlimited_marker(self):
        original = HardCeilings()
        lifted = HardCeilings(cpu_max=4.0)
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()):
            flags = revert_cgroup_limits("cid_x", original, lifted=lifted)
        assert "--cpus=0.0" in flags

    def test_revert_emits_pids_unlimited_marker(self):
        original = HardCeilings()
        lifted = HardCeilings(pids_max=2048)
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()):
            flags = revert_cgroup_limits("cid_x", original, lifted=lifted)
        assert "--pids-limit=-1" in flags

    def test_revert_mixed_original_and_lifted_only_fields(self):
        # Memory was capped originally; cpus was unlimited.
        # Workload lifted both. Revert should restore memory to its
        # original cap AND clear the cpu cap that was added.
        original = HardCeilings(memory_max="2g")
        lifted = HardCeilings(memory_max="8g", cpu_max=4.0)
        with patch("drydock.core.cgroup.subprocess.run", return_value=_ok()):
            flags = revert_cgroup_limits("cid_x", original, lifted=lifted)
        assert "--memory=2g" in flags
        assert "--memory-swap=2g" in flags
        assert "--cpus=0.0" in flags
