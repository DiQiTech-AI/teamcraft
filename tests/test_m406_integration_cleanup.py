"""M4-06 integration conflict detection + evidence-gated safe cleanup tests.

Every Git effect happens inside TEMPORARY repositories only — the real project
repository and the real ``.teamcraft`` runtime are never touched.

Acceptance matrix covered here:

* base drift is DETECTED (live base moved vs the recorded allocation base)
  and forms ONE explicit integration Task; repeated ensures converge onto it
  (deterministic Task id, single durable ``integration_required`` fact);
* a conflicting branch is REPORTED as ``conflict`` — nothing is ever merged,
  overwritten, or reset (branch tips, base HEAD, and file contents are
  asserted unchanged after detection and task creation);
* cleanup refuses without any of: CLEANABLE lifecycle, terminal Attempt,
  DONE delivery Task, current accepted Approval/complete evidence, exact
  identity, clean workspace, or with undelivered commits on the branch — and
  every refusal preserves the scene AND lands a durable audit fact;
* cleanup proceeds exactly once for a CLEANABLE, delivered, evidence-complete
  workspace: ``git worktree remove`` without force, path gone, branch ref
  kept, one deterministic CLEANED event; replays return the same fact;
* crash-after-effect recovery: a removal whose CLEANED fact never landed is
  adopted by reconcile (``disposition="adopted"``) and re-reconciles
  idempotently.
"""

from __future__ import annotations

import json
import os
import shutil
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
    runtime_adapter,
    storage,
    verification,
    worker_reports,
    workspaces,
    worktrees,
)
from teamcraft.errors import (
    IllegalTransitionError,
    NotFoundError,
    StorageError,
)

GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false",
    "-c", "user.name=T", "-c", "user.email=t@example.com",
    "-c", "init.defaultBranch=main",
]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        [*GIT, *args], cwd=str(cwd), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def init_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    # REPO-LOCAL identity: delivery commits run with the ambient env.
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
    """GitRunner wrapper recording every argv for the verb audit."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd) -> bytes:
        self.calls.append(list(args))
        return workspaces._default_run_git(args, str(cwd))

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls if call]


class M406Fixture(unittest.TestCase):
    """Temp repo + runtime DB + the full local-only delivery pipeline."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m406-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo = init_repo(self.base)
        self.db = self.base / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "demo", str(self.repo))
        self.rec = RecordingRunner()

    # -- fencing + pipeline helpers (M4-04 harness shape) --------------------

    def lease(self, holder: str = "m406-test") -> leader.Lease:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), holder,
            ttl_seconds=3700,
        )

    def make_ship(self, *, claims: list[str] | None = None,
                 feature: bool = True):
        task = domain.create_task(
            self.conn, self.project.id, "deliver", kind="ship",
            ownership_json=json.dumps(claims or ["src/**"]))
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version)
        task = domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"delivery": "local_only"}, actor="ceo",
            reason="m406 test policy")
        attempt = domain.create_attempt(self.conn, task.id,
                                       expected_version=task.version)
        domain.register_session(self.conn, attempt.id, "opencode",
                               native_session_id=f"native-{attempt.id}",
                               pane_id="pane-m406", state="working")
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        allocation, _ = worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.repo))
        if feature:
            (Path(allocation.worktree_path) / "src" / "feature.py").write_text(
                "feature\n", encoding="utf-8")
        brief = briefs.create_attempt_brief(self.conn, attempt.id,
                                           content="M4-06 brief")
        worker_reports.record_worker_report(self.conn, {
            "schema_version": 1, "task_id": task.id,
            "attempt_id": attempt.id, "task_version": brief.task_version,
            "brief_digest": brief.brief_digest, "report_kind": "final",
            "outcome": "completed", "milestone_id": "final",
            "summary": "done", "artifact_digest": None,
            "usage": {"total_tokens": None},
        })
        domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version)
        return (domain.get_task(self.conn, task.id),
                domain.get_attempt(self.conn, attempt.id), allocation)

    def configure_and_verify(self, attempt) -> None:
        verification.configure_verifier_profile(
            self.conn, self.project.id, profile="test",
            argv=[sys.executable, "-c", "print('ok')"], timeout_seconds=10,
            actor="human", idempotency_key="cfg-test", lease=self.lease())
        result = verification.run_worktree_verifier(
            self.conn, attempt.id, profile="test",
            idempotency_key=f"run-test-{attempt.id}", lease=self.lease())
        assert result.status == "pass", result.status

    def delivered(self, *, claims: list[str] | None = None, feature: bool = True):
        """Everything through the ONLY path to DONE: commit→ship→accept."""
        task, attempt, allocation = self.make_ship(claims=claims,
                                                  feature=feature)
        self.configure_and_verify(attempt)
        commit = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-1",
            lease=self.lease(), run_git=self.rec)
        ship = delivery.record_ship_evidence(
            self.conn, attempt.id, idempotency_key="ship-1",
            lease=self.lease(), run_git=self.rec)
        delivery.approve_local_delivery(
            self.conn, ship.id, decision="accept",
            commit_sha=commit.commit_sha, branch=commit.branch,
            actor="human", reason="ship it", idempotency_key="approve-1",
            lease=self.lease(), run_git=self.rec)
        task = domain.get_task(self.conn, task.id)
        assert task.status == "done", task.status
        return task, attempt, allocation, commit, ship

    def made_cleanable(self):
        task, attempt, allocation, commit, ship = self.delivered()
        worktrees.mark_worktree_cleanable(self.conn, attempt.id,
                                         run_git=self.rec)
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "cleanable")
        return task, attempt, allocation, commit, ship

    # -- assertion helpers ----------------------------------------------------

    def event_rows(self, kind: str, attempt_id: str | None = None,
                   task_id: str | None = None) -> list:
        sql = "SELECT seq, event_id, payload_json FROM events WHERE kind = ?"
        args: list = [kind]
        if attempt_id is not None:
            sql += " AND attempt_id = ?"
            args.append(attempt_id)
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        return self.conn.execute(sql + " ORDER BY seq", args).fetchall()

    def worktree_registered(self, path: str) -> bool:
        listed = git("worktree", "list", "--porcelain", cwd=self.repo)
        return os.path.realpath(path) in {
            os.path.realpath(line[len("worktree "):].strip())
            for line in listed.splitlines()
            if line.startswith("worktree ")}

    def bind_member(self, task, attempt, *, name: str) -> None:
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO members (name,role,responsibility,kind,task,"
                "ownership_json,read_only,cwd,agent_args_json,start_timeout_ms,"
                "phase,prompt_may_have_been_delivered,task_id,attempt_id,"
                "created_at,updated_at) VALUES "
                "(?,'developer','m406','opencode','m406',?,0,?,'[]',30000,"
                "'agent_started',0,?,?,0,0)",
                (name, task.ownership_json, str(self.repo), task.id, attempt.id),
            )

    def marker_value(self, path: str, key: str) -> str | None:
        proc = subprocess.run(
            [*GIT, "config", "--worktree", "--get", key], cwd=path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode == 1:
            return None
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return proc.stdout.decode("utf-8", "surrogateescape").strip()


# ---------------------------------------------------------------------------
# Acceptance 1 — base drift / conflict detection; explicit integration Task
# ---------------------------------------------------------------------------


class IntegrationDetectionTests(M406Fixture):
    def test_status_without_prior_check_is_non_mutating(self):
        task, attempt, _allocation = self.make_ship()
        self.bind_member(task, attempt, name="m406-status")
        self.assertEqual(
            self.event_rows(delivery.INTEGRATION_CHECK_EVENT_KIND,
                            attempt_id=attempt.id), [])

        status = runtime_adapter.member_integration_status(
            self.conn, "m406-status", run_git=self.rec)

        self.assertIsNotNone(status["integration"])
        self.assertEqual(
            self.event_rows(delivery.INTEGRATION_CHECK_EVENT_KIND,
                            attempt_id=attempt.id), [])

    def test_no_drift_requires_no_integration_task(self):
        _task, attempt, allocation = self.make_ship()
        status = delivery.check_integration_state(self.conn, attempt.id,
                                                 run_git=self.rec)
        self.assertFalse(status.base_drift)
        self.assertFalse(status.integration_required)
        # Nothing committed on the branch yet: branch tip == base.
        self.assertEqual(status.merge_status, worktrees.MERGE_STATUS_EMPTY)
        self.assertIsNone(status.integration_task_id)
        ensured_status, created_task, created = delivery.ensure_integration_task(
            self.conn, attempt.id, run_git=self.rec)
        self.assertIsNone(created_task)
        self.assertFalse(created)
        self.assertFalse(ensured_status.integration_required)
        # Detection itself left ONE auditable, digest-keyed fact (the ensure
        # re-check sees identical facts and converges onto the same row).
        rows = self.event_rows(delivery.INTEGRATION_CHECK_EVENT_KIND,
                               attempt_id=attempt.id)
        self.assertEqual(len(rows), 1)
        payload = json.loads(str(rows[0][2]))
        self.assertEqual(payload["recorded_base_sha"], allocation.base_sha)
        self.assertEqual(payload["live_base_sha"], allocation.base_sha)
        # ...and no requirement was ever recorded.
        self.assertIsNone(
            delivery.load_integration_requirement(self.conn, attempt.id))

    def test_base_drift_creates_one_idempotent_integration_task(self):
        task, attempt, _allocation = self.make_ship()
        # The world moves under the Attempt: base advances in the main repo.
        (self.repo / "later.py").write_text("later\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "base advanced", cwd=self.repo)
        live_base = git("rev-parse", "HEAD", cwd=self.repo)

        status = delivery.check_integration_state(self.conn, attempt.id,
                                                 run_git=self.rec)
        self.assertTrue(status.base_drift)
        self.assertEqual(status.live_base_sha, live_base)
        self.assertTrue(status.integration_required)

        _s1, integration_task, created = delivery.ensure_integration_task(
            self.conn, attempt.id, run_git=self.rec)
        self.assertTrue(created)
        assert integration_task is not None
        self.assertTrue(integration_task.id.startswith("itg-"))
        self.assertEqual(integration_task.kind, "ship")
        self.assertEqual(integration_task.status, "pending")
        self.assertEqual(json.loads(integration_task.ownership_json),
                         ["src/**"])
        self.assertIn("drift", integration_task.title.lower())

        # Idempotent: a second ensure re-converges onto the SAME task.
        _s2, again, created2 = delivery.ensure_integration_task(
            self.conn, attempt.id, run_git=self.rec)
        self.assertFalse(created2)
        assert again is not None
        self.assertEqual(again.id, integration_task.id)

        # Exactly ONE durable requirement fact, binding the same Task.
        rows = self.event_rows(delivery.INTEGRATION_REQUIRED_EVENT_KIND,
                               attempt_id=attempt.id)
        self.assertEqual(len(rows), 1)
        payload = json.loads(str(rows[0][2]))
        self.assertEqual(payload["integration_task_id"], integration_task.id)
        self.assertEqual(payload["source_task_id"], task.id)
        self.assertEqual(payload["trigger"], "base_drift")
        requirement = delivery.load_integration_requirement(self.conn,
                                                           attempt.id)
        assert requirement is not None
        self.assertEqual(requirement[0], integration_task.id)

        # The audit trail shows the drift itself.
        checks = self.event_rows(delivery.INTEGRATION_CHECK_EVENT_KIND,
                                attempt_id=attempt.id)
        first = json.loads(str(checks[0][2]))
        self.assertTrue(first["integration_required"])
        self.assertNotEqual(first["live_base_sha"], first["recorded_base_sha"])

    def test_merge_conflict_is_reported_and_never_merged(self):
        _task, attempt, allocation = self.make_ship(claims=["docs/**"],
                                                  feature=False)
        worktree = Path(allocation.worktree_path)
        (worktree / "docs" / "guide.md").write_text("branch version\n",
                                                   encoding="utf-8")
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "branch edit", cwd=worktree)
        branch_tip = git("rev-parse", "HEAD", cwd=worktree)
        # The same line moves differently on the base.
        (self.repo / "docs" / "guide.md").write_text("base version\n",
                                                    encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "base edit", cwd=self.repo)
        base_tip = git("rev-parse", "HEAD", cwd=self.repo)

        status = delivery.check_integration_state(self.conn, attempt.id,
                                                 run_git=self.rec)
        self.assertTrue(status.base_drift)
        self.assertEqual(status.merge_status, worktrees.MERGE_STATUS_CONFLICT)
        self.assertTrue(status.integration_required)
        self.assertIn("docs/guide.md", status.conflict_files)

        _s, integration_task, created = delivery.ensure_integration_task(
            self.conn, attempt.id, run_git=self.rec)
        self.assertTrue(created)
        assert integration_task is not None

        # NOTHING was merged or overwritten by detection or task creation:
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), branch_tip)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.repo), base_tip)
        self.assertEqual((worktree / "docs" / "guide.md").read_text(),
                         "branch version\n")
        self.assertEqual((self.repo / "docs" / "guide.md").read_text(),
                         "base version\n")
        self.assertEqual(
            git("rev-list", "--count",
                f"{allocation.base_sha}..{branch_tip}", cwd=self.repo),
            "1")
        # Only ever in-memory probes + no forbidden verbs:
        forbidden = {"merge", "rebase", "reset", "clean", "checkout", "switch",
                    "push", "pull", "fetch", "restore"}
        self.assertFalse(forbidden.intersection(set(self.rec.verbs())))

    def test_clean_divergence_and_ancestor_statuses(self):
        _task, attempt, _allocation = self.make_ship()
        # Branch advanced (feature.py committed in the worktree via commit):
        commit = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-1",
            lease=self.lease(), run_git=self.rec)
        status = delivery.check_integration_state(self.conn, attempt.id,
                                                 run_git=self.rec)
        # No base movement: fast-forward integration, nothing required.
        self.assertEqual(status.merge_status,
                         worktrees.MERGE_STATUS_FAST_FORWARD)
        self.assertFalse(status.integration_required)
        # Now the base absorbs the branch commit and moves past it: the
        # branch content is an ancestor of the live base (drift still forms
        # the explicit integration follow-up).
        git("merge", "-q", "--ff-only", commit.branch, cwd=self.repo)
        (self.repo / "more.py").write_text("more\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "past", cwd=self.repo)
        status = delivery.check_integration_state(self.conn, attempt.id,
                                                 run_git=self.rec)
        self.assertEqual(status.merge_status, worktrees.MERGE_STATUS_ANCESTOR)
        self.assertTrue(status.base_drift)
        self.assertTrue(status.integration_required)


# ---------------------------------------------------------------------------
# Acceptance 2 — cleanup gate: eligibility, refusal, preservation
# ---------------------------------------------------------------------------


class CleanupGateTests(M406Fixture):
    def test_cleanup_status_does_not_repair_missing_identity_markers(self):
        task, attempt, allocation, _commit, _ship = self.made_cleanable()
        self.bind_member(task, attempt, name="m406-cleanup-status")
        path = allocation.worktree_path
        marker_keys = (
            "teamcraft.allocationId", "teamcraft.attemptId",
            "teamcraft.taskId",
        )
        for key in marker_keys:
            git("config", "--worktree", "--unset-all", key, cwd=Path(path))
            self.assertIsNone(self.marker_value(path, key))
        event_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0])

        status = runtime_adapter.member_workspace_cleanup_status(
            self.conn, "m406-cleanup-status", run_git=self.rec)

        self.assertFalse(status["cleanup"]["eligible"])
        self.assertIn("marker", " | ".join(status["cleanup"]["reasons"]))
        self.assertEqual(int(self.conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]), event_count)
        for key in marker_keys:
            self.assertIsNone(self.marker_value(path, key))

    def test_fresh_allocation_is_not_eligible_and_refusal_is_audited(self):
        _task, attempt, allocation = self.make_ship()
        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.state, "refused")
        joined = " | ".join(verdict.reasons)
        self.assertIn("worktree-not-cleanable", joined)
        # Worker finished but nothing was approved yet: still not DONE.
        self.assertIn("delivery-task-verifying", joined)
        with self.assertRaises(delivery.DeliveryRefusal):
            delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                             lease=self.lease(),
                                             run_git=self.rec)
        # Refusal preserved the scene and left durable audit evidence.
        self.assertTrue(Path(allocation.worktree_path).exists())
        self.assertTrue(self.worktree_registered(allocation.worktree_path))
        refused = self.event_rows(delivery.CLEANUP_REFUSED_EVENT_KIND,
                                 attempt_id=attempt.id)
        self.assertEqual(len(refused), 1)
        payload = json.loads(str(refused[0][2]))
        self.assertIn("worktree-not-cleanable",
                     " | ".join(payload["reasons"]))

    def test_no_approval_gate_before_done(self):
        _task, attempt, allocation = self.make_ship()
        self.configure_and_verify(attempt)
        commit = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key="commit-1",
            lease=self.lease(), run_git=self.rec)
        delivery.record_ship_evidence(self.conn, attempt.id,
                                     idempotency_key="ship-1",
                                     lease=self.lease(), run_git=self.rec)
        # Task is VERIFYING (no approval yet): cleanup must refuse.
        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertFalse(verdict.eligible)
        self.assertIn("delivery-task-verifying", verdict.reasons)
        self.assertTrue(Path(allocation.worktree_path).exists())
        self.assertIsNotNone(commit.commit_sha)

    def test_cleanable_but_dirty_workspace_is_preserved(self):
        _task, attempt, allocation, _commit, _ship = self.made_cleanable()
        dirty = Path(allocation.worktree_path) / "src" / "residue.py"
        dirty.parent.mkdir(parents=True, exist_ok=True)
        dirty.write_text("worker residue\n", encoding="utf-8")

        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertFalse(verdict.eligible)
        joined = " | ".join(verdict.reasons)
        # The dirty scene surfaces through the live evidence-currency gate.
        self.assertIn("dirty", joined)
        self.assertIn("src/residue.py", joined)
        with self.assertRaises(delivery.DeliveryRefusal):
            delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                             lease=self.lease(),
                                             run_git=self.rec)
        # The dirty scene survived untouched; nothing was recorded CLEANED.
        self.assertEqual(dirty.read_text(), "worker residue\n")
        self.assertTrue(self.worktree_registered(allocation.worktree_path))
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))

    def test_undelivered_commits_fail_closed(self):
        _task, attempt, allocation, _commit, _ship = self.made_cleanable()
        # Someone commits on the branch AFTER delivery: the extra commit was
        # never delivered — the workspace must be preserved.
        worktree = Path(allocation.worktree_path)
        (worktree / "src" / "extra.py").write_text("extra\n", encoding="utf-8")
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "undelivered", cwd=worktree)

        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertFalse(verdict.eligible)
        joined = " | ".join(verdict.reasons)
        self.assertTrue("undelivered-commits" in joined or "moved" in joined,
                        joined)
        with self.assertRaises(delivery.DeliveryRefusal):
            delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                             lease=self.lease(),
                                             run_git=self.rec)
        self.assertTrue(worktree.exists())
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))

    def test_foreign_identity_markers_refused_and_preserved(self):
        _task, attempt, allocation, _commit, _ship = self.made_cleanable()
        worktree = Path(allocation.worktree_path)
        git("config", "extensions.worktreeConfig", "true", cwd=worktree)
        git("config", "--worktree", "teamcraft.taskId", "someone-else",
            cwd=worktree)

        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertFalse(verdict.eligible)
        joined = " | ".join(verdict.reasons)
        # Foreign identity markers must be named among the refusal reasons.
        self.assertIn("identity", joined)
        self.assertIn("someone-else", joined)
        with self.assertRaises(delivery.DeliveryRefusal):
            delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                             lease=self.lease(),
                                             run_git=self.rec)
        # Foreign state preserved: still registered, marker intact, no fact.
        self.assertTrue(self.worktree_registered(allocation.worktree_path))
        self.assertEqual(
            git("config", "--worktree", "--get", "teamcraft.taskId",
                cwd=worktree),
            "someone-else")
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))

    def test_workspace_missing_requires_reconcile_not_cleanup(self):
        _task, attempt, allocation, _commit, _ship = self.made_cleanable()
        # Something removed the directory outside our flow (unpruned).
        shutil.rmtree(allocation.worktree_path)

        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertFalse(verdict.eligible)
        self.assertIn("workspace-missing", verdict.reasons)
        with self.assertRaises(delivery.DeliveryRefusal):
            delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                             lease=self.lease(),
                                             run_git=self.rec)
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))


