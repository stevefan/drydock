"""Snapshot storage for the Port Auditor (Phase PA0).

JSON-file-per-snapshot at ``~/.drydock/auditor/snapshots/<iso-timestamp>.json``.
No SQLite, no schema migrations — just dated files. Easy to iterate, easy to
inspect, easy to feed into the (future) LLM watch loop as raw JSON context.

If the snapshot count grows past a few thousand we may want SQLite for query
patterns, but for PA0 + early PA1, file-per-snapshot is the right shape:
- Trivial to read by humans + LLMs
- Backups are just `cp -r`
- Pruning is `find -mtime +N -delete`
"""

from __future__ import annotations

import json
from pathlib import Path

from .measurement import HarborSnapshot


def snapshot_dir() -> Path:
    """Path to the snapshots directory; created lazily."""
    return Path.home() / ".drydock" / "auditor" / "snapshots"


def write_snapshot(snapshot: HarborSnapshot) -> Path:
    """Persist a snapshot to disk; returns the file path."""
    d = snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    # ISO timestamp is filename — sortable, unique, human-readable.
    # Replace ':' (filesystem-friendly on Mac/Linux/Windows).
    safe_ts = snapshot.snapshot_at.replace(":", "-")
    path = d / f"{safe_ts}.json"
    path.write_text(json.dumps(snapshot.to_dict(), indent=2))
    return path


def list_snapshots() -> list[Path]:
    """All snapshot files in chronological order (oldest first)."""
    d = snapshot_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def read_snapshot(path: Path) -> dict:
    """Load a snapshot file."""
    return json.loads(path.read_text())


def latest_snapshot() -> dict | None:
    """The most recent snapshot, or None if no snapshots exist."""
    snaps = list_snapshots()
    if not snaps:
        return None
    return read_snapshot(snaps[-1])


def prune_snapshots(keep_count: int) -> int:
    """Remove all but the most recent ``keep_count`` snapshots.
    Returns the number removed. Idempotent."""
    snaps = list_snapshots()
    if len(snaps) <= keep_count:
        return 0
    to_remove = snaps[:-keep_count] if keep_count > 0 else snaps
    for p in to_remove:
        try:
            p.unlink()
        except OSError:
            pass
    return len(to_remove)
