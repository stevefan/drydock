"""Tests for drydock.core.schedule — YAML parsing, cron rendering, launchd plist structure."""

import plistlib
from pathlib import Path

import pytest

from drydock.core import WsError
from drydock.core.schedule import (
    load_schedule,
    parse_cron_5field,
    render_cron_file,
    render_launchd_plist,
    ScheduleJob,
)


# --- YAML parsing: contract boundary (what callers depend on) ---


def test_load_schedule_happy_path(tmp_path):
    """Valid schedule.yaml produces the right jobs list."""
    f = tmp_path / "schedule.yaml"
    f.write_text(
        "jobs:\n"
        "  daily-crawl:\n"
        "    cron: '0 13 * * *'\n"
        "    command: bash deploy/run-daily.sh\n"
        "    log: /var/log/drydock/crawl.log\n"
        "  nightly:\n"
        "    cron: '30 2 * * *'\n"
        "    command: python run.py\n"
    )
    jobs = load_schedule(f)
    assert len(jobs) == 2
    assert jobs[0].name == "daily-crawl"
    assert jobs[0].cron == "0 13 * * *"
    assert jobs[0].command == "bash deploy/run-daily.sh"
    assert jobs[0].log == "/var/log/drydock/crawl.log"
    assert jobs[1].log == ""  # optional, defaults to empty


def test_load_schedule_missing_file(tmp_path):
    """Missing file raises WsError with fix."""
    with pytest.raises(WsError, match="not found"):
        load_schedule(tmp_path / "nope.yaml")


def test_load_schedule_invalid_yaml(tmp_path):
    """Broken YAML raises WsError."""
    f = tmp_path / "schedule.yaml"
    f.write_text("jobs:\n  - broken: [")
    with pytest.raises(WsError, match="Invalid YAML"):
        load_schedule(f)


def test_load_schedule_missing_required_key(tmp_path):
    """Job without 'command' key raises WsError."""
    f = tmp_path / "schedule.yaml"
    f.write_text("jobs:\n  bad:\n    cron: '0 0 * * *'\n")
    with pytest.raises(WsError, match="missing required key 'command'"):
        load_schedule(f)


def test_load_schedule_unknown_job_key(tmp_path):
    """Unknown key in job spec raises WsError."""
    f = tmp_path / "schedule.yaml"
    f.write_text("jobs:\n  j:\n    cron: '0 0 * * *'\n    command: x\n    bogus: y\n")
    with pytest.raises(WsError, match="Unknown keys"):
        load_schedule(f)


def test_load_schedule_no_jobs_key(tmp_path):
    """YAML without 'jobs' top-level key raises WsError."""
    f = tmp_path / "schedule.yaml"
    f.write_text("tasks:\n  a:\n    cron: '0 0 * * *'\n")
    with pytest.raises(WsError, match="must contain a 'jobs' mapping"):
        load_schedule(f)


# --- Cron expression parsing: non-obvious invariant (restricted syntax) ---


def test_parse_cron_5field_all_stars():
    """All wildcards → empty interval dict (every minute)."""
    result = parse_cron_5field("* * * * *")
    assert result == [{}]


def test_parse_cron_5field_specific_values():
    """Specific values map to correct launchd keys."""
    result = parse_cron_5field("0 13 * * *")
    assert result == [{"Minute": 0, "Hour": 13}]


def test_parse_cron_5field_rejects_step():
    """Step syntax (*/5) is rejected."""
    with pytest.raises(WsError, match="Unsupported cron syntax"):
        parse_cron_5field("*/5 * * * *")


def test_parse_cron_5field_rejects_range():
    """Range syntax (1-5) is rejected."""
    with pytest.raises(WsError, match="Unsupported cron syntax"):
        parse_cron_5field("* 1-5 * * *")


def test_parse_cron_5field_rejects_list():
    """List syntax (1,3,5) is rejected."""
    with pytest.raises(WsError, match="Unsupported cron syntax"):
        parse_cron_5field("1,3,5 * * * *")


