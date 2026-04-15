"""wsd Unix-socket server — Slice 1a stub dispatcher.

Binds a Unix stream socket at the configured path, accepts connections
(threaded, one per client), reads a single newline-delimited JSON line per
connection, and returns `{"echo": <payload>}` as the response. Malformed
JSON returns a structured `{"error": "parse_error", "message": ...}`.

Slice 1b will replace the echo handler with a JSON-RPC 2.0 dispatcher
and introduce method routing. The socket lifecycle + threading model
stays.
"""

from __future__ import annotations

import json
import logging
import socketserver
from pathlib import Path

logger = logging.getLogger(__name__)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            response: dict = {"error": "parse_error", "message": str(exc)}
        else:
            response = {"echo": payload}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(socket_path: Path) -> None:
    """Bind the Unix socket and serve until interrupted.

    Removes a stale socket file if one exists at `socket_path`. Creates
    parent directories if missing. Cleans up the socket file on exit.
    """
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    with _Server(str(socket_path), _Handler) as server:
        logger.info("wsd: listening on %s", socket_path)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("wsd: interrupted, shutting down")
        finally:
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
