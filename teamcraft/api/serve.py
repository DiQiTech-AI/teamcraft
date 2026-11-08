"""Uvicorn entrypoint for the TeamCraft API (M3-01).

CLI defaults are the safe ones: bind ``127.0.0.1`` on port 8765, persist the
session token under the project ``.teamcraft`` runtime directory, and refuse a
non-loopback bind unless the operator passes ``--allow-non-loopback`` (PLAN
§9.2-1 — external exposure must be an explicit, deliberate act).

``--print-token`` and ``--rotate-token`` exist for the local bootstrap UX
(acceptance risk: "token save/rotation experience"): the token is printed once,
on stdout, for the operator to hand to the CLI/UI; ``--rotate-token``
invalidates the previous token before printing the replacement.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from ..errors import TeamCraftError
from .app import create_app
from .config import DEFAULT_HOST, DEFAULT_PORT, is_loopback_host, load_api_config
from .security import SessionTokenStore

logger = logging.getLogger("teamcraft.api.serve")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teamcraft-api",
        description="Run the local TeamCraft API server (loopback by default).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"bind address (default: {DEFAULT_HOST}; non-loopback needs "
        "--allow-non-loopback)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"bind port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="acknowledge that binding outside loopback exposes this API to "
        "the network; authentication remains mandatory",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="project root owning the .teamcraft runtime directory",
    )
    parser.add_argument(
        "--static-root",
        default=None,
        help="directory served, path-confinement applied, under /static",
    )
    parser.add_argument(
        "--origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="extra allowed Origin for writes (repeatable, e.g. http://localhost:8765)",
    )
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="issue a fresh session token (invalidating the previous one) at startup",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print the active session token once to stdout after startup prep",
    )
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="enable uvicorn access logging (headers, and therefore tokens, "
        "are never logged)",
    )
    return parser


def make_server(
    argv: Sequence[str] | None = None,
) -> tuple[object, SessionTokenStore, argparse.Namespace]:
    """Parse CLI args into a (uvicorn.Server, token store, args) triple.

    Side effects: creates the token file (owner-only).  Does not bind sockets;
    :func:`main` starts serving.
    """

    import uvicorn

    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    config = load_api_config(
        project_root=args.project_root,
        host=args.host,
        port=args.port,
        allow_non_loopback=args.allow_non_loopback,
        extra_origins=tuple(args.origin),
        static_root=args.static_root,
    )

    store = SessionTokenStore(config.token_file)
    if args.rotate_token:
        store.rotate()
    else:
        store.ensure()

    app = create_app(token_store=store, config=config, static_root=config.static_root)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            access_log=bool(args.access_log),
            log_config=None,
            env_file=None,
        )
    )
    return server, store, args


def main(argv: Sequence[str] | None = None) -> int:
    """Console entrypoint; returns a process exit code."""

    try:
        server, store, args = make_server(argv)
    except TeamCraftError as exc:
        # Configuration refusals (e.g. non-loopback bind without opt-in).
        print(f"teamcraft-api: {exc}", file=sys.stderr)
        return 2

    if args.print_token:
        print(store.token())
        sys.stdout.flush()

    logger.info(
        "teamcraft api serving on http://%s:%s (loopback_only=%s)",
        server.config.host,
        server.config.port,
        is_loopback_host(server.config.host),
    )
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
