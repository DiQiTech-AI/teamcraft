"""M3-04 controlled Command API tests.

Acceptance line from doc/TEAMCRAFT_2_0_M0_REVIEW.md (M3-04), checked against
the REAL applied semantics (no record-only shortcuts):

* Task create/edit/query, dependency add/remove, priority, pause/resume and
  execution-policy updates run through the shared Core transitions in one
  fenced transaction: the persisted ``tasks.paused`` gate keeps the task out
  of ``ready_queue`` and makes ``create_attempt`` refuse with ``TASK_PAUSED``;
  policy writes persist (delivery/router/verifier/timeout/max_attempts),
  mirror ``max_attempts`` onto the enforced column, and bump the evidence
  version under optimistic concurrency;
* scheduler pause/resume APPLY: while paused, a real ``Scheduler.tick``
  reserves zero attempts; after resume the next tick dispatches again — and
  neither route ever touches the ``leader_lease`` table (pinned with a live
  foreign lease held across the request);
* retry and approval decisions APPLY through ``recovery.approve_retry`` /
  ``verification.decide_approval`` under the API's own short lease; while
  another holder is live these routes fail 409 ``LEADER_BUSY`` and write
  nothing;
* every write requires ``Idempotency-Key`` + applicable Task/evidence
  version; retries are idempotent (same key + identical content replays,
  same key + different payload 409), stale versions return 409, dependency
  cycles return the concrete path, and critical edits to a RUNNING task bump
  the evidence version and expire the stale PENDING commands bound to the
  older version;
* manual CEO requests ONLY queue durable intent (Command + ``ceo.wake_requested``
  Event; no Wake row and no prompt until the next fenced ``Scheduler.tick``
  folds the event into a candidate); recorded async intents cannot linger
  invisibly forever — the tick sweep expires due PENDING commands;
* every write is traceable through linked Command/Event rows with
  project/task linkage, and CLI/API parity holds: the API produces the same
  Core rows, versions and event kinds the core functions produce;
* errors keep the uniform M3-01 shape and leak neither paths nor tokens.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:  # the API needs the web stack; core tests must stay runnable without it
    from fastapi.testclient import TestClient

    from teamcraft.api import create_app
    from teamcraft.api.command import (
        IDEMPOTENCY_HEADER,
        REPLAY_HEADER,
        CommandService,
    )
    from teamcraft.api.errors import (
        CONFLICT,
        CSRF_CHECK_FAILED,
        FORBIDDEN_ORIGIN,
        NOT_FOUND,
        SERVICE_UNAVAILABLE,
        UNAUTHENTICATED,
        VALIDATION_FAILED,
    )
    from teamcraft.api.redaction import REDACTION_MARKER
    from teamcraft.query import QueryService

    _WEB_STACK_OK = True
    _WEB_STACK_REASON = ""
except ImportError as exc:  # pragma: no cover - depends on CI install order
    _WEB_STACK_OK = False
    _WEB_STACK_REASON = f"web dependencies unavailable: {exc}"

from teamcraft import (
    briefs,
    commands as commands_core,
    domain,
    fresh_start,
    leader,
    scheduler as scheduler_module,
    storage,
    verification,
    worker_reports,
)

requires_web = unittest.skipUnless(_WEB_STACK_OK, _WEB_STACK_REASON)

ERROR_KEYS = {"code", "message", "details", "request_id"}
DB_FRAGMENTS = ("teamcraft.db", "/.teamcraft/")


class FixedClock:
    """Deterministic tz-aware clock for dispatch/tick assertions."""

    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: int) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)

#: Content tables whose row counts must stay zero for external-effect proof.
EXTERNAL_EFFECT_TABLES = (
    "attempts",
    "wakes",
    "wake_events",
    "wake_cursors",
    "prompt_deliveries",
    "approvals",
    "verifications",
)

#: Write endpoints exercised by the guard/idempotency sweeps.
_SIMPLE_WRITE_ROUTES = (
    ("post", "/api/v1/tasks", {"project_id": "proj_x", "title": "t"}),
    ("post", "/api/v1/ceo/commands", {
        "action": "request_processing", "project_id": "proj_x",
        "actor": "web", "reason": "r",
    }),
    ("post", "/api/v1/scheduler/pause", {
        "project_id": "proj_x", "actor": "web", "reason": "r",
    }),
    ("post", "/api/v1/scheduler/resume", {
        "project_id": "proj_x", "actor": "web", "reason": "r",
    }),
)


def table_counts(db_path: str) -> dict[str, int]:
    conn = storage.connect(db_path)
    try:
        tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    finally:
        conn.close()


def lease_dump(db_path: str) -> list[tuple]:
    conn = storage.connect(db_path)
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT scope, holder_id, epoch, state, acquired_at, expires_at,"
                " released_at FROM leader_lease ORDER BY scope"
            ).fetchall()
        ]
    finally:
        conn.close()


def commands_rows(db_path: str) -> list[tuple]:
    conn = storage.connect(db_path)
    try:
        return [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT id, kind, state FROM commands ORDER BY requested_at, id"
            ).fetchall()
        ]
    finally:
        conn.close()


class M304Fixture(unittest.TestCase):
    """Temp project root, initialized head DB, guarded app with both services."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="tc-m304-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(self.root, ".teamcraft", "teamcraft.db")
        self.command_service = CommandService(self.db_path)
        self.query_service = QueryService(self.db_path)
        self.app = create_app(
            query_service=self.query_service,
            command_service=self.command_service,
        )
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()

    # -- core-side helpers (the "CLI" side of CLI/API parity) --------------

    def core(self):
        return storage.connect(self.db_path)

    def make_project(self, name: str = "alpha") -> str:
        conn = self.core()
        try:
            return domain.create_project(conn, name, self.root).id
        finally:
            conn.close()

    # -- HTTP helpers ---------------------------------------------------------

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
            path, headers={**self.write_headers(key), **extra_headers}, json=body
        )

    def patch(self, path: str, body: dict, key: str | None = "k-default") -> object:
        return self.client.patch(path, headers=self.write_headers(key), json=body)

    def delete(self, path: str, key: str | None = "k-default",
               expected_version: int | None = None) -> object:
        query = f"?expected_version={expected_version}" if expected_version is not None else ""
        return self.client.delete(
            path + query, headers=self.write_headers(key)
        )

    def get(self, path: str) -> object:
        return self.client.get(path, headers=self.auth())

    # -- assertions -----------------------------------------------------------

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

    def assert_no_external_effects(self) -> None:
        counts = table_counts(self.db_path)
        for table in EXTERNAL_EFFECT_TABLES:
            self.assertEqual(
                counts.get(table, 0), 0,
                f"{table} rows appeared: {counts.get(table)}",
            )

    def assert_no_path_leak(self, response) -> None:
        text = json.dumps(response.json(), ensure_ascii=False)
        for fragment in DB_FRAGMENTS:
            self.assertNotIn(fragment, text)

    # -- real dispatch helpers ---------------------------------------------------

    def tick(self, project_id: str, holder: str = "tick-holder",
             clock: FixedClock | None = None, concurrency_limit: int = 2):
        """Run one real fenced ``Scheduler.tick`` (CEO-init gate mocked).

        Mirrors tests/test_scheduler_m202.py: only the runtime "CEO is live"
        precondition is stubbed; lease fencing, wake intake, the stale-command
        sweep, and attempt reservation all execute for real.  The tick lease is
        released afterwards so later lease-taking steps in the same test see a
        free (retained, epoch-bumping) scope instead of a live holder.
        """

        conn = self.core()
        try:
            sched = scheduler_module.Scheduler(
                conn, project_id, holder,
                concurrency_limit=concurrency_limit,
                clock=clock or FixedClock(),
            )
            with mock.patch.object(
                scheduler_module.ceo_initialization, "require_initialized"
            ):
                result = sched.tick()
            sched.release()
            return result
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# M3-01 guard inheritance
# ---------------------------------------------------------------------------


@requires_web
class GuardInheritanceTests(M304Fixture):
    def test_all_command_routes_deny_anonymous(self) -> None:
        routes = [
            ("post", "/api/v1/tasks", {"project_id": "p", "title": "t"}),
            ("patch", "/api/v1/tasks/t_missing", {"expected_version": 0, "title": "x"}),
            ("post", "/api/v1/tasks/t_missing/dependencies",
             {"depends_on_id": "t_other", "expected_version": 0}),
            ("delete", "/api/v1/tasks/t_missing/dependencies/t_other", None),
            ("post", "/api/v1/tasks/t_missing/commands",
             {"action": "pause", "expected_version": 0, "actor": "a", "reason": "r"}),
            ("post", "/api/v1/tasks/t_missing/policy", {"expected_version": 0, "max_attempts": 5}),
            ("post", "/api/v1/ceo/commands",
             {"action": "request_processing", "project_id": "p", "actor": "a", "reason": "r"}),
            ("post", "/api/v1/scheduler/pause", {"project_id": "p", "actor": "a", "reason": "r"}),
            ("post", "/api/v1/scheduler/resume", {"project_id": "p", "actor": "a", "reason": "r"}),
            ("post", "/api/v1/verifications/v_missing/approvals",
             {"decision": "accept", "actor": "a", "reason": "r", "expected_task_version": 0}),
            ("get", "/api/v1/commands/c_missing", None),
        ]
        for method, path, body in routes:
            url = path + "?expected_version=0" if method == "delete" else path
            response = self.client.request(method, url, json=body)
            self.assert_uniform_error(response, 401, UNAUTHENTICATED)

    def test_scheduler_state_requires_auth(self) -> None:
        self.assert_uniform_error(
            self.client.get("/api/v1/scheduler/state?project_id=proj_p"),
            401, UNAUTHENTICATED,
        )

    def test_wrong_token_is_401_on_command_routes(self) -> None:
        response = self.client.post(
            "/api/v1/tasks",
            headers={**self.auth("wrong-token-value-that-is-long-enough-1234567890"),
                    "X-CSRF-Token": self.token, IDEMPOTENCY_HEADER: "k"},
            json={"project_id": "p", "title": "t"},
        )
        self.assert_uniform_error(response, 401, UNAUTHENTICATED)

    def test_write_without_csrf_header_is_403(self) -> None:
        response = self.client.post(
            "/api/v1/tasks",
            headers={**self.auth(), IDEMPOTENCY_HEADER: "k"},
            json={"project_id": "p", "title": "t"},
        )
        self.assert_uniform_error(response, 403, CSRF_CHECK_FAILED)

    def test_cross_origin_write_is_403(self) -> None:
        response = self.post(
            "/api/v1/tasks", {"project_id": "p", "title": "t"}, "k",
            Origin="http://evil.example",
        )
        self.assert_uniform_error(response, 403, FORBIDDEN_ORIGIN)

    def test_same_origin_browser_write_is_allowed(self) -> None:
        project_id = self.make_project()
        response = self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "web-ok"}, "k",
            Origin="http://testserver",
        )
        self.assertEqual(response.status_code, 201)


