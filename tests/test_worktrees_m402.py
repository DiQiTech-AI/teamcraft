"""M4-02 Worktree lifecycle tests (M0 review §9.4 M4-02).

Every test runs against TEMPORARY repositories only — the real project
repository is never touched, and no branch/worktree is added to it.

Coverage of the acceptance matrix:
* two concurrent writable Attempts get DISTINCT worktrees/branches and never
  share uncommitted edits;
* restart discovery/claim adopts ONLY the exact recorded identity
  (path + branch + repository + worktree markers);
* a failed worker with dirty changes leaves the workspace AND the durable
  allocation discoverable (never cleaned, never marked cleanable);
* read-only Attempts (no ownership claims) receive no writable worktree;
* refusal cases fail closed: foreign markers, occupied/registered paths,
  branch collision at a foreign SHA, unmerged git state, base evidence whose
  verified SHA moved, tampered/duplicated durable records;
* durable records ride the append-only events stream (schema unchanged at
  this milestone — the v16 head contract and all earlier suites stay green);
* ALLOCATED -> CLEANABLE only for a terminal Attempt with an exact identity
  and a completely clean workspace; CLEANABLE replays and NOTHING is ever
  deleted here (cleanup is M4-06).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from teamcraft import domain, storage, workspaces, worktrees
from teamcraft.errors import (
    ConflictError,
    IllegalTransitionError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from teamcraft.worktrees import (
    ALLOCATED_EVENT_KIND,
    CLEANABLE_EVENT_KIND,
    WorktreeIdentityError,
)

GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false", "-c", "user.name=T",
    "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
]


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run([*GIT, *args], cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def init_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo


class _Fixture(unittest.TestCase):
    """Temp Git repository + migrated runtime DB + ready Task/Attempt helper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m402-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo = init_repo(self.base)
        self.db = self.base / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "demo", str(self.repo))

    # -- helpers -----------------------------------------------------------

    def make_attempt(self, claims: list[str]) -> tuple[domain.Task, domain.Attempt]:
        task = domain.create_task(
            self.conn, self.project.id, "deliver",
            ownership_json=json.dumps(claims))
        domain.apply_task_event(self.conn, task.id, "dependencies_satisfied",
                               expected_version=task.version)
        task = domain.get_task(self.conn, task.id)
        attempt = domain.create_attempt(self.conn, task.id,
                                       expected_version=task.version)
        return domain.get_task(self.conn, task.id), attempt

    def allocate(self, task: domain.Task, attempt: domain.Attempt, **kw):
        return worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.repo), **kw)

    def raw(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db))

    def fail_attempt(self, attempt: domain.Attempt) -> None:
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "start_failed")


# ---------------------------------------------------------------------------
# Distinct, isolated worktrees per writable Attempt
# ---------------------------------------------------------------------------


