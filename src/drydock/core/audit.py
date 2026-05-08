"""Append-only audit log for drydock lifecycle events.

Two emitters write to the same JSONL file at ~/.drydock/audit.log:

- `log_event(event, drydock_id, extra)` — v1 shape. Used by host CLI
  commands today (cli/create.py, cli/destroy.py, cli/stop.py,
  cli/tailnet.py). Kept for backward compatibility — every existing
  consumer reads its keys (`event`, `drydock_id`, `timestamp`).

- `emit_audit(event, principal, request_id, method, result, details)`
  — V2 spec shape per docs/v2-design-state.md §1a. Used by the drydock daemon
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
    "drydock.created",
    "drydock.resumed",
    "drydock.spawned",
    "drydock.spawn_rejected",
    "drydock.stopped",
    "drydock.destroyed",
    "drydock.error",
    "lease.issued",
    "lease.renewed",
    "lease.released",
    "token.issued",
    "token.revoked",
    "tailnet.device_deleted",
    "tailnet.device_delete_failed",
    # Phase 2a.3 WL1 — workload-as-transaction primitive.
    "workload.lease_granted",
    "workload.lease_released",
    "workload.lease_apply_failed",
    "workload.lease_expired",
    "workload.lease_partial_revoked",
    # Phase 2a.4 M1 — migration primitive.
    "drydock.migration_started",
    "drydock.migration_stage",      # one event per stage transition; details.stage names it
    "drydock.migrated",             # terminal success
    "drydock.migration_rolled_back", # rollback from snapshot succeeded
    "drydock.migration_failed",     # rollback also failed; manual intervention
    # Phase PA3 — Auditor action authority.
    "auditor.designated",           # drydock token's scope flipped to 'auditor'
    "auditor.scope_revoked",        # token reverted to 'dock'
    "auditor.action_dry_run",       # Bucket-2 call audited but not executed (V1 default)
    "auditor.action_executed",      # Bucket-2 call actually invoked the primitive (live mode)
    "auditor.action_refused",       # caller's token wasn't auditor-scoped
    # Phase PA3.8 — clarification channel.
    "auditor.clarification_registered",  # worker registered context for the Auditor's judgment
    "auditor.clarification_rejected",    # sanitizer caught a violation (logged but not raised here)
    # Phase 2 (proxy rollout) — egress allowlist mutation.
    "egress.allowlist_updated",       # UpdateProxyAllowlist successfully wrote + signaled
    "egress.allowlist_rejected",      # narrowness gate refused the call
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
    drydock_id: str,
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
        "drydock_id": drydock_id,
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
