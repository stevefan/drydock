"""Tailscale admin API client.

Self-contained per v2-design-tailnet-identity.md §8. The v1.x CLI calls these
helpers from `ws destroy` and `ws tailnet prune`; the v2 daemon will call the
same functions without modification.

The admin API token is daemon-internal infrastructure (not a desk-facing
capability — see §3 of the design). It's stored at
`~/.drydock/daemon-secrets/tailscale_admin_token` (0400); tailnet name at
`.../tailscale_tailnet`. The v2-spec'd path is used now so the eventual
daemon picks up the same files with no migration.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import WsError

logger = logging.getLogger(__name__)

API_BASE = "https://api.tailscale.com/api/v2"
DAEMON_SECRETS_DIR = Path.home() / ".drydock" / "daemon-secrets"
TOKEN_PATH = DAEMON_SECRETS_DIR / "tailscale_admin_token"
TAILNET_PATH = DAEMON_SECRETS_DIR / "tailscale_tailnet"

# Tailscale normalises device hostnames to DNS labels: lowercase, digits,
# hyphens. Drydock's default identity is `{ws.name}-{short_id}` (see
# overlay._default_identity). The pattern matches that plus any project-set
# tailscale_hostname that uses the same shape. Used by `ws tailnet prune`
# to distinguish drydock-created devices from manually-joined nodes.
DRYDOCK_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

_FIX_GENERATE_TOKEN = (
    "Generate a Tailscale API token at login.tailscale.com -> Settings -> Keys "
    "-> Generate API access token, then write to "
    "~/.drydock/daemon-secrets/tailscale_admin_token (0400) and the tailnet "
    "name to ~/.drydock/daemon-secrets/tailscale_tailnet"
)


def load_admin_credentials() -> tuple[str, str] | None:
    """Return (api_token, tailnet) if both files exist; else None.

    Absence is non-fatal per §4 of the design. Callers decide whether to warn
    (destroy path: skip silently) or error (`ws tailnet prune`: refuse).
    """
    if not TOKEN_PATH.exists() or not TAILNET_PATH.exists():
        return None
    token = TOKEN_PATH.read_text().strip()
    tailnet = TAILNET_PATH.read_text().strip()
    if not token or not tailnet:
        return None
    return token, tailnet


def _request(method: str, url: str, api_token: str) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {api_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except urllib.error.URLError as e:
        raise WsError(
            f"Tailscale API request failed: {e.reason}",
            fix="Check network connectivity to api.tailscale.com",
        ) from e


def delete_tailnet_device(device_id: str, api_token: str) -> None:
    """DELETE a device record from the tailnet admin plane.

    404 is treated as success (record already gone). 401/403 raises WsError
    with a `fix:` pointing at token rotation. Other non-2xx raises with the
    status and response body excerpt.
    """
    url = f"{API_BASE}/device/{urllib.parse.quote(device_id, safe='')}"
    status, body = _request("DELETE", url, api_token)
    if 200 <= status < 300:
        return
    if status == 404:
        logger.warning("Tailscale device %s already absent (404); treating as success", device_id)
        return
    if status in (401, 403):
        raise WsError(
            f"Tailscale API rejected DELETE for device {device_id} (HTTP {status})",
            fix=(
                "The admin token is missing, expired, or lacks `devices` scope. "
                + _FIX_GENERATE_TOKEN
            ),
        )
    raise WsError(
        f"Tailscale API DELETE failed for device {device_id}: HTTP {status}",
        fix="Inspect the response; retry or delete the device via the Tailscale admin UI",
        context={"body_excerpt": body[:200].decode("utf-8", errors="replace")},
    )


def find_devices(tailnet: str, api_token: str) -> list[dict]:
    """GET the device list for a tailnet. Returns the `devices` array."""
    url = f"{API_BASE}/tailnet/{urllib.parse.quote(tailnet, safe='')}/devices"
    status, body = _request("GET", url, api_token)
    if not (200 <= status < 300):
        raise WsError(
            f"Tailscale API GET devices failed: HTTP {status}",
            fix=(
                "Verify the tailnet name and that the token has `devices` scope. "
                + _FIX_GENERATE_TOKEN
            ),
            context={"body_excerpt": body[:200].decode("utf-8", errors="replace")},
        )
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise WsError(
            f"Tailscale API returned non-JSON response: {e}",
            fix="Retry; if persistent, check api.tailscale.com status",
        ) from e
    return data.get("devices", [])


def find_device_by_hostname(hostname: str, devices: list[dict]) -> dict | None:
    """Return the first device whose `hostname` matches, or None."""
    for dev in devices:
        if dev.get("hostname") == hostname:
            return dev
    return None
