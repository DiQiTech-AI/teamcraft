"""Fenced report-only Verification and human Approval service (M2-06).

Only a project-persisted, explicitly approved argv is executed. Verifier
output is captured as an immutable, bounded database artifact; it is evidence,
not a Task transition. Only a current human acceptance of a passing scout
report can move a Task to DONE. Ship delivery remains reserved for M4.
"""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from . import briefs, commands, domain, events, leader, storage, workspaces, worktrees
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    IllegalTransitionError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from .ids import DEFAULT_ID_FACTORY, IdFactory, require_entity_id

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
# JSON escaping and UTF-8 replacement can expand hostile output substantially.
# This lower aggregate limit keeps the final bounded artifact under 2 MiB.
MAX_CAPTURE_BYTES = 256 * 1024
MAX_ARGV_COUNT = 128
MAX_ARG_BYTES = 4096
MAX_ARGV_TOTAL_BYTES = 32 * 1024


@dataclass(frozen=True)
class Verification:
    id: str
    command_id: str
    project_id: str
    task_id: str
    attempt_id: str
    attempt_no: int
    task_version: int
    dispatch_task_version: int
    brief_digest: str
    worker_report_id: int
    worker_report_digest: str
    config_version: int
    rules_version: str
    config_digest: str
    argv: tuple[str, ...]
    status: str
    exit_code: int | None
    artifact: bytes
    artifact_digest: str
    created_at: str


@dataclass(frozen=True)
class Approval:
    id: str
    command_id: str
    verification_id: str
    task_id: str
    attempt_id: str
    attempt_no: int
    task_version: int
    artifact_digest: str
    proposed_action: str
    decision: str
    actor: str
    reason: str
    created_at: str
    current: bool


