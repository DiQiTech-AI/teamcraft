"""Pure SQLite Scheduler Core primitives (M2-02 first slice).

This module deliberately has no runtime/Herdr integration and starts no
worker. Callers own the scheduling loop and pass a live project connection.
Every reservation is fenced in the same IMMEDIATE transaction as the Attempt
and READY->RUNNING transition.

M5-03 adds bounded failure escalation around those entry points: the retry
gate, the cloud-escalation authorization surface (including the durable
project cloud budget and its enforcement AT the reservation boundary), and the
CEO wake-budget advisory below.  They consume durable facts (Attempts, final
WorkerReports, confirmed recovery Commands, the M2-09 Wake inbox) through the
pure ``teamcraft.escalation`` decision layer and emit audit Events.  None of
them creates an Attempt, admits/sends a Wake, or calls a provider: no cloud
transport exists in this runtime, so a cloud-intent reservation is refused
fail-closed (unknown usage denies; even a fully authorized escalation is
refused as ``cloud_transport_unavailable``) rather than being silently
relabeled as local execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import timezone
from pathlib import PurePosixPath
import unicodedata

from . import (ceo_initialization, commands, domain, escalation, events, leader,
               recovery, router, wake)
from .clock import Clock, SystemClock
from .errors import NotFoundError, ValidationError
from .ids import require_entity_id
from .storage import TeamCraftConnection, transaction

ACTIVE_MEMBER_PHASES = frozenset({
    "reserved", "split_requested", "split_ambiguous", "pane_created",
    "start_requested", "start_ambiguous", "agent_started",
    "prompt_submitting", "prompt_ambiguous", "prompt_submitted",
    "prompt_skipped",
})


@dataclass(frozen=True)
class DeferredTask:
    task_id: str
    reason: str
    conflicts_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    project_id: str
    selected_task_ids: tuple[str, ...]
    deferred: tuple[DeferredTask, ...]


@dataclass(frozen=True)
class DispatchResult:
    lease: leader.Lease
    attempts: tuple[domain.Attempt, ...]
    deferred: tuple[DeferredTask, ...]


def project_scope(project_id: str) -> str:
    """Canonical long-lived lease scope for a project."""
    return leader.project_scope(project_id)


def acquire_or_renew(
    conn: TeamCraftConnection,
    project_id: str,
    holder_id: str,
    *,
    ttl_seconds: int = 30,
    clock: Clock | None = None,
) -> leader.Lease:
    """Acquire or renew the one Scheduler Leader lease for this project."""
    return leader.acquire_or_renew(
        conn, project_scope(project_id), holder_id,
        ttl_seconds=ttl_seconds, clock=clock,
    )


def release(
    conn: TeamCraftConnection,
    lease: leader.Lease,
    *,
    clock: Clock | None = None,
) -> bool:
    """Release a Scheduler lease while retaining its epoch history."""
    return leader.release(conn, lease.scope, lease.holder_id,
                          epoch=lease.epoch, clock=clock)


# ---------------------------------------------------------------------------
# Dispatch control plane (M3-04): durable pause/resume, Leader-fence-neutral
# ---------------------------------------------------------------------------


def _now_iso(clock: Clock | None) -> str:
    clock = clock if clock is not None else SystemClock()
    return clock.now().astimezone(timezone.utc).isoformat()


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))


def scheduler_state(
    conn: TeamCraftConnection, project_id: str
) -> dict | None:
    """Current durable scheduler control state (``None`` = never touched).

    Read-only and lease-free: pausing is a control-plane fact, observable by
    any caller without contending with the Leader.
    """

    row = conn.execute(
        "SELECT paused, actor, reason, version, updated_at FROM scheduler_controls"
        " WHERE project_id = ?",
        (require_entity_id(project_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "project_id": project_id,
        "paused": bool(int(row[0])),
        "actor": str(row[1]),
        "reason": str(row[2]),
        "version": int(row[3]),
        "updated_at": str(row[4]),
    }


def scheduler_paused(conn: TeamCraftConnection, project_id: str) -> bool:
    state = scheduler_state(conn, project_id)
    return bool(state and state["paused"])


def _set_paused(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    paused: bool,
    actor: str,
    reason: str,
    idempotency_key: str,
    clock: Clock | None,
) -> tuple[dict, commands.Command, bool]:
    """One fenced-agnostic write unit: Command + control row + Event.

    Ordering is deliberate: submit FIRST so a retry of the same key replays
    even if the control row already flipped; a fresh submit then claims,
    upserts the durable flag, emits the linked event and confirms — all in
    the caller's transaction.  The ``leader_lease`` table is never touched:
    pause withholds NEW dispatch decisions only; it cannot steal, release or
    renew any Leader's fencing rights.
    """

    action = "pause" if paused else "resume"
    project_id = require_entity_id(project_id)
    now = _now_iso(clock)
    submission = commands.submit_command(
        conn,
        idempotency_key=idempotency_key,
        kind=f"scheduler.{action}",
        payload_json=_canonical_json({
            "action": action, "project_id": project_id,
            "actor": actor, "reason": reason,
        }),
        # M3-04: project-stamped evidence so the Command's own audit events
        # (submitted/claimed/confirmed) are project-traceable even though no
        # Task is bound.
        evidence_json=_canonical_json({"project_id": project_id}),
        clock=clock,
    )
    command = submission.command
    if submission.reused:
        return (scheduler_state(conn, project_id)
                or {"project_id": project_id, "paused": paused,
                    "actor": actor, "reason": reason, "version": 0,
                    "updated_at": now}), command, True
    commands.claim_command(conn, command.id, clock=clock)
    conn.execute(
        "INSERT INTO scheduler_controls (project_id, paused, actor, reason,"
        " version, updated_at) VALUES (?, ?, ?, ?, 1, ?)"
        " ON CONFLICT(project_id) DO UPDATE SET paused = excluded.paused,"
        " actor = excluded.actor, reason = excluded.reason,"
        " version = scheduler_controls.version + 1,"
        " updated_at = excluded.updated_at",
        (project_id, 1 if paused else 0, actor, reason, now),
    )
    events.append_event(
        conn, f"scheduler.{action}d", project_id=project_id,
        payload_json=_canonical_json({
            "project_id": project_id, "paused": paused,
            "actor": actor, "reason": reason, "command_id": command.id,
        }),
        clock=clock,
    )
    state = scheduler_state(conn, project_id)
    commands.confirm_command(
        conn, command.id,
        result_json=_canonical_json({"project_id": project_id,
                                    "paused": paused}),
        clock=clock,
    )
    # Reload so the returned DTO reflects the CONFIRMED row, not the
    # pre-claim submit snapshot.
    command = commands.get_command(conn, command.id)
    return state, command, False


def pause_schedule(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    actor: str,
    reason: str,
    idempotency_key: str,
    clock: Clock | None = None,
) -> tuple[dict, commands.Command, bool]:
    """Durable project-level dispatch pause (returns state, command, replayed)."""

    if conn.in_transaction:
        return _set_paused(conn, project_id, paused=True, actor=actor,
                          reason=reason, idempotency_key=idempotency_key,
                          clock=clock)
    with transaction(conn):
        return _set_paused(conn, project_id, paused=True, actor=actor,
                          reason=reason, idempotency_key=idempotency_key,
                          clock=clock)


def resume_schedule(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    actor: str,
    reason: str,
    idempotency_key: str,
    clock: Clock | None = None,
) -> tuple[dict, commands.Command, bool]:
    """Durable project-level dispatch resume (returns state, command, replayed)."""

    if conn.in_transaction:
        return _set_paused(conn, project_id, paused=False, actor=actor,
                          reason=reason, idempotency_key=idempotency_key,
                          clock=clock)
    with transaction(conn):
        return _set_paused(conn, project_id, paused=False, actor=actor,
                          reason=reason, idempotency_key=idempotency_key,
                          clock=clock)


def _normalize_claim(claim: str) -> str:
    """Validate and canonicalize one project-relative Ownership claim."""
    if any(unicodedata.category(char) == "Cc" for char in claim):
        raise ValueError(f"invalid ownership claim {claim!r}: control characters are forbidden")
    raw = claim.strip()
    recursive = raw.endswith("/**")
    value = raw[:-3] if recursive else raw
    if not value or value in (".", "./"):
        raise ValueError(f"invalid ownership claim {claim!r}: path must not be empty or dot")
    if value.startswith("/") or "\\" in value:
        raise ValueError(f"invalid ownership claim {claim!r}: use a project-relative POSIX path")
    if any(char in value for char in "*?[]{}"):
        raise ValueError(f"invalid ownership claim {claim!r}: only a trailing /** wildcard is supported")
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(f"invalid ownership claim {claim!r}: empty, dot, and traversal segments are forbidden")
    if any(not part.strip() or any(unicodedata.category(char) == "Cc" for char in part)
           for part in raw_parts):
        raise ValueError(f"invalid ownership claim {claim!r}: blank or control path segments are forbidden")
    parts = PurePosixPath(value).parts
    if not parts:
        raise ValueError(f"invalid ownership claim {claim!r}: path has no segments")
    return "/".join(parts) + ("/**" if recursive else "")


def _parts(claim: str) -> tuple[str, ...]:
    normalized = _normalize_claim(claim)
    value = normalized[:-3] if normalized.endswith("/**") else normalized
    return PurePosixPath(value).parts


def claims_overlap(left: str, right: str) -> bool:
    """Match the legacy Ownership rule: any ancestor/descendant path overlaps."""
    a, b = _parts(left), _parts(right)
    return a[:min(len(a), len(b))] == b[:min(len(a), len(b))]


def _claims(raw: str) -> tuple[str, ...]:
    try:
        values = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid ownership_json: {exc}") from exc
    if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
        raise ValueError("invalid ownership_json")
    return tuple(_normalize_claim(value) for value in values)


def _active_owners(conn: TeamCraftConnection, project_id: str) -> list[tuple[str, tuple[str, ...]]]:
    owners: list[tuple[str, tuple[str, ...]]] = []
    # Runtime members are the existing durable record of read-only status and
    # in-flight lifecycle. Completed/released members no longer own paths.
    rows = conn.execute(
        "SELECT m.name, m.ownership_json, m.read_only, m.phase FROM members m "
        "LEFT JOIN tasks t ON t.id=m.task_id "
        "WHERE t.project_id=? OR m.task_id IS NULL",
        (project_id,),
    ).fetchall()
    for row in rows:
        name, ownership_json, read_only, phase = str(row[0]), str(row[1]), bool(row[2]), str(row[3])
        if read_only or phase not in ACTIVE_MEMBER_PHASES:
            continue
        claims = _claims(ownership_json)
        if claims:
            owners.append((f"member:{name}", claims))

    # Attempts created by the domain API can exist without a runtime member.
    # Preserve their ownership unless their only active associated members are
    # read-only. The attempt still counts toward quota in plan().
    rows = conn.execute(
        "SELECT DISTINCT t.id, t.ownership_json FROM tasks t JOIN attempts a ON a.task_id=t.id "
        "WHERE t.project_id=? AND upper(a.status) IN ('CREATED','STARTING','ACTIVE','UNKNOWN')",
        (project_id,),
    ).fetchall()
    for row in rows:
        associated = conn.execute(
            "SELECT read_only, phase FROM members WHERE task_id=?",
            (str(row[0]),),
        ).fetchall()
        active_associated = [m for m in associated if str(m[1]) in ACTIVE_MEMBER_PHASES]
        if active_associated and all(bool(m[0]) for m in active_associated):
            continue
        claims = _claims(str(row[1]))
        if claims:
            owners.append((f"task:{row[0]}", claims))
    return owners


def _plan_snapshot(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    concurrency_limit: int,
) -> Plan:
    """Build a plan from the caller's already-open consistent snapshot."""
    if not isinstance(concurrency_limit, int) or isinstance(concurrency_limit, bool) or concurrency_limit <= 0:
        raise ValueError("concurrency_limit must be a positive integer")
    queue = domain.ready_queue(conn, project_id)
    # M3-04 dispatch gate: while the project's durable control row says
    # paused, NO task is selected — every otherwise-ready task is deferred
    # with an explicit reason so the pause is observable, not silent.
    if scheduler_paused(conn, project_id):
        return Plan(project_id, (),
                    tuple(DeferredTask(task.id, "scheduler_paused")
                          for task in queue))
    owners = _active_owners(conn, project_id)
    occupying = int(conn.execute(
        "SELECT COUNT(*) FROM attempts a JOIN tasks t ON t.id=a.task_id "
        "WHERE t.project_id=? AND upper(a.status) IN ('CREATED','STARTING','ACTIVE','UNKNOWN')",
        (project_id,),
    ).fetchone()[0])
    slots = max(0, concurrency_limit - occupying)
    selected: list[domain.Task] = []
    deferred: list[DeferredTask] = []
    for task in queue:
        attempts_used = int(conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0])
        if attempts_used >= task.max_attempts:
            deferred.append(DeferredTask(task.id, "max_attempts_reached"))
            continue
        # M5-03 bounded retry gate: a re-dispatch after a prior Attempt is a
        # retry, and retries are only plannable from settled, durably approved
        # facts.  UNKNOWN/in-flight ambiguity defers fail-closed.
        allowed, gate = _retry_reservation_allowed(conn, task, attempts_used)
        if not allowed:
            deferred.append(DeferredTask(task.id, gate.reason_code))
            continue
        # M5-03 cloud gate at the actual reservation boundary: when the
        # durable project budget plus a task-bound cloud approval put this
        # Task on a cloud route, approval/budget/transport are enforced
        # here — fail closed, never a relabeled local dispatch.
        cloud_ok, cloud_gate = _cloud_reservation_gate(conn, project_id,
                                                      task.id)
        if not cloud_ok:
            assert cloud_gate is not None
            deferred.append(DeferredTask(task.id, cloud_gate.reason_code))
            continue
        claims = _claims(task.ownership_json)
        conflicts = sorted({
            owner for owner, existing in owners
            if any(claims_overlap(left, right) for left in claims for right in existing)
        })
        # Also prevent two tasks chosen in this same plan from claiming overlap.
        conflicts.extend(
            f"task:{prior.id}" for prior in selected
            if any(claims_overlap(left, right)
                   for left in claims for right in _claims(prior.ownership_json))
        )
        if conflicts:
            deferred.append(DeferredTask(task.id, "ownership_conflict", tuple(sorted(set(conflicts)))))
        elif len(selected) >= slots:
            deferred.append(DeferredTask(task.id, "concurrency_quota"))
        else:
            selected.append(task)
    return Plan(project_id, tuple(t.id for t in selected), tuple(deferred))


