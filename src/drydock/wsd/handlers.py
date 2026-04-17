"""JSON-RPC method handlers for the wsd daemon."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

from drydock.core import WsError
from drydock.core.audit import emit_audit
from drydock.core.checkout import create_checkout
from drydock.core.devcontainer import DevcontainerCLI
from drydock.core.overlay import OverlayConfig, remove_overlay, write_overlay
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
from drydock.core.workspace import Workspace
from drydock.wsd.auth import issue_token_for_desk
from drydock.wsd.server import _RpcError

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
    "forward_ports",
    "claude_profile",
)


def create_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict:
    del caller_desk_id
    spec = _validated_spec(params)
    registry = Registry(db_path=registry_path)
    try:
        existing = registry.get_workspace(spec["name"])
        if existing is not None:
            if existing.state in ("suspended", "defined"):
                result = _resume_desk(existing, registry=registry, dry_run=dry_run)
                emit_audit(
                    "desk.resumed",
                    principal=None,
                    request_id=request_id,
                    method="CreateDesk",
                    result="ok",
                    details={
                        "desk_id": result.get("desk_id"),
                        "project": result.get("project"),
                    },
                )
                return result
            raise _RpcError(
                code=-32001,
                message="workspace_already_running",
                data={"fix": f"ws create {spec['name']} --force"},
            )
        result = _perform_create(
            registry=registry,
            spec=spec,
            secrets_root=secrets_root,
            dry_run=dry_run,
        )
        emit_audit(
            "desk.created",
            principal=None,
            request_id=request_id,
            method="CreateDesk",
            result="ok",
            details={
                "desk_id": result.get("desk_id"),
                "project": result.get("project"),
                "parent_desk_id": None,
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
    caller_desk_id: str | None,
) -> dict[str, str | None]:
    del params
    del request_id
    return {"desk_id": caller_desk_id}


def destroy_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict[str, object]:
    logger.info("wsd: DestroyDesk requested caller_desk_id=%s", caller_desk_id)
    target_name, target_hint = _validated_destroy_target(params)

    registry = Registry(db_path=registry_path)
    try:
        workspace = _lookup_destroy_target(registry, target_name=target_name, target_hint=target_hint)
        if workspace is None:
            raise _RpcError(
                code=-32001,
                message="desk_not_found",
                data={"desk_id": target_hint},
            )

        cascaded: list[str] = []
        target_desk_id = workspace.id
        partial_failures = _destroy_tree(
            workspace,
            registry=registry,
            secrets_root=secrets_root,
            dry_run=dry_run,
            cascaded=cascaded,
            visited=set(),
        )

        result: dict[str, object] = {
            "destroyed": True,
            "desk_id": target_desk_id,
            "cascaded": cascaded,
        }
        if partial_failures:
            result["partial_failures"] = partial_failures
        emit_audit(
            "desk.destroyed",
            principal=caller_desk_id,
            request_id=request_id,
            method="DestroyDesk",
            result="error" if partial_failures else "ok",
            details={
                "desk_id": target_desk_id,
                "cascaded_children": cascaded,
            },
        )
        return result
    finally:
        registry.close()


def spawn_child(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict:
    if caller_desk_id is None:
        raise _RpcError(code=-32004, message="unauthenticated")

    spec = _validated_spec(params)
    registry = Registry(db_path=registry_path)
    try:
        existing = registry.get_workspace(spec["name"])
        if existing is not None:
            raise _RpcError(
                code=-32001,
                message="workspace_already_running",
                data={"fix": f"ws create {spec['name']} --force"},
            )

        # Per-parent in-flight cap. Counts children already in
        # 'provisioning' state (the window between create_workspace
        # and the container reaching 'running'). Cheaper than parsing
        # task_log; reuses an existing column.
        in_flight = _count_provisioning_children(registry, caller_desk_id)
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

        parent_policy = _load_parent_policy(registry, caller_desk_id)
        child_spec = _build_child_spec(spec)
        verdict = validate_spawn(parent_policy, child_spec)
        if isinstance(verdict, Reject):
            emit_audit(
                "desk.spawn_rejected",
                principal=caller_desk_id,
                request_id=request_id,
                method="SpawnChild",
                result="error",
                details={
                    "parent_desk_id": caller_desk_id,
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
            parent_desk_id=caller_desk_id,
            result_parent_desk_id=caller_desk_id,
        )
        emit_audit(
            "desk.spawned",
            principal=caller_desk_id,
            request_id=request_id,
            method="SpawnChild",
            result="ok",
            details={
                "desk_id": result.get("desk_id"),
                "parent_desk_id": caller_desk_id,
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

    return {
        "project": project,
        "name": name,
        "repo_path": repo_path,
        "branch": branch,
        "base_ref": base_ref,
        "image": image,
        "owner": owner,
        "devcontainer_subpath": devcontainer_subpath,
        "tailscale_hostname": tailscale_hostname,
        "tailscale_serve_port": tailscale_serve_port,
        "tailscale_authkey_env_var": tailscale_authkey_env_var,
        "remote_control_name": remote_control_name,
        "firewall_extra_domains": firewall_extra_domains,
        "firewall_ipv6_hosts": firewall_ipv6_hosts,
        "forward_ports": forward_ports,
        "claude_profile": claude_profile,
        "secret_entitlements": secret_entitlements,
        "extra_mounts": extra_mounts,
        "delegatable_firewall_domains": delegatable_firewall_domains,
        "delegatable_secrets": delegatable_secrets,
        "capabilities": capabilities,
    }


def _validated_destroy_target(params: dict | list | None) -> tuple[str | None, str]:
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"missing": ["desk_id"]},
        )

    name = params.get("name")
    if isinstance(name, str) and name:
        return name, Workspace(name=name, project=name, repo_path="").id

    desk_id = params.get("desk_id")
    if isinstance(desk_id, str) and desk_id:
        return None, desk_id

    raise _RpcError(
        code=-32602,
        message="invalid_params",
        data={"missing": ["desk_id"]},
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

    forward_ports = spec.get("forward_ports")
    if isinstance(forward_ports, list) and forward_ports:
        kwargs["forward_ports"] = forward_ports

    claude_profile = spec.get("claude_profile")
    if isinstance(claude_profile, str):
        kwargs["claude_profile"] = claude_profile

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
) -> Workspace | None:
    if target_name is not None:
        return registry.get_workspace(target_name)

    row = registry._conn.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (target_hint,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_workspace(row)


def _destroy_tree(
    workspace: Workspace,
    *,
    registry: Registry,
    secrets_root: Path,
    dry_run: bool,
    cascaded: list[str],
    visited: set[str],
) -> list[dict[str, str]]:
    if workspace.id in visited:
        raise _RpcError(
            code=-32603,
            message="destroy_cycle_detected",
            data={"desk_id": workspace.id},
        )
    visited.add(workspace.id)

    partial_failures: list[dict[str, str]] = []
    for child in registry.get_children(workspace.id):
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
            workspace,
            registry=registry,
            secrets_root=secrets_root,
            dry_run=dry_run,
        )
    )
    visited.remove(workspace.id)
    return partial_failures


def _destroy_one(
    workspace: Workspace,
    registry: Registry,
    secrets_root: Path,
    dry_run: bool,
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    devc = DevcontainerCLI(dry_run=dry_run)
    logger.info("wsd: destroy container desk_id=%s container_id=%s", workspace.id, workspace.container_id)
    if workspace.container_id:
        try:
            devc.stop(workspace.container_id)
            logger.info("wsd: container stopped desk_id=%s", workspace.id)
        except Exception as exc:
            logger.warning("wsd: failed to stop container for %s: %s", workspace.id, exc)
            failures.append(_failure(workspace.id, "container_stop", exc))
        try:
            devc.remove(workspace.container_id)
            logger.info("wsd: container removed desk_id=%s", workspace.id)
        except Exception as exc:
            logger.warning("wsd: failed to remove container for %s: %s", workspace.id, exc)
            failures.append(_failure(workspace.id, "container_remove", exc))

    # Capability-broker.md §6a: outstanding leases for the desk are
    # marked revoked before token + container teardown, so a racing
    # in-flight RequestCapability sees `desk_destroyed` rather than a
    # phantom lease against a soon-to-be-gone desk.
    try:
        active_leases = registry.list_active_leases_for_desk(workspace.id)
        revoked = registry.revoke_leases_for_desk(workspace.id, "desk_destroyed")
        if revoked:
            logger.info(
                "wsd: revoked %d active leases for desk_id=%s",
                revoked, workspace.id,
            )
        for lease in active_leases:
            emit_audit(
                "lease.released",
                principal=workspace.id,
                request_id=None,
                method="DestroyDesk",
                result="ok",
                details={"lease_id": lease.lease_id, "reason": "desk_destroyed"},
            )
    except Exception as exc:
        logger.warning("wsd: failed to revoke leases for %s: %s", workspace.id, exc)
        failures.append(_failure(workspace.id, "lease_revoke", exc))

    secret_dir = Path(secrets_root) / workspace.id
    token_path = secret_dir / "drydock-token"
    logger.info("wsd: revoke token desk_id=%s", workspace.id)
    try:
        registry.delete_token(workspace.id)
        emit_audit(
            "token.revoked",
            principal=workspace.id,
            request_id=None,
            method="DestroyDesk",
            result="ok",
            details={"desk_id": workspace.id},
        )
        logger.info("wsd: token row removed desk_id=%s", workspace.id)
    except Exception as exc:
        logger.warning("wsd: failed to delete token row for %s: %s", workspace.id, exc)
        failures.append(_failure(workspace.id, "token_delete", exc))
    try:
        token_path.unlink(missing_ok=True)
        logger.info("wsd: token file removed desk_id=%s path=%s", workspace.id, token_path)
    except Exception as exc:
        logger.warning("wsd: failed to remove token file for %s: %s", workspace.id, exc)
        failures.append(_failure(workspace.id, "token_file_remove", exc))
    shutil.rmtree(secret_dir, ignore_errors=True)
    if secret_dir.exists():
        logger.warning("wsd: failed to remove secret dir for %s: %s", workspace.id, secret_dir)
        failures.append(_failure(workspace.id, "secret_dir_remove", "directory still exists"))
    else:
        logger.info("wsd: secret dir removed desk_id=%s path=%s", workspace.id, secret_dir)

    logger.info("wsd: remove worktree desk_id=%s path=%s", workspace.id, workspace.worktree_path)
    worktree_path = Path(workspace.worktree_path) if workspace.worktree_path else None
    if worktree_path is not None:
        shutil.rmtree(worktree_path, ignore_errors=True)
        if worktree_path.exists():
            logger.warning("wsd: failed to remove worktree for %s: %s", workspace.id, worktree_path)
            failures.append(_failure(workspace.id, "worktree_remove", "directory still exists"))
        else:
            logger.info("wsd: worktree removed desk_id=%s", workspace.id)

    overlay_path = workspace.config.get("overlay_path")
    if not isinstance(overlay_path, str) or not overlay_path:
        overlay_path = str(Path.home() / ".drydock" / "overlays" / f"{workspace.id}.devcontainer.json")
    logger.info("wsd: remove overlay desk_id=%s path=%s", workspace.id, overlay_path)
    try:
        remove_overlay(overlay_path)
        logger.info("wsd: overlay removed desk_id=%s", workspace.id)
    except FileNotFoundError:
        logger.info("wsd: overlay already absent desk_id=%s", workspace.id)
    except Exception as exc:
        logger.warning("wsd: failed to remove overlay for %s: %s", workspace.id, exc)
        failures.append(_failure(workspace.id, "overlay_remove", exc))

    logger.info("wsd: delete workspace row desk_id=%s name=%s", workspace.id, workspace.name)
    try:
        registry.delete_workspace(workspace.name)
        logger.info("wsd: workspace row removed desk_id=%s", workspace.id)
    except Exception as exc:
        logger.warning("wsd: failed to delete workspace row for %s: %s", workspace.id, exc)
        failures.append(_failure(workspace.id, "workspace_delete", exc))

    return failures


def _failure(desk_id: str, step: str, error: Exception | str) -> dict[str, str]:
    return {"desk_id": desk_id, "step": step, "error": str(error)}


def _count_provisioning_children(registry: Registry, parent_desk_id: str) -> int:
    """Count children of `parent_desk_id` currently in 'provisioning' state.

    Used by SpawnChild to enforce the per-parent in-flight cap. Cheap
    SQL count; no row materialization. Uses the existing parent_desk_id
    column added in the V2 schema migration.
    """
    row = registry._conn.execute(
        """
        SELECT COUNT(*) AS n FROM workspaces
        WHERE parent_desk_id = ? AND state = 'provisioning'
        """,
        (parent_desk_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def _validate_devcontainer_subpath(devcontainer_subpath: str) -> None:
    subpath = Path(devcontainer_subpath)
    if subpath.is_absolute() or ".." in subpath.parts:
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"reason": "devcontainer_subpath must be relative and contain no .."},
        )


def _resume_desk(
    existing: Workspace,
    *,
    registry: Registry,
    dry_run: bool,
) -> dict[str, object]:
    """Re-up the container for a suspended/defined desk.

    Worktree, overlay, token, and named volumes are reused. Container is fresh.
    """
    config = existing.config or {}
    overlay_path = config.get("overlay_path") if isinstance(config, dict) else None
    if not overlay_path or not Path(overlay_path).exists():
        registry.update_state(existing.name, "error")
        raise WsError(
            f"Cannot resume desk '{existing.name}': overlay missing at {overlay_path or '(unset)'}",
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

    ws = registry.update_workspace(
        existing.name,
        container_id=container_id or "",
        state="running",
    )

    if container_id and not dry_run:
        from drydock.core.trust import _read_workspace_folder_from_overlay, seed_workspace_trust
        in_container_folder = _read_workspace_folder_from_overlay(str(overlay_path))
        seed_workspace_trust(container_id, in_container_folder)

    return {
        "desk_id": ws.id,
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
    parent_desk_id: str | None = None,
    result_parent_desk_id: str | None = None,
) -> dict[str, object]:
    overlay_config = _overlay_from_spec(spec)
    overlay_config_data = _overlay_config_data(spec)
    ws = Workspace(
        name=str(spec["name"]),
        project=str(spec["project"]),
        repo_path=str(spec["repo_path"]),
        branch=str(spec["branch"]),
        base_ref=str(spec["base_ref"]),
        image=str(spec["image"]),
        owner=str(spec["owner"]),
        config={
            "devcontainer_subpath": str(spec["devcontainer_subpath"]),
            "extra_mounts": list(spec["extra_mounts"]),
            **overlay_config_data,
        },
    )
    ws = registry.create_workspace(ws)
    registry.update_desk_delegations(
        ws.name,
        delegatable_firewall_domains=list(spec["delegatable_firewall_domains"]),
        delegatable_secrets=list(spec["delegatable_secrets"]),
        capabilities=list(spec["capabilities"]),
    )
    if parent_desk_id is not None:
        ws = registry.update_workspace(ws.name, parent_desk_id=parent_desk_id)

    checkout_path = create_checkout(ws)
    ws = registry.update_workspace(ws.name, worktree_path=str(checkout_path))

    workspace_folder = ws.worktree_path
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
    overlay_path = write_overlay(
        ws,
        Path.home() / ".drydock" / "overlays",
        overlay_config,
        base_devcontainer_path=devcontainer_json,
    )
    ws = registry.update_workspace(
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
        method="CreateDesk" if parent_desk_id is None else "SpawnChild",
        result="ok",
        details={"desk_id": ws.id, "rotation_reason": None},
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

    ws = registry.update_workspace(
        ws.name,
        container_id=container_id or "",
        state="running",
    )

    if container_id and not dry_run:
        from drydock.core.trust import _read_workspace_folder_from_overlay, seed_workspace_trust
        in_container_folder = _read_workspace_folder_from_overlay(str(overlay_path))
        seed_workspace_trust(container_id, in_container_folder)

    result = {
        "desk_id": ws.id,
        "name": ws.name,
        "project": ws.project,
        "branch": ws.branch or f"ws/{ws.name}",
        "state": "running",
        "container_id": ws.container_id,
        "worktree_path": ws.worktree_path,
    }
    if result_parent_desk_id is not None:
        result["parent_desk_id"] = result_parent_desk_id
    return result


def _load_parent_policy(registry: Registry, caller_desk_id: str) -> DeskPolicy:
    raw_policy = registry.load_desk_policy(caller_desk_id)
    if raw_policy is None:
        raise _RpcError(
            code=-32001,
            message="parent_not_found",
            data={"parent_desk_id": caller_desk_id},
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
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    dry_run: bool,
) -> dict[str, object]:
    """Stop a desk's container without destroying the workspace."""
    del request_id
    del caller_desk_id
    target_name, target_desk_id = _validated_stop_target(params)

    registry = Registry(db_path=registry_path)
    try:
        workspace = _lookup_by_name_or_id(registry, target_name, target_desk_id)
        if workspace is None:
            raise _RpcError(
                code=-32001,
                message="desk_not_found",
                data={"desk_id": target_desk_id},
            )

        if not dry_run:
            devc = DevcontainerCLI(dry_run=False)
            devc.stop(container_id=workspace.container_id)

        registry.update_state(workspace.name, "suspended")
        return {
            "desk_id": workspace.id,
            "name": workspace.name,
            "state": "suspended",
        }
    finally:
        registry.close()


