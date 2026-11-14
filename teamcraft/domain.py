"""M1-04 step 1: domain state machines and persistence over the v2 schema.

Scope (deliberately small, review-first):

* Task / Attempt / Session state machines as explicit, inspectable transition
  tables (:func:`task_transition_targets`, :func:`attempt_transition_targets`,
  :func:`session_transition_targets`) — status *strings* are canonical
  lowercase, chosen so they never collide with legacy ``phase`` / ``live_state``
  spellings and so the v1 partial unique index
  ``ux_attempts_single_occupying`` (which matches ``upper(status)``) keeps
  working unchanged;
* persistence APIs that only ever mutate through :func:`teamcraft.storage.transaction`
  (an already-open outer transaction is reused, never nested), on top of the
  v2 ``task_dependencies`` edge table;
* DAG dependency edges with concrete cycle-path reporting
  (:class:`teamcraft.errors.DependencyCycleError`, ``cycle`` is a closed path:
  consecutive entries are dependency edges and the last entry repeats the
  first);
* READY derivation (``recompute_ready``) and deterministic dispatch ordering
  (``ready_queue``: priority DESC, created_at ASC, id ASC);
* optimistic concurrency on Task rows via compare-and-swap on ``version``
  (:class:`teamcraft.errors.VersionConflictError`) — every real mutation of
  a Task or of its dependency edges bumps ``version`` exactly once, so
  staleness is never invisible (CEO review M1-04 #1); and events that assert
  dependency satisfaction (``dependencies_satisfied``, ``dispatch_reserved``)
  re-verify the DONE fact from ``task_dependencies`` inside the transaction
  rather than trusting status/queue derivations (CEO review M1-04 #2).

Non-goals for this step (owned by later tasks, intentionally absent):
Scheduler/lease/dispatch mechanics (M2), M4 Worktree/Commit/Diff delivery,
``delivery_status`` persistence (per M0 review 8.2-4
``READY_FOR_ACCEPTANCE`` must not be smuggled into the Task main-status enum),
Watcher/reconcile, and any touch of real ``.teamcraft`` runtime state.

Hard invariants pinned by tests (``tests/test_domain_m104.py``):

1. **UNKNOWN occupies the slot**: an ``unknown`` attempt holds the task's
   single occupying-attempt slot; a violating insert maps the DB constraint
   (``ux_attempts_single_occupying``) to
   :class:`teamcraft.errors.AttemptSlotOccupiedError` and rolls the whole
   transaction back — the Task row is not bumped (M0 review 8.2-7/8.2-11);
2. **Session state is never Task completion**: ``observe_session`` writes only
   the ``sessions`` row; the Task machine has no session-signal events
   (``session_idle``/``session_done``/``pane_gone`` are refused as illegal),
   and a vanished session never changes a Task's id or version
   (M0 review 8.2-4, 9.1 M1-04 / ADR-001 8.2-3);
3. **Cycles report the path**: adding a dependency edge that would close a
   cycle raises with the concrete closed path, self-dependency included;
   dependency CRUD demands the caller's ``expected_version``, bumps the
   owning Task exactly once per REAL change, and never bumps on idempotent
   no-ops or refusals (CEO review M1-04 fix 3);
4. **READY->RUNNING is atomic with reservation**: ``dispatch_reserved`` is
   refused through generic :func:`apply_task_event`
   (reason ``RESERVATION_REQUIRED``); only :func:`create_attempt` performs
   the transition, together with the attempt insert and an in-transaction
   re-check that every hard dependency is ``done`` (CEO review M1-04 fix 2);
5. **Schema head is demanded**: domain services refuse to run against an
   audited-but-lagging v1 database with
   :class:`teamcraft.errors.SchemaOutOfDateError`, while the storage layer's
   own contracts keep working there (M1-03 compatibility);
6. **Execution completion gates terminal Task movement** (CEO review P1-B):
   ``worker_reported_done`` / DONE / FAILED / CANCELLED refuse unless the
   current Attempt is ``finished`` (success claims) or at least settled
   (no occupying attempt); UNKNOWN always demands reconcile first; and
   dependencies are conservatively same-Project only — PLAN/ADR leave
   cross-Project semantics unspecified, so this implementation rejects them
   (CEO review P1-A);
7. **Evidence-version bumps invalidate queued commands** (M1-05): every
   accepted Task CAS bump atomically expires PENDING commands whose evidence
   binds this Task at an older version (review 8.2-6 principle), inside the
   same transaction (:mod:`teamcraft.commands` layer).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Iterator

from . import events, storage
from .clock import Clock, SystemClock
from .commands import invalidate_stale_commands_for_task
from .errors import (
    AttemptSlotOccupiedError,
    ConflictError,
    DependencyCycleError,
    IllegalTransitionError,
    NotFoundError,
    SchemaOutOfDateError,
    ValidationError,
    VersionConflictError,
)
from .ids import DEFAULT_ID_FACTORY, IdFactory, require_entity_id
from .storage import (
    OCCUPYING_ATTEMPT_STATUSES,
    SCHEMA_VERSION,
    TASK_KINDS,
    TeamCraftConnection,
)

# ---------------------------------------------------------------------------
# Canonical status enums (lowercase on disk; see module docstring)
# ---------------------------------------------------------------------------

#: Task main-status enum (PLAN 5.1).  ``delivery_status`` is deliberately NOT
#: part of this enum and is not persisted in this step (M0 review 8.2-4).
TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "ready",
    "running",
    "verifying",
    "ready_to_merge",
    "done",
    "blocked",
    "needs_human",
    "failed",
    "cancelled",
)

#: Terminal Task statuses: no outgoing transitions, no dependency edits.
TERMINAL_TASK_STATUSES = frozenset({"done", "cancelled", "failed"})

#: Task statuses whose dependency edges may be added/removed (M3-04 tightens
#: this further with evidence invalidation; refusing in-flight edits early is
#: the conservative default).
DEPENDENCY_EDITABLE_STATUSES = frozenset({"pending", "ready", "blocked"})

#: M3-04: statuses a controlled pause may settle from.  RUNNING/VERIFYING
#: keep their in-flight Attempt; pausing mid-flight would strand the slot, so
#: pause targets the dispatchable/held set only (cancel covers in-flight via
#: the recovery request path).
PAUSABLE_TASK_STATUSES = frozenset({"pending", "ready", "blocked"})

#: M3-04 execution-policy surface (roadmap: delivery/router/verifier/
#: timeout/max-attempt).  Values persist on the Task; the dispatch-affecting
#: part (max_attempts) lands on the enforced column, the rest is durable
#: configuration the M4 executors consume.
EXECUTION_POLICY_KEYS = frozenset(
    {"delivery", "router", "verifier", "timeout_seconds", "max_attempts"}
)
#: Mirrors PLAN's acceptance modes (report-only / local-only / direct-PR /
#: ship); anything else is rejected before persistence.
DELIVERY_MODES = frozenset({"report_only", "local_only", "direct_pr", "ship"})

#: Attempt statuses (PLAN 5.2).  ``unknown`` is non-terminal: only a
#: reconcile-class event may resolve it, and it keeps the occupying slot.
ATTEMPT_STATUSES: tuple[str, ...] = (
    "created",
    "starting",
    "active",
    "finished",
    "failed",
    "unknown",
    "cancelled",
)

#: Attempt statuses that end execution for good (slot released).
TERMINAL_ATTEMPT_STATUSES = frozenset({"finished", "failed", "cancelled"})

#: Mirror of the storage-layer occupying set — kept as the single reference,
#: never re-spelled here (M1-04 keeps it unchanged).
OCCUPYING_ATTEMPT_STATUSES = OCCUPYING_ATTEMPT_STATUSES

#: Session live-signal states (PLAN/M0 review 8.2-4: idle/working are live
#: signals, *not* progress; exited/missing are liveness observations).
#: ``unknown`` is the conservative initial and re-probe state.
SESSION_STATES: tuple[str, ...] = (
    "unknown",
    "starting",
    "idle",
    "working",
    "exited",
    "missing",
)

#: Priority is an integer 0..100 (higher wins); default mirrors the v1 DDL.
PRIORITY_MIN = 0
PRIORITY_MAX = 100
DEFAULT_PRIORITY = 50

# ---------------------------------------------------------------------------
# Transition tables
# ---------------------------------------------------------------------------

# M4-04 local-only delivery: one accepted Commit on the Attempt's own branch
# is what completes a ``local_only`` ship Task.  The facts ride the durable
# append-only ``events`` stream (same storage pattern M4-02 used for worktree
# lifecycle, so no schema step is needed); these constants name the contract
# shared by :mod:`teamcraft.delivery` and the fact gate below.  ``ship`` (the
# default policy when no delivery mode is configured) and ``direct_pr`` keep
# waiting on M4-05/M4-06: only an explicit ``local_only`` policy plus a bound
# Commit/Diff/Verifier chain may fire ``local_delivery_accepted``.
LOCAL_DELIVERY_ACCEPT_EVENT = "local_delivery_accepted"
DELIVERY_CONTRACT_VERSION = 1
COMMIT_EVIDENCE_EVENT_KIND = "delivery_commit_evidence"
SHIP_EVIDENCE_EVENT_KIND = "delivery_ship_evidence"
DELIVERY_APPROVAL_EVENT_KIND = "delivery_approval"
#: The only proposed/accepted action this milestone understands.
LOCAL_ONLY_ACCEPT_ACTION = "accept_local_commit"
#: Branches that local delivery must NEVER commit to or accept on.
PROTECTED_BRANCH_NAMES = frozenset({"main", "master"})

#: (status, event) -> allowed target tuple.  Multiple targets mean the caller
#: must choose one (ambiguous events, PLAN 5.1 rows "NEEDS_HUMAN 或 BLOCKED"
#: and "BLOCKED / READY"); zero targets is an illegal transition.
_TASK_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("pending", "dependencies_satisfied"): ("ready",),
    ("ready", "dispatch_reserved"): ("running",),
    ("running", "worker_reported_done"): ("verifying",),
    ("running", "runtime_unknown"): ("needs_human", "blocked"),
    ("running", "recovery_escalated"): ("needs_human",),
    ("running", "attempts_exhausted"): ("needs_human",),
    ("running", "retry_approved"): ("ready",),
    ("ready", "attempts_exhausted"): ("needs_human",),
    ("verifying", "verification_passed"): ("needs_human",),
    ("verifying", "verification_failed"): ("needs_human",),
    ("verifying", LOCAL_DELIVERY_ACCEPT_EVENT): ("done",),
    ("ready_to_merge", "approved_and_delivered"): ("done",),
    ("blocked", "blocked_cleared"): ("pending",),
    ("needs_human", "human_reconciled"): ("blocked", "ready", "failed"),
}

#: ``user_cancel`` fires from every non-terminal status (PLAN 5.1 last row);
#: terminal statuses have no outgoing edges at all.
_CANCEL_EVENT = "user_cancel"

#: Kind-aware state routing: only an approved report-only Scout can finish
#: before M4; verification itself never completes a task.
def task_transition_targets(
    status: str, event: str, kind: str = "ship"
) -> tuple[str, ...]:
    """Allowed Task targets for (status, event); empty tuple == illegal."""

    if status not in TASK_STATUSES:
        raise ValidationError(f"unknown task status: {status!r}")
    if kind not in TASK_KINDS:
        raise ValidationError(f"unknown task kind: {kind!r}")
    if status in TERMINAL_TASK_STATUSES:
        return ()
    if event == _CANCEL_EVENT:
        return ("cancelled",)
    if kind == "scout" and (status, event) == ("verifying", "report_accepted"):
        return ("done",)
    if (status, event) == ("verifying", "report_accepted"):
        return ()
    if event == LOCAL_DELIVERY_ACCEPT_EVENT:
        # Local-only acceptance is a ship-kind path only; scouts complete via
        # report_accepted.  The delivery-mode and evidence facts are re-checked
        # against durable rows inside apply_task_event (the door below).
        return ("done",) if kind == "ship" else ()
    return _TASK_TRANSITIONS.get((status, event), ())


#: Attempt machine (PLAN 5.2).  ``unknown`` closes ONLY through
#: reconcile-class events — never ``abandon``/``fail`` shortcuts — so an
#: unresolved dispatch can never be quietly forgotten.
_ATTEMPT_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("created", "start"): ("starting",),
    ("created", "abandon"): ("cancelled",),
    ("starting", "activated"): ("active",),
    ("starting", "start_failed"): ("failed",),
    ("starting", "abandon"): ("cancelled",),
    ("active", "finish"): ("finished",),
    ("active", "fail"): ("failed",),
    ("active", "runtime_lost"): ("unknown",),
    ("active", "cancel_confirmed"): ("cancelled",),
    ("unknown", "reconciled_active"): ("active",),
    ("unknown", "reconciled_finished"): ("finished",),
    ("unknown", "reconciled_failed"): ("failed",),
    ("unknown", "reconciled_cancelled"): ("cancelled",),
}


def attempt_transition_targets(status: str, event: str) -> tuple[str, ...]:
    """Allowed Attempt targets for (status, event); empty tuple == illegal."""

    if status not in ATTEMPT_STATUSES:
        raise ValidationError(f"unknown attempt status: {status!r}")
    if status in TERMINAL_ATTEMPT_STATUSES:
        return ()
    return _ATTEMPT_TRANSITIONS.get((status, event), ())


#: Session liveness machine.  Exit/missing observations may only fall back to
#: ``unknown`` (a re-probe), never straight back to ``idle``/``working``:
#: liveness never resumes by assumption.
_SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "unknown": frozenset({"unknown", "starting", "idle", "working", "exited", "missing"}),
    "starting": frozenset({"idle", "working", "exited", "missing", "unknown"}),
    "idle": frozenset({"working", "exited", "missing", "unknown"}),
    "working": frozenset({"idle", "exited", "missing", "unknown"}),
    "exited": frozenset({"missing", "unknown"}),
    "missing": frozenset({"unknown"}),
}


def session_transition_targets(state: str, new_state: str) -> bool:
    """True if a session observation may move from ``state`` to ``new_state``."""

    if state not in SESSION_STATES or new_state not in SESSION_STATES:
        raise ValidationError(f"unknown session state: {state!r} -> {new_state!r}")
    return new_state in _SESSION_TRANSITIONS[state]


# ---------------------------------------------------------------------------
# Row records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    repo_path: str
    config_json: str
    created_at: str


@dataclass(frozen=True)
class Task:
    id: str
    project_id: str
    title: str
    description: str
    acceptance_json: str
    kind: str
    priority: int
    status: str
    risk: str
    ownership_json: str
    max_attempts: int
    version: int
    created_at: str
    updated_at: str
    #: M3-04 dispatch gate: a paused Task is hidden from ``ready_queue`` and
    #: ``create_attempt`` refuses it (TASK_PAUSED) until resumed.
    paused: bool = False
    #: Canonical JSON object of the per-task execution policy (M3-04):
    #: delivery/router/verifier/timeout_seconds/max_attempts requests that
    #: persisted through the optimistic-CAS policy write.
    execution_policy_json: str = "{}"


@dataclass(frozen=True)
class Attempt:
    id: str
    task_id: str
    agent_id: str | None
    attempt_no: int
    status: str
    worktree_path: str | None
    branch: str | None
    started_at: str | None
    finished_at: str | None
    commit_sha: str | None
    report_ref: str | None
    result_json: str | None
    protocol_version: str | None
    protocol_hash: str | None


@dataclass(frozen=True)
class SessionRecord:
    id: str
    attempt_id: str | None
    backend: str
    native_session_id: str | None
    pane_id: str | None
    state: str
    last_observed_at: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_iso(clock: Clock | None) -> str:
    clock = clock if clock is not None else SystemClock()
    return clock.now().astimezone(timezone.utc).isoformat()


@contextlib.contextmanager
def _tx(conn: TeamCraftConnection) -> Iterator[TeamCraftConnection]:
    """Durability gate: reuse an open outer transaction, else open one.

    All domain writes run under :func:`teamcraft.storage.transaction` (BEGIN
    IMMEDIATE + schema re-check + gated write channel).  When a caller already
    holds a transaction the body joins it instead of nesting a BEGIN (SQLite
    forbids the latter), which is what lets M1-05 later bundle state+event
    writes into one atomic unit.
    """

    if conn.in_transaction:
        yield conn
    else:
        with storage.transaction(conn) as gated:
            yield gated


def require_head(conn: TeamCraftConnection) -> None:
    """Refuse domain service on anything but this build's head schema.

    An audited v1 database stays usable for storage contracts (M1-03); only
    the domain layer demands ``task_dependencies`` (M1-04).
    """

    row = conn.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row else 0
    if version > SCHEMA_VERSION:
        raise SchemaOutOfDateError(
            f"database schema version {version} is newer than this domain build "
            f"supports ({SCHEMA_VERSION}); refusing to run"
        )
    if version < SCHEMA_VERSION:
        raise SchemaOutOfDateError(
            f"database schema version {version} lags this domain build "
            f"({SCHEMA_VERSION}); run teamcraft.migrate_to_head() first"
        )
    tables = {
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = storage._V2_TABLES - tables  # first-party internals, same package
    if missing:
        raise SchemaOutOfDateError(
            f"user_version={SCHEMA_VERSION} but tables are missing: "
            f"{', '.join(sorted(missing))}; refusing domain services"
        )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _row_to(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    return dict(zip(columns, tuple(row)))


_TASK_COLUMNS = (
    "id", "project_id", "title", "description", "acceptance_json", "kind",
    "priority", "status", "risk", "ownership_json", "max_attempts", "version", "created_at",
    "updated_at", "paused", "execution_policy_json",
)
_ATTEMPT_COLUMNS = (
    "id", "task_id", "agent_id", "attempt_no", "status", "worktree_path",
    "branch", "started_at", "finished_at", "commit_sha", "report_ref",
    "result_json", "protocol_version", "protocol_hash",
)
_SESSION_COLUMNS = (
    "id", "attempt_id", "backend", "native_session_id", "pane_id", "state",
    "last_observed_at", "created_at",
)
_TASK_SELECT = "SELECT " + ", ".join(_TASK_COLUMNS) + " FROM tasks"


def _fetch_task(conn: TeamCraftConnection, task_id: str) -> Task:
    row = conn.execute(_TASK_SELECT + " WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"task not found: {task_id}")
    return Task(**_row_to(row, _TASK_COLUMNS))


def _cas_task(
    conn: TeamCraftConnection,
    task: Task,
    *,
    status: str | None = None,
    expected_version: int,
    now: str,
    **fields: Any,
) -> Task:
    """Compare-and-swap update of one Task row; refuses on version drift.

    Bumps ``version`` exactly once per accepted write.  Callers must hold an
    open write transaction (``_tx``).
    """

    assignments = ["version = version + 1", "updated_at = ?"]
    values: list[Any] = [now]
    if status is not None:
        assignments.append("status = ?")
        values.append(status)
    for name, value in fields.items():
        assignments.append(f"{name} = ?")
        values.append(value)
    values.extend([task.id, expected_version])
    cursor = conn.execute(
        f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ? AND version = ?",
        values,
    )
    if cursor.rowcount != 1:
        current = _fetch_task(conn, task.id)
        raise VersionConflictError(
            f"task {task.id} version conflict: expected {expected_version}, "
            f"found {current.version}",
            entity_id=task.id,
            expected=expected_version,
            found=current.version,
        )
    # M1-05 (review 8.2-6 principle): this bump IS the evidence-version
    # change — PENDING commands bound to an older version of this Task are
    # invalidated atomically with it, so a queued side effect can never fire
    # against facts that no longer exist.
    invalidate_stale_commands_for_task(conn, task.id, now=now)
    return _fetch_task(conn, task.id)


def _illegal(entity: str, current: str, event: str, reason: str = "ILLEGAL_TRANSITION") -> IllegalTransitionError:
    return IllegalTransitionError(
        f"{entity} cannot apply {event!r} in state {current!r}",
        entity=entity,
        current=current,
        event=event,
        reason_code=reason,
    )


def _resolve_target(
    entity: str, status: str, event: str, targets: tuple[str, ...], target: str | None
) -> str:
    if not targets:
        raise _illegal(entity, status, event)
    if target is None:
        if len(targets) > 1:
            raise ValidationError(
                f"{entity} event {event!r} has multiple allowed targets "
                f"({', '.join(targets)}); an explicit target is required"
            )
        return targets[0]
    if target not in targets:
        raise _illegal(entity, status, event, reason="TARGET_NOT_ALLOWED")
    return target


_OCCUPYING_SQL = " AND upper(status) IN ({})".format(
    ", ".join("'" + s.upper() + "'" for s in OCCUPYING_ATTEMPT_STATUSES)
)

#: Events whose PLAN 5.1 condition column asserts the hard-dependency fact
#: ("所有硬依赖按定义完成").  A caller may not smuggle this event past a DAG
#: that still has unfinished upstreams: the fact is re-verified from
#: ``task_dependencies`` inside the transaction, exactly as
#: :func:`recompute_ready` derives it.
#: (Evidence: doc/TEAMCRAFT_2_0_PLAN.md:350; M0 review 8.2-7 "不能仅依赖
#: 内存状态" — here: not even the persisted status='ready' alone.)
_FACT_GUARDED_TASK_EVENTS = frozenset({"dependencies_satisfied"})

#: READY->RUNNING is reserved for :func:`create_attempt`, which writes the
#: attempt-row reservation and the status transition in ONE transaction
#: (PLAN 5.1: "事务抢占成功"; CEO review M1-04 fix 2).  The generic event
#: door would allow RUNNING with no occupying attempt — a slot invariant the
#: database cannot enforce afterwards — so apply_task_event refuses this
#: event outright instead of trusting the caller to follow with a dispatch.
_DISPATCH_EVENT = "dispatch_reserved"

#: ``worker_reported_done`` asserts that a worker finished an execution
#: (PLAN 5.1 "仅启动验证" presupposes a finished Attempt; M0 review 8.2-4
#: "Worker 完成表示 Attempt 已停止执行并提交结构化结果").
_WORKER_DONE_EVENT = "worker_reported_done"

_CURRENT_ATTEMPT_SQL = (
    "SELECT id, status FROM attempts WHERE task_id = ? ORDER BY attempt_no DESC LIMIT 1"
)
_OCCUPYING_ORDERED_SQL = (
    "SELECT id, status FROM attempts WHERE task_id = ?" + _OCCUPYING_SQL
    + " ORDER BY attempt_no DESC LIMIT 1"
)


def _require_execution_finished(
    conn: TeamCraftConnection, task: Task, event: str, resolved: str
) -> None:
    """Shared scout/ship execution-completion guard (CEO review M1-04 P1-B).

    Enforced for ``worker_reported_done`` and every transition reaching a
    terminal Task status:

    * success claims (``worker_reported_done``, or a transition into DONE)
      require the Task's current Attempt to be ``finished`` — an Attempt in
      created/starting/active has not stopped executing, failed/cancelled
      cannot masquerade as success, and an unresolved UNKNOWN must NEVER pass
      the ordinary path (reconcile-class transition first, M0 review 8.2-11);
    * non-success terminal exits (FAILED/CANCELLED) may not leave an attempt
      occupying the slot — settle it first (abandon/fail/reconcile);
    * everything else (READY claims, retry via explicitly cleared failure,
      needs_human, blocked) is untouched: verifier evidence and Approval
      decisions are handled by the M2-06 service boundary.

    Runs inside the caller's write transaction; a refusal writes nothing.
    """

    def refuse(reason: str, message: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            message, entity="task", current=task.status, event=event, reason_code=reason
        )

    success_claim = event == _WORKER_DONE_EVENT or resolved == "done"
    occupying = conn.execute(_OCCUPYING_ORDERED_SQL, (task.id,)).fetchone()
    if success_claim:
        row = occupying
        if row is None:
            row = conn.execute(_CURRENT_ATTEMPT_SQL, (task.id,)).fetchone()
        if row is None:
            raise refuse(
                "NO_EXECUTION_ATTEMPT",
                f"task {task.id} event {event!r}: no Attempt has ever run for "
                "this Task; completion claims require a finished Attempt",
            )
        status = str(row[1])
        if status == "finished":
            return
        if status == "unknown":
            raise refuse(
                "EXECUTION_UNRESOLVED_RECONCILE_REQUIRED",
                f"task {task.id} event {event!r}: Attempt {row[0]} is UNKNOWN; "
                "a reconcile-class Attempt transition "
                "(reconciled_finished/reconciled_failed) is required first",
            )
        if status in ("failed", "cancelled"):
            raise refuse(
                "EXECUTION_NOT_SUCCESSFUL",
                f"task {task.id} event {event!r}: latest Attempt {row[0]} is "
                f"{status!r}; failed/cancelled execution cannot masquerade as "
                "completion",
            )
        raise refuse(
            "EXECUTION_NOT_FINISHED",
            f"task {task.id} event {event!r}: current Attempt {row[0]} is "
            f"{status!r} and still occupying the slot; execution has not "
            "finished",
        )
    if resolved in TERMINAL_TASK_STATUSES and occupying is not None:
        if str(occupying[1]) == "unknown":
            raise refuse(
                "EXECUTION_UNRESOLVED_RECONCILE_REQUIRED",
                f"task {task.id} event {event!r}: Attempt {occupying[0]} is "
                "UNKNOWN and holds the slot; reconcile it before terminalizing "
                "the Task",
            )
        raise refuse(
            "EXECUTION_NOT_FINISHED",
            f"task {task.id} event {event!r}: Attempt {occupying[0]} is "
            f"{occupying[1]!r} and still holds the slot; settle it "
            "(abandon/fail) so the Task does not end with a dangling attempt",
        )

_UNFINISHED_DEPS_SQL = (
    "SELECT p.id, p.status FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_id"
    " WHERE d.task_id = ? AND p.status <> 'done' ORDER BY p.id"
)

# ---------------------------------------------------------------------------
# M4-04 — durable local-only delivery facts (Commit/Diff/Verifier/Approval)
#
# The gate below is the SINGLE authority that lets ``verifying ->
# done`` fire via ``local_delivery_accepted``.  It reads only durable facts
# (append-only ``events`` rows + current Task/Attempt/Brief/WorkerReport/
# verifier-profile rows) — never caller arguments — and fails closed:
#
# * an accept Approval must exist, bound to this Task at its CURRENT version
#   (any Task/task/policy drift bumps the version and strands the binding);
# * the Approval chain must reference exactly one ship Verification evidence
#   row, which references exactly one Commit evidence row, with commit SHA,
#   branch, base SHA, diff digest and evidence digest agreeing across every
#   link (a tampered link breaks the recomputed digests and the chain);
# * delivery mode ``local_only`` and the canonical policy digest must still
#   match the live Task execution policy;
# * the bound Attempt must be this Task's latest Attempt, finished, and the
#   Brief/WorkerReport pins must still equal the durable rows;
# * every pinned M4-03 verifier profile must still be the current approved
#   version/config of its profile (verifier/config drift invalidates);
# * the bound branch is never a protected branch name.
#
# Live Git re-verification (HEAD == bound commit, clean worktree, complete
# diff) happens in :mod:`teamcraft.delivery` at evidence/approval time; the
# Task machine here is the durable-fact floor, mirroring how ``report_accepted``
# re-checks scout approvals from the table rows.
# ---------------------------------------------------------------------------

_LOCAL_ACCEPT_FACTS_SQL = (
    "SELECT ap.seq, ap.event_id, ap.payload_json,"
    " se.event_id, se.payload_json, ce.event_id, ce.payload_json"
    " FROM events ap"
    " JOIN events se ON se.kind = ?"
    "   AND se.event_id = json_extract(ap.payload_json, '$.ship_evidence_id')"
    "   AND se.task_id = ap.task_id AND se.attempt_id = ap.attempt_id"
    " JOIN events ce ON ce.kind = ?"
    "   AND ce.event_id = json_extract(se.payload_json, '$.commit_evidence_id')"
    "   AND ce.task_id = ap.task_id AND ce.attempt_id = ap.attempt_id"
    " WHERE ap.kind = ? AND ap.task_id = ?"
    "   AND json_extract(ap.payload_json, '$.schema_version') = ?"
    "   AND json_extract(ap.payload_json, '$.decision') = 'accept'"
    "   AND json_extract(ap.payload_json, '$.proposed_action')"
    "       = 'accept_local_commit'"
    "   AND json_extract(ap.payload_json, '$.task_version') = ?"
    "   AND json_extract(se.payload_json, '$.schema_version') = ?"
    "   AND json_extract(se.payload_json, '$.delivery_mode') = 'local_only'"
    "   AND json_extract(se.payload_json, '$.proposed_action')"
    "       = 'accept_local_commit'"
    "   AND json_extract(se.payload_json, '$.task_version')"
    "       = json_extract(ap.payload_json, '$.task_version')"
    "   AND json_extract(se.payload_json, '$.attempt_no')"
    "       = json_extract(ap.payload_json, '$.attempt_no')"
    "   AND json_extract(se.payload_json, '$.commit_sha')"
    "       = json_extract(ap.payload_json, '$.commit_sha')"
    "   AND json_extract(se.payload_json, '$.branch')"
    "       = json_extract(ap.payload_json, '$.branch')"
    "   AND json_extract(se.payload_json, '$.diff_digest')"
    "       = json_extract(ap.payload_json, '$.diff_digest')"
    "   AND json_extract(se.payload_json, '$.evidence_digest')"
    "       = json_extract(ap.payload_json, '$.evidence_digest')"
    "   AND json_extract(ce.payload_json, '$.schema_version') = ?"
    "   AND json_extract(ce.payload_json, '$.commit_sha')"
    "       = json_extract(se.payload_json, '$.commit_sha')"
    "   AND json_extract(ce.payload_json, '$.branch')"
    "       = json_extract(se.payload_json, '$.branch')"
    "   AND json_extract(ce.payload_json, '$.base_sha')"
    "       = json_extract(se.payload_json, '$.base_sha')"
    " ORDER BY ap.seq DESC LIMIT 1"
)

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST64_RE = re.compile(r"^[0-9a-f]{64}$")


def _delivery_payload_digest(payload: dict) -> str:
    """SHA-256 over the payload's canonical JSON minus ``evidence_digest``.

    The SAME rule is implemented in :mod:`teamcraft.delivery` (kept local to
    each layer on purpose, mirroring the documented duplication of
    ``require_head`` in ``events.py``); the M4-04 acceptance tests prove both
    recomputations agree, so a payload that any layer accepts is byte-stable.
    """
    stripped = {key: value for key, value in payload.items()
                if key != "evidence_digest"}
    return hashlib.sha256(
        json.dumps(stripped, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False).encode("utf-8")).hexdigest()


def _delivery_json_field(payload: dict, key: str) -> Any:
    value = payload.get(key)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _require_local_delivery_acceptance(
    conn: TeamCraftConnection, task: Task, event: str
) -> None:
    """Fail-closed durable-fact gate for ``local_delivery_accepted``.

    Raises :class:`IllegalTransitionError` with a stable reason code unless
    every bound fact still holds at the Task's CURRENT version.
    """

    def refuse(code: str, message: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"task {task.id} event {event!r}: {message}",
            entity="task", current=task.status, event=event, reason_code=code,
        )

    if task.kind != "ship":
        raise refuse("M4_EVIDENCE_REQUIRED",
                    "only ship Tasks with local-only Commit delivery can be "
                    "accepted; scouts complete via report_accepted")
    try:
        policy = json.loads(task.execution_policy_json or "{}")
    except (TypeError, ValueError):
        policy = None
    if not isinstance(policy, dict) or policy.get("delivery") != "local_only":
        raise refuse("LOCAL_DELIVERY_MODE_REQUIRED",
                    "the Task's live execution policy is not delivery=local_only;"
                    " direct_pr/ship delivery waits for M4-05/M4-06 and "
                    "report_only delivers nothing")
    row = conn.execute(
        _LOCAL_ACCEPT_FACTS_SQL,
        (SHIP_EVIDENCE_EVENT_KIND, COMMIT_EVIDENCE_EVENT_KIND,
         DELIVERY_APPROVAL_EVENT_KIND, task.id, DELIVERY_CONTRACT_VERSION,
         task.version, DELIVERY_CONTRACT_VERSION, DELIVERY_CONTRACT_VERSION),
    ).fetchone()
    if row is None:
        raise refuse("ACCEPTANCE_FACTS_MISSING",
                    "no durable accept Approval bound to this Task's current "
                    "version with a complete ship+commit evidence chain")
    try:
        approval = json.loads(str(row[2]))
        evidence = json.loads(str(row[4]))
        commit = json.loads(str(row[6]))
    except (TypeError, ValueError) as exc:
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    f"delivery evidence payload is not valid JSON: {exc}")
    for name, payload in (("approval", approval), ("ship evidence", evidence),
                         ("commit evidence", commit)):
        if not isinstance(payload, dict):
            raise refuse("ACCEPTANCE_FACTS_STALE",
                         f"{name} payload is not a JSON object")
    if _delivery_payload_digest(evidence) != evidence.get("evidence_digest"):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "ship evidence digest does not recompute; the durable "
                    "delivery evidence was tampered with")
    if _delivery_payload_digest(commit) != commit.get("evidence_digest"):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "commit evidence digest does not recompute")
    if approval.get("evidence_digest") != evidence.get("evidence_digest"):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "Approval does not bind the ship evidence's evidence version")
    branch = str(evidence.get("branch") or "")
    if branch.lower() in PROTECTED_BRANCH_NAMES or not branch.startswith("tc/"):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    f"bound branch {branch!r} is not the isolated Attempt "
                    "branch; local delivery never commits to protected branches")
    policy_digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False).encode("utf-8")).hexdigest()
    if policy_digest != evidence.get("policy_digest"):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "the Task execution policy changed after the ship "
                    "evidence was formed")
    attempt_id = str(evidence.get("attempt_id") or "")
    attempt = conn.execute(
        "SELECT id, status, attempt_no FROM attempts WHERE task_id = ?"
        " ORDER BY attempt_no DESC LIMIT 1", (task.id,),
    ).fetchone()
    if (attempt is None or str(attempt[0]) != attempt_id
            or str(attempt[1]) != "finished"
            or int(attempt[2]) != int(evidence.get("attempt_no") or -1)):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "the bound Attempt is not this Task's latest finished "
                    "Attempt; stale-attempt acceptance is refused")
    brief = conn.execute(
        "SELECT task_version, brief_version, brief_digest FROM attempt_briefs"
        " WHERE attempt_id = ?", (attempt_id,),
    ).fetchone()
    if (brief is None or int(brief[0]) != int(evidence.get("dispatch_task_version") or -1)
            or int(brief[1]) != int(evidence.get("brief_version") or -1)
            or str(brief[2]) != evidence.get("brief_digest")):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "the Brief bound into the ship evidence no longer matches "
                    "the durable Attempt Brief")
    report = conn.execute(
        "SELECT id, task_version, brief_digest, report_digest, outcome, task_id"
        " FROM worker_reports WHERE attempt_id = ? AND report_kind = 'final'"
        " ORDER BY id DESC LIMIT 1", (attempt_id,),
    ).fetchone()
    if (report is None or int(report[0]) != int(evidence.get("worker_report_id") or -1)
            or int(report[1]) != int(evidence.get("dispatch_task_version") or -1)
            or str(report[2]) != evidence.get("brief_digest")
            or str(report[3]) != evidence.get("worker_report_digest")
            or str(report[4]) != "completed" or str(report[5]) != task.id):
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "the WorkerReport bound into the ship evidence is no "
                    "longer the latest final completed report")
    verifiers = _delivery_json_field(evidence, "verifiers_json")
    if not isinstance(verifiers, list) or not verifiers:
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "ship evidence carries no verifier profile bindings")
    pinned: dict[str, tuple[int, str]] = {}
    for entry in verifiers:
        if not isinstance(entry, dict):
            raise refuse("ACCEPTANCE_FACTS_STALE",
                        "verifier binding entry is malformed")
        profile = str(entry.get("profile") or "")
        version = entry.get("profile_version")
        digest = str(entry.get("config_digest") or "")
        if (entry.get("status") != "pass" or not profile
                or isinstance(version, bool) or not isinstance(version, int)
                or not _DIGEST64_RE.match(digest)):
            raise refuse("ACCEPTANCE_FACTS_STALE",
                        f"verifier profile {profile!r} binding is not a "
                        "passing versioned configuration")
        pinned[profile] = (int(version), digest)
    current = conn.execute(
        "SELECT profile, profile_version, config_digest, enabled"
        " FROM verifier_profiles p WHERE p.project_id = ? AND profile_version ="
        " (SELECT MAX(p2.profile_version) FROM verifier_profiles p2"
        "   WHERE p2.project_id = p.project_id AND p2.profile = p.profile)",
        (task.project_id,),
    ).fetchall()
    current_required = {str(r[0]): (int(r[1]), str(r[2])) for r in current
                        if int(r[3]) == 1}
    if current_required != pinned:
        raise refuse("ACCEPTANCE_FACTS_STALE",
                    "the approved verifier profile configuration changed after "
                    "the ship evidence was formed (pinned"
                    f" {sorted(pinned.items())} vs current"
                    f" {sorted(current_required.items())})")


def _require_deps_done(
    conn: TeamCraftConnection, task: Task, event: str
) -> None:
    """Refuse an event whose PLAN condition requires every hard dep DONE.

    Structured refusal: :class:`IllegalTransitionError` with reason code
    ``DEPENDENCIES_NOT_SATISFIED`` — mirrors :func:`recompute_ready`, which
    derives READY from the same predicate instead of trusting it.
    """

    unfinished = conn.execute(_UNFINISHED_DEPS_SQL, (task.id,)).fetchall()
    if not unfinished:
        return
    detail = ", ".join(f"{row[0]}={row[1]}" for row in unfinished[:5])
    if len(unfinished) > 5:
        detail += f", … (+{len(unfinished) - 5} more)"
    raise IllegalTransitionError(
        f"task {task.id} event {event!r} refused: {len(unfinished)} hard "
        f"dependenc{'y' if len(unfinished) == 1 else 'ies'} not DONE ({detail})",
        entity="task",
        current=task.status,
        event=event,
        reason_code="DEPENDENCIES_NOT_SATISFIED",
    )


def _occupying_attempt_row(conn: TeamCraftConnection, task_id: str) -> Any:
    return conn.execute(
        "SELECT id FROM attempts WHERE task_id = ?" + _OCCUPYING_SQL + " LIMIT 1",
        (task_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def create_project(
    conn: TeamCraftConnection,
    name: str,
    repo_path: str,
    *,
    config_json: str = "{}",
    id_factory: IdFactory | None = None,
    clock: Clock | None = None,
) -> Project:
    """Insert a project; duplicate ``repo_path`` -> ConflictError."""

    require_head(conn)
    _require(bool(name.strip()), "project name must not be blank")
    _require(bool(repo_path.strip()), "repo_path must not be blank")
    factory = id_factory if id_factory is not None else DEFAULT_ID_FACTORY
    pid = factory.new("project")
    now = _utc_iso(clock)
    with _tx(conn):
        try:
            conn.execute(
                "INSERT INTO projects (id, name, repo_path, config_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (pid, name, repo_path, config_json, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"project for repo_path already exists: {repo_path}") from exc
    row = conn.execute(
        "SELECT id, name, repo_path, config_json, created_at FROM projects WHERE id = ?",
        (pid,),
    ).fetchone()
    assert row is not None  # committed above
    return Project(**_row_to(row, ("id", "name", "repo_path", "config_json", "created_at")))


def get_project(conn: TeamCraftConnection, project_id: str) -> Project:
    require_head(conn)
    row = conn.execute(
        "SELECT id, name, repo_path, config_json, created_at FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"project not found: {project_id}")
    return Project(**_row_to(row, ("id", "name", "repo_path", "config_json", "created_at")))


# ---------------------------------------------------------------------------
# Tasks: creation, CAS updates, event application
# ---------------------------------------------------------------------------


def create_task(
    conn: TeamCraftConnection,
    project_id: str,
    title: str,
    *,
    description: str = "",
    kind: str = "ship",
    priority: int = DEFAULT_PRIORITY,
    acceptance_json: str = "[]",
    ownership_json: str = "[]",
    risk: str = "normal",
    max_attempts: int = 3,
    id_factory: IdFactory | None = None,
    clock: Clock | None = None,
) -> Task:
    """Create a Task in ``pending`` at ``version`` 0 (derivation makes it READY)."""

    require_head(conn)
    project_id = require_entity_id(project_id)
    _require(bool(title.strip()), "task title must not be blank")
    _require(isinstance(max_attempts, int) and not isinstance(max_attempts, bool) and max_attempts >= 1, "max_attempts must be an integer >= 1")
    _require(kind in TASK_KINDS, f"kind must be one of {TASK_KINDS}, got {kind!r}")
    _require(
        isinstance(priority, int)
        and not isinstance(priority, bool)
        and PRIORITY_MIN <= priority <= PRIORITY_MAX,
        f"priority must be an integer in [{PRIORITY_MIN}, {PRIORITY_MAX}]",
    )
    factory = id_factory if id_factory is not None else DEFAULT_ID_FACTORY
    tid = factory.new("task")
    now = _utc_iso(clock)
    with _tx(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFoundError(f"project not found: {project_id}")
        try:
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, description, acceptance_json,"
                " kind, priority, status, risk, ownership_json, max_attempts, version, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, 0, ?, ?)",
                (tid, project_id, title, description, acceptance_json, kind, priority,
                 risk, ownership_json, max_attempts, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"task insert violated integrity: {tid}") from exc
    return _fetch_task(conn, tid)


def get_task(conn: TeamCraftConnection, task_id: str) -> Task:
    require_head(conn)
    return _fetch_task(conn, require_entity_id(task_id))


def list_tasks(
    conn: TeamCraftConnection, project_id: str, *, status: str | None = None
) -> list[Task]:
    require_head(conn)
    if status is not None:
        _require(status in TASK_STATUSES, f"unknown task status: {status!r}")
        rows = conn.execute(
            _TASK_SELECT + " WHERE project_id = ? AND status = ?"
            " ORDER BY priority DESC, created_at ASC, id ASC",
            (project_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            _TASK_SELECT + " WHERE project_id = ?"
            " ORDER BY priority DESC, created_at ASC, id ASC",
            (project_id,),
        ).fetchall()
    return [Task(**_row_to(row, _TASK_COLUMNS)) for row in rows]


def update_task(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    expected_version: int,
    title: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    risk: str | None = None,
    clock: Clock | None = None,
) -> Task:
    """Field updates under optimistic locking: stale version -> conflict."""

    require_head(conn)
    updates: dict[str, Any] = {}
    if title is not None:
        _require(bool(title.strip()), "task title must not be blank")
        updates["title"] = title
    if description is not None:
        updates["description"] = description
    if priority is not None:
        _require(
            isinstance(priority, int)
            and not isinstance(priority, bool)
            and PRIORITY_MIN <= priority <= PRIORITY_MAX,
            f"priority must be an integer in [{PRIORITY_MIN}, {PRIORITY_MAX}]",
        )
        updates["priority"] = priority
    if risk is not None:
        updates["risk"] = risk
    if not updates:
        raise ValidationError("update_task requires at least one field to change")
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        if task.status == "done" and conn.execute(
            "SELECT 1 FROM approvals WHERE task_id=? AND decision='accept' LIMIT 1",
            (task.id,),
        ).fetchone() is not None:
            # Any Task version bump invalidates the accepted report evidence.
            # Reopen in the same CAS so a formerly completed Scout cannot
            # remain DONE on the strength of stale evidence.
            reopened = _cas_task(conn, task, status="needs_human",
                                 expected_version=expected_version, now=_utc_iso(clock), **updates)
            events.append_event(conn, "verification.approval_invalidated",
                project_id=task.project_id, task_id=task.id,
                payload_json='{"reason":"task_evidence_changed"}', clock=clock)
            return reopened
        if task.status == "done" and conn.execute(
            "SELECT 1 FROM events WHERE kind = ? AND task_id = ?"
            " AND json_extract(payload_json, '$.decision') = 'accept' LIMIT 1",
            (DELIVERY_APPROVAL_EVENT_KIND, task.id),
        ).fetchone() is not None:
            # M4-04: a locally accepted ship Task is DONE only on the strength
            # of its bound Commit/Diff/Verifier evidence.  ANY later Task
            # mutation (title, priority, policy, dependencies...) invalidates
            # that evidence version; reopen in the same CAS so the old
            # approval can never carry a drifted Task to DONE again.
            reopened = _cas_task(conn, task, status="needs_human",
                                 expected_version=expected_version, now=_utc_iso(clock), **updates)
            events.append_event(conn, "delivery.approval_invalidated",
                project_id=task.project_id, task_id=task.id,
                payload_json=_canonical_json(
                    {"reason": "task_evidence_changed",
                     "next": "new bounded Attempt with fresh commit/ship evidence"}),
                clock=clock)
            return reopened
        return _cas_task(conn, task, expected_version=expected_version, now=_utc_iso(clock), **updates)


def apply_task_event(
    conn: TeamCraftConnection,
    task_id: str,
    event: str,
    *,
    expected_version: int,
    target: str | None = None,
    clock: Clock | None = None,
) -> Task:
    """Apply a state-machine event to a Task with CAS on ``version``.

    Ambiguous events (two allowed targets, PLAN 5.1) require an explicit
    ``target``.  Session-signal names are not Task events and are refused as
    illegal — session liveness never advances the Task machine.  Events that
    assert hard-dependency satisfaction (``dependencies_satisfied``) are
    additionally fact-checked against ``task_dependencies`` inside the
    transaction: status ``ready`` alone is a derived claim, never proof
    (PLAN 5.1 condition column; M0 review 8.2-7).  ``dispatch_reserved`` is
    NOT applicable here: READY->RUNNING only happens atomically with an
    attempt reservation via :func:`create_attempt` (CEO review M1-04 fix 2);
    using it through this generic door raises reason code
    ``RESERVATION_REQUIRED``.  ``worker_reported_done`` and every transition
    into a terminal status pass the shared execution-completion guard
    (:func:`_require_execution_finished`): DONE etc. require the current
    Attempt to be ``finished``; UNKNOWN demands reconcile first
    (CEO review M1-04 P1-B).
    """

    require_head(conn)
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        if event == _DISPATCH_EVENT:
            raise IllegalTransitionError(
                "task event 'dispatch_reserved' requires atomic attempt "
                "reservation: use create_attempt() so READY->RUNNING and the "
                "single-occupying-attempt slot commit together",
                entity="task",
                current=task.status,
                event=event,
                reason_code="RESERVATION_REQUIRED",
            )
        if event in {"verification_passed", "verification_failed"}:
            raise IllegalTransitionError(
                "Verification results must be written by the Verification service",
                entity="task", current=task.status, event=event,
                reason_code="VERIFICATION_SERVICE_REQUIRED",
            )
        if event == "approved_and_delivered":
            raise IllegalTransitionError(
                "ship delivery requires M4 Worktree/Commit/Diff evidence",
                entity="task", current=task.status, event=event,
                reason_code="M4_EVIDENCE_REQUIRED",
            )
        if event == "report_accepted":
            if task.kind != "scout":
                raise IllegalTransitionError(
                    "only report-only/scout tasks can be accepted before M4",
                    entity="task", current=task.status, event=event,
                    reason_code="M4_EVIDENCE_REQUIRED",
                )
            accepted = conn.execute(
                "SELECT a.task_version, a.attempt_id, a.attempt_no, a.brief_digest, a.artifact_digest, "
                "a.config_version, a.rules_version, a.config_digest, v.id, "
                "v.dispatch_task_version, v.worker_report_id, v.worker_report_digest "
                "FROM approvals a JOIN verifications v ON v.id=a.verification_id "
                "JOIN verification_configs c ON c.project_id=a.project_id AND c.config_version=a.config_version "
                "WHERE a.task_id=? AND a.decision='accept' ORDER BY a.created_at DESC LIMIT 1",
                (task.id,),
            ).fetchone()
            if accepted is None or int(accepted[0]) != task.version:
                raise IllegalTransitionError(
                    "a current accepted Approval is required for this Task evidence version",
                    entity="task", current=task.status, event=event,
                    reason_code="APPROVAL_REQUIRED",
                )
            if not conn.execute(
                "SELECT 1 FROM verifications v JOIN attempts a ON a.id=v.attempt_id "
                "JOIN attempt_briefs b ON b.attempt_id=a.id JOIN worker_reports r ON r.id=v.worker_report_id "
                "WHERE v.id=? AND v.task_id=? AND v.project_id=? AND v.attempt_id=? AND v.attempt_no=? "
                "AND v.task_version=? AND v.dispatch_task_version=? AND v.worker_report_id=? "
                "AND v.worker_report_digest=? AND v.brief_digest=? AND v.artifact_digest=? "
                "AND v.config_version=? AND v.rules_version=? AND v.config_digest=? "
                "AND v.status='pass' AND a.status='finished' "
                "AND b.task_version=v.dispatch_task_version AND b.brief_digest=v.brief_digest "
                "AND r.report_kind='final' AND r.outcome='completed' AND r.task_id=v.task_id "
                "AND r.task_version=v.dispatch_task_version AND r.brief_digest=v.brief_digest "
                "AND r.report_digest=v.worker_report_digest "
                "AND r.id=(SELECT MAX(r2.id) FROM worker_reports r2 WHERE r2.attempt_id=v.attempt_id AND r2.report_kind='final') "
                "AND EXISTS (SELECT 1 FROM verification_configs c "
                "WHERE c.project_id=v.project_id AND c.config_version=v.config_version "
                "AND c.config_digest=v.config_digest AND c.rules_version=v.rules_version "
                "AND c.config_version=(SELECT MAX(c2.config_version) FROM verification_configs c2 WHERE c2.project_id=v.project_id)) "
                "AND v.id=(SELECT v2.id FROM verifications v2 WHERE v2.task_id=v.task_id ORDER BY v2.created_at DESC, v2.id DESC LIMIT 1)",
                (str(accepted[8]), task.id, task.project_id, str(accepted[1]), int(accepted[2]), int(accepted[0]),
                 int(accepted[9]), int(accepted[10]), str(accepted[11]), str(accepted[3]), str(accepted[4]),
                 int(accepted[5]), str(accepted[6]), str(accepted[7])),
            ).fetchone():
                raise IllegalTransitionError(
                    "Approval evidence or verifier configuration is stale",
                    entity="task", current=task.status, event=event,
                    reason_code="APPROVAL_STALE",
                )
        if event == LOCAL_DELIVERY_ACCEPT_EVENT:
            # M4-04: the ONLY path to DONE for a local-only ship Task.  All
            # facts (Approval chain, commit/diff digests, Brief/WorkerReport
            # pins, verifier profile currency, policy digest, protected branch)
            # are re-derived from durable rows — never from the caller.
            _require_local_delivery_acceptance(conn, task, event)
        targets = task_transition_targets(task.status, event, task.kind)
        resolved = _resolve_target("task", task.status, event, targets, target)
        if event in _FACT_GUARDED_TASK_EVENTS:
            _require_deps_done(conn, task, event)
        if event == _WORKER_DONE_EVENT or resolved in TERMINAL_TASK_STATUSES:
            _require_execution_finished(conn, task, event, resolved)
        return _cas_task(conn, task, status=resolved, expected_version=expected_version, now=_utc_iso(clock))


# ---------------------------------------------------------------------------
# Dependency DAG (v2 task_dependencies)
# ---------------------------------------------------------------------------


def add_dependency(
    conn: TeamCraftConnection,
    task_id: str,
    depends_on_id: str,
    *,
    expected_version: int,
    clock: Clock | None = None,
) -> bool:
    """Add ``task`` depends-on ``depends_on``; returns False if it already existed.

    Refusals: self-dependency and any closing of a cycle raise
    :class:`DependencyCycleError` with the concrete closed path; the graph is
    unchanged either way (everything runs inside one transaction).

    Optimistic-locking contract (CEO review M1-04 fixes 1/3; M0 review
    M3-04 row: dependency CRUD demands Task version -> 409): the caller must
    present the owning Task's current ``version``; a stale view raises
    :class:`VersionConflictError` before anything is written.  A REAL edge
    insertion is paired, inside the same transaction, with a CAS version bump
    of the owning Task (``version`` +1, fresh ``updated_at``) — DAG churn is
    never invisible.  Idempotent no-ops (edge already present) and refusals
    do NOT bump.
    """

    require_head(conn)
    task_id = require_entity_id(task_id)
    depends_on_id = require_entity_id(depends_on_id)
    if task_id == depends_on_id:
        raise DependencyCycleError(
            f"task {task_id} cannot depend on itself", cycle=[task_id, task_id]
        )
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        dependency = _fetch_task(conn, depends_on_id)
        # PLAN/ADR do not specify cross-Project dependency semantics, and no
        # scheduler behavior exists for such edges, so this implementation
        # conservatively rejects them here — BEFORE any write, status, or
        # version side effect (CEO review M1-04 P1-A).
        _require(
            task.project_id == dependency.project_id,
            f"cross-Project dependency is not authorized: task {task.id} lives "
            f"in project {task.project_id} but depends_on {dependency.id} lives "
            f"in project {dependency.project_id}",
        )
        _require(
            task.status not in TERMINAL_TASK_STATUSES,
            f"cannot edit dependencies of terminal task {task.id} ({task.status})",
        )
        _require(
            task.status in DEPENDENCY_EDITABLE_STATUSES,
            f"dependencies are editable only while {task.status!r} not in "
            f"{sorted(DEPENDENCY_EDITABLE_STATUSES)}",
        )
        _require(
            dependency.status not in TERMINAL_TASK_STATUSES or dependency.status == "done",
            "cannot depend on a cancelled/failed task",
        )
        if task.version != expected_version:
            raise VersionConflictError(
                f"task {task.id} version conflict: expected {expected_version},"
                f" found {task.version}",
                entity_id=task.id,
                expected=expected_version,
                found=task.version,
            )
        existing = conn.execute(
            "SELECT 1 FROM task_dependencies WHERE task_id = ? AND depends_on_id = ?",
            (task_id, depends_on_id),
        ).fetchone()
        if existing is not None:
            return False
        cycle = _find_cycle_path(conn, task_id, depends_on_id)
        if cycle is not None:
            raise DependencyCycleError(
                "dependency "
                f"{task_id} -> {depends_on_id} would close cycle {' -> '.join(cycle)}",
                cycle=cycle,
            )
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, created_at)"
            " VALUES (?, ?, ?)",
            (task_id, depends_on_id, _utc_iso(clock)),
        )
        # Real edge change: bump the owning Task's version under CAS so the
        # DAG mutation is visible to every optimistic reader/dispatcher.
        _cas_task(conn, task, expected_version=expected_version, now=_utc_iso(clock))
        return True


def remove_dependency(
    conn: TeamCraftConnection,
    task_id: str,
    depends_on_id: str,
    *,
    expected_version: int,
    clock: Clock | None = None,
) -> bool:
    """Delete an edge; returns False (no bump) if it did not exist.

    Same optimistic-locking contract as :func:`add_dependency`: the caller
    must hold the owning Task's current ``version`` (stale -> Conflict), and
    a real removal bumps it exactly once inside the transaction.
    """

    require_head(conn)
    task_id = require_entity_id(task_id)
    depends_on_id = require_entity_id(depends_on_id)
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        _require(
            task.status in DEPENDENCY_EDITABLE_STATUSES,
            f"dependencies are editable only in {sorted(DEPENDENCY_EDITABLE_STATUSES)},"
            f" task is {task.status!r}",
        )
        if task.version != expected_version:
            raise VersionConflictError(
                f"task {task.id} version conflict: expected {expected_version},"
                f" found {task.version}",
                entity_id=task.id,
                expected=expected_version,
                found=task.version,
            )
        cursor = conn.execute(
            "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_id = ?",
            (task_id, depends_on_id),
        )
        if cursor.rowcount == 1:
            _cas_task(conn, task, expected_version=expected_version, now=_utc_iso(clock))
            return True
        return False


def dependencies_of(conn: TeamCraftConnection, task_id: str) -> list[str]:
    require_head(conn)
    _fetch_task(conn, require_entity_id(task_id))
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT depends_on_id FROM task_dependencies WHERE task_id = ?"
            " ORDER BY depends_on_id",
            (task_id,),
        )
    ]


def _find_cycle_path(
    conn: TeamCraftConnection, task_id: str, depends_on_id: str
) -> list[str] | None:
    """Closed path if edge task->depends_on would close a cycle, else None.

    BFS from ``depends_on_id`` along existing depends-on edges looking for
    ``task_id``.  The returned list is closed (first entry repeats the last):
    ``[task_id, depends_on_id, ..., task_id]`` where every consecutive pair
    is a real dependency edge (the new edge first, then the discovered path).
    """

    parents: dict[str, str | None] = {depends_on_id: None}
    frontier = [depends_on_id]
    while frontier:
        row_targets: list[str] = []
        placeholders = ", ".join("?" for _ in frontier)
        for row in conn.execute(
            "SELECT task_id, depends_on_id FROM task_dependencies"
            f" WHERE task_id IN ({placeholders})",
            frontier,
        ):
            src, dst = str(row[0]), str(row[1])
            if dst == task_id:
                # Path: depends_on_id .. src -> task_id; close with new edge.
                path = [src]
                node = src
                while parents[node] is not None:
                    node = parents[node]  # type: ignore[assignment]
                    path.append(node)
                path.reverse()  # depends_on_id .. src
                return [task_id] + path + [task_id]
            if dst not in parents:
                parents[dst] = src
                row_targets.append(dst)
        frontier = row_targets
    return None


# ---------------------------------------------------------------------------
# READY derivation and dispatch ordering
# ---------------------------------------------------------------------------


def recompute_ready(
    conn: TeamCraftConnection, project_id: str | None = None, *, clock: Clock | None = None
) -> dict[str, tuple[str, str]]:
    """Flip ``pending``<->``ready`` from dependency facts; returns changes.

    A task is READY iff every hard dependency is ``done`` (M0 review 8.2-5:
    DONE is the only accepted completion fact).  Only ``pending``/``ready``
    participate — running/verifying/blocked/needs_human/terminal statuses are
    never auto-derived — and every flip bumps ``version`` so optimistic
    callers observe the churn.  Dependency regression (a done dep reopened or
    cancelled) moves a READY task back to PENDING inside the same pass.
    Returns ``{task_id: (old_status, new_status)}`` for changed tasks only.
    """

    require_head(conn)
    now = _utc_iso(clock)
    changes: dict[str, tuple[str, str]] = {}
    with _tx(conn):
        where = "WHERE status IN ('pending', 'ready')"
        params: list[Any] = []
        if project_id is not None:
            where += " AND project_id = ?"
            params.append(project_id)
        candidates = conn.execute(
            _TASK_SELECT + f" {where} ORDER BY id", params
        ).fetchall()
        for row in candidates:
            task = Task(**_row_to(row, _TASK_COLUMNS))
            unfinished = conn.execute(
                "SELECT COUNT(*) FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_id"
                " WHERE d.task_id = ? AND p.status <> 'done'",
                (task.id,),
            ).fetchone()
            target = "ready" if int(unfinished[0]) == 0 else "pending"
            if target != task.status:
                _cas_task(conn, task, status=target, expected_version=task.version, now=now)
                changes[task.id] = (task.status, target)
    return changes


def ready_queue(conn: TeamCraftConnection, project_id: str | None = None) -> list[Task]:
    """Dispatchable READY tasks, ordered priority DESC, created_at ASC, id ASC.

    Excludes READY tasks whose dependencies are not all ``done`` (defensive
    re-derivation) and tasks already holding an occupying attempt — the queue
    is a read, it never reserves (reservation is :func:`create_attempt`).
    """

    require_head(conn)
    where = [
        "t.status = 'ready'",
        # M3-04: a paused Task is not dispatchable — the queue never shows
        # it and :func:`create_attempt` refuses it even if a caller insists.
        "t.paused = 0",
        "NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_id"
        " WHERE d.task_id = t.id AND p.status <> 'done')",
        "NOT EXISTS (SELECT 1 FROM attempts a WHERE a.task_id = t.id" + _OCCUPYING_SQL + ")",
    ]
    params: list[Any] = []
    if project_id is not None:
        where.append("t.project_id = ?")
        params.append(project_id)
    rows = conn.execute(
        "SELECT " + ", ".join("t." + c for c in _TASK_COLUMNS) + " FROM tasks t"
        " WHERE " + " AND ".join(where)
        + " ORDER BY t.priority DESC, t.created_at ASC, t.id ASC",
        params,
    ).fetchall()
    return [Task(**_row_to(row, _TASK_COLUMNS)) for row in rows]


# ---------------------------------------------------------------------------
# Task pause / resume / execution policy (M3-04 controlled transitions)
# ---------------------------------------------------------------------------


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))


def _version_gate(task: Task, expected_version: int) -> None:
    if task.version != expected_version:
        raise VersionConflictError(
            f"task {task.id} version conflict: expected {expected_version},"
            f" found {task.version}",
            entity_id=task.id,
            expected=expected_version,
            found=task.version,
        )


def pause_task(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    expected_version: int,
    actor: str = "",
    reason: str = "",
    clock: Clock | None = None,
) -> Task:
    """CAS-pause a dispatchable Task; the pause is durable and enforced.

    Contract (M3-04 roadmap): pause never touches the Leader lease, bumps
    ``version`` exactly once (so PENDING commands bound to the previous
    evidence version expire through :func:`_cas_task`'s invalidation), emits
    ``task.paused`` linked to project+task, and thereafter
    :func:`ready_queue` hides the task while :func:`create_attempt` refuses
    it with ``TASK_PAUSED`` regardless of what any caller cached.
    """

    require_head(conn)
    task_id = require_entity_id(task_id)
    now = _utc_iso(clock)
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        if bool(task.paused):
            raise _illegal("task", task.status, "pause",
                          reason="TASK_ALREADY_PAUSED")
        if task.status not in PAUSABLE_TASK_STATUSES:
            raise _illegal("task", task.status, "pause",
                          reason="TASK_NOT_PAUSABLE")
        _version_gate(task, expected_version)
        updated = _cas_task(conn, task, expected_version=expected_version,
                            now=now, paused=1)
        events.append_event(
            conn, "task.paused", project_id=task.project_id, task_id=task.id,
            payload_json=_canonical_json({
                "task_id": task.id, "version": updated.version,
                "actor": actor, "reason": reason,
            }),
            clock=clock,
        )
        return updated


def resume_task(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    expected_version: int,
    actor: str = "",
    reason: str = "",
    clock: Clock | None = None,
) -> Task:
    """CAS-unpause a paused Task, re-deriving READY from dependency facts.

    The status a paused task *had* is not memorized in a second flag: on
    resume the hard-dependency fact decides ``pending`` vs ``ready`` exactly
    like :func:`recompute_ready`, so unpausing can never resurrect a stale
    READY view.
    """

    require_head(conn)
    task_id = require_entity_id(task_id)
    now = _utc_iso(clock)
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        if not bool(task.paused):
            raise _illegal("task", task.status, "resume",
                          reason="TASK_NOT_PAUSED")
        _version_gate(task, expected_version)
        target: str | None = None
        if task.status == "pending":
            unfinished = int(conn.execute(
                "SELECT COUNT(*) FROM task_dependencies d"
                " JOIN tasks p ON p.id = d.depends_on_id"
                " WHERE d.task_id = ? AND p.status <> 'done'",
                (task.id,),
            ).fetchone()[0])
            if unfinished == 0:
                target = "ready"
        updated = _cas_task(conn, task, status=target,
                            expected_version=expected_version, now=now,
                            paused=0)
        events.append_event(
            conn, "task.resumed", project_id=task.project_id, task_id=task.id,
            payload_json=_canonical_json({
                "task_id": task.id, "version": updated.version,
                "status": updated.status, "actor": actor, "reason": reason,
            }),
            clock=clock,
        )
        return updated


def set_execution_policy(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    expected_version: int,
    policy: dict,
    actor: str = "",
    reason: str = "",
    clock: Clock | None = None,
) -> Task:
    """Persist the per-task execution policy under optimistic versioning.

    Keys are the closed M3-04 set (delivery/router/verifier/timeout_seconds/
    max_attempts).  A successful write merges into
    ``tasks.execution_policy_json``, CAS-bumps ``version`` (which expires any
    PENDING command bound to the older evidence — policy changes are
    critical edits), mirrors ``max_attempts`` onto the enforced column the
    dispatcher already respects, and emits ``task.policy_updated``.
    """

    require_head(conn)
    task_id = require_entity_id(task_id)
    if not isinstance(policy, dict) or not policy:
        raise ValidationError("execution policy must be a non-empty object")
    unknown = sorted(set(policy) - set(EXECUTION_POLICY_KEYS))
    if unknown:
        raise ValidationError(
            f"unknown execution-policy keys: {', '.join(unknown)}")
    if "delivery" in policy and policy["delivery"] not in DELIVERY_MODES:
        raise ValidationError(
            "delivery must be one of "
            + ", ".join(sorted(DELIVERY_MODES)))
    for name in ("router", "verifier"):
        if name in policy:
            value = policy[name]
            if (not isinstance(value, str) or not value.strip()
                    or len(value.encode("utf-8")) > 256):
                raise ValidationError(f"{name} must be a 1..256 byte string")
    if "timeout_seconds" in policy:
        value = policy["timeout_seconds"]
        if (not isinstance(value, int) or isinstance(value, bool)
                or not 1 <= value <= 3600):
            raise ValidationError("timeout_seconds must be 1..3600")
    if "max_attempts" in policy:
        value = policy["max_attempts"]
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 1):
            raise ValidationError("max_attempts must be an integer >= 1")
    now = _utc_iso(clock)
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            raise _illegal("task", task.status, "update_execution_policy",
                          reason="TASK_TERMINAL")
        _version_gate(task, expected_version)
        try:
            merged = json.loads(task.execution_policy_json or "{}")
        except (TypeError, ValueError):
            merged = {}
        if not isinstance(merged, dict):
            merged = {}
        merged.update(policy)
        fields: dict[str, Any] = {"execution_policy_json": _canonical_json(merged)}
        if "max_attempts" in policy:
            fields["max_attempts"] = int(policy["max_attempts"])
        updated = _cas_task(conn, task, expected_version=expected_version,
                            now=now, **fields)
        events.append_event(
            conn, "task.policy_updated", project_id=task.project_id,
            task_id=task.id,
            payload_json=_canonical_json({
                "task_id": task.id, "version": updated.version,
                "policy": merged, "requested": dict(policy),
                "actor": actor, "reason": reason,
            }),
            clock=clock,
        )
        return updated


# ---------------------------------------------------------------------------
# Attempts (single occupying slot, DB-enforced)
# ---------------------------------------------------------------------------


def get_attempt(conn: TeamCraftConnection, attempt_id: str) -> Attempt:
    require_head(conn)
    row = conn.execute(
        "SELECT " + ", ".join(_ATTEMPT_COLUMNS) + " FROM attempts WHERE id = ?",
        (require_entity_id(attempt_id),),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"attempt not found: {attempt_id}")
    return Attempt(**_row_to(row, _ATTEMPT_COLUMNS))


def occupying_attempt(conn: TeamCraftConnection, task_id: str) -> Attempt | None:
    """The attempt currently holding the task's slot (incl. UNKNOWN), if any."""

    require_head(conn)
    _fetch_task(conn, require_entity_id(task_id))
    row = conn.execute(
        "SELECT " + ", ".join(_ATTEMPT_COLUMNS) + " FROM attempts WHERE task_id = ?"
        + _OCCUPYING_SQL + " LIMIT 1",
        (task_id,),
    ).fetchone()
    return Attempt(**_row_to(row, _ATTEMPT_COLUMNS)) if row is not None else None


def create_attempt(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    expected_version: int,
    agent_id: str | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    protocol_version: str | None = None,
    protocol_hash: str | None = None,
    id_factory: IdFactory | None = None,
    clock: Clock | None = None,
) -> Attempt:
    """Atomically reserve the task slot: attempt ``created`` + READY->RUNNING.

    Guarantees inside one transaction:
    * Task must be ``ready`` at ``expected_version`` (else IllegalTransition/
      VersionConflict, nothing written);
    * hard dependencies are independently re-verified DONE from
      ``task_dependencies`` — status ``ready`` and :func:`ready_queue` are
      advisory derivations, never proof (PLAN 5.1 "事务抢占成功"; CEO review
      M1-04 #2); a violation raises IllegalTransitionError
      (reason ``DEPENDENCIES_NOT_SATISFIED``) with nothing written;
    * the single occupying slot is enforced by the database partial unique
      index — a violated slot (including an unresolved UNKNOWN attempt) maps
      to :class:`AttemptSlotOccupiedError` and the Task CAS is rolled back;
    * attempt_no is monotonically per-task (max + 1).
    """

    require_head(conn)
    task_id = require_entity_id(task_id)
    if (protocol_version is None) != (protocol_hash is None):
        raise ValidationError("protocol version and hash must be supplied together")
    if protocol_version is not None:
        if not isinstance(protocol_version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", protocol_version
        ):
            raise ValidationError("protocol_version must use MAJOR.MINOR.PATCH")
        if not isinstance(protocol_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", protocol_hash
        ):
            raise ValidationError("protocol_hash must be a lowercase SHA-256 hex digest")
    if agent_id is not None:
        agent_id = require_entity_id(agent_id)
    factory = id_factory if id_factory is not None else DEFAULT_ID_FACTORY
    aid = factory.new("attempt")
    now = _utc_iso(clock)
    with _tx(conn):
        task = _fetch_task(conn, task_id)
        targets = task_transition_targets(task.status, "dispatch_reserved", task.kind)
        _resolve_target("task", task.status, "dispatch_reserved", targets, None)
        if bool(task.paused):
            # M3-04 hard gate: pause is honored at the reservation boundary,
            # not just in advisory queues, so no fenced caller can dispatch a
            # paused Task regardless of what it saw earlier.
            raise _illegal("task", task.status, "dispatch_reserved",
                          reason="TASK_PAUSED")
        if task.version != expected_version:
            raise VersionConflictError(
                f"task {task.id} version conflict: expected {expected_version},"
                f" found {task.version}",
                entity_id=task.id,
                expected=expected_version,
                found=task.version,
            )
        _require_deps_done(conn, task, "dispatch_reserved")
        if int(conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task_id,)).fetchone()[0]) >= task.max_attempts:
            raise IllegalTransitionError("MAX_ATTEMPTS", entity="task", current=task.status, event="dispatch_reserved", reason_code="MAX_ATTEMPTS")
        attempt_no = int(
            conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )
        try:
            conn.execute(
                "INSERT INTO attempts (id, task_id, agent_id, attempt_no, status,"
                " worktree_path, branch, started_at, finished_at, commit_sha,"
                " report_ref, result_json, protocol_version, protocol_hash)"
                " VALUES (?, ?, ?, ?, 'created', ?, ?, NULL, NULL, NULL, NULL,"
                " NULL, ?, ?)",
                (aid, task_id, agent_id, attempt_no, worktree_path, branch,
                 protocol_version, protocol_hash),
            )
        except sqlite3.IntegrityError as exc:
            holder = _occupying_attempt_row(conn, task_id)
            if holder is not None:
                raise AttemptSlotOccupiedError(
                    f"task {task_id} already has an occupying attempt"
                    f" (created/starting/active/unknown): {holder[0]}",
                    task_id=task_id,
                    occupying_id=str(holder[0]),
                ) from exc
            if agent_id is not None and conn.execute(
                "SELECT 1 FROM agents WHERE id = ?", (agent_id,)
            ).fetchone() is None:
                raise NotFoundError(f"agent not found: {agent_id}") from exc
            raise ConflictError(f"attempt insert violated integrity: {aid}") from exc
        _cas_task(conn, task, status="running", expected_version=expected_version, now=now)
    return get_attempt(conn, aid)