def plan(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    concurrency_limit: int,
) -> Plan:
    """Read-only deterministic plan from one stable SQLite snapshot."""
    with transaction(conn, immediate=False):
        return _plan_snapshot(conn, project_id, concurrency_limit=concurrency_limit)


def dispatch_ready(
    conn: TeamCraftConnection,
    project_id: str,
    lease: leader.Lease,
    *,
    concurrency_limit: int,
    clock: Clock | None = None,
) -> DispatchResult:
    """Reserve eligible READY tasks atomically, rechecking fencing at write time."""
    if lease.scope != project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    if not isinstance(concurrency_limit, int) or isinstance(concurrency_limit, bool) or concurrency_limit <= 0:
        raise ValueError("concurrency_limit must be a positive integer")
    attempts: list[domain.Attempt] = []
    deferred: list[DeferredTask] = []
    with transaction(conn):
        # Recompute under the IMMEDIATE lock. No gap exists between checking
        # quota/ownership and making each reservation.
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        ceo_initialization.require_initialized(conn, project_id)
        for task in domain.list_tasks(conn, project_id, status="running"):
            used = int(conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task.id,)).fetchone()[0])
            if used >= task.max_attempts and domain.occupying_attempt(conn, task.id) is None:
                recovery.escalate_attempt_limit(conn, task.id, lease=lease, clock=clock)
        current = _plan_snapshot(conn, project_id, concurrency_limit=concurrency_limit)
        deferred.extend(current.deferred)
        for item in current.deferred:
            if item.reason == "max_attempts_reached":
                task = domain.get_task(conn, item.task_id)
                leader.require_project_lease(conn, lease, project_id, clock=clock)
                if task.status == "ready":
                    recovery.escalate_attempt_limit(conn, task.id, lease=lease, clock=clock)
        for task_id in current.selected_task_ids:
            # The domain API joins this transaction. Fence immediately at the
            # write boundary before each reservation as well as at admission.
            leader.require_project_lease(conn, lease, project_id, clock=clock)
            task = domain.get_task(conn, task_id)
            # M5-03: re-audit the bounded-retry facts at the write boundary,
            # same as the fence.  A task whose retry evidence vanished (or
            # became ambiguous) between snapshot and reservation is deferred
            # instead of reserved — fail closed, never a silent third try.
            used_now = int(conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task_id,),
            ).fetchone()[0])
            allowed, gate = _retry_reservation_allowed(conn, task, used_now)
            if not allowed:
                deferred.append(DeferredTask(task_id, gate.reason_code))
                continue
            cloud_ok, cloud_gate = _cloud_reservation_gate(conn, project_id,
                                                          task_id)
            if not cloud_ok:
                assert cloud_gate is not None
                deferred.append(DeferredTask(task_id, cloud_gate.reason_code))
                continue
            attempts.append(domain.create_attempt(
                conn, task_id, expected_version=task.version, clock=clock,
            ))
    return DispatchResult(lease, tuple(attempts), tuple(deferred))


