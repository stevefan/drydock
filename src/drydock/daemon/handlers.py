"""JSON-RPC method handlers for the drydock daemon."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from drydock.core import WsError
from drydock.core.audit import emit_audit
from drydock.core.checkout import create_checkout
from drydock.core.devcontainer import DevcontainerCLI
from drydock.core.overlay import OverlayConfig, regenerate_overlay_from_drydock, remove_overlay, write_overlay
from drydock.core.policy import (
    CapabilityKind,
    DeskPolicy,
    DeskSpec,
    Reject,
    canonicalize_domain,
    canonicalize_mount,
    validate_spawn,
)
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.daemon.auth import issue_token_for_desk
from drydock.daemon.server import _RpcError

logger = logging.getLogger(__name__)

_REQUIRED_PARAMS = ("project", "name")
DEFAULT_DEVCONTAINER_SUBPATH = ".devcontainer"

# Soft cap on concurrent SpawnChild calls per parent desk. Belt for the
# DevcontainerCLI bottleneck — a runaway parent calling SpawnChild in a
# loop would otherwise queue N container builds against the same `docker
# build` host. Resource budgets per capability-broker doc §4 are deferred
# to V3; this cap is a safety floor not a quota system. Override via env
# for ops who genuinely need wider concurrency.
import os as _os
SPAWN_CHILD_INFLIGHT_MAX = int(_os.environ.get("DRYDOCK_SPAWN_CHILD_INFLIGHT_MAX", "5"))
_OVERLAY_PARAM_FIELDS = (
    "tailscale_hostname",
    "tailscale_serve_port",
    "tailscale_authkey_env_var",
    "remote_control_name",
    "firewall_extra_domains",
    "firewall_ipv6_hosts",
    "firewall_aws_ip_ranges",
    "forward_ports",
    "claude_profile",
    "extra_env",
    "storage_mounts",
    "resources_hard",
    "egress_proxy",
)


def create_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict:
    del caller_drydock_id
    spec = _validated_spec(params)
    registry = Registry(db_path=registry_path)
    try:
        existing = registry.get_drydock(spec["name"])
        if existing is not None:
            if existing.state in ("suspended", "defined"):
                result = _resume_desk(existing, registry=registry, dry_run=dry_run)
                emit_audit(
                    "drydock.resumed",
                    principal=None,
                    request_id=request_id,
                    method="CreateDesk",
                    result="ok",
                    details={
                        "drydock_id": result.get("drydock_id"),
                        "project": result.get("project"),
                    },
                )
                return result
            raise _RpcError(
                code=-32001,
                message="drydock_already_running",
                data={"fix": f"ws create {spec['name']} --force"},
            )
        result = _perform_create(
            registry=registry,
            spec=spec,
            secrets_root=secrets_root,
            dry_run=dry_run,
        )
        emit_audit(
            "drydock.created",
            principal=None,
            request_id=request_id,
            method="CreateDesk",
            result="ok",
            details={
                "drydock_id": result.get("drydock_id"),
                "project": result.get("project"),
                "parent_drydock_id": None,
            },
        )
        return result
    except WsError as exc:
        raise _rpc_error_from_ws_error(exc) from exc
    finally:
        registry.close()


def whoami(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
) -> dict[str, str | None]:
    del params
    del request_id
    return {"drydock_id": caller_drydock_id}


def destroy_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict[str, object]:
    logger.info("daemon: DestroyDesk requested caller_drydock_id=%s", caller_drydock_id)
    target_name, target_hint = _validated_destroy_target(params)

    registry = Registry(db_path=registry_path)
    try:
        drydock = _lookup_destroy_target(registry, target_name=target_name, target_hint=target_hint)
        if drydock is None:
            raise _RpcError(
                code=-32001,
                message="desk_not_found",
                data={"drydock_id": target_hint},
            )

        cascaded: list[str] = []
        target_drydock_id = drydock.id
        partial_failures = _destroy_tree(
            drydock,
            registry=registry,
            secrets_root=secrets_root,
            dry_run=dry_run,
            cascaded=cascaded,
            visited=set(),
        )

        result: dict[str, object] = {
            "destroyed": True,
            "drydock_id": target_drydock_id,
            "cascaded": cascaded,
        }
        if partial_failures:
            result["partial_failures"] = partial_failures
        emit_audit(
            "drydock.destroyed",
            principal=caller_drydock_id,
            request_id=request_id,
            method="DestroyDesk",
            result="error" if partial_failures else "ok",
            details={
                "drydock_id": target_drydock_id,
                "cascaded_children": cascaded,
            },
        )
        return result
    finally:
        registry.close()


def spawn_child(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict:
    if caller_drydock_id is None:
        raise _RpcError(code=-32004, message="unauthenticated")

    spec = _validated_spec(params)
    registry = Registry(db_path=registry_path)
    try:
        existing = registry.get_drydock(spec["name"])
        if existing is not None:
            raise _RpcError(
                code=-32001,
                message="drydock_already_running",
                data={"fix": f"ws create {spec['name']} --force"},
            )

        # Per-parent in-flight cap. Counts children already in
        # 'provisioning' state (the window between create_drydock
        # and the container reaching 'running'). Cheaper than parsing
        # task_log; reuses an existing column.
        in_flight = _count_provisioning_children(registry, caller_drydock_id)
        if in_flight >= SPAWN_CHILD_INFLIGHT_MAX:
            raise _RpcError(
                code=-32014,
                message="rate_limited",
                data={
                    "in_flight": in_flight,
                    "max": SPAWN_CHILD_INFLIGHT_MAX,
                    "fix": "wait for in-flight spawns to complete or "
                           "raise DRYDOCK_SPAWN_CHILD_INFLIGHT_MAX",
                },
            )

        parent_policy = _load_parent_policy(registry, caller_drydock_id)
        child_spec = _build_child_spec(spec)
        verdict = validate_spawn(parent_policy, child_spec)
        if isinstance(verdict, Reject):
            emit_audit(
                "drydock.spawn_rejected",
                principal=caller_drydock_id,
                request_id=request_id,
                method="SpawnChild",
                result="error",
                details={
                    "parent_drydock_id": caller_drydock_id,
                    "reject": {
                        "rule": verdict.rule,
                        "offending_item": str(verdict.offending_item),
                    },
                },
            )
            raise _RpcError(
                code=-32001,
                message="narrowness_violated",
                data={"reject": _serialize_reject(verdict)},
            )

        result = _perform_create(
            registry=registry,
            spec=spec,
            secrets_root=secrets_root,
            dry_run=dry_run,
            parent_drydock_id=caller_drydock_id,
            result_parent_desk_id=caller_drydock_id,
        )
        emit_audit(
            "drydock.spawned",
            principal=caller_drydock_id,
            request_id=request_id,
            method="SpawnChild",
            result="ok",
            details={
                "drydock_id": result.get("drydock_id"),
                "parent_drydock_id": caller_drydock_id,
                "narrowness_check": "allow",
            },
        )
        return result
    except WsError as exc:
        raise _RpcError(
            code=-32000,
            message="spawn_failed",
            data={"detail": exc.message},
        ) from exc
    finally:
        registry.close()


def _validated_spec(params: dict | list | None) -> dict[str, object]:
    missing = list(_REQUIRED_PARAMS)
    if isinstance(params, dict):
        missing = [
            key for key in _REQUIRED_PARAMS
            if not isinstance(params.get(key), str) or not params.get(key)
        ]
    if not isinstance(params, dict) or missing:
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"missing": missing},
        )

    project = params["project"]
    name = params["name"]
    repo_path = params.get("repo_path")
    if not isinstance(repo_path, str) or not repo_path:
        repo_path = f"/srv/code/{project}"

    branch = params.get("branch")
    if not isinstance(branch, str) or not branch:
        branch = f"ws/{name}"

    base_ref = params.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref:
        base_ref = "HEAD"

    image = params.get("image")
    if not isinstance(image, str):
        image = ""

    owner = params.get("owner")
    if not isinstance(owner, str):
        owner = ""

    devcontainer_subpath = params.get("devcontainer_subpath")
    if devcontainer_subpath is None:
        devcontainer_subpath = DEFAULT_DEVCONTAINER_SUBPATH
    elif not isinstance(devcontainer_subpath, str):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": "devcontainer_subpath"},
        )
    _validate_devcontainer_subpath(devcontainer_subpath)

    firewall_extra_domains = _validated_string_list(
        params.get("firewall_extra_domains"),
        field_name="firewall_extra_domains",
    )
    firewall_ipv6_hosts = _validated_string_list(
        params.get("firewall_ipv6_hosts"),
        field_name="firewall_ipv6_hosts",
    )
    firewall_aws_ip_ranges = _validated_string_list(
        params.get("firewall_aws_ip_ranges"),
        field_name="firewall_aws_ip_ranges",
    )
    secret_entitlements = _validated_string_list(
        params.get("secret_entitlements"),
        field_name="secret_entitlements",
    )
    extra_mounts = _validated_string_list(
        params.get("extra_mounts"),
        field_name="extra_mounts",
    )
    delegatable_firewall_domains = _validated_string_list(
        params.get("delegatable_firewall_domains"),
        field_name="delegatable_firewall_domains",
    )
    delegatable_secrets = _validated_string_list(
        params.get("delegatable_secrets"),
        field_name="delegatable_secrets",
    )
    capabilities = _validated_string_list(
        params.get("capabilities"),
        field_name="capabilities",
    )
    delegatable_storage_scopes = _validated_string_list(
        params.get("delegatable_storage_scopes"),
        field_name="delegatable_storage_scopes",
    )
    delegatable_provision_scopes = _validated_string_list(
        params.get("delegatable_provision_scopes"),
        field_name="delegatable_provision_scopes",
    )
    delegatable_network_reach = _validated_string_list(
        params.get("delegatable_network_reach"),
        field_name="delegatable_network_reach",
    )
    network_reach_ports = _validated_int_list(
        params.get("network_reach_ports"),
        field_name="network_reach_ports",
    )
    tailscale_hostname = _validated_optional_string(
        params.get("tailscale_hostname"),
        field_name="tailscale_hostname",
    )
    tailscale_serve_port = _validated_optional_int(
        params.get("tailscale_serve_port"),
        field_name="tailscale_serve_port",
    )
    tailscale_authkey_env_var = _validated_optional_string(
        params.get("tailscale_authkey_env_var"),
        field_name="tailscale_authkey_env_var",
    )
    remote_control_name = _validated_optional_string(
        params.get("remote_control_name"),
        field_name="remote_control_name",
    )
    forward_ports = _validated_int_list(
        params.get("forward_ports"),
        field_name="forward_ports",
    )
    claude_profile = _validated_optional_string(
        params.get("claude_profile"),
        field_name="claude_profile",
    )
    # Phase 2a.1 E1: egress_proxy field. Accepts "enabled" or "disabled"
    # (anything else rejected). Default "disabled" — preserves the
    # iptables/ipset path until E2 flips the default.
    egress_proxy = str(params.get("egress_proxy") or "disabled").lower()
    if egress_proxy not in ("enabled", "disabled"):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "egress_proxy", "reason": "must be 'enabled' or 'disabled'"},
        )
    extra_env = _validated_str_map(
        params.get("extra_env"),
        field_name="extra_env",
    )
    storage_mounts = _validated_dict_list(
        params.get("storage_mounts"),
        field_name="storage_mounts",
    )

    # resources_hard is validated by HardCeilings.from_dict — that's the
    # single source of truth for the schema. Surface its error as
    # invalid_params so the user sees the same fix: hint regardless of
    # whether they trigger it via project YAML or the daemon RPC.
    resources_hard_raw = params.get("resources_hard") or {}
    if not isinstance(resources_hard_raw, dict):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "resources_hard", "reason": "must be an object"},
        )
    if resources_hard_raw:
        from drydock.core.resource_ceilings import HardCeilings, ResourceCeilingError
        try:
            HardCeilings.from_dict(resources_hard_raw)
        except ResourceCeilingError as exc:
            raise _RpcError(
                code=-32602, message="invalid_params",
                data={"field": "resources_hard", "reason": str(exc)},
            )

    # Phase Y0 (yard.md): optional yard membership. Validated string;
    # existence-check happens at create time (so the error has the
    # right "register yard first" fix hint, with access to the registry).
    yard_name = params.get("yard")
    if yard_name is not None and not isinstance(yard_name, str):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "yard", "reason": "must be a string or null"},
        )

    workspace_subdir = params.get("workspace_subdir") or ""
    if not isinstance(workspace_subdir, str):
        raise _RpcError(
            code=-32602, message="invalid_params",
            data={"field": "workspace_subdir"},
        )

    return {
        "project": project,
        "name": name,
        "repo_path": repo_path,
        "branch": branch,
        "base_ref": base_ref,
        "image": image,
        "owner": owner,
        "workspace_subdir": workspace_subdir,
        "devcontainer_subpath": devcontainer_subpath,
        "tailscale_hostname": tailscale_hostname,
        "tailscale_serve_port": tailscale_serve_port,
        "tailscale_authkey_env_var": tailscale_authkey_env_var,
        "remote_control_name": remote_control_name,
        "firewall_extra_domains": firewall_extra_domains,
        "firewall_ipv6_hosts": firewall_ipv6_hosts,
        "firewall_aws_ip_ranges": firewall_aws_ip_ranges,
        "forward_ports": forward_ports,
        "claude_profile": claude_profile,
        "egress_proxy": egress_proxy,
        "extra_env": extra_env,
        "storage_mounts": storage_mounts,
        "secret_entitlements": secret_entitlements,
        "extra_mounts": extra_mounts,
        "delegatable_firewall_domains": delegatable_firewall_domains,
        "delegatable_secrets": delegatable_secrets,
        "capabilities": capabilities,
        "delegatable_storage_scopes": delegatable_storage_scopes,
        "delegatable_provision_scopes": delegatable_provision_scopes,
        "delegatable_network_reach": delegatable_network_reach,
        "network_reach_ports": network_reach_ports,
        "resources_hard": resources_hard_raw,
        "yard": yard_name,
    }


def _validated_destroy_target(params: dict | list | None) -> tuple[str | None, str]:
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"missing": ["drydock_id"]},
        )

    name = params.get("name")
    if isinstance(name, str) and name:
        return name, Drydock(name=name, project=name, repo_path="").id

    drydock_id = params.get("drydock_id")
    if isinstance(drydock_id, str) and drydock_id:
        return None, drydock_id

    raise _RpcError(
        code=-32602,
        message="invalid_params",
        data={"missing": ["drydock_id"]},
    )


def _validated_string_list(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": field_name, "reason": "expected list[str]"},
        )
    return value


def _validated_int_list(value: object, *, field_name: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, int) for item in value):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": field_name, "reason": "expected list[int]"},
        )
    return value


def _run_setup_storage_mounts(container_id: str) -> None:
    """docker exec setup-storage-mounts.sh; best-effort, logs only on failure.

    Daemon-triggered rather than postStartCommand-triggered because user
    projects bring their own devcontainer.json that doesn't know about
    drydock's mount script.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", "-u", "node", container_id,
             "/usr/local/bin/setup-storage-mounts.sh"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.warning(
                "daemon: setup-storage-mounts.sh exit=%d: %s",
                result.returncode, (result.stderr or result.stdout or "").strip()[:500],
            )
        else:
            logger.info("daemon: setup-storage-mounts.sh completed container=%s", container_id)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("daemon: setup-storage-mounts.sh failed: %s", exc)