def transition_attempt(
    conn: TeamCraftConnection,
    attempt_id: str,
    event: str,
    *,
    target: str | None = None,
    result_json: str | None = None,
    clock: Clock | None = None,
) -> Attempt:
    """Apply an event to the attempt machine; UNKNOWN closes only via reconcile."""

    require_head(conn)
    now = _utc_iso(clock)
    with _tx(conn):
        attempt = get_attempt(conn, attempt_id)
        targets = attempt_transition_targets(attempt.status, event)
        resolved = _resolve_target("attempt", attempt.status, event, targets, target)
        fields: dict[str, Any] = {}
        if resolved == "active" and attempt.started_at is None:
            fields["started_at"] = now
        if resolved in TERMINAL_ATTEMPT_STATUSES:
            fields["finished_at"] = now
        if result_json is not None:
            fields["result_json"] = result_json
        assignments = ["status = ?"] + [f"{k} = ?" for k in fields]
        values: list[Any] = [resolved, *fields.values(), attempt.id, attempt.status]
        cursor = conn.execute(
            f"UPDATE attempts SET {', '.join(assignments)}"
            " WHERE id = ? AND status = ?",
            values,
        )
        if cursor.rowcount != 1:
            current = get_attempt(conn, attempt_id)
            raise _illegal("attempt", current.status, event, reason="RACE_RETRY")
    return get_attempt(conn, attempt_id)


