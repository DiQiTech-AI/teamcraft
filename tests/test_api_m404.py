"""M4-04 API phase C — local-only Commit/Diff delivery routes acceptance tests.

These exercise the authenticated HTTP surface that integrates
:mod:`teamcraft.delivery` with the guarded M3-01 app:

* READ  (``teamcraft.api.query``): current commit/ship/approval evidence per
  Attempt/Task, ship evidence by id, and the COMPLETE bounded diff artifact —
  honest about missing evidence (nulls / 404) and stale evidence (durable
  ``freshness`` verdicts with a controlled signal vocabulary);
* WRITE (``teamcraft.api.command``): fenced idempotent Commit creation,
  receipt reconciliation, ship-evidence recording, and exact-commit/branch
  local-only accept/reject — adapters over Core's ``delivery`` functions and
  nothing else.

Acceptance lines pinned here (M0 review §9.4 M4-04, API tier):

* guards identical to M3-04: anonymous 401, missing CSRF 403, cross-origin
  write 403, every write requires ``Idempotency-Key`` (delivery routes
  tighten to Core's 200-char bound); uniform ``{code,message,details,
  request_id}`` envelope everywhere, no path/token echoes;
* success: commit → ship evidence → exact-echo accept moves the Task DONE;
  the diff artifact re-reads byte-for-byte through ``GET``;
* replay: same key + identical body replays the stored result with
  ``X-Idempotent-Replay: true`` and writes nothing new; once the Commit has
  settled, ANY later call for that Attempt (same key even with different
  content, or a fresh key) converges onto the settled evidence — never a
  second commit; a key that already owns evidence for a DIFFERENT Attempt
  collides 409 with nothing written;
* stale evidence: fresh submissions gated on ``expected_task_version`` 409
  ``VERSION_CONFLICT`` with zero residue (no Command row, no Git effect); a
  Task edit after ship invalidates the approval window (409 + freshness
  signals);
* failed verifier: a failing profile fails the Task and the commit route
  refuses fail-closed; a missing/absent-pass required profile refuses ship
  with 409 ``DELIVERY_REFUSED`` / ``refusal_kind: verifier``;
* retention/gating: ``report_only`` keeps its M3-04 approval home and refuses
  local commits (kind ``mode``); ``direct_pr`` stays gated out of ship
  evidence (kind ``mode``) until M4-05;
* injection: clients can NEVER express Git argv, repository/worktree paths,
  artifact roots, or shell fragments — request models are ``extra="forbid"``
  and only opaque ids/digests/bounded text are accepted;
* lease fencing: while a foreign holder owns the project lease every delivery
  write fails 409 ``LEADER_BUSY`` with nothing written;
* reconcile: an interrupted Commit Command (durable Command + landed Git
  commit, lost receipt) is ADOPTED exactly once against observed facts
  (native Session/execution-finished/claim facts required); a Command whose
  HEAD still equals the base expires and delivery may then proceed under a
  fresh key; re-reconciling an expired Command replays its resolution with
  ``X-Idempotent-Replay: true``, and a Command already owning accepted
  evidence refuses (409) — settled results are immutable;

Harness: real Git repos in temp dirs; setup drives Core directly (the "CLI
side"), delivery operations go through the API.  No Web UI here (M6-02 owns
the rich Diff/Verification pages).
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

try:  # the API needs the web stack; core tests must stay runnable without it
    from fastapi.testclient import TestClient

    from teamcraft.api import create_app
    from teamcraft.api.command import IDEMPOTENCY_HEADER, REPLAY_HEADER, CommandService
    from teamcraft.api.errors import (
        CONFLICT,
        CSRF_CHECK_FAILED,
        FORBIDDEN_ORIGIN,
        NOT_FOUND,
        SERVICE_UNAVAILABLE,
        UNAUTHENTICATED,
        VALIDATION_FAILED,
    )
    from teamcraft.query import QueryService

    _WEB_STACK_OK = True
    _WEB_STACK_REASON = ""
except ImportError as exc:  # pragma: no cover - depends on CI install order
    _WEB_STACK_OK = False
    _WEB_STACK_REASON = f"web dependencies unavailable: {exc}"

from teamcraft import (
    briefs,
    commands as commands_core,
    delivery,
    domain,
    fresh_start,
    leader,
    storage,
    verification,
    worker_reports,
    worktrees,
)

requires_web = unittest.skipUnless(_WEB_STACK_OK, _WEB_STACK_REASON)

ERROR_KEYS = {"code", "message", "details", "request_id"}
DB_FRAGMENTS = ("teamcraft.db", "/.teamcraft/")

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
    # REPO-LOCAL identity: delivery commits run with the ambient environment.
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


@requires_web
class M404ApiFixture(unittest.TestCase):
    """Temp Git repo + initialized runtime DB + guarded app with services."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="tc-api-m404-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.base), ignore_errors=True)
        self.repo = init_repo(self.base)
        fresh_start.initialize_project_root(str(self.base))
        self.db_path = os.path.join(str(self.base), ".teamcraft", "teamcraft.db")
        self.command_service = CommandService(self.db_path)
        self.query_service = QueryService(self.db_path)
        self.app = create_app(
            query_service=self.query_service,
            command_service=self.command_service,
        )
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()
        conn = self.core()
        try:
            self.project_id = domain.create_project(
                conn, "demo", str(self.repo)).id
        finally:
            conn.close()

    # -- core-side helpers (the "CLI" side of CLI/API parity) ---------------

    def core(self):
        return storage.connect(self.db_path)

    def task_version(self, task_id: str) -> int:
        conn = self.core()
        try:
            return domain.get_task(conn, task_id).version
        finally:
            conn.close()

    def run_verifier(self, attempt_id: str, *, profile: str = "test",
                    outcome: str = "pass") -> str:
        """Configure + run one worktree verifier under a short harness lease.

        The lease is released afterwards so the API's own short lease can be
        acquired by the delivery routes under test.
        """

        argv = ([sys.executable, "-c", "print('ok')"] if outcome == "pass"
                else [sys.executable, "-c", "raise SystemExit(3)"])
        conn = self.core()
        try:
            lease = leader.acquire_or_renew(
                conn, leader.project_scope(self.project_id), "harness",
                ttl_seconds=3700)
            verification.configure_verifier_profile(
                conn, self.project_id, profile=profile, argv=argv,
                timeout_seconds=10, actor="human",
                idempotency_key=f"cfg-{profile}-{outcome}-{attempt_id}",
                lease=lease, enabled=True)
            result = verification.run_worktree_verifier(
                conn, attempt_id, profile=profile,
                idempotency_key=f"run-{profile}-{outcome}-{attempt_id}",
                lease=lease)
            leader.release(conn, leader.project_scope(self.project_id),
                          "harness")
            return result.status
        finally:
            conn.close()

    def ready_ship(self, *, title: str = "deliver", policy: str = "local_only",
                  verifier: str | None = "pass", feature: bool = True,
                  kind: str = "ship") -> dict:
        """Drive one Attempt to the exact pre-commit state.

        Task -> READY -> attempt -> worktree (+owned change) -> brief ->
        final WorkerReport -> VERIFYING; delivery policy lands through the
        API's own M3-04 policy route (CEO-side parity); the M4-03 verifier
        runs (or fails, or is skipped) core-side under a released lease.
        """

        conn = self.core()
        try:
            task = domain.create_task(
                conn, self.project_id, title, kind=kind,
                ownership_json=json.dumps(["src/**"]))
            task = domain.apply_task_event(
                conn, task.id, "dependencies_satisfied",
                expected_version=task.version)
            task_id = task.id
            ready_version = task.version
        finally:
            conn.close()
        # CEO-side policy write through the same guarded API (parity).  It
        # lands BEFORE dispatch so the Brief pins the post-policy evidence
        # version (Core pins dispatch-time facts exactly once).
        policy_response = self.post(
            f"/api/v1/tasks/{task_id}/policy",
            {"expected_version": ready_version, "delivery": policy},
            key=f"pol-{task_id}")
        self.assertEqual(policy_response.status_code, 200, policy_response.text)
        conn = self.core()
        try:
            attempt = domain.create_attempt(
                conn, task_id,
                expected_version=domain.get_task(conn, task_id).version)
            # The dispatched worker's native session fact (runtime parity:
            # reconcile adoption requires it to tie the crash-window commit to
            # this Attempt's worker run).
            domain.register_session(
                conn, attempt.id, "opencode",
                native_session_id=f"native-{attempt.id}", pane_id="pane-m404",
                state="working")
            domain.transition_attempt(conn, attempt.id, "start")
            domain.transition_attempt(conn, attempt.id, "activated")
            allocation, _ = worktrees.allocate_attempt_worktree(
                conn, task_id=task_id, attempt_id=attempt.id,
                repo_root=str(self.repo))
            if feature:
                (Path(allocation.worktree_path) / "src" / "feature.py").write_text(
                    "feature\n", encoding="utf-8")
            brief = briefs.create_attempt_brief(
                conn, attempt.id, content="M4-04 API delivery brief")
            worker_reports.record_worker_report(conn, {
                "schema_version": 1, "task_id": task_id,
                "attempt_id": attempt.id, "task_version": brief.task_version,
                "brief_digest": brief.brief_digest, "report_kind": "final",
                "outcome": "completed", "milestone_id": "final",
                "summary": "implementation complete", "artifact_digest": None,
                "usage": {"total_tokens": None},
            })
            domain.apply_task_event(
                conn, task_id, "worker_reported_done",
                expected_version=domain.get_task(conn, task_id).version)
        finally:
            conn.close()
        if verifier is not None:
            self.assertEqual(self.run_verifier(attempt.id, outcome=verifier),
                           verifier)
        return {
            "task_id": task_id,
            "attempt_id": attempt.id,
            "allocation": allocation,
            "worktree": Path(allocation.worktree_path),
            "branch": allocation.branch,
            "base_sha": allocation.base_sha,
        }

    def commits_above(self, ctx: dict, ref: str | None = None) -> int:
        return int(git("rev-list", "--count",
                       f"{ctx['base_sha']}..{ref or 'HEAD'}",
                       cwd=ctx["worktree"]))

    def worktree_porcelain(self, ctx: dict) -> str:
        return git("status", "--porcelain", cwd=ctx["worktree"])

    def command_rows(self, kind: str, attempt_id: str | None = None) -> list:
        conn = self.core()
        try:
            if attempt_id is None:
                return list(conn.execute(
                    "SELECT id, state FROM commands WHERE kind = ?",
                    (kind,)).fetchall())
            return list(conn.execute(
                "SELECT id, state FROM commands WHERE kind = ?"
                " AND json_extract(payload_json, '$.attempt_id') = ?",
                (kind, attempt_id)).fetchall())
        finally:
            conn.close()

    def event_count(self, kind: str, task_id: str) -> int:
        conn = self.core()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = ? AND task_id = ?",
                (kind, task_id)).fetchone()[0])
        finally:
            conn.close()

    # -- HTTP helpers ----------------------------------------------------------

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def write_headers(self, key: str | None = "k-default") -> dict[str, str]:
        headers = self.auth()
        headers["X-CSRF-Token"] = self.token
        if key is not None:
            headers[IDEMPOTENCY_HEADER] = key
        return headers

    def post(self, path: str, body: dict, key: str | None = "k-default",
             **extra_headers) -> object:
        return self.client.post(
            path, headers={**self.write_headers(key), **extra_headers}, json=body)

    def patch(self, path: str, body: dict, key: str | None = "k-default") -> object:
        return self.client.patch(path, headers=self.write_headers(key), json=body)

    def get(self, path: str) -> object:
        return self.client.get(path, headers=self.auth())

    # -- assertions -------------------------------------------------------------

    def assert_uniform_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body.keys()), ERROR_KEYS)
        self.assertEqual(body["code"], code)
        self.assertTrue(body["request_id"])
        self.assertIn("x-request-id", response.headers)
        return body

    def assert_ok(self, response, status: int = 200) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def assert_no_leaks(self, response) -> None:
        payload = response.json() if hasattr(response, "json") else response
        text = json.dumps(payload, ensure_ascii=False)
        fragments = list(DB_FRAGMENTS) + [str(self.base), str(self.repo)]
        for fragment in fragments:
            self.assertNotIn(fragment, text)

    # -- delivery shorthand ---------------------------------------------------------

    def api_commit(self, ctx: dict, key: str = "commit-1",
                  message: str | None = None) -> object:
        body: dict = {"expected_task_version": self.task_version(ctx["task_id"])}
        if message is not None:
            body["message"] = message
        return self.post(f"/api/v1/attempts/{ctx['attempt_id']}/delivery/commit",
                        body, key)

    def api_ship(self, ctx: dict, key: str = "ship-1") -> object:
        return self.post(
            f"/api/v1/attempts/{ctx['attempt_id']}/delivery/ship-evidence",
            {"expected_task_version": self.task_version(ctx["task_id"])}, key)

    def api_approve(self, ship_id: str, ctx: dict, *, decision: str = "accept",
                   commit_sha: str | None = None, branch: str | None = "AUTO",
                   key: str = "approve-1", **extra) -> object:
        body: dict = {
            "decision": decision,
            "actor": extra.get("actor", "ceo"),
            "reason": extra.get("reason", "reviewed diff via API"),
            "expected_task_version": self.task_version(ctx["task_id"]),
        }
        if commit_sha is not None:
            body["commit_sha"] = commit_sha
        if branch == "AUTO":
            body["branch"] = ctx["branch"]
        elif branch is not None:
            body["branch"] = branch
        return self.post(f"/api/v1/ship-evidence/{ship_id}/approval", body, key)

    def committed(self, **kw) -> tuple[dict, dict]:
        ctx = self.ready_ship(**kw)
        response = self.api_commit(ctx, key=kw.get("commit_key", "commit-1"))
        body = self.assert_ok(response, 201)
        return ctx, body["commit"]

    def shipped(self, **kw) -> tuple[dict, dict, dict]:
        ctx, commit = self.committed(**kw)
        response = self.api_ship(ctx, key=kw.get("ship_key", "ship-1"))
        return ctx, commit, self.assert_ok(response, 201)["ship"]


