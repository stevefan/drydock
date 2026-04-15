"""JSON-RPC method handlers for the wsd daemon."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from drydock.core import WsError
from drydock.core.checkout import create_checkout
from drydock.core.devcontainer import DevcontainerCLI
from drydock.core.overlay import OverlayConfig, write_overlay
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.wsd.server import _RpcError

logger = logging.getLogger(__name__)

_REQUIRED_PARAMS = ("project", "name")
def create_desk(
    params: dict | list | None,
    *,
    registry_path: Path,
    dry_run: bool,
) -> dict:
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

        ws = Workspace(
            name=spec["name"],
            project=spec["project"],
            repo_path=spec["repo_path"],
            branch=spec["branch"],
            base_ref=spec["base_ref"],
            image=spec["image"],
            owner=spec["owner"],
        )
        ws = registry.create_workspace(ws)

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
        ws = registry.update_workspace(ws.name, config={"overlay_path": str(overlay_path)})

        devc = DevcontainerCLI(dry_run=dry_run)
        if not dry_run:
            devc.check_available()

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
        return {
            "desk_id": ws.id,
            "name": ws.name,
            "project": ws.project,
            "branch": ws.branch or f"ws/{ws.name}",
            "state": "running",
            "container_id": ws.container_id,
            "worktree_path": ws.worktree_path,
        }
    except WsError as exc:
        raise _rpc_error_from_ws_error(exc) from exc
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

    return {
        "project": project,
        "name": name,
        "repo_path": repo_path,
        "branch": branch,
        "base_ref": base_ref,
        "image": image,
        "owner": owner,
    }


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