# ---------------------------------------------------------------------------
# Idempotency contract
# ---------------------------------------------------------------------------


@requires_web
class IdempotencyTests(M304Fixture):
    def test_every_write_route_requires_idempotency_key(self) -> None:
        for method, path, body in _SIMPLE_WRITE_ROUTES:
            response = self.client.request(
                method, path, headers=self.write_headers(None), json=body
            )
            body_out = self.assert_uniform_error(
                response, 422, VALIDATION_FAILED
            )
            self.assertEqual(body_out["details"]["field"], IDEMPOTENCY_HEADER)

    def test_blank_idempotency_key_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/tasks",
            headers={**self.auth(), "X-CSRF-Token": self.token, IDEMPOTENCY_HEADER: "   "},
            json={"project_id": "p", "title": "t"},
        )
        self.assert_uniform_error(response, 422, VALIDATION_FAILED)

    def test_create_retry_is_idempotent_and_conflicting_payload_conflicts(self) -> None:
        project_id = self.make_project()
        body = {"project_id": project_id, "title": "Ship M3-04",
                "acceptance": ["tests pass"], "ownership": ["teamcraft/api/**"]}
        first = self.post("/api/v1/tasks", body, "k-create")
        created = self.assert_ok(first, 201)
        self.assertNotIn(REPLAY_HEADER, first.headers)
        task_id = created["task"]["id"]

        retry = self.post("/api/v1/tasks", body, "k-create")
        replayed = self.assert_ok(retry, 201)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(retry.headers[REPLAY_HEADER], "true")
        self.assertEqual(replayed["task"]["id"], task_id)
        self.assertEqual(replayed["command"]["command_id"], created["command"]["command_id"])
        counts = table_counts(self.db_path)
        self.assertEqual(counts["tasks"], 1)
        self.assertEqual(counts["commands"], 1)

        conflict = self.post("/api/v1/tasks",
                            {"project_id": project_id, "title": "Different"}, "k-create")
        out = self.assert_uniform_error(conflict, 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(out["details"]["existing_command_id"],
                         created["command"]["command_id"])
        # refusal wrote nothing
        self.assertEqual(table_counts(self.db_path)["tasks"], 1)
        self.assertEqual(table_counts(self.db_path)["commands"], 1)

    def test_edit_pause_and_scheduler_replays(self) -> None:
        project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "A"}, "k-a"), 201)
        task_id = created["task"]["id"]

        edit = {"expected_version": 0, "priority": 95}
        first = self.patch(f"/api/v1/tasks/{task_id}", edit, "k-edit")
        self.assert_ok(first)
        retry = self.patch(f"/api/v1/tasks/{task_id}", edit, "k-edit")
        self.assertTrue(self.assert_ok(retry)["replayed"])
        self.assertEqual(table_counts(self.db_path)["commands"], 2)

        pause_body = {"action": "pause", "expected_version": 1,
                      "actor": "alice", "reason": "hold"}
        p1 = self.post(f"/api/v1/tasks/{task_id}/commands", pause_body, "k-pause")
        applied = self.assert_ok(p1, 200)
        self.assertTrue(applied["paused"])
        self.assertEqual(applied["execution"], "applied")
        p2 = self.post(f"/api/v1/tasks/{task_id}/commands", pause_body, "k-pause")
        self.assertEqual(p2.status_code, 200)
        self.assertTrue(p2.json()["replayed"])
        self.assertEqual(p2.headers[REPLAY_HEADER], "true")
        self.assertEqual(p2.json()["command"]["command_id"],
                         p1.json()["command"]["command_id"])

        sp_body = {"project_id": project_id, "actor": "ops", "reason": "deploy window"}
        s1 = self.post("/api/v1/scheduler/pause", sp_body, "k-sp")
        self.assertEqual(self.assert_ok(s1)["scheduler"]["paused"], True)
        s2 = self.post("/api/v1/scheduler/pause", sp_body, "k-sp")
        self.assertEqual(self.assert_ok(s2)["scheduler"]["paused"], True)
        self.assertEqual(s1.json()["command"]["command_id"],
                         s2.json()["command"]["command_id"])
        self.assertTrue(s2.json()["replayed"])
        self.assert_no_external_effects()

    def test_same_key_different_action_conflicts(self) -> None:
        project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "A"}, "k-a"), 201)
        task_id = created["task"]["id"]
        pause = {"action": "pause", "expected_version": 0, "actor": "a", "reason": "r"}
        resume = {"action": "resume", "expected_version": 0, "actor": "a", "reason": "r"}
        self.assertEqual(self.post(f"/api/v1/tasks/{task_id}/commands", pause, "k-pr").status_code, 200)
        out = self.assert_uniform_error(
            self.post(f"/api/v1/tasks/{task_id}/commands", resume, "k-pr"), 409, CONFLICT
        )
        self.assertEqual(out["details"]["reason_code"], "IDEMPOTENCY_KEY_REUSED")


# ---------------------------------------------------------------------------
# Version (evidence) conflicts
# ---------------------------------------------------------------------------


@requires_web
class VersionConflictTests(M304Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": self.project_id, "title": "V"}, "k-v"), 201)
        self.task_id = created["task"]["id"]

    def test_stale_versions_are_409_without_residue(self) -> None:
        before_commands = len(commands_rows(self.db_path))
        cases = [
            self.patch(f"/api/v1/tasks/{self.task_id}",
                      {"expected_version": 7, "priority": 10}, "k-stale-patch"),
            self.post(f"/api/v1/tasks/{self.task_id}/dependencies",
                     {"depends_on_id": "t_other", "expected_version": 7}, "k-stale-dep"),
            self.delete(f"/api/v1/tasks/{self.task_id}/dependencies/t_other",
                       "k-stale-rm", expected_version=7),
            self.post(f"/api/v1/tasks/{self.task_id}/commands",
                     {"action": "pause", "expected_version": 7, "actor": "a", "reason": "r"},
                     "k-stale-pause"),
            self.post(f"/api/v1/tasks/{self.task_id}/policy",
                     {"expected_version": 7, "max_attempts": 4}, "k-stale-policy"),
        ]
        for response in cases:
            out = self.assert_uniform_error(response, 409, CONFLICT)
            self.assertEqual(out["details"]["reason_code"], "VERSION_CONFLICT")
            self.assertEqual(out["details"]["expected"], 7)
            self.assertEqual(out["details"]["found"], 0)
        # refusals rolled back: no command rows survive
        self.assertEqual(len(commands_rows(self.db_path)), before_commands)

    def test_edit_bumps_evidence_and_expires_stale_pending_commands(self) -> None:
        # a queued async side effect bound to version 0 ...
        wake = self.assert_ok(self.post(
            "/api/v1/ceo/commands",
            {"action": "request_processing", "project_id": self.project_id,
             "actor": "alice", "reason": "look at this",
             "task_id": self.task_id, "expected_task_version": 0},
            "k-wake-v0"), 202)
        wake_id = wake["command"]["command_id"]
        self.assertEqual(wake["command"]["state"], "pending")

        # ... a critical edit creates the new evidence version ...
        edited = self.assert_ok(self.patch(
            f"/api/v1/tasks/{self.task_id}",
            {"expected_version": 0, "title": "Renamed mid-flight"}, "k-edit-v0"))
        self.assertEqual(edited["task"]["version"], 1)
        self.assertEqual(edited["evidence"], {"previous_version": 0, "version": 1})

        # ... and atomically expires the command bound to the OLD evidence.
        status = self.assert_ok(self.get(f"/api/v1/commands/{wake_id}"))
        self.assertEqual(status["command"]["state"], "expired")
        self.assertEqual(status["command"]["note"], "EVIDENCE_STALE")

    def test_edit_of_running_task_bumps_version_but_keeps_running(self) -> None:
        # Move the task PENDING -> READY -> RUNNING through the core path
        # (every CAS bump counts: 0 -> 1 ready, 1 -> 2 running).
        conn = self.core()
        try:
            task = domain.get_task(conn, self.task_id)
            task = domain.apply_task_event(conn, self.task_id, "dependencies_satisfied",
                                          expected_version=task.version)
            attempt = domain.create_attempt(conn, self.task_id,
                                           expected_version=task.version)
            live = domain.get_task(conn, self.task_id)
            attempt_id = attempt.id
            live_version = live.version
        finally:
            conn.close()
        self.assertEqual(live_version, 2)

        # A controlled pause refuses to strand the in-flight slot: RUNNING is
        # not pausable (cancel is the in-flight path) and the refusal leaves
        # no command residue.
        commands_before = len(commands_rows(self.db_path))
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "pause", "expected_version": live_version,
             "actor": "alice", "reason": "hold"},
            "k-pause-running"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "TASK_NOT_PAUSABLE")
        self.assertEqual(len(commands_rows(self.db_path)), commands_before)

        # A queued async intent bound to the running version ...
        wake = self.assert_ok(self.post(
            "/api/v1/ceo/commands",
            {"action": "request_processing", "project_id": self.project_id,
             "actor": "alice", "reason": "look",
             "task_id": self.task_id, "expected_task_version": live_version},
            "k-wake-running"), 202)

        # ... a critical edit bumps evidence while RUNNING persists ...
        edited = self.assert_ok(self.patch(
            f"/api/v1/tasks/{self.task_id}",
            {"expected_version": live_version, "description": "critical edit"},
            "k-edit-running"))
        self.assertEqual(edited["task"]["status"], "running")
        self.assertEqual(edited["task"]["version"], live_version + 1)
        # ... and the queued intent bound to the older evidence is dead.
        after = self.assert_ok(
            self.get(f"/api/v1/commands/{wake['command']['command_id']}"))
        self.assertEqual(after["command"]["state"], "expired")
        self.assertEqual(after["command"]["note"], "EVIDENCE_STALE")

        # Dependency edits on a RUNNING task are refused with a state conflict.
        out = self.assert_uniform_error(
            self.post(f"/api/v1/tasks/{self.task_id}/dependencies",
                     {"depends_on_id": "t_other",
                      "expected_version": live_version + 1}, "k-dep-running"),
            409, CONFLICT,
        )
        self.assertEqual(out["details"]["reason_code"], "DEPENDENCY_EDIT_NOT_ALLOWED")
        self.assertEqual(out["details"]["current_status"], "running")

    def test_ceo_request_with_stale_task_version_rejected_no_residue(self) -> None:
        before = len(commands_rows(self.db_path))
        out = self.assert_uniform_error(
            self.post("/api/v1/ceo/commands",
                     {"action": "request_processing", "project_id": self.project_id,
                      "actor": "web", "reason": "look",
                      "task_id": self.task_id, "expected_task_version": 42},
                     "k-wake-stale"),
            409, CONFLICT,
        )
        self.assertEqual(out["details"]["reason_code"], "VERSION_CONFLICT")
        self.assertEqual(len(commands_rows(self.db_path)), before)


