"""Fail-closed verification recovery for one post-completion policy CAS."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from teamcraft import (
    briefs,
    delivery,
    domain,
    events,
    leader,
    storage,
    verification,
    worker_reports,
    worktrees,
)
from teamcraft.errors import ConflictError, ValidationError

GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false", "-c", "user.name=T",
    "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
]


def git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        [*GIT, *args], cwd=str(cwd), capture_output=True, check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout.decode("utf-8", "surrogateescape").strip()


class PolicyRebindVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tc-policy-rebind-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        git("config", "user.name", "T", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        (self.repo / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "base", cwd=self.repo)

        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(
            self.conn, "policy-rebind", str(self.repo))

    def lease(self) -> leader.Lease:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "tester",
            ttl_seconds=3600,
        )

    def finished_ship(self, *, worker_done: bool = True):
        task = domain.create_task(
            self.conn, self.project.id, "ship", kind="ship",
            ownership_json=json.dumps(["src/**"]),
        )
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version,
        )
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version)
        domain.register_session(
            self.conn, attempt.id, "opencode",
            native_session_id=f"native-{attempt.id}", pane_id="pane-policy",
            state="working",
        )
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        allocation, _ = worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.repo),
        )
        (Path(allocation.worktree_path) / "src" / "feature.py").write_text(
            "feature\n", encoding="utf-8")
        brief = briefs.create_attempt_brief(
            self.conn, attempt.id, content="policy rebind brief")
        report = {
            "schema_version": 1, "task_id": task.id,
            "attempt_id": attempt.id, "task_version": brief.task_version,
            "brief_digest": brief.brief_digest, "report_kind": "final",
            "outcome": "completed", "milestone_id": "final",
            "summary": "finished", "artifact_digest": None,
            "usage": {"total_tokens": None},
        }
        worker_reports.record_worker_report(self.conn, report)
        if worker_done:
            live = domain.get_task(self.conn, task.id)
            domain.apply_task_event(
                self.conn, task.id, "worker_reported_done",
                expected_version=live.version,
            )
        return (
            domain.get_task(self.conn, task.id),
            domain.get_attempt(self.conn, attempt.id),
            brief,
            allocation,
        )

    def add_local_only_policy(self, task):
        return domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"delivery": "local_only"}, actor="ceo",
            reason="authorized delivery correction",
        )

    def assert_stale(self, attempt) -> None:
        with self.assertRaisesRegex(
            ValidationError, "outside worker_reported_done",
        ):
            verification._bound_facts(self.conn, attempt.id)

    def configure_profile(self) -> None:
        verification.configure_verifier_profile(
            self.conn, self.project.id, profile="test",
            argv=[sys.executable, "-c", "print('ok')"],
            timeout_seconds=30, actor="human",
            idempotency_key="profile-test", lease=self.lease(),
        )

    def run_profile(self, attempt, key: str):
        return verification.run_worktree_verifier(
            self.conn, attempt.id, profile="test", idempotency_key=key,
            lease=self.lease(),
        )

    def test_exact_post_completion_local_only_update_rebinds_current_version(self):
        task, attempt, brief, _ = self.finished_ship()
        task = self.add_local_only_policy(task)

        bound = verification._bound_facts(self.conn, attempt.id)

        self.assertEqual(bound[0].version, brief.task_version + 2)
        self.assertEqual(bound[4], brief.task_version)
        self.assertEqual(bound[2].brief_digest, brief.brief_digest)

    def test_existing_pass_remains_current_and_delivers_at_current_version(self):
        task, attempt, brief, _ = self.finished_ship()
        self.configure_profile()
        verified = self.run_profile(attempt, "first-pass")
        self.assertEqual(verified.status, "pass")
        self.assertEqual(verified.task_version, brief.task_version + 1)

        task = self.add_local_only_policy(task)
        self.assertTrue(verification.worktree_verification_is_current(
            self.conn, verified))
        with self.assertRaisesRegex(ConflictError, "already has settled"):
            self.run_profile(attempt, "duplicate-pass-refused")

        commit = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-after-rebind",
            lease=self.lease(),
        )
        shipped = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="ship-after-rebind",
            lease=self.lease(),
        )
        self.assertEqual(commit.task_version, task.version)
        self.assertEqual(shipped.task_version, task.version)
        self.assertEqual(shipped.dispatch_task_version, brief.task_version)

    def test_existing_commit_survives_exact_policy_rebind_and_ships_current_version(self):
        task, attempt, brief, _ = self.finished_ship()
        self.configure_profile()
        self.assertEqual(self.run_profile(attempt, "pass-before-policy").status,
                         "pass")
        commit = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-before-policy",
            lease=self.lease(),
        )
        self.assertEqual(commit.task_version, brief.task_version + 1)

        task = self.add_local_only_policy(task)

        current = delivery.commit_evidence_is_current(self.conn, commit)
        self.assertTrue(current.ok, current.problems)
        shipped = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="ship-after-policy",
            lease=self.lease(),
        )
        self.assertEqual(shipped.task_version, task.version)
        self.assertEqual(shipped.commit_evidence_id, commit.event_id)
        self.assertEqual(shipped.dispatch_task_version, brief.task_version)

    def test_existing_commit_stays_stale_for_non_exact_task_drift(self):
        cases = ("unrelated_edit", "different_policy", "repeated_policy")
        for case in cases:
            with self.subTest(case=case):
                task, attempt, _, _ = self.finished_ship()
                self.configure_profile()
                self.assertEqual(
                    self.run_profile(attempt, f"pass-{case}").status, "pass")
                commit = delivery.create_local_commit(
                    self.conn, attempt.id,
                    idempotency_key=f"commit-{case}", lease=self.lease(),
                )
                if case == "unrelated_edit":
                    domain.update_task(
                        self.conn, task.id, expected_version=task.version,
                        title="edited after commit",
                    )
                elif case == "different_policy":
                    domain.set_execution_policy(
                        self.conn, task.id, expected_version=task.version,
                        policy={"delivery": "ship"}, actor="ceo",
                        reason="wrong delivery mode",
                    )
                else:
                    task = self.add_local_only_policy(task)
                    self.add_local_only_policy(task)

                current = delivery.commit_evidence_is_current(
                    self.conn, commit, live_git=False)
                self.assertFalse(current.ok, case)
                self.assertTrue(
                    any(problem.startswith("version:")
                        for problem in current.problems),
                    current.problems,
                )
                self.assert_stale(attempt)

    def test_detached_existing_commit_remains_ineligible_after_exact_rebind(self):
        task, attempt, _, allocation = self.finished_ship()
        self.configure_profile()
        self.assertEqual(self.run_profile(attempt, "pass-before-detach").status,
                         "pass")
        commit = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-before-detach",
            lease=self.lease(),
        )
        self.add_local_only_policy(task)
        git("checkout", "--detach", commit.commit_sha,
            cwd=Path(allocation.worktree_path))

        current = delivery.commit_evidence_is_current(self.conn, commit)
        self.assertFalse(current.ok)
        self.assertTrue(
            any(problem.startswith("identity:") for problem in current.problems),
            current.problems,
        )
        with self.assertRaises(delivery.DeliveryRefusal) as raised:
            delivery.record_ship_evidence(
                self.conn, attempt.id, idempotency_key="ship-detached",
                lease=self.lease(),
            )
        self.assertEqual(raised.exception.kind, "drift")

    def test_unrelated_or_non_exact_drift_remains_stale(self):
        cases = (
            "unrelated_edit",
            "different_policy",
            "repeated_policy",
            "missing_event",
            "tampered_event",
            "policy_before_worker_done",
        )
        for case in cases:
            with self.subTest(case=case):
                task, attempt, brief, _ = self.finished_ship(
                    worker_done=case != "policy_before_worker_done")
                if case == "unrelated_edit":
                    domain.update_task(
                        self.conn, task.id, expected_version=task.version,
                        title="edited after completion",
                    )
                elif case == "different_policy":
                    domain.set_execution_policy(
                        self.conn, task.id, expected_version=task.version,
                        policy={"delivery": "ship"}, actor="ceo",
                        reason="wrong delivery mode",
                    )
                elif case == "repeated_policy":
                    task = self.add_local_only_policy(task)
                    self.add_local_only_policy(task)
                elif case == "missing_event":
                    with storage.transaction(self.conn):
                        self.conn.execute(
                            "UPDATE tasks SET version=version+1,"
                            " execution_policy_json=? WHERE id=?",
                            ('{"delivery":"local_only"}', task.id),
                        )
                elif case == "tampered_event":
                    with storage.transaction(self.conn):
                        self.conn.execute(
                            "UPDATE tasks SET version=version+1,"
                            " execution_policy_json=? WHERE id=?",
                            ('{"delivery":"local_only"}', task.id),
                        )
                        events.append_event(
                            self.conn, "task.policy_updated",
                            project_id=task.project_id, task_id=task.id,
                            payload_json=json.dumps({
                                "task_id": task.id,
                                "version": brief.task_version + 2,
                                "policy": {"delivery": "local_only"},
                                "requested": {"delivery": "ship"},
                                "actor": "ceo", "reason": "tampered",
                            }),
                        )
                elif case == "policy_before_worker_done":
                    task = self.add_local_only_policy(task)
                    domain.apply_task_event(
                        self.conn, task.id, "worker_reported_done",
                        expected_version=task.version,
                    )
                self.assert_stale(attempt)


if __name__ == "__main__":
    unittest.main()
