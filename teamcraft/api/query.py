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

M4-04 adds the READ side of the local-only Commit/Diff evidence chain
(:mod:`teamcraft.delivery`): the current commit/ship/approval evidence for one
Attempt (and per-Task across Attempts), one ship evidence by id, and the
COMPLETE base-to-Commit diff artifact.  These reads call the Core delivery
readers over one strict ``mode=ro`` connection derived from the QueryService's
database path — the same snapshot discipline every other query route uses — so
this surface physically cannot write Git or DB state, and no client input ever
becomes a Git argv, path, or shell argument.  Behavior stays honest:

* missing evidence -> explicit ``null`` fields (or 404 for unknown ids);
* stale evidence   -> durable ``freshness`` verdicts (Task version bump,
  policy/verifier-config drift, broken chain, settled-decision rules) using a
  controlled signal vocabulary — raw Core problem text never rides the wire;
* the diff artifact is returned COMPLETE (Core already refuses oversized diffs
  fail-closed; this surface never truncates a "complete" digest), and a
  tampered/missing artifact surfaces as 503/404 rather than a lie;
* absolute workspace paths never appear in any response body.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from fastapi import APIRouter

from .. import delivery as delivery_core
from .. import domain as domain_core
from .. import storage
from ..errors import NotFoundError, StorageError, ValidationError
from ..query import QueryService, QueryUnavailableError
from .errors import NOT_FOUND, SERVICE_UNAVAILABLE, VALIDATION_FAILED, ApiError
from .redaction import redact_value

#: Generic, uniform failure texts (never echo identifiers, paths, or raw
#: filter values — the shape and code are identical across all routes).
_NOT_FOUND_MESSAGE = "The requested resource does not exist."
_INVALID_QUERY_MESSAGE = "The query parameters are invalid."
_UNAVAILABLE_MESSAGE = "The core snapshot store is unavailable."

#: Pagination bounds for the per-Task delivery listing (evidence rows are
#: unique per Attempt; the cap keeps a response bounded regardless of data).
DELIVERY_LIST_DEFAULT_LIMIT = 50
DELIVERY_LIST_MAX_LIMIT = 200

#: Controlled vocabulary for ``freshness.signals``.  Core currency problems
#: are prefixed with one of these tags; the API exposes ONLY the tags so a
#: stale-evidence verdict is machine-checkable without leaking free text
#: (branch titles aside, problem messages can embed workspace paths).
_FRESHNESS_SIGNALS = frozenset({
    "bindings", "status", "version", "attempt", "brief", "worker report",
    "allocation", "branch", "base", "head", "dirty", "identity", "git",
    "policy", "chain", "commit", "diff", "verifier", "evidence", "binding",
    "other",
})


# ---------------------------------------------------------------------------
# Evidence DTOs (shared with the M4-04 Command write echoes)
#
# Absolute workspace paths (``worktree_path``) are deliberately EXCLUDED:
# responses identify evidence by durable ids/digests only, keeping the same
# no-path-leak discipline the M3-01..M3-04 surfaces pin in tests.
# ---------------------------------------------------------------------------


def commit_evidence_dto(evidence: delivery_core.CommitEvidence) -> dict[str, Any]:
    return {
        "seq": evidence.seq,
        "event_id": evidence.event_id,
        "project_id": evidence.project_id,
        "task_id": evidence.task_id,
        "attempt_id": evidence.attempt_id,
        "attempt_no": evidence.attempt_no,
        "task_version": evidence.task_version,
        "dispatch_task_version": evidence.dispatch_task_version,
        "brief_digest": evidence.brief_digest,
        "brief_version": evidence.brief_version,
        "worker_report_id": evidence.worker_report_id,
        "worker_report_digest": evidence.worker_report_digest,
        "allocation_id": evidence.allocation_id,
        "branch": evidence.branch,
        "base_sha": evidence.base_sha,
        "parent_sha": evidence.parent_sha,
        "commit_sha": evidence.commit_sha,
        "diff_digest": evidence.diff_digest,
        "file_count": evidence.file_count,
        "claims": list(evidence.claims),
        "claims_digest": evidence.claims_digest,
        "source": evidence.source,
        "command_id": evidence.command_id,
        "evidence_digest": evidence.evidence_digest,
        "occurred_at": evidence.occurred_at,
    }