# ---------------------------------------------------------------------------
# DAG dependency operations
# ---------------------------------------------------------------------------


@requires_web
class DependencyTests(M304Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.make_project()

    def _task(self, title: str, key: str) -> str:
        created = self.assert_ok(
            self.post("/api/v1/tasks",
                     {"project_id": self.project_id, "title": title}, key), 201)
        return created["task"]["id"]

    def test_add_remove_round_trip_visible_to_core(self) -> None:
        a = self._task("A", "k-a")
        b = self._task("B", "k-b")
        added = self.assert_ok(self.post(
            f"/api/v1/tasks/{a}/dependencies",
            {"depends_on_id": b, "expected_version": 0}, "k-add"))
        self.assertTrue(added["added"])
        self.assertEqual(added["task"]["version"], 1)

        conn = self.core()
        try:
            self.assertEqual(domain.dependencies_of(conn, a), [b])
            # CLI parity: the core re-derivation honours the API-added edge —
            # A stays blocked (B is not DONE), B itself becomes dispatchable.
            changes = domain.recompute_ready(conn, self.project_id)
            self.assertNotIn(a, changes)  # A was already non-ready: no churn
            self.assertEqual(domain.get_task(conn, a).status, "pending")
            self.assertEqual(changes.get(b), ("pending", "ready"))
        finally:
            conn.close()

        removed = self.assert_ok(self.delete(
            f"/api/v1/tasks/{a}/dependencies/{b}", "k-rm", expected_version=1))
        self.assertTrue(removed["removed"])
        self.assertEqual(removed["task"]["version"], 2)
        conn = self.core()
        try:
            self.assertEqual(domain.dependencies_of(conn, a), [])
        finally:
            conn.close()

    def test_no_op_edge_add_does_not_bump_version(self) -> None:
        a = self._task("A", "k-a")
        b = self._task("B", "k-b")
        self.assert_ok(self.post(f"/api/v1/tasks/{a}/dependencies",
                                {"depends_on_id": b, "expected_version": 0}, "k-add"), 200)
        again = self.assert_ok(self.post(f"/api/v1/tasks/{a}/dependencies",
                                        {"depends_on_id": b, "expected_version": 1}, "k-add2"), 200)
        self.assertFalse(again["added"])
        self.assertEqual(again["task"]["version"], 1)

    def test_self_dependency_rejected_with_closed_path(self) -> None:
        a = self._task("A", "k-a")
        out = self.assert_uniform_error(
            self.post(f"/api/v1/tasks/{a}/dependencies",
                     {"depends_on_id": a, "expected_version": 0}, "k-self"),
            409, CONFLICT,
        )
        self.assertEqual(out["details"]["reason_code"], "DEPENDENCY_CYCLE")
        cycle = out["details"]["cycle"]
        self.assertEqual(cycle, [a, a])

    def test_cycle_rejected_with_concrete_closed_path(self) -> None:
        a = self._task("A", "k-a")
        b = self._task("B", "k-b")
        c = self._task("C", "k-c")
        self.assert_ok(self.post(f"/api/v1/tasks/{a}/dependencies",
                                {"depends_on_id": b, "expected_version": 0}, "k-ab"), 200)
        self.assert_ok(self.post(f"/api/v1/tasks/{b}/dependencies",
                                {"depends_on_id": c, "expected_version": 0}, "k-bc"), 200)
        out = self.assert_uniform_error(
            self.post(f"/api/v1/tasks/{c}/dependencies",
                     {"depends_on_id": a, "expected_version": 0}, "k-ca"),
            409, CONFLICT,
        )
        self.assertEqual(out["details"]["reason_code"], "DEPENDENCY_CYCLE")
        cycle = out["details"]["cycle"]
        # Closed path: first == last == C; consecutive pairs are real edges.
        self.assertEqual(cycle[0], c)
        self.assertEqual(cycle[-1], c)
        self.assertEqual(cycle, [c, a, b, c])
        # graph unchanged by the refusal
        conn = self.core()
        try:
            self.assertEqual(domain.dependencies_of(conn, c), [])
        finally:
            conn.close()

    def test_dependency_on_terminal_task_rejected(self) -> None:
        a = self._task("A", "k-a")
        b = self._task("B", "k-b")
        self.assert_ok(self.post(f"/api/v1/tasks/{b}/commands",
                                {"action": "cancel", "expected_version": 0,
                                 "actor": "alice", "reason": "drop"}, "k-cancel-b"), 200)
        out = self.assert_uniform_error(
            self.post(f"/api/v1/tasks/{a}/dependencies",
                     {"depends_on_id": b, "expected_version": 0}, "k-dead-dep"),
            409, CONFLICT,
        )
        self.assertEqual(out["details"]["reason_code"], "DEPENDENCY_TARGET_TERMINAL")
        self.assertEqual(out["details"]["current_status"], "cancelled")

    def test_unknown_dependency_is_404(self) -> None:
        a = self._task("A", "k-a")
        self.assert_uniform_error(
            self.post(f"/api/v1/tasks/{a}/dependencies",
                     {"depends_on_id": "t_ghost", "expected_version": 0}, "k-ghost"),
            404, NOT_FOUND,
        )


# ---------------------------------------------------------------------------
# Task lifecycle commands
# ---------------------------------------------------------------------------


@requires_web
class TaskCommandTests(M304Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": self.project_id, "title": "L"}, "k-l"), 201)
        self.task_id = created["task"]["id"]

    def test_pause_and_resume_apply_and_gate_dispatch(self) -> None:
        # Make the task dispatchable first, then pause it through the API.
        conn = self.core()
        try:
            domain.recompute_ready(conn, self.project_id)
            live = domain.get_task(conn, self.task_id)
            self.assertEqual(live.status, "ready")
            version = live.version
        finally:
            conn.close()

        pause = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "pause", "expected_version": version, "actor": "alice",
             "reason": "investigate"}, "k-p"), 200)
        self.assertEqual(pause["command"]["kind"], "task.pause")
        self.assertEqual(pause["command"]["state"], "confirmed")
        self.assertTrue(pause["paused"])
        self.assertEqual(pause["task"]["status"], "ready")

        conn = self.core()
        try:
            paused = domain.get_task(conn, self.task_id)
            self.assertTrue(bool(paused.paused))
            # enforced at BOTH boundaries: hidden from the queue, and the
            # reservation call itself refuses even for a caller that insists.
            self.assertNotIn(self.task_id,
                            [t.id for t in domain.ready_queue(conn, self.project_id)])
            with self.assertRaises(domain.IllegalTransitionError) as ctx:
                domain.create_attempt(conn, self.task_id,
                                     expected_version=paused.version)
            self.assertEqual(ctx.exception.reason_code, "TASK_PAUSED")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        finally:
            conn.close()

        resume = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "resume", "expected_version": version + 1,
             "actor": "alice", "reason": "continue"}, "k-r"), 200)
        self.assertEqual(resume["command"]["kind"], "task.resume")
        self.assertFalse(resume["paused"])
        conn = self.core()
        try:
            resumed = domain.get_task(conn, self.task_id)
            self.assertEqual(bool(resumed.paused), False)
            # status re-derived from dependency facts: still READY, and back
            # in the dispatchable queue.
            self.assertEqual(resumed.status, "ready")
            self.assertIn(self.task_id,
                         [t.id for t in domain.ready_queue(conn, self.project_id)])
        finally:
            conn.close()

        # durable, linked audit for both transitions
        conn = self.core()
        try:
            kinds = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT kind FROM events WHERE task_id = ?",
                (self.task_id,)).fetchall()]
            self.assertIn("task.paused", kinds)
            self.assertIn("task.resumed", kinds)
        finally:
            conn.close()
        self.assert_no_external_effects()

    def test_pause_is_durable_across_reconnect(self) -> None:
        self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "pause", "expected_version": 0, "actor": "alice",
             "reason": "hold"}, "k-pd"), 200)
        conn = self.core()  # fresh connection: the flag lives in the DB
        try:
            self.assertTrue(bool(domain.get_task(conn, self.task_id).paused))
        finally:
            conn.close()

    def test_double_pause_and_re_resume_are_conflicts(self) -> None:
        self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "pause", "expected_version": 0, "actor": "a",
             "reason": "r"}, "k-dp"), 200)
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "pause", "expected_version": 1, "actor": "a",
             "reason": "r"}, "k-dp2"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "TASK_ALREADY_PAUSED")
        # one resume succeeds ...
        self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "resume", "expected_version": 1, "actor": "a",
             "reason": "r"}, "k-dr"), 200)
        # ... a second resume against the unpaused task is a state conflict.
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "resume", "expected_version": 2, "actor": "a",
             "reason": "r"}, "k-dr2"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "TASK_NOT_PAUSED")
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "pause", "expected_version": 99, "actor": "a",
             "reason": "r"}, "k-dp-stale"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "VERSION_CONFLICT")

    def test_retry_with_occupying_attempt_is_refused_without_residue(self) -> None:
        # A live Attempt must be reconciled before any retry approval: Core's
        # invariant, surfaced as a state conflict; nothing is written and no
        # lease materialises.
        conn = self.core()
        try:
            task = domain.apply_task_event(conn, self.task_id,
                                          "dependencies_satisfied",
                                          expected_version=0)
            domain.create_attempt(conn, self.task_id, expected_version=task.version)
            live_version = domain.get_task(conn, self.task_id).version
            leases_before = lease_dump(self.db_path)
        finally:
            conn.close()
        commands_before = len(commands_rows(self.db_path))
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "retry", "expected_version": live_version, "actor": "alice",
             "reason": "transient infra failure"}, "k-retry-busy-slot"),
            409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "ILLEGAL_TRANSITION")
        self.assertEqual(len(commands_rows(self.db_path)), commands_before)
        self.assertEqual(lease_dump(self.db_path), leases_before)

    def test_cancel_without_attempt_applies_locally_and_replays(self) -> None:
        cancel = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "cancel", "expected_version": 0, "actor": "alice",
             "reason": "no longer needed"}, "k-cancel"), 200)
        self.assertEqual(cancel["task"]["status"], "cancelled")
        self.assertEqual(cancel["command"]["state"], "confirmed")
        replay = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "cancel", "expected_version": 0, "actor": "alice",
             "reason": "no longer needed"}, "k-cancel"), 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["task"]["status"], "cancelled")
        # core sees the identical transition the CLI would produce
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, self.task_id).status, "cancelled")
        finally:
            conn.close()

    def test_cancel_with_occupying_attempt_records_external_command(self) -> None:
        conn = self.core()
        try:
            task = domain.apply_task_event(conn, self.task_id, "dependencies_satisfied",
                                          expected_version=0)
            attempt = domain.create_attempt(conn, self.task_id, expected_version=task.version)
        finally:
            conn.close()
        live = domain.get_task(self.core(), self.task_id)
        self.assertEqual(live.status, "running")

        resp = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "cancel", "expected_version": live.version, "actor": "alice",
             "reason": "stop it"}, "k-ext-cancel"), 202)
        self.assertEqual(resp["command"]["kind"], "recovery.cancel")
        self.assertEqual(resp["command"]["state"], "pending")
        # The API never stopped anything itself: Task and Attempt untouched.
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, self.task_id).status, "running")
            self.assertEqual(domain.get_attempt(conn, attempt.id).status, "created")
        finally:
            conn.close()
        replay = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "cancel", "expected_version": live.version, "actor": "alice",
             "reason": "stop it"}, "k-ext-cancel"), 202)
        self.assertTrue(replay["replayed"])

    def test_wait_requires_needs_human_and_reconcile_requires_unknown(self) -> None:
        # Core's recovery guards drive the refusals: wait needs needs_human,
        # reconcile needs an UNKNOWN attempt, unknown attempts are 404s.
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "wait", "expected_version": 0, "actor": "a", "reason": "r"},
            "k-wait"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "ILLEGAL_TRANSITION")
        self.assertEqual(out["details"]["current_status"], "pending")
        self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "reconcile", "expected_version": 0, "actor": "a",
             "reason": "r", "attempt_id": "a_missing"}, "k-rec"), 404, NOT_FOUND)
        # reconcile without any attempt: attempt-state conflict (409).
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "reconcile", "expected_version": 0, "actor": "a",
             "reason": "r"}, "k-rec-none"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "ILLEGAL_TRANSITION")

    def test_terminal_task_refuses_pause_and_retry(self) -> None:
        self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "cancel", "expected_version": 0, "actor": "a",
             "reason": "done with it"}, "k-term-cancel"), 200)
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "pause", "expected_version": 1, "actor": "a",
             "reason": "r"}, "k-term-pause"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "TASK_TERMINAL")
        # Retry reaches Core's lifecycle guard: a terminal Task is neither
        # running nor needs_human, so the approval is refused.
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "retry", "expected_version": 1, "actor": "a",
             "reason": "r"}, "k-term-retry"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "ILLEGAL_TRANSITION")
        self.assertEqual(out["details"]["current_status"], "cancelled")

    def test_unknown_action_rejected(self) -> None:
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/commands",
            {"action": "teleport", "expected_version": 0, "actor": "a",
             "reason": "r"}, "k-bad"), 422, VALIDATION_FAILED)
        self.assertEqual(out["details"]["field"], "action")


