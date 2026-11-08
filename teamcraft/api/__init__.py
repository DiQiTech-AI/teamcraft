"""M3-01 API security shell for TeamCraft.

This subpackage is the HTTP boundary scaffold only: it owns transport-level
security (loopback-only default binding, local session tokens, Origin/CSRF
checks, request correlation, a uniform error model, static-file path
confinement, and process-vs-dependency health).  Business Query/Command
routes arrive in M3-02/M3-04 and must be mounted on the app produced here so
they inherit the same guards.

Importing this package has no filesystem or network side effects; token files
are created only when a :class:`SessionTokenStore` with a path is explicitly
asked to ``ensure()`` a token, or when the server is started via
``teamcraft.api.serve``.
"""

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
from .security import SessionTokenStore
from .static import StaticPathError, resolve_static_path

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "STATUS_UNKNOWN",
    "ApiConfig",
    "ApiError",
    "DependencyHealth",
    "DependencyStatus",
    "HealthRegistry",
    "SessionTokenStore",
    "StaticPathError",
    "aggregate_status",
    "create_app",
    "error_payload",
    "error_response",
    "is_loopback_host",
    "load_api_config",
    "resolve_static_path",
]
