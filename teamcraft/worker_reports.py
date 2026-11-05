"""Strict, append-only WorkerReport validation and progress projection (M2-03)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from . import briefs, domain, storage
from .clock import Clock, SystemClock
from .errors import ConflictError, ValidationError
from .events import append_event
from .ids import require_entity_id

REPORT_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MILESTONE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class ValidatedProgress:
    task_id: str
    attempt_id: str
    task_version: int
    brief_digest: str
    milestone_id: str
    report_digest: str
    artifact_digest: str | None
    recorded_at: str


def _now(clock: Clock | None) -> str:
    return (clock or SystemClock()).now().astimezone().isoformat()


def _digest(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest" + (" or null" if nullable else ""))
    return value


def _canonical(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_worker_report(value: str | dict[str, Any]) -> dict[str, Any]:
    """Validate the wire schema and return a canonicalized report object.

    ``usage`` is optional; if supplied each known quantity is either a
    non-negative integer or ``null``.  Null deliberately means unknown, not 0.
    """
    if isinstance(value, str):
        try: value = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc: raise ValidationError("worker report must be JSON") from exc
    if not isinstance(value, dict): raise ValidationError("worker report must be an object")
    if any(not isinstance(key, str) for key in value): raise ValidationError("worker report keys must be strings")
    allowed = {"schema_version", "task_id", "attempt_id", "task_version", "brief_digest", "report_kind", "outcome", "milestone_id", "summary", "artifact_digest", "usage", "report_digest"}
    unknown = set(value) - allowed
    missing = {"schema_version", "task_id", "attempt_id", "task_version", "brief_digest", "report_kind", "milestone_id", "summary"} - set(value)
    if unknown or missing: raise ValidationError(f"worker report keys invalid; unknown={sorted(unknown)}, missing={sorted(missing)}")
    if isinstance(value["schema_version"], bool) or value["schema_version"] != REPORT_SCHEMA_VERSION: raise ValidationError("unsupported worker report schema_version")
    task_id, attempt_id = require_entity_id(value["task_id"]), require_entity_id(value["attempt_id"])
    version = value["task_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0: raise ValidationError("task_version must be an integer >= 0")
    brief_digest = _digest(value["brief_digest"], "brief_digest")
    kind = value["report_kind"]
    if not isinstance(kind, str) or kind not in {"progress", "final"}: raise ValidationError("report_kind must be progress or final")
    outcome = value.get("outcome")
    if kind == "final":
        if not isinstance(outcome, str) or outcome not in {"completed", "blocked", "failed"}: raise ValidationError("final report requires completed, blocked, or failed outcome")
    elif outcome is not None:
        raise ValidationError("progress report outcome must be null or omitted")
    milestone = value["milestone_id"]
    if not isinstance(milestone, str) or not _MILESTONE.fullmatch(milestone): raise ValidationError("milestone_id must be 1..128 safe identifier characters")
    summary = value["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary.encode("utf-8")) > 4096: raise ValidationError("summary must be non-empty and at most 4096 bytes")
    artifact = _digest(value.get("artifact_digest"), "artifact_digest", nullable=True)
    usage = value.get("usage")
    if usage is not None:
        if not isinstance(usage, dict) or set(usage) - {"input_tokens", "output_tokens", "total_tokens", "cost_microunits"}: raise ValidationError("usage has invalid fields")
        for key, amount in usage.items():
            if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int) or amount < 0): raise ValidationError(f"usage.{key} must be a non-negative integer or null")
    supplied_raw = value.get("report_digest")
    if supplied_raw is not None: _digest(supplied_raw, "report_digest")
    unsigned = dict(value); unsigned.pop("report_digest", None)
    expected = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    if supplied_raw is not None and supplied_raw != expected: raise ValidationError("report_digest does not match canonical report content")
    value = dict(value); value["report_digest"] = expected
    return json.loads(_canonical(value))


def record_worker_report(conn: storage.TeamCraftConnection, report: str | dict[str, Any], *, clock: Clock | None = None) -> ValidatedProgress:
    """Atomically validate and append an evidence report; exact replay is idempotent.

    This function never transitions an Attempt or Task.  A stale brief/task
    binding is rejected before insertion, preserving verification/approval for
    M2-06.
    """
    value = parse_worker_report(report)
    with storage.transaction(conn) if not conn.in_transaction else _joined(conn):
        attempt = domain.get_attempt(conn, value["attempt_id"])
        if attempt.task_id != value["task_id"]: raise ValidationError("report task_id does not match Attempt")
        brief = briefs.get_attempt_brief(conn, attempt.id)
        if (brief.task_version != value["task_version"] or brief.brief_digest != value["brief_digest"]): raise ValidationError("report evidence does not match immutable Attempt Brief")
        # The live Task version is checked too: a later task/evidence change
        # invalidates old report bindings rather than allowing stale progress.
        if domain.get_task(conn, attempt.task_id).version != brief.task_version: raise ValidationError("report is stale because Task evidence version changed")
        canonical = _canonical(value)
        row = conn.execute("SELECT task_id, attempt_id, task_version, brief_digest, milestone_id, report_digest, artifact_digest, recorded_at, report_json FROM worker_reports WHERE attempt_id=? AND report_digest=?", (attempt.id, value["report_digest"])).fetchone()
        if row is not None:
            if str(row[8]) != canonical: raise ConflictError("worker report digest collision")
            return _progress(row)
        if attempt.status != "active":
            raise ValidationError("new WorkerReport requires an active Attempt")
        if value["report_kind"] == "progress" and value["milestone_id"] == "final":
            raise ValidationError("final milestone is reserved for a final report")
        # A Brief may optionally define a plan as {"milestones":[{"id":...}]}.
        # A runtime Brief always carries a plan. Legacy/manual Briefs without
        # one may emit only the terminal final report; arbitrary progress ids
        # must not refresh the progress clock.
        try:
            plan = json.loads(brief.content)
        except json.JSONDecodeError:
            plan = None
        if isinstance(plan, dict) and "milestones" in plan:
            items = plan["milestones"]
            valid = {item.get("id") for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)} if isinstance(items, list) else set()
            if value["milestone_id"] not in valid:
                raise ValidationError("milestone_id is not declared by the Attempt Brief plan")
        elif value["report_kind"] != "final" or value["milestone_id"] != "final":
            raise ValidationError("Brief without milestone plan permits only final/final report")
        recorded = _now(clock)
        conn.execute("INSERT INTO worker_reports (attempt_id, task_id, task_version, brief_digest, report_kind, outcome, milestone_id, report_digest, artifact_digest, report_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (attempt.id, attempt.task_id, brief.task_version, brief.brief_digest, value["report_kind"], value.get("outcome"), value["milestone_id"], value["report_digest"], value.get("artifact_digest"), canonical, recorded))
        append_event(conn, "worker.report_recorded", task_id=attempt.task_id, attempt_id=attempt.id,
            payload_json=json.dumps({"report_digest": value["report_digest"], "report_kind": value["report_kind"]}, sort_keys=True), clock=clock)
        if value["report_kind"] == "final":
            if attempt.status != "active": raise ValidationError("final report requires an executing Attempt")
            domain.transition_attempt(conn, attempt.id, "finish", result_json=canonical, clock=clock)
            append_event(conn, "attempt.execution_finished", task_id=attempt.task_id, attempt_id=attempt.id,
                payload_json=json.dumps({"outcome": value["outcome"], "report_digest": value["report_digest"]}, sort_keys=True), clock=clock)
    return ValidatedProgress(attempt.task_id, attempt.id, brief.task_version, brief.brief_digest, value["milestone_id"], value["report_digest"], value.get("artifact_digest"), recorded)


def _progress(row: Any) -> ValidatedProgress:
    return ValidatedProgress(str(row[0]), str(row[1]), int(row[2]), str(row[3]), str(row[4]), str(row[5]), None if row[6] is None else str(row[6]), str(row[7]))


def load_validated_progress(conn: storage.TeamCraftConnection, attempt_id: str) -> list[ValidatedProgress]:
    """Return durable validated progress in append/persistent order."""
    rows = conn.execute("SELECT task_id, attempt_id, task_version, brief_digest, milestone_id, report_digest, artifact_digest, recorded_at FROM worker_reports WHERE attempt_id=? ORDER BY id ASC", (require_entity_id(attempt_id),)).fetchall()
    return [_progress(row) for row in rows]


persist_worker_report = record_worker_report


class _joined:
    def __init__(self, conn: storage.TeamCraftConnection): self.conn = conn
    def __enter__(self): return self.conn
    def __exit__(self, *args: Any): return False
