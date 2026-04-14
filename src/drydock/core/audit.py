"""Append-only audit log for workspace lifecycle events."""

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG_PATH = Path.home() / ".drydock" / "audit.log"


def log_event(
    event: str,
    workspace_id: str,
    extra: dict | None = None,
    *,
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "workspace_id": workspace_id,
    }
    if extra:
        entry.update(extra)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
