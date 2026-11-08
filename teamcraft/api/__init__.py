"""HTTP API package for TeamCraft (M3-01 security shell + M3-02 Query API + M3-03 SSE).

This subpackage owns the HTTP boundary: transport-level security
(loopback-only default binding, local session tokens, Origin/CSRF checks,
request correlation, a uniform error model, static-file path confinement,
process-vs-dependency health) plus the M3-02 read-only Query routes and the
M3-03 read-only SSE stream over the durable SQLite ``events.seq``.  Both are
mounted only when a :class:`teamcraft.query.QueryService` is supplied to
``create_app``, and they are mounted on the same app instance so they inherit
the guards; Command routes arrive in M3-04.

Importing this package has no filesystem or network side effects; token files
are created only when a :class:`SessionTokenStore` with a path is explicitly
asked to ``ensure()`` a token, or when the server is started via
``teamcraft.api.serve``.  Constructing a ``QueryService`` touches nothing on
disk either — reads (REST and SSE alike) open strict ``mode=ro`` connections
lazily, and the stream never mutates state, enqueues a Wake or dispatches.
"""

from ..query import QueryService, QueryUnavailableError
from .app import create_app
from .config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ApiConfig,
    is_loopback_host,
    load_api_config,
)
from .errors import ApiError, error_payload, error_response
from .health import (
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNKNOWN,
    DependencyHealth,
    DependencyStatus,
    HealthRegistry,
    aggregate_status,
)
from .query import build_query_router
from .redaction import (
    SecretRedactingFilter,
    install_redaction,
    redact_secrets,
    redact_value,
)
from .security import SessionTokenStore
from .static import StaticPathError, resolve_static_path
from .stream import (
    EVENT_SNAPSHOT_REQUIRED,
    LAST_EVENT_ID_HEADER,
    SseConfig,
    StreamConnectionLimiter,
    build_events_router,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "EVENT_SNAPSHOT_REQUIRED",
    "LAST_EVENT_ID_HEADER",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "STATUS_UNKNOWN",
    "ApiConfig",
    "ApiError",
    "DependencyHealth",
    "DependencyStatus",
    "HealthRegistry",
    "QueryService",
    "QueryUnavailableError",
    "SecretRedactingFilter",
    "SessionTokenStore",
    "SseConfig",
    "StaticPathError",
    "StreamConnectionLimiter",
    "aggregate_status",
    "build_events_router",
    "build_query_router",
    "create_app",
    "error_payload",
    "error_response",
    "install_redaction",
    "is_loopback_host",
    "load_api_config",
    "redact_secrets",
    "redact_value",
    "resolve_static_path",
]
