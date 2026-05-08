"""Smokescreen ACL generation from drydock's network_reach policy.

Phase 2a.1 of make-the-harness-live.md. Translates drydock's per-desk
``delegatable_network_reach`` (a list of domain globs like
``"*.github.com"`` or ``"api.anthropic.com"``) into the YAML format
smokescreen reads as its egress allowlist.

Smokescreen's ACL distinguishes:

- ``allowed_hosts`` — exact hostname match. ``"api.anthropic.com"`` here
  matches only that exact name.
- ``allowed_domains`` — domain + all subdomains. ``"github.com"`` here
  matches ``github.com``, ``api.github.com``, ``raw.github.com``, etc.

Drydock's wildcard convention (``"*.github.com"``) maps to
smokescreen's ``allowed_domains`` (strip the leading wildcard).
Bare hostnames map to ``allowed_hosts``.

Per-desk file path on Harbor: ``~/.drydock/proxy/<drydock_id>.yaml``.
Bind-mounted into the container at ``/run/drydock/proxy/allowlist.yaml``
(read-only). The daemon is the only writer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import json
import os
import yaml


# Default smokescreen project tag for events the proxy emits to its
# audit log. Per-drydock ID lets the Auditor's prompt distinguish.
def _project_tag(drydock_id: str) -> str:
    return f"drydock-{drydock_id}"


def split_globs(network_reach: Iterable[str]) -> tuple[list[str], list[str]]:
    """Partition glob entries into (allowed_hosts, allowed_domains).

    Rules:
    - "*.foo.com" → allowed_domains: "foo.com" (smokescreen treats domain
      entries as suffix-matching, so "foo.com" matches both itself and
      any subdomain).
    - "foo.com" → allowed_hosts: "foo.com" (exact only).
    - "*" alone → both lists get a permissive entry; smokescreen has no
      "match anything" so we represent this as an empty pattern with a
      broad allowed_domains. Practically a wildcard is rare and probably
      a misconfiguration; we still produce a valid file.
    - Empty/whitespace entries are skipped silently.
    - Order is preserved within each list; deduplication is applied.

    Returns: (sorted hosts, sorted domains).
    """
    hosts: list[str] = []
    domains: list[str] = []
    for raw in network_reach:
        if not raw:
            continue
        entry = raw.strip()
        if not entry:
            continue
        if entry == "*":
            # Wildcard-everything; smokescreen has no native "any" — best
            # we can do is leave allowlist effectively-empty AND let the
            # default allow-action handle it. We treat this as a bare
            # host marker; caller can detect and switch action mode.
            # In practice we don't expect this path; document for safety.
            continue
        if entry.startswith("*."):
            domain = entry[2:]
            if domain and domain not in domains:
                domains.append(domain)
        else:
            if entry not in hosts:
                hosts.append(entry)
    return sorted(hosts), sorted(domains)


def generate_smokescreen_acl(
    drydock_id: str,
    network_reach: Iterable[str],
) -> dict:
    """Build the smokescreen v1 ACL document for one drydock.

    Returns a dict ready for YAML/JSON serialization. Format follows
    smokescreen's documented v1 schema.

    Default action is 'enforce' — anything not in the allowlist is
    denied. SSRF protections (RFC1918, link-local) come from
    smokescreen's built-in deny rules; we don't need to specify them.
    """
    hosts, domains = split_globs(network_reach)
    # `allow_missing_role: true` lets the proxy use the default rule
    # for non-mTLS clients (which is everyone in our deployment —
    # we don't issue client certs for individual processes inside a
    # drydock). Without this, smokescreen returns 407 with
    # "defaultRoleFromRequest requires TLS" for every CONNECT.
    return {
        "version": "v1",
        "services": [],
        "allow_missing_role": True,
        "default": {
            "project": _project_tag(drydock_id),
            "action": "enforce",
            "allowed_hosts": hosts,
            "allowed_domains": domains,
        },
    }


def write_smokescreen_acl(
    drydock_id: str,
    network_reach: Iterable[str],
    proxy_root: Path,
) -> Path:
    """Atomically write the smokescreen ACL for ``drydock_id`` to disk.

    proxy_root is typically ``~/.drydock/proxy/`` (configurable for tests).
    File path: ``<proxy_root>/<drydock_id>.yaml``. Mode 0644 — readable
    by the container's UID via the bind mount.

    Atomic via tempfile + rename, mirroring the secret-write pattern in
    secrets.py. Returns the final path.
    """
    proxy_root = Path(proxy_root)
    proxy_root.mkdir(parents=True, exist_ok=True)
    target = proxy_root / f"{drydock_id}.yaml"
    acl = generate_smokescreen_acl(drydock_id, network_reach)

    # Write to tempfile in same dir for atomic rename.
    tmp = proxy_root / f".{drydock_id}.yaml.tmp"
    with open(tmp, "w") as f:
        # smokescreen accepts both YAML and JSON; we emit YAML for
        # readability when the principal greps the file.
        yaml.safe_dump(acl, f, default_flow_style=False, sort_keys=False)
    os.chmod(tmp, 0o644)
    tmp.replace(target)
    return target


def proxy_root_from_home(home: Path | None = None) -> Path:
    """Default proxy-config dir on the Harbor.

    Defaults to ``~/.drydock/proxy/`` if home is None. Test fixtures
    override.
    """
    base = home or Path.home()
    return base / ".drydock" / "proxy"
