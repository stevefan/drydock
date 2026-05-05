"""Hard resource ceilings — cgroup-enforced at container creation.

Phase A of docs/design/resource-ceilings.md: the substrate-enforced
half of the two-track design. These ceilings translate directly to
docker runArgs and the kernel kills any process that breaches them.
No Harbormaster/Harbormaster involvement — the substrate is the enforcer.

The deliberately-narrow Phase A surface:

| Field         | YAML key       | Docker flag         | Failure mode bounded |
|---------------|----------------|---------------------|----------------------|
| cpu_max       | cpu_max        | --cpus=N            | runaway loop / fork  |
| memory_max    | memory_max     | --memory=N          | OOM-takes-host       |
| pids_max      | pids_max       | --pids-limit=N      | fork bomb            |

`workspace_disk_max` is intentionally NOT in Phase A because reliable
filesystem quotas need an xfs/btrfs worktree volume that ext4-on-the-
Harbor doesn't have. Soft observation by the Harbormaster (Phase B) covers
the disk-fills case until quota fs lands.

Soft ceilings (anthropic_tokens_per_day, egress_bytes_per_day, etc.)
live in Phase B/C — same YAML neighbourhood, different module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Docker accepts memory values like "4g", "256m", "1024k", "2048b".
# We accept the same plus the IEC-suffixed forms ("4Gi", "256Mi") that
# Kubernetes-trained users write reflexively, normalizing to docker's
# expected form. Raw integers are interpreted as bytes (docker's default).
_MEMORY_RE = re.compile(r"^(\d+)\s*([kKmMgG]?)([iI]?)([bB]?)$")
_DOCKER_UNIT_FROM_PREFIX = {"": "b", "k": "k", "m": "m", "g": "g"}


class ResourceCeilingError(ValueError):
    """Raised for malformed `resources_hard` configuration."""


@dataclass(frozen=True)
class HardCeilings:
    """Validated, normalized hard resource ceilings.

    All fields are optional — None means "no ceiling at this layer; the
    substrate default applies." `from_dict` raises ResourceCeilingError
    on any malformed input rather than silently dropping.
    """
    cpu_max: float | None = None
    memory_max: str | None = None     # docker-format string ("4g", "256m", ...)
    pids_max: int | None = None

    @classmethod
    def from_dict(cls, raw: dict | None) -> "HardCeilings":
        if not raw:
            return cls()
        if not isinstance(raw, dict):
            raise ResourceCeilingError(
                f"resources_hard must be a mapping, got {type(raw).__name__}"
            )

        unknown = set(raw.keys()) - {"cpu_max", "memory_max", "pids_max"}
        if unknown:
            raise ResourceCeilingError(
                f"unknown resources_hard keys: {sorted(unknown)}; "
                f"supported: cpu_max, memory_max, pids_max"
            )

        cpu = _parse_cpu(raw.get("cpu_max"))
        mem = _parse_memory(raw.get("memory_max"))
        pids = _parse_pids(raw.get("pids_max"))
        return cls(cpu_max=cpu, memory_max=mem, pids_max=pids)

    def to_docker_runargs(self) -> list[str]:
        """Render as docker run flag strings to append to runArgs.

        Each flag is a single string (`--cpus=2.0`, not `["--cpus", "2.0"]`)
        to match the existing overlay convention.
        """
        args: list[str] = []
        if self.cpu_max is not None:
            args.append(f"--cpus={self.cpu_max}")
        if self.memory_max is not None:
            args.append(f"--memory={self.memory_max}")
        if self.pids_max is not None:
            args.append(f"--pids-limit={self.pids_max}")
        return args

    def is_empty(self) -> bool:
        return self.cpu_max is None and self.memory_max is None and self.pids_max is None

    def to_dict(self) -> dict:
        """Round-trippable JSON form for registry storage + audit."""
        out: dict = {}
        if self.cpu_max is not None:
            out["cpu_max"] = self.cpu_max
        if self.memory_max is not None:
            out["memory_max"] = self.memory_max
        if self.pids_max is not None:
            out["pids_max"] = self.pids_max
        return out


def _parse_cpu(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is technically int — exclude before the int check
        raise ResourceCeilingError("cpu_max must be a positive number, got bool")
    if not isinstance(value, (int, float)):
        raise ResourceCeilingError(
            f"cpu_max must be a positive number, got {type(value).__name__}"
        )
    if value <= 0:
        raise ResourceCeilingError(f"cpu_max must be > 0, got {value}")
    return float(value)


def _parse_pids(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResourceCeilingError(
            f"pids_max must be a positive integer, got {value!r}"
        )
    if value < 1:
        raise ResourceCeilingError(f"pids_max must be >= 1, got {value}")
    return value


def _parse_memory(value) -> str | None:
    """Normalize memory_max to docker's flag format.

    Accepted inputs (case-insensitive on the unit letters):
      "4g", "4G", "4gb", "4GB", "4Gi", "4GiB"  → "4g"
      "256m", "256Mi", "256MiB"                → "256m"
      "1024k"                                  → "1024k"
      4_000_000_000 (raw int)                  → "4000000000b"

    Rejects: empty string, "0g", negative values, unrecognized suffix.
    Raw int <= 0 also rejected. The "i"/"B" trailing letters are
    discarded (docker treats k/m/g as IEC binary units already).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ResourceCeilingError("memory_max must be a size string or int, got bool")
    if isinstance(value, int):
        if value <= 0:
            raise ResourceCeilingError(f"memory_max must be > 0, got {value}")
        return f"{value}b"
    if not isinstance(value, str):
        raise ResourceCeilingError(
            f"memory_max must be a size string or int, got {type(value).__name__}"
        )

    s = value.strip()
    if not s:
        raise ResourceCeilingError("memory_max cannot be empty")

    m = _MEMORY_RE.match(s)
    if not m:
        raise ResourceCeilingError(
            f"memory_max {value!r} is not a recognized size; "
            f"use forms like '4g', '256m', '1024k', or '4Gi'"
        )
    n = int(m.group(1))
    if n <= 0:
        raise ResourceCeilingError(f"memory_max must be > 0, got {value!r}")
    prefix = m.group(2).lower()  # "", k, m, g
    return f"{n}{_DOCKER_UNIT_FROM_PREFIX[prefix]}"
