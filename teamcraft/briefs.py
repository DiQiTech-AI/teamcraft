"""Immutable, version-bound execution briefs (M2-03).

A brief is evidence captured at dispatch time, not a mutable task description.
Consumers must use :func:`get_attempt_brief`; there is intentionally no update
API.  A new execution requires a new Attempt and a new snapshot.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from . import domain, storage
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, ValidationError
from .ids import require_entity_id

DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class AttemptBrief:
    task_id: str
    attempt_id: str
    task_version: int
    brief_version: int
    brief_digest: str
    deadline_at: str | None
    no_progress_timeout_seconds: int | None
    poll_interval_seconds: int | None
    content: str


def _now(clock: Clock | None) -> str:
    return (clock or SystemClock()).now().astimezone().isoformat()


def _content(value: str | dict[str, Any]) -> str:
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("brief content must be non-empty text or an object")
    if len(value.encode("utf-8")) > 256 * 1024:
        raise ValidationError("brief content exceeds 262144 bytes")
    return value


def _positive(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{name} must be a positive integer or null")
    return value


def brief_digest(content: str, *, task_id: str, attempt_id: str,
                 task_version: int, brief_version: int,
                 deadline_at: str | None = None,
                 no_progress_timeout_seconds: int | None = None,
                 poll_interval_seconds: int | None = None) -> str:
    """Stable SHA-256 over all immutable brief bindings and policy."""
    body = json.dumps({"attempt_id": attempt_id, "brief_version": brief_version,
        "content": content, "deadline_at": deadline_at,
        "no_progress_timeout_seconds": no_progress_timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds, "task_id": task_id,
        "task_version": task_version}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def get_attempt_brief(conn: storage.TeamCraftConnection, attempt_id: str) -> AttemptBrief:
    row = conn.execute("SELECT task_id, attempt_id, task_version, brief_version, brief_digest,"
        " deadline_at, no_progress_timeout_seconds, poll_interval_seconds, content"
        " FROM attempt_briefs WHERE attempt_id = ?", (require_entity_id(attempt_id),)).fetchone()
    if row is None:
        raise NotFoundError(f"attempt brief not found: {attempt_id}")
    return AttemptBrief(str(row[0]), str(row[1]), int(row[2]), int(row[3]), str(row[4]),
        None if row[5] is None else str(row[5]), None if row[6] is None else int(row[6]),
        None if row[7] is None else int(row[7]), str(row[8]))


def create_attempt_brief(conn: storage.TeamCraftConnection, attempt_id: str, *,
                         content: str | dict[str, Any], task_version: int | None = None,
                         deadline_at: str | None = None,
                         no_progress_timeout_seconds: int | None = None,
                         poll_interval_seconds: int | None = None,
                         clock: Clock | None = None) -> AttemptBrief:
    """Persist the sole immutable snapshot for an Attempt.

    Replaying byte-identical inputs returns the existing snapshot.  Any other
    attempt to write the same Attempt is a conflict, never an overwrite.
    """
    attempt_id = require_entity_id(attempt_id)
    content = _content(content)
    no_progress_timeout_seconds = (_positive(no_progress_timeout_seconds, "no_progress_timeout_seconds")
                                   if no_progress_timeout_seconds is not None
                                   else DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS)
    # Polling is never implicitly disabled: M2-04 needs one bounded,
    # low-frequency observation cadence even when the author omitted policy.
    poll_interval_seconds = (_positive(poll_interval_seconds, "poll_interval_seconds")
                             if poll_interval_seconds is not None
                             else DEFAULT_POLL_INTERVAL_SECONDS)
    if deadline_at is not None:
        if not isinstance(deadline_at, str) or not deadline_at.strip():
            raise ValidationError("deadline_at must be an ISO text value or null")
        try:
            if datetime.fromisoformat(deadline_at).tzinfo is None:
                raise ValueError("naive")
        except ValueError as exc:
            raise ValidationError("deadline_at must be timezone-aware ISO-8601") from exc
    with storage.transaction(conn) if not conn.in_transaction else _joined(conn):
        attempt = domain.get_attempt(conn, attempt_id)
        task = domain.get_task(conn, attempt.task_id)
        version = task.version if task_version is None else task_version
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValidationError("task_version must be an integer >= 0")
        if version != task.version:
            raise ValidationError("brief task_version must bind the current Task version")
        prior = conn.execute("SELECT 1 FROM attempt_briefs WHERE attempt_id=?", (attempt_id,)).fetchone()
        if prior is not None:
            existing = get_attempt_brief(conn, attempt_id)
            candidate = brief_digest(content, task_id=task.id, attempt_id=attempt_id,
                task_version=version, brief_version=existing.brief_version, deadline_at=deadline_at,
                no_progress_timeout_seconds=no_progress_timeout_seconds, poll_interval_seconds=poll_interval_seconds)
            if existing.brief_digest == candidate:
                return existing
            raise ConflictError(f"attempt {attempt_id} already has an immutable brief")
        brief_version = int(conn.execute("SELECT COALESCE(MAX(brief_version), 0) + 1 FROM attempt_briefs WHERE task_id=?", (task.id,)).fetchone()[0])
        digest = brief_digest(content, task_id=task.id, attempt_id=attempt_id, task_version=version,
            brief_version=brief_version, deadline_at=deadline_at,
            no_progress_timeout_seconds=no_progress_timeout_seconds, poll_interval_seconds=poll_interval_seconds)
        conn.execute("INSERT INTO attempt_briefs (attempt_id, task_id, task_version, brief_version, brief_digest, deadline_at, no_progress_timeout_seconds, poll_interval_seconds, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, task.id, version, brief_version, digest, deadline_at, no_progress_timeout_seconds, poll_interval_seconds, content, _now(clock)))
    return get_attempt_brief(conn, attempt_id)


class _joined:
    def __init__(self, conn: storage.TeamCraftConnection): self.conn = conn
    def __enter__(self): return self.conn
    def __exit__(self, *args: Any): return False
