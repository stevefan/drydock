"""SQLite drydock registry."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import WsError
from .capability import CapabilityLease, CapabilityType
from .runtime import Drydock

# Schema drops hostname and labels columns as of this revision.  Existing
# SQLite registries with those columns still read fine (SQLite ignores extra
# columns), but new databases won't have them.  If you hit column-mismatch
# errors after upgrading, delete ~/.drydock/registry.db and re-create.
SCHEMA = """
CREATE TABLE IF NOT EXISTS drydocks (
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
    drydock_id    TEXT NOT NULL,
    event           TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    data            TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (drydock_id) REFERENCES drydocks(id)
);
"""

V2_WORKSPACE_COLUMNS = (
    ("parent_drydock_id", "TEXT DEFAULT NULL"),
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
    # Phase A hard resource ceilings (cpu_max, memory_max, pids_max).
    # JSON dict; empty {} = no cgroup ceiling. See resource-ceilings.md.
    ("resources_hard", "TEXT DEFAULT '{}'"),
    # Phase 0 of project-dock-ontology.md: SHA-256 of the project YAML
    # at the moment this Drydock's policy was last pinned (create or
    # reload). Compared against the current YAML's SHA in `ws host audit`
    # to surface silent drift between the YAML on disk and the registry's
    # pinned snapshot. Empty string = unknown (e.g., legacy row).
    ("pinned_yaml_sha256", "TEXT DEFAULT ''"),
    # Phase Y0 of yard.md: optional FK to yards.id. NULL = standalone
    # Drydock (not in any Yard). Member-of-Yard for shared budget /
    # secrets / network is a Phase Y1+ feature; this column just
    # records membership.
    ("yard_id", "TEXT DEFAULT NULL"),
)

# Yards (Phase Y0): grouping of related Drydocks with shared substrate.
# The `config` JSON carries shared-substrate declarations (shared_secrets,
# shared_budget, internal_network). Empty {} for Phase Y0; populated as
# Y1-Y4 land. See docs/design/yard.md.
V4_TABLES = """
CREATE TABLE IF NOT EXISTS yards (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    repo_path       TEXT NULL,
    config          TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drydocks_yard_id
    ON drydocks (yard_id);
"""


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    conn.executescript(V4_TABLES)


# Amendments (Phase A0 of amendment-contract.md): structured proposals
# for infrastructure changes. Three classes: principal-direct (auto-approve),
# Dockworker-within-policy (Authority auto-applies), Dockworker-novel
# (Auditor escalates to principal). A0 is just the schema + CRUD; A1
# adds the auto-approval gate by hooking capability handlers.
V5_TABLES = """
CREATE TABLE IF NOT EXISTS amendments (
    id                  TEXT PRIMARY KEY,
    proposed_by_type    TEXT NOT NULL CHECK (proposed_by_type IN ('principal', 'dockworker')),
    proposed_by_id      TEXT NOT NULL,
    proposed_at         TEXT NOT NULL,
    yard_id             TEXT NULL,
    drydock_id          TEXT NULL,
    kind                TEXT NOT NULL,
    request_json        TEXT NOT NULL,
    reason              TEXT NULL,
    tos_notes           TEXT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'auto_approved', 'escalated',
                                          'approved', 'denied', 'applied', 'expired')),
    reviewed_by         TEXT NULL,
    reviewed_at         TEXT NULL,
    review_note         TEXT NULL,
    applied_at          TEXT NULL,
    expires_at          TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_amendments_status
    ON amendments (status, proposed_at DESC);
CREATE INDEX IF NOT EXISTS idx_amendments_drydock
    ON amendments (drydock_id, proposed_at DESC);
"""


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    conn.executescript(V5_TABLES)

V2_TABLES = """
CREATE TABLE IF NOT EXISTS leases (
    lease_id            TEXT PRIMARY KEY,
    drydock_id             TEXT NOT NULL,
    type                TEXT NOT NULL,
    scope               TEXT NOT NULL,
    issued_at           TIMESTAMP NOT NULL,
    expiry              TIMESTAMP NULL,
    issuer              TEXT NOT NULL,
    revoked             INTEGER NOT NULL DEFAULT 0,
    revocation_reason   TEXT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    drydock_id             TEXT PRIMARY KEY,
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
        for row in conn.execute("PRAGMA table_info('drydocks')").fetchall()
    }
    for column_name, column_def in V2_WORKSPACE_COLUMNS:
        if column_name in columns:
            continue
        conn.execute(
            f"ALTER TABLE drydocks ADD COLUMN {column_name} {column_def}"
        )
    conn.executescript(V2_TABLES)