# ---------------------------------------------------------------------------
# Guard inheritance (identical rules to M3-04 on the new routes)
# ---------------------------------------------------------------------------


@requires_web
class DeliveryGuardTests(M404ApiFixture):
    def test_all_new_routes_deny_anonymous(self) -> None:
        routes = [
            ("get", "/api/v1/tasks/t_missing/delivery", None),
            ("get", "/api/v1/attempts/a_missing/delivery", None),
            ("get", "/api/v1/ship-evidence/se_missing", None),
            ("get", "/api/v1/ship-evidence/se_missing/diff", None),
            ("post", "/api/v1/attempts/a_missing/delivery/commit",
             {"expected_task_version": 0}),
            ("post", "/api/v1/attempts/a_missing/delivery/ship-evidence",
             {"expected_task_version": 0}),
            ("post", "/api/v1/commands/c_missing/delivery/reconcile",
             {"actor": "ops", "reason": "r"}),
            ("post", "/api/v1/ship-evidence/se_missing/approval",
             {"decision": "reject", "actor": "ceo", "reason": "r",
              "expected_task_version": 0}),
        ]
        for method, path, body in routes:
            response = self.client.request(method, path, json=body)
            self.assert_uniform_error(response, 401, UNAUTHENTICATED)

    def test_write_without_csrf_header_is_403(self) -> None:
        response = self.client.post(
            "/api/v1/attempts/a_missing/delivery/commit",
            headers={**self.auth(), IDEMPOTENCY_HEADER: "k"},
            json={"expected_task_version": 0},
        )
        self.assert_uniform_error(response, 403, CSRF_CHECK_FAILED)

    def test_cross_origin_write_is_403(self) -> None:
        response = self.post(
            "/api/v1/attempts/a_missing/delivery/commit",
            {"expected_task_version": 0}, "k",
            Origin="http://evil.example",
        )
        self.assert_uniform_error(response, 403, FORBIDDEN_ORIGIN)

    def test_every_delivery_write_requires_idempotency_key(self) -> None:
        routes = [
            ("/api/v1/attempts/a_missing/delivery/commit",
             {"expected_task_version": 0}),
            ("/api/v1/attempts/a_missing/delivery/ship-evidence",
             {"expected_task_version": 0}),
            ("/api/v1/commands/c_missing/delivery/reconcile",
             {"actor": "ops", "reason": "r"}),
            ("/api/v1/ship-evidence/se_missing/approval",
             {"decision": "reject", "actor": "ceo", "reason": "r",
              "expected_task_version": 0}),
        ]
        for path, body in routes:
            response = self.client.post(
                path, headers=self.write_headers(None), json=body)
            out = self.assert_uniform_error(response, 422, VALIDATION_FAILED)
            self.assertEqual(out["details"]["field"], IDEMPOTENCY_HEADER)

    def test_delivery_key_beyond_core_bound_is_refused_at_edge(self) -> None:
        response = self.post(
            "/api/v1/attempts/a_missing/delivery/commit",
            {"expected_task_version": 0}, "k" * 201)
        out = self.assert_uniform_error(response, 422, VALIDATION_FAILED)
        self.assertEqual(out["details"]["field"], IDEMPOTENCY_HEADER)

    def test_clients_cannot_smuggle_git_argv_paths_or_shell(self) -> None:
        ctx = self.ready_ship()
        injections = [
            ("commit", {"expected_task_version": 1, "argv": ["push", "origin"]}),
            ("commit", {"expected_task_version": 1, "repo_root": "/tmp"}),
            ("commit", {"expected_task_version": 1,
                       "worktree_path": str(ctx["worktree"])}),
            ("commit", {"expected_task_version": 1,
                       "artifact_root": str(self.base / "evil")}),
            ("commit", {"expected_task_version": 1, "message": "ok",
                       "run_git": "bash -c 'rm -rf /'"}),
        ]
        for index, (_name, body) in enumerate(injections):
            response = self.post(
                f"/api/v1/attempts/{ctx['attempt_id']}/delivery/commit",
                body, f"k-inject-{index}")
            self.assert_uniform_error(response, 422, VALIDATION_FAILED)
        approval_injection = self.post(
            "/api/v1/ship-evidence/se_missing/approval",
            {"decision": "reject", "actor": "ceo", "reason": "r",
             "expected_task_version": 0, "argv": ["merge"]},
            "k-inject-approval")
        self.assert_uniform_error(approval_injection, 422, VALIDATION_FAILED)
        # nothing ran: no commit command, workspace untouched
        self.assertEqual(
            self.command_rows(delivery.COMMIT_COMMAND_KIND, ctx["attempt_id"]), [])
        self.assertIn("feature.py", self.worktree_porcelain(ctx))


