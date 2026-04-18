"""ws project — reconcile project YAML into the registry and regenerate overlay.

Closes the YAML-drift gap: the registry stores a snapshot of project
config at `ws create` time. Editing the YAML afterward (adding a new
firewall domain, changing `extra_mounts`, granting a capability) didn't
reach the registry until we did manual sqlite surgery or `--force`
destroyed + recreated the drydock.

`ws project reload <name>` re-reads `~/.drydock/projects/<project>.yaml`,
updates the drydock's registry `config` JSON + V2 policy columns
(capabilities, delegatable_secrets, delegatable_firewall_domains,
delegatable_storage_scopes), then regenerates the overlay JSON on disk.

The running container itself does NOT pick up new mounts or env vars
until `ws stop <name> && ws create <name>` — docker bind-mounts are
baked at container start. Firewall-domain changes take effect when the
next `refresh-firewall-allowlist.sh` tick runs inside the container,
which re-resolves DNS from the env var (also baked at start, so
newly-added domains need a recreate before they land in the ipset).

The command prints the next-step hint.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from drydock.core import WsError
from drydock.core.overlay import regenerate_overlay_from_workspace
from drydock.core.project_config import ProjectConfig, load_project_config


# Registry config keys populated from ProjectConfig fields. Mirrors
# _perform_create / _overlay_config_data in the daemon so a reload
# converges to the same stored shape a fresh create would produce.
_CONFIG_FIELDS_FROM_YAML = (
    "tailscale_hostname",
    "tailscale_serve_port",
    "tailscale_authkey_env_var",
    "remote_control_name",
    "firewall_extra_domains",
    "firewall_ipv6_hosts",
    "forward_ports",
    "claude_profile",
    "extra_mounts",
    "workspace_subdir",
    "devcontainer_subpath",
)


@click.group()
def project():
    """Manage project YAML → registry reconciliation."""


@project.command("reload")
@click.argument("name")
@click.option(
    "--no-regenerate",
    is_flag=True,
    help="Skip overlay regeneration (just update registry).",
)
@click.pass_context
def project_reload(ctx, name, no_regenerate):
    """Re-read <name>'s project YAML and apply to the registry + overlay."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_workspace(name)
    if ws is None:
        out.error(
            WsError(
                f"Drydock '{name}' not found",
                fix="Check `ws list` for the name",
                code="desk_not_found",
            )
        )
        return

    try:
        proj = load_project_config(ws.project)
    except WsError as e:
        out.error(e)
        return
    if proj is None:
        out.error(
            WsError(
                f"Project YAML for '{ws.project}' not found at ~/.drydock/projects/{ws.project}.yaml",
                fix=f"Create ~/.drydock/projects/{ws.project}.yaml before reloading",
                code="project_yaml_not_found",
            )
        )
        return

    # Update registry config JSON with YAML fields.
    current_config = ws.config if isinstance(ws.config, dict) else {}
    new_config = dict(current_config)
    for field in _CONFIG_FIELDS_FROM_YAML:
        value = getattr(proj, field, None)
        # Preserve empty-list semantics — an explicit `firewall_extra_domains: []`
        # in the YAML should clear any prior entries, not be ignored.
        if value is None:
            continue
        new_config[field] = value

    registry.update_workspace(name, config=new_config)

    # Update V2 policy columns via the dedicated method so we get the
    # same JSON-encoding + column-writing path `_perform_create` uses.
    delegation_kwargs = {}
    if proj.capabilities:
        delegation_kwargs["capabilities"] = list(proj.capabilities)
    if proj.delegatable_secrets:
        delegation_kwargs["delegatable_secrets"] = list(proj.delegatable_secrets)
    if proj.delegatable_firewall_domains:
        delegation_kwargs["delegatable_firewall_domains"] = list(proj.delegatable_firewall_domains)
    if proj.delegatable_storage_scopes:
        delegation_kwargs["delegatable_storage_scopes"] = list(proj.delegatable_storage_scopes)
    if delegation_kwargs:
        registry.update_desk_delegations(name, **delegation_kwargs)

    overlay_path: Path | None = None
    if not no_regenerate:
        # Re-fetch so the overlay regen sees the updated config JSON.
        refreshed = registry.get_workspace(name)
        try:
            overlay_path = regenerate_overlay_from_workspace(refreshed)
        except WsError as e:
            out.error(e)
            return

    result: dict[str, object] = {
        "name": name,
        "project": ws.project,
        "registry_updated": True,
        "overlay_regenerated": overlay_path is not None,
    }
    if overlay_path is not None:
        result["overlay_path"] = str(overlay_path)

    human_lines = [
        f"project '{ws.project}' reloaded into drydock '{name}'",
        f"  registry config: {len(new_config)} keys",
    ]
    if delegation_kwargs:
        human_lines.append(f"  policy columns: {sorted(delegation_kwargs)}")
    if overlay_path is not None:
        human_lines.append(f"  overlay: {overlay_path}")
    if ws.state == "running":
        human_lines.append(
            f"  next: `ws stop {name} && ws create {name}` "
            "to apply mount / env changes to the running container"
        )
    out.success(result, human_lines=human_lines)
