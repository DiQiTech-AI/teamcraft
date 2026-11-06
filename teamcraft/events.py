"""M1-05 append-only event stream (M0 review 8.2-1, 9.1 M1-05; PLAN §4/§8.3).

Contract pinned by this module and ``tests/test_events_m105.py``:

* ``events.seq`` is an INTEGER PRIMARY KEY AUTOINCREMENT.  The contract is
  exactly this (empirically verified against SQLite): the seq of COMMITTED
  events is persistent across restarts and strictly increasing — readers
  never observe reordering or regression; an event rolled back with its
  transaction never becomes visible.  AUTOINCREMENT is NOT a gap-free
  guarantee and no such promise is made here: if a consumer observes a gap
  it may safely refetch or take a snapshot (M3-03), but it must not assume
  any particular cause for that gap;
* the stream is append-only where SQLite can enforce it: v3 triggers abort
  any UPDATE or DELETE on ``events``; this API offers no mutation function;
* payload/kind hygiene guards the review's risk column ("payload 可能泄密或
  无限增长"): kinds are short lowercase identifiers, payloads must be valid
  JSON bounded by :data:`MAX_EVENT_PAYLOAD_BYTES` — payload CONTENT review
  (secret scrubbing) is a caller responsibility, noted not claimed here;
* every function joins an already-open outer
  :func:`teamcraft.storage.transaction`, so a state change and its event
  commit or roll back as one atomic unit (the acceptance half of M1-05);
* lagging (v1/v2) schemas are refused with :class:`SchemaOutOfDateError`
  before an opaque "no such table" SQL error (same pattern as M1-04).
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Iterator

from . import storage
from .clock import Clock, SystemClock
from .errors import NotFoundError, SchemaOutOfDateError, StorageError, ValidationError
from .ids import DEFAULT_ID_FACTORY, IdFactory, require_entity_id
from .storage import TeamCraftConnection

#: Kind identifiers: short, lowercase, dotted namespacing allowed.
MAX_EVENT_KIND_LENGTH = 128
#: Upper bound for payload_json text (64 KiB) — events are signals, logs and
#: large evidence stay in controlled files with references (PLAN §4 note).
MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
#: Maximum rows returned by one read of the stream.
MAX_EVENT_PAGE_SIZE = 1000

_EVENT_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")

_EVENT_COLUMNS = (
    "seq, event_id, kind, project_id, task_id, attempt_id, payload_json,"
    " occurred_at"
)


@dataclass(frozen=True)
class Event:
    """One immutable row of the persistent event stream."""

    seq: int
    event_id: str
    kind: str
    project_id: str | None
    task_id: str | None
    attempt_id: str | None
    payload_json: str
    occurred_at: str


def _utc_iso(clock: Clock | None) -> str:
    clock = clock if clock is not None else SystemClock()
    return clock.now().astimezone(timezone.utc).isoformat()


@contextlib.contextmanager
def _tx(conn: TeamCraftConnection) -> Iterator[TeamCraftConnection]:
    """Join an open outer transaction, else open one (same semantics as
    ``domain._tx``; duplicated deliberately so this module never imports the
    domain layer — the dependency direction stays events ← commands ← domain).
    """

    if conn.in_transaction:
        yield conn
    else:
        with storage.transaction(conn) as gated:
            yield gated


def _require_head(conn: TeamCraftConnection) -> None:
    """Refuse the event store on anything but this build's head schema.

    Mirrors :func:`teamcraft.domain.require_head` (duplicate kept small and
    local to avoid an import cycle; consolidation is a noted follow-up).
    """

    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise StorageError(f"cannot read user_version: {exc}") from exc
    if version > storage.SCHEMA_VERSION:
        raise SchemaOutOfDateError(
            f"database schema v{version} is newer than this build "
            f"supports ({storage.SCHEMA_VERSION}); refusing to run"
        )
    if version < storage.SCHEMA_VERSION:
        raise SchemaOutOfDateError(
            f"database schema v{version} lags this build "
            f"({storage.SCHEMA_VERSION}); run teamcraft.migrate_to_head() first"
        )


def _validate_kind(kind: str) -> str:
    if not isinstance(kind, str) or not kind.strip():
        raise ValidationError("event kind must be a non-empty string")
    kind = kind.strip()
    if len(kind) > MAX_EVENT_KIND_LENGTH:
        raise ValidationError(
            f"event kind exceeds {MAX_EVENT_KIND_LENGTH} characters"
        )
    if not _EVENT_KIND_RE.match(kind):
        raise ValidationError(
            f"event kind must match [a-z0-9][a-z0-9_.-]*, got {kind!r}"
        )
    return kind


def _validate_json(payload_json: str) -> str:
    if not isinstance(payload_json, str):
        raise ValidationError("event payload_json must be a string of valid JSON")
    if len(payload_json.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValidationError(
            f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes; store"
            " large evidence in controlled files and reference them"
        )
    try:
        json.loads(payload_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(f"event payload_json is not valid JSON: {exc}") from exc
    return payload_json


def append_event(
    conn: TeamCraftConnection,
    kind: str,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    payload_json: str = "{}",
    event_id: str | None = None,
    id_factory: IdFactory | None = None,
    clock: Clock | None = None,
) -> Event:
    """Append one event; returns the persisted row including its ``seq``.

    When an outer :func:`teamcraft.storage.transaction` is already open this
    joins it — that is the mechanism by which a state change and its event
    share one COMMIT/ROLLBACK.  Without an outer transaction the event is
    committed on its own (still atomic: one statement).
    """

    _require_head(conn)
    kind = _validate_kind(kind)
    payload_json = _validate_json(payload_json)
    if project_id is not None:
        project_id = require_entity_id(project_id)
    if task_id is not None:
        task_id = require_entity_id(task_id)
    if attempt_id is not None:
        attempt_id = require_entity_id(attempt_id)
    if event_id is not None:
        event_id = require_entity_id(event_id)
    else:
        factory = id_factory if id_factory is not None else DEFAULT_ID_FACTORY
        event_id = factory.new("event")
    occurred_at = _utc_iso(clock)
    with _tx(conn):
        try:
            cursor = conn.execute(
                "INSERT INTO events (event_id, kind, project_id, task_id,"
                " attempt_id, payload_json, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, kind, project_id, task_id, attempt_id, payload_json,
                 occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise StorageError(
                f"event append violated a storage constraint: {exc}"
            ) from exc
        seq = int(cursor.lastrowid)
    return Event(
        seq=seq,
        event_id=event_id,
        kind=kind,
        project_id=project_id,
        task_id=task_id,
        attempt_id=attempt_id,
        payload_json=payload_json,
        occurred_at=occurred_at,
    )


def append_event_idempotent(
    conn: TeamCraftConnection,
    kind: str,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    payload_json: str = "{}",
    event_id: str,
    clock: Clock | None = None,
) -> tuple[Event, bool]:
    """Append a caller-chosen event exactly once.

    This does not turn the stream into an upsertable store.  A duplicate ID is
    accepted only when ``kind``, ``project_id``, ``task_id``, ``attempt_id``,
    and ``payload_json`` match; a different event using the same key fails
    closed.  ``occurred_at`` is fixed by the first commit and replay returns
    that original value (the retry clock is intentionally not compared).  It
    returns ``(event, inserted)`` and joins an enclosing transaction just like
    :func:`append_event`.
    """

    _require_head(conn)
    kind = _validate_kind(kind)
    payload_json = _validate_json(payload_json)
    event_id = require_entity_id(event_id)
    if project_id is not None:
        project_id = require_entity_id(project_id)
    if task_id is not None:
        task_id = require_entity_id(task_id)
    if attempt_id is not None:
        attempt_id = require_entity_id(attempt_id)
    with _tx(conn):
        row = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is not None:
            existing = _row_to_event(row)
            if (
                existing.kind != kind
                or existing.project_id != project_id
                or existing.task_id != task_id
                or existing.attempt_id != attempt_id
                or existing.payload_json != payload_json
            ):
                raise StorageError(
                    "deterministic event_id already names a different immutable event"
                )
            return existing, False
        event = append_event(
            conn, kind, project_id=project_id, task_id=task_id,
            attempt_id=attempt_id, payload_json=payload_json,
            event_id=event_id, clock=clock,
        )
    return event, True


def _row_to_event(row: Any) -> Event:
    return Event(
        seq=int(row[0]),
        event_id=str(row[1]),
        kind=str(row[2]),
        project_id=None if row[3] is None else str(row[3]),
        task_id=None if row[4] is None else str(row[4]),
        attempt_id=None if row[5] is None else str(row[5]),
        payload_json=str(row[6]),
        occurred_at=str(row[7]),
    )


def current_seq(conn: TeamCraftConnection) -> int:
    """Highest durably-committed seq (0 when the stream is empty)."""

    _require_head(conn)
    return int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])


def get_event(conn: TeamCraftConnection, event_id: str) -> Event:
    _require_head(conn)
    row = conn.execute(
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE event_id = ?",
        (require_entity_id(event_id),),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"event not found: {event_id}")
    return _row_to_event(row)


def events_after(
    conn: TeamCraftConnection, after_seq: int = 0, *, limit: int = 200,
    project_id: str | None = None,
) -> tuple[Event, ...]:
    """Read the stream after ``after_seq`` (SSE backfill shape, M3-03).

    Ordering authority is the durable seq; a consumer that observes a gap
    should refetch a snapshot — the API deliberately does not define what a
    gap means or implies.
    """

    _require_head(conn)
    if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
        raise ValidationError("after_seq must be a non-negative integer")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_EVENT_PAGE_SIZE
    ):
        raise ValidationError(
            f"limit must be an integer in 1..{MAX_EVENT_PAGE_SIZE}"
        )
    if project_id is None:
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE seq > ? ORDER BY seq ASC"
            " LIMIT ?",
            (after_seq, limit),
        ).fetchall()
    else:
        project_id = require_entity_id(project_id)
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE project_id=? AND seq>? "
            "ORDER BY seq ASC LIMIT ?",
            (project_id, after_seq, limit),
        ).fetchall()
    return tuple(_row_to_event(row) for row in rows)