class Scheduler:
    """Explicit long-lived lease/tick interface for an embedding service."""

    def __init__(self, conn: TeamCraftConnection, project_id: str,
                 holder_id: str, *, concurrency_limit: int,
                 ttl_seconds: int = 30, clock: Clock | None = None) -> None:
        if not isinstance(concurrency_limit, int) or isinstance(concurrency_limit, bool) or concurrency_limit <= 0:
            raise ValueError("concurrency_limit must be a positive integer")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.conn = conn
        self.project_id = project_id
        self.holder_id = holder_id
        self.concurrency_limit = concurrency_limit
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.lease: leader.Lease | None = None

    def acquire_or_renew(self) -> leader.Lease:
        self.lease = acquire_or_renew(
            self.conn, self.project_id, self.holder_id,
            ttl_seconds=self.ttl_seconds, clock=self.clock,
        )
        return self.lease

    def renew(self) -> leader.Lease:
        return self.acquire_or_renew()

    def release(self) -> bool:
        if self.lease is None:
            return False
        released = release(self.conn, self.lease, clock=self.clock)
        if released:
            self.lease = None
        return released

    def tick(self) -> DispatchResult:
        """Renew, persist new event Wakes, then make one reservation pass."""
        lease = self.acquire_or_renew()
        # Event intake is a local SQLite operation and remains useful while
        # CEO execution is gated or busy.  It never sends a prompt; delivery
        # admission is a separate, M2-08-gated operation.
        # Housekeeping (M3-04): PENDING commands whose TTL elapsed or whose
        # bound evidence moved on are expired with audit events, so recorded
        # intents can never linger invisibly forever.  Runs BEFORE wake
        # intake so housekeeping holds even while initialization gates the
        # rest of the tick.
        commands.sweep_stale_commands(self.conn, clock=self.clock)
        wake.ingest_events(self.conn, self.project_id, lease, clock=self.clock)
        return dispatch_ready(
            self.conn, self.project_id, lease,
            concurrency_limit=self.concurrency_limit, clock=self.clock,
        )


