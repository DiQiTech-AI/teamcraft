"""M3-02 read-only Query API routes (mounted on the M3-01 guarded app).

Every route here is a ``GET``: authentication is inherited from
``SecurityMiddleware`` (query paths are not public), and all data comes from
the single :class:`teamcraft.query.QueryService` — routes never touch the
database directly.  Failure mapping is uniform:

* unknown entity            -> 404 ``NOT_FOUND`` (generic message, no echo)
* malformed filter/limit    -> 422 ``VALIDATION_FAILED`` (same code as the
  framework-level validation errors handled in ``create_app``)
* store unavailable/too-new -> 503 ``SERVICE_UNAVAILABLE``
* anything else             -> the app-level uniform 500 (no detail echo)

Responses run through response-level redaction so token material cannot ride
inside historical free text.  No route submits commands, dispatches work, or
enqueues wakes — the Query surface is physically read-only (the service only
opens ``mode=ro`` SQLite channels).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import APIRouter

from ..errors import NotFoundError, ValidationError
from ..query import QueryService, QueryUnavailableError
from .errors import NOT_FOUND, SERVICE_UNAVAILABLE, VALIDATION_FAILED, ApiError
from .redaction import redact_value

#: Generic, uniform failure texts (never echo identifiers, paths, or raw
#: filter values — the shape and code are identical across all routes).
_NOT_FOUND_MESSAGE = "The requested resource does not exist."
_INVALID_QUERY_MESSAGE = "The query parameters are invalid."
_UNAVAILABLE_MESSAGE = "The core snapshot store is unavailable."


def build_query_router(
    service: QueryService,
    *,
    secret_provider: Callable[[], Iterable[str]] = tuple,
) -> APIRouter:
    """Create the authenticated read-only query router.

    Args:
        service: the single Query Service the whole surface reads through.
        secret_provider: current secret values (session token and any
            rotated predecessors) used to redact response text.
    """

    router = APIRouter(prefix="/api/v1")

    def _secrets() -> tuple[str, ...]:
        try:
            return tuple(secret_provider())
        except Exception:  # pragma: no cover - defensive for providers
            return ()

    def _run(action: Callable[[], Any]) -> Any:
        try:
            payload = action()
        except NotFoundError:
            raise ApiError(404, NOT_FOUND, _NOT_FOUND_MESSAGE) from None
        except ValidationError:
            raise ApiError(422, VALIDATION_FAILED, _INVALID_QUERY_MESSAGE) from None
        except QueryUnavailableError:
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE) from None
        except sqlite3.Error:
            # Driver text can embed paths; surface only the uniform shape.
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE) from None
        return redact_value(payload, _secrets())

    # -- Projects -------------------------------------------------------------

    @router.get("/projects")
    def list_projects(limit: int | None = None, offset: int | None = None) -> Any:
        return _run(lambda: service.list_projects(limit=limit, offset=offset))

    @router.get("/projects/{project_id}")
    def get_project(project_id: str) -> Any:
        return _run(lambda: service.get_project(project_id))

    @router.get("/projects/{project_id}/snapshot")
    def project_snapshot(project_id: str) -> Any:
        return _run(lambda: service.project_snapshot(project_id))

    @router.get("/projects/{project_id}/tasks")
    def list_project_tasks(
        project_id: str,
        status: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_tasks(
                project_id, status=status, kind=kind, limit=limit, offset=offset
            )
        )

    @router.get("/projects/{project_id}/ceo")
    def project_ceo(project_id: str) -> Any:
        return _run(lambda: service.get_ceo_view(project_id))

    # -- Tasks ------------------------------------------------------------------

    @router.get("/tasks/{task_id}")
    def get_task(task_id: str) -> Any:
        return _run(lambda: service.get_task(task_id))

    @router.get("/tasks/{task_id}/attempts")
    def list_task_attempts(
        task_id: str,
        status: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_attempts(
                task_id, status=status, limit=limit, offset=offset
            )
        )

    @router.get("/tasks/{task_id}/dependencies")
    def list_task_dependencies(task_id: str) -> Any:
        return _run(lambda: service.list_dependencies(task_id))

    @router.get("/tasks/{task_id}/verifications")
    def list_task_verifications(
        task_id: str,
        status: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_verifications(
                task_id=task_id, status=status, limit=limit, offset=offset
            )
        )

    @router.get("/tasks/{task_id}/approvals")
    def list_task_approvals(
        task_id: str,
        decision: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_approvals(
                task_id=task_id, decision=decision, limit=limit, offset=offset
            )
        )

    @router.get("/tasks/{task_id}/recovery-actions")
    def list_task_recovery_actions(
        task_id: str,
        action: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_recovery_actions(
                task_id=task_id, action=action, state=state, limit=limit, offset=offset
            )
        )

    # -- Attempts and Sessions ----------------------------------------------------

    @router.get("/attempts/{attempt_id}")
    def get_attempt(attempt_id: str) -> Any:
        return _run(lambda: service.get_attempt(attempt_id))

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str) -> Any:
        return _run(lambda: service.get_session(session_id))

    # -- Agents / runtime members --------------------------------------------------

    @router.get("/agents")
    def list_agents(limit: int | None = None, offset: int | None = None) -> Any:
        return _run(lambda: service.list_agents(limit=limit, offset=offset))

    @router.get("/agents/{agent_id}")
    def get_agent(agent_id: str) -> Any:
        return _run(lambda: service.get_agent(agent_id))

    @router.get("/members")
    def list_members(limit: int | None = None, offset: int | None = None) -> Any:
        return _run(lambda: service.list_members(limit=limit, offset=offset))

    @router.get("/members/{member_name}")
    def get_member(member_name: str) -> Any:
        return _run(lambda: service.get_member(member_name))

    # -- Events ---------------------------------------------------------------------

    @router.get("/events")
    def list_events(
        project_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        kind: str | None = None,
        after_seq: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_events(
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                kind=kind,
                after_seq=after_seq,
                limit=limit,
                offset=offset,
            )
        )

    @router.get("/events/{event_id}")
    def get_event(event_id: str) -> Any:
        return _run(lambda: service.get_event(event_id))

    # -- Wakes ------------------------------------------------------------------------

    @router.get("/wakes")
    def list_wakes(
        project_id: str,
        target: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_wakes(
                project_id, target=target, state=state, limit=limit, offset=offset
            )
        )

    @router.get("/wakes/{wake_id}")
    def get_wake(wake_id: str) -> Any:
        return _run(lambda: service.get_wake(wake_id))

    @router.get("/wakes/{wake_id}/events")
    def list_wake_events(wake_id: str) -> Any:
        return _run(lambda: service.list_wake_events(wake_id))

    @router.get("/wake-cursors/{project_id}")
    def get_wake_cursor(project_id: str, target: str | None = None) -> Any:
        return _run(lambda: service.get_wake_cursor(project_id, target=target))

    # -- Approvals ----------------------------------------------------------------------

    @router.get("/approvals")
    def list_approvals(
        project_id: str | None = None,
        task_id: str | None = None,
        decision: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_approvals(
                project_id=project_id,
                task_id=task_id,
                decision=decision,
                limit=limit,
                offset=offset,
            )
        )

    @router.get("/approvals/{approval_id}")
    def get_approval(approval_id: str) -> Any:
        return _run(lambda: service.get_approval(approval_id))

    # -- Recovery records ---------------------------------------------------------------

    @router.get("/recovery-actions")
    def list_recovery_actions(
        project_id: str | None = None,
        task_id: str | None = None,
        action: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_recovery_actions(
                project_id=project_id,
                task_id=task_id,
                action=action,
                state=state,
                limit=limit,
                offset=offset,
            )
        )

    @router.get("/recovery-actions/{command_id}")
    def get_recovery_action(command_id: str) -> Any:
        return _run(lambda: service.get_recovery_action(command_id))

    # -- Verifications ---------------------------------------------------------------------

    @router.get("/verifications")
    def list_verifications(
        project_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_verifications(
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        )

    @router.get("/verifications/{verification_id}")
    def get_verification(verification_id: str) -> Any:
        return _run(lambda: service.get_verification(verification_id))

    @router.get("/verification-configs")
    def list_verification_configs(
        project_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return _run(
            lambda: service.list_verification_configs(
                project_id=project_id, limit=limit, offset=offset
            )
        )

    return router
