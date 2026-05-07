"""Smoke tests for the V6 schema migration: original_resources_hard column.

Pinning behaviors:
- Fresh DB: column exists with empty-dict default.
- Legacy DB (V5 shape, no V6 column): migration adds it; existing rows
  have empty default; new rows can populate via create_drydock.
- Round-trip: HardCeilings dict → registry → Drydock.original_resources_hard.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from drydock.core.registry import Registry
from drydock.core.runtime import Drydock


def test_fresh_db_has_v6_column():
    with tempfile.TemporaryDirectory() as td:
        r = Registry(db_path=Path(td) / "r.db")
        cols = {c[1] for c in r._conn.execute("PRAGMA table_info(drydocks)")}
        assert "original_resources_hard" in cols


def test_legacy_v5_db_gets_column_added():
    """Simulate a registry that was migrated up to V5 but not yet V6."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "r.db"
        # Build a synthetic pre-V6 schema (drydocks table without the column).
        conn = sqlite3.connect(str(db))
        conn.executescript("""
        CREATE TABLE drydocks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, project TEXT NOT NULL,
            repo_path TEXT NOT NULL, worktree_path TEXT DEFAULT '',
            branch TEXT DEFAULT '', base_ref TEXT DEFAULT 'HEAD',
            state TEXT DEFAULT 'defined', container_id TEXT DEFAULT '',
            workspace_subdir TEXT DEFAULT '', image TEXT DEFAULT '',
            owner TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            config TEXT DEFAULT '{}'
        );
        INSERT INTO drydocks (id, name, project, repo_path, created_at, updated_at)
        VALUES ('dock_legacy', 'legacy', 'p', '/r', '2026', '2026');
        """)
        conn.commit()
        conn.close()

        r = Registry(db_path=db)
        cols = {c[1] for c in r._conn.execute("PRAGMA table_info(drydocks)")}
        assert "original_resources_hard" in cols
        # Existing row got the default empty-dict value
        ws = r.get_drydock("legacy")
        assert ws is not None
        assert ws.original_resources_hard == {}


def test_create_drydock_persists_original_resources_hard():
    with tempfile.TemporaryDirectory() as td:
        r = Registry(db_path=Path(td) / "r.db")
        ws = Drydock(
            name="auction-crawl",
            project="auction-crawl",
            repo_path="/r",
            original_resources_hard={"cpu_max": 2.0, "memory_max": "4g"},
        )
        r.create_drydock(ws)

        # Round-trip via get_drydock
        got = r.get_drydock("auction-crawl")
        assert got is not None
        assert got.original_resources_hard == {"cpu_max": 2.0, "memory_max": "4g"}

        # And via raw SQL — confirm it's actually JSON-encoded in the column.
        row = r._conn.execute(
            "SELECT original_resources_hard FROM drydocks WHERE name = ?",
            ("auction-crawl",),
        ).fetchone()
        assert json.loads(row["original_resources_hard"]) == {
            "cpu_max": 2.0, "memory_max": "4g",
        }


def test_create_drydock_without_resources_persists_empty_dict():
    with tempfile.TemporaryDirectory() as td:
        r = Registry(db_path=Path(td) / "r.db")
        ws = Drydock(name="bare", project="bare", repo_path="/r")
        r.create_drydock(ws)
        got = r.get_drydock("bare")
        assert got.original_resources_hard == {}


def test_v6_migration_idempotent():
    """Running the registry init twice doesn't error and preserves data."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "r.db"
        r1 = Registry(db_path=db)
        ws = Drydock(
            name="x", project="x", repo_path="/r",
            original_resources_hard={"cpu_max": 1.0},
        )
        r1.create_drydock(ws)
        r1.close()

        # Second open — migration runs again, idempotently
        r2 = Registry(db_path=db)
        got = r2.get_drydock("x")
        assert got.original_resources_hard == {"cpu_max": 1.0}


def test_backfill_renames_stale_desk_id_column():
    """Recurring-rename fixup: a post-V8 registry that somehow still has
    a `desk_id` FK column (left over from a partial earlier migration)
    gets healed at next Registry init.

    Reproduces the hetzner regression where deskwatch_events.desk_id
    survived the deploy because the V1-vocab migration's body only ran
    when `workspaces` table existed and rerunning was a no-op."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "r.db"
        # Open + close once so the schema is created in current shape.
        Registry(db_path=db).close()

        # Surgically rename drydock_id back to desk_id on one FK table
        # to simulate the regression.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "ALTER TABLE deskwatch_events RENAME COLUMN drydock_id TO desk_id"
        )
        conn.commit(); conn.close()

        # Re-open: backfill should rename it back.
        r = Registry(db_path=db)
        cols = {c[1] for c in r._conn.execute("PRAGMA table_info(deskwatch_events)")}
        assert "drydock_id" in cols
        assert "desk_id" not in cols
