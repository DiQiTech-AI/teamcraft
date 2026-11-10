"""M3-05 minimal web console integration tests.

Acceptance line from doc/TEAMCRAFT_2_0_M0_REVIEW.md (M3-05), verified here
against the REAL API stack — the console shell plus the actual M3-02 Query,
M3-03 SSE and M3-04 Command routes it talks to:

* the bundled console (``teamcraft/api/web/``) is served through the
  existing confined ``/static`` route (public shell, traversal/dotfile
  escapes refused), defaults on in ``serve`` and can be overridden or
  disabled;
* the console script is safe by construction: no HTML-parsing or dynamic
  code sinks, no external resources, no localStorage, and it references no
  prompt/dispatch path — every operation maps onto the M3-02/M3-03/M3-04
  routes with bearer auth, the CSRF header, and an Idempotency-Key;
* integration over a real runtime database: honest empty states, overview
  snapshot counts, task create/edit/priority/dependency/policy/command
  flows, idempotent replay and reuse-conflict, stale-version 409 with
  expected/found evidence, dependency-cycle path, scheduler pause/resume
  with the lease untouched, manual CEO wake recorded only (no wake rows,
  no prompt deliveries, no attempts), events paging over the durable seq,
  and an SSE tail whose ids are the SQLite seq in strictly increasing
  order;
* hostile free text round-trips verbatim as JSON data (the API never
  HTML-renders; the UI renders only text nodes); the session token never
  appears in any served body;
* the policy form's delivery control is the closed set
  report_only/local_only/direct_pr/ship — pinned against the server-side
  ``domain.DELIVERY_MODES`` so invalid free-text configuration is prevented
  in the UI and refused (422, nothing persisted) even if forged on the
  wire — and ``pyproject.toml`` declares the three console assets as
  setuptools package-data so wheel and sdist ship the console (both
  archives are inspected at build time; results recorded in README and
  doc/TEAMCRAFT_2_0_M0_REVIEW.md 12.20).

Not covered by this file (recorded, not claimed): real-browser rendering,
click-through E2E, and GitHub Actions runs against this tree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:  # the API needs the web stack; core tests must stay runnable without it
    import httpx
    import uvicorn
    from fastapi.testclient import TestClient

    from teamcraft.api import create_app
    from teamcraft.api.command import IDEMPOTENCY_HEADER, REPLAY_HEADER
    from teamcraft.api.errors import (
        CSRF_CHECK_FAILED,
        FORBIDDEN_ORIGIN,
        NOT_FOUND,
        PATH_FORBIDDEN,
        SERVICE_UNAVAILABLE,
        UNAUTHENTICATED,
        VALIDATION_FAILED,
    )
    from teamcraft.api.serve import WEB_ROOT, make_server
    from teamcraft.api.stream import SseConfig
    from teamcraft.api.command import CommandService
    from teamcraft.query import QueryService

    _WEB_STACK_OK = True
    _WEB_STACK_REASON = ""
except ImportError as exc:  # pragma: no cover - depends on CI install order
    _WEB_STACK_OK = False
    _WEB_STACK_REASON = f"web dependencies unavailable: {exc}"

from teamcraft import domain, fresh_start, storage

requires_web = unittest.skipUnless(_WEB_STACK_OK, _WEB_STACK_REASON)

ERROR_KEYS = {"code", "message", "details", "request_id"}
WEB_DIR = Path(__file__).resolve().parents[1] / "teamcraft" / "api" / "web"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
#: Same header the M3-04 surface mandates (kept as a literal so this module
#: imports cleanly even without the web stack installed).
IDEMPOTENCY_KEY_NAME = "Idempotency-Key"

#: Rendering sinks the console must never contain (hostile HTML/Markdown
#: logs are attached as text nodes only).
FORBIDDEN_SINKS = (
    "innerHTML",
    "outerHTML",
    "document.write",
    "insertAdjacentHTML",
    "new Function",
    "eval(",
    "localStorage",
    "sessionStorage.clear()",  # blanket wipe is not the Forget flow
)

#: Substrings no UI-addressed path may contain (the console physically
#: cannot prompt, dispatch, or bypass the guards).
FORBIDDEN_PATH_MARKERS = ("prompt", "dispatch", "admit", "_probe")


def read_web(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _start_server(app) -> tuple[object, threading.Thread, str]:
    """Run ``app`` on an ephemeral 127.0.0.1 port; (server, thread, base).

    Live SSE bodies need a real transport (the ASGI TestClient cannot
    represent streaming responses) — same approach as the M3-03 suite.
    """

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test uvicorn server failed to start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, f"http://127.0.0.1:{port}"


def _stop_server(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)


@requires_web
class ConsoleAssetTests(unittest.TestCase):
    """The bundled shell exists and is safe by construction."""

    def test_bundled_assets_exist_and_are_non_empty(self) -> None:
        self.assertTrue(WEB_ROOT.is_dir(), f"missing {WEB_ROOT}")
        for name in ("index.html", "app.js", "style.css"):
            path = WEB_DIR / name
            self.assertTrue(path.is_file(), f"missing asset {name}")
            self.assertGreater(len(path.read_text(encoding="utf-8")), 200)

    def test_script_and_markup_have_no_dangerous_sinks(self) -> None:
        sources = {
            "app.js": read_web("app.js"),
            "index.html": read_web("index.html"),
        }
        for name, text in sources.items():
            for sink in FORBIDDEN_SINKS:
                self.assertNotIn(
                    sink, text, f"{name} contains forbidden sink {sink!r}"
                )

    def test_markup_has_no_inline_handlers_or_scripts(self) -> None:
        html = read_web("index.html")
        self.assertIsNone(
            re.search(r"\son[a-z]+\s*=", html),
            "index.html carries an inline event handler",
        )
        for match in re.finditer(r"<script\b[^>]*>", html):
            tag = match.group(0)
            self.assertIn("src=", tag, f"inline script tag: {tag}")

    def test_external_resource_urls_are_absent(self) -> None:
        for name in ("index.html", "app.js", "style.css"):
            text = read_web(name)
            self.assertNotIn("http://", text, f"{name} references http://")
            self.assertNotIn("https://", text, f"{name} references https://")

    def test_only_local_asset_wiring_and_containers(self) -> None:
        html = read_web("index.html")
        self.assertIn('src="app.js"', html)
        self.assertIn('href="style.css"', html)
        for element_id in ("token-input", "banner", "main", "nav",
                          "project-select", "result-log"):
            self.assertIn(f'id="{element_id}"', html)
        # every <script tag must be a local src reference (no inline code)
        for match in re.finditer(r"<script\b[^>]*>", html):
            self.assertIn("src=", match.group(0), html[: match.start() + 80])

    def test_console_addresses_only_guarded_api_routes(self) -> None:
        js = read_web("app.js")
        paths = re.findall(
            r"([\"'`])(/[A-Za-z0-9_./?&=%${}\-]*)\1", js
        )
        literals = [item[1] for item in paths]
        self.assertTrue(literals, "no API path literals found")
        self.assertIn("/api/v1", literals)
        for path in literals:
            for marker in FORBIDDEN_PATH_MARKERS:
                self.assertNotIn(
                    marker, path,
                    f"console references forbidden path {path!r}",
                )

    def test_token_never_persisted_beyond_session_storage(self) -> None:
        js = read_web("app.js")
        self.assertIn("sessionStorage.setItem", js)
        self.assertNotIn("document.cookie", js)
        # the token must never be written into a URL
        self.assertNotIn("token=", js)

    def test_policy_form_exposes_only_allowed_delivery_values(self) -> None:
        """The delivery control is the closed set, never free text.

        Mirrors ``teamcraft.domain.DELIVERY_MODES`` (imported live, so the
        UI can never silently drift from what the Command API enforces):
        the console builds its <select> from exactly those values, and no
        free-text delivery input remains to type an invalid mode into.
        """
        js = read_web("app.js")
        self.assertNotIn('placeholder: "delivery"', js,
                        "delivery must stay a closed <select>, not free text")
        declared = re.search(r"DELIVERY_MODES\s*=\s*\[([^\]]*)\]", js)
        self.assertIsNotNone(
            declared, "app.js lost its closed DELIVERY_MODES list")
        values = re.findall(r'"([a-z_]+)"', declared.group(1))
        self.assertEqual(len(values), len(set(values)),
                         "duplicate delivery value in app.js")
        self.assertEqual(frozenset(values), domain.DELIVERY_MODES,
                         "app.js delivery set drifted from the Core set")
        start = js.index("function policyForm")
        form = js[start:js.index("function taskCommands", start)]
        self.assertIn('el("select"', form,
                     "the policy form must build a <select> for delivery")
        self.assertIn("DELIVERY_MODES.map(", form,
                     "the delivery select must iterate the closed set")
        self.assertIn("DELIVERY_MODES.includes(", form,
                     "the submit path must guard delivery against the set")

    def test_package_data_ships_console_assets_in_wheel_and_sdist(self) -> None:
        """Packaging pin (M3-05 gap): the three console assets must be
        declared as package-data so normal wheel and sdist installs carry
        them; otherwise the default ``serve.WEB_ROOT`` is empty after a
        packaged install and the console silently disappears."""
        text = PYPROJECT.read_text(encoding="utf-8")
        header = "[tool.setuptools.package-data]"
        self.assertIn(header, text, "pyproject.toml lacks package-data")
        section = text[text.index(header) + len(header):]
        cut = section.find("\n[")
        section = section[:cut] if cut != -1 else section
        for name in ("index.html", "app.js", "style.css"):
            self.assertIn(f"api/web/{name}", section,
                        f"package-data does not ship api/web/{name}")


@requires_web
class ServeConsoleWiringTests(unittest.TestCase):
    """serve defaults to the confined bundled console; overrides honoured."""

    def test_default_static_root_is_the_bundled_console(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, _store, args = make_server(["--project-root", temporary])
            self.assertFalse(args.no_web_ui)
            config = server.config.app.state.api_config
            self.assertEqual(Path(config.static_root), WEB_ROOT.resolve())
            client = TestClient(server.config.app)
            response = client.get("/static/index.html")  # public shell
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.headers["content-type"].startswith("text/html")
            )

    def test_no_web_ui_flag_serves_no_console(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, _store, args = make_server(
                ["--project-root", temporary, "--no-web-ui"]
            )
            self.assertTrue(args.no_web_ui)
            self.assertIsNone(server.config.app.state.api_config.static_root)
            client = TestClient(server.config.app)
            response = client.get("/static/index.html")
            body = self.assert_uniform(response, 404, NOT_FOUND)
            self.assertTrue(body["request_id"])

    def test_explicit_static_root_overrides_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            custom = Path(temporary) / "custom-ui"
            custom.mkdir()
            (custom / "marker.txt").write_text("custom asset\n")
            server, _store, _args = make_server([
                "--project-root", temporary,
                "--static-root", str(custom),
            ])
            config = server.config.app.state.api_config
            self.assertEqual(Path(config.static_root), custom)
            client = TestClient(server.config.app)
            self.assertEqual(client.get("/static/marker.txt").status_code, 200)
            # the bundled console is NOT served from the overridden root
            self.assertEqual(client.get("/static/index.html").status_code, 404)

    @staticmethod
    def assert_uniform(response, status: int, code: str) -> dict:
        assert response.status_code == status, response.text
        body = response.json()
        assert set(body.keys()) == ERROR_KEYS
        assert body["code"] == code
        return body


class ConsoleServedTests(unittest.TestCase):
    """The console rides the existing path confinement unchanged."""

    def setUp(self) -> None:
        self.app = create_app(static_root=WEB_ROOT)
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def test_console_shell_is_public_but_business_api_is_not(self) -> None:
        for path in ("/static/index.html", "/static/app.js",
                    "/static/style.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        self.assertEqual(
            self.client.get("/api/v1/projects").status_code, 401
        )

    def test_shell_contains_no_token_or_store_material(self) -> None:
        for name in ("index.html", "app.js", "style.css"):
            self.assertNotIn(self.token, read_web(name))

    def test_traversal_and_hidden_paths_stay_refused(self) -> None:
        hostile = (
            "/static/%2e%2e%2fserve.py",
            "/static/%2e%2e/%2e%2e/teamcraft/query.py",
            "/static/.hidden",
            "/static/.teamcraft/api/session-token",
        )
        for path in hostile:
            response = self.client.get(path)
            self.assertIn(response.status_code, (403, 404), path)
            if response.status_code == 403:
                self.assertEqual(response.json()["code"], PATH_FORBIDDEN)

    def test_static_container_paths_are_not_browsable(self) -> None:
        # "/static" itself is not public (no token -> uniform 401); with a
        # token it resolves inside the root, which is not servable (not a
        # file).  "/static/" is a public prefix but the confinement refuses
        # the empty/browsable path.  Neither lists directory contents.
        unauth = self.client.get("/static")
        self.assertEqual(unauth.status_code, 401)
        authed = self.client.get("/static", headers=self.auth())
        self.assertIn(authed.status_code, (403, 404))
        listed = self.client.get("/static/")
        self.assertEqual(listed.status_code, 403)
        self.assertEqual(listed.json()["code"], PATH_FORBIDDEN)


class ConsoleIntegrationFixture(unittest.TestCase):
    """Real runtime DB + guarded app + console; the UI's exact call shape."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="tc-m305-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(self.root, ".teamcraft", "teamcraft.db")
        self.app = create_app(
            query_service=QueryService(self.db_path),
            command_service=CommandService(self.db_path),
            static_root=WEB_ROOT,
        )
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()

    # -- request shapes mirroring what app.js sends -------------------------

    def read(self, path: str):
        return self.client.get(path, headers={"Authorization":
                                             f"Bearer {self.token}"})

    def write(self, method: str, path: str, body: dict, key: str,
             **extra) -> object:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-CSRF-Token": self.token,
            IDEMPOTENCY_HEADER: key,
            # what a same-origin browser sends; the middleware must accept it
            "Origin": "http://testserver",
        }
        headers.update(extra)
        return self.client.request(method, path, headers=headers, json=body)

    def make_project(self, name: str = "alpha") -> str:
        conn = storage.connect(self.db_path)
        try:
            return domain.create_project(conn, name, self.root).id
        finally:
            conn.close()

    def make_task(self, project_id: str, key: str, **overrides) -> dict:
        body = {
            "project_id": project_id,
            "title": overrides.get("title", "m305 task"),
            "description": overrides.get("description", ""),
            "kind": overrides.get("kind", "ship"),
            "priority": overrides.get("priority", 50),
            "acceptance": overrides.get("acceptance", []),
            "ownership": overrides.get("ownership", []),
            "risk": overrides.get("risk", "normal"),
            "max_attempts": overrides.get("max_attempts", 3),
            "dependencies": overrides.get("dependencies", []),
        }
        response = self.write("POST", "/api/v1/tasks", body, key)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["task"]

    # -- assertions -----------------------------------------------------------

    def assert_uniform(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body.keys()), ERROR_KEYS)
        self.assertEqual(body["code"], code)
        self.assertTrue(body["request_id"])
        return body

    def table_count(self, table: str) -> int:
        conn = storage.connect(self.db_path)
        try:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()


