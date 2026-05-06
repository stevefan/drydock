"""Drydock drydock daemon (daemon) — V2.

V2's sole control plane for desk lifecycle operations. V1 host-mode CLI
keeps working unchanged; the daemon is opt-in. See docs/v2-scope.md and
docs/v2-design-{overview,protocol,state}.md for the full design.

Slice 1a (this commit): daemon skeleton — entrypoint, Unix-socket bind,
newline-delimited JSON stub dispatcher (echo only). Slice 1b swaps the
stub for JSON-RPC 2.0; 1c adds CreateDesk + task_log; 1d adds crash
recovery.
"""