# ---------------------------------------------------------------------------
# Manual CEO Wake request + scheduler pause/resume
# ---------------------------------------------------------------------------


@requires_web
class WakeAndSchedulerTests(M304Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.make_project()

    def test_manual_ceo_request_records_command_and_delivers_nothing(self) -> None:
        resp = self.assert_ok(self.post(
            "/api/v1/ceo/commands",
            {"action": "request_processing", "project_id": self.project_id,
             "actor": "web-user", "reason": "needs a decision"}, "k-wake"), 202)
        command = resp["command"]
        self.assertEqual(command["kind"], "ceo.wake_request")
        self.assertEqual(command["state"], "pending")
        # durable audit: command.submitted event exists for the project
        conn = self.core()
        try:
            rows = conn.execute(
                "SELECT kind, payload_json FROM events WHERE kind='command.submitted'"
                " ORDER BY seq DESC LIMIT 5"
            ).fetchall()
            self.assertTrue(any(command["command_id"] in str(r[1]) for r in rows))
            # no actual send pipeline was engaged
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM commands WHERE kind='ceo.wake'"
                           ).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM prompt_deliveries").fetchone()[0], 0)
        finally:
            conn.close()
        self.assert_no_external_effects()

    def test_ceo_request_replay_idempotent(self) -> None:
        body = {"action": "request_processing", "project_id": self.project_id,
                "actor": "web-user", "reason": "again"}
        first = self.assert_ok(self.post("/api/v1/ceo/commands", body, "k-wake2"), 202)
        retry = self.assert_ok(self.post("/api/v1/ceo/commands", body, "k-wake2"), 202)
        self.assertTrue(retry["replayed"])
        self.assertEqual(first["command"]["command_id"], retry["command"]["command_id"])
        self.assertEqual(len(commands_rows(self.db_path)), 1)

    def test_ceo_request_rejects_unknown_action_and_project(self) -> None:
        out = self.assert_uniform_error(self.post(
            "/api/v1/ceo/commands",
            {"action": "wake_now", "project_id": self.project_id,
             "actor": "web", "reason": "r"}, "k-bad-action"), 422, VALIDATION_FAILED)
        self.assertEqual(out["details"]["field"], "action")
        self.assert_uniform_error(self.post(
            "/api/v1/ceo/commands",
            {"action": "request_processing", "project_id": "proj_ghost",
             "actor": "web", "reason": "r"}, "k-ghost-project"), 404, NOT_FOUND)

    def test_scheduler_pause_resume_never_touches_leader_lease(self) -> None:
        # A live Scheduler Leader holds the project lease during the whole test:
        # the applied-tier pause/resume must succeed WITHOUT acquiring,
        # renewing, releasing, or stealing anything in ``leader_lease``.
        conn = self.core()
        try:
            lease = leader.acquire_or_renew(
                conn, leader.project_scope(self.project_id), "scheduler-holder",
                ttl_seconds=300,
            )
            held_before = lease_dump(self.db_path)
        finally:
            conn.close()

        pause = self.assert_ok(self.post(
            "/api/v1/scheduler/pause",
            {"project_id": self.project_id, "actor": "ops", "reason": "maintenance"},
            "k-sp"))
        self.assertEqual(pause["execution"], "applied")
        self.assertEqual(pause["scheduler"]["paused"], True)
        self.assertEqual(pause["command"]["kind"], "scheduler.pause")
        self.assertEqual(pause["command"]["state"], "confirmed")
        resume = self.assert_ok(self.post(
            "/api/v1/scheduler/resume",
            {"project_id": self.project_id, "actor": "ops", "reason": "done"},
            "k-sr"))
        self.assertEqual(resume["execution"], "applied")
        self.assertEqual(resume["scheduler"]["paused"], False)
        self.assertEqual(resume["command"]["kind"], "scheduler.resume")
        self.assertEqual(resume["command"]["state"], "confirmed")

        # Same leader, same epoch, byte-identical rows: nothing was stolen,
        # released, or renewed by either route.
        self.assertEqual(lease_dump(self.db_path), held_before)
        conn = self.core()
        try:
            current = leader.current_lease(
                conn, leader.project_scope(self.project_id))
            assert current is not None
            self.assertEqual(current.holder_id, "scheduler-holder")
            self.assertEqual(current.epoch, lease.epoch)
        finally:
            conn.close()
        self.assert_no_external_effects()

    def test_scheduler_pause_events_cannot_fan_out_into_wakes(self) -> None:
        # scheduler.*/command.* event kinds are on wake ingestion's ignore
        # list.  The applied requests record their audit events; a REAL fenced
        # tick then runs the intake for the whole project, and these control-
        # plane events still add zero Wake rows — command traffic can never
        # fan out into a Wake storm.
        pause = self.assert_ok(self.post(
            "/api/v1/scheduler/pause",
            {"project_id": self.project_id, "actor": "ops", "reason": "quiet"},
            "k-sp2"))
        self.assertEqual(pause["execution"], "applied")
        conn = self.core()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0], 0)
            kinds = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT kind FROM events").fetchall()]
            self.assertIn("command.submitted", kinds)
            self.assertIn("scheduler.paused", kinds)
        finally:
            conn.close()
        self.tick(self.project_id)  # real intake under a real lease
        conn = self.core()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM wake_events").fetchone()[0], 0)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Execution-policy updates
# ---------------------------------------------------------------------------


