"""Human-directed timeout, reconciliation, and retry controls (M2-05).

This module never stops a worker or creates an Attempt. External effects are
durable Commands, fenced by the project Leader lease, and ambiguous execution
continues to occupy its Attempt slot until verified reconciliation.
"""
from __future__ import annotations

import json
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from . import briefs, commands, domain, events, leader, worker_reports
from .clock import Clock, SystemClock
from .errors import ConflictError, IllegalTransitionError, NotFoundError, ValidationError
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
            "SELECT s.native_session_id, m.terminal_id FROM sessions s "
            "JOIN members m ON m.session_id = s.id "
            "WHERE s.attempt_id = ? AND m.attempt_id = ?",
            (attempt.id, attempt.id),
        ).fetchall()
        if not identity or not any(row[0] == evidence["native_session_id"] and row[1] == evidence["terminal_id"] for row in identity):
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
            "SELECT s.native_session_id, m.terminal_id FROM sessions s "
            "JOIN members m ON m.session_id = s.id WHERE s.attempt_id = ? AND m.attempt_id = ?",
            (attempt.id, attempt.id),
        ).fetchall()
        if not identity or not any(row[0] == evidence["native_session_id"] and row[1] == evidence["terminal_id"] for row in identity):
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
        if task.status not in {"running", "needs_human"}:
            raise IllegalTransitionError("retry approval requires running or needs_human task", entity="task", current=task.status, event="retry_approved")
        if domain.occupying_attempt(conn, task.id) is not None:
            raise IllegalTransitionError("occupying attempt must be reconciled before retry", entity="task", current=task.status, event="retry_approved")
        attempts = conn.execute("SELECT id, status, result_json FROM attempts WHERE task_id = ? ORDER BY attempt_no DESC LIMIT 1", (task.id,)).fetchone()
        if attempts is None or int(conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0]) >= task.max_attempts:
            raise IllegalTransitionError("retry attempt cap reached or no prior attempt", entity="task", current=task.status, event="retry_approved", reason_code="MAX_ATTEMPTS")
        prior_status = str(attempts[1])
        report = conn.execute("SELECT report_json FROM worker_reports WHERE attempt_id = ? AND report_kind='final' ORDER BY recorded_at DESC LIMIT 1", (str(attempts[0]),)).fetchone()
        outcome = None if report is None else worker_reports.parse_worker_report(str(report[0])).get("outcome")
        if prior_status not in {"failed", "cancelled"} and not (prior_status == "finished" and outcome in {"blocked", "failed"}):
            raise IllegalTransitionError("previous attempt is not explicitly retryable", entity="attempt", current=prior_status, event="retry_approved")
        commands.claim_command(conn, command.id, clock=clock)
        event = "retry_approved" if task.status == "running" else "human_reconciled"
        target = None if task.status == "running" else "ready"
        updated = domain.apply_task_event(conn, task.id, event, expected_version=task.version, target=target, clock=clock)
        commands.confirm_command(conn, command.id, result_json=_json({"task_id": task.id, "task_version": updated.version, "approved": True}), clock=clock)
        return updated
