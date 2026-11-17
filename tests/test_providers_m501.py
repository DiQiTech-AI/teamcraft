"""M5-01 capability registry, health, and Attempt snapshot acceptance."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from teamcraft import domain, providers, storage
from teamcraft.errors import ConflictError, ValidationError
from teamcraft.query import QueryService


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class ProviderCapabilitiesM501Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m501-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc))
        self.project = domain.create_project(
            self.conn, "m501", str(self.root / "repo"), clock=self.clock
        )
        self.task = domain.create_task(self.conn, self.project.id, "probe", clock=self.clock)
        self.task = domain.apply_task_event(
            self.conn, self.task.id, "dependencies_satisfied",
            expected_version=self.task.version, clock=self.clock,
        )
        self.attempt = domain.create_attempt(
            self.conn, self.task.id, expected_version=self.task.version, clock=self.clock
        )

    def test_harness_probe_records_version_discovery_and_claude_pending(self) -> None:
        repo = self.root / "checkout"
        (repo / ".agents/skills/teamcraft-ceo").mkdir(parents=True)
        (repo / ".agents/skills/teamcraft-ceo/SKILL.md").write_text("ok", encoding="utf-8")
        (repo / "AGENTS.md").write_text("rules", encoding="utf-8")

        snapshot = providers.probe_harness(
            "codex", repo, runner=lambda argv, cwd, timeout: (0, "codex-cli 0.166.0\n"),
            clock=self.clock,
        )
        self.assertEqual(snapshot.version, "codex-cli 0.166.0")
        self.assertEqual(snapshot.health, "healthy")
        self.assertEqual(snapshot.discovery["skill_rules"], "supported")
        self.assertEqual(snapshot.capabilities["prompt_ack"], "unknown")

        claude = providers.probe_harness("claude", repo, clock=self.clock)
        self.assertEqual(claude.health, "unsupported")
        self.assertEqual(claude.reason_code, "adapter_pending")

    def test_omlx_probe_detects_models_disconnect_and_refuses_non_loopback(self) -> None:
        body = json.dumps({"version": "0.9.1", "data": [{"id": "model-a"}]}).encode()
        healthy = providers.probe_omlx(
            getter=lambda url, timeout: (200, body), clock=self.clock
        )
        self.assertEqual(healthy.models, ("model-a",))
        self.assertTrue(healthy.effective_health(now=self.clock.now())["healthy"])

        disconnected = providers.probe_omlx(
            getter=lambda url, timeout: (0, b""), clock=self.clock
        )
        self.assertEqual(disconnected.health, "unhealthy")
        self.assertEqual(disconnected.reason_code, "service_disconnected")
        self.assertFalse(disconnected.effective_health(now=self.clock.now())["healthy"])

        with self.assertRaisesRegex(ValidationError, "loopback"):
            providers.probe_omlx("https://example.com/v1", getter=lambda u, t: (200, body))
        with self.assertRaisesRegex(ValidationError, "loopback"):
            providers.probe_omlx("http://localhost:8000/v1", getter=lambda u, t: (200, body))
        with self.assertRaisesRegex(ValidationError, "loopback"):
            providers.probe_omlx("http://user:secret@127.0.0.1:8000/v1")

    def test_default_omlx_transport_disables_ambient_proxy_handlers(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"data":[]}'
        response.__enter__.return_value = response
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch.object(
            providers.urllib.request, "build_opener", return_value=opener
        ) as build_opener:
            status, body = providers._default_http_get(
                "http://127.0.0.1:8000/v1/models", 1.0
            )
        self.assertEqual((status, body), (200, b'{"data":[]}'))
        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, providers.urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies, {})

    def test_unknown_and_stale_are_never_healthy(self) -> None:
        unknown = providers.CapabilitySnapshot(
            "harness", "codex", None, None, (), {}, {"prompt_ack": "unknown"},
            "unknown", "not_probed", "test", self.clock.now().isoformat(),
            (self.clock.now() + timedelta(seconds=1)).isoformat(),
        )
        self.assertFalse(unknown.effective_health(now=self.clock.now())["healthy"])
        stale = unknown.effective_health(now=self.clock.now() + timedelta(seconds=2))
        self.assertEqual(stale, {"status": "stale", "healthy": False,
                                 "reason_code": "probe_stale"})

    def test_registry_and_attempt_snapshot_are_durable_idempotent_and_queryable(self) -> None:
        harness = providers.CapabilitySnapshot(
            "harness", "opencode", "2.0.29", None, (),
            {"skill_rules": "supported"}, {"prompt_ack": "unknown"},
            "healthy", "ok", "local_process", self.clock.now().isoformat(),
            (self.clock.now() + timedelta(minutes=5)).isoformat(),
        )
        provider = providers.probe_omlx(
            getter=lambda url, timeout: (
                200, b'{"data":[{"id":"model-a"},{"id":"other-model"}]}'
            ),
            clock=self.clock,
        )
        unregistered = providers.CapabilitySnapshot(
            "harness", "opencode", "2.0.30", None, (),
            {"skill_rules": "supported"}, {"prompt_ack": "unknown"},
            "healthy", "ok", "local_process", self.clock.now().isoformat(),
            (self.clock.now() + timedelta(minutes=5)).isoformat(),
        )
        with self.assertRaisesRegex(ValidationError, "not durably registered"):
            providers.bind_attempt_capabilities(
                self.conn, self.attempt.id, harness=unregistered,
                provider=provider, model="model-a", clock=self.clock,
            )
        first = providers.record_capability_probe(self.conn, self.project.id, harness)
        replay = providers.record_capability_probe(self.conn, self.project.id, harness)
        self.assertEqual(first.event_id, replay.event_id)
        providers.record_capability_probe(self.conn, self.project.id, provider)
        bound = providers.bind_attempt_capabilities(
            self.conn, self.attempt.id, harness=harness, provider=provider,
            model="model-a", clock=self.clock,
        )
        self.assertEqual(
            bound.event_id,
            providers.bind_attempt_capabilities(
                self.conn, self.attempt.id, harness=harness, provider=provider,
                model="model-a", clock=self.clock,
            ).event_id,
        )
        with self.assertRaises(ConflictError):
            providers.bind_attempt_capabilities(
                self.conn, self.attempt.id, harness=harness, provider=provider,
                model="other-model", clock=self.clock,
            )

        unhealthy = providers.probe_omlx(
            getter=lambda url, timeout: (0, b""), clock=self.clock
        )
        providers.record_capability_probe(self.conn, self.project.id, unhealthy)
        with self.assertRaisesRegex(ValidationError, "not currently healthy"):
            providers.bind_attempt_capabilities(
                self.conn, self.attempt.id, harness=harness,
                provider=unhealthy, model="model-a", clock=self.clock,
            )
        unsupported = providers.probe_harness("claude", self.root, clock=self.clock)
        providers.record_capability_probe(self.conn, self.project.id, unsupported)
        with self.assertRaisesRegex(ValidationError, "not currently healthy"):
            providers.bind_attempt_capabilities(
                self.conn, self.attempt.id, harness=unsupported,
                provider=provider, model="model-a", clock=self.clock,
            )

        service = QueryService(self.db)
        registry = service.get_project_capabilities(self.project.id)
        self.assertEqual(
            {item["name"] for item in registry["items"]},
            {"opencode", "omlx", "claude"},
        )
        attempt = service.get_attempt(self.attempt.id)
        self.assertEqual(
            attempt["capability_snapshot"]["selection"],
            {"harness": "opencode", "provider": "omlx", "model": "model-a"},
        )
        self.assertIn("healthy", attempt["capability_snapshot"]["harness_effective_health"])
        self.assertIn("healthy", attempt["capability_snapshot"]["provider_effective_health"])
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from teamcraft.api.query import build_query_router
        except ImportError:
            self.skipTest("web test dependencies unavailable")
        app = FastAPI()
        app.include_router(build_query_router(service))
        api_payload = TestClient(app).get(
            f"/api/v1/projects/{self.project.id}/capabilities"
        ).json()
        self.assertEqual(
            {item["name"] for item in api_payload["items"]},
            {"opencode", "omlx", "claude"},
        )

    def test_secret_shaped_metadata_is_rejected_and_not_persisted(self) -> None:
        before = int(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind IN (?, ?)",
            (providers.CAPABILITY_PROBE_EVENT, providers.ATTEMPT_CAPABILITY_EVENT),
        ).fetchone()[0])
        with self.assertRaisesRegex(ValidationError, "credential-shaped"):
            providers.CapabilitySnapshot(
                "harness", "codex", "1.0", None, (), {"api_key": "supported"},
                {}, "healthy", "ok", "test", self.clock.now().isoformat(),
                (self.clock.now() + timedelta(minutes=5)).isoformat(),
            )
        with self.assertRaisesRegex(ValidationError, "credential-shaped"):
            providers.CapabilitySnapshot(
                "harness", "codex", "1.0", None, (), {}, {},
                "healthy", "ok", "Bearer secret-material",
                self.clock.now().isoformat(),
                (self.clock.now() + timedelta(minutes=5)).isoformat(),
            )
        after = int(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind IN (?, ?)",
            (providers.CAPABILITY_PROBE_EVENT, providers.ATTEMPT_CAPABILITY_EVENT),
        ).fetchone()[0])
        self.assertEqual(after, before, "rejected credential metadata must write no event")


if __name__ == "__main__":
    unittest.main()
