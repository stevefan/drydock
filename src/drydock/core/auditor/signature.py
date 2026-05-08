"""Snapshot signature dedup (Phase PA3.9).

Cost optimization for the watch loop. Each tick computes a stable
hash over the *substantively changeable* parts of a HarborSnapshot
(drydock states, lease counts, metric coarse-buckets, audit-log size,
clarification ids). If the hash matches the previous tick's — meaning
nothing meaningful has changed since we last asked the LLM — skip the
LLM call entirely. Heartbeat still fires (deadman won't trip), a
"deduplicated" verdict is logged so the watch_log shows continuity.

Deliberate carve-outs from the hash (so they don't trigger spurious
re-evaluation):
  - snapshot_at timestamp (every tick has a new one)
  - tick_at timestamp
  - exact metric values (we bucket cpu_pct to 10% bands and
    mem_used to 100MB bands — small wiggle is noise)
  - heartbeat / health-check fields

Floor: every FORCE_REFRESH_SECONDS (default 1800 = 30min) we force
a real LLM call regardless of signature match. Catches the "drydocks
all stably broken in the same way" failure mode where the signature
is identical but the principal would want to know about it again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# How long can we go between actual LLM calls before forcing one
# regardless of signature match. 30min default — short enough that
# a sustained anomaly gets re-evaluated; long enough that idle
# Harbors burn near-zero tokens.
FORCE_REFRESH_SECONDS = 30 * 60

# Coarse-bucket sizes — small wiggle in these dimensions is treated
# as identical for signature purposes. cpu_pct in 10% bands;
# mem_used in 100MB bands.
_CPU_BUCKET_PCT = 10.0
_MEM_BUCKET_BYTES = 100 * 1024 * 1024


@dataclass
class SignatureState:
    """Persisted across ticks. Stored at
    ~/.drydock/auditor/last_signature.json inside the auditor container."""
    last_signature: str = ""
    last_real_call_unix: float = 0.0


def _bucket_cpu(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v) // _CPU_BUCKET_PCT * _CPU_BUCKET_PCT
    except (TypeError, ValueError):
        return None


def _bucket_mem(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v) // _MEM_BUCKET_BYTES * _MEM_BUCKET_BYTES
    except (TypeError, ValueError):
        return None


def compute_signature(
    snapshot: dict,
    clarifications: list[dict] | None = None,
) -> str:
    """SHA-256 hex over a normalized signature payload.

    The payload includes only fields where a *meaningful* change
    means the LLM should reconsider. Pure-noise fields (timestamps,
    monotonic counters, exact float metrics) are excluded or bucketed.
    """
    docs_norm = []
    for d in snapshot.get("drydocks", []):
        metrics = d.get("metrics") or {}
        leases = d.get("leases") or {}
        audit_recent = d.get("audit_recent_1h") or {}
        norm = {
            "name": d.get("name"),
            "state": d.get("state"),
            "yard": d.get("yard_id"),
            "cpu_bucket": _bucket_cpu(metrics.get("cpu_pct")),
            "mem_bucket": _bucket_mem(metrics.get("mem_used_bytes")),
            "pids": metrics.get("pids"),
            "lease_active_total": leases.get("active_total", 0),
            "lease_by_type": leases.get("by_type") or {},
            "audit_events_1h": audit_recent.get("events_total", 0),
            "yaml_drift": d.get("yaml_drift"),
        }
        docs_norm.append(norm)
    docs_norm.sort(key=lambda x: x.get("name") or "")

    clar_norm = []
    if clarifications:
        for c in clarifications:
            clar_norm.append({
                "id": c.get("id"),
                "drydock_id": c.get("drydock_id"),
                "kind": c.get("kind"),
            })
        clar_norm.sort(key=lambda x: x.get("id") or 0)

    payload = {
        "harbor": snapshot.get("harbor_hostname"),
        "count": snapshot.get("drydock_count", 0),
        "drydocks": docs_norm,
        "clarifications": clar_norm,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_state(path: Path) -> SignatureState:
    """Read persisted state. Missing file → empty state (next call
    won't dedupe, which is fine — first tick is always a real call)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SignatureState()
    return SignatureState(
        last_signature=str(data.get("last_signature", "")),
        last_real_call_unix=float(data.get("last_real_call_unix", 0.0)),
    )


def save_state(path: Path, state: SignatureState) -> None:
    """Best-effort persist. Failure here doesn't break the watch loop —
    next tick will just lack the dedup hint and do a real call."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "last_signature": state.last_signature,
                "last_real_call_unix": state.last_real_call_unix,
            }),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("signature: failed to persist state to %s: %s", path, exc)


def should_skip_llm(
    state: SignatureState,
    new_signature: str,
    *,
    now_unix: float | None = None,
    force_refresh_seconds: float = FORCE_REFRESH_SECONDS,
) -> bool:
    """True iff we should skip the LLM call this tick.

    Skip rules:
      - signature matches previous tick AND
      - last real LLM call was < force_refresh_seconds ago

    Otherwise, do a real call.
    """
    if not state.last_signature or state.last_signature != new_signature:
        return False
    now = now_unix if now_unix is not None else time.time()
    if (now - state.last_real_call_unix) >= force_refresh_seconds:
        # Floor — even if nothing changed, re-evaluate periodically
        return False
    return True