@requires_web
class ConsoleEmptyAndFailureStatesTests(ConsoleIntegrationFixture):
    def test_empty_store_reports_real_emptiness(self) -> None:
        payload = self.read("/api/v1/projects").json()
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["page"]["total"], 0)
        # no demo rows exist server-side to render — the honest empty state

    def test_missing_store_is_unavailable_not_empty_lie(self) -> None:
        missing = Path(self.root) / "nope" / "teamcraft.db"
        app = create_app(query_service=QueryService(missing))
        client = TestClient(app)
        token = app.state.token_store.ensure()
        self.assert_uniform(
            client.get("/api/v1/projects",
                     headers={"Authorization": f"Bearer {token}"}),
            503,
            SERVICE_UNAVAILABLE,
        )

    def test_auth_state_is_uniform_and_never_echoes_token(self) -> None:
        body = self.assert_uniform(
            self.client.get("/api/v1/projects"), 401, UNAUTHENTICATED
        )
        self.assertNotIn(self.token, json.dumps(body))

    def test_write_without_csrf_or_from_foreign_origin_is_refused(self) -> None:
        pid = self.make_project()
        body = {"project_id": pid, "actor": "web", "reason": "r"}
        no_csrf = self.client.post(
            "/api/v1/scheduler/pause",
            headers={"Authorization": f"Bearer {self.token}",
                    IDEMPOTENCY_KEY_NAME: "k-nocsrf",
                    "Origin": "http://testserver"},
            json=body,
        )
        self.assert_uniform(no_csrf, 403, CSRF_CHECK_FAILED)
        foreign = self.write(
            "POST", "/api/v1/scheduler/pause", body, "k-foreign",
            Origin="http://evil.example",
        )
        self.assert_uniform(foreign, 403, FORBIDDEN_ORIGIN)
        # both refusals wrote nothing
        self.assertEqual(self.table_count("commands"), 0)

    def test_write_without_idempotency_key_is_refused_before_core(self) -> None:
        pid = self.make_project()
        response = self.client.post(
            "/api/v1/tasks",
            headers={"Authorization": f"Bearer {self.token}",
                    "X-CSRF-Token": self.token,
                    "Origin": "http://testserver"},
            json={"project_id": pid, "title": "no key"},
        )
        body = self.assert_uniform(response, 422, VALIDATION_FAILED)
        self.assertEqual(body["details"]["field"], IDEMPOTENCY_KEY_NAME)
        self.assertEqual(self.table_count("tasks"), 0)


