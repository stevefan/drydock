"""Slice 1a daemon skeleton tests. Each justified per CLAUDE.md §Tests
must justify their existence."""

from __future__ import annotations

import json
import socket


# Contract: the daemon must appear at its socket and respond to a valid
# request. Fails if socket setup breaks, if the handler doesn't parse,
# or if the response shape drifts.
def test_daemon_binds_socket_and_echoes(wsd):
    response = wsd.call({"hello": "world"})
    assert response == {"echo": {"hello": "world"}}


# Contract: malformed input must produce a structured `parse_error`,
# not a crash, not an empty response, not a socket close. This is the
# error-surface contract the V2 RPC design pins (v2-design-protocol.md §8).
def test_daemon_returns_parse_error_on_bad_json(wsd):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(str(wsd.socket_path))
    try:
        s.sendall(b"this is not json\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    response = json.loads(buf.decode("utf-8").strip())
    assert response["error"] == "parse_error"
    assert "message" in response
