"""Real-Git integration contracts for stale verifier-profile retry recovery.

Covers D1 (VERIFYING + ship evidence + verifier-profile drift), D2 (the one
audited exact local-only policy rebind, including PASS-at-N / Commit-at-N+1)
and multiple required verifier profiles.  Nothing here relies on mock
Connections: every fact is derived from a temporary SQLite database and a real
Git worktree.
"""
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
    leader,
    recovery,
    storage,
    verification,
    worker_reports,
    worktrees,
)
from teamcraft.errors import IllegalTransitionError

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


class StaleVerifierProfileRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tc-stale-profile-")
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
            self.conn, "stale-profile", str(self.repo))

    # -- fixtures ------------------------------------------------------------

    def lease(self) -> leader.Lease:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "tester",
            ttl_seconds=3600,
        )

    def start_ship(self, *, policy: dict | None = None):
        """READY -> (optional policy) -> Attempt/worktree/brief/report -> VERIFYING."""
        task = domain.create_task(
            self.conn, self.project.id, "deliver", kind="ship",
            ownership_json=json.dumps(["src/**"]),
        )
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version,
        )
        if policy is not None:
            task = domain.set_execution_policy(
                self.conn, task.id, expected_version=task.version,
                policy=policy, actor="ceo", reason="stale-profile test policy",
            )
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version)
        domain.register_session(
            self.conn, attempt.id, "opencode",
            native_session_id=f"native-{attempt.id}", pane_id="pane-stale",
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
            self.conn, attempt.id, content="stale-profile brief")
        worker_reports.record_worker_report(self.conn, {
            "schema_version": 1, "task_id": task.id,
            "attempt_id": attempt.id, "task_version": brief.task_version,
            "brief_digest": brief.brief_digest, "report_kind": "final",
            "outcome": "completed", "milestone_id": "final",
            "summary": "finished", "artifact_digest": None,
            "usage": {"total_tokens": None},
        })
        domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version,
        )
        return (
            domain.get_task(self.conn, task.id),
            domain.get_attempt(self.conn, attempt.id),
            brief,
            allocation,
        )

    def configure(self, profile: str, *, key: str, marker: str = "ok") -> None:
        verification.configure_verifier_profile(
            self.conn, self.project.id, profile=profile,
            argv=[sys.executable, "-c", f"print('{marker}')"],
            timeout_seconds=30, actor="human",
            idempotency_key=key, lease=self.lease(),
        )

    def run_profile(self, attempt, profile: str, *, key: str):
        result = verification.run_worktree_verifier(
            self.conn, attempt.id, profile=profile,
            idempotency_key=key, lease=self.lease(),
        )
        self.assertEqual(result.status, "pass", result.status)
        return result

    def add_local_only_policy(self, task):
        return domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"delivery": "local_only"}, actor="ceo",
            reason="authorized delivery correction",
        )

    def commit(self, attempt, *, key: str):
        return delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key=key, lease=self.lease())

    def ship(self, attempt, *, key: str):
        return delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key=key, lease=self.lease())

    def invalidation_payloads(self, task_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind='delivery.approval_invalidated'"
            " AND task_id=? ORDER BY seq", (task_id,),
        ).fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def approve(self, task_id: str, *, key: str):
        return recovery.approve_retry(
            self.conn, task_id, actor="human",
            reason="stale verifier profile recovery",
            idempotency_key=key, lease=self.lease(),
        )

    # -- D1: VERIFYING + ship evidence + profile drift -----------------------

    def test_verifying_ship_with_drifted_profile_retries_and_invalidates(self):
        task, attempt, brief, _ = self.start_ship(
            policy={"delivery": "local_only"})
        self.configure("test", key="cfg-test-v1")
        pass_row = self.run_profile(attempt, "test", key="run-test-v1")
        self.assertEqual(pass_row.task_version, brief.task_version + 1)
        commit = self.commit(attempt, key="commit-d1")
        ship = self.ship(attempt, key="ship-d1")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")
        self.assertEqual(ship.task_version, task.version)

        # A later human-approved profile version moves the required snapshot.
        self.configure("test", key="cfg-test-v2", marker="changed")

        approved = self.approve(task.id, key="retry-d1")

        self.assertEqual(approved.status, "ready")
        self.assertEqual(approved.version, task.version + 1)
        # No Attempt is created by the approval itself.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,),
        ).fetchone()[0], 1)
        # The historical chain is immutably retained but explicitly retired.
        self.assertIsNotNone(delivery.load_ship_evidence(self.conn, attempt.id))
        payloads = self.invalidation_payloads(task.id)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["reason"], "verifier_profile_changed")
        self.assertEqual(payloads[0]["ship_evidence_id"], ship.event_id)
        self.assertEqual(payloads[0]["commit_sha"], commit.commit_sha)
        # New Attempt reservation starts from READY, not by reusing old PASS.
        self.assertFalse(delivery.ship_evidence_is_current(
            self.conn, ship, live_git=False).ok)

    def test_verifying_ship_with_current_profile_still_refuses(self):
        task, attempt, _, _ = self.start_ship(
            policy={"delivery": "local_only"})
        self.configure("test", key="cfg-current-v1")
        self.run_profile(attempt, "test", key="run-current-v1")
        self.commit(attempt, key="commit-current")
        self.ship(attempt, key="ship-current")

        with self.assertRaisesRegex(
            IllegalTransitionError, "not explicitly retryable",
        ):
            self.approve(task.id, key="retry-current")
        self.assertEqual(self.invalidation_payloads(task.id), [])

    def test_rejected_ship_approval_is_retired_by_stale_profile_retry(self):
        task, attempt, _, _ = self.start_ship(
            policy={"delivery": "local_only"})
        self.configure("test", key="cfg-reject-v1")
        self.run_profile(attempt, "test", key="run-reject-v1")
        commit = self.commit(attempt, key="commit-reject")
        ship = self.ship(attempt, key="ship-reject")
        rejected = delivery.approve_local_delivery(
            self.conn, ship.event_id, decision="reject",
            commit_sha=commit.commit_sha, branch=commit.branch,
            actor="human", reason="needs a fix",
            idempotency_key="reject-ship", lease=self.lease(),
        )
        self.assertEqual(rejected.decision, "reject")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")
        self.configure("test", key="cfg-reject-v2", marker="drifted")

        approved = self.approve(task.id, key="retry-reject")

        self.assertEqual(approved.status, "ready")
        payloads = self.invalidation_payloads(task.id)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["ship_evidence_id"], ship.event_id)

    # -- D2: exact local-only policy rebind ----------------------------------

    def test_exact_policy_rebind_without_ship_allows_stale_profile_retry(self):
        task, attempt, brief, _ = self.start_ship()
        self.configure("test", key="cfg-rebind-v1")
        pass_row = self.run_profile(attempt, "test", key="run-rebind-v1")
        self.assertEqual(pass_row.task_version, brief.task_version + 1)

        task = self.add_local_only_policy(task)
        self.assertEqual(task.version, brief.task_version + 2)
        commit = self.commit(attempt, key="commit-rebind")
        self.assertEqual(commit.task_version, task.version)
        # No ship evidence at all: the PASS-only VERIFYING path must recover.
        self.assertIsNone(delivery.load_ship_evidence(self.conn, attempt.id))
        self.configure("test", key="cfg-rebind-v2", marker="drifted")

        approved = self.approve(task.id, key="retry-rebind")

        self.assertEqual(approved.status, "ready")
        rows = verification.list_worktree_verifications(self.conn, attempt.id)
        self.assertEqual([row.task_version for row in rows],
                         [brief.task_version + 1])
        self.assertEqual(self.invalidation_payloads(task.id), [])

    def test_exact_policy_rebind_with_ship_and_drift_retries(self):
        task, attempt, brief, _ = self.start_ship()
        self.configure("test", key="cfg-ship-rebind-v1")
        pass_row = self.run_profile(attempt, "test", key="run-ship-rebind-v1")
        self.assertEqual(pass_row.task_version, brief.task_version + 1)

        task = self.add_local_only_policy(task)
        commit = self.commit(attempt, key="commit-ship-rebind")
        ship = self.ship(attempt, key="ship-rebind")
        self.assertEqual(commit.task_version, brief.task_version + 2)
        self.assertEqual(ship.task_version, brief.task_version + 2)
        self.configure("test", key="cfg-ship-rebind-v2", marker="drifted")

        approved = self.approve(task.id, key="retry-ship-rebind")

        self.assertEqual(approved.status, "ready")
        payloads = self.invalidation_payloads(task.id)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["ship_evidence_id"], ship.event_id)

    def test_arbitrary_task_version_drift_is_not_accepted_as_rebind(self):
        task, attempt, brief, _ = self.start_ship()
        self.configure("test", key="cfg-drift-v1")
        self.run_profile(attempt, "test", key="run-drift-v1")
        task = self.add_local_only_policy(task)
        self.commit(attempt, key="commit-drift")
        self.ship(attempt, key="ship-drift")
        # A second, unrelated Task CAS is not the audited single policy edge.
        task = domain.update_task(
            self.conn, task.id, expected_version=task.version, title="edited")
        self.configure("test", key="cfg-drift-v2", marker="drifted")

        with self.assertRaisesRegex(
            IllegalTransitionError, "not explicitly retryable",
        ):
            self.approve(task.id, key="retry-drift")
        self.assertEqual(self.invalidation_payloads(task.id), [])

    # -- multiple required profiles ------------------------------------------

    def test_multiple_profiles_one_drifted_still_retries(self):
        task, attempt, _, _ = self.start_ship(
            policy={"delivery": "local_only"})
        self.configure("test", key="cfg-multi-test-v1")
        self.configure("lint", key="cfg-multi-lint-v1")
        self.run_profile(attempt, "test", key="run-multi-test-v1")
        self.run_profile(attempt, "lint", key="run-multi-lint-v1")
        self.commit(attempt, key="commit-multi")
        ship = self.ship(attempt, key="ship-multi")
        self.assertEqual(
            {entry["profile"] for entry in ship.verifiers}, {"test", "lint"})
        # Only one of the two pinned profiles drifts.
        self.configure("lint", key="cfg-multi-lint-v2", marker="changed")

        approved = self.approve(task.id, key="retry-multi")

        self.assertEqual(approved.status, "ready")
        payloads = self.invalidation_payloads(task.id)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["ship_evidence_id"], ship.event_id)


if __name__ == "__main__":
    unittest.main()