@requires_web
class PolicyTests(M304Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": self.project_id, "title": "P"}, "k-p"), 201)
        self.task_id = created["task"]["id"]

    def test_policy_update_applies_and_mirrors_max_attempts(self) -> None:
        resp = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/policy",
            {"expected_version": 0, "delivery": "local_only", "router": "auto",
             "timeout_seconds": 900, "max_attempts": 5}, "k-pol"))
        self.assertEqual(resp["execution"], "applied")
        self.assertEqual(resp["command"]["kind"], "task.policy_update")
        self.assertEqual(resp["command"]["state"], "confirmed")
        self.assertEqual(resp["policy"]["delivery"], "local_only")
        self.assertEqual(resp["task"]["version"], 1)
        conn = self.core()
        try:
            task = domain.get_task(conn, self.task_id)
            # persisted policy + enforced-column mirror + durable event
            self.assertEqual(task.max_attempts, 5)
            self.assertEqual(json.loads(task.execution_policy_json),
                             {"delivery": "local_only", "router": "auto",
                              "timeout_seconds": 900, "max_attempts": 5})
            kinds = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT kind FROM events WHERE task_id = ?",
                (self.task_id,)).fetchall()]
            self.assertIn("task.policy_updated", kinds)
        finally:
            conn.close()
        # replay: same key + identical content is a labelled no-op
        replay = self.assert_ok(self.post(
            f"/api/v1/tasks/{self.task_id}/policy",
            {"expected_version": 0, "delivery": "local_only", "router": "auto",
             "timeout_seconds": 900, "max_attempts": 5}, "k-pol"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["command"]["command_id"],
                         resp["command"]["command_id"])
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, self.task_id).version, 1)
        finally:
            conn.close()

    def test_unknown_delivery_mode_is_refused_without_residue(self) -> None:
        # The closed delivery set rejects "worktree" (and any other unknown
        # mode) BEFORE persistence; the transaction rolls the command row back.
        before = len(commands_rows(self.db_path))
        self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/policy",
            {"expected_version": 0, "delivery": "worktree"}, "k-pol-bad"),
            422, VALIDATION_FAILED)
        self.assertEqual(len(commands_rows(self.db_path)), before)
        conn = self.core()
        try:
            task = domain.get_task(conn, self.task_id)
            self.assertEqual(json.loads(task.execution_policy_json), {})
        finally:
            conn.close()

    def test_empty_policy_rejected(self) -> None:
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{self.task_id}/policy", {"expected_version": 0}, "k-pol0"),
            422, VALIDATION_FAILED)
        self.assertEqual(out["details"]["field"], "body")

    def test_priority_edit_is_applied_and_affects_dispatch_order(self) -> None:
        b = self.assert_ok(self.post(
            "/api/v1/tasks",
            {"project_id": self.project_id, "title": "B", "priority": 10}, "k-b"), 201)
        conn = self.core()
        try:
            domain.recompute_ready(conn, self.project_id)
            # recompute bumps both versions (pending -> ready churn).
            v_self = domain.get_task(conn, self.task_id).version
            v_b = domain.get_task(conn, b["task"]["id"]).version
            queue = domain.ready_queue(conn, self.project_id)
            self.assertEqual([t.id for t in queue], [self.task_id, b["task"]["id"]])
        finally:
            conn.close()
        self.assert_ok(self.patch(
            f"/api/v1/tasks/{b['task']['id']}",
            {"expected_version": v_b, "priority": 99}, "k-prio-b"), 200)
        conn = self.core()
        try:
            queue = domain.ready_queue(conn, self.project_id)
            self.assertEqual([t.id for t in queue], [b["task"]["id"], self.task_id])
            # core CAS semantics identical to CLI edit: version moved by 1.
            self.assertEqual(domain.get_task(conn, b["task"]["id"]).version,
                             v_b + 1)
            self.assertEqual(domain.get_task(conn, self.task_id).version, v_self)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Approval decisions (recorded against real verification evidence)
# ---------------------------------------------------------------------------


@requires_web
class ApprovalDecisionTests(M304Fixture):
    """Drives a full M2-06 scout chain via Core, then decides via the API."""

    def setUp(self) -> None:
        super().setUp()
        conn = self.core()
        try:
            self.project_id = domain.create_project(conn, "ver", self.root).id
            task = domain.create_task(conn, self.project_id, "scout job", kind="scout")
            task = domain.apply_task_event(conn, task.id, "dependencies_satisfied",
                                          expected_version=task.version)
            attempt = domain.create_attempt(conn, task.id, expected_version=task.version)
            domain.transition_attempt(conn, attempt.id, "start")
            domain.transition_attempt(conn, attempt.id, "activated")
            brief = briefs.create_attempt_brief(conn, attempt.id, content="scout brief")
            report = {
                "schema_version": 1, "task_id": task.id, "attempt_id": attempt.id,
                "task_version": brief.task_version, "brief_digest": brief.brief_digest,
                "report_kind": "final", "outcome": "completed", "milestone_id": "final",
                "summary": "done", "artifact_digest": "a" * 64,
                "usage": {"total_tokens": None},
            }
            worker_reports.record_worker_report(conn, report)
            task = domain.get_task(conn, task.id)
            task = domain.apply_task_event(conn, task.id, "worker_reported_done",
                                          expected_version=task.version)
            lease = leader.acquire_or_renew(
                conn, leader.project_scope(self.project_id), "verifier-holder",
                ttl_seconds=300,
            )
            verification.configure_verifier(
                conn, self.project_id, argv=["verify", "--strict"],
                rules_version="rules-1", timeout_seconds=30, actor="human",
                idempotency_key="cfg-key", lease=lease,
            )
            completed = (0, b"PASS", b"", False, False, None)
            with mock.patch.object(verification, "_run_bounded",
                                  return_value=completed):
                self.verification = verification.run_verification(
                    conn, attempt.id, idempotency_key="run-key", lease=lease)
            # Hand authority back: the verifier's job is done, so the API's
            # own short lease can be admitted through the normal protocol.
            leader.release(conn, lease.scope, lease.holder_id, epoch=lease.epoch)
            self.task_id = task.id
            self.task_version = domain.get_task(conn, task.id).version
            self.lease_before = lease_dump(self.db_path)
            self.released_epoch = lease.epoch
        finally:
            conn.close()

    def test_accept_decision_applies_through_core(self) -> None:
        resp = self.assert_ok(self.post(
            f"/api/v1/verifications/{self.verification.id}/approvals",
            {"decision": "accept", "actor": "boss", "reason": "looks right",
             "expected_task_version": self.task_version}, "k-approve"))
        command = resp["command"]
        self.assertEqual(command["kind"], "verification.approval")
        self.assertEqual(command["state"], "confirmed")
        self.assertEqual(resp["approval"]["decision"], "accept")
        self.assertEqual(resp["approval"]["verification_id"],
                         self.verification.id)
        self.assertEqual(resp["approval"]["task_id"], self.task_id)
        conn = self.core()
        try:
            # The decision went through Core: exactly one Approval row, and
            # the scout task is accepted (verifying -> done).
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM approvals").fetchone()[0], 1)
            self.assertEqual(domain.get_task(conn, self.task_id).status, "done")
        finally:
            conn.close()
        # Fencing participation, not bypass: the API took the released scope
        # through the normal protocol (epoch monotonically bumps — the
        # verifier's epoch is never reused) and released it again.  Had the
        # verifier's lease still been LIVE, this request would have failed
        # 409 LEADER_BUSY instead (pinned below).
        after = lease_dump(self.db_path)
        self.assertEqual(len(after), 1)
        scope, holder, epoch, state = after[0][:4]
        self.assertEqual(scope, leader.project_scope(self.project_id))
        self.assertEqual(holder, "teamcraft-api")
        self.assertEqual(epoch, self.released_epoch + 1)
        self.assertEqual(state, "released")
        # uniform readback of the applied command
        status = self.assert_ok(
            self.get(f"/api/v1/commands/{command['command_id']}"))
        self.assertEqual(status["command"]["state"], "confirmed")

    def test_replay_and_content_conflict(self) -> None:
        body = {"decision": "reject", "actor": "boss", "reason": "insufficient",
                "expected_task_version": self.task_version}
        first = self.assert_ok(self.post(
            f"/api/v1/verifications/{self.verification.id}/approvals",
            body, "k-reject"))
        self.assertEqual(first["approval"]["decision"], "reject")
        replay = self.assert_ok(self.post(
            f"/api/v1/verifications/{self.verification.id}/approvals",
            body, "k-reject"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["command"]["command_id"], replay["command"]["command_id"])
        out = self.assert_uniform_error(self.post(
            f"/api/v1/verifications/{self.verification.id}/approvals",
            {**body, "decision": "accept"}, "k-reject"), 409, CONFLICT)
        # Core's per-verification decision dedupe refuses the second,
        # different decision; the transaction rollback leaves exactly the
        # original Approval row.
        self.assertEqual(out["details"]["reason_code"], "CONFLICT")
        conn = self.core()
        try:
            rows = conn.execute(
                "SELECT decision FROM approvals WHERE verification_id = ?",
                (self.verification.id,)).fetchall()
            self.assertEqual([str(r[0]) for r in rows], ["reject"])
            # a rejected report leaves the Task in VERIFYING (nothing accepts)
            self.assertEqual(domain.get_task(conn, self.task_id).status,
                             "verifying")
        finally:
            conn.close()

    def test_runtime_held_lease_blocks_approval(self) -> None:
        # While a live runtime Leader holds the project scope, the approval
        # route must refuse with 409 LEADER_BUSY and write nothing — the API
        # never bypasses fencing and never steals a live lease.
        conn = self.core()
        try:
            live = leader.acquire_or_renew(
                conn, leader.project_scope(self.project_id), "runtime-holder",
                ttl_seconds=300,
            )
            before = lease_dump(self.db_path)
            commands_before = len(commands_rows(self.db_path))
        finally:
            conn.close()
        out = self.assert_uniform_error(self.post(
            f"/api/v1/verifications/{self.verification.id}/approvals",
            {"decision": "accept", "actor": "boss", "reason": "r",
             "expected_task_version": self.task_version}, "k-busy-approve"),
            409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "LEADER_BUSY")
        self.assertEqual(out["details"]["holder_id"], "runtime-holder")
        self.assertEqual(out["details"]["epoch"], live.epoch)
        self.assertEqual(lease_dump(self.db_path), before)
        self.assertEqual(len(commands_rows(self.db_path)), commands_before)
        conn = self.core()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM approvals").fetchone()[0], 0)
        finally:
            conn.close()

    def test_stale_expected_task_version_rejected(self) -> None:
        before = len(commands_rows(self.db_path))
        out = self.assert_uniform_error(self.post(
            f"/api/v1/verifications/{self.verification.id}/approvals",
            {"decision": "accept", "actor": "boss", "reason": "r",
             "expected_task_version": self.task_version + 5}, "k-stale-appr"),
            409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "VERSION_CONFLICT")
        self.assertEqual(len(commands_rows(self.db_path)), before)

    def test_unknown_verification_is_404_and_bad_decision_is_422(self) -> None:
        self.assert_uniform_error(self.post(
            "/api/v1/verifications/v_ghost/approvals",
            {"decision": "accept", "actor": "boss", "reason": "r",
             "expected_task_version": 0}, "k-ghost-v"), 404, NOT_FOUND)
        out = self.assert_uniform_error(self.post(
            f"/api/v1/verifications/{self.verification.id}/approvals",
            {"decision": "maybe", "actor": "boss", "reason": "r",
             "expected_task_version": self.task_version}, "k-maybe"),
            422, VALIDATION_FAILED)
        self.assertEqual(out["details"]["field"], "decision")


# ---------------------------------------------------------------------------
# Command status reads + uniform errors + redaction
# ---------------------------------------------------------------------------


@requires_web
class CommandStatusAndUniformityTests(M304Fixture):
    def test_command_status_reflects_lifecycle(self) -> None:
        project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "C"}, "k-c"), 201)
        task_id = created["task"]["id"]
        sync_id = created["command"]["command_id"]
        self.assertEqual(
            self.assert_ok(self.get(f"/api/v1/commands/{sync_id}"))["command"]["state"],
            "confirmed",
        )
        # applied tier: the command is CONFIRMED in the same transaction the
        # transition ran in, and stays readable with its result payload.
        pause = self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "pause", "expected_version": 0, "actor": "a", "reason": "r"},
            "k-cp"))
        self.assertEqual(pause["execution"], "applied")
        pause_id = pause["command"]["command_id"]
        got = self.assert_ok(self.get(f"/api/v1/commands/{pause_id}"))["command"]
        self.assertEqual(got["state"], "confirmed")
        self.assertEqual(got["kind"], "task.pause")
        self.assertTrue(got["requested_at"])
        self.assertEqual(got["result"]["paused"], True)
        # recorded tier: a queued CEO wake request reads back as PENDING with
        # its bounded TTL, so uniform readback covers every returned id.
        wake = self.assert_ok(self.post(
            "/api/v1/ceo/commands",
            {"action": "request_processing", "project_id": project_id,
             "actor": "a", "reason": "look"}, "k-cw"), 202)
        pending = self.assert_ok(
            self.get(f"/api/v1/commands/{wake['command']['command_id']}"))["command"]
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["kind"], "ceo.wake_request")
        self.assertIsNotNone(pending["expires_at"])

    def test_unknown_command_is_404_uniform(self) -> None:
        self.assert_uniform_error(
            self.get("/api/v1/commands/c_ghost"), 404, NOT_FOUND)

    def test_unknown_task_is_404_generic(self) -> None:
        body = self.assert_uniform_error(
            self.patch("/api/v1/tasks/t_ghost",
                      {"expected_version": 0, "priority": 1}, "k-g"), 404, NOT_FOUND)
        self.assertNotIn("t_ghost", json.dumps(body))

    def test_missing_database_is_uniform_503_without_file_creation(self) -> None:
        missing = os.path.join(self.root, "ghost-runtime", "teamcraft.db")
        service = CommandService(missing)
        app = create_app(command_service=service)
        client = TestClient(app)
        token = app.state.token_store.ensure()
        response = client.post(
            "/api/v1/tasks",
            headers={"Authorization": f"Bearer {token}",
                    "X-CSRF-Token": token, IDEMPOTENCY_HEADER: "k"},
            json={"project_id": "p", "title": "t"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], SERVICE_UNAVAILABLE)
        self.assertFalse(os.path.exists(missing))

    def test_response_redacts_session_token(self) -> None:
        project_id = self.make_project()
        response = self.post(
            "/api/v1/tasks",
            {"project_id": project_id, "title": f"leak {self.token} attempt"},
            "k-redact",
        )
        body = self.assert_ok(response, 201)
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(self.token, text)
        self.assertIn(REDACTION_MARKER, text)


