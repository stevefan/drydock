"""Deterministic measurement layer (Phase PA0).

Snapshots a Harbor's current state into structured facts for the (later)
LLM judgment layers to consume. All collection is via well-trodden
deterministic paths:

- ``docker stats --no-stream --format '{{json .}}'`` for cgroup-equivalent
  resource usage per container (works the same on macOS Docker Desktop and
  Linux Docker). Pure stdlib subprocess; no docker SDK dependency.
- Audit log scan for recent broker activity per Dock.
- Lease table query via Registry for active-lease counts by type.
- Project YAML SHA via existing ``project_yaml_sha`` module for drift status.

The output is a snapshot dict that's safe to JSON-serialize, version
in storage, and feed to LLMs as context.

Failure modes are explicit:
- Container lookup fails → metrics field is None; snapshot still emits.
- docker stats fails → metrics field is None; logged at info level.
- Audit log unreadable → audit-counts default to None.

The principle: never crash the snapshot just because one source is
unavailable. A partial snapshot is more useful than no snapshot.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from drydock.core.audit import DEFAULT_LOG_PATH as AUDIT_LOG_PATH
from drydock.core.project_yaml_sha import (
    compute_project_yaml_sha,
    yaml_drift_status,
)


# Unit multipliers. docker stats outputs both binary (KiB/MiB/GiB) and
# decimal (kB/MB/GB) depending on the field; handle both.
_UNIT_MULTIPLIERS = {
    "B":   1,
    "kB":  1_000,             "KiB": 1_024,
    "MB":  1_000_000,         "MiB": 1_024 * 1_024,
    "GB":  1_000_000_000,     "GiB": 1_024 * 1_024 * 1_024,
    "TB":  1_000_000_000_000, "TiB": 1_024 * 1_024 * 1_024 * 1_024,
}
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([A-Za-z]+)\s*$")
_DOCKER_STATS_TIMEOUT = 5
_AUDIT_RECENT_WINDOW = timedelta(hours=1)


def parse_size(s: str) -> int | None:
    """Parse a docker-stats size string (e.g. '1.842GiB', '256MB') to bytes.

    Returns None if the string can't be parsed. Accepts both binary
    (KiB/MiB/GiB) and decimal (kB/MB/GB) units. Stripped of whitespace.
    """
    if not s:
        return None
    m = _SIZE_RE.match(s.strip())
    if not m:
        return None
    n_str, unit = m.group(1), m.group(2)
    try:
        n = float(n_str)
    except ValueError:
        return None
    mult = _UNIT_MULTIPLIERS.get(unit)
    if mult is None:
        return None
    return int(n * mult)


def parse_percent(s: str) -> float | None:
    """Parse a percent string ('12.5%') to a float (12.5). None on failure."""
    if not s:
        return None
    s = s.strip().rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_docker_stats_line(line: str) -> dict | None:
    """Parse one line of `docker stats --format '{{json .}}'` into structured.

    Returns dict with keys: cpu_pct, mem_used_bytes, mem_limit_bytes,
    mem_pct, pids. Any individual field is None on parse failure; the
    function only returns None for completely malformed input.
    """
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    mem_usage = raw.get("MemUsage", "")  # "1.842GiB / 4GiB"
    mem_used = mem_limit = None
    if isinstance(mem_usage, str) and "/" in mem_usage:
        used_str, _, limit_str = mem_usage.partition("/")
        mem_used = parse_size(used_str)
        mem_limit = parse_size(limit_str)

    pids_raw = raw.get("PIDs", "")
    try:
        pids = int(pids_raw) if pids_raw not in ("", "--") else None
    except (ValueError, TypeError):
        pids = None

    return {
        "cpu_pct": parse_percent(raw.get("CPUPerc", "")),
        "mem_used_bytes": mem_used,
        "mem_limit_bytes": mem_limit,
        "mem_pct": parse_percent(raw.get("MemPerc", "")),
        "pids": pids,
        "container_id": raw.get("ID") or raw.get("Container") or "",
        "container_name": raw.get("Name") or "",
    }


def collect_docker_stats(container_ids: Iterable[str]) -> dict[str, dict]:
    """Run `docker stats` for the given container IDs; return id → metrics.

    Containers that are missing or unreachable are simply absent from the
    returned dict. Returns {} on docker-not-available.
    """
    ids = [c for c in container_ids if c]
    if not ids:
        return {}
    cmd = ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_DOCKER_STATS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        # Some containers may have died between list+stats; partial output
        # on stdout is still usable.
        if not result.stdout.strip():
            return {}

    out: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        parsed = parse_docker_stats_line(line)
        if parsed is None:
            continue
        # docker stats outputs the short ID; match prefix against our IDs.
        cid_short = parsed["container_id"]
        for full_id in ids:
            if full_id.startswith(cid_short) or cid_short.startswith(full_id):
                out[full_id] = parsed
                break
    return out


def count_recent_audit_events(
    desk_id: str,
    *,
    audit_path: Path | None = None,
    window: timedelta = _AUDIT_RECENT_WINDOW,
    now: datetime | None = None,
) -> dict | None:
    """Count audit events for a Dock in the recent window, broken by event class.

    Returns dict {events_total, by_event_class: {...}} or None if the
    audit log is unreadable. Counts are 0 (not None) when the log is
    readable but the Dock has no recent events.
    """
    if audit_path is None:
        audit_path = AUDIT_LOG_PATH
    if not audit_path.exists():
        return None
    cutoff = (now or datetime.now(timezone.utc)) - window
    counts: dict[str, int] = {}
    total = 0
    try:
        with audit_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Audit shape: {timestamp, event, principal, ...} or older
                # {timestamp, event_type, ...}. Tolerate both.
                ts_str = rec.get("timestamp") or rec.get("ts")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
                # Identify the Dock this event refers to (audit shape varies).
                principal = rec.get("principal") or ""
                details = rec.get("details", {}) or {}
                involved_desk = (
                    principal
                    or details.get("desk_id")
                    or details.get("caller_desk_id")
                    or ""
                )
                if involved_desk != desk_id:
                    continue
                event = rec.get("event") or rec.get("event_type") or "unknown"
                counts[event] = counts.get(event, 0) + 1
                total += 1
    except OSError:
        return None
    return {"events_total": total, "by_event_class": counts}


def count_active_leases(registry, desk_id: str) -> dict:
    """Active lease counts for a Dock, broken by capability type.

    Returns {active_total, by_type: {...}}. Empty if no active leases.
    """
    cur = registry._conn.execute(
        "SELECT type, COUNT(*) AS n FROM leases "
        "WHERE desk_id = ? AND revoked = 0 GROUP BY type",
        (desk_id,),
    )
    by_type: dict[str, int] = {}
    total = 0
    for row in cur.fetchall():
        by_type[row["type"]] = row["n"]
        total += row["n"]
    return {"active_total": total, "by_type": by_type}


@dataclass
class HarborSnapshot:
    """A point-in-time snapshot of a Harbor's state. JSON-serializable."""
    snapshot_at: str
    harbor_hostname: str
    drydock_count: int
    drydocks: list[dict]

    def to_dict(self) -> dict:
        return {
            "snapshot_at": self.snapshot_at,
            "harbor_hostname": self.harbor_hostname,
            "drydock_count": self.drydock_count,
            "drydocks": self.drydocks,
        }


