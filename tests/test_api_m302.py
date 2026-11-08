"""M3-02 Query API and snapshot tests.

Covers the acceptance line from doc/TEAMCRAFT_2_0_M0_REVIEW.md (M3-02):

* API data is same-source as the CLI/core (ordering and values mirror
  ``domain``/``events``/``wake``/``verification``/``runtime_adapter`` reads);
* read requests change nothing — no row, no event, no command, no wake,
  no dispatch (content-digest invariant plus physical ``mode=ro`` proof);
* Session/Attempt/Task, the three progress classes, and the Wake lifecycle
  stay distinct; unknown/missing token usage stays null, never zero;
* bounded pagination/filtering; malformed filters and limits fail uniformly;
* log/artifact text is truncation-bounded and secret-redacted; database
  paths, token material, and raw host-identity payloads never leak.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

try:  # the API needs the web stack; core tests must stay runnable without it
    from fastapi.testclient import TestClient

    from teamcraft.api import create_app
    from teamcraft.api.errors import (
        NOT_FOUND,
        SERVICE_UNAVAILABLE,
        VALIDATION_FAILED,
    )
    from teamcraft.api.redaction import REDACTION_MARKER
    from teamcraft.query import (
        DEFAULT_PAGE_SIZE,
        MAX_PAGE_SIZE,
        MAX_PREVIEW_BYTES,
        MAX_TEXT_BYTES,
        USAGE_FIELDS,
        QueryService,
        QueryUnavailableError,
    )

    _WEB_STACK_OK = True
    _WEB_STACK_REASON = ""
except ImportError as exc:  # pragma: no cover - depends on CI install order
    _WEB_STACK_OK = False
    _WEB_STACK_REASON = f"web dependencies unavailable: {exc}"

from teamcraft import (
    briefs,
    domain,
    events,
    fresh_start,
    leader,
    runtime_adapter,
    storage,
    wake,
    worker_reports,
)
from teamcraft import recovery as recovery_core

requires_web = unittest.skipUnless(_WEB_STACK_OK, _WEB_STACK_REASON)

ERROR_KEYS = {"code", "message", "details", "request_id"}
_HEX64 = "a" * 64

#: Absolute path fragments that must never appear in any response body.
SOCKET_FRAGMENT = "/var/run/herdr-unit-test.sock"
DB_FRAGMENTS = ("teamcraft.db", "/.teamcraft/")

#: Canonical query endpoints with no path parameters (auth sweep list).
_SIMPLE_PATHS = (
    "/api/v1/projects",
    "/api/v1/tasks/t_missing/attempts",
    "/api/v1/attempts/a_missing",
    "/api/v1/sessions/s_missing",
    "/api/v1/agents",
    "/api/v1/members",
    "/api/v1/events",
    "/api/v1/approvals",
    "/api/v1/recovery-actions",
    "/api/v1/verifications",
    "/api/v1/verification-configs",
)

_CONTENT_TABLES = (
    "projects",
    "agents",
    "tasks",
    "attempts",
    "sessions",
    "task_dependencies",
    "events",
    "commands",
    "leader_lease",
    "runtime_meta",
    "members",
    "attempt_briefs",
    "worker_reports",
    "prompt_deliveries",
    "verification_configs",
    "verifications",
    "approvals",
    "ceo_initializations",
    "wake_cursors",
    "wakes",
    "wake_events",
)


class CapturingHandler(logging.Handler):
    """Collect formatted log records so tests can assert on log text."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # pragma: no cover - formatting must never crash
            self.lines.append(f"<unformattable {record.levelname}>")


@contextlib.contextmanager
def capture_logs(level: int = logging.DEBUG):
    handler = CapturingHandler()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(min(previous_level or logging.WARNING, level))
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def iter_routes(app):
    """Flatten FastAPI routes, descending into included-router wrappers."""

    for route in app.routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from iter_routes(original)
        else:
            yield route


def db_state_digest(db_path: str) -> str:
    """SHA-256 over every durable row of every content table."""

    conn = storage.connect(db_path)
    try:
        digest = hashlib.sha256()
        for table in _CONTENT_TABLES:
            digest.update(table.encode("utf-8"))
            digest.update(b"\x00")
            for row in conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid"):
                digest.update(repr(row).encode("utf-8"))
                digest.update(b"\n")
        return digest.hexdigest()
    finally:
        conn.close()


def table_counts(db_path: str) -> dict[str, int]:
    conn = storage.connect(db_path)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _CONTENT_TABLES
        }
    finally:
        conn.close()


def _iso(seconds: int) -> str:
    from datetime import datetime, timezone

    return datetime(2026, 11, 8, tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixture: initialized project root with a fully-populated core state
# ---------------------------------------------------------------------------


class M302Fixture(unittest.TestCase):
    """Base fixture: temp project root, initialized DB, guarded app."""

    populate = False

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="tc-m302-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(
            self.root, ".teamcraft", "teamcraft.db"
        )
        self.service = QueryService(self.db_path)
        self.app = create_app(query_service=self.service)
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()

        # Seed data is prepared BEFORE the app serves; it embeds the live
        # token so redaction can be asserted against real seeded history.
        self.secret_sentinel = f"seeded-secret-{os.urandom(6).hex()}"
        self.fixture: dict[str, object] = {}
        if self.populate:
            self.fixture = populate_database(self.db_path, self.root, self.token)

    # -- helpers -------------------------------------------------------------

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def get(self, path: str, token: str | None = None, **params) -> object:
        query = "&".join(f"{key}={quote(str(value))}" for key, value in params.items())
        url = f"{path}?{query}" if query else path
        return self.client.get(url, headers=self.auth(token))

    def json_of(self, response) -> dict:
        return response.json()

    def assert_uniform_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status)
        body = self.json_of(response)
        self.assertEqual(set(body.keys()), ERROR_KEYS)
        self.assertEqual(body["code"], code)
        self.assertTrue(body["request_id"])
        return body

    def assert_ok(self, response) -> dict:
        self.assertEqual(response.status_code, 200, self.json_of(response))
        return self.json_of(response)

    def rw(self):
        return storage.connect(self.db_path)


# ---------------------------------------------------------------------------
# Populating the core (CLI-side writes; the API itself never writes)
# ---------------------------------------------------------------------------