def set_attempt_resources(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    worktree_path: str,
    branch: str,
) -> Attempt:
    """Bind the M4-02 resource identity (worktree/branch) onto an Attempt.

    Durable resource identity for the roadmap's "记录资源命名空间": the
    Attempt row carries the exact workspace its execution was routed into,
    alongside the immutable allocation event.  Contract:

    * terminal Attempts are refused — history is never rewritten;
    * an unbound (or provisionally bound: cwd-only, no branch) attempt binds
      the full identity atomically (guarded compare-and-set);
    * re-binding the IDENTICAL full identity is an idempotent replay;
    * re-pointing an Attempt that already carries a FULL resource identity to
      different resources is a loud :class:`ConflictError` (identity is
      immutable per Attempt once bound).
    """

    require_head(conn)
    attempt_id = require_entity_id(attempt_id)
    _require(isinstance(worktree_path, str) and worktree_path.startswith("/")
             and bool(worktree_path.strip("/")),
             "worktree_path must be an absolute non-blank path")
    _require(isinstance(branch, str) and bool(branch.strip()),
             "branch must be a non-blank string")
    with _tx(conn):
        attempt = get_attempt(conn, attempt_id)
        if attempt.status in TERMINAL_ATTEMPT_STATUSES:
            raise _illegal("attempt", attempt.status, "set_resources",
                          reason="ATTEMPT_TERMINAL")
        fully_bound = (attempt.worktree_path is not None
                       and attempt.branch is not None)
        if fully_bound:
            if (attempt.worktree_path == worktree_path
                    and attempt.branch == branch):
                return attempt  # replay
            raise ConflictError(
                f"attempt {attempt_id} already binds resources"
                f" (worktree={attempt.worktree_path!r},"
                f" branch={attempt.branch!r}); refusing to re-point it to"
                f" ({worktree_path!r}, {branch!r})")
        cursor = conn.execute(
            "UPDATE attempts SET worktree_path = ?, branch = ?"
            " WHERE id = ? AND worktree_path IS ? AND branch IS ?",
            (worktree_path, branch, attempt_id,
             attempt.worktree_path, attempt.branch),
        )
        if cursor.rowcount != 1:
            current = get_attempt(conn, attempt_id)
            if (current.worktree_path == worktree_path
                    and current.branch == branch):
                return current
            raise _illegal("attempt", current.status, "set_resources",
                          reason="RACE_RETRY")
    return get_attempt(conn, attempt_id)


