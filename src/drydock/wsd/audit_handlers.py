"""GetAudit RPC handler — paginated query over the audit JSONL (Slice 4c).

V2 ships a paginated query rather than true streaming per the design
call documented in commit ca8b783's parent thread: a real streaming
consumer hasn't materialized; pagination matches every audit-consumer
pattern actually built (grep, weekly review, ad-hoc dashboard).

Streaming variant remains earnable when a tail-following consumer
shows up — additive over this paginated handler.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from drydock.core.audit import V2_EVENTS
from drydock.wsd.server import _RpcError

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def get_audit(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    log_path: Path,
) -> dict:
    """Paginated query over the audit JSONL stream.

    Filters (all optional):
    - `before_ts`: ISO8601 cursor; only events with ts < cursor returned
    - `limit`: 1..1000, default 100
    - `event`: exact match against the `event` field
    - `principal`: exact match (matches V2 entries' `principal` or v1
      entries' `workspace_id` for cross-shape consumer convenience)

    Returns: {events: [...], next_before_ts: str | None}.
    `next_before_ts` is non-null when more events remain past the limit;
    pass it back as `before_ts` for the next page.
    """
    del request_id, caller_desk_id

    filters = _validate_filters(params)
    if not log_path.exists():
        return {"events": [], "next_before_ts": None}

    matching: list[dict] = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("audit: skipping malformed JSONL line in %s", log_path)
                continue
            if not isinstance(entry, dict):
                continue
            if not _matches(entry, filters):
                continue
            matching.append(entry)

    # Newest-first pagination — the typical "show me what just happened"
    # query. Sorted by ts (V2) or timestamp (v1) descending.
    matching.sort(key=_entry_ts_for_sort, reverse=True)

    limit = filters["limit"]
    page = matching[:limit]
    next_cursor: str | None = None
    if len(matching) > limit:
        next_cursor = _entry_ts(page[-1])

    return {"events": page, "next_before_ts": next_cursor}


def _validate_filters(params: object) -> dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "params must be an object"})

    limit = params.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or limit < 1 or limit > MAX_LIMIT:
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": f"limit must be int in [1, {MAX_LIMIT}]"})

    before_ts = params.get("before_ts")
    if before_ts is not None and not isinstance(before_ts, str):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "before_ts must be ISO8601 string"})

    event = params.get("event")
    if event is not None and not isinstance(event, str):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "event must be a string"})
    # We DON'T require event ∈ V2_EVENTS here — consumers may want to
    # filter on v1-shape events too (workspace.created etc.). Validation
    # is a no-op pass-through; mismatched names just yield empty results.

    principal = params.get("principal")
    if principal is not None and not isinstance(principal, str):
        raise _RpcError(code=-32602, message="invalid_params",
                        data={"reason": "principal must be a string"})

    return {
        "limit": limit,
        "before_ts": before_ts,
        "event": event,
        "principal": principal,
    }


def _matches(entry: dict, filters: dict) -> bool:
    if filters["event"] is not None and entry.get("event") != filters["event"]:
        return False
    if filters["principal"] is not None:
        # V2 entry: principal field. v1 entry: workspace_id.
        principal_match = (
            entry.get("principal") == filters["principal"]
            or entry.get("workspace_id") == filters["principal"]
        )
        if not principal_match:
            return False
    if filters["before_ts"] is not None:
        ts = _entry_ts(entry)
        if ts is None or ts >= filters["before_ts"]:
            return False
    return True


def _entry_ts(entry: dict) -> str | None:
    """V2 entries use `ts`; v1 entries use `timestamp`."""
    ts = entry.get("ts") or entry.get("timestamp")
    return ts if isinstance(ts, str) else None


def _entry_ts_for_sort(entry: dict) -> str:
    """Sort key tolerant of missing/malformed ts."""
    return _entry_ts(entry) or ""
