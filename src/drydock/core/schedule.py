"""Schedule primitives — translate deploy/schedule.yaml to host-native cron/launchd."""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import WsError

KNOWN_JOB_KEYS = {"cron", "command", "log"}
# Job names land in filesystem paths and launchd labels. Restrict to safe characters.
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class ScheduleJob:
    name: str
    cron: str
    command: str
    log: str


def load_schedule(schedule_path: Path) -> list[ScheduleJob]:
    """Parse deploy/schedule.yaml and return validated jobs."""
    if not schedule_path.exists():
        raise WsError(
            message=f"Schedule file not found: {schedule_path}",
            fix=f"Create {schedule_path} with a 'jobs:' mapping.",
        )

    try:
        raw = yaml.safe_load(schedule_path.read_text())
    except yaml.YAMLError as e:
        raise WsError(
            message=f"Invalid YAML in {schedule_path}: {e}",
            fix=f"Check {schedule_path} for syntax errors.",
        )

    if not isinstance(raw, dict) or "jobs" not in raw:
        raise WsError(
            message=f"Schedule file {schedule_path} must contain a 'jobs' mapping",
            fix=f"Add a top-level 'jobs:' key to {schedule_path}.",
        )

    jobs_raw = raw["jobs"]
    if not isinstance(jobs_raw, dict):
        raise WsError(
            message=f"'jobs' in {schedule_path} must be a mapping",
            fix=f"Each entry under 'jobs:' should be a named job with cron/command/log keys.",
        )

    jobs: list[ScheduleJob] = []
    for job_name, spec in jobs_raw.items():
        job_name = str(job_name)
        if not _JOB_NAME_RE.match(job_name):
            raise WsError(
                message=f"Invalid job name '{job_name}': must match [A-Za-z0-9_-]{{1,64}}",
                fix="Use only letters, digits, hyphens, and underscores in job names.",
            )
        if not isinstance(spec, dict):
            raise WsError(
                message=f"Job '{job_name}' must be a mapping, got {type(spec).__name__}",
                fix=f"Define '{job_name}' with keys: cron, command, log.",
            )

        unknown = set(spec.keys()) - KNOWN_JOB_KEYS
        if unknown:
            raise WsError(
                message=f"Unknown keys in job '{job_name}': {', '.join(sorted(unknown))}",
                fix=f"Valid job keys: {', '.join(sorted(KNOWN_JOB_KEYS))}",
            )

        for key in ("cron", "command"):
            if key not in spec:
                raise WsError(
                    message=f"Job '{job_name}' missing required key '{key}'",
                    fix=f"Add '{key}:' to job '{job_name}' in {schedule_path}.",
                )

        cron_expr = str(spec["cron"]).strip()
        parse_cron_5field(cron_expr)  # validates or raises WsError

        jobs.append(ScheduleJob(
            name=str(job_name),
            cron=cron_expr,
            command=str(spec["command"]),
            log=str(spec.get("log", "")),
        ))

    return jobs


def detect_backend() -> str:
    """Return 'launchd' on macOS, 'cron' on Linux."""
    if sys.platform == "darwin":
        return "launchd"
    return "cron"


# Restricted cron parser: only * and integers.
_CRON_FIELD_RE = re.compile(r"^(\*|\d+)$")

CRON_FIELD_NAMES = ("minute", "hour", "day", "month", "weekday")
LAUNCHD_FIELD_KEYS = ("Minute", "Hour", "Day", "Month", "Weekday")


def parse_cron_5field(expr: str) -> list[dict]:
    """Parse a restricted 5-field cron expression into calendar interval dicts.

    Only ``*`` (wildcard) and plain integers are accepted. Step (``*/5``),
    range (``1-5``), and list (``1,3,5``) syntax is rejected with a WsError.

    Returns a list with one dict mapping launchd StartCalendarInterval keys
    to integer values (wildcards are omitted).
    """
    fields = expr.split()
    if len(fields) != 5:
        raise WsError(
            message=f"Cron expression must have exactly 5 fields, got {len(fields)}: '{expr}'",
            fix="Use the format: minute hour day month weekday (e.g. '0 13 * * *').",
        )

    interval: dict[str, int] = {}
    for i, (raw, name, key) in enumerate(zip(fields, CRON_FIELD_NAMES, LAUNCHD_FIELD_KEYS)):
        if not _CRON_FIELD_RE.match(raw):
            raise WsError(
                message=f"Unsupported cron syntax in {name} field: '{raw}'",
                fix=f"Only '*' and plain integers are supported (no step/range/list). Got '{raw}' in '{expr}'.",
            )
        if raw != "*":
            interval[key] = int(raw)

    return [interval]


def _render_job_shell(desk: str, job: ScheduleJob) -> str:
    """Build the shell expression that runs one job and records its
    outcome. Single-line, / bin / sh-safe. Used by both cron and
    (wrapped in sh -c) launchd renderers.

    Shape:
        ws exec DESK -- CMD [>> LOG 2>&1] ; ec=$? ;
        ws deskwatch-record DESK job_run NAME ok|failed --detail "exit $ec" ;
        exit $ec
    """
    safe_desk = shlex.quote(desk)
    safe_name = shlex.quote(job.name)
    tail = f" >> {shlex.quote(job.log)} 2>&1" if job.log else ""
    return (
        f"/usr/local/bin/ws exec {safe_desk} -- {job.command}{tail}; "
        f"ec=$?; "
        f"/usr/local/bin/ws deskwatch-record {safe_desk} job_run {safe_name} "
        f"$([ $ec -eq 0 ] && echo ok || echo failed) --detail \"exit $ec\"; "
        f"exit $ec"
    )


