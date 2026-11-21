"""M5-06 controlled comparison acceptance (roadmap 9.5 M5-06; PLAN §7.3).

Pins: a reproducible cloud-only vs mixed comparison over a pinned task set and
shared acceptance rules; preserved failures/interventions/recoveries/elapsed and
auditable usage; real vs synthetic and unknown vs zero; and an explicit account
of the live adapters and authorizations a real run still needs.

Fixtures are entirely local: temp SQLite, real Core entities, and observations
whose provenance is declared.  Nothing here talks to a harness, a provider, or
the network, and no comparison in this file ever reports an invented result.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from teamcraft import (commands, domain, experiment, providers, scheduler,
                      storage, usage)
from teamcraft.errors import ConflictError, NotFoundError, ValidationError


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ExperimentM506Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m506-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc))
        self.project = domain.create_project(
            self.conn, "m506", str(self.root / "repo"), clock=self.clock)
        self.rules = experiment.AcceptanceRules(
            rules_version="rv-1",
            checks=("tests_pass", "ownership_respected"),
            config_digest=_digest_of("verifier-config-v1"),
        )
        # Two pinned tasks, each with its own durable attempt(s).
        self.tasks = [self._make_task(f"task {index}") for index in (1, 2)]

    # -- local fixture helpers ---------------------------------------------

    def _make_task(self, title: str):
        task = domain.create_task(self.conn, self.project.id, title, clock=self.clock)
        return domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version, clock=self.clock)

    def _fresh_attempt(self, task, *, result: str = "finished"):
        """Open (or re-open after an approved retry) a durable attempt.

        An attempt left ``unknown`` by a lost connection is settled through the
        reconcile edge first — that is the recovery path the roadmap requires
        before any further attempt, and it is why the observation that saw the
        lost contact legitimately recorded ``unknown`` at the time.
        """

        occupying = domain.occupying_attempt(self.conn, task.id)
        if occupying is not None and occupying.status == "unknown":
            domain.transition_attempt(self.conn, occupying.id, "reconciled_failed",
                                    clock=self.clock)
        current = domain.get_task(self.conn, task.id)
        if current.status == "running":
            current = domain.apply_task_event(
                self.conn, current.id, "retry_approved",
                expected_version=current.version, clock=self.clock)
        attempt = domain.create_attempt(
            self.conn, current.id, expected_version=current.version, clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated", clock=self.clock)
        if result == "failed":
            domain.transition_attempt(self.conn, attempt.id, "fail", clock=self.clock)
        elif result == "lost":
            domain.transition_attempt(self.conn, attempt.id, "runtime_lost",
                                    clock=self.clock)
        else:
            domain.transition_attempt(self.conn, attempt.id, "finish", clock=self.clock)
        return domain.get_attempt(self.conn, attempt.id)

    def _pinned(self, index: int, **overrides) -> experiment.PinnedTask:
        """Pinned task for fixture task ``index`` (plan order is sorted by id,
        so variants must reference tasks by identity, never by position)."""

        task = self.tasks[index]
        base = dict(acceptance_digest=_digest_of(f"acceptance-{task.title}"),
                    difficulty="low" if index == 0 else "medium")
        base.update(overrides)
        return experiment.PinnedTask(task.id, **base)

    def _plan(self, **overrides) -> experiment.ExperimentPlan:
        base = dict(
            title="cloud-only vs mixed",
            acceptance=self.rules,
            tasks=(self._pinned(0), self._pinned(1)),
            arms=(
                experiment.ArmSpec(arm="cloud_only", harness="opencode",
                                  providers=("example-cloud",), models=("cloud-model",)),
                experiment.ArmSpec(arm="mixed", harness="opencode",
                                  providers=("omlx", "example-cloud"),
                                  models=("local-model", "cloud-model")),
            ),
            pinned_at=self.clock.now().isoformat(),
        )
        base.update(overrides)
        return experiment.ExperimentPlan(**base)

    def _record_usage(self, task, attempt, *, locality, tokens_in, tokens_out,
                      cost=None, currency="USD", price_version="pv-1",
                      role="worker", precision="exact", model="cloud-model",
                      provider="example-cloud"):
        record = usage.UsageRecord(
            role=role, source="worker_report", precision=precision,
            harness="opencode", provider=provider, model=model, locality=locality,
            input_tokens=tokens_in, output_tokens=tokens_out,
            total_tokens=None if tokens_in is None or tokens_out is None
            else tokens_in + tokens_out,
            cache_tokens=None, response_count=1, wake_count=None,
            elapsed_ms=(None if tokens_in is None else tokens_in * 4),
            cost_microunits=cost, currency=currency, price_version=price_version,
            task_id=task.id, attempt_id=attempt.id,
            recorded_at=self.clock.now().isoformat())
        usage.record_usage(self.conn, self.project.id, record)
        return record.usage_id

    def _observation(self, plan, arm, index, *, origin="synthetic", outcome="verified",
                     elapsed=5000, interventions=0, recoveries=0, attempts=None,
                     usage_ids=(), note="local fixture arm", **extra):
        task = self.tasks[index]
        if attempts is None:
            attempt = self._fresh_attempt(task)
            # Cloud-only runs everything on the cloud provider; mixed genuinely
            # uses both, so neither arm contradicts its own label here.
            locality = "cloud" if arm == "cloud_only" or index == 0 else "local"
            attempts = (experiment.AttemptEvidence(
                attempt.id, "succeeded" if outcome == "verified" else
                ("failed" if outcome == "failed" else "unknown"),
                locality=locality, elapsed_ms=elapsed),)
        return experiment.TaskObservation(
            arm=arm, task_id=task.id, data_origin=origin, outcome=outcome,
            evidence_ref=f"fixture:m506/{arm}/{index}",
            observed_at=self.clock.now().isoformat(),
            attempts=tuple(attempts), usage_ids=tuple(usage_ids),
            elapsed_ms=elapsed, intervention_count=interventions,
            recovery_count=recoveries,
            synthetic_note=note if origin == "synthetic" else None,
            **extra)

    def _pin(self) -> experiment.ExperimentPlan:
        plan = self._plan()
        experiment.pin_plan(self.conn, self.project.id, plan)
        return plan

    def _clean_real_observations(self, plan):
        """Full coverage, both arms, fully attributed durable usage.

        Cloud-only does all the work on the cloud provider (2 x 1000 in /
        250 out = 2500 cloud tokens, costs 1200 + 900).  Mixed keeps
        2 x 600 in / 150 out in the cloud (1500 tokens, costs 400 + 300) and
        moves 2 x 400 in / 100 out local (1000 tokens, unpriced because a
        local run reported no cost).  These are the fixture's own numbers, and
        every figure in the report is reached through durable usage records.
        """

        cloud_only_costs = (1200, 900)
        mixed_cloud_costs = (400, 300)
        observations = []
        for index, task in enumerate(self.tasks):
            attempt = self._fresh_attempt(task)
            usage_id = self._record_usage(
                task, attempt, locality="cloud",
                tokens_in=1000, tokens_out=250, cost=cloud_only_costs[index])
            observations.append(self._observation(
                plan, "cloud_only", index, origin="real", elapsed=9000,
                attempts=(experiment.AttemptEvidence(
                    attempt.id, "succeeded", locality="cloud", elapsed_ms=9000),),
                usage_ids=(usage_id,), note=None))
        for index, task in enumerate(self.tasks):
            attempt = self._fresh_attempt(task)
            usage_ids = (
                self._record_usage(task, attempt, locality="cloud",
                                 tokens_in=600, tokens_out=150,
                                 cost=mixed_cloud_costs[index]),
                self._record_usage(task, attempt, locality="local",
                                 tokens_in=400, tokens_out=100,
                                 cost=None, currency=None, price_version=None,
                                 model="local-model", provider="omlx"),
            )
            observations.append(self._observation(
                plan, "mixed", index, origin="real", elapsed=7000,
                attempts=(experiment.AttemptEvidence(
                    attempt.id, "succeeded", locality="local", elapsed_ms=7000),),
                usage_ids=usage_ids, note=None))
        return observations

    def _event_count(self, kind: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0])

    # -- pinning -----------------------------------------------------------

    def test_plan_digest_is_reproducible_and_pins_every_choice(self) -> None:
        plan = self._plan()
        self.assertEqual(plan.plan_digest, self._plan().plan_digest)
        self.assertEqual(plan.plan_id, "exp-" + plan.plan_digest[:40])
        # Any change to the acceptance bar, the task set, a task's acceptance
        # binding, its difficulty stratum, or the arm profiles produces a
        # different experiment instead of silently reusing the pinned one.
        variants = [
            self._plan(acceptance=experiment.AcceptanceRules(
                rules_version="rv-2", checks=self.rules.checks,
                config_digest=self.rules.config_digest)),
            self._plan(acceptance=experiment.AcceptanceRules(
                rules_version="rv-1", checks=("tests_pass",),
                config_digest=self.rules.config_digest)),
            self._plan(acceptance=experiment.AcceptanceRules(
                rules_version="rv-1", checks=self.rules.checks,
                config_digest=_digest_of("other-verifier-config"))),
            self._plan(tasks=(self._pinned(0),)),
            self._plan(tasks=(self._pinned(0, acceptance_digest=_digest_of(
                "edited-acceptance")), self._pinned(1))),
            self._plan(tasks=(self._pinned(0, difficulty="high"), self._pinned(1))),
            self._plan(arms=(
                experiment.ArmSpec(arm="cloud_only", harness="opencode",
                                  providers=("other-cloud",), models=("cloud-model",)),
                self._plan().arms[1])),
        ]
        for variant in variants:
            self.assertNotEqual(variant.plan_digest, plan.plan_digest)
        # Task order must not change the experiment.
        shuffled = self._plan(tasks=tuple(reversed(self._plan().tasks)))
        self.assertEqual(shuffled.plan_digest, plan.plan_digest)

    def test_acceptance_is_shared_and_arms_cannot_be_relabelled(self) -> None:
        plan = self._plan()
        # One rules object for the whole plan: structurally, arms cannot carry
        # different acceptance bars.
        self.assertNotIn("acceptance", plan.arms[0].payload())
        self.assertEqual(plan.payload()["acceptance"]["rules_version"], "rv-1")
        with self.assertRaisesRegex(ValidationError, "fixed locality profile"):
            experiment.ArmSpec(arm="cloud_only", harness="opencode",
                              providers=("example-cloud",), models=("cloud-model",),
                              required_localities=("cloud", "local"))
        with self.assertRaisesRegex(ValidationError, "fixed locality profile"):
            experiment.ArmSpec(arm="mixed", harness="opencode",
                              providers=("omlx",), models=("local-model",),
                              required_localities=("local",))
        self.assertEqual(plan.arm_spec("mixed").required_localities, ("cloud", "local"))
        with self.assertRaisesRegex(ValidationError, "exactly cloud_only and mixed"):
            self._plan(arms=(self._plan().arms[0],))
        with self.assertRaisesRegex(ValidationError, "arm must be"):
            experiment.ArmSpec(arm="local_only", harness="opencode",
                              providers=("omlx",), models=("local-model",))
        with self.assertRaisesRegex(ValidationError, "non-empty task set"):
            self._plan(tasks=())
        with self.assertRaisesRegex(ValidationError, "duplicate task ids"):
            self._plan(tasks=(self._pinned(0), self._pinned(0)))
        # Acceptance rules themselves refuse blank/duplicate checks.
        with self.assertRaisesRegex(ValidationError, "checks"):
            experiment.AcceptanceRules(rules_version="rv-1", checks=())

    def test_pin_requires_real_tasks_and_replays_idempotently(self) -> None:
        plan = self._pin()
        experiment.pin_plan(self.conn, self.project.id, plan)
        self.assertEqual(self._event_count(experiment.PLAN_EVENT), 1)
        loaded = experiment.load_plan(self.conn, self.project.id, plan.plan_id)
        self.assertEqual(loaded.plan_digest, plan.plan_digest)
        self.assertEqual(len(experiment.list_plans(self.conn, self.project.id)), 1)
        ghost = experiment.ExperimentPlan(
            title="phantom", acceptance=self.rules,
            tasks=(experiment.PinnedTask("task-does-not-exist", _digest_of("x")),),
            arms=self._plan().arms, pinned_at=self.clock.now().isoformat())
        with self.assertRaises(NotFoundError):
            experiment.pin_plan(self.conn, self.project.id, ghost)

    def test_observations_stay_inside_the_pinned_matrix_and_bindings(self) -> None:
        plan = self._pin()
        obs = self._observation(plan, "cloud_only", 0, origin="synthetic")
        experiment.record_observation(self.conn, self.project.id, plan, obs)
        experiment.record_observation(self.conn, self.project.id, plan, obs)
        self.assertEqual(self._event_count(experiment.OBSERVATION_EVENT), 1)
        self.assertEqual(experiment.load_observations(
            self.conn, self.project.id, plan.plan_id), (obs,))

        # A task outside the pinned set cannot be observed against this plan.
        extra = self._make_task("task 3")
        extra_attempt = domain.create_attempt(
            self.conn, extra.id, expected_version=extra.version, clock=self.clock)
        with self.assertRaisesRegex(ValidationError, "outside the pinned task/arm"):
            experiment.record_observation(
                self.conn, self.project.id, plan,
                experiment.TaskObservation(
                    arm="mixed", task_id=extra.id, data_origin="synthetic",
                    outcome="verified", evidence_ref="fixture:m506/outside",
                    observed_at=self.clock.now().isoformat(),
                    attempts=(experiment.AttemptEvidence(
                        extra_attempt.id, "succeeded", locality="local"),),
                    synthetic_note="fixture"))

        # An attempt that belongs to another task is a binding violation.
        other_attempt = self._fresh_attempt(self.tasks[1])
        with self.assertRaisesRegex(ValidationError, "does not belong to task"):
            experiment.record_observation(
                self.conn, self.project.id, plan,
                experiment.TaskObservation(
                    arm="mixed", task_id=self.tasks[0].id, data_origin="synthetic",
                    outcome="verified", evidence_ref="fixture:m506/foreign",
                    observed_at=self.clock.now().isoformat(),
                    attempts=(experiment.AttemptEvidence(
                        other_attempt.id, "succeeded", locality="local"),),
                    synthetic_note="fixture"))

        # An invented usage id is refused rather than counted as nothing.
        with self.assertRaises(NotFoundError):
            experiment.record_observation(
                self.conn, self.project.id, plan,
                experiment.TaskObservation(
                    arm="mixed", task_id=self.tasks[0].id, data_origin="synthetic",
                    outcome="unknown", evidence_ref="fixture:m506/invented",
                    observed_at=self.clock.now().isoformat(),
                    usage_ids=("usage-not-real",), synthetic_note="fixture"))

    def test_duplicate_slot_in_one_report_is_a_conflict(self) -> None:
        plan = self._pin()
        first = self._observation(plan, "mixed", 0, origin="synthetic", elapsed=1000)
        second = self._observation(plan, "mixed", 0, origin="synthetic", elapsed=2000)
        with self.assertRaisesRegex(ConflictError, "duplicate observation"):
            experiment.reduce_comparison(self.conn, self.project.id, plan,
                                       (first, second),
                                       observed_at=self.clock.now().isoformat())
        # And a slot of the pinned matrix that was never observed at all stays
        # a visible gap rather than a silent zero.
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                             (first,),
                                             observed_at=self.clock.now().isoformat())
        self.assertEqual(report.summary("mixed").missing_task_ids,
                         (self.tasks[1].id,))
        self.assertFalse(report.claim_allowed)

    def test_provenance_and_outcome_claims_are_enforced(self) -> None:
        base = dict(arm="cloud_only", task_id=self.tasks[0].id,
                    outcome="unknown", evidence_ref="fixture:m506/x",
                    observed_at=self.clock.now().isoformat())
        with self.assertRaisesRegex(ValidationError, "provenance is never defaulted"):
            experiment.TaskObservation(**{**base, "data_origin": "huh"})
        # Synthetic needs an explanation; real must not have one.
        with self.assertRaisesRegex(ValidationError, "synthetic_note"):
            experiment.TaskObservation(**{**base, "data_origin": "synthetic"})
        with self.assertRaisesRegex(ValidationError, "synthetic_note"):
            experiment.TaskObservation(**{**base, "data_origin": "real",
                                         "synthetic_note": "fixture"})
        # Success and failure claims need the attempt evidence behind them.
        with self.assertRaisesRegex(ValidationError, "verified requires a succeeded"):
            experiment.TaskObservation(**{**base, "data_origin": "real",
                                         "outcome": "verified"})
        with self.assertRaisesRegex(ValidationError, "failed requires a failed"):
            experiment.TaskObservation(**{**base, "data_origin": "real",
                                         "outcome": "failed"})
        # Credentials never enter an observation, even as an evidence pointer.
        with self.assertRaisesRegex(ValidationError, "credential-shaped"):
            experiment.TaskObservation(**{**base, "data_origin": "real",
                                         "evidence_ref": "Bearer sk-abcdefgh1234"})

    # -- reduction: unknown vs zero, preserved evidence -------------------

    def test_nothing_observed_reports_unknown_not_zero(self) -> None:
        plan = self._pin()
        report = experiment.reduce_comparison(self.conn, self.project.id, plan, (),
                                            observed_at=self.clock.now().isoformat())
        self.assertEqual(report.data_class, "none")
        self.assertEqual(report.explanatory_power, "empty")
        self.assertFalse(report.claim_allowed)
        for arm in experiment.ARMS:
            summary = report.summary(arm)
            self.assertEqual(summary.tasks_observed, 0)
            self.assertEqual(set(summary.missing_task_ids), set(plan.task_ids))
            self.assertIsNone(summary.verified_rate.value)  # NOT 0.0
            self.assertIsNone(summary.total_elapsed_ms)
            self.assertIsNone(summary.median_verified_elapsed_ms)
        self.assertIsNone(report.metric("verified_rate").cloud_only.amount)
        self.assertIsNone(report.metric("cloud_total_tokens").delta)
        self.assertIn(experiment.BLOCKER_TASK_COVERAGE,
                      [item.code for item in report.blockers])

    def test_unreported_quantities_stay_unknown(self) -> None:
        plan = self._pin()
        observations = [
            self._observation(plan, arm, index, elapsed=None, interventions=None,
                            recoveries=None, outcome="unknown")
            for arm in experiment.ARMS for index in range(len(self.tasks))]
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        summary = report.summary("cloud_only")
        self.assertIsNone(summary.total_elapsed_ms)
        self.assertEqual(summary.elapsed.unknown_count, 2)
        self.assertIsNone(summary.interventions.known_sum)
        self.assertEqual(summary.interventions.unknown_count, 2)
        self.assertIsNone(summary.local_token_share.value)
        self.assertIsNone(report.metric("total_elapsed_ms").delta)
        codes = [item.code for item in report.blockers]
        self.assertIn(experiment.BLOCKER_USAGE_UNAVAILABLE, codes)
        self.assertFalse(report.claim_allowed)

    def test_reported_zero_is_kept_as_zero(self) -> None:
        plan = self._pin()
        observations = []
        for index, task in enumerate(self.tasks):
            attempt = self._fresh_attempt(task)
            usage_id = self._record_usage(task, attempt, locality="cloud",
                                         tokens_in=500, tokens_out=100, cost=700)
            observations.append(self._observation(
                plan, "cloud_only", index, origin="real", interventions=0,
                recoveries=0,
                attempts=(experiment.AttemptEvidence(
                    attempt.id, "succeeded", locality="cloud", elapsed_ms=1000),),
                usage_ids=(usage_id,), note=None))
        for index in range(len(self.tasks)):
            observations.append(self._observation(plan, "mixed", index,
                                                origin="synthetic", interventions=0))
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        cloud_only = report.summary("cloud_only")
        # A cloud-only arm with fully attributed usage genuinely has zero local
        # share; that is a known zero, not an unknown.
        self.assertEqual(cloud_only.local_token_share.value, 0.0)
        self.assertEqual(cloud_only.local_token_share.numerator, 0)
        self.assertTrue(cloud_only.local_token_share.complete)
        self.assertEqual(cloud_only.interventions.strict_total, 0)
        self.assertEqual(report.metric("intervention_count").cloud_only.amount, 0)

    def test_failures_interventions_and_recoveries_are_preserved(self) -> None:
        plan = self._pin()
        # cloud_only: task 1 fails outright, task 2 loses contact then recovers.
        failed = self._fresh_attempt(self.tasks[0], result="failed")
        lost = self._fresh_attempt(self.tasks[1], result="lost")
        cloud_obs = [
            self._observation(plan, "cloud_only", 0, origin="synthetic",
                            outcome="failed", interventions=2, recoveries=0,
                            attempts=(experiment.AttemptEvidence(
                                failed.id, "failed", locality="cloud",
                                elapsed_ms=8000),)),
            self._observation(plan, "cloud_only", 1, origin="synthetic",
                            outcome="unknown", interventions=1, recoveries=1,
                            attempts=(experiment.AttemptEvidence(
                                lost.id, "unknown", locality=None,
                                elapsed_ms=None),)),
        ]
        mixed_obs = [self._observation(plan, "mixed", index, origin="synthetic")
                     for index in range(len(self.tasks))]
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            cloud_obs + mixed_obs,
                                            observed_at=self.clock.now().isoformat())
        summary = report.summary("cloud_only")
        self.assertEqual(summary.failed_attempt_ids, (failed.id,))
        self.assertEqual(summary.failed_attempt_count, 1)
        self.assertEqual(summary.attempts_total, 2)
        self.assertEqual(summary.outcomes["failed"], 1)
        self.assertEqual(summary.outcomes["unknown"], 1)
        self.assertEqual(summary.interventions.known_sum, 3)
        self.assertEqual(summary.interventions.strict_total, 3)
        self.assertEqual(summary.recoveries.strict_total, 1)
        # The unattributed attempt locality is exposed as a gap, not guessed.
        self.assertEqual(summary.locality_evidence["unknown"], 1)
        self.assertEqual(summary.unattributed_locality_count, 1)
        codes = [item.code for item in report.blockers]
        self.assertIn(experiment.BLOCKER_OUTCOME_UNKNOWN, codes)
        self.assertIn(experiment.BLOCKER_LOCALITY_UNKNOWN, codes)
        # Failures survive the round trip through the report payload.
        payload = json.loads(json.dumps(report.payload(), sort_keys=True))
        self.assertIn(failed.id, payload["arm_summaries"]["cloud_only"]
                     ["failed_attempt_ids"])
        # An outcome the data cannot support is refused: failed_attempt_count
        # cannot be compared while an outcome is still unknown.
        metric = report.metric("failed_attempt_count")
        self.assertFalse(metric.comparable)
        self.assertIsNone(metric.better_arm)

    def test_median_and_coverage_strictness(self) -> None:
        plan = self._pin()
        observations = [self._observation(plan, arm, index, origin="synthetic",
                                         elapsed=elapsed)
                        for arm, elapsed in (("cloud_only", 9000), ("mixed", 6000))
                        for index in range(len(self.tasks))]
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        self.assertTrue(report.coverage_complete)
        self.assertEqual(report.summary("cloud_only")
                         .strict_median_verified_elapsed_ms, 9000.0)
        self.assertEqual(report.summary("mixed").strict_median_verified_elapsed_ms,
                         6000.0)
        # Drop one pinned task: coverage is incomplete, so the strict median is
        # unknown even though a partial median still exists for inspection.
        partial = experiment.reduce_comparison(
            self.conn, self.project.id, plan, observations[:3],
            observed_at=self.clock.now().isoformat())
        summary = partial.summary("mixed")
        self.assertEqual(summary.median_verified_elapsed_ms, 6000.0)
        self.assertIsNone(summary.strict_median_verified_elapsed_ms)
        self.assertIsNone(partial.metric("median_verified_elapsed_ms").delta)
        self.assertIn(experiment.BLOCKER_TASK_COVERAGE,
                      [item.code for item in partial.blockers])
        self.assertFalse(partial.claim_allowed)

    def test_deterministic_reduction(self) -> None:
        plan = self._pin()
        observations = [self._observation(plan, arm, index, origin="synthetic",
                                         elapsed=5000 + index)
                        for arm in experiment.ARMS for index in range(len(self.tasks))]
        stamp = self.clock.now().isoformat()
        first = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                           observations, observed_at=stamp)
        second = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                             list(reversed(observations)),
                                             observed_at=stamp)
        self.assertEqual(first.report_digest, second.report_digest)
        self.assertEqual(first.report_id, "cmp-" + first.report_digest[:40])
        # Pinning and reducing again from the durable store reproduces it.
        for observation in observations:
            experiment.record_observation(self.conn, self.project.id, plan, observation)
        replay = experiment.run_comparison(self.conn, self.project.id, plan.plan_id,
                                         observed_at=stamp)
        self.assertEqual(replay.report_digest, first.report_digest)

    # -- real vs synthetic -------------------------------------------------

    def test_synthetic_only_run_rehearses_and_never_claims(self) -> None:
        plan = self._pin()
        observations = [self._observation(plan, arm, index, origin="synthetic",
                                        elapsed=9000 if arm == "cloud_only" else 6000)
                        for arm in experiment.ARMS for index in range(len(self.tasks))]
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                             observations,
                                             observed_at=self.clock.now().isoformat())
        self.assertEqual(report.data_class, "synthetic")
        self.assertEqual(report.explanatory_power, "synthetic_rehearsal")
        self.assertFalse(report.claim_allowed)
        self.assertIn(experiment.BLOCKER_SYNTHETIC,
                      [item.code for item in report.blockers])
        # The arithmetic runs (so the method is exercised) but every metric
        # carries the synthetic marker and no metric may be quoted as a result.
        metric = report.metric("verified_rate")
        self.assertEqual(metric.delta, 0.0)
        self.assertIn(experiment.BLOCKER_SYNTHETIC, metric.reasons)
        self.assertTrue(report.coverage_complete)

    def test_mixed_origins_are_not_compared_at_all(self) -> None:
        plan = self._pin()
        observations = [self._observation(plan, "cloud_only", index, origin="real",
                                        interventions=0, recoveries=0,
                                        attempts=(experiment.AttemptEvidence(
                                            self._fresh_attempt(self.tasks[index]).id,
                                            "succeeded", locality="cloud"),),
                                        note=None)
                        for index in range(len(self.tasks))]
        observations += [self._observation(plan, "mixed", index, origin="synthetic")
                         for index in range(len(self.tasks))]
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        self.assertEqual(report.data_class, "mixed")
        self.assertEqual(report.explanatory_power, "mixed_origin")
        self.assertFalse(report.claim_allowed)
        self.assertIn(experiment.BLOCKER_MIXED_ORIGINS,
                      [item.code for item in report.blockers])
        for metric in report.metrics:
            self.assertFalse(metric.comparable)
            self.assertIsNone(metric.delta)
            self.assertIsNone(metric.better_arm)
            self.assertIn(experiment.BLOCKER_MIXED_ORIGINS, metric.reasons)

    def test_real_full_run_supports_a_claim_from_durable_usage(self) -> None:
        plan = self._pin()
        observations = self._clean_real_observations(plan)
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        self.assertEqual(report.data_class, "real")
        self.assertEqual([item.payload() for item in report.blockers], [])
        self.assertTrue(report.claim_allowed)
        self.assertEqual(report.explanatory_power, "measured")
        cloud_only = report.summary("cloud_only")
        mixed = report.summary("mixed")
        # Cloud tokens come straight from the durable records: cloud-only did
        # 2 x (1000+250); mixed kept 2 x (600+150) in the cloud.
        self.assertEqual(cloud_only.cloud_total_tokens.strict_total, 2500)
        self.assertEqual(mixed.cloud_total_tokens.strict_total, 1500)
        tokens = report.metric("cloud_total_tokens")
        self.assertTrue(tokens.comparable)
        self.assertEqual(tokens.delta, -1000)
        self.assertEqual(tokens.relative, -0.4)
        self.assertEqual(tokens.better_arm, "mixed")
        cost = report.metric("cloud_cost_microunits")
        self.assertEqual(cost.cloud_only.amount, 2100)
        self.assertEqual(cost.mixed.amount, 700)
        self.assertEqual(cost.better_arm, "mixed")
        self.assertEqual(cloud_only.local_token_share.value, 0.0)
        self.assertEqual(mixed.local_token_share.value, 1000 / 2500)
        share = report.metric("local_token_share")
        self.assertEqual(share.better_arm, "mixed")
        # Quality did not change, so it is a tie, not a win.
        self.assertEqual(report.metric("verified_rate").better_arm, "tie")
        # Cloud tokens per verified task stay traceable to the same totals.
        per_task = report.metric("cloud_tokens_per_verified_task")
        self.assertEqual(per_task.cloud_only.amount, 1250.0)
        self.assertEqual(per_task.mixed.amount, 750.0)
        # Every figure is backed by listed, resolvable usage records.
        self.assertEqual(cloud_only.usage_record_count, 2)
        for usage_id in mixed.usage_ids:
            usage.get_usage(self.conn, self.project.id, usage_id)

    def test_relabeled_arm_and_gaps_kill_the_claim(self) -> None:
        plan = self._pin()
        observations = self._clean_real_observations(plan)
        # cloud_only consumed local capacity: the label is now false.
        task, attempt = self.tasks[0], self._fresh_attempt(self.tasks[0])
        leak = self._record_usage(task, attempt, locality="local",
                                 tokens_in=50, tokens_out=10, cost=None,
                                 currency=None, price_version=None,
                                 model="local-model", provider="omlx")
        observations[0] = dataclasses.replace(observations[0],
                                             usage_ids=(*observations[0].usage_ids, leak))
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        self.assertFalse(report.claim_allowed)
        codes = [item.code for item in report.blockers]
        self.assertIn(experiment.BLOCKER_LOCALITY_MISMATCH, codes)
        self.assertFalse(report.metric("cloud_total_tokens").comparable)
        self.assertFalse(report.metric("cloud_cost_microunits").comparable)
        self.assertIsNone(report.metric("cloud_cost_microunits").delta)

    def test_unpriced_and_unattributed_costs_are_gaps_not_numbers(self) -> None:
        plan = self._pin()
        observations = []
        for index, task in enumerate(self.tasks):
            attempt = self._fresh_attempt(task)
            # No price version and (for one record) no currency: cost cannot be
            # totalled, and the spend is not simply reported as zero either.
            usage_id = self._record_usage(task, attempt, locality="cloud",
                                         tokens_in=800, tokens_out=200, cost=900,
                                         price_version=None if index == 0 else "pv-1",
                                         currency=None if index == 0 else "USD")
            observations.append(self._observation(
                plan, "cloud_only", index, origin="real",
                attempts=(experiment.AttemptEvidence(
                    attempt.id, "succeeded", locality="cloud", elapsed_ms=1000),),
                usage_ids=(usage_id,), note=None))
        for index in range(len(self.tasks)):
            observations.append(self._observation(plan, "mixed", index,
                                                origin="synthetic"))
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        cost = report.metric("cloud_cost_microunits").cloud_only
        self.assertIsNone(cost.amount)
        self.assertIn(experiment.BLOCKER_COST_UNATTRIBUTED, cost.reasons)
        self.assertIn(experiment.BLOCKER_COST_PRICE_VERSION, cost.reasons)
        codes = [item.code for item in report.blockers]
        self.assertIn(experiment.BLOCKER_COST_UNATTRIBUTED, codes)

    def test_declared_interventions_beyond_durable_evidence_are_flagged(self) -> None:
        plan = self._pin()
        observations = self._clean_real_observations(plan)
        observations[0] = dataclasses.replace(observations[0],
                                            intervention_count=1, recovery_count=0)
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        codes = [item.code for item in report.blockers]
        self.assertIn(experiment.BLOCKER_INTERVENTION_EVIDENCE, codes)
        self.assertFalse(report.metric("intervention_count").comparable)
        # Back the claim with a durable, confirmed cloud-escalation approval.
        self._grant_cloud_approval(self.tasks[0].id)
        healed = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        self.assertNotIn(experiment.BLOCKER_INTERVENTION_EVIDENCE,
                         [item.code for item in healed.blockers])
        self.assertEqual(healed.metric("intervention_count").cloud_only.amount, 1)
        self.assertTrue(healed.metric("intervention_count").comparable)
        self.assertTrue(healed.claim_allowed)

    def test_zero_denominators_are_reported_not_invented(self) -> None:
        """A losing arm with no accepted task gets no per-task ratio, and a
        known-zero baseline never becomes a percentage saving."""

        plan = self._pin()
        observations = []
        for index, task in enumerate(self.tasks):
            attempt = self._fresh_attempt(task, result="failed")
            # Cloud spend exists even though nothing was accepted: the spend is
            # real, the per-accepted-task ratio simply has no denominator.
            usage_id = self._record_usage(task, attempt, locality="cloud",
                                         tokens_in=700, tokens_out=200, cost=500)
            observations.append(self._observation(
                plan, "cloud_only", index, origin="synthetic", outcome="failed",
                attempts=(experiment.AttemptEvidence(
                    attempt.id, "failed", locality="cloud", elapsed_ms=1500),),
                usage_ids=(usage_id,)))
            observations.append(self._observation(plan, "mixed", index,
                                                origin="synthetic"))
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        per_task = report.metric("cloud_tokens_per_verified_task")
        self.assertIsNone(per_task.cloud_only.amount)
        self.assertIn(experiment.REASON_NO_VERIFIED_TASK, per_task.cloud_only.reasons)
        self.assertIsNone(per_task.delta)
        # cloud_only spent no cloud tokens (it never ran any): a known zero
        # baseline whose relative change stays undefined.
        verified_rate = report.metric("verified_task_count")
        self.assertEqual(verified_rate.cloud_only.amount, 0)
        self.assertEqual(verified_rate.delta, 2)
        self.assertIsNone(verified_rate.relative)
        self.assertIn(experiment.REASON_BASELINE_ZERO, verified_rate.reasons)
        self.assertEqual(verified_rate.better_arm, "mixed")

    def test_record_comparison_is_idempotent_audit(self) -> None:
        plan = self._pin()
        observations = [self._observation(plan, arm, index, origin="synthetic")
                        for arm in experiment.ARMS for index in range(len(self.tasks))]
        report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                            observations,
                                            observed_at=self.clock.now().isoformat())
        experiment.record_comparison(self.conn, self.project.id, report)
        experiment.record_comparison(self.conn, self.project.id, report)
        self.assertEqual(self._event_count(experiment.COMPARISON_EVENT), 1)
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind = ?",
            (experiment.COMPARISON_EVENT,)).fetchone()
        stored = json.loads(str(row[0]))
        self.assertEqual(stored["report_digest"], report.report_digest)
        self.assertEqual(stored["report"]["data_class"], "synthetic")

    # -- what a live run still needs --------------------------------------

    def _grant_cloud_approval(self, task_id: str) -> None:
        payload = {"project_id": self.project.id, "task_id": task_id,
                   "binding_digest": _digest_of(f"bind-{task_id}")}
        submission = commands.submit_command(
            self.conn, idempotency_key=f"m506-approval-{task_id}",
            kind=scheduler.CLOUD_APPROVAL_COMMAND_KIND,
            payload_json=json.dumps(payload, sort_keys=True),
            clock=self.clock)
        commands.claim_command(self.conn, submission.command.id, clock=self.clock)
        commands.confirm_command(
            self.conn, submission.command.id,
            result_json=json.dumps({"approved": True,
                                   "binding_digest": payload["binding_digest"]},
                                  sort_keys=True),
            clock=self.clock)

    def _healthy_probes(self, plan) -> None:
        observed = self.clock.now()
        valid = (observed + timedelta(seconds=300)).isoformat()
        seen: set[tuple[str, str]] = set()
        for spec in plan.arms:
            entries = [("harness", spec.harness, None, spec.models)]
            entries += [("provider", name, name, spec.models)
                       for name in spec.providers]
            for entity_type, name, provider, models in entries:
                if (entity_type, name) in seen:
                    continue
                seen.add((entity_type, name))
                providers.record_capability_probe(
                    self.conn, self.project.id,
                    providers.CapabilitySnapshot(
                        entity_type=entity_type, name=name, version="1.0.0",
                        provider=provider, models=models, discovery={},
                        capabilities={"tool_calling": "supported"},
                        health="healthy", reason_code="ok", source="fixture",
                        observed_at=observed.isoformat(), valid_until=valid))

    def _register_prices(self, plan) -> None:
        for spec in plan.arms:
            for model in spec.models:
                usage.record_price_snapshot(
                    self.conn, self.project.id,
                    usage.PriceSnapshot(
                        model=model, currency="USD", price_version="pv-1",
                        rates={"input": 1000, "output": 2000, "cache": 100},
                        source="operator-list",
                        observed_at=self.clock.now().isoformat()))

    def test_live_requirements_lists_adapters_and_authorizations(self) -> None:
        plan = self._pin()
        report = experiment.live_requirements(self.conn, self.project.id, plan,
                                            clock=self.clock)
        self.assertFalse(report.live_ready)
        kinds = {(item.kind, item.arm, item.status) for item in report.requirements}
        self.assertIn(("harness_adapter", "cloud_only", "missing"), kinds)
        self.assertIn(("harness_adapter", "mixed", "missing"), kinds)
        self.assertIn(("provider_adapter", "cloud_only", "missing"), kinds)
        self.assertIn(("price_provenance", "mixed", "missing"), kinds)
        self.assertIn(("cloud_authorization", "cloud_only", "missing"), kinds)
        # Both arms need cloud, so both need an authorization a human grants.
        authorizations = report.authorizations()
        self.assertTrue(authorizations)
        self.assertTrue(all(item.authorization_required for item in authorizations))
        self.assertEqual(
            {item.kind for item in authorizations},
            {"provider_adapter", "cloud_authorization"})
        # Nothing was probed or authorized here, and the report says so rather
        # than assuming the plan's providers are reachable.
        actions = " ".join(item.action for item in report.pending())
        self.assertIn("probe", actions)
        self.assertIn("approval", actions)

    def test_live_requirements_satisfied_only_by_durable_evidence(self) -> None:
        plan = self._pin()
        self._healthy_probes(plan)
        self._register_prices(plan)
        report = experiment.live_requirements(self.conn, self.project.id, plan,
                                            clock=self.clock)
        self.assertFalse(report.live_ready)
        self.assertEqual({item.kind for item in report.pending()},
                         {"cloud_authorization"})
        for task in self.tasks:
            self._grant_cloud_approval(task.id)
        ready = experiment.live_requirements(self.conn, self.project.id, plan,
                                           clock=self.clock)
        self.assertTrue(ready.live_ready)
        self.assertEqual(ready.pending(), ())
        self.assertTrue(all(item.status == "satisfied" for item in ready.requirements))

    def test_stale_or_unhealthy_probe_is_not_healthy(self) -> None:
        plan = self._pin()
        past = self.clock.now() - timedelta(seconds=600)
        providers.record_capability_probe(
            self.conn, self.project.id,
            providers.CapabilitySnapshot(
                entity_type="harness", name="opencode", version="1.0.0",
                provider=None, models=(), discovery={}, capabilities={},
                health="healthy", reason_code="ok", source="fixture",
                observed_at=past.isoformat(),
                valid_until=(past + timedelta(seconds=60)).isoformat()))
        report = experiment.live_requirements(self.conn, self.project.id, plan,
                                            clock=self.clock)
        statuses = {(item.kind, item.subject): item.status
                    for item in report.requirements}
        self.assertEqual(statuses[("harness_adapter", "opencode")], "stale")
        self.assertFalse(report.live_ready)
        providers.record_capability_probe(
            self.conn, self.project.id,
            providers.CapabilitySnapshot(
                entity_type="provider", name="example-cloud", version="1.0.0",
                provider="example-cloud", models=("cloud-model",), discovery={},
                capabilities={}, health="unhealthy", reason_code="http_error",
                source="fixture", observed_at=self.clock.now().isoformat(),
                valid_until=(self.clock.now() + timedelta(seconds=300)).isoformat()))
        after = experiment.live_requirements(self.conn, self.project.id, plan,
                                           clock=self.clock)
        statuses = {(item.kind, item.subject): item.status
                    for item in after.requirements}
        self.assertEqual(statuses[("provider_adapter", "example-cloud")], "unhealthy")
        self.assertFalse(after.live_ready)

    def test_no_live_call_is_made_and_no_credential_is_read(self) -> None:
        source = inspect.getsource(experiment)
        for forbidden in ("urllib", "http.client", "subprocess", "socket",
                         "requests", "os.environ", "getenv"):
            self.assertNotIn(forbidden, source,
                            f"experiment.py must not perform {forbidden}")

        def _refuse(*args, **kwargs):  # pragma: no cover - guard must trip
            raise AssertionError("experiment attempted a network call")

        plan = self._pin()
        observations = self._clean_real_observations(plan)
        stamp = self.clock.now().isoformat()
        with mock.patch.object(socket, "socket", _refuse), \
                mock.patch.object(socket, "create_connection", _refuse):
            report = experiment.reduce_comparison(self.conn, self.project.id, plan,
                                                observations, observed_at=stamp)
            requirements = experiment.live_requirements(self.conn, self.project.id,
                                                       plan, clock=self.clock)
        self.assertEqual(report.data_class, "real")
        self.assertFalse(requirements.live_ready)


if __name__ == "__main__":
    unittest.main()
