"""Wrapper around the devcontainer CLI."""

import json
import logging
import subprocess

from . import WsError

logger = logging.getLogger(__name__)


def _parse_devcontainer_output(stdout: str) -> dict | None:
    """Parse devcontainer CLI JSON output.

    The CLI with --log-format json emits NDJSON log events followed by a final
    result object.  Try json.loads on the full string first (single object);
    fall back to scanning lines from the end for the last valid JSON object.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _extract_container_id(parsed: dict) -> str | None:
    cid = parsed.get("containerId")
    if cid:
        return cid
    outcome = parsed.get("outcome")
    if isinstance(outcome, dict):
        cid = outcome.get("containerId")
    return cid or None


class DevcontainerCLI:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def check_available(self):
        try:
            subprocess.run(
                ["devcontainer", "--version"],
                capture_output=True,
                check=True,
            )
        except FileNotFoundError:
            raise WsError(
                "devcontainer CLI not found",
                fix="Install it: npm install -g @devcontainers/cli",
            )

    def up(
        self,
        workspace_folder: str,
        override_config: str | None = None,
    ) -> dict:
        cmd = [
            "devcontainer", "up",
            "--workspace-folder", workspace_folder,
            "--log-format", "json",
        ]
        if override_config:
            cmd.extend(["--override-config", override_config])

        if self.dry_run:
            return {"dry_run": True, "command": cmd}

        result = subprocess.run(cmd, capture_output=True, text=True)
        stderr = result.stderr.strip()

        parsed = _parse_devcontainer_output(result.stdout)
        container_id = _extract_container_id(parsed) if parsed else None

        if result.returncode != 0 and not container_id:
            raise WsError(
                f"devcontainer up failed: {stderr}",
                fix="Check the project's devcontainer.json and Dockerfile for errors",
            )

        if parsed is None:
            return {"stdout": result.stdout, "exit_code": result.returncode}

        out = {**parsed, "exit_code": result.returncode}
        if container_id:
            out["container_id"] = container_id
        if result.returncode != 0:
            out["warning"] = f"lifecycle command failed (exit {result.returncode}): {stderr}" if stderr else f"lifecycle command failed (exit {result.returncode})"
        return out

    def stop(self, container_id: str) -> None:
        # devcontainer CLI has no 'down' subcommand; stop the runtime container directly.
        if not container_id:
            return
        if self.dry_run:
            return
        result = subprocess.run(
            ["docker", "stop", container_id], capture_output=True, text=True
        )
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise WsError(
                f"docker stop failed: {result.stderr.strip()}",
                fix=f"Stop manually: docker stop {container_id}",
            )

    def remove(self, container_id: str) -> None:
        """Remove a stopped container so the next devcontainer up rebuilds fresh.

        Without this, devcontainer CLI happily reuses a stopped container with
        matching labels, ignoring any overlay changes that have landed since.
        """
        if not container_id or self.dry_run:
            return
        result = subprocess.run(
            ["docker", "rm", container_id], capture_output=True, text=True
        )
        if result.returncode != 0 and "No such container" not in result.stderr:
            logger.warning(
                "docker rm failed for %s: %s", container_id, result.stderr.strip()
            )

    def tailnet_logout(self, container_id: str) -> None:
        """Ask tailscale to log out before stopping the container."""
        if not container_id or self.dry_run:
            return
        try:
            subprocess.run(
                ["docker", "exec", container_id, "sudo", "tailscale", "logout"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            logger.warning("tailnet logout failed for %s: %s", container_id, exc)

    def exec_command(
        self,
        workspace_folder: str,
        command: list[str],
    ) -> subprocess.CompletedProcess:
        cmd = [
            "devcontainer",
            "exec",
            "--workspace-folder",
            workspace_folder,
            *command,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)
