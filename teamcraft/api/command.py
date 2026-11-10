"""M3-04 controlled Command API (mounted on the M3-01 guarded app).

Roadmap mapping (doc/TEAMCRAFT_2_0_M0_REVIEW.md §9.3 M3-04): every operation
below is an adapter over an EXISTING Core interface — this module adds no new
state machine, table, or transition anywhere; it only translates HTTP into the
approved Core calls and back:

==========================================  ==========================================
M3-04 operation                            Core interface used
==========================================  ==========================================
Task create                                ``domain.create_task``
Task edit (title/description/risk)        ``domain.update_task``
Task priority update                       ``domain.update_task(priority=...)``
Task query                                 M3-02 ``QueryService`` (same app)
DAG dependency add / remove               ``domain.add_dependency`` /
                                         ``domain.remove_dependency``
Task cancel, attempt-free                 ``domain.apply_task_event(user_cancel)``
Task cancel with occupying Attempt,       ``recovery.request_action``
wait, reconcile                          (durable ``recovery.*`` Commands)
Task pause / resume                       ``domain.pause_task`` /
                                         ``domain.resume_task`` (durable
                                         ``tasks.paused`` dispatch gate)
Execution-policy update                   ``domain.set_execution_policy``
                                         (delivery/router/verifier/timeout/
                                         max_attempts persisted under CAS)
Scheduler pause / resume                  ``scheduler.pause_schedule`` /
                                         ``scheduler.resume_schedule``
                                         (durable control row consulted by
                                         every planning snapshot)
Retry approval                            ``recovery.approve_retry`` under a
                                         short API-held lease
Approval decision (accept/reject)        ``verification.decide_approval``
                                         under a short API-held lease
Manual CEO Wake request                   ``wake.request_ceo_attention``
                                         (durable intent only: Command +
                                         ``ceo.wake_requested`` Event)
Command status read                        ``commands.get_command``
Scheduler pause-state read                 ``scheduler.scheduler_state``
==========================================  ==========================================

Three execution tiers, all durable through the M1-05 Command Service
(:mod:`teamcraft.commands` — the ONLY place commands are written):

* **Synchronous local transitions** (create/edit/dependency/pause/resume/
  policy/cancel-attempt-free/scheduler pause-resume): the whole unit —
  command ``submit -> claim`` + the Core state transition + ``confirm`` —
  runs inside ONE :func:`teamcraft.storage.transaction`, so a refused effect
  (version conflict, cycle, illegal transition) rolls the command row back
  with it: refusals leave NOTHING behind, accepted writes leave Command +
  domain rows + linked events as one atomic audit unit (the same envelope
  :func:`teamcraft.verification.configure_verifier` uses).  Success returns
  200/201 with ``execution: applied``.

* **Lease-fenced approvals** (retry approval, verification approval): the
  adapter acquires the project Leader lease through the normal
  :func:`teamcraft.leader.acquire_or_renew` protocol under holder
  ``teamcraft-api``, applies ``recovery.approve_retry`` /
  ``verification.decide_approval``, and releases — participating in fencing
  rather than bypassing it.  While another holder (e.g. the runtime
  scheduler) holds the lease these routes fail 409 ``LEADER_BUSY`` and
  write nothing; Core keeps every invariant (live-lease validation,
  occupying-attempt and attempt-cap checks, latest-Verification binding,
  scout-only acceptance, per-verification decision dedupe).

* **Recorded async intents** (cancel/wait/reconcile of a live Attempt via
  ``recovery.request_action``; CEO Wake request via
  ``wake.request_ceo_attention``): the API persists the durable Command
  (state ``pending``; the Wake request additionally its linked Event) and
  returns ``202``.  Stopping an occupied process and sending prompts belong
  to the fenced scheduler/runtime executor (M2-02/M2-08/M2-09); this
  surface never calls ``wake.admit_pending_wake``, ``wake.ingest_events``,
  ``domain.create_attempt`` or ``verification.run_verification``, so it
  physically cannot deliver a prompt, reserve an attempt, or bypass the
  Leader.  Recorded intents cannot stay invisible forever: ``Scheduler.tick``
  runs ``commands.sweep_stale_commands`` so every due/invalidated PENDING
  Command transitions to EXPIRED with its own audit event, and every
  returned command id stays readable at ``GET /commands/{command_id}``.
  The manual CEO Wake request deliberately only QUEUES: the
  ``ceo.wake_requested`` Event kind is not on the wake-ingestion ignore
  list, so the next fenced scheduler tick folds it into an ordinary merged
  Wake candidate — the only path to a prompt, gated by CEO initialization
  and lease fencing exactly like every automated Wake.

Hard rules pinned here (M0 review M3-04 row; PLAN §8.5):

* every write requires the ``Idempotency-Key`` header (uniform 422 refusal
  when missing/blank/oversized) and, where the operation binds a Task, an
  ``expected_version``;
* retries under the same key + identical request replay the stored Command
  (same ids, ``X-Idempotent-Replay: true``) and write nothing new; the same
  key with any different content raises through
  :class:`teamcraft.errors.IdempotencyConflictError` -> 409 with the existing
  command id;
* stale Task/evidence versions -> 409 (Core ``VersionConflictError`` or the
  claim-time evidence re-check ``EVIDENCE_STALE``); a Task version bump
  atomically expires PENDING commands bound to the older version
  (``domain._cas_task`` -> ``invalidate_stale_commands_for_task``), so
  critical edits either run against current evidence or leave queued
  side-effects dead — old evidence can never fire;
* dependency cycles -> 409 with the concrete closed ``cycle`` path
  (:class:`teamcraft.errors.DependencyCycleError`);
* errors keep the uniform M3-01 shape ``{code,message,details,request_id}``
  and never echo raw core/SQL text; responses run through redaction;
* the ``leader_lease`` table is touched ONLY through :mod:`teamcraft.leader`
  primitives, and only for the approval tier (short-lived acquire + release
  under the API's own holder id): no route can steal, renew, or release
  another holder's live lease, and lease contention surfaces as 409
  ``LEADER_BUSY`` instead of an unfenced write (pinned by
  ``tests/test_api_m304.py``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import contextlib

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import commands as commands_core
from .. import domain, fresh_start, leader, recovery, scheduler, verification, wake
from ..errors import (
    AttemptSlotOccupiedError,
    CommandStateConflictError,
    ConflictError,
    DependencyCycleError,
    IllegalTransitionError,
    IdempotencyConflictError,
    NotFoundError,
    SchemaOutOfDateError,
    StorageError,
    ValidationError,
    VersionConflictError,
)
from ..ids import require_entity_id
from ..storage import TeamCraftConnection, connect, transaction
from .errors import (
    CONFLICT,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    VALIDATION_FAILED,
    ApiError,
)
from .redaction import redact_value

#: Header mandated for every write (PLAN §8.5).
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: Replayed responses carry this so clients can distinguish a reuse.
REPLAY_HEADER = "X-Idempotent-Replay"

MAX_IDEMPOTENCY_KEY_LENGTH = commands_core.MAX_IDEMPOTENCY_KEY_LENGTH  # 256
MAX_ID_LENGTH = 512
MAX_TITLE_BYTES = 512
MAX_DESCRIPTION_BYTES = 4096
MAX_ITEM_BYTES = 512
MAX_ACTOR_BYTES = 1024
MAX_REASON_BYTES = 1024
MAX_LIST_ITEMS = 64

#: Canonical command kinds owned by this surface.  ``recovery.retry_approved``
#: and ``verification.approval`` are the exact kinds :mod:`teamcraft.recovery`
#: / :mod:`teamcraft.verification` write when the API applies the Core
#: transition under its own short lease.  ``task.*``/``scheduler.*`` submission
#: events are ignored by ``wake`` ingestion, so command traffic can never fan
#: out into a Wake storm; ``ceo.wake_requested`` is deliberately NOT ignored —
#: it is the only path that turns a manual CEO request into a Wake candidate
#: on the next fenced tick.
KIND_TASK_CREATE = "task.create"
KIND_TASK_UPDATE = "task.update"
KIND_TASK_DEPENDENCY_ADD = "task.dependency_add"
KIND_TASK_DEPENDENCY_REMOVE = "task.dependency_remove"
KIND_TASK_CANCEL_APPLIED = "task.cancel"
KIND_TASK_PAUSE = "task.pause"
KIND_TASK_RESUME = "task.resume"
KIND_TASK_POLICY_UPDATE = "task.policy_update"
KIND_RETRY_APPROVED = "recovery.retry_approved"
KIND_CEO_WAKE_REQUEST = "ceo.wake_request"
KIND_SCHEDULER_PAUSE = "scheduler.pause"
KIND_SCHEDULER_RESUME = "scheduler.resume"
KIND_APPROVAL_DECISION = "verification.approval"

#: Holder id for the short-lived lease the approval-tier routes acquire
#: through the normal Leader protocol (never steals a live lease).
API_LEASE_HOLDER = "teamcraft-api"
API_LEASE_TTL_SECONDS = 10

#: Actions accepted by POST /tasks/{task_id}/commands.
TASK_COMMAND_ACTIONS = frozenset(
    {"pause", "resume", "cancel", "retry", "wait", "reconcile"}
)
#: Actions recorded through ``recovery.request_action`` (attempt-scoped).
_RECOVERY_PASSTHROUGH_ACTIONS = frozenset({"cancel", "wait", "reconcile"})

APPROVAL_DECISIONS = frozenset({"accept", "reject"})
CEO_ACTIONS = frozenset({"request_processing"})
TASK_STATUSES_TERMINAL = domain.TERMINAL_TASK_STATUSES

#: Generic, uniform failure texts (identical across all routes; identifiers
#: ride only in structured ``details``).
_NOT_FOUND_MESSAGE = "The requested resource does not exist."
_INVALID_COMMAND_MESSAGE = "The command parameters are invalid."
_IDEMPOTENCY_REQUIRED_MESSAGE = (
    "Write requests must carry a non-empty Idempotency-Key header."
)
_CONFLICT_MESSAGE = "The command conflicts with the current state."
_UNAVAILABLE_MESSAGE = "The core command store is unavailable."


def _dump(value: dict[str, Any]) -> str:
    """Canonical command payload JSON (identical shape to Core modules)."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # A stored payload is durable Core data; an unparseable value is
        # reported as opaque rather than echoed.
        return {"unavailable": True}


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _text(value: str | None, field: str, *, required: bool, max_bytes: int) -> str:
    if value is None or not value.strip():
        if required:
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_COMMAND_MESSAGE,
                details={"field": field, "constraint": "non-empty string"},
            )
        return ""
    if _byte_len(value) > max_bytes:
        raise ApiError(
            422,
            VALIDATION_FAILED,
            _INVALID_COMMAND_MESSAGE,
            details={"field": field, "constraint": f"at most {max_bytes} bytes"},
        )
    return value


