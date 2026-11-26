"""Fail-closed retry gate for a completed ship Attempt whose verifier failed."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from teamcraft import (
    briefs,
    domain,
    events,
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


def _git(*args: str, cwd: Path) -> None:
    subprocess.run([*GIT, *args], cwd=cwd, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class FailedVerifierRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tc-retry-verifier-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git("init", "-q", cwd=self.repo)
        (self.repo / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "base.py").write_text("BASE = 1\n", encoding="utf-8")
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "base", cwd=self.repo)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "p", str(self.repo))
        self.artifacts = self.root / "artifacts"

    def lease(self):
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "human",
            ttl_seconds=600,
        )

    def failed_attempt(self):
        task = domain.create_task(
            self.conn, self.project.id, "ship", kind="ship", max_attempts=2,
            ownership_json=json.dumps(["src/**"]),
        )
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version,
        )
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version,
        )
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        allocation, _ = worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.repo),
        )
        (Path(allocation.worktree_path) / "src" / "change.py").write_text(
            "CHANGE = 1\n", encoding="utf-8",
        )
        brief = briefs.create_attempt_brief(
            self.conn, attempt.id, content="verify then deliver",
        )
        report = {
            "schema_version": 1, "task_id": task.id,
            "attempt_id": attempt.id, "task_version": brief.task_version,
            "brief_digest": brief.brief_digest, "report_kind": "final",
            "outcome": "completed", "milestone_id": "final",
            "summary": "implementation complete", "artifact_digest": None,
            "usage": {"total_tokens": None},
        }
        worker_reports.record_worker_report(self.conn, report)
        domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version,
        )
        verification.configure_verifier_profile(
            self.conn, self.project.id, profile="test",
            argv=[sys.executable, "-c", "raise SystemExit(7)"],
            timeout_seconds=10, actor="human", idempotency_key="cfg-fail",
            lease=self.lease(),
        )
        result = verification.run_worktree_verifier(
            self.conn, attempt.id, profile="test",
            idempotency_key=f"run-fail-{attempt.id}",
            lease=self.lease(), artifact_root=str(self.artifacts),
        )
        self.assertEqual(result.status, "fail")
        task = domain.get_task(self.conn, task.id)
        self.assertEqual(task.status, "needs_human")
        return task, domain.get_attempt(self.conn, attempt.id), result

    def test_completed_attempt_with_bound_failed_verifier_can_retry(self):
        task, attempt, _ = self.failed_attempt()

        approved = recovery.approve_retry(
            self.conn, task.id, actor="human", reason="fix verifier failure",
            idempotency_key="retry-failed-verifier", lease=self.lease(),
        )

        self.assertEqual(approved.status, "ready")
        self.assertEqual(approved.version, task.version + 1)
        self.assertEqual(domain.get_attempt(self.conn, attempt.id).status, "finished")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,),
        ).fetchone()[0], 1)

    def test_task_drift_after_failure_refuses_retry(self):
        task, _, _ = self.failed_attempt()
        task = domain.update_task(
            self.conn, task.id, expected_version=task.version, priority=51,
        )

        with self.assertRaisesRegex(
            IllegalTransitionError, "previous attempt is not explicitly retryable",
        ):
            recovery.approve_retry(
                self.conn, task.id, actor="human", reason="stale retry",
                idempotency_key="retry-after-drift", lease=self.lease(),
            )

    def test_delivery_artifact_and_unsatisfied_dependency_each_refuse(self):
        for blocker in ("delivery", "dependency"):
            with self.subTest(blocker=blocker):
                task, attempt, _ = self.failed_attempt()
                if blocker == "delivery":
                    events.append_event(
                        self.conn, domain.COMMIT_EVIDENCE_EVENT_KIND,
                        project_id=self.project.id, task_id=task.id,
                        attempt_id=attempt.id, payload_json="{}",
                    )
                else:
                    dependency = domain.create_task(
                        self.conn, self.project.id, "unfinished dependency",
                    )
                    with storage.transaction(self.conn):
                        self.conn.execute(
                            "INSERT INTO task_dependencies"
                            " (task_id, depends_on_id, created_at) VALUES (?,?,?)",
                            (task.id, dependency.id, task.updated_at),
                        )
                with self.assertRaises(IllegalTransitionError):
                    recovery.approve_retry(
                        self.conn, task.id, actor="human", reason=blocker,
                        idempotency_key=f"retry-blocked-{blocker}-{task.id}",
                        lease=self.lease(),
                    )


if __name__ == "__main__":
    unittest.main()