def ship_evidence_dto(evidence: delivery_core.ShipEvidence) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "seq": evidence.seq,
        "event_id": evidence.event_id,
        "project_id": evidence.project_id,
        "task_id": evidence.task_id,
        "attempt_id": evidence.attempt_id,
        "attempt_no": evidence.attempt_no,
        "task_version": evidence.task_version,
        "dispatch_task_version": evidence.dispatch_task_version,
        "brief_digest": evidence.brief_digest,
        "brief_version": evidence.brief_version,
        "worker_report_id": evidence.worker_report_id,
        "worker_report_digest": evidence.worker_report_digest,
        "delivery_mode": evidence.delivery_mode,
        "proposed_action": evidence.proposed_action,
        "allocation_id": evidence.allocation_id,
        "branch": evidence.branch,
        "base_sha": evidence.base_sha,
        "commit_sha": evidence.commit_sha,
        "commit_evidence_id": evidence.commit_evidence_id,
        "diff_digest": evidence.diff_digest,
        "diff_bytes": evidence.diff_bytes,
        "diff_relpath": evidence.diff_relpath,
        "file_count": evidence.file_count,
        "verifiers": [dict(entry) for entry in evidence.verifiers],
        "verifier_digest": evidence.verifier_digest,
        "policy_digest": evidence.policy_digest,
        "evidence_digest": evidence.evidence_digest,
        "command_id": evidence.command_id,
        "occurred_at": evidence.occurred_at,
    }


def delivery_approval_dto(approval: delivery_core.DeliveryApproval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "seq": approval.seq,
        "event_id": approval.event_id,
        "command_id": approval.command_id,
        "ship_evidence_id": approval.ship_evidence_id,
        "project_id": approval.project_id,
        "task_id": approval.task_id,
        "attempt_id": approval.attempt_id,
        "attempt_no": approval.attempt_no,
        "task_version": approval.task_version,
        "commit_sha": approval.commit_sha,
        "branch": approval.branch,
        "diff_digest": approval.diff_digest,
        "evidence_digest": approval.evidence_digest,
        "verifier_digest": approval.verifier_digest,
        "proposed_action": approval.proposed_action,
        "decision": approval.decision,
        "actor": approval.actor,
        "reason": approval.reason,
        "task_status_after": approval.task_status_after,
        "occurred_at": approval.occurred_at,
    }


def freshness_dto(verdict: delivery_core.DeliveryFreshness) -> dict[str, Any]:
    """Reduce a Core currency verdict to ``ok`` + controlled signal tags."""

    signals: list[str] = []
    for problem in verdict.problems:
        tag = str(problem).split(":", 1)[0].strip().lower()
        if tag not in _FRESHNESS_SIGNALS:
            tag = "other"
        if tag not in signals:
            signals.append(tag)
    return {"ok": bool(verdict.ok), "signals": signals}


# ---------------------------------------------------------------------------
# Read channel + view builders
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _ro_channel(service: QueryService) -> Iterator[sqlite3.Connection]:
    """One fresh strict ``mode=ro`` connection over the QueryService store.

    Mirrors :meth:`teamcraft.query.QueryService._read` (never creates files,
    schema-version-gated) so the M4-04 delivery readers run under exactly the
    same snapshot discipline as every other query route.
    """

    db_path = service.database_path
    if not db_path.is_file():
        raise QueryUnavailableError(
            "the TeamCraft runtime database is not initialized"
        )
    uri = db_path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise QueryUnavailableError(
            "the TeamCraft runtime database is not readable"
        ) from exc
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != storage.SCHEMA_VERSION:
            raise QueryUnavailableError(
                f"core schema v{version} is not readable by this build "
                f"(head v{storage.SCHEMA_VERSION})"
            )
        yield conn
    except QueryUnavailableError:
        raise
    except sqlite3.Error as exc:
        raise QueryUnavailableError(
            "the TeamCraft runtime database read failed"
        ) from exc
    finally:
        conn.close()


