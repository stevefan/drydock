"""Tests for `ws tailnet` commands."""

import json
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.tailnet import tailnet
from drydock.core.runtime import Drydock
from drydock.output.formatter import Output


def _invoke(args, registry):
    runner = CliRunner()
    out = Output(force_json=True)
    return runner.invoke(
        tailnet,
        args,
        obj={"registry": registry, "output": out, "dry_run": False},
    )


class TestPruneDryRun:
    # Contract: given devices on the tailnet that don't match any live
    # drydock, dry-run lists them as candidates without calling delete.
    # This is the core of the prune UX — the operator must see what WOULD
    # happen before --apply.
    def test_lists_orphans_no_live_desks(self, registry):
        fake_devices = [
            {"id": "dev-ghost", "hostname": "auction-crawl", "lastSeen": "2026-04-01T00:00:00Z"},
            {"id": "dev-manual", "hostname": "MyLaptop", "lastSeen": "2026-04-14T00:00:00Z"},
        ]
        with patch("drydock.cli.tailnet.tailnet_api") as mock_tn:
            # Pass-through the pattern constant used by _classify_candidates.
            from drydock.core import tailnet as real_tn
            mock_tn.DRYDOCK_HOSTNAME_PATTERN = real_tn.DRYDOCK_HOSTNAME_PATTERN
            mock_tn.load_admin_credentials.return_value = ("tok", "example.ts.net")
            mock_tn.find_devices.return_value = fake_devices

            result = _invoke(["prune"], registry)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["deleted"] == []
        # auction-crawl matches the drydock hostname pattern and has no live
        # desk; MyLaptop has uppercase so it doesn't match the pattern
        # (drydock-style hostnames are DNS-label shaped).
        candidate_hostnames = {c["hostname"] for c in data["candidates"]}
        assert "auction-crawl" in candidate_hostnames
        assert "MyLaptop" not in candidate_hostnames
        mock_tn.delete_tailnet_device.assert_not_called()

    # Contract: a device whose hostname matches a live drydock is NOT
    # a candidate. The match considers ws.id, ws.name, and the explicit
    # config tailscale_hostname. Skipping any of these risks deleting a
    # live desk's record.
    def test_skips_devices_matching_live_drydock(self, registry):
        registry.create_drydock(
            Drydock(name="auction-crawl", project="p", repo_path="/r")
        )
        fake_devices = [
            {"id": "dev-live", "hostname": "auction-crawl", "lastSeen": "now"},
        ]
        with patch("drydock.cli.tailnet.tailnet_api") as mock_tn:
            from drydock.core import tailnet as real_tn
            mock_tn.DRYDOCK_HOSTNAME_PATTERN = real_tn.DRYDOCK_HOSTNAME_PATTERN
            mock_tn.load_admin_credentials.return_value = ("tok", "example.ts.net")
            mock_tn.find_devices.return_value = fake_devices

            result = _invoke(["prune"], registry)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["candidates"] == []

    # Contract: absence of credentials is a hard error with a fix:
    # pointing the operator at the token-generation steps. This is
    # different from destroy's silent skip — prune is an explicit admin
    # action so silent no-op would be confusing.
    def test_missing_credentials_errors_with_fix(self, registry):
        with patch("drydock.cli.tailnet.tailnet_api") as mock_tn:
            mock_tn.load_admin_credentials.return_value = None

            result = _invoke(["prune"], registry)

        assert result.exit_code == 1
        err = json.loads(result.output)
        assert "fix" in err
        assert "login.tailscale.com" in err["fix"]