# ---------------------------------------------------------------------------
# Full local-only success chain through the API
# ---------------------------------------------------------------------------


@requires_web
class DeliverySuccessTests(M404ApiFixture):
    def test_full_chain_via_api(self) -> None:
        ctx = self.ready_ship()

        # Honest reads BEFORE anything exists: nulls, not lies.
        early = self.assert_ok(
            self.get(f"/api/v1/attempts/{ctx['attempt_id']}/delivery"))
        self.assertIsNone(early["commit"])
        self.assertIsNone(early["ship"])
        self.assertIsNone(early["approval"])
        self.assertEqual(early["task"]["status"], "verifying")
        self.assertEqual(early["task"]["delivery_mode"], "local_only")

        # 1) Commit.
        commit_response = self.api_commit(ctx, message="feat: deliver")
        created = self.assert_ok(commit_response, 201)
        self.assertEqual(created["execution"], "applied")
        self.assertFalse(created["replayed"])
        self.assertEqual(created["command"]["state"], "confirmed")
        self.assertEqual(created["command"]["kind"], delivery.COMMIT_COMMAND_KIND)
        commit = created["commit"]
        self.assertEqual(commit["source"], "created")
        self.assertEqual(commit["commit_sha"],
                         git("rev-parse", "HEAD", cwd=ctx["worktree"]))
        self.assertEqual(commit["file_count"], 1)
        self.assertEqual(self.commits_above(ctx), 1)
        self.assert_no_leaks(commit_response)

        # 2) Ship evidence.
        ship_response = self.api_ship(ctx)
        shipped = self.assert_ok(ship_response, 201)
        self.assertFalse(shipped["replayed"])
        ship = shipped["ship"]
        self.assertEqual(ship["commit_sha"], commit["commit_sha"])
        self.assertEqual(ship["diff_digest"], commit["diff_digest"])
        self.assertEqual(ship["delivery_mode"], "local_only")
        self.assertEqual(ship["proposed_action"], "accept_local_commit")
        self.assertEqual(ship["commit_evidence_id"], commit["event_id"])
        self.assertEqual([v["profile"] for v in ship["verifiers"]], ["test"])
        self.assertEqual(ship["verifiers"][0]["status"], "pass")
        self.assert_no_leaks(ship_response)

        # 3) Reads: aggregate, by-id, COMPLETE diff artifact.
        aggregate = self.assert_ok(
            self.get(f"/api/v1/attempts/{ctx['attempt_id']}/delivery"))
        self.assertIsNotNone(aggregate["commit"])
        self.assertIsNotNone(aggregate["ship"])
        self.assertIsNone(aggregate["approval"])
        self.assertTrue(aggregate["freshness"]["commit"]["ok"],
                       aggregate["freshness"]["commit"])
        self.assertTrue(aggregate["freshness"]["ship"]["ok"])

        task_view = self.assert_ok(
            self.get(f"/api/v1/tasks/{ctx['task_id']}/delivery?live_git=true"))
        self.assertEqual(task_view["total"], 1)
        self.assertTrue(task_view["items"][0]["freshness"]["ship"]["ok"],
                       task_view["items"][0]["freshness"]["ship"])

        by_id = self.assert_ok(self.get(f"/api/v1/ship-evidence/{ship['id']}"))
        self.assertEqual(by_id["ship_evidence"]["evidence_digest"],
                         ship["evidence_digest"])

        diff = self.assert_ok(
            self.get(f"/api/v1/ship-evidence/{ship['id']}/diff"))
        self.assertEqual(diff["diff_digest"], commit["diff_digest"])
        self.assertEqual(diff["diff_bytes"], ship["diff_bytes"])
        self.assertIn("+feature", diff["diff"])
        self.assertEqual(diff["proposed_action"], "accept_local_commit")
        self.assert_no_leaks(diff)

        # 4) Exact-echo acceptance moves the Task DONE (and only that).
        approval_response = self.api_approve(
            ship["id"], ctx, commit_sha=commit["commit_sha"])
        approval_body = self.assert_ok(approval_response)
        self.assertEqual(approval_body["approval"]["decision"], "accept")
        self.assertEqual(approval_body["approval"]["task_status_after"], "done")
        self.assertEqual(approval_body["task"]["status"], "done")
        self.assert_no_leaks(approval_response)

        conn = self.core()
        try:
            done = domain.get_task(conn, ctx["task_id"])
            self.assertEqual(done.status, "done")
            self.assertEqual(done.version, self.task_version(ctx["task_id"]))
        finally:
            conn.close()
        self.assertEqual(self.event_count("delivery.approval_accepted",
                                         ctx["task_id"]), 1)

        # Post-DONE view: the settled approval carries the currency verdict.
        after = self.assert_ok(
            self.get(f"/api/v1/attempts/{ctx['attempt_id']}/delivery"))
        self.assertEqual(after["approval"]["decision"], "accept")
        self.assertTrue(after["freshness"]["approval"]["ok"],
                       after["freshness"]["approval"])
        self.assertNotIn("ship", after["freshness"])

    def test_second_attempt_reports_per_attempt_items(self) -> None:
        ctx, _commit = self.committed()
        view = self.assert_ok(self.get(f"/api/v1/tasks/{ctx['task_id']}/delivery"))
        self.assertEqual(view["total"], 1)
        self.assertEqual(view["items"][0]["attempt"]["id"], ctx["attempt_id"])


