"""Human-directed timeout, reconciliation, and retry controls (M2-05).

This module never stops a worker or creates an Attempt. External effects are
durable Commands, fenced by the project Leader lease, and ambiguous execution
continues to occupy its Attempt slot until verified reconciliation.
"""
from __future__ import annotations

import json
import contextlib
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from . import briefs, commands, domain, events, leader, worker_reports
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    IllegalTransitionError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from .storage import TeamCraftConnection, transaction


@contextlib.contextmanager
def _tx(conn: TeamCraftConnection) -> Iterator[TeamCraftConnection]:
    """Join an existing Core write transaction or open one when standalone."""
    if conn.in_transaction:
        yield conn
    else:
        with transaction(conn) as gated:
            yield gated


@dataclass(frozen=True)
class RecoveryDecision:
    attempt_id: str
    action: str
    reason: str


def _now(clock: Clock | None) -> datetime:
    return (clock or SystemClock()).now().astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_command_evidence(command: commands.Command, task: domain.Task,
                               attempt: domain.Attempt, *, allow_stale_version: bool = False) -> None:
    try:
        evidence = json.loads(command.evidence_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("recovery Command evidence is malformed") from exc
    if not isinstance(evidence, dict):
        raise ValidationError("recovery Command evidence must be an object")
    if attempt.task_id != task.id:
        raise ConflictError("recovery Attempt does not belong to the bound Task")
    expected = {"task_id": task.id, "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no}
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ConflictError("recovery Command evidence Task/Attempt binding mismatch")
    try:
        request = json.loads(command.payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("recovery Command payload is malformed") from exc
    if not isinstance(request, dict):
        raise ValidationError("recovery Command payload must be an object")
    if (request.get("task_id") != task.id or request.get("attempt_id") != attempt.id
            or request.get("attempt_no") != attempt.attempt_no):
        raise ConflictError("recovery Command payload Task/Attempt binding mismatch")
    if evidence.get("task_version") != request.get("task_version"):
        raise ConflictError("recovery Command payload/evidence Task version mismatch")
    if not allow_stale_version and evidence.get("task_version") != task.version:
        raise ConflictError("recovery Command evidence Task version is stale")


def _require_active_restore_version(conn: TeamCraftConnection, task: domain.Task, attempt: domain.Attempt) -> None:
    """An active restore is safe only while its immutable Brief is current."""
    brief = briefs.get_attempt_brief(conn, attempt.id)
    if task.version != brief.task_version:
        raise ConflictError("cannot restore an active Attempt after Task evidence version changed; keep it UNKNOWN")


def evaluate_attempt(conn: TeamCraftConnection, attempt_id: str, *, now: datetime | None = None) -> RecoveryDecision:
    """Evaluate timeout facts; return an escalation recommendation only."""
    attempt = domain.get_attempt(conn, attempt_id)
    if attempt.status not in domain.OCCUPYING_ATTEMPT_STATUSES:
        return RecoveryDecision(attempt.id, "none", "attempt_not_occupying")
    if attempt.status == "unknown":
        return RecoveryDecision(attempt.id, "reconcile", "attempt_outcome_unknown")
    try:
        brief = briefs.get_attempt_brief(conn, attempt.id)
    except NotFoundError:
        return RecoveryDecision(attempt.id, "wait", "attempt_brief_unavailable")
    clock_now = now or _now(None)
    if clock_now.tzinfo is None:
        raise ValidationError("now must be timezone-aware")
    if brief.deadline_at and clock_now >= datetime.fromisoformat(brief.deadline_at):
        return RecoveryDecision(attempt.id, "escalate", "deadline_exceeded")
    if brief.no_progress_timeout_seconds:
        progress = conn.execute(
            "SELECT MAX(recorded_at) FROM worker_reports WHERE attempt_id = ?",
            (attempt.id,),
        ).fetchone()[0]
        baseline_raw = progress or attempt.started_at
        if baseline_raw and (clock_now - datetime.fromisoformat(str(baseline_raw))).total_seconds() >= brief.no_progress_timeout_seconds:
            return RecoveryDecision(attempt.id, "escalate", "no_progress_threshold")
    return RecoveryDecision(attempt.id, "wait", "within_policy")


def _trigger(conn: TeamCraftConnection, trigger_event_id: str, task_id: str, attempt_id: str, project_id: str, reason: str):
    if reason not in {"deadline_exceeded", "no_progress_threshold"}:
        raise ValidationError("reason must be a recognized Watcher timeout reason")
    row = conn.execute(
        "SELECT kind, project_id, task_id, attempt_id, payload_json FROM events WHERE event_id = ?",
        (trigger_event_id,),
    ).fetchone()
    if row is None or str(row[0]) not in {"watch.no_progress_threshold", "watch.deadline_exceeded"}:
        raise ValidationError("trigger_event_id must identify a Watcher timeout event")
    if str(row[1]) != project_id or str(row[2]) != task_id or str(row[3]) != attempt_id:
        raise ValidationError("Watcher event does not match this task and attempt")
    expected_kind = "watch.deadline_exceeded" if reason == "deadline_exceeded" else "watch.no_progress_threshold"
    if str(row[0]) != expected_kind:
        raise ValidationError("Watcher event kind does not match the evaluated timeout reason")
    return row


def escalate_threshold(conn: TeamCraftConnection, attempt_id: str, trigger_event_id: str, *, reason: str, lease: leader.Lease, clock: Clock | None = None) -> domain.Task:
    """Audit a valid Watcher trigger and move a running task to NEEDS_HUMAN."""
    with _tx(conn):
        attempt = domain.get_attempt(conn, attempt_id)
        task = domain.get_task(conn, attempt.task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        row = _trigger(conn, trigger_event_id, task.id, attempt.id, task.project_id, reason)
        if attempt.status not in {"created", "starting", "active", "unknown"}:
            raise IllegalTransitionError("terminal attempt cannot be escalated", entity="attempt", current=attempt.status, event="recovery_escalated")
        payload = {"trigger_event_id": trigger_event_id, "trigger_kind": str(row[0]), "attempt_no": attempt.attempt_no}
        events.append_event_idempotent(conn, "recovery.escalated", project_id=task.project_id,
            task_id=task.id, attempt_id=attempt.id, payload_json=_json(payload),
            event_id=f"recovery_escalated_{trigger_event_id}", clock=clock)
        if task.status == "running":
            return domain.apply_task_event(conn, task.id, "recovery_escalated", expected_version=task.version, clock=clock)
        if task.status == "needs_human":
            return task
        raise IllegalTransitionError("task cannot be escalated from its current state", entity="task", current=task.status, event="recovery_escalated")


def escalate_attempt_limit(conn: TeamCraftConnection, task_id: str, *, lease: leader.Lease, clock: Clock | None = None) -> domain.Task:
    """Move an exhausted READY task to NEEDS_HUMAN with a durable audit event."""
    with _tx(conn):
        task = domain.get_task(conn, task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        used = int(conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task.id,)).fetchone()[0])
        if used < task.max_attempts:
            raise ValidationError("attempt limit has not been reached")
        if task.status == "needs_human":
            return task
        if task.status not in {"ready", "running"}:
            raise IllegalTransitionError("attempt limit escalation requires READY/RUNNING task", entity="task", current=task.status, event="attempts_exhausted")
        if domain.occupying_attempt(conn, task.id) is not None:
            raise IllegalTransitionError("an occupying attempt must settle before attempt-limit escalation", entity="task", current=task.status, event="attempts_exhausted")
        events.append_event(conn, "recovery.attempts_exhausted", project_id=task.project_id,
            task_id=task.id, payload_json=_json({"attempts_used": used, "max_attempts": task.max_attempts}), clock=clock)
        return domain.apply_task_event(conn, task.id, "attempts_exhausted", expected_version=task.version, clock=clock)


LEGACY_MISSING_REPORT_COMMAND = domain.LEGACY_MISSING_REPORT_COMMAND_KIND
LEGACY_MISSING_REPORT_EVENT = domain.LEGACY_MISSING_REPORT_EVENT_KIND


def disposition_legacy_missing_report(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    attempt_id: str,
    expected_version: int,
    actor: str,
    reason: str,
    idempotency_key: str,
    lease: leader.Lease,
    clock: Clock | None = None,
) -> domain.Task:
    """Audit an incomplete legacy Attempt and move VERIFYING to NEEDS_HUMAN.

    This never manufactures execution or delivery evidence and never mutates
    the historical Attempt.  A separate :func:`approve_retry` call is needed
    before Core can reserve a fresh Attempt.
    """

    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValidationError("actor and reason are required")
    with _tx(conn):
        task = domain.get_task(conn, task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        existing = conn.execute(
            "SELECT id, kind, payload_json, state FROM commands WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            try:
                prior = json.loads(str(existing[2]))
            except (TypeError, json.JSONDecodeError):
                prior = {}
            expected = {
                "task_id": task_id, "attempt_id": attempt_id,
                "task_version": expected_version, "actor": actor.strip(),
                "reason": reason.strip(),
            }
            if (str(existing[1]) != LEGACY_MISSING_REPORT_COMMAND
                    or any(prior.get(key) != value for key, value in expected.items())):
                raise ConflictError("legacy recovery idempotency key has different content")
            if str(existing[3]) == "confirmed":
                return task
            raise IllegalTransitionError(
                "legacy recovery Command must be confirmed for replay",
                entity="command", current=str(existing[3]),
                event=domain.LEGACY_ATTEMPT_INCOMPLETE_EVENT,
            )
        if task.status != "verifying":
            raise IllegalTransitionError(
                "legacy recovery requires a VERIFYING Task",
                entity="task", current=task.status,
                event=domain.LEGACY_ATTEMPT_INCOMPLETE_EVENT,
            )
        if task.version != expected_version:
            raise ConflictError(
                f"task {task.id} version conflict: expected {expected_version}, found {task.version}"
            )
        attempt = domain.get_attempt(conn, attempt_id)
        domain.require_legacy_missing_report_facts(conn, task, attempt.id)
        payload = {
            "task_id": task.id, "task_version": task.version,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "actor": actor.strip(), "reason": reason.strip(),
        }
        absence_digest = hashlib.sha256(_json({
            "contract": "legacy-missing-report-v1",
            "task_id": task.id, "task_version": task.version,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "final_worker_report": False, "m4_evidence": False,
        }).encode("utf-8")).hexdigest()
        submission = commands.submit_command(
            conn, idempotency_key=idempotency_key,
            kind=LEGACY_MISSING_REPORT_COMMAND, payload_json=_json(payload),
            evidence_json=_json({
                "task_id": task.id, "task_version": task.version,
                "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
                "digest": absence_digest,
            }), clock=clock,
        )
        command = submission.command
        commands.claim_command(conn, command.id, clock=clock)
        updated = domain.apply_task_event(
            conn, task.id, domain.LEGACY_ATTEMPT_INCOMPLETE_EVENT,
            expected_version=task.version, clock=clock,
        )
        result = {
            "task_id": task.id, "task_version": updated.version,
            "task_status": updated.status, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no, "disposition": "incomplete",
        }
        commands.confirm_command(conn, command.id, result_json=_json(result), clock=clock)
        return updated


def _legacy_missing_report_retryable(
    conn: TeamCraftConnection, task: domain.Task, attempt: domain.Attempt
) -> bool:
    row = conn.execute(
        "SELECT payload_json FROM events WHERE kind=? AND task_id=? AND attempt_id=? "
        "ORDER BY seq DESC LIMIT 1",
        (LEGACY_MISSING_REPORT_EVENT, task.id, attempt.id),
    ).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, json.JSONDecodeError):
        return False
    if (not isinstance(payload, dict)
            or payload.get("task_version") != task.version
            or payload.get("disposition") != "incomplete"):
        return False
    domain.require_legacy_missing_report_facts(conn, task, attempt.id)
    return True


def _failed_worktree_verification_retryable(
    conn: TeamCraftConnection, task: domain.Task, attempt: domain.Attempt,
    *, report_id: int, report_digest: str, report_task_version: int,
    report_brief_digest: str,
) -> bool:
    """Whether a finished ship Attempt is retryable after verifier failure.

    A completed WorkerReport alone is normally a successful execution outcome,
    so it must never make the Attempt retryable.  M4 verification is the narrow
    exception: a durable FAIL/ERROR verdict moves the Task to NEEDS_HUMAN and
    explicitly requires a new bounded Attempt.  Re-derive every binding here so
    an unrelated, stale, or post-edited verifier row cannot authorize retry.
    """
    if (task.kind != "ship" or task.status != "needs_human"
            or attempt.status != "finished"):
        return False
    brief = conn.execute(
        "SELECT task_id, task_version, brief_digest FROM attempt_briefs"
        " WHERE attempt_id=?", (attempt.id,),
    ).fetchone()
    if (brief is None or str(brief[0]) != task.id
            or int(brief[1]) != report_task_version
            or str(brief[2]) != report_brief_digest):
        return False
    rows = conn.execute(
        "SELECT task_id, attempt_no, task_version, dispatch_task_version,"
        " brief_digest, worker_report_id, worker_report_digest, status"
        " FROM worktree_verifications WHERE attempt_id=?"
        " ORDER BY created_at, id", (attempt.id,),
    ).fetchall()
    if not rows or not any(str(row[7]) in {"fail", "error"} for row in rows):
        return False
    verification_version = int(rows[0][2])
    if task.version != verification_version + 1:
        return False
    for row in rows:
        if (str(row[0]) != task.id or int(row[1]) != attempt.attempt_no
                or int(row[2]) != verification_version
                or int(row[3]) != report_task_version
                or str(row[4]) != report_brief_digest
                or int(row[5]) != report_id
                or str(row[6]) != report_digest
                or str(row[7]) not in {"pass", "fail", "error", "skipped"}):
            return False
    failure = conn.execute(
        "SELECT 1 FROM events WHERE task_id=? AND attempt_id=?"
        " AND kind IN ('verification.worktree_failed',"
        " 'verification.worktree_suite_failed') LIMIT 1",
        (task.id, attempt.id),
    ).fetchone()
    if failure is None:
        return False
    # Verification precedes commit/ship/PR delivery.  Any delivery artifact or
    # approval for this Attempt makes its outcome a delivery-recovery problem,
    # never a verifier retry.
    if conn.execute(
        "SELECT 1 FROM events WHERE task_id=? AND attempt_id=? AND kind IN ("
        "'delivery_commit_evidence','delivery_ship_evidence','delivery_approval',"
        "'delivery_pr_evidence','delivery_pr_merge_approval',"
        "'delivery_pr_merge_evidence') LIMIT 1",
        (task.id, attempt.id),
    ).fetchone() is not None:
        return False
    unresolved = conn.execute(
        "SELECT kind, payload_json FROM commands WHERE state IN"
        " ('pending','running','unknown') AND"
        " (kind='verification.worktree_run' OR kind LIKE 'delivery.%')",
    ).fetchall()
    for kind, payload_json in unresolved:
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConflictError(
                f"unresolved {kind} Command has malformed payload") from exc
        if isinstance(payload, dict) and payload.get("attempt_id") == attempt.id:
            return False
    return True


def _exact_local_only_policy(task: domain.Task) -> bool:
    try:
        policy = json.loads(task.execution_policy_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return policy == {"delivery": "local_only"}


def _attempt_has_unresolved_delivery_command(
    conn: TeamCraftConnection, attempt_id: str,
) -> bool:
    rows = conn.execute(
        "SELECT kind, payload_json FROM commands WHERE state IN"
        " ('pending','running','unknown') AND"
        " (kind='verification.worktree_run' OR kind LIKE 'delivery.%')",
    ).fetchall()
    for kind, payload_json in rows:
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConflictError(
                f"unresolved {kind} Command has malformed payload") from exc
        if isinstance(payload, dict) and payload.get("attempt_id") == attempt_id:
            return True
    return False


def _completed_attempt_bindings(
    conn: TeamCraftConnection, task: domain.Task, attempt: domain.Attempt,
    *, report_id: int, report_digest: str, report_task_version: int,
    report_brief_digest: str,
) -> bool:
    if (task.kind != "ship" or attempt.task_id != task.id
            or attempt.status != "finished"):
        return False
    latest = conn.execute(
        "SELECT id FROM attempts WHERE task_id=?"
        " ORDER BY attempt_no DESC LIMIT 1", (task.id,),
    ).fetchone()
    if latest is None or str(latest[0]) != attempt.id:
        return False
    brief = conn.execute(
        "SELECT task_id, task_version, brief_digest FROM attempt_briefs"
        " WHERE attempt_id=?", (attempt.id,),
    ).fetchone()
    if (brief is None or str(brief[0]) != task.id
            or int(brief[1]) != report_task_version
            or str(brief[2]) != report_brief_digest):
        return False
    report = conn.execute(
        "SELECT id, task_id, task_version, brief_digest, report_digest, outcome"
        " FROM worker_reports WHERE attempt_id=? AND report_kind='final'"
        " ORDER BY recorded_at DESC, id DESC LIMIT 1", (attempt.id,),
    ).fetchone()
    return bool(
        report is not None
        and int(report[0]) == report_id
        and str(report[1]) == task.id
        and int(report[2]) == report_task_version
        and str(report[3]) == report_brief_digest
        and str(report[4]) == report_digest
        and str(report[5]) == "completed"
    )


def _stale_verifier_profile_retryable(
    conn: TeamCraftConnection, task: domain.Task, attempt: domain.Attempt,
    *, report_id: int, report_digest: str, report_task_version: int,
    report_brief_digest: str,
) -> bool:
    """Recognize the two audited stale-profile recovery shapes.

    A VERIFYING Task may retry when its immutable PASS rows and Commit still
    bind the latest completed Attempt but the required verifier snapshot has
    moved; already-recorded ship evidence is explicitly retired by an
    invalidation event before the fresh Attempt.  A NEEDS_HUMAN Task may retry
    only when a previously accepted local-only delivery was reopened by the
    exact profile-change invalidation event.  Both paths require a new Attempt;
    no historical evidence is edited or reused as current delivery evidence.
    """
    if (task.status not in {"verifying", "needs_human"}
            or not _exact_local_only_policy(task)
            or not _completed_attempt_bindings(
                conn, task, attempt, report_id=report_id,
                report_digest=report_digest,
                report_task_version=report_task_version,
                report_brief_digest=report_brief_digest,
            )
            or _attempt_has_unresolved_delivery_command(conn, attempt.id)):
        return False

    # Imports stay local so scheduler -> recovery does not eagerly load the
    # delivery/verification service graph.
    from . import delivery, verification

    try:
        commit = delivery.load_commit_evidence(conn, attempt.id)
        current_profiles = verification.required_profile_snapshot(
            conn, task.project_id)
    except (NotFoundError, StorageError, ValidationError):
        return False
    if commit is None:
        return False
    if (commit.project_id != task.project_id or commit.task_id != task.id
            or commit.attempt_id != attempt.id
            or commit.attempt_no != attempt.attempt_no
            or commit.dispatch_task_version != report_task_version
            or commit.brief_digest != report_brief_digest
            or commit.worker_report_id != report_id
            or commit.worker_report_digest != report_digest):
        return False

    if task.status == "verifying":
        if not delivery.commit_evidence_is_current(
                conn, commit, live_git=False).ok:
            return False
        try:
            brief = briefs.get_attempt_brief(conn, attempt.id)
        except NotFoundError:
            return False
        # The one supported post-completion local-only correction legitimately
        # produces PASS rows at version N and Commit/Ship evidence at N+1.
        # Accept exactly that audited rebind, never an arbitrary version gap.
        exact_rebind = verification._is_exact_local_only_policy_rebind(
            conn, task, attempt, brief, report_digest)
        rows = verification.list_worktree_verifications(conn, attempt.id)
        if not rows or any(
            row.status != "pass"
            or row.project_id != task.project_id
            or row.task_id != task.id
            or row.attempt_no != attempt.attempt_no
            or not (
                row.task_version == commit.task_version
                or (commit.task_version == row.task_version + 1
                    and exact_rebind)
            )
            or row.dispatch_task_version != report_task_version
            or row.brief_digest != report_brief_digest
            or row.worker_report_id != report_id
            or row.worker_report_digest != report_digest
            or row.allocation_id != commit.allocation_id
            or row.worktree_path != commit.worktree_path
            or row.branch != commit.branch
            or row.base_sha != commit.base_sha
            or row.profile_version is None
            or row.config_digest is None
            for row in rows
        ):
            return False
        pinned = {
            row.profile: (int(row.profile_version), str(row.config_digest))
            for row in rows
        }
        if len(pinned) != len(rows):
            return False
        try:
            ship = delivery.load_ship_evidence(conn, attempt.id)
        except (NotFoundError, StorageError, ValidationError):
            return False
        if ship is None:
            # A ship/approval event without loadable evidence is corrupt; fail
            # closed rather than silently falling back to the PASS-only path.
            if conn.execute(
                "SELECT 1 FROM events WHERE task_id=? AND attempt_id=? AND kind IN"
                " ('delivery_ship_evidence','delivery_approval') LIMIT 1",
                (task.id, attempt.id),
            ).fetchone() is not None:
                return False
            return pinned != current_profiles
        # Ship evidence exists: recovery is sanctioned only while it still
        # binds this exact completed Attempt/Commit/WorkerReport and no accepted
        # human Approval has settled the (now stale) evidence version.  A prior
        # rejection is not delivery evidence and is retired by the invalidation
        # the caller writes before the fresh Attempt.
        if (ship.project_id != task.project_id or ship.task_id != task.id
                or ship.attempt_id != attempt.id
                or ship.attempt_no != attempt.attempt_no
                or ship.task_version != task.version
                or ship.task_version != commit.task_version
                or ship.commit_evidence_id != commit.event_id
                or ship.commit_sha != commit.commit_sha
                or ship.diff_digest != commit.diff_digest
                or ship.branch != commit.branch
                or ship.base_sha != commit.base_sha
                or ship.dispatch_task_version != report_task_version
                or ship.brief_digest != report_brief_digest
                or ship.worker_report_id != report_id
                or ship.worker_report_digest != report_digest):
            return False
        try:
            approval = delivery.load_delivery_approval(conn, ship.event_id)
        except (NotFoundError, StorageError, ValidationError):
            return False
        if approval is not None and approval.decision != "reject":
            return False
        pinned_ship = {
            str(item["profile"]):
            (int(item["profile_version"]), str(item["config_digest"]))
            for item in ship.verifiers
        }
        if len(pinned_ship) != len(ship.verifiers):
            return False
        return pinned_ship != current_profiles

    try:
        ship = delivery.load_ship_evidence(conn, attempt.id)
        if ship is None:
            return False
        approval = delivery.load_delivery_approval(conn, ship.event_id)
    except (NotFoundError, StorageError, ValidationError):
        return False
    if (approval is None or approval.decision != "accept"
            or ship.project_id != task.project_id or ship.task_id != task.id
            or ship.attempt_id != attempt.id or ship.attempt_no != attempt.attempt_no
            or ship.commit_evidence_id != commit.event_id
            or ship.commit_sha != commit.commit_sha
            or ship.diff_digest != commit.diff_digest
            or ship.dispatch_task_version != report_task_version
            or ship.brief_digest != report_brief_digest
            or ship.worker_report_id != report_id
            or ship.worker_report_digest != report_digest
            or approval.task_id != task.id or approval.attempt_id != attempt.id
            or approval.task_version != ship.task_version
            or task.version != approval.task_version + 2):
        return False
    pinned = {
        str(item["profile"]):
        (int(item["profile_version"]), str(item["config_digest"]))
        for item in ship.verifiers
    }
    if pinned == current_profiles:
        return False
    invalidations = conn.execute(
        "SELECT seq, payload_json FROM events WHERE task_id=?"
        " AND kind='delivery.approval_invalidated' ORDER BY seq DESC",
        (task.id,),
    ).fetchall()
    if not invalidations:
        return False
    try:
        invalidation = json.loads(str(invalidations[0][1]))
    except (TypeError, json.JSONDecodeError):
        return False
    if (not isinstance(invalidation, dict)
            or invalidation.get("reason") != "verifier_profile_changed"
            or int(invalidations[0][0]) <= approval.seq):
        return False
    # More than one invalidation means the historical acceptance has crossed
    # another recovery boundary and needs a dedicated human disposition.
    return len(invalidations) == 1


def _invalidate_stale_ship_evidence(
    conn: TeamCraftConnection, task: domain.Task, attempt: domain.Attempt,
    *, clock: Clock | None = None,
) -> bool:
    """Mark a stale-profile Ship evidence chain explicitly non-current.

    The immutable ship/PASS rows are never edited or reused.  When a VERIFYING
    retry is sanctioned because the pinned verifier snapshot drifted, this
    appends the same audit event the DONE-reopen path uses, binding it to the
    exact historical ship evidence so the old chain can never be mistaken for
    current delivery evidence.
    """
    from . import delivery, verification

    try:
        ship = delivery.load_ship_evidence(conn, attempt.id)
        if ship is None:
            return False
        current = verification.required_profile_snapshot(conn, task.project_id)
    except (NotFoundError, StorageError, ValidationError):
        return False
    pinned = {
        str(item["profile"]):
        (int(item["profile_version"]), str(item["config_digest"]))
        for item in ship.verifiers
    }
    if pinned == current:
        return False
    already = conn.execute(
        "SELECT 1 FROM events WHERE task_id=? AND attempt_id=?"
        " AND kind='delivery.approval_invalidated'"
        " AND json_extract(payload_json, '$.ship_evidence_id')=?"
        " LIMIT 1",
        (task.id, attempt.id, ship.event_id),
    ).fetchone()
    if already is not None:
        return False
    events.append_event(
        conn, "delivery.approval_invalidated",
        project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
        payload_json=_json({
            "reason": "verifier_profile_changed",
            "ship_evidence_id": ship.event_id,
            "commit_sha": ship.commit_sha,
            "previous_verifier_digest": ship.verifier_digest,
            "next": "new bounded Attempt with fresh commit/ship evidence",
        }),
        clock=clock,
    )
    return True


def request_action(conn: TeamCraftConnection, *, task_id: str, action: str,
                   actor: str, reason: str, idempotency_key: str,
                   attempt_id: str | None = None, clock: Clock | None = None):
    """Persist a human-directed Command under one serialized write boundary."""
    with _tx(conn):
        return _request_action_locked(conn, task_id=task_id, action=action,
            actor=actor, reason=reason, idempotency_key=idempotency_key,
            attempt_id=attempt_id, clock=clock)


def _request_action_locked(conn: TeamCraftConnection, *, task_id: str, action: str,
                           actor: str, reason: str, idempotency_key: str,
                           attempt_id: str | None, clock: Clock | None):
    """Caller holds BEGIN IMMEDIATE so duplicate cancel checks cannot race."""
    if action not in {"wait", "cancel", "reconcile"}:
        raise ValidationError("action must be wait, cancel, or reconcile")
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValidationError("actor and reason are required")
    task = domain.get_task(conn, task_id)
    attempt = domain.get_attempt(conn, attempt_id) if attempt_id else None
    if attempt and attempt.task_id != task.id:
        raise ValidationError("attempt does not belong to task")
    existing = conn.execute("SELECT id, kind, payload_json FROM commands WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    if existing is not None:
        try:
            prior = json.loads(str(existing[2]))
        except (TypeError, json.JSONDecodeError):
            prior = {}
        if (str(existing[1]) != f"recovery.{action}" or prior.get("task_id") != task.id
                or prior.get("attempt_id") != (None if attempt is None else attempt.id)
                or prior.get("attempt_no") != (None if attempt is None else attempt.attempt_no)
                or prior.get("actor") != actor.strip() or prior.get("reason") != reason.strip()):
            raise ConflictError("recovery idempotency key has different content")
        return commands.get_command(conn, str(existing[0]))
    if action == "cancel" and attempt is not None:
        for row in conn.execute(
            "SELECT state, payload_json FROM commands WHERE kind='recovery.cancel' AND state IN ('pending','running','unknown')"
        ).fetchall():
            try:
                pending = json.loads(str(row[1]))
            except (TypeError, json.JSONDecodeError):
                raise ConflictError("an unresolved cancellation Command has malformed payload")
            if pending.get("attempt_id") == attempt.id:
                raise ConflictError("this Attempt already has a pending/running/UNKNOWN cancellation Command; reconcile it first")
    if action == "wait" and task.status != "needs_human":
        raise IllegalTransitionError("wait action requires needs_human task", entity="task", current=task.status, event="recovery.wait")
    if action == "cancel" and (attempt is None or attempt.status not in domain.OCCUPYING_ATTEMPT_STATUSES):
        raise IllegalTransitionError("cancel requires an occupying attempt", entity="attempt", current=None if attempt is None else attempt.status, event="recovery.cancel")
    if action == "reconcile" and (attempt is None or attempt.status != "unknown"):
        raise IllegalTransitionError("reconcile requires an unknown attempt", entity="attempt", current=None if attempt is None else attempt.status, event="recovery.reconcile")
    body = {"task_id": task.id, "attempt_id": None if attempt is None else attempt.id,
            "attempt_no": None if attempt is None else attempt.attempt_no,
            "task_version": task.version, "action": action, "actor": actor.strip(), "reason": reason.strip()}
    evidence = {"task_id": task.id, "task_version": task.version}
    if attempt is not None:
        evidence.update(attempt_id=attempt.id, attempt_no=attempt.attempt_no)
    return commands.submit_command(conn, idempotency_key=idempotency_key,
        kind=f"recovery.{action}", payload_json=_json(body),
        evidence_json=_json(evidence), clock=clock).command


def _persisted_identity_matches(rows: list[Any], evidence: dict[str, Any]) -> bool:
    """Match a present Session identity or its narrow legacy member fallback.

    Early runtimes persisted the exact native Harness Session only in
    ``members.agent_session_json``.  That compatibility value is consulted
    ONLY when it is an explicit ``{"kind": "id", "value": ...}`` record and
    the linked Session row has no native identity.  A present native Session
    identity is authoritative: if ANY row of the Attempt carries a present
    ``native_session_id`` that differs from the evidence, the legacy fallback
    is vetoed for the whole Attempt rather than authorizing one row in
    isolation.
    """
    native_evidence = evidence.get("native_session_id")
    terminal_evidence = evidence.get("terminal_id")
    for row in rows:
        native, terminal = row[0], row[1]
        if (native is not None and native == native_evidence
                and terminal == terminal_evidence):
            return True
    # A present native identity that disagrees with the evidence wins over
    # every legacy compatibility field on this Attempt.
    if any(row[0] is not None and row[0] != native_evidence for row in rows):
        return False
    for row in rows:
        native, terminal, legacy_json = row[0], row[1], row[2]
        if native is not None:
            continue
        try:
            legacy = json.loads(str(legacy_json))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(legacy, dict) or legacy.get("kind") != "id":
            continue
        value = legacy.get("value")
        if (isinstance(value, str) and value.strip()
                and value == native_evidence
                and terminal == terminal_evidence):
            return True
    return False


def resolve_unknown(conn: TeamCraftConnection, attempt_id: str, *, command_id: str,
                    lease: leader.Lease, resolution: str, evidence: dict[str, Any],
                    actor: str, reason: str, clock: Clock | None = None):
    """Resolve UNKNOWN only from a matching claimed Command and verified identity facts."""
    if resolution not in {"active", "finished", "failed", "cancelled"}:
        raise ValidationError("resolution must be active, finished, failed, or cancelled")
    expected_alive = resolution == "active"
    if not isinstance(evidence, dict) or evidence.get("identity_verified") is not True or evidence.get("process_alive") is not expected_alive:
        raise ValidationError("resolution requires verified identity and matching process liveness")
    for key in ("terminal_id", "native_session_id"):
        if not isinstance(evidence.get(key), str) or not evidence[key].strip():
            raise ValidationError(f"verified evidence requires {key}")
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValidationError("actor and reason are required")
    with _tx(conn):
        attempt = domain.get_attempt(conn, attempt_id)
        task = domain.get_task(conn, attempt.task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        command = commands.get_command(conn, command_id)
        if command.kind not in {"recovery.reconcile", "recovery.cancel"}:
            raise IllegalTransitionError("command is not a recovery reconcile/cancel request", entity="command", current=command.state, event="reconcile")
        request = json.loads(command.payload_json)
        if request.get("task_id") != task.id or request.get("attempt_id") != attempt.id or request.get("attempt_no") != attempt.attempt_no:
            raise ConflictError("reconciliation command is bound to different task/attempt evidence")
        if not (command.kind == "recovery.cancel" and command.state == "confirmed") and request.get("task_version") != task.version:
            raise ConflictError("reconciliation command Task version is stale")
        _validate_command_evidence(command, task, attempt,
                                   allow_stale_version=command.kind == "recovery.cancel" and command.state == "confirmed")
        if request.get("actor") != actor.strip() or request.get("reason") != reason.strip():
            raise ConflictError("reconciliation actor/reason differs from the approved request")
        expected_result = _json({"resolution": resolution, "actor": actor.strip(), "reason": reason.strip(), "evidence": evidence})
        if command.state == "confirmed":
            if command.kind == "recovery.cancel":
                stored = json.loads(command.result_json or "{}")
                if resolution != "cancelled" or attempt.status != "cancelled" or task.status != "cancelled" or stored.get("evidence") != evidence:
                    raise ConflictError("confirmed cancellation replay differs from stored facts")
                return attempt
            if attempt.status != resolution or command.result_json != expected_result:
                raise ConflictError("confirmed reconciliation replay differs from the stored resolution")
            return attempt
        if command.state not in {"running", "unknown"}:
            raise IllegalTransitionError("reconciliation command must be claimed or UNKNOWN", entity="command", current=command.state, event="reconcile")
        identity = conn.execute(
            "SELECT s.native_session_id, m.terminal_id, m.agent_session_json FROM sessions s "
            "JOIN members m ON m.session_id = s.id "
            "WHERE s.attempt_id = ? AND m.attempt_id = ?",
            (attempt.id, attempt.id),
        ).fetchall()
        if not _persisted_identity_matches(identity, evidence):
            raise ValidationError("evidence does not match the persisted native session and terminal identity")
        if command.kind == "recovery.cancel":
            if resolution == "active" and evidence.get("process_alive") is True:
                _require_active_restore_version(conn, task, attempt)
                if attempt.status == "unknown":
                    domain.transition_attempt(conn, attempt.id, "reconciled_active", result_json=_json({"actor": actor.strip(), "reason": reason.strip(), "evidence": evidence}), clock=clock)
                elif attempt.status != "active":
                    raise IllegalTransitionError("active cancellation reconcile requires active/unknown Attempt", entity="attempt", current=attempt.status, event="reconciled_active")
                commands.reconcile_command(conn, command.id, outcome="expired", note=f"cancel not observed: {reason.strip()}", clock=clock)
                events.append_event(conn, "recovery.cancel_not_effected", project_id=task.project_id,
                    task_id=task.id, attempt_id=attempt.id,
                    payload_json=_json({"command_id": command.id, "actor": actor.strip(), "reason": reason.strip(), "evidence": evidence}), clock=clock)
                return domain.get_attempt(conn, attempt.id)
            if resolution != "cancelled" or evidence.get("process_alive") is not False:
                raise ValidationError("cancellation reconcile must prove either the same live Agent or a stopped process")
            if attempt.status == "active":
                domain.transition_attempt(conn, attempt.id, "cancel_confirmed", result_json=_json({"actor": actor.strip(), "reason": reason.strip(), "evidence": evidence}), clock=clock)
            elif attempt.status == "unknown":
                domain.transition_attempt(conn, attempt.id, "reconciled_cancelled", result_json=_json({"actor": actor.strip(), "reason": reason.strip(), "evidence": evidence}), clock=clock)
            else:
                raise IllegalTransitionError("cancel Command can settle only active/unknown Attempt", entity="attempt", current=attempt.status, event="cancel_confirmed")
            updated_task = domain.apply_task_event(conn, task.id, "user_cancel", expected_version=task.version, clock=clock)
            commands.reconcile_command(conn, command.id, outcome="confirmed", note=reason.strip(), result_json=expected_result, clock=clock)
            events.append_event(conn, "recovery.cancelled", project_id=task.project_id, task_id=task.id,
                attempt_id=attempt.id, payload_json=_json({"command_id": command.id, "task_version": updated_task.version}), clock=clock)
            return domain.get_attempt(conn, attempt.id)
        if attempt.status != "unknown":
            raise IllegalTransitionError("only unknown attempts can be reconciled", entity="attempt", current=attempt.status, event="reconcile")
        report = conn.execute("SELECT report_json FROM worker_reports WHERE attempt_id = ? AND report_kind = 'final' ORDER BY recorded_at DESC LIMIT 1", (attempt.id,)).fetchone()
        if resolution in {"finished", "failed"}:
            if report is None:
                raise ValidationError("finished/failed resolution requires a durable final WorkerReport")
            final_report = worker_reports.parse_worker_report(str(report[0]))
            expected_outcomes = {"completed"} if resolution == "finished" else {"failed", "blocked"}
            if final_report.get("outcome") not in expected_outcomes:
                raise ValidationError("durable final WorkerReport outcome does not match the resolution")
        if resolution == "active": event = "reconciled_active"
        elif resolution == "finished": event = "reconciled_finished"
        elif resolution == "failed": event = "reconciled_failed"
        else: event = "reconciled_cancelled"
        if resolution == "active":
            _require_active_restore_version(conn, task, attempt)
        domain.transition_attempt(conn, attempt.id, event, result_json=_json({"actor": actor.strip(), "reason": reason.strip(), "evidence": evidence}), clock=clock)
        result = {"resolution": resolution, "actor": actor.strip(), "reason": reason.strip(), "evidence": evidence}
        commands.reconcile_command(conn, command.id, outcome="confirmed", note=reason.strip(), result_json=_json(result), clock=clock)
        return domain.get_attempt(conn, attempt.id)


def confirm_cancellation(conn: TeamCraftConnection, attempt_id: str, *, command_id: str,
                         lease: leader.Lease, evidence: dict[str, Any],
                         clock: Clock | None = None) -> domain.Task:
    """Commit an explicitly requested, identity-verified stop atomically."""
    if not isinstance(evidence, dict) or evidence.get("identity_verified") is not True or evidence.get("process_alive") is not False:
        raise ValidationError("cancellation requires verified identity and process_alive=false")
    for key in ("terminal_id", "native_session_id"):
        if not isinstance(evidence.get(key), str) or not evidence[key].strip():
            raise ValidationError(f"verified evidence requires {key}")
    with _tx(conn):
        attempt = domain.get_attempt(conn, attempt_id)
        task = domain.get_task(conn, attempt.task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        command = commands.get_command(conn, command_id)
        if command.kind != "recovery.cancel":
            raise IllegalTransitionError("cancellation command must be claimed and running", entity="command", current=command.state, event="confirm_cancel")
        request = json.loads(command.payload_json)
        if request.get("task_id") != task.id or request.get("attempt_id") != attempt.id or request.get("attempt_no") != attempt.attempt_no:
            raise ConflictError("cancellation command is bound to different task/attempt evidence")
        if command.state != "confirmed" and request.get("task_version") != task.version:
            raise ConflictError("cancellation command Task version is stale")
        _validate_command_evidence(command, task, attempt, allow_stale_version=command.state == "confirmed")
        identity = conn.execute(
            "SELECT s.native_session_id, m.terminal_id, m.agent_session_json FROM sessions s "
            "JOIN members m ON m.session_id = s.id WHERE s.attempt_id = ? AND m.attempt_id = ?",
            (attempt.id, attempt.id),
        ).fetchall()
        if not _persisted_identity_matches(identity, evidence):
            raise ValidationError("cancellation evidence does not match persisted session/terminal identity")
        if command.state == "confirmed":
            if task.status != "cancelled" or command.result_json is None:
                raise ConflictError("confirmed cancellation replay does not match stored state")
            stored = json.loads(command.result_json)
            if stored.get("cancelled") is not True or stored.get("task_id") != task.id or stored.get("evidence") != evidence:
                raise ConflictError("confirmed cancellation replay differs from stored evidence")
            return task
        if command.state != "running":
            raise IllegalTransitionError("cancellation command must be claimed and running", entity="command", current=command.state, event="confirm_cancel")
        if attempt.status == "active":
            domain.transition_attempt(conn, attempt.id, "cancel_confirmed", result_json=_json({"evidence": evidence}), clock=clock)
        elif attempt.status == "unknown":
            domain.transition_attempt(conn, attempt.id, "reconciled_cancelled", result_json=_json({"evidence": evidence}), clock=clock)
        else:
            raise IllegalTransitionError("only active/unknown attempts can be cancelled", entity="attempt", current=attempt.status, event="cancel_confirmed")
        updated = domain.apply_task_event(conn, task.id, "user_cancel", expected_version=task.version, clock=clock)
        commands.confirm_command(conn, command.id, result_json=_json({"cancelled": True, "task_id": task.id, "task_version": updated.version, "evidence": evidence}), clock=clock)
        events.append_event(conn, "recovery.cancelled", project_id=task.project_id, task_id=task.id,
                            attempt_id=attempt.id, payload_json=_json({"command_id": command.id, "task_version": updated.version}), clock=clock)
        return updated


def approve_retry(conn: TeamCraftConnection, task_id: str, *, actor: str, reason: str,
                  idempotency_key: str, lease: leader.Lease, clock: Clock | None = None):
    """Record an explicit retry approval; it never creates an Attempt."""
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValidationError("actor and reason are required")
    with _tx(conn):
        task = domain.get_task(conn, task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        existing = conn.execute("SELECT id, kind, payload_json, state FROM commands WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if existing is not None:
            try:
                prior_payload = json.loads(str(existing[2]))
            except (TypeError, json.JSONDecodeError):
                prior_payload = {}
            if (str(existing[1]) != "recovery.retry_approved" or prior_payload.get("task_id") != task.id
                    or prior_payload.get("actor") != actor.strip() or prior_payload.get("reason") != reason.strip()):
                raise ConflictError("retry approval idempotency key has different content")
            if str(existing[3]) == "confirmed":
                return task
        evidence = _json({"task_id": task.id, "task_version": task.version})
        payload = _json({"task_id": task.id, "task_version": task.version, "actor": actor.strip(), "reason": reason.strip()})
        submission = commands.submit_command(conn, idempotency_key=idempotency_key,
            kind="recovery.retry_approved", payload_json=payload, evidence_json=evidence, clock=clock)
        command = submission.command
        if not submission.created and command.state == "confirmed":
            return task
        if command.payload_json != payload:
            raise ConflictError("retry approval idempotency key has different content")
        if command.state != "pending":
            raise IllegalTransitionError("retry approval Command must be pending", entity="command", current=command.state, event="retry_approved")
        if task.status not in {"running", "verifying", "needs_human"}:
            raise IllegalTransitionError("retry approval requires running, verifying, or needs_human task", entity="task", current=task.status, event="retry_approved")
        if domain.occupying_attempt(conn, task.id) is not None:
            raise IllegalTransitionError("occupying attempt must be reconciled before retry", entity="task", current=task.status, event="retry_approved")
        attempts = conn.execute("SELECT id, status, result_json FROM attempts WHERE task_id = ? ORDER BY attempt_no DESC LIMIT 1", (task.id,)).fetchone()
        if attempts is None or int(conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0]) >= task.max_attempts:
            raise IllegalTransitionError("retry attempt cap reached or no prior attempt", entity="task", current=task.status, event="retry_approved", reason_code="MAX_ATTEMPTS")
        prior_status = str(attempts[1])
        report = conn.execute(
            "SELECT id, report_json, task_version, brief_digest, report_digest"
            " FROM worker_reports WHERE attempt_id = ? AND report_kind='final'"
            " ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (str(attempts[0]),),
        ).fetchone()
        outcome = (
            None if report is None
            else worker_reports.parse_worker_report(str(report[1])).get("outcome")
        )
        legacy_retryable = (
            task.status == "needs_human"
            and prior_status == "finished"
            and outcome is None
            and _legacy_missing_report_retryable(
                conn, task, domain.get_attempt(conn, str(attempts[0]))
            )
        )
        verifier_retryable = (
            task.status == "needs_human"
            and prior_status == "finished"
            and outcome == "completed"
            and report is not None
            and _failed_worktree_verification_retryable(
                conn, task, domain.get_attempt(conn, str(attempts[0])),
                report_id=int(report[0]), report_digest=str(report[4]),
                report_task_version=int(report[2]),
                report_brief_digest=str(report[3]),
            )
        )
        profile_retryable = (
            task.status in {"verifying", "needs_human"}
            and prior_status == "finished"
            and outcome == "completed"
            and report is not None
            and _stale_verifier_profile_retryable(
                conn, task, domain.get_attempt(conn, str(attempts[0])),
                report_id=int(report[0]), report_digest=str(report[4]),
                report_task_version=int(report[2]),
                report_brief_digest=str(report[3]),
            )
        )
        if (legacy_retryable or verifier_retryable or profile_retryable) and conn.execute(
            "SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_id "
            "WHERE d.task_id=? AND p.status<>'done' LIMIT 1",
            (task.id,),
        ).fetchone() is not None:
            raise IllegalTransitionError(
                "retry requires all dependencies to be DONE",
                entity="task", current=task.status, event="retry_approved",
                reason_code="DEPENDENCIES_NOT_SATISFIED",
            )
        if (prior_status not in {"failed", "cancelled"}
                and not (prior_status == "finished" and outcome in {"blocked", "failed"})
                and not legacy_retryable
                and not verifier_retryable
                and not profile_retryable):
            raise IllegalTransitionError("previous attempt is not explicitly retryable", entity="attempt", current=prior_status, event="retry_approved")
        if profile_retryable and task.status == "verifying":
            # A VERIFYING retry after verifier-pin drift must audibly retire
            # any historical ship chain; it is never reused as current
            # delivery evidence for the fresh Attempt.
            _invalidate_stale_ship_evidence(
                conn, task, domain.get_attempt(conn, str(attempts[0])),
                clock=clock,
            )
        commands.claim_command(conn, command.id, clock=clock)
        event = "human_reconciled" if task.status == "needs_human" else "retry_approved"
        target = "ready" if task.status == "needs_human" else None
        updated = domain.apply_task_event(conn, task.id, event, expected_version=task.version, target=target, clock=clock)
        commands.confirm_command(conn, command.id, result_json=_json({"task_id": task.id, "task_version": updated.version, "approved": True}), clock=clock)
        return updated
