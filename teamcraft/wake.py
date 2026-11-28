"""Durable Wake inbox, event aggregation, and delivery state (M2-09).

This module consumes the append-only project Event stream under the existing
Scheduler Leader lease.  It persists deterministic Wake batches and explicit
acknowledgement cursors, but deliberately performs no Herdr calls.  Any future
delivery or acknowledgement path must pass the M2-08 CEO initialization gate.

M5-03 adds an optional wake budget to the admission boundary.  When no budget
is configured (the default, ``budget=None``) admission behaves exactly as the
pre-budget M2-09 contract: identical deferred reasons, identical events, and
no budget accounting.  When a budget IS configured, ``admit_pending_wake``
consults the pure ``teamcraft.escalation`` layer against durable consumption
already persisted in this project's Wake rows before any external dispatch is
bound, so a budget cannot be bypassed by any delivery driver.  An urgent wake
may interrupt a working CEO only when the budget has remaining allowance AND
the caller explicitly opts in through ``EscalationPolicy.allow_urgent_interrupt``
— the "dedicated policy and budget path" the M2-09 hold referred to.

The same slice owns the needs-human escape for an urgent Wake the budget
refused: :func:`record_urgent_wake_escalation` writes the durable
``wake.urgent_escalated`` Event and, in that same transaction, moves the Wake
from ``pending`` to ``blocked`` with an auditable
``urgent_wake_budget_exhausted:<used>/<max>`` detail.  ``blocked`` therefore
carries two compatible meanings — "explicit reconciliation proved this was
NOT delivered, never auto-resend" and "budget-exhausted needs-human
escalation" — both of which forbid automatic delivery until a human acts, and
both of which remain visible through ``wake-status --state blocked``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import ceo_initialization, commands, domain, escalation, events, leader, storage
from .clock import Clock, SystemClock, require_utc
from .errors import NotFoundError, ValidationError
from .ids import DEFAULT_ID_FACTORY
from .storage import TeamCraftConnection, transaction

DEFAULT_WAKE_WINDOW_SECONDS = 30
MAX_WAKE_WINDOW_SECONDS = 3600
MAX_WAKE_BATCH_SIZE = 1000
_CEO_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_DEDUPE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/@+-]{0,191}\Z")

# High priority events keep their relative order when coalesced with normal
# activity.  Unknown domain events remain eligible at ordinary priority.
_PRIORITY_BY_KIND = {
    "recovery.escalated": 100,
    "recovery.attempts_exhausted": 100,
    "watch.deadline_exceeded": 100,
    "watch.no_progress_threshold": 90,
    "verification.approval_invalidated": 75,
    "verification.recorded": 60,
    "recovery.cancel_not_effected": 80,
    "recovery.cancelled": 50,
    "watch.agent_activity": 10,
    "watch.session_observed": 10,
    "watch.task_progress": 25,
    "attempt.executing": 20,
}
_IGNORED_EVENT_PREFIXES = (
    "wake.", "command.", "leader.", "ceo.initialization.", "scheduler.", "project.", "member."
)

_WAKE_COLUMNS = (
    "id, project_id, target_ceo_name, target_session_id, dedupe_key, window_start, "
    "priority, state, first_seq, last_seq, event_count, event_digest, command_id, "
    "detail, created_at, updated_at, submitted_at, acknowledged_at, processed_at"
)
_CURSOR_COLUMNS = (
    "project_id, target_ceo_name, target_session_id, scanned_seq, "
    "acknowledged_seq, processed_seq, created_at, updated_at"
)


@dataclass(frozen=True)
class Wake:
    id: str
    project_id: str
    target_ceo_name: str
    target_session_id: str | None
    dedupe_key: str
    window_start: str
    priority: int
    state: str
    first_seq: int
    last_seq: int
    event_count: int
    event_digest: str
    command_id: str | None
    detail: str | None
    created_at: str
    updated_at: str
    submitted_at: str | None
    acknowledged_at: str | None
    processed_at: str | None


@dataclass(frozen=True)
class WakeCursor:
    project_id: str
    target_ceo_name: str
    target_session_id: str | None
    scanned_seq: int
    acknowledged_seq: int
    processed_seq: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IngestResult:
    cursor: WakeCursor
    wakes: tuple[Wake, ...]
    scanned_events: int
    created_wakes: int
    merged_events: int


@dataclass(frozen=True)
class AdmissionResult:
    wake: Wake
    command: commands.Command | None
    deferred_reason: str | None = None
    #: SHA-256 digest of the M5-03 ``escalation.WakeDecision`` that
    #: authorized or deferred this admission.  ``None`` whenever no budget
    #: was configured, keeping pre-budget results byte-identical.
    decision_digest: str | None = None


# ---------------------------------------------------------------------------
# M5-03 wake budget (durable enforcement before external dispatch)
# ---------------------------------------------------------------------------

DEFAULT_WAKE_BUDGET_WINDOW_SECONDS = 3600
MAX_WAKE_BUDGET_WINDOW_SECONDS = 86400

#: States that represent a wake which reached (or may have reached) the CEO
#: transport and therefore conservatively consumes window allowance.  Pending
#: wakes never count; ``blocked`` rows never consumed attention and stay
#: excluded — that covers both the explicitly reconciled NOT-delivered row and
#: the budget-exhausted needs-human escalation written by
#: :func:`record_urgent_wake_escalation`.
_CONSUMING_WAKE_STATES = ("sending", "submitted", "acknowledged", "processed",
                         "unknown")


@dataclass(frozen=True)
class WakeBudgetConfig:
    """Explicit per-target wake allowance enforced at admission.

    Consumption is derived from durable Wake rows — never from process memory
    — so restarts and leader changes replay the same allowance.  In-flight
    ``sending``/``unknown`` rows count as spent because the prompt may already
    have reached the CEO; deferring rather than double-spending is the
    conservative direction.  ``max_wakes_per_window == 0`` deliberately freezes
    automatic dispatch without freezing intake.
    """

    max_wakes_per_window: int
    window_seconds: int = DEFAULT_WAKE_BUDGET_WINDOW_SECONDS

    def __post_init__(self) -> None:
        if (isinstance(self.max_wakes_per_window, bool)
                or not isinstance(self.max_wakes_per_window, int)
                or self.max_wakes_per_window < 0):
            raise ValidationError("max_wakes_per_window must be an integer >= 0")
        if (isinstance(self.window_seconds, bool)
                or not isinstance(self.window_seconds, int)
                or not 1 <= self.window_seconds <= MAX_WAKE_BUDGET_WINDOW_SECONDS):
            raise ValidationError(
                f"window_seconds must be an integer in 1..{MAX_WAKE_BUDGET_WINDOW_SECONDS}")


def _budget_window(now: datetime, seconds: int) -> tuple[str, str]:
    """Deterministic (floor_iso, window_id) bucket for the budget window.

    The id equals the floor timestamp so a replayed evaluation binds the same
    persisted rows the decision saw.
    """
    epoch = int(now.timestamp())
    bucket = epoch - epoch % seconds
    floor_iso = _iso(datetime.fromtimestamp(bucket, timezone.utc))
    return floor_iso, floor_iso


def _consumed_in_window(conn: TeamCraftConnection, project_id: str,
                        target_ceo_name: str, floor_iso: str) -> int:
    placeholders = ",".join("?" for _ in _CONSUMING_WAKE_STATES)
    row = conn.execute(
        "SELECT COUNT(*) FROM wakes WHERE project_id=? AND target_ceo_name=? "
        f"AND state IN ({placeholders}) AND COALESCE(submitted_at, updated_at) >= ?",
        (project_id, target_ceo_name, *_CONSUMING_WAKE_STATES, floor_iso),
    ).fetchone()
    return int(row[0])


def wake_budget_usage(
    conn: TeamCraftConnection, project_id: str, *,
    target_ceo_name: str = "ceo",
    window_seconds: int = DEFAULT_WAKE_BUDGET_WINDOW_SECONDS,
    clock: Clock | None = None,
) -> int:
    """Read-only audit view: wakes consumed within the current budget window.

    Every counted row is one that reached (or ambiguously may have reached)
    the transport inside the window; this never mutates state.
    """
    domain.require_head(conn)
    target_ceo_name = _validate_target(target_ceo_name)
    if (isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 1 <= window_seconds <= MAX_WAKE_BUDGET_WINDOW_SECONDS):
        raise ValidationError(
            f"window_seconds must be an integer in 1..{MAX_WAKE_BUDGET_WINDOW_SECONDS}")
    floor_iso, _ = _budget_window(_now(clock), window_seconds)
    return _consumed_in_window(conn, project_id, target_ceo_name, floor_iso)


#: Durable source of truth for the project wake budget (M5-01 precedent:
#: append-only events as config snapshots).  The kind starts with ``wake.``
#: so event ingestion ignores it — recording a budget can never wake the CEO.
WAKE_BUDGET_CONFIG_EVENT = "wake.budget_config"
_WAKE_CONFIG_ACTOR_MAX_BYTES = 64
_WAKE_CONFIG_REASON_MAX_BYTES = 512


@dataclass(frozen=True)
class WakeBudgetPolicy:
    """Persisted budget plus the urgent-interrupt policy evaluated with it."""

    budget: WakeBudgetConfig
    policy: escalation.EscalationPolicy

    def payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "max_wakes_per_window": self.budget.max_wakes_per_window,
            "window_seconds": self.budget.window_seconds,
            "allow_urgent_interrupt": self.policy.allow_urgent_interrupt,
            "urgent_priority": self.policy.urgent_priority,
        }


def _config_audit_text(value: str, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must not be blank")
    text = value.strip()
    if len(text.encode("utf-8")) > limit:
        raise ValidationError(f"{field} exceeds {limit} bytes")
    if re.search(r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}", text, re.I):
        raise ValidationError(f"{field} contains credential-shaped text")
    return text


def record_wake_budget(
    conn: TeamCraftConnection,
    project_id: str,
    lease: leader.Lease,
    *,
    max_wakes_per_window: int,
    window_seconds: int = DEFAULT_WAKE_BUDGET_WINDOW_SECONDS,
    allow_urgent_interrupt: bool = False,
    urgent_priority: int = escalation.DEFAULT_URGENT_WAKE_PRIORITY,
    actor: str,
    reason: str,
    clock: Clock | None = None,
) -> WakeBudgetPolicy:
    """Durably set the project wake budget; the newest record is authoritative.

    This writes one audited ``wake.budget_config`` Event under the project
    Leader lease — no Command, no transport, no CEO interaction.  Because the
    budget lives in the durable event store, EVERY later
    :func:`admit_pending_wake` enforces it even if its caller (for example
    the pre-M5-03 CLI dispatch loop) knows nothing about budget parameters;
    changing or lifting a budget is itself a lease-fenced, actor-attributed
    event, never a silent per-call bypass of persisted state by omission.
    """
    domain.require_head(conn)
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project",
                                     scope=lease.scope, epoch=lease.epoch)
    audit_actor = _config_audit_text(actor, "actor",
                                    limit=_WAKE_CONFIG_ACTOR_MAX_BYTES)
    audit_reason = _config_audit_text(reason, "reason",
                                     limit=_WAKE_CONFIG_REASON_MAX_BYTES)
    # Validate before any write so a bad policy can never persist halfway.
    proposed = WakeBudgetPolicy(
        budget=WakeBudgetConfig(max_wakes_per_window=max_wakes_per_window,
                               window_seconds=window_seconds),
        policy=escalation.EscalationPolicy(
            urgent_priority=urgent_priority,
            allow_urgent_interrupt=allow_urgent_interrupt),
    )
    payload = proposed.payload() | {"actor": audit_actor, "reason": audit_reason}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    if conn.in_transaction:
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        events.append_event(conn, WAKE_BUDGET_CONFIG_EVENT,
                           project_id=project_id, payload_json=body,
                           clock=clock)
        return proposed
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        events.append_event(conn, WAKE_BUDGET_CONFIG_EVENT,
                           project_id=project_id, payload_json=body,
                           clock=clock)
        return proposed


def get_wake_budget(
    conn: TeamCraftConnection, project_id: str,
) -> WakeBudgetPolicy | None:
    """Return the newest durable budget policy, or ``None`` when unset.

    A present-but-invalid config raises (fail closed): admissions must not
    silently run unenforced because the audit row could not be understood.
    """
    domain.require_head(conn)
    row = conn.execute(
        "SELECT payload_json FROM events WHERE kind=? AND project_id=?"
        " ORDER BY seq DESC LIMIT 1",
        (WAKE_BUDGET_CONFIG_EVENT, project_id),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise TypeError("config payload is not an object")
        return WakeBudgetPolicy(
            budget=WakeBudgetConfig(
                max_wakes_per_window=payload["max_wakes_per_window"],
                window_seconds=payload["window_seconds"]),
            policy=escalation.EscalationPolicy(
                urgent_priority=payload["urgent_priority"],
                allow_urgent_interrupt=payload["allow_urgent_interrupt"]),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ValidationError(
            "persisted wake budget config is invalid; repair it before "
            "admitting wakes") from exc


#: Reason prefix that marks a deferral as "a human has to act": the decision
#: layer classifies an urgent item the per-window budget refused as
#: ``needs_human`` (``urgent_wake_budget_exhausted:<used>/<max>``).
URGENT_WAKE_BUDGET_EXHAUSTED_REASON = "urgent_wake_budget_exhausted"

#: Durable human-escalation record for that refusal.  Like the budget config
#: kind it starts with ``wake.``, so event ingestion ignores it and an
#: escalation can never wake the CEO itself.
URGENT_WAKE_ESCALATION_EVENT = "wake.urgent_escalated"
_URGENT_ESCALATION_DETAIL_MAX_BYTES = 512


def _urgent_escalation_reason(
    conn: TeamCraftConnection,
    project_id: str,
    target_ceo_name: str,
    persisted: WakeBudgetPolicy | None,
    detail: str | None,
    clock: Clock | None,
) -> str:
    """Auditable ``<code>:<used>/<max>`` reason for the blocked Wake.

    The caller's refusal detail wins when it already carries the stable code
    (it is the exact count the admission path observed).  Otherwise the count
    is re-derived from the durable budget window, so the Wake ``detail`` is
    always a reproducible audit fact rather than free text.
    """
    text: str | None = None
    if detail is not None:
        text = _config_audit_text(
            detail, "detail", limit=_URGENT_ESCALATION_DETAIL_MAX_BYTES)
        if text.startswith(f"{URGENT_WAKE_BUDGET_EXHAUSTED_REASON}:"):
            return text
    if persisted is not None:
        floor_iso, _ = _budget_window(
            _now(clock), persisted.budget.window_seconds)
        used = _consumed_in_window(
            conn, project_id, target_ceo_name, floor_iso)
        return (f"{URGENT_WAKE_BUDGET_EXHAUSTED_REASON}:{used}/"
                f"{persisted.budget.max_wakes_per_window}")
    if text is not None:
        return text
    return URGENT_WAKE_BUDGET_EXHAUSTED_REASON


def _find_urgent_wake_escalation_event(
    conn: TeamCraftConnection, project_id: str, wake_id: str,
) -> events.Event | None:
    """Newest persisted escalation Event for one Wake, if any.

    The content-addressed event id collapses identical replays; this lookup
    makes the *Wake state* transition idempotent too, because once the Wake
    left ``pending`` the durable ledger is the only authority on whether it
    was already escalated.
    """
    rows = conn.execute(
        "SELECT event_id FROM events WHERE kind=? AND project_id=?"
        " ORDER BY seq DESC",
        (URGENT_WAKE_ESCALATION_EVENT, project_id),
    ).fetchall()
    for (event_id,) in rows:
        event = events.get_event(conn, str(event_id))
        try:
            payload = json.loads(event.payload_json)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("wake_id") == wake_id:
            return event
    return None


def record_urgent_wake_escalation(
    conn: TeamCraftConnection,
    project_id: str,
    wake_id: str,
    lease: leader.Lease,
    *,
    detail: str | None = None,
    clock: Clock | None = None,
) -> tuple[events.Event, bool]:
    """Escalate an over-budget urgent Wake to a human, durably and atomically.

    :func:`admit_pending_wake` deliberately only *defers* when the budget
    refused an urgent item — it never drops the Wake and never silently
    resolves it.  The dispatch path that observed the refusal calls this
    function, which is the ONLY transition that turns the refusal into durable
    work for a human: one transaction writes the ``wake.urgent_escalated``
    Event **and** moves the Wake from ``pending`` to ``blocked``, recording
    ``urgent_wake_budget_exhausted:<used>/<max>`` (or the equivalent caller
    reason) in the Wake ``detail``.  A blocked Wake is never admitted again,
    so the refusal cannot be mistaken for a retryable backlog item.

    ``blocked`` is shared with the explicit reconciliation vocabulary
    ("inspected and proven NOT delivered; automatic resend prohibited", see
    :func:`reconcile_wake_delivery`).  Both uses mean "automatic delivery is
    forbidden until a human reconciles", and ``wake-status --state blocked``
    is the notification consumer that lists them with their ``detail``.

    Idempotent per Wake: if the Wake is already blocked by an earlier
    escalation, the existing Event is returned with ``inserted=False`` and
    neither the row nor the event stream changes.  Repeated dispatch attempts
    therefore cannot duplicate the escalation, and a later budget window
    cannot produce a second record for the same Wake because the Wake is
    permanently blocked.  It never emits a Command and never touches the
    transport.
    """
    domain.require_head(conn)
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError(
            "lease scope does not match project", scope=lease.scope,
            epoch=lease.epoch)

    def _record() -> tuple[events.Event, bool]:
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        row = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=? AND project_id=?",
            (wake_id, project_id),
        ).fetchone()
        if row is None:
            raise ValidationError(
                "urgent Wake escalation target is not a Wake of this project")
        state = str(row[7])
        if state == "blocked":
            existing = _find_urgent_wake_escalation_event(
                conn, project_id, wake_id)
            if existing is not None:
                return existing, False
            raise ValidationError(
                "blocked Wake has no recorded urgent escalation; reconcile"
                f" it explicitly instead; wake {wake_id} is blocked")
        if state != "pending":
            raise ValidationError(
                "only pending Wakes can escalate an urgent budget refusal;"
                f" wake {wake_id} is {state}")
        persisted: WakeBudgetPolicy | None = None
        budget_config_invalid = False
        try:
            persisted = get_wake_budget(conn, project_id)
        except ValidationError:
            # A corrupt budget config must not block the escalation itself:
            # admission already refused fail-closed, and losing the human
            # signal would be the worse failure.
            budget_config_invalid = True
        reason = _urgent_escalation_reason(
            conn, project_id, str(row[2]), persisted, detail, clock)
        payload: dict[str, Any] = {
            "version": 1,
            "escalation": "needs_human",
            "reason_code": URGENT_WAKE_BUDGET_EXHAUSTED_REASON,
            "wake_id": wake_id,
            "target_ceo_name": str(row[2]),
            "wake_state": "blocked",
            "state_before": "pending",
            "wake_detail": reason,
            "priority": int(row[6]),
            "first_seq": int(row[8]),
            "last_seq": int(row[9]),
            "event_digest": str(row[11]),
        }
        if budget_config_invalid:
            payload["budget_config_invalid"] = True
        elif persisted is not None:
            floor_iso, window_id = _budget_window(
                _now(clock), persisted.budget.window_seconds)
            payload["max_wakes_per_window"] = (
                persisted.budget.max_wakes_per_window)
            payload["window_seconds"] = persisted.budget.window_seconds
            payload["window_id"] = window_id
        if detail is not None:
            payload["detail"] = _config_audit_text(
                detail, "detail", limit=_URGENT_ESCALATION_DETAIL_MAX_BYTES)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
        # A content-addressed key keeps identical repeated attempts at one
        # record while letting a genuinely changed context be recorded again.
        event_id = ("wake_urgent_escalated_"
                   + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32])
        event, _inserted = events.append_event_idempotent(
            conn, URGENT_WAKE_ESCALATION_EVENT, project_id=project_id,
            payload_json=body, event_id=event_id, clock=clock)
        # Same transaction as the Event: the Wake leaves the dispatchable
        # state only together with its durable human-escalation record.  The
        # CAS also converges a legacy pending row that already had a record.
        now = _iso(_now(clock))
        changed = conn.execute(
            "UPDATE wakes SET state='blocked',detail=?,updated_at=? "
            "WHERE id=? AND state='pending'",
            (reason, now, wake_id),
        ).rowcount
        if changed != 1:
            raise storage.StorageError(
                "urgent Wake escalation lost its pending-state"
                " compare-and-swap")
        return event, _inserted

    if conn.in_transaction:
        return _record()
    with transaction(conn):
        return _record()


def _now(clock: Clock | None) -> datetime:
    active = clock if clock is not None else SystemClock()
    return require_utc(active.now())


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _validate_target(name: str) -> str:
    if not isinstance(name, str) or not _CEO_NAME_RE.fullmatch(name):
        raise ValidationError("target CEO name must be a normalized TeamCraft member name")
    return name


def _row_wake(row: object) -> Wake:
    return Wake(
        str(row[0]), str(row[1]), str(row[2]),
        None if row[3] is None else str(row[3]), str(row[4]), str(row[5]),
        int(row[6]), str(row[7]), int(row[8]), int(row[9]), int(row[10]),
        str(row[11]), None if row[12] is None else str(row[12]),
        None if row[13] is None else str(row[13]), str(row[14]), str(row[15]),
        None if row[16] is None else str(row[16]),
        None if row[17] is None else str(row[17]),
        None if row[18] is None else str(row[18]),
    )


def _row_cursor(row: object) -> WakeCursor:
    return WakeCursor(
        str(row[0]), str(row[1]), None if row[2] is None else str(row[2]),
        int(row[3]), int(row[4]), int(row[5]), str(row[6]), str(row[7]),
    )


def get_cursor(
    conn: TeamCraftConnection, project_id: str, target_ceo_name: str = "ceo",
) -> WakeCursor | None:
    domain.require_head(conn)
    target_ceo_name = _validate_target(target_ceo_name)
    row = conn.execute(
        f"SELECT {_CURSOR_COLUMNS} FROM wake_cursors WHERE project_id=? AND target_ceo_name=?",
        (project_id, target_ceo_name),
    ).fetchone()
    return None if row is None else _row_cursor(row)


def list_wakes(
    conn: TeamCraftConnection, project_id: str, *, target_ceo_name: str = "ceo",
    states: tuple[str, ...] | None = None, limit: int = MAX_WAKE_BATCH_SIZE,
) -> tuple[Wake, ...]:
    domain.require_head(conn)
    target_ceo_name = _validate_target(target_ceo_name)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_WAKE_BATCH_SIZE:
        raise ValidationError(f"limit must be an integer in 1..{MAX_WAKE_BATCH_SIZE}")
    if states is not None:
        if not states or any(state not in {
            "pending", "sending", "submitted", "acknowledged", "processed", "unknown", "blocked"
        } for state in states):
            raise ValidationError("states must contain recognized Wake states")
        placeholders = ",".join("?" for _ in states)
        rows = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE project_id=? AND target_ceo_name=? "
            f"AND state IN ({placeholders}) ORDER BY priority DESC, first_seq ASC, id ASC LIMIT ?",
            (project_id, target_ceo_name, *states, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE project_id=? AND target_ceo_name=? "
            "ORDER BY priority DESC, first_seq ASC, id ASC LIMIT ?",
            (project_id, target_ceo_name, limit),
        ).fetchall()
    return tuple(_row_wake(row) for row in rows)


def _event_priority(event: events.Event, payload: object) -> int:
    raw = payload.get("priority") if isinstance(payload, dict) else None
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 100:
            raise ValidationError(f"event {event.event_id} has invalid Wake priority")
        return raw
    return _PRIORITY_BY_KIND.get(event.kind, 50)


def _dedupe_key(event: events.Event, payload: object) -> str:
    raw = payload.get("dedupe_key") if isinstance(payload, dict) else None
    if raw is None:
        raw = ":".join((event.kind, event.task_id or "-", event.attempt_id or "-"))
    if not isinstance(raw, str) or not _DEDUPE_RE.fullmatch(raw):
        raise ValidationError(f"event {event.event_id} has an invalid Wake dedupe_key")
    return raw


def _window_start(event: events.Event, seconds: int) -> str:
    try:
        timestamp = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        timestamp = require_utc(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"event {event.event_id} has an invalid occurred_at") from exc
    epoch = int(timestamp.timestamp())
    bucket = epoch - epoch % seconds
    return _iso(datetime.fromtimestamp(bucket, timezone.utc))


def _event_digest(previous_digest: str | None, event: events.Event) -> str:
    source = f"{previous_digest or ''}\n{event.seq}\n{event.event_id}\n{event.kind}\n{event.payload_json}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _candidate(event: events.Event, window_seconds: int) -> tuple[str, int, str] | None:
    if event.project_id is None or event.kind.startswith(_IGNORED_EVENT_PREFIXES):
        return None
    payload = json.loads(event.payload_json)
    priority = _event_priority(event, payload)
    key = _dedupe_key(event, payload)
    return key, priority, _window_start(event, window_seconds)


def ingest_events(
    conn: TeamCraftConnection,
    project_id: str,
    lease: leader.Lease,
    *,
    target_ceo_name: str = "ceo",
    window_seconds: int = DEFAULT_WAKE_WINDOW_SECONDS,
    limit: int = 200,
    clock: Clock | None = None,
) -> IngestResult:
    """Persist one page of project events as deterministic pending Wake rows.

    Wake intake is safe before CEO initialization because it performs no
    external action.  Event links and ``scanned_seq`` advance atomically under
    the project Leader lease; retries after a crash either see both or neither.
    """
    domain.require_head(conn)
    target_ceo_name = _validate_target(target_ceo_name)
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or not 1 <= window_seconds <= MAX_WAKE_WINDOW_SECONDS:
        raise ValidationError(f"window_seconds must be an integer in 1..{MAX_WAKE_WINDOW_SECONDS}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_WAKE_BATCH_SIZE:
        raise ValidationError(f"limit must be an integer in 1..{MAX_WAKE_BATCH_SIZE}")
    now = _iso(_now(clock))
    created = 0
    merged = 0
    output: list[Wake] = []
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        cursor_row = conn.execute(
            f"SELECT {_CURSOR_COLUMNS} FROM wake_cursors WHERE project_id=? AND target_ceo_name=?",
            (project_id, target_ceo_name),
        ).fetchone()
        if cursor_row is None:
            conn.execute(
                "INSERT INTO wake_cursors (project_id,target_ceo_name,scanned_seq,"
                "acknowledged_seq,processed_seq,created_at,updated_at) VALUES (?,?,0,0,0,?,?)",
                (project_id, target_ceo_name, now, now),
            )
            scanned_seq = 0
        else:
            scanned_seq = int(cursor_row[3])

        page = events.events_after(conn, scanned_seq, limit=limit, project_id=project_id)
        for event in page:
            candidate = _candidate(event, window_seconds)
            if candidate is None:
                continue
            dedupe_key, priority, bucket = candidate
            existing_link = conn.execute(
                "SELECT wake_id FROM wake_events WHERE event_seq=?", (event.seq,),
            ).fetchone()
            if existing_link is not None:
                # A cursor and event link commit together.  Finding a link past
                # the cursor means persisted state is inconsistent; do not
                # silently attach the same event to another Wake.
                raise storage.StorageError(
                    f"event seq {event.seq} is already linked to Wake {existing_link[0]} past its cursor"
                )
            row = conn.execute(
                f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE project_id=? AND target_ceo_name=? "
                "AND dedupe_key=? AND window_start=? AND state='pending'",
                (project_id, target_ceo_name, dedupe_key, bucket),
            ).fetchone()
            if row is None:
                wake_id = DEFAULT_ID_FACTORY.new("wake")
                digest = _event_digest(None, event)
                conn.execute(
                    "INSERT INTO wakes (id,project_id,target_ceo_name,target_session_id,"
                    "dedupe_key,window_start,priority,state,first_seq,last_seq,event_count,"
                    "event_digest,created_at,updated_at) VALUES (?,?,?,NULL,?,?,?,'pending',?,?,?,?,?,?)",
                    (wake_id, project_id, target_ceo_name, dedupe_key, bucket, priority,
                     event.seq, event.seq, 1, digest, now, now),
                )
                created += 1
            else:
                wake_id = str(row[0])
                digest = _event_digest(str(row[11]), event)
                conn.execute(
                    "UPDATE wakes SET priority=?,last_seq=?,event_count=event_count+1,"
                    "event_digest=?,updated_at=? WHERE id=? AND state='pending'",
                    (max(int(row[6]), priority), event.seq, digest, now, wake_id),
                )
                merged += 1
            conn.execute(
                "INSERT INTO wake_events (wake_id,event_seq,linked_at) VALUES (?,?,?)",
                (wake_id, event.seq, now),
            )
            current = conn.execute(
                f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=?", (wake_id,),
            ).fetchone()
            output.append(_row_wake(current))

        if page:
            scanned_seq = page[-1].seq
            conn.execute(
                "UPDATE wake_cursors SET scanned_seq=?,updated_at=? "
                "WHERE project_id=? AND target_ceo_name=?",
                (scanned_seq, now, project_id, target_ceo_name),
            )
        final_row = conn.execute(
            f"SELECT {_CURSOR_COLUMNS} FROM wake_cursors WHERE project_id=? AND target_ceo_name=?",
            (project_id, target_ceo_name),
        ).fetchone()
        assert final_row is not None
        cursor = _row_cursor(final_row)
    return IngestResult(cursor, tuple(output), len(page), created, merged)


def admit_pending_wake(
    conn: TeamCraftConnection,
    project_id: str,
    wake_id: str,
    lease: leader.Lease,
    *,
    clock: Clock | None = None,
    budget: WakeBudgetConfig | None = None,
    policy: escalation.EscalationPolicy | None = None,
) -> AdmissionResult:
    """Bind one eligible Wake to an idempotent CEO Command and reserve sending.

    This is only the durable pre-I/O boundary.  It does not contact Herdr.
    The host-trusted M2-08 initialization record is required before *any*
    command or Wake state is changed.  A future sender must reconcile a
    persisted ``sending`` row after interruption; it must never call this API
    as a way to blindly resend that row.

    M5-03 budget semantics:

    * ``budget=None`` and no persisted config — the pre-budget M2-09 path,
      unchanged: working CEO defers as ``ceo_working``, non-idle CEO
      isolates as ``ceo_<state>``, and no counting or decision digest is
      produced.
    * Persisted config — the newest durable ``wake.budget_config`` Event
      (see :func:`record_wake_budget`) is enforced even when the caller
      never passes budget parameters, so legacy dispatch loops cannot
      dispatch over budget.
    * An explicit ``budget`` overrides the persisted budget for this call;
      the accompanying urgent policy comes from ``policy`` when given,
      otherwise the persisted policy when the budget was persisted,
      otherwise the conservative default (urgent interrupt off).
    * Configured ``budget`` — window consumption is re-derived from durable
      Wake rows under the same write transaction, and the pure
      :func:`teamcraft.escalation.decide_wake` layer adjudicates before a
      Command is created.  A denied admission writes nothing: the Wake stays
      ``pending``, no cursor or state moves, and the reason carries the
      stable code plus consumed/max counts (for example
      ``wake_budget_exhausted:2/2``).  An exhausted *urgent* item surfaces
      as ``urgent_wake_budget_exhausted:<n>/<max>`` for the caller to
      escalate through the recovery path — this module never fabricates a
      NEEDS_HUMAN transition itself.
    * A working CEO is interrupted only by an ``urgent_bypass`` decision,
      which requires urgency (priority at/above ``policy.urgent_priority``),
      remaining budget allowance, AND the explicit
      ``policy.allow_urgent_interrupt`` opt-in (default off).  All M2-08
      identity gates still apply to a bypass.
    """
    domain.require_head(conn)
    if budget is not None and not isinstance(budget, WakeBudgetConfig):
        raise ValidationError("budget must be a WakeBudgetConfig or None")
    if policy is not None and not isinstance(
            policy, escalation.EscalationPolicy):
        raise ValidationError("policy must be an EscalationPolicy or None")
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        initialization = ceo_initialization.require_initialized(conn, project_id)
        row = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=? AND project_id=?",
            (wake_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("Wake not found")
        item = _row_wake(row)
        if item.target_ceo_name != initialization.canonical_ceo_name:
            raise ValidationError("Wake target does not match the initialized CEO")
        if item.state != "pending":
            raise ValidationError(f"only pending Wakes can be admitted; found {item.state}")

        session = domain.get_session(conn, initialization.teamcraft_session_id or "")
        if session.native_session_id != initialization.native_session_id:
            raise ValidationError("CEO Session native identity does not match initialization")

        current = _now(clock)
        budget_decision = None
        budget_audit = None
        consumed = 0
        budget_source = "parameter" if budget is not None else None
        if budget is None:
            # Enforceability: even callers that predate M5-03 (and therefore
            # never pass a budget) are governed by the newest durable
            # wake.budget_config event.  An explicit parameter overrides the
            # persisted budget, and its policy defaults stay conservative
            # (no urgent interrupt) unless passed or persisted alongside.
            persisted = get_wake_budget(conn, project_id)
            if persisted is not None:
                budget = persisted.budget
                if policy is None:
                    policy = persisted.policy
                budget_source = "persisted"
        if budget is not None:
            floor_iso, window_id = _budget_window(current, budget.window_seconds)
            consumed = _consumed_in_window(conn, project_id, item.target_ceo_name,
                                          floor_iso)
            # A configuration shrunk below durable consumption must defer, not
            # crash the admission; the raw count still reports the truth.
            counted = min(consumed, budget.max_wakes_per_window)
            budget_decision = escalation.decide_wake(
                escalation.WakeRequest(
                    priority=item.priority,
                    ceo_state=session.state,
                    budget=escalation.WakeBudget(
                        budget.max_wakes_per_window, counted, window_id),
                ),
                policy=policy if policy is not None
                else escalation.EscalationPolicy(),
            )
            budget_audit = {
                "source": budget_source,
                "max_wakes_per_window": budget.max_wakes_per_window,
                "window_seconds": budget.window_seconds,
                "window_id": window_id,
                "consumed_in_window": consumed,
                "action": budget_decision.action,
                "decision_digest": budget_decision.decision_digest,
            }
        decision_digest = (None if budget_decision is None
                          else budget_decision.decision_digest)

        if session.state == "working":
            # The M2-09 hold stays the default.  M5-03 releases an interrupt
            # ONLY through an in-budget, explicitly opted-in urgent bypass.
            if budget_decision is None or budget_decision.action != "urgent_bypass":
                reason = ("ceo_working" if budget_decision is None
                        else budget_decision.reason_code)
                if reason != "ceo_working":
                    reason = (f"{reason}:{consumed}/"
                            f"{budget.max_wakes_per_window}")
                return AdmissionResult(item, None, reason, decision_digest)
        elif session.state != "idle":
            # Unknown, missing, or exited sessions are isolated.  This path
            # never starts a replacement session.
            return AdmissionResult(item, None, f"ceo_{session.state}",
                                 decision_digest)
        elif budget_decision is not None and budget_decision.action != "deliver":
            # Idle CEO but the window budget denies dispatch.  Nothing is
            # written: the Wake remains pending for a later window, and the
            # urgent variant carries the escalation code for the caller.
            return AdmissionResult(
                item, None,
                f"{budget_decision.reason_code}:{consumed}/"
                f"{budget.max_wakes_per_window}",
                decision_digest,
            )

        active = conn.execute(
            "SELECT id,state FROM wakes WHERE project_id=? AND target_ceo_name=? "
            "AND state IN ('sending','submitted','unknown') AND id<>? "
            "ORDER BY first_seq LIMIT 1",
            (project_id, item.target_ceo_name, item.id),
        ).fetchone()
        if active is not None:
            return AdmissionResult(item, None, f"wake_{active[1]}:{active[0]}",
                                 decision_digest)

        now = _iso(current)
        payload = {
            "action": "wake_ceo",
            "project_id": project_id,
            "wake_id": item.id,
            "target_ceo_name": item.target_ceo_name,
            "target_session_id": initialization.native_session_id,
            "first_seq": item.first_seq,
            "last_seq": item.last_seq,
            "priority": item.priority,
            "event_digest": item.event_digest,
        }
        if budget_audit is not None:
            # Additive only when a budget is configured: the durable audit
            # binds this dispatch to the exact window allowance it consumed.
            payload["wake_budget"] = budget_audit
            payload["urgent_bypass"] = (
                budget_decision.action == "urgent_bypass")
        submission = commands.submit_command(
            conn,
            idempotency_key=f"wake:{project_id}:{item.id}:send:v1",
            kind="ceo.wake",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            # M3-04 traceability: project-stamped so the wake Command's audit
            # events resolve through the project even without a Task binding.
            evidence_json=json.dumps({"project_id": project_id},
                                    sort_keys=True, separators=(",", ":")),
            clock=clock,
        )
        command = submission.command
        if command.state == "pending":
            command = commands.claim_command(conn, command.id, clock=clock)
        elif command.state != "running":
            raise ValidationError(
                f"Wake Command {command.id} is {command.state}; reconcile before admission"
            )
        changed = conn.execute(
            "UPDATE wakes SET state='sending',target_session_id=?,command_id=?,updated_at=? "
            "WHERE id=? AND state='pending'",
            (initialization.native_session_id, command.id, now, item.id),
        ).rowcount
        if changed != 1:
            raise storage.StorageError("Wake admission lost its pending-state compare-and-swap")
        updated_row = conn.execute(f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=?", (item.id,)).fetchone()
        assert updated_row is not None
        sending_payload = {"wake_id": item.id, "command_id": command.id,
                           "target_session_id": initialization.native_session_id,
                           "first_seq": item.first_seq, "last_seq": item.last_seq}
        if budget_audit is not None:
            # Durable audit binds the reserved dispatch to the exact window
            # allowance it consumed; absent without a configured budget.
            sending_payload["wake_budget"] = budget_audit
            sending_payload["urgent_bypass"] = (
                budget_decision.action == "urgent_bypass")
        events.append_event(
            conn, "wake.sending", project_id=project_id,
            payload_json=json.dumps(sending_payload,
                                    ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")),
            clock=clock,
        )
        return AdmissionResult(_row_wake(updated_row), command, None,
                              decision_digest)


def record_submission_result(
    conn: TeamCraftConnection,
    project_id: str,
    wake_id: str,
    lease: leader.Lease,
    *,
    outcome: str,
    detail: str | None = None,
    receipt_json: str | None = None,
    clock: Clock | None = None,
) -> Wake:
    """Commit a Wake transport result without treating it as CEO acknowledgement.

    ``submitted`` means the Herdr transport confirmed prompt submission;
    ``unknown`` means the effect may have happened and must be reconciled.
    Neither outcome advances the CEO acknowledgement cursor.
    """
    domain.require_head(conn)
    if outcome not in {"submitted", "unknown"}:
        raise ValidationError("Wake submission outcome must be submitted or unknown")
    if outcome == "unknown" and (not isinstance(detail, str) or not detail.strip()):
        raise ValidationError("unknown Wake submission requires an audit detail")
    if detail is not None and (not isinstance(detail, str) or len(detail.encode("utf-8")) > 1024):
        raise ValidationError("Wake submission detail must be at most 1024 UTF-8 bytes")
    try:
        receipt = json.loads(receipt_json or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("transport receipt must be valid JSON") from exc
    if not isinstance(receipt, dict) or len(json.dumps(receipt, ensure_ascii=False).encode("utf-8")) > 8192:
        raise ValidationError("transport receipt must be an object of at most 8192 UTF-8 bytes")
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    now = _iso(_now(clock))
    with transaction(conn):
        if outcome == "submitted":
            leader.require_project_lease(conn, lease, project_id, clock=clock)
            ceo_initialization.require_initialized(conn, project_id)
        row = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=? AND project_id=?",
            (wake_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("Wake not found")
        item = _row_wake(row)
        if item.state != "sending" or item.command_id is None:
            raise ValidationError("only a sending Wake with a bound Command can record transport outcome")
        command = conn.execute("SELECT state FROM commands WHERE id=?", (item.command_id,)).fetchone()
        if command is None or str(command[0]) != "running":
            raise ValidationError("Wake Command is not running; reconcile its persisted state")
        if outcome == "submitted":
            result = {"transport": "submitted", "receipt": receipt}
            commands.confirm_command(
                conn, item.command_id,
                result_json=json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                clock=clock,
            )
            conn.execute(
                "UPDATE wakes SET state='submitted',submitted_at=?,updated_at=? WHERE id=? AND state='sending'",
                (now, now, wake_id),
            )
        else:
            commands.mark_command_unknown(conn, item.command_id, note=detail or "Wake transport outcome unknown", clock=clock)
            conn.execute(
                "UPDATE wakes SET state='unknown',detail=?,updated_at=? WHERE id=? AND state='sending'",
                (detail, now, wake_id),
            )
        events.append_event(
            conn, f"wake.{outcome}", project_id=project_id,
            payload_json=json.dumps({"wake_id": wake_id, "command_id": item.command_id,
                                     "detail": detail, "transport_receipt": receipt},
                                    ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            clock=clock,
        )
        updated = conn.execute(f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=?", (wake_id,)).fetchone()
        assert updated is not None
        return _row_wake(updated)


def recover_interrupted_submissions(
    conn: TeamCraftConnection,
    project_id: str,
    lease: leader.Lease,
    *,
    clock: Clock | None = None,
) -> tuple[Wake, ...]:
    """Move prior Leader ``sending`` rows to UNKNOWN after lease takeover.

    A current fenced Leader proves the previous executor can no longer own
    the project. The old external call may nevertheless have completed, so
    recovery records ambiguity and never retries it.
    """
    domain.require_head(conn)
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    now = _iso(_now(clock))
    recovered: list[Wake] = []
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        rows = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE project_id=? AND state='sending' "
            "ORDER BY first_seq,id",
            (project_id,),
        ).fetchall()
        for row in rows:
            item = _row_wake(row)
            if item.command_id is None:
                raise storage.StorageError(f"sending Wake {item.id} has no bound Command")
            command = conn.execute("SELECT state FROM commands WHERE id=?", (item.command_id,)).fetchone()
            if command is None or str(command[0]) != "running":
                raise storage.StorageError(f"sending Wake {item.id} has no running Command")
            note = "prior Leader ended during Wake transport; inspect Herdr before resolving"
            commands.mark_command_unknown(conn, item.command_id, note=note, clock=clock)
            conn.execute(
                "UPDATE wakes SET state='unknown',detail=?,updated_at=? WHERE id=? AND state='sending'",
                (note, now, item.id),
            )
            events.append_event(conn, "wake.unknown", project_id=project_id,
                payload_json=json.dumps({"wake_id": item.id, "command_id": item.command_id,
                                         "detail": note, "recovered_after_leader_takeover": True},
                                        sort_keys=True, separators=(",", ":")), clock=clock)
            updated = conn.execute(f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=?", (item.id,)).fetchone()
            assert updated is not None
            recovered.append(_row_wake(updated))
    return tuple(recovered)


def reconcile_wake_delivery(
    conn: TeamCraftConnection,
    project_id: str,
    wake_id: str,
    lease: leader.Lease,
    *,
    delivered: bool,
    actor: str,
    note: str,
    evidence_json: str,
    clock: Clock | None = None,
) -> Wake:
    """Settle UNKNOWN only from explicit inspection; never resends the prompt.

    A positive finding settles the Command and Wake as submitted. A finding
    that delivery did not occur expires the Command and blocks the Wake for
    manual follow-up. Neither result creates a replacement Command.

    ``blocked`` is also written by :func:`record_urgent_wake_escalation` for
    a budget-exhausted urgent Wake.  Both rows share one meaning — automatic
    delivery is forbidden until a human acts — and the ``detail`` column says
    which cause applies.
    """
    domain.require_head(conn)
    if not isinstance(delivered, bool):
        raise ValidationError("delivered must be a boolean inspection result")
    if not isinstance(actor, str) or not actor.strip() or len(actor.encode("utf-8")) > 64:
        raise ValidationError("reconciliation actor must be a non-empty string up to 64 bytes")
    if not isinstance(note, str) or not note.strip() or len(note.encode("utf-8")) > 256:
        raise ValidationError("reconciliation note must be a non-empty string up to 256 bytes")
    if not isinstance(evidence_json, str):
        raise ValidationError("reconciliation evidence must be a JSON string")
    try:
        evidence = json.loads(evidence_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("reconciliation evidence must be valid JSON") from exc
    if not isinstance(evidence, dict) or len(evidence_json.encode("utf-8")) > 512:
        raise ValidationError("reconciliation evidence must be an object up to 512 bytes")
    audit_note = f"actor={actor.strip()}; {note.strip()}; evidence={json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    if len(audit_note.encode("utf-8")) > commands.MAX_COMMAND_NOTE_BYTES:
        raise ValidationError("combined reconciliation audit note exceeds the command note limit")
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    now = _iso(_now(clock))
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        row = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=? AND project_id=?",
            (wake_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("Wake not found")
        item = _row_wake(row)
        if item.state != "unknown" or item.command_id is None:
            raise ValidationError("only an UNKNOWN Wake with a bound Command can be reconciled")
        commands.reconcile_command(
            conn, item.command_id,
            outcome="confirmed" if delivered else "expired",
            note=audit_note,
            result_json=json.dumps({"reconciled_delivered": True, "evidence": evidence},
                                   ensure_ascii=False, sort_keys=True, separators=(",", ":")) if delivered else None,
            clock=clock,
        )
        next_state = "submitted" if delivered else "blocked"
        detail = "reconciled as submitted" if delivered else "reconciled as not submitted; automatic resend prohibited"
        conn.execute(
            "UPDATE wakes SET state=?,detail=?,submitted_at=CASE WHEN ?='submitted' THEN ? ELSE submitted_at END,updated_at=? "
            "WHERE id=? AND state='unknown'",
            (next_state, detail, next_state, now, now, wake_id),
        )
        events.append_event(
            conn, "wake.reconciled", project_id=project_id,
            payload_json=json.dumps({"wake_id": item.id, "command_id": item.command_id,
                                     "state": next_state, "actor": actor.strip(),
                                     "note": note.strip(), "evidence": evidence},
                                    ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            clock=clock,
        )
        updated = conn.execute(f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=?", (wake_id,)).fetchone()
        assert updated is not None
        return _row_wake(updated)


def acknowledge_wake(
    conn: TeamCraftConnection,
    project_id: str,
    wake_id: str,
    lease: leader.Lease,
    *,
    target_session_id: str,
    acknowledged_through_seq: int,
    clock: Clock | None = None,
) -> Wake:
    """Record explicit CEO acknowledgement; never infer it from idle/done.

    The CEO initialization policy must pass; transport submission alone is
    never treated as CEO acknowledgement.
    """
    domain.require_head(conn)
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        initialization = ceo_initialization.require_initialized(conn, project_id)
        row = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=? AND project_id=?",
            (wake_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("Wake not found")
        wake = _row_wake(row)
        if wake.target_session_id != target_session_id or initialization.native_session_id != target_session_id:
            raise ValidationError("Wake acknowledgement session does not match the initialized CEO")
        if wake.state == "acknowledged":
            if acknowledged_through_seq == wake.last_seq:
                return wake
            raise ValidationError("replayed acknowledgement does not match the Wake event range")
        if wake.state != "submitted":
            raise ValidationError("only a submitted Wake can be acknowledged")
        if acknowledged_through_seq != wake.last_seq:
            raise ValidationError("acknowledgement must bind the exact Wake event range")
        now = _iso(_now(clock))
        conn.execute(
            "UPDATE wakes SET state='acknowledged',acknowledged_at=?,updated_at=? WHERE id=?",
            (now, now, wake_id),
        )
        cursor = conn.execute(
            "SELECT acknowledged_seq,scanned_seq FROM wake_cursors WHERE project_id=? AND target_ceo_name=?",
            (project_id, wake.target_ceo_name),
        ).fetchone()
        if cursor is None:
            raise storage.StorageError("Wake cursor is missing")
        unacknowledged = conn.execute(
            "SELECT MIN(we.event_seq) FROM wake_events we JOIN wakes w ON w.id=we.wake_id "
            "WHERE w.project_id=? AND w.target_ceo_name=? AND w.state<>'acknowledged' "
            "AND w.state<>'processed'",
            (project_id, wake.target_ceo_name),
        ).fetchone()[0]
        candidate = int(cursor[1]) if unacknowledged is None else int(unacknowledged) - 1
        new_ack = max(int(cursor[0]), candidate)
        conn.execute(
            "UPDATE wake_cursors SET acknowledged_seq=?,updated_at=? "
            "WHERE project_id=? AND target_ceo_name=?",
            (new_ack, now, project_id, wake.target_ceo_name),
        )
        updated = conn.execute(f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=?", (wake_id,)).fetchone()
        assert updated is not None
        return _row_wake(updated)


def mark_processed(
    conn: TeamCraftConnection,
    project_id: str,
    wake_id: str,
    lease: leader.Lease,
    *,
    clock: Clock | None = None,
) -> Wake:
    """Mark an acknowledged Wake processed and advance the explicit cursor."""
    domain.require_head(conn)
    if lease.scope != leader.project_scope(project_id):
        raise leader.StaleLeaderError("lease scope does not match project", scope=lease.scope, epoch=lease.epoch)
    with transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        ceo_initialization.require_initialized(conn, project_id)
        row = conn.execute(
            f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=? AND project_id=?",
            (wake_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("Wake not found")
        wake = _row_wake(row)
        if wake.state == "processed":
            return wake
        if wake.state != "acknowledged":
            raise ValidationError("only an acknowledged Wake can be processed")
        now = _iso(_now(clock))
        conn.execute(
            "UPDATE wakes SET state='processed',processed_at=?,updated_at=? WHERE id=?",
            (now, now, wake_id),
        )
        cursor = conn.execute(
            "SELECT acknowledged_seq,processed_seq FROM wake_cursors "
            "WHERE project_id=? AND target_ceo_name=?",
            (project_id, wake.target_ceo_name),
        ).fetchone()
        if cursor is None:
            raise storage.StorageError("Wake cursor is missing")
        unprocessed = conn.execute(
            "SELECT MIN(we.event_seq) FROM wake_events we JOIN wakes w ON w.id=we.wake_id "
            "WHERE w.project_id=? AND w.target_ceo_name=? AND w.state<>'processed'",
            (project_id, wake.target_ceo_name),
        ).fetchone()[0]
        candidate = int(cursor[0]) if unprocessed is None else int(unprocessed) - 1
        new_processed = max(int(cursor[1]), candidate)
        conn.execute(
            "UPDATE wake_cursors SET processed_seq=?,updated_at=? "
            "WHERE project_id=? AND target_ceo_name=?",
            (new_processed, now, project_id, wake.target_ceo_name),
        )
        updated = conn.execute(f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id=?", (wake_id,)).fetchone()
        assert updated is not None
        return _row_wake(updated)


#: M3-04: default horizon after which an unconsumed manual CEO request is
#: swept to ``expired`` (TTL) instead of lingering invisibly forever.
MANUAL_WAKE_REQUEST_TTL_SECONDS = 24 * 3600


def request_ceo_attention(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    actor: str,
    reason: str,
    idempotency_key: str,
    target_ceo_name: str = "ceo",
    task_id: str | None = None,
    task_version: int | None = None,
    ttl_seconds: int = MANUAL_WAKE_REQUEST_TTL_SECONDS,
    clock: Clock | None = None,
) -> tuple[commands.Command, bool]:
    """Queue a durable manual "ask the CEO to look at this" request.

    This records intent ONLY:

    * one durable ``ceo.wake_request`` Command (idempotency key + bounded
      TTL, so the request is always visible and can never stay pending
      forever — the scheduler sweep expires it if the runtime is absent);
    * one ``ceo.wake_requested`` Event linked to the project (and Task when
      bound).  The event kind deliberately is NOT on the ingestion ignore
      list, so the next fenced scheduler tick folds it into an ordinary
      merged Wake candidate under M2-09's dedupe/window rules — that is the
      ONLY path to prompt delivery, and it lives exclusively behind the
      Leader lease plus CEO initialization gates.

    This function never admits, sends, or resumes anything.
    """

    target = _validate_target(target_ceo_name)
    if (not isinstance(actor, str) or not actor.strip()
            or not isinstance(reason, str) or not reason.strip()):
        raise ValidationError("actor and reason are required")
    evidence: dict = {"project_id": project_id}
    if task_id is not None:
        task = domain.get_task(conn, task_id)
        if task.project_id != project_id:
            raise ValidationError("bound task does not belong to this project")
        evidence = {"project_id": project_id,
                   "task_id": task.id, "task_version": task_version}
    payload = {
        "action": "request_processing",
        "project_id": project_id,
        "target_ceo_name": target,
        "actor": actor.strip(),
        "reason": reason.strip(),
        "task_id": task_id,
        "task_version": task_version,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))

    def _body(conn: TeamCraftConnection) -> tuple[commands.Command, bool]:
        domain.get_project(conn, project_id)  # uniform NotFound first
        submission = commands.submit_command(
            conn, idempotency_key=idempotency_key, kind="ceo.wake_request",
            payload_json=body, evidence_json=evidence_json,
            ttl_seconds=ttl_seconds, clock=clock,
        )
        if not submission.reused:
            events.append_event(
                conn, "ceo.wake_requested", project_id=project_id,
                task_id=task_id,
                payload_json=json.dumps({
                    "command_id": submission.command.id,
                    "target_ceo_name": target,
                    "actor": actor.strip(), "reason": reason.strip(),
                    "task_id": task_id, "task_version": task_version,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                clock=clock,
            )
        return submission.command, submission.reused

    if conn.in_transaction:
        return _body(conn)
    with transaction(conn):
        return _body(conn)
