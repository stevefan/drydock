"""ws status — per-drydock health overview."""

import json
import logging
import re
import subprocess
from pathlib import Path

import click

from drydock.core import WsError
from drydock.core.compliance import days_until_review, load_compliance
from drydock.core.devcontainer import DevcontainerCLI


logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 5
REFRESH_SENTINEL_EXIT = 42


def _effective_workspace_folder(ws) -> str:
    return str(
        Path(ws.worktree_path) / ws.workspace_subdir
        if ws.workspace_subdir
        else Path(ws.worktree_path)
    )


def _read_workspace_folder(ws) -> str:
    overlay_path = ws.config.get("overlay_path", "")
    if not overlay_path:
        return "/drydock"
    try:
        with open(overlay_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("status: failed reading overlay for %s: %s", ws.name, exc)
        return "/drydock"
    return data.get("drydockFolder", "/drydock")


def _docker_container_id(*candidate_paths: str) -> str:
    """Find a running container by devcontainer.local_folder label.

    Tries each candidate path in order and returns the first match. The
    label is set at container creation and can be either the bare
    worktree or worktree+subdir depending on how drydock spawned it;
    callers pass both candidates so we find the container either way.
    """
    for path in candidate_paths:
        if not path:
            continue
        try:
            result = subprocess.run(
                [
                    "docker", "ps", "-q",
                    "--filter", f"label=devcontainer.local_folder={path}",
                ],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT,
            )
            cid = result.stdout.strip().split("\n")[0].strip()
            if cid:
                return cid
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("status: container lookup failed for %s: %s", path, exc)
    return ""


def _probe_tailscale(container_id: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "tailscale", "status"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("status: tailscale probe failed for %s: %s", container_id, exc)
        return False


def _probe_supervisor(container_id: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "pgrep", "-f", "start-remote-control"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("status: supervisor probe failed for %s: %s", container_id, exc)
        return False


def _probe_firewall(container_id: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "sudo", "iptables", "-L", "OUTPUT"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        return result.returncode == 0 and "DROP" in result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("status: firewall probe failed for %s: %s", container_id, exc)
        return False


def _exec_in_drydock(ws, command: list[str], devcontainer: DevcontainerCLI | None = None):
    if not ws.worktree_path:
        return None
    devcontainer = devcontainer or DevcontainerCLI()
    try:
        return devcontainer.exec_command(_effective_workspace_folder(ws), command)
    except OSError as exc:
        logger.debug("status: devcontainer exec failed for %s: %s", ws.name, exc)
        return None


def _probe_refresh_supervisor(
    ws,
    firewall_status: str,
    devcontainer: DevcontainerCLI | None = None,
) -> str:
    if firewall_status != "active":
        return "not_applicable"
    result = _exec_in_drydock(
        ws,
        [
            "sh",
            "-lc",
            (
                "if [ ! -x /usr/local/bin/refresh-firewall-allowlist.sh ]; then "
                f"exit {REFRESH_SENTINEL_EXIT}; "
                "fi; pgrep -f refresh-firewall-allowlist.sh"
            ),
        ],
        devcontainer,
    )
    if result is None:
        return "not_applicable"
    if result.returncode == REFRESH_SENTINEL_EXIT:
        return "not_applicable"
    if result.returncode == 0 and result.stdout.strip():
        return "alive"
    return "dead"


def _probe_ipset(ws, devcontainer: DevcontainerCLI | None = None) -> dict[str, int] | None:
    result = _exec_in_drydock(
        ws,
        ["ipset", "list", "allowed-domains", "-t"],
        devcontainer,
    )
    if result is None or result.returncode != 0:
        return None
    size_match = re.search(r"Number of entries:\s*(\d+)", result.stdout)
    max_match = re.search(r"\bmaxelem\s+(\d+)", result.stdout)
    if not size_match or not max_match:
        logger.debug("status: unparseable ipset output for %s", ws.name)
        return None
    return {"size": int(size_match.group(1)), "max": int(max_match.group(1))}


def _trusted_drydock_entry_matches(entry, workspace_folder: str) -> bool | None:
    if isinstance(entry, str):
        return entry == workspace_folder
    if isinstance(entry, dict):
        for key in ("path", "drydockFolder"):
            value = entry.get(key)
            if isinstance(value, str):
                return value == workspace_folder
    return None


def _probe_trust_accepted(
    ws,
    devcontainer: DevcontainerCLI | None = None,
) -> bool | None:
    result = _exec_in_drydock(
        ws,
        ["cat", "/home/node/.claude/.claude.json"],
        devcontainer,
    )
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.debug("status: invalid claude config for %s: %s", ws.name, exc)
        return None

    trusted = data.get("trustedWorkspaces")
    if trusted is None:
        logger.debug("status: unrecognized claude trust schema for %s", ws.name)
        return None

    workspace_folder = _read_workspace_folder(ws)
    if isinstance(trusted, dict):
        return workspace_folder in trusted
    if isinstance(trusted, list):
        saw_recognized = False
        for entry in trusted:
            matched = _trusted_drydock_entry_matches(entry, workspace_folder)
            if matched is None:
                continue
            saw_recognized = True
            if matched:
                return True
        return False if saw_recognized else None

    logger.debug("status: unsupported trustedWorkspaces type for %s", ws.name)
    return None


def _docker_inspect_value(container_id: str, template: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "inspect", container_id, "--format", template],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("status: docker inspect failed for %s: %s", container_id, exc)
        return ""
    if result.returncode != 0:
        logger.debug(
            "status: docker inspect returned %s for %s: %s",
            result.returncode,
            container_id,
            result.stderr.strip(),
        )
        return ""
    value = result.stdout.strip()
    return "" if value == "<no value>" else value


def _dockerfile_from_overlay(ws) -> Path | None:
    overlay_path = ws.config.get("overlay_path", "")
    if not overlay_path:
        return None
    try:
        with open(overlay_path) as f:
            overlay = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("status: failed reading overlay dockerfile for %s: %s", ws.name, exc)
        return None
    build = overlay.get("build")
    if isinstance(build, dict):
        dockerfile = build.get("dockerfile")
        if isinstance(dockerfile, str) and dockerfile:
            return Path(dockerfile)
    dockerfile = overlay.get("dockerFile")
    if isinstance(dockerfile, str) and dockerfile:
        path = Path(dockerfile)
        return path if path.is_absolute() else Path(overlay_path).parent / path
    return None


def _parse_dockerfile_from(dockerfile_path: Path) -> str | None:
    try:
        with dockerfile_path.open() as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"FROM\s+([^\s]+)", line, re.IGNORECASE)
                if match:
                    return match.group(1)
    except OSError as exc:
        logger.debug("status: failed reading Dockerfile %s: %s", dockerfile_path, exc)
        return None
    return None


def _probe_base_image(ws, container_id: str = "") -> str | None:
    if not container_id:
        return None

    label_ref = _docker_inspect_value(
        container_id,
        '{{index .Config.Labels "org.opencontainers.image.ref.name"}}',
    )
    if label_ref:
        return label_ref

    config_image = _docker_inspect_value(container_id, "{{.Config.Image}}")
    if "drydock-base" in config_image:
        return config_image

    dockerfile_path = _dockerfile_from_overlay(ws)
    if dockerfile_path is None:
        return None
    return _parse_dockerfile_from(dockerfile_path)


# Log files drydock-base and common project setups write during postCreate /
# postStart. If the container has them, we tail them and flag ones whose last
# lines smell like an error. Generic enough that projects get this for free
# without declaring anything in their YAML.
_INIT_LOG_PATHS = (
    "/tmp/pip-install.log",        # devcontainer postCreate pip editable install
    "/tmp/storage-mounts.log",     # drydock-base storage_mounts setup + refresh
    "/tmp/firewall-refresh.log",   # drydock-base firewall-allowlist refresh loop
    "/tmp/remote-control.log",     # drydock-base claude remote-control boot
)

# Substrings that, if present in the last non-empty line, mean the init step
# failed or is loudly unhealthy. Conservative — we'd rather miss than cry wolf.
_INIT_LOG_ERROR_MARKERS = ("Traceback", "ERROR", "error:", "FATAL", "No such")


def _probe_init_logs(container_id: str) -> dict[str, str] | None:
    """Scan known init-log paths; return {basename: status} where status is
    'ok', 'errored: <last line>', or 'missing'. None if container isn't
    reachable.
    """
    if not container_id:
        return None
    results: dict[str, str] = {}
    for path in _INIT_LOG_PATHS:
        try:
            probe = subprocess.run(
                ["docker", "exec", container_id,
                 "sh", "-c", f"test -f {path} && tail -n 20 {path} || echo __MISSING__"],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("status: init-log probe failed for %s: %s", path, exc)
            continue
        basename = path.rsplit("/", 1)[-1]
        out = probe.stdout.strip()
        if out == "__MISSING__" or not out:
            continue  # skip logs the project doesn't produce
        last_line = next(
            (line for line in reversed(out.splitlines()) if line.strip()),
            "",
        )
        if any(m in out for m in _INIT_LOG_ERROR_MARKERS):
            results[basename] = f"errored: {last_line[:120]}"
        else:
            results[basename] = "ok"
    return results or None


def _probe_compliance(ws) -> str | None:
    if not ws.worktree_path:
        return None
    try:
        cfg = load_compliance(Path(ws.worktree_path))
    except WsError as exc:
        return f"error: {exc.message}"
    if cfg is None:
        return None
    if cfg.last_reviewed is None or cfg.review_cadence_days is None:
        return "incomplete"
    days = days_until_review(cfg)
    if days is None:
        return "incomplete"
    if days < 0:
        return f"stale ({-days} days overdue)"
    return "ok"


def _probe_drydock(ws) -> dict:
    row = {
        "name": ws.name,
        "state": ws.state,
        "container": "not found",
        "tailscale": "unknown",
        "supervisor": "unknown",
        "firewall": "unknown",
        "refresh_supervisor": "not_applicable",
        "ipset": None,
        "trust_accepted": None,
        "base_image": None,
        "init_logs": None,
        "compliance": _probe_compliance(ws),
    }

    if not ws.worktree_path:
        return row

    cid = _docker_container_id(_effective_workspace_folder(ws), ws.worktree_path)
    if not cid:
        row["container"] = "not found"
        return row

    row["container"] = "running"
    row["tailscale"] = "joined" if _probe_tailscale(cid) else "disconnected"
    row["supervisor"] = "alive" if _probe_supervisor(cid) else "dead"
    row["firewall"] = "active" if _probe_firewall(cid) else "inactive"

    devcontainer = DevcontainerCLI()
    row["refresh_supervisor"] = _probe_refresh_supervisor(
        ws, row["firewall"], devcontainer
    )
    row["ipset"] = _probe_ipset(ws, devcontainer)
    row["trust_accepted"] = _probe_trust_accepted(ws, devcontainer)
    row["base_image"] = _probe_base_image(ws, cid)
    row["init_logs"] = _probe_init_logs(cid)
    return row


@click.command()
@click.pass_context
def status(ctx):
    """Show per-drydock health status."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    drydocks = registry.list_drydocks()
    rows = [_probe_drydock(ws) for ws in drydocks]

    out.table(
        rows,
        columns=[
            "name",
            "state",
            "container",
            "tailscale",
            "supervisor",
            "firewall",
            "refresh_supervisor",
            "ipset",
            "trust_accepted",
            "base_image",
            "init_logs",
            "compliance",
        ],
    )