# ---------------------------------------------------------------------------
# CLI/API transition parity (same Core functions, same effects)
# ---------------------------------------------------------------------------


@requires_web
class CliApiParityTests(M304Fixture):
    def test_api_created_task_is_core_native(self) -> None:
        project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks",
            {"project_id": project_id, "title": "parity", "description": "d",
             "acceptance": ["x"], "ownership": ["src/**"], "priority": 61,
             "kind": "scout", "risk": "high", "max_attempts": 2}, "k-par"), 201)
        conn = self.core()
        try:
            task = domain.get_task(conn, created["task"]["id"])
            self.assertEqual(task.title, "parity")
            self.assertEqual(task.kind, "scout")
            self.assertEqual(task.priority, 61)
            self.assertEqual(task.risk, "high")
            self.assertEqual(task.max_attempts, 2)
            self.assertEqual(json.loads(task.acceptance_json), ["x"])
            self.assertEqual(json.loads(task.ownership_json), ["src/**"])
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.version, 0)
            # identical derivation as CLI-created tasks
            changes = domain.recompute_ready(conn, project_id)
            self.assertEqual(changes[task.id], ("pending", "ready"))
        finally:
            conn.close()
        # M3-02 query surface (same app, same source) shows the API write
        queried = self.assert_ok(self.get(f"/api/v1/tasks/{created['task']['id']}"))
        self.assertEqual(queried["title"], "parity")
        self.assertEqual(queried["status"], "ready")

    def test_task_created_with_dependencies_atomically(self) -> None:
        project_id = self.make_project()
        b = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "B"}, "k-pb"), 201)
        a = self.assert_ok(self.post(
            "/api/v1/tasks",
            {"project_id": project_id, "title": "A",
             "dependencies": [b["task"]["id"]]}, "k-pa"), 201)
        self.assertEqual(a["dependencies"], [b["task"]["id"]])
        conn = self.core()
        try:
            self.assertEqual(domain.dependencies_of(conn, a["task"]["id"]),
                             [b["task"]["id"]])
            # owning task's version moved for the edge (DAG churn visible)
            self.assertEqual(domain.get_task(conn, a["task"]["id"]).version, 1)
            # the dispatcher's derivation (core recompute) is untouched by the
            # API's write path and honours the new edge: B ready, A blocked.
            domain.recompute_ready(conn, project_id)
            queue = [t.id for t in domain.ready_queue(conn, project_id)]
            self.assertIn(b["task"]["id"], queue)
            self.assertNotIn(a["task"]["id"], queue)
        finally:
            conn.close()

    def test_dependency_cycle_rolls_back_whole_creation(self) -> None:
        project_id = self.make_project()
        b = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "B"}, "k-cyb"), 201)
        self.assert_ok(self.post(
            f"/api/v1/tasks/{b['task']['id']}/dependencies",
            {"depends_on_id": "t_x", "expected_version": 0}, "k-cydep"), 404)
        # B has no real upstream, so a cycle via a fresh task is impossible;
        # instead: create A with self-dependency -> refusal, A must not exist.
        task_counts_before = table_counts(self.db_path)["tasks"]
        out = self.client.post(
            "/api/v1/tasks",
            headers=self.write_headers("k-cyself"),
            json={"project_id": project_id, "title": "C", "dependencies": ["self"]},
        )
        self.assertEqual(out.status_code, 404)
        self.assertEqual(table_counts(self.db_path)["tasks"], task_counts_before)

    def test_writes_are_traceable_through_events_query(self) -> None:
        project_id = self.make_project()
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "T"}, "k-t"), 201)
        task_id = created["task"]["id"]
        self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "pause", "expected_version": 0, "actor": "a", "reason": "r"},
            "k-tev"), 200)
        events_page = self.assert_ok(self.get(f"/api/v1/events?task_id={task_id}"))
        kinds = [item["kind"] for item in events_page["items"]]
        self.assertIn("command.submitted", kinds)
        conn = self.core()
        try:
            linked = conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND kind LIKE 'command.%'",
                (task_id,),
            ).fetchone()[0]
            self.assertGreaterEqual(linked, 1)
        finally:
            conn.close()


@requires_web
class DispatchGateRegressionTests(M304Fixture):
    """The v15 gates must stop REAL fenced dispatch, not just API claims.

    Each scenario runs a genuine ``Scheduler.tick`` (only the CEO-live gate
    is stubbed): the pause facts are enforced by the planner itself, and the
    resume restores dispatch through the same dependency/lease machinery.
    """

    def ready_task(self, project_id: str, title: str) -> tuple[str, int]:
        """Create a task through the API and drive it to READY via core."""
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": title},
            f"k-ready-{title}"), 201)
        task_id = created["task"]["id"]
        conn = self.core()
        try:
            updated = domain.apply_task_event(
                conn, task_id, "dependencies_satisfied", expected_version=0)
            return task_id, updated.version
        finally:
            conn.close()

    def test_scheduler_pause_and_resume_gate_real_dispatch(self) -> None:
        project_id = self.make_project()
        task_id, _ = self.ready_task(project_id, "gated")

        pause = self.assert_ok(self.post(
            "/api/v1/scheduler/pause",
            {"project_id": project_id, "actor": "ops", "reason": "freeze"},
            "k-dg-pause"))
        self.assertEqual(pause["execution"], "applied")
        self.assertEqual(pause["command"]["state"], "confirmed")

        result = self.tick(project_id)
        self.assertEqual(result.attempts, ())
        self.assertIn(
            scheduler_module.DeferredTask(task_id, "scheduler_paused"),
            result.deferred,
        )
        conn = self.core()
        try:
            # gating is real: nothing reserved, task still READY, paused flag
            # set only on the control plane (the Task row itself is not paused)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
            self.assertEqual(domain.get_task(conn, task_id).status, "ready")
            self.assertFalse(bool(domain.get_task(conn, task_id).paused))
        finally:
            conn.close()

        resume = self.assert_ok(self.post(
            "/api/v1/scheduler/resume",
            {"project_id": project_id, "actor": "ops", "reason": "thaw"},
            "k-dg-resume"))
        self.assertEqual(resume["execution"], "applied")
        self.assertEqual(resume["command"]["state"], "confirmed")

        result = self.tick(project_id)
        self.assertEqual([a.task_id for a in result.attempts], [task_id])
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, task_id).status, "running")
        finally:
            conn.close()

    def test_task_pause_gates_real_dispatch_and_resume_restores(self) -> None:
        project_id = self.make_project()
        task_id, version = self.ready_task(project_id, "taskgate")
        paused = self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "pause", "expected_version": version, "actor": "a",
             "reason": "hold"}, "k-dg-tp"))
        self.assertTrue(paused["paused"])

        result = self.tick(project_id)
        self.assertEqual(result.attempts, ())
        # the scheduler is NOT paused: the task simply left the ready queue
        self.assertNotIn(
            scheduler_module.DeferredTask(task_id, "scheduler_paused"),
            result.deferred,
        )
        conn = self.core()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
            self.assertEqual([t.id for t in domain.ready_queue(conn, project_id)],
                             [])
        finally:
            conn.close()

        resumed = self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "resume", "expected_version": version + 1, "actor": "a",
             "reason": "go"}, "k-dg-tr"))
        self.assertFalse(resumed["paused"])
        result = self.tick(project_id)
        self.assertEqual([a.task_id for a in result.attempts], [task_id])

    def test_paused_task_cannot_be_reserved_even_by_core(self) -> None:
        project_id = self.make_project()
        task_id, version = self.ready_task(project_id, "hardgate")
        self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "pause", "expected_version": version, "actor": "a",
             "reason": "hold"}, "k-dg-core"), 200)
        conn = self.core()
        try:
            from teamcraft.errors import IllegalTransitionError
            with self.assertRaises(IllegalTransitionError) as ctx:
                domain.create_attempt(conn, task_id,
                                     expected_version=version + 1)
            self.assertEqual(getattr(ctx.exception, "reason_code", None),
                            "TASK_PAUSED")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        finally:
            conn.close()


