"""M5-04 usage data and price provenance acceptance (roadmap 9.5).

Pins: role separation (CEO/Worker/Verifier), NULL-preserving quantities
(unknown is never zero), exact/estimated/unavailable distinction, local/cloud
screening, auditable totals without invented prices, and versioned price
provenance.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from teamcraft import (briefs, domain, escalation, events, leader, providers,
                      scheduler, storage, usage, verification, worker_reports)
from teamcraft.errors import NotFoundError, ValidationError


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class UsageM504Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m504-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc))
        self.project = domain.create_project(
            self.conn, "m504", str(self.root / "repo"), clock=self.clock)
        self.task = domain.create_task(
            self.conn, self.project.id, "billable", clock=self.clock)
        self.task = domain.apply_task_event(
            self.conn, self.task.id, "dependencies_satisfied",
            expected_version=self.task.version, clock=self.clock)
        self.attempt = domain.create_attempt(
            self.conn, self.task.id, expected_version=self.task.version,
            clock=self.clock)

    # -- helpers ----------------------------------------------------------

    def _record(self, **overrides):
        base = dict(
            role="worker", source="worker_report", precision="exact",
            harness="opencode", provider="omlx", model="local-model",
            locality="local", input_tokens=1000, output_tokens=250,
            total_tokens=1250, cache_tokens=64, response_count=3,
            wake_count=None, elapsed_ms=4200, task_id=self.task.id,
            attempt_id=self.attempt.id,
            recorded_at=self.clock.now().isoformat(),
        )
        base.update(overrides)
        return usage.UsageRecord(**base)

    def _event_count(self, kind: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0])

    # -- record contract --------------------------------------------------

    def test_nulls_are_preserved_never_zeroed(self) -> None:
        record = self._record(cache_tokens=None, wake_count=None,
                             elapsed_ms=None, cost_microunits=None,
                             currency=None, price_version=None)
        usage.record_usage(self.conn, self.project.id, record)
        stored = usage.get_usage(self.conn, self.project.id, record.usage_id)
        self.assertIsNone(stored.cache_tokens)
        self.assertIsNone(stored.wake_count)
        self.assertIsNone(stored.elapsed_ms)
        self.assertIsNone(stored.cost_microunits)
        self.assertIsNone(stored.currency)
        self.assertIsNone(stored.price_version)
        # An explicitly reported zero is a different, known value.
        zero = self._record(cache_tokens=0)
        self.assertNotEqual(zero.usage_id, record.usage_id)
        self.assertEqual(zero.cache_tokens, 0)

    def test_roles_and_locality_and_precision_are_validated(self) -> None:
        for role in ("ceo", "worker", "verifier"):
            self.assertEqual(self._record(role=role,
                                        wake_count=2 if role == "ceo" else None,
                                        ).role, role)
        with self.assertRaisesRegex(ValidationError, "role"):
            self._record(role="observer")
        with self.assertRaisesRegex(ValidationError, "locality"):
            self._record(locality="onprem")
        # exact/estimated require a known token quantity; unavailable forbids
        # any — the three states cannot be conflated.
        with self.assertRaisesRegex(ValidationError, "unavailable contradicts"):
            self._record(precision="unavailable")
        with self.assertRaisesRegex(ValidationError, "requires at least one"):
            self._record(precision="exact", input_tokens=None,
                        output_tokens=None, total_tokens=None,
                        cache_tokens=None)
        quiet = self._record(input_tokens=None, output_tokens=None,
                             total_tokens=None, cache_tokens=None,
                             precision="unavailable")
        self.assertEqual(quiet.precision, "unavailable")

    def test_total_tokens_is_audit_evidence_and_contradictions_are_loud(self) -> None:
        # total == known parts is consistent; total below parts is a loud
        # contradiction (never silently inflating or shrinking totals).
        ok = self._record(total_tokens=1314)  # 1000 + 250 + 64 cache aside
        self.assertEqual(ok.total_tokens, 1314)
        with self.assertRaisesRegex(ValidationError, "contradicts known parts"):
            self._record(total_tokens=100)
        # a total alone is still a known token quantity
        total_only = self._record(input_tokens=None, output_tokens=None,
                                 cache_tokens=None, total_tokens=800)
        self.assertEqual(total_only.precision, "exact")

    def test_negative_and_boolean_quantities_rejected(self) -> None:
        for field, bad in (("input_tokens", -1), ("elapsed_ms", -5),
                          ("response_count", True), ("wake_count", False),
                          ("cost_microunits", -1)):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    self._record(**{field: bad})

    def test_cost_currency_and_identifier_rules(self) -> None:
        # A cost without a currency is stored (dropping money would be its
        # own falsification) but flagged as unattributed — never attributed.
        unattributed = self._record(cost_microunits=500)
        self.assertIsNone(unattributed.currency)
        self.assertTrue(unattributed.cost_unattributed)
        with self.assertRaisesRegex(ValidationError, "ISO-4217"):
            self._record(cost_microunits=500, currency="usd")
        with self.assertRaisesRegex(ValidationError, "price_version"):
            self._record(price_version="price v1; latest!")
        priced = self._record(cost_microunits=500, currency="USD",
                             price_version="2026-11")
        self.assertEqual(priced.currency, "USD")
        self.assertFalse(priced.cost_unattributed)

    def test_records_are_durable_idempotent_and_role_separable(self) -> None:
        worker = self._record()
        ceo = self._record(role="ceo", source="wake_dispatcher",
                          harness=None, provider=None, model=None,
                          locality=None,
                          input_tokens=None, output_tokens=None,
                          total_tokens=None, cache_tokens=None,
                          precision="unavailable",
                          response_count=None, wake_count=5)
        verifier = self._record(role="verifier", source="verifier_run",
                               model=None, locality="local",
                               cost_microunits=None)
        for record in (worker, ceo, verifier):
            usage.record_usage(self.conn, self.project.id, record)
        replay = usage.record_usage(self.conn, self.project.id, worker)
        self.assertEqual(
            replay.event_id, "evt-usage-" + worker.record_digest[:40])
        self.assertEqual(self._event_count(usage.USAGE_EVENT), 3,
                         "identical facts must replay without duplicates")

        self.assertEqual({r.role for r in usage.list_usage(self.conn, self.project.id)},
                         {"ceo", "worker", "verifier"})
        self.assertEqual([r.usage_id for r in
                         usage.list_usage(self.conn, self.project.id, role="ceo")],
                         [ceo.usage_id])
        self.assertEqual([r.usage_id for r in
                         usage.list_usage(self.conn, self.project.id, role="worker")],
                         [worker.usage_id])
        self.assertEqual([r.usage_id for r in
                         usage.list_usage(self.conn, self.project.id,
                                         locality="local")],
                         [worker.usage_id, verifier.usage_id])
        # unknown locality is included when unfiltered, excluded when pinned
        self.assertEqual([r.usage_id for r in
                         usage.list_usage(self.conn, self.project.id,
                                         locality="cloud")], [])
        with self.assertRaises(NotFoundError):
            usage.get_usage(self.conn, self.project.id, "usage-ghost")

    def test_bindings_must_exist_and_nest_correctly(self) -> None:
        other_task = domain.create_task(
            self.conn, self.project.id, "other", clock=self.clock)
        other_task = domain.apply_task_event(
            self.conn, other_task.id, "dependencies_satisfied",
            expected_version=other_task.version, clock=self.clock)
        other_attempt = domain.create_attempt(
            self.conn, other_task.id, expected_version=other_task.version,
            clock=self.clock)
        with self.assertRaises(NotFoundError):
            usage.record_usage(self.conn, self.project.id,
                             self._record(task_id="task-does-not-exist"))
        with self.assertRaises(NotFoundError):
            usage.record_usage(self.conn, self.project.id,
                             self._record(attempt_id="att-does-not-exist"))
        with self.assertRaisesRegex(ValidationError, "does not belong"):
            usage.record_usage(self.conn, self.project.id,
                             self._record(attempt_id=other_attempt.id,
                                        task_id=self.task.id))
        with self.assertRaises(NotFoundError):
            usage.record_usage(self.conn, "project-ghost", self._record())

    # -- totals ------------------------------------------------------------

    def test_totals_never_sum_nulls_and_flag_gaps(self) -> None:
        usage.record_usage(self.conn, self.project.id,
                         self._record(input_tokens=1000, output_tokens=100,
                                    cache_tokens=None, elapsed_ms=1000))
        usage.record_usage(self.conn, self.project.id,
                         self._record(input_tokens=None, output_tokens=50,
                                    cache_tokens=None, elapsed_ms=2000))
        totals = usage.usage_totals(self.conn, self.project.id, role="worker")
        inputs = totals.dimension("input_tokens")
        self.assertEqual(inputs.known_sum, 1000)
        self.assertEqual(inputs.unknown_count, 1)
        self.assertFalse(inputs.complete)
        # strict answer stays NULL: partial data is never presented as total
        self.assertIsNone(inputs.strict_total)
        outputs = totals.dimension("output_tokens")
        self.assertEqual(outputs.strict_total, 150)
        caches = totals.dimension("cache_tokens")
        self.assertFalse(caches.available)
        self.assertIsNone(caches.known_sum)
        self.assertIsNone(caches.strict_total)
        elapsed = totals.dimension("elapsed_ms")
        self.assertEqual(elapsed.strict_total, 3000)

    def test_empty_totals_are_unavailable_not_zero(self) -> None:
        totals = usage.usage_totals(self.conn, self.project.id)
        self.assertEqual(totals.record_count, 0)
        for name in usage.USAGE_DIMENSIONS:
            dimension = totals.dimension(name)
            self.assertIsNone(dimension.known_sum)
            self.assertIsNone(dimension.strict_total)
            self.assertFalse(dimension.available)

    def test_precision_counts_distinguish_exact_from_estimated(self) -> None:
        usage.record_usage(self.conn, self.project.id, self._record(precision="exact"))
        usage.record_usage(self.conn, self.project.id,
                         self._record(precision="estimated", input_tokens=None,
                                    output_tokens=500, total_tokens=None,
                                    cache_tokens=None))
        usage.record_usage(self.conn, self.project.id,
                         self._record(role="ceo", precision="unavailable",
                                    input_tokens=None, output_tokens=None,
                                    total_tokens=None, cache_tokens=None,
                                    wake_count=2))
        totals = usage.usage_totals(self.conn, self.project.id)
        self.assertEqual((totals.exact_count, totals.estimated_count,
                         totals.unavailable_count), (1, 1, 1))

    def test_cost_totals_track_currency_and_price_version_without_invention(self) -> None:
        usage.record_usage(self.conn, self.project.id, self._record(
            precision="estimated", cost_microunits=300, currency="USD",
            price_version="2026-10"))
        usage.record_usage(self.conn, self.project.id, self._record(
            precision="estimated", cost_microunits=200, currency="USD",
            price_version="2026-11"))
        usage.record_usage(self.conn, self.project.id, self._record(
            precision="estimated", cost_microunits=75, currency="EUR",
            price_version="2026-11"))
        usage.record_usage(self.conn, self.project.id, self._record(
            precision="estimated", cost_microunits=10, currency="USD",
            price_version=None))
        # a claimed cost that arrived without a currency: kept, but never
        # merged into a real-currency total
        usage.record_usage(self.conn, self.project.id, self._record(
            precision="estimated", cost_microunits=40))
        totals = usage.usage_totals(self.conn, self.project.id)
        self.assertTrue(totals.mixed_currencies)
        usd = totals.cost_by_currency["USD"]
        self.assertEqual(usd.known_sum, 510)  # 300 + 200 + 10 — no 40 leak
        self.assertEqual(totals.cost_by_currency["EUR"].known_sum, 75)
        self.assertEqual(
            totals.cost_by_currency[usage.UNATTRIBUTED_CURRENCY].known_sum, 40)
        self.assertEqual(totals.unattributed_cost_count, 1)
        self.assertEqual(totals.missing_price_version_cost_count, 2)
        self.assertEqual(totals.price_versions, ("2026-10", "2026-11"))
        self.assertTrue(totals.has_traceability_gap)
        # No API exists to fold mixed/unattributed money into one number.
        self.assertNotIn("total_cost", totals.payload())
        self.assertNotIn("total_cost_microunits", totals.payload())

    # -- price provenance ----------------------------------------------------

    def _snapshot(self, **overrides):
        base = dict(model="cloud-model", currency="USD",
                    price_version="2026-11",
                    rates={"input": 2_500_000, "output": 10_000_000,
                          "cache": 250_000},
                    source="provider_pricing_page",
                    observed_at=self.clock.now().isoformat())
        base.update(overrides)
        return usage.PriceSnapshot(**base)

    def test_price_snapshots_are_durable_and_latest_wins(self) -> None:
        older = self._snapshot(price_version="2026-10",
                              observed_at=datetime(2026, 10, 1,
                                                 tzinfo=timezone.utc).isoformat())
        newer = self._snapshot(price_version="2026-11")
        usage.record_price_snapshot(self.conn, self.project.id, older)
        usage.record_price_snapshot(self.conn, self.project.id, newer)
        replay = usage.record_price_snapshot(self.conn, self.project.id, newer)
        self.assertEqual(replay.event_id,
                         "evt-price-" + newer.snapshot_digest[:40])
        self.assertEqual(self._event_count(usage.PRICE_SNAPSHOT_EVENT), 2)
        latest = usage.latest_price_snapshot(self.conn, self.project.id,
                                            "cloud-model")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.price_version, "2026-11")
        self.assertIsNone(
            usage.latest_price_snapshot(self.conn, self.project.id, "ghost"))
        self.assertIsNone(usage.latest_price_snapshot(
            self.conn, self.project.id, "cloud-model", currency="EUR"))
        with self.assertRaises(ValidationError):
            self._snapshot(rates={"input": 1, "output": 2})  # missing cache
        with self.assertRaises(ValidationError):
            self._snapshot(rates={"input": 1, "output": 2, "cache": None,
                                 "extra": 3})

    def test_estimated_cost_never_fabricates_and_stays_labeled_derivation(self) -> None:
        snapshot = self._snapshot()
        usage.record_price_snapshot(self.conn, self.project.id, snapshot)

        billable = self._record(model="cloud-model", locality="cloud",
                               input_tokens=1_000_000, output_tokens=100_000,
                               total_tokens=None, cache_tokens=None,
                               precision="estimated")
        # cache_tokens unknown -> whole estimate refused, not treated as free
        self.assertIsNone(usage.estimate_cost_microunits(billable, snapshot))

        complete = self._record(model="cloud-model", locality="cloud",
                               input_tokens=1_000_000, output_tokens=100_000,
                               total_tokens=1_100_000, cache_tokens=0,
                               precision="estimated")
        # 1_000_000*2_500_000 + 100_000*10_000_000 = 3.5e12 / 1e6
        self.assertEqual(usage.estimate_cost_microunits(complete, snapshot),
                         3_500_000)

        quoted = self._record(model="cloud-model", locality="cloud",
                              precision="exact", cost_microunits=1234,
                              currency="USD", price_version="2026-11")
        # a reported bill is never overwritten or shadowed by a derivation
        self.assertIsNone(usage.estimate_cost_microunits(quoted, snapshot))

        missing_rate = self._snapshot(
            rates={"input": 2_500_000, "output": None, "cache": 250_000})
        self.assertIsNone(usage.estimate_cost_microunits(complete, missing_rate))
        # model/currency mismatches refuse instead of approximating
        self.assertIsNone(usage.estimate_cost_microunits(
            self._record(model="other-model", locality="cloud"), snapshot))
        self.assertIsNone(usage.estimate_cost_microunits(
            self._record(locality="cloud", currency="EUR"), snapshot))

    # -- scheduler (M5-03) seam ---------------------------------------------

    def test_task_token_usage_is_null_aware_for_the_scheduler_seam(self) -> None:
        # No records yet: all-NULL, exactly what scheduler expects pre-data.
        empty = usage.task_token_usage(self.conn, self.project.id, self.task.id)
        self.assertFalse(empty.is_known)

        usage.record_usage(self.conn, self.project.id,
                         self._record(input_tokens=1000, output_tokens=100))
        partial = usage.task_token_usage(self.conn, self.project.id, self.task.id)
        self.assertEqual(partial.input_tokens, 1000)
        self.assertEqual(partial.total_tokens, 1100)

        # A second record with unknown input keeps the TASK dimension unknown
        # — unknown is never summed as zero into the budget.
        usage.record_usage(self.conn, self.project.id,
                         self._record(role="verifier", input_tokens=None,
                                    output_tokens=50, total_tokens=None))
        unknown = usage.task_token_usage(self.conn, self.project.id, self.task.id)
        self.assertIsNone(unknown.input_tokens)
        # the total fallback needs EVERY record's total; one NULL total keeps
        # the task total unknown too
        self.assertIsNone(unknown.total_tokens)
        self.assertEqual(unknown.output_tokens, 150)

        # Budget layer keeps its conservative posture on the unknown side.
        evaluation = escalation.evaluate_budget(
            escalation.BudgetLimits(max_input_tokens=10_000), unknown)
        self.assertEqual(evaluation.aggregate, "unknown")

    def test_task_token_usage_refuses_mixed_currency_costs(self) -> None:
        usage.record_usage(self.conn, self.project.id, self._record(
            precision="estimated", cost_microunits=100, currency="USD"))
        usage.record_usage(self.conn, self.project.id, self._record(
            role="verifier", precision="estimated", cost_microunits=50,
            currency="EUR"))
        combined = usage.task_token_usage(self.conn, self.project.id, self.task.id)
        self.assertIsNone(combined.cost_microunits)
        single = usage.task_token_usage(self.conn, self.project.id, self.task.id,
                                       locality="local")
        self.assertIsNone(single.cost_microunits)  # still mixed within local

    # -- hygiene -------------------------------------------------------------

    def test_credential_shaped_content_rejected_without_writes(self) -> None:
        before = self._event_count(usage.USAGE_EVENT)
        with self.assertRaisesRegex(ValidationError, "credential-shaped"):
            self._record(source="bearer-abc123xyz")
        with self.assertRaisesRegex(ValidationError, "credential-shaped"):
            self._record(model="sk-supersecretvalue")
        after = self._event_count(usage.USAGE_EVENT)
        self.assertEqual(after, before)

    def test_digest_identity_is_stable_across_payload_round_trip(self) -> None:
        record = self._record()
        clone = usage.UsageRecord.from_payload(record.payload())
        self.assertEqual(clone.usage_id, record.usage_id)
        self.assertEqual(clone.record_digest, record.record_digest)
        self.assertEqual(clone.input_tokens, 1000)


class WorkerReportUsageM504Tests(unittest.TestCase):
    """The authentic WorkerReport -> usage-ledger hook (roadmap M5-04)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m504-wr-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc))
        self.project = domain.create_project(
            self.conn, "m504wr", str(self.root / "repo"), clock=self.clock)
        self.task = domain.create_task(
            self.conn, self.project.id, "hooked", clock=self.clock)
        self.task = domain.apply_task_event(
            self.conn, self.task.id, "dependencies_satisfied",
            expected_version=self.task.version, clock=self.clock)
        self.attempt = domain.create_attempt(
            self.conn, self.task.id, expected_version=self.task.version,
            clock=self.clock)
        domain.transition_attempt(self.conn, self.attempt.id, "start",
                                 clock=self.clock)
        domain.transition_attempt(self.conn, self.attempt.id, "activated",
                                 clock=self.clock)
        self.brief = briefs.create_attempt_brief(
            self.conn, self.attempt.id,
            content={"milestones": [{"id": "m1"}, {"id": "final"}]},
            clock=self.clock)

    def _report(self, *, kind="final", outcome="completed", milestone="final",
                usage_obj=None):
        value = {"schema_version": 1, "task_id": self.task.id,
                 "attempt_id": self.attempt.id,
                 "task_version": self.brief.task_version,
                 "brief_digest": self.brief.brief_digest,
                 "report_kind": kind,
                 "outcome": outcome if kind == "final" else None,
                 "milestone_id": milestone, "summary": "slice done",
                 "artifact_digest": None, "usage": usage_obj}
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        value["report_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
        return value

    def _ledger(self, role="worker"):
        return usage.list_usage(self.conn, self.project.id, role=role)

    def _bind_capabilities(self) -> None:
        harness = providers.CapabilitySnapshot(
            "harness", "opencode", "2.0.29", None, (),
            {"skill_rules": "supported"}, {"prompt_ack": "unknown"},
            "healthy", "ok", "local_process",
            self.clock.now().isoformat(),
            (self.clock.now() + timedelta(minutes=5)).isoformat())
        provider = providers.probe_omlx(
            getter=lambda url, timeout: (200, b'{"data":[{"id":"model-a"}]}'),
            clock=self.clock)
        providers.record_capability_probe(self.conn, self.project.id, harness)
        providers.record_capability_probe(self.conn, self.project.id, provider)
        providers.bind_attempt_capabilities(
            self.conn, self.attempt.id, harness=harness, provider=provider,
            model="model-a", clock=self.clock)

    def test_final_report_with_usage_projects_one_exact_local_row(self) -> None:
        self._bind_capabilities()
        worker_reports.record_worker_report(
            self.conn,
            self._report(usage_obj={"input_tokens": 1200, "output_tokens": 300,
                                   "total_tokens": 1500, "cost_microunits": None}),
            clock=self.clock)
        rows = self._ledger()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.precision, row.source, row.task_id,
                         row.attempt_id),
                         ("exact", "worker_report", self.task.id,
                          self.attempt.id))
        # harness/model/locality come from the durable capability binding;
        # locality is only ever loopback-local, never an assumed cloud.
        self.assertEqual((row.harness, row.provider, row.model),
                         ("opencode", "omlx", "model-a"))
        self.assertEqual(row.locality, "local")
        self.assertEqual((row.input_tokens, row.output_tokens,
                         row.total_tokens), (1200, 300, 1500))
        # dimensions this layer does not measure stay NULL, not zero
        self.assertIsNone(row.cache_tokens)
        self.assertIsNone(row.response_count)
        self.assertIsNone(row.elapsed_ms)
        self.assertIsNone(row.cost_microunits)

    def test_progress_reports_never_double_count_and_replays_add_nothing(self) -> None:
        report = self._report(kind="progress", outcome=None, milestone="m1",
                             usage_obj={"input_tokens": 500, "output_tokens": None,
                                      "total_tokens": None, "cost_microunits": None})
        worker_reports.record_worker_report(self.conn, report, clock=self.clock)
        self.assertEqual(len(self._ledger()), 0,
                         "progress usage claims may be cumulative — not an "
                         "accounting unit")
        final = self._report(
            usage_obj={"input_tokens": 900, "output_tokens": 200,
                      "total_tokens": 1100, "cost_microunits": None})
        first = worker_reports.record_worker_report(self.conn, final,
                                                    clock=self.clock)
        self.assertEqual(len(self._ledger()), 1)
        replay = worker_reports.record_worker_report(self.conn, final,
                                                     clock=self.clock)
        self.assertEqual(replay, first)
        self.assertEqual(len(self._ledger()), 1,
                         "exact replay must not duplicate the ledger row")
        self.assertEqual(self._ledger()[0].input_tokens, 900)

    def test_failed_and_blocked_finals_are_counted_missing_as_unavailable(self) -> None:
        # This Attempt fails with NO usage object: the row must still exist,
        # with every quantity NULL and precision unavailable (missing is
        # visible, never zero).
        worker_reports.record_worker_report(
            self.conn, self._report(outcome="failed", usage_obj=None),
            clock=self.clock)
        rows = self._ledger()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.precision, "unavailable")
        for name in usage.USAGE_DIMENSIONS:
            self.assertIsNone(getattr(row, name))
        totals = usage.usage_totals(self.conn, self.project.id)
        self.assertEqual(totals.unavailable_count, 1)
        self.assertIsNone(totals.dimension("input_tokens").known_sum)

    def test_contradictory_usage_falls_back_to_unavailable_keeping_cost(self) -> None:
        # Wire-valid (M2-03) but ledger-contradictory: total < parts.  The
        # report must still commit; its tokens degrade to unavailable while
        # the separately claimed cost survives as unattributed evidence.
        worker_reports.record_worker_report(
            self.conn,
            self._report(usage_obj={"input_tokens": 10, "output_tokens": 5,
                                   "total_tokens": 7, "cost_microunits": 3}),
            clock=self.clock)
        row = self._ledger()[0]
        self.assertEqual(row.precision, "unavailable")
        self.assertIsNone(row.input_tokens)
        self.assertIsNone(row.total_tokens)
        self.assertEqual(row.cost_microunits, 3)
        self.assertTrue(row.cost_unattributed)

    def test_scheduler_seam_consumes_the_ledger_null_aware(self) -> None:
        # No report yet -> the cloud gate sees all-NULL (unknown), never 0.
        empty = scheduler._durable_token_usage(self.conn, self.project.id,
                                              self.task.id)
        self.assertFalse(empty.is_known)
        worker_reports.record_worker_report(
            self.conn,
            self._report(usage_obj={"input_tokens": None, "output_tokens": None,
                                   "total_tokens": 5000, "cost_microunits": None}),
            clock=self.clock)
        used = scheduler._durable_token_usage(self.conn, self.project.id,
                                             self.task.id)
        self.assertEqual(used.total_tokens, 5000)
        self.assertIsNone(used.input_tokens)  # not reported -> stays unknown
        # Total alone never proves a budget dimension (escalation.TokenUsage:
        # is_known covers the directional/cost dimensions) — carrying it is
        # audit evidence, not headroom.
        self.assertFalse(used.is_known)
        # Budget layer: unknown directional dimensions keep the gate
        # conservative even though total tokens are known.
        evaluation = escalation.evaluate_budget(
            escalation.BudgetLimits(max_input_tokens=10_000,
                                   max_cost_microunits=10_000), used)
        self.assertEqual(evaluation.aggregate, "unknown")


