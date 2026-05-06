"""Port Auditor — observation + judgment + bounded-defensive-action role.

Phase PA0 (this module's scope): pure deterministic measurement layer.
Polls cgroup stats via `docker stats`, reads broker lease ledger,
scans audit log; produces structured snapshots; writes them as JSON
files; exposes CLI for ad-hoc queries.

NO LLM in PA0. The LLM layers (PA1 watch loop, PA2 deep analysis) build
on top of these snapshots — but PA0 is useful in isolation: the principal
can run `ws auditor snapshot` to capture the Harbor's state, `ws auditor
metrics <dock>` to query recent measurements, etc.

See docs/design/port-auditor.md for the full architecture.
"""
