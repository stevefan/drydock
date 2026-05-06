"""Auditor heartbeat — the deterministic signal the deadman switch reads.

The Auditor (Phase PA1+) touches a heartbeat file every cycle. The deadman
switch (separate, deterministic, Authority-side) reads the file's mtime to
decide whether the Auditor is alive.

Why a file rather than a registry table:
- Zero coupling to wsd/SQLite. Deadman runs as a small standalone script
  (cron / systemd-timer), needs no daemon dependency.
- Atomic mtime updates from `touch` or `Path.touch()` — no SQL transactions.
- Trivially observable: `stat` the file and you have the answer.
- Survives wsd restarts / SQLite migrations without coupling.

Why this matters: the deadman is the LAST line of defense for "is the LLM
agent watching the fleet still alive." It has to work even when wsd is
in a bad state. File-based heartbeat keeps that separation honest.

Design constraint (per port-auditor.md): the heartbeat-WRITER is the
Auditor's loop (PA1+); the heartbeat-READER is the deadman script. Writer
runs in the Auditor Drydock; reader runs on the Harbor host. Both touch
the same file at ``~/.drydock/auditor/heartbeat`` (path is in the host's
home; the Auditor Drydock has it bind-mounted from the host).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_PATH = Path.home() / ".drydock" / "auditor" / "heartbeat"


def heartbeat_path() -> Path:
    """Returns the heartbeat file path. Helper for testability."""
    return HEARTBEAT_PATH


def touch(path: Path | None = None) -> None:
    """Update the heartbeat file's mtime to now. Creates if absent.

    Called by the Auditor's watch loop on every cycle. Idempotent.
    """
    p = path or HEARTBEAT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def last_heartbeat(path: Path | None = None) -> datetime | None:
    """Return the heartbeat file's last-modified time, or None if absent."""
    p = path or HEARTBEAT_PATH
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)


def staleness_seconds(path: Path | None = None) -> float | None:
    """Seconds since the heartbeat was last touched, or None if never."""
    last = last_heartbeat(path)
    if last is None:
        return None
    return (datetime.now(timezone.utc) - last).total_seconds()


def is_stale(threshold_seconds: int, path: Path | None = None) -> bool:
    """True if heartbeat is older than threshold OR file doesn't exist.

    Returns False if the heartbeat file has never existed (interpretation:
    no Auditor designated yet — deadman should not alert in that case).
    Returns True only when the file existed at some point AND is now stale.

    The "no Auditor exists" vs "Auditor exists but is silent" distinction
    matters for alerting behavior: don't wake the principal because they
    haven't built the Auditor yet.
    """
    last = last_heartbeat(path)
    if last is None:
        return False  # Never seen a heartbeat → no Auditor to monitor → silent
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age > threshold_seconds
