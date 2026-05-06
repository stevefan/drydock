"""Slice 1b daemon JSON-RPC tests. Each justified per CLAUDE.md §Tests
must justify their existence."""

from __future__ import annotations

import json
import socket


# Contract: the daemon must expose the pinned health method over the
# JSON-RPC response envelope. Fails if method dispatch, response shape,
# or runtime metadata wiring drifts.
def test_wsd_health_returns_ok(daemon):
    response = daemon.call_rpc("daemon.health")
    result = response["result"]
    assert result["ok"] is True
    assert isinstance(result["pid"], int) and result["pid"] > 0
    assert result["version"] == "v2-slice1b"


# Contract: malformed input must produce the JSON-RPC parse-error shape,
# not a crash, not an empty response, not an unstructured payload. This
# is the RPC error-surface contract the V2 design pins.
def test_daemon_returns_parse_error_on_bad_json(daemon):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(str(daemon.socket_path))
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
    assert response["id"] is None
    assert response["error"]["code"] == -32700
    assert "message" in response["error"]


# Contract: unknown methods must fail with method-not-found, otherwise
# clients cannot reliably distinguish typoed calls from transport issues.
def test_wsd_method_not_found_returns_32601(daemon):
    response = daemon.call_rpc("daemon.nope")
    assert response["error"]["code"] == -32601
    assert "daemon.nope" in response["error"]["message"]


# Contract: malformed envelopes missing required JSON-RPC fields must
# fail as invalid requests instead of reaching handler dispatch.
def test_wsd_invalid_request_missing_method(daemon):
    response = daemon.call({"jsonrpc": "2.0", "id": 1})
    assert response["error"]["code"] == -32600


# Contract: notifications are valid JSON-RPC but produce no response.
# Fails if a future refactor accidentally writes success/error envelopes
# for id-less requests and breaks fire-and-forget callers.
def test_wsd_notification_produces_no_response(daemon):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    s.connect(str(daemon.socket_path))
    try:
        s.sendall(b'{"jsonrpc":"2.0","method":"daemon.health"}\n')
        try:
            buf = s.recv(4096)
        except socket.timeout:
            buf = b""
    finally:
        s.close()
    assert buf == b""
