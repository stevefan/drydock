"""`python -m drydock.wsd` entrypoint.

Parses CLI flags, configures logging to stderr, installs signal handlers,
and hands off to `server.serve`. Designed to run under launchd (macOS)
or systemd user unit (Linux); those supervisors call the same command.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from drydock.wsd.server import serve


def _install_signal_handlers() -> None:
    def _shutdown(signum: int, frame) -> None:  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="drydock.wsd",
        description="Drydock workspace daemon (V2).",
    )
    parser.add_argument("--socket", required=True, help="Unix socket path to bind")
    parser.add_argument(
        "--registry",
        help="Registry DB path (reserved for Slice 1b+; unused in 1a).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _install_signal_handlers()
    serve(
        Path(args.socket),
        Path(args.registry) if args.registry else None,
        _env_truthy(os.environ.get("DRYDOCK_WSD_DRY_RUN")),
    )
    return 0


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    sys.exit(main())