def _entity_id(value: str, field: str) -> str:
    if _byte_len(value) > MAX_ID_LENGTH:
        raise ApiError(
            422,
            VALIDATION_FAILED,
            _INVALID_COMMAND_MESSAGE,
            details={"field": field, "constraint": f"at most {MAX_ID_LENGTH} bytes"},
        )
    try:
        return str(require_entity_id(value))
    except ValidationError:
        raise ApiError(
            422,
            VALIDATION_FAILED,
            _INVALID_COMMAND_MESSAGE,
            details={"field": field, "constraint": "non-empty entity id"},
        ) from None


def _idempotency_key(request: Request) -> str:
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None or not raw.strip():
        raise ApiError(
            422,
            VALIDATION_FAILED,
            _IDEMPOTENCY_REQUIRED_MESSAGE,
            details={"field": IDEMPOTENCY_HEADER},
        )
    key = raw.strip()
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ApiError(
            422,
            VALIDATION_FAILED,
            _IDEMPOTENCY_REQUIRED_MESSAGE,
            details={
                "field": IDEMPOTENCY_HEADER,
                "constraint": f"at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters",
            },
        )
    return key


def _version(expected_version: int) -> int:
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ApiError(
            422,
            VALIDATION_FAILED,
            _INVALID_COMMAND_MESSAGE,
            details={"field": "expected_version", "constraint": "integer >= 0"},
        )
    return expected_version


# ---------------------------------------------------------------------------
# DTO builders (compact mirrors of the query surface for write echoes)
# ---------------------------------------------------------------------------


def _task_dto(task: domain.Task) -> dict[str, Any]:
    def _container(raw: str) -> Any:
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return []

    def _policy(raw: str) -> Any:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    return {
        "id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "description": task.description,
        "acceptance": _container(task.acceptance_json),
        "kind": task.kind,
        "priority": task.priority,
        "status": task.status,
        "risk": task.risk,
        "ownership": _container(task.ownership_json),
        "max_attempts": task.max_attempts,
        "version": task.version,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        # M3-04: durable pause gate + persisted execution policy.
        "paused": bool(task.paused),
        "execution_policy": _policy(task.execution_policy_json),
    }


def live_execution_policy(task: domain.Task) -> dict[str, Any]:
    """Parsed persisted execution policy (never raises on odd stored text)."""

    try:
        value = json.loads(task.execution_policy_json)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _command_dto(command: commands_core.Command) -> dict[str, Any]:
    return {
        "command_id": command.id,
        "idempotency_key": command.idempotency_key,
        "kind": command.kind,
        "state": command.state,
        "requested_at": command.requested_at,
        "updated_at": command.updated_at,
        "expires_at": command.expires_at,
        "note": command.note,
        "result": _loads(command.result_json),
    }


def _approval_dto(approval: verification.Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "command_id": approval.command_id,
        "verification_id": approval.verification_id,
        "task_id": approval.task_id,
        "attempt_id": approval.attempt_id,
        "attempt_no": approval.attempt_no,
        "task_version": approval.task_version,
        "artifact_digest": approval.artifact_digest,
        "proposed_action": approval.proposed_action,
        "decision": approval.decision,
        "actor": approval.actor,
        "reason": approval.reason,
        "created_at": approval.created_at,
        "current": bool(approval.current),
    }


