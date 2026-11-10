"""M1-05 persistent Command service (M0 review 8.2-7/-9/-11, 9.1 M1-05).

Contract pinned by this module and ``tests/test_events_m105.py``:

* every command row carries a UNIQUE ``idempotency_key``; same key +
  identical request identity — ``(kind, payload_json, evidence_json,
  ttl_seconds)`` — REUSES the stored outcome (including the ``result_json``
  of a confirmed command) and writes nothing; same key with any difference
  (including a TTL difference) raises
  :class:`teamcraft.errors.IdempotencyConflictError` without side effects;
* TTL is part of that normalized identity and is compared as *relative*
  seconds, never as an absolute deadline.  A retry computes its own
  ``expires_at`` from its own wall clock, so absolute deadlines of two
  identical requests always differ; comparing them would turn every retry
  into a spurious conflict.  v3 already persists the immutable
  ``requested_at``/``expires_at`` pair written by the single INSERT, so the
  originally requested TTL is recovered durably as
  ``expires_at - requested_at`` and no schema bump is needed — see
  :func:`_stored_ttl_seconds` for the invariant and its fail-closed rules;
* state machine ``pending -> running -> confirmed | unknown | expired``
  plus ``running|unknown -> reconcile -> confirmed | expired`` and
  ``pending -> expired`` (TTL / evidence invalidation); every transition is
  a compare-and-swap on the persisted state (losers get
  :class:`CommandStateConflictError`) and appends its event inside the SAME
  transaction — there is no API path that changes command state without its
  event, and fault injection proves neither half survives a rollback;
* commands bind an optional ``evidence_json`` (task_id + task_version,
  optionally attempt_id + attempt_no + digest).  Evidence drift invalidates
  PENDING commands (they move to ``expired`` with reason ``EVIDENCE_STALE``
  — automatically on the owning Task's CAS bump via ``domain``, and via the
  explicit sweep); CLAIM additionally re-verifies evidence and TTL under the
  write lock, so a command can never execute against stale evidence;
* UNKNOWN is never auto-retried: it closes only through ``reconcile`` with
  an explicit fact and audit note (review 8.2-11);
* every human-written note/reason persisted onto a command row is bounded
  by :data:`MAX_COMMAND_NOTE_BYTES` (UTF-8 bytes); oversized or blank text
  is refused BEFORE any transaction opens, leaving state and events
  untouched; sweep/expiry reasons are internal constants, inherently
  bounded, and notes additionally ride inside the 64 KiB event payload cap;
* Herdr/GitHub external interfaces are NOT exactly-once (review 8.2-7
  boundary): this module records and arbitrates commands, it does not invoke
  external effects, run a Leader, or deliver prompts (M1-07/M2 scope).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .clock import Clock
from .errors import (
    CommandStateConflictError,
    IdempotencyConflictError,
    IllegalTransitionError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from .events import (
    _require_head,
    _tx,
    _utc_iso,
    _validate_json,
    _validate_kind,
    append_event,
)
from .ids import DEFAULT_ID_FACTORY, IdFactory, require_entity_id
from .storage import TeamCraftConnection

#: The five canonical command states (review 8.2-11, 9.1 M1-05).
COMMAND_STATES = ("pending", "running", "confirmed", "unknown", "expired")
#: States from which no further transition is ever legal.
TERMINAL_COMMAND_STATES = frozenset({"confirmed", "expired"})
#: States a reconcile may settle an uncertain execution into.
RECONCILE_OUTCOMES = ("confirmed", "expired")

MAX_IDEMPOTENCY_KEY_LENGTH = 256
#: Byte cap for every human-written note/reason persisted onto a command row
#: (and carried into event payloads): audit text must stay bounded.
MAX_COMMAND_NOTE_BYTES = 1024
#: One-month ceiling: an absurd TTL would park a "will execute someday"
#: promise forever, contradicting review 8.2-9 ("尚未执行" must stay honest).
MAX_TTL_SECONDS = 30 * 24 * 60 * 60

_EVIDENCE_ALLOWED_KEYS = frozenset(
    {"task_id", "task_version", "attempt_id", "attempt_no", "digest",
     # M3-04: project-scoped commands (creates, scheduler gates, manual CEO
     # requests) carry an explicit project stamp so their Command/Event audit
     # is project-traceable even before/unless a Task binding exists.
     "project_id"}
)


@dataclass(frozen=True)
class Command:
    """One persistent command row (immutable snapshot of the current state)."""

    id: str
    idempotency_key: str
    kind: str
    payload_json: str
    evidence_json: str
    state: str
    requested_at: str
    updated_at: str
    expires_at: str | None
    result_json: str | None
    note: str | None


@dataclass(frozen=True)
class Submission:
    """Outcome of :func:`submit_command` — created or reused, never a lie."""

    command: Command
    created: bool

    @property
    def reused(self) -> bool:
        return not self.created


_COMMAND_COLUMNS = (
    "id, idempotency_key, kind, payload_json, evidence_json, state,"
    " requested_at, updated_at, expires_at, result_json, note"
)


def _row_to_command(row: Any) -> Command:
    return Command(
        id=str(row[0]),
        idempotency_key=str(row[1]),
        kind=str(row[2]),
        payload_json=str(row[3]),
        evidence_json=str(row[4]),
        state=str(row[5]),
        requested_at=str(row[6]),
        updated_at=str(row[7]),
        expires_at=None if row[8] is None else str(row[8]),
        result_json=None if row[9] is None else str(row[9]),
        note=None if row[10] is None else str(row[10]),
    )


def _fetch(conn: TeamCraftConnection, command_id: str) -> Command:
    row = conn.execute(
        f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE id = ?",
        (require_entity_id(command_id),),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"command not found: {command_id}")
    return _row_to_command(row)


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValidationError("idempotency_key must be a non-empty string")
    key = key.strip()
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValidationError(
            f"idempotency_key exceeds {MAX_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    return key


def _validate_evidence(evidence_json: str) -> str:
    """Validate the optional evidence binding (review 8.2-6 shape)."""

    if not isinstance(evidence_json, str):
        raise ValidationError("evidence_json must be a string of valid JSON")
    try:
        evidence = json.loads(evidence_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(f"evidence_json is not valid JSON: {exc}") from exc
    if not isinstance(evidence, dict):
        raise ValidationError("evidence_json must be a JSON object")
    unknown = set(evidence) - _EVIDENCE_ALLOWED_KEYS
    if unknown:
        raise ValidationError(
            f"evidence_json keys not allowed: {sorted(unknown)}; permitted are"
            f" {sorted(_EVIDENCE_ALLOWED_KEYS)}"
        )
    if "task_id" in evidence or "task_version" in evidence:
        if "task_id" not in evidence or "task_version" not in evidence:
            raise ValidationError(
                "evidence_json must bind task_id and task_version together"
            )
        require_entity_id(evidence["task_id"])
        version = evidence["task_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValidationError("evidence task_version must be an integer >= 0")
    if "attempt_id" in evidence or "attempt_no" in evidence:
        if "attempt_id" not in evidence or "attempt_no" not in evidence:
            raise ValidationError(
                "evidence_json must bind attempt_id and attempt_no together"
            )
        require_entity_id(evidence["attempt_id"])
        number = evidence["attempt_no"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValidationError("evidence attempt_no must be an integer >= 1")
    if "project_id" in evidence:
        project = evidence["project_id"]
        if not isinstance(project, str) or not project.strip():
            raise ValidationError(
                "evidence project_id must be a non-empty string")
    if "digest" in evidence and (
        not isinstance(evidence["digest"], str) or not evidence["digest"].strip()
        or len(evidence["digest"]) > 128
    ):
        raise ValidationError("evidence digest must be a 1..128 character string")
    return evidence_json


def _validate_result(result_json: str) -> str:
    return _validate_json(result_json)


def _validate_note(
    note: str | None, *, field: str = "note", required: bool = True
) -> str | None:
    """Bounded audit text (byte cap): emptiness is refused when required,
    oversized text is refused always; both before any transaction opens, so
    a rejection never touches state or events."""

    if note is None:
        if required:
            raise ValidationError(f"{field} is required")
        return None
    if not isinstance(note, str) or not note.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    if len(note.encode("utf-8")) > MAX_COMMAND_NOTE_BYTES:
        raise ValidationError(
            f"{field} exceeds {MAX_COMMAND_NOTE_BYTES} bytes"
        )
    return note


def _normalize_ttl(ttl_seconds: int | None) -> int | None:
    """Validate a caller-supplied TTL into its canonical identity form.

    ``None`` means "no expiry deadline" and is a distinct identity member from
    every integer TTL — it is not a default that an explicit TTL may later
    match against.
    """

    if ttl_seconds is None:
        return None
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValidationError("ttl_seconds must be an integer number of seconds")
    if not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ValidationError(
            f"ttl_seconds must be within 1..{MAX_TTL_SECONDS} (one month)"
        )
    return ttl_seconds


def _ttl_expires_at(ttl_seconds: int | None, now: str) -> str | None:
    """Absolute deadline for an ALREADY normalized TTL, at submit time only.

    This value is written once and is never used to judge a retry: see
    :func:`_stored_ttl_seconds`.
    """

    if ttl_seconds is None:
        return None
    return (datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)).isoformat()


def _stored_ttl_seconds(command: Command) -> int | None:
    """Recover the originally requested TTL from a persisted row.

    Invariant (why no v4 schema is needed): v3 writes ``requested_at`` and
    ``expires_at`` exactly once, in the single ``INSERT`` of
    :func:`submit_command`, and no code path or trigger rewrites either column
    — the only ``UPDATE commands`` in this module is :func:`_cas_state`, whose
    assignments are limited to ``state``/``updated_at``/``result_json``/
    ``note``, and the v3 trigger forbids ``DELETE`` outright.  Therefore the
    pair is immutable for the row's whole life and
    ``expires_at - requested_at`` reproduces the caller's original validated
    ``ttl_seconds`` bit-for-bit (``None`` when the row was submitted with no
    deadline).  That relative duration is the durable representation of the
    request's TTL, and it is what idempotency compares — never a retry's
    freshly computed absolute deadline.

    Fail-closed: a row whose pair cannot yield a sane duration (unparseable,
    naive, non-integral, or outside the accepted TTL window) contradicts the
    invariant above, so it can only come from tampering or a half-repair.  A
    :class:`~teamcraft.errors.StorageError` is raised instead of guessing equal
    or different, because inventing a TTL here would either silently reuse or
    silently conflict a request on corrupted identity data.
    """

    if command.expires_at is None:
        return None
    try:
        requested = datetime.fromisoformat(command.requested_at)
        expires = datetime.fromisoformat(command.expires_at)
    except ValueError as exc:
        raise StorageError(
            f"command {command.id} carries an unparseable TTL pair"
            f" (requested_at={command.requested_at!r},"
            f" expires_at={command.expires_at!r}); cannot recover the original"
            " ttl_seconds for idempotency identity"
        ) from exc
    if requested.tzinfo is None or expires.tzinfo is None:
        raise StorageError(
            f"command {command.id} TTL pair is not timezone-aware"
            f" (requested_at={command.requested_at!r},"
            f" expires_at={command.expires_at!r})"
        )
    delta = (expires - requested).total_seconds()
    if not float(delta).is_integer():
        raise StorageError(
            f"command {command.id} derived TTL {delta!r} is not a whole number"
            " of seconds; the requested_at/expires_at invariant is violated"
        )
    ttl = int(delta)
    if not 1 <= ttl <= MAX_TTL_SECONDS:
        raise StorageError(
            f"command {command.id} derived TTL {ttl}s falls outside"
            f" 1..{MAX_TTL_SECONDS}; the requested_at/expires_at invariant is"
            " violated"
        )
    return ttl


def _is_expired(expires_at: str | None, now: str) -> bool:
    """Expiry by ISO-lexicographic ordering (both sides are UTC +00:00)."""

    return expires_at is not None and expires_at <= now


def _evidence_staleness(
    conn: TeamCraftConnection, evidence_json: str
) -> str | None:
    """Return a reason when bound evidence no longer holds, else ``None``.

    Only the Task-version binding is fact-checked live (it is the one every
    CAS bump advances); attempt/digest bindings are checked by whoever
    consumes them (M2-05/06), here they merely round-trip.
    """

    evidence = json.loads(evidence_json)
    if not isinstance(evidence, dict) or "task_id" not in evidence:
        return None
    row = conn.execute(
        "SELECT version FROM tasks WHERE id = ?", (evidence["task_id"],)
    ).fetchone()
    if row is None:
        return "EVIDENCE_TARGET_GONE"
    if int(row[0]) != int(evidence["task_version"]):
        return "EVIDENCE_STALE"
    return None


def _associate(conn: TeamCraftConnection, evidence_json: str) -> tuple[str | None, str | None]:
    """Resolve (project_id, task_id) for events from the evidence binding.

    An explicit ``project_id`` in the evidence wins (M3-04: project-scoped
    commands such as creates, scheduler gates, and manual CEO requests have
    no Task binding yet — their Command/Event audit must still be traceable
    through the project).  Otherwise the Task row's project is used.
    A binding whose Task row does not (or no longer) exists and carries no
    project resolves to (None, None) — the events table enforces the foreign
    key, and the command row itself still records the (possibly vanished)
    binding for audit.
    """

    evidence = json.loads(evidence_json)
    explicit = evidence.get("project_id") if isinstance(evidence, dict) else None
    task_id = evidence.get("task_id") if isinstance(evidence, dict) else None
    if task_id is not None:
        row = conn.execute(
            "SELECT project_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is not None:
            return str(row[0]), str(task_id)
        # task vanished (or not yet created): fall back to the explicit
        # project stamp so the audit trail is never orphaned.
        if isinstance(explicit, str) and explicit.strip():
            return explicit, None
        return None, None
    if isinstance(explicit, str) and explicit.strip():
        return explicit, None
    return None, None


# ---------------------------------------------------------------------------
# public API — submit (idempotency core)
# ---------------------------------------------------------------------------


def _matches_identity(
    command: Command,
    *,
    kind: str,
    payload_json: str,
    evidence_json: str,
    ttl_seconds: int | None,
) -> bool:
    """Full normalized request identity: kind, payload, evidence, TTL.

    All four components must match for a key to be a REPLAY; a difference in
    any one of them is a different request under the same key, i.e. a
    conflict.  TTL participates as relative seconds recovered from the stored
    row (see :func:`_stored_ttl_seconds`), compared against the caller's
    normalized ``ttl_seconds`` — not against an absolute deadline.
    """

    return (
        command.kind == kind
        and command.payload_json == payload_json
        and command.evidence_json == evidence_json
        and _stored_ttl_seconds(command) == ttl_seconds
    )


def submit_command(
    conn: TeamCraftConnection,
    *,
    idempotency_key: str,
    kind: str,
    payload_json: str = "{}",
    evidence_json: str = "{}",
    ttl_seconds: int | None = None,
    id_factory: IdFactory | None = None,
    clock: Clock | None = None,
) -> Submission:
    """Persist a command under its idempotency key.

    Same key + identical request identity (kind, payload, evidence, TTL)
    returns the stored command (``created`` False) — for a confirmed command
    this IS the reused result.  Same key with any different kind/payload/
    evidence/TTL raises :class:`IdempotencyConflictError`; nothing is written
    by refusals.  A brand-new submission writes the row and its
    ``command.submitted`` event inside one transaction.  Concurrent identical
    submissions converge onto the winner's row via the UNIQUE index;
    concurrent submissions that differ (in any identity component, TTL
    included) produce exactly one winner and one conflict.
    """

    _require_head(conn)
    key = _validate_key(idempotency_key)
    kind = _validate_kind(kind)
    payload_json = _validate_json(payload_json)
    evidence_json = _validate_evidence(evidence_json)
    ttl_seconds = _normalize_ttl(ttl_seconds)
    now = _utc_iso(clock)
    expires_at = _ttl_expires_at(ttl_seconds, now)
    with _tx(conn):
        existing = conn.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            command = _row_to_command(existing)
            if _matches_identity(
                command, kind=kind, payload_json=payload_json,
                evidence_json=evidence_json, ttl_seconds=ttl_seconds,
            ):
                return Submission(command=command, created=False)
            raise IdempotencyConflictError(
                f"idempotency key {key!r} already owns command"
                f" {command.id} with different content (kind/payload/evidence"
                "/ttl must match exactly to reuse)",
                idempotency_key=key,
                existing_command_id=command.id,
            )
        factory = id_factory if id_factory is not None else DEFAULT_ID_FACTORY
        command_id = factory.new("command")
        try:
            conn.execute(
                "INSERT INTO commands (id, idempotency_key, kind, payload_json,"
                " evidence_json, state, requested_at, updated_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (command_id, key, kind, payload_json, evidence_json, now, now,
                 expires_at),
            )
        except sqlite3.IntegrityError as exc:
            # Lost a race with an identical submit that committed first:
            # converge onto its row (at-least-once submit, exactly-once row);
            # different content under the key stays a conflict.
            raced = conn.execute(
                f"SELECT {_COMMAND_COLUMNS} FROM commands"
                " WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if raced is None:
                raise StorageError(
                    f"command insert rejected but no holder row visible: {exc}"
                ) from exc
            command = _row_to_command(raced)
            if _matches_identity(
                command, kind=kind, payload_json=payload_json,
                evidence_json=evidence_json, ttl_seconds=ttl_seconds,
            ):
                return Submission(command=command, created=False)
            raise IdempotencyConflictError(
                f"idempotency key {key!r} was claimed concurrently with"
                " different content",
                idempotency_key=key,
                existing_command_id=command.id,
            ) from exc
        project_id, task_id = _associate(conn, evidence_json)
        append_event(
            conn,
            "command.submitted",
            project_id=project_id,
            task_id=task_id,
            payload_json=json.dumps(
                {
                    "command_id": command_id,
                    "kind": kind,
                    "idempotency_key": key,
                    "expires_at": expires_at,
                },
                sort_keys=True,
            ),
            clock=clock,
        )
        command = _fetch(conn, command_id)
    return Submission(command=command, created=True)


# ---------------------------------------------------------------------------
# public API — state machine (every edge is a CAS + event, one transaction)
# ---------------------------------------------------------------------------


def _refuse(command: Command, event: str, reason: str, message: str) -> IllegalTransitionError:
    return IllegalTransitionError(
        message, entity="command", current=command.state, event=event,
        reason_code=reason,
    )


def _cas_state(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    from_states: tuple[str, ...],
    to_state: str,
    now: str,
    result_json: str | None = None,
    note: str | None = None,
    set_result: bool = False,
    set_note: bool = False,
) -> int:
    assignments = ["state = ?", "updated_at = ?"]
    params: list[Any] = [to_state, now]
    if set_result:
        assignments.append("result_json = ?")
        params.append(result_json)
    if set_note:
        assignments.append("note = ?")
        params.append(note)
    placeholders = ", ".join("?" for _ in from_states)
    cursor = conn.execute(
        f"UPDATE commands SET {', '.join(assignments)}"
        f" WHERE id = ? AND state IN ({placeholders})",
        (*params, command_id, *from_states),
    )
    return int(cursor.rowcount)


def claim_command(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    clock: Clock | None = None,
) -> Command:
    """Admission for execution: CAS ``pending -> running``.

    Under the write lock the claim re-verifies TTL and evidence (review
    8.2-7/-9): an expired row refuses with ``TTL_EXPIRED`` and stale
    evidence with ``EVIDENCE_STALE``/``EVIDENCE_TARGET_GONE`` — the refusal
    itself writes nothing (the sweep/expire APIs transition the row); only
    a successful claim mutates and appends its event atomically.
    """

    _require_head(conn)
    now = _utc_iso(clock)
    with _tx(conn):
        command = _fetch(conn, command_id)
        if command.state in TERMINAL_COMMAND_STATES:
            raise _refuse(
                command, "claim", "TERMINAL_FROZEN",
                f"command {command_id} is terminal ({command.state})",
            )
        if command.state == "running":
            raise CommandStateConflictError(
                f"command {command_id} is already running (claimed by"
                " someone else)",
                command_id=command_id,
                expected_state="pending",
                found_state=command.state,
            )
        if command.state == "unknown":
            raise _refuse(
                command, "claim", "ILLEGAL_TRANSITION",
                f"command {command_id} is UNKNOWN and must reconcile before"
                " any re-admission (8.2-11: never blind-retry)",
            )
        if _is_expired(command.expires_at, now):
            raise _refuse(
                command, "claim", "TTL_EXPIRED",
                f"command {command_id} expired at {command.expires_at};"
                " claiming a TTL-expired command is refused (run the sweep to"
                " transition it)",
            )
        stale = _evidence_staleness(conn, command.evidence_json)
        if stale is not None:
            raise _refuse(
                command, "claim", stale,
                f"command {command_id} bound evidence no longer holds"
                f" ({stale}); execution against stale evidence is refused",
            )
        if _cas_state(conn, command_id, from_states=("pending",),
                      to_state="running", now=now) == 0:
            found = _fetch(conn, command_id)
            raise CommandStateConflictError(
                f"command {command_id} state moved under the claim",
                command_id=command_id,
                expected_state="pending",
                found_state=found.state,
            )
        project_id, task_id = _associate(conn, command.evidence_json)
        append_event(
            conn,
            "command.claimed",
            project_id=project_id,
            task_id=task_id,
            payload_json=json.dumps({"command_id": command_id}, sort_keys=True),
            clock=clock,
        )
        return _fetch(conn, command_id)


def confirm_command(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    result_json: str = "{}",
    note: str | None = None,
    clock: Clock | None = None,
) -> Command:
    """CAS ``running -> confirmed`` storing the durable result."""

    _require_head(conn)
    note = _validate_note(note, required=False)
    result_json = _validate_result(result_json)
    now = _utc_iso(clock)
    with _tx(conn):
        command = _fetch(conn, command_id)
        if command.state in TERMINAL_COMMAND_STATES:
            raise _refuse(
                command, "confirm", "TERMINAL_FROZEN",
                f"command {command_id} is terminal ({command.state})",
            )
        if command.state != "running":
            raise _refuse(
                command, "confirm", "ILLEGAL_TRANSITION",
                f"command {command_id} in state {command.state!r} cannot be"
                " confirmed directly (must run first; UNKNOWN closes via"
                " reconcile)",
            )
        if _cas_state(conn, command_id, from_states=("running",),
                      to_state="confirmed", now=now, result_json=result_json,
                      note=note, set_result=True, set_note=note is not None) == 0:
            found = _fetch(conn, command_id)
            raise CommandStateConflictError(
                f"command {command_id} state moved under the confirm",
                command_id=command_id,
                expected_state="running",
                found_state=found.state,
            )
        project_id, task_id = _associate(conn, command.evidence_json)
        append_event(
            conn,
            "command.confirmed",
            project_id=project_id,
            task_id=task_id,
            payload_json=json.dumps(
                {"command_id": command_id, "result_json": result_json},
                sort_keys=True,
            ),
            clock=clock,
        )
        return _fetch(conn, command_id)


def mark_command_unknown(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    note: str,
    clock: Clock | None = None,
) -> Command:
    """CAS ``running -> unknown`` for ambiguous outcomes (no blind retry).

    The note recording what was observed is REQUIRED — unknown without an
    audit trail is exactly the failure mode review 8.2-11 closes.
    """

    _require_head(conn)
    note = _validate_note(note, field="audit note")
    now = _utc_iso(clock)
    with _tx(conn):
        command = _fetch(conn, command_id)
        if command.state != "running":
            raise _refuse(
                command, "mark_unknown",
                "TERMINAL_FROZEN" if command.state in TERMINAL_COMMAND_STATES
                else "ILLEGAL_TRANSITION",
                f"command {command_id} in state {command.state!r} cannot move"
                " to unknown (only a running execution outcome can be"
                " uncertain)",
            )
        if _cas_state(conn, command_id, from_states=("running",),
                      to_state="unknown", now=now, note=note,
                      set_note=True) == 0:
            found = _fetch(conn, command_id)
            raise CommandStateConflictError(
                f"command {command_id} state moved under mark_unknown",
                command_id=command_id,
                expected_state="running",
                found_state=found.state,
            )
        project_id, task_id = _associate(conn, command.evidence_json)
        append_event(
            conn,
            "command.unknown",
            project_id=project_id,
            task_id=task_id,
            payload_json=json.dumps(
                {"command_id": command_id, "note": note}, sort_keys=True
            ),
            clock=clock,
        )
        return _fetch(conn, command_id)


def reconcile_command(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    outcome: str,
    note: str,
    result_json: str | None = None,
    clock: Clock | None = None,
) -> Command:
    """Settle ``running``/``unknown`` against observed fact (8.2-11).

    ``outcome`` is ``confirmed`` (fact shows the effect happened — carry its
    ``result_json``) or ``expired`` (fact shows it did not happen / was
    abandoned).  A mandatory audit note records the evidence.  UNKNOWN never
    auto-retries: no path in this module creates a successor command.
    """

    _require_head(conn)
    if outcome not in RECONCILE_OUTCOMES:
        raise ValidationError(
            f"reconcile outcome must be one of {RECONCILE_OUTCOMES}"
        )
    note = _validate_note(note, field="audit note")
    if outcome == "confirmed":
        result_json = "{}" if result_json is None else _validate_result(result_json)
    elif result_json is not None:
        raise ValidationError("an expired reconcile must not carry a result")
    now = _utc_iso(clock)
    with _tx(conn):
        command = _fetch(conn, command_id)
        if command.state not in ("running", "unknown"):
            raise _refuse(
                command, "reconcile",
                "TERMINAL_FROZEN" if command.state in TERMINAL_COMMAND_STATES
                else "ILLEGAL_TRANSITION",
                f"command {command_id} in state {command.state!r} has nothing"
                " to reconcile (only running/unknown executions settle by fact)",
            )
        if _cas_state(
            conn, command_id, from_states=("running", "unknown"),
            to_state=outcome, now=now, result_json=result_json, note=note,
            set_result=outcome == "confirmed", set_note=True,
        ) == 0:
            found = _fetch(conn, command_id)
            raise CommandStateConflictError(
                f"command {command_id} state moved under reconcile",
                command_id=command_id,
                expected_state=command.state,
                found_state=found.state,
            )
        project_id, task_id = _associate(conn, command.evidence_json)
        append_event(
            conn,
            "command.reconciled",
            project_id=project_id,
            task_id=task_id,
            payload_json=json.dumps(
                {"command_id": command_id, "outcome": outcome, "note": note},
                sort_keys=True,
            ),
            clock=clock,
        )
        return _fetch(conn, command_id)


# ---------------------------------------------------------------------------
# expiry: TTL sweep + evidence invalidation (the M1-05 acceptance gates)
# ---------------------------------------------------------------------------


def _expire_locked(
    conn: TeamCraftConnection, command: Command, reason: str, now: str,
    clock: Clock | None,
) -> Command:
    if _cas_state(conn, command.id, from_states=("pending",),
                  to_state="expired", now=now, note=reason,
                  set_note=True) == 0:
        return _fetch(conn, command.id)  # raced; current truth wins
    project_id, task_id = _associate(conn, command.evidence_json)
    append_event(
        conn,
        "command.expired",
        project_id=project_id,
        task_id=task_id,
        payload_json=json.dumps(
            {"command_id": command.id, "reason": reason}, sort_keys=True
        ),
        clock=clock,
    )
    return _fetch(conn, command.id)


def expire_command(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    reason: str = "EXPIRED",
    clock: Clock | None = None,
) -> Command:
    """Manual CAS ``pending -> expired`` (terminal; records the reason)."""

    _require_head(conn)
    reason = _validate_note(reason, field="reason")
    now = _utc_iso(clock)
    with _tx(conn):
        command = _fetch(conn, command_id)
        if command.state != "pending":
            raise _refuse(
                command, "expire",
                "TERMINAL_FROZEN" if command.state in TERMINAL_COMMAND_STATES
                else "ILLEGAL_TRANSITION",
                f"command {command_id} in state {command.state!r} cannot be"
                " expired directly (running/unknown settle via reconcile;"
                " confirmed is done)",
            )
        return _expire_locked(conn, command, reason.strip(), now, clock)


def invalidate_stale_commands_for_task(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    now: str | None = None,
    clock: Clock | None = None,
) -> list[tuple[str, str]]:
    """Expire PENDING commands whose evidence binds this task at an older
    version (review 8.2-6: "任一绑定字段…变化都使旧…失效"; the same principle
    applied to queued commands — a stale queued side-effect must never fire).

    Runs inside the caller's transaction (wired into every Task CAS bump by
    :mod:`teamcraft.domain`), so the version bump and the invalidations are
    one atomic unit.  Returns ``[(command_id, reason), ...]``.
    """

    require_entity_id(task_id)
    if now is None:
        now = _utc_iso(clock)
    rows = conn.execute(
        f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE state = 'pending'"
    ).fetchall()
    affected: list[tuple[str, str]] = []
    for row in rows:
        command = _row_to_command(row)
        evidence = json.loads(command.evidence_json)
        if (
            isinstance(evidence, dict)
            and evidence.get("task_id") == task_id
            and int(evidence.get("task_version", -1)) != _live_task_version(
                conn, task_id
            )
        ):
            _expire_locked(conn, command, "EVIDENCE_STALE", now, clock)
            affected.append((command.id, "EVIDENCE_STALE"))
    return affected


def _live_task_version(conn: TeamCraftConnection, task_id: str) -> int:
    row = conn.execute(
        "SELECT version FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return -1 if row is None else int(row[0])


def sweep_stale_commands(
    conn: TeamCraftConnection,
    *,
    clock: Clock | None = None,
) -> list[tuple[str, str]]:
    """Transition every due/invalidated PENDING command to EXPIRED.

    TTL violations are labelled ``TTL_EXPIRED``, evidence drift
    ``EVIDENCE_STALE``/``EVIDENCE_TARGET_GONE``; each transition carries its
    own event, all inside one transaction.  RUNNING commands are never
    swept — an in-flight side effect may already exist and must reconcile
    (8.2-11), never be silently expired.
    """

    _require_head(conn)
    now = _utc_iso(clock)
    with _tx(conn):
        rows = conn.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE state = 'pending'"
            " ORDER BY requested_at ASC, id ASC"
        ).fetchall()
        swept: list[tuple[str, str]] = []
        for row in rows:
            command = _row_to_command(row)
            reason: str | None = None
            if _is_expired(command.expires_at, now):
                reason = "TTL_EXPIRED"
            else:
                stale = _evidence_staleness(conn, command.evidence_json)
                if stale is not None:
                    reason = stale
            if reason is None:
                continue
            _expire_locked(conn, command, reason, now, clock)
            swept.append((command.id, reason))
    return swept


# ---------------------------------------------------------------------------
# public API — reads
# ---------------------------------------------------------------------------


def get_command(conn: TeamCraftConnection, command_id: str) -> Command:
    _require_head(conn)
    return _fetch(conn, command_id)


def command_is_current(conn: TeamCraftConnection, command: Command) -> bool:
    """Live evidence verdict for scheduler admission (re-checked at claim)."""

    _require_head(conn)
    return _evidence_staleness(conn, command.evidence_json) is None


def list_commands(
    conn: TeamCraftConnection,
    *,
    state: str | None = None,
    limit: int = 100,
) -> tuple[Command, ...]:
    _require_head(conn)
    if state is not None and state not in COMMAND_STATES:
        raise ValidationError(
            f"state filter must be one of {COMMAND_STATES}, got {state!r}"
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not (
        1 <= limit <= 1000
    ):
        raise ValidationError("limit must be an integer in 1..1000")
    if state is None:
        rows = conn.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM commands"
            " ORDER BY requested_at ASC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE state = ?"
            " ORDER BY requested_at ASC, id ASC LIMIT ?",
            (state, limit),
        ).fetchall()
    return tuple(_row_to_command(row) for row in rows)