class AllocationTests(_Fixture):
    def test_two_attempts_get_distinct_worktrees_and_do_not_share_edits(self):
        task_a, attempt_a = self.make_attempt(["src/a/**"])
        task_b, attempt_b = self.make_attempt(["src/b/**"])
        alloc_a, created_a = self.allocate(task_a, attempt_a)
        alloc_b, created_b = self.allocate(task_b, attempt_b)
        self.assertTrue(created_a and created_b)
        self.assertNotEqual(alloc_a.worktree_path, alloc_b.worktree_path)
        self.assertNotEqual(alloc_a.branch, alloc_b.branch)
        self.assertEqual(alloc_a.branch, f"tc/{task_a.id}/{attempt_a.id}")
        self.assertEqual(alloc_b.branch, f"tc/{task_b.id}/{attempt_b.id}")
        self.assertTrue(alloc_a.worktree_path.startswith(
            str(self.repo) + "/.teamcraft/worktrees/"))
        # Edits in one workspace are invisible to the other and to the root.
        (Path(alloc_a.worktree_path) / "src" / "a" / "only_a.py").parent.mkdir(
            parents=True, exist_ok=True)
        (Path(alloc_a.worktree_path) / "src" / "a" / "only_a.py").write_text(
            "a only\n", encoding="utf-8")
        self.assertFalse((Path(alloc_b.worktree_path) / "src" / "a"
                         / "only_a.py").exists())
        self.assertFalse((self.repo / "src" / "a" / "only_a.py").exists())
        listed = git("worktree", "list", cwd=self.repo)
        self.assertIn(alloc_a.worktree_path, listed)
        self.assertIn(alloc_b.worktree_path, listed)

    def test_allocated_worktree_starts_at_base_and_attempt_row_binds(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        self.assertEqual(alloc.base_sha, git("rev-parse", "HEAD", cwd=self.repo))
        self.assertEqual(git("rev-parse", "HEAD",
                             cwd=Path(alloc.worktree_path)), alloc.base_sha)
        self.assertEqual(git("symbolic-ref", "--short", "HEAD",
                             cwd=Path(alloc.worktree_path)), alloc.branch)
        row = domain.get_attempt(self.conn, attempt.id)
        self.assertEqual(row.worktree_path, alloc.worktree_path)
        self.assertEqual(row.branch, alloc.branch)
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "allocated")

    def test_replay_adopts_exact_identity_idempotently(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc_a, created_a = self.allocate(task, attempt)
        alloc_b, created_b = self.allocate(task, attempt)
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(alloc_a, alloc_b)

    def test_retry_attempt_gets_its_own_workspace(self):
        task, attempt1 = self.make_attempt(["src/**"])
        alloc1, _ = self.allocate(task, attempt1)
        self.fail_attempt(attempt1)
        domain.apply_task_event(self.conn, task.id, "retry_approved",
                               expected_version=task.version)
        task = domain.get_task(self.conn, task.id)
        attempt2 = domain.create_attempt(self.conn, task.id,
                                        expected_version=task.version)
        alloc2, created2 = self.allocate(task, attempt2)
        self.assertTrue(created2)
        self.assertNotEqual(alloc1.worktree_path, alloc2.worktree_path)
        self.assertNotEqual(alloc1.branch, alloc2.branch)

    def test_uses_m401_base_evidence_and_refuses_moved_base(self):
        task, attempt = self.make_attempt(["src/**"])
        evidence, _ = workspaces.capture_base_evidence(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.repo))
        alloc, _ = self.allocate(task, attempt)
        self.assertEqual(alloc.base_sha, evidence.base_sha)
        # A different Attempt whose verified base moved off live HEAD fails.
        (self.repo / "moved.py").write_text("moved\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "advanced", cwd=self.repo)
        task2, attempt2 = self.make_attempt(["src/**"])
        workspaces.capture_base_evidence(self.conn, task_id=task2.id,
                                        attempt_id=attempt2.id,
                                        repo_root=str(self.repo))
        # capture bound the NEW head; the first evidence is now stale vs HEAD.
        task3, attempt3 = self.make_attempt(["src/**"])
        # Forge: capture evidence then advance again before allocating.
        workspaces.capture_base_evidence(self.conn, task_id=task3.id,
                                        attempt_id=attempt3.id,
                                        repo_root=str(self.repo))
        (self.repo / "again.py").write_text("again\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "again", cwd=self.repo)
        with self.assertRaises(ConflictError):
            self.allocate(task3, attempt3)

    def test_read_only_attempt_gets_no_worktree(self):
        task, attempt = self.make_attempt([])  # no owned territory
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(task, attempt)
        self.assertIn("READ_ONLY_ATTEMPT_NO_WORKTREE", str(ctx.exception))
        self.assertIsNone(worktrees.lifecycle_state(self.conn, attempt.id))
        self.assertFalse((self.base / "repo" / ".teamcraft" / "worktrees").exists())

    def test_binding_and_terminal_refusals(self):
        task, attempt = self.make_attempt(["src/**"])
        with self.assertRaises(ValidationError):
            worktrees.allocate_attempt_worktree(
                self.conn, task_id="task-other", attempt_id=attempt.id,
                repo_root=str(self.repo))
        self.fail_attempt(attempt)
        with self.assertRaises(IllegalTransitionError):
            self.allocate(task, attempt)

    def test_foreign_registered_path_and_branch_collision_fail_closed(self):
        task, attempt = self.make_attempt(["src/**"])
        path = worktrees.worktree_path_for(str(self.repo), attempt.id)
        branch = worktrees.worktree_branch(task.id, attempt.id)
        # Somebody else's worktree already occupies the target path.
        git("worktree", "add", "-q", path, "-b", "foreign/branch", cwd=self.repo)
        with self.assertRaises(WorktreeIdentityError) as ctx:
            self.allocate(task, attempt)
        self.assertEqual(ctx.exception.kind, "foreign")
        git("worktree", "remove", "--force", path, cwd=self.repo)
        git("branch", "-D", "foreign/branch", cwd=self.repo)
        # The roadmap branch exists at a foreign SHA.
        git("branch", branch, "HEAD", cwd=self.repo)
        (self.repo / "later.py").write_text("later\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "later", cwd=self.repo)
        with self.assertRaises(ConflictError):
            self.allocate(task, attempt)

    def test_occupied_target_path_is_never_overwritten(self):
        task, attempt = self.make_attempt(["src/**"])
        path = Path(worktrees.worktree_path_for(str(self.repo), attempt.id))
        path.mkdir(parents=True)
        (path / "keepme.txt").write_text("precious\n", encoding="utf-8")
        with self.assertRaises(WorktreeIdentityError) as ctx:
            self.allocate(task, attempt)
        self.assertEqual(ctx.exception.kind, "occupied")
        self.assertEqual((path / "keepme.txt").read_text(), "precious\n")

    def test_failure_after_durable_allocation_preserves_state(self):
        task, attempt = self.make_attempt(["src/**"])

        def failing_add(args, cwd):
            if list(args[:2]) == ["worktree", "add"]:
                raise workspaces.GitStateError("injected git failure")
            return workspaces._default_run_git(args, cwd)

        with self.assertRaises(StorageError):
            self.allocate(task, attempt, run_git=failing_add)
        # Durable allocation survives; a retry must NOT fabricate a
        # different-identity workspace silently.
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "allocated")
        found = worktrees.discover_allocations(self.conn, self.project.id)
        self.assertEqual([a.attempt_id for a, _ in found], [attempt.id])
        with self.assertRaises(WorktreeIdentityError) as ctx:
            self.allocate(task, attempt)
        self.assertEqual(ctx.exception.kind, "missing")


# ---------------------------------------------------------------------------
# Restart discovery / exact-identity claim
# ---------------------------------------------------------------------------


class RestartClaimTests(_Fixture):
    def test_restart_discovers_and_claims_exact_recorded_identity(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        (Path(alloc.worktree_path) / "note.txt").write_text("draft\n",
                                                           encoding="utf-8")
        self.conn.close()
        self.conn = storage.connect(self.db)  # process restart
        found = worktrees.discover_allocations(self.conn)
        self.assertEqual([(a.attempt_id, s) for a, s in found],
                         [(attempt.id, "allocated")])
        claimed = worktrees.claim_worktree(self.conn, attempt.id)
        self.assertEqual(claimed, alloc)
        self.assertTrue((Path(claimed.worktree_path) / "note.txt").exists())

    def test_claim_refuses_foreign_identity_never_adopts(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        git("config", "--worktree", "teamcraft.attemptId", "attempt-evil",
            cwd=Path(alloc.worktree_path))
        with self.assertRaises(WorktreeIdentityError) as ctx:
            worktrees.claim_worktree(self.conn, attempt.id)
        self.assertEqual(ctx.exception.kind, "foreign")
        # The foreign workspace and its content stay untouched.
        self.assertTrue(Path(alloc.worktree_path).is_dir())

    def test_claim_refuses_branch_drift(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        git("checkout", "-q", "--detach", cwd=Path(alloc.worktree_path))
        with self.assertRaises(WorktreeIdentityError) as ctx:
            worktrees.claim_worktree(self.conn, attempt.id)
        self.assertEqual(ctx.exception.kind, "drift")

    def test_claim_repairs_lost_markers_on_exact_identity(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        wt_git = Path(git("rev-parse", "--git-dir",
                          cwd=Path(alloc.worktree_path)))
        if not wt_git.is_absolute():
            wt_git = Path(alloc.worktree_path) / wt_git
        config = wt_git / "config.worktree"
        self.assertTrue(config.exists())
        config.unlink()
        claimed = worktrees.claim_worktree(self.conn, attempt.id)
        self.assertEqual(claimed, alloc)
        self.assertTrue(config.exists())  # re-pinned

    def test_claim_missing_workspace_fails_closed(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        git("worktree", "remove", "--force", alloc.worktree_path, cwd=self.repo)
        with self.assertRaises(WorktreeIdentityError) as ctx:
            worktrees.claim_worktree(self.conn, attempt.id)
        self.assertEqual(ctx.exception.kind, "missing")
        # The durable record is STILL discoverable for investigation.
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "allocated")

    def test_duplicate_allocations_and_tampered_payload_fail_closed(self):
        task, attempt = self.make_attempt(["src/**"])
        self.allocate(task, attempt)
        raw = self.raw()
        raw.execute(
            "INSERT INTO events (event_id, kind, project_id, task_id,"
            " attempt_id, payload_json, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("evt-wta-duplicate", ALLOCATED_EVENT_KIND, self.project.id,
             task.id, attempt.id, '{"bogus": 1}', "2026-01-01T00:00:00+00:00"))
        raw.commit()
        raw.close()
        with self.assertRaises(StorageError):
            worktrees.load_allocation(self.conn, attempt.id)

    def test_derived_identity_is_pinned_in_the_record(self):
        # A record whose branch/path do not equal the derived naming was
        # tampered with or is foreign; loading fails closed.
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        raw = self.raw()
        raw.execute(
            "INSERT INTO events (event_id, kind, project_id, task_id,"
            " attempt_id, payload_json, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("evt-wta-tamper", ALLOCATED_EVENT_KIND, self.project.id,
             task.id, "attempt-other",
             json.dumps({
                 "allocation_id": alloc.allocation_id,
                 "attempt_id": "attempt-other", "base_sha": alloc.base_sha,
                 "branch": "somewhere/else", "claims": ["src/**"],
                 "claims_digest": "0" * 64, "project_id": self.project.id,
                 "repo_root": str(self.repo), "schema_version": 1,
                 "task_id": task.id, "worktree_path": alloc.worktree_path,
             }, sort_keys=True, separators=(",", ":")),
             "2026-01-01T00:00:00+00:00"))
        raw.commit()
        raw.close()
        with self.assertRaises(StorageError):
            worktrees.load_allocation(self.conn, "attempt-other")


# ---------------------------------------------------------------------------
# ALLOCATED -> CLEANABLE (safe; nothing is ever deleted here)
# ---------------------------------------------------------------------------


class CleanableTests(_Fixture):
    def test_dirty_failed_worker_preserves_scene_and_is_never_cleanable(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        dirty = Path(alloc.worktree_path) / "src" / "work_in_progress.py"
        dirty.parent.mkdir(parents=True, exist_ok=True)
        dirty.write_text("half done\n", encoding="utf-8")
        self.fail_attempt(attempt)
        with self.assertRaises(IllegalTransitionError) as ctx:
            worktrees.mark_worktree_cleanable(self.conn, attempt.id)
        self.assertEqual(ctx.exception.reason_code, "WORKTREE_DIRTY")
        # The failed worker's changes and durable state remain discoverable.
        self.assertEqual(dirty.read_text(), "half done\n")
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "allocated")
        status = worktrees.inspect_worktree(self.conn, attempt.id)
        self.assertTrue(status.exists)
        self.assertTrue(status.dirty)
        self.assertEqual(status.change_count, 1)

    def test_clean_terminal_attempt_becomes_cleanable_and_replays(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        domain.transition_attempt(self.conn, attempt.id, "finish")
        marked = worktrees.mark_worktree_cleanable(self.conn, attempt.id)
        self.assertEqual(marked, alloc)
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "cleanable")
        # Idempotent replay; no reverse transition; NOTHING deleted.
        again = worktrees.mark_worktree_cleanable(self.conn, attempt.id)
        self.assertEqual(again, alloc)
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "cleanable")
        self.assertTrue(Path(alloc.worktree_path).is_dir())
        states = dict((a.attempt_id, s)
                      for a, s in worktrees.discover_allocations(
                          self.conn, self.project.id))
        self.assertEqual(states[attempt.id], "cleanable")

    def test_cleanable_refused_while_attempt_holds_the_slot(self):
        task, attempt = self.make_attempt(["src/**"])
        self.allocate(task, attempt)
        domain.transition_attempt(self.conn, attempt.id, "start")
        with self.assertRaises(IllegalTransitionError) as ctx:
            worktrees.mark_worktree_cleanable(self.conn, attempt.id)
        self.assertEqual(ctx.exception.reason_code, "ATTEMPT_NOT_TERMINAL")

    def test_unmerged_workspace_fails_closed_never_cleanable(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        wt = Path(alloc.worktree_path)
        # Diverging side branch conflicting with the tc branch content.
        target = wt / "src" / "app.py"
        git("checkout", "-q", "-b", "side", cwd=wt)
        target.write_text("side\n", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", "side", cwd=wt)
        git("checkout", "-q", alloc.branch, cwd=wt)
        target.write_text("mine\n", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", "mine", cwd=wt)
        merge = subprocess.run([*GIT, "merge", "--no-commit", "side"],
                              cwd=str(wt), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False)
        self.assertNotEqual(merge.returncode, 0)  # conflict
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        domain.transition_attempt(self.conn, attempt.id, "fail")
        with self.assertRaises(workspaces.GitStateError):
            worktrees.mark_worktree_cleanable(self.conn, attempt.id)
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt.id),
                         "allocated")

    def test_cleanable_requires_exact_identity_too(self):
        task, attempt = self.make_attempt(["src/**"])
        alloc, _ = self.allocate(task, attempt)
        git("config", "--worktree", "teamcraft.taskId", "task-sabotage",
            cwd=Path(alloc.worktree_path))
        self.fail_attempt(attempt)
        with self.assertRaises(WorktreeIdentityError) as ctx:
            worktrees.mark_worktree_cleanable(self.conn, attempt.id)
        self.assertEqual(ctx.exception.kind, "foreign")

    def test_unallocated_attempt_has_no_lifecycle(self):
        task, attempt = self.make_attempt(["src/**"])
        self.assertIsNone(worktrees.lifecycle_state(self.conn, attempt.id))
        with self.assertRaises(NotFoundError):
            worktrees.mark_worktree_cleanable(self.conn, attempt.id)
        with self.assertRaises(NotFoundError):
            worktrees.claim_worktree(self.conn, attempt.id)


# ---------------------------------------------------------------------------
# Naming + storage-shape contracts
# ---------------------------------------------------------------------------


class NamingAndStorageTests(_Fixture):
    def test_branch_and_path_naming(self):
        self.assertEqual(worktrees.worktree_branch("t1", "a1"), "tc/t1/a1")
        self.assertEqual(
            worktrees.worktree_path_for("/abs/root", "a1"),
            "/abs/root/.teamcraft/worktrees/a1")
        for bad in ("", "  ", "with/slash", "with space", "ünïcode"):
            with self.assertRaises(ValidationError):
                worktrees.worktree_branch("t", bad)
            with self.assertRaises(ValidationError):
                worktrees.worktree_path_for("/abs/root", bad)

    def test_lifecycle_records_are_events_and_schema_is_unchanged(self):
        task, attempt = self.make_attempt(["src/**"])
        self.allocate(task, attempt)
        kinds = {str(row[0]) for row in self.conn.execute(
            "SELECT DISTINCT kind FROM events")}
        self.assertIn(ALLOCATED_EVENT_KIND, kinds)
        self.assertNotIn(CLEANABLE_EVENT_KIND, kinds)
        # M4-02 persists on the v16 append-only events stream: no new tables.
        report = storage.migrate_to_head(self.db)
        self.assertEqual(report.to_version, storage.SCHEMA_VERSION)
        tables = set(storage.verify_database(self.db)["tables"])
        self.assertNotIn("worktree_allocations", tables)
        self.assertTrue(storage.verify_database(self.db)["ok"])
        # the allocation is exactly one immutable row per Attempt: the
        # deterministic event_id cannot be reused with different content.
        from teamcraft import events as _events
        with self.assertRaises(StorageError):
            _events.append_event_idempotent(
                self.conn, ALLOCATED_EVENT_KIND, project_id=self.project.id,
                task_id=task.id, attempt_id=attempt.id, payload_json='{"x":1}',
                event_id=worktrees._allocation_event_id(attempt.id))


if __name__ == "__main__":
    unittest.main()
