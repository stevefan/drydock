"""Tests for the Tailscale admin API client."""

from unittest.mock import patch

import pytest

from drydock.core import WsError
from drydock.core import tailnet


class TestDeleteTailnetDevice:
    # Contract: 404 means the device record is already gone — the caller's
    # goal is met. Raising here would force every caller to special-case it
    # and would make destroy.py's best-effort cleanup noisy for benign state.
    def test_404_is_success(self):
        with patch.object(tailnet, "_request", return_value=(404, b'{"error":"not found"}')):
            # Does not raise.
            tailnet.delete_tailnet_device("dev123", "tok")

    # Contract: 401/403 raises WsError with a `fix:` pointing at token
    # rotation. This is the most common failure mode operators hit (expired
    # or wrong-scope token); the fix text is a stable contract.
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failure_raises_with_rotation_fix(self, status):
        with patch.object(tailnet, "_request", return_value=(status, b"")):
            with pytest.raises(WsError) as exc:
                tailnet.delete_tailnet_device("dev123", "bad-token")
        assert exc.value.fix is not None
        # The fix text must tell the operator how to generate a new token.
        assert "login.tailscale.com" in exc.value.fix
        assert "devices" in exc.value.fix

    # Contract: other non-2xx raises WsError; status code appears in the
    # message so the operator can correlate with the Tailscale API docs.
    def test_other_error_raises_with_status(self):
        with patch.object(tailnet, "_request", return_value=(500, b"server error")):
            with pytest.raises(WsError) as exc:
                tailnet.delete_tailnet_device("dev123", "tok")
        assert "500" in exc.value.message