def _task_brief_dto(task: domain_core.Task) -> dict[str, Any]:
    try:
        policy = json.loads(task.execution_policy_json or "{}")
    except (TypeError, ValueError):
        policy = None
    delivery_mode = policy.get("delivery") if isinstance(policy, dict) else None
    return {
        "id": task.id,
        "project_id": task.project_id,
        "kind": task.kind,
        "status": task.status,
        "version": task.version,
        "delivery_mode": delivery_mode,
    }


def _attempt_delivery_view(
    conn: sqlite3.Connection,
    task: domain_core.Task,
    attempt_id: str,
    attempt_no: int,
    commit: delivery_core.CommitEvidence | None,
    ship: delivery_core.ShipEvidence | None,
    approval: delivery_core.DeliveryApproval | None,
    *,
    live_git: bool,
) -> dict[str, Any]:
    """Honest per-Attempt delivery view with durable currency verdicts.

    ``freshness`` keys exist only for records that exist.  Once an ``accept``
    approval has settled the chain, commit/ship currency is no longer reported
    (the Task legitimately sits DONE at ``task_version + 1``); the approval's
    own verdict — which re-derives the whole chain under ``after_accept``
    semantics — is the durable currency statement in that state.
    """

    accepted = approval is not None and approval.decision == "accept"
    freshness: dict[str, Any] = {}
    if approval is not None:
        freshness["approval"] = freshness_dto(
            delivery_core.delivery_approval_is_current(
                conn, approval, live_git=live_git))
    if not accepted:
        if commit is not None:
            freshness["commit"] = freshness_dto(
                delivery_core.commit_evidence_is_current(
                    conn, commit, live_git=live_git))
        if ship is not None:
            freshness["ship"] = freshness_dto(
                delivery_core.ship_evidence_is_current(
                    conn, ship, live_git=live_git))
    return {
        "task": _task_brief_dto(task),
        "attempt": {"id": attempt_id, "attempt_no": attempt_no},
        "commit": commit_evidence_dto(commit) if commit is not None else None,
        "ship": ship_evidence_dto(ship) if ship is not None else None,
        "approval": (delivery_approval_dto(approval)
                    if approval is not None else None),
        "freshness": freshness,
    }


def _attempt_delivery(
    service: QueryService, attempt_id: str, *, live_git: bool
) -> dict[str, Any]:
    with _ro_channel(service) as conn:
        attempt = domain_core.get_attempt(conn, attempt_id)  # 404
        task = domain_core.get_task(conn, attempt.task_id)  # 404
        commit = delivery_core.load_commit_evidence(conn, attempt.id)
        ship = delivery_core.load_ship_evidence(conn, attempt.id)
        approval = (delivery_core.load_delivery_approval(conn, ship.id)
                    if ship is not None else None)
        return _attempt_delivery_view(
            conn, task, attempt.id, attempt.attempt_no, commit, ship, approval,
            live_git=live_git)


