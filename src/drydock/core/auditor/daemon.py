"""Auditor watch-loop daemon (Phase PA1).

Long-running process: forever, decide cadence (adaptive per
scheduler.next_cadence), call watch_once, sleep, repeat. The daemon
itself is dumb — all the smart parts live in watch_once + scheduler.

Run via:
    ws auditor watch-loop                       # foreground
    ws auditor watch-loop --max-iterations N    # bounded (test/debug)

For production, deploy under systemd (Linux) or launchd (Mac); the
units are TODO — for now just run with `ws auditor watch-loop &`
or under nohup. Logging goes to wsd's standard logging facility
(stderr by default).

Failure modes:
- KeyboardInterrupt: graceful exit
- LLM unavailable repeatedly: each watch_once returns error verdict;
  heartbeat stops updating; deadman fires (separately)
- Registry unreachable: re-raises; daemon dies; supervisor (systemd
  Restart=on-failure) brings it back. The init system IS the
  supervision-of-the-supervisor.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field

from .scheduler import next_cadence
from .watch import WatchVerdict, watch_once

logger = logging.getLogger(__name__)


@dataclass
class DaemonStats:
    iterations: int = 0
    last_verdict: str = ""
    last_tick_at: str = ""
    consecutive_errors: int = 0
    cadences_chosen: list[int] = field(default_factory=list)


def run(
    *,
    registry,
    max_iterations: int | None = None,
    sleep_fn=time.sleep,
    next_cadence_fn=next_cadence,
    watch_once_fn=watch_once,
    on_iteration_complete=None,
) -> DaemonStats:
    """Run the watch loop.

    Returns DaemonStats summarizing the run (mostly useful for tests +
    bounded runs). For unbounded production use, this returns only on
    KeyboardInterrupt.

    Parameters are injectable for testing:
    - sleep_fn: replaced with no-op or fast-forward in tests
    - next_cadence_fn: replaced to control cadence selection
    - watch_once_fn: replaced with mock to skip real LLM calls
    - on_iteration_complete: callback(verdict, cadence) per iteration

    The daemon is intentionally simple — it does NOT:
    - retry watch_once on errors (single attempt; verdict='error' is
      still recorded; the deadman is the failure-detector)
    - escalate to deep analysis when verdict='anomaly_suspected'
      (that's PA2; until PA2 lands, the verdict is logged-only)
    - touch the heartbeat itself (watch_once does that on LLM-reachable)

    Each iteration is independent. State accumulation lives in the
    watch_log JSONL + the snapshot files.
    """
    stats = DaemonStats()
    stop_requested = False

    def _sigterm_handler(signum, frame):
        nonlocal stop_requested
        logger.info("auditor daemon: received signal %s; stopping after current iter", signum)
        stop_requested = True

    # Install graceful-shutdown handlers for systemd/launchd-style stop.
    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
        signal.signal(signal.SIGINT, _sigterm_handler)
    except ValueError:
        # signal.signal can only be called from the main thread; fall back
        # to KeyboardInterrupt-only in non-main-thread contexts (tests).
        pass

    logger.info("auditor daemon: starting")

    while True:
        try:
            verdict = watch_once_fn(registry=registry)
            stats.iterations += 1
            stats.last_verdict = verdict.verdict
            stats.last_tick_at = verdict.tick_at
            if verdict.verdict == "error":
                stats.consecutive_errors += 1
                if stats.consecutive_errors == 1 or stats.consecutive_errors % 10 == 0:
                    logger.warning(
                        "auditor daemon: tick %d returned error: %s "
                        "(consecutive_errors=%d)",
                        stats.iterations, verdict.error or verdict.reason,
                        stats.consecutive_errors,
                    )
            else:
                if stats.consecutive_errors > 0:
                    logger.info(
                        "auditor daemon: recovered after %d consecutive errors",
                        stats.consecutive_errors,
                    )
                stats.consecutive_errors = 0
                logger.info(
                    "auditor daemon: tick %d verdict=%s reason=%r",
                    stats.iterations, verdict.verdict, verdict.reason[:80],
                )

            cadence = next_cadence_fn(registry=registry)
            stats.cadences_chosen.append(cadence)

            if on_iteration_complete is not None:
                on_iteration_complete(verdict, cadence)

            if max_iterations is not None and stats.iterations >= max_iterations:
                logger.info(
                    "auditor daemon: reached max_iterations=%d; stopping",
                    max_iterations,
                )
                break

            if stop_requested:
                break

            logger.debug("auditor daemon: sleeping %ds before next tick", cadence)
            sleep_fn(cadence)

            if stop_requested:
                break

        except KeyboardInterrupt:
            logger.info("auditor daemon: KeyboardInterrupt; stopping")
            break
        except Exception:
            # Re-raise unexpected — let supervisor restart us.
            logger.exception("auditor daemon: unexpected exception; exiting")
            raise

    logger.info(
        "auditor daemon: stopped after %d iteration(s) "
        "(consecutive_errors=%d, last_verdict=%s)",
        stats.iterations, stats.consecutive_errors, stats.last_verdict,
    )
    return stats