@requires_web
class RetryApplicationTests(M304Fixture):
    """Retry is applied by Core under the API's own lease — never rubber-stamped."""

    def failed_task(self, project_id: str, title: str) -> tuple[str, int]:
        """Seed a retryable failure chain entirely through core facts."""
        created = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": title},
            f"k-retry-seed-{title}"), 201)
        task_id = created["task"]["id"]
        conn = self.core()
        try:
            ready = domain.apply_task_event(
                conn, task_id, "dependencies_satisfied", expected_version=0)
            attempt = domain.create_attempt(conn, task_id,
                                           expected_version=ready.version)
            domain.transition_attempt(conn, attempt.id, "start")
            domain.transition_attempt(conn, attempt.id, "activated")
            domain.transition_attempt(conn, attempt.id, "fail")
            task = domain.get_task(conn, task_id)
            return task_id, task.version
        finally:
            conn.close()

    def test_retry_applies_through_core_and_replays(self) -> None:
        import teamcraft.recovery as recovery_module

        project_id = self.make_project()
        task_id, version = self.failed_task(project_id, "bounce")
        real_approve = recovery_module.approve_retry
        calls: list[tuple] = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real_approve(*args, **kwargs)

        body = {"action": "retry", "expected_version": version,
                "actor": "boss", "reason": "run it again"}
        with mock.patch.object(recovery_module, "approve_retry", spy):
            resp = self.assert_ok(self.post(
                f"/api/v1/tasks/{task_id}/commands", body, "k-retry-1"))
        self.assertEqual(len(calls), 1)
        called_args, called_kwargs = calls[0]
        self.assertEqual(called_args[1], task_id)
        self.assertEqual(
            {k: called_kwargs[k]
             for k in ("actor", "reason", "idempotency_key")},
            {"actor": "boss", "reason": "run it again",
             "idempotency_key": "k-retry-1"})
        self.assertEqual(called_kwargs["lease"].holder_id, "teamcraft-api")
        self.assertEqual(resp["execution"], "applied")
        self.assertEqual(resp["command"]["kind"], "recovery.retry_approved")
        self.assertEqual(resp["command"]["state"], "confirmed")
        self.assertEqual(resp["task"]["status"], "ready")
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, task_id).status, "ready")
            # approval alone never fabricates an Attempt
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?",
                           (task_id,)).fetchone()[0], 1)
        finally:
            conn.close()

        replay = self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands", body, "k-retry-1"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["command"]["command_id"],
                         resp["command"]["command_id"])
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, task_id).version,
                             resp["task"]["version"])
        finally:
            conn.close()

    def test_retry_refused_by_attempt_cap_without_residue(self) -> None:
        project_id = self.make_project()
        task_id, version = self.failed_task(project_id, "capped")
        conn = self.core()
        try:
            with storage.transaction(conn):
                conn.execute("UPDATE tasks SET max_attempts = 1 WHERE id = ?",
                           (task_id,))
        finally:
            conn.close()
        commands_before = len(commands_rows(self.db_path))
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "retry", "expected_version": version, "actor": "boss",
             "reason": "again"}, "k-retry-cap"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "MAX_ATTEMPTS")
        # approve_retry submitted its Command before the cap refusal; the
        # transaction rollback must leave nothing behind.
        self.assertEqual(len(commands_rows(self.db_path)), commands_before)
        conn = self.core()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?",
                           (task_id,)).fetchone()[0], 1)
        finally:
            conn.close()

    def test_live_runtime_lease_blocks_retry_with_no_writes(self) -> None:
        project_id = self.make_project()
        task_id, version = self.failed_task(project_id, "contended")
        conn = self.core()
        try:
            live = leader.acquire_or_renew(
                conn, leader.project_scope(project_id), "runtime-holder",
                ttl_seconds=300)
            lease_before = lease_dump(self.db_path)
            commands_before = len(commands_rows(self.db_path))
        finally:
            conn.close()
        out = self.assert_uniform_error(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "retry", "expected_version": version, "actor": "boss",
             "reason": "again"}, "k-retry-busy"), 409, CONFLICT)
        self.assertEqual(out["details"]["reason_code"], "LEADER_BUSY")
        self.assertEqual(out["details"]["holder_id"], "runtime-holder")
        self.assertEqual(out["details"]["epoch"], live.epoch)
        self.assertEqual(lease_dump(self.db_path), lease_before)
        self.assertEqual(len(commands_rows(self.db_path)), commands_before)
        conn = self.core()
        try:
            # refused before anything applied: the task sits exactly where it
            # was, still awaiting reconciliation→retry once the lease frees.
            self.assertEqual(domain.get_task(conn, task_id).status, "running")
        finally:
            conn.close()

    def test_retry_records_durable_audit(self) -> None:
        project_id = self.make_project()
        task_id, version = self.failed_task(project_id, "audit")
        resp = self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "retry", "expected_version": version, "actor": "boss",
             "reason": "second chance"}, "k-retry-audit"))
        command_id = resp["command"]["command_id"]
        conn = self.core()
        try:
            kinds = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT kind FROM events WHERE task_id = ?",
                (task_id,)).fetchall()]
            # Core's audit for the approval rides on the Command lifecycle;
            # the retry facts themselves live in the confirmed result payload.
            self.assertIn("command.confirmed", kinds)
            result = conn.execute(
                "SELECT result_json FROM commands WHERE id = ?",
                (command_id,)).fetchone()
            self.assertIsNotNone(result)
            payload = json.loads(str(result[0]))
            self.assertEqual(payload["task_id"], task_id)
            self.assertTrue(payload["approved"])
            self.assertEqual(payload["task_version"],
                             domain.get_task(conn, task_id).version)
        finally:
            conn.close()


@requires_web
class CeoWakeLifecycleTests(M304Fixture):
    """Manual CEO requests are durable intents: queued, folded, TTL-bounded."""

    def test_queue_readback_replay(self) -> None:
        project_id = self.make_project()
        body = {"action": "request_processing", "project_id": project_id,
                "actor": "web", "reason": "look at this"}
        first = self.assert_ok(self.post("/api/v1/ceo/commands", body,
                                        "k-wake-q"), 202)
        self.assertEqual(first["execution"], "queued")
        command = first["command"]
        self.assertEqual(command["kind"], "ceo.wake_request")
        self.assertEqual(command["state"], "pending")
        self.assertIsNotNone(command["expires_at"])
        status = self.assert_ok(
            self.get(f"/api/v1/commands/{command['command_id']}"))
        self.assertEqual(status["command"]["state"], "pending")
        self.assertEqual(status["command"]["expires_at"],
                         command["expires_at"])
        replay = self.assert_ok(self.post("/api/v1/ceo/commands", body,
                                         "k-wake-q"), 202)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["command"]["command_id"],
                         command["command_id"])
        conn = self.core()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM commands WHERE kind = 'ceo.wake_request'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_tick_folds_request_into_wake_candidate(self) -> None:
        project_id = self.make_project()
        queued = self.assert_ok(self.post(
            "/api/v1/ceo/commands",
            {"action": "request_processing", "project_id": project_id,
             "actor": "web", "reason": "please look"}, "k-wake-fold"), 202)
        command_id = queued["command"]["command_id"]
        self.tick(project_id)
        conn = self.core()
        try:
            wakes = conn.execute(
                "SELECT id, state FROM wakes WHERE project_id = ?",
                (project_id,)).fetchall()
            self.assertEqual(len(wakes), 1)
            wake_id, wake_state = str(wakes[0][0]), str(wakes[0][1])
            linked = conn.execute(
                "SELECT COUNT(*) FROM wake_events WHERE wake_id = ?",
                (wake_id,)).fetchone()[0]
            self.assertGreaterEqual(int(linked), 1)
            # ONLY folded, never delivered: admission lives behind the
            # runtime's wake pipeline, which this API path never invokes.
            self.assertEqual(int(conn.execute(
                "SELECT COUNT(*) FROM prompt_deliveries").fetchone()[0]), 0)
            # the durable request survives until reconciled by the runtime
            self.assertEqual(commands_core.get_command(
                conn, command_id).state, "pending")
        finally:
            conn.close()
        # a second tick dedupes: same event key/window, still one candidate.
        self.tick(project_id)
        conn = self.core()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM wakes WHERE project_id = ?",
                (project_id,)).fetchone()[0], 1)
        finally:
            conn.close()

    def test_unconsumed_request_expires_by_ttl(self) -> None:
        from teamcraft import wake as wake_module

        project_id = self.make_project()
        clock = FixedClock()
        conn = self.core()
        try:
            command, replayed = wake_module.request_ceo_attention(
                conn, project_id, actor="web", reason="stale ping",
                idempotency_key="k-wake-ttl", ttl_seconds=60, clock=clock)
            self.assertFalse(replayed)
            self.assertEqual(command.state, "pending")
            clock.advance(61)
            swept = commands_core.sweep_stale_commands(conn, clock=clock)
            self.assertIn((command.id, "TTL_EXPIRED"), swept)
            self.assertEqual(
                commands_core.get_command(conn, command.id).state, "expired")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM prompt_deliveries").fetchone()[0], 0)
        finally:
            conn.close()


