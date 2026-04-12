"""Tests for devcontainer override generator."""

import json
from pathlib import Path

import pytest

from drydock.core.overlay import (
    OverlayConfig,
    generate_overlay,
    write_overlay,
)
from drydock.core.project_config import load_project_config
from drydock.core.workspace import Workspace


@pytest.fixture
def ws():
    return Workspace(
        name="payments-refactor",
        project="app",
        repo_path="/srv/code/app",
        branch="ws/payments-refactor",
    )


class TestGenerateOverlay:
    def test_minimal_overlay_has_name(self, ws):
        overlay = generate_overlay(ws)
        assert overlay["name"] == "payments-refactor"

    def test_workspace_identity_env_vars(self, ws):
        overlay = generate_overlay(ws)
        env = overlay["containerEnv"]
        assert env["DRYDOCK_WORKSPACE_ID"] == ws.id
        assert env["DRYDOCK_WORKSPACE_NAME"] == "payments-refactor"
        assert env["DRYDOCK_PROJECT"] == "app"

    def test_tailscale_hostname_defaults_to_workspace_name(self, ws):
        overlay = generate_overlay(ws)
        assert overlay["containerEnv"]["TAILSCALE_HOSTNAME"] == "payments-refactor"

    def test_tailscale_hostname_override(self, ws):
        config = OverlayConfig(tailscale_hostname="my-custom-host")
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["TAILSCALE_HOSTNAME"] == "my-custom-host"

    def test_tailscale_authkey_included_when_set(self, ws):
        config = OverlayConfig(tailscale_authkey="tskey-auth-abc123")
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["TAILSCALE_AUTHKEY"] == "tskey-auth-abc123"

    def test_tailscale_authkey_omitted_when_empty(self, ws):
        overlay = generate_overlay(ws)
        assert "TAILSCALE_AUTHKEY" not in overlay["containerEnv"]

    def test_tailscale_serve_port_default(self, ws):
        overlay = generate_overlay(ws)
        assert overlay["containerEnv"]["TAILSCALE_SERVE_PORT"] == "3000"

    def test_tailscale_serve_port_custom(self, ws):
        config = OverlayConfig(tailscale_serve_port=8080)
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["TAILSCALE_SERVE_PORT"] == "8080"

    def test_remote_control_name_defaults_to_workspace_name(self, ws):
        overlay = generate_overlay(ws)
        assert overlay["containerEnv"]["REMOTE_CONTROL_NAME"] == "payments-refactor"

    def test_remote_control_name_override(self, ws):
        config = OverlayConfig(remote_control_name="My Agent")
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["REMOTE_CONTROL_NAME"] == "My Agent"

    def test_firewall_extra_domains(self, ws):
        config = OverlayConfig(firewall_extra_domains=["example.com", "api.stripe.com"])
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["FIREWALL_EXTRA_DOMAINS"] == "example.com api.stripe.com"

    def test_firewall_extra_domains_omitted_when_empty(self, ws):
        overlay = generate_overlay(ws)
        assert "FIREWALL_EXTRA_DOMAINS" not in overlay["containerEnv"]

    def test_firewall_ipv6_hosts(self, ws):
        config = OverlayConfig(firewall_ipv6_hosts=["[::1]:8080"])
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["FIREWALL_IPV6_HOSTS"] == "[::1]:8080"

    def test_extra_env_merged(self, ws):
        config = OverlayConfig(extra_env={"MY_VAR": "hello"})
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["MY_VAR"] == "hello"

    def test_extra_env_can_override_defaults(self, ws):
        config = OverlayConfig(extra_env={"DRYDOCK_PROJECT": "overridden"})
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["DRYDOCK_PROJECT"] == "overridden"

    def test_secrets_mount_uses_project_scoped_path(self, ws):
        overlay = generate_overlay(ws)
        mounts = overlay["mounts"]
        assert any("/srv/secrets/app" in m and "/run/secrets" in m for m in mounts)

    def test_secrets_mount_is_readonly(self, ws):
        overlay = generate_overlay(ws)
        secrets_mount = [m for m in overlay["mounts"] if "/run/secrets" in m][0]
        assert "readonly" in secrets_mount

    def test_custom_secrets_paths(self, ws):
        config = OverlayConfig(
            secrets_host_dir="/opt/secrets",
            secrets_container_dir="/secrets",
        )
        overlay = generate_overlay(ws, config)
        secrets_mount = [m for m in overlay["mounts"] if "/secrets" in m][0]
        assert "/opt/secrets/app" in secrets_mount
        assert "target=/secrets" in secrets_mount

    def test_extra_mounts_appended(self, ws):
        config = OverlayConfig(
            extra_mounts=["source=/data,target=/data,type=bind"]
        )
        overlay = generate_overlay(ws, config)
        assert "source=/data,target=/data,type=bind" in overlay["mounts"]

    def test_overlay_is_valid_json_serializable(self, ws):
        config = OverlayConfig(
            firewall_extra_domains=["a.com"],
            extra_env={"K": "V"},
        )
        overlay = generate_overlay(ws, config)
        roundtripped = json.loads(json.dumps(overlay))
        assert roundtripped == overlay


class TestWriteOverlay:
    def test_writes_file_to_output_dir(self, ws, tmp_path):
        path = write_overlay(ws, tmp_path)
        assert path.exists()
        assert path.name == f"{ws.id}.devcontainer.override.json"

    def test_written_file_is_valid_json(self, ws, tmp_path):
        path = write_overlay(ws, tmp_path)
        data = json.loads(path.read_text())
        assert data["name"] == ws.name

    def test_creates_output_dir_if_missing(self, ws, tmp_path):
        nested = tmp_path / "a" / "b"
        path = write_overlay(ws, nested)
        assert path.exists()

    def test_config_passed_through(self, ws, tmp_path):
        config = OverlayConfig(tailscale_hostname="custom")
        path = write_overlay(ws, tmp_path, config)
        data = json.loads(path.read_text())
        assert data["containerEnv"]["TAILSCALE_HOSTNAME"] == "custom"


class TestProjectYamlToOverlay:
    """Integration: project YAML on disk -> OverlayConfig -> overlay output."""

    def test_project_yaml_produces_expected_overlay(self, ws, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "app.yaml").write_text(
            "tailscale_hostname: app-ts\n"
            "tailscale_serve_port: 9090\n"
            "remote_control_name: App RC\n"
            "firewall_extra_domains:\n"
            "  - api.stripe.com\n"
            "firewall_ipv6_hosts:\n"
            "  - '[::1]:4000'\n"
        )
        proj_cfg = load_project_config("app", base_dir=projects_dir)
        assert proj_cfg is not None

        overlay_config = OverlayConfig(
            tailscale_hostname=proj_cfg.tailscale_hostname or "",
            tailscale_serve_port=proj_cfg.tailscale_serve_port or 3000,
            remote_control_name=proj_cfg.remote_control_name or "",
            firewall_extra_domains=proj_cfg.firewall_extra_domains,
            firewall_ipv6_hosts=proj_cfg.firewall_ipv6_hosts,
        )
        overlay = generate_overlay(ws, overlay_config)
        env = overlay["containerEnv"]
        assert env["TAILSCALE_HOSTNAME"] == "app-ts"
        assert env["TAILSCALE_SERVE_PORT"] == "9090"
        assert env["REMOTE_CONTROL_NAME"] == "App RC"
        assert env["FIREWALL_EXTRA_DOMAINS"] == "api.stripe.com"
        assert env["FIREWALL_IPV6_HOSTS"] == "[::1]:4000"
