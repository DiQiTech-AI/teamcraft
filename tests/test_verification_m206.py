"""M2-06 verifier, evidence binding, approval and delivery-boundary tests."""
from __future__ import annotations

import hashlib
import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from teamcraft import briefs, commands, domain, leader, storage, verification, worker_reports
from teamcraft.errors import ConflictError, IllegalTransitionError, ValidationError


class Clock:
    def __init__(self): self.value = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    def now(self): return self.value
    def advance(self, seconds): self.value += timedelta(seconds=seconds)


class VerificationM206Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        self.project = domain.create_project(self.conn, "p", str(self.root), clock=self.clock)

    def lease(self, holder="tester", ttl_seconds=60):
        return leader.acquire_or_renew(self.conn, leader.project_scope(self.project.id), holder,
            ttl_seconds=ttl_seconds, clock=self.clock)

    def prepared_attempt(self, kind="scout"):
        task = domain.create_task(self.conn, self.project.id, "task", kind=kind, clock=self.clock)
        task = domain.apply_task_event(self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version, clock=self.clock)
        attempt = domain.create_attempt(self.conn, task.id, expected_version=task.version, clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        attempt = domain.transition_attempt(self.conn, attempt.id, "activated", clock=self.clock)
        brief = briefs.create_attempt_brief(self.conn, attempt.id, content="M2-06 verification brief", clock=self.clock)
        return task, attempt, brief

    def finish_worker(self, task, attempt, brief):
        report = {"schema_version": 1, "task_id": task.id, "attempt_id": attempt.id,
            "task_version": brief.task_version, "brief_digest": brief.brief_digest,
            "report_kind": "final", "outcome": "completed", "milestone_id": "final",
            "summary": "implementation finished", "artifact_digest": "a" * 64,
            "usage": {"total_tokens": None}}
        stored = worker_reports.record_worker_report(self.conn, report, clock=self.clock)
        self.assertEqual(domain.get_attempt(self.conn, attempt.id).status, "finished")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "running")
        return stored

    def verifying_scout(self):
        task, attempt, brief = self.prepared_attempt("scout")
        report = self.finish_worker(task, attempt, brief)
        task = domain.get_task(self.conn, task.id)
        task = domain.apply_task_event(self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock)
        return task, attempt, brief, report

    def configure(self, *, argv=None, key="config-key", lease=None):
        return verification.configure_verifier(self.conn, self.project.id,
            argv=argv or ["verifier", "--check"], rules_version="rules-1",
            timeout_seconds=30, actor="human", idempotency_key=key,
            lease=lease or self.lease(), clock=self.clock)

    def run_pass(self, attempt, *, lease=None, key="run-key", run=None):
        completed = (0, b"PASS\n", b"", False, False, None)
        with mock.patch.object(verification, "_run_bounded", return_value=completed) as runner:
            result = verification.run_verification(self.conn, attempt.id, idempotency_key=key,
                lease=lease or self.lease(), clock=self.clock)
        return result, runner

    def test_schema_v8_migrates_historical_v7_and_validates_objects(self):
        raw = sqlite3.connect(self.db)
        try:
            for trigger in storage._V8_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V8_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {index}")
            for table in ("approvals", "verifications", "verification_configs"):
                raw.execute(f"DROP TABLE IF EXISTS {table}")
            for trigger in storage._V9_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V9_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {index}")
            raw.execute("DROP TABLE IF EXISTS ceo_initializations")
            for trigger in storage._V10_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V10_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {index}")
            for table in ("wake_events", "wakes", "wake_cursors"):
                raw.execute(f"DROP TABLE IF EXISTS {table}")
            for trigger in storage._V11_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            columns = {row[1] for row in raw.execute("PRAGMA table_info(ceo_initializations)")}
            if "receipt_source" in columns:
                raw.execute("ALTER TABLE ceo_initializations DROP COLUMN receipt_source")
            raw.execute("DELETE FROM schema_migrations WHERE version > 7")
            raw.execute("PRAGMA user_version=7")
            raw.commit()
        finally:
            raw.close()
        migration = storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.assertEqual((migration.from_version, migration.to_version, migration.applied), (7, 11, (8, 9, 10, 11)))
        report = storage.verify_database(self.db)
        self.assertTrue(report["at_head"])
        self.assertTrue(set(storage._V8_TABLES).issubset(set(report["tables"])))

    def test_verifier_argv_is_direct_shell_false_and_artifact_hash_is_exact(self):
        task, attempt, brief, report = self.verifying_scout()
        script = "import sys; sys.stdout.write('clean\\n'); sys.stderr.write('warning\\n')"
        argv = [sys.executable, "-c", script]
        self.configure(argv=argv)
        with mock.patch.object(verification.subprocess, "Popen", wraps=verification.subprocess.Popen) as runner:
            result = verification.run_verification(self.conn, attempt.id,
                idempotency_key="verification-run-1", lease=self.lease(), clock=self.clock)
        call = runner.call_args
        self.assertEqual(call.args[0], argv)
        self.assertIs(call.kwargs["shell"], False)
        self.assertEqual(call.kwargs["cwd"], str(self.root))
        self.assertEqual(result.argv, tuple(argv))
        artifact = json.loads(result.artifact)
        self.assertEqual(artifact["stdout"], "clean\n")
        self.assertEqual(artifact["stderr"], "warning\n")
        self.assertEqual(result.artifact_digest, hashlib.sha256(result.artifact).hexdigest())
        self.assertEqual((result.task_id, result.attempt_id), (task.id, attempt.id))
        self.assertEqual(result.task_version, brief.task_version + 1)
        self.assertEqual(result.dispatch_task_version, brief.task_version)
        self.assertEqual(result.brief_digest, brief.brief_digest)
        self.assertEqual(result.worker_report_digest, report.report_digest)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")

    def test_verifier_output_overflow_kills_and_reaps_process_group_with_bounded_artifact(self):
        task, attempt, _, _ = self.verifying_scout()
        parent_marker = self.root / "parent-finished"
        child_marker = self.root / "child-survived"
        child_pid_file = self.root / "child.pid"
        child_script = (
            "import time; from pathlib import Path; time.sleep(1.5); "
            f"Path({str(child_marker)!r}).write_text('survived')"
        )
        verifier_script = (
            "import os, subprocess, sys; from pathlib import Path; "
            f"child=subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
            f"Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
            "chunk=b'x'*65536; "
            "[(os.write(1, chunk), os.write(2, chunk)) for _ in range(16)]; "
            f"Path({str(parent_marker)!r}).write_text('finished')"
        )
        self.configure(argv=[sys.executable, "-c", verifier_script], key="overflow-config")
        started = time.monotonic()
        result = verification.run_verification(self.conn, attempt.id,
            idempotency_key="overflow-run", lease=self.lease(), clock=self.clock)
        elapsed = time.monotonic() - started

        artifact = json.loads(result.artifact)
        output_bytes = len(artifact["stdout"].encode("utf-8")) + len(artifact["stderr"].encode("utf-8"))
        self.assertEqual(result.status, "error")
        self.assertTrue(artifact["truncated"])
        self.assertLessEqual(output_bytes, verification.MAX_CAPTURE_BYTES)
        self.assertIn("exceeded 262144 bytes", artifact["error"])
        self.assertIn("terminated", artifact["error"])
        self.assertLess(len(result.artifact), verification.MAX_CAPTURE_BYTES + 2048)
        self.assertLess(elapsed, 5.0, "verifier output overflow must not hang collection")
        self.assertFalse(parent_marker.exists(), "verifier must be terminated before its final marker")
        if os.name == "posix":
            self.assertTrue(child_pid_file.exists(), "verifier did not start its process-group child")
            time.sleep(1.7)
            self.assertFalse(child_marker.exists(), "verifier process-group child survived termination")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")

    def test_cleanup_waits_are_bounded_and_timeout_returns_unknown_exit_code(self):
        source = Path(verification.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        run_bounded = next(node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run_bounded")
        wait_calls = [node for node in ast.walk(run_bounded)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait"]
        self.assertGreaterEqual(len(wait_calls), 2)
        for call in wait_calls:
            self.assertTrue(any(keyword.arg == "timeout" for keyword in call.keywords),
                "every process wait in _run_bounded must have a finite timeout")

        class Stream:
            def fileno(self): return 40
            def close(self): pass

        class EmptySelector:
            def register(self, *args): pass
            def get_map(self): return {}
            def close(self): pass

        class StuckProcess:
            pid = 42
            stdout = Stream()
            stderr = Stream()
            def __init__(self): self.wait_timeouts = []
            def poll(self): return None
            def wait(self, *, timeout=None):
                self.wait_timeouts.append(timeout)
                raise subprocess.TimeoutExpired("verifier", timeout)

        proc = StuckProcess()
        with mock.patch.object(verification.subprocess, "Popen", return_value=proc), \
             mock.patch.object(verification.selectors, "DefaultSelector", return_value=EmptySelector()), \
             mock.patch.object(verification.os, "killpg", return_value=None), \
             mock.patch.object(verification.os, "set_blocking", return_value=None):
            result = verification._run_bounded(["verifier"], cwd=str(self.root), timeout_seconds=1)

        returncode, stdout, stderr, timed_out, truncated, cleanup_error = result
        self.assertIsNone(returncode)
        self.assertEqual(stdout + stderr, b"")
        self.assertFalse(timed_out)
        self.assertFalse(truncated)
        self.assertIn("4-second", cleanup_error)
        self.assertGreaterEqual(len(proc.wait_timeouts), 2)
        for timeout in proc.wait_timeouts:
            self.assertIsInstance(timeout, (int, float))
            self.assertGreaterEqual(timeout, 0)
            self.assertLessEqual(timeout, 4.0)

    def test_verification_rejects_stale_task_brief_report_binding(self):
        task, attempt, _ = self.prepared_attempt("scout")
        self.configure()
        with self.assertRaisesRegex(ValidationError, "durable final WorkerReport"):
            verification.run_verification(self.conn, attempt.id, idempotency_key="missing-report",
                lease=self.lease(), clock=self.clock)
        brief = briefs.get_attempt_brief(self.conn, attempt.id)
        self.finish_worker(task, attempt, brief)
        task = domain.get_task(self.conn, task.id)
        task = domain.apply_task_event(self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock)
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET version=version+1 WHERE id=?", (task.id,))
        with self.assertRaisesRegex(ValidationError, "outside worker_reported_done"):
            verification.run_verification(self.conn, attempt.id, idempotency_key="stale-task",
                lease=self.lease(), clock=self.clock)

    def test_lost_lease_records_unknown_and_prevents_verifier_reexecution(self):
        task, attempt, _, _ = self.verifying_scout()
        self.configure()
        lease = self.lease(ttl_seconds=10)
        completed = (0, b"PASS", b"", False, False, None)
        calls = []
        def lose_lease(argv, **kwargs):
            calls.append((argv, kwargs))
            self.clock.advance(11)
            leader.acquire_or_renew(self.conn, leader.project_scope(self.project.id), "replacement",
                ttl_seconds=60, clock=self.clock)
            return completed
        with mock.patch.object(verification, "_run_bounded", side_effect=lose_lease):
            with self.assertRaises(leader.StaleLeaderError):
                verification.run_verification(self.conn, attempt.id, idempotency_key="lost-lease-run",
                    lease=lease, clock=self.clock)
        command = self.conn.execute("SELECT id,state FROM commands WHERE idempotency_key='lost-lease-run'").fetchone()
        self.assertEqual(command[1], "unknown")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0], 0)
        with mock.patch.object(verification, "_run_bounded", side_effect=AssertionError("must not reexecute")) as retry_runner:
            with self.assertRaises(IllegalTransitionError):
                verification.run_verification(self.conn, attempt.id, idempotency_key="lost-lease-run",
                    lease=leader.acquire_or_renew(self.conn, leader.project_scope(self.project.id), "replacement",
                        ttl_seconds=60, clock=self.clock), clock=self.clock)
        retry_runner.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_only_passing_scout_verification_and_acceptance_can_finish_task(self):
        task, attempt, _, _ = self.verifying_scout()
        self.configure()
        passing, _ = self.run_pass(attempt)
        self.assertEqual(passing.status, "pass")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")
        approval = verification.decide_approval(self.conn, passing.id, decision="accept",
            actor="human", reason="accepted report", idempotency_key="approval-key",
            lease=self.lease(), clock=self.clock)
        done = domain.get_task(self.conn, task.id)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.version, approval.task_version + 1)
        replay = verification.decide_approval(self.conn, passing.id, decision="accept",
            actor="human", reason="accepted report", idempotency_key="approval-key",
            lease=self.lease(), clock=self.clock)
        self.assertEqual(replay.id, approval.id)
        self.assertEqual(domain.get_task(self.conn, task.id).version, done.version)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approvals WHERE verification_id=?", (passing.id,)).fetchone()[0], 1)
        changed = domain.update_task(self.conn, task.id, expected_version=done.version, priority=done.priority + 1, clock=self.clock)
        self.assertEqual(changed.status, "needs_human")
        self.assertFalse(verification.list_approvals(self.conn, task.id)[0].current)

    def test_failed_verification_cannot_be_accepted(self):
        task, attempt, _, _ = self.verifying_scout()
        self.configure()
        with mock.patch.object(verification, "_run_bounded", return_value=(1, b"FAIL", b"bad", False, False, None)):
            result = verification.run_verification(self.conn, attempt.id,
                idempotency_key="verification-fails", lease=self.lease(), clock=self.clock)
        self.assertEqual(result.status, "fail")
        with self.assertRaisesRegex(IllegalTransitionError, "passing Verification"):
            verification.decide_approval(self.conn, result.id, decision="accept", actor="human",
                reason="accept", idempotency_key="accept-failed", lease=self.lease(), clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")

    def test_ship_verification_and_m4_delivery_are_blocked_in_m2(self):
        task, attempt, brief = self.prepared_attempt("ship")
        self.finish_worker(task, attempt, brief)
        task = domain.get_task(self.conn, task.id)
        task = domain.apply_task_event(self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock)
        self.configure()
        with mock.patch.object(verification, "_run_bounded") as runner:
            with self.assertRaisesRegex(IllegalTransitionError, "M4 Worktree/Commit/Diff"):
                verification.run_verification(self.conn, attempt.id, idempotency_key="ship-run",
                    lease=self.lease(), clock=self.clock)
        runner.assert_not_called()
        with self.assertRaises(IllegalTransitionError) as exc:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                expected_version=task.version, clock=self.clock)
        self.assertEqual(exc.exception.reason_code, "M4_EVIDENCE_REQUIRED")


if __name__ == "__main__": unittest.main()