# ---------------------------------------------------------------------------
# Idempotency / replay contract
# ---------------------------------------------------------------------------


@requires_web
class DeliveryReplayTests(M404ApiFixture):
    def test_commit_replay_same_key_and_new_key_convergence(self) -> None:
        ctx = self.ready_ship()
        first = self.api_commit(ctx, key="k-c", message="one shot")
        created = self.assert_ok(first, 201)
        self.assertNotIn(REPLAY_HEADER, first.headers)

        retry = self.api_commit(ctx, key="k-c", message="one shot")
        replay = self.assert_ok(retry, 201)
        self.assertTrue(replay["replayed"])
        self.assertEqual(retry.headers[REPLAY_HEADER], "true")
        self.assertEqual(replay["commit"]["event_id"], created["commit"]["event_id"])
        self.assertEqual(replay["command"]["command_id"],
                         created["command"]["command_id"])
        self.assertEqual(len(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                              ctx["attempt_id"])), 1)

        # A NEW key after settlement converges onto the settled evidence —
        # never a second commit.
        fresh_key = self.api_commit(ctx, key="k-c2", message="one shot")
        converged = self.assert_ok(fresh_key, 201)
        self.assertTrue(converged["replayed"])
        self.assertEqual(converged["commit"]["event_id"], created["commit"]["event_id"])
        self.assertEqual(self.commits_above(ctx), 1)
        self.assertEqual(len(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                              ctx["attempt_id"])), 1)

    def test_settled_commit_converges_even_for_same_key_different_content(self) -> None:
        # Core's delivery rule is STRONGER than the generic M3-04 refusal
        # once the effect has landed: a settled Commit is returned (never
        # redone) on any later call for the same Attempt — the key already
        # produced its durable effect and refusal could not undo it.  The
        # response stays honest via replayed=true + the identical ids.
        ctx = self.ready_ship()
        first = self.assert_ok(self.api_commit(ctx, key="k-c",
                                              message="original"), 201)
        converge = self.assert_ok(self.api_commit(
            ctx, key="k-c", message="tampered retry"), 201)
        self.assertTrue(converge["replayed"])
        self.assertEqual(converge["commit"]["event_id"],
                         first["commit"]["event_id"])
        self.assertEqual(converge["command"]["command_id"],
                         first["command"]["command_id"])
        self.assertEqual(self.commits_above(ctx), 1)
        self.assertEqual(len(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                              ctx["attempt_id"])), 1)

    def test_same_key_bound_to_other_attempt_collides(self) -> None:
        # The genuine idempotency-collision gate: a key that already owns
        # commit evidence for ANOTHER Attempt must never be reused to move
        # this one (409, nothing written).
        ctx_a = self.ready_ship(title="attempt-a")
        self.assert_ok(self.api_commit(ctx_a, key="k-shared"), 201)
        ctx_b = self.ready_ship(title="attempt-b")
        out = self.assert_uniform_error(
            self.api_commit(ctx_b, key="k-shared"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "CONFLICT")
        self.assertEqual(self.commits_above(ctx_b), 0)
        self.assertEqual(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                          ctx_b["attempt_id"]), [])

    def test_ship_and_approval_replays(self) -> None:
        ctx, _commit, ship = self.shipped()
        again = self.api_ship(ctx, key="ship-1")
        replay = self.assert_ok(again, 201)
        self.assertTrue(replay["replayed"])
        self.assertEqual(again.headers[REPLAY_HEADER], "true")
        self.assertEqual(replay["ship"]["id"], ship["id"])
        self.assertEqual(len(self.command_rows(delivery.SHIP_COMMAND_KIND,
                                              ctx["attempt_id"])), 1)

        body = {"decision": "reject", "actor": "ceo", "reason": "not this",
                "expected_task_version": self.task_version(ctx["task_id"])}
        first = self.post(f"/api/v1/ship-evidence/{ship['id']}/approval", body,
                         "k-rej")
        decided = self.assert_ok(first)
        self.assertEqual(decided["approval"]["decision"], "reject")
        retry = self.post(f"/api/v1/ship-evidence/{ship['id']}/approval", body,
                         "k-rej")
        replayed = self.assert_ok(retry)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(retry.headers[REPLAY_HEADER], "true")
        self.assertEqual(replayed["approval"]["id"], decided["approval"]["id"])
        # An identical decision under a NEW key also converges.
        convergence = self.assert_ok(self.post(
            f"/api/v1/ship-evidence/{ship['id']}/approval", body, "k-rej-2"))
        self.assertEqual(convergence["approval"]["id"], decided["approval"]["id"])


