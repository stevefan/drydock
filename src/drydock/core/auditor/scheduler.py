"""Adaptive cadence scheduler for the Auditor watch loop (Phase PA1).

Per Steven's V1 architecture decision (project_auditor_v1_decisions.md):

  | Activity signal                                  | Cadence |
  |--------------------------------------------------|---------|
  | Open Telegram thread (last 10 min)               | 1 min   |
  | Active workload registration                     | 1 min   |
  | Recent broker activity (last 10 min audit events)| 2 min   |
  | Default                                          | 5 min   |
  | Night (02:00-06:00 local) AND no other signals   | 15 min  |
  | Sustained quiet (no audit events in 1h+)         | 15 min  |

The signal source for activity is the audit log itself — Authority's
emissions. No separate signal channel needed.

This scheduler is decision-only (decide cadence). The daemon module
runs watch_once + sleeps for that duration. Separating decision from
execution keeps the cadence logic deterministic + testable without
spinning up real LLM calls.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from drydock.core.audit import DEFAULT_LOG_PATH as AUDIT_LOG_PATH


# Cadence values in seconds, named to match the design table.
CADENCE_RESPONSIVE = 60       # 1 min
CADENCE_NEAR_RESPONSIVE = 120  # 2 min
CADENCE_DEFAULT = 300         # 5 min
CADENCE_QUIET = 900           # 15 min

# Signal lookback windows
RECENT_BROKER_ACTIVITY_WINDOW = timedelta(minutes=10)
SUSTAINED_QUIET_WINDOW = timedelta(hours=1)

# Night-time defaults (principal-local). Override with --night-start / --night-end.
DEFAULT_NIGHT_START = time(2, 0)
DEFAULT_NIGHT_END = time(6, 0)

# Telegram-thread heuristic: a recent message-out file. The Auditor (or any
# alerting code) touches this whenever it sends to principal; we use it as
# proxy for "principal is currently engaged with Auditor."
TELEGRAM_THREAD_PROXY = Path.home() / ".drydock" / "auditor" / "last_telegram_send"


def is_night(now: datetime, start: time = DEFAULT_NIGHT_START,
             end: time = DEFAULT_NIGHT_END) -> bool:
    """True if `now`'s local time falls in [start, end). Tolerates wraps."""
    t = now.astimezone().time()
    if start <= end:
        return start <= t < end
    # Wraps midnight (e.g. 22:00 → 06:00)
    return t >= start or t < end


def has_recent_broker_activity(
    *,
    now: datetime,
    window: timedelta = RECENT_BROKER_ACTIVITY_WINDOW,
    audit_path: Path | None = None,
) -> bool:
    """True if the audit log has any event in the recent window.

    Cheap check — just scans the tail of audit.log (a JSONL file). Empty
    log or missing log returns False (treated as 'quiet').
    """
    p = audit_path or AUDIT_LOG_PATH
    if not p.exists():
        return False
    cutoff = now - window
    try:
        # Read last ~100KB of the file — enough to find recent events
        # without loading the whole log.
        size = p.stat().st_size
        offset = max(0, size - 100_000)
        with p.open("rb") as f:
            f.seek(offset)
            chunk = f.read().decode("utf-8", errors="replace")
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = rec.get("timestamp") or rec.get("ts")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                return True
    except OSError:
        return False
    return False


def has_sustained_quiet(
    *,
    now: datetime,
    window: timedelta = SUSTAINED_QUIET_WINDOW,
    audit_path: Path | None = None,
) -> bool:
    """True if NO audit events in the past `window` — opposite of activity.

    Used to push cadence toward the slow end. False if the audit log is
    missing entirely (interpretation: not enough info to call it quiet).
    """
    p = audit_path or AUDIT_LOG_PATH
    if not p.exists():
        return False  # Conservative: don't claim quiet without data
    return not has_recent_broker_activity(now=now, window=window, audit_path=p)


def has_open_telegram_thread(
    *,
    now: datetime,
    window: timedelta = timedelta(minutes=10),
    proxy_path: Path | None = None,
) -> bool:
    """True if the Auditor has sent a Telegram message in the recent window.

    Heuristic for 'principal is currently engaged with Auditor.' Approximate;
    real bidirectional state would require Telegram polling. The Auditor
    touches the proxy file whenever it sends; we read mtime here.
    """
    p = proxy_path or TELEGRAM_THREAD_PROXY
    if not p.exists():
        return False
    last_send = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return (now - last_send) <= window


def has_active_workload(*, registry) -> bool:
    """True if any workload-registration lease is currently active.

    Workload registrations are amendments of kind=workload_register that
    are status='applied' (or in V0, just status='approved') with future
    expires_at. For PA1 we approximate via the leases table — any active
    lease whose type indicates a workload (TBD when amendment-A1 lands).
    Until then, this is a conservative False.
    """
    # Phase A1 will introduce workload_register amendments + bundled leases;
    # for PA1 with no A1 yet, this signal is always False.
    return False


def next_cadence(
    *,
    now: datetime | None = None,
    registry=None,
    audit_path: Path | None = None,
    telegram_proxy_path: Path | None = None,
) -> int:
    """Decide the next watch-loop cadence in seconds.

    Pure decision logic — no I/O beyond signal-checking helpers above.
    Returns one of CADENCE_RESPONSIVE, CADENCE_NEAR_RESPONSIVE,
    CADENCE_DEFAULT, CADENCE_QUIET.

    Order matters: signals are checked from highest-priority (responsive)
    to lowest (quiet); first match wins.
    """
    n = now or datetime.now(timezone.utc)

    # Highest priority: principal engagement OR active workload
    if has_open_telegram_thread(now=n, proxy_path=telegram_proxy_path):
        return CADENCE_RESPONSIVE
    if registry is not None and has_active_workload(registry=registry):
        return CADENCE_RESPONSIVE

    # Next: recent broker activity (10-min window) → near-responsive
    if has_recent_broker_activity(now=n, audit_path=audit_path):
        return CADENCE_NEAR_RESPONSIVE

    # Lowest: sustained quiet OR night-time → quiet cadence
    if is_night(n) or has_sustained_quiet(now=n, audit_path=audit_path):
        return CADENCE_QUIET

    return CADENCE_DEFAULT