# Deskwatch: observational record of workload health for each Dock.
# `kind` is a short tag ('job_run', 'probe_result', 'output_check'),
# `name` is the caller-defined identifier within that kind (the job
# name from schedule.yaml, the probe name from the project YAML, the
# output path). `status` is 'ok', 'failed', or 'missing'. `detail` is
# a free-form string (exit code, stderr tail, file age, etc.).
V3_TABLES = """
CREATE TABLE IF NOT EXISTS deskwatch_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    drydock_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_deskwatch_lookup
    ON deskwatch_events (drydock_id, kind, name, timestamp DESC);
"""


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(V3_TABLES)


def _migrate_v1_vocab_to_drydock(conn: sqlite3.Connection) -> None:
    """One-time vocabulary rename: workspace/desk/ws_ → drydock/dock_.

    Idempotent — only fires when the legacy ``workspaces`` table exists.
    Performs an atomic in-place rename of the table, FK columns, ID
    prefixes, and audit event names. Filesystem secret directories are
    migrated separately (see ``drydock host init``).
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "workspaces" not in tables:
        return  # already migrated, or fresh DB

    # If the SCHEMA executescript hasn't run yet (we're called first),
    # 'drydocks' should not exist. If it does, it's empty (legacy bug
    # from a partial migration); drop it so the rename can proceed.
    if "drydocks" in tables:
        row = conn.execute("SELECT COUNT(*) FROM drydocks").fetchone()
        if row[0] == 0:
            conn.execute("DROP TABLE drydocks")
        else:
            raise RuntimeError(
                "Both 'workspaces' and 'drydocks' tables hold rows; manual "
                "reconciliation needed before vocab migration can proceed."
            )

    conn.execute("ALTER TABLE workspaces RENAME TO drydocks")

    # Rename FK columns workspace_id|desk_id → drydock_id wherever present.
    # (V1 used a mix: events/amendments/deskwatch_events used workspace_id,
    # while tokens/leases used desk_id.)
    fk_tables = ("events", "leases", "tokens", "amendments", "deskwatch_events")
    for tbl in fk_tables:
        if tbl not in tables:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{tbl}')")}
        if "workspace_id" in cols and "drydock_id" not in cols:
            conn.execute(f"ALTER TABLE {tbl} RENAME COLUMN workspace_id TO drydock_id")
        elif "desk_id" in cols and "drydock_id" not in cols:
            conn.execute(f"ALTER TABLE {tbl} RENAME COLUMN desk_id TO drydock_id")
        if "parent_workspace_id" in cols and "parent_drydock_id" not in cols:
            conn.execute(f"ALTER TABLE {tbl} RENAME COLUMN parent_workspace_id TO parent_drydock_id")

    # Also rename parent_workspace_id on drydocks itself (set in V2 migration).
    drydocks_cols = {r[1] for r in conn.execute("PRAGMA table_info('drydocks')")}
    if "parent_workspace_id" in drydocks_cols and "parent_drydock_id" not in drydocks_cols:
        conn.execute("ALTER TABLE drydocks RENAME COLUMN parent_workspace_id TO parent_drydock_id")

    # Rewrite ws_<slug> → dock_<slug> in primary keys + FK columns.
    conn.execute(r"UPDATE drydocks SET id = 'dock_' || substr(id, 4) WHERE id LIKE 'ws\_%' ESCAPE '\'")
    for tbl in fk_tables:
        if tbl not in tables:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{tbl}')")}
        if "drydock_id" in cols:
            conn.execute(
                rf"UPDATE {tbl} SET drydock_id = 'dock_' || substr(drydock_id, 4) "
                rf"WHERE drydock_id LIKE 'ws\_%' ESCAPE '\'"
            )
        if "parent_drydock_id" in cols:
            conn.execute(
                rf"UPDATE {tbl} SET parent_drydock_id = 'dock_' || substr(parent_drydock_id, 4) "
                rf"WHERE parent_drydock_id LIKE 'ws\_%' ESCAPE '\'"
            )
    if "parent_drydock_id" in drydocks_cols or "parent_drydock_id" in {
        r[1] for r in conn.execute("PRAGMA table_info('drydocks')")
    }:
        conn.execute(
            r"UPDATE drydocks SET parent_drydock_id = 'dock_' || substr(parent_drydock_id, 4) "
            r"WHERE parent_drydock_id LIKE 'ws\_%' ESCAPE '\'"
        )

    # Rename audit event names: desk.* → drydock.*
    if "events" in tables:
        conn.execute(
            "UPDATE events SET event = 'drydock.' || substr(event, 6) "
            "WHERE event LIKE 'desk.%'"
        )

    # Rewrite stale ws_<slug> path fragments inside drydocks.worktree_path
    # and drydocks.config (JSON-serialized — overlay_path lives in here).
    # The filesystem-side migrate_v1_artifacts renames the actual paths on
    # disk; this fixes the registry rows so resume can find them.
    drydock_rows = conn.execute(
        "SELECT id, worktree_path, config FROM drydocks"
    ).fetchall()
    for row in drydock_rows:
        new_wt = (row[1] or "").replace("/worktrees/ws_", "/worktrees/dock_")
        old_cfg = row[2] or "{}"
        new_cfg = (
            old_cfg
            .replace("/overlays/ws_", "/overlays/dock_")
            .replace("/secrets/ws_", "/secrets/dock_")
            .replace("/worktrees/ws_", "/worktrees/dock_")
        )
        if new_wt != row[1] or new_cfg != old_cfg:
            conn.execute(
                "UPDATE drydocks SET worktree_path = ?, config = ? WHERE id = ?",
                (new_wt, new_cfg, row[0]),
            )

    # Rename indexes that hardcoded 'workspaces' in their name.
    for old_idx, new_idx, ddl in (
        (
            "idx_workspaces_yard_id",
            "idx_drydocks_yard_id",
            "CREATE INDEX IF NOT EXISTS idx_drydocks_yard_id ON drydocks (yard_id)",
        ),
    ):
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        if old_idx in existing:
            conn.execute(f"DROP INDEX {old_idx}")
        if new_idx not in existing:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column not present yet; later migration recreates


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
        # Pre-V6 vocabulary rename (workspace/desk/ws_ → drydock/dock_).
        # Must run BEFORE the SCHEMA executescript so we can detect the
        # legacy 'workspaces' table before CREATE TABLE IF NOT EXISTS
        # silently creates an empty 'drydocks' alongside it.
        _migrate_v1_vocab_to_drydock(self._conn)
        self._conn.executescript(SCHEMA)
        _migrate_to_v2(self._conn)
        _migrate_to_v3(self._conn)
        _migrate_to_v4(self._conn)
        _migrate_to_v5(self._conn)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def create_drydock(self, ws: Drydock) -> Drydock:
        existing = self.get_drydock(ws.name)
        if existing:
            raise WsError(
                f"Drydock '{ws.name}' already exists (state: {existing.state})",
                fix=f"Use a different name, or destroy it first: ws destroy {ws.name}",
            )
        self._conn.execute(
            """INSERT INTO drydocks
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
        self.log_event(ws.id, "drydock.created")
        return ws

    def get_drydock(self, name: str) -> Drydock | None:
        row = self._conn.execute(
            "SELECT * FROM drydocks WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_drydock(row)

    def get_children(self, parent_drydock_id: str) -> list[Drydock]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM drydocks
            WHERE parent_drydock_id = ?
            ORDER BY name ASC
            """,
            (parent_drydock_id,),
        ).fetchall()
        return [self._row_to_drydock(row) for row in rows]

    def list_drydocks(
        self,
        project: str | None = None,
        state: str | None = None,
    ) -> list[Drydock]:
        query = "SELECT * FROM drydocks WHERE 1=1"
        params: list = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if state:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY updated_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_drydock(r) for r in rows]

    def update_state(self, name: str, state: str) -> Drydock:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE drydocks SET state = ?, updated_at = ? WHERE name = ?",
            (state, now, name),
        )
        self._conn.commit()
        ws = self.get_drydock(name)
        if ws:
            self.log_event(ws.id, f"drydock.{state}")
        return ws

    def update_drydock(self, name: str, **fields) -> Drydock:
        if not fields:
            return self.get_drydock(name)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"])
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [name]
        self._conn.execute(
            f"UPDATE drydocks SET {set_clause} WHERE name = ?",
            values,
        )
        self._conn.commit()
        return self.get_drydock(name)

    def delete_drydock(self, name: str) -> None:
        ws = self.get_drydock(name)
        if ws:
            self.log_event(ws.id, "drydock.destroyed")
        self._conn.execute("DELETE FROM drydocks WHERE name = ?", (name,))
        self._conn.commit()

    def log_event(
        self, drydock_id: str, event: str, data: dict | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO events (drydock_id, event, timestamp, data) VALUES (?, ?, ?, ?)",
            (drydock_id, event, now, json.dumps(data or {})),
        )
        self._conn.commit()

    def insert_token(self, drydock_id: str, token_sha256: str, issued_at: datetime) -> None:
        self._conn.execute(
            """
            INSERT INTO tokens (drydock_id, token_sha256, issued_at, rotated_at)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(drydock_id) DO NOTHING
            """,
            (drydock_id, token_sha256, issued_at.isoformat()),
        )
        self._conn.commit()

    def find_desk_by_token_hash(self, token_sha256: str) -> str | None:
        row = self._conn.execute(
            "SELECT drydock_id FROM tokens WHERE token_sha256 = ?",
            (token_sha256,),
        ).fetchone()
        if row is None:
            return None
        return str(row["drydock_id"])

    def get_token_info(self, drydock_id: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT drydock_id, token_sha256, issued_at, rotated_at
            FROM tokens
            WHERE drydock_id = ?
            """,
            (drydock_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def delete_token(self, drydock_id: str) -> None:
        self._conn.execute("DELETE FROM tokens WHERE drydock_id = ?", (drydock_id,))
        self._conn.commit()

    def load_desk_policy(self, drydock_id: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT delegatable_firewall_domains, delegatable_secrets, capabilities,
                   delegatable_storage_scopes, delegatable_provision_scopes,
                   delegatable_network_reach, network_reach_ports,
                   resources_hard, config
            FROM drydocks
            WHERE id = ?
            """,
            (drydock_id,),
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
        resources_hard: dict | None = None,
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
        if resources_hard is not None:
            fields["resources_hard"] = json.dumps(resources_hard)
        if not fields:
            return
        self.update_drydock(name, **fields)

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
                (lease_id, drydock_id, type, scope, issued_at, expiry,
                 issuer, revoked, revocation_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease.lease_id,
                lease.drydock_id,
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
            SELECT lease_id, drydock_id, type, scope, issued_at, expiry,
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

    def revoke_leases_for_desk(self, drydock_id: str, reason: str) -> int:
        """Revoke every active lease belonging to a desk. Returns count."""
        cur = self._conn.execute(
            """
            UPDATE leases
            SET revoked = 1, revocation_reason = ?
            WHERE drydock_id = ? AND revoked = 0
            """,
            (reason, drydock_id),
        )
        self._conn.commit()
        return cur.rowcount

    def list_active_leases_for_desk(self, drydock_id: str) -> list[CapabilityLease]:
        rows = self._conn.execute(
            """
            SELECT lease_id, drydock_id, type, scope, issued_at, expiry,
                   issuer, revoked, revocation_reason
            FROM leases
            WHERE drydock_id = ? AND revoked = 0
            ORDER BY issued_at
            """,
            (drydock_id,),
        ).fetchall()
        return [_row_to_lease(row) for row in rows]

    def find_active_secret_lease(
        self, drydock_id: str, secret_name: str
    ) -> CapabilityLease | None:
        """Return any active lease for (drydock_id, secret_name) or None.

        Used by the daemon when releasing a lease to decide whether the
        materialized file at /run/secrets/<name> should also be removed
        (only when no other active lease still grants the same secret).
        """
        for lease in self.list_active_leases_for_desk(drydock_id):
            if lease.type == CapabilityType.SECRET and lease.scope.get("secret_name") == secret_name:
                return lease
        return None

    def find_active_storage_lease(self, drydock_id: str) -> CapabilityLease | None:
        """Return any active STORAGE_MOUNT lease for drydock_id, or None.

        STORAGE_MOUNT uses a single-lease-at-a-time semantic: materialized
        aws_* files are overwritten by the latest lease, so the daemon
        needs to know whether any storage lease is still live before
        cleaning up on release.
        """
        for lease in self.list_active_leases_for_desk(drydock_id):
            if lease.type == CapabilityType.STORAGE_MOUNT:
                return lease
        return None

    def find_active_aws_lease(self, drydock_id: str) -> CapabilityLease | None:
        """Any active STORAGE_MOUNT or INFRA_PROVISION lease for drydock_id.

        Both types materialize the same 4 aws_* files, so supersede /
        cleanup decisions must consider them together.
        """
        for lease in self.list_active_leases_for_desk(drydock_id):
            if lease.type in (CapabilityType.STORAGE_MOUNT, CapabilityType.INFRA_PROVISION):
                return lease
        return None

    # ------------------------------------------------------------------
    # Deskwatch events (v3)
    # ------------------------------------------------------------------

    def record_deskwatch_event(
        self,
        drydock_id: str,
        kind: str,
        name: str,
        status: str,
        detail: str | None = None,
        timestamp: str | None = None,
    ) -> int:
        """Append one deskwatch event. Returns the rowid."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO deskwatch_events (drydock_id, kind, name, timestamp, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (drydock_id, kind, name, ts, status, detail),
        )
        self._conn.commit()
        return cur.lastrowid

    def last_deskwatch_event(
        self, drydock_id: str, kind: str, name: str,
    ) -> dict | None:
        row = self._conn.execute(
            "SELECT timestamp, status, detail FROM deskwatch_events "
            "WHERE drydock_id = ? AND kind = ? AND name = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (drydock_id, kind, name),
        ).fetchone()
        return dict(row) if row else None

    def list_deskwatch_events(
        self, drydock_id: str, limit: int = 100,
    ) -> list[dict]:
        rows = self._conn.execute(
            "SELECT kind, name, timestamp, status, detail FROM deskwatch_events "
            "WHERE drydock_id = ? ORDER BY timestamp DESC LIMIT ?",
            (drydock_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_drydock_extra_mounts(self, name: str) -> list[str]:
        row = self._conn.execute(
            "SELECT config FROM drydocks WHERE name = ?",
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

    # ----- Yards (Phase Y0 of yard.md) -----

    def create_yard(
        self, name: str, *, repo_path: str | None = None, config: dict | None = None,
    ) -> dict:
        """Register a new Yard. Idempotent on (name); raises on duplicate."""
        existing = self.get_yard(name)
        if existing:
            raise WsError(
                f"Yard '{name}' already exists",
                fix=f"Use a different name, or destroy it first: ws yard destroy {name}",
            )
        slug = name.replace("-", "_").replace(" ", "_")
        yard_id = f"yd_{slug}"
        now = datetime.now(timezone.utc).isoformat()
        cfg_json = json.dumps(config or {})
        self._conn.execute(
            """INSERT INTO yards (id, name, repo_path, config, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (yard_id, name, repo_path, cfg_json, now, now),
        )
        self._conn.commit()
        return {
            "id": yard_id, "name": name, "repo_path": repo_path,
            "config": config or {}, "created_at": now, "updated_at": now,
        }

    def get_yard(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM yards WHERE name = ?", (name,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["config"] = json.loads(d["config"])
        return d

    def get_yard_by_id(self, yard_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM yards WHERE id = ?", (yard_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["config"] = json.loads(d["config"])
        return d

    def list_yards(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM yards ORDER BY created_at",
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["config"] = json.loads(d["config"])
            out.append(d)
        return out

    def list_yard_members(self, yard_id: str) -> list[Drydock]:
        rows = self._conn.execute(
            "SELECT * FROM drydocks WHERE yard_id = ? ORDER BY name",
            (yard_id,),
        ).fetchall()
        return [self._row_to_drydock(row) for row in rows]

    def destroy_yard(self, name: str, *, with_members: bool = False) -> int:
        """Remove a Yard. Refuses if members exist unless with_members=True
        (in which case sets their yard_id to NULL — does NOT destroy the
        member Drydocks themselves; that's a separate destructive op).
        Returns the number of members detached. Raises if Yard not found."""
        yard = self.get_yard(name)
        if yard is None:
            raise WsError(f"Yard '{name}' not found", fix="Check `ws yard list`")
        members = self.list_yard_members(yard["id"])
        if members and not with_members:
            raise WsError(
                f"Yard '{name}' has {len(members)} member drydock(s); refusing to destroy",
                fix=f"Re-run with --with-members to detach them, or remove members first",
            )
        for m in members:
            self._conn.execute(
                "UPDATE drydocks SET yard_id = NULL WHERE id = ?", (m.id,),
            )
        self._conn.execute("DELETE FROM yards WHERE id = ?", (yard["id"],))
        self._conn.commit()
        return len(members)

    # ----- Amendments (Phase A0 of amendment-contract.md) -----

    def create_amendment(
        self,
        *,
        kind: str,
        request: dict,
        proposed_by_type: str,
        proposed_by_id: str,
        drydock_id: str | None = None,
        yard_id: str | None = None,
        reason: str | None = None,
        tos_notes: str | None = None,
        expires_at: str | None = None,
        status: str = "pending",
    ) -> dict:
        """Insert a new amendment. Returns the created record (incl. id).

        ID format: am_<8-char-hex> (random) for V0; could become content-
        addressable hash later if dedup matters.
        """
        import uuid
        amendment_id = f"am_{uuid.uuid4().hex[:8]}"
        proposed_at = datetime.now(timezone.utc).isoformat()
        request_json = json.dumps(request)
        self._conn.execute(
            """INSERT INTO amendments
               (id, proposed_by_type, proposed_by_id, proposed_at,
                yard_id, drydock_id, kind, request_json, reason,
                tos_notes, status, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (amendment_id, proposed_by_type, proposed_by_id, proposed_at,
             yard_id, drydock_id, kind, request_json, reason,
             tos_notes, status, expires_at),
        )
        self._conn.commit()
        return self.get_amendment(amendment_id)

    def get_amendment(self, amendment_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM amendments WHERE id = ?", (amendment_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["request"] = json.loads(d.pop("request_json"))
        return d

    def list_amendments(
        self,
        *,
        status: str | None = None,
        drydock_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List amendments, optionally filtered. Newest first."""
        clauses = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if drydock_id:
            clauses.append("drydock_id = ?")
            params.append(drydock_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM amendments {where} "
            f"ORDER BY proposed_at DESC LIMIT ?",
            params,
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["request"] = json.loads(d.pop("request_json"))
            out.append(d)
        return out

    def update_amendment_status(
        self,
        amendment_id: str,
        *,
        status: str,
        reviewed_by: str | None = None,
        review_note: str | None = None,
        applied_at: str | None = None,
    ) -> dict:
        """Update amendment status + review fields. Returns updated record."""
        now = datetime.now(timezone.utc).isoformat()
        # Set reviewed_at if reviewed_by is provided AND it's not yet set
        existing = self.get_amendment(amendment_id)
        if existing is None:
            raise WsError(f"Amendment {amendment_id} not found",
                          fix="Check `ws amendment list` for valid IDs")
        reviewed_at = existing.get("reviewed_at")
        if reviewed_by and not reviewed_at:
            reviewed_at = now
        self._conn.execute(
            """UPDATE amendments
               SET status = ?,
                   reviewed_by = COALESCE(?, reviewed_by),
                   reviewed_at = COALESCE(?, reviewed_at),
                   review_note = COALESCE(?, review_note),
                   applied_at = COALESCE(?, applied_at)
               WHERE id = ?""",
            (status, reviewed_by, reviewed_at, review_note, applied_at,
             amendment_id),
        )
        self._conn.commit()
        return self.get_amendment(amendment_id)

    def expire_old_pending_amendments(
        self, *, now: datetime | None = None,
    ) -> int:
        """Mark expired any pending amendments past their expires_at.
        Returns count expired. Idempotent."""
        cutoff = (now or datetime.now(timezone.utc)).isoformat()
        cur = self._conn.execute(
            """UPDATE amendments
               SET status = 'expired'
               WHERE status IN ('pending', 'escalated')
                 AND expires_at IS NOT NULL
                 AND expires_at < ?""",
            (cutoff,),
        )
        self._conn.commit()
        return cur.rowcount

    def _row_to_drydock(self, row: sqlite3.Row) -> Drydock:
        d = dict(row)
        d["config"] = json.loads(d["config"])
        # Drop columns that the current Drydock dataclass doesn't accept.
        # Lets existing registries (with legacy columns like hostname/labels)
        # migrate forward without needing a schema rewrite.
        allowed = Drydock.__dataclass_fields__.keys()
        d = {k: v for k, v in d.items() if k in allowed}
        return Drydock(**d)


def _row_to_lease(row: sqlite3.Row) -> CapabilityLease:
    return CapabilityLease(
        lease_id=str(row["lease_id"]),
        drydock_id=str(row["drydock_id"]),
        type=CapabilityType(row["type"]),
        scope=json.loads(row["scope"]),
        issued_at=datetime.fromisoformat(row["issued_at"]),
        expiry=datetime.fromisoformat(row["expiry"]) if row["expiry"] else None,
        issuer=str(row["issuer"]),
        revoked=bool(row["revoked"]),
        revocation_reason=row["revocation_reason"],
    )