# ---------------------------------------------------------------------------
# Stale/missing evidence honesty
# ---------------------------------------------------------------------------


@requires_web
class DeliveryStaleEvidenceTests(M404ApiFixture):
    def test_stale_expected_version_refuses_with_zero_residue(self) -> None:
        ctx = self.ready_ship()
        response = self.post(
            f"/api/v1/attempts/{ctx['attempt_id']}/delivery/commit",
            {"expected_task_version": self.task_version(ctx["task_id"]) + 5},
            "k-stale")
        out = self.assert_uniform_error(response, 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "VERSION_CONFLICT")
        self.assertEqual(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                          ctx["attempt_id"]), [])
        self.assertEqual(self.commits_above(ctx), 0)
        self.assertIn("feature.py", self.worktree_porcelain(ctx))
        # The same key remains usable after the refusal wrote nothing.
        self.assert_ok(self.api_commit(ctx, key="k-stale"), 201)
        self.assertEqual(self.commits_above(ctx), 1)

    def test_ship_without_commit_is_honest_404(self) -> None:
        ctx = self.ready_ship()
        self.assert_uniform_error(self.api_ship(ctx), 404, NOT_FOUND)
        view = self.assert_ok(
            self.get(f"/api/v1/attempts/{ctx['attempt_id']}/delivery"))
        self.assertIsNone(view["commit"])
        self.assertIsNone(view["ship"])

    def test_task_edit_after_ship_invalidates_approval_window(self) -> None:
        ctx, commit, ship = self.shipped()
        edited = self.assert_ok(self.patch(
            f"/api/v1/tasks/{ctx['task_id']}",
            {"expected_version": self.task_version(ctx["task_id"]),
             "title": "edited mid-approval"}, "k-edit"))
        self.assertEqual(edited["task"]["version"], ship["task_version"] + 1)

        # A client still holding the OLD evidence version is refused precisely.
        stale = self.client.post(
            f"/api/v1/ship-evidence/{ship['id']}/approval",
            headers=self.write_headers("k-approve-stale"),
            json={"decision": "accept", "actor": "ceo", "reason": "stale",
                  "expected_task_version": ship["task_version"],
                  "commit_sha": commit["commit_sha"], "branch": ctx["branch"]})
        out = self.assert_uniform_error(stale, 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "VERSION_CONFLICT")

        # Even with a CURRENT version pin, Core refuses the drifted chain.
        drifted = self.api_approve(ship["id"], ctx,
                                  commit_sha=commit["commit_sha"],
                                  key="k-approve-drift")
        self.assert_uniform_error(drifted, 409, CONFLICT)

        # The read surface says so too, with controlled signals only.
        view = self.assert_ok(
            self.get(f"/api/v1/attempts/{ctx['attempt_id']}/delivery"))
        self.assertFalse(view["freshness"]["ship"]["ok"])
        self.assertIn("version", view["freshness"]["ship"]["signals"])
        self.assertIsNone(view["approval"])

    def test_missing_ids_are_404(self) -> None:
        self.assert_uniform_error(
            self.get("/api/v1/attempts/no-such-attempt/delivery"),
            404, NOT_FOUND)
        self.assert_uniform_error(
            self.get("/api/v1/ship-evidence/no-such-evidence"), 404, NOT_FOUND)
        self.assert_uniform_error(
            self.get("/api/v1/ship-evidence/no-such-evidence/diff"),
            404, NOT_FOUND)

    def test_diff_artifact_missing_or_tampered_surfaces_honestly(self) -> None:
        ctx, _commit, ship = self.shipped()
        artifact = self.repo / ".teamcraft" / "deliveries" / ship["diff_relpath"]
        self.assertTrue(artifact.is_file())
        original = artifact.read_bytes()
        self.assertEqual(len(original), ship["diff_bytes"])

        os.remove(artifact)
        self.assert_uniform_error(
            self.get(f"/api/v1/ship-evidence/{ship['id']}/diff"),
            404, NOT_FOUND)

        artifact.write_bytes(original + b"# tampered\n")
        self.assert_uniform_error(
            self.get(f"/api/v1/ship-evidence/{ship['id']}/diff"),
            503, SERVICE_UNAVAILABLE)

        artifact.write_bytes(original)
        restored = self.assert_ok(
            self.get(f"/api/v1/ship-evidence/{ship['id']}/diff"))
        self.assertEqual(restored["diff_digest"], ship["diff_digest"])


