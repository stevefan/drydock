"""Capability-lease data model per docs/v2-design-capability-broker.md §2.

V2 implements one capability type (`SECRET`); the rest are enum-reserved
so the daemon can reject them with `capability_unsupported` without
needing per-type code today.

`scope` is an unversioned dict by design (capability-broker.md §2 + §8):
treat as append-only per type — never rename keys, never narrow value
types. The first V4 type (storage mount, compute quota, network reach)
defines the formal versioning model when it ships.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class CapabilityType(str, enum.Enum):
    SECRET = "SECRET"
    # V4-reserved. Daemon rejects these with `capability_unsupported`
    # until the corresponding scope schema and broker logic land.
    STORAGE_MOUNT = "STORAGE_MOUNT"
    COMPUTE_QUOTA = "COMPUTE_QUOTA"
    NETWORK_REACH = "NETWORK_REACH"


@dataclass(frozen=True)
class CapabilityLease:
    """V2 capability-lease record.

    Persisted in `leases` SQLite table; mapped 1:1 with row columns.
    Mutability after issuance is limited to revocation (revoked + reason).
    """

    lease_id: str
    desk_id: str
    type: CapabilityType
    scope: dict
    issued_at: datetime
    expiry: datetime | None
    issuer: str
    revoked: bool = False
    revocation_reason: str | None = None

    def to_wire(self) -> dict:
        """Serialize for JSON-RPC response (matches docs/v2-design-protocol.md)."""
        return {
            "lease_id": self.lease_id,
            "desk_id": self.desk_id,
            "type": self.type.value,
            "scope": dict(self.scope),
            "issued_at": self.issued_at.isoformat(),
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "issuer": self.issuer,
            "revoked": self.revoked,
            "revocation_reason": self.revocation_reason,
        }
