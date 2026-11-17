"""M5-02 deterministic Router acceptance tests."""

from __future__ import annotations

import itertools
import unittest
from datetime import datetime, timedelta, timezone

from teamcraft import providers, router
from teamcraft.errors import ValidationError


class DeterministicRouterM502Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)

    def snapshot(self, entity_type: str, name: str, *, models=(),
                 capabilities=None, discovery=None, health="healthy",
                 valid_for=300) -> providers.CapabilitySnapshot:
        return providers.CapabilitySnapshot(
            entity_type, name, "1.0", None, tuple(models),
            discovery or {}, capabilities or {}, health,
            "ok" if health == "healthy" else "probe_failed", "test",
            self.now.isoformat(),
            (self.now + timedelta(seconds=valid_for)).isoformat(),
        )

    def candidate(self, name: str, *, locality="local", assurance=1,
                  cost=1, capabilities=("inference_request",),
                  health="healthy", valid_for=300,
                  model_in_catalog=True) -> router.RouteCandidate:
        harness = self.snapshot(
            "harness", f"harness-{name}",
            capabilities={"prompt_ack": "supported"},
            discovery={"skill_rules": "supported"}, health=health,
            valid_for=valid_for,
        )
        provider = self.snapshot(
            "provider", f"provider-{name}",
            models=((f"model-{name}",) if model_in_catalog else ()),
            capabilities={item: "supported" for item in capabilities},
            health=health, valid_for=valid_for,
        )
        return router.RouteCandidate(
            harness, provider, f"model-{name}", locality, assurance, cost
        )

    def test_same_inputs_are_deterministic_across_candidate_order(self) -> None:
        candidates = (
            self.candidate("b", assurance=2, cost=2),
            self.candidate("a", assurance=2, cost=2),
            self.candidate("cloud", locality="cloud", assurance=3, cost=0),
        )
        request = router.RouteRequest(required_capabilities=("prompt_ack",))
        decisions = [router.route(request, items, now=self.now)
                     for items in itertools.permutations(candidates)]
        self.assertEqual(
            {decision.selection.candidate_id for decision in decisions},
            {'["harness-a","provider-a","model-a"]'},
        )
        self.assertEqual({decision.reason_code for decision in decisions},
                         {"selected_local_first"})
        self.assertEqual(len({decision.decision_digest for decision in decisions}), 1)

    def test_local_first_beats_cheaper_cloud_for_normal_task(self) -> None:
        local = self.candidate("local", locality="local", assurance=1, cost=10)
        cloud = self.candidate("cloud", locality="cloud", assurance=3, cost=0)
        decision = router.route(router.RouteRequest(), (cloud, local), now=self.now)
        self.assertEqual(decision.selection, local.selection)
        self.assertEqual(decision.reason_code, "selected_local_first")

        price_first = router.route(
            router.RouteRequest(), (cloud, local),
            policy=router.RouterPolicy(local_first=False), now=self.now,
        )
        self.assertEqual(price_first.selection, cloud.selection)
        self.assertEqual(price_first.reason_code, "selected_deterministic_best")

    def test_high_risk_rule_precedes_locality_and_price(self) -> None:
        weak_cheap_local = self.candidate(
            "weak", locality="local", assurance=2, cost=0
        )
        strong_cloud = self.candidate(
            "strong", locality="cloud", assurance=3, cost=100
        )
        decision = router.route(
            router.RouteRequest(task_tags=("deployment",)),
            (weak_cheap_local, strong_cloud), now=self.now,
        )
        self.assertEqual(decision.effective_risk, "high")
        self.assertEqual(decision.selection, strong_cloud.selection)
        self.assertEqual(decision.reason_code, "selected_high_risk_assurance")

    def test_auth_secret_and_deploy_tags_are_high_risk(self) -> None:
        for tag in ("auth", "secrets", "deploy"):
            with self.subTest(tag=tag):
                weak = self.candidate(tag, assurance=1)
                decision = router.route(
                    router.RouteRequest(task_tags=(tag,)), (weak,), now=self.now
                )
                self.assertIsNone(decision.selection)
                self.assertEqual(decision.reason_code,
                                 "high_risk_assurance_unsatisfied")

    def test_required_capabilities_must_be_explicitly_supported(self) -> None:
        candidate = self.candidate("basic", capabilities=("inference_request",))
        missing = router.route(
            router.RouteRequest(required_capabilities=("event_stream",)),
            (candidate,), now=self.now,
        )
        self.assertIsNone(missing.selection)
        self.assertEqual(missing.reason_code, "capabilities_unsatisfied")

        supported = router.route(
            router.RouteRequest(required_capabilities=("skill_rules",)),
            (candidate,), now=self.now,
        )
        self.assertEqual(supported.selection, candidate.selection)

    def test_unknown_unhealthy_stale_and_missing_model_are_not_routes(self) -> None:
        unknown = self.candidate("unknown", health="unknown")
        stale = self.candidate("stale", valid_for=1)
        missing_model = self.candidate("catalog", model_in_catalog=False)
        decision = router.route(
            router.RouteRequest(), (unknown, stale, missing_model),
            now=self.now + timedelta(seconds=2),
        )
        self.assertIsNone(decision.selection)
        self.assertEqual(decision.reason_code, "no_healthy_candidates")

    def test_manual_override_is_explicit_audited_and_deterministic(self) -> None:
        first = self.candidate("first", assurance=2, cost=0)
        chosen = self.candidate("chosen", assurance=2, cost=10)
        override = router.ManualOverride(
            chosen.selection, actor="owner", reason="Use verified compatibility"
        )
        decision = router.route(
            router.RouteRequest(risk="high", override=override),
            (first, chosen), now=self.now,
        )
        self.assertEqual(decision.selection, chosen.selection)
        self.assertEqual(decision.reason_code, "selected_manual_override")
        self.assertEqual(decision.override_audit, router.OverrideAudit(
            actor="owner", reason="Use verified compatibility",
            requested_selection=chosen.selection, applied=True,
        ))
        self.assertEqual(decision.decision_digest, router.route(
            router.RouteRequest(risk="high", override=override),
            (chosen, first), now=self.now,
        ).decision_digest)

    def test_override_cannot_bypass_health_capability_or_risk(self) -> None:
        eligible = self.candidate("eligible", assurance=3)
        weak = self.candidate("weak", assurance=1)
        override = router.ManualOverride(
            weak.selection, actor="owner", reason="Prefer local execution"
        )
        decision = router.route(
            router.RouteRequest(task_tags=("keys",), override=override),
            (eligible, weak), now=self.now,
        )
        self.assertIsNone(decision.selection)
        self.assertEqual(decision.reason_code, "override_target_ineligible")
        self.assertFalse(decision.override_audit.applied)

    def test_missing_override_target_fails_closed_without_fallback(self) -> None:
        candidate = self.candidate("available", assurance=3)
        override = router.ManualOverride(
            router.RouteSelection("missing", "missing", "missing"),
            actor="owner", reason="Pinned compatibility",
        )
        decision = router.route(
            router.RouteRequest(override=override), (candidate,), now=self.now
        )
        self.assertIsNone(decision.selection)
        self.assertEqual(decision.reason_code, "override_target_not_found")

    def test_slash_containing_selection_components_cannot_collide(self) -> None:
        first = router.RouteCandidate(
            self.snapshot("harness", "h/a", capabilities={"x": "supported"}),
            self.snapshot("provider", "p", models=("m",),
                          capabilities={"x": "supported"}),
            "m", "local", 2, 1,
        )
        second = router.RouteCandidate(
            self.snapshot("harness", "h", capabilities={"x": "supported"}),
            self.snapshot("provider", "a/p", models=("m",),
                          capabilities={"x": "supported"}),
            "m", "local", 2, 1,
        )
        self.assertNotEqual(first.selection.candidate_id,
                            second.selection.candidate_id)
        override = router.ManualOverride(
            second.selection, actor="owner", reason="Exact namespaced route"
        )
        decision = router.route(
            router.RouteRequest(required_capabilities=("x",), override=override),
            (first, second), now=self.now,
        )
        self.assertEqual(decision.selection, second.selection)
        self.assertEqual(decision.reason_code, "selected_manual_override")
        self.assertEqual(len(decision.considered), 2)
        self.assertEqual(len(set(decision.considered)), 2)

    def test_override_audit_rejects_credential_shaped_text(self) -> None:
        with self.assertRaisesRegex(ValidationError, "credential-shaped"):
            router.ManualOverride(
                router.RouteSelection("h", "p", "m"), actor="owner",
                reason="Bearer secret-material",
            )

    def test_invalid_and_ambiguous_inputs_fail_closed(self) -> None:
        duplicate = self.candidate("same")
        with self.assertRaisesRegex(ValidationError, "unique"):
            router.route(router.RouteRequest(), (duplicate, duplicate), now=self.now)
        with self.assertRaisesRegex(ValidationError, "timezone-aware"):
            router.route(router.RouteRequest(), (), now=datetime(2026, 1, 1))
        with self.assertRaisesRegex(ValidationError, "duplicates"):
            router.RouteRequest(required_capabilities=("x", "x"))


if __name__ == "__main__":
    unittest.main()