# ---------------------------------------------------------------------------
# Verifier evidence gates (failed / missing verifier can never deliver)
# ---------------------------------------------------------------------------


@requires_web
class DeliveryVerifierGateTests(M404ApiFixture):
    def test_failed_verifier_blocks_commit_fail_closed(self) -> None:
        ctx = self.ready_ship(verifier="fail")
        # The failing profile moved the Task out of VERIFYING (needs_human).
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, ctx["task_id"]).status,
                             "needs_human")
        finally:
            conn.close()
        response = self.api_commit(ctx, key="k-fail")
        # Core's bound-facts gate refuses non-VERIFYING/stale-evidence commit
        # requests as a validation failure (uniform 422, like every other
        # core ValidationError on this surface) — fail-closed, zero effects.
        self.assert_uniform_error(response, 422, VALIDATION_FAILED)
        self.assertEqual(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                          ctx["attempt_id"]), [])
        self.assertEqual(self.commits_above(ctx), 0)
        self.assertIn("feature.py", self.worktree_porcelain(ctx))

    def test_missing_required_profile_can_never_ship(self) -> None:
        ctx = self.ready_ship(verifier=None)
        # Commit itself does not depend on the verifier — ship does.
        self.assert_ok(self.api_commit(ctx, key="k-c"), 201)
        out = self.assert_uniform_error(self.api_ship(ctx, key="k-s"),
                                       409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "DELIVERY_REFUSED")
        self.assertEqual(out["details"]["refusal_kind"], "verifier")
        self.assertEqual(self.command_rows(delivery.SHIP_COMMAND_KIND,
                                          ctx["attempt_id"]), [])
        view = self.assert_ok(
            self.get(f"/api/v1/attempts/{ctx['attempt_id']}/delivery"))
        self.assertIsNotNone(view["commit"])
        self.assertIsNone(view["ship"])


