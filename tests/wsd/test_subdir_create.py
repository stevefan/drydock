"""_validated_spec must round-trip workspace_subdir through the RPC.

Regression: without this, subdir-desk creates via wsd routed through
the daemon landed in the repo root, merged the wrong base
devcontainer.json, and silently lost postCreateCommand. This caused
auction-crawl's pip-install to not run on every container rebuild,
so the project's binary was missing until somebody noticed.
"""

from __future__ import annotations

from drydock.wsd.handlers import _validated_spec


def _minimal_params(**overrides):
    params = {
        "project": "p", "name": "p", "repo_path": "/srv/code/p",
        "branch": "ws/p", "base_ref": "HEAD", "image": "",
        "owner": "", "devcontainer_subpath": ".devcontainer",
        "tailscale_hostname": None, "tailscale_serve_port": None,
        "tailscale_authkey_env_var": None, "remote_control_name": None,
        "firewall_extra_domains": [], "firewall_ipv6_hosts": [],
        "firewall_aws_ip_ranges": [], "forward_ports": [],
        "claude_profile": None, "extra_env": {}, "extra_mounts": [],
        "secret_entitlements": [],
        "delegatable_firewall_domains": [], "delegatable_secrets": [],
        "capabilities": [],
        "delegatable_storage_scopes": [],
        "delegatable_provision_scopes": [],
        "storage_mounts": [],
    }
    params.update(overrides)
    return params


def test_validated_spec_propagates_workspace_subdir():
    spec = _validated_spec(_minimal_params(workspace_subdir="auction-crawl"))
    assert spec["workspace_subdir"] == "auction-crawl"


def test_validated_spec_defaults_workspace_subdir_to_empty():
    """Desks that don't declare a subdir must not surprise the handler
    with a missing key; default to '' so downstream paths can
    unconditionally read spec['workspace_subdir']."""
    spec = _validated_spec(_minimal_params())
    assert spec["workspace_subdir"] == ""
