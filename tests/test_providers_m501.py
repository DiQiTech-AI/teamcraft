"""M5-01 capability registry, health, and Attempt snapshot acceptance.

M6-06 P1 additions (Claude offline discovery probe, tests at the bottom): the
no-runner Claude path must remain the historical static
``unsupported/adapter_pending`` snapshot; an injected runner yields ONLY bounded
version + project skill/rules observations, health stays ``unsupported`` until
:data:`providers.CLAUDE_COMPATIBLE_VERSIONS` establishes an explicit contract,
and ``event_stream``/``prompt_ack`` remain ``unknown`` — nothing here proves or
claims live Claude support.
"""

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
            "codex", repo, runner=lambda argv, cwd, timeout: (
                0, "WARNING: could not create PATH aliases\ncodex-cli 0.166.0\n"
            ),
            clock=self.clock,
        )
        self.assertEqual(snapshot.version, "codex-cli 0.166.0")
        self.assertEqual(snapshot.health, "healthy")
        self.assertEqual(snapshot.discovery["skill_rules"], "supported")
        self.assertEqual(snapshot.capabilities["prompt_ack"], "unknown")

        claude = providers.probe_harness("claude", repo, clock=self.clock)
        self.assertEqual(claude.health, "unsupported")
        self.assertEqual(claude.reason_code, "adapter_pending")

    def test_default_harness_runner_uses_isolated_minimal_environment(self) -> None:
        observed: dict[str, object] = {}

        def run(argv, **kwargs):
            env = kwargs["env"]
            observed["argv"] = argv
            observed["env"] = dict(env)
            self.assertEqual(
                set(env),
                {"PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                 "XDG_CACHE_HOME"},
            )
            home = Path(env["HOME"])
            self.assertTrue(home.is_dir())
            for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
                location = Path(env[name])
                self.assertTrue(location.is_dir())
                self.assertEqual(location.parent, home)
            return mock.Mock(
                returncode=0,
                stdout="opencode v2.2.6\n" + "x" * 5000,
            )

        with mock.patch.object(providers.subprocess, "run", side_effect=run):
            code, output = providers._default_process_runner(
                ("opencode", "--version"), str(self.root), 5.0
            )

        self.assertEqual(code, 0)
        self.assertEqual(observed["argv"], ["opencode", "--version"])
        self.assertEqual(len(output), providers.MAX_HARNESS_VERSION_OUTPUT_BYTES)
        self.assertFalse(Path(observed["env"]["HOME"]).exists())

    def test_default_harness_runner_classifies_process_errors(self) -> None:
        cases = (
            (FileNotFoundError(), 127),
            (providers.subprocess.TimeoutExpired(["codex", "--version"], 5), 124),
            (PermissionError("denied"), 126),
        )
        for error, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                providers.subprocess, "run", side_effect=error
            ):
                self.assertEqual(
                    providers._default_process_runner(
                        ("codex", "--version"), str(self.root), 5.0
                    ),
                    (expected, ""),
                )

    def test_version_parser_skips_diagnostics_and_unsafe_lines(self) -> None:
        self.assertEqual(
            providers._bounded_version(
                "Warning: setup failed\nPlatformError: denied\nopencode v2.2.6\n"
            ),
            "opencode v2.2.6",
        )
        self.assertIsNone(providers._bounded_version("warning 2\nno-version\n"))

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


