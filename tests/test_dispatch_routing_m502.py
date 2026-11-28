"""M5-02 dispatch wiring: strict catalog, fail-closed decide, immutable commit."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from teamcraft import dispatch_routing, domain, providers, router, storage
from teamcraft.errors import ConflictError, ValidationError


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class DispatchRoutingM502Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m502-wire-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))
        self.now = self.clock.now()
        self.project = domain.create_project(
            self.conn, "m502-wire", str(self.root / "repo"), clock=self.clock
        )
        task = domain.create_task(
            self.conn, self.project.id, "route me", clock=self.clock
        )
        self.task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version, clock=self.clock,
        )
        self.attempt = domain.create_attempt(
            self.conn, self.task.id, expected_version=self.task.version,
            clock=self.clock,
        )

    # -- helpers -----------------------------------------------------------

    def snapshot(
        self, entity_type: str, name: str, *, models=(),
        capabilities=None, discovery=None, health="healthy",
        valid_for=300,
    ) -> providers.CapabilitySnapshot:
        observed = self.now - timedelta(seconds=60)
        return providers.CapabilitySnapshot(
            entity_type, name, "1.0", None, tuple(models),
            discovery or {}, capabilities or {}, health,
            "ok" if health == "healthy" else "probe_failed", "test",
            observed.isoformat(),
            (self.now + timedelta(seconds=valid_for)).isoformat(),
        )

    def record(self, entity_type: str, name: str, **kwargs) -> None:
        providers.record_capability_probe(
            self.conn, self.project.id,
            self.snapshot(entity_type, name, **kwargs),
        )

    def harness(self, name: str = "opencode", **kwargs) -> None:
        capabilities = kwargs.pop(
            "capabilities", {"event_stream": "unknown", "prompt_ack": "unknown"}
        )
        discovery = kwargs.pop("discovery", {"skill_rules": "supported"})
        self.record("harness", name, capabilities=capabilities,
                    discovery=discovery, **kwargs)

    def provider(self, name: str = "omlx", models=("qwen3:8b",),
                 capabilities=None, **kwargs) -> None:
        if capabilities is None:
            capabilities = {
                "inference_request": "supported", "event_stream": "unsupported",
                "prompt_ack": "unsupported",
            }
        self.record("provider", name, models=models,
                    capabilities=capabilities, **kwargs)

    def entry(self, *, harness="opencode", provider="omlx", model="qwen3:8b",
              locality="local", assurance=2, cost_rank=0) -> dispatch_routing.CatalogEntry:
        return dispatch_routing.CatalogEntry(
            harness, provider, model, locality, assurance, cost_rank
        )

    def base_catalog(self):
        return [self.entry()]

    def decide(self, catalog=None, **kwargs):
        return dispatch_routing.decide(
            self.conn, self.project.id,
            catalog=catalog or self.base_catalog(),
            now=kwargs.pop("now", self.now), **kwargs,
        )

    def route_event_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?",
            (dispatch_routing.ROUTE_DECIDED_EVENT,),
        ).fetchone()[0])

    # -- catalog -----------------------------------------------------------

    def test_catalog_strict_schema_and_digest(self) -> None:
        parsed = dispatch_routing.parse_catalog([{
            "harness": "opencode", "provider": "omlx", "model": "qwen3:8b",
            "locality": "local", "assurance": 2, "cost_rank": 0,
        }])
        self.assertEqual(parsed, (self.entry(),))
        self.assertEqual(
            dispatch_routing.catalog_digest(parsed),
            dispatch_routing.catalog_digest([self.entry()]),
        )
        self.assertEqual(len(dispatch_routing.catalog_digest(parsed)), 64)

    def test_catalog_rejects_unknown_extra_and_missing_fields(self) -> None:
        good = {
            "harness": "opencode", "provider": "omlx", "model": "qwen3:8b",
            "locality": "local", "assurance": 2, "cost_rank": 0,
        }
        for value in (
            [{**good, "unexpected": 1}],
            [{key: item for key, item in good.items() if key != "model"}],
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                dispatch_routing.parse_catalog(value)

    def test_catalog_rejects_bad_types_ranges_and_empty(self) -> None:
        good = {
            "harness": "opencode", "provider": "omlx", "model": "qwen3:8b",
            "locality": "local", "assurance": 2, "cost_rank": 0,
        }
        bad = [
            [],
            [None],
            [{**good, "assurance": 4}],
            [{**good, "assurance": -1}],
            [{**good, "assurance": True}],
            [{**good, "assurance": "2"}],
            [{**good, "cost_rank": -1}],
            [{**good, "cost_rank": True}],
            [{**good, "locality": "edge"}],
            [{**good, "harness": 7}],
            [{**good, "provider": ""}],
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                dispatch_routing.parse_catalog(value)
        with self.assertRaises(ValidationError):
            dispatch_routing.parse_catalog({"harness": "opencode"})

    def test_catalog_rejects_unlaunchable_identifiers(self) -> None:
        # ``launch`` joins provider + model with "/", so accepting a slash in
        # any catalog component would create a route ``decide`` can select but
        # ``launch`` must refuse.  Reject it once, at catalog parse time.
        good = {
            "harness": "opencode", "provider": "omlx", "model": "qwen3:8b",
            "locality": "local", "assurance": 2, "cost_rank": 0,
        }
        for field in ("model", "provider", "harness"):
            for value in ("a/b", "a b", "", " a", "a\tb", "a\nb"):
                with self.subTest(field=field, value=value), \
                        self.assertRaises(ValidationError):
                    dispatch_routing.parse_catalog([{**good, field: value}])
        with self.assertRaises(ValidationError):
            self.entry(model="a/b")
        parsed = dispatch_routing.parse_catalog([{
            **good, "model": "Qwen3.8-Flash-Next-4bit-mtp",
        }])
        self.assertEqual(parsed[0].model, "Qwen3.8-Flash-Next-4bit-mtp")

    def test_catalog_rejects_credential_shaped_text(self) -> None:
        good = {
            "harness": "opencode", "provider": "omlx", "model": "qwen3:8b",
            "locality": "local", "assurance": 2, "cost_rank": 0,
        }
        for value in (
            [{**good, "model": "sk-abcdefghij"}],
            [{**good, "provider": "api_key=super-secret"}],
            [{**good, "api_key": "x"}],
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                dispatch_routing.parse_catalog(value)

    def test_load_catalog_rejects_missing_and_malformed_file(self) -> None:
        with self.assertRaises(ValidationError):
            dispatch_routing.load_catalog(self.root / "nope.json")
        broken = self.root / "broken.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaises(ValidationError):
            dispatch_routing.load_catalog(broken)

    # -- decide ------------------------------------------------------------

    def test_healthy_route_selects_and_digest_is_deterministic(self) -> None:
        self.harness()
        self.provider()
        self.provider(name="cloud", models=("cloud-model",))
        catalog = [
            self.entry(),
            self.entry(provider="cloud", model="cloud-model",
                       locality="cloud", assurance=3, cost_rank=1),
        ]
        first = self.decide(catalog=catalog)
        second = self.decide(catalog=catalog)
        reversed_input = self.decide(catalog=list(reversed(catalog)))
        self.assertEqual(first.decision.selection,
                         router.RouteSelection("opencode", "omlx", "qwen3:8b"))
        self.assertEqual(first.opencode_model, "omlx/qwen3:8b")
        self.assertEqual(first.decision.reason_code, "selected_local_first")
        self.assertEqual(first.decision_digest, second.decision_digest)
        self.assertEqual(first.decision_digest, reversed_input.decision_digest)
        self.assertEqual(first.catalog_digest, reversed_input.catalog_digest)
        self.assertEqual((first.harness.name, first.provider.name),
                         ("opencode", "omlx"))

    def test_stale_snapshot_is_unhealthy_and_fails_closed(self) -> None:
        self.harness()
        self.provider(valid_for=-1)
        with self.assertRaises(dispatch_routing.RouteUnavailable) as ctx:
            self.decide()
        error = ctx.exception
        self.assertEqual(error.reason_code, "no_healthy_candidates")
        self.assertFalse(error.decision.override_audit)
        self.assertIn("no_healthy_candidates", str(error))

    def test_missing_snapshots_are_skipped_and_reported(self) -> None:
        self.harness()
        with self.assertRaises(dispatch_routing.RouteUnavailable) as ctx:
            self.decide()
        self.assertEqual(ctx.exception.reason_code, "no_healthy_candidates")
        self.assertIn("provider:omlx", ctx.exception.missing)
        self.assertEqual(len(ctx.exception.skipped), 1)

        self.provider()
        cloud = self.entry(provider="cloud", model="cloud-model",
                           locality="cloud", assurance=3, cost_rank=1)
        outcome = self.decide(catalog=[self.entry(), cloud])
        self.assertEqual(outcome.provider.name, "omlx")
        self.assertIn("provider:cloud", outcome.missing)
        self.assertEqual(len(outcome.skipped), 1)

    def test_high_risk_assurance_gate_fails_closed(self) -> None:
        self.harness()
        self.provider()
        with self.assertRaises(dispatch_routing.RouteUnavailable) as ctx:
            self.decide(catalog=[self.entry(assurance=1)], risk="high")
        self.assertEqual(
            ctx.exception.reason_code, "high_risk_assurance_unsatisfied"
        )
        self.assertEqual(ctx.exception.effective_risk, "high")

    def test_required_capabilities_gate(self) -> None:
        self.harness()
        self.provider()
        with self.assertRaises(dispatch_routing.RouteUnavailable) as ctx:
            self.decide(required_capabilities=("prompt_ack",))
        self.assertEqual(ctx.exception.reason_code, "capabilities_unsatisfied")
        outcome = self.decide(required_capabilities=("inference_request",))
        self.assertEqual(outcome.decision.reason_code, "selected_local_first")

    def test_override_is_applied_only_after_gates(self) -> None:
        self.harness()
        self.provider()
        self.provider(name="cloud", models=("cloud-model",))
        cloud = self.entry(provider="cloud", model="cloud-model",
                           locality="cloud", assurance=3, cost_rank=0)
        override = router.ManualOverride(
            router.RouteSelection("opencode", "cloud", "cloud-model"),
            "alice", "pin the cloud route",
        )
        outcome = self.decide(catalog=[self.entry(), cloud], override=override)
        self.assertEqual(outcome.decision.reason_code,
                         "selected_manual_override")
        self.assertEqual(outcome.opencode_model, "cloud/cloud-model")
        audit = outcome.decision.override_audit
        self.assertIsNotNone(audit)
        self.assertTrue(audit.applied)
        self.assertEqual((audit.actor, audit.reason),
                         ("alice", "pin the cloud route"))

    def test_override_rejected_by_gate_fails_closed(self) -> None:
        self.harness()
        self.provider()
        self.provider(name="cloud", models=("cloud-model",))
        cloud = self.entry(provider="cloud", model="cloud-model",
                           locality="cloud", assurance=2, cost_rank=0)
        override = router.ManualOverride(
            router.RouteSelection("opencode", "omlx", "qwen3:8b"),
            "alice", "force weak local",
        )
        with self.assertRaises(dispatch_routing.RouteUnavailable) as ctx:
            self.decide(
                catalog=[self.entry(assurance=1), cloud],
                risk="high", override=override,
            )
        self.assertEqual(ctx.exception.reason_code, "override_target_ineligible")
        self.assertIsNotNone(ctx.exception.decision.override_audit)
        self.assertFalse(ctx.exception.decision.override_audit.applied)

    def test_override_target_not_in_catalog_fails_closed(self) -> None:
        self.harness()
        self.provider()
        override = router.ManualOverride(
            router.RouteSelection("opencode", "elsewhere", "other-model"),
            "alice", "wrong target",
        )
        with self.assertRaises(dispatch_routing.RouteUnavailable) as ctx:
            self.decide(override=override)
        self.assertEqual(ctx.exception.reason_code, "override_target_not_found")

    # -- commit ------------------------------------------------------------

    def test_commit_binds_attempt_and_appends_one_audit_event(self) -> None:
        self.harness()
        self.provider()
        outcome = self.decide()
        event = dispatch_routing.commit(
            self.conn, self.attempt.id, outcome, clock=self.clock,
        )
        self.assertEqual(event.kind, dispatch_routing.ROUTE_DECIDED_EVENT)
        payload = json.loads(event.payload_json)
        self.assertEqual(payload["attempt_id"], self.attempt.id)
        self.assertEqual(payload["decision_digest"], outcome.decision_digest)
        self.assertEqual(payload["catalog_digest"], outcome.catalog_digest)
        self.assertEqual(payload["selection"],
                         {"harness": "opencode", "provider": "omlx",
                          "model": "qwen3:8b"})
        bound = providers.get_attempt_capabilities(self.conn, self.attempt.id)
        self.assertIsNotNone(bound)
        self.assertEqual(bound["selection"], payload["selection"])
        self.assertEqual(self.route_event_count(), 1)

        replay = dispatch_routing.commit(
            self.conn, self.attempt.id, outcome, clock=self.clock,
        )
        self.assertEqual((replay.event_id, replay.seq),
                         (event.event_id, event.seq))
        self.assertEqual(self.route_event_count(), 1)

    def test_different_decision_on_same_attempt_conflicts(self) -> None:
        self.harness()
        self.provider()
        self.provider(name="cloud", models=("cloud-model",))
        first = self.decide()
        dispatch_routing.commit(
            self.conn, self.attempt.id, first, clock=self.clock,
        )
        second = self.decide(catalog=[self.entry(
            provider="cloud", model="cloud-model", locality="cloud",
            assurance=3,
        )])
        self.assertNotEqual(first.decision_digest, second.decision_digest)
        with self.assertRaises(ConflictError):
            dispatch_routing.commit(
                self.conn, self.attempt.id, second, clock=self.clock,
            )
        self.assertEqual(self.route_event_count(), 1)

    def test_bind_failure_leaves_no_route_audit_event(self) -> None:
        self.harness()
        self.provider()
        outcome = self.decide()
        with mock.patch.object(
            providers, "bind_attempt_capabilities",
            side_effect=ValidationError("bind refused"),
        ):
            with self.assertRaises(ValidationError):
                dispatch_routing.commit(
                    self.conn, self.attempt.id, outcome, clock=self.clock,
                )
        self.assertEqual(self.route_event_count(), 0)
        self.assertIsNone(
            providers.get_attempt_capabilities(self.conn, self.attempt.id)
        )


if __name__ == "__main__":
    unittest.main()