# ---------------------------------------------------------------------------
# M5-03 bounded retry gate (durable facts -> escalation decisions)
# ---------------------------------------------------------------------------


def _latest_failure_fact(
    conn: TeamCraftConnection, task_id: str,
) -> escalation.FailureFact | None:
    """Settled facts of the most recent Attempt for one Task (read-only)."""
    row = conn.execute(
        "SELECT id, status FROM attempts WHERE task_id = ?"
        " ORDER BY attempt_no DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    outcome_row = conn.execute(
        "SELECT outcome FROM worker_reports WHERE attempt_id = ?"
        " AND report_kind = 'final' ORDER BY recorded_at DESC, id DESC LIMIT 1",
        (str(row[0]),),
    ).fetchone()
    outcome = (None if outcome_row is None or outcome_row[0] is None
              else str(outcome_row[0]))
    return escalation.FailureFact(attempt_status=str(row[1]), outcome=outcome)


def _durable_retry_approval_present(conn: TeamCraftConnection,
                                   task_id: str) -> bool:
    """Whether a CONFIRMED ``recovery.retry_approved`` Command binds this Task.

    Durability comes from the Command lifecycle (M2-05): only
    :func:`teamcraft.recovery.approve_retry` writes this kind, and it refuses
    while an Attempt occupies the slot, at the cap, or without retryable
    prior evidence.  Corrupt/unparsable rows never count as approval.
    """
    rows = conn.execute(
        "SELECT payload_json, result_json FROM commands"
        " WHERE kind = 'recovery.retry_approved' AND state = 'confirmed'"
        " ORDER BY requested_at, id",
    ).fetchall()
    for payload_json, result_json in rows:
        try:
            payload = json.loads(str(payload_json))
            result = json.loads(str(result_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if (isinstance(payload, dict) and payload.get("task_id") == task_id
                and isinstance(result, dict)
                and result.get("approved") is True):
            return True
    return False


def _retry_decision(conn: TeamCraftConnection, task: domain.Task,
                    attempts_used: int) -> escalation.AttemptDecision:
    budget = escalation.AttemptBudget(
        max_attempts=task.max_attempts,
        # A corrupted used>max row must not crash planning; clamp so the
        # decision is the conservative "exhausted" shape instead.
        attempts_used=min(attempts_used, task.max_attempts),
    )
    latest = _latest_failure_fact(conn, task.id)
    approved = _durable_retry_approval_present(conn, task.id)
    return escalation.decide_attempt(budget, latest, retry_approved=approved)


def retry_gate(conn: TeamCraftConnection,
               task_id: str) -> escalation.AttemptDecision:
    """Auditable bounded-retry decision for one Task from durable facts.

    Read-only and lease-free (like :func:`scheduler_state`): UNKNOWN never
    permits a new Attempt, exhaustion maps to NEEDS_HUMAN, and only an
    explicit durable approval yields ``retry`` with
    ``new_attempt_permitted=True``.  Callers still must go through the fenced
    dispatch path — nothing here reserves anything.
    """
    task = domain.get_task(conn, task_id)
    used = int(conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task.id,),
    ).fetchone()[0])
    return _retry_decision(conn, task, used)


