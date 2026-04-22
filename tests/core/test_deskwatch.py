"""Deskwatch: duration parsing, YAML model, evaluation contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from drydock.core import WsError
from drydock.core.deskwatch import (
    DeskwatchConfig,
    JobExpectation,
    OutputExpectation,
    ProbeExpectation,
    evaluate_jobs,
    evaluate_outputs,
    evaluate_probes,
    format_age,
    parse_deskwatch_config,
    parse_duration,
)


# --- parse_duration: what YAML authors actually type ------------------------


@pytest.mark.parametrize("value,expected_seconds", [
    ("30s", 30),
    ("5m", 300),
    ("1h", 3600),
    ("25h", 90000),
    ("1d", 86400),
    ("2d", 172800),
    (" 1h ", 3600),   # whitespace tolerance
    ("1H", 3600),     # case tolerance
    (600, 600),       # bare int → seconds
])
def test_parse_duration_accepts_common_forms(value, expected_seconds):
    assert parse_duration(value).total_seconds() == expected_seconds


@pytest.mark.parametrize("bad", ["", "abc", "10", "10 hours", "1w", "-1h"])
def test_parse_duration_rejects_malformed(bad):
    with pytest.raises(WsError, match="Invalid duration"):
        parse_duration(bad)


def test_format_age_human_forms():
    assert format_age(timedelta(seconds=30)) == "30s"
    assert format_age(timedelta(minutes=45)) == "45m"
    assert format_age(timedelta(hours=6)) == "6h"
    assert format_age(timedelta(hours=6, minutes=30)) == "6h 30m"
    assert format_age(timedelta(days=4, hours=2)) == "4d 2h"


# --- parse_deskwatch_config: YAML → dataclass ------------------------------


def test_parse_empty_yields_empty_config():
    c = parse_deskwatch_config({})
    assert c.is_empty
    c2 = parse_deskwatch_config(None)
    assert c2.is_empty


def test_parse_jobs_outputs_probes_full_cycle():
    raw = {
        "jobs": [{"name": "daily-crawl", "expect_success_within": "25h"}],
        "outputs": [{"path": "/workspace/data/db.sqlite", "max_age": "25h",
                     "may_be_empty": True}],
        "probes": [{"name": "alive", "cmd": "true", "interval": "1h"}],
    }
    c = parse_deskwatch_config(raw)
    assert c.jobs == [JobExpectation("daily-crawl", timedelta(hours=25))]
    assert c.outputs == [OutputExpectation(
        "/workspace/data/db.sqlite", timedelta(hours=25), may_be_empty=True,
    )]
    assert c.probes == [ProbeExpectation("alive", "true", timedelta(hours=1))]


def test_parse_rejects_malformed_entries_loudly():
    """Typos in YAML must raise, not silently drop the entry."""
    with pytest.raises(WsError, match="deskwatch.jobs"):
        parse_deskwatch_config({"jobs": [{"nome": "oops"}]})
    with pytest.raises(WsError, match="deskwatch.outputs"):
        parse_deskwatch_config({"outputs": [{"paht": "/x"}]})
    with pytest.raises(WsError, match="deskwatch.probes"):
        parse_deskwatch_config({"probes": [{"name": "no-cmd"}]})


# --- evaluate_jobs: three outcomes users rely on ---------------------------


def _fake_registry(last_event):
    reg = MagicMock()
    reg.last_deskwatch_event.return_value = last_event
    return reg


def test_evaluate_jobs_no_run_on_record_is_unhealthy():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    jobs = [JobExpectation("daily", timedelta(hours=25))]
    reg = _fake_registry(None)
    checks = evaluate_jobs(reg, "desk1", jobs, now=now)
    assert len(checks) == 1
    assert checks[0].healthy is False
    assert "no run on record" in checks[0].detail


def test_evaluate_jobs_recent_success_is_healthy():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    jobs = [JobExpectation("daily", timedelta(hours=25))]
    reg = _fake_registry({
        "timestamp": (now - timedelta(hours=6)).isoformat(),
        "status": "ok", "detail": None,
    })
    checks = evaluate_jobs(reg, "desk1", jobs, now=now)
    assert checks[0].healthy is True
    assert "6h" in checks[0].detail


def test_evaluate_jobs_stale_success_is_unhealthy():
    """Success old enough to fall outside the expect_success_within window."""
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    jobs = [JobExpectation("daily", timedelta(hours=25))]
    reg = _fake_registry({
        "timestamp": (now - timedelta(days=4)).isoformat(),
        "status": "ok", "detail": None,
    })
    checks = evaluate_jobs(reg, "desk1", jobs, now=now)
    assert checks[0].healthy is False
    assert "exceeds" in checks[0].detail


def test_evaluate_jobs_last_failure_is_unhealthy_even_if_recent():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    jobs = [JobExpectation("daily", timedelta(hours=25))]
    reg = _fake_registry({
        "timestamp": (now - timedelta(minutes=30)).isoformat(),
        "status": "failed", "detail": "exit 1",
    })
    checks = evaluate_jobs(reg, "desk1", jobs, now=now)
    assert checks[0].healthy is False
    assert "failed" in checks[0].detail


# --- evaluate_outputs: live probe via docker exec stat --------------------


def _fake_stat_result(returncode: int, stdout: str) -> MagicMock:
    r = MagicMock()
    r.returncode, r.stdout = returncode, stdout
    return r


def test_evaluate_outputs_missing_container_marks_unhealthy():
    checks = evaluate_outputs(
        container_id="",
        outputs=[OutputExpectation("/x", timedelta(hours=1))],
    )
    assert checks[0].healthy is False
    assert "container not running" in checks[0].detail


def test_evaluate_outputs_missing_file_marks_unhealthy():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    with patch("drydock.core.deskwatch._docker_exec_probe",
               return_value=_fake_stat_result(1, "")):
        checks = evaluate_outputs(
            container_id="abc",
            outputs=[OutputExpectation("/missing", timedelta(hours=1))],
            now=now,
        )
    assert checks[0].healthy is False
    assert checks[0].detail == "missing"


def test_evaluate_outputs_stale_file_marks_unhealthy():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    four_days_ago = int((now - timedelta(days=4)).timestamp())
    with patch("drydock.core.deskwatch._docker_exec_probe",
               return_value=_fake_stat_result(0, f"{four_days_ago} 12345")):
        checks = evaluate_outputs(
            container_id="abc",
            outputs=[OutputExpectation("/workspace/db", timedelta(hours=25))],
            now=now,
        )
    assert checks[0].healthy is False
    assert "exceeds" in checks[0].detail


def test_evaluate_outputs_empty_file_flagged_unless_allowed():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    six_hours_ago = int((now - timedelta(hours=6)).timestamp())
    with patch("drydock.core.deskwatch._docker_exec_probe",
               return_value=_fake_stat_result(0, f"{six_hours_ago} 0")):
        unhealthy = evaluate_outputs(
            container_id="abc",
            outputs=[OutputExpectation("/x", timedelta(hours=25))],
            now=now,
        )
        healthy = evaluate_outputs(
            container_id="abc",
            outputs=[OutputExpectation("/x", timedelta(hours=25), may_be_empty=True)],
            now=now,
        )
    assert unhealthy[0].healthy is False
    assert healthy[0].healthy is True


# --- evaluate_probes: re-use within interval, re-run after ---------------


def test_evaluate_probes_reuses_recent_result_without_rerun():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    reg = MagicMock()
    reg.last_deskwatch_event.return_value = {
        "timestamp": (now - timedelta(minutes=10)).isoformat(),
        "status": "ok", "detail": "exit 0",
    }
    with patch("drydock.core.deskwatch._docker_exec_probe") as mock_exec:
        checks = evaluate_probes(
            reg, "desk1", "abc",
            [ProbeExpectation("alive", "true", timedelta(hours=1))],
            now=now,
        )
    assert mock_exec.call_count == 0  # within interval → no re-run
    assert checks[0].healthy is True
    assert reg.record_deskwatch_event.call_count == 0


def test_evaluate_probes_reruns_after_interval_elapses():
    now = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    reg = MagicMock()
    reg.last_deskwatch_event.side_effect = [
        {"timestamp": (now - timedelta(hours=2)).isoformat(),
         "status": "ok", "detail": "exit 0"},
        {"timestamp": now.isoformat(), "status": "failed", "detail": "exit 2"},
    ]
    probe_result = MagicMock()
    probe_result.returncode, probe_result.stderr = 2, "boom"
    with patch("drydock.core.deskwatch._docker_exec_probe",
               return_value=probe_result):
        checks = evaluate_probes(
            reg, "desk1", "abc",
            [ProbeExpectation("alive", "false", timedelta(hours=1))],
            now=now,
        )
    assert reg.record_deskwatch_event.called  # new result was stored
    assert checks[0].healthy is False


# --- Registry round-trip (contract for CLI + evaluator) ------------------


def test_registry_record_and_last_event_roundtrip(tmp_path):
    from drydock.core.registry import Registry
    reg = Registry(db_path=tmp_path / "registry.db")
    reg.record_deskwatch_event("ws_x", "job_run", "nightly", "ok", detail="exit 0")
    reg.record_deskwatch_event("ws_x", "job_run", "nightly", "failed", detail="exit 1")
    latest = reg.last_deskwatch_event("ws_x", "job_run", "nightly")
    assert latest["status"] == "failed"
    assert latest["detail"] == "exit 1"

    # Different event name → separate history.
    assert reg.last_deskwatch_event("ws_x", "job_run", "other") is None
    reg.close()


# --- render_cron_file: outcome recording appears in the cron line ----------


def test_render_cron_file_wraps_command_with_deskwatch_record():
    """The schedule wrapper must chain `ws deskwatch-record` after each job
    so cron-driven runs always leave a trail — even if the user hasn't
    declared deskwatch expectations yet, the history becomes available
    the moment they do."""
    from drydock.core.schedule import ScheduleJob, render_cron_file
    job = ScheduleJob(name="daily-crawl", cron="0 13 * * *",
                      command="bash run.sh", log="/var/log/out.log")
    content = render_cron_file("auction-crawl", [job])
    assert "/usr/local/bin/ws exec auction-crawl -- bash run.sh" in content
    assert "ws deskwatch-record auction-crawl job_run daily-crawl" in content
    assert "exit $ec" in content
