"""Tests for Phase PA0 auditor measurement layer.

Pin the contracts:
- parse_size handles both binary (KiB/MiB/GiB) and decimal (kB/MB/GB) units
- parse_percent strips % and parses float; None on garbage
- parse_docker_stats_line handles realistic docker stats output
- malformed input returns None for the field, not crash
- count_recent_audit_events filters by desk_id + window
- count_active_leases excludes revoked
- snapshot_harbor handles missing-container gracefully (metrics=None)
- HarborSnapshot is JSON-serializable
- Storage round-trips snapshots; prune respects --keep
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drydock.core.auditor.measurement import (
    HarborSnapshot,
    count_active_leases,
    count_recent_audit_events,
    parse_docker_stats_line,
    parse_percent,
    parse_size,
    snapshot_harbor,
)
from drydock.core.auditor.storage import (
    list_snapshots,
    prune_snapshots,
    read_snapshot,
    write_snapshot,
)
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace


class TestParseSize:
    @pytest.mark.parametrize("inp,expected", [
        ("1024B", 1024),
        ("1KiB", 1024),
        ("1kB", 1000),
        ("1MiB", 1024 * 1024),
        ("1MB", 1_000_000),
        ("1.842GiB", int(1.842 * 1024 ** 3)),
        ("4GiB", 4 * 1024 ** 3),
        ("256m", None),  # bare 'm' is ambiguous, not parsed
        ("", None),
        (None, None),
        ("garbage", None),
    ])
    def test_size_parsing(self, inp, expected):
        assert parse_size(inp) == expected

    def test_whitespace_tolerated(self):
        assert parse_size("  4 GiB  ") == 4 * 1024 ** 3


class TestParsePercent:
    @pytest.mark.parametrize("inp,expected", [
        ("12.5%", 12.5),
        ("0.00%", 0.0),
        ("100%", 100.0),
        ("12.5", 12.5),  # no % is fine
        ("", None),
        ("--", None),
        ("garbage", None),
    ])
    def test_percent_parsing(self, inp, expected):
        assert parse_percent(inp) == expected


class TestParseDockerStatsLine:
    def test_typical_line(self):
        line = (
            '{"BlockIO":"0B / 0B","CPUPerc":"12.50%","Container":"abc123def",'
            '"ID":"abc123def","MemPerc":"45.50%","MemUsage":"1.842GiB / 4GiB",'
            '"Name":"foo","NetIO":"1.2MB / 800kB","PIDs":"47"}'
        )
        out = parse_docker_stats_line(line)
        assert out["cpu_pct"] == 12.5
        assert out["mem_used_bytes"] == int(1.842 * 1024 ** 3)
        assert out["mem_limit_bytes"] == 4 * 1024 ** 3
        assert out["mem_pct"] == 45.5
        assert out["pids"] == 47
        assert out["container_id"] == "abc123def"
        assert out["container_name"] == "foo"

    def test_empty_line_returns_none(self):
        assert parse_docker_stats_line("") is None
        assert parse_docker_stats_line("   ") is None

    def test_malformed_json_returns_none(self):
        assert parse_docker_stats_line("{not json") is None

    def test_partial_data_keeps_parseable_fields(self):
        # Some fields missing — others should still parse.
        line = '{"CPUPerc":"5.0%","ID":"abc","Name":"foo"}'
        out = parse_docker_stats_line(line)
        assert out["cpu_pct"] == 5.0
        assert out["mem_used_bytes"] is None  # MemUsage absent
        assert out["pids"] is None  # PIDs absent

    def test_pids_dash_becomes_none(self):
        # docker stats outputs "--" for stopped containers.
        line = '{"PIDs":"--","ID":"abc","Name":"foo"}'
        out = parse_docker_stats_line(line)
        assert out["pids"] is None


@pytest.fixture
def registry(tmp_path):
    db = tmp_path / "registry.db"
    r = Registry(db_path=db)
    yield r
    r.close()


def _ws(name: str) -> Workspace:
    return Workspace(
        name=name, project=name, repo_path="/tmp/r", worktree_path=f"/tmp/{name}",
        branch=f"ws/{name}", state="running", container_id="cid_" + name,
    )


class TestCountActiveLeases:
    def test_empty(self, registry):
        registry.create_workspace(_ws("a"))
        result = count_active_leases(registry, "ws_a")
        assert result == {"active_total": 0, "by_type": {}}

    def test_with_leases(self, registry):
        from drydock.core.capability import CapabilityLease, CapabilityType
        registry.create_workspace(_ws("a"))
        # Issue two SECRET leases and one STORAGE_MOUNT
        for i, t in enumerate([CapabilityType.SECRET, CapabilityType.SECRET,
                                CapabilityType.STORAGE_MOUNT]):
            registry.insert_lease(CapabilityLease(
                lease_id=f"l_{i}", desk_id="ws_a", type=t,
                scope={}, issued_at=datetime.now(timezone.utc),
                expiry=None, issuer="wsd",
            ))
        result = count_active_leases(registry, "ws_a")
        assert result["active_total"] == 3
        assert result["by_type"] == {"SECRET": 2, "STORAGE_MOUNT": 1}

    def test_revoked_excluded(self, registry):
        from drydock.core.capability import CapabilityLease, CapabilityType
        registry.create_workspace(_ws("a"))
        registry.insert_lease(CapabilityLease(
            lease_id="l1", desk_id="ws_a", type=CapabilityType.SECRET,
            scope={}, issued_at=datetime.now(timezone.utc),
            expiry=None, issuer="wsd",
        ))
        registry.revoke_lease("l1", "test")
        result = count_active_leases(registry, "ws_a")
        assert result == {"active_total": 0, "by_type": {}}


class TestCountRecentAuditEvents:
    def test_missing_log_returns_none(self, tmp_path):
        result = count_recent_audit_events(
            "ws_a", audit_path=tmp_path / "no-such-log",
        )
        assert result is None

    def test_empty_log_returns_zero(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text("")
        result = count_recent_audit_events("ws_a", audit_path=log)
        assert result == {"events_total": 0, "by_event_class": {}}

    def test_filters_by_desk_id(self, tmp_path):
        log = tmp_path / "audit.log"
        now = datetime.now(timezone.utc).isoformat()
        log.write_text(
            json.dumps({"timestamp": now, "event": "lease.issued",
                        "principal": "ws_a"}) + "\n"
            + json.dumps({"timestamp": now, "event": "lease.issued",
                          "principal": "ws_b"}) + "\n"
            + json.dumps({"timestamp": now, "event": "lease.denied",
                          "principal": "ws_a"}) + "\n"
        )
        result = count_recent_audit_events("ws_a", audit_path=log)
        assert result["events_total"] == 2
        assert result["by_event_class"] == {"lease.issued": 1, "lease.denied": 1}

    def test_filters_by_window(self, tmp_path):
        log = tmp_path / "audit.log"
        recent = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        log.write_text(
            json.dumps({"timestamp": recent, "event": "x", "principal": "ws_a"}) + "\n"
            + json.dumps({"timestamp": old, "event": "y", "principal": "ws_a"}) + "\n"
        )
        result = count_recent_audit_events(
            "ws_a", audit_path=log, window=timedelta(hours=1),
        )
        assert result["events_total"] == 1

    def test_malformed_lines_skipped(self, tmp_path):
        log = tmp_path / "audit.log"
        now = datetime.now(timezone.utc).isoformat()
        log.write_text(
            json.dumps({"timestamp": now, "event": "x", "principal": "ws_a"}) + "\n"
            + "not json\n"
            + "\n"
            + json.dumps({"timestamp": now, "event": "y", "principal": "ws_a"}) + "\n"
        )
        result = count_recent_audit_events("ws_a", audit_path=log)
        assert result["events_total"] == 2


class TestSnapshotHarbor:
    def test_empty_harbor(self, registry):
        snap = snapshot_harbor(registry, hostname="test-harbor")
        assert snap.harbor_hostname == "test-harbor"
        assert snap.drydock_count == 0
        assert snap.drydocks == []

    def test_with_drydocks_metrics_none_when_no_docker(self, registry, monkeypatch):
        # Force collect_docker_stats to return empty (simulates no docker
        # / no running containers). All metrics will be None.
        from drydock.core.auditor import measurement
        monkeypatch.setattr(measurement, "collect_docker_stats", lambda ids: {})

        registry.create_workspace(_ws("alpha"))
        registry.create_workspace(_ws("beta"))
        snap = snapshot_harbor(registry, hostname="test")
        assert snap.drydock_count == 2
        assert all(d["metrics"] is None for d in snap.drydocks)
        # Other fields still present
        for d in snap.drydocks:
            assert d["leases"] == {"active_total": 0, "by_type": {}}
            assert d["yaml_drift"] in ("unpinned", "unknown")

    def test_serializes_to_json(self, registry):
        registry.create_workspace(_ws("alpha"))
        snap = snapshot_harbor(registry, hostname="test")
        # Should not raise
        json.dumps(snap.to_dict())


class TestStorage:
    def test_write_and_read_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Re-import to pick up the new HOME
        snap = HarborSnapshot(
            snapshot_at="2026-05-05T22:00:00+00:00",
            harbor_hostname="test",
            drydock_count=0,
            drydocks=[],
        )
        path = write_snapshot(snap)
        assert path.exists()
        loaded = read_snapshot(path)
        assert loaded["harbor_hostname"] == "test"
        assert loaded["drydock_count"] == 0

    def test_list_snapshots_chronological(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        for ts in ("2026-05-05T10:00:00+00:00",
                   "2026-05-05T11:00:00+00:00",
                   "2026-05-05T09:00:00+00:00"):
            snap = HarborSnapshot(snapshot_at=ts, harbor_hostname="h",
                                  drydock_count=0, drydocks=[])
            write_snapshot(snap)
        snaps = list_snapshots()
        assert len(snaps) == 3
        # Sorted by filename = sorted by ISO timestamp = chronological
        assert snaps[0].name < snaps[1].name < snaps[2].name

    def test_prune_keeps_most_recent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        for hour in range(5):
            snap = HarborSnapshot(
                snapshot_at=f"2026-05-05T{hour:02d}:00:00+00:00",
                harbor_hostname="h", drydock_count=0, drydocks=[],
            )
            write_snapshot(snap)
        removed = prune_snapshots(keep_count=2)
        assert removed == 3
        remaining = list_snapshots()
        assert len(remaining) == 2
        # Most recent two should remain
        names = [p.name for p in remaining]
        assert "2026-05-05T03-00-00+00-00.json" in names
        assert "2026-05-05T04-00-00+00-00.json" in names

    def test_prune_no_op_when_under_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        for hour in range(2):
            snap = HarborSnapshot(
                snapshot_at=f"2026-05-05T{hour:02d}:00:00+00:00",
                harbor_hostname="h", drydock_count=0, drydocks=[],
            )
            write_snapshot(snap)
        removed = prune_snapshots(keep_count=10)
        assert removed == 0
        assert len(list_snapshots()) == 2
