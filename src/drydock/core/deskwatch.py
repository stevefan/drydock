"""Deskwatch — workload health layer.

Observes (does not repair) whether a Dock's declared workload is
actually doing its job:

- **Scheduled jobs**: last success within `expect_success_within` of
  now. Populated by scheduler wrappers calling
  `Registry.record_deskwatch_event(..., kind='job_run', ...)`.
- **Outputs**: files inside the container whose mtime should advance
  on some cadence. Probed live via `docker exec stat`.
- **Probes**: lightweight one-shot commands that return 0 when
  healthy. Probed live and recorded.

Project YAML:

    deskwatch:
      jobs:
        - name: daily-crawl
          expect_success_within: 25h
      outputs:
        - path: /workspace/data/auction_crawl.db
          max_age: 25h
          may_be_empty: true
      probes:
        - name: db-readable
          cmd: "test -r /workspace/data/auction_crawl.db"
          interval: 1h

All three sections optional. A desk without a `deskwatch:` block is
not evaluated (result: `{"checks": [], "healthy": true}`).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import WsError


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str | int) -> timedelta:
    """Parse '25h' / '30m' / '1d' / '600' → timedelta.

    Bare integers are interpreted as seconds.
    """
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    match = _DURATION_RE.match(str(value))
    if not match:
        raise WsError(
            f"Invalid duration: {value!r}",
            fix="Use forms like '25h', '30m', '1d', or bare seconds",
            code="invalid_duration",
        )
    n, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(seconds=n * _DURATION_UNITS[unit])


def format_age(delta: timedelta) -> str:
    """Human-friendly age: '4d 2h', '6h', '45m', '30s'."""
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        h, m = divmod(total // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(total, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


# ---------------------------------------------------------------------------
# Configuration model (loaded from project YAML)
# ---------------------------------------------------------------------------


@dataclass
class JobExpectation:
    name: str
    expect_success_within: timedelta


@dataclass
class OutputExpectation:
    path: str
    max_age: timedelta
    may_be_empty: bool = False


@dataclass
class ProbeExpectation:
    name: str
    cmd: str
    interval: timedelta


@dataclass
class DeskwatchConfig:
    jobs: list[JobExpectation] = field(default_factory=list)
    outputs: list[OutputExpectation] = field(default_factory=list)
    probes: list[ProbeExpectation] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.jobs or self.outputs or self.probes)


def parse_deskwatch_config(raw: dict | None) -> DeskwatchConfig:
    """Build a DeskwatchConfig from the project YAML's `deskwatch:` block.

    Raises WsError on malformed input so mistakes are loud at reload time,
    not silent at evaluation.
    """
    if not raw:
        return DeskwatchConfig()

    jobs: list[JobExpectation] = []
    for item in raw.get("jobs") or []:
        if not isinstance(item, dict) or "name" not in item:
            raise WsError(
                "deskwatch.jobs entries must be dicts with at least 'name'",
                fix="Example: - name: daily-crawl\\n          expect_success_within: 25h",
                code="deskwatch_jobs_invalid",
            )
        jobs.append(JobExpectation(
            name=item["name"],
            expect_success_within=parse_duration(item.get("expect_success_within", "25h")),
        ))

    outputs: list[OutputExpectation] = []
    for item in raw.get("outputs") or []:
        if not isinstance(item, dict) or "path" not in item:
            raise WsError(
                "deskwatch.outputs entries must be dicts with at least 'path'",
                fix="Example: - path: /workspace/data/out.db\\n          max_age: 25h",
                code="deskwatch_outputs_invalid",
            )
        outputs.append(OutputExpectation(
            path=item["path"],
            max_age=parse_duration(item.get("max_age", "25h")),
            may_be_empty=bool(item.get("may_be_empty", False)),
        ))

    probes: list[ProbeExpectation] = []
    for item in raw.get("probes") or []:
        if not isinstance(item, dict) or "name" not in item or "cmd" not in item:
            raise WsError(
                "deskwatch.probes entries must have 'name' and 'cmd'",
                fix="Example: - name: db-readable\\n          cmd: 'test -r /path'",
                code="deskwatch_probes_invalid",
            )
        probes.append(ProbeExpectation(
            name=item["name"],
            cmd=item["cmd"],
            interval=parse_duration(item.get("interval", "1h")),
        ))

    return DeskwatchConfig(jobs=jobs, outputs=outputs, probes=probes)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """One health check result (for serialization / display)."""
    kind: str           # 'job', 'output', 'probe'
    name: str           # job name / path / probe name
    healthy: bool
    detail: str         # human-readable explanation
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "healthy": self.healthy,
            "detail": self.detail,
            **self.raw,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime:
    # Accept 'Z' shorthand and naive strings.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate_jobs(
    registry,
    desk_id: str,
    jobs: list[JobExpectation],
    now: datetime | None = None,
) -> list[Check]:
    now = now or _utcnow()
    checks: list[Check] = []
    for job in jobs:
        last = registry.last_deskwatch_event(desk_id, "job_run", job.name)
        if last is None:
            checks.append(Check(
                kind="job", name=job.name, healthy=False,
                detail=f"no run on record; expected within {format_age(job.expect_success_within)}",
                raw={"last_run": None, "expect_success_within": int(job.expect_success_within.total_seconds())},
            ))
            continue
        last_ts = _parse_ts(last["timestamp"])
        age = now - last_ts
        last_ok = last["status"] == "ok"
        within = age <= job.expect_success_within
        healthy = last_ok and within
        if not last_ok:
            detail = f"last run {format_age(age)} ago → {last['status']} ({last.get('detail') or 'no detail'})"
        elif not within:
            detail = f"last success {format_age(age)} ago, exceeds {format_age(job.expect_success_within)}"
        else:
            detail = f"last success {format_age(age)} ago, within {format_age(job.expect_success_within)}"
        checks.append(Check(
            kind="job", name=job.name, healthy=healthy, detail=detail,
            raw={
                "last_run": last["timestamp"],
                "last_status": last["status"],
                "age_seconds": int(age.total_seconds()),
                "expect_success_within": int(job.expect_success_within.total_seconds()),
            },
        ))
    return checks


def _docker_exec_probe(container_id: str, argv: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container_id, *argv],
        capture_output=True, text=True, timeout=timeout,
    )


def evaluate_outputs(
    container_id: str,
    outputs: list[OutputExpectation],
    now: datetime | None = None,
) -> list[Check]:
    """Probe file mtimes via `docker exec stat`. No container → every
    check is unhealthy/missing (can't verify)."""
    now = now or _utcnow()
    checks: list[Check] = []
    for out in outputs:
        if not container_id:
            checks.append(Check(
                kind="output", name=out.path, healthy=False,
                detail="container not running; cannot check",
                raw={"path": out.path},
            ))
            continue
        try:
            result = _docker_exec_probe(
                container_id,
                ["stat", "-c", "%Y %s", out.path],
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            checks.append(Check(
                kind="output", name=out.path, healthy=False,
                detail=f"stat failed: {exc}",
                raw={"path": out.path},
            ))
            continue
        if result.returncode != 0:
            checks.append(Check(
                kind="output", name=out.path, healthy=False,
                detail="missing",
                raw={"path": out.path, "missing": True},
            ))
            continue
        try:
            mtime_epoch, size = result.stdout.split()
            mtime = datetime.fromtimestamp(int(mtime_epoch), tz=timezone.utc)
            size = int(size)
        except ValueError:
            checks.append(Check(
                kind="output", name=out.path, healthy=False,
                detail=f"unparseable stat output: {result.stdout.strip()!r}",
                raw={"path": out.path},
            ))
            continue
        age = now - mtime
        if size == 0 and not out.may_be_empty:
            checks.append(Check(
                kind="output", name=out.path, healthy=False,
                detail=f"empty file (may_be_empty: false)",
                raw={"path": out.path, "size": 0, "age_seconds": int(age.total_seconds())},
            ))
            continue
        if age > out.max_age:
            checks.append(Check(
                kind="output", name=out.path, healthy=False,
                detail=f"age {format_age(age)} exceeds {format_age(out.max_age)}",
                raw={"path": out.path, "size": size, "age_seconds": int(age.total_seconds()),
                     "max_age_seconds": int(out.max_age.total_seconds())},
            ))
            continue
        checks.append(Check(
            kind="output", name=out.path, healthy=True,
            detail=f"fresh ({format_age(age)} old, {size} bytes)",
            raw={"path": out.path, "size": size, "age_seconds": int(age.total_seconds())},
        ))
    return checks


def evaluate_probes(
    registry,
    desk_id: str,
    container_id: str,
    probes: list[ProbeExpectation],
    now: datetime | None = None,
    force_rerun: bool = False,
) -> list[Check]:
    """Run any probe whose last result is older than `interval` (or
    missing); re-use recent results. Each run is recorded for later
    inspection.

    `force_rerun=True` (from `ws deskwatch --scan`) ignores interval
    gating and re-runs every probe, useful for interactive diagnosis.
    """
    now = now or _utcnow()
    checks: list[Check] = []
    for probe in probes:
        last = registry.last_deskwatch_event(desk_id, "probe_result", probe.name)
        should_run = True
        if not force_rerun and last is not None:
            last_ts = _parse_ts(last["timestamp"])
            if now - last_ts < probe.interval:
                should_run = False

        if should_run:
            if not container_id:
                registry.record_deskwatch_event(
                    desk_id, "probe_result", probe.name, "missing",
                    detail="container not running",
                )
                checks.append(Check(
                    kind="probe", name=probe.name, healthy=False,
                    detail="container not running",
                    raw={"cmd": probe.cmd},
                ))
                continue
            try:
                result = _docker_exec_probe(container_id, ["sh", "-lc", probe.cmd])
                status = "ok" if result.returncode == 0 else "failed"
                detail = f"exit {result.returncode}"
                if result.stderr.strip():
                    detail += f": {result.stderr.strip()[:120]}"
            except (subprocess.TimeoutExpired, OSError) as exc:
                status = "failed"
                detail = f"probe exception: {exc}"
            registry.record_deskwatch_event(
                desk_id, "probe_result", probe.name, status, detail=detail,
            )
            last = registry.last_deskwatch_event(desk_id, "probe_result", probe.name)

        last_ts = _parse_ts(last["timestamp"])
        age = now - last_ts
        healthy = last["status"] == "ok"
        detail = f"{last['status']} ({format_age(age)} ago)"
        if last.get("detail"):
            detail += f" — {last['detail']}"
        checks.append(Check(
            kind="probe", name=probe.name, healthy=healthy, detail=detail,
            raw={
                "cmd": probe.cmd,
                "last_status": last["status"],
                "age_seconds": int(age.total_seconds()),
            },
        ))
    return checks


def evaluate_desk(
    registry,
    ws,
    container_id: str,
    config: DeskwatchConfig,
    now: datetime | None = None,
    force_rerun_probes: bool = False,
) -> dict:
    """Full evaluation. Returns {'checks': [...], 'healthy': bool,
    'violations': int}."""
    now = now or _utcnow()
    checks: list[Check] = []
    checks.extend(evaluate_jobs(registry, ws.id, config.jobs, now=now))
    checks.extend(evaluate_outputs(container_id, config.outputs, now=now))
    checks.extend(evaluate_probes(
        registry, ws.id, container_id, config.probes,
        now=now, force_rerun=force_rerun_probes,
    ))
    violations = sum(1 for c in checks if not c.healthy)
    return {
        "desk": ws.name,
        "desk_id": ws.id,
        "evaluated_at": now.isoformat(),
        "checks": [c.to_dict() for c in checks],
        "violations": violations,
        "healthy": violations == 0,
    }
