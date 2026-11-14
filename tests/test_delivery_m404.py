"""M4-04 Commit/Diff evidence + local-only acceptance acceptance tests.

Covers the bounded M4-04 question end to end against REAL Git worktrees in
temporary directories: fenced idempotent Commit creation (durable Command
BEFORE any Git effect), the COMPLETE base-to-Commit diff proof (digest +
artifact), immutable ship Verification evidence pinning the passing M4-03
verifier profile/config, the append-only Approval that accepts EXACTLY the
bound Commit/branch and only then moves the Task to DONE — plus every
fail-closed rejection, the lost-receipt reconcile, drift invalidation and
concurrency races.

Ownership: this file belongs to M4-04 Core.  It reuses the M4-03 harness
shape (repo + DB + lease + profile + verifier) but never edits other tests.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
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
    delivery,
    domain,
    leader,
    storage,
    verification,
    worker_reports,
    workspaces,
    worktrees,
)
from teamcraft.errors import ConflictError, IllegalTransitionError, ValidationError


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
    # REPO-LOCAL identity (unlike the GIT -c constants, delivery commits run
    # with the ambient environment, so the identity must be persisted here).
    git("config", "user.name", "T", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    (repo / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo


class RecordingRunner:
    """GitRunner wrapper recording every argv for the forbidden-verb audit."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd) -> bytes:
        self.calls.append(list(args))
        return workspaces._default_run_git(args, str(cwd))

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls if call]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class LocalDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m404-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo = init_repo(self.base)
        self.db = self.base / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "demo", str(self.repo))
        self.rec = RecordingRunner()

    # -- helpers -------------------------------------------------------------

    def lease(self, holder: str = "m404-test") -> leader.Lease:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), holder,
            ttl_seconds=3700,
        )

    def make_ship(self, *, title: str = "deliver", kind: str = "ship",
                 policy: dict | None = None, feature: bool = True):
        """Task->READY->policy->attempt->worktree(+change)->brief->report->DONE."""
        task = domain.create_task(
            self.conn, self.project.id, title, kind=kind,
            ownership_json=json.dumps(["src/**"]),
        )
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version,
        )
        if policy is not None:
            task = domain.set_execution_policy(
                self.conn, task.id, expected_version=task.version,
                policy=policy, actor="ceo", reason="m404 test policy",
            )
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version,
        )
        domain.register_session(
            self.conn, attempt.id, "opencode",
            native_session_id=f"native-{attempt.id}", pane_id="pane-m404",
            state="working",
        )
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        allocation, _ = worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.repo),
        )
        if feature:
            (Path(allocation.worktree_path) / "src" / "feature.py").write_text(
                "feature\n", encoding="utf-8")
        brief = briefs.create_attempt_brief(
            self.conn, attempt.id, content="M4-04 delivery brief",
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
        return (domain.get_task(self.conn, task.id),
                domain.get_attempt(self.conn, attempt.id), allocation)

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

    def run_verifier(self, attempt, *, profile: str = "test",
                     key: str | None = None):
        # artifact_root omitted on purpose: verifier artifacts then land under
        # the default configured root, which is what ship evidence re-reads.
        return verification.run_worktree_verifier(
            self.conn, attempt.id, profile=profile,
            idempotency_key=key or f"run-{profile}-{attempt.id}",
            lease=self.lease(),
        )

    def verified(self, *, title: str = "deliver", feature: bool = True,
                 policy: dict | None = None):
        task, attempt, allocation = self.make_ship(
            title=title, feature=feature,
            policy=policy or {"delivery": "local_only"})
        self.configure()
        result = self.run_verifier(attempt)
        assert result.status == "pass", result.status
        return task, attempt, allocation

    def committed(self, *, title: str = "deliver", key: str = "commit-1"):
        task, attempt, allocation = self.verified(title=title)
        evidence = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key=key, lease=self.lease(),
            run_git=self.rec,
        )
        return task, attempt, allocation, evidence

    def shipped(self, *, title: str = "deliver", commit_key: str = "commit-1",
                ship_key: str = "ship-1"):
        task, attempt, allocation, commit = self.committed(
            title=title, key=commit_key)
        ship = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key=ship_key,
            lease=self.lease(), run_git=self.rec,
        )
        return task, attempt, allocation, commit, ship

    def commits_above(self, allocation, ref: str | None = None) -> int:
        return int(git("rev-list", "--count",
                       f"{allocation.base_sha}..{ref or 'HEAD'}",
                       cwd=Path(allocation.worktree_path)))

    def worktree_clean(self, allocation) -> bool:
        return git("status", "--porcelain",
                  cwd=Path(allocation.worktree_path)) == ""

    def command_rows(self, kind: str, attempt_id: str) -> list:
        return self.conn.execute(
            "SELECT id, state FROM commands WHERE kind = ?"
            " AND json_extract(payload_json, '$.attempt_id') = ?",
            (kind, attempt_id)).fetchall()

    def has_event(self, kind: str, task_id: str, **json_equals) -> bool:
        sql = "SELECT 1 FROM events WHERE kind = ? AND task_id = ?"
        args: list = [kind, task_id]
        for key, value in json_equals.items():
            sql += f" AND json_extract(payload_json, '$.{key}') = ?"
            args.append(value)
        return self.conn.execute(sql, args).fetchone() is not None

    # -- success path ----------------------------------------------------------

    def test_full_local_delivery_commit_ship_approve_done(self):
        task, attempt, allocation, commit = self.committed()

        # Commit landed exactly once, on its own branch, on top of the base.
        worktree = Path(allocation.worktree_path)
        self.assertEqual(commit.commit_sha, git("rev-parse", "HEAD", cwd=worktree))
        self.assertEqual(commit.parent_sha, allocation.base_sha)
        self.assertEqual(commit.base_sha, allocation.base_sha)
        self.assertEqual(commit.branch, allocation.branch)
        self.assertEqual(commit.branch, f"tc/{task.id}/{attempt.id}")
        self.assertEqual(commit.source, "created")
        self.assertEqual(commit.file_count, 1)
        self.assertEqual(self.commits_above(allocation), 1)
        self.assertTrue(self.worktree_clean(allocation))
        # SHA mirrored onto the Attempt row (resource fact).
        self.assertEqual(
            self.conn.execute("SELECT commit_sha FROM attempts WHERE id=?",
                             (attempt.id,)).fetchone()[0], commit.commit_sha)
        # Durable Command settled confirmed and references the evidence.
        command = self.conn.execute(
            "SELECT state, result_json FROM commands WHERE id=?",
            (commit.command_id,)).fetchone()
        self.assertEqual(command[0], "confirmed")
        self.assertEqual(json.loads(str(command[1]))["commit_evidence_id"],
                         commit.event_id)
        self.assertTrue(self.has_event("delivery.commit_recorded", task.id,
                                      commit_sha=commit.commit_sha))

        # Ship evidence: complete chain pinned, artifact written, still VERIFYING.
        ship = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="ship-1",
            lease=self.lease(), run_git=self.rec)
        self.assertEqual(ship.commit_sha, commit.commit_sha)
        self.assertEqual(ship.diff_digest, commit.diff_digest)
        self.assertEqual(ship.delivery_mode, "local_only")
        self.assertEqual(ship.proposed_action, "accept_local_commit")
        self.assertEqual(ship.commit_evidence_id, commit.event_id)
        self.assertEqual([entry["profile"] for entry in ship.verifiers], ["test"])
        self.assertEqual(ship.verifiers[0]["status"], "pass")
        artifact = self.repo / ".teamcraft" / "deliveries" / ship.diff_relpath
        self.assertTrue(artifact.is_file())
        self.assertEqual(artifact.stat().st_size, ship.diff_bytes)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")
        self.assertTrue(self.has_event("delivery.ship_evidence_recorded", task.id,
                                      commit_sha=commit.commit_sha))

        # Everything is still current inside the acceptance window.
        fresh_commit = delivery.commit_evidence_is_current(
            self.conn, commit, run_git=self.rec)
        self.assertTrue(fresh_commit.ok, fresh_commit.problems)
        fresh_ship = delivery.ship_evidence_is_current(
            self.conn, ship, run_git=self.rec)
        self.assertTrue(fresh_ship.ok, fresh_ship.problems)

        # Approval must echo EXACTLY the bound commit/branch.
        with self.assertRaises(ConflictError):
            delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept", commit_sha="f" * 40,
                branch=ship.branch, actor="ceo", reason="wrong sha",
                idempotency_key="approve-wrong-sha", lease=self.lease(),
                run_git=self.rec)
        with self.assertRaises(ConflictError):
            delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept", commit_sha=ship.commit_sha,
                branch="main", actor="ceo", reason="wrong branch",
                idempotency_key="approve-wrong-branch", lease=self.lease(),
                run_git=self.rec)
        with self.assertRaises(ValidationError):
            delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept", commit_sha=None,
                branch=None, actor="ceo", reason="no echo",
                idempotency_key="approve-no-echo", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")
        self.assertIsNone(delivery.load_delivery_approval(self.conn, ship.id))

        approval = delivery.approve_local_delivery(
            self.conn, ship.id, decision="accept", commit_sha=commit.commit_sha,
            branch=ship.branch, actor="ceo", reason="reviewed diff; accept local",
            idempotency_key="approve-1", lease=self.lease(), run_git=self.rec)
        self.assertEqual(approval.decision, "accept")
        self.assertEqual(approval.task_status_after, "done")
        self.assertEqual(approval.evidence_digest, ship.evidence_digest)
        done = domain.get_task(self.conn, task.id)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.version, task.version + 1)
        self.assertTrue(self.has_event("delivery.approval_accepted", task.id,
                                      decision="accept"))
        self.assertEqual(delivery.load_delivery_approval(self.conn, ship.id).id,
                         approval.id)

        # The diff artifact re-verifies byte-for-byte.
        read = delivery.read_ship_diff(self.conn, ship.id)
        self.assertEqual(read["diff_digest"], commit.diff_digest)
        self.assertIn("+feature", read["diff"])

        # The accepted decision still stands against live facts.
        fresh = delivery.delivery_approval_is_current(
            self.conn, approval, run_git=self.rec)
        self.assertTrue(fresh.ok, fresh.problems)

        # Forbidden verbs were NEVER issued through the audited runner.
        forbidden = [call for call in self.rec.calls
                    if call and call[0] in delivery.FORBIDDEN_GIT_VERBS]
        self.assertEqual(forbidden, [])
        self.assertIn("commit", self.rec.verbs())

    # -- commit-stage refusals ---------------------------------------------------

    def test_unowned_changes_refuse_before_any_command_or_git_effect(self):
        task, attempt, allocation = self.verified()
        (Path(allocation.worktree_path) / "docs" / "stray.md").write_text(
            "stray\n", encoding="utf-8")
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.create_local_commit(
                self.conn, attempt.id, idempotency_key="c1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "unowned")
        self.assertEqual(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                          attempt.id), [])
        self.assertEqual(self.commits_above(allocation), 0)
        self.assertIsNone(delivery.load_commit_evidence(self.conn, attempt.id))
        # nothing staged or committed: the scene is preserved
        self.assertIn("docs/stray.md", git("status", "--porcelain",
                                          cwd=Path(allocation.worktree_path)))

    def test_empty_workspace_refuses(self):
        task, attempt, allocation = self.verified(feature=False)
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.create_local_commit(
                self.conn, attempt.id, idempotency_key="c1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "empty")
        self.assertEqual(self.commits_above(allocation), 0)

    def test_delivery_mode_refusals(self):
        # report_only: nothing to commit, nothing to ship.
        task, attempt, allocation = self.make_ship(
            title="report-only", policy={"delivery": "report_only"})
        self.configure()
        self.assertEqual(self.run_verifier(attempt).status, "pass")
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.create_local_commit(
                self.conn, attempt.id, idempotency_key="c1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "mode")
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.record_ship_evidence(
                self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "mode")
        # direct_pr waits for M4-05: ship evidence is local_only-only.
        task2, attempt2, _ = self.make_ship(
            title="direct-pr", policy={"delivery": "direct_pr"})
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.record_ship_evidence(
                self.conn, attempt2.id, idempotency_key="s2", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "mode")
        # scout kind never delivers via the local-commit path.
        task3, attempt3, _ = self.make_ship(
            title="scout", kind="scout", policy={"delivery": "local_only"})
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.create_local_commit(
                self.conn, attempt3.id, idempotency_key="c3", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "mode")

    def test_missing_git_identity_refuses_without_fabricating(self):
        task, attempt, allocation = self.make_ship(title="no-identity")
        git("config", "--unset-all", "user.name", cwd=self.repo)
        git("config", "--unset-all", "user.email", cwd=self.repo)
        with mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }):
            with self.assertRaises(delivery.DeliveryRefusal) as ctx:
                delivery.create_local_commit(
                    self.conn, attempt.id, idempotency_key="c1",
                    lease=self.lease(), run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "identity")
        self.assertEqual(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                          attempt.id), [])
        self.assertNotIn("add", self.rec.verbs())
        self.assertEqual(self.commits_above(allocation), 0)

    def test_protected_branches_are_never_targets(self):
        for branch in ("main", "master", "MAIN", "trunk", "feature/x", ""):
            with self.subTest(branch=branch):
                with self.assertRaises(delivery.DeliveryRefusal) as ctx:
                    delivery._refuse_protected_branch(branch)
                self.assertEqual(ctx.exception.kind, "main-branch")
        # Only the derived isolated Attempt branch naming is accepted.
        delivery._refuse_protected_branch("tc/task-1/att-1")

    # -- verifier refusals ---------------------------------------------------------

    def test_missing_verifier_profile_can_never_ship(self):
        task, attempt, allocation = self.make_ship(
            title="no-verifier", policy={"delivery": "local_only"})
        evidence = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="c1", lease=self.lease(),
            run_git=self.rec)
        self.assertEqual(evidence.source, "created")
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.record_ship_evidence(
                self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "verifier")

    def test_required_profile_without_pass_or_with_stale_config_refuses(self):
        task, attempt, allocation = self.verified(title="stale-config")
        delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="c1", lease=self.lease(),
            run_git=self.rec)
        # profile config v2 lands after the PASS -> pinned evidence is stale
        self.configure(argv=[sys.executable, "-c", "print('ok v2')"],
                       key="cfg-test-v2")
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.record_ship_evidence(
                self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "verifier")

        # two required profiles, only one passed
        task2, attempt2, _ = self.make_ship(
            title="lint-missing", policy={"delivery": "local_only"})
        self.configure(profile="lint", key="cfg-lint")
        self.assertEqual(self.run_verifier(attempt2, profile="lint",
                                          key="run-lint").status, "pass")
        delivery.create_local_commit(
            self.conn, attempt2.id, idempotency_key="c2", lease=self.lease(),
            run_git=self.rec)
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.record_ship_evidence(
                self.conn, attempt2.id, idempotency_key="s2", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "verifier")

    def test_failed_verifier_never_reaches_delivery(self):
        task, attempt, _ = self.make_ship(title="failed-verifier")
        self.configure(argv=[sys.executable, "-c", "raise SystemExit(3)"])
        result = self.run_verifier(attempt)
        self.assertEqual(result.status, "fail")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "needs_human")
        with self.assertRaises(ValidationError):
            delivery.create_local_commit(
                self.conn, attempt.id, idempotency_key="c1", lease=self.lease(),
                run_git=self.rec)

    # -- ship-stage drift refusals --------------------------------------------------

    def test_amended_commit_refuses_ship_and_approval(self):
        task, attempt, allocation, commit = self.committed(title="amend-ship")
        worktree = Path(allocation.worktree_path)
        (worktree / "src" / "feature.py").write_text("feature v2\n",
                                                    encoding="utf-8")
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "--amend", "--no-edit", cwd=worktree)
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.record_ship_evidence(
                self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "drift")
        self.assertIsNone(delivery.load_ship_evidence(self.conn, attempt.id))
        self.assertFalse(delivery.commit_evidence_is_current(
            self.conn, commit, run_git=self.rec).ok)

    def test_dirty_worktree_refuses_ship(self):
        task, attempt, allocation, commit = self.committed(title="dirty-ship")
        (Path(allocation.worktree_path) / "src" / "extra.py").write_text(
            "extra\n", encoding="utf-8")
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.record_ship_evidence(
                self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "drift")
        self.assertIsNone(delivery.load_ship_evidence(self.conn, attempt.id))
        fresh = delivery.commit_evidence_is_current(
            self.conn, commit, run_git=self.rec)
        self.assertFalse(fresh.ok)
        self.assertTrue(any(p.startswith("dirty") for p in fresh.problems),
                        fresh.problems)

    # -- approval refusals -----------------------------------------------------------

    def test_reject_records_decision_leaves_verifying_and_closes_evidence(self):
        task, attempt, allocation, commit, ship = self.shipped(title="reject")
        approval = delivery.approve_local_delivery(
            self.conn, ship.id, decision="reject", commit_sha=commit.commit_sha,
            branch=ship.branch, actor="ceo", reason="diff not acceptable",
            idempotency_key="reject-1", lease=self.lease(), run_git=self.rec)
        self.assertEqual(approval.decision, "reject")
        self.assertEqual(approval.task_status_after, "verifying")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")
        self.assertTrue(self.has_event("delivery.approval_rejected", task.id,
                                      decision="reject"))
        fresh = delivery.delivery_approval_is_current(
            self.conn, approval, run_git=self.rec)
        self.assertTrue(fresh.ok, fresh.problems)
        # one decision per evidence: a later accept on the SAME evidence dies
        with self.assertRaises(ConflictError):
            delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept",
                commit_sha=commit.commit_sha, branch=ship.branch,
                actor="ceo", reason="changed my mind",
                idempotency_key="late-accept", lease=self.lease(),
                run_git=self.rec)
        # an identical reject converges onto the same durable decision
        replay = delivery.approve_local_delivery(
            self.conn, ship.id, decision="reject", commit_sha=commit.commit_sha,
            branch=ship.branch, actor="ceo", reason="diff not acceptable",
            idempotency_key="reject-replay-newkey", lease=self.lease(),
            run_git=self.rec)
        self.assertEqual(replay.id, approval.id)
        # ship evidence replay (same and different key) returns the settled one
        again = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="ship-1", lease=self.lease(),
            run_git=self.rec)
        self.assertEqual(again.id, ship.id)

    def test_stale_policy_or_version_invalidates_the_approval_window(self):
        task, attempt, allocation, commit, ship = self.shipped(title="drift-ver")
        # Task version bump (title edit) invalidates the pinned evidence
        domain.update_task(self.conn, task.id, title="edited",
                          expected_version=task.version)
        with self.assertRaises(ConflictError):
            delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept",
                commit_sha=commit.commit_sha, branch=ship.branch,
                actor="ceo", reason="stale version",
                idempotency_key="approve-stale", lease=self.lease(),
                run_git=self.rec)
        self.assertIsNone(delivery.load_delivery_approval(self.conn, ship.id))
        self.assertFalse(delivery.ship_evidence_is_current(
            self.conn, ship, run_git=self.rec).ok)

    def test_policy_delivery_drift_invalidates_the_approval_window(self):
        task, attempt, allocation, commit, ship = self.shipped(title="drift-pol")
        # Same version pin is broken by a policy edit (which also bumps the
        # version); refuse both ways.
        domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"delivery": "report_only"}, actor="ceo",
            reason="policy flipped",
        )
        with self.assertRaises(ConflictError):
            delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept",
                commit_sha=commit.commit_sha, branch=ship.branch,
                actor="ceo", reason="stale policy",
                idempotency_key="approve-stale-pol", lease=self.lease(),
                run_git=self.rec)

    def test_stale_lease_cannot_approve(self):
        task, attempt, allocation, commit, ship = self.shipped(title="stale-lease")
        clock = MutableClock()
        stale = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "m404-test",
            ttl_seconds=60, clock=clock)
        clock.advance(120)
        with self.assertRaises(leader.LeaderError):
            delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept",
                commit_sha=commit.commit_sha, branch=ship.branch,
                actor="ceo", reason="stale leader",
                idempotency_key="approve-staleleader", lease=stale,
                clock=clock, run_git=self.rec)
        self.assertIsNone(delivery.load_delivery_approval(self.conn, ship.id))

    # -- post-DONE invalidation -------------------------------------------------------

    def test_task_mutation_after_done_reopens_and_invalidates(self):
        task, attempt, allocation, commit = self.committed(title="done-edit")
        ship = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
            run_git=self.rec)
        approval = delivery.approve_local_delivery(
            self.conn, ship.id, decision="accept", commit_sha=commit.commit_sha,
            branch=ship.branch, actor="ceo", reason="ok",
            idempotency_key="ap1", lease=self.lease(), run_git=self.rec)
        done = domain.get_task(self.conn, task.id)
        self.assertEqual(done.status, "done")
        reopened = domain.update_task(self.conn, task.id, priority=done.priority,
                                     title="post-done edit",
                                     expected_version=done.version)
        self.assertEqual(reopened.status, "needs_human")
        self.assertTrue(self.has_event("delivery.approval_invalidated", task.id,
                                      reason="task_evidence_changed"))
        fresh = delivery.delivery_approval_is_current(
            self.conn, approval, run_git=self.rec)
        self.assertFalse(fresh.ok)

    def test_verifier_reconfiguration_after_done_reopens_ship(self):
        task, attempt, allocation, commit = self.committed(title="done-cfg")
        ship = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
            run_git=self.rec)
        approval = delivery.approve_local_delivery(
            self.conn, ship.id, decision="accept", commit_sha=commit.commit_sha,
            branch=ship.branch, actor="ceo", reason="ok",
            idempotency_key="ap1", lease=self.lease(), run_git=self.rec)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "done")
        self.configure(argv=[sys.executable, "-c", "print('stricter v2')"],
                       key="cfg-v2-after-done")
        reopened = domain.get_task(self.conn, task.id)
        self.assertEqual(reopened.status, "needs_human")
        self.assertTrue(self.has_event("delivery.approval_invalidated", task.id,
                                      reason="verifier_profile_changed"))
        fresh = delivery.delivery_approval_is_current(
            self.conn, approval, run_git=self.rec)
        self.assertFalse(fresh.ok)

    # -- idempotency / convergence ------------------------------------------------------

    def test_commit_replays_converge_and_second_key_sees_settled_evidence(self):
        task, attempt, allocation, evidence = self.committed(title="replay")
        again = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-1",
            lease=self.lease(), run_git=self.rec)
        self.assertEqual(again.event_id, evidence.event_id)
        self.assertEqual(again.commit_sha, evidence.commit_sha)
        # Different key AFTER settlement: converges, never a second commit.
        second_key = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-2",
            lease=self.lease(), run_git=self.rec)
        self.assertEqual(second_key.event_id, evidence.event_id)
        self.assertEqual(self.commits_above(allocation), 1)
        self.assertEqual(len(delivery.list_commit_evidence(self.conn, task.id)), 1)
        # An unresolved OTHER-key command blocks a fresh attempt at the effect.
        task2, attempt2, allocation2 = self.verified(title="other-key")
        commands.submit_command(
            self.conn, idempotency_key="half-open",
            kind=delivery.COMMIT_COMMAND_KIND,
            payload_json=json.dumps({"attempt_id": attempt2.id,
                                    "project_id": self.project.id}),
            evidence_json=json.dumps({"project_id": self.project.id}),
        )
        with self.assertRaises(ConflictError):
            delivery.create_local_commit(
                self.conn, attempt2.id, idempotency_key="c-b",
                lease=self.lease(), run_git=self.rec)
        self.assertEqual(self.commits_above(allocation2), 0)
        # Once the unresolved command is settled against fact, delivery may
        # proceed under a new key.
        half = self.conn.execute(
            "SELECT id FROM commands WHERE idempotency_key='half-open'").fetchone()[0]
        commands.claim_command(self.conn, str(half))
        expired = delivery.reconcile_commit_receipt(
            self.conn, str(half), lease=self.lease(), actor="ops",
            reason="settling half-open command", run_git=self.rec)
        self.assertEqual(expired["resolution"], "expired")
        progressed = delivery.create_local_commit(
            self.conn, attempt2.id, idempotency_key="c-b",
            lease=self.lease(), run_git=self.rec)
        self.assertEqual(progressed.source, "created")
        self.assertEqual(self.commits_above(allocation2), 1)

    def test_ship_artifact_survives_and_read_ship_diff_verifies(self):
        task, attempt, allocation, commit, ship = self.shipped(title="artifact")
        artifact = self.repo / ".teamcraft" / "deliveries" / ship.diff_relpath
        self.assertTrue(artifact.is_file())
        original = artifact.read_bytes()
        self.assertEqual(len(original), ship.diff_bytes)

        artifact.write_bytes(b"")
        with self.assertRaises(storage.StorageError):
            delivery.read_ship_diff(self.conn, ship.id)
        artifact.write_bytes(original + b"# tampered tail\n")
        with self.assertRaises(storage.StorageError):
            delivery.read_ship_diff(self.conn, ship.id)
        artifact.write_bytes(original)
        read = delivery.read_ship_diff(self.conn, ship.id)
        self.assertEqual(read["diff_digest"], commit.diff_digest)

    # -- lost commit receipt -------------------------------------------------------------

    def _submit_and_claim(self, task, attempt, key: str):
        command = commands.submit_command(
            self.conn, idempotency_key=key,
            kind=delivery.COMMIT_COMMAND_KIND,
            payload_json=json.dumps({
                "project_id": self.project.id, "task_id": task.id,
                "attempt_id": attempt.id}),
            evidence_json=json.dumps({"project_id": self.project.id}),
        ).command
        commands.claim_command(self.conn, command.id)
        return command

    def test_lost_receipt_adopts_existing_commit_never_a_second(self):
        task, attempt, allocation = self.verified(title="lost-adopt")
        command = self._submit_and_claim(task, attempt, "lost-1")
        # The crash window: Git effect happened without our receipt.
        worktree = Path(allocation.worktree_path)
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "manual crash commit", cwd=worktree)

        result = delivery.reconcile_commit_receipt(
            self.conn, command.id, lease=self.lease(), actor="ops",
            reason="host restarted between commit and receipt",
            run_git=self.rec)
        self.assertEqual(result["resolution"], "adopted")
        evidence = result["commit_evidence"]
        self.assertEqual(evidence.source, "reconciled")
        self.assertEqual(evidence.commit_sha, git("rev-parse", "HEAD",
                                                 cwd=worktree))
        self.assertEqual(self.commits_above(allocation), 1)
        self.assertEqual(
            self.conn.execute("SELECT state FROM commands WHERE id=?",
                             (command.id,)).fetchone()[0], "confirmed")
        self.assertTrue(self.has_event("delivery.commit_reconciled", task.id,
                                      resolution="adopted"))
        # Re-reconciling an evidence-owning command fails closed.
        with self.assertRaises(ConflictError):
            delivery.reconcile_commit_receipt(
                self.conn, command.id, lease=self.lease(), actor="ops",
                reason="again", run_git=self.rec)
        # The adopted commit still completes the whole chain.
        ship = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="s1", lease=self.lease(),
            run_git=self.rec)
        delivery.approve_local_delivery(
            self.conn, ship.id, decision="accept",
            commit_sha=evidence.commit_sha, branch=ship.branch,
            actor="ceo", reason="adopted commit reviewed",
            idempotency_key="ap1", lease=self.lease(), run_git=self.rec)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "done")
        self.assertEqual(self.commits_above(allocation), 1)

    def test_lost_receipt_expires_then_fresh_create_succeeds(self):
        task, attempt, allocation = self.verified(title="lost-expire")
        command = self._submit_and_claim(task, attempt, "lost-2")
        result = delivery.reconcile_commit_receipt(
            self.conn, command.id, lease=self.lease(), actor="ops",
            reason="nothing landed", run_git=self.rec)
        self.assertEqual(result["resolution"], "expired")
        self.assertIsNone(result["commit_evidence"])
        self.assertTrue(self.has_event("delivery.commit_reconciled", task.id,
                                      resolution="expired"))
        replay = delivery.reconcile_commit_receipt(
            self.conn, command.id, lease=self.lease(), actor="ops",
            reason="idempotent", run_git=self.rec)
        self.assertEqual(replay["resolution"], "expired")
        fresh = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="fresh-1",
            lease=self.lease(), run_git=self.rec)
        self.assertEqual(fresh.source, "created")
        self.assertEqual(self.commits_above(allocation), 1)

    def test_lost_receipt_requires_bound_native_session_fact(self):
        task, attempt, allocation = self.verified(title="missing-session")
        with storage.transaction(self.conn):
            self.conn.execute("DELETE FROM sessions WHERE attempt_id = ?",
                              (attempt.id,))
        command = self._submit_and_claim(task, attempt, "lost-no-session")
        worktree = Path(allocation.worktree_path)
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "unattributed commit", cwd=worktree)
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.reconcile_commit_receipt(
                self.conn, command.id, lease=self.lease(), actor="ops",
                reason="native session fact missing", run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "ambiguous")
        self.assertIsNone(delivery.load_commit_evidence(self.conn, attempt.id))
        self.assertEqual(
            self.conn.execute("SELECT state FROM commands WHERE id = ?",
                              (command.id,)).fetchone()[0],
            "running")

    def test_reconcile_refuses_ambiguous_and_dirty_scenes(self):
        task, attempt, allocation = self.verified(title="ambiguous")
        command = self._submit_and_claim(task, attempt, "lost-3")
        worktree = Path(allocation.worktree_path)
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "one", cwd=worktree)
        (worktree / "src" / "second.py").write_text("second\n", encoding="utf-8")
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "two", cwd=worktree)
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.reconcile_commit_receipt(
                self.conn, command.id, lease=self.lease(), actor="ops",
                reason="two commits", run_git=self.rec)
        self.assertIn(ctx.exception.kind, ("drift", "ambiguous"))
        self.assertEqual(self.commits_above(allocation), 2)  # nothing destroyed
        self.assertEqual(
            self.conn.execute("SELECT state FROM commands WHERE id=?",
                             (command.id,)).fetchone()[0], "running")

        task2, attempt2, allocation2 = self.verified(title="dirty-reconcile")
        command2 = self._submit_and_claim(task2, attempt2, "lost-4")
        wt2 = Path(allocation2.worktree_path)
        git("add", "-A", cwd=wt2)
        git("commit", "-q", "-m", "landed", cwd=wt2)
        (wt2 / "src" / "residue.py").write_text("residue\n", encoding="utf-8")
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.reconcile_commit_receipt(
                self.conn, command2.id, lease=self.lease(), actor="ops",
                reason="dirty residue", run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "dirty")
        self.assertIsNone(delivery.load_commit_evidence(self.conn, attempt2.id))
        self.assertEqual(self.commits_above(allocation2), 1)  # preserved

    def test_orphan_commit_without_command_refuses_creation(self):
        task, attempt, allocation = self.verified(title="orphan")
        worktree = Path(allocation.worktree_path)
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "orphan by other hand", cwd=worktree)
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.create_local_commit(
                self.conn, attempt.id, idempotency_key="c1", lease=self.lease(),
                run_git=self.rec)
        self.assertEqual(ctx.exception.kind, "drift")
        self.assertEqual(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                          attempt.id), [])

    # -- concurrency -----------------------------------------------------------------------

    def test_concurrent_creates_converge_on_exactly_one_commit(self):
        task, attempt, allocation = self.verified(title="race-commit")
        lease = self.lease()

        def create():
            conn = storage.connect(self.db, allow_unverified_location=True)
            try:
                return delivery.create_local_commit(
                    conn, attempt.id, idempotency_key="race-1", lease=lease)
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            concurrent.futures.wait(futures)
        outcomes = [(f.exception() if f.exception() is not None else f.result())
                    for f in futures]
        successes = [item for item in outcomes
                    if isinstance(item, delivery.CommitEvidence)]
        failures = [item for item in outcomes
                   if isinstance(item, BaseException)]
        # Both schedules are valid.  The second caller may observe the first
        # Command while it is still running and fail closed, or it may enter
        # after the receipt settles and replay that exact receipt.  A replay
        # is an idempotent success, not evidence that Git ran twice.
        self.assertGreaterEqual(len(successes), 1,
                                [str(item) for item in outcomes])
        self.assertEqual(len(successes) + len(failures), 2)
        self.assertTrue(
            all(isinstance(item, IllegalTransitionError) for item in failures),
            [str(item) for item in outcomes],
        )
        self.assertTrue(all(item == successes[0] for item in successes),
                        [str(item) for item in outcomes])
        self.assertEqual(len({item.command_id for item in successes}), 1)
        self.assertEqual(self.commits_above(allocation), 1)
        evidence = delivery.list_commit_evidence(self.conn, task.id)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0], successes[0])
        commands_for_attempt = self.command_rows(
            delivery.COMMIT_COMMAND_KIND, attempt.id)
        self.assertEqual(len(commands_for_attempt), 1)
        self.assertEqual(str(commands_for_attempt[0][0]), successes[0].command_id)
        self.assertEqual(str(commands_for_attempt[0][1]), "confirmed")
        recorded = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? AND task_id = ?",
            ("delivery.commit_recorded", task.id),
        ).fetchone()[0]
        self.assertEqual(recorded, 1)

    def test_concurrent_approves_produce_exactly_one_done_decision(self):
        task, attempt, allocation, commit, ship = self.shipped(title="race-appr")
        lease = self.lease()

        def approve(key: str):
            conn = storage.connect(self.db, allow_unverified_location=True)
            try:
                return delivery.approve_local_delivery(
                    conn, ship.id, decision="accept",
                    commit_sha=commit.commit_sha, branch=ship.branch,
                    actor="ceo", reason="race accept",
                    idempotency_key=key, lease=lease)
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve, k) for k in ("race-a", "race-b")]
            concurrent.futures.wait(futures)
        outcomes = [(f.exception() if f.exception() is not None else f.result())
                    for f in futures]
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? AND task_id = ?",
            (delivery.APPROVAL_EVENT_KIND, task.id)).fetchone()[0]
        self.assertEqual(rows, 1, [str(o) for o in outcomes])
        self.assertEqual(domain.get_task(self.conn, task.id).status, "done")
        approvals = [item for item in outcomes
                     if isinstance(item, delivery.DeliveryApproval)]
        rejections = [item for item in outcomes if isinstance(item, BaseException)]
        self.assertGreaterEqual(len(approvals), 1)
        self.assertEqual(len(approvals) + len(rejections), 2)
        if len(approvals) == 2:
            self.assertEqual(approvals[0].id, approvals[1].id)


if __name__ == "__main__":
    unittest.main()
