"""Fleet monitor — peer-Harbor probes over SSH.

V1 channel is plain SSH: each probe shells out to ``ssh <target> 'ws ... --json'``
on the remote Harbor. This reuses existing SSH-key topology (already proven by
``scripts/mac/claude-refresh.sh``) and avoids extending ``wsd`` with a
tailnet-facing listener for the first cut. Upgrade path: tailnet-served HTTPS
to the wsd Unix socket once per-call SSH overhead actually hurts.

Probe set (V1):

* ``daemon`` — ``ws daemon status`` round-trip; verifies wsd is responsive.
* ``desks`` — ``ws list``; rolls up state per declared peer-desk.
* ``deskwatch`` — ``ws deskwatch <desk>``; per-Dock workload health.
* ``cc_liveness`` — ``ws exec <desk> -- timeout 30 claude -p ":"``; the
  headline probe — distinguishes "container up" from "container up but
  Claude OAuth is dead". A failure here is what the auth-broker exists to
  remediate.

Peer config lives at ``~/.drydock/fleet/peers.yaml``::

    peers:
      - host: hetzner            # ssh target (resolves via .ssh/config)
        ssh_user: root           # optional; if set, becomes user@host
        desks: ["*"]             # "*" or list of desk names
      - host: my-mac.tailnet
        desks: [auction-crawl, infra]
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from drydock.core import WsError


CONFIG_PATH = Path.home() / ".drydock" / "fleet" / "peers.yaml"
SSH_TIMEOUT_DEFAULT = 15  # seconds; covers slow tailnet links without hanging
CC_PROBE_TIMEOUT = 35     # claude -p ":" warm-up budget + ssh overhead


@dataclass
class PeerSpec:
    host: str
    ssh_user: str | None = None
    desks: list[str] = field(default_factory=lambda: ["*"])

    @property
    def ssh_target(self) -> str:
        return f"{self.ssh_user}@{self.host}" if self.ssh_user else self.host


@dataclass
class ProbeResult:
    peer: str
    desk: str | None      # None for peer-level probes (daemon, desks)
    kind: str             # daemon | desks | deskwatch | cc_liveness
    status: str           # ok | failed | unreachable
    detail: str = ""
    elapsed_ms: int = 0
    data: dict | None = None  # parsed JSON from remote when relevant

    def to_dict(self) -> dict:
        return {
            "peer": self.peer,
            "desk": self.desk,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "data": self.data,
        }


def load_peers(path: Path = CONFIG_PATH) -> list[PeerSpec]:
    if not path.exists():
        raise WsError(
            f"Fleet config not found at {path}",
            fix=f"Create {path} with a 'peers:' block — see docs/design/fleet-monitor.md",
            code="fleet_config_missing",
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise WsError(
            f"Fleet config at {path} is not valid YAML: {e}",
            fix="Fix the YAML syntax",
            code="fleet_config_invalid",
        )
    peers_raw = raw.get("peers", [])
    if not isinstance(peers_raw, list):
        raise WsError(
            f"Fleet config 'peers' must be a list (got {type(peers_raw).__name__})",
            fix="See docs/design/fleet-monitor.md for the schema",
            code="fleet_config_invalid",
        )
    peers = []
    for i, entry in enumerate(peers_raw):
        if not isinstance(entry, dict) or "host" not in entry:
            raise WsError(
                f"Fleet config peer #{i} missing required 'host' field",
                fix="Each peer needs at least: '- host: <ssh-target>'",
                code="fleet_config_invalid",
            )
        peers.append(PeerSpec(
            host=entry["host"],
            ssh_user=entry.get("ssh_user"),
            desks=entry.get("desks", ["*"]),
        ))
    return peers


def _run_ssh(target: str, remote_cmd: str, timeout: int) -> tuple[int, str, str]:
    """Run a remote command via SSH; returns (exit_code, stdout, stderr).

    BatchMode=yes prevents password prompts hanging the monitor; we only
    support key-based auth here (matches claude-refresh.sh's contract).
    """
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={min(timeout, 10)}",
        "-o", "StrictHostKeyChecking=accept-new",
        target,
        remote_cmd,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh timed out after {timeout}s"
    except OSError as e:
        return 255, "", f"ssh invocation failed: {e}"


def probe_daemon(peer: PeerSpec) -> ProbeResult:
    t0 = time.monotonic()
    code, stdout, stderr = _run_ssh(
        peer.ssh_target, "ws daemon status --json", SSH_TIMEOUT_DEFAULT,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    if code == 124 or code == 255:
        return ProbeResult(
            peer=peer.host, desk=None, kind="daemon",
            status="unreachable", detail=stderr.strip() or f"ssh exit {code}",
            elapsed_ms=elapsed,
        )
    if code != 0:
        return ProbeResult(
            peer=peer.host, desk=None, kind="daemon",
            status="failed",
            detail=(stderr or stdout).strip().splitlines()[-1] if (stderr or stdout).strip() else f"exit {code}",
            elapsed_ms=elapsed,
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return ProbeResult(
            peer=peer.host, desk=None, kind="daemon",
            status="failed", detail="non-JSON response from ws daemon status",
            elapsed_ms=elapsed,
        )
    return ProbeResult(
        peer=peer.host, desk=None, kind="daemon",
        status="ok", detail=f"pid={data.get('pid', '?')}",
        elapsed_ms=elapsed, data=data,
    )


def probe_desks(peer: PeerSpec) -> ProbeResult:
    t0 = time.monotonic()
    code, stdout, stderr = _run_ssh(
        peer.ssh_target, "ws list --json", SSH_TIMEOUT_DEFAULT,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    if code != 0:
        return ProbeResult(
            peer=peer.host, desk=None, kind="desks",
            status="unreachable" if code in (124, 255) else "failed",
            detail=(stderr or stdout).strip()[:200] or f"exit {code}",
            elapsed_ms=elapsed,
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return ProbeResult(
            peer=peer.host, desk=None, kind="desks",
            status="failed", detail="non-JSON response from ws list",
            elapsed_ms=elapsed,
        )
    desks = data if isinstance(data, list) else data.get("workspaces", [])
    return ProbeResult(
        peer=peer.host, desk=None, kind="desks",
        status="ok", detail=f"{len(desks)} desk(s)",
        elapsed_ms=elapsed, data={"desks": desks},
    )


def probe_deskwatch(peer: PeerSpec, desk: str) -> ProbeResult:
    t0 = time.monotonic()
    code, stdout, stderr = _run_ssh(
        peer.ssh_target, f"ws deskwatch {shlex.quote(desk)} --json",
        SSH_TIMEOUT_DEFAULT,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    # ws deskwatch exits 1 on violations — that's a "failed probe" not "unreachable"
    if code in (124, 255):
        return ProbeResult(
            peer=peer.host, desk=desk, kind="deskwatch",
            status="unreachable", detail=stderr.strip() or f"ssh exit {code}",
            elapsed_ms=elapsed,
        )
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        return ProbeResult(
            peer=peer.host, desk=desk, kind="deskwatch",
            status="failed", detail="non-JSON response from ws deskwatch",
            elapsed_ms=elapsed,
        )
    healthy = data.get("healthy", code == 0)
    violations = data.get("violations", 0)
    return ProbeResult(
        peer=peer.host, desk=desk, kind="deskwatch",
        status="ok" if healthy else "failed",
        detail=f"{violations} violation(s)" if not healthy else "healthy",
        elapsed_ms=elapsed, data=data,
    )


def probe_cc_liveness(peer: PeerSpec, desk: str) -> ProbeResult:
    """Verify Claude Code's OAuth state inside the Dock is still valid.

    A successful ``claude -p ":"`` confirms the access token (or in-memory
    refresh) is working. Failure here is the headline event: container is
    up but the agent is dead until creds are refreshed.
    """
    t0 = time.monotonic()
    # Outer ssh timeout slightly larger than the inner `timeout 30` so we
    # see the inner timeout's exit code rather than ssh's.
    remote = f"ws exec {shlex.quote(desk)} -- timeout 30 claude -p ':'"
    code, stdout, stderr = _run_ssh(peer.ssh_target, remote, CC_PROBE_TIMEOUT)
    elapsed = int((time.monotonic() - t0) * 1000)
    if code in (124, 255):
        return ProbeResult(
            peer=peer.host, desk=desk, kind="cc_liveness",
            status="unreachable", detail=stderr.strip() or f"ssh exit {code}",
            elapsed_ms=elapsed,
        )
    if code != 0:
        tail = (stderr or stdout).strip().splitlines()
        detail = tail[-1] if tail else f"exit {code}"
        # Hint at the most likely cause when the inner timeout fires.
        if code == 124:
            detail = f"claude -p timed out (likely auth/network) — {detail}"
        return ProbeResult(
            peer=peer.host, desk=desk, kind="cc_liveness",
            status="failed", detail=detail[:200], elapsed_ms=elapsed,
        )
    return ProbeResult(
        peer=peer.host, desk=desk, kind="cc_liveness",
        status="ok", detail="claude responsive", elapsed_ms=elapsed,
    )


def resolve_desks(peer: PeerSpec, listed: list[dict]) -> list[str]:
    """Expand peer.desks against the peer's actual desk list."""
    available = [d.get("name") for d in listed if d.get("name")]
    if peer.desks == ["*"] or "*" in peer.desks:
        return available
    return [d for d in peer.desks if d in available]


def probe_peer(peer: PeerSpec) -> list[ProbeResult]:
    """Run the full probe set against one peer. Order matters: skip
    per-Dock probes if the peer is unreachable."""
    results: list[ProbeResult] = []
    daemon = probe_daemon(peer)
    results.append(daemon)
    if daemon.status == "unreachable":
        return results

    desks_probe = probe_desks(peer)
    results.append(desks_probe)
    if desks_probe.status != "ok":
        return results

    listed = (desks_probe.data or {}).get("desks", [])
    target_desks = resolve_desks(peer, listed)
    for desk in target_desks:
        results.append(probe_deskwatch(peer, desk))
        results.append(probe_cc_liveness(peer, desk))
    return results


def rollup(results: list[ProbeResult]) -> dict:
    """Summarize a flat probe-result list into per-peer / per-Dock health."""
    peers: dict[str, dict] = {}
    for r in results:
        p = peers.setdefault(r.peer, {
            "peer": r.peer, "reachable": True, "probes": [], "desks": {},
        })
        p["probes"].append(r.to_dict())
        if r.status == "unreachable":
            p["reachable"] = False
        if r.desk:
            d = p["desks"].setdefault(r.desk, {"desk": r.desk, "checks": []})
            d["checks"].append({
                "kind": r.kind, "status": r.status, "detail": r.detail,
            })
    for p in peers.values():
        for d in p["desks"].values():
            d["healthy"] = all(c["status"] == "ok" for c in d["checks"])
        p["healthy"] = p["reachable"] and all(
            d["healthy"] for d in p["desks"].values()
        )
    overall_healthy = all(p["healthy"] for p in peers.values())
    return {
        "healthy": overall_healthy,
        "peers": list(peers.values()),
    }