def _now(clock: Clock | None) -> str:
    return (clock or SystemClock()).now().astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _terminate_verifier(proc: subprocess.Popen[bytes]) -> str | None:
    """Kill the verifier process group so inherited pipe writers cannot hang collection."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.poll() is None:
            proc.kill()
    except ProcessLookupError:
        return None
    except OSError as exc:
        return f"could not kill verifier process: {exc}"
    return None


def _run_bounded(
    argv: Sequence[str], *, cwd: str, timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> tuple[int | None, bytes, bytes, bool, bool, str | None]:
    """Run without a shell and cap combined stdout/stderr while reading pipes.

    At most MAX_CAPTURE_BYTES are retained. A one-byte probe detects overflow;
    upon overflow the entire process group is killed and remaining pipe data is
    drained/discarded (with a bounded cleanup window) before the process is
    reaped. The timeout path uses the same termination and reaping mechanism.

    ``env`` (M4-03): when provided it is the COMPLETE, pinned environment of
    the child — inherited ambient variables (tokens, proxy settings, harness
    secrets) never reach an approved verifier command. ``None`` preserves the
    M2-06 inherit-the-ambient behaviour for the legacy repo-root verifier.
    """
    proc = subprocess.Popen(
        list(argv), cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=(os.name == "posix"),
    )
    assert proc.stdout is not None and proc.stderr is not None
    selector = selectors.DefaultSelector()
    stdout_fd, stderr_fd = proc.stdout.fileno(), proc.stderr.fileno()
    outputs = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    timed_out = False
    truncated = False
    started = time.monotonic()
    deadline = started + timeout_seconds
    cleanup_deadline: float | None = None
    hard_cleanup_deadline: float | None = None
    terminated = False
    cleanup_error: str | None = None

    def terminate() -> None:
        nonlocal cleanup_deadline, hard_cleanup_deadline, terminated, cleanup_error
        now = time.monotonic()
        if hard_cleanup_deadline is None:
            # All post-kill draining and reaping share this hard four-second
            # budget; subsequent cleanup paths cannot extend it.
            hard_cleanup_deadline = now + 4.0
        cleanup_deadline = min(now + 2.0, hard_cleanup_deadline)
        terminated = True
        failure = _terminate_verifier(proc)
        if failure is not None:
            cleanup_error = failure

    try:
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            now = time.monotonic()
            if not terminated and now >= deadline:
                timed_out = True
                terminate()
            if proc.poll() is not None and cleanup_deadline is None:
                # Normally both pipes reach EOF with the process. Bound the
                # case where a detached descendant inherited either pipe.
                cleanup_deadline = now + 1.0
            if cleanup_deadline is not None and now >= cleanup_deadline:
                if not terminated and proc.poll() is not None:
                    cleanup_error = _terminate_verifier(proc) or "verifier child retained an output pipe after the parent exited"
                for key in list(selector.get_map().values()):
                    try:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    except OSError:
                        pass
                break

            wait_for = 0.1
            if not terminated:
                wait_for = max(0.0, min(wait_for, deadline - now))
            elif cleanup_deadline is not None:
                wait_for = max(0.0, min(wait_for, cleanup_deadline - now))
            for key, _ in selector.select(wait_for):
                stream = key.fileobj
                fd = stream.fileno()
                remaining = MAX_CAPTURE_BYTES - sum(len(value) for value in outputs.values())
                # Once terminating, only drain/discard. Otherwise read no more
                # than remaining+1, retaining only the allowed prefix.
                read_size = 64 * 1024 if terminated else min(64 * 1024, remaining + 1)
                try:
                    chunk = os.read(fd, max(1, read_size))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if not terminated:
                    accepted = min(len(chunk), max(0, remaining))
                    outputs[fd].extend(chunk[:accepted])
                    if len(chunk) > accepted:
                        truncated = True
                        terminate()
        if proc.poll() is None:
            terminate()
        if hard_cleanup_deadline is None:
            # The process exited naturally; wait(0) only collects its already
            # available return code and cannot extend the process deadline.
            returncode = proc.wait(timeout=0)
        else:
            wait_budget = max(0.0, hard_cleanup_deadline - time.monotonic())
            try:
                returncode = proc.wait(timeout=wait_budget)
            except subprocess.TimeoutExpired:
                cleanup_error = _terminate_verifier(proc) or "verifier did not exit within the 4-second termination cleanup budget"
                returncode = None
        return (returncode, bytes(outputs[stdout_fd]), bytes(outputs[stderr_fd]),
                timed_out, truncated, cleanup_error)
    finally:
        if proc.poll() is None:
            if hard_cleanup_deadline is None:
                hard_cleanup_deadline = time.monotonic() + 4.0
            _terminate_verifier(proc)
            wait_budget = max(0.0, hard_cleanup_deadline - time.monotonic())
            try:
                proc.wait(timeout=wait_budget)
            except subprocess.TimeoutExpired:
                _terminate_verifier(proc)
            except Exception:
                # Preserve any collection exception; cleanup is best-effort
                # and never adds an unbounded wait.
                pass
        selector.close()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _tx(conn: storage.TeamCraftConnection):
    return storage.transaction(conn) if not conn.in_transaction else _Joined(conn)


class _Joined:
    def __init__(self, conn: storage.TeamCraftConnection): self.conn = conn
    def __enter__(self): return self.conn
    def __exit__(self, *args: Any): return False


def _config(conn: storage.TeamCraftConnection, project_id: str):
    row = conn.execute(
        "SELECT id, config_version, rules_version, argv_json, timeout_seconds, config_digest, approved_by, idempotency_key, created_at "
        "FROM verification_configs WHERE project_id=? ORDER BY config_version DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("project has no explicitly approved verifier argv")
    return row


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValidationError("verifier argv must be a non-empty argument array")
    if len(argv) > MAX_ARGV_COUNT:
        raise ValidationError(f"verifier argv exceeds {MAX_ARGV_COUNT} arguments")
    normalized: list[str] = []
    total_bytes = 0
    for index, value in enumerate(argv):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValidationError(f"verifier argv[{index}] must be a non-empty string without NUL")
        if len(value.encode("utf-8")) > MAX_ARG_BYTES:
            raise ValidationError(f"verifier argv[{index}] exceeds {MAX_ARG_BYTES} bytes")
        total_bytes += len(value.encode("utf-8"))
        if total_bytes > MAX_ARGV_TOTAL_BYTES:
            raise ValidationError(f"verifier argv exceeds {MAX_ARGV_TOTAL_BYTES} total bytes")
        normalized.append(value)
    return tuple(normalized)


def configure_verifier(
    conn: storage.TeamCraftConnection, project_id: str, *, argv: Sequence[str],
    rules_version: str, timeout_seconds: int, actor: str,
    idempotency_key: str, lease: leader.Lease,
    id_factory: IdFactory | None = None, clock: Clock | None = None,
) -> dict[str, Any]:
    """Append an explicitly human-approved argv policy for one project."""
    project_id = require_entity_id(project_id)
    argv_tuple = _validate_argv(argv)
    if not isinstance(rules_version, str) or not rules_version.strip() or len(rules_version.encode("utf-8")) > 128:
        raise ValidationError("rules_version must be 1..128 UTF-8 bytes")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
        raise ValidationError("timeout_seconds must be an integer in [1, 3600]")
    if not isinstance(actor, str) or not actor.strip() or len(actor.encode("utf-8")) > 1024:
        raise ValidationError("actor is required and must be at most 1024 bytes")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key.strip()) > 236:
        raise ValidationError("idempotency_key is required and must be at most 236 characters")
    argv_json = _json(list(argv_tuple))
    factory = id_factory or DEFAULT_ID_FACTORY
    with _tx(conn):
        project = domain.get_project(conn, project_id)
        leader.require_project_lease(conn, lease, project.id, clock=clock)
        prior = conn.execute(
            "SELECT id, config_version, rules_version, argv_json, timeout_seconds, config_digest, approved_by, idempotency_key, created_at, project_id "
            "FROM verification_configs WHERE idempotency_key=?", (idempotency_key.strip(),),
        ).fetchone()
        proposed_digest = _digest(_json({"argv": list(argv_tuple), "rules_version": rules_version.strip(), "timeout_seconds": timeout_seconds}).encode())
        if prior is not None:
            if (str(prior[9]) != project.id or str(prior[2]) != rules_version.strip()
                    or str(prior[3]) != argv_json or int(prior[4]) != timeout_seconds
                    or str(prior[5]) != proposed_digest or str(prior[6]) != actor.strip()):
                raise ConflictError("verifier config idempotency key has different content")
            return _config_value(prior[:9])
        current = conn.execute("SELECT COALESCE(MAX(config_version),0) FROM verification_configs WHERE project_id=?", (project.id,)).fetchone()
        version = int(current[0]) + 1
        config_digest = proposed_digest
        config_id = str(factory.new("verification_config"))
        created = _now(clock)
        body = {"project_id": project.id, "config_version": version, "rules_version": rules_version.strip(),
                "argv": list(argv_tuple), "timeout_seconds": timeout_seconds,
                "config_digest": config_digest, "approved_by": actor.strip()}
        command = commands.submit_command(conn, idempotency_key=f"verification-config:{idempotency_key.strip()}",
            kind="verification.configure", payload_json=_json(body), evidence_json="{}", clock=clock).command
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise ConflictError("verifier config Command exists without its durable config row")
        conn.execute(
            "INSERT INTO verification_configs (id, command_id, project_id, config_version, rules_version, argv_json, timeout_seconds, config_digest, approved_by, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (config_id, command.id, project.id, version, rules_version.strip(), argv_json, timeout_seconds,
             config_digest, actor.strip(), idempotency_key.strip(), created),
        )
        events.append_event(conn, "verification.configured", project_id=project.id,
            payload_json=_json({**body, "config_id": config_id, "command_id": command.id}), clock=clock)
        commands.confirm_command(conn, command.id, result_json=_json({"config_id": config_id, "config_version": version}), clock=clock)
        # A new verifier rules/config version invalidates accepted report
        # approvals and reopens completed scouts in the same transaction.
        approved = conn.execute(
            "SELECT DISTINCT t.id FROM tasks t JOIN approvals a ON a.task_id=t.id "
            "JOIN verifications v ON v.id=a.verification_id "
            "WHERE t.project_id=? AND t.status='done' AND a.decision='accept' "
            "AND v.config_version<>? ORDER BY t.id", (project.id, version),
        ).fetchall()
        for row in approved:
            task = domain.get_task(conn, str(row[0]))
            reopened = domain._cas_task(conn, task, status="needs_human", expected_version=task.version, now=created)
            events.append_event(conn, "verification.approval_invalidated", project_id=project.id,
                task_id=reopened.id, payload_json=_json({"reason": "verifier_config_changed", "config_version": version}), clock=clock)
        return {"id": config_id, "project_id": project.id, "config_version": version,
                "rules_version": rules_version.strip(), "argv": list(argv_tuple),
                "timeout_seconds": timeout_seconds, "config_digest": config_digest,
                "approved_by": actor.strip(), "created_at": created}


def _config_value(row: Any) -> dict[str, Any]:
    return {"id": str(row[0]), "config_version": int(row[1]), "rules_version": str(row[2]),
            "argv": json.loads(str(row[3])), "timeout_seconds": int(row[4]),
            "config_digest": str(row[5]), "approved_by": str(row[6]), "idempotency_key": str(row[7]),
            "created_at": str(row[8])}


def _bound_facts(conn: storage.TeamCraftConnection, attempt_id: str):
    attempt = domain.get_attempt(conn, attempt_id)
    task = domain.get_task(conn, attempt.task_id)
    brief = briefs.get_attempt_brief(conn, attempt.id)
    report_row = conn.execute(
        "SELECT id, task_id, task_version, brief_digest, report_digest, outcome "
        "FROM worker_reports WHERE attempt_id=? AND report_kind='final' ORDER BY id DESC LIMIT 1",
        (attempt.id,),
    ).fetchone()
    if report_row is None:
        raise ValidationError("a durable final WorkerReport is required for Verification")
    if (attempt.status != "finished" or str(report_row[1]) != task.id
            or int(report_row[2]) != brief.task_version
            or str(report_row[3]) != brief.brief_digest or str(report_row[5]) != "completed"):
        raise ValidationError("Verification requires a finished Attempt with completed WorkerReport")
    # WorkerReport/Brief remain pinned to dispatch-time evidence. Exactly one
    # later Task CAS is expected for worker_reported_done; any other version
    # drift before/after that completion transition fails closed.
    if task.version != brief.task_version + 1:
        raise ValidationError("Task evidence version changed outside worker_reported_done; Verification is stale")
    if task.status != "verifying":
        raise IllegalTransitionError("Verification requires Task status VERIFYING", entity="task", current=task.status, event="verification_run")
    return task, attempt, brief, int(report_row[0]), int(report_row[2]), str(report_row[4])


def _verification(row: Any) -> Verification:
    return Verification(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]),
        int(row[6]), int(row[7]), str(row[8]), int(row[9]), str(row[10]), int(row[11]), str(row[12]), str(row[13]),
        tuple(json.loads(str(row[14]))), str(row[15]), None if row[16] is None else int(row[16]),
        bytes(row[17]), str(row[18]), str(row[19]))


_VERIFICATION_COLUMNS = (
    "id, command_id, project_id, task_id, attempt_id, attempt_no, task_version, dispatch_task_version, "
    "brief_digest, worker_report_id, worker_report_digest, config_version, rules_version, config_digest, "
    "argv_json, status, exit_code, artifact, artifact_digest, created_at"
)


def get_verification(conn: storage.TeamCraftConnection, verification_id: str) -> Verification:
    row = conn.execute(f"SELECT {_VERIFICATION_COLUMNS} FROM verifications WHERE id=?", (require_entity_id(verification_id),)).fetchone()
    if row is None: raise NotFoundError(f"verification not found: {verification_id}")
    return _verification(row)


def verification_is_current(conn: storage.TeamCraftConnection, verification: Verification) -> bool:
    """Return whether immutable Verification evidence still matches live facts."""
    try:
        task = domain.get_task(conn, verification.task_id)
        attempt = domain.get_attempt(conn, verification.attempt_id)
        brief = briefs.get_attempt_brief(conn, attempt.id)
        report = conn.execute(
            "SELECT id, task_id, task_version, brief_digest, report_digest, outcome "
            "FROM worker_reports WHERE attempt_id=? AND report_kind='final' ORDER BY id DESC LIMIT 1",
            (attempt.id,),
        ).fetchone()
        config = _config(conn, task.project_id)
        latest = conn.execute(
            "SELECT id FROM verifications WHERE task_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (task.id,),
        ).fetchone()
        return bool(
            task.status == "verifying" and task.version == verification.task_version
            and task.project_id == verification.project_id and attempt.task_id == task.id
            and attempt.status == "finished" and attempt.attempt_no == verification.attempt_no
            and brief.task_version == verification.dispatch_task_version
            and brief.brief_digest == verification.brief_digest
            and report is not None and int(report[0]) == verification.worker_report_id
            and str(report[1]) == task.id and int(report[2]) == verification.dispatch_task_version
            and str(report[3]) == verification.brief_digest
            and str(report[4]) == verification.worker_report_digest and str(report[5]) == "completed"
            and int(config[1]) == verification.config_version
            and str(config[2]) == verification.rules_version
            and str(config[5]) == verification.config_digest
            and latest is not None and str(latest[0]) == verification.id
        )
    except (ValidationError, NotFoundError, IllegalTransitionError):
        return False


def run_verification(
    conn: storage.TeamCraftConnection, attempt_id: str, *, idempotency_key: str,
    lease: leader.Lease, id_factory: IdFactory | None = None, clock: Clock | None = None,
) -> Verification:
    """Run the current approved argv outside SQLite, then fence its report commit."""
    factory = id_factory or DEFAULT_ID_FACTORY
    with _tx(conn):
        task, attempt, brief, report_id, dispatch_task_version, report_digest = _bound_facts(conn, attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        if task.kind != "scout":
            raise IllegalTransitionError("ship verification waits for M4 Worktree/Commit/Diff evidence", entity="task", current=task.status, event="verification_run", reason_code="M4_EVIDENCE_REQUIRED")
        config_row = _config(conn, task.project_id)
        config = _config_value(config_row)
        evidence_digest = _digest(_json({"brief_digest": brief.brief_digest,
            "dispatch_task_version": dispatch_task_version, "worker_report_id": report_id,
            "config_digest": config["config_digest"], "worker_report_digest": report_digest}).encode())
        payload = {"project_id": task.project_id, "task_id": task.id, "task_version": task.version,
            "dispatch_task_version": dispatch_task_version, "worker_report_id": report_id,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no, "brief_digest": brief.brief_digest,
            "worker_report_digest": report_digest, "config_version": config["config_version"],
            "rules_version": config["rules_version"], "config_digest": config["config_digest"]}
        evidence = {"task_id": task.id, "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no, "digest": evidence_digest}
        unresolved = conn.execute(
            "SELECT id, idempotency_key, state, payload_json FROM commands "
            "WHERE kind='verification.run' AND state IN ('pending','running','unknown') ORDER BY requested_at",
        ).fetchall()
        for row in unresolved:
            try:
                prior_payload = json.loads(str(row[3]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ConflictError("an unresolved Verification Command has malformed payload") from exc
            if prior_payload.get("attempt_id") == attempt.id and str(row[1]) != idempotency_key:
                raise ConflictError("this Attempt has an unresolved Verification Command; inspect/reconcile it before another run")
        command = commands.submit_command(conn, idempotency_key=idempotency_key,
            kind="verification.run", payload_json=_json(payload), evidence_json=_json(evidence), clock=clock).command
        prior = conn.execute(f"SELECT {_VERIFICATION_COLUMNS} FROM verifications WHERE command_id=?", (command.id,)).fetchone()
        if prior is not None: return _verification(prior)
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError("Verification Command is already running/uncertain; reconcile it before retry", entity="command", current=command.state, event="verification_run")
        repo_path = domain.get_project(conn, task.project_id).repo_path
        run_id = str(factory.new("verification"))
    # No shell and no SQLite transaction around the approved external argv.
    status, exit_code, stdout, stderr, timed_out = "error", None, b"", b"", False
    truncated = False
    cleanup_error: str | None = None
    try:
        exit_code, stdout, stderr, timed_out, truncated, cleanup_error = _run_bounded(
            config["argv"], cwd=repo_path, timeout_seconds=config["timeout_seconds"])
        status = "error" if timed_out or truncated or cleanup_error else ("pass" if exit_code == 0 else "fail")
    except Exception as exc:
        stderr = str(exc).encode("utf-8", errors="replace")
    artifact_obj = {"argv": config["argv"], "cwd": repo_path, "exit_code": exit_code,
        "timed_out": timed_out, "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"), "truncated": truncated}
    if truncated:
        artifact_obj["error"] = f"combined stdout/stderr exceeded {MAX_CAPTURE_BYTES} bytes; verifier was terminated"
    elif timed_out:
        artifact_obj["error"] = f"verifier exceeded timeout of {config['timeout_seconds']} seconds; process was terminated"
    elif cleanup_error:
        artifact_obj["error"] = cleanup_error
    artifact = _json(artifact_obj).encode("utf-8")
    if len(artifact) > MAX_ARTIFACT_BYTES:
        artifact = _json({"argv": config["argv"], "cwd": repo_path, "exit_code": exit_code,
            "timed_out": timed_out, "truncated": truncated,
            "error": cleanup_error or "serialized verifier artifact exceeded size limit",
            "output_omitted": True}).encode("utf-8")
        status = "error"
    artifact_digest = _digest(artifact)
    try:
        with _tx(conn):
            # A verifier may finish after losing the Leader. Its result is not
            # authoritative and must not be persisted as PASS by a stale holder.
            leader.require_project_lease(conn, lease, task.project_id, clock=clock)
            current_task = domain.get_task(conn, task.id)
            current_attempt = domain.get_attempt(conn, attempt.id)
            current_brief = briefs.get_attempt_brief(conn, attempt.id)
            report_row = conn.execute(
                "SELECT id, task_id, task_version, brief_digest, report_digest, outcome "
                "FROM worker_reports WHERE attempt_id=? AND report_kind='final' ORDER BY id DESC LIMIT 1",
                (attempt.id,),
            ).fetchone()
            current_config = _config(conn, task.project_id)
            fresh = (current_task.version == task.version and current_task.status == "verifying"
                and current_attempt.status == "finished" and current_attempt.attempt_no == attempt.attempt_no
                and current_brief.brief_digest == brief.brief_digest
                and current_brief.task_version == dispatch_task_version
                and report_row is not None and int(report_row[0]) == report_id
                and str(report_row[1]) == task.id and int(report_row[2]) == dispatch_task_version
                and str(report_row[3]) == brief.brief_digest and str(report_row[4]) == report_digest
                and str(report_row[5]) == "completed"
                and int(current_config[1]) == config["config_version"] and str(current_config[5]) == config["config_digest"])
            if not fresh:
                status = "stale"
            conn.execute(
                "INSERT INTO verifications (id, command_id, project_id, task_id, attempt_id, attempt_no, task_version, dispatch_task_version, brief_digest, worker_report_id, worker_report_digest, config_version, rules_version, config_digest, argv_json, status, exit_code, artifact, artifact_digest, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, command.id, task.project_id, task.id, attempt.id, attempt.attempt_no,
                 task.version, dispatch_task_version, brief.brief_digest, report_id, report_digest,
                 config["config_version"], config["rules_version"],
                 config["config_digest"], _json(config["argv"]), status, exit_code, artifact, artifact_digest, _now(clock)),
            )
            events.append_event(conn, "verification.recorded", project_id=task.project_id,
                task_id=task.id, attempt_id=attempt.id,
                payload_json=_json({"verification_id": run_id, "command_id": command.id, "status": status,
                    "artifact_digest": artifact_digest, "config_version": config["config_version"], "rules_version": config["rules_version"]}), clock=clock)
            commands.confirm_command(conn, command.id, result_json=_json({"verification_id": run_id,
                "status": status, "artifact_digest": artifact_digest}), clock=clock)
            return get_verification(conn, run_id)
    except Exception as exc:
        # The external verifier has already returned, but no authoritative
        # durable result committed. UNKNOWN blocks replay until inspection.
        detail = str(exc).encode("utf-8")[:500].decode("utf-8", errors="ignore")
        try:
            commands.mark_command_unknown(conn, command.id,
                note=f"Verifier returned but result commit failed: {detail}", clock=clock)
        except Exception:
            pass
        raise


def _approval(row: Any, *, current: bool) -> Approval:
    return Approval(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]),
        int(row[6]), str(row[8]), str(row[12]), str(row[13]), str(row[14]), str(row[15]), str(row[16]), current)


def _approval_current(conn: storage.TeamCraftConnection, row: Any) -> bool:
    try:
        attempt = domain.get_attempt(conn, str(row[4]))
        task = domain.get_task(conn, attempt.task_id)
        brief = briefs.get_attempt_brief(conn, attempt.id)
        report = conn.execute(
            "SELECT id, task_id, task_version, brief_digest, report_digest, outcome "
            "FROM worker_reports WHERE attempt_id=? AND report_kind='final' ORDER BY id DESC LIMIT 1",
            (attempt.id,),
        ).fetchone()
        config = _config(conn, task.project_id)
        verification = get_verification(conn, str(row[2]))
        if report is None:
            return False
        report_id, report_task_id, report_dispatch_version = int(report[0]), str(report[1]), int(report[2])
        report_digest = str(report[4])
        decision = str(row[13])
    except (ValidationError, NotFoundError, IllegalTransitionError):
        return False
    # A human acceptance advances Task version once to DONE; a rejection does
    # not. Any later Task mutation invalidates either decision. Brief and
    # WorkerReport versions remain pinned to dispatch and are checked
    # separately from this completion-time version.
    accepted = decision == "accept"
    expected_status = "done" if accepted else "verifying"
    expected_task_version = int(row[6]) + (1 if accepted else 0)
    latest = conn.execute("SELECT id FROM verifications WHERE task_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (task.id,)).fetchone()
    return (task.status == expected_status and task.version == expected_task_version
        and attempt.status == "finished" and attempt.task_id == task.id
        and report_task_id == task.id and report_dispatch_version == brief.task_version
        and str(report[3]) == brief.brief_digest and str(report[5]) == "completed"
        and verification.project_id == task.project_id and verification.task_id == task.id
        and verification.attempt_id == attempt.id and verification.attempt_no == attempt.attempt_no
        and attempt.attempt_no == int(row[5]) and attempt.status == "finished"
        and brief.task_version == verification.dispatch_task_version
        and brief.brief_digest == str(row[7]) == verification.brief_digest
        and report_id == verification.worker_report_id and report_digest == verification.worker_report_digest
        and latest is not None and str(latest[0]) == verification.id
        and (task.kind == "scout" if accepted else True)
        and (verification.status == "pass" if accepted else True)
        and verification.artifact_digest == str(row[8])
        and verification.task_version == int(row[6]) and verification.config_version == int(row[9])
        and verification.rules_version == str(row[10]) and verification.config_digest == str(row[11])
        and int(config[1]) == int(row[9]) and str(config[2]) == str(row[10]) and str(config[5]) == str(row[11]))


def decide_approval(
    conn: storage.TeamCraftConnection, verification_id: str, *, decision: str,
    actor: str, reason: str, idempotency_key: str, lease: leader.Lease,
    clock: Clock | None = None,
) -> Approval:
    """Append an idempotent decision and CAS-accept only a current scout report."""
    if decision not in {"accept", "reject"}: raise ValidationError("decision must be accept or reject")
    if (not isinstance(actor, str) or not actor.strip() or len(actor.encode("utf-8")) > 1024
            or not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 1024):
        raise ValidationError("actor and reason are required and must each be at most 1024 bytes")
    with _tx(conn):
        verification = get_verification(conn, verification_id)
        existing = conn.execute(
            "SELECT a.id, a.command_id, a.verification_id, a.task_id, a.attempt_id, a.attempt_no, a.task_version, a.brief_digest, a.artifact_digest, a.config_version, a.rules_version, a.config_digest, a.proposed_action, a.decision, a.actor, a.reason, a.created_at, c.idempotency_key "
            "FROM approvals a JOIN commands c ON c.id=a.command_id WHERE a.verification_id=?",
            (verification.id,),
        ).fetchone()
        if existing is not None:
            if (str(existing[17]) != idempotency_key or str(existing[13]) != decision
                    or str(existing[14]) != actor.strip() or str(existing[15]) != reason.strip()):
                raise ConflictError("this Verification already has a different Approval decision")
            return _approval(existing[:17], current=_approval_current(conn, existing[:17]))
        task, attempt, brief, report_id, dispatch_task_version, report_digest = _bound_facts(conn, verification.attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        latest = conn.execute("SELECT id FROM verifications WHERE task_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (task.id,)).fetchone()
        if latest is None or str(latest[0]) != verification.id:
            raise ConflictError("Approval must bind the latest Verification for this Task")
        if verification.project_id != task.project_id or verification.task_id != task.id:
            raise ConflictError("Verification belongs to a different Task/Project")
        if task.kind != "scout" and decision == "accept":
            raise IllegalTransitionError("ship Tasks require M4 Worktree/Commit/Diff evidence; report acceptance cannot complete them", entity="task", current=task.status, event="report_accepted", reason_code="M4_EVIDENCE_REQUIRED")
        current_config = _config(conn, task.project_id)
        payload = {"task_id": task.id, "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no, "dispatch_task_version": dispatch_task_version,
            "worker_report_id": report_id, "brief_digest": brief.brief_digest,
            "verification_id": verification.id, "artifact_digest": verification.artifact_digest,
            "config_version": verification.config_version, "rules_version": verification.rules_version,
            "config_digest": verification.config_digest, "proposed_action": "accept_report",
            "decision": decision, "actor": actor.strip(), "reason": reason.strip()}
        evidence_digest = _digest(_json({key: payload[key] for key in (
            "brief_digest", "verification_id", "worker_report_id", "dispatch_task_version",
            "artifact_digest", "config_version",
            "rules_version", "config_digest", "proposed_action")}).encode())
        evidence = {"task_id": task.id, "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no, "digest": evidence_digest}
        command = commands.submit_command(conn, idempotency_key=idempotency_key,
            kind="verification.approval", payload_json=_json(payload), evidence_json=_json(evidence), clock=clock).command
        if task.status != "verifying":
            raise IllegalTransitionError("Approval requires Task status VERIFYING", entity="task", current=task.status, event="approval")
        if verification.status != "pass" and decision == "accept":
            raise IllegalTransitionError("only a passing Verification can be accepted", entity="verification", current=verification.status, event="approval")
        if (verification.task_version != task.version or verification.attempt_no != attempt.attempt_no
                or verification.dispatch_task_version != dispatch_task_version
                or verification.worker_report_id != report_id
                or verification.brief_digest != brief.brief_digest or verification.worker_report_digest != report_digest
                or int(current_config[1]) != verification.config_version or str(current_config[5]) != verification.config_digest):
            raise ConflictError("Verification evidence/configuration is stale")
        if command.state == "pending": commands.claim_command(conn, command.id, clock=clock)
        approval_id = commands.DEFAULT_ID_FACTORY.new("approval")
        created = _now(clock)
        conn.execute(
            "INSERT INTO approvals (id, command_id, verification_id, project_id, task_id, attempt_id, attempt_no, task_version, brief_digest, artifact_digest, config_version, rules_version, config_digest, proposed_action, decision, actor, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(approval_id), command.id, verification.id, task.project_id, task.id, attempt.id,
             attempt.attempt_no, task.version, brief.brief_digest, verification.artifact_digest,
             verification.config_version, verification.rules_version, verification.config_digest,
             "accept_report", decision, actor.strip(), reason.strip(), created),
        )
        if decision == "accept":
            if task.kind != "scout": raise IllegalTransitionError("only scout/report-only can be accepted in M2", entity="task", current=task.status, event="report_accepted")
            updated = domain.apply_task_event(conn, task.id, "report_accepted", expected_version=task.version, clock=clock)
        else:
            updated = task
        result = {"approval_id": str(approval_id), "verification_id": verification.id,
            "decision": decision, "task_status": updated.status, "task_version": updated.version}
        commands.confirm_command(conn, command.id, result_json=_json(result), clock=clock)
        event_kind = "verification.approval_accepted" if decision == "accept" else "verification.approval_rejected"
        events.append_event(conn, event_kind,
            project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
            payload_json=_json({**result, "actor": actor.strip(), "reason": reason.strip(), "evidence": evidence}), clock=clock)
        row = conn.execute(
            "SELECT id, command_id, verification_id, task_id, attempt_id, attempt_no, task_version, brief_digest, artifact_digest, config_version, rules_version, config_digest, proposed_action, decision, actor, reason, created_at FROM approvals WHERE id=?",
            (str(approval_id),),
        ).fetchone()
        return _approval(row, current=True)


def list_verifications(conn: storage.TeamCraftConnection, task_id: str) -> list[Verification]:
    rows = conn.execute(f"SELECT {_VERIFICATION_COLUMNS} FROM verifications WHERE task_id=? ORDER BY created_at, id", (require_entity_id(task_id),)).fetchall()
    return [_verification(row) for row in rows]


def list_approvals(conn: storage.TeamCraftConnection, task_id: str) -> list[Approval]:
    columns = ("id, command_id, verification_id, task_id, attempt_id, attempt_no, task_version, "
        "brief_digest, artifact_digest, config_version, rules_version, config_digest, proposed_action, "
        "decision, actor, reason, created_at")
    rows = conn.execute(f"SELECT {columns} FROM approvals WHERE task_id=? ORDER BY created_at, id",
                        (require_entity_id(task_id),)).fetchall()
    return [_approval(row, current=_approval_current(conn, row)) for row in rows]


def list_verification_commands(conn: storage.TeamCraftConnection, task_id: str) -> list[dict[str, Any]]:
    """Expose pending/uncertain verifier command state for safe recovery."""
    task_id = require_entity_id(task_id)
    rows = conn.execute(
        "SELECT id, state, payload_json, note FROM commands WHERE kind='verification.run' ORDER BY requested_at, id"
    ).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(str(row[2]))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            result.append({"command_id": str(row[0]), "state": str(row[1]),
                           "attempt_id": payload.get("attempt_id"), "note": row[3]})
    return result


# ===========================================================================
# M4-03 — Worktree Verifier enhancement (M0 review §9.4 M4-03)
#
# Bounded question this section answers, fail-closed:
#
#   "For one finished Attempt of a registered, identity-verified worktree,
#    did the APPROVED verifier profile actually run — in exactly that
#    workspace, with pinned cwd/env/timeout/argv, with the complete M4-01
#    Diff Gate ok before and after — and is the bounded log artifact durably
#    preserved under the configured artifact root so a PASS here is evidence,
#    while anything else (nonzero exit, timeout, crash, missing command,
#    out-of-scope change) can never read as PASS/DONE?"
#
# Pinned decisions:
#
# * **Worker completion only triggers verification.** A run binds the exact
#   same facts as M2-06 :func:`_bound_facts`: a ``finished`` Attempt, a
#   durable final WorkerReport with outcome ``completed``, and Task status
#   VERIFYING (i.e. exactly after ``worker_reported_done``). An attempt that
#   merely started, never reported, or reported blocked/failed cannot be
#   "verified" into anything.
# * **Exact registered identity.** Execution happens only after
#   :func:`teamcraft.worktrees.claim_worktree` adopts the EXACT recorded
#   path/branch/repository/markers (M4-02). Identity is re-verified after the
#   run; drift makes the result ERROR, never PASS.
# * **Pinned execution.** cwd = realpath of the allocated worktree; env = a
#   freshly built minimal environment (ambient secrets never leak); timeout
#   and argv come verbatim from the current approved profile config and are
#   persisted as the real argv.
# * **Diff Gate before AND after.** Violations either side (or HEAD moving
#   mid-run) are ERROR and the verifier is not even started (before-case).
# * **Bounded artifact under the configured artifact root.** The report is
#   written as a file relative to the artifact root; every path component is
#   validated, containment is verified through ``realpath`` before AND after
#   the write, and the stored ``log_relpath``/digests can never escape the
#   root (also pinned by a schema-v17 storage trigger).
# * **One settled verdict per Attempt+profile.** A recorded verification (any
#   status) is never re-run and never duplicated; a fix must arrive as a new
#   bounded Attempt (retry_approved -> dispatch -> new allocation). Failed,
#   errored and stale verdicts move the Task to NEEDS_HUMAN via the existing
#   ``verification_failed`` edge, so the only forward path is a human-approved
#   retry. PASS/SKIPPED leave the Task VERIFYING (delivery evidence and
#   acceptance are M4-04, not this milestone).
# ===========================================================================

VERIFIER_PROFILES: tuple[str, ...] = ("lint", "test", "typecheck", "secret_scan")
WORKTREE_VERIFICATION_STATUSES: tuple[str, ...] = ("pass", "fail", "error", "skipped", "stale")
WORKTREE_SUITE_KIND = "verification.worktree_suite"
WORKTREE_RUN_KIND = "verification.worktree_run"
WORKTREE_VERIFICATION_EVENT_KIND = "verification.worktree_recorded"
#: Default artifact-root namespace inside the project runtime directory
#: (``.teamcraft/`` is already excluded by project .gitignore conventions).
WORKTREE_ARTIFACT_DIRNAME = "verifications"
MAX_WORKTREE_REPORT_BYTES = 256 * 1024
MAX_EMBED_OUTPUT_BYTES = 64 * 1024
_MAX_GATE_VIOLATIONS_RECORDED = 10

#: Characters safe for artifact path segments (same vocabulary as the M4-02
#: resource-naming rule); Attempt/verification IDs that cannot be used safely
#: in a path are refused at this boundary, fail-closed.
_SAFE_SEGMENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: The pinned child environment. Built fresh on every run: NO ambient variable
#: is inherited, so tokens/secrets that happen to sit in the caller's env can
#: never reach an approved verifier command.
_PINNED_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "GIT_CONFIG_NOSYSTEM",
                   "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "TEAMCRAFT_ROLE")


@dataclass(frozen=True)
class VerifierProfile:
    """One durable, human-approved profile policy for one project."""

    id: str
    command_id: str
    project_id: str
    profile: str
    profile_version: int
    enabled: bool
    argv: tuple[str, ...]
    timeout_seconds: int
    config_digest: str
    approved_by: str
    idempotency_key: str
    created_at: str


@dataclass(frozen=True)
class WorktreeVerification:
    """One immutable per-profile worktree verification result + artifact."""

    id: str
    command_id: str
    project_id: str
    task_id: str
    attempt_id: str
    attempt_no: int
    task_version: int
    dispatch_task_version: int
    brief_digest: str
    worker_report_id: int
    worker_report_digest: str
    allocation_id: str
    worktree_path: str
    branch: str
    base_sha: str
    claims: tuple[str, ...]
    profile: str
    profile_version: int | None
    config_digest: str | None
    argv: tuple[str, ...] | None
    status: str
    exit_code: int | None
    report: bytes
    report_digest: str
    log_relpath: str | None
    log_digest: str | None
    created_at: str


def _safe_segment(entity: str, value: str) -> str:
    value = require_entity_id(value)
    if not value or any(char not in _SAFE_SEGMENT_CHARS for char in value):
        raise ValidationError(
            f"{entity} {value!r} cannot be used as an artifact path segment "
            "(letters, digits, '.', '_' and '-' only)")
    return value


def default_artifact_root(repo_root: str) -> str:
    """Default configured artifact root: ``<repo>/.teamcraft/verifications``."""
    return os.path.join(repo_root, ".teamcraft", WORKTREE_ARTIFACT_DIRNAME)


def _ensure_artifact_root(artifact_root: str) -> str:
    """Validate/create the configured artifact root; return its realpath.

    An absolute path is required (the containment contract is anchored at the
    real root). ``.git`` spellings are refused; creation failures propagate.
    """
    if not isinstance(artifact_root, str) or not artifact_root.strip():
        raise ValidationError("artifact root must be a non-blank string")
    if not os.path.isabs(artifact_root):
        raise ValidationError("artifact root must be an absolute path")
    if os.path.basename(os.path.normpath(artifact_root)) == ".git":
        raise ValidationError("artifact root must not be a '.git' directory")
    real = os.path.realpath(artifact_root)
    if os.path.exists(real) and not os.path.isdir(real):
        raise ValidationError(f"artifact root exists but is not a directory: {artifact_root!r}")
    os.makedirs(real, exist_ok=True)
    return real


def _contained_artifact_path(root_real: str, relpath: str) -> str:
    """Resolve ``relpath`` under ``root_real`` or refuse (never escapes).

    Every component is naming-safe (no ``..``, dot, absolute, backslash, NUL,
    space, or unicode whitespace), symlinked ancestors must stay inside the
    root (M4-01 :func:`teamcraft.workspaces.resolve_actual_path`), and the
    final realpath is re-checked for containment.
    """
    parts = relpath.split("/")
    if not parts or any(not part for part in parts):
        raise ValidationError(f"artifact relpath {relpath!r} has empty segments")
    for part in parts:
        if part in (".", "..") or any(char not in _SAFE_SEGMENT_CHARS for char in part):
            raise ValidationError(
                f"artifact relpath {relpath!r} contains unsafe segment {part!r}")
    actual = workspaces.resolve_actual_path(root_real, relpath)
    final = os.path.realpath(os.path.join(root_real, *parts))
    prefix = root_real.rstrip("/") + "/"
    if not (final == root_real or final.startswith(prefix)):
        raise ValidationError(f"artifact path {relpath!r} escapes the artifact root")
    if actual.case_alias:
        raise ValidationError(f"artifact path {relpath!r} is an unsafe case alias")
    return final


def _write_artifact(root_real: str, relpath: str, payload: bytes) -> str:
    """Write ``payload`` exclusively under the root; return sha256 of the file.

    ``O_EXCL`` makes overwriting (even of one's own previous file) impossible;
    the file is read back and digest-checked, and containment is re-verified
    after the write (a directory swapped in mid-flight is caught).
    """
    target = _contained_artifact_path(root_real, relpath)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    # The parent chain was rebuilt after the containment check; re-validate
    # through realpath before opening, and again from the opened file after.
    if os.path.realpath(parent) != parent or not parent.startswith(root_real.rstrip("/") + "/"):
        raise ValidationError(f"artifact parent escapes the artifact root: {parent!r}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    final = os.path.realpath(target)
    prefix = root_real.rstrip("/") + "/"
    if not final.startswith(prefix):
        raise ValidationError(f"written artifact escaped the artifact root: {final!r}")
    with open(final, "rb") as handle:
        content = handle.read()
    if len(content) != len(payload) or content != payload:
        raise StorageError("artifact file read-back does not match the written payload")
    return _digest(content)


def _pinned_env(worktree_path: str) -> dict[str, str]:
    """Fresh minimal environment for one verifier run (nothing inherited)."""
    # Keep bare approved commands practical without trusting the caller's
    # PATH.  The running interpreter directory supports ``python3``-adjacent
    # tools from the selected runtime; the remaining entries are a fixed,
    # reviewable set of conventional system package locations.
    interpreter_dir = os.path.dirname(os.path.realpath(sys.executable))
    path_entries = [
        interpreter_dir, "/usr/local/bin", "/opt/homebrew/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    pinned_path = os.pathsep.join(dict.fromkeys(path_entries))
    return {
        "PATH": pinned_path,
        "HOME": worktree_path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "TEAMCRAFT_ROLE": "verifier",
    }


def _pinned_run_git(
    args: Sequence[str], cwd: str,
    timeout: float = workspaces.GIT_TIMEOUT_SECONDS,
) -> bytes:
    """Run M4-03 Git checks without inheriting the caller environment."""
    command = ["git", "--no-pager", *args]
    try:
        proc = subprocess.run(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, env=_pinned_env(cwd),
        )
    except FileNotFoundError as exc:
        raise workspaces.GitStateError(
            "git executable is not available; failing closed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise workspaces.GitStateError(
            f"git {' '.join(args[:2])} timed out after {timeout}s; failing closed"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:400]
        raise workspaces.GitStateError(
            f"git {' '.join(args[:2])} failed (exit {proc.returncode}): {detail}"
        )
    return proc.stdout


def _profile_config(conn: storage.TeamCraftConnection, project_id: str, profile: str):
    row = conn.execute(
        "SELECT id, command_id, project_id, profile, profile_version, enabled, argv_json, "
        "timeout_seconds, config_digest, approved_by, idempotency_key, created_at "
        "FROM verifier_profiles WHERE project_id=? AND profile=? "
        "ORDER BY profile_version DESC LIMIT 1",
        (project_id, profile),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"project has no explicitly approved verifier profile {profile!r}")
    return row


def _profile_value(row: Any) -> VerifierProfile:
    return VerifierProfile(
        id=str(row[0]), command_id=str(row[1]), project_id=str(row[2]),
        profile=str(row[3]), profile_version=int(row[4]), enabled=bool(row[5]),
        argv=tuple(json.loads(str(row[6]))), timeout_seconds=int(row[7]),
        config_digest=str(row[8]), approved_by=str(row[9]),
        idempotency_key=str(row[10]), created_at=str(row[11]))


def configure_verifier_profile(
    conn: storage.TeamCraftConnection, project_id: str, *, profile: str,
    argv: Sequence[str], timeout_seconds: int, actor: str, idempotency_key: str,
    lease: leader.Lease, enabled: bool = True,
    id_factory: IdFactory | None = None, clock: Clock | None = None,
) -> dict[str, Any]:
    """Append an explicitly human-approved argv policy for one profile.

    ``enabled=False`` records an explicit disable (profile runs then produce
    SKIPPED, never PASS).  Versions are per (project, profile); replay of the
    identical request is idempotent, any different content under the same key
    conflicts.
    """
    project_id = require_entity_id(project_id)
    if profile not in VERIFIER_PROFILES:
        raise ValidationError(
            f"unknown verifier profile {profile!r}; supported: "
            + ", ".join(VERIFIER_PROFILES))
    if not isinstance(enabled, bool):
        raise ValidationError("enabled must be a boolean")
    argv_tuple = _validate_argv(argv)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
        raise ValidationError("timeout_seconds must be an integer in [1, 3600]")
    if not isinstance(actor, str) or not actor.strip() or len(actor.encode("utf-8")) > 1024:
        raise ValidationError("actor is required and must be at most 1024 bytes")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key.strip()) > 236:
        raise ValidationError("idempotency_key is required and must be at most 236 characters")
    argv_json = _json(list(argv_tuple))
    factory = id_factory or DEFAULT_ID_FACTORY
    with _tx(conn):
        project = domain.get_project(conn, project_id)
        leader.require_project_lease(conn, lease, project.id, clock=clock)
        proposed_digest = _digest(_json({
            "profile": profile, "enabled": enabled, "argv": list(argv_tuple),
            "timeout_seconds": timeout_seconds}).encode())
        prior = conn.execute(
            "SELECT id, command_id, project_id, profile, profile_version, enabled, argv_json, "
            "timeout_seconds, config_digest, approved_by, idempotency_key, created_at "
            "FROM verifier_profiles WHERE idempotency_key=?",
            (idempotency_key.strip(),),
        ).fetchone()
        if prior is not None:
            if (str(prior[2]) != project.id or str(prior[3]) != profile
                    or str(prior[6]) != argv_json or int(prior[7]) != timeout_seconds
                    or int(prior[5]) != int(enabled) or str(prior[8]) != proposed_digest
                    or str(prior[9]) != actor.strip()):
                raise ConflictError("verifier profile idempotency key has different content")
            value = _profile_value(prior)
            return {"id": value.id, "project_id": value.project_id, "profile": value.profile,
                    "profile_version": value.profile_version, "enabled": value.enabled,
                    "argv": list(value.argv), "timeout_seconds": value.timeout_seconds,
                    "config_digest": value.config_digest, "approved_by": value.approved_by,
                    "created_at": value.created_at}
        current = conn.execute(
            "SELECT COALESCE(MAX(profile_version),0) FROM verifier_profiles "
            "WHERE project_id=? AND profile=?", (project.id, profile),
        ).fetchone()
        version = int(current[0]) + 1
        profile_id = str(factory.new("verifier_profile"))
        created = _now(clock)
        body = {"project_id": project.id, "profile": profile, "profile_version": version,
                "enabled": enabled, "argv": list(argv_tuple),
                "timeout_seconds": timeout_seconds, "config_digest": proposed_digest,
                "approved_by": actor.strip()}
        command = commands.submit_command(
            conn, idempotency_key=f"verifier-profile:{idempotency_key.strip()}",
            kind="verifier.profile_configure", payload_json=_json(body),
            evidence_json="{}", clock=clock).command
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise ConflictError("verifier profile Command exists without its durable profile row")
        conn.execute(
            "INSERT INTO verifier_profiles (id, command_id, project_id, profile, profile_version,"
            " enabled, argv_json, timeout_seconds, config_digest, approved_by, idempotency_key, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (profile_id, command.id, project.id, profile, version, 1 if enabled else 0,
             argv_json, timeout_seconds, proposed_digest, actor.strip(),
             idempotency_key.strip(), created),
        )
        events.append_event(conn, "verifier.profile_configured", project_id=project.id,
            payload_json=_json({**body, "profile_id": profile_id, "command_id": command.id}),
            clock=clock)
        commands.confirm_command(conn, command.id,
            result_json=_json({"profile_id": profile_id, "profile_version": version}),
            clock=clock)
        return {"id": profile_id, "project_id": project.id, "profile": profile,
                "profile_version": version, "enabled": enabled,
                "argv": list(argv_tuple), "timeout_seconds": timeout_seconds,
                "config_digest": proposed_digest, "approved_by": actor.strip(),
                "created_at": created}


def list_verifier_profiles(conn: storage.TeamCraftConnection,
                          project_id: str) -> list[VerifierProfile]:
    rows = conn.execute(
        "SELECT id, command_id, project_id, profile, profile_version, enabled, argv_json, "
        "timeout_seconds, config_digest, approved_by, idempotency_key, created_at "
        "FROM verifier_profiles WHERE project_id=? ORDER BY profile, profile_version",
        (require_entity_id(project_id),),
    ).fetchall()
    return [_profile_value(row) for row in rows]


_WORKTREE_COLUMNS = (
    "id, command_id, project_id, task_id, attempt_id, attempt_no, task_version, "
    "dispatch_task_version, brief_digest, worker_report_id, worker_report_digest, "
    "allocation_id, worktree_path, branch, base_sha, claims_json, profile, "
    "profile_version, config_digest, argv_json, status, exit_code, report_json, "
    "report_digest, log_relpath, log_digest, created_at"
)


def _worktree_verification(row: Any) -> WorktreeVerification:
    return WorktreeVerification(
        id=str(row[0]), command_id=str(row[1]), project_id=str(row[2]),
        task_id=str(row[3]), attempt_id=str(row[4]), attempt_no=int(row[5]),
        task_version=int(row[6]), dispatch_task_version=int(row[7]),
        brief_digest=str(row[8]), worker_report_id=int(row[9]),
        worker_report_digest=str(row[10]), allocation_id=str(row[11]),
        worktree_path=str(row[12]), branch=str(row[13]), base_sha=str(row[14]),
        claims=tuple(json.loads(str(row[15]))), profile=str(row[16]),
        profile_version=None if row[17] is None else int(row[17]),
        config_digest=None if row[18] is None else str(row[18]),
        argv=None if row[19] is None else tuple(json.loads(str(row[19]))),
        status=str(row[20]),
        exit_code=None if row[21] is None else int(row[21]),
        report=str(row[22]).encode("utf-8"), report_digest=str(row[23]),
        log_relpath=None if row[24] is None else str(row[24]),
        log_digest=None if row[25] is None else str(row[25]),
        created_at=str(row[26]))


def get_worktree_verification(conn: storage.TeamCraftConnection,
                              verification_id: str) -> WorktreeVerification:
    row = conn.execute(
        f"SELECT {_WORKTREE_COLUMNS} FROM worktree_verifications WHERE id=?",
        (require_entity_id(verification_id),),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"worktree verification not found: {verification_id}")
    return _worktree_verification(row)


def list_worktree_verifications(conn: storage.TeamCraftConnection,
                               attempt_id: str) -> list[WorktreeVerification]:
    rows = conn.execute(
        f"SELECT {_WORKTREE_COLUMNS} FROM worktree_verifications WHERE attempt_id=?"
        " ORDER BY created_at, id", (require_entity_id(attempt_id),),
    ).fetchall()
    return [_worktree_verification(row) for row in rows]


def _gate_snapshot(state: workspaces.WorkspaceState | None,
                  claims: Sequence[str],
                  violations: Sequence[str] | None) -> dict[str, Any] | None:
    """Bounded, digestible rendering of one Diff Gate observation."""
    if state is None:
        return None
    rendered = [{"path": workspaces._display(entry.path),
                 "staged": entry.staged, "unstaged": entry.unstaged,
                 "untracked": entry.untracked, "added": entry.added,
                 "deleted": entry.deleted, "renamed": entry.renamed,
                 "rename_source": (workspaces._display(entry.rename_source)
                                  if entry.rename_source else None),
                 "rename_target": (workspaces._display(entry.rename_target)
                                  if entry.rename_target else None)}
                for entry in state.changes[:200]]
    rendered.sort(key=_json)
    violations = list(violations or ())
    return {
        "ok": not violations,
        "base_sha": state.base_sha,
        "branch": state.branch,
        "change_count": len(state.changes),
        "violations": [workspaces._display(v) for v in violations[:_MAX_GATE_VIOLATIONS_RECORDED]],
        "violation_count": len(violations),
        "violations_truncated": len(violations) > _MAX_GATE_VIOLATIONS_RECORDED,
        "claims": list(claims),
        "changes_digest": hashlib.sha256(_json(rendered).encode("utf-8")).hexdigest(),
    }


def _run_gate(worktree_path: str, claims: Sequence[str],
              run_git: Any) -> tuple[dict[str, Any] | None, str | None]:
    """One full M4-01 Diff Gate inside the worktree; (snapshot, error)."""
    try:
        result = workspaces.run_diff_gate(worktree_path, claims, run_git=run_git)
    except (workspaces.GitStateError, workspaces.WorkspacePathError) as exc:
        return None, f"diff gate refused: {exc}"
    return {"ok": result.ok, "base_sha": result.base_sha, "branch": result.branch,
            "change_count": len(result.changes),
            "violations": [workspaces._display(v)
                          for v in result.violations[:_MAX_GATE_VIOLATIONS_RECORDED]],
            "violation_count": len(result.violations),
            "violations_truncated": len(result.violations) > _MAX_GATE_VIOLATIONS_RECORDED,
            "claims": list(result.claims),
            "changes_digest": hashlib.sha256(_json([
                workspaces._display(entry.path) for entry in result.changes]
            ).encode("utf-8")).hexdigest()}, None


def run_worktree_verifier(
    conn: storage.TeamCraftConnection, attempt_id: str, *, profile: str,
    idempotency_key: str, lease: leader.Lease, artifact_root: str | None = None,
    run_git: Any = None, id_factory: IdFactory | None = None,
    clock: Clock | None = None, _defer_failure_escalation: bool = False,
) -> WorktreeVerification:
    """Run ONE approved verifier profile inside the Attempt's own worktree.

    The command runs OUTSIDE any SQLite transaction with pinned
    cwd/env/timeout/argv; the fenced commit re-checks every binding fact and
    Leader lease (a stale leader's result is recorded ``stale``, never PASS).
    Refusals (nothing persisted, nothing executed):

    * unknown profile / malformed key  -> ValidationError;
    * Attempt not finished, no durable completed WorkerReport, Task not
      VERIFYING -> ValidationError/IllegalTransitionError (worker completion
      only *triggers* verification — it never proves anything);
    * no worktree allocation -> NotFoundError (verification runs only in the
      registered, identity-verified Attempt worktree);
    * no approved profile config -> ValidationError (a missing verifier
      command cannot verify anything, and is never a default PASS);
    * another run of this Attempt+profile already settled -> ConflictError
      ("a fix must arrive as a new bounded Attempt");
    * an unresolved Command for this Attempt+profile -> ConflictError
      (reconcile first).
    """
    if profile not in VERIFIER_PROFILES:
        raise ValidationError(
            f"unknown verifier profile {profile!r}; supported: "
            + ", ".join(VERIFIER_PROFILES))
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    factory = id_factory or DEFAULT_ID_FACTORY
    attempt_id = require_entity_id(attempt_id)
    attempt_segment = _safe_segment("attempt_id", attempt_id)

    with _tx(conn):
        # Replay FIRST: a durable row already owned by this idempotency key is
        # returned verbatim even when live facts have since moved (e.g. the
        # failure escalation CAS advanced the Task version).  Accepted/settled
        # results are never re-submitted, never re-executed, never duplicated;
        # restart/reconcile therefore neither loses nor duplicates them.
        replayed = conn.execute(
            f"SELECT v.{_WORKTREE_COLUMNS.replace(', ', ', v.')} FROM worktree_verifications v"
            " JOIN commands c ON c.id = v.command_id WHERE c.idempotency_key = ?",
            (idempotency_key.strip(),),
        ).fetchone()
        if replayed is not None:
            verification = _worktree_verification(replayed)
            if (verification.attempt_id != attempt_id or verification.profile != profile):
                raise ConflictError(
                    "idempotency key already owns a Worktree Verification for a"
                    " different Attempt/profile; refusing the collision")
            leader.require_project_lease(conn, lease, verification.project_id, clock=clock)
            return verification
        task, attempt, brief, report_id, dispatch_task_version, worker_digest = \
            _bound_facts(conn, attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        allocation = worktrees.require_allocation(conn, attempt.id)
        if (allocation.task_id != task.id or allocation.attempt_id != attempt.id
                or allocation.project_id != task.project_id):
            raise ConflictError("worktree allocation binds a different Task/Attempt/Project")
        try:
            profile_row = _profile_config(conn, task.project_id, profile)
        except NotFoundError as exc:
            raise ValidationError(
                f"project {task.project_id} has no approved verifier configuration for"
                f" profile {profile!r}; a missing verifier command cannot PASS and the"
                " run is refused before any state is written") from exc
        config = _profile_value(profile_row)
        evidence_digest = _digest(_json({
            "brief_digest": brief.brief_digest,
            "dispatch_task_version": dispatch_task_version,
            "worker_report_id": report_id, "worker_report_digest": worker_digest,
            "config_digest": config.config_digest, "allocation_id": allocation.allocation_id,
            "profile": profile}).encode())
        payload = {"project_id": task.project_id, "task_id": task.id,
            "task_version": task.version, "dispatch_task_version": dispatch_task_version,
            "worker_report_id": report_id, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no, "brief_digest": brief.brief_digest,
            "worker_report_digest": worker_digest, "profile": profile,
            "profile_version": config.profile_version, "config_digest": config.config_digest,
            "allocation_id": allocation.allocation_id,
            "worktree_path": allocation.worktree_path, "branch": allocation.branch,
            "base_sha": allocation.base_sha}
        evidence = {"task_id": task.id, "task_version": task.version,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "project_id": task.project_id, "digest": evidence_digest}
        command = commands.submit_command(conn, idempotency_key=idempotency_key.strip(),
            kind=WORKTREE_RUN_KIND, payload_json=_json(payload),
            evidence_json=_json(evidence), clock=clock).command
        prior = conn.execute(
            f"SELECT {_WORKTREE_COLUMNS} FROM worktree_verifications WHERE command_id=?",
            (command.id,),
        ).fetchone()
        if prior is not None:
            # Replay via command convergence: the accepted result is returned
            # exactly once; nothing here re-executes or duplicates it.
            return _worktree_verification(prior)
        settled = conn.execute(
            "SELECT id, status FROM worktree_verifications WHERE attempt_id=? AND profile=?"
            " ORDER BY created_at, id", (attempt.id, profile),
        ).fetchall()
        if settled:
            detail = ", ".join(f"{row[0]}={row[1]}" for row in settled)
            raise ConflictError(
                f"attempt {attempt.id} profile {profile!r} already has settled Worktree"
                f" Verification(s) [{detail}]; a settled verdict is never re-run or"
                " duplicated — a fix must arrive as a NEW bounded Attempt"
                " (retry_approved -> dispatch), never another verification of this"
                " finished Attempt")
        unresolved = conn.execute(
            "SELECT id, idempotency_key FROM commands WHERE kind=? AND state IN"
            " ('pending','running','unknown') ORDER BY requested_at",
            (WORKTREE_RUN_KIND,),
        ).fetchall()
        for row in unresolved:
            prior_payload_row = conn.execute(
                "SELECT payload_json FROM commands WHERE id=?", (str(row[0]),)).fetchone()
            try:
                prior_payload = json.loads(str(prior_payload_row[0]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ConflictError("an unresolved Worktree Verification Command has malformed payload") from exc
            if (isinstance(prior_payload, dict)
                    and prior_payload.get("attempt_id") == attempt.id
                    and prior_payload.get("profile") == profile
                    and str(row[1]) != idempotency_key.strip()):
                raise ConflictError(
                    "this Attempt has an unresolved Worktree Verification Command for"
                    f" profile {profile!r}; inspect/reconcile it before another run")
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "Worktree Verification Command is already running/uncertain; reconcile it before retry",
                entity="command", current=command.state, event="worktree_verification_run")
        run_id = str(factory.new("verification"))
        repo_root = allocation.repo_root
    worktree_path = allocation.worktree_path
    resolved_root = _ensure_artifact_root(
        artifact_root if artifact_root is not None else default_artifact_root(repo_root))

    # ---- outside the transaction: identity, Diff Gates, pinned execution ----
    problems: list[str] = []
    identity_verified = False
    gate_before: dict[str, Any] | None = None
    gate_after: dict[str, Any] | None = None
    ran = False
    exit_code: int | None = None
    stdout, stderr = b"", b""
    timed_out = truncated = False
    cleanup_error: str | None = None
    env = _pinned_env(worktree_path)
    effective_run_git = run_git or _pinned_run_git
    started_iso = _now(clock)
    started_mono = time.monotonic()

    try:
        worktrees.claim_worktree(conn, attempt.id, run_git=effective_run_git)
        identity_verified = True
    except (worktrees.WorktreeIdentityError, workspaces.GitStateError,
            StorageError, NotFoundError) as exc:
        kind = getattr(exc, "kind", "git-unreadable")
        problems.append(f"worktree identity verification failed ({kind}): {exc}")

    if identity_verified:
        gate_before, gate_error = _run_gate(
            worktree_path, allocation.claims, effective_run_git)
        if gate_error is not None:
            problems.append(f"pre-verification Diff Gate failed: {gate_error}")
        elif not gate_before["ok"]:
            preview = ", ".join(gate_before["violations"][:5])
            problems.append(
                f"out-of-scope changes present before verification "
                f"({gate_before['violation_count']}): {preview}")

    if not problems and not config.enabled:
        # Explicitly disabled by approved config: SKIPPED, never executed.
        ran = False
    elif not problems:
        try:
            exit_code, stdout, stderr, timed_out, truncated, cleanup_error = _run_bounded(
                config.argv, cwd=worktree_path,
                timeout_seconds=config.timeout_seconds, env=env)
            ran = True
        except FileNotFoundError as exc:
            problems.append(
                f"approved verifier command is missing: {exc}"
                " (a missing command cannot PASS)")
        except OSError as exc:
            problems.append(f"verifier could not be executed: {exc}")

    if identity_verified:
        try:
            worktrees._verify_identity(
                conn, allocation, effective_run_git, repair=False)
        except (worktrees.WorktreeIdentityError, workspaces.GitStateError) as exc:
            problems.append(f"worktree identity drifted during verification: {exc}")
        gate_after, gate_error = _run_gate(
            worktree_path, allocation.claims, effective_run_git)
        if gate_error is not None:
            problems.append(f"post-verification Diff Gate failed: {gate_error}")
        elif not gate_after["ok"]:
            preview = ", ".join(gate_after["violations"][:5])
            problems.append(
                f"out-of-scope changes present after verification "
                f"({gate_after['violation_count']}): {preview}")
        elif (gate_before is not None
              and gate_after["base_sha"] != gate_before["base_sha"]):
            problems.append(
                "worktree HEAD moved during verification "
                f"({gate_before['base_sha']!r} -> {gate_after['base_sha']!r});"
                " the verifier must not commit or check out")

    if timed_out:
        problems.append(
            f"verifier exceeded timeout of {config.timeout_seconds} seconds; process was terminated")
    if truncated:
        problems.append(
            f"combined stdout/stderr exceeded {MAX_CAPTURE_BYTES} bytes; verifier was terminated")
    if cleanup_error:
        problems.append(cleanup_error)
    if exit_code is not None and exit_code < 0:
        problems.append(
            f"verifier process terminated by signal {-exit_code}; "
            "a crashed verifier is ERROR, not a test failure")

    if problems:
        status = "error"
    elif not ran:
        status = "skipped"
    elif exit_code == 0:
        status = "pass"
    else:
        status = "fail"

    finished_iso = _now(clock)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    stdout_capped = len(stdout) > MAX_EMBED_OUTPUT_BYTES
    stderr_capped = len(stderr) > MAX_EMBED_OUTPUT_BYTES
    report_obj: dict[str, Any] = {
        "schema_version": 1,
        "profile": profile,
        "profile_version": config.profile_version,
        "config_digest": config.config_digest,
        "argv": list(config.argv),
        "cwd": worktree_path,
        "env_pinned": sorted(env),
        "timeout_seconds": config.timeout_seconds,
        "ran": ran,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": truncated,
        "cleanup_error": cleanup_error,
        "identity_verified": identity_verified,
        "diff_gate_before": gate_before,
        "diff_gate_after": gate_after,
        "stdout": stdout[:MAX_EMBED_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stderr": stderr[:MAX_EMBED_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_capped": stdout_capped,
        "stderr_capped": stderr_capped,
        "workspace": {"repo_root": repo_root, "worktree_path": worktree_path,
                     "branch": allocation.branch, "base_sha": allocation.base_sha,
                     "allocation_id": allocation.allocation_id},
        "bindings": {"task_id": task.id, "task_version": task.version,
                    "dispatch_task_version": dispatch_task_version,
                    "brief_digest": brief.brief_digest,
                    "worker_report_id": report_id,
                    "worker_report_digest": worker_digest,
                    "attempt_id": attempt.id, "attempt_no": attempt.attempt_no},
        "started_at": started_iso, "finished_at": finished_iso,
        "duration_ms": duration_ms,
        "problems": problems,
    }
    if problems:
        report_obj["error"] = "; ".join(problems)[:4096]
    report_bytes = _json(report_obj).encode("utf-8")
    if len(report_bytes) > MAX_WORKTREE_REPORT_BYTES:
        report_obj["stdout"] = ""
        report_obj["stderr"] = ""
        report_obj["output_omitted"] = True
        report_bytes = _json(report_obj).encode("utf-8")
        if len(report_bytes) > MAX_WORKTREE_REPORT_BYTES:
            report_bytes = _json({
                "schema_version": 1, "profile": profile,
                "profile_version": config.profile_version,
                "config_digest": config.config_digest, "argv": list(config.argv),
                "cwd": worktree_path, "ran": ran, "exit_code": exit_code,
                "timed_out": timed_out, "truncated": truncated,
                "output_omitted": True,
                "error": "serialized worktree verification artifact exceeded size limit",
            }).encode("utf-8")
            status = "error"
            problems.append("serialized worktree verification artifact exceeded size limit")
    artifact_digest = _digest(report_bytes)

    log_relpath: str | None = None
    log_digest: str | None = None
    try:
        verification_segment = _safe_segment("verification id", run_id)
        log_relpath = "worktree/{}/{}-{}.log".format(
            attempt_segment, verification_segment, profile)
        log_digest = _write_artifact(resolved_root, log_relpath, report_bytes)
    except (ValidationError, StorageError, OSError) as exc:
        problems.append(f"could not persist verifier artifact: {exc}")
        status = "error"
        log_relpath = log_digest = None
        report_bytes = _json({**report_obj, "problems": problems,
                             "error": "; ".join(problems)[:4096]}).encode("utf-8")
        if len(report_bytes) > MAX_WORKTREE_REPORT_BYTES:
            report_bytes = _json({"schema_version": 1, "profile": profile,
                                 "error": "artifact persistence failed",
                                 "output_omitted": True}).encode("utf-8")
        artifact_digest = _digest(report_bytes)

    try:
        with _tx(conn):
            # A stale leader's verdict is never authoritative: record stale.
            try:
                leader.require_project_lease(conn, lease, task.project_id, clock=clock)
            except leader.StaleLeaderError:
                status = "stale"
            current_task = domain.get_task(conn, task.id)
            current_attempt = domain.get_attempt(conn, attempt.id)
            current_brief = briefs.get_attempt_brief(conn, attempt.id)
            report_row = conn.execute(
                "SELECT id, task_id, task_version, brief_digest, report_digest, outcome "
                "FROM worker_reports WHERE attempt_id=? AND report_kind='final' ORDER BY id DESC LIMIT 1",
                (attempt.id,),
            ).fetchone()
            current_profile = _profile_config(conn, task.project_id, profile)
            current_allocation = worktrees.require_allocation(conn, attempt.id)
            fresh = (current_task.version == task.version and current_task.status == "verifying"
                and current_attempt.status == "finished" and current_attempt.attempt_no == attempt.attempt_no
                and current_brief.brief_digest == brief.brief_digest
                and current_brief.task_version == dispatch_task_version
                and report_row is not None and int(report_row[0]) == report_id
                and str(report_row[1]) == task.id and int(report_row[2]) == dispatch_task_version
                and str(report_row[3]) == brief.brief_digest and str(report_row[4]) == worker_digest
                and str(report_row[5]) == "completed"
                and int(current_profile[4]) == config.profile_version
                and str(current_profile[8]) == config.config_digest
                and current_allocation.allocation_id == allocation.allocation_id)
            if not fresh:
                status = "stale"
            conn.execute(
                "INSERT INTO worktree_verifications (id, command_id, project_id, task_id,"
                " attempt_id, attempt_no, task_version, dispatch_task_version, brief_digest,"
                " worker_report_id, worker_report_digest, allocation_id, worktree_path, branch,"
                " base_sha, claims_json, profile, profile_version, config_digest, argv_json,"
                " status, exit_code, report_json, report_digest, log_relpath, log_digest, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, command.id, task.project_id, task.id, attempt.id, attempt.attempt_no,
                 task.version, dispatch_task_version, brief.brief_digest, report_id, worker_digest,
                 allocation.allocation_id, worktree_path, allocation.branch, allocation.base_sha,
                 _json(list(allocation.claims)), profile, config.profile_version,
                 config.config_digest, _json(list(config.argv)), status, exit_code,
                 report_bytes.decode("utf-8"), artifact_digest, log_relpath, log_digest,
                 _now(clock)),
            )
            events.append_event(conn, WORKTREE_VERIFICATION_EVENT_KIND,
                project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                payload_json=_json({"verification_id": run_id, "command_id": command.id,
                    "profile": profile, "profile_version": config.profile_version,
                    "config_digest": config.config_digest, "status": status,
                    "exit_code": exit_code, "report_digest": artifact_digest,
                    "log_relpath": log_relpath, "log_digest": log_digest,
                    "allocation_id": allocation.allocation_id,
                    "identity_verified": identity_verified,
                    "diff_gate_before_ok": bool(gate_before and gate_before["ok"]),
                    "diff_gate_after_ok": bool(gate_after and gate_after["ok"])}),
                clock=clock)
            task_status_after = current_task.status
            if (status in ("fail", "error", "stale")
                    and current_task.status == "verifying"
                    and not _defer_failure_escalation):
                # Failures escalate to NEEDS_HUMAN (the Verification service is
                # the authority that writes verification outcomes; the generic
                # Task event door refuses this event by design).  The only
                # forward path is a human-approved retry that dispatches a NEW
                # bounded Attempt — never another verification of this one.
                updated = domain._cas_task(conn, current_task, status="needs_human",
                    expected_version=current_task.version, now=_now(clock))
                task_status_after = updated.status
                events.append_event(conn, "verification.worktree_failed",
                    project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                    payload_json=_json({"verification_id": run_id, "profile": profile,
                        "status": status, "task_status": task_status_after,
                        "next": "retry_approved -> new bounded Attempt (never re-verify this Attempt)"}),
                    clock=clock)
            commands.confirm_command(conn, command.id, result_json=_json({
                "verification_id": run_id, "status": status, "exit_code": exit_code,
                "report_digest": artifact_digest, "log_digest": log_digest,
                "task_status": task_status_after}), clock=clock)
            return get_worktree_verification(conn, run_id)
    except Exception as exc:
        detail = str(exc).encode("utf-8")[:500].decode("utf-8", errors="ignore")
        try:
            commands.mark_command_unknown(conn, command.id,
                note=f"Worktree verifier returned but result commit failed: {detail}",
                clock=clock)
        except Exception:
            pass
        raise


def worktree_verification_is_current(conn: storage.TeamCraftConnection,
                                    verification: WorktreeVerification) -> bool:
    """Whether immutable Worktree Verification evidence still matches facts."""
    if verification.status == "stale":
        return False
    try:
        task = domain.get_task(conn, verification.task_id)
        attempt = domain.get_attempt(conn, verification.attempt_id)
        brief = briefs.get_attempt_brief(conn, attempt.id)
        report = conn.execute(
            "SELECT id, task_id, task_version, brief_digest, report_digest, outcome "
            "FROM worker_reports WHERE attempt_id=? AND report_kind='final' ORDER BY id DESC LIMIT 1",
            (attempt.id,),
        ).fetchone()
        profile_row = _profile_config(conn, task.project_id, verification.profile)
        allocation = worktrees.load_allocation(conn, attempt.id)
        latest = conn.execute(
            "SELECT id FROM worktree_verifications WHERE attempt_id=? AND profile=?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (attempt.id, verification.profile),
        ).fetchone()
        accepted = verification.status == "pass"
        return bool(
            task.project_id == verification.project_id
            and attempt.task_id == task.id
            and attempt.status == "finished"
            and attempt.attempt_no == verification.attempt_no
            and (task.status == "verifying" if accepted else task.status in ("verifying", "needs_human"))
            and (task.version == verification.task_version if accepted
                 else task.version >= verification.task_version)
            and brief.task_version == verification.dispatch_task_version
            and brief.brief_digest == verification.brief_digest
            and report is not None and int(report[0]) == verification.worker_report_id
            and str(report[4]) == verification.worker_report_digest
            and str(report[5]) == "completed"
            and allocation is not None and allocation.allocation_id == verification.allocation_id
            and int(profile_row[4]) == verification.profile_version
            and str(profile_row[8]) == verification.config_digest
            and latest is not None and str(latest[0]) == verification.id)
    except (ValidationError, NotFoundError, IllegalTransitionError):
        return False


def read_worktree_artifact(conn: storage.TeamCraftConnection, verification_id: str,
                          *, artifact_root: str | None = None) -> dict[str, Any]:
    """Load and verify the persisted artifact of one worktree verification.

    The stored relpath/digest are re-validated against the configured root:
    containment (never escapes the root) and content integrity. A verification
    without a persisted artifact, a missing file, an escaping or tampered file
    all fail closed.
    """
    verification = get_worktree_verification(conn, verification_id)
    if verification.log_relpath is None or verification.log_digest is None:
        raise NotFoundError(
            f"worktree verification {verification_id} has no persisted artifact")
    root = _ensure_artifact_root(
        artifact_root if artifact_root is not None
        else default_artifact_root(domain.get_project(conn, verification.project_id).repo_path))
    target = _contained_artifact_path(root, verification.log_relpath)
    if not os.path.isfile(target):
        raise NotFoundError(f"verifier artifact file is missing: {verification.log_relpath!r}")
    with open(target, "rb") as handle:
        content = handle.read()
    if _digest(content) != verification.log_digest:
        raise StorageError("verifier artifact digest mismatch; the artifact was tampered with")
    return {"verification_id": verification.id, "status": verification.status,
            "profile": verification.profile,
            "log_relpath": verification.log_relpath, "log_digest": verification.log_digest,
            "report_digest": verification.report_digest,
            "report": json.loads(content.decode("utf-8"))}


def list_worktree_verification_commands(conn: storage.TeamCraftConnection,
                                       task_id: str) -> list[dict[str, Any]]:
    """Pending/uncertain Worktree Verification Commands for safe recovery."""
    task_id = require_entity_id(task_id)
    rows = conn.execute(
        "SELECT id, state, payload_json, note FROM commands WHERE kind=?"
        " ORDER BY requested_at, id", (WORKTREE_RUN_KIND,)).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(str(row[2]))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            result.append({"command_id": str(row[0]), "state": str(row[1]),
                          "attempt_id": payload.get("attempt_id"),
                          "profile": payload.get("profile"), "note": row[3]})
    return result


def reconcile_worktree_run(conn: storage.TeamCraftConnection, command_id: str, *,
                           lease: leader.Lease, actor: str, reason: str,
                           clock: Clock | None = None) -> commands.Command:
    """Close an interrupted (running/unknown) worktree verification Command.

    The ONLY authoritative reconciliation is ``expired``: the interrupted run
    produced no accepted result, it is settled without PASS, and verification
    may then be re-attempted under a fresh idempotency key (the Attempt itself
    was never re-run).  This never fabricates a verification row; accepted
    results, once recorded, are immutable facts that replay returns.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValidationError("actor is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("reason is required")
    with _tx(conn):
        command = commands.get_command(conn, command_id)
        if command.kind != WORKTREE_RUN_KIND:
            raise IllegalTransitionError(
                "command is not a worktree verification run", entity="command",
                current=command.state, event="worktree_verification_reconcile")
        try:
            scope = json.loads(command.evidence_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("worktree verification Command evidence is malformed") from exc
        project_id = scope.get("project_id") if isinstance(scope, dict) else None
        if project_id is None:
            raise ValidationError(
                "worktree verification reconcile requires a project-scoped Command")
        leader.require_project_lease(conn, lease, str(project_id), clock=clock)
        row = conn.execute(
            "SELECT id FROM worktree_verifications WHERE command_id=?", (command.id,),
        ).fetchone()
        if row is not None:
            raise ConflictError(
                "this Command already owns a durable Worktree Verification row;"
                " accepted results are immutable and must not be reconciled away")
        if command.state in {"confirmed", "expired"}:
            return command
        note = f"worktree run reconciled as not-accepted by {actor.strip()}: {reason.strip()}"[:1900]
        return commands.reconcile_command(conn, command.id, outcome="expired",
                                         note=note, clock=clock)


def _suite_verdict(statuses: dict[str, str]) -> str:
    """Overall verdict: error dominates, then fail, then pass, else skipped."""
    if any(status in ("error", "stale") for status in statuses.values()):
        return "error"
    if "fail" in statuses.values():
        return "fail"
    if "pass" in statuses.values():
        return "pass"
    return "skipped"


def run_worktree_suite(
    conn: storage.TeamCraftConnection, attempt_id: str, *,
    idempotency_key: str, lease: leader.Lease,
    profiles: Sequence[str] | None = None, artifact_root: str | None = None,
    run_git: Any = None, id_factory: IdFactory | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Run the approved P0 profile suite for one finished Attempt.

    Each profile runs through :func:`run_worktree_verifier` under the
    deterministic sub-key ``<key>:<profile>``; the aggregate verdict
    (error > fail > pass, else skipped) is persisted as one idempotent
    ``verification.worktree_suite`` event.  A replay of the same key never
    re-executes nor duplicates any profile's settled result.
    """
    selected = list(profiles) if profiles else list(VERIFIER_PROFILES)
    if not selected:
        raise ValidationError("a verifier suite needs at least one profile")
    if len(set(selected)) != len(selected):
        raise ValidationError("duplicate profiles in suite request")
    for profile in selected:
        if profile not in VERIFIER_PROFILES:
            raise ValidationError(
                f"unknown verifier profile {profile!r}; supported: "
                + ", ".join(VERIFIER_PROFILES))
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key.strip()) > 180:
        raise ValidationError("idempotency_key is required and must be at most 180 characters")
    attempt_id = require_entity_id(attempt_id)
    results: dict[str, Any] = {}
    for profile in selected:
        verification = run_worktree_verifier(
            conn, attempt_id, profile=profile,
            idempotency_key=f"{idempotency_key.strip()}:{profile}",
            lease=lease, artifact_root=artifact_root, run_git=run_git,
            id_factory=id_factory, clock=clock,
            _defer_failure_escalation=True)
        results[profile] = {
            "verification_id": verification.id, "status": verification.status,
            "exit_code": verification.exit_code,
            "profile_version": verification.profile_version,
            "config_digest": verification.config_digest,
            "report_digest": verification.report_digest,
            "log_relpath": verification.log_relpath, "log_digest": verification.log_digest}
    statuses = {profile: str(value["status"]) for profile, value in results.items()}
    verdict = _suite_verdict(statuses)
    task = domain.get_task(conn, domain.get_attempt(conn, attempt_id).task_id)
    if verdict in ("fail", "error") and task.status == "verifying":
        with _tx(conn):
            current = domain.get_task(conn, task.id)
            if current.status == "verifying":
                task = domain._cas_task(
                    conn, current, status="needs_human",
                    expected_version=current.version, now=_now(clock))
                events.append_event(
                    conn, "verification.worktree_suite_failed",
                    project_id=task.project_id, task_id=task.id,
                    attempt_id=attempt_id,
                    payload_json=_json({
                        "verdict": verdict, "profiles": statuses,
                        "next": "retry_approved -> new bounded Attempt",
                    }), clock=clock)
    event_payload = _json({"attempt_id": attempt_id, "task_id": task.id,
        "idempotency_key": idempotency_key.strip(), "verdict": verdict,
        "profiles": results})
    event_id = "evt-wvs-" + _digest(
        f"worktree-suite|{idempotency_key.strip()}|{attempt_id}".encode("utf-8"))[:40]
    events.append_event_idempotent(conn, WORKTREE_SUITE_KIND,
        project_id=task.project_id, task_id=task.id, attempt_id=attempt_id,
        payload_json=event_payload, event_id=event_id, clock=clock)
    return {"attempt_id": attempt_id, "task_id": task.id, "verdict": verdict,
            "profiles": results}

