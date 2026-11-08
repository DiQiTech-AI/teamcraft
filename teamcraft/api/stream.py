"""M3-03 Server-Sent Events over the durable SQLite event stream.

Scope pinned by the M0 review (9.3 M3-03 / FR-014 / ADR-001 8.7-5):
authenticated, read-only SSE with ``after``/``Last-Event-ID`` cursor
semantics, bounded batched backfill, heartbeats, bounded connection and
payload behaviour, and an explicit ``snapshot_required`` fallback when a
sequence gap is detected.

Design invariants (pinned by ``tests/test_api_m303.py``):

* **SQLite is the sole ordering authority.**  Frames are produced by
  polling the durable ``events.seq`` on a strict ``mode=ro`` connection —
  the same table and ordering the core read helpers expose.  There is no
  process-local broadcaster: the only in-memory state is this
  connection's delivered cursor and a connection-count resource guard,
  so a reconnecting client converges purely from persisted seq values.
* **Cursor semantics.**  The resume position comes from the ``after``
  query parameter or the SSE ``Last-Event-ID`` header (the query wins
  when both are present).  Malformed cursors fail with the uniform
  ``422 VALIDATION_FAILED``; a cursor ahead of the durable stream is a
  lost-events signal and fails before the stream opens with
  ``409 CONFLICT`` carrying ``action: "snapshot_required"``.
* **Bounded by construction.**  Concurrent streams are capped by
  :class:`StreamConnectionLimiter` (a resource guard, never an event
  source — exceeding it yields ``429 TOO_MANY_REQUESTS``); backfill runs
  in capped batches under a per-connection budget; every frame is
  size-capped (an oversized payload degrades to a compact, flagged
  reference instead of an unbounded write); connections expire after
  ``max_duration_seconds`` so clients re-sync periodically.  Resource
  release is guarded at the ASGI lifecycle boundary (see
  :class:`_GuardedStreamingResponse`): even when the transport fails or the
  client vanishes before the body generator ever starts, the read
  connection and the limiter slot are freed via an idempotent cleanup.
* **Gaps fall back to a snapshot.**  Whenever the next committed seq is
  not ``cursor + 1``, the stream emits a named ``snapshot_required``
  event — with no ``id:`` line, because control frames never consume
  sequence IDs — and closes.  The client refreshes from the REST
  snapshot/query endpoints and reconnects with a re-established cursor.
  Backfill overflow and connection expiry use the same explicit signal.
* **Strict monotonicity.**  Within a connection, event IDs are the
  durable seq values in strictly increasing order; a row whose seq is
  not beyond the delivered cursor is dropped (duplicate guard), so a
  reconnect that overlaps a prior connection never re-emits an ID.
* **Read-only.**  All reads run on ``mode=ro`` SQLite channels (SQLite
  physically rejects DML there).  The stream never mutates state, never
  enqueues a Wake and never dispatches; payload text is secret-redacted
  exactly like the M3-02 Query surface.

Path note: ``/api/v1/events/stream`` is registered *before* the M3-02
query router in :func:`teamcraft.api.app.create_app`, otherwise Starlette
would match it against ``GET /api/v1/events/{event_id}``.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import storage
from ..errors import NotFoundError, ValidationError
from ..query import EVENT_PROGRESS_CLASS, QueryService, QueryUnavailableError
from .errors import (
    CONFLICT,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    TOO_MANY_REQUESTS,
    VALIDATION_FAILED,
    ApiError,
)
from .redaction import redact_value

#: SSE reconnection-state header (lower-casing is part of the SSE spec).
LAST_EVENT_ID_HEADER = "Last-Event-ID"
#: Named control event telling the client to refetch a REST snapshot.
EVENT_SNAPSHOT_REQUIRED = "snapshot_required"
#: Explicit machine-readable recovery hints carried in control payloads.
ACTION_SNAPSHOT_REQUIRED = "snapshot_required"
ACTION_REST_BACKFILL = "rest_backfill_required"
#: Maximum accepted textual length of a Last-Event-ID value.
MAX_LAST_EVENT_ID_LENGTH = 20

# Defaults chosen for a local loopback API; tests pass faster values via
# SseConfig, production wiring uses these conservative ones.
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
MIN_POLL_INTERVAL_SECONDS = 0.01
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
MIN_HEARTBEAT_INTERVAL_SECONDS = 0.05
DEFAULT_BACKFILL_BATCH_SIZE = 100
MIN_BACKFILL_BATCH_SIZE = 1
#: Hard cap per backfill batch — matches the core event page limit.
MAX_BACKFILL_BATCH_SIZE = 1000
DEFAULT_MAX_BACKFILL_EVENTS = 1000
DEFAULT_MAX_CONNECTIONS = 8
MIN_MAX_CONNECTIONS = 1
MAX_MAX_CONNECTIONS = 128
DEFAULT_MAX_DURATION_SECONDS = 600.0
MIN_MAX_DURATION_SECONDS = 0.05
DEFAULT_RETRY_MS = 1000
MIN_RETRY_MS = 1
MAX_RETRY_MS = 60000
#: Upper bound for one serialized SSE frame (see :func:`_bounded_dto`).
DEFAULT_MAX_FRAME_BYTES = 192 * 1024
MIN_MAX_FRAME_BYTES = 128
MAX_MAX_FRAME_BYTES = 4 * 1024 * 1024

#: Uniform failure texts (no paths, no identifiers, no driver text).
_UNAVAILABLE_MESSAGE = "The core event stream is unavailable."
_NOT_FOUND_MESSAGE = "The requested resource does not exist."
_INVALID_CURSOR_MESSAGE = "The resume cursor is invalid."
_INVALID_QUERY_MESSAGE = "The query parameters are invalid."
_CURSOR_AHEAD_MESSAGE = (
    "The resume cursor is ahead of the durable event stream."
)
_BACKFILL_TOO_LARGE_MESSAGE = (
    "The pending backfill exceeds what one stream connection may carry."
)
_TOO_MANY_STREAMS_MESSAGE = (
    "The maximum number of concurrent event streams is in use."
)

_EVENT_COLUMNS = (
    "seq, event_id, kind, project_id, task_id, attempt_id, payload_json,"
    " occurred_at"
)


@dataclass(frozen=True)
class SseConfig:
    """Transport bounds for one SSE stream surface (validated, immutable)."""

    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    backfill_batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE
    max_backfill_events: int = DEFAULT_MAX_BACKFILL_EVENTS
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS
    retry_ms: int = DEFAULT_RETRY_MS
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES

    def validate(self) -> "SseConfig":
        """Return a validated copy; raise :class:`ValidationError` if not."""

        def _seconds(value: float, field: str, low: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"{field} must be a number")
            number = float(value)
            if not math.isfinite(number) or number < low:
                raise ValidationError(f"{field} must be at least {low}")
            return number

        def _integer(value: int, field: str, low: int, high: int) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"{field} must be an integer")
            if not low <= value <= high:
                raise ValidationError(f"{field} must be in {low}..{high}")
            return value

        poll = _seconds(
            self.poll_interval_seconds,
            "poll_interval_seconds",
            MIN_POLL_INTERVAL_SECONDS,
        )
        heartbeat = _seconds(
            self.heartbeat_interval_seconds,
            "heartbeat_interval_seconds",
            MIN_HEARTBEAT_INTERVAL_SECONDS,
        )
        batch = _integer(
            self.backfill_batch_size,
            "backfill_batch_size",
            MIN_BACKFILL_BATCH_SIZE,
            MAX_BACKFILL_BATCH_SIZE,
        )
        max_backfill = _integer(
            self.max_backfill_events,
            "max_backfill_events",
            batch,
            MAX_BACKFILL_BATCH_SIZE,
        )
        connections = _integer(
            self.max_connections,
            "max_connections",
            MIN_MAX_CONNECTIONS,
            MAX_MAX_CONNECTIONS,
        )
        duration = _seconds(
            self.max_duration_seconds,
            "max_duration_seconds",
            MIN_MAX_DURATION_SECONDS,
        )
        retry = _integer(self.retry_ms, "retry_ms", MIN_RETRY_MS, MAX_RETRY_MS)
        frame = _integer(
            self.max_frame_bytes,
            "max_frame_bytes",
            MIN_MAX_FRAME_BYTES,
            MAX_MAX_FRAME_BYTES,
        )
        return SseConfig(
            poll_interval_seconds=poll,
            heartbeat_interval_seconds=heartbeat,
            backfill_batch_size=batch,
            max_backfill_events=max_backfill,
            max_connections=connections,
            max_duration_seconds=duration,
            retry_ms=retry,
            max_frame_bytes=frame,
        )


class StreamConnectionLimiter:
    """Counted cap over concurrently open SSE connections.

    This is only a resource guard (review M3-03 risk "slow clients pin
    resources").  It carries no events and keeps no sequence state, so it
    can never drift from, let alone replace, the SQLite authority.
    """

    def __init__(self, max_connections: int) -> None:
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or not MIN_MAX_CONNECTIONS <= max_connections <= MAX_MAX_CONNECTIONS
        ):
            raise ValidationError(
                "max_connections must be an integer in "
                f"{MIN_MAX_CONNECTIONS}..{MAX_MAX_CONNECTIONS}"
            )
        self._max = max_connections
        self._active = 0
        self._lock = threading.Lock()

    @property
    def max_connections(self) -> int:
        return self._max

    @property
    def active(self) -> int:
        """Connections currently holding a slot (for observability/tests)."""

        with self._lock:
            return self._active

    def try_acquire(self) -> bool:
        """Reserve one slot; ``False`` when the cap is reached."""

        with self._lock:
            if self._active >= self._max:
                return False
            self._active += 1
            return True

    def release(self) -> bool:
        """Return one slot; ``False`` (without going negative) if empty."""

        with self._lock:
            if self._active == 0:
                return False
            self._active -= 1
            return True


# ---------------------------------------------------------------------------
# Read channel (strict mode=ro, schema-gated — mirrors QueryService._read
# locally because the core service keeps its channel private and core files
# are outside this milestone's ownership).
# ---------------------------------------------------------------------------


def _open_read_connection(service: QueryService) -> sqlite3.Connection:
    path = service.database_path
    if not path.is_file():
        raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE)
    uri = path.as_uri() + "?mode=ro"
    try:
        # check_same_thread=False: the stream hops threads between the
        # request handler and run_in_threadpool reads; access is strictly
        # sequential (every read is awaited) and the channel is ro.
        conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                              check_same_thread=False)
    except sqlite3.Error as exc:
        raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE) from exc
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != storage.SCHEMA_VERSION:
            raise ApiError(503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE)
    except ApiError:
        conn.close()
        raise
    except sqlite3.Error as exc:
        conn.close()
        raise ApiError(
            503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE
        ) from exc
    return conn


def _parse_payload(raw: Any) -> Any:
    """Parse a stored JSON column into a container; anything else -> None."""

    if raw is None:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, (dict, list)) else None


def _event_dto(row: Any) -> dict[str, Any]:
    """Stream DTO identical in shape to the M3-02 Query event DTO."""

    kind = str(row[2])
    return {
        "seq": int(row[0]),
        "event_id": str(row[1]),
        "kind": kind,
        "project_id": None if row[3] is None else str(row[3]),
        "task_id": None if row[4] is None else str(row[4]),
        "attempt_id": None if row[5] is None else str(row[5]),
        # Payload bytes are bounded to 64 KiB by the event store itself;
        # the per-frame cap below adds a second, transport-level bound.
        "payload": _parse_payload(row[6]),
        "occurred_at": str(row[7]),
        "progress_class": EVENT_PROGRESS_CLASS.get(kind),
    }


def _durable_seq(conn: sqlite3.Connection, project_id: str | None) -> int:
    if project_id is None:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row[0])


def _fetch_batch(
    conn: sqlite3.Connection,
    after_seq: int,
    project_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Next committed events after ``after_seq`` in durable seq order."""

    if project_id is None:
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE seq > ?"
            " ORDER BY seq ASC LIMIT ?",
            (after_seq, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events"
            " WHERE project_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
            (project_id, after_seq, limit),
        ).fetchall()
    return [_event_dto(row) for row in rows]


def _count_after(
    conn: sqlite3.Connection, after_seq: int, project_id: str | None
) -> int:
    """Exact number of committed events still ahead of ``after_seq``."""

    if project_id is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE seq > ?", (after_seq,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE project_id = ? AND seq > ?",
            (project_id, after_seq),
        ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Frame building
# ---------------------------------------------------------------------------


def _sse_comment(text: str) -> str:
    """An SSE comment line — visible to keep proxies warm, ID-neutral."""

    return f": {text}\n\n"


def _sse_retry(ms: int) -> str:
    """SSE ``retry`` field advising the client's reconnect delay."""

    return f"retry: {ms}\n\n"


def _event_frame(dto: dict[str, Any]) -> str:
    """One data frame; the durable seq is the SSE id (never a UUID)."""

    seq = int(dto["seq"])
    return (
        f"id: {seq}\n"
        f"event: {dto['kind']}\n"
        f"data: {json.dumps(dto, ensure_ascii=True, sort_keys=True)}\n\n"
    )


def _snapshot_required(
    reason: str, *, resume_after: int, **extra: Any
) -> str:
    """Named control event: refetch a REST snapshot before resuming.

    Control frames deliberately carry no ``id:`` line so they cannot shift
    the client's Last-Event-ID (comments and control events never consume
    sequence IDs).
    """

    data: dict[str, Any] = {
        "reason": reason,
        "action": ACTION_SNAPSHOT_REQUIRED,
        "resume_after": resume_after,
    }
    data.update(extra)
    return (
        f"event: {EVENT_SNAPSHOT_REQUIRED}\n"
        f"data: {json.dumps(data, ensure_ascii=True, sort_keys=True)}\n\n"
    )


def _bounded_dto(dto: dict[str, Any], max_frame_bytes: int) -> dict[str, Any]:
    """Cap one serialized frame; oversized payloads degrade to references.

    The event store already bounds ``payload_json`` to 64 KiB, but JSON
    re-escaping can still inflate the frame, so the transport applies a
    second hard bound.  A compact reference keeps the seq (ordering and
    dedup keep working) and flags the payload as omitted rather than
    letting an unbounded frame through.
    """

    if len(_event_frame(dto).encode("utf-8")) <= max_frame_bytes:
        return dto
    payload = dto.get("payload")
    payload_bytes = (
        len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
        if payload is not None
        else 0
    )
    return {
        "seq": dto["seq"],
        "event_id": dto["event_id"],
        "kind": dto["kind"],
        "project_id": dto.get("project_id"),
        "task_id": dto.get("task_id"),
        "attempt_id": dto.get("attempt_id"),
        "occurred_at": dto.get("occurred_at"),
        "progress_class": dto.get("progress_class"),
        "payload": None,
        "payload_truncated": True,
        "payload_size_bytes": payload_bytes,
        "recover": (
            f"/api/v1/events/{dto['event_id']}"
            if dto.get("event_id")
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Batch rendering
# ---------------------------------------------------------------------------


def _emit_batch(
    conn: sqlite3.Connection,
    cursor: int,
    batch: list[dict[str, Any]],
    *,
    project_id: str | None,
    secrets: tuple[str, ...],
    max_frame_bytes: int,
) -> tuple[str, int, str | None]:
    """Render one batch; on a durable gap return the fallback frame.

    Returns ``(frames_text, new_cursor, snapshot_required_or_None)``.
    Rows at or behind the cursor are dropped (duplicate guard).  On the
    global stream the first row strictly ahead of ``cursor + 1`` proves
    those seq values never committed (or were pruned), which triggers the
    explicit ``snapshot_required`` fallback described inline below.
    """

    chunks: list[str] = []
    for dto in batch:
        seq = int(dto["seq"])
        if seq <= cursor:
            continue  # never re-emit or move the cursor backwards
        if seq != cursor + 1 and project_id is None:
            # Global stream only: ``seq`` is dense store-wide, so a jump
            # past ``cursor + 1`` proves those seq values never committed
            # (or were pruned) — the M1-05 contract says a consumer must
            # not guess a gap's cause, so the stream hands control back to
            # the client with an explicit snapshot requirement instead of
            # skipping silently.  Scoped streams see only their project's
            # slice of the dense seq space; foreign rows occupy the
            # intervening ids, which is not a durability gap, so the
            # cursor simply advances with the delivered scoped event.
            gap_frame = _snapshot_required(
                "sequence_gap",
                resume_after=cursor,
                last_seq=cursor,
                expected_seq=cursor + 1,
                gap_size=seq - cursor - 1,
                durable_seq=_durable_seq(conn, project_id),
            )
            return "".join(chunks), cursor, gap_frame
        bounded = _bounded_dto(redact_value(dto, secrets), max_frame_bytes)
        chunks.append(_event_frame(bounded))
        cursor = seq
    return "".join(chunks), cursor, None


# ---------------------------------------------------------------------------
# Cursor resolution
# ---------------------------------------------------------------------------


def _resolve_cursor(request: Request, after: int | None) -> int:
    """Resolve the resume position from ``after`` / ``Last-Event-ID``.

    Precedence: an explicit ``after`` query parameter wins over the
    header.  Anything malformed raises the uniform 422; being ahead of
    the durable stream is checked by the caller (it needs the seq).
    """

    if after is not None:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ApiError(
                422,
                VALIDATION_FAILED,
                _INVALID_CURSOR_MESSAGE,
                details={"field": "after"},
            )
        return after
    raw = request.headers.get(LAST_EVENT_ID_HEADER)
    if raw is None:
        return 0
    text = raw.strip()
    # Digits only (ASCII): signs, spaces, UUIDs and non-ASCII digits are
    # all malformed for this stream — the client must restart from 0 or
    # take a snapshot instead of guessing.
    if (
        not text
        or len(text) > MAX_LAST_EVENT_ID_LENGTH
        or not all("0" <= ch <= "9" for ch in text)
    ):
        raise ApiError(
            422,
            VALIDATION_FAILED,
            _INVALID_CURSOR_MESSAGE,
            details={"field": "last_event_id"},
        )
    return int(text)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class _GuardedStreamingResponse(StreamingResponse):
    """StreamingResponse whose cleanup survives a never-started body.

    Starlette drives the body generator only once the response task gets to
    stream: ``http.response.start`` leaves first, and if the ASGI ``send``
    then raises (client vanished / transport failure) — or the task is
    cancelled before the first chunk — the async generator may never start,
    so *its* ``finally`` never runs and any resources it was holding (the
    read connection, the limiter slot) would leak.  Wrapping ``__call__`` in
    a ``finally`` guarantees the shared idempotent cleanup on every exit
    path: normal completion, send failure, disconnect, or cancellation.

    Normal streaming is untouched — on the happy path this is exactly a
    ``StreamingResponse`` plus one redundant (no-op) cleanup call, because
    ``cleanup`` is the same idempotent closure the generator's ``finally``
    uses.
    """

    def __init__(self, content, *, cleanup: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._cleanup = cleanup

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._cleanup()


def build_events_router(
    service: QueryService,
    *,
    secret_provider: Callable[[], Iterable[str]] = tuple,
    config: SseConfig | None = None,
    limiter: StreamConnectionLimiter | None = None,
) -> APIRouter:
    """Create the authenticated read-only SSE router (M3-03).

    Args:
        service: the Query Service whose database path the stream polls
            (the router itself never uses more than read access).
        secret_provider: current secret values redacted from payloads.
        config: transport bounds; :class:`SseConfig` defaults when omitted.
        limiter: shared connection guard; a fresh one is created otherwise.
    """

    cfg = (config or SseConfig()).validate()
    connection_limiter = limiter or StreamConnectionLimiter(cfg.max_connections)
    router = APIRouter(prefix="/api/v1")

    def _secrets() -> tuple[str, ...]:
        try:
            return tuple(secret_provider())
        except Exception:  # pragma: no cover - defensive for providers
            return ()

    @router.get("/events/stream")
    def stream_events(
        request: Request,
        after: int | None = None,
        project_id: str | None = None,
    ) -> StreamingResponse:
        """GET-only, authenticated SSE over the durable ``events.seq``."""

        cursor = _resolve_cursor(request, after)

        if project_id is not None:
            # Existence + id-shape validation through the Query Service,
            # before any header is written, so failures keep the uniform
            # JSON error shape.
            try:
                service.get_project(project_id)
            except NotFoundError:
                raise ApiError(404, NOT_FOUND, _NOT_FOUND_MESSAGE) from None
            except ValidationError:
                raise ApiError(
                    422, VALIDATION_FAILED, _INVALID_QUERY_MESSAGE
                ) from None
            except QueryUnavailableError:
                raise ApiError(
                    503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE
                ) from None

        if not connection_limiter.try_acquire():
            raise ApiError(
                429,
                TOO_MANY_REQUESTS,
                _TOO_MANY_STREAMS_MESSAGE,
                details={"max_connections": connection_limiter.max_connections},
            )

        released_flag = threading.Event()

        def release_once() -> None:
            if not released_flag.is_set():
                released_flag.set()
                connection_limiter.release()

        conn: sqlite3.Connection | None = None
        try:
            conn = _open_read_connection(service)
            durable = _durable_seq(conn, project_id)
            if cursor > durable:
                # The client claims to have seen a seq this store never
                # committed: its history is lost — demand a snapshot.
                details: dict[str, Any] = {
                    "reason": "cursor_ahead",
                    "action": ACTION_SNAPSHOT_REQUIRED,
                    "cursor": cursor,
                    "durable_seq": durable,
                    "snapshot_endpoint": (
                        f"/api/v1/projects/{project_id}/snapshot"
                        if project_id is not None
                        else "/api/v1/events"
                    ),
                }
                raise ApiError(
                    409, CONFLICT, _CURSOR_AHEAD_MESSAGE, details=details
                )
            pending_total = _count_after(conn, cursor, project_id)
            if pending_total > cfg.max_backfill_events:
                raise ApiError(
                    409,
                    CONFLICT,
                    _BACKFILL_TOO_LARGE_MESSAGE,
                    details={
                        "reason": "backfill_too_large",
                        "action": ACTION_REST_BACKFILL,
                        "cursor": cursor,
                        "pending": pending_total,
                        "budget": cfg.max_backfill_events,
                        "resume_after": cursor,
                        "events_endpoint": "/api/v1/events",
                    },
                )
        except ApiError:
            if conn is not None:
                conn.close()
            release_once()
            raise
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            release_once()
            raise ApiError(
                503, SERVICE_UNAVAILABLE, _UNAVAILABLE_MESSAGE
            ) from None

        assert conn is not None
        closed_flag = threading.Event()

        def close_conn_once() -> None:
            if closed_flag.is_set():
                return
            closed_flag.set()
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - best-effort cleanup
                pass

        def cleanup_once() -> None:
            """Idempotent resource release, shared by generator and guard."""

            close_conn_once()
            release_once()

        response = _GuardedStreamingResponse(
            _stream_frames(
                request,
                conn,
                cursor,
                durable,
                project_id,
                cfg,
                _secrets,
                cleanup_once,
            ),
            cleanup=cleanup_once,
            media_type="text/event-stream; charset=utf-8",
            headers={
                # Streams must never be cached or buffered by intermediaries
                # (review risk: "proxy buffering").
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        # NOTE: no ``response.background`` cleanup task on purpose.  With
        # BaseHTTPMiddleware in the chain (request-id + security), Starlette
        # runs response background tasks right after the headers leave, not
        # after the body finishes — the slot would be released while the
        # stream is still open.  Coverage instead comes from two idempotent
        # hooks: the generator's ``finally`` (normal end, gap, expiry, client
        # disconnect via ``is_disconnected`` polling) and the guard response's
        # ``__call__`` finally, which also covers the case where the body
        # generator never starts at all (transport failure before the first
        # chunk).  Errors before the response exists are released in the
        # handler's except paths.
        return response

    return router


async def _stream_frames(
    request: Request,
    conn: sqlite3.Connection,
    cursor: int,
    durable: int,
    project_id: str | None,
    cfg: SseConfig,
    secrets: Callable[[], tuple[str, ...]],
    cleanup: Callable[[], None],
) -> AsyncIterator[str]:
    """Async generator: bounded backfill, then live tail with heartbeats.

    The loop never holds writable state anywhere except the local cursor;
    every ``id`` it emits is re-read from SQLite at that moment.  Resource
    release goes through the shared idempotent ``cleanup`` (also used by
    :class:`_GuardedStreamingResponse`, which covers the path where this
    generator never starts).
    """

    started = time.monotonic()
    heartbeat_polls = max(
        1, math.ceil(cfg.heartbeat_interval_seconds / cfg.poll_interval_seconds)
    )
    closing: str | None = None
    try:
        # Opening handshake: reconnect advice + watermark comment.  Neither
        # carries an id, so neither consumes sequence space.
        yield _sse_retry(cfg.retry_ms)
        yield _sse_comment(f"stream-open durable_seq={durable} cursor={cursor}")

        # -- bounded batched backfill ------------------------------------------
        budget = cfg.max_backfill_events
        while budget > 0:
            want = min(cfg.backfill_batch_size, budget)
            batch = await run_in_threadpool(
                _fetch_batch, conn, cursor, project_id, want
            )
            if not batch:
                break
            budget -= len(batch)
            text, cursor, gap = _emit_batch(
                conn,
                cursor,
                batch,
                project_id=project_id,
                secrets=secrets(),
                max_frame_bytes=cfg.max_frame_bytes,
            )
            if text:
                yield text
            if gap is not None:
                closing = gap
                break
            if len(batch) < want:
                break  # tail reached
            if budget <= 0:
                more = await run_in_threadpool(
                    _fetch_batch, conn, cursor, project_id, 1
                )
                if more:
                    closing = _snapshot_required(
                        "backfill_limit",
                        resume_after=cursor,
                        durable_seq=_durable_seq(conn, project_id),
                        budget=cfg.max_backfill_events,
                        action=ACTION_REST_BACKFILL,
                        events_endpoint="/api/v1/events",
                    )
                break

        if closing is not None:
            yield closing
            return

        # -- live tail ----------------------------------------------------------
        idle_polls = 0
        while True:
            if await request.is_disconnected():
                return
            if time.monotonic() - started >= cfg.max_duration_seconds:
                yield _snapshot_required(
                    "max_duration",
                    resume_after=cursor,
                    durable_seq=_durable_seq(conn, project_id),
                )
                return
            batch = await run_in_threadpool(
                _fetch_batch, conn, cursor, project_id, cfg.backfill_batch_size
            )
            if batch:
                text, cursor, gap = _emit_batch(
                    conn,
                    cursor,
                    batch,
                    project_id=project_id,
                    secrets=secrets(),
                    max_frame_bytes=cfg.max_frame_bytes,
                )
                if text:
                    yield text
                if gap is not None:
                    yield gap
                    return
                idle_polls = 0
            else:
                idle_polls += 1
                if idle_polls >= heartbeat_polls:
                    # A comment keeps proxies and clients warm without ever
                    # touching id/event/data lines: no ID is consumed.
                    yield _sse_comment(f"heartbeat cursor={cursor}")
                    idle_polls = 0
            await asyncio.sleep(cfg.poll_interval_seconds)
    except sqlite3.Error:
        # The read channel died mid-stream (store replaced/unmounted): the
        # client cannot trust its cursor any more — demand a snapshot.
        yield _snapshot_required(
            "stream_unavailable",
            resume_after=cursor,
        )
    finally:
        # Idempotent: the guard response's ``__call__`` finally may have
        # already released everything (send failure / cancellation), and on
        # the happy path whichever hook runs second is a no-op.
        cleanup()