# ---------------------------------------------------------------------------
# Mode retention + direct-PR gating (M4-05 not yet implemented)
# ---------------------------------------------------------------------------


@requires_web
class DeliveryModeGatingTests(M404ApiFixture):
    def test_report_only_task_never_commits_or_ships_locally(self) -> None:
        ctx = self.ready_ship(title="report-only", policy="report_only")
        commit = self.api_commit(ctx, key="k-ro-c")
        out = self.assert_uniform_error(commit, 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "DELIVERY_REFUSED")
        self.assertEqual(out["details"]["refusal_kind"], "mode")
        out = self.assert_uniform_error(self.api_ship(ctx, key="k-ro-s"),
                                       409, CONFLICT)
        self.assertEqual(out["details"]["refusal_kind"], "mode")
        self.assertEqual(self.commits_above(ctx), 0)
        # Report-only approval RETAINS its M3-04 home: the verification
        # approvals route still exists and enforces the same guards.
        scout_guard = self.client.post(
            "/api/v1/verifications/v_missing/approvals",
            json={"decision": "accept", "actor": "a", "reason": "r",
                  "expected_task_version": 0})
        self.assert_uniform_error(scout_guard, 401, UNAUTHENTICATED)

    def test_direct_pr_remains_gated_until_m405(self) -> None:
        ctx = self.ready_ship(title="direct-pr", policy="direct_pr")
        out = self.assert_uniform_error(self.api_ship(ctx, key="k-dp-s"),
                                       409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "DELIVERY_REFUSED")
        self.assertEqual(out["details"]["refusal_kind"], "mode")
        # No push-capable surface exists on this API (M4-05 owns direct PR).
        self.assertEqual(
            self.command_rows(delivery.SHIP_COMMAND_KIND, ctx["attempt_id"]), [])


# ---------------------------------------------------------------------------
# Exact-commit/branch acceptance rules
# ---------------------------------------------------------------------------


@requires_web
class DeliveryApprovalEchoTests(M404ApiFixture):
    def test_accept_must_echo_exact_commit_and_branch(self) -> None:
        ctx, commit, ship = self.shipped()

        wrong_sha = self.api_approve(ship["id"], ctx,
                                    commit_sha="f" * 40, key="k-wrong-sha")
        self.assert_uniform_error(wrong_sha, 409, CONFLICT)

        wrong_branch = self.api_approve(ship["id"], ctx,
                                       commit_sha=commit["commit_sha"],
                                       branch="main", key="k-wrong-branch")
        self.assert_uniform_error(wrong_branch, 409, CONFLICT)

        malformed = self.api_approve(ship["id"], ctx,
                                    commit_sha="XYZNOTSHA", key="k-malformed")
        self.assert_uniform_error(malformed, 422, VALIDATION_FAILED)

        missing_echo = self.post(
            f"/api/v1/ship-evidence/{ship['id']}/approval",
            {"decision": "accept", "actor": "ceo", "reason": "no echo",
             "expected_task_version": self.task_version(ctx["task_id"])},
            "k-no-echo")
        self.assert_uniform_error(missing_echo, 422, VALIDATION_FAILED)

        # Nothing settled on any refusal.
        self.assertEqual(self.task_version(ctx["task_id"]), ship["task_version"])
        view = self.assert_ok(
            self.get(f"/api/v1/attempts/{ctx['attempt_id']}/delivery"))
        self.assertIsNone(view["approval"])

        accepted = self.assert_ok(self.api_approve(
            ship["id"], ctx, commit_sha=commit["commit_sha"]))
        self.assertEqual(accepted["task"]["status"], "done")

    def test_reject_leaves_verifying_and_closes_evidence_version(self) -> None:
        ctx, _commit, ship = self.shipped()
        rejected = self.assert_ok(self.api_approve(
            ship["id"], ctx, decision="reject", key="k-rej",
            reason="diff not acceptable"))
        self.assertEqual(rejected["approval"]["task_status_after"], "verifying")
        self.assertEqual(rejected["task"]["status"], "verifying")
        self.assertEqual(self.event_count("delivery.approval_rejected",
                                         ctx["task_id"]), 1)

        # A settled decision is immutable: a late accept on the SAME evidence
        # dies (fixes require a new bounded Attempt).
        late = self.api_approve(ship["id"], ctx, decision="accept",
                               commit_sha=ship["commit_sha"],
                               branch=ship["branch"],
                               key="k-late-accept")
        self.assert_uniform_error(late, 409, CONFLICT)
        self.assertEqual(self.task_version(ctx["task_id"]), ship["task_version"])


# ---------------------------------------------------------------------------
# Lease fencing
# ---------------------------------------------------------------------------


