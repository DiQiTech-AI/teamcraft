"""HTTP security middleware (M3-01).

Two responsibilities, kept as separate middlewares so ordering is explicit:

``RequestIdMiddleware``
    Assigns (or accepts a sanitized inbound) correlation id and stamps it on
    every response.

``SecurityMiddleware``
    * every non-public route requires the session token as an
      ``Authorization: Bearer`` header — the local address alone is *never*
      trusted (PLAN §9.2-2);
    * writes (anything that is not GET/HEAD/OPTIONS/TRACE) additionally must
      pass an Origin check and the CSRF token check.

Failures short-circuit with the uniform error model; secret values are only
ever compared, never logged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .config import ApiConfig
from .errors import (
    CSRF_CHECK_FAILED,
    FORBIDDEN_ORIGIN,
    UNAUTHENTICATED,
    error_response,
)
from .request_context import (
    REQUEST_ID_HEADER,
    resolve_request_id,
)
from .security import SessionTokenStore

CSRF_HEADER = "X-CSRF-Token"
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "bearer "

# Methods that must not require CSRF treatment.  TRACE is deliberately NOT
# safe here: it can be abused for credential reflection.
CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Paths reachable without a session token.  They expose no secrets and no
#: state changes: liveness/readiness statuses and confined static assets.
PUBLIC_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/health/live",
        "/api/v1/health/ready",
    }
)
PUBLIC_PREFIXES = ("/static/",)


def request_id_of(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def extract_bearer_token(request: Request) -> str | None:
    """Return the bearer token from the Authorization header, if present."""

    raw = request.headers.get(AUTHORIZATION_HEADER)
    if raw is None:
        return None
    if raw.lower().startswith(BEARER_PREFIX):
        token = raw[len(BEARER_PREFIX) :].strip()
        return token or None
    return None


def request_origin(request: Request) -> str:
    """Reconstruct this request's own origin from scheme + Host."""

    scheme = request.url.scheme or "http"
    host = request.headers.get("host") or request.url.netloc or "127.0.0.1"
    return f"{scheme.lower()}://{host.lower()}"


def is_allowed_origin(
    origin: str, request: Request, extra_origins: Iterable[str] = ()
) -> bool:
    """Same-origin comparison against this request, plus explicit allowlist."""

    normalized = origin.strip().rstrip("/").lower()
    if not normalized:
        return False
    if normalized == request_origin(request).rstrip("/").lower():
        return True
    return normalized in {o.rstrip("/").lower() for o in extra_origins}


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Correlate every request/response with an id, echoed safely inbound."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        if REQUEST_ID_HEADER not in response.headers:
            response.headers[REQUEST_ID_HEADER] = request_id
        return response


class SecurityMiddleware(BaseHTTPMiddleware):
    """Token authentication plus Origin/CSRF enforcement for writes."""

    def __init__(
        self,
        app,
        *,
        token_store: SessionTokenStore,
        config: ApiConfig | None = None,
    ) -> None:
        super().__init__(app)
        self._token_store = token_store
        self._config = config or ApiConfig()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request_id_of(request)
        if is_public_path(request.url.path):
            return await call_next(request)

        token = extract_bearer_token(request)
        if not self._token_store.verify(token):
            return error_response(
                401,
                UNAUTHENTICATED,
                "A valid local session token is required for this endpoint.",
                request_id=request_id,
            )

        if request.method.upper() not in CSRF_SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not is_allowed_origin(
                origin, request, self._config.extra_origins
            ):
                return error_response(
                    403,
                    FORBIDDEN_ORIGIN,
                    "The request Origin is not allowed by this service.",
                    details={"allowed": "same-origin"},
                    request_id=request_id,
                )
            csrf = request.headers.get(CSRF_HEADER)
            if not self._token_store.verify(csrf):
                return error_response(
                    403,
                    CSRF_CHECK_FAILED,
                    "Write requests must present a matching CSRF token header.",
                    request_id=request_id,
                )
        return await call_next(request)
