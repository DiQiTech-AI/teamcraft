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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from . import briefs, commands, domain, events, leader, storage
from .clock import Clock, SystemClock
from .errors import ConflictError, IllegalTransitionError, NotFoundError, ValidationError
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
) -> tuple[int | None, bytes, bytes, bool, bool, str | None]:
    """Run without a shell and cap combined stdout/stderr while reading pipes.

    At most MAX_CAPTURE_BYTES are retained. A one-byte probe detects overflow;
    upon overflow the entire process group is killed and remaining pipe data is
    drained/discarded (with a bounded cleanup window) before the process is
    reaped. The timeout path uses the same termination and reaping mechanism.
    """
    proc = subprocess.Popen(
        list(argv), cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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