def _task_delivery(
    service: QueryService, task_id: str, *, live_git: bool,
    limit: int | None, offset: int | None,
) -> dict[str, Any]:
    """Per-Attempt delivery evidence for one Task, bounded by pagination."""

    page_limit = DELIVERY_LIST_DEFAULT_LIMIT if limit is None else limit
    page_offset = 0 if offset is None else offset
    if page_limit < 1 or page_limit > DELIVERY_LIST_MAX_LIMIT:
        raise ValidationError(
            f"limit must be between 1 and {DELIVERY_LIST_MAX_LIMIT}")
    if page_offset < 0:
        raise ValidationError("offset must be >= 0")
    with _ro_channel(service) as conn:
        task = domain_core.get_task(conn, task_id)  # 404
        commits = {e.attempt_id: e
                   for e in delivery_core.list_commit_evidence(conn, task_id)}
        ships = {e.attempt_id: e
                 for e in delivery_core.list_ship_evidence(conn, task_id)}
        approvals = {e.attempt_id: e
                     for e in delivery_core.list_delivery_approvals(conn, task_id)}
        attempt_no: dict[str, int] = {}
        for evidence in list(commits.values()) + list(ships.values()) + list(
                approvals.values()):
            attempt_no.setdefault(evidence.attempt_id, evidence.attempt_no)
        ordered = sorted(attempt_no, key=lambda aid: (attempt_no[aid], aid))
        total = len(ordered)
        items: list[dict[str, Any]] = []
        for aid in ordered[page_offset:page_offset + page_limit]:
            ship = ships.get(aid)
            approval = approvals.get(aid)
            items.append(_attempt_delivery_view(
                conn, task, aid, attempt_no[aid], commits.get(aid), ship,
                approval, live_git=live_git))
    return {
        "task": _task_brief_dto(task),
        "live_git": live_git,
        "items": items,
        "limit": page_limit,
        "offset": page_offset,
        "total": total,
    }


def _ship_evidence(service: QueryService, ship_evidence_id: str) -> dict[str, Any]:
    with _ro_channel(service) as conn:
        evidence = delivery_core.get_ship_evidence(conn, ship_evidence_id)
    return {"ship_evidence": ship_evidence_dto(evidence)}


def _ship_diff(service: QueryService, ship_evidence_id: str) -> dict[str, Any]:
    """COMPLETE, digest-verified diff artifact of one ship evidence record."""

    with _ro_channel(service) as conn:
        evidence = delivery_core.get_ship_evidence(conn, ship_evidence_id)
        task = domain_core.get_task(conn, evidence.task_id)
        repo = delivery_core.task_repo_root(conn, task)
        root = delivery_core.default_delivery_artifact_root(repo)
        if not os.path.isdir(root):
            # Honest read behavior: the configured artifact store is gone.
            raise NotFoundError("ship diff artifact root is missing")
        payload = delivery_core.read_ship_diff(conn, ship_evidence_id)
    payload["delivery_mode"] = evidence.delivery_mode
    return payload


def _delivery_page_params(
    limit: int | None, offset: int | None
) -> tuple[int | None, int | None]:
    return limit, offset


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


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
        except StorageError:
            # Durable evidence that no longer re-proves itself (tampered
            # artifact, broken chain) is a store-integrity failure: the
            # uniform 503 keeps the shape honest without echoing Core text.
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

    # -- M4-04 local-only delivery evidence ------------------------------------------
    #
    # Read-only views over :mod:`teamcraft.delivery` durable records.  Route
    # params are opaque ids only: no path, argv, or shell fragment can be
    # expressed through them, and the underlying readers/verifiers run with the
    # Core audited Git runner (local plumbing verbs only) when ``live_git``
    # is requested; default currency checks are pure durable-row reads.

    @router.get("/tasks/{task_id}/delivery")
    def get_task_delivery(
        task_id: str,
        live_git: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        limit, offset = _delivery_page_params(limit, offset)
        return _run(
            lambda: _task_delivery(
                service, task_id, live_git=live_git, limit=limit, offset=offset)
        )

    @router.get("/attempts/{attempt_id}/delivery")
    def get_attempt_delivery(
        attempt_id: str, live_git: bool = False,
    ) -> Any:
        return _run(
            lambda: _attempt_delivery(service, attempt_id, live_git=live_git)
        )

    @router.get("/ship-evidence/{ship_evidence_id}")
    def get_ship_evidence(ship_evidence_id: str) -> Any:
        return _run(lambda: _ship_evidence(service, ship_evidence_id))

    @router.get("/ship-evidence/{ship_evidence_id}/diff")
    def get_ship_evidence_diff(ship_evidence_id: str) -> Any:
        return _run(lambda: _ship_diff(service, ship_evidence_id))

    return router
