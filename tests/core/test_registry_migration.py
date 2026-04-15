import sqlite3

import pytest

from drydock.core.registry import Registry

V1_WORKSPACES_SCHEMA = """
CREATE TABLE workspaces (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    project         TEXT NOT NULL,
    repo_path       TEXT NOT NULL,
    worktree_path   TEXT NOT NULL DEFAULT '',
    branch          TEXT NOT NULL DEFAULT '',
    base_ref        TEXT NOT NULL DEFAULT 'HEAD',
    state           TEXT NOT NULL DEFAULT 'defined',
    container_id    TEXT NOT NULL DEFAULT '',
    workspace_subdir TEXT NOT NULL DEFAULT '',
    image           TEXT NOT NULL DEFAULT '',
    owner           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    config          TEXT NOT NULL DEFAULT '{}'
);
"""

EXPECTED_V2_COLUMNS = {
    "parent_desk_id": "NULL",
    "delegatable_firewall_domains": "'[]'",
    "delegatable_secrets": "'[]'",
    "capabilities": "'[]'",
}

EXPECTED_V2_TABLES = {"leases", "tokens", "task_log"}


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_v1_registry(db_path):
    conn = _connect(db_path)
    conn.executescript(V1_WORKSPACES_SCHEMA)
    conn.commit()
    return conn


def _workspace_columns(conn):
    return {
        row["name"]: row["dflt_value"]
        for row in conn.execute("PRAGMA table_info('workspaces')").fetchall()
    }


def _user_tables(conn):
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {row["name"] for row in rows}


def test_registry_migrates_v1_schema_and_preserves_existing_rows(tmp_path):
    db_path = tmp_path / "registry.db"
    conn = _create_v1_registry(db_path)
    conn.execute(
        """
        INSERT INTO workspaces
            (id, name, project, repo_path, worktree_path, branch, base_ref,
             state, container_id, workspace_subdir, image, owner,
             created_at, updated_at, config)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ws_legacy",
            "legacy",
            "proj",
            "/srv/code/proj",
            "/tmp/worktree",
            "main",
            "HEAD",
            "running",
            "container-123",
            "subdir",
            "image:latest",
            "owner",
            "2026-04-14T00:00:00+00:00",
            "2026-04-14T01:00:00+00:00",
            '{"k":"v"}',
        ),
    )
    conn.commit()
    conn.close()

    registry = Registry(db_path=db_path)

    migrated_conn = _connect(db_path)
    columns = _workspace_columns(migrated_conn)
    for column_name, default_value in EXPECTED_V2_COLUMNS.items():
        assert columns[column_name] == default_value
    assert EXPECTED_V2_TABLES.issubset(_user_tables(migrated_conn))

    row = migrated_conn.execute(
        "SELECT * FROM workspaces WHERE name = ?", ("legacy",)
    ).fetchone()
    assert row["id"] == "ws_legacy"
    assert row["project"] == "proj"
    assert row["repo_path"] == "/srv/code/proj"
    assert row["worktree_path"] == "/tmp/worktree"
    assert row["branch"] == "main"
    assert row["state"] == "running"
    assert row["container_id"] == "container-123"
    assert row["workspace_subdir"] == "subdir"
    assert row["image"] == "image:latest"
    assert row["owner"] == "owner"
    assert row["created_at"] == "2026-04-14T00:00:00+00:00"
    assert row["updated_at"] == "2026-04-14T01:00:00+00:00"
    assert row["config"] == '{"k":"v"}'
    assert row["parent_desk_id"] is None
    assert row["delegatable_firewall_domains"] == "[]"
    assert row["delegatable_secrets"] == "[]"
    assert row["capabilities"] == "[]"

    fetched = registry.get_workspace("legacy")
    assert fetched is not None
    assert fetched.id == "ws_legacy"
    assert fetched.project == "proj"
    assert fetched.state == "running"

    migrated_conn.close()
    registry.close()


def test_registry_migration_is_idempotent_on_reopen(tmp_path):
    db_path = tmp_path / "registry.db"
    conn = _create_v1_registry(db_path)
    conn.close()

    registry = Registry(db_path=db_path)
    registry.close()

    first_conn = _connect(db_path)
    first_columns = _workspace_columns(first_conn)
    first_tables = _user_tables(first_conn)
    first_conn.close()

    registry = Registry(db_path=db_path)
    registry.close()

    second_conn = _connect(db_path)
    second_columns = _workspace_columns(second_conn)
    second_tables = _user_tables(second_conn)
    second_conn.close()

    assert first_columns == second_columns
    assert first_tables == second_tables
    assert EXPECTED_V2_TABLES.issubset(second_tables)


def test_registry_creates_fresh_db_with_v2_schema(tmp_path):
    db_path = tmp_path / "fresh.db"

    registry = Registry(db_path=db_path)
    registry.close()

    conn = _connect(db_path)
    columns = _workspace_columns(conn)
    for column_name, default_value in EXPECTED_V2_COLUMNS.items():
        assert columns[column_name] == default_value
    assert EXPECTED_V2_TABLES.issubset(_user_tables(conn))
    conn.close()


def test_v2_tables_accept_valid_rows_and_reject_invalid_constraints(tmp_path):
    db_path = tmp_path / "contracts.db"
    registry = Registry(db_path=db_path)
    registry.close()

    conn = _connect(db_path)
    conn.execute(
        """
        INSERT INTO leases
            (lease_id, desk_id, type, scope, issued_at, expiry, issuer, revoked, revocation_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "lease-1",
            "desk-1",
            "SECRET",
            '{"secret_name":"api_key"}',
            "2026-04-14T00:00:00+00:00",
            None,
            "wsd",
            0,
            None,
        ),
    )
    conn.execute(
        """
        INSERT INTO tokens
            (desk_id, token_sha256, issued_at, rotated_at)
        VALUES (?, ?, ?, ?)
        """,
        ("desk-1", "abc123", "2026-04-14T00:00:00+00:00", None),
    )
    conn.execute(
        """
        INSERT INTO task_log
            (request_id, method, spec_json, status, outcome_json, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "req-1",
            "SpawnChild",
            '{"desk":"child"}',
            "completed",
            '{"ok":true}',
            "2026-04-14T00:00:00+00:00",
            "2026-04-14T00:01:00+00:00",
        ),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO leases
                (lease_id, desk_id, type, scope, issued_at, expiry, issuer, revoked, revocation_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lease-1",
                "desk-2",
                "SECRET",
                '{"secret_name":"other"}',
                "2026-04-14T00:00:00+00:00",
                None,
                "wsd",
                0,
                None,
            ),
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO tokens
                (desk_id, token_sha256, issued_at, rotated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("desk-1", "def456", "2026-04-14T00:05:00+00:00", None),
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO task_log
                (request_id, method, spec_json, status, outcome_json, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "req-2",
                "SpawnChild",
                '{"desk":"child"}',
                "queued",
                None,
                "2026-04-14T00:00:00+00:00",
                None,
            ),
        )

    conn.close()
