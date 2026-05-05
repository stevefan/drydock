"""SQLite workspace registry."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import WsError
from .capability import CapabilityLease, CapabilityType
from .workspace import Workspace

# Schema drops hostname and labels columns as of this revision.  Existing
# SQLite registries with those columns still read fine (SQLite ignores extra
# columns), but new databases won't have them.  If you hit column-mismatch
# errors after upgrading, delete ~/.drydock/registry.db and re-create.
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
    workspace_subdir TEXT NOT NULL DEFAULT '',
    image           TEXT NOT NULL DEFAULT '',
    owner           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
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

V2_WORKSPACE_COLUMNS = (
    ("parent_desk_id", "TEXT DEFAULT NULL"),
    ("delegatable_firewall_domains", "TEXT DEFAULT '[]'"),
    ("delegatable_secrets", "TEXT DEFAULT '[]'"),
    ("capabilities", "TEXT DEFAULT '[]'"),
    # Phase 1b narrowness for STORAGE_MOUNT leases. Empty list = no
    # narrowness declared (capability gate alone). See policy.DeskPolicy.
    ("delegatable_storage_scopes", "TEXT DEFAULT '[]'"),
    # INFRA_PROVISION narrowness: list of IAM action globs. Empty list =
    # no narrowness declared (capability gate alone). See policy.DeskPolicy.
    ("delegatable_provision_scopes", "TEXT DEFAULT '[]'"),
    # NETWORK_REACH narrowness: list of domain glob patterns. Empty list =
    # NO dynamic firewall opens permitted. Stricter empty semantics than
    # storage/provision by deliberate design (see network-reach.md).
    ("delegatable_network_reach", "TEXT DEFAULT '[]'"),
    # Companion port allowlist for NETWORK_REACH. Empty = default [80, 443].
    ("network_reach_ports", "TEXT DEFAULT '[]'"),
)

V2_TABLES = """
CREATE TABLE IF NOT EXISTS leases (
    lease_id            TEXT PRIMARY KEY,
    desk_id             TEXT NOT NULL,
    type                TEXT NOT NULL,
    scope               TEXT NOT NULL,
    issued_at           TIMESTAMP NOT NULL,
    expiry              TIMESTAMP NULL,
    issuer              TEXT NOT NULL,
    revoked             INTEGER NOT NULL DEFAULT 0,
    revocation_reason   TEXT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    desk_id             TEXT PRIMARY KEY,
    token_sha256        TEXT NOT NULL,
    issued_at           TIMESTAMP NOT NULL,
    rotated_at          TIMESTAMP NULL
);

CREATE TABLE IF NOT EXISTS task_log (
    request_id          TEXT PRIMARY KEY,
    method              TEXT NOT NULL,
    spec_json           TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    outcome_json        TEXT NULL,
    created_at          TIMESTAMP NOT NULL,
    completed_at        TIMESTAMP NULL
);
"""


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info('workspaces')").fetchall()
    }
    for column_name, column_def in V2_WORKSPACE_COLUMNS:
        if column_name in columns:
            continue
        conn.execute(
            f"ALTER TABLE workspaces ADD COLUMN {column_name} {column_def}"
        )
    conn.executescript(V2_TABLES)


# Deskwatch: observational record of workload health for each desk.
# `kind` is a short tag ('job_run', 'probe_result', 'output_check'),
# `name` is the caller-defined identifier within that kind (the job
# name from schedule.yaml, the probe name from the project YAML, the
# output path). `status` is 'ok', 'failed', or 'missing'. `detail` is
# a free-form string (exit code, stderr tail, file age, etc.).
V3_TABLES = """
CREATE TABLE IF NOT EXISTS deskwatch_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    desk_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_deskwatch_lookup
    ON deskwatch_events (desk_id, kind, name, timestamp DESC);