@requires_web
class ConsoleOverviewTests(ConsoleIntegrationFixture):
    def test_overview_snapshot_and_ceo_view_are_real_or_null(self) -> None:
        pid = self.make_project()
        self.make_task(pid, "k-o1")
        self.make_task(pid, "k-o2", title="second")
        snap = self.read(
            f"/api/v1/projects/{pid}/snapshot").json()
        self.assertEqual(snap["project"]["id"], pid)
        self.assertEqual(snap["counts"]["tasks_by_status"]["pending"], 2)
        self.assertGreater(snap["event_seq"], 0)
        self.assertIsNone(snap["ceo_initialization_state"])
        ceo = self.read(f"/api/v1/projects/{pid}/ceo").json()
        self.assertIsNone(ceo["initialization"])  # honest null, no fake row
        self.assertFalse(ceo["protocol"]["available"])
        sched = self.read(
            f"/api/v1/scheduler/state?project_id={pid}").json()["scheduler"]
        self.assertFalse(sched["paused"])

    def test_agents_wakes_approvals_verifications_are_honest_empty(self) -> None:
        pid = self.make_project()
        for path in (
            "/api/v1/agents",
            "/api/v1/members",
            f"/api/v1/wakes?project_id={pid}",
            f"/api/v1/approvals?project_id={pid}",
            f"/api/v1/verifications?project_id={pid}",
            f"/api/v1/recovery-actions?project_id={pid}",
        ):
            payload = self.read(path).json()
            self.assertEqual(payload["items"], [], path)
            self.assertEqual(payload["page"]["total"], 0, path)