def test_parse_cron_5field_wrong_field_count():
    """Wrong number of fields raises WsError."""
    with pytest.raises(WsError, match="exactly 5 fields"):
        parse_cron_5field("0 13 *")


# --- Cron renderer: snapshot test (contract for output format) ---


def test_render_cron_file_snapshot():
    """Cron file has provenance comment, PATH header, correct job lines."""
    jobs = [
        ScheduleJob(name="crawl", cron="0 13 * * *", command="bash run.sh", log="/var/log/drydock/c.log"),
        ScheduleJob(name="op", cron="37 13 * * *", command="python op.py", log=""),
    ]
    rendered = render_cron_file("mydesk", jobs)
    lines = rendered.splitlines()
    assert lines[0].startswith("# Managed by drydock")
    assert "desk=mydesk" in lines[0]
    assert lines[1].startswith("PATH=")
    assert "0 13 * * * root /usr/local/bin/ws exec mydesk -- bash run.sh >> /var/log/drydock/c.log 2>&1" in rendered
    assert "37 13 * * * root /usr/local/bin/ws exec mydesk -- python op.py" in rendered
    # No-log job should NOT have redirect
    op_line = [l for l in lines if "python op.py" in l][0]
    assert ">>" not in op_line


# --- Launchd plist: structure test (contract for what launchctl expects) ---


def test_render_launchd_plist_structure():
    """Plist contains Label, ProgramArguments, StartCalendarInterval."""
    job = ScheduleJob(name="crawl", cron="0 13 * * *", command="bash deploy/run.sh", log="/tmp/out.log")
    data = plistlib.loads(render_launchd_plist("desk1", job))
    assert data["Label"] == "com.drydock.desk1.crawl"
    assert data["ProgramArguments"] == ["/usr/local/bin/ws", "exec", "desk1", "--", "bash", "deploy/run.sh"]
    assert data["StartCalendarInterval"] == {"Minute": 0, "Hour": 13}
    assert data["StandardOutPath"] == "/tmp/out.log"
    assert data["StandardErrorPath"] == "/tmp/out.log"


def test_render_launchd_plist_no_log():
    """Plist without log omits StandardOutPath/StandardErrorPath."""
    job = ScheduleJob(name="j", cron="* * * * *", command="echo hi", log="")
    data = plistlib.loads(render_launchd_plist("d", job))
    assert "StandardOutPath" not in data
    assert "StandardErrorPath" not in data


# --- Security edge cases (found during review 2026-04-16) ---

def test_render_cron_file_log_path_shell_quoted():
    """Log paths must be shell-quoted to prevent command injection.

    Regression: a malicious schedule.yaml with `log: "/tmp/x; rm -rf /"` must
    not produce an executable shell fragment in the cron line.
    """
    job = ScheduleJob(name="j", cron="0 13 * * *", command="bash run.sh",
                      log="/tmp/x; rm -rf /; echo")
    content = render_cron_file("desk", [job])
    # The log path must be single-quoted by shlex.quote
    assert "'/tmp/x; rm -rf /; echo'" in content
    # The UNQUOTED injection string must NOT appear
    assert ">> /tmp/x; rm -rf /" not in content


def test_load_schedule_rejects_invalid_job_name(tmp_path):
    """Job names with path traversal characters must be rejected."""
    schedule = tmp_path / "schedule.yaml"
    schedule.write_text(
        "jobs:\n"
        "  ../../etc/passwd:\n"
        "    cron: '0 13 * * *'\n"
        "    command: echo pwned\n"
    )
    with pytest.raises(WsError) as exc_info:
        load_schedule(schedule)
    assert "Invalid job name" in exc_info.value.message


def test_load_schedule_rejects_job_name_with_spaces(tmp_path):
    """Job names with spaces would break cron and launchd labels."""
    schedule = tmp_path / "schedule.yaml"
    schedule.write_text(
        "jobs:\n"
        "  'my job':\n"
        "    cron: '0 13 * * *'\n"
        "    command: echo hi\n"
    )
    with pytest.raises(WsError) as exc_info:
        load_schedule(schedule)
    assert "Invalid job name" in exc_info.value.message
