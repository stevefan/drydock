"""Best-effort Claude workspace-trust seeding for newly-created desks.

Without this, every desk recreate forces an interactive `claude` first run
to mark the workspace as trusted. The trust state lives at
/home/node/.claude/.claude.json (a shared Docker volume across desks).
We seed it via docker exec right after the container is up.

Failures are logged and swallowed — the desk is functional without trust
seeded; the user just falls back to manual one-time accept.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CLAUDE_JSON_PATH = "/home/node/.claude/.claude.json"
PROBE_TIMEOUT = 10


def _read_workspace_folder_from_overlay(overlay_path: str | Path | None) -> str:
    if not overlay_path:
        return "/workspace"
    try:
        with open(overlay_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "/workspace"
    folder = data.get("workspaceFolder")
    return folder if isinstance(folder, str) and folder else "/workspace"


def _docker_exec(container_id: str, *args: str, input_text: str | None = None,
                 user: str = "node"):
    cmd = ["docker", "exec", "--user", user, container_id, *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=PROBE_TIMEOUT,
    )


def _read_existing(container_id: str) -> dict:
    """Read the in-container .claude.json. Returns {} on missing/invalid."""
    result = _docker_exec(container_id, "cat", CLAUDE_JSON_PATH)
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "trust: existing %s is not valid JSON; will overwrite", CLAUDE_JSON_PATH,
        )
        return {}
    return data if isinstance(data, dict) else {}


def _already_trusted(data: dict, workspace_folder: str) -> bool:
    trusted = data.get("trustedWorkspaces")
    if isinstance(trusted, dict):
        return workspace_folder in trusted
    if isinstance(trusted, list):
        for entry in trusted:
            if isinstance(entry, str) and entry == workspace_folder:
                return True
            if isinstance(entry, dict):
                for key in ("path", "workspaceFolder"):
                    if entry.get(key) == workspace_folder:
                        return True
    return False


def seed_workspace_trust(container_id: str, workspace_folder: str) -> bool:
    """Idempotently mark workspace_folder as trusted in the desk's claude config.

    Returns True on success or already-trusted; False on any failure (logged).
    """
    if not container_id or not workspace_folder:
        return False

    try:
        existing = _read_existing(container_id)
        if _already_trusted(existing, workspace_folder):
            logger.debug("trust: %s already trusted in %s", workspace_folder, container_id)
            return True

        trusted = existing.get("trustedWorkspaces")
        if not isinstance(trusted, dict):
            trusted = {}
        trusted[workspace_folder] = {"trusted": True}
        existing["trustedWorkspaces"] = trusted

        payload = json.dumps(existing, indent=2)
        # Ensure the .claude dir exists, then overwrite the file. Run as root
        # for mkdir (the ~/.claude dir may not exist or be node-owned), then
        # chown back to node.
        mk = subprocess.run(
            ["docker", "exec", "--user", "root", container_id,
             "mkdir", "-p", "/home/node/.claude"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
        if mk.returncode != 0:
            logger.warning("trust: mkdir failed in %s: %s", container_id, mk.stderr.strip())
            return False

        write = subprocess.run(
            ["docker", "exec", "-i", "--user", "node", container_id,
             "sh", "-c", f"cat > {CLAUDE_JSON_PATH}"],
            input=payload, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
        if write.returncode != 0:
            logger.warning("trust: write failed in %s: %s", container_id, write.stderr.strip())
            return False

        logger.info("trust: seeded %s in %s", workspace_folder, container_id)
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("trust: seeding failed for %s: %s", container_id, exc)
        return False