def _maybe_setup_storage_mounts(container_id: str, source: dict | None) -> None:
    """Run setup-storage-mounts.sh if the caller's config declares any."""
    if source and source.get("storage_mounts"):
        _run_setup_storage_mounts(container_id)


def _validated_dict_list(value: object, *, field_name: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": field_name, "reason": "expected list[dict]"},
        )
    return [dict(x) for x in value]


def _validated_str_map(value: object, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": field_name, "reason": "expected dict[str, str]"},
        )
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise _RpcError(
                code=-32602,
                message="invalid_params",
                data={"field": field_name, "reason": "expected dict[str, str]"},
            )
    return dict(value)


def _validated_optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": field_name, "reason": "expected str"},
        )
    return value


def _validated_optional_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": field_name, "reason": "expected int"},
        )
    return value


def _overlay_from_spec(spec: dict[str, object]) -> OverlayConfig:
    kwargs: dict[str, object] = {}

    tailscale_hostname = spec.get("tailscale_hostname")
    if isinstance(tailscale_hostname, str):
        kwargs["tailscale_hostname"] = tailscale_hostname

    tailscale_serve_port = spec.get("tailscale_serve_port")
    if isinstance(tailscale_serve_port, int):
        kwargs["tailscale_serve_port"] = tailscale_serve_port

    tailscale_authkey_env_var = spec.get("tailscale_authkey_env_var")
    if isinstance(tailscale_authkey_env_var, str):
        authkey = os.getenv(tailscale_authkey_env_var, "")
        if authkey:
            kwargs["tailscale_authkey"] = authkey

    remote_control_name = spec.get("remote_control_name")
    if isinstance(remote_control_name, str):
        kwargs["remote_control_name"] = remote_control_name

    firewall_extra_domains = spec.get("firewall_extra_domains")
    if isinstance(firewall_extra_domains, list) and firewall_extra_domains:
        kwargs["firewall_extra_domains"] = firewall_extra_domains

    firewall_ipv6_hosts = spec.get("firewall_ipv6_hosts")
    if isinstance(firewall_ipv6_hosts, list) and firewall_ipv6_hosts:
        kwargs["firewall_ipv6_hosts"] = firewall_ipv6_hosts

    firewall_aws_ip_ranges = spec.get("firewall_aws_ip_ranges")
    if isinstance(firewall_aws_ip_ranges, list) and firewall_aws_ip_ranges:
        kwargs["firewall_aws_ip_ranges"] = firewall_aws_ip_ranges

    forward_ports = spec.get("forward_ports")
    if isinstance(forward_ports, list) and forward_ports:
        kwargs["forward_ports"] = forward_ports

    claude_profile = spec.get("claude_profile")
    if isinstance(claude_profile, str):
        kwargs["claude_profile"] = claude_profile

    extra_env = spec.get("extra_env")
    if isinstance(extra_env, dict) and extra_env:
        kwargs["extra_env"] = dict(extra_env)

    storage_mounts = spec.get("storage_mounts")
    if isinstance(storage_mounts, list) and storage_mounts:
        kwargs["storage_mounts"] = list(storage_mounts)

    resources_hard = spec.get("resources_hard")
    if isinstance(resources_hard, dict) and resources_hard:
        kwargs["resources_hard"] = dict(resources_hard)

    # Phase 2a.1 E1: thread egress_proxy + per-Harbor proxy config dir
    # so the overlay knows whether to bind-mount the smokescreen
    # allowlist file and set EGRESS_PROXY_ENABLED.
    egress_proxy = spec.get("egress_proxy")
    if isinstance(egress_proxy, str):
        kwargs["egress_proxy"] = egress_proxy.lower()
    if egress_proxy == "enabled":
        # Default per-Harbor location matches the secrets/overlays layout.
        # Daemon writes <dir>/<drydock_id>.yaml; bind mount maps that one
        # file readonly into the container.
        kwargs["proxy_config_host_dir"] = str(
            Path.home() / ".drydock" / "proxy"
        )

    return OverlayConfig(**kwargs)


