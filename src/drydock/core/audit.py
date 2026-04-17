"""Append-only audit log for workspace lifecycle events.

Two emitters write to the same JSONL file at ~/.drydock/audit.log:

- `log_event(event, workspace_id, extra)` — v1 shape. Used by host CLI
  commands today (cli/create.py, cli/destroy.py, cli/stop.py,
  cli/tailnet.py). Kept for backward compatibility — every existing
  consumer reads its keys (`event`, `workspace_id`, `timestamp`).

- `emit_audit(event, principal, request_id, method, result, details)`
  — V2 spec shape per docs/v2-design-state.md §1a. Used by the wsd
  daemon. Adds `principal`, `request_id`, `method`, `result` so audit
  consumers can correlate events to RPC calls and filter by caller.

Both shapes coexist in the same JSONL stream. The Slice 4c GetAudit
query handler returns whatever shape was written; consumers read the
union of keys and tolerate absence per the contract in §1a.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

DEFAULT_LOG_PATH = Path.home() / ".drydock" / "audit.log"


# Stable event vocabulary per docs/v2-design-state.md §1a. Tests pin
# these names — adding events is free; renaming or removing is a
# breaking change for every audit consumer.
V2_EVENTS = frozenset({
    "desk.created",
    "desk.resumed",
    "desk.spawned",
    "desk.spawn_rejected",
    "desk.stopped",
    "desk.destroyed",
    "desk.error",
    "lease.issued",
    "lease.renewed",
    "lease.released",
    "token.issued",
    "token.revoked",
    "tailnet.device_deleted",
    "tailnet.device_delete_failed",
})


def _resolve_log_path(log_path: Path | None) -> Path:
    """Look up DEFAULT_LOG_PATH at call time.

    Tests monkeypatch the module attribute after import; resolving the
    default here (instead of in the function signature) makes the patch
    take effect.
    """
    if log_path is not None:
        return log_path
    # Module-level lookup so test monkeypatching of audit.DEFAULT_LOG_PATH
    # propagates to every caller without needing to re-import.
    import drydock.core.audit as _self
    return _self.DEFAULT_LOG_PATH


def log_event(
    event: str,
    workspace_id: str,
    extra: dict | None = None,
    *,
    log_path: Path | None = None,
) -> None:
    """v1 audit emitter — host CLI commands.

    Kept for backward compatibility; every existing host CLI uses this
    shape. Daemon-side emission uses `emit_audit` (V2 schema).
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "workspace_id": workspace_id,
    }
    if extra:
        entry.update(extra)
    _append(_resolve_log_path(log_path), entry)


def emit_audit(
    event: str,
    *,
    principal: str | None,
    request_id: str | int | None,
    method: str,
    result: Literal["ok", "error"],
    details: dict | None = None,
    log_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """V2 daemon audit emitter (docs/v2-design-state.md §1a).

    Writes one JSONL line. Returns the entry that was written so callers
    can also log it elsewhere if helpful. `now` parameter exists for
    deterministic test assertions.
    """
    if event not in V2_EVENTS:
        # Soft-fail with logging would be friendlier, but the event-name
        # vocabulary IS the consumer contract — a typo here is a bug
        # that ships rotten data into the audit stream. Fail loud at
        # development time; production daemon catches at the dispatcher.
        raise ValueError(f"unknown audit event: {event!r} (known: {sorted(V2_EVENTS)})")

    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    entry = {
        "ts": timestamp,
        "event": event,
        "principal": principal,
        "request_id": str(request_id) if request_id is not None else None,
        "method": method,
        "result": result,
        "details": details or {},
    }
    _append(_resolve_log_path(log_path), entry)
    return entry


def _append(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
