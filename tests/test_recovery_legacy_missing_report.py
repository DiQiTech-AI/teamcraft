"""Focused contracts for evidence-free legacy Attempt recovery."""
from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from teamcraft.api import create_app
    from teamcraft.api.command import CommandService, IDEMPOTENCY_HEADER
    _WEB_OK = True
except ImportError:
    _WEB_OK = False

from teamcraft import commands, domain, events, leader, recovery, storage
from teamcraft.errors import ConflictError, IllegalTransitionError


TEAMCTL_PATH = (Path(__file__).resolve().parents[1]
                / ".agents/skills/teamcraft/scripts/teamctl.py")


def load_teamctl():
    spec = importlib.util.spec_from_file_location("teamctl_legacy_test", TEAMCTL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TEAMCTL = load_teamctl()


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)

    def now(self):
        return self.value


class LegacyMissingReportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name).resolve())
        self.db = Path(self.temp.name) / "state.sqlite"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        self.project = domain.create_project(
            self.conn, "project", self.root, clock=self.clock
        )

    def lease(self):
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "test-owner",
            ttl_seconds=60, clock=self.clock,
        )

    def legacy(self, *, max_attempts=3, worktree_path="root"):
        task = domain.create_task(
            self.conn, self.project.id, "legacy", max_attempts=max_attempts,
            clock=self.clock,
        )
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version, clock=self.clock,
        )
        path = self.root if worktree_path == "root" else worktree_path
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version,
            worktree_path=path, clock=self.clock,
        )
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated", clock=self.clock)
        attempt = domain.transition_attempt(
            self.conn, attempt.id, "finish", clock=self.clock
        )
        task = domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version,
            clock=self.clock,
        )
        return task, attempt

    def disposition(self, task, attempt, *, key="legacy-one", actor="owner",
                    reason="missing historical result", version=None):
        return recovery.disposition_legacy_missing_report(
            self.conn, task.id, attempt_id=attempt.id,
            expected_version=task.version if version is None else version,
            actor=actor, reason=reason, idempotency_key=key,
            lease=self.lease(), clock=self.clock,
        )

    def counts(self):
        return {
            "commands": self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0],
            "events": self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

    def insert_report(self, task, attempt, *, final=False):
        kind = "final" if final else "progress"
        outcome = "completed" if final else None
        digest = ("f" if final else "a") * 64
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO worker_reports (attempt_id,task_id,task_version,brief_digest,"
                "report_kind,outcome,milestone_id,report_digest,artifact_digest,report_json,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (attempt.id, task.id, task.version, "b" * 64, kind, outcome,
                 "final" if final else "progress", digest, None, "{}",
                 self.clock.now().isoformat()),
            )
        report_id = self.conn.execute(
            "SELECT id FROM worker_reports WHERE attempt_id=? AND report_digest=?",
            (attempt.id, digest),
        ).fetchone()
        return int(report_id[0]), digest

    def confirmed_command(self, kind, payload):
        submission = commands.submit_command(
            self.conn, idempotency_key=f"{kind}-{payload.get('attempt_id', 'x')}",
            kind=kind, payload_json=json.dumps(payload), evidence_json="{}",
            clock=self.clock,
        )
        command = submission.command
        commands.claim_command(self.conn, command.id, clock=self.clock)
        commands.confirm_command(self.conn, command.id, result_json="{}", clock=self.clock)
        return command

    def insert_verification(self, task, attempt, *, approval=False):
        report_id, report_digest = self.insert_report(task, attempt, final=False)
        command = self.confirmed_command(
            "verification.run.fixture", {"attempt_id": attempt.id}
        )
        verification_id = f"ver_{attempt.id}"
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO verifications (id,command_id,project_id,task_id,attempt_id,"
                "attempt_no,task_version,dispatch_task_version,brief_digest,worker_report_id,"
                "worker_report_digest,config_version,rules_version,config_digest,argv_json,"
                "status,exit_code,artifact,artifact_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (verification_id, command.id, self.project.id, task.id, attempt.id,
                 attempt.attempt_no, task.version, task.version, "b" * 64, report_id,
                 report_digest, 1, "rules", "c" * 64, "[]", "fail", 1, b"x",
                 "d" * 64, self.clock.now().isoformat()),
            )
        if approval:
            approval_command = self.confirmed_command(
                "verification.approval.fixture", {"attempt_id": attempt.id}
            )
            with storage.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO approvals (id,command_id,verification_id,project_id,task_id,"
                    "attempt_id,attempt_no,task_version,brief_digest,artifact_digest,"
                    "config_version,rules_version,config_digest,proposed_action,decision,"
                    "actor,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"approval_{attempt.id}", approval_command.id, verification_id,
                     self.project.id, task.id, attempt.id, attempt.attempt_no,
                     task.version, "b" * 64, "d" * 64, 1, "rules", "c" * 64,
                     "accept_report", "reject", "owner", "fixture",
                     self.clock.now().isoformat()),
                )

    def insert_worktree_verification(self, task, attempt):
        report_id, report_digest = self.insert_report(task, attempt, final=False)
        command = self.confirmed_command(
            "verification.worktree_fixture", {"attempt_id": attempt.id}
        )
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO worktree_verifications (id,command_id,project_id,task_id,"
                "attempt_id,attempt_no,task_version,dispatch_task_version,brief_digest,"
                "worker_report_id,worker_report_digest,allocation_id,worktree_path,branch,"
                "base_sha,claims_json,profile,profile_version,config_digest,argv_json,status,"
                "exit_code,report_json,report_digest,log_relpath,log_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"wv_{attempt.id}", command.id, self.project.id, task.id, attempt.id,
                 attempt.attempt_no, task.version, task.version, "b" * 64,
                 report_id, report_digest, "allocation", self.root, "tc/task/attempt",
                 "e" * 40, "[]", "test", None, None, "[]", "fail", 1,
                 "{}", "9" * 64, None, None, self.clock.now().isoformat()),
            )

    def test_allowed_disposition_binds_command_and_preserves_attempt(self):
        task, attempt = self.legacy()
        before = domain.get_attempt(self.conn, attempt.id)
        updated = self.disposition(task, attempt)
        self.assertEqual((updated.status, updated.version), ("needs_human", task.version + 1))
        self.assertEqual(domain.get_attempt(self.conn, attempt.id), before)
        row = self.conn.execute(
            "SELECT state,payload_json,evidence_json FROM commands WHERE kind=?",
            (domain.LEGACY_MISSING_REPORT_COMMAND_KIND,),
        ).fetchone()
        self.assertEqual(row[0], "confirmed")
        payload, evidence = json.loads(row[1]), json.loads(row[2])
        self.assertEqual(
            (payload["task_id"], payload["attempt_id"], payload["task_version"],
             payload["actor"], payload["reason"]),
            (task.id, attempt.id, task.version, "owner", "missing historical result"),
        )
        self.assertRegex(evidence["digest"], r"^[0-9a-f]{64}$")
        event = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind=?",
            (domain.LEGACY_MISSING_REPORT_EVENT_KIND,),
        ).fetchone()
        self.assertEqual(json.loads(event[0])["task_version"], updated.version)

    def test_exact_replay_and_content_conflict(self):
        task, attempt = self.legacy()
        first = self.disposition(task, attempt)
        counts = self.counts()
        replay = self.disposition(task, attempt)
        self.assertEqual(replay, first)
        self.assertEqual(self.counts(), counts)
        with self.assertRaises(ConflictError):
            self.disposition(task, attempt, actor="different")
        self.assertEqual(self.counts(), counts)

    def test_bare_domain_event_and_wrong_bindings_leave_no_partial_writes(self):
        task, attempt = self.legacy()
        before = self.counts()
        with self.assertRaisesRegex(IllegalTransitionError, "claimed matching"):
            domain.apply_task_event(
                self.conn, task.id, domain.LEGACY_ATTEMPT_INCOMPLETE_EVENT,
                expected_version=task.version, clock=self.clock,
            )
        self.assertEqual(domain.get_task(self.conn, task.id), task)
        self.assertEqual(self.counts(), before)

        other_task, other_attempt = self.legacy()
        for kwargs in (
            {"attempt_id": other_attempt.id},
            {"version": task.version + 1},
            {"actor": ""},
            {"reason": ""},
        ):
            with self.subTest(kwargs=kwargs):
                snapshot = self.counts()
                call = dict(task=task, attempt=attempt, key=f"bad-{len(snapshot)}-{repr(kwargs)}")
                call.update(kwargs)
                with self.assertRaises(Exception):
                    if "attempt_id" in call:
                        recovery.disposition_legacy_missing_report(
                            self.conn, task.id, attempt_id=call["attempt_id"],
                            expected_version=task.version, actor="owner", reason="reason",
                            idempotency_key=call["key"], lease=self.lease(), clock=self.clock)
                    else:
                        self.disposition(call["task"], call["attempt"], key=call["key"],
                                         actor=call.get("actor", "owner"),
                                         reason=call.get("reason", "reason"),
                                         version=call.get("version"))
                self.assertEqual(self.counts(), snapshot)

    def test_rejects_null_nonroot_and_existing_evidence(self):
        for label, setup in (
            ("null_path", lambda task, attempt: None),
            ("nonroot_path", lambda task, attempt: None),
            ("final_report", lambda task, attempt: self.insert_report(task, attempt, final=True)),
            ("worktree", lambda task, attempt: events.append_event(
                self.conn, "worktree_allocated", project_id=self.project.id,
                task_id=task.id, attempt_id=attempt.id, payload_json="{}", clock=self.clock)),
            ("worktree_verification", self.insert_worktree_verification),
            ("delivery", lambda task, attempt: events.append_event(
                self.conn, "delivery_commit_evidence", project_id=self.project.id,
                task_id=task.id, attempt_id=attempt.id, payload_json="{}", clock=self.clock)),
            ("verification", lambda task, attempt: self.insert_verification(task, attempt)),
            ("approval", lambda task, attempt: self.insert_verification(task, attempt, approval=True)),
        ):
            with self.subTest(label=label):
                path = None if label == "null_path" else ("/other" if label == "nonroot_path" else "root")
                task, attempt = self.legacy(worktree_path=path)
                setup(task, attempt)
                before = self.counts()
                with self.assertRaises((ConflictError, IllegalTransitionError)):
                    self.disposition(task, attempt, key=f"reject-{label}")
                self.assertEqual(domain.get_task(self.conn, task.id), task)
                self.assertEqual(self.counts(), before)

    def test_active_and_unknown_attempts_are_refused_without_writes(self):
        task = domain.create_task(self.conn, self.project.id, "live", clock=self.clock)
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version, clock=self.clock,
        )
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version,
            worktree_path=self.root, clock=self.clock,
        )
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        attempt = domain.transition_attempt(
            self.conn, attempt.id, "activated", clock=self.clock
        )
        for state in ("active", "unknown"):
            if state == "unknown":
                attempt = domain.transition_attempt(
                    self.conn, attempt.id, "runtime_lost", clock=self.clock
                )
            before = self.counts()
            with self.subTest(state=state), self.assertRaises(IllegalTransitionError):
                recovery.disposition_legacy_missing_report(
                    self.conn, task.id, attempt_id=attempt.id,
                    expected_version=domain.get_task(self.conn, task.id).version,
                    actor="owner", reason="invalid live attempt",
                    idempotency_key=f"live-{state}", lease=self.lease(),
                    clock=self.clock,
                )
            self.assertEqual(self.counts(), before)

    def test_rejects_unresolved_verification_and_delivery_commands(self):
        for kind in ("verification.run", "verification.approval", "delivery.local_commit"):
            with self.subTest(kind=kind):
                task, attempt = self.legacy()
                commands.submit_command(
                    self.conn, idempotency_key=f"pending-{kind}", kind=kind,
                    payload_json=json.dumps({"attempt_id": attempt.id}),
                    evidence_json="{}", clock=self.clock,
                )
                before = self.counts()
                with self.assertRaises(ConflictError):
                    self.disposition(task, attempt, key=f"reject-{kind}")
                self.assertEqual(domain.get_task(self.conn, task.id), task)
                self.assertEqual(self.counts(), before)

    def test_retry_is_separate_rechecks_facts_and_preserves_attempt(self):
        task, attempt = self.legacy()
        before_attempt = domain.get_attempt(self.conn, attempt.id)
        needs_human = self.disposition(task, attempt)
        approved = recovery.approve_retry(
            self.conn, task.id, actor="owner", reason="approved fresh attempt",
            idempotency_key="retry-legacy", lease=self.lease(), clock=self.clock,
        )
        self.assertEqual((approved.status, approved.version),
                         ("ready", needs_human.version + 1))
        self.assertEqual(domain.get_attempt(self.conn, attempt.id), before_attempt)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)
        ).fetchone()[0], 1)

        task2, attempt2 = self.legacy()
        needs_human2 = self.disposition(task2, attempt2, key="legacy-two")
        events.append_event(
            self.conn, "delivery_commit_evidence", project_id=self.project.id,
            task_id=task2.id, attempt_id=attempt2.id, payload_json="{}", clock=self.clock,
        )
        with self.assertRaises(ConflictError):
            recovery.approve_retry(
                self.conn, task2.id, actor="owner", reason="retry",
                idempotency_key="retry-stale-evidence", lease=self.lease(), clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, task2.id), needs_human2)

    def test_retry_rejects_version_drift_max_attempts_and_unfinished_dependency(self):
        task, attempt = self.legacy()
        self.disposition(task, attempt)
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET version=version+1 WHERE id=?", (task.id,))
        with self.assertRaises(IllegalTransitionError):
            recovery.approve_retry(
                self.conn, task.id, actor="owner", reason="retry",
                idempotency_key="retry-version-drift", lease=self.lease(), clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "needs_human")

        capped, capped_attempt = self.legacy(max_attempts=1)
        capped_state = self.disposition(capped, capped_attempt, key="legacy-cap")
        with self.assertRaisesRegex(IllegalTransitionError, "cap"):
            recovery.approve_retry(
                self.conn, capped.id, actor="owner", reason="retry",
                idempotency_key="retry-cap", lease=self.lease(), clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, capped.id), capped_state)

        dependent, dependent_attempt = self.legacy()
        dependent_state = self.disposition(
            dependent, dependent_attempt, key="legacy-dependency"
        )
        blocker = domain.create_task(self.conn, self.project.id, "blocker", clock=self.clock)
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO task_dependencies(task_id,depends_on_id,created_at) VALUES (?,?,?)",
                (dependent.id, blocker.id, self.clock.now().isoformat()),
            )
        with self.assertRaisesRegex(IllegalTransitionError, "dependencies"):
            recovery.approve_retry(
                self.conn, dependent.id, actor="owner", reason="retry",
                idempotency_key="retry-dependency", lease=self.lease(), clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, dependent.id), dependent_state)

    @unittest.skipUnless(_WEB_OK, "FastAPI test stack unavailable")
    def test_api_requires_auth_attempt_and_current_version_then_applies(self):
        task, attempt = self.legacy()
        service = CommandService(str(self.db))
        app = create_app(command_service=service)
        client = TestClient(app)
        token = app.state.token_store.ensure()
        path = f"/api/v1/tasks/{task.id}/commands"
        body = {"action": "legacy_incomplete", "expected_version": task.version,
                "actor": "owner", "reason": "legacy result absent",
                "attempt_id": attempt.id}
        self.assertEqual(client.post(path, json=body).status_code, 401)
        headers = {"Authorization": f"Bearer {token}", "X-CSRF-Token": token,
                   IDEMPOTENCY_HEADER: "api-legacy"}
        missing = dict(body)
        missing.pop("attempt_id")
        self.assertEqual(client.post(path, headers={**headers, IDEMPOTENCY_HEADER: "api-missing"},
                                     json=missing).status_code, 422)
        stale = dict(body)
        stale["expected_version"] += 1
        self.assertEqual(client.post(path, headers={**headers, IDEMPOTENCY_HEADER: "api-stale"},
                                     json=stale).status_code, 409)
        response = client.post(path, headers=headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task"]["status"], "needs_human")

    def test_cli_parser_and_handler_forward_required_binding(self):
        parser = TEAMCTL.build_parser()
        argv = ["recovery-legacy-incomplete", "--task-id", "task-1",
                "--attempt-id", "attempt-1", "--expected-version", "3",
                "--actor", "owner", "--reason", "missing", "--idempotency-key", "key"]
        args = parser.parse_args(argv)
        self.assertIs(args.handler, TEAMCTL.recovery_legacy_incomplete)
        with self.assertRaises(SystemExit):
            parser.parse_args(["recovery-legacy-incomplete", "--task-id", "task-1"])

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = ("command-1",)
        store = mock.Mock()
        store._connect.return_value = conn
        lease = SimpleNamespace(scope="project:p", holder_id="owner", epoch=1)
        updated = SimpleNamespace(id="task-1", status="needs_human", version=4)
        with mock.patch.object(TEAMCTL, "resolve_project_root", return_value=Path(self.root)), \
             mock.patch.object(TEAMCTL, "StateStore", return_value=store), \
             mock.patch.object(TEAMCTL, "_recovery_lease", return_value=lease), \
             mock.patch.object(TEAMCTL._recovery, "disposition_legacy_missing_report",
                               return_value=updated) as apply, \
             mock.patch("builtins.print"):
            self.assertEqual(TEAMCTL.recovery_legacy_incomplete(args), 0)
        apply.assert_called_once_with(
            conn, "task-1", attempt_id="attempt-1", expected_version=3,
            actor="owner", reason="missing", idempotency_key="key", lease=lease)
        store.release_lease.assert_called_once_with(lease)


if __name__ == "__main__":
    unittest.main()