def inspect_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
) -> dict[str, object]:
    """Return full details for one desk."""
    del request_id
    del caller_desk_id
    target_name, target_desk_id = _validated_stop_target(params)

    registry = Registry(db_path=registry_path)
    try:
        workspace = _lookup_by_name_or_id(registry, target_name, target_desk_id)
        if workspace is None:
            raise _RpcError(
                code=-32001,
                message="desk_not_found",
                data={"desk_id": target_desk_id},
            )
        return workspace.to_dict()
    finally:
        registry.close()


def list_desks(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
) -> dict[str, object]:
    """List workspaces with optional filters."""
    del request_id
    del caller_desk_id
    project: str | None = None
    state: str | None = None
    if isinstance(params, dict):
        project = params.get("project")
        state = params.get("state")

    registry = Registry(db_path=registry_path)
    try:
        workspaces = registry.list_workspaces(project=project, state=state)
        return {"desks": [ws.to_dict() for ws in workspaces]}
    finally:
        registry.close()


def list_children(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
) -> dict[str, object]:
    """List children of a parent desk."""
    del request_id
    parent_id: str | None = None
    if isinstance(params, dict):
        parent_id = params.get("parent_id")
    if not parent_id:
        parent_id = caller_desk_id
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
    """Validate params for StopDesk / InspectDesk — accepts {name} or {desk_id}."""
    if not isinstance(params, dict):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"missing": ["name or desk_id"]},
        )
    name = params.get("name")
    if isinstance(name, str) and name:
        return name, Workspace(name=name, project=name, repo_path="").id

    desk_id = params.get("desk_id")
    if isinstance(desk_id, str) and desk_id:
        return None, desk_id

    raise _RpcError(
        code=-32602,
        message="invalid_params",
        data={"missing": ["name or desk_id"]},
    )


def _lookup_by_name_or_id(
    registry: Registry,
    target_name: str | None,
    target_desk_id: str,
) -> Workspace | None:
    """Look up a workspace by name or desk_id."""
    if target_name is not None:
        return registry.get_workspace(target_name)
    row = registry._conn.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (target_desk_id,),
    ).fetchone()
    if row is None:
        return None
    return registry._row_to_workspace(row)


def _ensure_gitconfig_stub() -> None:
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        return
    gitconfig.touch(mode=0o644)
    logger.info("wsd: created empty %s for devcontainer bind-mount", gitconfig)
