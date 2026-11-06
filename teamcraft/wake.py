"""Durable Wake inbox, event aggregation, and delivery state (M2-09).

This module consumes the append-only project Event stream under the existing
Scheduler Leader lease.  It persists deterministic Wake batches and explicit
acknowledgement cursors, but deliberately performs no Herdr calls.  Any future
delivery or acknowledgement path must pass the M2-08 CEO initialization gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import ceo_initialization, commands, domain, events, leader, storage
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
) -> AdmissionResult:
    """Bind one eligible Wake to an idempotent CEO Command and reserve sending.

    This is only the durable pre-I/O boundary.  It does not contact Herdr.
    The host-trusted M2-08 initialization record is required before *any*
    command or Wake state is changed.  A future sender must reconcile a
    persisted ``sending`` row after interruption; it must never call this API
    as a way to blindly resend that row.
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
        item = _row_wake(row)
        if item.target_ceo_name != initialization.canonical_ceo_name:
            raise ValidationError("Wake target does not match the initialized CEO")
        if item.state != "pending":
            raise ValidationError(f"only pending Wakes can be admitted; found {item.state}")

        session = domain.get_session(conn, initialization.teamcraft_session_id or "")
        if session.native_session_id != initialization.native_session_id:
            raise ValidationError("CEO Session native identity does not match initialization")
        if session.state == "working":
            # Even urgent notifications are held until a dedicated policy and
            # budget path can prove that an interrupt is safe.
            return AdmissionResult(item, None, "ceo_working")
        if session.state != "idle":
            # Unknown, missing, or exited sessions are isolated.  This path
            # never starts a replacement session.
            return AdmissionResult(item, None, f"ceo_{session.state}")

        active = conn.execute(
            "SELECT id,state FROM wakes WHERE project_id=? AND target_ceo_name=? "
            "AND state IN ('sending','submitted','unknown') AND id<>? "
            "ORDER BY first_seq LIMIT 1",
            (project_id, item.target_ceo_name, item.id),
        ).fetchone()
        if active is not None:
            return AdmissionResult(item, None, f"wake_{active[1]}:{active[0]}")

        now = _iso(_now(clock))
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
        submission = commands.submit_command(
            conn,
            idempotency_key=f"wake:{project_id}:{item.id}:send:v1",
            kind="ceo.wake",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            evidence_json="{}",
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
        events.append_event(
            conn, "wake.sending", project_id=project_id,
            payload_json=json.dumps({"wake_id": item.id, "command_id": command.id,
                                     "target_session_id": initialization.native_session_id,
                                     "first_seq": item.first_seq, "last_seq": item.last_seq},
                                    ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            clock=clock,
        )
        return AdmissionResult(_row_wake(updated_row), command)


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
