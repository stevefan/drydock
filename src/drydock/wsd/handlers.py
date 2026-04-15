"""JSON-RPC method handlers for the wsd daemon."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import uuid4

from drydock.core import WsError
from drydock.core.checkout import create_checkout
from drydock.core.devcontainer import DevcontainerCLI
from drydock.core.overlay import OverlayConfig, write_overlay
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


def create_desk(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict:
    del request_id
    del caller_desk_id
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
        return _perform_create(
            registry=registry,
            spec=spec,
            secrets_root=secrets_root,
            dry_run=dry_run,
        )
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


def spawn_child(
    params: dict | list | None,
    request_id: str | int | None,
    caller_desk_id: str | None,
    *,
    registry_path: Path,
    secrets_root: Path,
    dry_run: bool,
) -> dict:
    del request_id
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

        parent_policy = _load_parent_policy(registry, caller_desk_id)
        child_spec = _build_child_spec(spec)
        verdict = validate_spawn(parent_policy, child_spec)
        if isinstance(verdict, Reject):
            raise _RpcError(
                code=-32001,
                message="narrowness_violated",
                data={"reject": _serialize_reject(verdict)},
            )

        return _perform_create(
            registry=registry,
            spec=spec,
            secrets_root=secrets_root,
            dry_run=dry_run,
            parent_desk_id=caller_desk_id,
            result_parent_desk_id=caller_desk_id,
        )
    except WsError as exc:
        raise _RpcError(
            code=-32000,
            message="spawn_failed",
            data={"detail": exc.message},
        ) from exc
    finally:
        registry.close()


def _validated_spec(params: dict | list | None) -> dict[str, str]:
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

    firewall_extra_domains = _validated_string_list(
        params.get("firewall_extra_domains"),
        field_name="firewall_extra_domains",
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

    return {
        "project": project,
        "name": name,
        "repo_path": repo_path,
        "branch": branch,
        "base_ref": base_ref,
        "image": image,
        "owner": owner,
        "firewall_extra_domains": firewall_extra_domains,
        "secret_entitlements": secret_entitlements,
        "extra_mounts": extra_mounts,
        "delegatable_firewall_domains": delegatable_firewall_domains,
        "delegatable_secrets": delegatable_secrets,
        "capabilities": capabilities,
    }


def _validated_string_list(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _RpcError(
            code=-32602,
            message="invalid_params",
            data={"field": field_name},
        )
    return value


def _perform_create(
    *,
    registry: Registry,
    spec: dict[str, object],
    secrets_root: Path,
    dry_run: bool,
    parent_desk_id: str | None = None,
    result_parent_desk_id: str | None = None,
) -> dict[str, object]:
    ws = Workspace(
        name=str(spec["name"]),
        project=str(spec["project"]),
        repo_path=str(spec["repo_path"]),
        branch=str(spec["branch"]),
        base_ref=str(spec["base_ref"]),
        image=str(spec["image"]),
        owner=str(spec["owner"]),
        config={"extra_mounts": list(spec["extra_mounts"])},
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
    devcontainer_json = Path(workspace_folder) / ".devcontainer" / "devcontainer.json"
    if not devcontainer_json.exists():
        registry.update_state(ws.name, "error")
        raise WsError(
            f"devcontainer.json not found at {devcontainer_json}",
            fix=(
                f"Create {workspace_folder}/.devcontainer/devcontainer.json, "
                "or use a repo that already has one"
            ),
        )

    _ensure_gitconfig_stub()
    overlay_path = write_overlay(
        ws,
        Path.home() / ".drydock" / "overlays",
        OverlayConfig(),
        base_devcontainer_path=devcontainer_json,
    )
    ws = registry.update_workspace(
        ws.name,
        config={"overlay_path": str(overlay_path), "extra_mounts": list(spec["extra_mounts"])},
    )

    devc = DevcontainerCLI(dry_run=dry_run)
    if not dry_run:
        devc.check_available()

    issue_token_for_desk(ws.id, secrets_root=secrets_root, registry=registry)
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


def _ensure_gitconfig_stub() -> None:
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        return
    gitconfig.touch(mode=0o644)
    logger.info("wsd: created empty %s for devcontainer bind-mount", gitconfig)
