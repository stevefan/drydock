"""Tests for devcontainer override generator."""

import json
from pathlib import Path

import pytest

from drydock.core import WsError
from drydock.core.overlay import (
    OverlayConfig,
    generate_overlay,
    merge_into_base,
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

    def test_tailscale_hostname_defaults_to_name_with_short_id(self, ws):
        hostname = generate_overlay(ws)["containerEnv"]["TAILSCALE_HOSTNAME"]
        assert hostname.startswith("payments-refactor-")
        suffix = hostname.removeprefix("payments-refactor-")
        assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)

    def test_tailscale_hostname_is_deterministic(self, ws):
        first = generate_overlay(ws)["containerEnv"]["TAILSCALE_HOSTNAME"]
        second = generate_overlay(ws)["containerEnv"]["TAILSCALE_HOSTNAME"]
        assert first == second

    def test_tailscale_hostname_differs_across_workspaces(self):
        ws_a = Workspace(name="app-foo", project="proj", repo_path="/x", branch="b")
        ws_b = Workspace(name="app-bar", project="proj", repo_path="/x", branch="b")
        a = generate_overlay(ws_a)["containerEnv"]["TAILSCALE_HOSTNAME"]
        b = generate_overlay(ws_b)["containerEnv"]["TAILSCALE_HOSTNAME"]
        assert a != b

    def test_tailscale_hostname_override(self, ws):
        config = OverlayConfig(tailscale_hostname="my-custom-host")
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["TAILSCALE_HOSTNAME"] == "my-custom-host"

    def test_tailscale_authkey_included_when_set(self, ws):
        config = OverlayConfig(tailscale_authkey="tskey-auth-abc123")
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["TAILSCALE_AUTHKEY"] == "tskey-auth-abc123"

    def test_tailscale_serve_port_custom(self, ws):
        config = OverlayConfig(tailscale_serve_port=8080)
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["TAILSCALE_SERVE_PORT"] == "8080"

    def test_remote_control_name_override(self, ws):
        config = OverlayConfig(remote_control_name="My Agent")
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["REMOTE_CONTROL_NAME"] == "My Agent"

    def test_firewall_extra_domains(self, ws):
        config = OverlayConfig(firewall_extra_domains=["example.com", "api.stripe.com"])
        overlay = generate_overlay(ws, config)
        assert overlay["containerEnv"]["FIREWALL_EXTRA_DOMAINS"] == "example.com api.stripe.com"

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

    def test_workspace_subdir_included_when_set(self):
        ws = Workspace(
            name="sub", project="mono", repo_path="/srv/code/mono",
            branch="ws/sub", workspace_subdir="apps/frontend",
        )
        overlay = generate_overlay(ws)
        assert overlay["containerEnv"]["DRYDOCK_WORKSPACE_SUBDIR"] == "apps/frontend"

    def test_secrets_mount_uses_workspace_scoped_path(self, ws):
        expected_host_dir = str(Path.home() / ".drydock" / "secrets")
        overlay = generate_overlay(ws)
        mounts = overlay["mounts"]
        assert any(f"{expected_host_dir}/{ws.id}" in m and "/run/secrets" in m for m in mounts)

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
        assert f"/opt/secrets/{ws.id}" in secrets_mount
        assert "target=/secrets" in secrets_mount

    def test_infra_mounts_count_and_order(self, ws):
        overlay = generate_overlay(ws)
        mounts = overlay["mounts"]
        assert len(mounts) == 8
        assert "/run/secrets" in mounts[0]
        assert "claude-code-config" in mounts[1]
        assert "claude-code-bashhistory" in mounts[2]
        assert "tailscale-state" in mounts[3]
        assert "drydock-vscode-server" in mounts[4]
        assert "drydock-npm-cache" in mounts[5]
        assert "drydock-pip-cache" in mounts[6]
        assert ".gitconfig" in mounts[7]

    def test_claude_profile_parameterizes_volume_name(self, ws):
        config = OverlayConfig(claude_profile="staging")
        overlay = generate_overlay(ws, config)
        m = [x for x in overlay["mounts"] if "/home/node/.claude" in x][0]
        assert "source=claude-code-config-staging" in m
        assert "type=volume" in m

    def test_extra_mounts_after_infra_mounts(self, ws):
        config = OverlayConfig(
            extra_mounts=["source=/data,target=/data,type=bind"]
        )
        overlay = generate_overlay(ws, config)
        mounts = overlay["mounts"]
        assert len(mounts) == 9
        assert mounts[-1] == "source=/data,target=/data,type=bind"

    def test_forward_ports_included_when_set(self, ws):
        config = OverlayConfig(forward_ports=[8000])
        overlay = generate_overlay(ws, config)
        assert overlay["forwardPorts"] == [8000]


