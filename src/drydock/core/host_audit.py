"""Host-state audit — answer "what's actually live on this Harbor?"

Six layers, each independently gathered and resilient to partial failure
(any single gather failing yields a {"ok": False, "error": ...} sub-block;
the audit as a whole still succeeds). The point is closing the gap
between "code committed in main" and "code actually running on this
specific Harbor right now."

Layers, in order of "where things drift":

1. **code**       — installed package version, install location, git SHA
                    if editable, ahead/behind origin, dirty flag.
2. **daemon**     — wsd running? pid + socket + health probe.
3. **capability** — supported capability types (introspected from the
                    same code this CLI was loaded from), reserved-but-
                    unsupported types.
4. **base_images**— drydock-base image tags pulled locally + how many
                    desks reference each tag.
5. **desks**      — per-desk: state, container, base-tag actually used
                    in the desk's Dockerfile, capability gates declared,
                    secret slot count, project-YAML mtime (drift hint).
6. **leases**     — count of active vs revoked from the broker table.

Optional `--probe-desks` adds a seventh:

7. **helpers**    — for each running desk, docker-exec test for presence
                    of the in-container scripts (sync-claude-auth.sh,
                    add-allowed-domain.sh, etc.). Detects "feature
                    designed and merged but base-image not rebuilt".

Returns a single dict shape that the CLI formats for human or JSON output.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import drydock
from drydock.core.project_config import load_project_config
from drydock.core.registry import Registry


# Reuse upgrade.py's image-ref vocabulary so audit and upgrade agree.
_BASE_IMAGE_REF = "ghcr.io/stevefan/drydock-base"
_FROM_LINE_RE = re.compile(
    r"^\s*FROM\s+" + re.escape(_BASE_IMAGE_REF) + r":(\S+)",
    re.IGNORECASE | re.MULTILINE,
)
_DEFAULT_DEVCONTAINER_SUBPATH = ".devcontainer"

# In-container scripts whose presence the --probe-desks flag tests.
# Maps script name → which feature its absence suggests is unwired.
_HELPER_SCRIPTS = {
    "/usr/local/bin/init-firewall.sh":             "default-deny firewall (core)",
    "/usr/local/bin/start-tailscale.sh":           "tailnet identity (core)",
    "/usr/local/bin/start-remote-control.sh":      "claude remote-control (core)",
    "/usr/local/bin/sync-claude-auth.sh":          "claude auth materialization (core)",
    "/usr/local/bin/refresh-firewall-allowlist.sh": "CDN-rotation refresher (core)",
    "/usr/local/bin/add-allowed-domain.sh":        "NETWORK_REACH dynamic firewall opens",
    "/usr/local/bin/sync-aws-auth.sh":             "AWS STS lease materialization",
    "/usr/local/bin/setup-storage-mounts.sh":      "S3 storage mounts",
}


def gather_audit(*, probe_desks: bool = False) -> dict:
    """Top-level entry. Returns the full audit shape."""
    return {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "code":         _safe(_gather_code),
        "daemon":       _safe(_gather_daemon),
        "capability":   _safe(_gather_capability_surface),
        "base_images":  _safe(_gather_base_images),
        "desks":        _safe(_gather_desks),
        "leases":       _safe(_gather_leases),
        "helpers":      _safe(_gather_helpers) if probe_desks else None,
    }


def _safe(fn) -> dict:
    """Run a gather function; convert any exception to a structured error."""
    try:
        result = fn()
        if isinstance(result, dict):
            result.setdefault("ok", True)
        return result
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------- code layer ----------------

def _gather_code() -> dict:
    src_path = Path(drydock.__file__).resolve()
    install_root = src_path.parent.parent  # …/site-packages or …/src

    # Editable install signature: pyproject.toml or setup.py near the source.
    # Walk up from src_path looking for the repo root (nearest .git).
    repo_root = None
    for candidate in [src_path] + list(src_path.parents):
        if (candidate / ".git").exists():
            repo_root = candidate
            break
    editable = repo_root is not None

    out: dict = {
        "package_version": getattr(drydock, "__version__", "?"),
        "install_location": str(install_root),
        "editable": editable,
    }

    if not repo_root:
        return out

    out["repo_root"] = str(repo_root)
    sha = _git(repo_root, "rev-parse", "HEAD")
    out["git_sha"] = sha[:12] if sha else None
    out["git_branch"] = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    dirty = _git(repo_root, "status", "--porcelain")
    out["dirty"] = bool(dirty and dirty.strip())

    # ahead/behind origin/main without a network fetch — purely local rev counts.
    rl = _git(repo_root, "rev-list", "--left-right", "--count", "origin/main...HEAD")
    if rl:
        try:
            behind, ahead = rl.split()
            out["behind_origin_main"] = int(behind)
            out["ahead_origin_main"] = int(ahead)
        except (ValueError, TypeError):
            pass
    return out


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# ---------------- daemon layer ----------------

def _gather_daemon() -> dict:
    # Reuse cli/daemon.py's status logic so we don't drift from `ws daemon status`.
    from drydock.cli.daemon import _daemon_status, _socket_path, _log_path
    socket = _socket_path()
    log = _log_path()
    status = _daemon_status(socket, log)

    # Augment with uptime if pid present.
    if status.get("pid"):
        uptime = _process_uptime(status["pid"])
        if uptime:
            status["uptime_seconds"] = uptime
            status["uptime_human"] = _format_duration(uptime)
    return status


def _process_uptime(pid: int) -> int | None:
    """Seconds since process start. Best-effort; macOS + Linux."""
    try:
        # POSIX-portable: ps -o etime returns elapsed time in [[dd-]hh:]mm:ss.
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return _parse_etime(r.stdout.strip())


def _parse_etime(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(p) for p in s.split(":")]
    if len(parts) == 2:
        h, m, sec = 0, parts[0], parts[1]
    elif len(parts) == 3:
        h, m, sec = parts
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + sec


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


# ---------------- capability layer ----------------

def _gather_capability_surface() -> dict:
    """Introspect what the code we just imported actually supports."""
    from drydock.core.capability import CapabilityType
    from drydock.wsd.capability_handlers import _SUPPORTED_CAPABILITY_TYPES
    from drydock.core.policy import CapabilityKind

    all_types = sorted(ct.value for ct in CapabilityType)
    supported = sorted(_SUPPORTED_CAPABILITY_TYPES)
    reserved = sorted(set(all_types) - set(supported))
    return {
        "supported_types": supported,
        "reserved_types": reserved,
        "capability_kinds": sorted(ck.value for ck in CapabilityKind),
    }


# ---------------- base images layer ----------------

def _gather_base_images() -> dict:
    """drydock-base tags pulled locally; cross-referenced against desks."""
    try:
        r = subprocess.run(
            ["docker", "images", _BASE_IMAGE_REF, "--format",
             "{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "docker not available"}
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip()}

    tags: list[dict] = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] != "<none>":
            tags.append({"tag": parts[0], "id": parts[1], "created": parts[2]})
    return {"ref": _BASE_IMAGE_REF, "tags": tags}


# ---------------- desks layer ----------------

def _gather_desks() -> dict:
    registry = Registry()
    try:
        workspaces = registry.list_workspaces()
        desks: list[dict] = []
        for ws in workspaces:
            desks.append(_describe_desk(registry, ws))
        return {"count": len(desks), "desks": desks}
    finally:
        registry.close()


def _describe_desk(registry: Registry, ws) -> dict:
    proj_cfg = None
    try:
        proj_cfg = load_project_config(ws.project)
    except Exception:
        proj_cfg = None

    base_tag = _read_desk_base_tag(ws, proj_cfg)
    yaml_mtime = _project_yaml_mtime(ws.project)
    secrets_count = _count_secrets(ws.id)
    pol = registry.load_desk_policy(ws.id) or {}

    capabilities = _safe_json_list(pol.get("capabilities"))
    return {
        "name": ws.name,
        "id": ws.id,
        "project": ws.project,
        "state": ws.state,
        "container_id": (ws.container_id or "")[:12] if ws.container_id else None,
        "worktree": ws.worktree_path,
        "base_image_tag": base_tag,
        "capabilities": capabilities,
        "delegatable_secrets":         _safe_json_list(pol.get("delegatable_secrets")),
        "delegatable_storage_scopes":  _safe_json_list(pol.get("delegatable_storage_scopes")),
        "delegatable_network_reach":   _safe_json_list(pol.get("delegatable_network_reach")),
        "network_reach_ports":         _safe_json_list(pol.get("network_reach_ports")),
        "secrets_count": secrets_count,
        "project_yaml_mtime": yaml_mtime,
    }


def _read_desk_base_tag(ws, proj_cfg) -> str | None:
    """Parse the FROM ghcr.io/stevefan/drydock-base:<tag> line in the desk's
    Dockerfile. Returns None if the file isn't there or doesn't reference
    the base image (e.g. project uses a custom image)."""
    if not ws.repo_path:
        return None
    workspace_subdir = (proj_cfg.workspace_subdir if proj_cfg and proj_cfg.workspace_subdir else "")
    devcontainer_subpath = (
        proj_cfg.devcontainer_subpath
        if proj_cfg and proj_cfg.devcontainer_subpath is not None
        else _DEFAULT_DEVCONTAINER_SUBPATH
    )
    parts = [ws.repo_path]
    if workspace_subdir:
        parts.append(workspace_subdir)
    parts.append(devcontainer_subpath)
    parts.append("Dockerfile")
    dockerfile = Path(*parts)
    if not dockerfile.exists():
        return None
    try:
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FROM_LINE_RE.search(text)
    return m.group(1) if m else None


def _project_yaml_mtime(project: str) -> str | None:
    path = Path.home() / ".drydock" / "projects" / f"{project}.yaml"
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _count_secrets(desk_id: str) -> int:
    secrets_dir = Path.home() / ".drydock" / "secrets" / desk_id
    if not secrets_dir.exists():
        return 0
    try:
        return sum(1 for p in secrets_dir.iterdir() if p.is_file())
    except OSError:
        return 0


def _safe_json_list(s: object) -> list:
    if not s:
        return []
    if isinstance(s, list):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------- leases layer ----------------

def _gather_leases() -> dict:
    registry = Registry()
    try:
        cur = registry._conn.execute(
            "SELECT type, revoked, COUNT(*) AS n FROM leases GROUP BY type, revoked"
        )
        active_by_type: dict[str, int] = {}
        revoked_by_type: dict[str, int] = {}
        total_active = 0
        total_revoked = 0
        for row in cur.fetchall():
            t = row["type"]
            n = row["n"]
            if row["revoked"]:
                revoked_by_type[t] = revoked_by_type.get(t, 0) + n
                total_revoked += n
            else:
                active_by_type[t] = active_by_type.get(t, 0) + n
                total_active += n
        return {
            "active_total": total_active,
            "revoked_total": total_revoked,
            "active_by_type": active_by_type,
            "revoked_by_type": revoked_by_type,
        }
    finally:
        registry.close()


# ---------------- helpers layer (probe desks) ----------------

def _gather_helpers() -> dict:
    """Probe one running desk per project (cheap sample) for in-container
    helper presence. Detects "feature in main but base-image not rebuilt"."""
    registry = Registry()
    try:
        workspaces = registry.list_workspaces()
    finally:
        registry.close()

    running = [ws for ws in workspaces if ws.container_id]
    if not running:
        return {"probed": [], "note": "no running desks to probe"}

    # Sample up to one per project to keep this fast.
    seen_projects: set[str] = set()
    sample = []
    for ws in running:
        if ws.project not in seen_projects:
            sample.append(ws)
            seen_projects.add(ws.project)

    probed = []
    for ws in sample:
        results: dict[str, bool] = {}
        for path in _HELPER_SCRIPTS:
            results[path] = _container_has_executable(ws.container_id, path)
        missing = [p for p, ok in results.items() if not ok]
        probed.append({
            "desk": ws.name,
            "project": ws.project,
            "container_id": ws.container_id[:12],
            "scripts": results,
            "missing": missing,
            "feature_implications": [
                _HELPER_SCRIPTS[p] for p in missing
            ],
        })
    return {"probed": probed, "sample_size": len(sample),
            "total_running": len(running)}


def _container_has_executable(container_id: str, path: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "exec", container_id, "test", "-x", path],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0
