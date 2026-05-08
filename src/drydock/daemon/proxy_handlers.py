"""UpdateProxyAllowlist RPC handler (Phase 2 — proxy rollout).

Daemon-side RPC for live mutation of a desk's egress allowlist
without container restart. Writes the dockwarden YAML, signals the
in-container dockwarden process via `docker kill --signal=HUP`.

Auth model:
- dock-scope caller: can update *their own* desk's allowlist, and
  the daemon validates the incoming domain list against the desk's
  delegatable_network_reach narrowness gate.
- auditor-scope caller: can update *any* desk's allowlist. No
  narrowness gate (the Auditor authority IS the gate, audited).
  This is the surface PA4's throttle_egress action calls into.

Idempotency: same {domain set} written = same file = no-op SIGHUP.
The handler always writes + signals; dockwarden's file-watch will
detect equal-content writes and treat as no-change (modtime is
still updated, so reload happens; cost is one extra YAML parse).
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Iterable

from drydock.core.audit import emit_audit
from drydock.core.proxy import write_smokescreen_acl, proxy_root_from_home
from drydock.core.registry import Registry
from drydock.daemon.rpc_common import _RpcError

logger = logging.getLogger(__name__)


def update_proxy_allowlist(
    params: dict | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
) -> dict:
    """Update a desk's egress proxy allowlist + signal reload.

    Params:
      target_drydock_id (str): which desk to update. If absent,
        defaults to caller (self-update).
      domains (list[str]): the FULL replacement allowlist. Entries
        match the existing delegatable_network_reach format —
        bare hostnames go to allowed_hosts, "*.foo.com" entries go
        to allowed_domains.
      reason (str, optional): audit-log reason field.

    Returns:
      {drydock_id, written_path, sighup_sent: bool, host_count, domain_count}
    """
    del request_id  # not idempotency-critical; always write+signal

    if caller_drydock_id is None:
        raise _RpcError(
            code=-32004,
            message="unauthenticated",
            data={"reason": "UpdateProxyAllowlist requires bearer token"},
        )
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"reason": "params must be an object"},
        )

    target_id = params.get("target_drydock_id") or caller_drydock_id
    if not isinstance(target_id, str):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "target_drydock_id", "reason": "must be a string"},
        )
    domains_raw = params.get("domains", [])
    if not isinstance(domains_raw, list):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "domains", "reason": "must be a list"},
        )
    domains = [str(d) for d in domains_raw if d]
    reason = str(params.get("reason") or "")

    registry = Registry(db_path=registry_path)
    try:
        # Auth gate: caller must either own the target OR have auditor scope.
        # We resolve the caller's scope from the registry's tokens table.
        caller_scope = _get_caller_scope(registry, caller_drydock_id)
        if target_id != caller_drydock_id and caller_scope != "auditor":
            raise _RpcError(
                code=-32020, message="auditor_scope_required",
                data={
                    "reason": "cross-desk allowlist updates require auditor scope",
                    "caller_scope": caller_scope,
                },
            )

        # Narrowness gate (dock-scope only): the new domain set must be
        # a subset of the desk's delegatable_network_reach. Auditor
        # bypasses this — its authority is to override worker's policy.
        if caller_scope != "auditor":
            policy = registry.load_desk_policy(target_id) or {}
            allowed = policy.get("delegatable_network_reach") or []
            if isinstance(allowed, str):
                allowed = json.loads(allowed)
            allowed_set = {str(a) for a in allowed}
            disallowed = [d for d in domains if d not in allowed_set]
            if disallowed:
                raise _RpcError(
                    code=-32006,
                    message="narrowness_violated",
                    data={
                        "field": "domains",
                        "reason": ("domains exceed delegatable_network_reach; "
                                    "edit project YAML + drydock project reload "
                                    "to widen the policy"),
                        "disallowed": sorted(disallowed),
                    },
                )

        target = registry.get_drydock_by_id(target_id) if hasattr(
            registry, "get_drydock_by_id") else None
        if target is None:
            # Fallback — find by id via list
            for d in registry.list_drydocks():
                if d.id == target_id:
                    target = d
                    break
        if target is None:
            raise _RpcError(
                code=-32602, message="invalid_params",
                data={"field": "target_drydock_id", "reason": f"no drydock with id {target_id}"},
            )
        container_id = target.container_id
    finally:
        registry.close()

    # Write the ACL — atomic via tempfile + rename in write_smokescreen_acl.
    written_path = write_smokescreen_acl(
        target_id, domains, proxy_root_from_home(),
    )

    # Signal dockwarden to reload immediately. dockwarden's file-watch
    # would catch the change within 5s anyway, but SIGHUP makes it
    # sub-second. Failure to signal isn't fatal — the file write is the
    # source of truth; reload happens at the next stat-poll.
    sighup_sent = False
    if container_id:
        try:
            # `-u root`: dockwarden runs as root inside the container
            # (started via sudo from postStartCommand); the container's
            # default user `node` can't signal it. Daemon runs as root
            # on host and can `docker exec -u root`.
            r = subprocess.run(
                ["docker", "exec", "-u", "root", container_id,
                 "pkill", "-HUP", "dockwarden"],
                check=False, capture_output=True, timeout=5,
            )
            sighup_sent = (r.returncode == 0)
            if not sighup_sent:
                logger.warning(
                    "update_proxy_allowlist: SIGHUP rc=%d stderr=%s",
                    r.returncode, r.stderr.decode("utf-8", errors="replace"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_proxy_allowlist: SIGHUP failed for %s: %s",
                            container_id, exc)

    # Compute counts for the response
    host_count = sum(1 for d in domains if not d.startswith("*"))
    domain_count = sum(1 for d in domains if d.startswith("*"))

    emit_audit(
        "egress.allowlist_updated",
        principal=caller_drydock_id,
        request_id=None,
        method="UpdateProxyAllowlist",
        result="ok",
        details={
            "caller_drydock_id": caller_drydock_id,
            "caller_scope": caller_scope,
            "target_drydock_id": target_id,
            "domain_count": len(domains),
            "host_count": host_count,
            "wildcard_count": domain_count,
            "reason": reason,
            "sighup_sent": sighup_sent,
        },
    )

    return {
        "drydock_id": target_id,
        "written_path": str(written_path),
        "sighup_sent": sighup_sent,
        "host_count": host_count,
        "domain_count": domain_count,
    }


def _get_caller_scope(registry: Registry, caller_drydock_id: str) -> str:
    """Look up the scope of the caller's bearer token. Returns 'dock'
    or 'auditor'. Raises if no token row found."""
    info = registry.get_token_info(caller_drydock_id)
    if info is None:
        raise _RpcError(
            code=-32004,
            message="unauthenticated",
            data={"reason": f"no token row for caller {caller_drydock_id}"},
        )
    return str(info.get("scope") or "dock")
