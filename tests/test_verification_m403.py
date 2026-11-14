"""M4-03 Worktree Verifier acceptance tests.

All Git worktrees and verifier artifacts live below temporary directories.
The project checkout is never used as a verifier target.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from teamcraft import (
    briefs,
    commands,
    domain,
    leader,
    storage,
    verification,
    worker_reports,
    worktrees,
)
from teamcraft.errors import ConflictError, StorageError, ValidationError


GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false", "-c", "user.name=T",
    "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        [*GIT, *args], cwd=str(cwd), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode:
        raise AssertionError(proc.stderr.decode("utf-8", "replace"))
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def init_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo


class WorktreeVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m403-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo = init_repo(self.base)
        self.db = self.base / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "demo", str(self.repo))
        self.artifacts = self.base / "artifacts"

    def lease(self) -> leader.Lease:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "m403-test",
            ttl_seconds=3700,
        )

    def make_verifying(self, *, title: str = "deliver"):
        task = domain.create_task(
            self.conn, self.project.id, title, kind="ship",
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
        worktree = Path(allocation.worktree_path)
        (worktree / "src" / "feature.py").write_text("feature\n", encoding="utf-8")
        brief = briefs.create_attempt_brief(
            self.conn, attempt.id, content="M4-03 verifier brief",
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
        return domain.get_task(self.conn, task.id), domain.get_attempt(
            self.conn, attempt.id), allocation

    def configure(self, *, profile: str = "test", argv=None, enabled=True,
                  timeout: int = 10, key: str | None = None):
        if argv is None:
            argv = [sys.executable, "-c", "print('ok')"]
        return verification.configure_verifier_profile(
            self.conn, self.project.id, profile=profile, argv=argv,
            timeout_seconds=timeout, actor="human",
            idempotency_key=key or f"cfg-{profile}", lease=self.lease(),
            enabled=enabled,
        )

    def run_verifier(self, attempt, *, profile="test", key="run", artifact_root=None):
        return verification.run_worktree_verifier(
            self.conn, attempt.id, profile=profile, idempotency_key=key,
            lease=self.lease(), artifact_root=str(artifact_root or self.artifacts),
        )

    def test_pass_pins_cwd_env_argv_and_artifact_digest(self):
        _, attempt, allocation = self.make_verifying()
        script = (
            "import json,os; print(json.dumps({'cwd':os.getcwd(),"
            "'home':os.environ['HOME'],'role':os.environ['TEAMCRAFT_ROLE'],"
            "'secret':os.environ.get('M403_SECRET'),'path':os.environ['PATH']}))"
        )
        argv = [sys.executable, "-c", script]
        self.configure(argv=argv)
        with mock.patch.dict(os.environ, {
            "M403_SECRET": "must-not-leak",
            "PATH": "/hostile/caller/path",
        }):
            result = self.run_verifier(attempt)

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.argv, tuple(argv))
        report = json.loads(result.report)
        child = json.loads(report["stdout"])
        self.assertEqual(child["cwd"], allocation.worktree_path)
        self.assertEqual(child["home"], allocation.worktree_path)
        self.assertEqual(child["role"], "verifier")
        self.assertIsNone(child["secret"])
        self.assertNotIn("/hostile/caller/path", child["path"])
        self.assertEqual(
            child["path"], verification._pinned_env(allocation.worktree_path)["PATH"])
        self.assertTrue(report["diff_gate_before"]["ok"])
        self.assertTrue(report["diff_gate_after"]["ok"])
        artifact = verification.read_worktree_artifact(
            self.conn, result.id, artifact_root=str(self.artifacts),
        )
        self.assertEqual(artifact["log_digest"], result.log_digest)
        self.assertEqual(artifact["report_digest"], result.report_digest)
        self.assertTrue(
            (self.artifacts / result.log_relpath).resolve().is_relative_to(
                self.artifacts.resolve()))

        (self.artifacts / result.log_relpath).write_text("tampered", encoding="utf-8")
        with self.assertRaises(StorageError):
            verification.read_worktree_artifact(
                self.conn, result.id, artifact_root=str(self.artifacts))

    def test_disabled_profile_is_skipped_without_execution(self):
        _, attempt, allocation = self.make_verifying()
        marker = Path(allocation.worktree_path) / "src" / "ran.txt"
        self.configure(
            enabled=False,
            argv=[sys.executable, "-c", f"open({str(marker)!r},'w').write('ran')"],
        )
        result = self.run_verifier(attempt)
        self.assertEqual(result.status, "skipped")
        self.assertIsNone(result.exit_code)
        self.assertFalse(marker.exists())
        self.assertEqual(domain.get_task(self.conn, result.task_id).status, "verifying")

    def test_profile_configuration_is_idempotent_and_append_only(self):
        first = self.configure()
        replay = self.configure()
        self.assertEqual(replay["id"], first["id"])
        with self.assertRaises(ConflictError):
            self.configure(argv=[sys.executable, "-c", "print('different')"])
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "UPDATE verifier_profiles SET enabled=0 WHERE id=?",
                    (first["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "DELETE FROM verifier_profiles WHERE id=?", (first["id"],))

    def test_artifact_path_rejects_traversal_and_symlink_escape(self):
        self.artifacts.mkdir()
        with self.assertRaises(ValidationError):
            verification._contained_artifact_path(
                str(self.artifacts), "../escape.log")
        outside = self.base / "outside"
        outside.mkdir()
        link = self.artifacts / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable")
        with self.assertRaises(ValidationError):
            verification._contained_artifact_path(
                str(self.artifacts), "link/escape.log")

    def test_nonzero_timeout_and_missing_command_never_pass(self):
        cases = [
            ([sys.executable, "-c", "raise SystemExit(7)"], 10, "fail"),
            ([sys.executable, "-c", "import time; time.sleep(5)"], 1, "error"),
            (["/definitely/missing/m403-verifier"], 10, "error"),
            ([sys.executable, "-c",
              "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"],
             10, "error"),
        ]
        for index, (argv, timeout, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                task, attempt, _ = self.make_verifying(title=f"case-{index}")
                self.configure(argv=argv, timeout=timeout, key=f"cfg-case-{index}")
                result = self.run_verifier(attempt, key=f"run-case-{index}")
                self.assertEqual(result.status, expected)
                self.assertNotEqual(result.status, "pass")
                self.assertEqual(domain.get_task(self.conn, task.id).status,
                                 "needs_human")
                replay = self.run_verifier(attempt, key=f"run-case-{index}")
                self.assertEqual(replay.id, result.id)
                with self.assertRaises((ConflictError, ValidationError)):
                    self.run_verifier(attempt, key=f"different-{index}")

    def test_missing_profile_refuses_without_command_or_result(self):
        _, attempt, _ = self.make_verifying()
        before = self.conn.execute(
            "SELECT COUNT(*) FROM commands WHERE kind=?",
            (verification.WORKTREE_RUN_KIND,),
        ).fetchone()[0]
        with self.assertRaises(ValidationError):
            self.run_verifier(attempt, profile="lint", key="missing-profile")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM commands WHERE kind=?",
            (verification.WORKTREE_RUN_KIND,),
        ).fetchone()[0], before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM worktree_verifications").fetchone()[0], 0)

    def test_diff_gate_refuses_out_of_scope_before_and_after(self):
        task, attempt, allocation = self.make_verifying(title="before")
        (Path(allocation.worktree_path) / "docs" / "outside.txt").write_text(
            "outside\n", encoding="utf-8")
        self.configure(key="cfg-before")
        before = self.run_verifier(attempt, key="run-before")
        self.assertEqual(before.status, "error")
        report = json.loads(before.report)
        self.assertFalse(report["ran"])
        self.assertFalse(report["diff_gate_before"]["ok"])
        self.assertEqual(domain.get_task(self.conn, task.id).status, "needs_human")

        task2, attempt2, allocation2 = self.make_verifying(title="after")
        outside = Path(allocation2.worktree_path) / "docs" / "verifier.txt"
        self.configure(
            argv=[sys.executable, "-c",
                  f"from pathlib import Path; Path({str(outside)!r}).write_text('bad')"],
            key="cfg-after",
        )
        after = self.run_verifier(attempt2, key="run-after")
        self.assertEqual(after.status, "error")
        report = json.loads(after.report)
        self.assertTrue(report["ran"])
        self.assertTrue(report["diff_gate_before"]["ok"])
        self.assertFalse(report["diff_gate_after"]["ok"])
        self.assertTrue(outside.exists(), "verifier evidence must be preserved")
        self.assertEqual(domain.get_task(self.conn, task2.id).status, "needs_human")

    def test_restart_replay_returns_same_result_without_rerun(self):
        _, attempt, allocation = self.make_verifying()
        marker = Path(allocation.worktree_path) / "src" / "counter.txt"
        script = (
            "from pathlib import Path; p=Path(%r); "
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        ) % str(marker)
        self.configure(argv=[sys.executable, "-c", script])
        first = self.run_verifier(attempt, key="restart-key")
        self.assertEqual(first.status, "pass")
        self.assertEqual(marker.read_text(), "1")
        self.conn.close()
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        replay = self.run_verifier(attempt, key="restart-key")
        self.assertEqual(replay.id, first.id)
        self.assertEqual(marker.read_text(), "1")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM worktree_verifications").fetchone()[0], 1)

    def test_interrupted_command_reconciles_expired_then_fresh_run_succeeds(self):
        task, attempt, _ = self.make_verifying()
        self.configure()
        submitted = commands.submit_command(
            self.conn, idempotency_key="interrupted-run",
            kind=verification.WORKTREE_RUN_KIND,
            payload_json=json.dumps({
                "project_id": self.project.id, "task_id": task.id,
                "attempt_id": attempt.id, "profile": "test"}),
            evidence_json=json.dumps({"project_id": self.project.id}),
        ).command
        commands.claim_command(self.conn, submitted.id)
        reconciled = verification.reconcile_worktree_run(
            self.conn, submitted.id, lease=self.lease(), actor="human",
            reason="host restarted before verifier result",
        )
        self.assertEqual(reconciled.state, "expired")
        replay = verification.reconcile_worktree_run(
            self.conn, submitted.id, lease=self.lease(), actor="human",
            reason="idempotent replay")
        self.assertEqual(replay.state, "expired")
        result = self.run_verifier(attempt, key="fresh-after-reconcile")
        self.assertEqual(result.status, "pass")

    def test_leader_expiry_during_execution_records_stale_never_pass(self):
        class MutableClock:
            def __init__(self):
                self.value = datetime.now(timezone.utc)

            def now(self):
                return self.value

            def advance(self, seconds: int):
                self.value += timedelta(seconds=seconds)

        task, attempt, _ = self.make_verifying()
        clock = MutableClock()
        lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "stale-runner",
            ttl_seconds=1, clock=clock)
        verification.configure_verifier_profile(
            self.conn, self.project.id, profile="test",
            argv=[sys.executable, "-c", "print('would pass')"],
            timeout_seconds=10, actor="human", idempotency_key="stale-cfg",
            lease=lease, clock=clock)

        def finish_after_lease(*args, **kwargs):
            clock.advance(2)
            return 0, b"would pass\n", b"", False, False, None

        with mock.patch.object(
            verification, "_run_bounded", side_effect=finish_after_lease,
        ):
            result = verification.run_worktree_verifier(
                self.conn, attempt.id, profile="test",
                idempotency_key="stale-run", lease=lease,
                artifact_root=str(self.artifacts), clock=clock)
        self.assertEqual(result.status, "stale")
        self.assertNotEqual(result.status, "pass")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "needs_human")
        self.assertFalse(
            verification.worktree_verification_is_current(self.conn, result))
        self.assertEqual(self.conn.execute(
            "SELECT state FROM commands WHERE id=?", (result.command_id,)
        ).fetchone()[0], "confirmed")

    def test_suite_records_all_profile_outcomes_before_single_escalation(self):
        task, attempt, _ = self.make_verifying()
        self.configure(profile="lint", key="cfg-lint")
        self.configure(
            profile="test", key="cfg-test",
            argv=[sys.executable, "-c", "raise SystemExit(3)"],
        )
        self.configure(
            profile="typecheck", key="cfg-typecheck", enabled=False)
        self.configure(
            profile="secret_scan", key="cfg-secret",
            argv=["/definitely/missing/m403-secret-scan"],
        )
        result = verification.run_worktree_suite(
            self.conn, attempt.id, idempotency_key="suite-all",
            lease=self.lease(), artifact_root=str(self.artifacts),
        )
        self.assertEqual(result["verdict"], "error")
        self.assertEqual(
            {name: value["status"] for name, value in result["profiles"].items()},
            {"lint": "pass", "test": "fail", "typecheck": "skipped",
             "secret_scan": "error"},
        )
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM worktree_verifications WHERE attempt_id=?",
            (attempt.id,)).fetchone()[0], 4)
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "needs_human")

    def test_parallel_attempts_keep_results_and_artifacts_isolated(self):
        _, attempt1, _ = self.make_verifying(title="parallel-1")
        _, attempt2, _ = self.make_verifying(title="parallel-2")
        self.configure(argv=[sys.executable, "-c", "print('parallel')"])
        lease = self.lease()

        def execute(attempt_id: str, key: str):
            conn = storage.connect(self.db, allow_unverified_location=True)
            try:
                return verification.run_worktree_verifier(
                    conn, attempt_id, profile="test", idempotency_key=key,
                    lease=lease, artifact_root=str(self.artifacts))
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda pair: execute(*pair),
                [(attempt1.id, "parallel-a"), (attempt2.id, "parallel-b")],
            ))
        self.assertEqual(
            [item.status for item in results], ["pass", "pass"],
            [json.loads(item.report).get("problems") for item in results])
        self.assertNotEqual(results[0].id, results[1].id)
        self.assertNotEqual(results[0].log_relpath, results[1].log_relpath)
        self.assertEqual({item.attempt_id for item in results},
                         {attempt1.id, attempt2.id})


if __name__ == "__main__":
    unittest.main()