class VerifierUsageM504Tests(unittest.TestCase):
    """Verifier evidence rows for both verification entry points."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m504-vf-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc))
        self.project = domain.create_project(
            self.conn, "m504vf", str(self.root), clock=self.clock)

    def lease(self, holder="tester"):
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project.id), holder,
            ttl_seconds=60, clock=self.clock)

    def verifying_scout(self):
        task = domain.create_task(self.conn, self.project.id, "scout",
                                 kind="scout", clock=self.clock)
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version, clock=self.clock)
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated",
                                 clock=self.clock)
        brief = briefs.create_attempt_brief(self.conn, attempt.id,
                                           content="verify me", clock=self.clock)
        report = {"schema_version": 1, "task_id": task.id, "attempt_id": attempt.id,
                  "task_version": brief.task_version, "brief_digest": brief.brief_digest,
                  "report_kind": "final", "outcome": "completed",
                  "milestone_id": "final", "summary": "done",
                  "artifact_digest": None, "usage": None}
        worker_reports.record_worker_report(self.conn, report, clock=self.clock)
        task = domain.get_task(self.conn, task.id)
        task = domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock)
        return task, attempt

    def _configure(self):
        verification.configure_verifier(
            self.conn, self.project.id, argv=["/usr/bin/true"],
            rules_version="rules-1", timeout_seconds=10, actor="human",
            idempotency_key="cfg-1", lease=self.lease(), clock=self.clock)

    def _verifier_rows(self):
        return usage.list_usage(self.conn, self.project.id, role="verifier")

    def test_pass_and_fail_both_record_measured_elapsed_tokens_null(self) -> None:
        self._configure()
        task, attempt = self.verifying_scout()
        with mock.patch.object(verification, "_run_bounded",
                              return_value=(0, b"ok", b"", False, False, None)):
            result = verification.run_verification(
                self.conn, attempt.id, idempotency_key="run-1",
                lease=self.lease(), clock=self.clock)
        self.assertEqual(result.status, "pass")
        rows = self._verifier_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.source, "verification_run")
        self.assertEqual((row.task_id, row.attempt_id), (task.id, attempt.id))
        self.assertIsInstance(row.elapsed_ms, int)
        self.assertGreaterEqual(row.elapsed_ms, 0)
        self.assertEqual(row.precision, "unavailable")
        for name in ("input_tokens", "output_tokens", "cache_tokens",
                    "cost_microunits"):
            self.assertIsNone(getattr(row, name))

        # A failing verifier spent real time too — it must be counted.
        task2, attempt2 = self.verifying_scout()
        with mock.patch.object(verification, "_run_bounded",
                              return_value=(1, b"no", b"", False, False, None)):
            result2 = verification.run_verification(
                self.conn, attempt2.id, idempotency_key="run-2",
                lease=self.lease(), clock=self.clock)
        self.assertEqual(result2.status, "fail")
        self.assertEqual(len(self._verifier_rows()), 2)

    def test_replayed_verification_adds_no_ledger_row(self) -> None:
        self._configure()
        task, attempt = self.verifying_scout()
        with mock.patch.object(verification, "_run_bounded",
                              return_value=(0, b"ok", b"", False, False, None)):
            first = verification.run_verification(
                self.conn, attempt.id, idempotency_key="run-idem",
                lease=self.lease(), clock=self.clock)
            replay = verification.run_verification(
                self.conn, attempt.id, idempotency_key="run-idem",
                lease=self.lease(), clock=self.clock)
        self.assertEqual(first.id, replay.id)
        self.assertEqual(len(self._verifier_rows()), 1)


class CeoWakeUsageM504Tests(unittest.TestCase):
    """CEO usage projection traced to the durable wake ledger (M2-09/M5-03)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m504-ceo-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))
        self.project = domain.create_project(
            self.conn, "m504ceo", str(self.root / "repo"), clock=self.clock)

    def _wake(self, wake_id: str, *, state: str) -> None:
        source = events.append_event(self.conn, "test.signal",
                                    project_id=self.project.id,
                                    payload_json="{}", clock=self.clock)
        now = self.clock.now().isoformat()
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO wakes (id, project_id, target_ceo_name, dedupe_key,"
                " window_start, priority, state, first_seq, last_seq, event_count,"
                " event_digest, created_at, updated_at)"
                " VALUES (?,?,'ceo',?,?,50,'pending',?,?,1,?,?,?)",
                (wake_id, self.project.id, f"dk-{wake_id}", now,
                 source.seq, source.seq, "0" * 64, now, now))
            if state == "blocked":
                self.conn.execute(
                    "UPDATE wakes SET state='blocked', updated_at=? WHERE id=?",
                    (now, wake_id))
            elif state != "pending":
                self.conn.execute(
                    "UPDATE wakes SET state='sending', updated_at=? WHERE id=?",
                    (now, wake_id))
                if state == "submitted":
                    self.conn.execute(
                        "UPDATE wakes SET state='submitted', updated_at=?,"
                        " submitted_at=? WHERE id=?", (now, now, wake_id))

    def test_wake_count_traces_to_ledger_tokens_stay_unavailable(self) -> None:
        self.assertEqual(
            usage.ceo_wake_usage(self.conn, self.project.id).wake_count, 0)
        self._wake("wk-1", state="submitted")
        self._wake("wk-2", state="pending")   # never delivered: no count
        self._wake("wk-3", state="blocked")   # reconciled NOT delivered: none
        view = usage.ceo_wake_usage(self.conn, self.project.id)
        self.assertEqual(view.role, "ceo")
        self.assertEqual(view.source, "wake_ledger")
        self.assertEqual(view.wake_count, 1)
        self.assertEqual(view.precision, "unavailable")
        for name in ("input_tokens", "output_tokens", "cache_tokens",
                    "cost_microunits", "elapsed_ms", "response_count"):
            self.assertIsNone(getattr(view, name))
        # a reproducible view: identical pinned window -> identical digest
        pinned_a = usage.ceo_wake_usage(
            self.conn, self.project.id,
            since="2026-11-21T11:00:00+00:00",
            until="2026-11-21T13:00:00+00:00",
            clock=self.clock)
        pinned_b = usage.ceo_wake_usage(
            self.conn, self.project.id,
            since="2026-11-21T11:00:00+00:00",
            until="2026-11-21T13:00:00+00:00",
            clock=self.clock)
        self.assertEqual(pinned_a.record_digest, pinned_b.record_digest)
        self.assertEqual(pinned_a.wake_count, 1)
        # it is a read model: nothing appended
        self.assertEqual(int(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?",
            (usage.USAGE_EVENT,)).fetchone()[0]), 0)
        with self.assertRaises(NotFoundError):
            usage.ceo_wake_usage(self.conn, "project-ghost")


if __name__ == "__main__":
    unittest.main()
