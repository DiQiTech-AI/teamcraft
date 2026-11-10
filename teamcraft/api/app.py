"""FastAPI application factory for the M3-01 security shell.

``create_app`` wires, in order (outermost first):

1. ``RequestIdMiddleware`` – correlation id on every response;
2. ``SecurityMiddleware``  – bearer-token auth + Origin/CSRF for writes;

plus the uniform error-model handlers, the public health surface, an optional
confined static mount, and log redaction bound to the session token.

The two ``/api/v1/ping`` / ``/api/v1/_probe`` routes are transport scaffolds
used to exercise the guards end-to-end; M3-02/M3-04 replace them with real
Query/Command routes mounted on this same app so they inherit the guards.
When a ``query_service`` is supplied, the M3-02 Query routes and the M3-03
read-only SSE stream (``GET /api/v1/events/stream``, durable-seq backed) are
mounted on this same app and inherit every guard; the SSE router is declared
first so ``/events/stream`` is not shadowed by ``/events/{event_id}``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..query import QueryService
from .command import CommandService, build_command_router
from .config import ApiConfig
from .errors import (
    INTERNAL_ERROR,
    SERVICE_UNAVAILABLE,
    VALIDATION_FAILED,
    ApiError,
    code_for_status,
    error_response,
)
from .health import HealthRegistry, register_health_routes
from .middleware import RequestIdMiddleware, SecurityMiddleware
from .query import build_query_router
from .redaction import install_redaction
from .request_context import new_request_id
from .security import SessionTokenStore
from .static import register_static_routes
from .stream import SseConfig, StreamConnectionLimiter, build_events_router

logger = logging.getLogger("teamcraft.api")

#: Generic text for unmapped HTTP failures; never echoes library internals.
_GENERIC_HTTP_MESSAGES = {
    400: "The request could not be understood.",
    401: "Authentication is required.",
    403: "The request is forbidden.",
    404: "The requested resource does not exist.",
    405: "The method is not allowed for this resource.",
    409: "The request conflicts with the current state.",
    413: "The request payload is too large.",
    429: "Too many requests.",
    503: "The service is temporarily unavailable.",
}


class ProbeRequest(BaseModel):
    """Body of the write-path scaffold probe (no state change)."""

    note: str = Field(default="", max_length=128)


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else new_request_id()


def _safe_validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Field-level validation feedback without leaking internals."""

    details: list[dict[str, Any]] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()))
        details.append(
            {
                "field": loc,
                "type": str(error.get("type", "value_error")),
                "message": str(error.get("msg", "Invalid value.")),
            }
        )
    return details