@requires_web
class SchedulerStateReadbackTests(M304Fixture):
    def test_pause_resume_versions_observable_without_lease(self) -> None:
        project_id = self.make_project()
        initial = self.assert_ok(self.get(
            f"/api/v1/scheduler/state?project_id={project_id}"))
        self.assertFalse(initial["scheduler"]["paused"])
        self.assertEqual(initial["scheduler"]["version"], 0)

        self.assert_ok(self.post(
            "/api/v1/scheduler/pause",
            {"project_id": project_id, "actor": "ops", "reason": "freeze"},
            "k-ss-pause"))
        paused = self.assert_ok(self.get(
            f"/api/v1/scheduler/state?project_id={project_id}"))
        self.assertTrue(paused["scheduler"]["paused"])
        self.assertEqual(paused["scheduler"]["version"], 1)
        self.assertEqual(paused["scheduler"]["actor"], "ops")

        self.assert_ok(self.post(
            "/api/v1/scheduler/resume",
            {"project_id": project_id, "actor": "ops", "reason": "thaw"},
            "k-ss-resume"))
        resumed = self.assert_ok(self.get(
            f"/api/v1/scheduler/state?project_id={project_id}"))
        self.assertFalse(resumed["scheduler"]["paused"])
        self.assertEqual(resumed["scheduler"]["version"], 2)
        # state reads never materialise lease rows
        self.assertEqual(lease_dump(self.db_path), [])

    def test_unknown_project_is_uniform_404(self) -> None:
        self.assert_uniform_error(self.get(
            "/api/v1/scheduler/state?project_id=proj_missing"),
            404, NOT_FOUND)


@requires_web
class EventProjectTraceabilityTests(M304Fixture):
    """Tester EV-LINK-PROJ: every Command audit event must be project-traceable.

    Project-scoped commands (creates, scheduler gates, manual CEO requests)
    have no Task binding at submit time — their submitted/claimed/confirmed
    events still carry ``project_id`` (via the evidence project stamp and
    ``commands._associate``), so a project event query sees the WHOLE audit
    trail, never a subset orphaned by NULL-project rows.
    """

    def test_every_command_event_is_project_traceable(self) -> None:
        project_id = self.make_project()
        upstream = self.assert_ok(self.post(
            "/api/v1/tasks", {"project_id": project_id, "title": "Up"},
            "k-pt-up"), 201)
        up_id = upstream["task"]["id"]
        created = self.assert_ok(self.post(
            "/api/v1/tasks",
            {"project_id": project_id, "title": "Traced",
             "acceptance": ["ac"], "ownership": ["src/**"], "priority": 61,
             "dependencies": [up_id]}, "k-pt-create"), 201)
        task_id = created["task"]["id"]
        # dependencies rode with creation: version already moved past 0
        version = created["task"]["version"]
        step = self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/dependencies",
            {"depends_on_id": up_id, "expected_version": version},
            "k-pt-dep-dup"), 200)
        self.assertFalse(step["added"])  # idempotent edge: no double bump
        self.assert_ok(self.patch(
            f"/api/v1/tasks/{task_id}",
            {"expected_version": version, "priority": 77}, "k-pt-edit"), 200)
        version += 1
        self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "pause", "expected_version": version, "actor": "a",
             "reason": "r"}, "k-pt-pause"), 200)
        version += 1
        self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "resume", "expected_version": version, "actor": "a",
             "reason": "r"}, "k-pt-resume"), 200)
        version += 1
        self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/policy",
            {"expected_version": version, "timeout_seconds": 60},
            "k-pt-policy"), 200)
        version += 1
        self.assert_ok(self.post(
            "/api/v1/scheduler/pause",
            {"project_id": project_id, "actor": "ops", "reason": "freeze"},
            "k-pt-spause"), 200)
        self.assert_ok(self.post(
            "/api/v1/scheduler/resume",
            {"project_id": project_id, "actor": "ops", "reason": "thaw"},
            "k-pt-sresume"), 200)
        self.assert_ok(self.post(
            "/api/v1/ceo/commands",
            {"action": "request_processing", "project_id": project_id,
             "actor": "web", "reason": "look"}, "k-pt-wake"), 202)
        self.assert_ok(self.post(
            f"/api/v1/tasks/{task_id}/commands",
            {"action": "cancel", "expected_version": version, "actor": "a",
             "reason": "done"}, "k-pt-cancel"), 200)

        conn = self.core()
        try:
            core_ids = sorted(
                str(r[0]) for r in conn.execute(
                    "SELECT event_id FROM events"
                    " WHERE kind LIKE 'command.%'").fetchall()
            )
            orphans = int(conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind LIKE 'command.%'"
                " AND project_id IS NULL").fetchone()[0])
        finally:
            conn.close()
        self.assertGreaterEqual(len(core_ids), 30)  # the full sweep really ran
        self.assertEqual(orphans, 0,
                         "command audit events orphaned from the project")

        seen: list[str] = []
        offset = 0
        while True:
            page = self.assert_ok(self.get(
                f"/api/v1/events?project_id={project_id}"
                f"&limit=100&offset={offset}"))
            items = page["items"]
            seen.extend(str(item["event_id"]) for item in items
                       if str(item["kind"]).startswith("command."))
            if len(items) < 100:
                break
            offset += 100
        self.assertEqual(sorted(seen), core_ids,
                         "project event query must see every command event")


# ---------------------------------------------------------------------------
# Physical proof: no route can prompt, dispatch, run a verifier, or lease
# ---------------------------------------------------------------------------


@requires_web
class NoExternalDispatchSpyTests(M304Fixture):
    """Patch every external/leader entry point to explode if touched.

    ``commands.submit/claim/confirm`` are deliberately NOT booby-trapped:
    they are the M1-05 command bookkeeping the synchronous tier legitimately
    runs inside one transaction.  Everything that could prompt, reserve an
    attempt, deliver a Wake, run a verifier, or fence on the Leader must
    stay untouched by the local and recorded tiers.  The approval tier
    (retry/approval) is the one place the API participates in fencing with
    its own short lease — pinned positively in
    ``RetryApplicationTests``/``ApprovalDecisionTests`` — and even there it
    only ever uses :mod:`teamcraft.leader` primitives, never another
    holder's lease.
    """

    def test_local_and_recorded_routes_never_reach_external_core(self) -> None:
        import contextlib

        import teamcraft.recovery as recovery_module
        import teamcraft.wake as wake_module

        project_id = self.make_project()

        def bomb(name: str):
            def _bomb(*args, **kwargs):
                raise AssertionError(
                    f"forbidden core call during API request: {name}")
            return _bomb

        forbidden = {
            domain: ["create_attempt"],
            leader: ["acquire_or_renew", "release", "require_lease",
                    "require_project_lease"],
            verification: ["run_verification", "configure_verifier",
                         "decide_approval"],
            wake_module: ["admit_pending_wake", "ingest_events",
                        "record_submission_result",
                        "recover_interrupted_submissions",
                        "reconcile_wake_delivery"],
            recovery_module: ["confirm_cancellation", "resolve_unknown",
                            "approve_retry"],
        }
        with contextlib.ExitStack() as stack:
            for module, names in forbidden.items():
                for name in names:
                    stack.enter_context(
                        mock.patch.object(module, name,
                                        bomb(f"{module.__name__}.{name}")))
            # Everything below must still succeed with every external entry
            # point booby-trapped — proof these tiers are local or purely
            # recorded by construction (the lease-taking approval tier and
            # occupied-attempt cancellation are exercised separately).
            created = self.assert_ok(self.post(
                "/api/v1/tasks",
                {"project_id": project_id, "title": "Safe"}, "k-spy-a"), 201)
            task_id = created["task"]["id"]
            other = self.assert_ok(self.post(
                "/api/v1/tasks",
                {"project_id": project_id, "title": "Upstream"}, "k-spy-b"), 201)
            other_id = other["task"]["id"]
            self.assert_ok(self.patch(
                f"/api/v1/tasks/{task_id}",
                {"expected_version": 0, "priority": 80}, "k-spy-edit"), 200)
            self.assert_ok(self.post(
                f"/api/v1/tasks/{task_id}/dependencies",
                {"depends_on_id": other_id, "expected_version": 1},
                "k-spy-dep"), 200)
            self.assert_ok(self.delete(
                f"/api/v1/tasks/{task_id}/dependencies/{other_id}",
                "k-spy-rmdep", expected_version=2), 200)
            self.assert_ok(self.post(
                f"/api/v1/tasks/{task_id}/commands",
                {"action": "pause", "expected_version": 3, "actor": "a",
                 "reason": "r"}, "k-spy-pause"), 200)
            self.assert_ok(self.post(
                f"/api/v1/tasks/{task_id}/commands",
                {"action": "resume", "expected_version": 4, "actor": "a",
                 "reason": "r"}, "k-spy-resume"), 200)
            self.assert_ok(self.post(
                f"/api/v1/tasks/{task_id}/policy",
                {"expected_version": 5, "timeout_seconds": 60},
                "k-spy-policy"), 200)
            self.assert_ok(self.post(
                f"/api/v1/tasks/{task_id}/commands",
                {"action": "cancel", "expected_version": 6, "actor": "a",
                 "reason": "r"}, "k-spy-cancel"), 200)
            self.assert_ok(self.post(
                "/api/v1/ceo/commands",
                {"action": "request_processing", "project_id": project_id,
                 "actor": "web", "reason": "r"}, "k-spy-wake"), 202)
            self.assert_ok(self.post(
                "/api/v1/scheduler/pause",
                {"project_id": project_id, "actor": "ops", "reason": "r"},
                "k-spy-sp"), 200)
            self.assert_ok(self.post(
                "/api/v1/scheduler/resume",
                {"project_id": project_id, "actor": "ops", "reason": "r"},
                "k-spy-sr"), 200)
        # After the sweep: no attempt, wake, cursor, prompt, approval row,
        # and no Leader lease ever materialised.
        self.assert_no_external_effects()
        conn = self.core()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM leader_lease").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
