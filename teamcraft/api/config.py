"""API server configuration boundaries (M3-01).

The security-relevant defaults live here so every entry point agrees on them:

* the server binds ``127.0.0.1`` unless a non-loopback bind is *explicitly*
  requested AND explicitly acknowledged (PLAN §9.2-1: localhost is not a
  trust boundary, external exposure needs deliberate opt-in);
* the session token file lives inside the project-local ``.teamcraft`` runtime
  directory with owner-only permissions.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import load_config
from ..errors import ConfigurationError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

API_HOST_ENV = "TEAMCRAFT_API_HOST"
API_PORT_ENV = "TEAMCRAFT_API_PORT"

_TOKEN_FILENAME = "session-token"

_ORIGIN_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/]+)$")


@dataclass(frozen=True)
class ApiConfig:
    """Immutable transport-security configuration for the API shell."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_non_loopback: bool = False
    extra_origins: tuple[str, ...] = ()
    token_file: Path | None = None
    static_root: Path | None = None

    def validate(self) -> ApiConfig:
        """Return self when every value is inside its allowed range.

        Raises:
            ConfigurationError: host/port invalid, or a non-loopback bind is
                requested without ``allow_non_loopback``.
        """

        host = (self.host or "").strip()
        if not host:
            raise ConfigurationError("API host must not be empty")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ConfigurationError("API port must be an integer")
        if not 1 <= self.port <= 65535:
            raise ConfigurationError(f"API port out of range: {self.port}")
        if not is_loopback_host(host) and not self.allow_non_loopback:
            raise ConfigurationError(
                f"refusing non-loopback bind {host!r}: external exposure requires "
                "an explicit allow_non_loopback opt-in (PLAN §9.2-1)"
            )
        normalized_origins = tuple(
            normalize_origin(origin) for origin in self.extra_origins
        )
        return ApiConfig(
            host=host,
            port=self.port,
            allow_non_loopback=self.allow_non_loopback,
            extra_origins=normalized_origins,
            token_file=self.token_file,
            static_root=self.static_root,
        )


def is_loopback_host(host: str) -> bool:
    """True for addresses that only ever reach this machine.

    ``localhost`` is accepted as the canonical name; every other name or IP is
    treated as *not* loopback so a bind to a outward-facing interface must go
    through the explicit acknowledgement path.
    """

    candidate = (host or "").strip().lower()
    if not candidate:
        return False
    if candidate == "localhost":
        return True
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def normalize_origin(origin: str) -> str:
    """Lower-case and validate an ``Origin``-shaped string."""

    match = _ORIGIN_RE.match((origin or "").strip())
    if match is None:
        raise ConfigurationError(f"invalid origin: {origin!r}")
    return f"{match.group('scheme').lower()}://{match.group('authority').lower()}"


def default_token_file(project_root: str | os.PathLike[str] | None = None) -> Path:
    """Return the project-local token path (not created as a side effect)."""

    return load_config(project_root).runtime_directory / "api" / _TOKEN_FILENAME


def load_api_config(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
    host: str | None = None,
    port: int | None = None,
    allow_non_loopback: bool = False,
    extra_origins: tuple[str, ...] = (),
    static_root: str | os.PathLike[str] | None = None,
) -> ApiConfig:
    """Resolve API config with explicit argument > environment > default.

    The host default is loopback; ``TEAMCRAFT_API_HOST`` can point elsewhere
    but still needs ``allow_non_loopback`` (or ``TEAMCRAFT_API_ALLOW_NON_LOOPBACK``)
    to actually take effect.
    """

    environment = os.environ if environ is None else environ
    resolved_host = host or environment.get(API_HOST_ENV) or DEFAULT_HOST
    raw_port = port
    if raw_port is None:
        env_port = environment.get(API_PORT_ENV)
        if env_port is not None and str(env_port).strip():
            try:
                raw_port = int(str(env_port).strip())
            except ValueError as exc:
                raise ConfigurationError(
                    f"{API_PORT_ENV} must be an integer port number"
                ) from exc
    resolved_port = DEFAULT_PORT if raw_port is None else raw_port
    env_allow = str(
        environment.get("TEAMCRAFT_API_ALLOW_NON_LOOPBACK", "")
    ).strip().lower() in {"1", "true", "yes", "on"}
    config = ApiConfig(
        host=str(resolved_host),
        port=int(resolved_port),
        allow_non_loopback=bool(allow_non_loopback or env_allow),
        extra_origins=tuple(extra_origins),
        token_file=default_token_file(project_root),
        static_root=Path(static_root) if static_root is not None else None,
    )
    return config.validate()