def create_app(
    *,
    token_store: SessionTokenStore | None = None,
    config: ApiConfig | None = None,
    health_registry: HealthRegistry | None = None,
    static_root: str | os.PathLike[str] | None = None,
    query_service: QueryService | None = None,
    event_stream_config: SseConfig | None = None,
    command_service: CommandService | None = None,
) -> FastAPI:
    """Build the guarded API application.

    Args:
        token_store: session token source; an in-memory random token is issued
            when omitted (nothing is written to disk in that case).
        config: transport/security config; loopback defaults apply otherwise.
        health_registry: dependency probes; the health surface still answers
            with an empty registry (``dependencies`` = ``{}``).
        static_root: optional directory exposed under ``/static`` with the
            confinement rules from :mod:`teamcraft.api.static`.
        query_service: when provided, the M3-02 read-only Query routes and the
            M3-03 read-only SSE stream are mounted on this same app so they
            inherit the security guards; omitted, the app stays the M3-01
            security shell unchanged.
        event_stream_config: transport bounds for the M3-03 SSE stream
            (poll cadence, heartbeats, backfill/connection/frame caps); the
            :class:`~teamcraft.api.stream.SseConfig` defaults apply otherwise.
        command_service: when provided, the M3-04 controlled Command routes
            are mounted on this same app (inheriting every guard) so writes
            enforce ``Idempotency-Key`` + Task/evidence versions and execute
            only through existing Core services (never a direct prompt,
            dispatch, Wake delivery, or Leader touch).
    """

    resolved_config = (config or ApiConfig()).validate()
    store = token_store or SessionTokenStore()
    registry = health_registry or HealthRegistry()

    app = FastAPI(
        title="TeamCraft API",
        version="0.1.0-m301",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.api_config = resolved_config
    app.state.token_store = store
    app.state.health_registry = registry

    # Correlation id first so even authentication failures carry it.
    # (Starlette's add_middleware inserts at position 0: last added runs
    # outermost, hence Security here and RequestId after it.)
    app.add_middleware(SecurityMiddleware, token_store=store, config=resolved_config)
    app.add_middleware(RequestIdMiddleware)

    # Never print the session token, wherever a log line might come from.
    token = store.ensure()
    redactor = install_redaction(logger, (token,))
    app.state.redaction_filter = redactor

    def rotate_session_token() -> str:
        """Rotate the live token and keep log redaction current with it."""

        new_token = store.rotate()
        redactor.register(new_token)
        return new_token

    app.state.rotate_session_token = rotate_session_token

    def _secret_provider() -> tuple[str, ...]:
        try:
            return (store.ensure(),)
        except Exception:  # pragma: no cover - defensive for probes path
            return ()

    def _response_secrets() -> tuple[str, ...]:
        """Current token plus rotated predecessors held by the redactor."""

        try:
            return tuple(redactor.secrets | {store.ensure()})
        except Exception:  # pragma: no cover - defensive for stores
            return ()

    register_health_routes(app, registry, secret_provider=_secret_provider)
    register_static_routes(app, static_root)

    if query_service is not None:
        # M3-02 Query routes ride the same guards (auth/CSRF/request-id) by
        # being mounted on this app; responses additionally scrub every known
        # secret (current token plus rotated predecessors the filter holds).
        app.state.query_service = query_service

        # M3-03 read-only SSE (durable events.seq, snapshot-required gaps).
        resolved_stream_config = (event_stream_config or SseConfig()).validate()
        sse_limiter = StreamConnectionLimiter(
            resolved_stream_config.max_connections
        )
        app.state.sse_config = resolved_stream_config
        app.state.sse_connection_limiter = sse_limiter

        def _stream_secrets() -> tuple[str, ...]:
            return tuple(redactor.secrets | {store.ensure()})

        # Registration order matters: Starlette matches routes in declared
        # order, so the SSE path must precede the Query router's
        # GET /api/v1/events/{event_id} pattern (M3-03 path note).
        app.include_router(
            build_events_router(
                query_service,
                secret_provider=_stream_secrets,
                config=resolved_stream_config,
                limiter=sse_limiter,
            )
        )
        app.include_router(
            build_query_router(
                query_service,
                secret_provider=_stream_secrets,
            )
        )

    if command_service is not None:
        # M3-04 Command routes ride the same guards (auth/CSRF/Origin/
        # request-id) by being mounted on this app; every write additionally
        # requires Idempotency-Key and executes only through existing Core
        # services (see teamcraft/api/command.py for the operation map).
        app.state.command_service = command_service
        app.include_router(
            build_command_router(
                command_service,
                secret_provider=_response_secrets,
            )
        )

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        if exc.status_code >= 500:
            # Readiness refusals are an expected operational state, not a bug:
            # log them at WARNING so they do not flood error channels.
            level = (
                logging.WARNING
                if exc.code == SERVICE_UNAVAILABLE
                else logging.ERROR
            )
            logger.log(level, "api error %s: %s", exc.code, exc.message)
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            details=exc.details,
            request_id=_request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        status = int(exc.status_code)
        if status >= 500:
            # Library-level server errors must not echo detail text.
            return error_response(
                status,
                code_for_status(status),
                "Internal server error.",
                request_id=_request_id(request),
            )
        message = _GENERIC_HTTP_MESSAGES.get(status, "The request failed.")
        return error_response(
            status, code_for_status(status), message, request_id=_request_id(request)
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            422,
            VALIDATION_FAILED,
            "Request validation failed.",
            details=_safe_validation_details(exc),
            request_id=_request_id(request),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error while serving request (type=%s)", type(exc).__name__
        )
        return error_response(
            500,
            INTERNAL_ERROR,
            "Internal server error.",
            request_id=_request_id(request),
        )

    @app.get("/api/v1/ping")
    async def ping(request: Request) -> dict[str, Any]:
        """Authenticated reachability probe (security-shell scaffold)."""

        return {"pong": True, "request_id": _request_id(request)}

    @app.post("/api/v1/_probe")
    async def probe(request: Request, payload: ProbeRequest | None = None) -> dict[str, Any]:
        """Write-path probe exercising Origin/CSRF (security-shell scaffold).

        It performs no state change; M3-04 replaces it with real Command routes
        that require Idempotency-Key and versioned evidence.
        """

        return {
            "accepted": True,
            "note": payload.note if payload is not None else "",
            "request_id": _request_id(request),
        }

    return app
