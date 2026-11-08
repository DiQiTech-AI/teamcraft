"""Process-vs-dependency health (M3-01).

Acceptance requires the health surface to distinguish *"the server process is
running"* from *"the things it depends on are healthy"*:

* ``GET /api/v1/health``       — aggregate snapshot: separate ``process`` and
  ``dependencies`` blocks; top-level ``status`` is ``ok`` (all dependencies
  confirmed up), ``degraded`` (something is confirmed down) or ``unknown``
  (nothing confirmed down, but something is unproven);
* ``GET /api/v1/health/live``  — process liveness only (never dependency
  state, so a flaky or unproven dependency cannot flap the liveness signal);
* ``GET /api/v1/health/ready`` — readiness gated on registered dependencies:
  HTTP 503 with the uniform error shape when any dependency is ``down``
  **or** ``unknown``.

An ``unknown`` dependency (no probe conclusion) is never treated as healthy:
it blocks readiness and prevents an ``ok`` aggregate rather than fabricating
confidence.  The 503 body carries dependency *names* and the coarse status
only — probe detail (which may embed paths or credentials) is logged in
scrubbed form and never returned to clients.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from fastapi import FastAPI

from .errors import SERVICE_UNAVAILABLE, ApiError
from .redaction import redact_secrets

logger = logging.getLogger("teamcraft.api.health")


class DependencyStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DependencyHealth:
    """Outcome of one dependency probe.

    ``detail`` is server-side only; it must never be echoed to clients.
    """

    status: DependencyStatus
    detail: str | None = None

    @staticmethod
    def up() -> DependencyHealth:
        return DependencyHealth(DependencyStatus.UP)

    @staticmethod
    def down(detail: str | None = None) -> DependencyHealth:
        return DependencyHealth(DependencyStatus.DOWN, detail)

    @staticmethod
    def unknown(detail: str | None = None) -> DependencyHealth:
        return DependencyHealth(DependencyStatus.UNKNOWN, detail)


ProbeResult = bool | DependencyStatus | DependencyHealth | None
Probe = Callable[[], ProbeResult]


def _coerce(result: ProbeResult) -> DependencyHealth:
    if result is True:
        return DependencyHealth.up()
    if result is False:
        return DependencyHealth.down()
    if result is None:
        return DependencyHealth.unknown()
    if isinstance(result, DependencyStatus):
        return DependencyHealth(result)
    if isinstance(result, DependencyHealth):
        return result
    return DependencyHealth.unknown()


class HealthRegistry:
    """Named dependency probes registered by the embedding application."""

    def __init__(self) -> None:
        self._probes: dict[str, Probe] = {}

    def register(self, name: str, probe: Probe) -> None:
        key = (name or "").strip()
        if not key:
            raise ValueError("health check name must not be empty")
        self._probes[key] = probe

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._probes))

    def check(self, name: str, *, secrets: tuple[str, ...] = ()) -> DependencyHealth:
        probe = self._probes.get(name)
        if probe is None:
            return DependencyHealth.unknown()
        try:
            outcome = _coerce(probe())
        except Exception as exc:  # probes are external code: contain failures
            safe_detail = redact_secrets(type(exc).__name__, secrets)
            logger.warning("health probe %r raised %s", name, safe_detail)
            return DependencyHealth.down()
        return outcome

    def snapshot(self, *, secrets: tuple[str, ...] = ()) -> dict[str, dict[str, str]]:
        return {
            name: {"status": self.check(name, secrets=secrets).status.value}
            for name in self.names
        }

    def failing(self, *, secrets: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Dependencies confirmed ``down`` (drives the aggregate degraded state)."""

        return tuple(
            name
            for name in self.names
            if self.check(name, secrets=secrets).status is DependencyStatus.DOWN
        )

    def not_ready(self, *, secrets: tuple[str, ...] = ()) -> tuple[tuple[str, str], ...]:
        """Dependencies that block readiness.

        ``UNKNOWN`` blocks readiness alongside ``down``: an unproven dependency
        must never let the service advertise itself as ready (no fabricated
        health).  Each entry is ``(name, status)`` with status in
        ``{"down", "unknown"}`` — names and coarse status only, never probe
        detail.
        """

        blocked = []
        for name in self.names:
            status = self.check(name, secrets=secrets).status
            if status is not DependencyStatus.UP:
                blocked.append((name, status.value))
        return tuple(blocked)


#: Aggregate status when every registered dependency is up (or none are).
STATUS_OK = "ok"
#: Aggregate status when at least one dependency is confirmed down.
STATUS_DEGRADED = "degraded"
#: Aggregate status when nothing is confirmed down but something is unproven.
STATUS_UNKNOWN = "unknown"


def aggregate_status(dependency_statuses: Iterable[object]) -> str:
    """Collapse per-dependency statuses into one honest aggregate value.

    Accepts ``DependencyStatus`` members or their string values.  Confirmed
    failure outranks uncertainty (``degraded``); otherwise any ``unknown``
    yields ``unknown`` rather than a misleading ``ok``.
    """

    values = [str(getattr(item, "value", item)) for item in dependency_statuses]
    if DependencyStatus.DOWN.value in values:
        return STATUS_DEGRADED
    if DependencyStatus.UNKNOWN.value in values:
        return STATUS_UNKNOWN
    return STATUS_OK


def process_info() -> dict[str, object]:
    """Liveness facts about this process (no dependency state)."""

    return {"status": "up", "pid": os.getpid()}


def register_health_routes(
    app: FastAPI,
    registry: HealthRegistry,
    *,
    secret_provider: Callable[[], tuple[str, ...]] = lambda: (),
) -> None:
    """Mount the public health surface on ``app``."""

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        secrets = tuple(secret_provider())
        dependencies = registry.snapshot(secrets=secrets)
        return {
            "status": aggregate_status(
                info["status"] for info in dependencies.values()
            ),
            "process": process_info(),
            "dependencies": dependencies,
        }

    @app.get("/api/v1/health/live")
    async def health_live() -> dict[str, object]:
        return {"status": "alive", "process": process_info()}

    @app.get("/api/v1/health/ready")
    async def health_ready() -> dict[str, object]:
        secrets = tuple(secret_provider())
        blocked = registry.not_ready(secrets=secrets)
        if blocked:
            # Names plus coarse status only: probe detail stays server-side.
            raise ApiError(
                503,
                SERVICE_UNAVAILABLE,
                "Service is not ready: one or more dependencies are not "
                "confirmed healthy.",
                details={
                    "not_ready": [
                        {"name": name, "status": status} for name, status in blocked
                    ]
                },
            )
        return {
            "status": "ready",
            "dependencies": registry.snapshot(secrets=secrets),
        }
