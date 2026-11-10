"""Pure SQLite Scheduler Core primitives (M2-02 first slice).

This module deliberately has no runtime/Herdr integration and starts no
worker. Callers own the scheduling loop and pass a live project connection.
Every reservation is fenced in the same IMMEDIATE transaction as the Attempt
and READY->RUNNING transition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timezone
from pathlib import PurePosixPath
import unicodedata

from . import ceo_initialization, commands, domain, events, leader, recovery, wake
from .clock import Clock, SystemClock
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
