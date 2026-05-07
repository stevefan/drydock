"""Live cgroup ceiling adjustment for running drydock containers.

Phase 2a.2 of make-the-harness-live: the harness's hard ceilings
(memory, CPU, PIDs) become mutable via `docker update` instead of being
frozen at container creation. Used by WorkloadLease grant/revoke and
by any future admin RPC that needs to adjust a running drydock's
resource envelope without recreate.

The kernel side is already live — cgroups always supported runtime
limit changes. Docker exposes this as `docker update <flags> <container>`.
This module wraps that with:
- normalized inputs from `HardCeilings`,
- structured errors for the daemon's audit pipeline,
- idempotent semantics (apply same value twice is a no-op).

Reverting is just `apply_cgroup_limits` with the original values, looked
up from the registry's `original_resources_hard` column (added in V6
schema migration).

What this module DOESN'T do:
- Track lease state. That's the broker's job.
- Decide what limits should be. That's policy (project YAML + WorkloadLease spec).
- Validate against `workload_max`. That's `core/policy.py`.

It is a pure mechanism layer over `docker update`.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Optional

from drydock.core.resource_ceilings import HardCeilings

logger = logging.getLogger(__name__)


class CgroupUpdateError(RuntimeError):
    """Raised when `docker update` fails for any reason.

    Carries the failed flags and the docker error text so callers can
    log structured audit events without parsing stderr.
    """
    def __init__(self, message: str, *, flags: list[str], stderr: str):
        super().__init__(message)
        self.flags = flags
        self.stderr = stderr


def apply_cgroup_limits(
    container_id: str,
    limits: HardCeilings,
    *,
    docker_bin: Optional[str] = None,
) -> list[str]:
    """Apply `limits` to the running container via `docker update`.

    Returns the list of `docker update` flags that were applied (useful
    for audit). Empty list if `limits.is_empty()` — caller can treat as
    a no-op.

    Idempotent: applying the same values twice produces no observable
    change beyond the docker daemon round-trip.

    Raises CgroupUpdateError if docker is unreachable, the container
    doesn't exist, or the kernel refuses (e.g., requested memory below
    current RSS — kernel can't shrink past in-use memory).
    """
    if not container_id:
        raise CgroupUpdateError(
            "container_id is required",
            flags=[], stderr="",
        )

    flags = _flags_for(limits)
    if not flags:
        logger.debug("apply_cgroup_limits: no-op (empty limits) for %s", container_id)
        return []

    docker = docker_bin or shutil.which("docker") or "docker"
    cmd = [docker, "update", *flags, container_id]
    logger.info("apply_cgroup_limits: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise CgroupUpdateError(
            f"docker binary not found: {exc}",
            flags=flags, stderr=str(exc),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CgroupUpdateError(
            "docker update timed out after 15s",
            flags=flags, stderr=exc.stderr or "",
        ) from exc

    if result.returncode != 0:
        raise CgroupUpdateError(
            f"docker update failed (exit {result.returncode}): {result.stderr.strip()}",
            flags=flags, stderr=result.stderr,
        )

    return flags


def revert_cgroup_limits(
    container_id: str,
    original: HardCeilings,
    *,
    lifted: Optional[HardCeilings] = None,
    docker_bin: Optional[str] = None,
) -> list[str]:
    """Restore the original ceilings.

    The wrinkle: if the desk had no original ceiling for some field
    (``original.memory_max is None`` → kernel was unlimited at create
    time) and the lift set one (``lifted.memory_max == "8g"``), then
    plain ``apply_cgroup_limits(container_id, original)`` would emit
    no flag for memory and the kernel would keep the lifted 8g cap
    in place forever. We need to emit docker's "unlimited" sentinels
    (-1 for memory, 0.0 for cpus, -1 for pids-limit) for any field
    that was lifted but wasn't capped originally.

    ``lifted`` is what was applied at grant time; passing it lets us
    detect this asymmetry. When omitted, fall back to plain apply
    (the legacy path — fine when original is non-empty for every
    field that was lifted).

    Returns the flags that were applied, for audit.

    Note: if the container is currently using more memory than the
    revert target, the kernel won't reclaim it. New allocations beyond
    the new ceiling will fail. That is correct behavior — the lease is
    over; the worker is expected to have wound down. If it didn't, the
    soft ceiling already flagged the divergence to the Auditor.
    """
    flags = _revert_flags_for(original, lifted)
    if not flags:
        return []
    docker = docker_bin or shutil.which("docker") or "docker"
    cmd = [docker, "update", *flags, container_id]
    logger.info("revert_cgroup_limits: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as exc:
        raise CgroupUpdateError(
            f"docker binary not found: {exc}", flags=flags, stderr=str(exc),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CgroupUpdateError(
            "docker update timed out after 15s", flags=flags, stderr=exc.stderr or "",
        ) from exc
    if result.returncode != 0:
        raise CgroupUpdateError(
            f"docker update failed (exit {result.returncode}): {result.stderr.strip()}",
            flags=flags, stderr=result.stderr,
        )
    return flags


def _revert_flags_for(
    original: HardCeilings,
    lifted: Optional[HardCeilings],
) -> list[str]:
    """Compute revert flags, emitting docker's unlimited sentinels for
    fields that were lifted but had no original cap."""
    flags: list[str] = []
    # Memory: -1 means unlimited in docker update.
    if original.memory_max is not None:
        flags.append(f"--memory={original.memory_max}")
        flags.append(f"--memory-swap={original.memory_max}")
    elif lifted is not None and lifted.memory_max is not None:
        flags.append("--memory=-1")
        flags.append("--memory-swap=-1")

    # CPUs: 0.0 means unlimited in docker update.
    if original.cpu_max is not None:
        flags.append(f"--cpus={original.cpu_max}")
    elif lifted is not None and lifted.cpu_max is not None:
        flags.append("--cpus=0.0")

    # PIDs: -1 means unlimited.
    if original.pids_max is not None:
        flags.append(f"--pids-limit={original.pids_max}")
    elif lifted is not None and lifted.pids_max is not None:
        flags.append("--pids-limit=-1")

    return flags


def _flags_for(limits: HardCeilings) -> list[str]:
    """Translate HardCeilings to `docker update` flag strings.

    Mirrors HardCeilings.to_docker_runargs() but only emits flags for
    fields that are set (None = leave alone, don't pass an empty value).
    """
    flags: list[str] = []
    if limits.cpu_max is not None:
        flags.append(f"--cpus={limits.cpu_max}")
    if limits.memory_max is not None:
        flags.append(f"--memory={limits.memory_max}")
        # docker requires --memory-swap when --memory is set, otherwise
        # the swap limit defaults to 2x memory which can surprise users.
        # Pin swap == memory (no swap allowed) — matches the docker run
        # flags drydock issues at container creation.
        flags.append(f"--memory-swap={limits.memory_max}")
    if limits.pids_max is not None:
        flags.append(f"--pids-limit={limits.pids_max}")
    return flags