# ---------------------------------------------------------------------------
# Sessions (live signals only — never Task completion)
# ---------------------------------------------------------------------------


def register_session(
    conn: TeamCraftConnection,
    attempt_id: str,
    backend: str,
    *,
    native_session_id: str | None = None,
    pane_id: str | None = None,
    state: str = "unknown",
    id_factory: IdFactory | None = None,
    clock: Clock | None = None,
) -> SessionRecord:
    """Record a (possibly not-yet-confirmed) harness session for an attempt.

    Initial state is conservatively ``unknown`` unless the caller supplies an
    observed value; dispatch-submission facts belong to M2 delivery tracking,
    and nothing here touches the Task or Attempt rows.
    """

    require_head(conn)
    attempt_id = require_entity_id(attempt_id)
    _require(bool(backend.strip()), "backend must not be blank")
    _require(state in SESSION_STATES, f"unknown session state: {state!r}")
    factory = id_factory if id_factory is not None else DEFAULT_ID_FACTORY
    sid = factory.new("session")
    now = _utc_iso(clock)
    with _tx(conn):
        if conn.execute("SELECT 1 FROM attempts WHERE id = ?", (attempt_id,)).fetchone() is None:
            raise NotFoundError(f"attempt not found: {attempt_id}")
        conn.execute(
            "INSERT INTO sessions (id, attempt_id, backend, native_session_id, pane_id,"
            " state, last_observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, attempt_id, backend, native_session_id, pane_id, state, now, now),
        )
    return _fetch_session(conn, sid)