def populate_database(db_path: str, root: str, token: str) -> dict:
    """Create one rich, deterministic core state through the core APIs."""

    conn = storage.connect(db_path)
    fx: dict[str, object] = {}
    project = domain.create_project(conn, "alpha", root)
    fx["project"] = project

    # Task wiring -------------------------------------------------------------
    task_ship = domain.create_task(
        conn,
        project.id,
        "Ship M3-02 query surface",
        description="long " * 2000,  # exceeds MAX_TEXT_BYTES for truncation
        kind="ship",
        priority=80,
        acceptance_json='["all GET routes are read-only","redaction verified"]',
        ownership_json='["teamcraft/query.py","teamcraft/api/**"]',
        risk="normal",
        max_attempts=3,
    )
    task_scout = domain.create_task(
        conn, project.id, "Scout repo layout", kind="scout", priority=40
    )
    task_down = domain.create_task(
        conn, project.id, "Downstream wiring", kind="ship", priority=50
    )
    domain.recompute_ready(conn, project.id)
    domain.add_dependency(
        conn,
        task_down.id,
        task_ship.id,
        expected_version=domain.get_task(conn, task_down.id).version,
    )
    domain.recompute_ready(conn, project.id)
    fx.update(task_ship=task_ship, task_scout=task_scout, task_down=task_down)

    # Agents -------------------------------------------------------------------
    with storage.transaction(conn):
        conn.execute(
            "INSERT INTO agents (id, role, harness, model, provider, config_json)"
            " VALUES ('agent_dev','developer','codex','omlx/qwen','omlx','{}')"
        )
        conn.execute(
            "INSERT INTO agents (id, role, harness, model, provider, config_json)"
            " VALUES ('agent_qa','tester','opencode',NULL,NULL,'{\"ask\":true}')"
        )

    # Ship attempt (active, with brief + progress report + session) ----------
    ship_task = domain.get_task(conn, task_ship.id)
    attempt_ship = domain.create_attempt(conn, task_ship.id, expected_version=ship_task.version)
    domain.transition_attempt(conn, attempt_ship.id, "start")
    domain.transition_attempt(conn, attempt_ship.id, "activated")
    session_ship = domain.register_session(
        conn,
        attempt_ship.id,
        "herdr",
        native_session_id="native-ship-1",
        pane_id="pane-ship",
        state="working",
    )
    brief = briefs.create_attempt_brief(
        conn,
        attempt_ship.id,
        content={"milestones": [{"id": "stage-1"}, {"id": "final"}]},
        task_version=ship_task.version + 1,
    )
    report = {
        "schema_version": 1,
        "task_id": task_ship.id,
        "attempt_id": attempt_ship.id,
        "task_version": brief.task_version,
        "brief_digest": brief.brief_digest,
        "report_kind": "progress",
        "outcome": None,
        "milestone_id": "stage-1",
        "summary": f"checkpoint reached {token}",
        "artifact_digest": None,
        "usage": {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_microunits": None,
        },
    }
    progress = worker_reports.record_worker_report(conn, report)
    fx.update(
        attempt_ship=attempt_ship,
        session_ship=session_ship,
        brief=brief,
        progress=progress,
    )

    # Scout attempt (UNKNOWN) + exited session --------------------------
    scout_task = domain.get_task(conn, task_scout.id)
    attempt_scout = domain.create_attempt(conn, task_scout.id, expected_version=scout_task.version)
    domain.transition_attempt(conn, attempt_scout.id, "start")
    domain.transition_attempt(conn, attempt_scout.id, "activated")
    domain.transition_attempt(conn, attempt_scout.id, "runtime_lost")
    session_scout = domain.register_session(
        conn, attempt_scout.id, "herdr", pane_id="pane-scout", state="working"
    )
    domain.observe_session(conn, session_scout.id, "exited")
    fx.update(attempt_scout=attempt_scout, session_scout=session_scout)

    # Persistent recovery Commands -----------------------------------------
    cancel = recovery_core.request_action(
        conn,
        task_id=task_scout.id,
        action="cancel",
        actor="operator",
        reason=f"halt ambiguous scout run {token}",
        idempotency_key="cancel-scout-1",
        attempt_id=attempt_scout.id,
    )
    fx["cancel_command"] = cancel

    # Events + Wakes -------------------------------------------------------
    lease = leader.acquire_or_renew(
        conn, leader.project_scope(project.id), "seed-leader", ttl_seconds=300
    )
    for index in range(2):
        events.append_event(
            conn,
            "watch.no_progress_threshold",
            project_id=project.id,
            task_id=task_ship.id,
            attempt_id=attempt_ship.id,
            payload_json=json.dumps({"dedupe_key": f"no-progress-{index}"}),
        )
    events.append_event(
        conn,
        "watch.session_observed",
        project_id=project.id,
        task_id=task_scout.id,
        attempt_id=attempt_scout.id,
        payload_json=json.dumps({"state": "exited", "note": f"seen {token}"}),
    )
    events.append_event(
        conn,
        "recovery.escalated",
        project_id=project.id,
        task_id=task_scout.id,
        attempt_id=attempt_scout.id,
        payload_json=json.dumps({"reason": "no progress past threshold"}),
    )
    ingest = wake.ingest_events(conn, project.id, lease, limit=100)
    fx.update(ingest=ingest, lease=lease)

    # Verification + Approval (durable records, seeded directly) ---------
    artifact = (
        f"verifier stdout line {token}\n".encode() + b"x" * (MAX_PREVIEW_BYTES * 2)
    )
    artifact_digest = hashlib.sha256(artifact).hexdigest()
    now = _iso(0)
    with storage.transaction(conn):
        conn.execute(
            "INSERT INTO commands (id, idempotency_key, kind, payload_json,"
            " evidence_json, state, requested_at, updated_at) VALUES"
            " ('cmd_verify','ik-verify','verification.run','{}','{}','confirmed',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO commands (id, idempotency_key, kind, payload_json,"
            " evidence_json, state, requested_at, updated_at) VALUES"
            " ('cmd_config','ik-config','verification.configure','{}','{}','confirmed',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO commands (id, idempotency_key, kind, payload_json,"
            " evidence_json, state, requested_at, updated_at) VALUES"
            " ('cmd_approve','ik-approve','verification.approve','{}','{}','confirmed',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO verification_configs (id, command_id, project_id,"
            " config_version, rules_version, argv_json, timeout_seconds,"
            " config_digest, approved_by, idempotency_key, created_at)"
            " VALUES ('vcfg_1','cmd_config',?,1,'rules-v1',"
            " '[\"python3\",\"-B\",\"-m\",\"pytest\",\"tests\"]',60,?,?,?,?)",
            (project.id, _HEX64, "owner", "ik-vcfg-1", now),
        )
        conn.execute(
            "INSERT INTO verifications (id, command_id, project_id, task_id,"
            " attempt_id, attempt_no, task_version, dispatch_task_version,"
            " brief_digest, worker_report_id, worker_report_digest,"
            " config_version, rules_version, config_digest, argv_json, status,"
            " exit_code, artifact, artifact_digest, created_at)"
            " VALUES ('ver_1','cmd_verify',?,?,?,?,?,?,?,?,?,1,'rules-v1',?,"
            " '[\"python3\",\"-B\",\"-m\",\"pytest\",\"tests\"]','pass',0,?,?,?)",
            (
                project.id,
                task_ship.id,
                attempt_ship.id,
                attempt_ship.attempt_no,
                brief.task_version,
                brief.task_version,
                brief.brief_digest,
                progress.id if hasattr(progress, "id") else 1,
                progress.report_digest,
                _HEX64,
                artifact,
                artifact_digest,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO approvals (id, command_id, verification_id, project_id,"
            " task_id, attempt_id, attempt_no, task_version, brief_digest,"
            " artifact_digest, config_version, rules_version, config_digest,"
            " proposed_action, decision, actor, reason, created_at)"
            " VALUES ('apr_1','cmd_approve','ver_1',?,?,?,?,?,?,?,1,'rules-v1',?,"
            " 'accept report-only delivery','accept','owner',?,?)",
            (
                project.id,
                task_ship.id,
                attempt_ship.id,
                attempt_ship.attempt_no,
                brief.task_version,
                brief.brief_digest,
                artifact_digest,
                _HEX64,
                f"artifact matches {token}",
                now,
            ),
        )

        # Runtime compat rows (members/team) mirror CLI state.
        conn.execute(
            "INSERT INTO runtime_meta (id, team_id, project_root, herdr_session_json,"
            " created_at) VALUES (1,'team-seed',?,?,?)",
            (root, json.dumps({"transport": "seed"}), 1762560000),
        )
        conn.execute(
            "INSERT INTO members (name, role, responsibility, kind, task,"
            " ownership_json, read_only, cwd, from_pane_id, pane_id, terminal_id,"
            " agent_session_json, agent_args_json, opencode_config_content,"
            " direction, start_timeout_ms, phase,"
            " prompt_may_have_been_delivered, live_state, error, task_id,"
            " attempt_id, session_id, created_at, updated_at)"
            " VALUES ('dev_a','developer','implement M3-02','codex',"
            " 'ship the query surface','[\"teamcraft/query.py\"]',0,?,NULL,"
            " 'pane-ship','term-ship','{\"session_id\": \"native-ship-1\"}',"
            " '[]','{\"default_agent\": \"herdr-developer\"}','right',30000,"
            " 'prompt_submitted',1,'idle',NULL,?,?,?,1762560000,1762560001)",
            (root, task_ship.id, attempt_ship.id, session_ship.id),
        )

        # Prompt delivery record (submitted is NOT acknowledged).
        conn.execute(
            "INSERT INTO commands (id, idempotency_key, kind, payload_json,"
            " evidence_json, state, requested_at, updated_at) VALUES"
            " ('cmd_prompt','ik-prompt','prompt.send','{}','{}','running',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO prompt_deliveries (command_id, attempt_id, session_id,"
            " task_version, brief_version, brief_digest, evidence_version, state,"
            " updated_at, note) VALUES (?,?,?,?,?,?,?, 'submitted', ?, ?)",
            (
                "cmd_prompt",
                attempt_ship.id,
                session_ship.id,
                brief.task_version,
                brief.brief_version,
                brief.brief_digest,
                brief.task_version,
                now,
                f"delivered {token}",
            ),
        )

        # CEO initialization under the explicit dev assumption.
        conn.execute(
            "INSERT INTO ceo_initializations (project_id, canonical_ceo_name,"
            " state, team_id, teamcraft_session_id, herdr_session_identity_json,"
            " native_session_id, terminal_id, protocol_version, protocol_hash,"
            " runtime_skill_hash, snapshot_digest, nonce_hash, nonce_expires_at,"
            " nonce_consumed_at, audit_material_json, detail, receipt_source,"
            " identity_mode, live_process_identity_json, created_at, updated_at)"
            " VALUES (?, 'ceo', 'initialized', 'team-seed', ?,?,"
            " 'native-ceo-1','term-ceo','1.0.0',?,?,?,?,"
            " NULL,NULL,NULL,?, 'assumed_default','native',NULL,?,?)",
            (
                project.id,
                session_ship.id,
                json.dumps(
                    {
                        "socket_path": SOCKET_FRAGMENT,
                        "device": 16777220,
                        "inode": 4242,
                    }
                ),
                _HEX64,
                _HEX64,
                _HEX64,
                _HEX64,
                f"protocol read; token inline {token}",
                now,
                now,
            ),
        )

    # CEO protocol artifacts under the project root (M1-08 layout).
    skill_root = os.path.join(root, ".agents", "skills", "teamcraft-ceo")
    references = os.path.join(skill_root, "references")
    os.makedirs(references, exist_ok=True)
    protocol_bytes = b"---\nprotocol_version: 1.0.0\n---\n# CEO Protocol\nbody\n"
    with open(os.path.join(references, "CEO_PROTOCOL.md"), "wb") as handle:
        handle.write(protocol_bytes)
    manifest = {
        "protocol_version": "1.0.0",
        "sha256": hashlib.sha256(protocol_bytes).hexdigest(),
    }
    with open(os.path.join(references, "protocol.manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    with open(os.path.join(skill_root, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write("---\nname: teamcraft-ceo\ndescription: thin loader\n---\nload\n")

    fx["artifact_digest"] = artifact_digest
    conn.close()
    return fx


class PopulatedM302Fixture(M302Fixture):
    populate = True


# ---------------------------------------------------------------------------
# Route surface and guards
# ---------------------------------------------------------------------------


@requires_web
class RouteSurfaceTests(M302Fixture):
    def test_shell_without_service_is_unchanged(self) -> None:
        shell = create_app()
        paths = {route.path for route in iter_routes(shell)}
        self.assertNotIn("/api/v1/projects", paths)
        client = TestClient(shell)
        denial = client.get(
            "/api/v1/projects", headers=self.auth(shell.state.token_store.ensure())
        )
        self.assertEqual(denial.status_code, 404)
        self.assertEqual(denial.json()["code"], NOT_FOUND)

    def test_query_routes_are_get_only(self) -> None:
        seen = {route.path for route in iter_routes(self.app)}
        for path in (
            "/api/v1/projects",
            "/api/v1/projects/{project_id}",
            "/api/v1/projects/{project_id}/snapshot",
            "/api/v1/projects/{project_id}/tasks",
            "/api/v1/projects/{project_id}/ceo",
            "/api/v1/tasks/{task_id}",
            "/api/v1/tasks/{task_id}/attempts",
            "/api/v1/tasks/{task_id}/dependencies",
            "/api/v1/tasks/{task_id}/verifications",
            "/api/v1/tasks/{task_id}/approvals",
            "/api/v1/tasks/{task_id}/recovery-actions",
            "/api/v1/attempts/{attempt_id}",
            "/api/v1/sessions/{session_id}",
            "/api/v1/agents",
            "/api/v1/agents/{agent_id}",
            "/api/v1/members",
            "/api/v1/members/{member_name}",
            "/api/v1/events",
            "/api/v1/events/{event_id}",
            "/api/v1/wakes",
            "/api/v1/wakes/{wake_id}",
            "/api/v1/wakes/{wake_id}/events",
            "/api/v1/wake-cursors/{project_id}",
            "/api/v1/approvals",
            "/api/v1/approvals/{approval_id}",
            "/api/v1/recovery-actions",
            "/api/v1/recovery-actions/{command_id}",
            "/api/v1/verifications",
            "/api/v1/verifications/{verification_id}",
            "/api/v1/verification-configs",
        ):
            self.assertIn(path, seen, path)
        for route in iter_routes(self.app):
            path = getattr(route, "path", "")
            if not path.startswith("/api/v1/") or path == "/api/v1/_probe":
                continue
            methods = {str(method).upper() for method in (getattr(route, "methods", None) or ())}
            self.assertTrue(
                methods <= {"GET", "HEAD", "OPTIONS"},
                f"{path} exposes non-read methods {methods}",
            )

    def test_all_query_paths_require_authentication(self) -> None:
        for path in _SIMPLE_PATHS:
            denial = self.client.get(path)
            self.assertEqual(denial.status_code, 401, path)
            self.assertEqual(denial.json()["code"], "UNAUTHENTICATED", path)
        wrong = self.client.get("/api/v1/projects", headers=self.auth("wrong-token"))
        self.assertEqual(wrong.status_code, 401)

    def test_write_methods_on_query_paths_are_refused(self) -> None:
        before = db_state_digest(self.db_path)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.client.request(
                method, "/api/v1/projects", headers=self.auth()
            )
            self.assertIn(response.status_code, (403, 405), method)
        self.assertEqual(db_state_digest(self.db_path), before)


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


@requires_web
class EmptyStateTests(M302Fixture):
    def test_lists_are_empty_not_missing(self) -> None:
        projects = self.assert_ok(self.get("/api/v1/projects"))
        self.assertEqual(projects["items"], [])
        self.assertEqual(
            projects["page"],
            {"limit": DEFAULT_PAGE_SIZE, "offset": 0, "total": 0, "has_more": False},
        )
        # An empty database is a valid empty answer, not 503 or a lie.
        events_body = self.assert_ok(self.get("/api/v1/events"))
        self.assertEqual(events_body["items"], [])

    def test_unknown_entities_are_uniform_404(self) -> None:
        paths = (
            "/api/v1/projects/proj_missing",
            "/api/v1/projects/proj_missing/snapshot",
            "/api/v1/projects/proj_missing/tasks",
            "/api/v1/projects/proj_missing/ceo",
            "/api/v1/tasks/task_missing",
            "/api/v1/tasks/task_missing/attempts",
            "/api/v1/tasks/task_missing/dependencies",
            "/api/v1/tasks/task_missing/verifications",
            "/api/v1/tasks/task_missing/approvals",
            "/api/v1/tasks/task_missing/recovery-actions",
            "/api/v1/attempts/att_missing",
            "/api/v1/sessions/sess_missing",
            "/api/v1/agents/agent_missing",
            "/api/v1/members/member_missing",
            "/api/v1/events/evt_missing",
            "/api/v1/wakes/wake_missing",
            "/api/v1/wakes/wake_missing/events",
            "/api/v1/wake-cursors/proj_missing",
            "/api/v1/approvals/apr_missing",
            "/api/v1/recovery-actions/cmd_missing",
            "/api/v1/verifications/ver_missing",
        )
        for path in paths:
            self.assert_uniform_error(self.get(path), 404, NOT_FOUND)

    def test_unknown_filter_parents_are_404(self) -> None:
        for path, params in (
            ("/api/v1/events", {"project_id": "ghost"}),
            ("/api/v1/approvals", {"task_id": "ghost"}),
            ("/api/v1/recovery-actions", {"project_id": "ghost"}),
            ("/api/v1/verifications", {"task_id": "ghost"}),
        ):
            self.assert_uniform_error(self.get(path, **params), 404, NOT_FOUND)


# ---------------------------------------------------------------------------
# Store availability
# ---------------------------------------------------------------------------


@requires_web
class StoreAvailabilityTests(unittest.TestCase):
    def test_missing_database_is_503_without_path_leak(self) -> None:
        root = tempfile.mkdtemp(prefix="tc-m302-nodb-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        missing = os.path.join(root, "not-yet", "teamcraft.db")
        app = create_app(query_service=QueryService(missing))
        client = TestClient(app)
        token = app.state.token_store.ensure()
        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
        body = self.assert_uniform_error_static(response, 503, SERVICE_UNAVAILABLE)
        self.assertNotIn("teamcraft.db", body["message"])
        self.assertNotIn(root, json.dumps(body))
        with self.assertRaises(QueryUnavailableError):
            QueryService(missing).list_projects()

    def test_too_new_schema_is_refused(self) -> None:
        root = tempfile.mkdtemp(prefix="tc-m302-new-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fresh_start.initialize_project_root(root)
        db_path = os.path.join(root, ".teamcraft", "teamcraft.db")
        raw = sqlite3.connect(db_path)
        raw.execute("PRAGMA user_version = 999")
        raw.commit()
        raw.close()
        service = QueryService(db_path)
        with self.assertRaises(QueryUnavailableError):
            service.list_projects()
        app = create_app(query_service=service)
        client = TestClient(app)
        token = app.state.token_store.ensure()
        response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
        body = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["code"], SERVICE_UNAVAILABLE)
        self.assertNotIn(db_path, json.dumps(body))

    @staticmethod
    def assert_uniform_error_static(response, status: int, code: str) -> dict:
        assert response.status_code == status, response.text
        body = response.json()
        assert set(body.keys()) == ERROR_KEYS
        assert body["code"] == code
        return body

    def test_service_never_opens_a_writable_connection(self) -> None:
        root = tempfile.mkdtemp(prefix="tc-m302-ro-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fresh_start.initialize_project_root(root)
        db_path = os.path.join(root, ".teamcraft", "teamcraft.db")
        recorded: list[str] = []
        real_connect = sqlite3.connect

        def spy_connect(*args, **kwargs):
            target = args[0] if args else kwargs.get("database")
            recorded.append(str(target))
            return real_connect(*args, **kwargs)

        service = QueryService(db_path)
        with mock.patch.object(sqlite3, "connect", side_effect=spy_connect):
            service.list_projects()
            service.list_events()
            # A missing entity still round-trips through the read-only channel.
            with contextlib.suppress(Exception):
                service.get_project("does-not-exist")
        self.assertTrue(recorded)
        for uri in recorded:
            self.assertIn("mode=ro", uri, uri)


# ---------------------------------------------------------------------------
# Populated state: same-source data, layered distinctions
# ---------------------------------------------------------------------------


@requires_web
class PopulatedReadTests(PopulatedM302Fixture):
    def test_projects_same_source(self) -> None:
        body = self.assert_ok(self.get("/api/v1/projects"))
        self.assertEqual(body["page"]["total"], 1)
        dto = body["items"][0]
        self.assertEqual(dto["id"], self.fixture["project"].id)
        self.assertEqual(dto["name"], "alpha")
        one = self.assert_ok(self.get(f"/api/v1/projects/{dto['id']}"))
        self.assertEqual(one["id"], dto["id"])

    def test_task_list_matches_core_order_and_filters(self) -> None:
        project_id = self.fixture["project"].id
        conn = self.rw()
        try:
            core_ids = [task.id for task in domain.list_tasks(conn, project_id)]
            core_ready = [task.id for task in domain.list_tasks(conn, project_id, status="ready")]
            core_scout = [
                task.id for task in domain.list_tasks(conn, project_id) if task.kind == "scout"
            ]
        finally:
            conn.close()
        api_ids = [item["id"] for item in self.assert_ok(self.get(f"/api/v1/projects/{project_id}/tasks"))["items"]]
        self.assertEqual(api_ids, core_ids)
        api_ready = [
            item["id"]
            for item in self.assert_ok(self.get(f"/api/v1/projects/{project_id}/tasks", status="ready"))["items"]
        ]
        self.assertEqual(api_ready, core_ready)
        api_scout = [
            item["id"]
            for item in self.assert_ok(self.get(f"/api/v1/projects/{project_id}/tasks", kind="scout"))["items"]
        ]
        self.assertEqual(api_scout, core_scout)

    def test_task_detail_carries_edges_and_version(self) -> None:
        task_down = self.fixture["task_down"]
        detail = self.assert_ok(self.get(f"/api/v1/tasks/{task_down.id}"))
        self.assertEqual(detail["id"], task_down.id)
        self.assertEqual(detail["depends_on"], [self.fixture["task_ship"].id])
        self.assertEqual(detail["required_by"], [])
        self.assertGreaterEqual(detail["version"], 1)
        edges = self.assert_ok(self.get(f"/api/v1/tasks/{self.fixture['task_ship'].id}/dependencies"))
        self.assertEqual(edges["required_by"], [task_down.id])

    def test_layered_states_are_never_collapsed(self) -> None:
        # Task running / Attempt UNKNOWN / Session exited coexist and stay
        # distinct in every DTO.
        scout_task = self.assert_ok(self.get(f"/api/v1/tasks/{self.fixture['task_scout'].id}"))
        attempt = self.assert_ok(self.get(f"/api/v1/attempts/{self.fixture['attempt_scout'].id}"))
        session = self.assert_ok(self.get(f"/api/v1/sessions/{self.fixture['session_scout'].id}"))
        self.assertEqual(scout_task["status"], "running")
        self.assertEqual(attempt["status"], "unknown")
        self.assertEqual(session["state"], "exited")
        snapshot = self.assert_ok(self.get(f"/api/v1/projects/{self.fixture['project'].id}/snapshot"))
        counts = snapshot["counts"]
        self.assertEqual(counts["attempts_by_status"]["unknown"], 1)
        self.assertEqual(counts["sessions_by_state"]["exited"], 1)
        self.assertEqual(counts["tasks_by_status"]["running"], 2)

    def test_attempt_detail_bundles_brief_sessions_and_reports(self) -> None:
        attempt = self.assert_ok(self.get(f"/api/v1/attempts/{self.fixture['attempt_ship'].id}"))
        brief = self.fixture["brief"]
        self.assertIsNotNone(attempt["brief"])
        self.assertEqual(attempt["brief"]["brief_digest"], brief.brief_digest)
        self.assertEqual(attempt["brief"]["task_version"], brief.task_version)
        self.assertEqual(
            [item["id"] for item in attempt["sessions"]], [self.fixture["session_ship"].id]
        )
        self.assertEqual(len(attempt["worker_reports"]), 1)
        report = attempt["worker_reports"][0]
        self.assertEqual(report["report_kind"], "progress")
        self.assertIsNone(report["outcome"])
        self.assertEqual(report["progress_class"], "task_progress")

    def test_unknown_token_usage_is_null_never_zero(self) -> None:
        attempt = self.assert_ok(self.get(f"/api/v1/attempts/{self.fixture['attempt_ship'].id}"))
        usage = attempt["worker_reports"][0]["usage"]
        self.assertEqual(
            usage,
            {
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cost_microunits": None,
                "available": False,
            },
        )
        for field in USAGE_FIELDS:
            self.assertIsNot(usage[field], 0)

    def test_prompt_delivery_submitted_is_not_acknowledged(self) -> None:
        attempt = self.assert_ok(self.get(f"/api/v1/attempts/{self.fixture['attempt_ship'].id}"))
        delivery = attempt["prompt_delivery"]
        self.assertEqual(delivery["state"], "submitted")
        self.assertNotEqual(delivery["state"], "acknowledged")
        self.assertEqual(delivery["brief_digest"], self.fixture["brief"].brief_digest)

    def test_events_match_core_stream_and_filters(self) -> None:
        project_id = self.fixture["project"].id
        conn = self.rw()
        try:
            core_seqs = [event.seq for event in events.events_after(conn, 0, limit=1000, project_id=project_id)]
        finally:
            conn.close()
        api_body = self.assert_ok(self.get("/api/v1/events", project_id=project_id, limit=100))
        api_seqs = [item["seq"] for item in api_body["items"]]
        self.assertEqual(api_seqs, core_seqs)
        self.assertTrue(all(seq > 0 for seq in api_seqs))
        # after_seq cursor skips the prefix
        if len(api_seqs) > 2:
            tail = self.assert_ok(self.get("/api/v1/events", project_id=project_id, after_seq=api_seqs[1]))
            self.assertEqual([item["seq"] for item in tail["items"]], api_seqs[2:])
        kind_body = self.assert_ok(self.get("/api/v1/events", kind="recovery.escalated"))
        self.assertEqual(len(kind_body["items"]), 1)
        self.assertEqual(kind_body["items"][0]["progress_class"], None)
        observed = self.assert_ok(self.get("/api/v1/events", kind="watch.session_observed"))
        self.assertEqual(observed["items"][0]["progress_class"], "process_liveness")
        threshold = self.assert_ok(self.get("/api/v1/events", kind="watch.no_progress_threshold"))
        self.assertTrue(all(item["progress_class"] == "task_progress" for item in threshold["items"]))
        one = self.assert_ok(self.get(f"/api/v1/events/{kind_body['items'][0]['event_id']}"))
        self.assertEqual(one["kind"], "recovery.escalated")

    def test_wakes_match_core_queue_and_keep_lifecycle(self) -> None:
        project_id = self.fixture["project"].id
        conn = self.rw()
        try:
            core_ids = [item.id for item in wake.list_wakes(conn, project_id)]
        finally:
            conn.close()
        body = self.assert_ok(self.get("/api/v1/wakes", project_id=project_id))
        api_ids = [item["id"] for item in body["items"]]
        self.assertEqual(api_ids, core_ids)
        pending = [item for item in body["items"] if item["state"] == "pending"]
        self.assertTrue(pending)
        for item in pending:
            self.assertIsNone(item["submitted_at"])
            self.assertIsNone(item["acknowledged_at"])
            self.assertIsNone(item["processed_at"])
            self.assertGreaterEqual(item["event_count"], 1)
            self.assertEqual(len(item["event_digest"]), 64)
        linked = self.assert_ok(self.get(f"/api/v1/wakes/{body['items'][0]['id']}/events"))
        self.assertEqual(len(linked["event_seqs"]), body["items"][0]["event_count"])
        cursor = self.assert_ok(self.get(f"/api/v1/wake-cursors/{project_id}"))
        self.assertEqual(cursor["scanned_seq"], self.fixture["ingest"].cursor.scanned_seq)
        self.assertEqual(cursor["acknowledged_seq"], 0)
        self.assertEqual(cursor["processed_seq"], 0)

    def test_wake_state_filter_uses_lifecycle_vocabulary(self) -> None:
        project_id = self.fixture["project"].id
        body = self.assert_ok(self.get("/api/v1/wakes", project_id=project_id, state="pending"))
        self.assertTrue(all(item["state"] == "pending" for item in body["items"]))
        empty = self.assert_ok(self.get("/api/v1/wakes", project_id=project_id, state="processed"))
        self.assertEqual(empty["items"], [])

    def test_agents_and_members_same_source(self) -> None:
        agents = self.assert_ok(self.get("/api/v1/agents"))
        self.assertEqual([item["id"] for item in agents["items"]], ["agent_dev", "agent_qa"])
        one = self.assert_ok(self.get("/api/v1/agents/agent_dev"))
        self.assertEqual(one["harness"], "codex")
        self.assertIsNone(self.assert_ok(self.get("/api/v1/agents/agent_qa"))["model"])

        members = self.assert_ok(self.get("/api/v1/members"))
        self.assertEqual([item["name"] for item in members["items"]], ["dev_a"])
        member = members["items"][0]
        # CLI parity: identical field values to runtime_adapter.team_view.
        conn = self.rw()
        try:
            cli_view = runtime_adapter.team_view(conn)
            assert cli_view is not None
            cli_member = cli_view["members"]["dev_a"]
        finally:
            conn.close()
        for field in (
            "name",
            "role",
            "kind",
            "ownership",
            "read_only",
            "cwd",
            "pane_id",
            "terminal_id",
            "agent_session",
            "direction",
            "start_timeout_ms",
            "phase",
            "prompt_may_have_been_delivered",
            "live_state",
            "task_id",
            "attempt_id",
            "session_id",
            "created_at",
            "updated_at",
        ):
            self.assertEqual(member[field], cli_member[field], field)
        # phase (launch compat) and live_state (Herdr signal) are separate
        # columns and both distinct from the Task status.
        self.assertEqual(member["phase"], "prompt_submitted")
        self.assertEqual(member["live_state"], "idle")
        single = self.assert_ok(self.get("/api/v1/members/dev_a"))
        self.assertEqual(single["name"], "dev_a")

    def test_verifications_and_approvals_records(self) -> None:
        task_id = self.fixture["task_ship"].id
        verifications = self.assert_ok(self.get(f"/api/v1/tasks/{task_id}/verifications"))
        self.assertEqual([item["id"] for item in verifications["items"]], ["ver_1"])
        verification = verifications["items"][0]
        self.assertEqual(verification["status"], "pass")
        self.assertEqual(verification["exit_code"], 0)
        self.assertEqual(verification["brief_digest"], self.fixture["brief"].brief_digest)
        self.assertEqual(verification["worker_report_digest"], self.fixture["progress"].report_digest)
        self.assertEqual(verification["artifact"]["digest"], self.fixture["artifact_digest"])

        approvals = self.assert_ok(self.get(f"/api/v1/tasks/{task_id}/approvals"))
        self.assertEqual([item["id"] for item in approvals["items"]], ["apr_1"])
        approval = approvals["items"][0]
        self.assertEqual(approval["decision"], "accept")
        self.assertEqual(approval["verification_id"], "ver_1")
        self.assertEqual(approval["attempt_no"], self.fixture["attempt_ship"].attempt_no)
        self.assertEqual(approval["artifact_digest"], self.fixture["artifact_digest"])
        # durable evidence binding survives even if "current" is not derived here
        self.assertIn("task_version", approval)
        filtered = self.assert_ok(self.get("/api/v1/approvals", decision="reject"))
        self.assertEqual(filtered["items"], [])
        one = self.assert_ok(self.get("/api/v1/approvals/apr_1"))
        self.assertEqual(one["id"], "apr_1")
        configs = self.assert_ok(self.get("/api/v1/verification-configs", project_id=self.fixture["project"].id))
        self.assertEqual(configs["items"][0]["config_version"], 1)
        self.assertEqual(configs["items"][0]["argv"], ["python3", "-B", "-m", "pytest", "tests"])

    def test_recovery_action_records(self) -> None:
        cancel = self.fixture["cancel_command"]
        body = self.assert_ok(self.get("/api/v1/recovery-actions"))
        ids = [item["command_id"] for item in body["items"]]
        self.assertIn(cancel.id, ids)
        one = self.assert_ok(self.get(f"/api/v1/recovery-actions/{cancel.id}"))
        self.assertEqual(one["action"], "cancel")
        self.assertEqual(one["state"], "pending")
        self.assertEqual(one["task_id"], self.fixture["task_scout"].id)
        self.assertEqual(one["attempt_id"], self.fixture["attempt_scout"].id)
        self.assertEqual(one["actor"], "operator")
        self.assertIsNone(one["result"])
        by_task = self.assert_ok(
            self.get(f"/api/v1/tasks/{self.fixture['task_scout'].id}/recovery-actions", state="pending")
        )
        self.assertIn(cancel.id, [item["command_id"] for item in by_task["items"]])
        none = self.assert_ok(self.get("/api/v1/recovery-actions", action="reconcile"))
        self.assertEqual(none["items"], [])

    def test_project_snapshot_aggregates(self) -> None:
        snapshot = self.assert_ok(self.get(f"/api/v1/projects/{self.fixture['project'].id}/snapshot"))
        counts = snapshot["counts"]
        self.assertEqual(counts["tasks_by_status"]["running"], 2)
        self.assertEqual(counts["tasks_by_status"]["pending"], 1)
        self.assertEqual(counts["attempts_by_status"]["active"], 1)
        self.assertEqual(counts["attempts_by_status"]["unknown"], 1)
        self.assertEqual(counts["sessions_by_state"]["working"], 1)
        self.assertEqual(counts["sessions_by_state"]["exited"], 1)
        self.assertGreaterEqual(counts["wakes_by_state"]["pending"], 2)
        self.assertGreaterEqual(snapshot["event_seq"], 4)
        self.assertEqual(snapshot["ceo_initialization_state"], "initialized")
        self.assertIsNotNone(snapshot["wake_cursor"])

    def test_ceo_view_initialization_and_protocol(self) -> None:
        view = self.assert_ok(self.get(f"/api/v1/projects/{self.fixture['project'].id}/ceo"))
        initialization = view["initialization"]
        self.assertIsNotNone(initialization)
        self.assertEqual(initialization["state"], "initialized")
        # Honest provenance: assumed_default is NOT a trusted host receipt.
        self.assertEqual(initialization["receipt_source"], "assumed_default")
        self.assertEqual(initialization["identity_mode"], "native")
        self.assertTrue(initialization["has_herdr_session_identity"])
        self.assertFalse(initialization["has_live_process_identity"])
        self.assertFalse(initialization["nonce_consumed"])
        protocol = view["protocol"]
        self.assertTrue(protocol["available"])
        self.assertEqual(protocol["protocol_version"], "1.0.0")
        self.assertEqual(len(protocol["sha256"]), 64)
        self.assertEqual(protocol["harnesses"], {"codex": "available", "opencode": "available"})

    def test_ceo_view_without_initialization_or_protocol(self) -> None:
        conn = self.rw()
        try:
            other = domain.create_project(conn, "beta", self.root + "-beta")
        finally:
            conn.close()
        view = self.assert_ok(self.get(f"/api/v1/projects/{other.id}/ceo"))
        self.assertIsNone(view["initialization"])
        protocol = view["protocol"]
        self.assertFalse(protocol["available"])
        self.assertIsNone(protocol["protocol_version"])
        self.assertEqual(protocol["harnesses"], {"codex": "missing", "opencode": "missing"})


# ---------------------------------------------------------------------------
# Pagination and filtering
# ---------------------------------------------------------------------------


@requires_web
class PaginationFilterTests(PopulatedM302Fixture):
    def test_pagination_walks_all_tasks_deterministically(self) -> None:
        project_id = self.fixture["project"].id
        conn = self.rw()
        try:
            core_ids = [task.id for task in domain.list_tasks(conn, project_id)]
        finally:
            conn.close()
        collected: list[str] = []
        offset = 0
        while True:
            body = self.assert_ok(
                self.get(f"/api/v1/projects/{project_id}/tasks", limit=1, offset=offset)
            )
            self.assertEqual(len(body["items"]), 1)
            self.assertEqual(body["page"]["limit"], 1)
            self.assertEqual(body["page"]["offset"], offset)
            collected.append(body["items"][0]["id"])
            if not body["page"]["has_more"]:
                break
            offset += 1
        self.assertEqual(collected, core_ids)

    def test_offset_beyond_end_is_empty_not_error(self) -> None:
        project_id = self.fixture["project"].id
        body = self.assert_ok(self.get(f"/api/v1/projects/{project_id}/tasks", limit=5, offset=50))
        self.assertEqual(body["items"], [])
        self.assertFalse(body["page"]["has_more"])
        self.assertEqual(body["page"]["total"], 3)

    def test_max_limit_accepted(self) -> None:
        body = self.assert_ok(self.get("/api/v1/events", limit=MAX_PAGE_SIZE))
        self.assertEqual(body["page"]["limit"], MAX_PAGE_SIZE)

    def test_malformed_limits_and_filters_fail_uniformly(self) -> None:
        project_id = self.fixture["project"].id
        cases = [
            ("/api/v1/projects", {"limit": "0"}),
            ("/api/v1/projects", {"limit": "-3"}),
            ("/api/v1/projects", {"limit": "abc"}),
            ("/api/v1/projects", {"limit": "1.5"}),
            ("/api/v1/projects", {"limit": str(MAX_PAGE_SIZE + 1)}),
            ("/api/v1/projects", {"offset": "-1"}),
            ("/api/v1/projects", {"offset": "x"}),
            (f"/api/v1/projects/{project_id}/tasks", {"status": "not-a-status"}),
            (f"/api/v1/projects/{project_id}/tasks", {"kind": "bogus"}),
            (f"/api/v1/tasks/{self.fixture['task_ship'].id}/attempts", {"status": "sleeping"}),
            ("/api/v1/events", {"after_seq": "-1"}),
            ("/api/v1/events", {"after_seq": "nope"}),
            ("/api/v1/events", {"kind": "Bad Kind!"}),
            ("/api/v1/events", {"project_id": " "}),
            ("/api/v1/wakes", {"project_id": project_id, "state": "awake"}),
            ("/api/v1/wakes", {"project_id": project_id, "target": "Bad Target!"}),
            ("/api/v1/approvals", {"decision": "maybe"}),
            ("/api/v1/recovery-actions", {"action": "explode"}),
            ("/api/v1/recovery-actions", {"state": "flying"}),
            ("/api/v1/verifications", {"status": "purple"}),
            ("/api/v1/verification-configs", {"limit": str(MAX_PAGE_SIZE + 1)}),
            ("/api/v1/members/Bad%20Name!", {}),
        ]
        codes = set()
        for path, params in cases:
            body = self.assert_uniform_error(self.get(path, **params), 422, VALIDATION_FAILED)
            codes.add(body["code"])
        self.assertEqual(codes, {VALIDATION_FAILED})

    def test_oversized_id_filters_are_rejected(self) -> None:
        huge = "z" * 4000
        self.assert_uniform_error(self.get("/api/v1/events", project_id=huge), 422, VALIDATION_FAILED)


# ---------------------------------------------------------------------------
# Truncation and redaction
# ---------------------------------------------------------------------------


@requires_web
class TruncationRedactionTests(PopulatedM302Fixture):
    def test_long_description_is_bounded_with_flag(self) -> None:
        detail = self.assert_ok(self.get(f"/api/v1/tasks/{self.fixture['task_ship'].id}"))
        self.assertTrue(detail["description_truncated"])
        self.assertLessEqual(len(detail["description"].encode("utf-8")), MAX_TEXT_BYTES)

    def test_artifact_preview_is_bounded_and_digest_anchored(self) -> None:
        verification = self.assert_ok(self.get("/api/v1/verifications/ver_1"))
        artifact = verification["artifact"]
        self.assertTrue(artifact["preview_truncated"])
        self.assertLessEqual(len(artifact["preview"].encode("utf-8")), MAX_PREVIEW_BYTES)
        self.assertGreater(artifact["size_bytes"], MAX_PREVIEW_BYTES)
        self.assertEqual(artifact["digest"], self.fixture["artifact_digest"])
        self.assertFalse(artifact["binary"])
        # The durable bytes stay in the DB (preview is not the source).
        conn = self.rw()
        try:
            stored = conn.execute(
                "SELECT length(artifact) FROM verifications WHERE id='ver_1'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(stored, artifact["size_bytes"])

    def test_brief_preview_is_bounded(self) -> None:
        attempt = self.assert_ok(self.get(f"/api/v1/attempts/{self.fixture['attempt_ship'].id}"))
        preview = attempt["brief"]["content_preview"]
        self.assertIsInstance(preview, str)
        self.assertFalse(attempt["brief"]["content_truncated"])

    def test_session_token_never_leaks_in_bodies(self) -> None:
        paths = [
            "/api/v1/projects",
            "/api/v1/events",
            "/api/v1/events?kind=worker.report_recorded",
            "/api/v1/tasks/" + self.fixture["task_ship"].id,
            "/api/v1/attempts/" + self.fixture["attempt_ship"].id,
            "/api/v1/sessions/" + self.fixture["session_ship"].id,
            "/api/v1/verifications/ver_1",
            "/api/v1/approvals/apr_1",
            "/api/v1/members",
            f"/api/v1/projects/{self.fixture['project'].id}/ceo",
            f"/api/v1/projects/{self.fixture['project'].id}/snapshot",
            "/api/v1/wakes?project_id=" + self.fixture["project"].id,
            "/api/v1/recovery-actions",
        ]
        for path in paths:
            response = self.client.get(path, headers=self.auth())
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn(
                self.token,
                response.text,
                f"session token leaked via {path}",
            )
        # seeded in-line token material renders as the redaction marker
        attempt_text = self.client.get(
            f"/api/v1/attempts/{self.fixture['attempt_ship'].id}", headers=self.auth()
        ).text
        self.assertIn(REDACTION_MARKER, attempt_text)
        ceo_text = self.client.get(
            f"/api/v1/projects/{self.fixture['project'].id}/ceo", headers=self.auth()
        ).text
        self.assertIn(REDACTION_MARKER, ceo_text)

    def test_redaction_survives_token_rotation(self) -> None:
        old_token = self.token
        new_token = self.app.state.rotate_session_token()
        response = self.client.get(
            f"/api/v1/projects/{self.fixture['project'].id}/ceo", headers=self.auth(new_token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(old_token, response.text)
        self.assertNotIn(new_token, response.text)
        self.assertIn(REDACTION_MARKER, response.text)

    def test_sensitive_paths_never_leak(self) -> None:
        paths = [
            "/api/v1/projects",
            f"/api/v1/projects/{self.fixture['project'].id}",
            f"/api/v1/projects/{self.fixture['project'].id}/snapshot",
            f"/api/v1/projects/{self.fixture['project'].id}/ceo",
            f"/api/v1/tasks/{self.fixture['task_ship'].id}",
            f"/api/v1/attempts/{self.fixture['attempt_ship'].id}",
            "/api/v1/verifications/ver_1",
            "/api/v1/approvals/apr_1",
            "/api/v1/members",
            "/api/v1/recovery-actions",
            "/api/v1/wakes?project_id=" + self.fixture["project"].id,
            "/api/v1/wake-cursors/" + self.fixture["project"].id,
            "/api/v1/verification-configs",
            "/api/v1/events",
        ]
        for path in paths:
            text = self.client.get(path, headers=self.auth()).text
            for fragment in DB_FRAGMENTS + (SOCKET_FRAGMENT,):
                self.assertNotIn(fragment, text, f"{fragment} leaked via {path}")
        # 404 bodies stay generic too (no ids echoed, no paths).
        missing = self.client.get("/api/v1/projects/ghost-project", headers=self.auth())
        self.assertNotIn(self.db_path, missing.text)
        self.assertEqual(missing.json()["message"], "The requested resource does not exist.")

    def test_logs_never_print_the_token(self) -> None:
        with capture_logs() as handler:
            for path in _SIMPLE_PATHS:
                self.client.get(path, headers=self.auth())
            self.client.get("/api/v1/projects")  # unauthenticated path
        joined = "\n".join(handler.lines)
        self.assertNotIn(self.token, joined)
        self.assertNotIn("Bearer", joined)


# ---------------------------------------------------------------------------
# No-mutation invariant
# ---------------------------------------------------------------------------


@requires_web
class NoMutationInvariantTests(PopulatedM302Fixture):
    def sweep(self) -> None:
        project_id = self.fixture["project"].id
        task_id = self.fixture["task_ship"].id
        attempt_id = self.fixture["attempt_ship"].id
        paths = [
            "/api/v1/projects",
            f"/api/v1/projects/{project_id}",
            f"/api/v1/projects/{project_id}/snapshot",
            f"/api/v1/projects/{project_id}/tasks",
            f"/api/v1/projects/{project_id}/tasks?status=ready",
            f"/api/v1/projects/{project_id}/ceo",
            f"/api/v1/tasks/{task_id}",
            f"/api/v1/tasks/{task_id}/attempts",
            f"/api/v1/tasks/{task_id}/dependencies",
            f"/api/v1/tasks/{task_id}/verifications",
            f"/api/v1/tasks/{task_id}/approvals",
            f"/api/v1/tasks/{task_id}/recovery-actions",
            f"/api/v1/attempts/{attempt_id}",
            f"/api/v1/sessions/{self.fixture['session_ship'].id}",
            "/api/v1/agents",
            "/api/v1/agents/agent_dev",
            "/api/v1/members",
            "/api/v1/members/dev_a",
            "/api/v1/events",
            f"/api/v1/events?project_id={project_id}&after_seq=1",
            "/api/v1/wakes?project_id=" + project_id,
            f"/api/v1/wakes/{self.fixture['ingest'].wakes[0].id}",
            f"/api/v1/wakes/{self.fixture['ingest'].wakes[0].id}/events",
            f"/api/v1/wake-cursors/{project_id}",
            "/api/v1/approvals",
            "/api/v1/approvals/apr_1",
            "/api/v1/recovery-actions",
            "/api/v1/verifications",
            "/api/v1/verifications/ver_1",
            "/api/v1/verification-configs",
            # malformed + unauthenticated + unknown routes too
            "/api/v1/projects?limit=0",
            "/api/v1/events?kind=Bad%20Kind",
            "/api/v1/projects/ghost",
        ]
        statuses = []
        for path in paths:
            statuses.append(self.client.get(path, headers=self.auth()).status_code)
        self.assertTrue(all(status in (200, 404, 422) for status in statuses), statuses)

    def test_reads_never_mutate_state_events_wakes_or_dispatch(self) -> None:
        before_digest = db_state_digest(self.db_path)
        before_counts = table_counts(self.db_path)
        self.sweep()
        self.sweep()  # twice: idempotent reads, no drift
        after_digest = db_state_digest(self.db_path)
        after_counts = table_counts(self.db_path)
        self.assertEqual(before_digest, after_digest)
        self.assertEqual(before_counts, after_counts)
        # explicit: no new events/commands/wakes and no lease churn
        for table in ("events", "commands", "wakes", "wake_events", "leader_lease", "sessions"):
            self.assertEqual(before_counts[table], after_counts[table], table)

    def test_queries_do_not_wake_or_dispatch(self) -> None:
        conn = self.rw()
        try:
            pending_before = conn.execute(
                "SELECT COUNT(*) FROM wakes WHERE state IN ('pending','sending','submitted')"
            ).fetchone()[0]
            running_before = conn.execute(
                "SELECT COUNT(*) FROM commands WHERE state = 'running'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.sweep()
        conn = self.rw()
        try:
            pending_after = conn.execute(
                "SELECT COUNT(*) FROM wakes WHERE state IN ('pending','sending','submitted')"
            ).fetchone()[0]
            running_after = conn.execute(
                "SELECT COUNT(*) FROM commands WHERE state = 'running'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(pending_before, pending_after)
        self.assertEqual(running_before, running_after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