# ---------------------------------------------------------------------------
# Acceptance 2/3 — allowed cleanup, single durable fact, replay
# ---------------------------------------------------------------------------


class CleanupSuccessTests(M406Fixture):
    def test_full_gate_cleans_once_and_replays(self):
        _task, attempt, allocation, commit, _ship = self.made_cleanable()
        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertTrue(verdict.eligible, verdict.reasons)
        self.assertEqual(verdict.state, "eligible")

        result = delivery.cleanup_attempt_worktree(
            self.conn, attempt.id, lease=self.lease(), run_git=self.rec)
        self.assertEqual(result["disposition"], "removed")
        self.assertEqual(result["cleaned"]["commit_sha"], commit.commit_sha)
        self.assertEqual(result["cleaned"]["delivery_mode"], "local_only")
        self.assertFalse(Path(allocation.worktree_path).exists())
        self.assertFalse(self.worktree_registered(allocation.worktree_path))
        # Branch + delivered commit are PRESERVED: refs outlive the workspace.
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{commit.branch}",
                cwd=self.repo),
            commit.commit_sha)
        # Exactly one deterministic CLEANED fact; lifecycle marker preserved.
        rows = self.event_rows(worktrees.CLEANED_EVENT_KIND,
                              attempt_id=attempt.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "cleanable")

        # Replay: idempotent convergence, no second fact, no error.
        replay = delivery.cleanup_attempt_worktree(
            self.conn, attempt.id, lease=self.lease(), run_git=self.rec)
        self.assertEqual(replay["disposition"], "replayed")
        self.assertEqual(replay["cleaned"]["event_id"],
                        result["cleaned"]["event_id"])
        self.assertEqual(len(self.event_rows(worktrees.CLEANED_EVENT_KIND,
                                            attempt_id=attempt.id)), 1)
        status = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertEqual(status.state, "cleaned")
        self.assertEqual(status.reasons, ("already-cleaned",))

    def test_cleanup_verbs_are_never_forbidden_and_never_forced(self):
        _task, attempt, allocation, _commit, _ship = self.made_cleanable()
        self.rec.calls.clear()
        delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                         lease=self.lease(), run_git=self.rec)
        verbs = set(self.rec.verbs())
        forbidden = {"push", "pull", "fetch", "clone", "merge", "rebase",
                    "reset", "clean", "checkout", "switch", "restore",
                    "prune", "gc", "filter-branch", "tag", "notes", "mv"}
        self.assertFalse(verbs.intersection(forbidden))
        removals = [c for c in self.rec.calls
                   if c[:2] == ["worktree", "remove"]]
        self.assertEqual(len(removals), 1)
        self.assertNotIn("--force", removals[0])
        self.assertNotIn("-f", removals[0])
        self.assertEqual(removals[0][2], allocation.worktree_path)

    def test_stale_evidence_after_done_refuses_cleanup(self):
        task, attempt, allocation, _commit, _ship = self.made_cleanable()
        # Task mutation after DONE reopens it (evidence invalidation): the
        # workspace must not be cleaned on the strength of stale evidence.
        domain.update_task(self.conn, task.id, expected_version=task.version,
                          title="deliver (edited)")
        reopened = domain.get_task(self.conn, task.id)
        self.assertEqual(reopened.status, "needs_human")
        verdict = delivery.evaluate_cleanup_eligibility(self.conn, attempt.id,
                                                       run_git=self.rec)
        self.assertFalse(verdict.eligible)
        self.assertIn("delivery-task-needs_human", verdict.reasons)
        with self.assertRaises(delivery.DeliveryRefusal):
            delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                             lease=self.lease(),
                                             run_git=self.rec)
        self.assertTrue(Path(allocation.worktree_path).exists())


