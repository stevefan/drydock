"""Wrapper around the devcontainer CLI."""

import json
import subprocess

from .errors import WsError


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
        if result.returncode != 0:
            raise WsError(
                f"devcontainer up failed: {result.stderr.strip()}",
                fix="Check the project's devcontainer.json and Dockerfile for errors",
            )
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"stdout": result.stdout, "returncode": result.returncode}

        container_id = parsed.get("containerId")
        if container_id:
            return {"container_id": container_id, **parsed}
        return parsed

    def stop(self, workspace_folder: str) -> None:
        cmd = ["devcontainer", "down", "--workspace-folder", workspace_folder]
        if self.dry_run:
            return
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise WsError(
                f"devcontainer down failed: {result.stderr.strip()}",
                fix=f"Try stopping manually: docker stop <container_id>",
            )

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