@requires_web
class ConsoleTaskLifecycleTests(ConsoleIntegrationFixture):
    def test_create_replay_conflict_edit_priority_stale(self) -> None:
        pid = self.make_project()
        created = self.make_task(
            pid, "k-t1", title="first", acceptance=["a1"],
            ownership=["src/**"],
        )
        self.assertEqual(created["status"], "pending")
        task_id = created["id"]

        # identical re-submission under the same key replays (double click)
        replay = self.write("POST", "/api/v1/tasks", {
            "project_id": pid, "title": "first", "description": "",
            "kind": "ship", "priority": 50, "acceptance": ["a1"],
            "ownership": ["src/**"], "risk": "normal", "max_attempts": 3,
            "dependencies": [],
        }, "k-t1")
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.headers.get(REPLAY_HEADER), "true")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.table_count("tasks"), 1)

        # same key, different content -> conflict naming the first command
        clash = self.write("POST", "/api/v1/tasks", {
            "project_id": pid, "title": "different",
        }, "k-t1")
        body = self.assert_uniform(clash, 409, "CONFLICT")
        self.assertEqual(body["details"]["reason_code"],
                         "IDEMPOTENCY_KEY_REUSED")
        self.assertTrue(body["details"]["existing_command_id"])

        # stale version (what the UI surfaces as "reload before retry")
        stale = self.write("PATCH", f"/api/v1/tasks/{task_id}", {
            "expected_version": created["version"] + 7, "priority": 90,
        }, "k-stale")
        body = self.assert_uniform(stale, 409, "CONFLICT")
        self.assertEqual(body["details"]["reason_code"], "VERSION_CONFLICT")
        self.assertEqual(body["details"]["found"], created["version"])

        # priority edit with the current version applies and bumps evidence
        edited = self.write("PATCH", f"/api/v1/tasks/{task_id}", {
            "expected_version": created["version"], "priority": 91,
            "title": "first edited",
        }, "k-edit1")
        self.assertEqual(edited.status_code, 200, edited.text)
        payload = edited.json()
        self.assertEqual(payload["task"]["priority"], 91)
        self.assertEqual(payload["evidence"]["version"],
                         created["version"] + 1)

    def test_dependency_add_query_remove_and_cycle_path(self) -> None:
        pid = self.make_project()
        one = self.make_task(pid, "k-d1", title="one")
        two = self.make_task(pid, "k-d2", title="two")
        added = self.write("POST", f"/api/v1/tasks/{one['id']}/dependencies",
                          {"depends_on_id": two["id"],
                           "expected_version": one["version"]}, "k-dep1")
        self.assertEqual(added.status_code, 200, added.text)
        deps = self.read(
            f"/api/v1/tasks/{one['id']}/dependencies").json()
        self.assertEqual(deps["depends_on"], [two["id"]])
        deps2 = self.read(
            f"/api/v1/tasks/{two['id']}/dependencies").json()
        self.assertEqual(deps2["required_by"], [one["id"]])

        # closing the loop must return the concrete cycle for the UI banner
        current = self.read(f"/api/v1/tasks/{two['id']}").json()
        cyclic = self.write(
            "POST", f"/api/v1/tasks/{two['id']}/dependencies",
            {"depends_on_id": one["id"],
             "expected_version": current["version"]}, "k-dep2")
        body = self.assert_uniform(cyclic, 409, "CONFLICT")
        self.assertEqual(body["details"]["reason_code"], "DEPENDENCY_CYCLE")
        self.assertIn(one["id"], body["details"]["cycle"])
        self.assertIn(two["id"], body["details"]["cycle"])

        removed = self.write(
            "DELETE",
            f"/api/v1/tasks/{one['id']}/dependencies/{two['id']}"
            f"?expected_version={one['version'] + 1}",
            {}, "k-dep3")
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertTrue(removed.json()["removed"])

    def test_delivery_outside_the_closed_set_is_refused_without_residue(
            self) -> None:
        """Server-side backstop for the UI's closed select.

        Even a forged request whose delivery value is free text outside
        report_only/local_only/direct_pr/ship (what the form can no longer
        produce) must fail 422 VALIDATION_FAILED and roll back completely:
        no policy written, no version bump, no surviving command row.
        """
        pid = self.make_project()
        task = self.make_task(pid, "k-pol-closed")
        commands_before = self.table_count("commands")
        for bad in ("free_text", "report-only", "REPORT_ONLY", "shipping"):
            refused = self.write(
                "POST", f"/api/v1/tasks/{task['id']}/policy",
                {"expected_version": task["version"], "delivery": bad},
                "k-pol-closed-" + bad)
            self.assert_uniform(refused, 422, VALIDATION_FAILED)
        current = self.read(f"/api/v1/tasks/{task['id']}").json()
        self.assertEqual(current["version"], task["version"])
        self.assertNotIn("delivery", current["execution_policy"])
        self.assertEqual(self.table_count("commands"), commands_before)
        # the allowed values remain accepted through the console shape
        applied = self.write("POST", f"/api/v1/tasks/{task['id']}/policy", {
            "expected_version": task["version"], "delivery": "ship",
        }, "k-pol-closed-ok")
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["policy"]["delivery"], "ship")

    def test_policy_pause_resume_and_commands_via_console_shapes(self) -> None:
        pid = self.make_project()
        task = self.make_task(pid, "k-p1")
        policy = self.write("POST", f"/api/v1/tasks/{task['id']}/policy", {
            "expected_version": task["version"],
            "delivery": "report_only",
            "timeout_seconds": 120,
            "max_attempts": 5,
        }, "k-policy1")
        self.assertEqual(policy.status_code, 200, policy.text)
        payload = policy.json()
        self.assertEqual(payload["policy"]["delivery"], "report_only")
        self.assertEqual(payload["task"]["max_attempts"], 5)

        paused = self.write("POST", f"/api/v1/tasks/{task['id']}/commands", {
            "action": "pause", "expected_version": payload["task"]["version"],
            "actor": "web-console", "reason": "operator hold",
        }, "k-pause1")
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertTrue(paused.json()["paused"])

        resumed = self.write("POST", f"/api/v1/tasks/{task['id']}/commands", {
            "action": "resume",
            "expected_version": paused.json()["task"]["version"],
            "actor": "web-console", "reason": "operator release",
        }, "k-resume1")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertFalse(resumed.json()["paused"])

    def test_scheduler_pause_resume_never_touches_lease(self) -> None:
        pid = self.make_project()
        paused = self.write("POST", "/api/v1/scheduler/pause",
                           {"project_id": pid, "actor": "web-console",
                            "reason": "hold dispatch"}, "k-shpause")
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertTrue(paused.json()["scheduler"]["paused"])
        state = self.read(
            f"/api/v1/scheduler/state?project_id={pid}").json()["scheduler"]
        self.assertTrue(state["paused"])
        self.assertEqual(state["actor"], "web-console")
        resumed = self.write("POST", "/api/v1/scheduler/resume",
                            {"project_id": pid, "actor": "web-console",
                             "reason": "dispatch again"}, "k-shresume")
        self.assertFalse(resumed.json()["scheduler"]["paused"])
        conn = storage.connect(self.db_path)
        try:
            leases = conn.execute(
                "SELECT COUNT(*) FROM leader_lease").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(leases), 0)

    def test_manual_ceo_wake_records_intent_only(self) -> None:
        pid = self.make_project()
        queued = self.write("POST", "/api/v1/ceo/commands", {
            "action": "request_processing", "project_id": pid,
            "actor": "web-console", "reason": "please take a look",
        }, "k-ceo1")
        self.assertEqual(queued.status_code, 202, queued.text)
        body = queued.json()
        self.assertEqual(body["execution"], "queued")
        self.assertEqual(body["command"]["kind"], "ceo.wake_request")
        self.assertEqual(body["command"]["state"], "pending")
        # the queue-only guarantee: no wake row, no attempt, no prompt
        self.assertEqual(self.table_count("wakes"), 0)
        self.assertEqual(self.table_count("wake_events"), 0)
        self.assertEqual(self.table_count("attempts"), 0)
        self.assertEqual(self.table_count("prompt_deliveries"), 0)
        events = self.read(f"/api/v1/events?project_id={pid}&limit=200")\
            .json()["items"]
        kinds = [event["kind"] for event in events]
        self.assertIn("ceo.wake_requested", kinds)

    def test_hostile_free_text_round_trips_as_data_never_markup(self) -> None:
        pid = self.make_project()
        hostile = '<img src=x onerror=alert(1)>'
        scripty = '</script><script>alert("xss")</script>'
        task = self.make_task(pid, "k-x1", title=hostile,
                             description=scripty)
        fetched = self.read(f"/api/v1/tasks/{task['id']}")
        self.assertTrue(
            fetched.headers["content-type"].startswith("application/json")
        )
        payload = fetched.json()
        self.assertEqual(payload["title"], hostile)
        self.assertEqual(payload["description"], scripty)
        # the API hands structured JSON only; the console attaches values
        # via text nodes (statically pinned in ConsoleAssetTests), so the
        # hostile markup is inert text, never an executed script.

    def test_long_stored_text_surfaces_truncation_flags(self) -> None:
        pid = self.make_project()
        task = self.make_task(pid, "k-trunc", title="bounded title")
        # API-side writes are byte-bounded at the boundary itself; text
        # persisted beyond the query clip (older rows, core-side paths)
        # must reach the browser flagged, never silently clipped.
        conn = storage.connect(self.db_path)
        try:
            domain.update_task(conn, task["id"],
                              expected_version=task["version"],
                              description="D" * 5000)
        finally:
            conn.close()
        payload = self.read(f"/api/v1/tasks/{task['id']}").json()
        self.assertTrue(payload["description_truncated"])
        self.assertLessEqual(
            len(payload["description"].encode("utf-8")), 4096
        )

    def test_events_page_by_durable_seq_and_flag_progress_classes(self) -> None:
        pid = self.make_project()
        self.make_task(pid, "k-e1")
        self.make_task(pid, "k-e2", title="again")
        page = self.read(f"/api/v1/events?project_id={pid}&limit=200").json()
        seqs = [item["seq"] for item in page["items"]]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertGreater(len(seqs), 1)
        after = self.read(
            f"/api/v1/events?project_id={pid}&after_seq={seqs[0]}").json()
        self.assertEqual(after["items"][0]["seq"], seqs[1])
        for item in page["items"]:
            # control-plane kinds carry no progress class; advisory only
            if not item["kind"].startswith(("watch.", "worker.")):
                self.assertIsNone(item["progress_class"])

    def test_sse_tail_ids_are_durable_and_strictly_increasing(self) -> None:
        fast = SseConfig(poll_interval_seconds=0.05,
                        heartbeat_interval_seconds=0.05,
                        backfill_batch_size=10,
                        max_backfill_events=100)
        pid = self.make_project()
        self.make_task(pid, "k-s1")
        self.make_task(pid, "k-s2", title="second")
        app = create_app(
            token_store=self.app.state.token_store,
            query_service=QueryService(self.db_path),
            command_service=CommandService(self.db_path),
            static_root=WEB_ROOT,
            event_stream_config=fast,
        )
        server, thread, base = _start_server(app)
        self.addCleanup(_stop_server, server, thread)
        # the console's own shell is served by the same confined route on
        # the real transport, unauthenticated
        with httpx.Client(base_url=base, timeout=10) as client:
            shell = client.get("/static/index.html")
            self.assertEqual(shell.status_code, 200)
            anon = client.get(f"/api/v1/events/stream?project_id={pid}")
            self.assertEqual(anon.status_code, 401)
            seen: list[int] = []
            with client.stream(
                "GET", f"/api/v1/events/stream?project_id={pid}&after=0",
                headers={"Authorization": f"Bearer {self.token}"},
            ) as stream:
                self.assertEqual(stream.status_code, 200)
                self.assertTrue(stream.headers["content-type"].startswith(
                    "text/event-stream"))
                started = time.monotonic()
                for line in stream.iter_lines():
                    if line.startswith("id: "):
                        seen.append(int(line[4:]))
                    if len(seen) >= 2 or time.monotonic() - started > 8:
                        break
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen, sorted(set(seen)))
        # the ids are exactly the durable seqs the REST surface reports
        rest = self.read(f"/api/v1/events?project_id={pid}&limit=2").json()
        self.assertEqual(seen[:2],
                        [item["seq"] for item in rest["items"]])

    def test_session_token_never_appears_in_served_bodies(self) -> None:
        pid = self.make_project()
        self.make_task(pid, "k-sec")
        for path in (
            "/static/index.html", "/static/app.js", "/static/style.css",
            "/api/v1/projects",
            f"/api/v1/projects/{pid}",
            f"/api/v1/projects/{pid}/snapshot",
            f"/api/v1/projects/{pid}/tasks",
            f"/api/v1/projects/{pid}/ceo",
            f"/api/v1/scheduler/state?project_id={pid}",
            f"/api/v1/wakes?project_id={pid}",
            f"/api/v1/approvals?project_id={pid}",
            f"/api/v1/verifications?project_id={pid}",
            f"/api/v1/recovery-actions?project_id={pid}",
            "/api/v1/agents", "/api/v1/members",
            f"/api/v1/events?project_id={pid}",
        ):
            response = self.read(path)
            self.assertIn(response.status_code, (200, 404), path)
            if response.status_code == 200:
                self.assertNotIn(self.token, response.text, path)


@requires_web
class ConsoleBackgroundWorkTests(ConsoleIntegrationFixture):
    """No browser-side authority: disconnecting changes nothing durable."""

    def test_durable_intent_survives_without_any_client(self) -> None:
        pid = self.make_project()
        queued = self.write("POST", "/api/v1/ceo/commands", {
            "action": "request_processing", "project_id": pid,
            "actor": "web-console", "reason": "queued while browser lives",
        }, "k-bg")
        command_id = queued.json()["command"]["command_id"]
        # simulate the browser going away: drop the client entirely and
        # read the same fact back through a fresh guarded client — the
        # durable command is untouched because the console was never the
        # executor of anything.
        del self.client
        second = TestClient(create_app(
            token_store=self.app.state.token_store,
            query_service=QueryService(self.db_path),
            command_service=CommandService(self.db_path),
        ))
        token = self.app.state.token_store.ensure()
        fetched = second.get(
            f"/api/v1/commands/{command_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["command"]["idempotency_key"],
                         "k-bg")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
