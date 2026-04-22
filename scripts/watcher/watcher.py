#!/usr/bin/env python3
"""Drydock deskwatch watcher — periodic Harbor-side observer.

Runs `ws --json deskwatch`, diffs against prior state, and appends a
short markdown note to a sink file on state transitions (desk went
unhealthy → healthy or vice versa, or the set of violations changed).

This is the minimal "alerting employee" — it closes the observability
loop without committing to a push channel. Point the sink at whatever
you actually read:

    WATCHER_SINK=/path/to/obsidian/vault/deskwatch-alerts.md watcher.py
    WATCHER_SINK=/workspace/data/alerts.md watcher.py           # inside a desk
    WATCHER_SINK=/tmp/drydock-alerts.md watcher.py              # local tail

Sink writes are append-only markdown. The script deliberately does
NOT own: alert suppression/de-dup logic beyond "state unchanged",
threshold policy, or routing. Those belong to whatever reads the sink.

Exit 0 on success (regardless of desk health). Non-zero only on
internal failure (couldn't invoke ws, couldn't write sink).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


STATE_FILE = Path(
    os.environ.get("WATCHER_STATE",
                   str(Path.home() / ".drydock" / "watcher-state.json")),
)
SINK = Path(os.environ.get("WATCHER_SINK", "/tmp/drydock-alerts.md"))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_deskwatch() -> dict:
    """Invoke `ws --json deskwatch` and return the parsed payload.

    Raises RuntimeError if ws isn't on PATH or returns unparseable
    output.
    """
    try:
        # exit 1 from deskwatch means "unhealthy" — that's expected, not
        # a failure of this script. Capture rc but don't treat nonzero
        # as fatal unless stdout is empty / unparseable.
        result = subprocess.run(
            ["ws", "--json", "deskwatch"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to invoke ws: {exc}") from exc
    if not result.stdout.strip():
        raise RuntimeError(
            f"ws returned empty stdout (rc={result.returncode}, stderr={result.stderr!r})"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ws output was not JSON: {exc}\nstdout: {result.stdout[:300]}")


def summarize(payload: dict) -> dict[str, dict]:
    """Flatten `ws --json deskwatch` output into {desk_name: {healthy,
    violations, check_keys}} — a minimal shape we can diff against the
    prior tick.

    `check_keys` is a sorted tuple of "<kind>:<name>:<healthy>"
    strings. That way we detect both "new violation appeared" and
    "existing violation resolved" without capturing every field.
    """
    desks = payload.get("desks") if "desks" in payload else [payload]
    state: dict[str, dict] = {}
    for d in desks:
        name = d.get("desk") or "unknown"
        checks = d.get("checks") or []
        check_keys = sorted(
            f"{c['kind']}:{c['name']}:{'ok' if c['healthy'] else 'bad'}"
            for c in checks
        )
        state[name] = {
            "healthy": bool(d.get("healthy")),
            "violations": int(d.get("violations", 0)),
            "check_keys": list(check_keys),
            "note": d.get("note"),  # e.g. "no deskwatch: block declared"
        }
    return state


def load_prior_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def detect_transitions(prior: dict, current: dict) -> list[str]:
    """Return markdown-formatted transition notes. Empty list means no
    change — sink isn't touched.
    """
    notes: list[str] = []
    all_desks = sorted(set(prior) | set(current))
    for name in all_desks:
        was = prior.get(name)
        now = current.get(name)
        if now is None:
            notes.append(f"- `{name}` disappeared from `ws list`")
            continue
        if was is None:
            if now["healthy"]:
                notes.append(
                    f"- `{name}` appeared and is healthy "
                    f"({len(now['check_keys'])} check{'s' if len(now['check_keys']) != 1 else ''})"
                )
            else:
                notes.append(
                    f"- `{name}` appeared UNHEALTHY "
                    f"({now['violations']} violation{'s' if now['violations'] != 1 else ''})"
                )
            continue
        if was["healthy"] != now["healthy"]:
            direction = "recovered" if now["healthy"] else "went UNHEALTHY"
            notes.append(f"- `{name}` {direction} ({now['violations']} violation(s))")
            continue
        if was["check_keys"] != now["check_keys"]:
            added = set(now["check_keys"]) - set(was["check_keys"])
            removed = set(was["check_keys"]) - set(now["check_keys"])
            parts = []
            if added:
                parts.append(f"+{len(added)} check(s)")
            if removed:
                parts.append(f"-{len(removed)} check(s)")
            notes.append(f"- `{name}` check set changed: {', '.join(parts)}")
    return notes


def append_sink(notes: list[str], current: dict[str, dict]) -> None:
    SINK.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## {_utcnow()}  deskwatch transitions",
        "",
        *notes,
        "",
        "Current state:",
        "",
    ]
    for name in sorted(current):
        st = current[name]
        if st.get("note"):
            lines.append(f"- `{name}`: {st['note']}")
        else:
            mark = "healthy" if st["healthy"] else f"UNHEALTHY ({st['violations']} violations)"
            lines.append(f"- `{name}`: {mark}")
    lines.append("")
    lines.append("---")
    lines.append("")
    with SINK.open("a") as f:
        f.write("\n".join(lines))


def main() -> int:
    try:
        payload = run_deskwatch()
    except RuntimeError as exc:
        print(f"watcher: {exc}", file=sys.stderr)
        return 2

    current = summarize(payload)
    prior = load_prior_state()
    notes = detect_transitions(prior, current)

    if notes:
        try:
            append_sink(notes, current)
        except OSError as exc:
            print(f"watcher: sink write failed: {exc}", file=sys.stderr)
            return 3
        print(f"watcher: {len(notes)} transition(s) → {SINK}")
    else:
        # Quiet by design — no transitions means nothing to say.
        pass

    save_state(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
