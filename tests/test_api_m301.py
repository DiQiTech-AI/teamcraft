"""M3-01 API security shell tests.

Covers the acceptance line from doc/TEAMCRAFT_2_0_M0_REVIEW.md (M3-01):
unauthorized / wrong-Origin / CSRF-missing writes are rejected; the server
defaults to loopback; logs never print the session token; health distinguishes
process liveness from dependency health.  Plus the uniform error model, request
correlation, static path confinement, and token storage/rotation behaviour.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:  # the API shell needs the web stack; core tests must stay runnable without it
    from fastapi.testclient import TestClient

    from teamcraft.api import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        STATUS_DEGRADED,
        STATUS_OK,
        STATUS_UNKNOWN,
        ApiConfig,
        ApiError,
        DependencyStatus,
        HealthRegistry,
        SessionTokenStore,
        StaticPathError,
        aggregate_status,
        create_app,
        is_loopback_host,
        load_api_config,
        resolve_static_path,
    )
    from teamcraft.api.errors import (
        CSRF_CHECK_FAILED,
        FORBIDDEN_ORIGIN,
        INTERNAL_ERROR,
        NOT_FOUND,
        PATH_FORBIDDEN,
        SERVICE_UNAVAILABLE,
        UNAUTHENTICATED,
        VALIDATION_FAILED,
    )
    from teamcraft.api.redaction import (
        REDACTION_MARKER,
        SecretRedactingFilter,
        install_redaction,
        redact_secrets,
    )
    from teamcraft.api.request_context import resolve_request_id, sanitize_request_id

    _WEB_STACK_OK = True
    _WEB_STACK_REASON = ""
except ImportError as exc:  # pragma: no cover - depends on CI install order
    _WEB_STACK_OK = False
    _WEB_STACK_REASON = f"web dependencies unavailable: {exc}"

from teamcraft.errors import ConfigurationError

requires_web = unittest.skipUnless(_WEB_STACK_OK, _WEB_STACK_REASON)

ERROR_KEYS = {"code", "message", "details", "request_id"}
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")


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
    """Capture all log output (including propagated children) as text."""

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


@requires_web
class LoopbackDefaultTests(unittest.TestCase):
    """Service must not face outward by default (PLAN §9.2-1)."""

    def test_default_host_is_loopback(self) -> None:
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")
        self.assertTrue(is_loopback_host(DEFAULT_HOST))
        self.assertEqual(ApiConfig().host, "127.0.0.1")
        self.assertEqual(load_api_config(environ={}).host, "127.0.0.1")
        self.assertEqual(load_api_config(environ={}).port, DEFAULT_PORT)

    def test_loopback_recognition(self) -> None:
        for host in ("127.0.0.1", "127.5.6.7", "localhost", "LOCALHOST", "::1", "[::1]"):
            self.assertTrue(is_loopback_host(host), host)
        for host in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.1", "example.com", ""):
            self.assertFalse(is_loopback_host(host), host)

    def test_non_loopback_bind_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_api_config(environ={}, host="0.0.0.0")
        with self.assertRaises(ConfigurationError):
            load_api_config(environ={}, host="192.168.1.5")
        # explicit acknowledgement unlocks it, auth stays mandatory regardless
        config = load_api_config(
            environ={}, host="0.0.0.0", allow_non_loopback=True
        )
        self.assertEqual(config.host, "0.0.0.0")
        self.assertTrue(config.allow_non_loopback)

    def test_environment_non_loopback_still_needs_acknowledgement(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_api_config(environ={"TEAMCRAFT_API_HOST": "0.0.0.0"})
        config = load_api_config(
            environ={
                "TEAMCRAFT_API_HOST": "0.0.0.0",
                "TEAMCRAFT_API_ALLOW_NON_LOOPBACK": "true",
            }
        )
        self.assertEqual(config.host, "0.0.0.0")

    def test_invalid_port_rejected(self) -> None:
        for port in (0, -1, 70000):
            with self.assertRaises(ConfigurationError):
                load_api_config(environ={}, port=port)
        with self.assertRaises(ConfigurationError):
            load_api_config(environ={"TEAMCRAFT_API_PORT": "not-a-port"})
        with self.assertRaises(ConfigurationError):
            load_api_config(environ={}, host="   ")

    def test_serve_cli_defaults_to_loopback_without_binding(self) -> None:
        from teamcraft.api.serve import make_server

        with tempfile.TemporaryDirectory() as temporary:
            server, _store, _args = make_server(["--project-root", temporary])
            self.assertEqual(server.config.host, "127.0.0.1")
            self.assertEqual(server.config.port, DEFAULT_PORT)

    def test_serve_cli_refuses_non_loopback_bind(self) -> None:
        from teamcraft.api.serve import main

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "uvicorn.Server.run"
        ) as run, contextlib.redirect_stderr(stderr):
            code = main(["--host", "0.0.0.0", "--project-root", temporary])
        self.assertEqual(code, 2)
        self.assertIn("non-loopback", stderr.getvalue())
        self.assertEqual(stderr.getvalue().count("teamcraft-api:"), 1)
        run.assert_not_called()


@requires_web
class SessionTokenStoreTests(unittest.TestCase):
    """Random local token with safe storage and rotation."""

    posix_only = unittest.skipUnless(os.name == "posix", "POSIX permissions")

    def test_generated_tokens_are_random_and_urlsafe(self) -> None:
        tokens = {SessionTokenStore.generate_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for token in tokens:
            self.assertGreaterEqual(len(token), 43)  # 32 bytes urlsafe
            self.assertRegex(token, r"^[A-Za-z0-9_-]+$")

    @posix_only
    def test_token_file_is_owner_only_and_dir_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".teamcraft" / "api" / "session-token"
            store = SessionTokenStore(path)
            token = store.ensure()
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, oct(mode))
            directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
            self.assertEqual(directory_mode, 0o700, oct(directory_mode))
            self.assertEqual(path.read_text().strip(), token)
            # no leftover temp files next to the token
            leftovers = [p.name for p in path.parent.iterdir()]
            self.assertEqual(leftovers, ["session-token"])

    def test_token_persists_across_process_restart_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session-token"
            first = SessionTokenStore(path).ensure()
            second = SessionTokenStore(path)
            self.assertEqual(second.ensure(), first)
            self.assertTrue(second.verify(first))

    def test_rotation_invalidates_previous_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session-token"
            store = SessionTokenStore(path)
            old = store.ensure()
            new = store.rotate()
            self.assertNotEqual(old, new)
            self.assertTrue(store.verify(new))
            self.assertFalse(store.verify(old))
            # a freshly loaded store sees the rotated value only
            reloaded = SessionTokenStore(path)
            self.assertEqual(reloaded.ensure(), new)
            self.assertFalse(reloaded.verify(old))

    def test_corrupted_token_file_is_replaced_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session-token"
            path.write_text("short\n", encoding="utf-8")
            store = SessionTokenStore(path)
            token = store.ensure()
            self.assertNotEqual(token, "short")
            self.assertTrue(store.verify(token))
            self.assertFalse(store.verify("short"))

    def test_in_memory_store_never_touches_disk(self) -> None:
        store = SessionTokenStore()
        self.assertIsNone(store.path)
        token = store.ensure()
        self.assertTrue(store.verify(token))

    def test_verify_rejects_missing_or_empty_and_uses_constant_time_compare(
        self,
    ) -> None:
        store = SessionTokenStore()
        token = store.ensure()
        self.assertFalse(store.verify(None))
        self.assertFalse(store.verify(""))
        self.assertFalse(store.verify("x" * 64))
        with mock.patch(
            "teamcraft.api.security.hmac.compare_digest",
            side_effect=lambda a, b: a == b,
        ) as compare:
            self.assertTrue(store.verify(token))
            compare.assert_called_once()

    def test_fingerprint_is_stable_and_not_the_token(self) -> None:
        store = SessionTokenStore()
        token = store.ensure()
        fingerprint = store.token_fingerprint()
        self.assertEqual(fingerprint, store.token_fingerprint())
        self.assertNotEqual(fingerprint, token)
        self.assertRegex(fingerprint, r"^[0-9a-f]{12}$")


@requires_web
class AuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionTokenStore()
        self.app = create_app(token_store=self.store)
        self.client = TestClient(self.app)
        self.token = self.store.token()

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def test_missing_token_is_401_with_uniform_error(self) -> None:
        response = self.client.get("/api/v1/ping")
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(set(body), ERROR_KEYS)
        self.assertEqual(body["code"], UNAUTHENTICATED)
        self.assertIn("request_id", json.dumps(body))
        self.assertEqual(body["request_id"], response.headers["X-Request-Id"])

    def test_wrong_token_is_401_and_never_echoed(self) -> None:
        response = self.client.get("/api/v1/ping", headers=self.auth("wrong-token"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], UNAUTHENTICATED)
        self.assertNotIn("wrong-token", response.text)
        self.assertNotIn(self.token, response.text)

    def test_non_bearer_scheme_is_rejected(self) -> None:
        response = self.client.get(
            "/api/v1/ping", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        self.assertEqual(response.status_code, 401)

    def test_valid_token_grants_access(self) -> None:
        response = self.client.get("/api/v1/ping", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["pong"])

    def test_token_rotation_locks_out_old_bearer_immediately(self) -> None:
        before = self.client.get("/api/v1/ping", headers=self.auth())
        self.assertEqual(before.status_code, 200)
        new_token = self.store.rotate()
        stale = self.client.get("/api/v1/ping", headers=self.auth())
        self.assertEqual(stale.status_code, 401)
        fresh = self.client.get("/api/v1/ping", headers=self.auth(new_token))
        self.assertEqual(fresh.status_code, 200)

    def test_health_surface_is_public_but_token_gated_business(self) -> None:
        for path in ("/api/v1/health", "/api/v1/health/live", "/api/v1/health/ready"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/api/v1/ping").status_code, 401)

    def test_token_in_query_string_is_never_accepted(self) -> None:
        # secrets belong in headers only; URL tokens leak via logs/history
        response = self.client.get(
            f"/api/v1/ping?token={self.token}&access_token={self.token}"
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(self.token, response.text)

    def test_no_cors_grant_headers_are_emitted(self) -> None:
        # this API is same-origin only; a cross-site page must be able neither
        # to send nor to read anything
        response = self.client.get(
            "/api/v1/ping",
            headers={"Authorization": f"Bearer {self.token}", "Origin": "http://evil.example"},
        )
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in response.headers})
        preflight = self.client.options("/api/v1/ping")
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in preflight.headers})


@requires_web
class OriginAndCsrfTests(unittest.TestCase):
    SAME_ORIGIN = "http://testserver"

    def setUp(self) -> None:
        self.store = SessionTokenStore()
        self.app = create_app(token_store=self.store)
        self.client = TestClient(self.app)
        self.token = self.store.token()

    def headers(self, **extra: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(extra)
        return headers

    def test_foreign_origin_write_rejected_even_with_csrf(self) -> None:
        response = self.client.post(
            "/api/v1/_probe",
            headers=self.headers(Origin="http://evil.example", **{"X-CSRF-Token": self.token}),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], FORBIDDEN_ORIGIN)
        self.assertEqual(set(response.json()), ERROR_KEYS)

    def test_same_origin_write_with_csrf_accepted(self) -> None:
        response = self.client.post(
            "/api/v1/_probe",
            headers=self.headers(
                Origin=self.SAME_ORIGIN, **{"X-CSRF-Token": self.token}
            ),
            json={"note": "ok"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["accepted"])

    def test_write_without_csrf_header_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/_probe", headers=self.headers(Origin=self.SAME_ORIGIN)
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], CSRF_CHECK_FAILED)

    def test_write_with_wrong_csrf_header_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/_probe",
            headers=self.headers(
                Origin=self.SAME_ORIGIN, **{"X-CSRF-Token": "attacker-value"}
            ),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], CSRF_CHECK_FAILED)

    def test_csrf_header_alone_cannot_authorize(self) -> None:
        response = self.client.post(
            "/api/v1/_probe",
            headers={"Origin": self.SAME_ORIGIN, "X-CSRF-Token": "whatever"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], UNAUTHENTICATED)

    def test_headerless_write_from_local_cli_allowed_with_csrf(self) -> None:
        # non-browser clients send no Origin; the CSRF header must still match
        response = self.client.post(
            "/api/v1/_probe",
            headers=self.headers(**{"X-CSRF-Token": self.token}),
        )
        self.assertEqual(response.status_code, 200)

    def test_all_unsafe_methods_require_csrf(self) -> None:
        for method in ("post", "put", "patch", "delete"):
            without_csrf = self.client.request(
                method.upper(), "/api/v1/_probe", headers=self.headers()
            )
            self.assertEqual(without_csrf.status_code, 403, method)
            self.assertEqual(without_csrf.json()["code"], CSRF_CHECK_FAILED, method)
        # GET is exempt from CSRF (still needs the token)
        self.assertEqual(
            self.client.get("/api/v1/ping", headers=self.headers()).status_code, 200
        )

    def test_extra_origin_allowlist_honored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_api_config(
                environ={},
                project_root=temporary,
                extra_origins=("https://ui.localhost:3000",),
            )
        app = create_app(token_store=self.store, config=config)
        client = TestClient(app)
        response = client.post(
            "/api/v1/_probe",
            headers=self.headers(
                Origin="https://ui.localhost:3000", **{"X-CSRF-Token": self.token}
            ),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_origin_check_is_case_and_trailing_slash_tolerant(self) -> None:
        response = self.client.post(
            "/api/v1/_probe",
            headers=self.headers(
                Origin="HTTP://TESTSERVER", **{"X-CSRF-Token": self.token}
            ),
        )
        self.assertEqual(response.status_code, 200, response.text)


@requires_web
class RequestContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionTokenStore()
        self.client = TestClient(create_app(token_store=self.store))
        self.token = self.store.token()

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def test_generated_request_id_on_every_response(self) -> None:
        success = self.client.get("/api/v1/ping", headers=self.auth())
        denial = self.client.get("/api/v1/ping")
        for response in (success, denial):
            request_id = response.headers.get("X-Request-Id")
            self.assertIsNotNone(request_id)
            self.assertRegex(request_id, _HEX_ID)
        self.assertNotEqual(
            success.headers["X-Request-Id"], denial.headers["X-Request-Id"]
        )

    def test_valid_inbound_request_id_echoed(self) -> None:
        response = self.client.get(
            "/api/v1/ping", headers={**self.auth(), "X-Request-Id": "trace-42_9"}
        )
        self.assertEqual(response.headers["X-Request-Id"], "trace-42_9")
        self.assertEqual(response.json()["request_id"], "trace-42_9")

    def test_hostile_inbound_request_id_replaced(self) -> None:
        for hostile in (
            "<script>alert(1)</script>",
            "has space",
            "x" * 200,
            "line1\nline2",
            "   ",
        ):
            response = self.client.get(
                "/api/v1/ping", headers={**self.auth(), "X-Request-Id": hostile}
            )
            self.assertEqual(response.status_code, 200, hostile)
            issued = response.headers["X-Request-Id"]
            self.assertNotIn(issued, hostile)
            self.assertRegex(issued, _HEX_ID, hostile)
            self.assertEqual(issued, response.json()["request_id"])

    def test_sanitizer_helpers(self) -> None:
        self.assertEqual(sanitize_request_id("abc-123.def:ghi"), "abc-123.def:ghi")
        self.assertIsNone(sanitize_request_id("bad id"))
        self.assertIsNone(sanitize_request_id(None))
        self.assertIsNone(sanitize_request_id("x" * 129))
        self.assertRegex(resolve_request_id(None), _HEX_ID)
        self.assertEqual(resolve_request_id("ok"), "ok")


@requires_web
class UniformErrorModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionTokenStore()
        self.app = create_app(token_store=self.store)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.token = self.store.token()

    def auth(self, **extra: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(extra)
        return headers

    def _assert_uniform(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status)
        body = response.json()
        self.assertEqual(set(body), ERROR_KEYS)
        self.assertEqual(body["code"], code)
        self.assertTrue(isinstance(body["message"], str) and body["message"])
        self.assertTrue(body["request_id"])
        self.assertEqual(body["request_id"], response.headers["X-Request-Id"])
        return body

    def test_404_405_422_all_uniform(self) -> None:
        self._assert_uniform(
            self.client.get("/api/v1/does-not-exist", headers=self.auth()),
            404,
            NOT_FOUND,
        )
        self._assert_uniform(
            self.client.put(
                "/api/v1/ping",
                headers=self.auth(**{"X-CSRF-Token": self.token}),
            ),
            405,
            "METHOD_NOT_ALLOWED",
        )
        body = self._assert_uniform(
            self.client.post(
                "/api/v1/_probe",
                headers=self.auth(**{"X-CSRF-Token": self.token}),
                json={"note": 12345},
            ),
            422,
            VALIDATION_FAILED,
        )
        self.assertEqual(body["details"][0]["field"], "body.note")

    def test_unhandled_exception_is_generic_and_non_leaky(self) -> None:
        secret = self.token

        def boom() -> None:
            raise ValueError(f"database at /home/operator/.teamcraft/db for {secret}")

        self.app.add_api_route("/boom", boom, methods=["GET"])
        with capture_logs() as logs:
            response = self.client.get("/boom", headers=self.auth())
        body = self._assert_uniform(response, 500, INTERNAL_ERROR)
        self.assertEqual(body["message"], "Internal server error.")
        self.assertNotIn("operator", response.text)
        self.assertNotIn(secret, response.text)
        captured = "\n".join(logs.lines)
        self.assertIn("unhandled error", captured)
        self.assertNotIn(secret, captured)
        self.assertIn(REDACTION_MARKER, captured)

    def test_api_error_carries_details(self) -> None:
        def unavailable() -> None:
            raise ApiError(
                503, SERVICE_UNAVAILABLE, "Backend offline.", details={"retry": True}
            )

        self.app.add_api_route("/down", unavailable, methods=["GET"])
        body = self._assert_uniform(
            self.client.get("/down", headers=self.auth()), 503, SERVICE_UNAVAILABLE
        )
        self.assertEqual(body["details"], {"retry": True})

    def test_error_responses_are_not_cacheable(self) -> None:
        response = self.client.get("/api/v1/ping")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")


@requires_web
class HealthSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = HealthRegistry()
        self.store = SessionTokenStore()
        self.client = TestClient(
            create_app(token_store=self.store, health_registry=self.registry)
        )

    def test_empty_registry_reports_process_up_no_dependencies(self) -> None:
        body = self.client.get("/api/v1/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["process"]["status"], "up")
        self.assertIsInstance(body["process"]["pid"], int)
        self.assertEqual(body["dependencies"], {})

    def test_dependency_failure_does_not_mask_process_liveness(self) -> None:
        self.registry.register("storage", lambda: True)
        self.registry.register("herdr", lambda: False)
        aggregate = self.client.get("/api/v1/health")
        self.assertEqual(aggregate.status_code, 200)
        body = aggregate.json()
        self.assertEqual(body["status"], STATUS_DEGRADED)
        self.assertEqual(body["process"]["status"], "up")
        self.assertEqual(
            body["dependencies"], {"herdr": {"status": "down"}, "storage": {"status": "up"}}
        )
        # liveness stays green: only readiness reflects the dependency
        live = self.client.get("/api/v1/health/live")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["status"], "alive")
        ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 503)
        payload = ready.json()
        self.assertEqual(set(payload), ERROR_KEYS)
        self.assertEqual(payload["code"], SERVICE_UNAVAILABLE)
        self.assertEqual(payload["details"], {"not_ready": [{"name": "herdr", "status": "down"}]})

    def test_unknown_dependency_is_not_ready(self) -> None:
        """CEO review fix: UNKNOWN must block readiness, never read as ready."""

        self.registry.register("herdr", lambda: None)
        ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 503)
        payload = ready.json()
        self.assertEqual(set(payload), ERROR_KEYS)
        self.assertEqual(payload["code"], SERVICE_UNAVAILABLE)
        self.assertEqual(payload["details"], {"not_ready": [{"name": "herdr", "status": "unknown"}]})
        self.assertNotIn("status", payload)  # error shape only, never a success status
        # aggregate must not claim "ok" either
        aggregate = self.client.get("/api/v1/health")
        self.assertEqual(aggregate.status_code, 200)
        self.assertEqual(aggregate.json()["status"], STATUS_UNKNOWN)
        self.assertEqual(aggregate.json()["dependencies"]["herdr"]["status"], "unknown")
        self.assertEqual(aggregate.json()["process"]["status"], "up")
        # liveness is unaffected: the process is genuinely running
        live = self.client.get("/api/v1/health/live")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["status"], "alive")

    def test_down_outranks_unknown_in_aggregate_but_both_block_readiness(self) -> None:
        self.registry.register("storage", lambda: False)
        self.registry.register("herdr", lambda: None)
        aggregate = self.client.get("/api/v1/health").json()
        self.assertEqual(aggregate["status"], STATUS_DEGRADED)
        ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(
            ready.json()["details"],
            {
                "not_ready": [
                    {"name": "herdr", "status": "unknown"},
                    {"name": "storage", "status": "down"},
                ]
            },
        )
        self.assertEqual(self.client.get("/api/v1/health/live").status_code, 200)

    def test_readiness_requires_every_dependency_up(self) -> None:
        self.registry.register("cache", lambda: True)
        self.registry.register("storage", lambda: True)
        self.assertEqual(self.client.get("/api/v1/health/ready").status_code, 200)
        self.registry.register("herdr", lambda: None)
        ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 503)
        names = [entry["name"] for entry in ready.json()["details"]["not_ready"]]
        self.assertEqual(names, ["herdr"])

    def test_recovering_dependency_flips_readiness_back(self) -> None:
        state = {"up": False}
        self.registry.register("storage", lambda: state["up"])
        self.assertEqual(self.client.get("/api/v1/health/ready").status_code, 503)
        state["up"] = True
        ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["dependencies"]["storage"]["status"], "up")

    def test_unproven_dependency_resolving_to_up_restores_ok(self) -> None:
        conclusions: list[object] = [None]
        self.registry.register("herdr", lambda: conclusions[-1])
        self.assertEqual(self.client.get("/api/v1/health/ready").status_code, 503)
        self.assertEqual(
            self.client.get("/api/v1/health").json()["status"], STATUS_UNKNOWN
        )
        conclusions.append(True)
        self.assertEqual(self.client.get("/api/v1/health/ready").status_code, 200)
        self.assertEqual(
            self.client.get("/api/v1/health").json()["status"], STATUS_OK
        )

    def test_probe_exception_detail_never_reaches_client(self) -> None:
        def broken() -> None:
            raise RuntimeError("cannot open sqlite:/home/operator/.teamcraft/db.sqlite")

        self.registry.register("storage", broken)
        with capture_logs() as logs:
            ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 503)
        self.assertNotIn("operator", ready.text)
        self.assertNotIn("sqlite", ready.text)
        self.assertNotIn(".teamcraft", ready.text)
        # names and coarse status only
        self.assertEqual(
            ready.json()["details"], {"not_ready": [{"name": "storage", "status": "down"}]}
        )
        self.assertIn("storage", "\n".join(logs.lines))

    def test_empty_registry_is_ready_and_ok(self) -> None:
        ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["dependencies"], {})
        self.assertEqual(self.client.get("/api/v1/health").json()["status"], STATUS_OK)


@requires_web
class AggregateStatusTests(unittest.TestCase):
    """Pure aggregate collapse, including the UNKNOWN rule."""

    def test_confirmed_failure_wins(self) -> None:
        self.assertEqual(
            aggregate_status(["up", "down", "unknown"]), STATUS_DEGRADED
        )
        self.assertEqual(aggregate_status(["down"]), STATUS_DEGRADED)

    def test_unproven_yields_unknown_not_ok(self) -> None:
        self.assertEqual(aggregate_status(["up", "unknown"]), STATUS_UNKNOWN)
        self.assertEqual(aggregate_status(["unknown"]), STATUS_UNKNOWN)

    def test_all_up_or_nothing_is_ok(self) -> None:
        self.assertEqual(aggregate_status(["up", "up"]), STATUS_OK)
        self.assertEqual(aggregate_status([]), STATUS_OK)

    def test_accepts_status_enum_members(self) -> None:
        self.assertEqual(
            aggregate_status([DependencyStatus.UP, DependencyStatus.DOWN]),
            STATUS_DEGRADED,
        )
        self.assertEqual(
            aggregate_status([DependencyStatus.UP, DependencyStatus.UNKNOWN]),
            STATUS_UNKNOWN,
        )


@requires_web
class StaticPathBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "static"
        (self.root / "assets").mkdir(parents=True)
        (self.root / "index.html").write_text("<h1>ui</h1>", encoding="utf-8")
        (self.root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=1", encoding="utf-8")
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "credential.txt").write_text("bearcoken", encoding="utf-8")
        self.link = self.root / "escape"
        try:
            self.link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - restricted FS
            self.link = None

        self.store = SessionTokenStore()
        self.app = create_app(token_store=self.store, static_root=self.root)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_serves_confined_files(self) -> None:
        response = self.client.get("/static/index.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>ui</h1>", response.text)
        nested = self.client.get("/static/assets/app.js")
        self.assertEqual(nested.status_code, 200)

    def test_traversal_and_absolute_paths_rejected_by_resolver(self) -> None:
        hostile = [
            "../outside/credential.txt",
            "./../outside/credential.txt",
            "assets/../../outside/credential.txt",
            "%2e%2e/outside/credential.txt",
            "%2e%2e%2f%2e%2e%2fetc/passwd",
            "/etc/passwd",
            "\\outside\\credential.txt",
            "C:/Windows/win.ini",
            "assets/%00.png",
            "assets/../.env",
            ".env",
            ".teamcraft/api/session-token",
            "..",
            "",
        ]
        for candidate in hostile:
            with self.assertRaises(StaticPathError, msg=candidate):
                resolve_static_path(self.root, candidate)

    def test_encoded_traversal_over_http_rejected_uniformly(self) -> None:
        response = self.client.get("/static/%2e%2e%2foutside%2fcredential.txt")
        self.assertEqual(response.status_code, 403, response.text)
        body = response.json()
        self.assertEqual(body["code"], PATH_FORBIDDEN)
        self.assertEqual(set(body), ERROR_KEYS)
        self.assertNotIn("bearcoken", response.text)

    def test_dotfiles_never_served(self) -> None:
        response = self.client.get("/static/.env")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("SECRET", response.text)

    def test_symlink_outside_root_rejected_by_resolver(self) -> None:
        if self.link is None:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(StaticPathError):
            resolve_static_path(self.root, "escape/credential.txt")

    def test_missing_and_directory_are_404(self) -> None:
        missing = self.client.get("/static/nope.html")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], NOT_FOUND)
        directory = self.client.get("/static/assets")
        self.assertEqual(directory.status_code, 404)

    def test_static_route_absent_when_no_root_configured(self) -> None:
        client = TestClient(create_app(token_store=self.store))
        response = client.get("/static/index.html")
        self.assertEqual(response.status_code, 404)

    def test_runtime_token_directory_never_servable_even_at_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            token_file = project / ".teamcraft" / "api" / "session-token"
            store = SessionTokenStore(token_file)
            token = store.ensure()
            client = TestClient(create_app(token_store=store, static_root=project))
            response = client.get("/static/.teamcraft/api/session-token")
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(token, response.text)


@requires_web
class NoTokenInLogsTests(unittest.TestCase):
    def test_redaction_replaces_all_secret_occurrences(self) -> None:
        secret = "Zm9vYmFyYmF6IHF1dXg"
        text = f"auth header Bearer {secret} retried {secret}"
        redacted = redact_secrets(text, (secret,))
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted.count(REDACTION_MARKER), 2)

    def test_longest_secret_wins_for_overlapping_values(self) -> None:
        redacted = redact_secrets(
            "prefix-secretvalue and secretvalue", ("secretvalue", "prefix-secretvalue")
        )
        self.assertEqual(redacted, f"{REDACTION_MARKER} and {REDACTION_MARKER}")

    def test_redacting_filter_leaves_clean_records_alone(self) -> None:
        secret_filter = SecretRedactingFilter()
        secret_filter.register("sauce")
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "all good", (), None)
        self.assertTrue(secret_filter.filter(record))
        self.assertEqual(record.getMessage(), "all good")

    def test_token_never_appears_in_logs_for_authenticated_traffic(self) -> None:
        store = SessionTokenStore()
        app = create_app(token_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        token = store.token()
        with capture_logs() as logs:
            client.get("/api/v1/ping", headers={"Authorization": f"Bearer {token}"})
            client.get("/api/v1/ping")  # 401 path
            client.post("/api/v1/_probe", headers={"Authorization": f"Bearer {token}"})
            client.get("/boom-ish", headers={"Authorization": f"Bearer {token}"})
        captured = "\n".join(logs.lines)
        self.assertNotIn(token, captured)
        self.assertNotIn(f"Bearer {token}", captured)

    def test_accidental_secret_logging_is_scrubbed(self) -> None:
        store = SessionTokenStore()
        token = store.token()
        app = create_app(token_store=store)

        def leaky() -> None:
            # Simulates a future bug that logs the token: redaction must catch it.
            raise RuntimeError(f"token leaked: {token}")

        app.add_api_route("/leak", leaky, methods=["GET"])
        client = TestClient(app, raise_server_exceptions=False)
        with capture_logs() as logs:
            response = client.get("/leak", headers={"Authorization": f"Bearer {token}"})
        captured = "\n".join(logs.lines)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(token, captured)
        self.assertIn(REDACTION_MARKER, captured)

    def test_install_redaction_applies_to_existing_root_handlers(self) -> None:
        root = logging.getLogger()
        handler = CapturingHandler()
        root.addHandler(handler)
        try:
            logger = logging.getLogger("teamcraft.api.subcomponent")
            install_redaction(logger, ("topsecret",))
            logger.error("config contains topsecret value")
            self.assertNotIn("topsecret", "\n".join(handler.lines))
            self.assertIn(REDACTION_MARKER, "\n".join(handler.lines))
        finally:
            root.removeHandler(handler)

    def test_repeated_app_creation_does_not_stack_duplicate_filters(self) -> None:
        logger = logging.getLogger("teamcraft.api")
        create_app(token_store=SessionTokenStore())
        create_app(token_store=SessionTokenStore())
        redactors = [
            f for f in logger.filters if isinstance(f, SecretRedactingFilter)
        ]
        self.assertEqual(len(redactors), 1)
        # ...and the single shared filter still knows every issued token
        self.assertGreater(redactors[0].registered_count, 0)

    def test_runtime_rotation_updates_auth_and_redaction(self) -> None:
        store = SessionTokenStore()
        app = create_app(token_store=store)
        client = TestClient(app, raise_server_exceptions=False)
        old = store.token()
        issued = app.state.rotate_session_token()
        self.assertEqual(issued, store.token())
        self.assertNotEqual(issued, old)
        self.assertEqual(
            client.get("/api/v1/ping", headers={"Authorization": f"Bearer {old}"}
            ).status_code,
            401,
        )
        self.assertEqual(
            client.get("/api/v1/ping", headers={"Authorization": f"Bearer {issued}"}
            ).status_code,
            200,
        )

        def leaky() -> None:
            raise RuntimeError(f"now using {issued}")

        app.add_api_route("/leak-new", leaky, methods=["GET"])
        with capture_logs() as logs:
            client.get("/leak-new", headers={"Authorization": f"Bearer {issued}"})
        captured = "\n".join(logs.lines)
        self.assertNotIn(issued, captured)
        self.assertIn(REDACTION_MARKER, captured)


@requires_web
class ServeBootstrapTests(unittest.TestCase):
    def test_print_token_and_rotate_cli_flow(self) -> None:
        from teamcraft.api.serve import main

        with tempfile.TemporaryDirectory() as temporary:
            stdout = io.StringIO()
            with mock.patch("uvicorn.Server.run") as run, contextlib.redirect_stdout(
                stdout
            ):
                code = main(["--project-root", temporary, "--print-token"])
            run.assert_called_once()
            self.assertEqual(code, 0)
            printed = stdout.getvalue().strip().splitlines()
            self.assertTrue(printed)
            token = printed[0]
            token_file = Path(temporary) / ".teamcraft" / "api" / "session-token"
            self.assertEqual(token_file.read_text().strip(), token)
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)

            # rotation through the CLI replaces the persisted token
            second_stdout = io.StringIO()
            with mock.patch("uvicorn.Server.run"), contextlib.redirect_stdout(
                second_stdout
            ):
                code = main(
                    ["--project-root", temporary, "--print-token", "--rotate-token"]
                )
            self.assertEqual(code, 0)
            rotated = second_stdout.getvalue().strip().splitlines()[-1]
            self.assertNotEqual(rotated, token)
            self.assertEqual(token_file.read_text().strip(), rotated)
            self.assertFalse(SessionTokenStore(token_file).verify(token))
            self.assertTrue(SessionTokenStore(token_file).verify(rotated))

    def test_module_entrypoint_exposes_main(self) -> None:
        from teamcraft.api import __main__ as api_main
        from teamcraft.api.serve import main

        self.assertIs(api_main.main, main)


class PackageBoundaryTests(unittest.TestCase):
    @requires_web
    def test_api_import_has_no_side_effects(self) -> None:
        """Importing teamcraft.api must not create files or bind sockets."""

        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(repository)
            result = subprocess.run(
                [sys.executable, "-B", "-c", "import teamcraft.api"],
                cwd=temporary,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_core_package_still_imports_without_web_dependencies(self) -> None:
        """teamcraft core must not require fastapi at import time."""

        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(repository)
            # Block web-stack imports to prove core isolation.
            blocker = (
                "import sys\n"
                "BLOCKED = {'fastapi', 'uvicorn', 'starlette', 'pydantic'}\n"
                "class Blocker:\n"
                "    def find_spec(self, name, path=None, target=None):\n"
                "        if name.split('.')[0] in BLOCKED:\n"
                "            raise ModuleNotFoundError('blocked: ' + name)\n"
                "        return None\n"
                "sys.meta_path.insert(0, Blocker())\n"
                "import teamcraft\n"
                "print('core-ok')\n"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", blocker],
                cwd=temporary,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("core-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
