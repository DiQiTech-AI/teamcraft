"""M5-03 integration: bounded retry gate, cloud escalation, wake budgets.

These tests exercise the scheduler-side integration around the existing
production entry points (``plan`` / ``dispatch_ready``) plus the durable
cloud-approval and wake-budget surfaces built on the pure
``teamcraft.escalation`` decision layer.  No provider is (or can be) invoked:
this runtime has no cloud transport, and every affirmative decision must
reserve/send nothing by itself.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import (domain, escalation, errors, events, leader, recovery,
                      router, scheduler, storage, wake)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = NOW

    def now(self):
        return self.value


def selection(harness="codex", provider="omlx", model="qwen/local"):
    return router.RouteSelection(harness, provider, model)


class EscalationM503Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = Path(self.temp.name) / "db.sqlite"
        storage.migrate_to_head(db)
        self.conn = storage.connect(db)
        self.clock = Clock()
        self.project = domain.create_project(self.conn, "p", "/repo/p",
                                            clock=self.clock)
        self.lease = scheduler.acquire_or_renew(self.conn, self.project.id,
                                                "sched", ttl_seconds=60,
                                                clock=self.clock)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def task(self, name, *, ownership=("src/esc/**",), max_attempts=3):
        task = domain.create_task(
            self.conn, self.project.id, name,
            ownership_json=json.dumps(list(ownership)),
            max_attempts=max_attempts, clock=self.clock,
        )
        if task.status == "pending":
            task = domain.apply_task_event(
                self.conn, task.id, "dependencies_satisfied",
                expected_version=task.version, clock=self.clock,
            )
        return domain.get_task(self.conn, task.id)

    def dispatch(self, limit=2):
        with mock.patch.object(scheduler.ceo_initialization,
                              "require_initialized"):
            return scheduler.dispatch_ready(
                self.conn, self.project.id, self.lease,
                concurrency_limit=limit, clock=self.clock,
            )

    def fail_attempt(self, attempt):
        domain.transition_attempt(self.conn, attempt.id, "start",
                                 clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated",
                                 clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "fail",
                                 clock=self.clock)
        return domain.get_attempt(self.conn, attempt.id)

    def reasons(self, plan_or_result):
        return {item.task_id: item.reason for item in plan_or_result.deferred}

    def raw_attempt(self, task_id, *, status, attempt_no=1):
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO attempts(id,task_id,attempt_no,status)"
                " VALUES (?,?,?,?)",
                (f"raw-{task_id}-{attempt_no}", task_id, attempt_no, status),
            )

    # ------------------------------------------------------------------
    # Bounded retry gate
    # ------------------------------------------------------------------

    def test_first_dispatch_is_unaffected_by_the_retry_gate(self):
        task = self.task("fresh")
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertEqual(plan.selected_task_ids, (task.id,))
        before = scheduler.retry_gate(self.conn, task.id)
        self.assertEqual(before.action, "none")
        self.assertFalse(before.new_attempt_permitted)
        self.assertIsNone(before.classification)
        result = self.dispatch()
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].task_id, task.id)
        # Once reserved, the Attempt occupies the slot: the gate now says
        # wait, so a second dispatch tick can never stack attempts.
        after = scheduler.retry_gate(self.conn, task.id)
        self.assertEqual(after.action, "wait")
        self.assertEqual(after.reason_code, "attempt_in_flight")
        self.assertFalse(after.new_attempt_permitted)
        plan_again = scheduler.plan(self.conn, self.project.id,
                                   concurrency_limit=2)
        self.assertNotIn(task.id, plan_again.selected_task_ids)

    def test_retry_without_durable_approval_is_deferred_fail_closed(self):
        task = self.task("failed-unapproved")
        self.raw_attempt(task.id, status="failed")
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertEqual(self.reasons(plan)[task.id], "retry_approval_required")
        result = self.dispatch()
        self.assertEqual(result.attempts, ())
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")
        decision = scheduler.retry_gate(self.conn, task.id)
        self.assertEqual(decision.action, "await_approval")
        self.assertFalse(decision.new_attempt_permitted)
        self.assertEqual(decision.classification, "retryable_failure")

    def test_unknown_attempt_is_never_an_auto_retryable_failure(self):
        task = self.task("ambiguous")
        self.raw_attempt(task.id, status="unknown")
        # Structure first: the queue itself never exposes a task holding an
        # UNKNOWN Attempt (M1-04 occupying-slot invariant).
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertNotIn(task.id, plan.selected_task_ids)
        result = self.dispatch()
        self.assertEqual(result.attempts, ())
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")
        # The audit surface states the reason explicitly and never permits.
        decision = scheduler.retry_gate(self.conn, task.id)
        self.assertEqual(decision.action, "reconcile")
        self.assertEqual(decision.reason_code, "unknown_never_auto_retry")
        self.assertFalse(decision.new_attempt_permitted)
        self.assertEqual(decision.classification, "unknown")

    def test_finished_without_final_report_requires_human_disposition(self):
        task = self.task("unreported")
        self.raw_attempt(task.id, status="finished")
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertEqual(self.reasons(plan)[task.id], "final_report_missing")
        result = self.dispatch()
        self.assertEqual(result.attempts, ())

    def test_bounded_retry_after_durable_approval_reserves_second_attempt(
            self):
        task = self.task("approved-retry")
        first = self.dispatch().attempts[0]
        self.fail_attempt(first)
        recovery.approve_retry(
            self.conn, task.id, actor="cto", reason="bounded second try",
            idempotency_key="m503-retry-1", lease=self.lease,
            clock=self.clock,
        )
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")
        decision = scheduler.retry_gate(self.conn, task.id)
        self.assertEqual(decision.action, "retry")
        self.assertTrue(decision.new_attempt_permitted)
        self.assertEqual(decision.attempts_remaining,
                        task.max_attempts - 1)
        result = self.dispatch()
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].attempt_no, 2)

    def test_attempt_cap_still_wins_and_exhaustion_never_retries(self):
        task = self.task("capped", max_attempts=2)
        first = self.dispatch().attempts[0]
        self.fail_attempt(first)
        recovery.approve_retry(
            self.conn, task.id, actor="cto", reason="one bounded retry",
            idempotency_key="m503-cap-1", lease=self.lease, clock=self.clock,
        )
        second = self.dispatch().attempts[0]
        self.assertEqual(second.attempt_no, 2)
        self.fail_attempt(second)
        # RUNNING + used==max + unoccupied -> existing NEEDS_HUMAN escalation.
        self.dispatch()
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "needs_human")
        decision = scheduler.retry_gate(self.conn, task.id)
        self.assertEqual(decision.action, "needs_human")
        self.assertEqual(decision.reason_code, "attempt_budget_exhausted")
        self.assertFalse(decision.new_attempt_permitted)
        self.assertEqual(decision.attempts_remaining, 0)
        # A READY task over the cap is the pre-existing hard defer.
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?",
                             (task.id,))
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertEqual(self.reasons(plan)[task.id], "max_attempts_reached")

    # ------------------------------------------------------------------
    # Cloud escalation: durable approval + conservative budget
    # ------------------------------------------------------------------

    def evaluate(self, task_id, **kwargs):
        defaults = dict(selection=selection(), locality="cloud",
                        limits=escalation.BudgetLimits(),
                        used=escalation.TokenUsage())
        defaults.update(kwargs)
        return scheduler.evaluate_cloud_escalation(
            self.conn, self.project.id, task_id, clock=self.clock, **defaults)

    def decision_events(self, task_id):
        rows = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind=? AND task_id=?"
            " ORDER BY seq",
            (scheduler.CLOUD_DECISION_EVENT, task_id),
        ).fetchall()
        return [json.loads(str(r[0])) for r in rows]

    def test_cloud_denied_without_durable_approval(self):
        task = self.task("cloud-novideo")
        decision = self.evaluate(task.id, limits=escalation.BudgetLimits(
            max_input_tokens=1000, max_output_tokens=1000,
            max_cost_microunits=1_000_000))
        self.assertEqual(decision.action, "denied")
        self.assertEqual(decision.reason_code, "approval_required")
        self.assertFalse(decision.approval_applied)
        self.assertFalse(decision.usage_known)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,),
        ).fetchone()[0], 0)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")
        events_out = self.decision_events(task.id)
        self.assertEqual(len(events_out), 1)
        self.assertEqual(events_out[0]["reason_code"], "approval_required")
        self.assertEqual(events_out[0]["effect"], "evidence_only_no_dispatch")

    def test_unknown_usage_denied_never_counted_as_zero_or_free(self):
        task = self.task("cloud-nullusage")
        scheduler.approve_cloud_escalation(
            self.conn, self.project.id, task.id, selection=selection(),
            limits=escalation.BudgetLimits(max_input_tokens=1000),
            actor="cto", reason="authorize one bounded cloud retry",
            idempotency_key="m503-cloud-null", lease=self.lease,
            clock=self.clock,
        )
        decision = self.evaluate(
            task.id, limits=escalation.BudgetLimits(max_input_tokens=1000),
            used=escalation.TokenUsage(),
        )
        self.assertEqual(decision.action, "denied")
        self.assertEqual(decision.reason_code,
                         "budget_unknown_conservative_denied")
        self.assertFalse(decision.usage_known)
        self.assertTrue(decision.approval_applied)
        # total_tokens never substitutes for missing directional usage.
        partial = self.evaluate(
            task.id, limits=escalation.BudgetLimits(max_input_tokens=1000),
            used=escalation.TokenUsage(total_tokens=10),
        )
        self.assertEqual(partial.reason_code,
                         "budget_unknown_conservative_denied")
        # Proven exhaustion stays a distinct state from unknown.
        exceeded = self.evaluate(
            task.id, limits=escalation.BudgetLimits(max_input_tokens=1000),
            used=escalation.TokenUsage(input_tokens=5000, total_tokens=5000),
        )
        self.assertEqual(exceeded.reason_code, "budget_exceeded")
        self.assertTrue(exceeded.usage_known)

    def test_approval_must_bind_exact_route_and_caps(self):
        task = self.task("cloud-binding")
        limits = escalation.BudgetLimits(max_input_tokens=1000)
        scheduler.approve_cloud_escalation(
            self.conn, self.project.id, task.id, selection=selection(),
            limits=limits, actor="cto", reason="exact route only",
            idempotency_key="m503-cloud-bind", lease=self.lease,
            clock=self.clock,
        )
        known = escalation.TokenUsage(input_tokens=100, total_tokens=100)
        off_route = self.evaluate(task.id, selection=selection(model="other"),
                                 limits=limits, used=known)
        self.assertEqual(off_route.action, "denied")
        self.assertEqual(off_route.reason_code, "approval_target_mismatch")
        off_caps = self.evaluate(task.id, limits=escalation.BudgetLimits(
            max_input_tokens=9_999_999), used=known)
        self.assertEqual(off_caps.action, "denied")
        self.assertEqual(off_caps.reason_code, "approval_limits_mismatch")

    def test_locality_and_unresolved_unknown_refusals(self):
        task = self.task("cloud-refusals")
        limits = escalation.BudgetLimits(max_input_tokens=1000)
        known = escalation.TokenUsage(input_tokens=100, total_tokens=100)
        scheduler.approve_cloud_escalation(
            self.conn, self.project.id, task.id, selection=selection(),
            limits=limits, actor="cto", reason="route pinned",
            idempotency_key="m503-cloud-refuse", lease=self.lease,
            clock=self.clock,
        )
        local = self.evaluate(task.id, locality="local", limits=limits,
                             used=known)
        self.assertEqual(local.reason_code, "target_locality_not_cloud")
        self.assertFalse(local.approval_applied)
        self.raw_attempt(task.id, status="unknown")
        ambiguous = self.evaluate(task.id, limits=limits, used=known)
        self.assertEqual(ambiguous.action, "denied")
        self.assertEqual(ambiguous.reason_code, "unresolved_unknown_attempt")

    def test_authorization_is_evidence_only_audited_and_replay_stable(self):
        task = self.task("cloud-ok")
        limits = escalation.BudgetLimits(max_input_tokens=1000,
                                        max_output_tokens=1000,
                                        max_cost_microunits=1_000_000)
        used = escalation.TokenUsage(input_tokens=100, output_tokens=50,
                                    total_tokens=150, cost_microunits=500)
        command, replayed = scheduler.approve_cloud_escalation(
            self.conn, self.project.id, task.id, selection=selection(),
            limits=limits, actor="cto", reason="budgeted cloud escalation",
            idempotency_key="m503-cloud-ok", lease=self.lease,
            clock=self.clock,
        )
        self.assertFalse(replayed)
        self.assertEqual(command.state, "confirmed")
        again, replayed_again = scheduler.approve_cloud_escalation(
            self.conn, self.project.id, task.id, selection=selection(),
            limits=limits, actor="cto", reason="budgeted cloud escalation",
            idempotency_key="m503-cloud-ok", lease=self.lease,
            clock=self.clock,
        )
        self.assertTrue(replayed_again)
        self.assertEqual(again.id, command.id)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind=?",
            (scheduler.CLOUD_APPROVAL_EVENT,),
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM commands WHERE kind=?",
            (scheduler.CLOUD_APPROVAL_COMMAND_KIND,),
        ).fetchone()[0], 1)

        decision = self.evaluate(task.id, limits=limits, used=used)
        self.assertEqual(decision.action, "authorized")
        self.assertTrue(decision.approval_applied)
        self.assertTrue(decision.usage_known)
        # Authorizing evidence reserves nothing.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,),
        ).fetchone()[0], 0)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")

        replay = self.evaluate(task.id, limits=limits, used=used)
        self.assertEqual(replay.decision_digest, decision.decision_digest)
        # Identical digest: no duplicate audit event on repeated polling.
        self.assertEqual(len(self.decision_events(task.id)), 1)
        changed = self.evaluate(
            task.id, limits=limits,
            used=escalation.TokenUsage(input_tokens=9999, total_tokens=9999),
        )
        self.assertEqual(changed.reason_code, "budget_exceeded")
        events_out = self.decision_events(task.id)
        self.assertEqual(len(events_out), 2)
        self.assertEqual(events_out[1]["decision_digest"],
                         changed.decision_digest)

    def test_cloud_decision_digest_is_deterministic_and_inputs_validated(self):
        task = self.task("cloud-purity")
        decision = self.evaluate(task.id)
        # Identical facts replay to an identical digest; task binding lives
        # on the audit Event row, not inside the decision digest itself.
        twin = self.evaluate(self.task("cloud-purity2").id)
        self.assertEqual(decision.decision_digest, twin.decision_digest)
        different = self.evaluate(task.id, locality="local")
        self.assertNotEqual(decision.decision_digest, different.decision_digest)
        with self.assertRaises(errors.ValidationError):
            self.evaluate(task.id, locality="edge")
        with self.assertRaises(errors.ValidationError):
            self.evaluate(task.id, used=escalation.TokenUsage(
                input_tokens=100, total_tokens=50))
        with self.assertRaises(errors.ValidationError):
            self.evaluate(task.id, selection=selection(),
                         limits=escalation.BudgetLimits(),
                         used=escalation.TokenUsage(),
                         policy=escalation.EscalationPolicy(
                             urgent_priority=101))

    def test_approval_idempotency_key_cannot_be_recycled(self):
        task = self.task("cloud-recycle")
        limits = escalation.BudgetLimits(max_input_tokens=10)
        scheduler.approve_cloud_escalation(
            self.conn, self.project.id, task.id, selection=selection(),
            limits=limits, actor="cto", reason="first grant",
            idempotency_key="m503-cloud-recycle", lease=self.lease,
            clock=self.clock,
        )
        with self.assertRaises(errors.IdempotencyConflictError):
            scheduler.approve_cloud_escalation(
                self.conn, self.project.id, task.id,
                selection=selection(model="different/model"),
                limits=limits, actor="cto", reason="sneaky second grant",
                idempotency_key="m503-cloud-recycle", lease=self.lease,
                clock=self.clock,
            )

    # ------------------------------------------------------------------
    # CEO wake budgets (advisory, fail-closed)
    # ------------------------------------------------------------------

    def seed_wakes(self):
        events.append_event(self.conn, "recovery.escalated",
                           project_id=self.project.id, clock=self.clock)
        events.append_event(self.conn, "watch.agent_activity",
                           project_id=self.project.id, clock=self.clock)
        wake.ingest_events(self.conn, self.project.id, self.lease,
                          clock=self.clock)

    def as_ceo(self, session_id):
        return mock.patch.object(
            scheduler.ceo_initialization, "get_initialization",
            return_value=SimpleNamespace(teamcraft_session_id=session_id))

    def insert_session(self, session_id, state):
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO sessions (id, backend, native_session_id, state,"
                " last_observed_at, created_at) VALUES (?, 'herdr', 'native-1',"
                " ?, ?, ?)",
                (session_id, state, self.clock.now().isoformat(),
                 self.clock.now().isoformat()),
            )

    def wake_map(self, **kwargs):
        return {item.dedupe_key: decision
                for item, decision
                in scheduler.pending_wake_budget_decisions(
                    self.conn, self.project.id, **kwargs)}

    def test_unreachable_ceo_isolates_without_replacement(self):
        self.seed_wakes()
        decisions = self.wake_map(max_wakes_per_window=5)
        urgent = decisions["recovery.escalated:-:-"]
        ordinary = decisions["watch.agent_activity:-:-"]
        self.assertEqual(urgent.action, "needs_human")  # urgent -> a human
        self.assertEqual(urgent.reason_code, "ceo_state_missing")
        self.assertEqual(ordinary.action, "defer")
        self.assertEqual(ordinary.reason_code, "ceo_state_missing")
        self.assertFalse(urgent.consumes_budget)
        # Advisory only: nothing was admitted or sent.
        for item in wake.list_wakes(self.conn, self.project.id,
                                   states=("pending",)):
            self.assertEqual(item.state, "pending")

    def test_window_budget_delivers_in_priority_order(self):
        self.seed_wakes()
        self.insert_session("ses_ceo", "idle")
        with self.as_ceo("ses_ceo"):
            decisions = self.wake_map(max_wakes_per_window=2)
        urgent = decisions["recovery.escalated:-:-"]
        ordinary = decisions["watch.agent_activity:-:-"]
        self.assertEqual(urgent.action, "deliver")
        self.assertEqual(urgent.budget_remaining, 2)
        self.assertTrue(urgent.consumes_budget)
        self.assertEqual(ordinary.action, "deliver")
        self.assertEqual(ordinary.budget_remaining, 1)

    def test_exhausted_budget_defers_ordinary_and_escalates_urgent(self):
        self.seed_wakes()
        self.insert_session("ses_ceo", "idle")
        with self.as_ceo("ses_ceo"):
            zero = self.wake_map(max_wakes_per_window=0)
            self.assertEqual(zero["recovery.escalated:-:-"].action,
                             "needs_human")
            self.assertEqual(zero["recovery.escalated:-:-"].reason_code,
                             "urgent_wake_budget_exhausted")
            self.assertEqual(zero["watch.agent_activity:-:-"].action, "defer")
            self.assertEqual(zero["watch.agent_activity:-:-"].reason_code,
                             "wake_budget_exhausted")
            one = self.wake_map(max_wakes_per_window=1)
            self.assertEqual(one["recovery.escalated:-:-"].action, "deliver")
            self.assertEqual(one["watch.agent_activity:-:-"].action, "defer")

    def test_already_sent_wakes_count_against_the_window(self):
        self.seed_wakes()
        self.insert_session("ses_ceo", "idle")
        seq = int(self.conn.execute(
            "SELECT MIN(seq) FROM events").fetchone()[0])
        pending = wake.list_wakes(self.conn, self.project.id,
                                 states=("pending",))
        urgent = next(item for item in pending
                     if item.dedupe_key == "recovery.escalated:-:-")
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO wakes (id,project_id,target_ceo_name,dedupe_key,"
                "window_start,priority,state,first_seq,last_seq,event_count,"
                "event_digest,created_at,updated_at,submitted_at)"
                " VALUES (?,?, 'ceo','other.sent',? ,100,'submitted',?,?,1,?,"
                " ?,?,?)",
                ("wake-sent-1", self.project.id, urgent.window_start, seq, seq,
                 "0" * 64, urgent.created_at, urgent.created_at,
                 urgent.created_at),
            )
        with self.as_ceo("ses_ceo"):
            decisions = self.wake_map(max_wakes_per_window=1)
        # The window already spent its single allowance: urgent escalates to
        # a human instead of silently delaying; ordinary defers.
        self.assertEqual(decisions["recovery.escalated:-:-"].action,
                         "needs_human")
        self.assertEqual(decisions["recovery.escalated:-:-"].reason_code,
                         "urgent_wake_budget_exhausted")
        self.assertEqual(decisions["watch.agent_activity:-:-"].action, "defer")

    def test_working_ceo_defers_unless_urgent_interrupt_policy_allows(self):
        self.seed_wakes()
        self.insert_session("ses_ceo", "working")
        with self.as_ceo("ses_ceo"):
            default = self.wake_map(max_wakes_per_window=5)
            self.assertEqual(default["recovery.escalated:-:-"].action, "defer")
            self.assertEqual(default["recovery.escalated:-:-"].reason_code,
                             "ceo_working")
            interrupt = self.wake_map(
                max_wakes_per_window=5,
                policy=escalation.EscalationPolicy(
                    allow_urgent_interrupt=True))
            urgent = interrupt["recovery.escalated:-:-"]
            self.assertEqual(urgent.action, "urgent_bypass")
            self.assertTrue(urgent.consumes_budget)
            self.assertEqual(interrupt["watch.agent_activity:-:-"].action,
                             "defer")
            starved = self.wake_map(
                max_wakes_per_window=0,
                policy=escalation.EscalationPolicy(
                    allow_urgent_interrupt=True))
            self.assertEqual(starved["recovery.escalated:-:-"].action,
                             "needs_human")

    def test_wake_budget_input_validation_and_read_only(self):
        self.seed_wakes()
        before = self.conn.execute(
            "SELECT COUNT(*) FROM commands").fetchone()[0]
        with self.assertRaises(ValueError):
            scheduler.pending_wake_budget_decisions(
                self.conn, self.project.id, max_wakes_per_window=-1)
        with self.assertRaises(ValueError):
            scheduler.pending_wake_budget_decisions(
                self.conn, self.project.id, max_wakes_per_window=True)
        self.wake_map(max_wakes_per_window=1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM commands").fetchone()[0], before)

    def test_needs_human_decision_is_advisory_until_durably_escalated(self):
        self.seed_wakes()
        self.insert_session("ses_ceo", "idle")
        with self.as_ceo("ses_ceo"):
            decisions = {
                item.dedupe_key: (item, decision)
                for item, decision
                in scheduler.pending_wake_budget_decisions(
                    self.conn, self.project.id, max_wakes_per_window=0)}
        urgent, decision = decisions["recovery.escalated:-:-"]
        self.assertEqual(decision.action, "needs_human")
        self.assertEqual(decision.reason_code,
                         "urgent_wake_budget_exhausted")
        self.assertFalse(decision.consumes_budget)
        # Planning is advisory: it neither blocks nor records anything.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='wake.urgent_escalated'"
        ).fetchone()[0], 0)
        self.assertEqual(wake.list_wakes(self.conn, self.project.id,
                                        states=("blocked",)), ())
        pending = wake.list_wakes(self.conn, self.project.id,
                                  states=("pending",))
        self.assertEqual(len(pending), 2)
        self.assertEqual({item.state for item in pending}, {"pending"})
        # Only the durable escalation transition moves the Wake to blocked.
        event, inserted = wake.record_urgent_wake_escalation(
            self.conn, self.project.id, urgent.id, self.lease,
            detail=f"{decision.reason_code}:0/0", clock=self.clock)
        self.assertTrue(inserted)
        payload = json.loads(event.payload_json)
        self.assertEqual(payload["escalation"], "needs_human")
        self.assertEqual(payload["wake_state"], "blocked")
        blocked = wake.list_wakes(self.conn, self.project.id,
                                  states=("blocked",))
        self.assertEqual([item.id for item in blocked], [urgent.id])
        self.assertEqual(blocked[0].detail,
                         "urgent_wake_budget_exhausted:0/0")
        replay, inserted_again = wake.record_urgent_wake_escalation(
            self.conn, self.project.id, urgent.id, self.lease,
            detail=f"{decision.reason_code}:0/0", clock=self.clock)
        self.assertFalse(inserted_again)
        self.assertEqual(replay.event_id, event.event_id)
        self.assertEqual(len(wake.list_wakes(self.conn, self.project.id,
                                            states=("blocked",))), 1)


class CloudBudgetReservationTests(unittest.TestCase):
    """Durable cloud budget + enforcement at the real reservation boundary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = Path(self.temp.name) / "db.sqlite"
        storage.migrate_to_head(db)
        self.conn = storage.connect(db)
        self.clock = Clock()
        self.project = domain.create_project(self.conn, "p", "/repo/p",
                                            clock=self.clock)
        self.lease = scheduler.acquire_or_renew(self.conn, self.project.id,
                                                "sched", ttl_seconds=60,
                                                clock=self.clock)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def task(self, name, *, ownership=("src/cloud/**",), max_attempts=3):
        task = domain.create_task(
            self.conn, self.project.id, name,
            ownership_json=json.dumps(list(ownership)),
            max_attempts=max_attempts, clock=self.clock,
        )
        if task.status == "pending":
            task = domain.apply_task_event(
                self.conn, task.id, "dependencies_satisfied",
                expected_version=task.version, clock=self.clock,
            )
        return domain.get_task(self.conn, task.id)

    def dispatch(self, limit=2):
        with mock.patch.object(scheduler.ceo_initialization,
                              "require_initialized"):
            return scheduler.dispatch_ready(
                self.conn, self.project.id, self.lease,
                concurrency_limit=limit, clock=self.clock,
            )

    def fail_attempt(self, attempt):
        domain.transition_attempt(self.conn, attempt.id, "start",
                                 clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated",
                                 clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "fail",
                                 clock=self.clock)

    def raw_attempt(self, task_id, *, status, attempt_no=1):
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO attempts(id,task_id,attempt_no,status)"
                " VALUES (?,?,?,?)",
                (f"raw-{task_id}-{attempt_no}", task_id, attempt_no, status),
            )

    def reasons(self, plan_or_result):
        return {item.task_id: item.reason for item in plan_or_result.deferred}

    def config_limits(self, cap=1000):
        return escalation.BudgetLimits(max_input_tokens=cap,
                                      max_output_tokens=cap,
                                      max_cost_microunits=cap * 1000)

    def configure(self, *, limits=None, enabled=True, require_approval=True):
        return scheduler.configure_cloud_budget(
            self.conn, self.project.id, self.lease,
            limits=limits if limits is not None else self.config_limits(),
            actor="cto", reason="cloud pilot budget",
            require_approval=require_approval, enabled=enabled,
            clock=self.clock,
        )

    def cloud_intent(self, task, *, limits=None, key=None):
        return scheduler.approve_cloud_escalation(
            self.conn, self.project.id, task.id, selection=selection(),
            limits=limits if limits is not None else self.config_limits(),
            actor="cto", reason="approve cloud escalation once",
            idempotency_key=key or f"cloud-intent-{task.id}",
            lease=self.lease, clock=self.clock,
        )

    def failed_ready_task(self, *, cloud_before_local, name="cloud-retry"):
        """Failed Attempt -> local disposition -> optional cloud intent, in a
        strict clock order so supersession is unambiguous."""
        task = self.task(name)
        first = self.dispatch().attempts[0]
        self.fail_attempt(first)
        self.clock.value += timedelta(seconds=1)
        if cloud_before_local:
            self.cloud_intent(task)
            self.clock.value += timedelta(seconds=1)
            recovery.approve_retry(
                self.conn, task.id, actor="cto", reason="local retry",
                idempotency_key=f"local-{task.id}", lease=self.lease,
                clock=self.clock)
        else:
            recovery.approve_retry(
                self.conn, task.id, actor="cto", reason="local retry",
                idempotency_key=f"local-{task.id}", lease=self.lease,
                clock=self.clock)
            self.clock.value += timedelta(seconds=1)
            self.cloud_intent(task)
        return task

    def test_latest_cloud_budget_config_wins_and_is_audited(self):
        self.assertIsNone(scheduler.get_cloud_budget(self.conn,
                                                    self.project.id))
        proposed = self.configure()
        current = scheduler.get_cloud_budget(self.conn, self.project.id)
        self.assertEqual(current.limits.payload(), proposed.limits.payload())
        self.assertTrue(current.enabled)
        self.assertTrue(current.require_approval)
        self.configure(enabled=False)
        lifted = scheduler.get_cloud_budget(self.conn, self.project.id)
        self.assertFalse(lifted.enabled)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind=?",
            (scheduler.CLOUD_BUDGET_CONFIG_EVENT,),
        ).fetchone()[0], 2)
        before = self.conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]
        with self.assertRaises(errors.ValidationError):
            self.configure(limits=escalation.BudgetLimits(
                max_input_tokens=-5))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0], before)

    def test_config_writer_requires_the_current_lease(self):
        self.clock.value += timedelta(seconds=61)
        scheduler.acquire_or_renew(self.conn, self.project.id, "other-holder",
                                  ttl_seconds=30, clock=self.clock)
        with self.assertRaises(leader.StaleLeaderError):
            self.configure()

    def test_cloud_intent_retry_blocked_fail_closed_unknown_usage(self):
        self.configure()
        task = self.failed_ready_task(cloud_before_local=False)
        decision = scheduler.cloud_dispatch_decision(self.conn,
                                                    self.project.id, task.id)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "denied")
        self.assertEqual(decision.reason_code,
                         "budget_unknown_conservative_denied")
        self.assertFalse(decision.usage_known)
        self.assertTrue(decision.approval_applied)
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertNotIn(task.id, plan.selected_task_ids)
        self.assertEqual(self.reasons(plan)[task.id],
                         "budget_unknown_conservative_denied")
        result = self.dispatch()
        self.assertEqual(result.attempts, ())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,),
        ).fetchone()[0], 1)  # the failed original; no retry was reserved
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")

    def test_newer_local_retry_disposition_supersedes_cloud_intent(self):
        self.configure()
        task = self.failed_ready_task(cloud_before_local=True)
        self.assertIsNone(scheduler.cloud_dispatch_decision(self.conn,
                                                           self.project.id,
                                                           task.id))
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertIn(task.id, plan.selected_task_ids)
        result = self.dispatch()
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].task_id, task.id)
        self.assertEqual(result.attempts[0].attempt_no, 2)

    def test_within_budget_cloud_is_still_refused_without_transport(self):
        self.configure()
        task = self.failed_ready_task(cloud_before_local=False)
        known = escalation.TokenUsage(input_tokens=100, output_tokens=50,
                                     total_tokens=150, cost_microunits=500)
        with mock.patch.object(scheduler, "_durable_token_usage",
                              return_value=known):
            decision = scheduler.cloud_dispatch_decision(self.conn,
                                                        self.project.id,
                                                        task.id)
            self.assertEqual(decision.reason_code,
                             "cloud_transport_unavailable")
            self.assertTrue(decision.usage_known)
            self.assertTrue(decision.approval_applied)
            self.assertEqual(self.dispatch().attempts, ())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,),
        ).fetchone()[0], 1)

    def test_shrunken_project_caps_void_a_prior_approval(self):
        self.configure()  # caps 1000
        task = self.failed_ready_task(cloud_before_local=False)
        self.clock.value += timedelta(seconds=1)
        wide = self.cloud_intent(task, limits=self.config_limits(cap=5000),
                                key=f"wide-{task.id}")
        self.assertEqual(wide[1], False)
        known = escalation.TokenUsage(input_tokens=100, output_tokens=50,
                                     total_tokens=150, cost_microunits=500)
        with mock.patch.object(scheduler, "_durable_token_usage",
                              return_value=known):
            decision = scheduler.cloud_dispatch_decision(self.conn,
                                                        self.project.id,
                                                        task.id)
        self.assertEqual(decision.reason_code, "approval_limits_mismatch")
        self.assertEqual(self.dispatch().attempts, ())

    def test_unresolved_unknown_attempt_denied_before_any_budget_reasoning(
            self):
        self.configure()
        task = self.task("cloud-unknown")
        self.raw_attempt(task.id, status="unknown")
        self.cloud_intent(task)
        decision = scheduler.cloud_dispatch_decision(self.conn,
                                                    self.project.id, task.id)
        self.assertEqual(decision.reason_code, "unresolved_unknown_attempt")
        plan = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertNotIn(task.id, plan.selected_task_ids)
        self.assertEqual(self.dispatch().attempts, ())

    def test_explicit_budget_lift_releases_the_gate(self):
        self.configure()
        task = self.failed_ready_task(cloud_before_local=False)
        self.assertEqual(self.dispatch().attempts, ())
        self.clock.value += timedelta(seconds=1)
        self.configure(enabled=False)
        result = self.dispatch()
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].attempt_no, 2)

    def test_corrupt_persisted_budget_halts_dispatch_fail_closed(self):
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO events (event_id,kind,project_id,payload_json,"
                " occurred_at) VALUES (?,?,?,?,?)",
                ("corrupt-cloud-budget",
                 scheduler.CLOUD_BUDGET_CONFIG_EVENT, self.project.id,
                 '{"bogus":1}', self.clock.now().isoformat()),
            )
        with self.assertRaises(errors.ValidationError):
            scheduler.get_cloud_budget(self.conn, self.project.id)
        self.task("halting")  # a ready task exists on disk...
        with self.assertRaises(errors.ValidationError):
            scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        with self.assertRaises(errors.ValidationError):
            self.dispatch()

    def test_budget_config_event_cannot_wake_the_ceo(self):
        self.configure()
        wake.ingest_events(self.conn, self.project.id, self.lease,
                          clock=self.clock)
        self.assertEqual(
            wake.list_wakes(self.conn, self.project.id,
                           states=("pending",)), ())


if __name__ == "__main__":
    unittest.main()