class TestMergeIntoBase:
    def _write_base(self, tmp_path, content):
        base = tmp_path / ".devcontainer" / "devcontainer.json"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(content)
        return base

    def test_base_build_preserved_with_overlay_env_and_mounts(self, tmp_path):
        base_path = self._write_base(tmp_path, json.dumps({
            "build": {"dockerfile": "Dockerfile"},
            "containerEnv": {"EXISTING": "yes"},
        }))
        overlay = {
            "name": "ws1",
            "containerEnv": {"NEW_VAR": "val"},
            "mounts": ["source=/a,target=/b,type=bind"],
        }
        composite = merge_into_base(base_path, overlay)
        assert composite["build"] == {"dockerfile": "Dockerfile"}
        assert composite["containerEnv"]["EXISTING"] == "yes"
        assert composite["containerEnv"]["NEW_VAR"] == "val"
        assert composite["mounts"] == ["source=/a,target=/b,type=bind"]

    def test_jsonc_comments_parsed(self, tmp_path):
        jsonc = '{\n  // line comment\n  "image": "node:20", /* block */\n  "name": "base"\n}'
        base_path = self._write_base(tmp_path, jsonc)
        composite = merge_into_base(base_path, {"name": "overlay"})
        assert composite["image"] == "node:20"
        assert composite["name"] == "overlay"

    def test_overlay_container_env_overrides_base(self, tmp_path):
        base_path = self._write_base(tmp_path, json.dumps({
            "image": "node:20",
            "containerEnv": {"SHARED": "base_val", "BASE_ONLY": "keep"},
        }))
        overlay = {"containerEnv": {"SHARED": "overlay_val"}}
        composite = merge_into_base(base_path, overlay)
        assert composite["containerEnv"]["SHARED"] == "overlay_val"
        assert composite["containerEnv"]["BASE_ONLY"] == "keep"

    def test_missing_base_raises_ws_error(self, tmp_path):
        missing = tmp_path / "nope" / "devcontainer.json"
        with pytest.raises(WsError, match="not found"):
            merge_into_base(missing, {})

    def test_forward_ports_dedup(self, tmp_path):
        base_path = self._write_base(tmp_path, json.dumps({
            "image": "node:20",
            "forwardPorts": [3000, 8080],
        }))
        overlay = {"forwardPorts": [8080, 9090]}
        composite = merge_into_base(base_path, overlay)
        assert composite["forwardPorts"] == [3000, 8080, 9090]

    def test_mounts_concatenated(self, tmp_path):
        base_path = self._write_base(tmp_path, json.dumps({
            "image": "node:20",
            "mounts": ["source=/base,target=/base,type=bind"],
        }))
        overlay = {"mounts": ["source=/overlay,target=/overlay,type=bind"]}
        composite = merge_into_base(base_path, overlay)
        assert composite["mounts"] == [
            "source=/base,target=/base,type=bind",
            "source=/overlay,target=/overlay,type=bind",
        ]

    def test_mounts_dedup_by_target_overlay_wins(self, tmp_path):
        base_path = self._write_base(tmp_path, json.dumps({
            "image": "node:20",
            "mounts": [
                "source=old-vol,target=/shared,type=volume",
                "source=/keep,target=/keep,type=bind",
            ],
        }))
        overlay = {"mounts": ["source=new-vol,target=/shared,type=volume"]}
        composite = merge_into_base(base_path, overlay)
        targets = [m.split("target=")[1].split(",")[0] for m in composite["mounts"]]
        assert targets.count("/shared") == 1
        shared = [m for m in composite["mounts"] if "target=/shared" in m][0]
        assert "source=new-vol" in shared
        assert "source=/keep,target=/keep,type=bind" in composite["mounts"]


class TestWriteOverlay:
    def _make_base(self, tmp_path):
        base = tmp_path / "base" / ".devcontainer" / "devcontainer.json"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps({"build": {"dockerfile": "Dockerfile"}}))
        return base

    def test_writes_file_to_output_dir(self, ws, tmp_path):
        base = self._make_base(tmp_path)
        out = tmp_path / "out"
        path = write_overlay(ws, out, base_devcontainer_path=base)
        assert path.exists()
        assert path.name == f"{ws.id}.devcontainer.json"

    def test_written_file_is_valid_json_with_base_merged(self, ws, tmp_path):
        base = self._make_base(tmp_path)
        out = tmp_path / "out"
        path = write_overlay(ws, out, base_devcontainer_path=base)
        data = json.loads(path.read_text())
        assert data["name"] == ws.name
        assert data["build"] == {"dockerfile": "Dockerfile"}

    def test_config_passed_through(self, ws, tmp_path):
        base = self._make_base(tmp_path)
        out = tmp_path / "out"
        config = OverlayConfig(tailscale_hostname="custom")
        path = write_overlay(ws, out, config, base_devcontainer_path=base)
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

    def test_project_yaml_extra_mounts_in_overlay(self, ws, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "app.yaml").write_text(
            "extra_mounts:\n"
            "  - 'source=/data,target=/data,type=bind'\n"
        )
        proj_cfg = load_project_config("app", base_dir=projects_dir)
        assert proj_cfg is not None
        overlay_config = OverlayConfig(extra_mounts=proj_cfg.extra_mounts)
        overlay = generate_overlay(ws, overlay_config)
        assert "source=/data,target=/data,type=bind" in overlay["mounts"]

    def test_project_yaml_claude_profile_in_overlay(self, ws, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "app.yaml").write_text("claude_profile: prod\n")
        proj_cfg = load_project_config("app", base_dir=projects_dir)
        assert proj_cfg is not None
        overlay_config = OverlayConfig(claude_profile=proj_cfg.claude_profile or "")
        overlay = generate_overlay(ws, overlay_config)
        m = [x for x in overlay["mounts"] if "/home/node/.claude" in x][0]
        assert "source=claude-code-config-prod" in m

    def test_project_yaml_forward_ports_in_overlay(self, ws, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "app.yaml").write_text(
            "forward_ports:\n  - 8000\n  - 3000\n"
        )
        proj_cfg = load_project_config("app", base_dir=projects_dir)
        assert proj_cfg is not None

        overlay_config = OverlayConfig(forward_ports=proj_cfg.forward_ports)
        overlay = generate_overlay(ws, overlay_config)
        assert overlay["forwardPorts"] == [8000, 3000]