def _overlay_config_data(spec: dict[str, object]) -> dict[str, object]:
    return {
        field: spec[field]
        for field in _OVERLAY_PARAM_FIELDS
        if field in spec and spec[field] is not None and spec[field] != []
    }


def _lookup_destroy_target(
    registry: Registry,
    *,
    target_name: str | None,
    target_hint: str,
) -> Drydock | None:
    if target_name is not None:
        return registry.get_drydock(target_name)

    row = registry._conn.execute(
        "SELECT * FROM drydocks WHERE id = ?",
        (target_hint,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_drydock(row)


def _destroy_tree(
    drydock: Drydock,
    *,
    registry: Registry,
    secrets_root: Path,
    dry_run: bool,
    cascaded: list[str],
    visited: set[str],
) -> list[dict[str, str]]:
    if drydock.id in visited:
        raise _RpcError(
            code=-32603,
            message="destroy_cycle_detected",
            data={"drydock_id": drydock.id},
        )
    visited.add(drydock.id)

    partial_failures: list[dict[str, str]] = []
    for child in registry.get_children(drydock.id):
        cascaded.append(child.id)
        partial_failures.extend(
            _destroy_tree(
                child,
                registry=registry,
                secrets_root=secrets_root,
                dry_run=dry_run,
                cascaded=cascaded,
                visited=visited,
            )
        )

    partial_failures.extend(
        _destroy_one(
            drydock,
            registry=registry,
            secrets_root=secrets_root,
            dry_run=dry_run,
        )
    )
    visited.remove(drydock.id)
    return partial_failures


def _destroy_one(
    drydock: Drydock,
    registry: Registry,
    secrets_root: Path,
    dry_run: bool,
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    devc = DevcontainerCLI(dry_run=dry_run)
    logger.info("daemon: destroy container drydock_id=%s container_id=%s", drydock.id, drydock.container_id)
    if drydock.container_id:
        try:
            devc.stop(drydock.container_id)
            logger.info("daemon: container stopped drydock_id=%s", drydock.id)
        except Exception as exc:
            logger.warning("daemon: failed to stop container for %s: %s", drydock.id, exc)
            failures.append(_failure(drydock.id, "container_stop", exc))
        try:
            devc.remove(drydock.container_id)
            logger.info("daemon: container removed drydock_id=%s", drydock.id)
        except Exception as exc:
            logger.warning("daemon: failed to remove container for %s: %s", drydock.id, exc)
            failures.append(_failure(drydock.id, "container_remove", exc))

    # Capability-broker.md §6a: outstanding leases for the Dock are
    # marked revoked before token + container teardown, so a racing
    # in-flight RequestCapability sees `desk_destroyed` rather than a
    # phantom lease against a soon-to-be-gone desk.
    try:
        active_leases = registry.list_active_leases_for_desk(drydock.id)
        revoked = registry.revoke_leases_for_desk(drydock.id, "desk_destroyed")
        if revoked:
            logger.info(
                "daemon: revoked %d active leases for drydock_id=%s",
                revoked, drydock.id,
            )
        for lease in active_leases:
            emit_audit(
                "lease.released",
                principal=drydock.id,
                request_id=None,
                method="DestroyDesk",
                result="ok",
                details={"lease_id": lease.lease_id, "reason": "desk_destroyed"},
            )
    except Exception as exc:
        logger.warning("daemon: failed to revoke leases for %s: %s", drydock.id, exc)
        failures.append(_failure(drydock.id, "lease_revoke", exc))

    secret_dir = Path(secrets_root) / drydock.id
    token_path = secret_dir / "drydock-token"
    logger.info("daemon: revoke token drydock_id=%s", drydock.id)
    try:
        registry.delete_token(drydock.id)
        emit_audit(
            "token.revoked",
            principal=drydock.id,
            request_id=None,
            method="DestroyDesk",
            result="ok",
            details={"drydock_id": drydock.id},
        )
        logger.info("daemon: token row removed drydock_id=%s", drydock.id)
    except Exception as exc:
        logger.warning("daemon: failed to delete token row for %s: %s", drydock.id, exc)
        failures.append(_failure(drydock.id, "token_delete", exc))
    try:
        token_path.unlink(missing_ok=True)
        logger.info("daemon: token file removed drydock_id=%s path=%s", drydock.id, token_path)
    except Exception as exc:
        logger.warning("daemon: failed to remove token file for %s: %s", drydock.id, exc)
        failures.append(_failure(drydock.id, "token_file_remove", exc))
    shutil.rmtree(secret_dir, ignore_errors=True)
    if secret_dir.exists():
        logger.warning("daemon: failed to remove secret dir for %s: %s", drydock.id, secret_dir)
        failures.append(_failure(drydock.id, "secret_dir_remove", "directory still exists"))
    else:
        logger.info("daemon: secret dir removed drydock_id=%s path=%s", drydock.id, secret_dir)

    logger.info("daemon: remove worktree drydock_id=%s path=%s", drydock.id, drydock.worktree_path)
    worktree_path = Path(drydock.worktree_path) if drydock.worktree_path else None
    if worktree_path is not None:
        shutil.rmtree(worktree_path, ignore_errors=True)
        if worktree_path.exists():
            logger.warning("daemon: failed to remove worktree for %s: %s", drydock.id, worktree_path)
            failures.append(_failure(drydock.id, "worktree_remove", "directory still exists"))
        else:
            logger.info("daemon: worktree removed drydock_id=%s", drydock.id)

    overlay_path = drydock.config.get("overlay_path")
    if not isinstance(overlay_path, str) or not overlay_path:
        overlay_path = str(Path.home() / ".drydock" / "overlays" / f"{drydock.id}.devcontainer.json")
    logger.info("daemon: remove overlay drydock_id=%s path=%s", drydock.id, overlay_path)
    try:
        remove_overlay(overlay_path)
        logger.info("daemon: overlay removed drydock_id=%s", drydock.id)
    except FileNotFoundError:
        logger.info("daemon: overlay already absent drydock_id=%s", drydock.id)
    except Exception as exc:
        logger.warning("daemon: failed to remove overlay for %s: %s", drydock.id, exc)
        failures.append(_failure(drydock.id, "overlay_remove", exc))

    logger.info("daemon: delete drydock row drydock_id=%s name=%s", drydock.id, drydock.name)
    try:
        registry.delete_drydock(drydock.name)
        logger.info("daemon: drydock row removed drydock_id=%s", drydock.id)
    except Exception as exc:
        logger.warning("daemon: failed to delete drydock row for %s: %s", drydock.id, exc)
        failures.append(_failure(drydock.id, "drydock_delete", exc))

    return failures


def _failure(drydock_id: str, step: str, error: Exception | str) -> dict[str, str]:
    return {"drydock_id": drydock_id, "step": step, "error": str(error)}


def _count_provisioning_children(registry: Registry, parent_drydock_id: str) -> int:
    """Count children of `parent_drydock_id` currently in 'provisioning' state.

    Used by SpawnChild to enforce the per-parent in-flight cap. Cheap
    SQL count; no row materialization. Uses the existing parent_drydock_id
    column added in the V2 schema migration.
    """
    row = registry._conn.execute(
        """
        SELECT COUNT(*) AS n FROM drydocks
        WHERE parent_drydock_id = ? AND state = 'provisioning'
        """,
        (parent_drydock_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def _regenerate_overlay_for_resume(existing: Drydock, overlay_path: Path) -> None:
    """Thin wrapper over regenerate_overlay_from_drydock for the resume path.

    Kept as a function so the resume flow's logging + error-handling
    stays colocated with `_resume_desk`. The heavy lifting is in
    `drydock.core.overlay.regenerate_overlay_from_drydock` so the CLI
    (`ws overlay regenerate`, `ws project reload`) can call it directly.
    """
    regenerate_overlay_from_drydock(existing, overlay_dir=overlay_path.parent)
    logger.info("resume: regenerated overlay for %s at %s", existing.name, overlay_path)


def _validate_devcontainer_subpath(devcontainer_subpath: str) -> None:
    subpath = Path(devcontainer_subpath)
    if subpath.is_absolute() or ".." in subpath.parts:
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"reason": "devcontainer_subpath must be relative and contain no .."},
        )


def _resume_desk(
    existing: Drydock,
    *,
    registry: Registry,
    dry_run: bool,
) -> dict[str, object]:
    """Re-up the container for a suspended/defined desk.

    Worktree, token, and named volumes are reused. Container is fresh.
    Overlay is REGENERATED from current OverlayConfig defaults + stored
    config — picks up overlay-code changes (e.g. new bind-mounts) without
    requiring a `--force` destroy+create. Stored overlay_path is
    authoritative for WHERE to write; contents are re-derived every time.
    """
    config = existing.config or {}
    overlay_path = config.get("overlay_path") if isinstance(config, dict) else None
    if not overlay_path:
        registry.update_state(existing.name, "error")
        raise WsError(
            f"Cannot resume desk '{existing.name}': overlay_path missing from registry",
            fix=f"Rebuild from clean state: ws create {existing.project} {existing.name} --force",
        )

    # Regenerate overlay so new OverlayConfig defaults (bind-mounts, env vars)
    # land without requiring `--force`. Pull persistent fields from the stored
    # registry config so user-set tailscale_hostname, firewall_extra_domains,
    # etc. survive the regen.
    try:
        _regenerate_overlay_for_resume(existing, Path(overlay_path))
    except (WsError, FileNotFoundError, OSError) as exc:
        logger.warning(
            "resume: overlay regen failed for %s (%s); using stored file",
            existing.name, exc,
        )

    if not Path(overlay_path).exists():
        registry.update_state(existing.name, "error")
        raise WsError(
            f"Cannot resume desk '{existing.name}': overlay missing at {overlay_path}",
            fix=f"Rebuild from clean state: ws create {existing.project} {existing.name} --force",
        )
    workspace_folder = existing.worktree_path
    if not workspace_folder or not Path(workspace_folder).exists():
        registry.update_state(existing.name, "error")
        raise WsError(
            f"Cannot resume desk '{existing.name}': worktree missing at {workspace_folder or '(unset)'}",
            fix=f"Rebuild from clean state: ws create {existing.project} {existing.name} --force",
        )

    devc = DevcontainerCLI(dry_run=dry_run)
    if not dry_run:
        devc.check_available()

    registry.update_state(existing.name, "provisioning")
    try:
        up_result = devc.up(
            workspace_folder=workspace_folder,
            override_config=overlay_path,
        )
    except WsError:
        registry.update_state(existing.name, "error")
        raise

    container_id = up_result.get("container_id") or up_result.get("containerId")
    if dry_run and not container_id:
        container_id = f"dry-run-{uuid4().hex[:8]}"

    ws = registry.update_drydock(
        existing.name,
        container_id=container_id or "",
        state="running",
    )

    if container_id and not dry_run:
        from drydock.core.trust import _read_workspace_folder_from_overlay, seed_drydock_trust
        in_container_folder = _read_workspace_folder_from_overlay(str(overlay_path))
        seed_drydock_trust(container_id, in_container_folder)
        _maybe_setup_storage_mounts(
            container_id,
            existing.config if isinstance(existing.config, dict) else None,
        )

    return {
        "drydock_id": ws.id,
        "name": ws.name,
        "project": ws.project,
        "branch": ws.branch or f"ws/{ws.name}",
        "state": "running",
        "container_id": ws.container_id,
        "worktree_path": ws.worktree_path,
    }


def _perform_create(
    *,
    registry: Registry,
    spec: dict[str, object],
    secrets_root: Path,
    dry_run: bool,
    parent_drydock_id: str | None = None,
    result_parent_desk_id: str | None = None,
) -> dict[str, object]:
    overlay_config = _overlay_from_spec(spec)
    overlay_config_data = _overlay_config_data(spec)
    workspace_subdir = str(spec.get("workspace_subdir") or "")
    # Phase 0 (project-dock-ontology.md): pin the SHA of the project YAML
    # at create-time so `ws host audit` can surface drift later if the
    # YAML changes without a `ws project reload`.
    from drydock.core.project_yaml_sha import compute_project_yaml_sha
    pinned_yaml_sha256 = compute_project_yaml_sha(str(spec["project"]))
    # Phase 2a.2 (make-the-harness-live): pin the *applied* hard ceilings
    # at create time as the authoritative revert target for future
    # WorkloadLease grants. Independent of project YAML so a mid-lease
    # YAML edit can't shift the revert target out from under the lease.
    original_resources_hard = dict(spec.get("resources_hard") or {})
    ws = Drydock(
        name=str(spec["name"]),
        project=str(spec["project"]),
        repo_path=str(spec["repo_path"]),
        branch=str(spec["branch"]),
        base_ref=str(spec["base_ref"]),
        image=str(spec["image"]),
        owner=str(spec["owner"]),
        workspace_subdir=workspace_subdir,
        config={
            "devcontainer_subpath": str(spec["devcontainer_subpath"]),
            "extra_mounts": list(spec["extra_mounts"]),
            **overlay_config_data,
        },
        original_resources_hard=original_resources_hard,
    )
    ws = registry.create_drydock(ws)
    registry.update_desk_delegations(
        ws.name,
        delegatable_firewall_domains=list(spec["delegatable_firewall_domains"]),
        delegatable_secrets=list(spec["delegatable_secrets"]),
        capabilities=list(spec["capabilities"]),
        delegatable_storage_scopes=list(spec.get("delegatable_storage_scopes") or []),
        delegatable_provision_scopes=list(spec.get("delegatable_provision_scopes") or []),
        delegatable_network_reach=list(spec.get("delegatable_network_reach") or []),
        network_reach_ports=list(spec.get("network_reach_ports") or []),
        resources_hard=dict(spec.get("resources_hard") or {}),
    )
    if parent_drydock_id is not None:
        ws = registry.update_drydock(ws.name, parent_drydock_id=parent_drydock_id)

    if pinned_yaml_sha256:
        ws = registry.update_drydock(ws.name, pinned_yaml_sha256=pinned_yaml_sha256)

    # Phase 2a.1 E1: when this desk has egress_proxy enabled, write its
    # smokescreen allowlist file to the Harbor's proxy-config dir BEFORE
    # the container starts (start-egress-proxy.sh refuses to launch with
    # no allowlist). The container's bind mount points at this exact file.
    if str(spec.get("egress_proxy") or "disabled").lower() == "enabled":
        from drydock.core.proxy import write_smokescreen_acl, proxy_root_from_home
        write_smokescreen_acl(
            ws.id,
            list(spec.get("delegatable_network_reach") or []),
            proxy_root_from_home(),
        )

    # Phase Y0: opt the new Drydock into a Yard if the Project declared one.
    # Yard must already exist (registered via `ws yard create`); otherwise
    # raise so the user sees a clear remediation.
    yard_name = spec.get("yard")
    if yard_name:
        yard_row = registry.get_yard(str(yard_name))
        if yard_row is None:
            raise WsError(
                f"Project declares yard '{yard_name}' but no such Yard is registered",
                fix=f"Register the Yard first: ws yard create {yard_name}",
            )
        ws = registry.update_drydock(ws.name, yard_id=yard_row["id"])

    checkout_path = create_checkout(ws)
    ws = registry.update_drydock(ws.name, worktree_path=str(checkout_path))

    # Subdir desks anchor the devcontainer.json lookup inside the
    # subproject, matching the CLI-local create path. Without this, the
    # overlay composite is merged against the repo-root devcontainer.json
    # instead of the subproject's — silently losing lifecycle commands
    # (postCreateCommand, onCreateCommand) that the project declares.
    workspace_folder = (
        str(Path(ws.worktree_path) / ws.workspace_subdir)
        if ws.workspace_subdir
        else ws.worktree_path
    )
    devcontainer_subpath = str(spec["devcontainer_subpath"])
    devcontainer_json = Path(workspace_folder) / devcontainer_subpath / "devcontainer.json"
    if not devcontainer_json.exists():
        registry.update_state(ws.name, "error")
        raise WsError(
            f"devcontainer.json not found at {devcontainer_json}",
            fix=(
                f"Create {devcontainer_json}, "
                "or use a repo that already has one"
            ),
        )

    _ensure_gitconfig_stub()
    # Bind-mount source paths must exist before docker run. Creating
    # empty per-Dock secrets dir here (mode 0700) matches the CLI path
    # and prevents the "invalid mount config" failure on first create.
    (secrets_root / ws.id).mkdir(mode=0o700, parents=True, exist_ok=True)
    overlay_path = write_overlay(
        ws,
        Path.home() / ".drydock" / "overlays",
        overlay_config,
        base_devcontainer_path=devcontainer_json,
    )
    ws = registry.update_drydock(
        ws.name,
        config={
            "overlay_path": str(overlay_path),
            "devcontainer_subpath": devcontainer_subpath,
            "extra_mounts": list(spec["extra_mounts"]),
            **overlay_config_data,
        },
    )

    devc = DevcontainerCLI(dry_run=dry_run)
    if not dry_run:
        devc.check_available()

    issue_token_for_desk(ws.id, secrets_root=secrets_root, registry=registry)
    emit_audit(
        "token.issued",
        principal=ws.id,
        request_id=None,
        method="CreateDesk" if parent_drydock_id is None else "SpawnChild",
        result="ok",
        details={"drydock_id": ws.id, "rotation_reason": None},
    )
    ws = registry.update_state(ws.name, "provisioning")
    try:
        up_result = devc.up(
            workspace_folder=workspace_folder,
            override_config=str(overlay_path),
        )
    except WsError:
        registry.update_state(ws.name, "error")
        raise

    container_id = up_result.get("container_id") or up_result.get("containerId")
    if dry_run and not container_id:
        container_id = f"dry-run-{uuid4().hex[:8]}"

    ws = registry.update_drydock(
        ws.name,
        container_id=container_id or "",
        state="running",
    )

    if container_id and not dry_run:
        from drydock.core.trust import _read_workspace_folder_from_overlay, seed_drydock_trust
        in_container_folder = _read_workspace_folder_from_overlay(str(overlay_path))
        seed_drydock_trust(container_id, in_container_folder)
        _maybe_setup_storage_mounts(container_id, spec)

    result = {
        "drydock_id": ws.id,
        "name": ws.name,
        "project": ws.project,
        "branch": ws.branch or f"ws/{ws.name}",
        "state": "running",
        "container_id": ws.container_id,
        "worktree_path": ws.worktree_path,
    }
    if result_parent_desk_id is not None:
        result["parent_drydock_id"] = result_parent_desk_id
    return result


def _load_parent_policy(registry: Registry, caller_drydock_id: str) -> DeskPolicy:
    raw_policy = registry.load_desk_policy(caller_drydock_id)
    if raw_policy is None:
        raise _RpcError(
            code=-32001,
            message="parent_not_found",
            data={"parent_drydock_id": caller_drydock_id},
        )

    try:
        firewall_domains_raw = _decode_string_list(raw_policy["delegatable_firewall_domains"])
        delegatable_secrets = frozenset(_decode_string_list(raw_policy["delegatable_secrets"]))
        capability_strings = _decode_string_list(raw_policy["capabilities"])
        config = json.loads(raw_policy["config"])
        extra_mounts_raw = config.get("extra_mounts", []) if isinstance(config, dict) else []
        if not isinstance(extra_mounts_raw, list) or any(not isinstance(item, str) for item in extra_mounts_raw):
            raise ValueError("invalid config.extra_mounts")
        capabilities = frozenset(CapabilityKind(value) for value in capability_strings)
        firewall_domains = frozenset(canonicalize_domain(value) for value in firewall_domains_raw)
        extra_mounts = frozenset(canonicalize_mount(value) for value in extra_mounts_raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise _RpcError(
            code=-32603,
            message="policy_load_failed",
            data={"reason": str(exc)},
        ) from exc

    return DeskPolicy(
        delegatable_firewall_domains=firewall_domains,
        delegatable_secrets=delegatable_secrets,
        capabilities=capabilities,
        extra_mounts=extra_mounts,
    )


def _build_child_spec(spec: dict[str, object]) -> DeskSpec:
    try:
        firewall_extra_domains = frozenset(
            canonicalize_domain(value) for value in spec["firewall_extra_domains"]
        )
        secret_entitlements = frozenset(spec["secret_entitlements"])
        capabilities = frozenset(CapabilityKind(value) for value in spec["capabilities"])
        extra_mounts = frozenset(canonicalize_mount(value) for value in spec["extra_mounts"])
    except (TypeError, ValueError) as exc:
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"reason": str(exc)},
        ) from exc

    return DeskSpec(
        firewall_extra_domains=firewall_extra_domains,
        secret_entitlements=secret_entitlements,
        capabilities=capabilities,
        extra_mounts=extra_mounts,
    )


def _decode_string_list(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise ValueError("expected JSON string list")
    decoded = json.loads(raw)
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError("expected JSON string list")
    return decoded


def _serialize_reject(reject: Reject) -> dict[str, object]:
    return {
        "rule": reject.rule,
        "parent_value": _serialize_value(reject.parent_value),
        "requested_value": _serialize_value(reject.requested_value),
        "offending_item": _serialize_value(reject.offending_item),
        "fix_hint": reject.fix_hint,
    }


def _serialize_value(value: object) -> object:
    if isinstance(value, CapabilityKind):
        return value.value
    if isinstance(value, frozenset):
        return sorted((_serialize_value(item) for item in value), key=lambda item: json.dumps(item))
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    return value


def _rpc_error_from_ws_error(exc: WsError) -> _RpcError:
    data: dict[str, object] = {"detail": exc.message}
    if exc.fix:
        data["fix"] = exc.fix
    if exc.context:
        data["context"] = exc.context
    if exc.code:
        data["error"] = exc.code
    return _RpcError(code=-32000, message="create_desk_failed", data=data)


def stop_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
    dry_run: bool,
) -> dict[str, object]:
    """Stop a Dock's container without destroying the drydock."""
    del request_id
    del caller_drydock_id
    target_name, target_drydock_id = _validated_stop_target(params)

    registry = Registry(db_path=registry_path)
    try:
        drydock = _lookup_by_name_or_id(registry, target_name, target_drydock_id)
        if drydock is None:
            raise _RpcError(
                code=-32001,
                message="desk_not_found",
                data={"drydock_id": target_drydock_id},
            )

        if not dry_run:
            devc = DevcontainerCLI(dry_run=False)
            devc.stop(container_id=drydock.container_id)

        registry.update_state(drydock.name, "suspended")
        return {
            "drydock_id": drydock.id,
            "name": drydock.name,
            "state": "suspended",
        }
    finally:
        registry.close()


def inspect_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
) -> dict[str, object]:
    """Return full details for one desk."""
    del request_id
    del caller_drydock_id
    target_name, target_drydock_id = _validated_stop_target(params)

    registry = Registry(db_path=registry_path)
    try:
        drydock = _lookup_by_name_or_id(registry, target_name, target_drydock_id)
        if drydock is None:
            raise _RpcError(
                code=-32001,
                message="desk_not_found",
                data={"drydock_id": target_drydock_id},
            )
        return drydock.to_dict()
    finally:
        registry.close()


def list_desks(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
) -> dict[str, object]:
    """List drydocks with optional filters."""
    del request_id
    del caller_drydock_id
    project: str | None = None
    state: str | None = None
    if isinstance(params, dict):
        project = params.get("project")
        state = params.get("state")

    registry = Registry(db_path=registry_path)
    try:
        drydocks = registry.list_drydocks(project=project, state=state)
        return {"desks": [ws.to_dict() for ws in drydocks]}
    finally:
        registry.close()


def list_children(
    params: dict | list | None,
    request_id: str | int | None,
    caller_drydock_id: str | None,
    *,
    registry_path: Path,
) -> dict[str, object]:
    """List children of a parent desk."""
    del request_id
    parent_id: str | None = None
    if isinstance(params, dict):
        parent_id = params.get("parent_id")
    if not parent_id:
        parent_id = caller_drydock_id
    if not parent_id:
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"missing": ["parent_id"]},
        )

    registry = Registry(db_path=registry_path)
    try:
        children = registry.get_children(parent_id)
        return {"children": [ws.to_dict() for ws in children]}
    finally:
        registry.close()


def _validated_stop_target(params: dict | list | None) -> tuple[str | None, str]:
    """Validate params for StopDesk / InspectDesk — accepts {name} or {drydock_id}."""
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"missing": ["name or drydock_id"]},
        )
    name = params.get("name")
    if isinstance(name, str) and name:
        return name, Drydock(name=name, project=name, repo_path="").id

    drydock_id = params.get("drydock_id")
    if isinstance(drydock_id, str) and drydock_id:
        return None, drydock_id

    raise _RpcError(
        code=-32602,
        message="invalid_params",
        data={"missing": ["name or drydock_id"]},
    )


def _lookup_by_name_or_id(
    registry: Registry,
    target_name: str | None,
    target_drydock_id: str,
) -> Drydock | None:
    """Look up a drydock by name or drydock_id."""
    if target_name is not None:
        return registry.get_drydock(target_name)
    row = registry._conn.execute(
        "SELECT * FROM drydocks WHERE id = ?",
        (target_drydock_id,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_drydock(row)


def _ensure_gitconfig_stub() -> None:
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        return
    gitconfig.touch(mode=0o644)
    logger.info("daemon: created empty %s for devcontainer bind-mount", gitconfig)