class ClaudeProbeM606P1Tests(unittest.TestCase):
    """Offline bounded Claude version/discovery reporting (M6-06 P1).

    No test in this class runs a real ``claude`` binary: every probe uses an
    injected runner.  Health and ack outcomes are asserted to stay conservative
    regardless of probe output.
    """

    PINNED = "Claude Code 2.1.284"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m606p1-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.clock = FixedClock(datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc))

    def _with_skill(self) -> None:
        (self.repo / ".claude/skills/teamcraft-ceo").mkdir(parents=True)
        (self.repo / ".claude/skills/teamcraft-ceo/SKILL.md").write_text(
            "skill", encoding="utf-8")

    def _recording_runner(self, code: int, output: str):
        calls: list[tuple[tuple[str, ...], str, float]] = []

        def run(argv, cwd, timeout):
            calls.append((tuple(argv), cwd, timeout))
            return code, output

        return run, calls

    # -- static policy preservation -----------------------------------------

    def test_static_path_without_runner_is_unchanged(self) -> None:
        self._with_skill()
        snapshot = providers.probe_harness("claude", self.repo, clock=self.clock)
        self.assertEqual(snapshot.health, "unsupported")
        self.assertEqual(snapshot.reason_code, "adapter_pending")
        self.assertEqual(snapshot.source, "static_policy")
        self.assertIsNone(snapshot.version)
        self.assertEqual(snapshot.discovery,
                         {"skill_rules": "unsupported",
                          "project_rules": "unsupported"})
        self.assertEqual(snapshot.capabilities,
                         {"event_stream": "unknown", "prompt_ack": "unknown"})
        self.assertFalse(
            snapshot.effective_health(now=self.clock.now())["healthy"])

    def test_contract_registry_is_empty_guard(self) -> None:
        # Pins the CURRENT reality: no explicit compatible installed contract
        # has been established, so nothing can report a healthy Claude.
        self.assertEqual(providers.CLAUDE_COMPATIBLE_VERSIONS, frozenset())
        self.assertTrue(providers.CLAUDE_CONTRACT_REQUIRES_SKILL)

    # -- injected-runner observation ----------------------------------------

    def test_version_seen_reports_facts_without_claiming_support(self) -> None:
        self._with_skill()
        (self.repo / "CLAUDE.md").write_text("rules", encoding="utf-8")
        run, calls = self._recording_runner(0, f"{self.PINNED}\n")
        snapshot = providers.probe_harness("claude", self.repo, runner=run,
                                          clock=self.clock)
        self.assertEqual(calls, [(("claude", "--version"),
                                 str(self.repo.resolve()), 5.0)])
        self.assertEqual(snapshot.version, self.PINNED)
        self.assertEqual(snapshot.discovery,
                         {"skill_rules": "supported",
                          "project_rules": "supported"})
        # Observation must NOT upgrade health or invent ack/stream claims.
        self.assertEqual(snapshot.health, "unsupported")
        self.assertEqual(snapshot.reason_code, "adapter_pending_version_seen")
        self.assertEqual(snapshot.capabilities,
                         {"event_stream": "unknown", "prompt_ack": "unknown"})
        self.assertEqual(snapshot.source, "local_process")
        self.assertFalse(
            snapshot.effective_health(now=self.clock.now())["healthy"])
        # clock/TTL honored on the new path
        self.assertEqual(snapshot.observed_at, self.clock.now().isoformat())
        self.assertGreater(snapshot.valid_until, snapshot.observed_at)

    def test_skill_absent_is_reported_as_observed(self) -> None:
        run, _ = self._recording_runner(0, f"{self.PINNED}\n")
        snapshot = providers.probe_harness("claude", self.repo, runner=run,
                                          clock=self.clock)
        self.assertEqual(snapshot.version, self.PINNED)
        self.assertEqual(snapshot.discovery,
                         {"skill_rules": "unsupported",
                          "project_rules": "unsupported"})
        self.assertEqual(snapshot.health, "unsupported")

    def test_failure_shapes_classified_and_stay_unsupported(self) -> None:
        cases = (
            (127, "", "executable_missing", None),
            (1, "boom\n", "version_probe_failed", None),
            (0, "no digits in this output\n", "version_unparsable", None),
        )
        for code, output, reason, version in cases:
            with self.subTest(code=code):
                run, _ = self._recording_runner(code, output)
                snapshot = providers.probe_harness("claude", self.repo,
                                                 runner=run, clock=self.clock)
                self.assertEqual(snapshot.health, "unsupported")
                self.assertEqual(snapshot.reason_code, reason)
                self.assertEqual(snapshot.version, version)
                self.assertEqual(snapshot.capabilities,
                                 {"event_stream": "unknown",
                                  "prompt_ack": "unknown"})

    def test_credential_shaped_probe_output_fails_closed(self) -> None:
        # digits keep the line version-shaped, so the credential text reaches
        # snapshot validation, where the secret rule refuses it fail-closed.
        run, _ = self._recording_runner(0, "Bearer tokensecret123 v2.1.0\n")
        with self.assertRaisesRegex(ValidationError, "credential-shaped"):
            providers.probe_harness("claude", self.repo, runner=run,
                                   clock=self.clock)

    # -- future contract path (registry deliberately empty today) ----------

    def test_contract_path_flags_health_but_never_ack(self) -> None:
        self._with_skill()
        run, _ = self._recording_runner(0, f"{self.PINNED}\n")
        with mock.patch.object(
                providers, "CLAUDE_COMPATIBLE_VERSIONS",
                frozenset({self.PINNED})):
            snapshot = providers.probe_harness("claude", self.repo, runner=run,
                                              clock=self.clock)
        self.assertEqual(snapshot.health, "healthy")
        self.assertEqual(snapshot.reason_code, "ok")
        # Even contract-conformant observations leave ack/stream unmeasured;
        # P4 must close PG-3 with real evidence, never this constant.
        self.assertEqual(snapshot.capabilities,
                         {"event_stream": "unknown", "prompt_ack": "unknown"})

    def test_contract_version_without_skill_discovery_stays_unsupported(self) -> None:
        run, _ = self._recording_runner(0, f"{self.PINNED}\n")
        with mock.patch.object(
                providers, "CLAUDE_COMPATIBLE_VERSIONS",
                frozenset({self.PINNED})):
            snapshot = providers.probe_harness("claude", self.repo, runner=run,
                                              clock=self.clock)
        self.assertEqual(snapshot.health, "unsupported")
        self.assertEqual(snapshot.reason_code, "adapter_pending_version_seen")

    def test_unhealthy_claude_can_never_bind_an_attempt(self) -> None:
        run, _ = self._recording_runner(0, f"{self.PINNED}\n")
        snapshot = providers.probe_harness("claude", self.repo, runner=run,
                                          clock=self.clock)
        self.assertFalse(
            snapshot.effective_health(now=self.clock.now())["healthy"])
        # Routing-layer consumers see no healthy claude candidate either.
        self.assertEqual(
            snapshot.effective_health(now=self.clock.now())["reason_code"],
            "adapter_pending_version_seen")


if __name__ == "__main__":
    unittest.main()