# ---------------------------------------------------------------------------
# Acceptance 3 — crash recovery / replay
# ---------------------------------------------------------------------------


class CleanupRecoveryTests(M406Fixture):
    def test_crash_after_effect_is_adopted_by_reconcile(self):
        _task, attempt, allocation, commit, _ship = self.made_cleanable()
        # Simulate the crash window: the guarded removal landed, but the
        # CLEANED fact never committed.
        git("worktree", "remove", allocation.worktree_path, cwd=self.repo)
        self.assertFalse(Path(allocation.worktree_path).exists())
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))

        # Plain cleanup fails closed with diagnosis (workspace-missing) and
        # does NOT fabricate a fact.
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.cleanup_attempt_worktree(self.conn, attempt.id,
                                             lease=self.lease(),
                                             run_git=self.rec)
        self.assertIn("workspace-missing", str(ctx.exception))
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))

        # Reconcile re-proves every durable fact and adopts the effect.
        result = delivery.reconcile_cleanup_worktree(
            self.conn, attempt.id, lease=self.lease(), run_git=self.rec)
        self.assertEqual(result["disposition"], "adopted")
        self.assertEqual(result["cleaned"]["commit_sha"], commit.commit_sha)
        rows = self.event_rows(worktrees.CLEANED_EVENT_KIND,
                              attempt_id=attempt.id)
        self.assertEqual(len(rows), 1)

        # Recovery is itself idempotent.
        again = delivery.reconcile_cleanup_worktree(
            self.conn, attempt.id, lease=self.lease(), run_git=self.rec)
        self.assertEqual(again["disposition"], "replayed")
        self.assertEqual(len(self.event_rows(worktrees.CLEANED_EVENT_KIND,
                                            attempt_id=attempt.id)), 1)

    def test_finalize_requires_cleanable_and_terminal(self):
        _task, attempt, allocation, commit, _ship = self.delivered()
        # Delivered + DONE but the workspace was NEVER marked cleanable.
        with self.assertRaises(IllegalTransitionError) as ctx:
            worktrees.finalize_cleaned_worktree(
                self.conn, attempt.id, commit_sha=commit.commit_sha,
                delivery_mode="local_only", run_git=self.rec)
        self.assertEqual(getattr(ctx.exception, "reason_code", None),
                         "WORKTREE_NOT_CLEANABLE")
        self.assertTrue(Path(allocation.worktree_path).exists())
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))