# ---------------------------------------------------------------------------
# Request models (extra=forbid: unknown fields fail 422 uniformly, which is
# how critical-but-unsupported edits such as ownership/max_attempts are
# refused BEFORE any Core call — the adapter never silently drops a field)
# ---------------------------------------------------------------------------


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    title: str = Field(min_length=1, max_length=2048)
    description: str = Field(default="", max_length=16384)
    kind: str = Field(default="ship", min_length=1, max_length=32)
    priority: int = 50
    acceptance: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    ownership: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    risk: str = Field(default="normal", min_length=1, max_length=32)
    max_attempts: int = Field(default=3, ge=1, le=1000)
    dependencies: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class TaskEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    title: str | None = Field(default=None, max_length=2048)
    description: str | None = Field(default=None, max_length=16384)
    priority: int | None = None
    risk: str | None = Field(default=None, max_length=32)


class DependencyAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depends_on_id: str
    expected_version: int = Field(ge=0)


class TaskCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    expected_version: int = Field(ge=0)
    actor: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=1024)
    attempt_id: str | None = None


class TaskPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    delivery: str | None = Field(default=None, max_length=64)
    router: str | None = Field(default=None, max_length=256)
    verifier: str | None = Field(default=None, max_length=256)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    max_attempts: int | None = Field(default=None, ge=1, le=1000)


class CeoCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    project_id: str
    actor: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=1024)
    target_ceo_name: str = Field(default="ceo", min_length=1, max_length=32)
    task_id: str | None = None
    expected_task_version: int | None = Field(default=None, ge=0)


class SchedulerControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    actor: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=1024)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    actor: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=1024)
    expected_task_version: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Command service: the only writable boundary this API uses
# ---------------------------------------------------------------------------