@requires_web
class DeliveryLeaseFencingTests(M404ApiFixture):
    def test_foreign_live_lease_blocks_every_delivery_write(self) -> None:
        ctx = self.ready_ship()
        conn = self.core()
        try:
            leader.acquire_or_renew(
                conn, leader.project_scope(self.project_id), "runtime-holder",
                ttl_seconds=3700)
        finally:
            conn.close()
        try:
            commit = self.api_commit(ctx, key="k-fenced")
            out = self.assert_uniform_error(commit, 409, CONFLICT)
            self.assertEqual(out["details"]["reason_code"], "LEADER_BUSY")
            self.assertEqual(out["details"]["holder_id"], "runtime-holder")
            self.assertEqual(self.command_rows(delivery.COMMIT_COMMAND_KIND,
                                              ctx["attempt_id"]), [])
            self.assertEqual(self.commits_above(ctx), 0)
        finally:
            conn = self.core()
            try:
                leader.release(conn, leader.project_scope(self.project_id),
                             "runtime-holder")
            finally:
                conn.close()
        # After the foreign holder releases, the same key proceeds normally.
        self.assert_ok(self.api_commit(ctx, key="k-fenced"), 201)
        self.assertEqual(self.commits_above(ctx), 1)


# ---------------------------------------------------------------------------
# Lost-receipt reconciliation through the API
# ---------------------------------------------------------------------------


@requires_web
class DeliveryReconcileTests(M404ApiFixture):
    def test_lost_commit_receipt_adopts_via_api_never_a_second(self) -> None:
        ctx = self.ready_ship()
        # Simulate the crash window: durable Command claimed, the Git effect
        # landed, the receipt was lost.
        conn = self.core()
        try:
            command = commands_core.submit_command(
                conn, idempotency_key="half-open",
                kind=delivery.COMMIT_COMMAND_KIND,
                payload_json=json.dumps({
                    "project_id": self.project_id, "task_id": ctx["task_id"],
                    "attempt_id": ctx["attempt_id"]}),
                evidence_json=json.dumps(
                    {"project_id": self.project_id}),
            ).command
            commands_core.claim_command(conn, command.id)
        finally:
            conn.close()
        git("add", "-A", cwd=ctx["worktree"])
        git("commit", "-q", "-m", "crash-window commit", cwd=ctx["worktree"])

        response = self.post(
            f"/api/v1/commands/{command.id}/delivery/reconcile",
            {"actor": "ops", "reason": "host restarted between commit and "
                                       "receipt"}, "k-rec")
        body = self.assert_ok(response)
        self.assertEqual(body["resolution"], "adopted")
        self.assertFalse(body["replayed"])
        self.assertEqual(body["command"]["state"], "confirmed")
        self.assertEqual(body["commit"]["source"], "reconciled")
        self.assertEqual(body["commit"]["commit_sha"],
                         git("rev-parse", "HEAD", cwd=ctx["worktree"]))
        self.assertEqual(self.commits_above(ctx), 1)
        self.assert_no_leaks(response)

        # An accepted result is immutable: reconciling the Command that owns
        # durable evidence is refused (nothing can be reconciled away), and
        # the worktree keeps exactly the one commit.
        again = self.post(
            f"/api/v1/commands/{command.id}/delivery/reconcile",
            {"actor": "ops", "reason": "double check"}, "k-rec-2")
        self.assert_uniform_error(again, 409, CONFLICT)
        self.assertEqual(self.commits_above(ctx), 1)

        # The adopted evidence feeds the normal ship chain under a NEW key.
        ship_body = self.assert_ok(self.api_ship(ctx, key="k-after-rec"), 201)
        self.assertEqual(ship_body["ship"]["commit_sha"],
                         body["commit"]["commit_sha"])

    def test_reconcile_requires_matching_command_kind(self) -> None:
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": self.project_id, "title": "wrong kind"},
            "k-other"), 201)
        response = self.post(
            f"/api/v1/commands/{created['command']['command_id']}"
            "/delivery/reconcile",
            {"actor": "ops", "reason": "wrong kind"}, "k-rec-kind")
        self.assert_uniform_error(response, 422, VALIDATION_FAILED)

    def test_reconcile_head_at_base_expires_command_and_allows_retry(self) -> None:
        ctx = self.ready_ship()
        conn = self.core()
        try:
            command = commands_core.submit_command(
                conn, idempotency_key="half-open-2",
                kind=delivery.COMMIT_COMMAND_KIND,
                payload_json=json.dumps({
                    "project_id": self.project_id, "task_id": ctx["task_id"],
                    "attempt_id": ctx["attempt_id"]}),
                evidence_json=json.dumps({"project_id": self.project_id}),
            ).command
            commands_core.claim_command(conn, command.id)
        finally:
            conn.close()
        # Nothing landed: HEAD still equals the verified base.
        response = self.post(
            f"/api/v1/commands/{command.id}/delivery/reconcile",
            {"actor": "ops", "reason": "nothing landed"}, "k-rec-expire")
        body = self.assert_ok(response)
        self.assertEqual(body["resolution"], "expired")
        self.assertIsNone(body["commit"])
        # Re-reconciling the settled (expired) Command replays its resolution.
        again = self.post(
            f"/api/v1/commands/{command.id}/delivery/reconcile",
            {"actor": "ops", "reason": "double check"}, "k-rec-expire-2")
        replay = self.assert_ok(again)
        self.assertEqual(replay["resolution"], "expired")
        self.assertTrue(replay["replayed"])
        self.assertEqual(again.headers[REPLAY_HEADER], "true")
        # Delivery may proceed under a fresh key.
        self.assert_ok(self.api_commit(ctx, key="k-after-expire"), 201)
        self.assertEqual(self.commits_above(ctx), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