class LowerLevelSafetyTests(M406Fixture):
    def test_unknown_integration_state_never_claims_clean(self):
        _task, attempt, allocation = self.make_ship()
        # Make the branch diverge for real so the merge-tree probe is the
        # only way to answer — then make that probe fail.
        worktree = Path(allocation.worktree_path)
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "branch work", cwd=worktree)
        (self.repo / "later.py").write_text("later\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "base advanced", cwd=self.repo)

        failing = RecordingRunner()

        def runner(args, cwd):
            if args and args[0] == "merge-tree":
                raise workspaces.GitStateError(
                    "git merge-tree failed (exit 128): boom")
            return failing(args, cwd)

        status = delivery.check_integration_state(self.conn, attempt.id,
                                                 run_git=runner)
        self.assertTrue(status.base_drift)
        # Undecidable merge probe must NOT be reported as clean.
        self.assertEqual(status.merge_status, worktrees.MERGE_STATUS_UNKNOWN)
        self.assertIn("undecidable", " | ".join(status.problems))
        # Drift alone keeps the integration requirement honest.
        self.assertTrue(status.integration_required)
        _s, created_task, created = delivery.ensure_integration_task(
            self.conn, attempt.id, run_git=runner)
        self.assertTrue(created)
        assert created_task is not None

    def test_cleanup_requires_allocation(self):
        with self.assertRaises(NotFoundError):
            delivery.cleanup_attempt_worktree(self.conn, "att-unknown",
                                             lease=self.lease(),
                                             run_git=self.rec)
        verdict = delivery.evaluate_cleanup_eligibility(self.conn,
                                                       "att-unknown")
        self.assertEqual(verdict.reasons, ("no-allocation",))

    def test_adopted_path_refuses_when_registration_remains(self):
        _task, attempt, allocation, commit, _ship = self.made_cleanable()
        # Directory deleted manually WITHOUT git: registration persists.
        shutil.rmtree(allocation.worktree_path)
        with self.assertRaises(StorageError):
            worktrees.finalize_cleaned_worktree(
                self.conn, attempt.id, commit_sha=commit.commit_sha,
                delivery_mode="local_only", run_git=self.rec)
        self.assertIsNone(worktrees.load_cleaned_record(self.conn, attempt.id))

    def test_idempotent_requirement_payload_survives_moving_facts(self):
        # Requirement payload is stable, so a LATER drifted re-check still
        # replays the same requirement instead of failing or duplicating.
        _task, attempt, _allocation = self.make_ship()
        (self.repo / "one.py").write_text("one\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "advance one", cwd=self.repo)
        _s, task1, created1 = delivery.ensure_integration_task(
            self.conn, attempt.id, run_git=self.rec)
        self.assertTrue(created1)
        # Base moves AGAIN; ensure must still converge (not fail closed)
        # onto the same integration Task.
        (self.repo / "two.py").write_text("two\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "advance two", cwd=self.repo)
        _s2, task2, created2 = delivery.ensure_integration_task(
            self.conn, attempt.id, run_git=self.rec)
        self.assertFalse(created2)
        assert task1 is not None and task2 is not None
        self.assertEqual(task1.id, task2.id)
        self.assertEqual(len(self.event_rows(
            delivery.INTEGRATION_REQUIRED_EVENT_KIND,
            attempt_id=attempt.id)), 1)


if __name__ == "__main__":
    unittest.main()
