"""SQLite workspace registry."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .errors import WsError
from .workspace import Workspace

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    project         TEXT NOT NULL,
    repo_path       TEXT NOT NULL,
    worktree_path   TEXT NOT NULL DEFAULT '',
    branch          TEXT NOT NULL DEFAULT '',
    base_ref        TEXT NOT NULL DEFAULT 'HEAD',
    state           TEXT NOT NULL DEFAULT 'defined',
    container_id    TEXT NOT NULL DEFAULT '',
    image           TEXT NOT NULL DEFAULT '',
    owner           TEXT NOT NULL DEFAULT '',
    hostname        TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    labels          TEXT NOT NULL DEFAULT '{}',
    config          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id    TEXT NOT NULL,
    event           TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    data            TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
"""


class Registry:
    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            home = Path.home() / ".drydock"
            home.mkdir(parents=True, exist_ok=True)
            db_path = home / "registry.db"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self):
        self._conn.executescript(SCHEMA)

    def close(self):
        self._conn.close()

    def create_workspace(self, ws: Workspace) -> Workspace:
        existing = self.get_workspace(ws.name)
        if existing:
            raise WsError(
                f"Workspace '{ws.name}' already exists (state: {existing.state})",
                fix=f"Use a different name, or destroy it first: ws destroy {ws.name}",
            )
        self._conn.execute(
            """INSERT INTO workspaces
               (id, name, project, repo_path, worktree_path, branch, base_ref,
                state, container_id, image, owner, hostname, created_at, updated_at,
                labels, config)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ws.id,
                ws.name,
                ws.project,
                ws.repo_path,
                ws.worktree_path,
                ws.branch,
                ws.base_ref,
                ws.state,
                ws.container_id,
                ws.image,
                ws.owner,
                ws.hostname,
                ws.created_at,
                ws.updated_at,
                json.dumps(ws.labels),
                json.dumps(ws.config),
            ),
        )
        self._conn.commit()
        self.log_event(ws.id, "workspace.created")
        return ws

    def get_workspace(self, name: str) -> Workspace | None:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_workspace(row)

    def list_workspaces(
        self,
        project: str | None = None,
        state: str | None = None,
    ) -> list[Workspace]:
        query = "SELECT * FROM workspaces WHERE 1=1"
        params: list = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if state:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY updated_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_workspace(r) for r in rows]

    def update_state(self, name: str, state: str) -> Workspace:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE workspaces SET state = ?, updated_at = ? WHERE name = ?",
            (state, now, name),
        )
        self._conn.commit()
        ws = self.get_workspace(name)
        if ws:
            self.log_event(ws.id, f"workspace.{state}")
        return ws

    def update_workspace(self, name: str, **fields) -> Workspace:
        if not fields:
            return self.get_workspace(name)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [name]
        self._conn.execute(
            f"UPDATE workspaces SET {set_clause} WHERE name = ?",
            values,
        )
        self._conn.commit()
        return self.get_workspace(name)

    def delete_workspace(self, name: str) -> None:
        ws = self.get_workspace(name)
        if ws:
            self.log_event(ws.id, "workspace.destroyed")
        self._conn.execute("DELETE FROM workspaces WHERE name = ?", (name,))
        self._conn.commit()

    def log_event(
        self, workspace_id: str, event: str, data: dict | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO events (workspace_id, event, timestamp, data) VALUES (?, ?, ?, ?)",
            (workspace_id, event, now, json.dumps(data or {})),
        )
        self._conn.commit()

    def _row_to_workspace(self, row: sqlite3.Row) -> Workspace:
        d = dict(row)
        d["labels"] = json.loads(d["labels"])
        d["config"] = json.loads(d["config"])
        return Workspace(**d)
