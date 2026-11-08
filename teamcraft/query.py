"""M3-02 read-only Query Service (M0 review 9.3 M3-02, ADR-001 8.2-8).

This module is the *only* data source allowed behind the HTTP Query API:
routes never touch tables directly, and every read here runs on a strict
``mode=ro`` SQLite channel, so a query physically cannot mutate core state
(SQLite refuses DML on that channel), create a file when none exists,
submit a Command, dispatch a Task, or enqueue a Wake.

Contract pinned by this module (and ``tests/test_api_m302.py``):

* **Same source as the CLI.**  Data comes from the same ``.teamcraft``
  SQLite file the CLI/core writes through; ordering and identity mirror the
  core read helpers (``domain.list_tasks``, ``events.events_after``,
  ``wake.list_wakes``, ``verification.list_verifications``,
  ``runtime_adapter.team_view``).  Data visible to the CLI is visible here.
* **Read-only invariant.**  One fresh ``mode=ro`` connection per read
  (latest committed snapshot, no long-lived handle).  Writing on the
  service's own connection raises at SQLite's boundary; no code path here
  opens the writable facade.
* **Schema gate.**  A database whose ``user_version`` is not this build's
  head is refused with :class:`QueryUnavailableError` (mirrors
  ``domain.require_head`` / ``events._require_head``); a missing database is
  ``QueryUnavailableError`` too — never a silently "empty" lie.
* **Bounded everywhere.**  Lists paginate with ``limit``/``offset``
  (default ``DEFAULT_PAGE_SIZE``, hard cap ``MAX_PAGE_SIZE``) and report
  ``total``/``has_more``.  Filters are validated against the core's own
  state enumerations; anything unrecognized raises ``ValidationError`` so the
  API can fail uniformly.  Free text is clipped at byte caps with an
  explicit ``*_truncated`` flag; verification artifacts become bounded,
  digest-anchored previews (the durable artifact stays in the DB, untouched).
* **Layered distinctions preserved.**  Task, Attempt and Session states are
  returned as separate fields and never collapsed.  The three progress
  classes (process liveness / agent activity / task progress) stay distinct
  event kinds with an advisory ``progress_class`` tag.  Wake lifecycle keeps
  ``pending/sending/submitted/acknowledged/processed/unknown/blocked`` with
  its separate timestamps — ``submitted`` is never rendered as acknowledged.
* **Unknown usage stays unknown.**  Token usage fields are integers or
  ``null``; missing usage is all-``null`` with ``available: false`` and is
  never fabricated as zero (FR-018).
* **No path/secret leakage.**  Error messages carry no filesystem paths,
  exception text, or token material; the response layer additionally runs
  redaction over every text value.  Session identity JSON (Herdr socket
  paths, live-process fingerprints) is summarized to booleans only.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import storage
from .commands import COMMAND_STATES
from .domain import (
    ATTEMPT_STATUSES,
    SESSION_STATES,
    TASK_KINDS,
    TASK_STATUSES,
)
from .errors import NotFoundError, TeamCraftError, ValidationError
from .ids import require_entity_id

# ---------------------------------------------------------------------------
# Bounds (single source of truth for the Query surface)
# ---------------------------------------------------------------------------

#: Default page size when a caller omits ``limit``.
DEFAULT_PAGE_SIZE = 50
MIN_PAGE_SIZE = 1
#: Hard cap for ``limit`` on every list endpoint.
MAX_PAGE_SIZE = 200
#: Entity-ID filter values longer than this are treated as malformed.
MAX_ID_LENGTH = 512

#: Byte caps for human-readable free text surfaced in DTOs.
MAX_TITLE_BYTES = 512
MAX_TEXT_BYTES = 4096
MAX_REPORT_SUMMARY_BYTES = 2048
#: Byte cap for log/artifact previews (the durable bytes stay in the DB).
MAX_PREVIEW_BYTES = 8192

#: Wake lifecycle states (mirrors the wakes DDL CHECK constraint).
WAKE_STATES = frozenset(
    {
        "pending",
        "sending",
        "submitted",
        "acknowledged",
        "processed",
        "unknown",
        "blocked",
    }
)
#: Approval decisions (mirrors the approvals DDL CHECK constraint).
APPROVAL_DECISIONS = frozenset({"accept", "reject"})
#: Verification statuses (mirrors the verifications DDL CHECK constraint).
VERIFICATION_STATUSES = frozenset({"pass", "fail", "error", "stale"})
#: Recovery actions (mirrors recovery.request_action's accepted set).
RECOVERY_ACTIONS = frozenset({"wait", "cancel", "reconcile"})
RECOVERY_KIND_PREFIX = "recovery."

#: Canonical usage quantities.  Null means *unknown*, never zero (FR-018).
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cost_microunits")

#: Advisory mapping from watch/report event kinds to the three progress
#: classes (review 8.2-10 / ADR-001 8.7-5).  This tag is presentation-only:
#: it never advances or rewrites Task state.
EVENT_PROGRESS_CLASS = {
    "watch.session_observed": "process_liveness",
    "watch.agent_activity": "agent_activity",
    "watch.task_progress": "task_progress",
    "watch.no_progress_threshold": "task_progress",
    "watch.deadline_exceeded": "task_progress",
    "worker.report_recorded": "task_progress",
}

_EVENT_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")
_TARGET_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MEMBER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_RECOVERY_COLUMNS = (
    "id, idempotency_key, kind, payload_json, evidence_json, state,"
    " requested_at, updated_at, expires_at, result_json, note"
)
#: Same columns qualified for queries that JOIN tasks (resolves id ambiguity).
_RECOVERY_COLUMNS_Q = ", ".join(
    f"commands.{column.strip()}" for column in _RECOVERY_COLUMNS.split(",")
)


class QueryUnavailableError(TeamCraftError):
    """The core snapshot store cannot be served read-only right now.

    Covers: runtime database not initialized, schema version outside what
    this build can read, and low-level open/read failures.  The message is
    deliberately generic — it never carries paths or driver text.
    """


# ---------------------------------------------------------------------------
# Small internal helpers
# ---------------------------------------------------------------------------


def _entity_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_ID_LENGTH:
        raise ValidationError(f"{field} must be a non-empty string of at most {MAX_ID_LENGTH} characters")
    return require_entity_id(value)


def _optional_entity_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _entity_id(value, field)


def _choice(value: Any, allowed: frozenset[str], field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field} must be one of {sorted(allowed)}")
    return value


def _page(limit: int | None, offset: int | None) -> tuple[int, int]:
    resolved_limit = DEFAULT_PAGE_SIZE if limit is None else limit
    resolved_offset = 0 if offset is None else offset
    if isinstance(resolved_limit, bool) or not isinstance(resolved_limit, int) or not (
        MIN_PAGE_SIZE <= resolved_limit <= MAX_PAGE_SIZE
    ):
        raise ValidationError(
            f"limit must be an integer in {MIN_PAGE_SIZE}..{MAX_PAGE_SIZE}"
        )
    if isinstance(resolved_offset, bool) or not isinstance(resolved_offset, int) or resolved_offset < 0:
        raise ValidationError("offset must be a non-negative integer")
    return resolved_limit, resolved_offset


def _clip_bytes(value: str, limit: int) -> tuple[str, bool]:
    """Clip to ``limit`` UTF-8 bytes without splitting a codepoint."""

    data = value.encode("utf-8")
    if len(data) <= limit:
        return value, False
    return data[:limit].decode("utf-8", errors="ignore"), True


def _text_field(value: Any, limit: int, field: str) -> dict[str, Any]:
    text = "" if value is None else str(value)
    clipped, truncated = _clip_bytes(text, limit)
    return {f"{field}": clipped, f"{field}_truncated": truncated}


def _parse_json_container(raw: Any) -> Any:
    """Parse a stored JSON column into a container; anything else -> None."""

    if raw is None:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, (dict, list)) else None


def _usage_view(raw: Any) -> dict[str, Any]:
    """Usage passthrough where null means unknown and stays null (FR-018)."""

    source = raw if isinstance(raw, dict) else {}
    view: dict[str, Any] = {}
    for field in USAGE_FIELDS:
        amount = source.get(field)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            amount = None
        view[field] = amount
    view["available"] = any(view[field] is not None for field in USAGE_FIELDS)
    return view


def _page_payload(items: list[dict[str, Any]], limit: int, offset: int, total: int) -> dict[str, Any]:
    return {
        "items": items,
        "page": {
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": offset + len(items) < total,
        },
    }


# ---------------------------------------------------------------------------
# Row -> DTO mappers
# ---------------------------------------------------------------------------

_PROJECT_COLUMNS = "id, name, repo_path, config_json, created_at"


def _project_dto(row: Any) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "name": str(row[1]),
        "repo_path": str(row[2]),
        "config": _parse_json_container(row[3]) or {},
        "created_at": str(row[4]),
    }


_TASK_COLUMNS = (
    "id, project_id, title, description, acceptance_json, kind, priority,"
    " status, risk, ownership_json, max_attempts, version, created_at, updated_at"
)


def _task_dto(row: Any) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "id": str(row[0]),
        "project_id": str(row[1]),
        "created_at": str(row[12]),
        "updated_at": str(row[13]),
    }
    dto.update(_text_field(row[2], MAX_TITLE_BYTES, "title"))
    dto.update(_text_field(row[3], MAX_TEXT_BYTES, "description"))
    acceptance = _parse_json_container(row[4])
    dto["acceptance"] = acceptance if isinstance(acceptance, list) else []
    dto["kind"] = str(row[5])
    dto["priority"] = int(row[6])
    # Task status is its OWN column: live session signals and attempt
    # statuses never collapse into it (review 8.2-4).
    dto["status"] = str(row[7])
    dto["risk"] = str(row[8])
    ownership = _parse_json_container(row[9])
    dto["ownership"] = ownership if isinstance(ownership, list) else []
    dto["max_attempts"] = int(row[10])
    dto["version"] = int(row[11])
    return dto


_ATTEMPT_COLUMNS = (
    "id, task_id, agent_id, attempt_no, status, worktree_path, branch,"
    " started_at, finished_at, commit_sha, report_ref, result_json,"
    " protocol_version, protocol_hash"
)


def _attempt_summary_dto(row: Any) -> dict[str, Any]:
    attempt_no = int(row[3])
    return {
        "id": str(row[0]),
        "task_id": str(row[1]),
        "agent_id": None if row[2] is None else str(row[2]),
        "attempt_no": attempt_no,
        # Attempt status is separate from both Task status and the Session
        # live signal; UNKNOWN is a first-class, never auto-retried state.
        "status": str(row[4]),
        "worktree_path": None if row[5] is None else str(row[5]),
        "branch": None if row[6] is None else str(row[6]),
        "started_at": None if row[7] is None else str(row[7]),
        "finished_at": None if row[8] is None else str(row[8]),
        "commit_sha": None if row[9] is None else str(row[9]),
        "report_ref": None if row[10] is None else str(row[10]),
        "result": _parse_json_container(row[11]),
        "protocol_version": None if row[12] is None else str(row[12]),
        "protocol_hash": None if row[13] is None else str(row[13]),
    }


_SESSION_COLUMNS = (
    "id, attempt_id, backend, native_session_id, pane_id, state,"
    " last_observed_at, created_at"
)


def _session_dto(row: Any) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "attempt_id": None if row[1] is None else str(row[1]),
        "backend": str(row[2]),
        "native_session_id": None if row[3] is None else str(row[3]),
        "pane_id": None if row[4] is None else str(row[4]),
        # Live signal only: idle/working/exited/missing/unknown never
        # imply Task completion or Attempt finish (review 8.2-4).
        "state": str(row[5]),
        "last_observed_at": None if row[6] is None else str(row[6]),
        "created_at": str(row[7]),
    }


_EVENT_COLUMNS = (
    "seq, event_id, kind, project_id, task_id, attempt_id, payload_json,"
    " occurred_at"
)


def _event_dto(row: Any) -> dict[str, Any]:
    kind = str(row[2])
    return {
        "seq": int(row[0]),
        "event_id": str(row[1]),
        "kind": kind,
        "project_id": None if row[3] is None else str(row[3]),
        "task_id": None if row[4] is None else str(row[4]),
        "attempt_id": None if row[5] is None else str(row[5]),
        # Payloads are bounded to 64 KiB by the event store itself.
        "payload": _parse_json_container(row[6]),
        "occurred_at": str(row[7]),
        # Advisory only: which progress class this signal belongs to
        # (process liveness / agent activity / task progress); None for
        # control-plane kinds.  It never rewrites Task status.
        "progress_class": EVENT_PROGRESS_CLASS.get(kind),
    }


_WAKE_COLUMNS = (
    "id, project_id, target_ceo_name, target_session_id, dedupe_key,"
    " window_start, priority, state, first_seq, last_seq, event_count,"
    " event_digest, command_id, detail, created_at, updated_at,"
    " submitted_at, acknowledged_at, processed_at"
)


def _wake_dto(row: Any) -> dict[str, Any]:
    # Every lifecycle timestamp stays separate: submitted (Herdr accepted
    # delivery) is NOT acknowledged (explicit CEO receipt) and NOT processed
    # (decision + cursor persisted) — review ADR-001 8.7-6.
    return {
        "id": str(row[0]),
        "project_id": str(row[1]),
        "target_ceo_name": str(row[2]),
        "target_session_id": None if row[3] is None else str(row[3]),
        "dedupe_key": str(row[4]),
        "window_start": str(row[5]),
        "priority": int(row[6]),
        "state": str(row[7]),
        "first_seq": int(row[8]),
        "last_seq": int(row[9]),
        "event_count": int(row[10]),
        "event_digest": str(row[11]),
        "command_id": None if row[12] is None else str(row[12]),
        "detail": _text_field(row[13], MAX_TEXT_BYTES, "detail"),
        "created_at": str(row[14]),
        "updated_at": str(row[15]),
        "submitted_at": None if row[16] is None else str(row[16]),
        "acknowledged_at": None if row[17] is None else str(row[17]),
        "processed_at": None if row[18] is None else str(row[18]),
    }


_WAKE_CURSOR_COLUMNS = (
    "project_id, target_ceo_name, target_session_id, scanned_seq,"
    " acknowledged_seq, processed_seq, created_at, updated_at"
)


def _wake_cursor_dto(row: Any) -> dict[str, Any]:
    return {
        "project_id": str(row[0]),
        "target_ceo_name": str(row[1]),
        "target_session_id": None if row[2] is None else str(row[2]),
        "scanned_seq": int(row[3]),
        "acknowledged_seq": int(row[4]),
        "processed_seq": int(row[5]),
        "created_at": str(row[6]),
        "updated_at": str(row[7]),
    }


_APPROVAL_COLUMNS = (
    "id, command_id, verification_id, project_id, task_id, attempt_id,"
    " attempt_no, task_version, brief_digest, artifact_digest, config_version,"
    " rules_version, config_digest, proposed_action, decision, actor, reason,"
    " created_at"
)


def _approval_dto(row: Any) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "id": str(row[0]),
        "command_id": str(row[1]),
        "verification_id": str(row[2]),
        "project_id": str(row[3]),
        "task_id": str(row[4]),
        "attempt_id": str(row[5]),
        "attempt_no": int(row[6]),
        "task_version": int(row[7]),
        "brief_digest": str(row[8]),
        "artifact_digest": str(row[9]),
        "config_version": int(row[10]),
        "rules_version": str(row[11]),
        "config_digest": str(row[12]),
        "proposed_action": str(row[13]),
        # The durable decision only. Whether it is still *current* is
        # derived from evidence versions by the command layer; the query
        # surface never re-interprets it.
        "decision": str(row[14]),
        "actor": str(row[15]),
        "created_at": str(row[17]),
    }
    dto.update(_text_field(row[16], MAX_TEXT_BYTES, "reason"))
    return dto


_VERIFICATION_COLUMNS = (
    "id, command_id, project_id, task_id, attempt_id, attempt_no, task_version,"
    " dispatch_task_version, brief_digest, worker_report_id, worker_report_digest,"
    " config_version, rules_version, config_digest, argv_json, status, exit_code,"
    " artifact, artifact_digest, created_at"
)


def _artifact_preview(raw: Any, digest: str) -> dict[str, Any]:
    data = bytes(raw) if isinstance(raw, (bytes, bytearray)) else b""
    preview_text: str | None
    binary = False
    try:
        decoded = data.decode("utf-8")
        preview_text, truncated = _clip_bytes(decoded, MAX_PREVIEW_BYTES)
    except UnicodeDecodeError:
        preview_text, truncated, binary = None, len(data) > MAX_PREVIEW_BYTES, True
    return {
        "size_bytes": len(data),
        "digest": digest,
        "preview": preview_text,
        "preview_truncated": truncated,
        "binary": binary,
    }


def _verification_dto(row: Any) -> dict[str, Any]:
    argv = _parse_json_container(row[14])
    return {
        "id": str(row[0]),
        "command_id": str(row[1]),
        "project_id": str(row[2]),
        "task_id": str(row[3]),
        "attempt_id": str(row[4]),
        "attempt_no": int(row[5]),
        "task_version": int(row[6]),
        "dispatch_task_version": int(row[7]),
        "brief_digest": str(row[8]),
        "worker_report_id": int(row[9]),
        "worker_report_digest": str(row[10]),
        "config_version": int(row[11]),
        "rules_version": str(row[12]),
        "config_digest": str(row[13]),
        "argv": argv if isinstance(argv, list) else [],
        "status": str(row[15]),
        "exit_code": None if row[16] is None else int(row[16]),
        "artifact": _artifact_preview(row[17], str(row[18])),
        "created_at": str(row[19]),
    }


_VERIFICATION_CONFIG_COLUMNS = (
    "id, command_id, project_id, config_version, rules_version, argv_json,"
    " timeout_seconds, config_digest, approved_by, created_at"
)


def _verification_config_dto(row: Any) -> dict[str, Any]:
    argv = _parse_json_container(row[5])
    return {
        "id": str(row[0]),
        "command_id": str(row[1]),
        "project_id": str(row[2]),
        "config_version": int(row[3]),
        "rules_version": str(row[4]),
        "argv": argv if isinstance(argv, list) else [],
        "timeout_seconds": int(row[6]),
        "config_digest": str(row[7]),
        "approved_by": str(row[8]),
        "created_at": str(row[9]),
    }


_BRIEF_COLUMNS = (
    "attempt_id, task_id, task_version, brief_version, brief_digest,"
    " deadline_at, no_progress_timeout_seconds, poll_interval_seconds, content"
)


def _brief_dto(row: Any) -> dict[str, Any]:
    content = str(row[8])
    preview, truncated = _clip_bytes(content, MAX_PREVIEW_BYTES)
    return {
        "attempt_id": str(row[0]),
        "task_id": str(row[1]),
        "task_version": int(row[2]),
        "brief_version": int(row[3]),
        "brief_digest": str(row[4]),
        "deadline_at": None if row[5] is None else str(row[5]),
        "no_progress_timeout_seconds": (
            None if row[6] is None else int(row[6])
        ),
        "poll_interval_seconds": None if row[7] is None else int(row[7]),
        "content_preview": preview,
        "content_truncated": truncated,
    }


_WORKER_REPORT_COLUMNS = (
    "id, attempt_id, task_id, task_version, brief_digest, report_kind,"
    " outcome, milestone_id, report_digest, artifact_digest, report_json,"
    " recorded_at"
)


def _worker_report_dto(row: Any) -> dict[str, Any]:
    report = _parse_json_container(row[10])
    usage = report.get("usage") if isinstance(report, dict) else None
    dto: dict[str, Any] = {
        "id": int(row[0]),
        "attempt_id": str(row[1]),
        "task_id": str(row[2]),
        "task_version": int(row[3]),
        "brief_digest": str(row[4]),
        "report_kind": str(row[5]),
        # outcome is null for progress reports — that is honest "not yet
        # concluded", not a fabricated value.
        "outcome": None if row[6] is None else str(row[6]),
        "milestone_id": str(row[7]),
        "report_digest": str(row[8]),
        # Null artifact_digest means "no artifact declared", never empty hash.
        "artifact_digest": None if row[9] is None else str(row[9]),
        "recorded_at": str(row[11]),
        # Task-progress evidence (one of the three progress classes).
        "progress_class": "task_progress",
        "usage": _usage_view(usage),
    }
    summary = report.get("summary") if isinstance(report, dict) else None
    dto.update(_text_field(summary, MAX_REPORT_SUMMARY_BYTES, "summary"))
    return dto


_PROMPT_DELIVERY_COLUMNS = (
    "command_id, attempt_id, session_id, task_version, brief_version,"
    " brief_digest, evidence_version, state, updated_at, note"
)


def _prompt_delivery_dto(row: Any) -> dict[str, Any]:
    return {
        "command_id": str(row[0]),
        "attempt_id": str(row[1]),
        "session_id": None if row[2] is None else str(row[2]),
        "task_version": int(row[3]),
        "brief_version": int(row[4]),
        "brief_digest": str(row[5]),
        "evidence_version": int(row[6]),
        # Delivery stages stay separate: pending/sending/submitted/
        # acknowledged/unknown (submitted != acknowledged, review 8.7-4).
        "state": str(row[7]),
        "updated_at": str(row[8]),
        "note": _text_field(row[9], MAX_TEXT_BYTES, "note"),
    }


_AGENT_COLUMNS = "id, role, harness, model, provider, config_json"


def _agent_dto(row: Any) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "role": str(row[1]),
        "harness": str(row[2]),
        "model": None if row[3] is None else str(row[3]),
        "provider": None if row[4] is None else str(row[4]),
        "config": _parse_json_container(row[5]) or {},
    }


_MEMBER_COLUMNS = (
    "name, role, responsibility, kind, task, ownership_json, read_only, cwd,"
    " from_pane_id, pane_id, terminal_id, agent_session_json, agent_args_json,"
    " opencode_config_content, direction, start_timeout_ms, phase,"
    " prompt_may_have_been_delivered, live_state, error, task_id, attempt_id,"
    " session_id, created_at, updated_at"
)


def _member_dto(row: Any) -> dict[str, Any]:
    """Runtime member view matching ``runtime_adapter``'s CLI-compat shape.

    Text fields are additionally bounded; ids/phase/live_state semantics are
    passed through unchanged so the API and ``teamctl status`` agree.
    """

    ownership = _parse_json_container(row[5])
    dto: dict[str, Any] = {
        "name": str(row[0]),
        "task_id": None if row[20] is None else str(row[20]),
        "attempt_id": None if row[21] is None else str(row[21]),
        "session_id": None if row[22] is None else str(row[22]),
        "role": str(row[1]),
        "kind": str(row[3]),
        "ownership": ownership if isinstance(ownership, list) else [],
        "read_only": bool(row[6]),
        "cwd": str(row[7]),
        "from_pane_id": None if row[8] is None else str(row[8]),
        "pane_id": None if row[9] is None else str(row[9]),
        "terminal_id": None if row[10] is None else str(row[10]),
        "agent_session": _parse_json_container(row[11]),
        "direction": None if row[14] is None else str(row[14]),
        "start_timeout_ms": int(row[15]),
        # Launch phase is an Attempt/Session compatibility field, never a
        # Task status (compat matrix, state layering row).
        "phase": str(row[16]),
        "prompt_may_have_been_delivered": bool(row[17]),
        # Live signal from Herdr, kept apart from phase and task status.
        "live_state": None if row[18] is None else str(row[18]),
        "created_at": int(row[23]),
        "updated_at": int(row[24]),
    }
    dto.update(_text_field(row[2], MAX_TEXT_BYTES, "responsibility"))
    dto.update(_text_field(row[4], MAX_TEXT_BYTES, "task"))
    dto.update(_text_field(row[19], MAX_TEXT_BYTES, "error"))
    return dto


_CEO_INIT_COLUMNS = (
    "project_id, canonical_ceo_name, state, team_id, teamcraft_session_id,"
    " native_session_id, terminal_id, protocol_version, protocol_hash,"
    " runtime_skill_hash, snapshot_digest, nonce_expires_at, nonce_consumed_at,"
    " detail, receipt_source, identity_mode, created_at, updated_at"
)


def _ceo_initialization_dto(row: Any) -> dict[str, Any]:
    """CEO initialization record without sensitive identity payloads.

    ``herdr_session_identity_json`` (socket path/device/inode) and
    ``live_process_identity_json`` (process fingerprint) are summarized to
    booleans; the nonce *hash* and raw audit material stay server-side.
    ``receipt_source`` is surfaced verbatim so a client can never mistake
    ``assumed_default`` for a trusted host receipt (review 12.17).
    """

    return {
        "project_id": str(row[0]),
        "canonical_ceo_name": str(row[1]),
        # Skill discovered / protocol read / initialized / failed /
        # incompatible / unknown are distinct — never implied from a prompt.
        "state": str(row[2]),
        "team_id": str(row[3]),
        "teamcraft_session_id": None if row[4] is None else str(row[4]),
        "native_session_id": None if row[5] is None else str(row[5]),
        "terminal_id": None if row[6] is None else str(row[6]),
        "protocol_version": None if row[7] is None else str(row[7]),
        "protocol_hash": None if row[8] is None else str(row[8]),
        "runtime_skill_hash": None if row[9] is None else str(row[9]),
        "snapshot_digest": None if row[10] is None else str(row[10]),
        "nonce_expires_at": None if row[11] is None else str(row[11]),
        "nonce_consumed": row[12] is not None,
        "detail": _text_field(row[13], MAX_TEXT_BYTES, "detail"),
        "receipt_source": str(row[14]),
        "identity_mode": str(row[15]),
        "has_herdr_session_identity": False,  # replaced below via extra column
        "has_live_process_identity": False,
        "created_at": str(row[16]),
        "updated_at": str(row[17]),
    }


_RECOVERY_SELECT = (
    "SELECT " + _RECOVERY_COLUMNS + " FROM commands"
    " WHERE kind LIKE ? AND json_valid(payload_json)"
)


def _recovery_action_dto(row: Any) -> dict[str, Any]:
    payload = _parse_json_container(row[3])
    payload = payload if isinstance(payload, dict) else {}
    evidence = _parse_json_container(row[4])
    evidence = evidence if isinstance(evidence, dict) else {}
    dto: dict[str, Any] = {
        "command_id": str(row[0]),
        "kind": str(row[2]),
        "action": str(row[2])[len(RECOVERY_KIND_PREFIX):],
        "state": str(row[5]),
        "task_id": payload.get("task_id"),
        "attempt_id": payload.get("attempt_id"),
        "attempt_no": payload.get("attempt_no"),
        "task_version": payload.get("task_version"),
        "actor": payload.get("actor"),
        "requested_at": str(row[6]),
        "updated_at": str(row[7]),
        "expires_at": None if row[8] is None else str(row[8]),
        "evidence": evidence,
        # Recovery result only exists once a command reached a settled
        # state; null is honest "not settled yet" (unknown stays unknown).
        "result": _parse_json_container(row[9]),
    }
    dto.update(_text_field(payload.get("reason"), MAX_TEXT_BYTES, "reason"))
    dto.update(_text_field(row[10], MAX_TEXT_BYTES, "note"))
    return dto


# ---------------------------------------------------------------------------
# The Query Service
# ---------------------------------------------------------------------------


class QueryService:
    """Read-only snapshot queries over the TeamCraft core database."""

    def __init__(self, database_path: str | Path) -> None:
        text = str(database_path).strip() if database_path is not None else ""
        if not text:
            raise ValidationError("database_path must be a non-empty path")
        self._db_path = Path(text)

    @classmethod
    def for_project_root(cls, project_root: str | Path) -> QueryService:
        """Service over ``<root>/.teamcraft/teamcraft.db`` (not created)."""

        from .fresh_start import DATABASE_FILENAME, RUNTIME_DIRNAME

        return cls(Path(project_root) / RUNTIME_DIRNAME / DATABASE_FILENAME)

    @property
    def database_path(self) -> Path:
        return self._db_path

    # -- read channel ---------------------------------------------------------

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One fresh strict read-only connection, schema-gated.

        ``mode=ro`` physically refuses DDL/DML and never creates a missing
        file; the version gate mirrors the core's ``require_head`` so the
        API never serves a schema it does not understand.
        """

        if not self._db_path.is_file():
            raise QueryUnavailableError(
                "the TeamCraft runtime database is not initialized"
            )
        uri = self._db_path.as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        except sqlite3.Error as exc:
            raise QueryUnavailableError(
                "the TeamCraft runtime database is not readable"
            ) from exc
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != storage.SCHEMA_VERSION:
                raise QueryUnavailableError(
                    f"core schema v{version} is not readable by this build "
                    f"(head v{storage.SCHEMA_VERSION})"
                )
            yield conn
        except QueryUnavailableError:
            raise
        except sqlite3.Error as exc:
            raise QueryUnavailableError(
                "the TeamCraft runtime database read failed"
            ) from exc
        finally:
            conn.close()

    # -- shared internal queries ----------------------------------------------

    def _require_project(self, conn: sqlite3.Connection, project_id: str) -> Any:
        row = conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?",
            (_entity_id(project_id, "project_id"),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"project not found: {project_id}")
        return row

    def _require_task(self, conn: sqlite3.Connection, task_id: str) -> Any:
        row = conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?",
            (_entity_id(task_id, "task_id"),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"task not found: {task_id}")
        return row

    # -- Projects -------------------------------------------------------------

    def list_projects(self, *, limit: int | None = None, offset: int | None = None) -> dict[str, Any]:
        page_limit, page_offset = _page(limit, offset)
        with self._read() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
            rows = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM projects"
                " ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                (page_limit, page_offset),
            ).fetchall()
        return _page_payload([_project_dto(row) for row in rows], page_limit, page_offset, total)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._read() as conn:
            return _project_dto(self._require_project(conn, project_id))

    def project_snapshot(self, project_id: str) -> dict[str, Any]:
        """One consistent read snapshot of a project's layered state."""

        with self._read() as conn:
            project = _project_dto(self._require_project(conn, project_id))

            def group_by_status(sql: str, params: tuple[Any, ...], canonical: tuple[str, ...]) -> dict[str, int]:
                counts = {status: 0 for status in canonical}
                for value, count in conn.execute(sql, params).fetchall():
                    counts[str(value)] = int(count)
                return counts

            tasks_by_status = group_by_status(
                "SELECT status, COUNT(*) FROM tasks WHERE project_id = ?"
                " GROUP BY status",
                (project["id"],),
                TASK_STATUSES,
            )
            attempts_by_status = group_by_status(
                "SELECT a.status, COUNT(*) FROM attempts a"
                " JOIN tasks t ON t.id = a.task_id WHERE t.project_id = ?"
                " GROUP BY a.status",
                (project["id"],),
                ATTEMPT_STATUSES,
            )
            sessions_by_state = group_by_status(
                "SELECT s.state, COUNT(*) FROM sessions s"
                " JOIN attempts a ON a.id = s.attempt_id"
                " JOIN tasks t ON t.id = a.task_id WHERE t.project_id = ?"
                " GROUP BY s.state",
                (project["id"],),
                SESSION_STATES,
            )
            wakes_by_state = group_by_status(
                "SELECT state, COUNT(*) FROM wakes WHERE project_id = ?"
                " GROUP BY state",
                (project["id"],),
                tuple(sorted(WAKE_STATES)),
            )
            event_seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events WHERE project_id = ?",
                    (project["id"],),
                ).fetchone()[0]
            )
            cursor_row = conn.execute(
                f"SELECT {_WAKE_CURSOR_COLUMNS} FROM wake_cursors"
                " WHERE project_id = ? AND target_ceo_name = 'ceo'",
                (project["id"],),
            ).fetchone()
            init_row = conn.execute(
                "SELECT "
                + _CEO_INIT_COLUMNS
                + ", herdr_session_identity_json, live_process_identity_json"
                " FROM ceo_initializations WHERE project_id = ?",
                (project["id"],),
            ).fetchone()
        ceo_state: str | None = None
        if init_row is not None:
            ceo_state = str(init_row[2])
        return {
            "project": project,
            "counts": {
                "tasks_by_status": tasks_by_status,
                "attempts_by_status": attempts_by_status,
                "sessions_by_state": sessions_by_state,
                "wakes_by_state": wakes_by_state,
            },
            "event_seq": event_seq,
            "wake_cursor": (
                _wake_cursor_dto(cursor_row) if cursor_row is not None else None
            ),
            "ceo_initialization_state": ceo_state,
        }

    # -- Tasks ---------------------------------------------------------------

    def list_tasks(
        self,
        project_id: str,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        status = _choice(status, frozenset(TASK_STATUSES), "status")
        kind = _choice(kind, frozenset(TASK_KINDS), "kind")
        page_limit, page_offset = _page(limit, offset)
        clauses = ["project_id = ?"]
        params: list[Any] = [_entity_id(project_id, "project_id")]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        with self._read() as conn:
            self._require_project(conn, project_id)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", tuple(params)).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE {where}"
                # Same ordering authority as domain.list_tasks.
                " ORDER BY priority DESC, created_at ASC, id ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload([_task_dto(row) for row in rows], page_limit, page_offset, total)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = self._require_task(conn, task_id)
            depends_on, required_by = self._dependency_edges(conn, str(row[0]))
        dto = _task_dto(row)
        dto["depends_on"] = depends_on
        dto["required_by"] = required_by
        return dto

    def list_dependencies(self, task_id: str) -> dict[str, Any]:
        with self._read() as conn:
            self._require_task(conn, task_id)
            depends_on, required_by = self._dependency_edges(conn, str(task_id))
        return {"task_id": str(task_id), "depends_on": depends_on, "required_by": required_by}

    @staticmethod
    def _dependency_edges(conn: sqlite3.Connection, task_id: str) -> tuple[list[str], list[str]]:
        depends_on = [
            str(row[0])
            for row in conn.execute(
                "SELECT depends_on_id FROM task_dependencies WHERE task_id = ?"
                " ORDER BY depends_on_id",
                (task_id,),
            ).fetchall()
        ]
        required_by = [
            str(row[0])
            for row in conn.execute(
                "SELECT task_id FROM task_dependencies WHERE depends_on_id = ?"
                " ORDER BY task_id",
                (task_id,),
            ).fetchall()
        ]
        return depends_on, required_by

    # -- Attempts --------------------------------------------------------------

    def list_attempts(
        self,
        task_id: str,
        *,
        status: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        status = _choice(status, frozenset(ATTEMPT_STATUSES), "status")
        page_limit, page_offset = _page(limit, offset)
        clauses = ["task_id = ?"]
        params: list[Any] = [_entity_id(task_id, "task_id")]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        with self._read() as conn:
            self._require_task(conn, task_id)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM attempts WHERE {where}", tuple(params)).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE {where}"
                " ORDER BY attempt_no ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload(
            [_attempt_summary_dto(row) for row in rows], page_limit, page_offset, total
        )

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        attempt_id = _entity_id(attempt_id, "attempt_id")
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"attempt not found: {attempt_id}")
            brief_row = conn.execute(
                f"SELECT {_BRIEF_COLUMNS} FROM attempt_briefs WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            delivery_row = conn.execute(
                f"SELECT {_PROMPT_DELIVERY_COLUMNS} FROM prompt_deliveries"
                " WHERE attempt_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            session_rows = conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE attempt_id = ?"
                " ORDER BY created_at ASC, id ASC",
                (attempt_id,),
            ).fetchall()
            report_rows = conn.execute(
                f"SELECT {_WORKER_REPORT_COLUMNS} FROM worker_reports"
                " WHERE attempt_id = ? ORDER BY id ASC",
                (attempt_id,),
            ).fetchall()
        dto = _attempt_summary_dto(row)
        # Layered view: Task status (via task_id), Attempt status (here),
        # and Session live signals (sessions[]) stay three separate facts.
        dto["brief"] = _brief_dto(brief_row) if brief_row is not None else None
        dto["prompt_delivery"] = (
            _prompt_delivery_dto(delivery_row) if delivery_row is not None else None
        )
        dto["sessions"] = [_session_dto(item) for item in session_rows]
        dto["worker_reports"] = [_worker_report_dto(item) for item in report_rows]
        return dto

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?",
                (_entity_id(session_id, "session_id"),),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"session not found: {session_id}")
        return _session_dto(row)

    # -- Agents and runtime members -------------------------------------------

    def list_agents(self, *, limit: int | None = None, offset: int | None = None) -> dict[str, Any]:
        page_limit, page_offset = _page(limit, offset)
        with self._read() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0])
            rows = conn.execute(
                f"SELECT {_AGENT_COLUMNS} FROM agents ORDER BY rowid ASC LIMIT ? OFFSET ?",
                (page_limit, page_offset),
            ).fetchall()
        return _page_payload([_agent_dto(row) for row in rows], page_limit, page_offset, total)

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_AGENT_COLUMNS} FROM agents WHERE id = ?",
                (_entity_id(agent_id, "agent_id"),),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"agent not found: {agent_id}")
        return _agent_dto(row)

    def list_members(self, *, limit: int | None = None, offset: int | None = None) -> dict[str, Any]:
        page_limit, page_offset = _page(limit, offset)
        with self._read() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM members").fetchone()[0])
            rows = conn.execute(
                f"SELECT {_MEMBER_COLUMNS} FROM members"
                # Same ordering as runtime_adapter.team_view (CLI status).
                " ORDER BY created_at ASC, name ASC LIMIT ? OFFSET ?",
                (page_limit, page_offset),
            ).fetchall()
        return _page_payload([_member_dto(row) for row in rows], page_limit, page_offset, total)

    def get_member(self, member_name: str) -> dict[str, Any]:
        if not isinstance(member_name, str) or not _MEMBER_NAME_RE.fullmatch(member_name):
            raise ValidationError("member name must be a safe identifier")
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_MEMBER_COLUMNS} FROM members WHERE name = ?",
                (member_name,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"member not found: {member_name}")
        return _member_dto(row)

    # -- CEO initialization / protocol -----------------------------------------

    def get_ceo_view(self, project_id: str) -> dict[str, Any]:
        """Initialization record + protocol facts for a project (nullable)."""

        with self._read() as conn:
            project = self._require_project(conn, project_id)
            init_row = conn.execute(
                "SELECT "
                + _CEO_INIT_COLUMNS
                + ", herdr_session_identity_json, live_process_identity_json"
                " FROM ceo_initializations WHERE project_id = ?",
                (str(project[0]),),
            ).fetchone()
            repo_path = str(project[2])
        initialization = None
        if init_row is not None:
            initialization = _ceo_initialization_dto(init_row)
            initialization["has_herdr_session_identity"] = (
                init_row[18] is not None
            )
            initialization["has_live_process_identity"] = init_row[19] is not None
        return {
            "project_id": str(project[0]),
            "initialization": initialization,
            "protocol": self._protocol_summary(repo_path),
        }

    @staticmethod
    def _protocol_summary(repo_path: str) -> dict[str, Any]:
        """Best-effort protocol view via the shared M1-08 discovery path.

        File-level failures (missing/malformed/pinned mismatch) degrade to
        ``available: false`` per harness; error text (which contains paths)
        is never surfaced.
        """

        from .ceo_protocol import ProtocolError, discover_protocol

        harnesses: dict[str, str] = {}
        version: str | None = None
        digest: str | None = None
        for harness in ("codex", "opencode"):
            try:
                snapshot = discover_protocol(repo_path, harness)
            except ProtocolError:
                harnesses[harness] = "missing"
                continue
            except OSError:  # pragma: no cover - defensive fs failure
                harnesses[harness] = "error"
                continue
            harnesses[harness] = "available"
            version, digest = snapshot.version, snapshot.sha256
        return {
            "available": version is not None,
            "protocol_version": version,
            "sha256": digest,
            "harnesses": harnesses,
        }

    # -- Events ---------------------------------------------------------------

    def list_events(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        kind: str | None = None,
        after_seq: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        project_id = _optional_entity_id(project_id, "project_id")
        task_id = _optional_entity_id(task_id, "task_id")
        attempt_id = _optional_entity_id(attempt_id, "attempt_id")
        if (
            kind is not None
            and (not isinstance(kind, str) or not _EVENT_KIND_RE.match(kind))
        ):
            raise ValidationError("kind must match [a-z0-9][a-z0-9_.-]*")
        if after_seq is None:
            after_seq = 0
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ValidationError("after_seq must be a non-negative integer")
        page_limit, page_offset = _page(limit, offset)
        clauses = ["seq > ?"]
        params: list[Any] = [after_seq]
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if attempt_id is not None:
            clauses.append("attempt_id = ?")
            params.append(attempt_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        with self._read() as conn:
            if project_id is not None:
                self._require_project(conn, project_id)
            if task_id is not None:
                self._require_task(conn, task_id)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}", tuple(params)).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM events WHERE {where}"
                # Durable seq is the ordering authority (review 9.1 M3-03).
                " ORDER BY seq ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload([_event_dto(row) for row in rows], page_limit, page_offset, total)

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM events WHERE event_id = ?",
                (_entity_id(event_id, "event_id"),),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"event not found: {event_id}")
        return _event_dto(row)

    # -- Wakes -----------------------------------------------------------------

    def list_wakes(
        self,
        project_id: str,
        *,
        target: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        project_id = _entity_id(project_id, "project_id")
        target = "ceo" if target is None else target
        if not isinstance(target, str) or not _TARGET_NAME_RE.fullmatch(target):
            raise ValidationError("target must be a normalized TeamCraft member name")
        state = _choice(state, WAKE_STATES, "state")
        page_limit, page_offset = _page(limit, offset)
        clauses = ["project_id = ?", "target_ceo_name = ?"]
        params: list[Any] = [project_id, target]
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        where = " AND ".join(clauses)
        with self._read() as conn:
            self._require_project(conn, project_id)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM wakes WHERE {where}", tuple(params)).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE {where}"
                # Same queue order as wake.list_wakes.
                " ORDER BY priority DESC, first_seq ASC, id ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload([_wake_dto(row) for row in rows], page_limit, page_offset, total)

    def get_wake(self, wake_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_WAKE_COLUMNS} FROM wakes WHERE id = ?",
                (_entity_id(wake_id, "wake_id"),),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"wake not found: {wake_id}")
        return _wake_dto(row)

    def list_wake_events(self, wake_id: str) -> dict[str, Any]:
        wake_id = _entity_id(wake_id, "wake_id")
        with self._read() as conn:
            if conn.execute("SELECT 1 FROM wakes WHERE id = ?", (wake_id,)).fetchone() is None:
                raise NotFoundError(f"wake not found: {wake_id}")
            seqs = [
                int(row[0])
                for row in conn.execute(
                    "SELECT event_seq FROM wake_events WHERE wake_id = ?"
                    " ORDER BY event_seq ASC",
                    (wake_id,),
                ).fetchall()
            ]
        return {"wake_id": wake_id, "event_seqs": seqs}

    def get_wake_cursor(self, project_id: str, *, target: str | None = None) -> dict[str, Any]:
        project_id = _entity_id(project_id, "project_id")
        target = "ceo" if target is None else target
        if not isinstance(target, str) or not _TARGET_NAME_RE.fullmatch(target):
            raise ValidationError("target must be a normalized TeamCraft member name")
        with self._read() as conn:
            self._require_project(conn, project_id)
            row = conn.execute(
                f"SELECT {_WAKE_CURSOR_COLUMNS} FROM wake_cursors"
                " WHERE project_id = ? AND target_ceo_name = ?",
                (project_id, target),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"wake cursor not found for project {project_id} target {target}"
            )
        return _wake_cursor_dto(row)

    # -- Approvals ---------------------------------------------------------------

    def list_approvals(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        decision: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        project_id = _optional_entity_id(project_id, "project_id")
        task_id = _optional_entity_id(task_id, "task_id")
        decision = _choice(decision, APPROVAL_DECISIONS, "decision")
        page_limit, page_offset = _page(limit, offset)
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if decision is not None:
            clauses.append("decision = ?")
            params.append(decision)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._read() as conn:
            if task_id is not None:
                self._require_task(conn, task_id)
            elif project_id is not None:
                self._require_project(conn, project_id)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM approvals{where}", tuple(params)).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals{where}"
                " ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload(
            [_approval_dto(row) for row in rows], page_limit, page_offset, total
        )

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE id = ?",
                (_entity_id(approval_id, "approval_id"),),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"approval not found: {approval_id}")
        return _approval_dto(row)

    # -- Recovery records ---------------------------------------------------------

    def list_recovery_actions(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        action: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        project_id = _optional_entity_id(project_id, "project_id")
        task_id = _optional_entity_id(task_id, "task_id")
        action = _choice(action, RECOVERY_ACTIONS, "action")
        state = _choice(state, frozenset(COMMAND_STATES), "state")
        page_limit, page_offset = _page(limit, offset)
        # Columns stay commands.-qualified so the optional tasks JOIN
        # (needed for the project filter) cannot introduce ambiguity.
        clauses = ["commands.kind LIKE ?", "json_valid(commands.payload_json)"]
        params: list[Any] = [RECOVERY_KIND_PREFIX + "%"]
        if action is not None:
            clauses.append("commands.kind = ?")
            params.append(RECOVERY_KIND_PREFIX + action)
        if state is not None:
            clauses.append("commands.state = ?")
            params.append(state)
        join = ""
        if task_id is not None:
            clauses.append("json_extract(commands.payload_json, '$.task_id') = ?")
            params.append(task_id)
        if project_id is not None:
            join = " JOIN tasks t ON t.id = json_extract(commands.payload_json, '$.task_id')"
            clauses.append("t.project_id = ?")
            params.append(project_id)
        where = " AND ".join(clauses)
        with self._read() as conn:
            if task_id is not None:
                self._require_task(conn, task_id)
            elif project_id is not None:
                self._require_project(conn, project_id)
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM commands{join} WHERE {where}", tuple(params)
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_RECOVERY_COLUMNS_Q} FROM commands{join} WHERE {where}"
                " ORDER BY commands.requested_at ASC, commands.id ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload(
            [_recovery_action_dto(row) for row in rows], page_limit, page_offset, total
        )

    def get_recovery_action(self, command_id: str) -> dict[str, Any]:
        command_id = _entity_id(command_id, "command_id")
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_RECOVERY_COLUMNS} FROM commands WHERE id = ?",
                (command_id,),
            ).fetchone()
        if row is None or not str(row[2]).startswith(RECOVERY_KIND_PREFIX):
            raise NotFoundError(f"recovery action not found: {command_id}")
        return _recovery_action_dto(row)

    # -- Verifications -----------------------------------------------------------

    def list_verifications(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        project_id = _optional_entity_id(project_id, "project_id")
        task_id = _optional_entity_id(task_id, "task_id")
        attempt_id = _optional_entity_id(attempt_id, "attempt_id")
        status = _choice(status, VERIFICATION_STATUSES, "status")
        page_limit, page_offset = _page(limit, offset)
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("task_id", task_id),
            ("attempt_id", attempt_id),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._read() as conn:
            if task_id is not None:
                self._require_task(conn, task_id)
            elif project_id is not None:
                self._require_project(conn, project_id)
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM verifications{where}", tuple(params)
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_VERIFICATION_COLUMNS} FROM verifications{where}"
                " ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload(
            [_verification_dto(row) for row in rows], page_limit, page_offset, total
        )

    def get_verification(self, verification_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_VERIFICATION_COLUMNS} FROM verifications WHERE id = ?",
                (_entity_id(verification_id, "verification_id"),),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"verification not found: {verification_id}")
        return _verification_dto(row)

    def list_verification_configs(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        project_id = _optional_entity_id(project_id, "project_id")
        page_limit, page_offset = _page(limit, offset)
        clauses = ""
        params: list[Any] = []
        if project_id is not None:
            clauses = " WHERE project_id = ?"
            params.append(project_id)
        with self._read() as conn:
            if project_id is not None:
                self._require_project(conn, project_id)
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM verification_configs{clauses}", tuple(params)
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT {_VERIFICATION_CONFIG_COLUMNS} FROM verification_configs{clauses}"
                " ORDER BY config_version DESC, id ASC LIMIT ? OFFSET ?",
                (*params, page_limit, page_offset),
            ).fetchall()
        return _page_payload(
            [_verification_config_dto(row) for row in rows],
            page_limit,
            page_offset,
            total,
        )