def _retry_reservation_allowed(
    conn: TeamCraftConnection, task: domain.Task, attempts_used: int,
) -> tuple[bool, escalation.AttemptDecision]:
    """Gate one READY->RUNNING reservation on the M5-03 bounded-retry rules."""
    decision = _retry_decision(conn, task, attempts_used)
    if decision.new_attempt_permitted:
        return True, decision
    if decision.action == "none" and decision.classification is None:
        # Initial dispatch: no Attempt on record; quotas/ownership decide.
        return True, decision
    # Ambiguity and exhaustion are never dispatchable here, approval or not:
    # UNKNOWN must be settled by verified reconciliation (M2-05) and an
    # exhausted budget belongs to a human, not a third silent attempt.
    if decision.reason_code in {"attempt_in_flight", "unknown_never_auto_retry",
                                "attempt_budget_exhausted"}:
        return False, decision
    # A durable confirmed ``recovery.retry_approved`` Command is itself the
    # M2-05 disposition: approve_retry re-derived retryability (including the
    # missing-report and verifier-failure shapes) under its own gates before
    # it made the Task READY again.  Honor it at the dispatch boundary too.
    approved = _durable_retry_approval_present(conn, task.id)
    if (approved and decision.classification
            in {"retryable_failure", "unreported", "completed"}):
        return True, decision
    return False, decision


# ---------------------------------------------------------------------------
# M5-03 cloud escalation authorization (approval + conservative budget)
# ---------------------------------------------------------------------------

#: Scheduler-owned durable approval Command kind for one cloud route.  The
#: approval binds the EXACT harness/provider/model selection AND the exact
#: budget caps it authorizes; a different route or cap needs its own approval.
CLOUD_APPROVAL_COMMAND_KIND = "cloud.escalation_approved"
CLOUD_APPROVAL_EVENT = "escalation.cloud_approved"
CLOUD_DECISION_EVENT = "escalation.cloud_decision"


def _sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def approve_cloud_escalation(
    conn: TeamCraftConnection,
    project_id: str,
    task_id: str,
    *,
    selection: router.RouteSelection,
    limits: escalation.BudgetLimits,
    actor: str,
    reason: str,
    idempotency_key: str,
    lease: leader.Lease,
    clock: Clock | None = None,
) -> tuple[commands.Command, bool]:
    """Durable, human, route-bound cloud-escalation approval.

    Returns ``(command, replayed)``.  Mirrors the M2-05 approval discipline:
    fenced by the project Leader lease, idempotent per key, confirmed with its
    binding digest, and audited by one event.  ``escalation.CloudApproval``
    re-validates the audit text (bounded, no credential-shaped content).
    This records authorization evidence ONLY — it never dispatches, never
    creates an Attempt, and calls no provider.
    """
    approval = escalation.CloudApproval(selection=selection, actor=actor,
                                       reason=reason)
    project_id = require_entity_id(project_id)

    def _grant() -> tuple[commands.Command, bool]:
        domain.get_project(conn, project_id)  # uniform NotFound first
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        task = domain.get_task(conn, task_id)
        if task.project_id != project_id:
            raise ValidationError("approved task does not belong to this project")
        if not isinstance(limits, escalation.BudgetLimits):
            raise ValidationError("limits must be a BudgetLimits")
        binding = {
            "project_id": project_id, "task_id": task.id,
            "selection": asdict(selection), "limits": limits.payload(),
        }
        digest = _sha256({"contract": "cloud-escalation-approval-v1",
                          **binding})
        submission = commands.submit_command(
            conn, idempotency_key=idempotency_key,
            kind=CLOUD_APPROVAL_COMMAND_KIND,
            payload_json=_canonical_json({
                **binding, "actor": approval.actor, "reason": approval.reason,
                "binding_digest": digest,
            }),
            evidence_json=_canonical_json({
                "project_id": project_id, "task_id": task.id,
                "task_version": task.version, "digest": digest,
            }),
            clock=clock,
        )
        command = submission.command
        if submission.reused and command.state == "confirmed":
            return commands.get_command(conn, command.id), True
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        elif command.state != "running":
            raise ValidationError(
                "cloud approval Command is"
                f" {command.state}; reconcile before re-approving")
        events.append_event(
            conn, CLOUD_APPROVAL_EVENT, project_id=project_id, task_id=task.id,
            payload_json=_canonical_json({
                "command_id": command.id, **binding,
                "actor": approval.actor, "reason": approval.reason,
                "binding_digest": digest,
            }),
            clock=clock,
        )
        commands.confirm_command(
            conn, command.id,
            result_json=_canonical_json({"approved": True, "task_id": task.id,
                                        "binding_digest": digest}),
            clock=clock,
        )
        return commands.get_command(conn, command.id), False

    if conn.in_transaction:
        return _grant()
    with transaction(conn):
        return _grant()