def observe_session(
    conn: TeamCraftConnection, session_id: str, state: str, *, clock: Clock | None = None
) -> SessionRecord:
    """Update a session's live signal; ONLY the sessions row is written.

    This is the whole point of the third layer (M0 review 8.2-4): ``idle``,
    ``working``, ``exited`` and ``missing`` are liveness observations.  No
    session observation here can change a Task's id, status or version, nor an
    Attempt's status — Task/Attempt movement only happens through their own
    explicit event APIs (and eventually M2 reconcile/verification evidence).
    """

    require_head(conn)
    session_id = require_entity_id(session_id)
    _require(state in SESSION_STATES, f"unknown session state: {state!r}")
    with _tx(conn):
        session = _fetch_session(conn, session_id)
        if not session_transition_targets(session.state, state):
            raise _illegal("session", session.state, f"observe:{state}")
        conn.execute(
            "UPDATE sessions SET state = ?, last_observed_at = ? WHERE id = ?",
            (state, _utc_iso(clock), session_id),
        )
    return _fetch_session(conn, session_id)


def get_session(conn: TeamCraftConnection, session_id: str) -> SessionRecord:
    require_head(conn)
    return _fetch_session(conn, require_entity_id(session_id))


def _fetch_session(conn: TeamCraftConnection, session_id: str) -> SessionRecord:
    row = conn.execute(
        "SELECT " + ", ".join(_SESSION_COLUMNS) + " FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"session not found: {session_id}")
    return SessionRecord(**_row_to(row, _SESSION_COLUMNS))


__all__ = [
    "ATTEMPT_STATUSES",
    "COMMIT_EVIDENCE_EVENT_KIND",
    "DELIVERY_APPROVAL_EVENT_KIND",
    "DELIVERY_CONTRACT_VERSION",
    "DELIVERY_MODES",
    "DEPENDENCY_EDITABLE_STATUSES",
    "EXECUTION_POLICY_KEYS",
    "LOCAL_DELIVERY_ACCEPT_EVENT",
    "LOCAL_ONLY_ACCEPT_ACTION",
    "OCCUPYING_ATTEMPT_STATUSES",
    "PAUSABLE_TASK_STATUSES",
    "PROTECTED_BRANCH_NAMES",
    "PRIORITY_MAX",
    "PRIORITY_MIN",
    "SESSION_STATES",
    "SHIP_EVIDENCE_EVENT_KIND",
    "TASK_STATUSES",
    "TERMINAL_ATTEMPT_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "Attempt",
    "Project",
    "SessionRecord",
    "Task",
    "add_dependency",
    "apply_task_event",
    "attempt_transition_targets",
    "create_attempt",
    "create_project",
    "create_task",
    "dependencies_of",
    "get_attempt",
    "get_project",
    "get_session",
    "get_task",
    "list_tasks",
    "occupying_attempt",
    "observe_session",
    "pause_task",
    "ready_queue",
    "recompute_ready",
    "register_session",
    "remove_dependency",
    "resume_task",
    "set_attempt_resources",
    "set_execution_policy",
    "require_head",
    "session_transition_targets",
    "task_transition_targets",
    "transition_attempt",
    "update_task",
]