"""


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(V3_TABLES)


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
        _migrate_to_v2(self._conn)
        _migrate_to_v3(self._conn)
        self._conn.commit()

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
                state, container_id, workspace_subdir, image, owner,
                created_at, updated_at, config)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ws.workspace_subdir,
                ws.image,
                ws.owner,
                ws.created_at,
                ws.updated_at,
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

    def get_children(self, parent_desk_id: str) -> list[Workspace]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM workspaces
            WHERE parent_desk_id = ?
            ORDER BY name ASC
            """,
            (parent_desk_id,),
        ).fetchall()
        return [self._row_to_workspace(row) for row in rows]

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
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"])
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

    def insert_token(self, desk_id: str, token_sha256: str, issued_at: datetime) -> None:
        self._conn.execute(
            """
            INSERT INTO tokens (desk_id, token_sha256, issued_at, rotated_at)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(desk_id) DO NOTHING
            """,
            (desk_id, token_sha256, issued_at.isoformat()),
        )
        self._conn.commit()

    def find_desk_by_token_hash(self, token_sha256: str) -> str | None:
        row = self._conn.execute(
            "SELECT desk_id FROM tokens WHERE token_sha256 = ?",
            (token_sha256,),
        ).fetchone()
        if row is None:
            return None
        return str(row["desk_id"])

    def get_token_info(self, desk_id: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT desk_id, token_sha256, issued_at, rotated_at
            FROM tokens
            WHERE desk_id = ?
            """,
            (desk_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def delete_token(self, desk_id: str) -> None:
        self._conn.execute("DELETE FROM tokens WHERE desk_id = ?", (desk_id,))
        self._conn.commit()

    def load_desk_policy(self, desk_id: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT delegatable_firewall_domains, delegatable_secrets, capabilities,
                   delegatable_storage_scopes, delegatable_provision_scopes,
                   delegatable_network_reach, network_reach_ports, config
            FROM workspaces
            WHERE id = ?
            """,
            (desk_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def update_desk_delegations(
        self,
        name: str,
        *,
        delegatable_firewall_domains: list[str] | None = None,
        delegatable_secrets: list[str] | None = None,
        capabilities: list[str] | None = None,
        delegatable_storage_scopes: list[str] | None = None,
        delegatable_provision_scopes: list[str] | None = None,
        delegatable_network_reach: list[str] | None = None,
        network_reach_ports: list[int] | None = None,
    ) -> None:
        fields: dict[str, str] = {}
        if delegatable_firewall_domains is not None:
            fields["delegatable_firewall_domains"] = json.dumps(delegatable_firewall_domains)
        if delegatable_secrets is not None:
            fields["delegatable_secrets"] = json.dumps(delegatable_secrets)
        if capabilities is not None:
            fields["capabilities"] = json.dumps(capabilities)
        if delegatable_storage_scopes is not None:
            fields["delegatable_storage_scopes"] = json.dumps(delegatable_storage_scopes)
        if delegatable_provision_scopes is not None:
            fields["delegatable_provision_scopes"] = json.dumps(delegatable_provision_scopes)
        if delegatable_network_reach is not None:
            fields["delegatable_network_reach"] = json.dumps(delegatable_network_reach)
        if network_reach_ports is not None:
            fields["network_reach_ports"] = json.dumps(network_reach_ports)
        if not fields:
            return
        self.update_workspace(name, **fields)

    # ----- Task log maintenance (gotcha #1) -----

    def evict_old_task_log(
        self,
        *,
        older_than_hours: int = 24,
        now: datetime | None = None,
    ) -> int:
        """Delete completed/failed task_log rows older than the cutoff.

        Per docs/v2-design-protocol.md §3: bounded LRU — evict entries
        that are BOTH older than 24h AND in a terminal state. In-progress
        rows are preserved regardless of age (they may still be reconciled
        by the recovery sweeper).

        `now` is settable for deterministic testing; production passes None.
        """
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=older_than_hours)
        cutoff_iso = cutoff.isoformat()
        cur = self._conn.execute(
            """
            DELETE FROM task_log
            WHERE status IN ('completed', 'failed')
              AND completed_at IS NOT NULL
              AND completed_at < ?
            """,
            (cutoff_iso,),
        )
        self._conn.commit()
        return cur.rowcount

    # ----- Capability leases (Slice 3b) -----

    def insert_lease(self, lease: CapabilityLease) -> None:
        self._conn.execute(
            """
            INSERT INTO leases
                (lease_id, desk_id, type, scope, issued_at, expiry,
                 issuer, revoked, revocation_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease.lease_id,
                lease.desk_id,
                lease.type.value,
                json.dumps(lease.scope),
                lease.issued_at.isoformat(),
                lease.expiry.isoformat() if lease.expiry else None,
                lease.issuer,
                int(lease.revoked),
                lease.revocation_reason,
            ),
        )
        self._conn.commit()

    def get_lease(self, lease_id: str) -> CapabilityLease | None:
        row = self._conn.execute(
            """
            SELECT lease_id, desk_id, type, scope, issued_at, expiry,
                   issuer, revoked, revocation_reason
            FROM leases
            WHERE lease_id = ?
            """,
            (lease_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_lease(row)

    def revoke_lease(self, lease_id: str, reason: str) -> bool:
        """Mark a single lease revoked. Returns True if a row was changed.

        Idempotent: revoking an already-revoked lease returns False without
        clobbering the original reason.
        """
        cur = self._conn.execute(
            """
            UPDATE leases
            SET revoked = 1, revocation_reason = ?
            WHERE lease_id = ? AND revoked = 0
            """,
            (reason, lease_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def revoke_leases_for_desk(self, desk_id: str, reason: str) -> int:
        """Revoke every active lease belonging to a desk. Returns count."""
        cur = self._conn.execute(
            """
            UPDATE leases
            SET revoked = 1, revocation_reason = ?
            WHERE desk_id = ? AND revoked = 0
            """,
            (reason, desk_id),
        )
        self._conn.commit()
        return cur.rowcount

    def list_active_leases_for_desk(self, desk_id: str) -> list[CapabilityLease]:
        rows = self._conn.execute(
            """
            SELECT lease_id, desk_id, type, scope, issued_at, expiry,
                   issuer, revoked, revocation_reason
            FROM leases
            WHERE desk_id = ? AND revoked = 0
            ORDER BY issued_at
            """,
            (desk_id,),
        ).fetchall()
        return [_row_to_lease(row) for row in rows]

    def find_active_secret_lease(
        self, desk_id: str, secret_name: str
    ) -> CapabilityLease | None:
        """Return any active lease for (desk_id, secret_name) or None.

        Used by the daemon when releasing a lease to decide whether the
        materialized file at /run/secrets/<name> should also be removed
        (only when no other active lease still grants the same secret).
        """
        for lease in self.list_active_leases_for_desk(desk_id):
            if lease.type == CapabilityType.SECRET and lease.scope.get("secret_name") == secret_name:
                return lease
        return None

    def find_active_storage_lease(self, desk_id: str) -> CapabilityLease | None:
        """Return any active STORAGE_MOUNT lease for desk_id, or None.

        STORAGE_MOUNT uses a single-lease-at-a-time semantic: materialized
        aws_* files are overwritten by the latest lease, so the daemon
        needs to know whether any storage lease is still live before
        cleaning up on release.
        """
        for lease in self.list_active_leases_for_desk(desk_id):
            if lease.type == CapabilityType.STORAGE_MOUNT:
                return lease
        return None

    def find_active_aws_lease(self, desk_id: str) -> CapabilityLease | None:
        """Any active STORAGE_MOUNT or INFRA_PROVISION lease for desk_id.

        Both types materialize the same 4 aws_* files, so supersede /
        cleanup decisions must consider them together.
        """
        for lease in self.list_active_leases_for_desk(desk_id):
            if lease.type in (CapabilityType.STORAGE_MOUNT, CapabilityType.INFRA_PROVISION):
                return lease
        return None

    # ------------------------------------------------------------------
    # Deskwatch events (v3)
    # ------------------------------------------------------------------

    def record_deskwatch_event(
        self,
        desk_id: str,
        kind: str,
        name: str,
        status: str,
        detail: str | None = None,
        timestamp: str | None = None,
    ) -> int:
        """Append one deskwatch event. Returns the rowid."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO deskwatch_events (desk_id, kind, name, timestamp, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (desk_id, kind, name, ts, status, detail),
        )
        self._conn.commit()
        return cur.lastrowid

    def last_deskwatch_event(
        self, desk_id: str, kind: str, name: str,
    ) -> dict | None:
        row = self._conn.execute(
            "SELECT timestamp, status, detail FROM deskwatch_events "
            "WHERE desk_id = ? AND kind = ? AND name = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (desk_id, kind, name),
        ).fetchone()
        return dict(row) if row else None

    def list_deskwatch_events(
        self, desk_id: str, limit: int = 100,
    ) -> list[dict]:
        rows = self._conn.execute(
            "SELECT kind, name, timestamp, status, detail FROM deskwatch_events "
            "WHERE desk_id = ? ORDER BY timestamp DESC LIMIT ?",
            (desk_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_workspace_extra_mounts(self, name: str) -> list[str]:
        row = self._conn.execute(
            "SELECT config FROM workspaces WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return []
        try:
            config = json.loads(row["config"])
        except (TypeError, json.JSONDecodeError):
            return []
        mounts = config.get("extra_mounts")
        if not isinstance(mounts, list):
            return []
        return [value for value in mounts if isinstance(value, str)]

    def _row_to_workspace(self, row: sqlite3.Row) -> Workspace:
        d = dict(row)
        d["config"] = json.loads(d["config"])
        # Drop columns that the current Workspace dataclass doesn't accept.
        # Lets existing registries (with legacy columns like hostname/labels)
        # migrate forward without needing a schema rewrite.
        allowed = Workspace.__dataclass_fields__.keys()
        d = {k: v for k, v in d.items() if k in allowed}
        return Workspace(**d)


def _row_to_lease(row: sqlite3.Row) -> CapabilityLease:
    return CapabilityLease(
        lease_id=str(row["lease_id"]),
        desk_id=str(row["desk_id"]),
        type=CapabilityType(row["type"]),
        scope=json.loads(row["scope"]),
        issued_at=datetime.fromisoformat(row["issued_at"]),
        expiry=datetime.fromisoformat(row["expiry"]) if row["expiry"] else None,
        issuer=str(row["issuer"]),
        revoked=bool(row["revoked"]),
        revocation_reason=row["revocation_reason"],
    )