class CommandService:
    """Durable write service behind the M3-04 Command routes.

    Every mutation opens one fresh writable :class:`TeamCraftConnection` and
    runs inside :func:`teamcraft.storage.transaction` (BEGIN IMMEDIATE +
    schema re-check under the write lock).  A missing database file is refused
    with a uniform 503 BEFORE connecting, so the API never creates an empty
    store as a side effect.  No method here can touch ``leader_lease``,
    dispatch an attempt, admit/send a Wake, or run a verifier: only the Core
    functions named in the module docstring are called.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._db_path = Path(database_path)

    @classmethod
    def for_project_root(cls, project_root: str | Path) -> "CommandService":
        runtime = Path(project_root) / fresh_start.RUNTIME_DIRNAME
        return cls(runtime / fresh_start.DATABASE_FILENAME)

    @property
    def database_path(self) -> Path:
        return self._db_path

    # -- connection boundaries -------------------------------------------------

    @contextlib.contextmanager
    def _write(self) -> Iterator[TeamCraftConnection]:
        if not self._db_path.exists():
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE)
        try:
            conn = connect(self._db_path)
        except (StorageError, sqlite3.Error):
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE) from None
        try:
            with transaction(conn):
                yield conn
        except ApiError:
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def _read(self) -> Iterator[TeamCraftConnection]:
        if not self._db_path.exists():
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE)
        try:
            conn = connect(self._db_path)
        except (StorageError, sqlite3.Error):
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE) from None
        try:
            yield conn
        finally:
            conn.close()

    # -- shared runners ----------------------------------------------------------

    @staticmethod
    def _submit(
        conn: TeamCraftConnection,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
        evidence: dict[str, Any],
    ) -> commands_core.Submission:
        return commands_core.submit_command(
            conn,
            idempotency_key=idempotency_key,
            kind=kind,
            payload_json=_dump(payload),
            evidence_json=_dump(evidence),
        )

    @staticmethod
    def _replay_or_reuse(
        submission: commands_core.Submission,
    ) -> tuple[commands_core.Command, Any, bool]:
        """Either replay a confirmed command's stored result or refuse."""

        command = submission.command
        if submission.reused:
            if command.state != "confirmed":
                raise ApiError(
                    409,
                    CONFLICT,
                    _CONFLICT_MESSAGE,
                    details={
                        "reason_code": "COMMAND_NOT_COMPLETED",
                        "command_id": command.id,
                        "state": command.state,
                    },
                )
            return command, _loads(command.result_json), True
        return command, None, False

    @staticmethod
    def _command_by_key(
        conn: TeamCraftConnection, key: str
    ) -> commands_core.Command | None:
        """The Command row a given idempotency key currently resolves to."""

        row = conn.execute(
            "SELECT id FROM commands WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return None if row is None else commands_core.get_command(
            conn, str(row[0])
        )

    @contextlib.contextmanager
    def _lease(
        self, conn: TeamCraftConnection, project_id: str
    ) -> Iterator[leader.Lease]:
        """Short-lived API lease taken through the normal Leader protocol.

        ``acquire_or_renew`` is the SAME admission primitive the runtime
        scheduler uses: while another holder has a live lease this raises
        :class:`teamcraft.leader.LeaderBusyError` (mapped to 409
        ``LEADER_BUSY``) and nothing is applied.  The lease is released on
        exit, handing authority back for the next scheduler tick; it never
        renews or releases anyone else's lease.
        """

        lease = scheduler.acquire_or_renew(
            conn, project_id, API_LEASE_HOLDER,
            ttl_seconds=API_LEASE_TTL_SECONDS,
        )
        try:
            yield lease
        finally:
            scheduler.release(conn, lease)

    @staticmethod
    def _require_task(conn: TeamCraftConnection, task_id: str) -> domain.Task:
        return domain.get_task(conn, task_id)

    @staticmethod
    def _require_version(task: domain.Task, expected_version: int) -> None:
        if task.version != expected_version:
            raise VersionConflictError(
                f"task {task.id} version conflict: expected {expected_version},"
                f" found {task.version}",
                entity_id=task.id,
                expected=expected_version,
                found=task.version,
            )

    @staticmethod
    def _stored_task_id(stored: Any) -> str | None:
        """Task id from a confirmed command's stored result (if well-formed)."""

        if isinstance(stored, dict) and isinstance(stored.get("task_id"), str):
            return str(stored["task_id"])
        return None

    @staticmethod
    def _require_non_terminal(task: domain.Task, action: str) -> None:
        if task.status in TASK_STATUSES_TERMINAL:
            raise IllegalTransitionError(
                f"task {task.id} is terminal ({task.status}); {action!r}"
                " cannot be requested",
                entity="task",
                current=task.status,
                event=action,
                reason_code="TASK_TERMINAL",
            )

    @staticmethod
    def _require_dependency_editable(task: domain.Task) -> None:
        """Same rule Core enforces, surfaced with a precise conflict code."""

        if task.status not in domain.DEPENDENCY_EDITABLE_STATUSES:
            raise IllegalTransitionError(
                f"task {task.id} dependencies are not editable while"
                f" {task.status!r}",
                entity="task",
                current=task.status,
                event="edit_dependencies",
                reason_code="DEPENDENCY_EDIT_NOT_ALLOWED",
            )

    @staticmethod
    def _require_dependency_target_live(conn: TeamCraftConnection,
                                       depends_on_id: str) -> None:
        """Existence + liveness of the upstream Task (Core re-checks both).

        ``domain.add_dependency`` refuses a cancelled/failed upstream with a
        Core ``ValidationError``; surfacing it as 409 (state conflict, not
        malformed input) keeps the conflict semantics honest without adding
        any new rule.
        """

        target = domain.get_task(conn, depends_on_id)  # 404 first
        if target.status in ("cancelled", "failed"):
            raise IllegalTransitionError(
                f"task {depends_on_id} is {target.status}; nothing to depend on",
                entity="task",
                current=target.status,
                event="add_dependency",
                reason_code="DEPENDENCY_TARGET_TERMINAL",
            )

    # -- synchronous local transitions ---------------------------------------------

    def create_task(self, payload: TaskCreateRequest, key: str) -> tuple[int, dict[str, Any], bool]:
        project_id = _entity_id(payload.project_id, "project_id")
        title = _text(payload.title, "title", required=True, max_bytes=MAX_TITLE_BYTES)
        description = _text(
            payload.description, "description", required=False,
            max_bytes=MAX_DESCRIPTION_BYTES,
        )
        for index, item in enumerate(payload.acceptance):
            _text(item, f"acceptance[{index}]", required=True, max_bytes=MAX_ITEM_BYTES)
        for index, claim in enumerate(payload.ownership):
            try:
                scheduler._normalize_claim(claim)
            except ValueError:
                raise ApiError(
                    422,
                    VALIDATION_FAILED,
                    _INVALID_COMMAND_MESSAGE,
                    details={
                        "field": f"ownership[{index}]",
                        "constraint": "project-relative POSIX path with optional trailing /**",
                    },
                ) from None
        dependencies: list[str] = []
        for dep in payload.dependencies:
            dep_id = _entity_id(dep, "dependencies[]")
            if dep_id not in dependencies:
                dependencies.append(dep_id)

        with self._write() as conn:
            domain.get_project(conn, project_id)  # 404 BEFORE any command row
            submission = self._submit(
                conn,
                kind=KIND_TASK_CREATE,
                idempotency_key=key,
                payload={
                    "action": "create_task",
                    "project_id": project_id,
                    "title": title,
                    "description": description,
                    "kind": payload.kind,
                    "priority": payload.priority,
                    "risk": payload.risk,
                    "max_attempts": payload.max_attempts,
                    "acceptance": list(payload.acceptance),
                    "ownership": list(payload.ownership),
                    "dependencies": list(dependencies),
                },
                # M3-04: create commands carry a project stamp so submitted/
                # claimed/confirmed events are project-traceable from the
                # first moment (the Task row may not exist yet at submit).
                evidence={"project_id": project_id},
            )
            command, stored, replayed = self._replay_or_reuse(submission)
            if replayed:
                body: dict[str, Any] = {
                    "command": _command_dto(command),
                    "replayed": True,
                }
                created_id = self._stored_task_id(stored)
                if created_id is not None:
                    live = domain.get_task(conn, created_id)
                    body["task"] = _task_dto(live)
                    body["dependencies"] = domain.dependencies_of(conn, live.id)
                return 201, body, True
            commands_core.claim_command(conn, command.id)
            task = domain.create_task(
                conn,
                project_id,
                title,
                description=description,
                kind=payload.kind,
                priority=payload.priority,
                acceptance_json=_dump(list(payload.acceptance)),
                ownership_json=_dump(list(payload.ownership)),
                risk=payload.risk,
                max_attempts=payload.max_attempts,
            )
            for dep_id in dependencies:
                live = domain.get_task(conn, task.id)
                domain.add_dependency(conn, task.id, dep_id, expected_version=live.version)
                task = domain.get_task(conn, task.id)
            body = {
                "task": _task_dto(task),
                "dependencies": domain.dependencies_of(conn, task.id),
                "command": _command_dto(
                    commands_core.confirm_command(
                        conn,
                        command.id,
                        result_json=_dump({"task_id": task.id, "version": task.version}),
                    )
                ),
                "replayed": False,
            }
            return 201, body, False

    def edit_task(
        self, task_id: str, payload: TaskEditRequest, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        task_id = _entity_id(task_id, "task_id")
        expected_version = _version(payload.expected_version)
        if (
            payload.title is None
            and payload.description is None
            and payload.priority is None
            and payload.risk is None
        ):
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_COMMAND_MESSAGE,
                details={
                    "field": "body",
                    "constraint": "at least one of title/description/priority/risk",
                },
            )
        fields: dict[str, Any] = {}
        if payload.title is not None:
            fields["title"] = _text(payload.title, "title", required=True, max_bytes=MAX_TITLE_BYTES)
        if payload.description is not None:
            fields["description"] = _text(
                payload.description, "description", required=False,
                max_bytes=MAX_DESCRIPTION_BYTES,
            )
        if payload.priority is not None:
            fields["priority"] = payload.priority
        if payload.risk is not None:
            fields["risk"] = _text(payload.risk, "risk", required=True, max_bytes=32)

        payload_body = {"action": "edit_task", "task_id": task_id,
                       "task_version": expected_version, **fields}
        evidence = {"task_id": task_id, "task_version": expected_version}
        with self._write() as conn:
            task = self._require_task(conn, task_id)
            submission = self._submit(
                conn,
                kind=KIND_TASK_UPDATE,
                idempotency_key=key,
                payload=payload_body,
                evidence=evidence,
            )
            command, stored, replayed = self._replay_or_reuse(submission)
            if replayed:
                body = {
                    "command": _command_dto(command),
                    "replayed": True,
                }
                edited_id = self._stored_task_id(stored)
                if edited_id is not None:
                    live = domain.get_task(conn, edited_id)
                    body["task"] = _task_dto(live)
                if isinstance(stored, dict) and "version" in stored:
                    body["evidence"] = {
                        "previous_version": stored.get("previous_version"),
                        "version": stored.get("version"),
                    }
                return 200, body, True
            self._require_version(task, expected_version)
            commands_core.claim_command(conn, command.id)
            # CAS bump inside update_task IS the new evidence version; the
            # same transaction expires PENDING commands bound to older
            # versions (domain._cas_task -> invalidate_stale_commands_for_task).
            updated = domain.update_task(
                conn, task_id, expected_version=expected_version, **fields
            )
            confirmed = commands_core.confirm_command(
                conn,
                command.id,
                result_json=_dump({
                    "task_id": updated.id,
                    "version": updated.version,
                    "previous_version": task.version,
                }),
            )
            result = {
                "task": _task_dto(updated),
                "evidence": {"previous_version": task.version, "version": updated.version},
                "command": _command_dto(confirmed),
                "replayed": False,
            }
            return 200, result, False

    def add_dependency(
        self, task_id: str, payload: DependencyAddRequest, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        task_id = _entity_id(task_id, "task_id")
        depends_on_id = _entity_id(payload.depends_on_id, "depends_on_id")
        expected_version = _version(payload.expected_version)
        evidence = {"task_id": task_id, "task_version": expected_version}
        with self._write() as conn:
            task = self._require_task(conn, task_id)
            submission = self._submit(
                conn,
                kind=KIND_TASK_DEPENDENCY_ADD,
                idempotency_key=key,
                payload={"action": "add_dependency", "task_id": task_id,
                       "depends_on_id": depends_on_id, "task_version": expected_version},
                evidence=evidence,
            )
            command, stored, replayed = self._replay_or_reuse(submission)
            if replayed:
                return 200, {
                    "task_id": task_id,
                    "depends_on_id": depends_on_id,
                    "added": stored.get("added") if isinstance(stored, dict) else None,
                    "task": _task_dto(domain.get_task(conn, task_id)),
                    "command": _command_dto(command),
                    "replayed": True,
                }, True
            self._require_version(task, expected_version)
            self._require_dependency_editable(task)
            self._require_dependency_target_live(conn, depends_on_id)
            commands_core.claim_command(conn, command.id)
            added = domain.add_dependency(
                conn, task_id, depends_on_id, expected_version=expected_version
            )
            updated = domain.get_task(conn, task_id)
            result = {
                "task_id": task_id,
                "depends_on_id": depends_on_id,
                "added": added,
                "task": _task_dto(updated),
                "command": _command_dto(
                    commands_core.confirm_command(
                        conn,
                        command.id,
                        result_json=_dump({"added": added, "version": updated.version}),
                    )
                ),
                "replayed": False,
            }
            return 200, result, False

    def remove_dependency(
        self, task_id: str, depends_on_id: str, expected_version: int, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        task_id = _entity_id(task_id, "task_id")
        depends_on_id = _entity_id(depends_on_id, "depends_on_id")
        expected_version = _version(expected_version)
        evidence = {"task_id": task_id, "task_version": expected_version}
        with self._write() as conn:
            task = self._require_task(conn, task_id)
            submission = self._submit(
                conn,
                kind=KIND_TASK_DEPENDENCY_REMOVE,
                idempotency_key=key,
                payload={"action": "remove_dependency", "task_id": task_id,
                       "depends_on_id": depends_on_id, "task_version": expected_version},
                evidence=evidence,
            )
            command, stored, replayed = self._replay_or_reuse(submission)
            if replayed:
                return 200, {
                    "task_id": task_id,
                    "depends_on_id": depends_on_id,
                    "removed": stored.get("removed") if isinstance(stored, dict) else None,
                    "task": _task_dto(domain.get_task(conn, task_id)),
                    "command": _command_dto(command),
                    "replayed": True,
                }, True
            self._require_version(task, expected_version)
            self._require_dependency_editable(task)
            commands_core.claim_command(conn, command.id)
            removed = domain.remove_dependency(
                conn, task_id, depends_on_id, expected_version=expected_version
            )
            updated = domain.get_task(conn, task_id)
            result = {
                "task_id": task_id,
                "depends_on_id": depends_on_id,
                "removed": removed,
                "task": _task_dto(updated),
                "command": _command_dto(
                    commands_core.confirm_command(
                        conn,
                        command.id,
                        result_json=_dump({"removed": removed, "version": updated.version}),
                    )
                ),
                "replayed": False,
            }
            return 200, result, False

    def task_command(
        self, task_id: str, payload: TaskCommandRequest, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        action = payload.action.strip().lower()
        if action not in TASK_COMMAND_ACTIONS:
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_COMMAND_MESSAGE,
                details={"field": "action",
                       "constraint": f"one of {sorted(TASK_COMMAND_ACTIONS)}"},
            )
        task_id = _entity_id(task_id, "task_id")
        expected_version = _version(payload.expected_version)
        actor = _text(payload.actor, "actor", required=True, max_bytes=MAX_ACTOR_BYTES)
        reason = _text(payload.reason, "reason", required=True, max_bytes=MAX_REASON_BYTES)
        attempt_id = (
            _entity_id(payload.attempt_id, "attempt_id")
            if payload.attempt_id is not None
            else None
        )

        with self._write() as conn:
            task = self._require_task(conn, task_id)

            if action in ("pause", "resume"):
                kind = KIND_TASK_PAUSE if action == "pause" else KIND_TASK_RESUME
                evidence = {"task_id": task_id,
                          "task_version": expected_version}
                submission = self._submit(
                    conn,
                    kind=kind,
                    idempotency_key=key,
                    payload={"action": action, "task_id": task_id,
                           "task_version": expected_version,
                           "actor": actor, "reason": reason},
                    evidence=evidence,
                )
                command, stored, replayed = self._replay_or_reuse(submission)
                if replayed:
                    body: dict[str, Any] = {
                        "execution": "applied",
                        "command": _command_dto(command),
                        "replayed": True,
                    }
                    applied_id = self._stored_task_id(stored)
                    if applied_id is not None:
                        live = domain.get_task(conn, applied_id)
                        body["task"] = _task_dto(live)
                        body["paused"] = bool(live.paused)
                    return 200, body, True
                # Guards run only for FRESH submissions (a replay of an
                # already-applied pause must keep succeeding even though the
                # world moved on): optimistic version + lifecycle gate, then
                # the enforced domain transition in the same transaction.
                self._require_version(task, expected_version)
                self._require_non_terminal(task, action)
                commands_core.claim_command(conn, command.id)
                if action == "pause":
                    updated = domain.pause_task(
                        conn, task_id, expected_version=expected_version,
                        actor=actor, reason=reason,
                    )
                else:
                    updated = domain.resume_task(
                        conn, task_id, expected_version=expected_version,
                        actor=actor, reason=reason,
                    )
                result = {
                    "task": _task_dto(updated),
                    "paused": bool(updated.paused),
                    "execution": "applied",
                    "command": _command_dto(
                        commands_core.confirm_command(
                            conn,
                            command.id,
                            result_json=_dump({
                                "task_id": updated.id,
                                "status": updated.status,
                                "paused": bool(updated.paused),
                                "version": updated.version,
                            }),
                        )
                    ),
                    "replayed": False,
                }
                return 200, result, False

            if action == "retry":
                # Retry is APPLIED through Core's own approval transition
                # (recovery.approve_retry) under a short-lived API lease:
                # occupying-attempt, attempt-cap and prior-attempt-state
                # invariants and the retry_approved/human_reconciled task
                # event all belong to Core; the durable Command
                # (kind recovery.retry_approved) is written by Core under
                # the caller's idempotency key.
                existing = self._command_by_key(conn, key)
                if existing is None:
                    # Version gate for FRESH submissions only (a confirmed
                    # replay must keep succeeding).
                    self._require_version(task, expected_version)
                if attempt_id is not None:
                    attempt = domain.get_attempt(conn, attempt_id)
                    if attempt.task_id != task_id:
                        raise ConflictError(
                            "retry request attempt does not belong to the task"
                        )
                with self._lease(conn, task.project_id) as lease:
                    updated = recovery.approve_retry(
                        conn, task_id, actor=actor, reason=reason,
                        idempotency_key=key, lease=lease,
                    )
                command = self._command_by_key(conn, key)
                assert command is not None
                replayed = existing is not None
                return 200, {
                    "task": _task_dto(updated),
                    "execution": "applied",
                    "command": _command_dto(command),
                    "replayed": replayed,
                }, replayed

            if action == "cancel":
                occupying = (
                    domain.get_attempt(conn, attempt_id)
                    if attempt_id is not None
                    else domain.occupying_attempt(conn, task_id)
                )
                if occupying is not None and occupying.task_id != task_id:
                    raise ConflictError("cancel attempt does not belong to the task")
                if occupying is None:
                    # No live execution exists: cancellation is a purely local
                    # domain transition (Core still refuses if a slot holder
                    # appeared under the lock).
                    submission = self._submit(
                        conn,
                        kind=KIND_TASK_CANCEL_APPLIED,
                        idempotency_key=key,
                        payload={"action": "cancel", "task_id": task_id,
                               "task_version": expected_version,
                               "actor": actor, "reason": reason},
                        evidence={"task_id": task_id, "task_version": expected_version},
                    )
                    command, stored, replayed = self._replay_or_reuse(submission)
                    if replayed:
                        body = {
                            "execution": "applied",
                            "command": _command_dto(command),
                            "replayed": True,
                        }
                        cancelled_id = self._stored_task_id(stored)
                        if cancelled_id is not None:
                            body["task"] = _task_dto(domain.get_task(conn, cancelled_id))
                        return 200, body, True
                    self._require_version(task, expected_version)
                    commands_core.claim_command(conn, command.id)
                    updated = domain.apply_task_event(
                        conn, task_id, "user_cancel",
                        expected_version=expected_version,
                    )
                    result = {
                        "task": _task_dto(updated),
                        "execution": "applied",
                        "command": _command_dto(
                            commands_core.confirm_command(
                                conn,
                                command.id,
                                result_json=_dump({
                                    "task_id": updated.id,
                                    "status": updated.status,
                                    "version": updated.version,
                                }),
                            )
                        ),
                        "replayed": False,
                    }
                    return 200, result, False
                # A live attempt exists: stopping that process is external —
                # record the durable recovery command; a fenced executor must
                # carry it out (confirm_cancellation / resolve_unknown).
                return self._recovery_request(
                    conn, task, task_id, action="cancel", actor=actor,
                    reason=reason, key=key, expected_version=expected_version,
                    attempt_id=occupying.id,
                )

            # wait / reconcile: attempt-scoped recovery requests through Core.
            return self._recovery_request(
                conn, task, task_id, action=action, actor=actor, reason=reason,
                key=key, expected_version=expected_version, attempt_id=attempt_id,
            )

    def _recovery_request(
        self,
        conn: TeamCraftConnection,
        task: domain.Task,
        task_id: str,
        *,
        action: str,
        actor: str,
        reason: str,
        key: str,
        expected_version: int,
        attempt_id: str | None,
    ) -> tuple[int, dict[str, Any], bool]:
        """Idempotent pass-through to ``recovery.request_action``.

        The pre-probe exists only to label a genuine replay: the durable
        dedupe and content-conflict checks are ``request_action``'s own
        (same key + different content raises through Core, nothing is
        written). The version gate applies only to FRESH submissions.
        """

        probe = conn.execute(
            "SELECT id FROM commands WHERE idempotency_key = ?", (key,)
        ).fetchone()
        existing_id = str(probe[0]) if probe is not None else None
        if existing_id is None:
            self._require_version(task, expected_version)
        command = recovery.request_action(
            conn,
            task_id=task_id,
            action=action,
            actor=actor,
            reason=reason,
            idempotency_key=key,
            attempt_id=attempt_id,
        )
        replayed = existing_id is not None and command.id == existing_id
        return 202, {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "execution": "recorded",
            "command": _command_dto(command),
            "replayed": replayed,
        }, replayed

    def update_policy(
        self, task_id: str, payload: TaskPolicyRequest, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        task_id = _entity_id(task_id, "task_id")
        expected_version = _version(payload.expected_version)
        requested = {
            name: value
            for name, value in (
                ("delivery", payload.delivery),
                ("router", payload.router),
                ("verifier", payload.verifier),
                ("timeout_seconds", payload.timeout_seconds),
                ("max_attempts", payload.max_attempts),
            )
            if value is not None
        }
        if not requested:
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_COMMAND_MESSAGE,
                details={
                    "field": "body",
                    "constraint": "at least one of delivery/router/verifier/timeout_seconds/max_attempts",
                },
            )
        with self._write() as conn:
            task = self._require_task(conn, task_id)
            submission = self._submit(
                conn,
                kind=KIND_TASK_POLICY_UPDATE,
                idempotency_key=key,
                payload={"action": "update_execution_policy", "task_id": task_id,
                       "task_version": expected_version, "policy": requested},
                evidence={"task_id": task_id, "task_version": expected_version},
            )
            command, stored, replayed = self._replay_or_reuse(submission)
            if replayed:
                body: dict[str, Any] = {
                    "execution": "applied",
                    "command": _command_dto(command),
                    "replayed": True,
                }
                policy_id = self._stored_task_id(stored)
                if policy_id is not None:
                    live = domain.get_task(conn, policy_id)
                    body["task"] = _task_dto(live)
                    body["policy"] = live_execution_policy(live)
                return 200, body, True
            # A policy change is a critical edit: version-guard first, then
            # the CAS persists it and (via the version bump) expires any
            # PENDING commands bound to the older evidence.
            self._require_version(task, expected_version)
            commands_core.claim_command(conn, command.id)
            updated = domain.set_execution_policy(
                conn, task_id, expected_version=expected_version,
                policy=requested,
            )
            merged = live_execution_policy(updated)
            result = {
                "task": _task_dto(updated),
                "policy": merged,
                "execution": "applied",
                "command": _command_dto(
                    commands_core.confirm_command(
                        conn,
                        command.id,
                        result_json=_dump({
                            "task_id": updated.id,
                            "version": updated.version,
                            "policy": merged,
                        }),
                    )
                ),
                "replayed": False,
            }
            return 200, result, False

    # -- async intents (recorded; consumed by the fenced executor) --------------

    def ceo_command(
        self, payload: CeoCommandRequest, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        """Queue a durable manual CEO request; NEVER send a prompt (roadmap 3).

        Writes exactly one ``ceo.wake_request`` Command (bounded TTL so the
        intent cannot stay pending forever) plus one linked
        ``ceo.wake_requested`` Event via
        :func:`teamcraft.wake.request_ceo_attention`.  The Event kind is not
        on the wake-ingestion ignore list, so the next fenced scheduler tick
        folds it into an ordinary merged Wake candidate — admission, CEO
        initialization and lease fencing gate delivery exactly like every
        automated Wake.  Nothing here admits, sends, or resumes.
        """

        action = payload.action.strip().lower()
        if action not in CEO_ACTIONS:
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_COMMAND_MESSAGE,
                details={"field": "action", "constraint": f"one of {sorted(CEO_ACTIONS)}"},
            )
        project_id = _entity_id(payload.project_id, "project_id")
        actor = _text(payload.actor, "actor", required=True, max_bytes=MAX_ACTOR_BYTES)
        reason = _text(payload.reason, "reason", required=True, max_bytes=MAX_REASON_BYTES)
        try:
            target = wake._validate_target(payload.target_ceo_name)
        except ValidationError:
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_COMMAND_MESSAGE,
                details={"field": "target_ceo_name",
                       "constraint": "normalized TeamCraft member name"},
            ) from None
        bound_task_id: str | None = None
        bound_version: int | None = None
        with self._write() as conn:
            domain.get_project(conn, project_id)  # 404 before any command row
            if payload.task_id is not None:
                bound_task_id = _entity_id(payload.task_id, "task_id")
                if payload.expected_task_version is None:
                    raise ApiError(
                        422,
                        VALIDATION_FAILED,
                        _INVALID_COMMAND_MESSAGE,
                        details={"field": "expected_task_version",
                               "constraint": "required when task_id is bound"},
                    )
                bound_version = _version(payload.expected_task_version)
                task = self._require_task(conn, bound_task_id)
                if task.project_id != project_id:
                    raise ConflictError(
                        "CEO request task does not belong to this project"
                    )
                # The version gate applies only to FRESH submissions (a
                # genuine replay of an already-queued request keeps
                # succeeding even though the Task may have moved on since).
                if self._command_by_key(conn, key) is None:
                    self._require_version(task, bound_version)
            command, replayed = wake.request_ceo_attention(
                conn,
                project_id,
                actor=actor,
                reason=reason,
                idempotency_key=key,
                target_ceo_name=target,
                task_id=bound_task_id,
                task_version=bound_version,
            )
            return 202, {
                "execution": "queued",
                "command": _command_dto(command),
                "replayed": replayed,
            }, replayed

    def scheduler_control(
        self, action: str, payload: SchedulerControlRequest, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        """Apply a durable dispatch pause/resume through the Scheduler plane.

        One transaction writes Command + control row + linked event via
        :func:`teamcraft.scheduler.pause_schedule` / :func:`resume_schedule`;
        every planning/dispatch snapshot afterwards consults the control row
        and defers READY tasks with reason ``scheduler_paused`` until the
        resume lands.  The ``leader_lease`` table is neither read nor
        written: pausing never steals, releases, or bypasses a live Leader's
        fencing rights — it only withholds NEW dispatch decisions.
        """

        if action not in ("pause", "resume"):  # pragma: no cover - route-guarded
            raise ApiError(
                422, VALIDATION_FAILED, _INVALID_COMMAND_MESSAGE,
                details={"field": "action", "constraint": "pause or resume"},
            )
        project_id = _entity_id(payload.project_id, "project_id")
        actor = _text(payload.actor, "actor", required=True, max_bytes=MAX_ACTOR_BYTES)
        reason = _text(payload.reason, "reason", required=True, max_bytes=MAX_REASON_BYTES)
        with self._write() as conn:
            domain.get_project(conn, project_id)  # 404 before any command row
            if action == "pause":
                state, command, replayed = scheduler.pause_schedule(
                    conn, project_id, actor=actor, reason=reason,
                    idempotency_key=key,
                )
            else:
                state, command, replayed = scheduler.resume_schedule(
                    conn, project_id, actor=actor, reason=reason,
                    idempotency_key=key,
                )
            return 200, {
                "scheduler": state,
                "execution": "applied",
                "command": _command_dto(command),
                "replayed": replayed,
            }, replayed

    def get_scheduler_state(self, project_id: str) -> dict[str, Any]:
        """Observable pause state (roadmap 4): lease-free read of the plane."""

        project_id = _entity_id(project_id, "project_id")
        with self._read() as conn:
            domain.get_project(conn, project_id)  # 404
            state = scheduler.scheduler_state(conn, project_id)
        if state is None:
            state = {"project_id": project_id, "paused": False, "actor": "",
                    "reason": "", "version": 0, "updated_at": None}
        return {"scheduler": state}

    def approval_decision(
        self, verification_id: str, payload: ApprovalDecisionRequest, key: str
    ) -> tuple[int, dict[str, Any], bool]:
        """Apply an approval decision through ``verification.decide_approval``.

        Runs under the API's own short Leader lease; Core re-validates every
        safety invariant (live lease, latest-Verification binding, evidence
        freshness, pass-only acceptance, scout-only acceptance, and
        per-verification dedupe where a second, different decision under the
        same key conflicts 409).  Contention with the runtime Leader fails
        409 ``LEADER_BUSY`` with nothing written.
        """

        verification_id = _entity_id(verification_id, "verification_id")
        decision = payload.decision.strip().lower()
        if decision not in APPROVAL_DECISIONS:
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_COMMAND_MESSAGE,
                details={"field": "decision", "constraint": "accept or reject"},
            )
        actor = _text(payload.actor, "actor", required=True, max_bytes=MAX_ACTOR_BYTES)
        reason = _text(payload.reason, "reason", required=True, max_bytes=MAX_REASON_BYTES)
        with self._write() as conn:
            record = verification.get_verification(conn, verification_id)  # 404
            task = self._require_task(conn, record.task_id)
            has_decision = conn.execute(
                "SELECT 1 FROM approvals WHERE verification_id = ?",
                (record.id,),
            ).fetchone() is not None
            if not has_decision:
                # Fresh decision: the caller must bind the current Task
                # evidence; an already-recorded decision keeps replaying.
                self._require_version(task, payload.expected_task_version)
            with self._lease(conn, task.project_id) as lease:
                approval = verification.decide_approval(
                    conn, verification_id, decision=decision, actor=actor,
                    reason=reason, idempotency_key=key, lease=lease,
                )
            command = commands_core.get_command(conn, approval.command_id)
            replayed = has_decision
            return 200, {
                "approval": _approval_dto(approval),
                "command": _command_dto(command),
                "replayed": replayed,
            }, replayed

    # -- reads ------------------------------------------------------------------------

    def get_command(self, command_id: str) -> dict[str, Any]:
        command_id = _entity_id(command_id, "command_id")
        with self._read() as conn:
            command = commands_core.get_command(conn, command_id)
        return {"command": _command_dto(command)}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_command_router(
    service: CommandService,
    *,
    secret_provider: Callable[[], Iterable[str]] = tuple,
) -> APIRouter:
    """Authenticated M3-04 Command routes, mounted on the guarded M3-01 app.

    Uniform failure mapping (identical body shape on every route):

    * missing/blank/oversized ``Idempotency-Key``    -> 422 VALIDATION_FAILED
    * malformed input                                 -> 422 VALIDATION_FAILED
    * unknown entity                                 -> 404 NOT_FOUND
    * stale Task/evidence version                   -> 409 CONFLICT
      (details carry ``reason_code``/expected/found)
    * idempotency key reused with different content -> 409 CONFLICT
      (details carry ``existing_command_id``)
    * dependency cycle                              -> 409 CONFLICT
      (details carry the closed ``cycle`` path)
    * illegal transition / occupied slot            -> 409 CONFLICT
    * approval tier while the runtime holds the lease -> 409 CONFLICT
      (details carry ``reason_code: LEADER_BUSY``)
    * store missing/too-new/unusable                -> 503 SERVICE_UNAVAILABLE

    Responses are redacted; raw Core/SQL text never echoes back.
    """

    router = APIRouter(prefix="/api/v1")

    def _secrets() -> tuple[str, ...]:
        try:
            return tuple(secret_provider())
        except Exception:  # pragma: no cover - defensive for providers
            return ()

    def _map(action: Callable[[], Any]) -> Any:
        try:
            payload = action()
        except ApiError:
            raise
        except NotFoundError:
            raise ApiError(404, NOT_FOUND, _NOT_FOUND_MESSAGE) from None
        except IdempotencyConflictError as exc:
            raise ApiError(
                409,
                CONFLICT,
                _CONFLICT_MESSAGE,
                details={
                    "reason_code": "IDEMPOTENCY_KEY_REUSED",
                    "existing_command_id": exc.existing_command_id,
                },
            ) from None
        except DependencyCycleError as exc:
            raise ApiError(
                409,
                CONFLICT,
                _CONFLICT_MESSAGE,
                details={"reason_code": "DEPENDENCY_CYCLE", "cycle": exc.cycle},
            ) from None
        except VersionConflictError as exc:
            raise ApiError(
                409,
                CONFLICT,
                _CONFLICT_MESSAGE,
                details={
                    "reason_code": "VERSION_CONFLICT",
                    "entity_id": exc.entity_id,
                    "expected": exc.expected,
                    "found": exc.found,
                },
            ) from None
        except CommandStateConflictError as exc:
            raise ApiError(
                409,
                CONFLICT,
                _CONFLICT_MESSAGE,
                details={
                    "reason_code": "COMMAND_STATE_CONFLICT",
                    "command_id": exc.command_id,
                    "expected_state": exc.expected_state,
                    "found_state": exc.found_state,
                },
            ) from None
        except AttemptSlotOccupiedError as exc:
            raise ApiError(
                409,
                CONFLICT,
                _CONFLICT_MESSAGE,
                details={
                    "reason_code": "ATTEMPT_SLOT_OCCUPIED",
                    "task_id": exc.task_id,
                    "occupying_attempt_id": exc.occupying_id,
                },
            ) from None
        except IllegalTransitionError as exc:
            raise ApiError(
                409,
                CONFLICT,
                _CONFLICT_MESSAGE,
                details={
                    "reason_code": exc.reason_code,
                    "entity": exc.entity,
                    "current_status": exc.current,
                    "event": exc.event,
                },
            ) from None
        except leader.LeaderBusyError as exc:
            # The runtime Leader holds the project lease: the API refuses to
            # write rather than bypassing fencing (approval-tier routes).
            raise ApiError(
                409, CONFLICT, _CONFLICT_MESSAGE,
                details={
                    "reason_code": "LEADER_BUSY",
                    "scope": exc.scope,
                    "holder_id": exc.holder_id,
                    "epoch": exc.epoch,
                    "expires_at": exc.expires_at,
                },
            ) from None
        except leader.StaleLeaderError:
            raise ApiError(
                409, CONFLICT, _CONFLICT_MESSAGE,
                details={"reason_code": "LEADER_STALE"},
            ) from None
        except ConflictError:
            raise ApiError(
                409, CONFLICT, _CONFLICT_MESSAGE,
                details={"reason_code": "CONFLICT"},
            ) from None
        except ValidationError:
            raise ApiError(422, VALIDATION_FAILED, _INVALID_COMMAND_MESSAGE) from None
        except (SchemaOutOfDateError, StorageError):
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE) from None
        except sqlite3.Error:
            # Driver text can embed paths; surface only the uniform shape.
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE) from None
        return payload

    def _run(action: Callable[[], Any]) -> Any:
        return redact_value(_map(action), _secrets())

    def _respond(
        action: Callable[[], tuple[int, dict[str, Any], bool]]
    ) -> JSONResponse:
        status, body, replayed = _map(action)
        response = JSONResponse(status_code=status, content=redact_value(body, _secrets()))
        if replayed:
            response.headers[REPLAY_HEADER] = "true"
        return response

    # -- Tasks -------------------------------------------------------------------

    @router.post("/tasks")
    def create_task(request: Request, payload: TaskCreateRequest) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.create_task(payload, key))

    @router.patch("/tasks/{task_id}")
    def edit_task(task_id: str, request: Request, payload: TaskEditRequest) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.edit_task(task_id, payload, key))

    @router.post("/tasks/{task_id}/dependencies")
    def add_dependency(
        task_id: str, request: Request, payload: DependencyAddRequest
    ) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.add_dependency(task_id, payload, key))

    @router.delete("/tasks/{task_id}/dependencies/{depends_on_id}")
    def remove_dependency(
        task_id: str,
        depends_on_id: str,
        request: Request,
        expected_version: int = Query(..., ge=0),
    ) -> Any:
        key = _idempotency_key(request)
        return _respond(
            lambda: service.remove_dependency(task_id, depends_on_id, expected_version, key)
        )

    @router.post("/tasks/{task_id}/commands")
    def task_command(
        task_id: str, request: Request, payload: TaskCommandRequest
    ) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.task_command(task_id, payload, key))

    @router.post("/tasks/{task_id}/policy")
    def update_policy(
        task_id: str, request: Request, payload: TaskPolicyRequest
    ) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.update_policy(task_id, payload, key))

    # -- CEO / scheduler / approvals ----------------------------------------------

    @router.post("/ceo/commands")
    def ceo_command(request: Request, payload: CeoCommandRequest) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.ceo_command(payload, key))

    @router.post("/scheduler/pause")
    def scheduler_pause(request: Request, payload: SchedulerControlRequest) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.scheduler_control("pause", payload, key))

    @router.post("/scheduler/resume")
    def scheduler_resume(request: Request, payload: SchedulerControlRequest) -> Any:
        key = _idempotency_key(request)
        return _respond(lambda: service.scheduler_control("resume", payload, key))

    @router.get("/scheduler/state")
    def scheduler_state(
        request: Request, project_id: str = Query(..., min_length=1),
    ) -> Any:
        return _run(lambda: service.get_scheduler_state(project_id))

    @router.post("/verifications/{verification_id}/approvals")
    def approval_decision(
        verification_id: str, request: Request, payload: ApprovalDecisionRequest
    ) -> Any:
        key = _idempotency_key(request)
        return _respond(
            lambda: service.approval_decision(verification_id, payload, key)
        )

    # -- Command status ------------------------------------------------------------

    @router.get("/commands/{command_id}")
    def get_command(command_id: str) -> Any:
        return _run(lambda: service.get_command(command_id))

    return router