def snapshot_harbor(
    registry, *, hostname: str | None = None,
    audit_path: Path | None = None,
) -> HarborSnapshot:
    """Take a full Harbor snapshot — all Drydocks measured.

    The snapshot is the unit of work the (future) Auditor LLM consumes:
    one snapshot = one moment in time across the whole Harbor. Stored as
    a JSON file (see storage.py); fed as context to LLM judgment calls.
    """
    import platform
    if hostname is None:
        hostname = platform.node() or "unknown"

    workspaces = registry.list_workspaces()
    container_ids = [w.container_id for w in workspaces if w.container_id]
    stats_by_cid = collect_docker_stats(container_ids)

    snapshot_at = datetime.now(timezone.utc).isoformat()
    drydocks_data: list[dict] = []
    for ws in workspaces:
        metrics = stats_by_cid.get(ws.container_id) if ws.container_id else None
        leases = count_active_leases(registry, ws.id)
        audit = count_recent_audit_events(ws.id, audit_path=audit_path)
        pinned = getattr(ws, "pinned_yaml_sha256", "") or ""
        current = compute_project_yaml_sha(ws.project)
        drydocks_data.append({
            "name": ws.name,
            "id": ws.id,
            "project": ws.project,
            "yard_id": getattr(ws, "yard_id", None),
            "state": ws.state,
            "container_id": ws.container_id[:12] if ws.container_id else None,
            "metrics": metrics,
            "leases": leases,
            "audit_recent_1h": audit,
            "yaml_drift": yaml_drift_status(pinned, current),
        })

    return HarborSnapshot(
        snapshot_at=snapshot_at,
        harbor_hostname=hostname,
        drydock_count=len(drydocks_data),
        drydocks=drydocks_data,
    )
