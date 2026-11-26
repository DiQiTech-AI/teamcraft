"""M4-05 direct-PR Core delivery acceptance tests (M0 review §9.4 M4-05).

End-to-end against REAL local Git (temp repo + a LOCAL BARE ``origin`` reached
by filesystem path) with an injected fake ``gh`` runner.  This suite NEVER
contacts GitHub, never spawns ``gh``, never uses credentials: the fake mirrors
GitHub's decision surface (head guards, mergeability, checks, one-PR-per-head
adoption) and reads branch heads straight from the bare repo, so every fact it
returns is a real Git fact.  Pinned acceptance:

* missing ``gh``/auth -> BLOCKED verdict with ZERO durable writes;
* retries/replays never duplicate a PR (idempotency + reconcile adoption);
* PR creation moves a ship Task VERIFYING -> READY_TO_MERGE and no further;
* head/base/check/policy/Task drift invalidates PR evidence and approvals;
* merge requires a current explicit human approval and a live Leader lease;
* merging into the intended base + verified read-back is the ONLY path to DONE;
* nothing ever pushes a protected branch, and no M4-06 cleanup happens.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import (
    briefs,
    commands,
    delivery,
    domain,
    events,
    github_adapter,
    leader,
    storage,
    verification,
    worker_reports,
    workspaces,
    worktrees,
)
from teamcraft.errors import (
    ConflictError,
    IllegalTransitionError,
    StorageError,
    ValidationError,
)

GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false", "-c", "user.name=T",
    "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
]

POLICY = {"delivery": "direct_pr",
          "direct_pr": {"repo": "acme/widgets", "base": "main"}}

PASSING_CHECKS = [{"name": "ci/test", "status": "COMPLETED",
                   "conclusion": "SUCCESS"}]
PENDING_CHECKS = [{"name": "ci/test", "status": "IN_PROGRESS",
                   "conclusion": None}]


def git(*args: str, cwd: Path, git_dir: Path | None = None) -> str:
    base = list(GIT)
    if git_dir is not None:
        base += ["--git-dir", str(git_dir)]
    proc = subprocess.run([*base, *args], cwd=str(cwd),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         check=False)
    if proc.returncode:
        raise AssertionError(proc.stderr.decode("utf-8", "replace"))
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)


def payload_digest(payload: dict) -> str:
    stripped = {k: v for k, v in payload.items() if k != "evidence_digest"}
    return hashlib.sha256(canonical(stripped).encode("utf-8")).hexdigest()


def init_repo_and_origin(base: Path) -> tuple[Path, Path]:
    origin = base / "origin.git"
    origin.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=base, git_dir=origin)
    repo = base / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.name", "T", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    (repo / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return repo, origin


class FakeGitHub:
    """A deterministic in-process stand-in for the GitHub PR surface.

    Branch heads are read LIVE from the local bare origin, so head drift is a
    real Git fact, not a fixture.  Merge behavior mirrors gh: open +
    mergeable + passing checks + exact ``--match-head-commit`` or refusal.
    """

    def __init__(self, origin: Path, *, authed: bool = True, checks=None):
        self.origin = origin
        self.authed = authed
        self.checks = list(checks if checks is not None else PASSING_CHECKS)
        self.prs: dict[int, dict] = {}
        self.next_number = 1
        self.calls: list[list[str]] = []
        self.create_lost_response = False   # store the PR, then fake a failure
        self.merge_lands_base: str | None = None  # simulate wrong-base landing

    # -- helpers -------------------------------------------------------------

    def head_of(self, branch: str) -> str | None:
        proc = subprocess.run(
            [*GIT, "--git-dir", str(self.origin), "rev-parse", "--verify",
             f"refs/heads/{branch}"],
            cwd=str(self.origin.parent), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False)
        if proc.returncode:
            return None
        return proc.stdout.decode().strip().lower()

    def open_for(self, head: str, base: str) -> list[dict]:
        return [p for p in self.prs.values()
                if p["state"] == "OPEN" and p["head"] == head
                and p["base"] == base]

    def seed(self, *, head: str, base: str, mergeable: str = "MERGEABLE",
             checks=None) -> int:
        number = self.next_number
        self.next_number += 1
        self.prs[number] = {"number": number, "head": head, "base": base,
                           "state": "OPEN", "mergeable": mergeable,
                           "checks": list(checks if checks is not None
                                         else self.checks),
                           "merged_at": None, "merge_commit": None,
                           "url": f"https://github.example/pr/{number}"}
        return number

    def set_base(self, number: int, base: str) -> None:
        self.prs[number]["base"] = base

    def set_checks(self, number: int, checks) -> None:
        self.prs[number]["checks"] = list(checks)

    # -- the runner seam -----------------------------------------------------

    def _json(self, pr: dict) -> bytes:
        head = self.head_of(pr["head"]) or pr.get("head_sha") or ("a" * 40)
        base_oid = self.head_of(pr["base"]) or ("b" * 40)
        return json.dumps({
            "number": pr["number"], "url": pr["url"],
            "headRefName": pr["head"], "headRefOid": head,
            "baseRefName": pr["base"], "baseRefOid": base_oid,
            "state": pr["state"], "mergeable": pr["mergeable"],
            "statusCheckRollup": pr["checks"], "mergedAt": pr["merged_at"],
            "mergeCommit": (None if pr["merge_commit"] is None
                           else {"oid": pr["merge_commit"]}),
        }).encode()

    @staticmethod
    def _flags(argv) -> dict:
        out: dict = {}
        i = 0
        while i < len(argv):
            token = argv[i]
            if token.startswith("--"):
                name = token[2:]
                if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    out[name] = argv[i + 1]
                    i += 2
                    continue
                out[name] = True
            i += 1
        return out

    def __call__(self, argv, cwd: str) -> bytes:
        argv = list(argv)
        self.calls.append(argv)
        verbs = [a for a in argv if not a.startswith("--")]
        if argv[0] == "auth":
            if not self.authed:
                raise github_adapter.GitHubInvocationError(
                    "gh auth status exit 1", exit_code=1,
                    stderr="github.com: not logged in")
            return (b"github.com\n  \u2713 Logged in to github.com account"
                    b" tester (keyring)\n")
        if argv[0] == "pr" and verbs[1:2] == ["list"]:
            flags = self._flags(argv)
            matches = self.open_for(flags.get("head", ""), flags.get("base", ""))
            return json.dumps([json.loads(self._json(p).decode())
                              for p in matches]).encode()
        if argv[0] == "pr" and verbs[1:2] == ["create"]:
            flags = self._flags(argv)
            head = flags.get("head", "")
            base = flags.get("base", "")
            if not base:
                raise github_adapter.GitHubInvocationError(
                    "no base", exit_code=1, stderr="base branch required")
            head_sha = self.head_of(head)
            if head_sha is None:
                raise github_adapter.GitHubInvocationError(
                    "no commits", exit_code=1,
                    stderr=f"No commits on branch {head}")
            if self.open_for(head, base):
                raise github_adapter.GitHubInvocationError(
                    "duplicate", exit_code=1,
                    stderr="A pull request already exists for this branch")
            number = self.seed(head=head, base=base)
            if self.create_lost_response:
                # The PR exists server-side but the response never arrives.
                raise github_adapter.GitHubInvocationError(
                    "connection reset", exit_code=1,
                    stderr="the connection was closed before a response")
            return self._json(self.prs[number])
        if argv[0] == "pr" and verbs[1:2] == ["view"]:
            number = int(verbs[2])
            if number not in self.prs:
                raise github_adapter.GitHubInvocationError(
                    "not found", exit_code=1, stderr="Could not resolve PR")
            return self._json(self.prs[number])
        if argv[0] == "pr" and verbs[1:2] == ["merge"]:
            number = int(verbs[2])
            flags = self._flags(argv)
            pr = self.prs.get(number)
            if pr is None or pr["state"] != "OPEN":
                raise github_adapter.GitHubInvocationError(
                    "not open", exit_code=1, stderr="pull request not open")
            head = self.head_of(pr["head"])
            guard = flags.get("match-head-commit")
            if guard and guard != head:
                raise github_adapter.GitHubInvocationError(
                    "head mismatch", exit_code=1,
                    stderr="the head commit does not match")
            if pr["mergeable"] != "MERGEABLE":
                raise github_adapter.GitHubInvocationError(
                    "not mergeable", exit_code=1,
                    stderr="pull request is not mergeable")
            if not all(str(c.get("conclusion") or "").upper()
                       in ("SUCCESS", "NEUTRAL", "SKIPPED")
                       for c in pr["checks"]):
                raise github_adapter.GitHubInvocationError(
                    "checks", exit_code=1,
                    stderr="status checks are pending or failing")
            if self.merge_lands_base:
                pr["base"] = self.merge_lands_base
            pr["state"] = "MERGED"
            pr["merged_at"] = "2026-11-14T12:00:00Z"
            pr["merge_commit"] = sha1(f"merge|{number}|{head}")
            return f"pull request #{number} merged\n".encode()
        raise AssertionError(f"unexpected gh argv from core: {argv!r}")


class RecordingGitRunner:
    """Real git runner that records every argv for verb auditing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd) -> bytes:
        self.calls.append(list(args))
        return workspaces._default_run_git(args, str(cwd))

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls if c]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = self.value + timedelta(seconds=seconds)


class DirectPrDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m405-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo, self.origin = init_repo_and_origin(self.base)
        self.db = self.base / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "demo", str(self.repo))
        self.rec = RecordingGitRunner()
        self.gh = FakeGitHub(self.origin)

    # -- helpers -------------------------------------------------------------

    def lease(self, holder: str = "m405-leader") -> leader.Lease:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), holder,
            ttl_seconds=3700)

    def count(self, table: str) -> int:
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def durable_snapshot(self) -> tuple[int, int]:
        return self.count("commands"), self.count("events")

    def make_ship(self, *, policy: dict | None = None, title: str = "ship it",
                 kind: str = "ship"):
        task = domain.create_task(
            self.conn, self.project.id, title, kind=kind,
            ownership_json=json.dumps(["src/**"]))
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version)
        task = domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy=policy if policy is not None else POLICY,
            actor="ceo", reason="m405 test policy")
        attempt = domain.create_attempt(self.conn, task.id,
                                       expected_version=task.version)
        domain.register_session(self.conn, attempt.id, "opencode",
                               native_session_id=f"native-{attempt.id}",
                               pane_id="pane-m405", state="working")
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        allocation, _ = worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.repo))
        (Path(allocation.worktree_path) / "src" / "feature.py").write_text(
            "feature\n", encoding="utf-8")
        brief = briefs.create_attempt_brief(self.conn, attempt.id,
                                           content="M4-05 delivery brief")
        report = {
            "schema_version": 1, "task_id": task.id, "attempt_id": attempt.id,
            "task_version": brief.task_version,
            "brief_digest": brief.brief_digest, "report_kind": "final",
            "outcome": "completed", "milestone_id": "final",
            "summary": "implementation complete", "artifact_digest": None,
            "usage": {"total_tokens": None},
        }
        worker_reports.record_worker_report(self.conn, report)
        domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version)
        return (domain.get_task(self.conn, task.id),
                domain.get_attempt(self.conn, attempt.id), allocation)

    def configure_and_verify(self, attempt):
        verification.configure_verifier_profile(
            self.conn, self.project.id, profile="test",
            argv=[sys.executable, "-c", "print('ok')"], timeout_seconds=30,
            actor="human", idempotency_key="cfg-test", lease=self.lease(),
            enabled=True)
        result = verification.run_worktree_verifier(
            self.conn, attempt.id, profile="test",
            idempotency_key=f"run-test-{attempt.id}", lease=self.lease())
        assert result.status == "pass", result.status

    def ready_attempt(self, *, policy: dict | None = None, kind: str = "ship",
                     tag: str = "a"):
        task, attempt, allocation = self.make_ship(policy=policy, kind=kind,
                                                  title=f"m405 ship {tag}")
        self.configure_and_verify(attempt)
        commit = delivery.create_local_commit(
            self.conn, attempt.id, idempotency_key=f"commit-{tag}",
            lease=self.lease(), run_git=self.rec)
        return task, attempt, allocation, commit

    def prepared(self, attempt, key: str = "pr-1", **kw):
        result = delivery.prepare_direct_pr(
            self.conn, attempt.id, idempotency_key=key, lease=self.lease(),
            run_git=self.rec, run_gh=self.gh,
            which=lambda name: "/usr/bin/gh", **kw)
        return result

    def approved(self, evidence, *, decision="approve", key="ap-1",
                 pr_number=None, head_sha=None, base=None, **over):
        return delivery.approve_direct_pr_merge(
            self.conn, evidence.id, decision=decision,
            pr_number=(evidence.pr_number if pr_number is None else pr_number),
            head_sha=(evidence.head_sha if head_sha is None else head_sha),
            base=(evidence.base_ref if base is None else base),
            actor="human-approver", reason="looks good",
            idempotency_key=key, lease=self.lease(), run_gh=self.gh,
            which=lambda name: "/usr/bin/gh", **over)

    def delivered(self, evidence, key: str = "merge-1"):
        return delivery.merge_direct_pr(
            self.conn, evidence.id, idempotency_key=key, lease=self.lease(),
            run_gh=self.gh, which=lambda name: "/usr/bin/gh")

    def events_of(self, kind: str, task_id: str | None = None) -> list[str]:
        sql = "SELECT payload_json FROM events WHERE kind = ?"
        args: list = [kind]
        if task_id:
            sql += " AND task_id = ?"
            args.append(task_id)
        return [str(r[0]) for r in self.conn.execute(sql, args).fetchall()]

    def command_state(self, kind: str, index: int = 0) -> str:
        rows = self.conn.execute(
            "SELECT state FROM commands WHERE kind = ? ORDER BY requested_at",
            (kind,)).fetchall()
        return str(rows[index][0])

    def _legacy_pr_payload_json(self, evidence) -> str:
        """Rewrite one v2 PR row as the exact v1 shape (no verifier_digest)."""

        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind = ? AND event_id = ?",
            (delivery.PR_EVIDENCE_KIND, evidence.id)).fetchone()
        payload = json.loads(str(row[0]))
        payload.pop("verifier_digest", None)
        payload["schema_version"] = 1
        payload.pop("evidence_digest", None)
        payload["evidence_digest"] = payload_digest(payload)
        return canonical(payload)

    def _append_legacy_pr(self, task, attempt, evidence,
                          event_id: str = "evt-dpe-legacy-v1"):
        return events.append_event(
            self.conn, delivery.PR_EVIDENCE_KIND,
            project_id=task.project_id, task_id=task.id,
            attempt_id=attempt.id,
            payload_json=self._legacy_pr_payload_json(evidence),
            event_id=event_id)

    def _legacy_pr_command_payload(self, task, attempt, allocation, commit):
        # Exact pre-v2 shape: no verifier_digest pin was written at all.
        return {
            "project_id": self.project.id, "task_id": task.id,
            "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "dispatch_task_version": task.version - 1,
            "brief_digest": self._brief_digest(attempt.id),
            "brief_version": 1, "worker_report_id": self._report_id(attempt.id),
            "worker_report_digest": self._report_digest(attempt.id),
            "allocation_id": allocation.allocation_id,
            "worktree_path": allocation.worktree_path, "branch": allocation.branch,
            "base_sha": allocation.base_sha, "commit_sha": commit.commit_sha,
            "commit_evidence_id": "x", "policy_digest": self._policy_digest(),
            "repo": POLICY["direct_pr"]["repo"], "base": "main",
            "merge_method": "merge",
        }

    # -- BLOCKED with zero writes ---------------------------------------------

    def test_missing_gh_blocks_with_zero_writes(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        before = self.durable_snapshot()
        git_calls_before = len(self.rec.calls)
        result = delivery.prepare_direct_pr(
            self.conn, attempt.id, idempotency_key="pr-1", lease=self.lease(),
            run_git=self.rec, run_gh=self.gh, which=lambda name: None)
        self.assertTrue(result.blocked)
        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.task_status, "verifying")
        self.assertIsNone(result.pr_evidence)
        # Nothing was written and nothing was asked of git/gh.
        self.assertEqual(self.durable_snapshot(), before)
        self.assertEqual(len(self.rec.calls), git_calls_before)
        self.assertEqual(self.gh.calls, [])
        # No PR evidence rows exist.
        self.assertEqual(self.events_of(delivery.PR_EVIDENCE_KIND, task.id), [])

    def test_unauthenticated_blocks_with_zero_writes(self):
        _task, attempt, _alloc, _commit = self.ready_attempt()
        self.gh.authed = False
        before = self.durable_snapshot()
        result = delivery.prepare_direct_pr(
            self.conn, attempt.id, idempotency_key="pr-1", lease=self.lease(),
            run_git=self.rec, run_gh=self.gh,
            which=lambda name: "/usr/bin/gh")
        self.assertTrue(result.blocked)
        self.assertIn("not authenticated", result.detail)
        self.assertEqual(self.durable_snapshot(), before)
        # Only the read-only auth probe touched gh; no push, no pr create.
        self.assertEqual(self.gh.calls, [["auth", "status"]])
        self.assertNotIn("push", self.rec.verbs())

    # -- happy path: create -> approve -> merge -> DONE ----------------------

    def test_full_direct_pr_create_approve_merge_to_done(self):
        task, attempt, allocation, commit = self.ready_attempt()
        result = self.prepared(attempt)
        self.assertEqual(result.outcome, "ready")
        self.assertFalse(result.blocked)
        self.assertTrue(result.ready)
        evidence = result.pr_evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.head_sha, commit.commit_sha)
        self.assertEqual(evidence.base_ref, "main")
        self.assertEqual(evidence.source, "created")
        after_prepare = domain.get_task(self.conn, task.id)
        self.assertEqual(after_prepare.status, "ready_to_merge")
        self.assertFalse(after_prepare.status == "done")

        approval = self.approved(evidence)
        self.assertEqual(approval.decision, "approve")
        self.assertEqual(approval.task_status_after, "ready_to_merge")
        mid = domain.get_task(self.conn, task.id)
        self.assertEqual(mid.status, "ready_to_merge")  # approval never merges

        merge = self.delivered(evidence)
        self.assertTrue(merge.merged)
        self.assertEqual(merge.merged_into if hasattr(merge, "merged_into")
                        else merge.base_ref, "main")
        self.assertEqual(merge.intended_base, "main")
        self.assertRegex(merge.merge_commit_sha, r"^[0-9a-f]{40}$")
        done = domain.get_task(self.conn, task.id)
        self.assertEqual(done.status, "done")
        self.assertEqual(self.command_state(delivery.PR_MERGE_COMMAND_KIND),
                       "confirmed")
        self.assertTrue(self.events_of("delivery.pr_merged", task.id))

        # Exactly one PR was ever created; the push was scoped and singular.
        creates = [c for c in self.gh.calls if c[:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(self.gh.prs), 1)
        pushes = [c for c in self.rec.calls if c[0] == "push"]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0], ["push", "origin", allocation.branch])
        for verb in ("merge", "rebase", "reset", "clean", "checkout", "fetch",
                    "pull"):
            self.assertNotIn(verb, self.rec.verbs())
        self.assertNotIn("--force", " ".join(" ".join(c) for c in self.rec.calls))

    def test_idempotent_replay_same_key_and_settled_convergence(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        first = self.prepared(attempt, key="pr-1")
        again = self.prepared(attempt, key="pr-1")
        self.assertEqual(first.pr_evidence.id, again.pr_evidence.id)
        # A NEW key after settled evidence converges too — never a second PR.
        third = self.prepared(attempt, key="pr-2")
        self.assertEqual(third.pr_evidence.id, first.pr_evidence.id)
        creates = [c for c in self.gh.calls if c[:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(self.gh.prs), 1)
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                       "ready_to_merge")

    def test_open_pr_adoption_never_duplicates(self):
        task, attempt, allocation, commit = self.ready_attempt()
        # Push the branch out-of-band and seed an open PR (e.g. filed by a
        # human before TeamCraft got to it).
        git("push", "-q", "origin", allocation.branch, cwd=self.repo)
        self.gh.seed(head=allocation.branch, base="main")
        result = self.prepared(attempt)
        self.assertEqual(result.outcome, "ready")
        self.assertEqual(result.pr_evidence.source, "adopted")
        self.assertEqual(result.pr_evidence.head_sha, commit.commit_sha)
        creates = [c for c in self.gh.calls if c[:2] == ["pr", "create"]]
        self.assertEqual(creates, [])
        self.assertEqual(len(self.gh.prs), 1)

    # -- reconcile after lost receipts -----------------------------------------

    def test_lost_create_response_reconciles_to_adoption_not_duplicate(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        self.gh.create_lost_response = True
        before = self.durable_snapshot()
        with self.assertRaises(StorageError):
            self.prepared(attempt)
        self.gh.create_lost_response = False
        self.assertEqual(self.command_state(delivery.PR_PREPARE_COMMAND_KIND),
                         "unknown")
        rows = self.conn.execute(
            "SELECT id FROM commands WHERE kind = ?",
            (delivery.PR_PREPARE_COMMAND_KIND,)).fetchall()
        outcome = delivery.reconcile_pr_receipt(
            self.conn, str(rows[0][0]), lease=self.lease(), actor="ops",
            reason="response lost", run_git=self.rec, run_gh=self.gh)
        self.assertEqual(outcome["resolution"], "adopted")
        self.assertEqual(len(self.gh.prs), 1)  # exactly one PR, ever
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")
        self.assertEqual(outcome["pr_evidence"].source, "reconciled")
        self.assertEqual(self.command_state(delivery.PR_PREPARE_COMMAND_KIND),
                         "confirmed")
        self.assertGreater(self.durable_snapshot()[1], before[1])

    def test_no_pr_effect_expires_and_fresh_prepare_succeeds(self):
        task, attempt, allocation, commit = self.ready_attempt()
        payload = {
            "project_id": self.project.id, "task_id": task.id,
            "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no, "dispatch_task_version": task.version - 1,
            "brief_digest": self._brief_digest(attempt.id),
            "brief_version": 1, "worker_report_id": self._report_id(attempt.id),
            "worker_report_digest": self._report_digest(attempt.id),
            "allocation_id": allocation.allocation_id,
            "worktree_path": allocation.worktree_path, "branch": allocation.branch,
            "base_sha": allocation.base_sha, "commit_sha": commit.commit_sha,
            "commit_evidence_id": "x", "policy_digest": self._policy_digest(),
            "repo": POLICY["direct_pr"]["repo"], "base": "main",
            "merge_method": "merge", "verifier_digest": "0" * 64,
        }
        submitted = commands.submit_command(
            self.conn, idempotency_key="crashed-1",
            kind=delivery.PR_PREPARE_COMMAND_KIND,
            payload_json=canonical(payload), evidence_json="{}")
        commands.claim_command(self.conn, submitted.command.id)
        commands.mark_command_unknown(self.conn, submitted.command.id,
                                     note="process died after claim")
        outcome = delivery.reconcile_pr_receipt(
            self.conn, submitted.command.id, lease=self.lease(), actor="ops",
            reason="crash window", run_git=self.rec, run_gh=self.gh)
        self.assertEqual(outcome["resolution"], "expired")
        self.assertIsNone(outcome["pr_evidence"])
        # A fresh prepare under a NEW key now creates the one PR.
        result = self.prepared(attempt, key="pr-fresh")
        self.assertEqual(result.outcome, "ready")
        self.assertEqual(len(self.gh.prs), 1)

    # -- contract versioning: v2 current vs v1 read-only ----------------------

    def test_pr_evidence_contract_v2_is_current(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        self.assertEqual(evidence.schema_version, 2)
        self.assertFalse(evidence.legacy)
        self.assertEqual(
            delivery.get_pr_evidence(self.conn, evidence.id).schema_version, 2)
        fresh = delivery.pr_evidence_is_current(self.conn, evidence,
                                               live_gh=False)
        self.assertTrue(fresh.ok, fresh.problems)
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")

    def test_legacy_v1_pr_evidence_is_read_only_and_never_current(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        legacy_row = self._append_legacy_pr(task, attempt, evidence)
        legacy = delivery.load_latest_pr_evidence(self.conn, task.id)
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.event_id, legacy_row.event_id)
        self.assertTrue(legacy.legacy)
        self.assertEqual(legacy.schema_version, 1)
        self.assertEqual(legacy.verifier_digest, "")
        # The legacy reader is stable and does not shape-crash.
        reread = delivery.get_pr_evidence(self.conn, legacy.event_id)
        self.assertEqual(reread.evidence_digest, legacy.evidence_digest)
        fresh = delivery.pr_evidence_is_current(self.conn, legacy,
                                               live_gh=False)
        self.assertFalse(fresh.ok)
        self.assertIn("legacy", " ".join(fresh.problems))
        # A legacy row can never authorize approval or merge.
        with self.assertRaises(delivery.DeliveryRefusal) as caught:
            self.approved(legacy)
        self.assertEqual(caught.exception.kind, "legacy-schema")
        with self.assertRaises(delivery.DeliveryRefusal) as caught:
            self.delivered(legacy)
        self.assertEqual(caught.exception.kind, "legacy-schema")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")
        self.assertEqual(self.events_of("delivery.pr_merged", task.id), [])

    def test_legacy_pr_command_adoption_is_refused(self):
        task, attempt, allocation, commit = self.ready_attempt()
        payload = self._legacy_pr_command_payload(task, attempt, allocation,
                                                  commit)
        submitted = commands.submit_command(
            self.conn, idempotency_key="legacy-crash-1",
            kind=delivery.PR_PREPARE_COMMAND_KIND,
            payload_json=canonical(payload), evidence_json="{}")
        commands.claim_command(self.conn, submitted.command.id)
        commands.mark_command_unknown(self.conn, submitted.command.id,
                                     note="process died before v2 contract")
        git("push", "-q", "origin", allocation.branch, cwd=self.repo)
        self.gh.seed(head=allocation.branch, base="main")
        with self.assertRaises(ConflictError) as caught:
            delivery.reconcile_pr_receipt(
                self.conn, submitted.command.id, lease=self.lease(),
                actor="ops", reason="legacy crash window", run_git=self.rec,
                run_gh=self.gh)
        message = str(caught.exception)
        self.assertIn("predates", message)
        self.assertIn("verifier_digest", message)
        self.assertIn("re-prepare", message)
        self.assertEqual(self.events_of(delivery.PR_EVIDENCE_KIND, task.id), [])
        self.assertEqual(self.command_state(delivery.PR_PREPARE_COMMAND_KIND),
                         "unknown")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "verifying")

    def test_legacy_pr_command_without_pr_effect_expires_cleanly(self):
        task, attempt, allocation, commit = self.ready_attempt()
        payload = self._legacy_pr_command_payload(task, attempt, allocation,
                                                  commit)
        submitted = commands.submit_command(
            self.conn, idempotency_key="legacy-crash-2",
            kind=delivery.PR_PREPARE_COMMAND_KIND,
            payload_json=canonical(payload), evidence_json="{}")
        commands.claim_command(self.conn, submitted.command.id)
        commands.mark_command_unknown(self.conn, submitted.command.id,
                                     note="process died before v2 contract")
        outcome = delivery.reconcile_pr_receipt(
            self.conn, submitted.command.id, lease=self.lease(), actor="ops",
            reason="legacy crash window", run_git=self.rec, run_gh=self.gh)
        self.assertEqual(outcome["resolution"], "expired")
        self.assertIsNone(outcome["pr_evidence"])
        self.assertEqual(self.command_state(delivery.PR_PREPARE_COMMAND_KIND),
                         "expired")
        self.assertEqual(self.events_of(delivery.PR_EVIDENCE_KIND, task.id), [])
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "verifying")

    def _brief_digest(self, attempt_id: str) -> str:
        row = self.conn.execute(
            "SELECT brief_digest, task_version FROM attempt_briefs WHERE"
            " attempt_id = ?", (attempt_id,)).fetchone()
        return str(row[0])

    def _report_id(self, attempt_id: str) -> int:
        return int(self.conn.execute(
            "SELECT id FROM worker_reports WHERE attempt_id = ? AND"
            " report_kind='final' ORDER BY id DESC LIMIT 1",
            (attempt_id,)).fetchone()[0])

    def _report_digest(self, attempt_id: str) -> str:
        return str(self.conn.execute(
            "SELECT report_digest FROM worker_reports WHERE id = ?",
            (self._report_id(attempt_id),)).fetchone()[0])

    def _policy_digest(self) -> str:
        return hashlib.sha256(canonical(POLICY).encode("utf-8")).hexdigest()

    # -- readiness/checks conditions ------------------------------------------

    def test_pending_checks_hold_verifying_until_sync(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        self.gh.checks = PENDING_CHECKS
        result = self.prepared(attempt)
        self.assertEqual(result.outcome, "pending_checks")
        self.assertFalse(result.ready)
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "verifying")
        # Checks complete -> sync records new facts and CASes readiness.
        self.gh.set_checks(result.pr_evidence.pr_number, PASSING_CHECKS)
        synced = delivery.sync_direct_pr(
            self.conn, task.id, idempotency_key="sync-1", lease=self.lease(),
            run_gh=self.gh, which=lambda name: "/usr/bin/gh")
        self.assertEqual(synced.outcome, "ready")
        self.assertTrue(synced.ready)
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")
        self.assertEqual(len(self.gh.prs), 1)
        creates = [c for c in self.gh.calls if c[:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1)

    def test_sync_blocked_reports_blocked_without_new_evidence(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        self.prepared(attempt)
        rows_before = len(self.events_of(delivery.PR_EVIDENCE_KIND, task.id))
        result = delivery.sync_direct_pr(
            self.conn, task.id, idempotency_key="sync-x", lease=self.lease(),
            run_gh=self.gh, which=lambda name: None)
        self.assertTrue(result.blocked)
        self.assertEqual(
            len(self.events_of(delivery.PR_EVIDENCE_KIND, task.id)),
            rows_before)

    # -- drift invalidation ----------------------------------------------------

    def test_head_drift_invalidates_evidence_and_approval(self):
        task, attempt, allocation, _commit = self.ready_attempt()
        result = self.prepared(attempt)
        evidence = result.pr_evidence
        # Someone pushes a new commit to the PR branch on the remote.
        wt = Path(allocation.worktree_path)
        (wt / "src" / "extra.py").write_text("sneaky\n", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", "unrequested follow-up", cwd=wt)
        git("push", "-q", "origin", allocation.branch, cwd=wt)
        freshness = delivery.pr_evidence_is_current(
            self.conn, evidence, run_gh=self.gh,
            which=lambda name: "/usr/bin/gh")
        self.assertFalse(freshness.ok)
        self.assertTrue(any("head" in p for p in freshness.problems))
        with self.assertRaises(delivery.DirectPrRefusal):
            self.approved(evidence)
        merges = [c for c in self.gh.calls if c[:2] == ["pr", "merge"]]
        self.assertEqual(merges, [])
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")

    def test_base_drift_invalidates_approval(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        self.gh.set_base(evidence.pr_number, "release")
        freshness = delivery.pr_evidence_is_current(
            self.conn, evidence, run_gh=self.gh,
            which=lambda name: "/usr/bin/gh")
        self.assertFalse(freshness.ok)
        with self.assertRaises(delivery.DirectPrRefusal):
            self.approved(evidence)

    def test_policy_drift_invalidates_pr_evidence_and_approval(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        domain.set_execution_policy(
            self.conn, task.id,
            expected_version=domain.get_task(self.conn, task.id).version,
            policy={"direct_pr": {"repo": POLICY["direct_pr"]["repo"],
                                 "base": "release"}},
            actor="ceo", reason="operator retargets base")
        freshness = delivery.pr_evidence_is_current(
            self.conn, evidence, live_gh=False)
        self.assertFalse(freshness.ok)
        self.assertTrue(any("policy" in p for p in freshness.problems))
        with self.assertRaises((ConflictError, IllegalTransitionError)):
            self.approved(evidence)

    # -- approval binding / stale leader / merge gates -------------------------

    def test_approval_must_echo_exact_binding(self):
        _task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        with self.assertRaises(ValidationError):
            delivery.approve_direct_pr_merge(
                self.conn, evidence.id, decision="approve", pr_number=None,
                head_sha=None, base=None, actor="a", reason="r",
                idempotency_key="ap-missing", lease=self.lease(),
                run_gh=self.gh, which=lambda name: "/usr/bin/gh")
        # Override the echo values to force collisions.
        delivery_kwargs = dict(decision="approve", pr_number=999,
                               head_sha=evidence.head_sha,
                               base=evidence.base_ref)
        with self.assertRaises(ConflictError):
            delivery.approve_direct_pr_merge(
                self.conn, evidence.id, actor="a", reason="r",
                idempotency_key="ap-bad1", lease=self.lease(),
                run_gh=self.gh, **delivery_kwargs)
        with self.assertRaises(ConflictError):
            delivery.approve_direct_pr_merge(
                self.conn, evidence.id, decision="approve",
                pr_number=evidence.pr_number, head_sha="f" * 40,
                base=evidence.base_ref, actor="a", reason="r",
                idempotency_key="ap-bad2", lease=self.lease(),
                run_gh=self.gh, which=lambda name: "/usr/bin/gh")
        ok = self.approved(evidence, key="ap-ok")
        self.assertEqual(ok.pr_number, evidence.pr_number)

    def test_merge_without_approval_refuses_and_records_nothing(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        before_cmds = self.count("commands")
        with self.assertRaises(delivery.DirectPrRefusal) as caught:
            self.delivered(evidence)
        self.assertEqual(caught.exception.kind, "approval")
        self.assertEqual(self.count("commands"), before_cmds)
        merges = [c for c in self.gh.calls if c[:2] == ["pr", "merge"]]
        self.assertEqual(merges, [])
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")

    def test_reject_is_settled_and_immutable(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        rejected = self.approved(evidence, decision="reject", key="ap-rej")
        self.assertEqual(rejected.decision, "reject")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")
        replay = self.approved(evidence, decision="reject", key="ap-rej")
        self.assertEqual(replay.id, rejected.id)
        with self.assertRaises(ConflictError):
            self.approved(evidence, decision="approve", key="ap-flip")

    def test_stale_leader_cannot_approve_or_merge(self):
        _task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        clock = MutableClock()
        old = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), "m405-leader",
            ttl_seconds=60, clock=clock)
        clock.advance(120)
        with self.assertRaises(leader.LeaderError):
            delivery.approve_direct_pr_merge(
                self.conn, evidence.id, decision="approve",
                pr_number=evidence.pr_number, head_sha=evidence.head_sha,
                base=evidence.base_ref, actor="ghost", reason="nope",
                idempotency_key="ap-stale", lease=old, run_gh=self.gh,
                which=lambda name: "/usr/bin/gh", clock=clock)
        with self.assertRaises(leader.LeaderError):
            delivery.merge_direct_pr(
                self.conn, evidence.id, idempotency_key="merge-stale",
                lease=old, run_gh=self.gh, which=lambda name: "/usr/bin/gh",
                clock=clock)
        merges = [c for c in self.gh.calls if c[:2] == ["pr", "merge"]]
        self.assertEqual(merges, [])
        self.assertIsNone(delivery.load_pr_merge_approval(self.conn,
                                                         evidence.id))

    # -- wrong base -------------------------------------------------------------

    def test_wrong_base_refused_before_merge_effect(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        self.approved(evidence)
        # The PR's base moves after approval: merge must refuse pre-effect.
        self.gh.set_base(evidence.pr_number, "release")
        with self.assertRaises(delivery.DirectPrRefusal) as caught:
            self.delivered(evidence)
        self.assertEqual(caught.exception.kind, "wrong-base")
        merges = [c for c in self.gh.calls if c[:2] == ["pr", "merge"]]
        self.assertEqual(merges, [])
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")
        self.assertEqual(self.events_of(delivery.PR_MERGE_EVIDENCE_KIND,
                                       task.id), [])

    def test_wrong_base_landed_never_completes_delivery(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        self.approved(evidence)
        # gh merges, but the merge lands on the wrong base anyway.
        self.gh.merge_lands_base = "release"
        with self.assertRaises(delivery.DirectPrRefusal) as caught:
            self.delivered(evidence)
        self.assertEqual(caught.exception.kind, "wrong-base")
        done = domain.get_task(self.conn, task.id)
        self.assertNotEqual(done.status, "done")
        self.assertEqual(done.status, "ready_to_merge")
        self.assertEqual(self.events_of(delivery.PR_MERGE_EVIDENCE_KIND,
                                       task.id), [])
        self.assertTrue(self.events_of("delivery_pr_wrong_base_merge", task.id))
        self.assertEqual(self.command_state(delivery.PR_MERGE_COMMAND_KIND),
                         "unknown")
        rows = self.conn.execute(
            "SELECT id FROM commands WHERE kind = ?",
            (delivery.PR_MERGE_COMMAND_KIND,)).fetchall()
        outcome = delivery.reconcile_pr_merge(
            self.conn, str(rows[0][0]), lease=self.lease(), actor="ops",
            reason="inspect merge", run_gh=self.gh)
        self.assertEqual(outcome["resolution"], "wrong_base")
        self.assertIsNone(outcome["merge_evidence"])
        self.assertNotEqual(domain.get_task(self.conn, task.id).status, "done")

    def test_lost_merge_response_reconciles_to_done_once(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        self.approved(evidence)
        # Break AFTER gh merged but before the receipt: simulate by making the
        # post-merge read-back fail once (view returns not-found mid-flight).
        real_call = self.gh.__call__
        state = {"armed": False}

        def flaky(argv, cwd):
            argv = list(argv)
            if (state["armed"] and argv[:2] == ["pr", "view"]
                    and argv[2] == str(evidence.pr_number)
                    and self.gh.prs[evidence.pr_number]["state"] == "MERGED"):
                state["armed"] = False
                raise github_adapter.GitHubInvocationError(
                    "connection reset", exit_code=1, stderr="reset mid-read")
            return real_call(argv, cwd)

        state["armed"] = True
        with self.assertRaises((StorageError,
                               github_adapter.GitHubInvocationError)):
            delivery.merge_direct_pr(
                self.conn, evidence.id, idempotency_key="merge-lost",
                lease=self.lease(), run_gh=flaky,
                which=lambda name: "/usr/bin/gh")
        self.assertEqual(self.command_state(delivery.PR_MERGE_COMMAND_KIND),
                         "unknown")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")
        rows = self.conn.execute(
            "SELECT id FROM commands WHERE kind = ?",
            (delivery.PR_MERGE_COMMAND_KIND,)).fetchall()
        outcome = delivery.reconcile_pr_merge(
            self.conn, str(rows[0][0]), lease=self.lease(), actor="ops",
            reason="read back after lost receipt", run_gh=self.gh)
        self.assertEqual(outcome["resolution"], "adopted")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "done")
        self.assertEqual(len(self.events_of(delivery.PR_MERGE_EVIDENCE_KIND,
                                           task.id)), 1)
        # A second merge attempt under a new key refuses: never merge twice
        # (DONE is terminal, and settled merge evidence is immutable).
        with self.assertRaises((ConflictError, IllegalTransitionError)):
            self.delivered(evidence, key="merge-again")

    # -- mode gating -----------------------------------------------------------

    def test_non_direct_pr_modes_refuse(self):
        task, attempt, _alloc, _commit = self.ready_attempt(
            policy={"delivery": "local_only"})
        with self.assertRaises(delivery.DirectPrRefusal) as caught:
            self.prepared(attempt)
        self.assertEqual(caught.exception.kind, "mode")
        # delivery=direct_pr WITHOUT explicit repo/base config: persistable,
        # but never executable — the pipeline refuses with kind "mode".
        task2, attempt2, _a2, _c2 = self.ready_attempt(
            policy={"delivery": "direct_pr"}, tag="b")
        self.assertIsNone(domain.direct_pr_config(task2))
        with self.assertRaises(delivery.DirectPrRefusal) as caught2:
            self.prepared(attempt2)
        self.assertEqual(caught2.exception.kind, "mode")

    def test_task_mutation_after_direct_pr_done_reopens(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        evidence = self.prepared(attempt).pr_evidence
        self.approved(evidence)
        self.delivered(evidence)
        done = domain.get_task(self.conn, task.id)
        self.assertEqual(done.status, "done")
        reopened = domain.update_task(self.conn, task.id,
                                     expected_version=done.version,
                                     description="post-hoc edit")
        self.assertEqual(reopened.status, "needs_human")
        self.assertTrue(self.events_of("delivery.pr_merge_approval_invalidated",
                                      task.id))

    def test_approve_before_ready_refuses(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        self.gh.checks = PENDING_CHECKS
        result = self.prepared(attempt)
        self.assertEqual(result.outcome, "pending_checks")
        with self.assertRaises(IllegalTransitionError):
            self.approved(result.pr_evidence)

    def test_unknown_sync_command_requires_reconcile(self):
        task, attempt, _alloc, _commit = self.ready_attempt()
        self.prepared(attempt)
        with self.assertRaises((IllegalTransitionError, ConflictError)):
            delivery.sync_direct_pr(
                self.conn, task.id, idempotency_key="pr-1", lease=self.lease(),
                run_gh=self.gh, which=lambda name: "/usr/bin/gh")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
