"""M2-05 recovery and retry acceptance contracts."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import briefs, commands, domain, events, leader, recovery, storage, worker_reports
from teamcraft.errors import ConflictError, IllegalTransitionError, ValidationError


class Clock:
    def __init__(self): self.value = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    def now(self): return self.value


class RecoveryM205Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "db.sqlite"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        self.project = domain.create_project(self.conn, "p", "/repo", clock=self.clock)

    def task(self, *, max_attempts=3):
        task = domain.create_task(self.conn, self.project.id, "task", max_attempts=max_attempts, clock=self.clock)
        task = domain.apply_task_event(self.conn, task.id, "dependencies_satisfied", expected_version=task.version, clock=self.clock)
        return task

    def active(self, task=None):
        task = task or self.task()
        attempt = domain.create_attempt(self.conn, task.id, expected_version=task.version, clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        attempt = domain.transition_attempt(self.conn, attempt.id, "activated", clock=self.clock)
        session = domain.register_session(self.conn, attempt.id, "herdr", native_session_id="native", pane_id="pane", clock=self.clock)
        with storage.transaction(self.conn):
            self.conn.execute("INSERT INTO members (name,role,responsibility,kind,task,ownership_json,read_only,cwd,pane_id,terminal_id,phase,task_id,attempt_id,session_id,created_at,updated_at) VALUES (?,'worker','x','herdr','x','[]',0,'/repo','pane','term','active',?,?,?,0,0)", (f"w-{task.id}", task.id, attempt.id, session.id))
        briefs.create_attempt_brief(self.conn, attempt.id, content="work", clock=self.clock)
        return task, attempt, session

    def lease(self): return leader.acquire_or_renew(self.conn, leader.project_scope(self.project.id), "leader", ttl_seconds=60, clock=self.clock)

    def request(self, task, attempt, key, action="reconcile"):
        return recovery.request_action(self.conn, task_id=task.id, attempt_id=attempt.id,
            action=action, actor="human", reason="inspect", idempotency_key=key, clock=self.clock)

    def evidence(self, alive):
        return {"identity_verified": True, "process_alive": alive,
                "terminal_id": "term", "native_session_id": "native"}

    def test_create_bounds_and_max_attempts_default(self):
        task = self.task()
        self.assertEqual(task.max_attempts, 3)
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                domain.create_task(self.conn, self.project.id, "invalid", max_attempts=value, clock=self.clock)

    def test_escalation_event_is_bound_to_exact_timeout_reason_and_lease(self):
        task, attempt, _ = self.active()
        deadline = events.append_event(self.conn, "watch.deadline_exceeded", project_id=self.project.id,
            task_id=task.id, attempt_id=attempt.id, payload_json='{}', clock=self.clock)
        wrong = events.append_event(self.conn, "watch.no_progress_threshold", project_id=self.project.id,
            task_id=task.id, attempt_id=attempt.id, payload_json='{}', clock=self.clock)
        lease = self.lease()
        with self.assertRaises(ValidationError):
            recovery.escalate_threshold(self.conn, attempt.id, wrong.event_id, reason="deadline_exceeded", lease=lease, clock=self.clock)
        updated = recovery.escalate_threshold(self.conn, attempt.id, deadline.event_id,
            reason="deadline_exceeded", lease=lease, clock=self.clock)
        self.assertEqual(updated.status, "needs_human")

    def test_concurrent_cancel_same_key_reuses_command_and_distinct_key_conflicts(self):
        task, attempt, _ = self.active()
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait()
            conn = storage.connect(self.db)
            try:
                return recovery.request_action(conn, task_id=task.id, attempt_id=attempt.id,
                    action="cancel", actor="human", reason="inspect", idempotency_key="cancel-one", clock=self.clock).id
            finally: conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with self.assertRaises(ConflictError): self.request(task, attempt, "cancel-two", action="cancel")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commands WHERE kind='recovery.cancel'").fetchone()[0], 1)

    def test_cancel_unknown_reconciles_no_stop_or_confirmed_stop(self):
        task, attempt, _ = self.active()
        cancel = self.request(task, attempt, "cancel-live", action="cancel")
        commands.claim_command(self.conn, cancel.id, clock=self.clock)
        commands.mark_command_unknown(self.conn, cancel.id, note="lost response", clock=self.clock)
        lease = self.lease()
        recovery.resolve_unknown(self.conn, attempt.id, command_id=cancel.id, lease=lease,
            resolution="active", evidence=self.evidence(True), actor="human", reason="inspect", clock=self.clock)
        self.assertEqual(domain.get_attempt(self.conn, attempt.id).status, "active")
        self.assertEqual(commands.get_command(self.conn, cancel.id).state, "expired")

        # A fresh fixture exercises verified stop after an ambiguous result.
        task2, attempt2, _ = self.active(self.task())
        cmd = self.request(task2, attempt2, "cancel-stopped", action="cancel")
        commands.claim_command(self.conn, cmd.id, clock=self.clock)
        commands.mark_command_unknown(self.conn, cmd.id, note="lost response", clock=self.clock)
        recovery.resolve_unknown(self.conn, attempt2.id, command_id=cmd.id, lease=lease,
            resolution="cancelled", evidence=self.evidence(False), actor="human", reason="inspect", clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, task2.id).status, "cancelled")
        self.assertEqual(commands.get_command(self.conn, cmd.id).state, "confirmed")

    def test_active_restore_requires_current_brief_task_version(self):
        task, attempt, _ = self.active()
        domain.transition_attempt(self.conn, attempt.id, "runtime_lost", clock=self.clock)
        # A Task edit after the immutable Brief was made leaves the Brief stale.
        # The reconcile request must bind the current Task version so this test
        # reaches the active-restore Brief-version guard itself.
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET version=version+1 WHERE id=?", (task.id,))
        cmd = self.request(task, attempt, "reconcile-active")
        commands.claim_command(self.conn, cmd.id, clock=self.clock)
        with self.assertRaisesRegex(ConflictError, "Task evidence version changed"):
            recovery.resolve_unknown(self.conn, attempt.id, command_id=cmd.id, lease=self.lease(),
                resolution="active", evidence=self.evidence(True), actor="human", reason="inspect", clock=self.clock)
        self.assertEqual(domain.get_attempt(self.conn, attempt.id).status, "unknown")

    def test_retry_approval_cap_and_previous_outcome(self):
        task = self.task(max_attempts=1)
        attempt = domain.create_attempt(self.conn, task.id, expected_version=task.version, clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "fail", clock=self.clock)
        task = domain.get_task(self.conn, task.id)
        with self.assertRaises(IllegalTransitionError):
            recovery.approve_retry(self.conn, task.id, actor="human", reason="retry", idempotency_key="retry-cap", lease=self.lease(), clock=self.clock)

    def test_retry_approval_success_and_same_key_exact_replay(self):
        task = self.task(max_attempts=2)
        attempt = domain.create_attempt(self.conn, task.id, expected_version=task.version, clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "fail", clock=self.clock)
        before = domain.get_task(self.conn, task.id)
        lease = self.lease()

        approved = recovery.approve_retry(self.conn, task.id, actor="human", reason="retry after failure",
            idempotency_key="retry-approved-once", lease=lease, clock=self.clock)
        self.assertEqual(approved.status, "ready")
        self.assertEqual(approved.version, before.version + 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0], 1)

        command = self.conn.execute("SELECT state,payload_json,evidence_json,result_json FROM commands WHERE idempotency_key=?",
            ("retry-approved-once",)).fetchone()
        payload, evidence = json.loads(command[1]), json.loads(command[2])
        self.assertEqual(command[0], "confirmed")
        self.assertEqual(payload["task_version"], before.version)
        self.assertEqual(evidence["task_version"], before.version)
        self.assertEqual(json.loads(command[3])["task_version"], approved.version)

        event_count = self.conn.execute("SELECT COUNT(*) FROM events WHERE kind LIKE 'command.%'").fetchone()[0]
        replay = recovery.approve_retry(self.conn, task.id, actor="human", reason="retry after failure",
            idempotency_key="retry-approved-once", lease=lease, clock=self.clock)
        self.assertEqual(replay, approved)
        self.assertEqual(domain.get_task(self.conn, task.id).version, approved.version)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind LIKE 'command.%'").fetchone()[0], event_count)


if __name__ == "__main__": unittest.main()