def render_cron_file(desk: str, jobs: list[ScheduleJob]) -> str:
    """Render a cron file for /etc/cron.d/drydock-<desk>.

    Each line wraps the job command so its outcome is recorded via
    `ws deskwatch-record`. The wrapper preserves the original exit code
    (cron reports failures via MAILTO as before) and adds a deskwatch
    event regardless of whether the desk declares deskwatch expectations
    — the history is free, expectations gate only the `ws deskwatch`
    evaluation.

    Log paths are shell-quoted to prevent command injection — a malicious
    schedule.yaml with `log: "/tmp/x; rm -rf /"` must not be executable.
    The command field is intentionally NOT quoted (it's a shell command
    by design), but the desk name, job name, and log path are
    user-controlled strings that land in a cron line interpreted by
    /bin/sh.
    """
    lines = [
        f"# Managed by drydock — do not edit. desk={desk}",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "",
    ]
    for job in jobs:
        lines.append(f"{job.cron} root {_render_job_shell(desk, job)}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def render_launchd_plist(desk: str, job: ScheduleJob) -> bytes:
    """Render a launchd plist for one job.

    Wraps the command in `sh -c` so we can tack on the deskwatch-record
    call (same shape as the cron wrapper in `_render_job_shell`).
    StandardOut/ErrorPath are still set so the existing log tailing
    flow keeps working; the shell wrapper doesn't redirect since
    launchd handles it at the plist level.
    """
    intervals = parse_cron_5field(job.cron)

    # Launchd doesn't chain commands natively — wrap in sh -c.
    safe_desk = shlex.quote(desk)
    safe_name = shlex.quote(job.name)
    program_args = [
        "/bin/sh", "-c",
        (
            f"/usr/local/bin/ws exec {safe_desk} -- {job.command}; "
            f"ec=$?; "
            f"/usr/local/bin/ws deskwatch-record {safe_desk} job_run {safe_name} "
            f"$([ $ec -eq 0 ] && echo ok || echo failed) --detail \"exit $ec\"; "
            f"exit $ec"
        ),
    ]

    plist: dict = {
        "Label": f"com.drydock.{desk}.{job.name}",
        "ProgramArguments": program_args,
        "StartCalendarInterval": intervals[0] if len(intervals) == 1 else intervals,
    }

    if job.log:
        plist["StandardOutPath"] = job.log
        plist["StandardErrorPath"] = job.log

    return plistlib.dumps(plist)


def _cron_file_path(desk: str) -> Path:
    return Path(f"/etc/cron.d/drydock-{desk}")


def _launchd_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_label(desk: str, job_name: str) -> str:
    return f"com.drydock.{desk}.{job_name}"


def _launchd_plist_path(desk: str, job_name: str) -> Path:
    return _launchd_dir() / f"{_launchd_label(desk, job_name)}.plist"


def install_cron(desk: str, jobs: list[ScheduleJob]) -> Path:
    """Write /etc/cron.d/drydock-<desk>. Returns the path written."""
    path = _cron_file_path(desk)
    content = render_cron_file(desk, jobs)
    try:
        path.write_text(content)
        os.chmod(path, 0o644)
    except PermissionError:
        raise WsError(
            message=f"Cannot write {path} — permission denied",
            fix=f"Run with appropriate permissions or use sudo to write to {path.parent}.",
        )
    return path


def install_launchd(desk: str, jobs: list[ScheduleJob]) -> list[Path]:
    """Write per-job plists and remove stale ones. Returns paths written."""
    agent_dir = _launchd_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)

    wanted_labels = {_launchd_label(desk, j.name) for j in jobs}

    # Remove stale plists for this desk
    prefix = f"com.drydock.{desk}."
    for existing in agent_dir.glob(f"{prefix}*.plist"):
        label = existing.stem
        if label not in wanted_labels:
            existing.unlink()

    # Write current plists
    written: list[Path] = []
    for job in jobs:
        plist_path = _launchd_plist_path(desk, job.name)
        plist_data = render_launchd_plist(desk, job)
        plist_path.write_bytes(plist_data)
        written.append(plist_path)

    return written


def remove_cron(desk: str) -> Path | None:
    """Remove the cron file for a desk. Returns path if it existed."""
    path = _cron_file_path(desk)
    if path.exists():
        try:
            path.unlink()
        except PermissionError:
            raise WsError(
                message=f"Cannot remove {path} — permission denied",
                fix=f"Run with appropriate permissions or use sudo.",
            )
        return path
    return None


def remove_launchd(desk: str) -> list[Path]:
    """Remove all launchd plists for a desk. Returns paths removed."""
    agent_dir = _launchd_dir()
    if not agent_dir.exists():
        return []

    prefix = f"com.drydock.{desk}."
    removed: list[Path] = []
    for plist in agent_dir.glob(f"{prefix}*.plist"):
        plist.unlink()
        removed.append(plist)
    return removed


def list_installed_cron(desk: str) -> list[str]:
    """Return raw lines from the installed cron file for a desk, or []."""
    path = _cron_file_path(desk)
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line and not line.startswith("#") and not line.startswith("PATH=")]


def list_installed_launchd(desk: str) -> list[str]:
    """Return labels of installed launchd plists for a desk."""
    agent_dir = _launchd_dir()
    if not agent_dir.exists():
        return []
    prefix = f"com.drydock.{desk}."
    return [p.stem for p in agent_dir.glob(f"{prefix}*.plist")]
