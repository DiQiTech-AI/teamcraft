"""Uvicorn entrypoint for the TeamCraft API (M3-01, M3-05 web console).

CLI defaults are the safe ones: bind ``127.0.0.1`` on port 8765, persist the
session token under the project ``.teamcraft`` runtime directory, and refuse a
non-loopback bind unless the operator passes ``--allow-non-loopback`` (PLAN
§9.2-1 — external exposure must be an explicit, deliberate act).

``--print-token`` and ``--rotate-token`` exist for the local bootstrap UX
(acceptance risk: "token save/rotation experience"): the token is printed once,
on stdout, for the operator to hand to the CLI/UI; ``--rotate-token``
invalidates the previous token before printing the replacement.

M3-05: unless ``--static-root`` is given explicitly (or ``--no-web-ui``
disables it), the server serves the bundled minimal console shipped at
``teamcraft/api/web/`` through the SAME path-confinement as every static
asset (:mod:`teamcraft.api.static`): the entrypoint is
``http://127.0.0.1:8765/static/index.html``, the console itself contains no
data, and every value it shows comes from the guarded ``/api/v1`` routes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from ..errors import TeamCraftError
from ..fresh_start import DATABASE_FILENAME
from ..query import QueryService
from .app import create_app
from .command import CommandService
from .config import DEFAULT_HOST, DEFAULT_PORT, is_loopback_host, load_api_config
from .security import SessionTokenStore

logger = logging.getLogger("teamcraft.api.serve")

#: M3-05 bundled console directory (static shell only; no data, no tokens).
WEB_ROOT = Path(__file__).resolve().parent / "web"


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
        help="directory served, path-confinement applied, under /static "
        "(default: the bundled M3-05 console in teamcraft/api/web)",
    )
    parser.add_argument(
        "--no-web-ui",
        action="store_true",
        help="do not serve the bundled web console (no static root unless "
        "--static-root is given)",
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
    # M3-05: the bundled console is the default static root so the local UI
    # is served through the same confined /static route as every other
    # asset; an explicit --static-root overrides it, --no-web-ui turns it
    # off entirely.  Nothing about the UI changes the security posture:
    # /static/ stays public (no secrets, no state changes) and every value
    # the console displays rides the authenticated /api/v1 routes.
    static_root = args.static_root
    if static_root is None and not args.no_web_ui and WEB_ROOT.is_dir():
        static_root = str(WEB_ROOT)
    config = load_api_config(
        project_root=args.project_root,
        host=args.host,
        port=args.port,
        allow_non_loopback=args.allow_non_loopback,
        extra_origins=tuple(args.origin),
        static_root=static_root,
    )

    store = SessionTokenStore(config.token_file)
    if args.rotate_token:
        store.rotate()
    else:
        store.ensure()

    # M3-02 Query surface: the runtime database sits beside the api token
    # directory (<root>/.teamcraft/teamcraft.db).  Constructing the service
    # touches nothing on disk; unauthenticated reads stay 401 and reads
    # against a not-yet-initialized store return uniform 503s.
    # M3-04 adds the Command surface on that same file: constructing it is
    # likewise side-effect free, and every write runs through the Core
    # Command Service (never prompt/dispatch/Wake/Leader).
    query_service: QueryService | None = None
    command_service: CommandService | None = None
    if config.token_file is not None:
        # token_file = <root>/.teamcraft/api/session-token, so two levels up
        # is the runtime directory that owns teamcraft.db.
        runtime_database = config.token_file.parents[1] / DATABASE_FILENAME
        query_service = QueryService(runtime_database)
        command_service = CommandService(runtime_database)

    app = create_app(
        token_store=store,
        config=config,
        static_root=config.static_root,
        query_service=query_service,
        command_service=command_service,
    )
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
    if server.config.app.state.api_config.static_root is not None:
        # M3-05 entrypoint hint (logged, never printed on the token line,
        # so --print-token output stays a single clean value for tooling).
        logger.info(
            "web console: http://%s:%s/static/index.html (paste the "
            "session token in the page; it is kept in this browser tab only)",
            server.config.host,
            server.config.port,
        )
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
