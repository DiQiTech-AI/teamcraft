"""M4-05 authenticated API surface tests for direct-PR delivery.

The Core/GitHub behavior is covered by the delivery and adapter suites.  This
module pins the HTTP boundary: inherited security guards, strict request
shapes, idempotency-key forwarding, replay headers, and version/refusal error
mapping.  No test invokes ``gh`` or a network service.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from fastapi.testclient import TestClient

    from teamcraft import delivery, domain, fresh_start, storage
    from teamcraft.api import create_app
    from teamcraft.api.command import (
        IDEMPOTENCY_HEADER,
        REPLAY_HEADER,
        CommandService,
        DirectPrPrepareRequest,
    )
    from teamcraft.api.errors import (
        CONFLICT,
        CSRF_CHECK_FAILED,
        FORBIDDEN_ORIGIN,
        UNAUTHENTICATED,
        VALIDATION_FAILED,
    )

    _WEB_OK = True
    _WEB_REASON = ""
except ImportError as exc:  # pragma: no cover - optional web dependency
    _WEB_OK = False
    _WEB_REASON = f"web dependencies unavailable: {exc}"


requires_web = unittest.skipUnless(_WEB_OK, _WEB_REASON)
ERROR_KEYS = {"code", "message", "details", "request_id"}


def _route_cases() -> list[tuple[str, dict]]:
    return [
        ("/api/v1/attempts/attempt-1/delivery/pr",
         {"expected_task_version": 3}),
        ("/api/v1/tasks/task-1/delivery/pr/sync",
         {"expected_task_version": 3}),
        ("/api/v1/pr-evidence/pr-1/approval",
         {"decision": "approve", "actor": "human", "reason": "reviewed",
          "expected_task_version": 3, "pr_number": 7,
          "head_sha": "a" * 40, "base": "main"}),
        ("/api/v1/pr-evidence/pr-1/merge",
         {"expected_task_version": 3}),
        ("/api/v1/commands/cmd-1/delivery/pr/reconcile",
         {"actor": "ops", "reason": "observed remote facts"}),
    ]


class _FakeCommandService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.replayed = False
        self.error: Exception | None = None

    def _result(self, name: str, *args):
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error
        return 200, {"operation": name}, self.replayed

    def direct_pr_prepare(self, *args):
        return self._result("prepare", *args)

    def direct_pr_sync(self, *args):
        return self._result("sync", *args)

    def direct_pr_approval(self, *args):
        return self._result("approval", *args)

    def direct_pr_merge(self, *args):
        return self._result("merge", *args)

    def direct_pr_reconcile(self, *args):
        return self._result("reconcile", *args)


@requires_web
class DirectPrTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _FakeCommandService()
        self.app = create_app(command_service=self.service)
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()

    def headers(self, key: str | None = "key-1") -> dict[str, str]:
        out = {"Authorization": f"Bearer {self.token}",
               "X-CSRF-Token": self.token}
        if key is not None:
            out[IDEMPOTENCY_HEADER] = key
        return out

    def assert_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body), ERROR_KEYS)
        self.assertEqual(body["code"], code)
        self.assertTrue(body["request_id"])
        return body

    def test_every_route_inherits_auth_csrf_origin_and_idempotency_guards(self):
        for path, body in _route_cases():
            with self.subTest(path=path, guard="auth"):
                self.assert_error(self.client.post(path, json=body),
                                  401, UNAUTHENTICATED)
            with self.subTest(path=path, guard="csrf"):
                headers = {"Authorization": f"Bearer {self.token}",
                           IDEMPOTENCY_HEADER: "key"}
                self.assert_error(self.client.post(path, headers=headers, json=body),
                                  403, CSRF_CHECK_FAILED)
            with self.subTest(path=path, guard="origin"):
                self.assert_error(self.client.post(
                    path, headers={**self.headers(), "Origin": "https://evil.test"},
                    json=body), 403, FORBIDDEN_ORIGIN)
            with self.subTest(path=path, guard="idempotency"):
                self.assert_error(self.client.post(
                    path, headers=self.headers(None), json=body),
                    422, VALIDATION_FAILED)
        self.assertEqual(self.service.calls, [])

    def test_routes_forward_only_validated_models_and_exact_key(self):
        expected = ["prepare", "sync", "approval", "merge", "reconcile"]
        for (path, body), operation in zip(_route_cases(), expected):
            response = self.client.post(
                path, headers=self.headers(f"key-{operation}"), json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["operation"], operation)
            name, args = self.service.calls[-1]
            self.assertEqual(name, operation)
            self.assertEqual(args[-1], f"key-{operation}")
            self.assertEqual(args[1].model_dump(exclude_none=True), body)

    def test_unknown_fields_and_malformed_approval_never_reach_service(self):
        for path, body in _route_cases():
            with self.subTest(path=path):
                response = self.client.post(
                    path, headers=self.headers(), json={**body, "git_argv": ["push"]})
                self.assert_error(response, 422, VALIDATION_FAILED)
        bad = self.client.post(
            "/api/v1/pr-evidence/pr-1/approval", headers=self.headers(),
            json={"decision": "approve", "actor": "h", "reason": "r",
                  "expected_task_version": 3, "pr_number": 0})
        self.assert_error(bad, 422, VALIDATION_FAILED)
        self.assertEqual(self.service.calls, [])

    def test_replay_header_is_exposed(self):
        self.service.replayed = True
        response = self.client.post(
            "/api/v1/tasks/task-1/delivery/pr/sync", headers=self.headers(),
            json={"expected_task_version": 3})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get(REPLAY_HEADER), "true")

    def test_core_refusal_is_redacted_to_stable_conflict_shape(self):
        self.service.error = delivery.DirectPrRefusal(
            "wrong-base", "/private/worktree and remote diagnostics")
        response = self.client.post(
            "/api/v1/pr-evidence/pr-1/merge", headers=self.headers(),
            json={"expected_task_version": 3})
        body = self.assert_error(response, 409, CONFLICT)
        self.assertEqual(body["details"], {
            "reason_code": "DELIVERY_REFUSED", "refusal_kind": "wrong-base"})
        self.assertNotIn("/private/worktree", response.text)


@requires_web
class DirectPrCommandServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tc-api-m405-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        fresh_start.initialize_project_root(str(self.root))
        self.db = self.root / ".teamcraft" / "teamcraft.db"
        conn = storage.connect(self.db)
        try:
            project = domain.create_project(conn, "demo", str(self.root))
            task = domain.create_task(
                conn, project.id, "direct pr", kind="ship",
                ownership_json='["src/**"]')
            task = domain.apply_task_event(
                conn, task.id, "dependencies_satisfied",
                expected_version=task.version)
            task = domain.set_execution_policy(
                conn, task.id, expected_version=task.version,
                policy={"delivery": "direct_pr",
                        "direct_pr": {"repo": "acme/widgets", "base": "main"}},
                actor="ceo", reason="M4-05 API test")
            self.attempt = domain.create_attempt(
                conn, task.id, expected_version=task.version)
            self.task_id = task.id
            self.version = domain.get_task(conn, task.id).version
        finally:
            conn.close()
        self.service = CommandService(self.db)

    def test_stale_version_refuses_before_core_effect(self):
        payload = DirectPrPrepareRequest(expected_task_version=self.version + 1)
        with mock.patch("teamcraft.api.command.delivery_core.prepare_direct_pr") \
                as prepare:
            with self.assertRaises(Exception) as caught:
                self.service.direct_pr_prepare(self.attempt.id, payload, "key-stale")
        self.assertEqual(type(caught.exception).__name__, "VersionConflictError")
        prepare.assert_not_called()

    def test_prepare_forwards_opaque_attempt_key_and_fenced_lease(self):
        result = SimpleNamespace(
            outcome="blocked", blocked=True, ready=False,
            task_status="verifying", pr_evidence=None, command_id=None)
        payload = DirectPrPrepareRequest(expected_task_version=self.version)
        with mock.patch(
                "teamcraft.api.command.delivery_core.load_latest_pr_evidence",
                return_value=None), mock.patch(
                "teamcraft.api.command.delivery_core.prepare_direct_pr",
                return_value=result) as prepare:
            status, body, replayed = self.service.direct_pr_prepare(
                self.attempt.id, payload, "opaque-key")
        self.assertEqual(status, 200)
        self.assertTrue(body["blocked"])
        self.assertFalse(replayed)
        args, kwargs = prepare.call_args
        self.assertEqual(args[1], self.attempt.id)
        self.assertEqual(kwargs["idempotency_key"], "opaque-key")
        self.assertEqual(kwargs["lease"].scope, f"project:{self._project_id()}")

    def _project_id(self) -> str:
        conn = storage.connect(self.db)
        try:
            return domain.get_task(conn, self.task_id).project_id
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
