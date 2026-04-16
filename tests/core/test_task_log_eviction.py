"""Tests for task_log LRU eviction (gotcha #1).

Per docs/v2-design-protocol.md §3: evict completed/failed entries older
than 24h. In-progress rows are NEVER evicted regardless of age — they
may still be reconciled by the recovery sweeper.
"""

from datetime import datetime, timedelta, timezone

import pytest

from drydock.core.registry import Registry


@pytest.fixture
def registry(tmp_path):
    reg = Registry(db_path=tmp_path / "registry.db")
    yield reg
    reg.close()


def _insert(reg, *, request_id, status, completed_at):
    reg._conn.execute(
        """
        INSERT INTO task_log
            (request_id, method, spec_json, status, outcome_json,
             created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id, "CreateDesk", "{}", status, None,
            completed_at if completed_at else "2026-04-15T00:00:00+00:00",
            completed_at,
        ),
    )
    reg._conn.commit()


def _exists(reg, request_id) -> bool:
    return reg._conn.execute(
        "SELECT 1 FROM task_log WHERE request_id = ?", (request_id,),
    ).fetchone() is not None


class TestEvictOldTaskLog:
    def test_evicts_completed_25h_old(self, registry):
        now = datetime(2026, 4, 16, 12, tzinfo=timezone.utc)
        _insert(registry, request_id="r-old",
                status="completed",
                completed_at=(now - timedelta(hours=25)).isoformat())
        evicted = registry.evict_old_task_log(now=now)
        assert evicted == 1
        assert not _exists(registry, "r-old")

    def test_does_not_evict_completed_1h_old(self, registry):
        now = datetime(2026, 4, 16, 12, tzinfo=timezone.utc)
        _insert(registry, request_id="r-fresh",
                status="completed",
                completed_at=(now - timedelta(hours=1)).isoformat())
        assert registry.evict_old_task_log(now=now) == 0
        assert _exists(registry, "r-fresh")

    # In-progress rows must NEVER be evicted regardless of age — the
    # recovery sweeper may still reconcile them on the next daemon boot.
    def test_does_not_evict_in_progress_regardless_of_age(self, registry):
        now = datetime(2026, 4, 16, 12, tzinfo=timezone.utc)
        # Insert an in-progress row from 3 days ago — would be evicted
        # if status were ignored, but must be preserved.
        ancient = (now - timedelta(days=3)).isoformat()
        registry._conn.execute(
            """
            INSERT INTO task_log
                (request_id, method, spec_json, status, outcome_json,
                 created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("r-stuck", "CreateDesk", "{}", "in_progress", None, ancient, None),
        )
        registry._conn.commit()
        assert registry.evict_old_task_log(now=now) == 0
        assert _exists(registry, "r-stuck")

    def test_evicts_failed_too(self, registry):
        now = datetime(2026, 4, 16, 12, tzinfo=timezone.utc)
        _insert(registry, request_id="r-fail",
                status="failed",
                completed_at=(now - timedelta(hours=48)).isoformat())
        assert registry.evict_old_task_log(now=now) == 1
        assert not _exists(registry, "r-fail")

    def test_idempotent_when_nothing_to_evict(self, registry):
        now = datetime(2026, 4, 16, 12, tzinfo=timezone.utc)
        _insert(registry, request_id="r-fresh",
                status="completed",
                completed_at=(now - timedelta(hours=1)).isoformat())
        assert registry.evict_old_task_log(now=now) == 0
        assert registry.evict_old_task_log(now=now) == 0  # second call no-op

    def test_custom_cutoff_hours(self, registry):
        now = datetime(2026, 4, 16, 12, tzinfo=timezone.utc)
        _insert(registry, request_id="r-2h",
                status="completed",
                completed_at=(now - timedelta(hours=2)).isoformat())
        # Default 24h: not evicted
        assert registry.evict_old_task_log(now=now) == 0
        # Tighter 1h cutoff: evicted
        assert registry.evict_old_task_log(older_than_hours=1, now=now) == 1