def _durable_cloud_approval(
    conn: TeamCraftConnection, project_id: str, task_id: str,
) -> tuple[escalation.CloudApproval | None, dict | None, str | None]:
    """Latest confirmed approval Command for (project, task).

    Returns ``(approval, stored_limits, requested_at)``;
    ``(None, None, None)`` when no usable durable approval exists (missing,
    unconfirmed, or corrupt -> fail closed).
    """
    rows = conn.execute(
        "SELECT payload_json, result_json, requested_at FROM commands WHERE kind = ?"
        " AND state = 'confirmed' ORDER BY requested_at, id",
        (CLOUD_APPROVAL_COMMAND_KIND,),
    ).fetchall()
    latest: dict | None = None
    latest_at: str | None = None
    for payload_json, result_json, requested_at in rows:
        try:
            payload = json.loads(str(payload_json))
            result = json.loads(str(result_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if (isinstance(payload, dict) and payload.get("project_id") == project_id
                and payload.get("task_id") == task_id
                and isinstance(result, dict) and result.get("approved") is True
                and result.get("binding_digest") == payload.get("binding_digest")):
            latest = payload
            latest_at = str(requested_at)
    if latest is None:
        return None, None, None
    try:
        selection = router.RouteSelection(**latest["selection"])
        approval = escalation.CloudApproval(
            selection=selection, actor=str(latest["actor"]),
            reason=str(latest["reason"]))
    except (KeyError, TypeError, ValidationError):
        return None, None, None
    stored_limits = latest.get("limits")
    return approval, (stored_limits if isinstance(stored_limits, dict) else {}), latest_at


def _last_cloud_decision_digest(conn: TeamCraftConnection,
                               task_id: str) -> str | None:
    row = conn.execute(
        "SELECT payload_json FROM events WHERE kind = ? AND task_id = ?"
        " ORDER BY seq DESC LIMIT 1",
        (CLOUD_DECISION_EVENT, task_id),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, json.JSONDecodeError):
        return None
    digest = payload.get("decision_digest") if isinstance(payload, dict) else None
    return str(digest) if isinstance(digest, str) else None


def evaluate_cloud_escalation(
    conn: TeamCraftConnection,
    project_id: str,
    task_id: str,
    *,
    selection: router.RouteSelection,
    locality: str,
    limits: escalation.BudgetLimits,
    used: escalation.TokenUsage,
    projected: escalation.TokenUsage | None = None,
    policy: escalation.EscalationPolicy | None = None,
    clock: Clock | None = None,
) -> escalation.CloudDecision:
    """Conservative, fail-closed cloud authorization for one Task.

    Fixed order (owned by ``escalation.authorize_cloud_escalation``):
    locality, unresolved UNKNOWN Attempt, durable approval presence and exact
    route binding, then the budget.  Unknown usage is denied exactly like
    exceeded usage (never summed as zero or treated as free); a null cap is
    reported as unconstrained, never as proven headroom.  Additionally, the
    budget caps presented here must match the caps pinned into the durable
    approval (``approval_limits_mismatch`` otherwise).

    The decision is evidence ONLY: even ``authorized`` reserves nothing.  No
    cloud transport exists in this runtime, so there is no provider to call;
    any future dispatcher must still pass the READY state, Leader fence,
    quotas, and :func:`dispatch_ready`'s reservation path.  Identical inputs
    replay to an identical decision digest; the audit event is written only
    when the digest changes, so repeated polling cannot spam the Wake stream.
    """
    policy = policy if policy is not None else escalation.EscalationPolicy()
    if not isinstance(selection, router.RouteSelection):
        raise ValidationError("selection must be a RouteSelection")
    task_id = require_entity_id(task_id)

    def _read_facts():
        task = domain.get_task(conn, task_id)
        if task.project_id != project_id:
            raise ValidationError("task does not belong to this project")
        return _latest_failure_fact(conn, task.id)

    if conn.in_transaction:
        latest = _read_facts()
        approval, stored_limits, _requested_at = _durable_cloud_approval(
            conn, project_id, task_id)
    else:
        with transaction(conn, immediate=False):
            latest = _read_facts()
            approval, stored_limits, _requested_at = _durable_cloud_approval(
                conn, project_id, task_id)

    request = escalation.CloudEscalationRequest(
        selection=selection, locality=locality, limits=limits, used=used,
        projected=projected, approval=approval, latest=latest,
    )
    decision = escalation.authorize_cloud_escalation(request, policy=policy)
    if (decision.action == "authorized" and approval is not None
            and stored_limits != limits.payload()):
        decision = escalation.CloudDecision(
            action="denied", reason_code="approval_limits_mismatch",
            budget=decision.budget, approval_applied=True,
            usage_known=decision.usage_known,
        )

    payload = decision.payload()
    payload["task_id"] = task_id
    payload["selection"] = asdict(selection)
    payload["decision_digest"] = decision.decision_digest
    payload["effect"] = "evidence_only_no_dispatch"
    digest = decision.decision_digest

    def _audit() -> None:
        if _last_cloud_decision_digest(conn, task_id) == digest:
            return
        events.append_event(
            conn, CLOUD_DECISION_EVENT, project_id=project_id, task_id=task_id,
            payload_json=_canonical_json(payload), clock=clock,
        )

    if conn.in_transaction:
        _audit()
    else:
        with transaction(conn):
            _audit()
    return decision


# ---------------------------------------------------------------------------
# M5-03 durable project cloud budget + reservation-boundary enforcement
# ---------------------------------------------------------------------------

#: Durable source of truth for the project cloud budget (the wake-budget
#: precedent, M5-03): the newest ``scheduler.cloud_budget_config`` Event is
#: authoritative.  The ``scheduler.`` prefix is on M2-09's wake-ingestion
#: ignore list, so recording or lifting a budget can never wake the CEO.
CLOUD_BUDGET_CONFIG_EVENT = "scheduler.cloud_budget_config"
_CLOUD_CONFIG_ACTOR_MAX_BYTES = 64
_CLOUD_CONFIG_REASON_MAX_BYTES = 512

#: The exact cloud-route text sanitizer from the router/escalation convention,
#: applied to budget-config audit text before persistence.
_CONFIG_SECRET_TEXT = re.compile(
    r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+",
    re.I,
)


def _config_audit_text(value: str, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must not be blank")
    text = value.strip()
    if len(text.encode("utf-8")) > limit:
        raise ValidationError(f"{field} exceeds {limit} bytes")
    if _CONFIG_SECRET_TEXT.search(text):
        raise ValidationError(f"{field} contains credential-shaped text")
    return text


@dataclass(frozen=True)
class CloudBudgetConfig:
    """Persisted per-project cloud caps and approval posture.

    ``enabled=False`` is an explicit auditable lift: the reservation-boundary
    cloud gate stops applying to new dispatches.  A null cap is
    *unconstrained*, never proven headroom (that distinction is owned by
    ``escalation.evaluate_budget``).
    """

    limits: escalation.BudgetLimits
    require_approval: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.limits, escalation.BudgetLimits):
            raise ValidationError("cloud budget limits must be a BudgetLimits")
        if not isinstance(self.require_approval, bool):
            raise ValidationError("require_approval must be boolean")
        if not isinstance(self.enabled, bool):
            raise ValidationError("enabled must be boolean")

    def payload(self) -> dict:
        return {
            "schema_version": 1,
            "limits": self.limits.payload(),
            "require_approval": self.require_approval,
            "enabled": self.enabled,
        }


def configure_cloud_budget(
    conn: TeamCraftConnection,
    project_id: str,
    lease: leader.Lease,
    *,
    limits: escalation.BudgetLimits,
    actor: str,
    reason: str,
    require_approval: bool = True,
    enabled: bool = True,
    clock: Clock | None = None,
) -> CloudBudgetConfig:
    """Durably set the project cloud budget; the newest record is authoritative.

    One audited ``scheduler.cloud_budget_config`` Event under the project
    Leader lease — no Command, no transport, no provider.  Because the budget
    lives in the durable event store, EVERY later :func:`plan` /
    :func:`dispatch_ready` enforces it even if its caller knows nothing about
    budget parameters; changing or lifting a budget is itself lease-fenced
    and actor-attributed, never a silent per-call bypass by omission.
    Inputs are validated before any write so a bad posture cannot persist
    halfway.
    """
    domain.require_head(conn)
    project_id = require_entity_id(project_id)
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project",
                                     scope=lease.scope, epoch=lease.epoch)
    audit_actor = _config_audit_text(actor, "actor",
                                    limit=_CLOUD_CONFIG_ACTOR_MAX_BYTES)
    audit_reason = _config_audit_text(reason, "reason",
                                     limit=_CLOUD_CONFIG_REASON_MAX_BYTES)
    proposed = CloudBudgetConfig(limits=limits,
                                require_approval=require_approval,
                                enabled=enabled)
    body = _canonical_json({**proposed.payload(),
                            "actor": audit_actor, "reason": audit_reason})
    if conn.in_transaction:
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        events.append_event(conn, CLOUD_BUDGET_CONFIG_EVENT,
                            project_id=project_id, payload_json=body,
                            clock=clock)
        return proposed
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        events.append_event(conn, CLOUD_BUDGET_CONFIG_EVENT,
                            project_id=project_id, payload_json=body,
                            clock=clock)
        return proposed


def get_cloud_budget(
    conn: TeamCraftConnection, project_id: str,
) -> CloudBudgetConfig | None:
    """Return the newest durable cloud budget, or ``None`` when unset.

    A present-but-invalid record raises (fail closed): dispatch must not
    silently run unenforced because the audit row could not be understood.
    """
    domain.require_head(conn)
    row = conn.execute(
        "SELECT payload_json FROM events WHERE kind = ? AND project_id = ?"
        " ORDER BY seq DESC LIMIT 1",
        (CLOUD_BUDGET_CONFIG_EVENT, project_id),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise TypeError("config payload is not an object")
        return CloudBudgetConfig(
            limits=escalation.BudgetLimits(**payload["limits"]),
            require_approval=payload["require_approval"],
            enabled=payload["enabled"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ValidationError(
            "persisted cloud budget config is invalid; repair it before "
            "dispatching") from exc


def _durable_token_usage(
    conn: TeamCraftConnection, project_id: str, task_id: str,
) -> escalation.TokenUsage:
    """Durable token usage for one Task, null-aware.

    M5-04 (per-Attempt usage records with source and exact/estimated flags)
    does not exist yet, so this runtime persists no usage anywhere and the
    only honest answer is all-NULL — UNKNOWN.  The reservation gate must
    treat that as unknown (never zero, never free), which is exactly why a
    cloud dispatch without usage evidence is conservatively refused today.
    When M5-04 lands usage rows, this seam aggregates them per Task without
    substituting nulls.
    """
    return escalation.TokenUsage()


def _latest_retry_approval_requested_at(conn: TeamCraftConnection,
                                       task_id: str) -> str | None:
    """``requested_at`` of the newest confirmed local retry approval."""
    rows = conn.execute(
        "SELECT payload_json, requested_at FROM commands"
        " WHERE kind = 'recovery.retry_approved' AND state = 'confirmed'"
        " ORDER BY requested_at, id",
    ).fetchall()
    latest: str | None = None
    for payload_json, requested_at in rows:
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            latest = str(requested_at)
    return latest


def _cloud_reservation_gate(
    conn: TeamCraftConnection, project_id: str, task_id: str,
) -> tuple[bool, escalation.CloudDecision | None]:
    """Cloud approval+budget enforcement AT the reservation boundary.

    Returns ``(reservation_allowed, decision_or_None)``:

    * no durable budget configured, or an explicit lift — cloud is not in
      play; local-first dispatch stands (the bounded retry gate above still
      requires the human ``recovery.retry_approved`` disposition for any
      retry);
    * no task-bound cloud approval — no cloud intent exists, and a local
      reservation spends no cloud budget;
    * a confirmed local retry approval STRICTLY NEWER than the cloud
      approval — the human re-directed this retry to local; the cloud
      intent is superseded;
    * otherwise the durable cloud approval binds this Task to a cloud route
      and the full conservative evaluation runs against the CONFIGURED
      project caps: unresolved UNKNOWN Attempt denies; missing approval
      denies; unknown usage denies (never summed as zero); exceeded caps
      deny; caps that no longer match what the approval was pinned against
      deny (``approval_limits_mismatch``); and even a fully authorized
      escalation denies with ``cloud_transport_unavailable`` because this
      runtime has no cloud transport and the scheduler refuses to relabel
      cloud-bound work as local execution.

    Every denial defers the reservation fail-closed: no Attempt row, no
    provider call, and the reason code is visible on ``Plan.deferred`` /
    ``DispatchResult.deferred``.
    """
    config = get_cloud_budget(conn, project_id)
    if config is None or not config.enabled:
        return True, None
    approval, stored_limits, requested_at = _durable_cloud_approval(
        conn, project_id, task_id)
    if approval is None:
        return True, None
    local_at = _latest_retry_approval_requested_at(conn, task_id)
    if local_at is not None and requested_at is not None \
            and local_at > requested_at:
        return True, None
    decision = escalation.authorize_cloud_escalation(
        escalation.CloudEscalationRequest(
            selection=approval.selection, locality="cloud",
            limits=config.limits,
            used=_durable_token_usage(conn, project_id, task_id),
            approval=approval,
            latest=_latest_failure_fact(conn, task_id),
        ),
        policy=escalation.EscalationPolicy(
            cloud_escalation_requires_approval=config.require_approval),
    )
    if decision.action == "authorized":
        if stored_limits != config.limits.payload():
            decision = escalation.CloudDecision(
                action="denied", reason_code="approval_limits_mismatch",
                budget=decision.budget, approval_applied=True,
                usage_known=decision.usage_known,
            )
        else:
            # Approval and budget are proven — but there is no cloud
            # transport in this runtime, so honoring the cloud intent here
            # is impossible and silently executing locally instead would
            # betray exactly what was approved.  Fail closed, loudly audited.
            decision = escalation.CloudDecision(
                action="denied", reason_code="cloud_transport_unavailable",
                budget=decision.budget, approval_applied=True,
                usage_known=decision.usage_known,
            )
    return False, decision


def cloud_dispatch_decision(
    conn: TeamCraftConnection, project_id: str, task_id: str,
) -> escalation.CloudDecision | None:
    """Read-only audit view of the reservation-boundary cloud gate.

    ``None`` means cloud is not in play for this Task (no budget configured,
    explicit lift, no task-bound cloud intent, or a newer local retry
    disposition).  Read-only: reserves and emits nothing.
    """
    _allowed, decision = _cloud_reservation_gate(conn, project_id, task_id)
    return decision


# ---------------------------------------------------------------------------
# M5-03 CEO wake budget advisory (M2-09 merge windows)
# ---------------------------------------------------------------------------


def pending_wake_budget_decisions(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    max_wakes_per_window: int,
    policy: escalation.EscalationPolicy | None = None,
    target_ceo_name: str = "ceo",
) -> tuple[tuple[wake.Wake, escalation.WakeDecision], ...]:
    """Read-only wake-budget decisions for pending CEO wakes (fail-closed).

    ``max_wakes_per_window`` is the operator-set ceiling for one M2-09 merge
    window.  Rows already in flight/delivered in the same window count
    against it, and candidates consume the ceiling in delivery order
    (priority desc, then event order).  An urgent wake that cannot be
    afforded escalates to ``needs_human`` instead of being silently delayed,
    and an unreachable CEO session isolates exactly like the M2-09 admission
    gate — no replacement session is ever implied.

    Advisory ONLY: nothing is admitted or sent.  Actual delivery still
    requires :func:`teamcraft.wake.admit_pending_wake` (Leader lease +
    M2-08 initialization), which remains the sole pre-I/O boundary.
    """
    if (isinstance(max_wakes_per_window, bool)
            or not isinstance(max_wakes_per_window, int)
            or max_wakes_per_window < 0):
        raise ValueError("max_wakes_per_window must be a non-negative integer")
    policy = policy if policy is not None else escalation.EscalationPolicy()

    ceo_state = "missing"
    initialization = ceo_initialization.get_initialization(conn, project_id)
    if initialization is not None and initialization.teamcraft_session_id:
        try:
            ceo_state = domain.get_session(
                conn, initialization.teamcraft_session_id).state
        except NotFoundError:
            ceo_state = "missing"

    pending = wake.list_wakes(conn, project_id,
                              target_ceo_name=target_ceo_name,
                              states=("pending",))
    consumed: dict[str, int] = {}
    results: list[tuple[wake.Wake, escalation.WakeDecision]] = []
    for item in pending:  # priority DESC, first_seq ASC, id ASC
        window = item.window_start
        if window not in consumed:
            already_sent = int(conn.execute(
                "SELECT COUNT(*) FROM wakes WHERE project_id = ?"
                " AND target_ceo_name = ? AND window_start = ?"
                " AND state IN"
                " ('sending','submitted','acknowledged','processed')",
                (project_id, target_ceo_name, window),
            ).fetchone()[0])
            consumed[window] = min(already_sent, max_wakes_per_window)
        budget = escalation.WakeBudget(
            max_wakes_per_window=max_wakes_per_window,
            wakes_sent_in_window=consumed[window],
            window_id=window,
        )
        decision = escalation.decide_wake(
            escalation.WakeRequest(priority=item.priority, ceo_state=ceo_state,
                                  budget=budget,
                                  coalesced_events=item.event_count),
            policy=policy,
        )
        if decision.consumes_budget:
            consumed[window] += 1
        results.append((item, decision))
    return tuple(results)
