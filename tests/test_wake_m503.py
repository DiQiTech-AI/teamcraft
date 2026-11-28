"""M5-03 wake budget acceptance tests (durable enforcement + urgency rules).

Covers the budget slice layered on the M2-09 durable Wake pipeline:
persisted project policy (``wake.budget_config`` events), conservative
window accounting from durable rows, zero-write denials with preserved
dedupe/cursor/CAS semantics, and urgent bypass only within budget under an
explicit opt-in.  No Herdr is contacted; all state lives in temp SQLite.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from teamcraft import ceo_initialization as init
from teamcraft import domain, escalation, events, leader, runtime_adapter, storage, wake
from teamcraft.errors import ValidationError

TEAMCTL_SCRIPT = (Path(__file__).resolve().parents[1]
                  / ".agents" / "skills" / "teamcraft" / "scripts"
                  / "teamctl.py")


def _load_teamctl():
    spec = importlib.util.spec_from_file_location("teamctl_wake_m503",
                                                 TEAMCTL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TEAMCTL = _load_teamctl()


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class WakeBudgetM503Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1] / ".agents" / "skills"
        shutil.copytree(source / "teamcraft-ceo",
                       self.root / ".agents/skills/teamcraft-ceo")
        skill = self.root / ".agents/skills/teamcraft"
        skill.mkdir(parents=True)
        shutil.copy2(source / "teamcraft/SKILL.md", skill / "SKILL.md")
        self.conn, _ = runtime_adapter.open_runtime(self.root)
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        self.state = {
            "version": runtime_adapter.STATE_VERSION,
            "team_id": "team-m503",
            "project_root": str(self.root),
            "herdr_session": {"server_id": "srv-m503",
                             "socket_path": "/tmp/herdr-m503.sock"},
            "created_at": 1,
            "members": {"ceo": {
                "name": "ceo", "role": "ceo", "responsibility": "lead",
                "kind": "codex", "task": "initialize project",
                "ownership": [], "read_only": True, "cwd": str(self.root),
                "from_pane_id": "w1:p1", "pane_id": "w1:p1",
                "terminal_id": "terminal-m503",
                "agent_session": {"kind": "native", "value": "native-m503"},
                "agent_args": [], "opencode_config_content": None,
                "direction": None, "start_timeout_ms": 30_000,
                "phase": "prompt_submitted",
                "prompt_may_have_been_delivered": True,
                "live_state": "working", "error": None, "task_id": None,
                "attempt_id": None, "session_id": None,
                "created_at": 1, "updated_at": 1,
            }},
        }
        runtime_adapter.sync_state(self.conn, self.state, clock=self.clock)
        self.project_id = runtime_adapter.project_id_for_root(
            self.conn, str(self.root))
        self.lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "m503-test",
            ttl_seconds=1_000_000_000, clock=self.clock)
        _, nonce = init.discover_and_issue(
            self.conn, self.project_id, "ceo", nonce_ttl_seconds=300,
            clock=self.clock)
        init.mark_bootstrap_unknown(self.conn, self.project_id, nonce,
                                   lease=self.lease, clock=self.clock)
        self.initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce,
            native_session_id="native-m503", session_state="working",
            lease=self.lease, clock=self.clock)
        self.tsid = self.initialized.teamcraft_session_id

    # -- helpers ----------------------------------------------------------

    def set_ceo(self, state: str) -> None:
        row = self.conn.execute(
            "SELECT state FROM sessions WHERE id=?", (self.tsid,)).fetchone()
        if str(row[0]) != state:
            domain.observe_session(self.conn, self.tsid, state,
                                  clock=self.clock)

    def new_wake(self, key: str, priority: int | None = None):
        payload: dict[str, int | str] = {"dedupe_key": key}
        if priority is not None:
            payload["priority"] = priority
        events.append_event(self.conn, "task.updated",
                           project_id=self.project_id,
                           payload_json=json.dumps(payload),
                           clock=self.clock)
        return wake.ingest_events(self.conn, self.project_id, self.lease,
                                 clock=self.clock).wakes[-1]

    def record_budget(self, max_wakes: int, window: int = 3600, *,
                     allow_urgent: bool = False, urgent_priority: int = 100):
        return wake.record_wake_budget(
            self.conn, self.project_id, self.lease,
            max_wakes_per_window=max_wakes, window_seconds=window,
            allow_urgent_interrupt=allow_urgent,
            urgent_priority=urgent_priority,
            actor="owner", reason="M5-03 budget window for CEO wakes",
            clock=self.clock)

    def sending_payload(self, wake_id: str) -> dict:
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind='wake.sending' "
            "AND payload_json LIKE ?", (f"%{wake_id}%",)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(str(row[0]))

    def command_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM commands").fetchone()[0])

    def wake_state(self, wake_id: str) -> str:
        row = self.conn.execute(
            "SELECT state FROM wakes WHERE id=?", (wake_id,)).fetchone()
        return str(row[0])

    def wake_row(self, wake_id: str):
        return self.conn.execute(
            "SELECT state, detail, updated_at FROM wakes WHERE id=?",
            (wake_id,)).fetchone()

    def escalation_payloads(self, wake_id: str) -> list[dict]:
        payloads = [json.loads(str(row[0])) for row in self.conn.execute(
            "SELECT payload_json FROM events"
            " WHERE kind='wake.urgent_escalated'")]
        return [payload for payload in payloads
                if isinstance(payload, dict)
                and payload.get("wake_id") == wake_id]

    def full_lifecycle(self, admitted) -> None:
        item = (admitted.wake
                if isinstance(admitted, wake.AdmissionResult) else admitted)
        wake.record_submission_result(
            self.conn, self.project_id, item.id, self.lease,
            outcome="submitted", receipt_json='{"accepted":true}',
            clock=self.clock)
        wake.acknowledge_wake(
            self.conn, self.project_id, item.id, self.lease,
            target_session_id=self.initialized.native_session_id,
            acknowledged_through_seq=item.last_seq, clock=self.clock)
        wake.mark_processed(self.conn, self.project_id, item.id, self.lease,
                           clock=self.clock)

    def advance(self, seconds: int = 3600) -> None:
        self.clock.value += timedelta(seconds=seconds)

    # -- default behaviour preserved ---------------------------------------

    def test_default_admission_is_byte_for_byte_legacy(self) -> None:
        self.set_ceo("idle")
        item = self.new_wake("task:legacy")
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, item.id, self.lease,
            clock=self.clock)
        self.assertEqual(admitted.wake.state, "sending")
        self.assertIsNone(admitted.decision_digest)
        payload = self.sending_payload(item.id)
        self.assertNotIn("wake_budget", payload)
        self.assertNotIn("urgent_bypass", payload)
        command = self.conn.execute(
            "SELECT payload_json FROM commands WHERE id=?",
            (admitted.command.id,)).fetchone()
        self.assertNotIn("wake_budget", json.loads(str(command[0])))

    def test_working_ceo_defers_identically_without_budget(self) -> None:
        item = self.new_wake("task:hold")
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, item.id, self.lease,
            clock=self.clock)
        self.assertIsNone(admitted.command)
        self.assertEqual(admitted.deferred_reason, "ceo_working")
        self.assertIsNone(admitted.decision_digest)
        self.assertEqual(self.wake_state(item.id), "pending")

    # -- persisted config enforces for legacy callers ---------------------

    def test_persisted_budget_enforces_legacy_kwarg_free_caller(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1)
        first = self.new_wake("task:p1")
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, first.id, self.lease,
            clock=self.clock)  # legacy call site: no budget kwargs
        self.assertEqual(admitted.wake.state, "sending")
        self.assertEqual(admitted.decision_digest,
                         self.sending_payload(first.id)["wake_budget"]
                         ["decision_digest"])
        self.full_lifecycle(first)
        second = self.new_wake("task:p2")
        before = self.command_count()
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, second.id, self.lease,
            clock=self.clock)  # still no budget kwargs -> persisted enforced
        self.assertIsNone(deferred.command)
        self.assertEqual(deferred.deferred_reason, "wake_budget_exhausted:1/1")
        self.assertEqual(self.wake_state(second.id), "pending")
        self.assertEqual(self.command_count(), before)

    def test_explicit_parameter_overrides_persisted_budget(self) -> None:
        self.set_ceo("idle")
        # Persisted config is tight AND opt-in, so an explicit-budget call
        # must not inherit the persisted urgent opt-in (conservative default).
        self.record_budget(1, allow_urgent=True)
        first = self.new_wake("task:o1")
        self.full_lifecycle(
            wake.admit_pending_wake(self.conn, self.project_id, first.id,
                                   self.lease, clock=self.clock))
        second = self.new_wake("task:o2")
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, second.id, self.lease,
            clock=self.clock, budget=wake.WakeBudgetConfig(2, 3600))
        self.assertEqual(admitted.wake.state, "sending")
        audit = self.sending_payload(second.id)["wake_budget"]
        self.assertEqual(audit["source"], "parameter")
        self.assertEqual(audit["max_wakes_per_window"], 2)
        self.full_lifecycle(admitted)
        self.set_ceo("working")
        third = self.new_wake("task:o3", priority=100)
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, third.id, self.lease,
            clock=self.clock, budget=wake.WakeBudgetConfig(5, 3600))
        self.assertIsNone(deferred.command)
        self.assertEqual(deferred.deferred_reason, "ceo_working")

    def test_latest_persisted_config_wins_and_zero_freezes(self) -> None:
        self.set_ceo("idle")
        self.record_budget(5)
        self.record_budget(0)
        current = wake.get_wake_budget(self.conn, self.project_id)
        self.assertIsNotNone(current)
        self.assertEqual(current.budget.max_wakes_per_window, 0)
        item = self.new_wake("task:zero")
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, item.id, self.lease,
            clock=self.clock)
        self.assertIsNone(deferred.command)
        self.assertEqual(deferred.deferred_reason, "wake_budget_exhausted:0/0")
        # Lifting the freeze is itself an audited, lease-fenced event.
        self.record_budget(2)
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, item.id, self.lease,
            clock=self.clock)
        self.assertEqual(admitted.wake.state, "sending")
        self.assertEqual(admitted.decision_digest,
                         self.sending_payload(item.id)["wake_budget"]
                         ["decision_digest"])

    def test_get_wake_budget_none_and_invalid_fails_closed(self) -> None:
        self.assertIsNone(wake.get_wake_budget(self.conn, self.project_id))
        with storage.transaction(self.conn):
            events.append_event(
                self.conn, wake.WAKE_BUDGET_CONFIG_EVENT,
                project_id=self.project_id,
                payload_json=json.dumps({"max_wakes_per_window": "lots"}),
                clock=self.clock)
        with self.assertRaisesRegex(ValidationError, "invalid"):
            wake.get_wake_budget(self.conn, self.project_id)
        self.set_ceo("idle")
        item = self.new_wake("task:corrupt")
        # Admission refuses rather than silently dispatching unenforced.
        with self.assertRaisesRegex(ValidationError, "invalid"):
            wake.admit_pending_wake(self.conn, self.project_id, item.id,
                                   self.lease, clock=self.clock)
        self.assertEqual(self.wake_state(item.id), "pending")
        self.assertEqual(self.command_count(), 0)

    def test_budget_config_recording_requires_current_lease(self) -> None:
        # Take a real previous-epoch lease via a genuine takeover cycle
        # instead of constructing lease internals.
        self.assertTrue(leader.release(
            self.conn, leader.project_scope(self.project_id), "m503-test",
            clock=self.clock))
        first = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "holder-a",
            ttl_seconds=1, clock=self.clock)
        self.clock.value += timedelta(seconds=2)
        self.lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "holder-b",
            ttl_seconds=120, clock=self.clock)
        with self.assertRaises(leader.StaleLeaderError):
            wake.record_wake_budget(
                self.conn, self.project_id, first,
                max_wakes_per_window=1, actor="owner", reason="stale attempt",
                clock=self.clock)
        self.assertIsNone(wake.get_wake_budget(self.conn, self.project_id))

    def test_record_wake_budget_validation(self) -> None:
        for bad in ({"max_wakes_per_window": -1},
                   {"max_wakes_per_window": 1, "window_seconds": 0},
                   {"max_wakes_per_window": 1, "window_seconds": 86401},
                   {"max_wakes_per_window": 1, "urgent_priority": 101}):
            with self.assertRaises(ValidationError):
                wake.record_wake_budget(
                    self.conn, self.project_id, self.lease,
                    allow_urgent_interrupt=True, actor="owner",
                    reason="validation probe", clock=self.clock, **bad)
        for bad_actor in ("", "   ", "x" * 65, "Bearer sk-abcdefgh12345"):
            with self.assertRaises(ValidationError):
                wake.record_wake_budget(
                    self.conn, self.project_id, self.lease,
                    max_wakes_per_window=1, actor=bad_actor,
                    reason="validation probe", clock=self.clock)
        self.assertIsNone(wake.get_wake_budget(self.conn, self.project_id))

    def test_config_event_never_becomes_wake_candidate(self) -> None:
        self.record_budget(2)
        result = wake.ingest_events(self.conn, self.project_id, self.lease,
                                   clock=self.clock)
        self.assertEqual(result.created_wakes, 0)
        self.assertEqual(result.merged_events, 0)

    # -- urgency rules -----------------------------------------------------

    def test_urgent_bypass_requires_persisted_opt_in_and_budget(self) -> None:
        # Persisted default: interrupt opt-in off -> legacy hold.
        self.record_budget(1)
        urgent = self.new_wake("task:u1", priority=100)
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertIsNone(deferred.command)
        self.assertEqual(deferred.deferred_reason, "ceo_working")
        # Opt in durably; now the bypass runs and is flagged in the audit.
        self.record_budget(1, allow_urgent=True)
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertEqual(admitted.wake.state, "sending")
        payload = self.sending_payload(urgent.id)
        self.assertTrue(payload["urgent_bypass"])
        self.assertEqual(payload["wake_budget"]["source"], "persisted")
        self.full_lifecycle(admitted)
        # Window now exhausted: an urgent item escalates instead of interrupt.
        urgent2 = self.new_wake("task:u2", priority=100)
        before = self.command_count()
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, urgent2.id, self.lease,
            clock=self.clock)
        self.assertIsNone(deferred.command)
        self.assertEqual(deferred.deferred_reason,
                         "urgent_wake_budget_exhausted:1/1")
        self.assertEqual(self.wake_state(urgent2.id), "pending")
        self.assertEqual(self.command_count(), before)

    def test_urgent_priority_floor_is_persisted(self) -> None:
        self.record_budget(1, allow_urgent=True, urgent_priority=90)
        current = wake.get_wake_budget(self.conn, self.project_id)
        self.assertEqual(current.policy.urgent_priority, 90)
        item = self.new_wake("task:u90", priority=90)
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, item.id, self.lease,
            clock=self.clock)
        self.assertEqual(admitted.wake.state, "sending")  # 90 now urgent

    def test_exhausted_urgent_surfaces_even_while_idle(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1)
        first = self.new_wake("task:i1")
        self.full_lifecycle(
            wake.admit_pending_wake(self.conn, self.project_id, first.id,
                                   self.lease, clock=self.clock))
        urgent = self.new_wake("task:i2", priority=100)
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertIsNone(deferred.command)
        self.assertEqual(deferred.deferred_reason,
                         "urgent_wake_budget_exhausted:1/1")
        # Not silently dropped: a later window admits the same wake once,
        # preserving the M2-09 single-send CAS.
        self.advance()
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertEqual(admitted.wake.state, "sending")
        with self.assertRaisesRegex(Exception, "only pending Wakes"):
            wake.admit_pending_wake(self.conn, self.project_id, urgent.id,
                                   self.lease, clock=self.clock)

    # -- needs-human escalation of an over-budget urgent wake --------------

    def test_urgent_escalation_blocks_wake_with_auditable_reason(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1)
        first = self.new_wake("task:b1")
        self.full_lifecycle(
            wake.admit_pending_wake(self.conn, self.project_id, first.id,
                                   self.lease, clock=self.clock))
        urgent = self.new_wake("task:b2", priority=100)
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertEqual(deferred.deferred_reason,
                         "urgent_wake_budget_exhausted:1/1")
        self.assertEqual(self.wake_state(urgent.id), "pending")
        # The refusal alone is not the escalation: the dispatch path records
        # it, and that record is what turns the Wake into human work.
        event, inserted = wake.record_urgent_wake_escalation(
            self.conn, self.project_id, urgent.id, self.lease,
            detail=deferred.deferred_reason, clock=self.clock)
        self.assertTrue(inserted)
        row = self.wake_row(urgent.id)
        self.assertEqual(row[0], "blocked")
        self.assertEqual(row[1], "urgent_wake_budget_exhausted:1/1")
        payload = json.loads(event.payload_json)
        self.assertEqual(payload["escalation"], "needs_human")
        self.assertEqual(payload["wake_state"], "blocked")
        self.assertEqual(payload["state_before"], "pending")
        self.assertEqual(payload["wake_detail"],
                         "urgent_wake_budget_exhausted:1/1")
        # Notification consumer: the blocked Wake (and its reason) is listed.
        blocked = wake.list_wakes(self.conn, self.project_id,
                                  states=("blocked",))
        self.assertEqual([item.id for item in blocked], [urgent.id])
        self.assertEqual(blocked[0].detail,
                         "urgent_wake_budget_exhausted:1/1")
        # Replay is a strict no-op for the row and the event stream.
        again, inserted_again = wake.record_urgent_wake_escalation(
            self.conn, self.project_id, urgent.id, self.lease,
            detail=deferred.deferred_reason, clock=self.clock)
        self.assertFalse(inserted_again)
        self.assertEqual(again.event_id, event.event_id)
        self.assertEqual(self.wake_row(urgent.id), row)
        self.assertEqual(len(self.escalation_payloads(urgent.id)), 1)
        # ...including after the budget window has rolled: the Wake is
        # permanently out of the dispatchable state, so no second record.
        self.advance()
        still, inserted_later = wake.record_urgent_wake_escalation(
            self.conn, self.project_id, urgent.id, self.lease,
            detail=deferred.deferred_reason, clock=self.clock)
        self.assertFalse(inserted_later)
        self.assertEqual(still.event_id, event.event_id)
        self.assertEqual(self.wake_row(urgent.id), row)
        self.assertEqual(len(self.escalation_payloads(urgent.id)), 1)
        # Blocked is terminal for admission: a retry tick cannot deliver it.
        with self.assertRaisesRegex(ValidationError, "only pending Wakes"):
            wake.admit_pending_wake(self.conn, self.project_id, urgent.id,
                                   self.lease, clock=self.clock)
        self.assertEqual(self.command_count(), 1)

    def test_escalation_derives_reason_when_caller_omits_detail(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1)
        first = self.new_wake("task:d1")
        self.full_lifecycle(
            wake.admit_pending_wake(self.conn, self.project_id, first.id,
                                   self.lease, clock=self.clock))
        urgent = self.new_wake("task:d2", priority=100)
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertEqual(deferred.deferred_reason,
                         "urgent_wake_budget_exhausted:1/1")
        _event, inserted = wake.record_urgent_wake_escalation(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertTrue(inserted)
        self.assertEqual(self.wake_row(urgent.id)[:2],
                         ("blocked", "urgent_wake_budget_exhausted:1/1"))

    def test_zero_budget_urgent_escalation_also_blocks(self) -> None:
        self.set_ceo("idle")
        self.record_budget(0)
        urgent = self.new_wake("task:z0", priority=100)
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertEqual(deferred.deferred_reason,
                         "urgent_wake_budget_exhausted:0/0")
        _, inserted = wake.record_urgent_wake_escalation(
            self.conn, self.project_id, urgent.id, self.lease,
            detail=deferred.deferred_reason, clock=self.clock)
        self.assertTrue(inserted)
        self.assertEqual(self.wake_row(urgent.id)[:2],
                         ("blocked", "urgent_wake_budget_exhausted:0/0"))
        # Escalating a never-attempted Wake consumes no window allowance and
        # creates no Command: the fail-closed budget stays intact.
        self.assertEqual(wake.wake_budget_usage(
            self.conn, self.project_id, window_seconds=3600,
            clock=self.clock), 0)
        self.assertEqual(self.command_count(), 0)

    def test_non_urgent_exhaustion_stays_pending_and_unrecorded(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1)
        first = self.new_wake("task:n1")
        self.full_lifecycle(
            wake.admit_pending_wake(self.conn, self.project_id, first.id,
                                   self.lease, clock=self.clock))
        ordinary = self.new_wake("task:n2")
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, ordinary.id, self.lease,
            clock=self.clock)
        self.assertEqual(deferred.deferred_reason, "wake_budget_exhausted:1/1")
        self.assertEqual(self.wake_state(ordinary.id), "pending")
        self.assertEqual(self.escalation_payloads(ordinary.id), [])
        self.assertEqual(wake.list_wakes(self.conn, self.project_id,
                                         states=("blocked",)), ())

    def test_escalation_is_lease_fenced_and_writes_nothing_when_stale(self) -> None:
        self.set_ceo("idle")
        self.record_budget(0)
        urgent = self.new_wake("task:fence", priority=100)
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, urgent.id, self.lease,
            clock=self.clock)
        self.assertEqual(deferred.deferred_reason,
                         "urgent_wake_budget_exhausted:0/0")
        stale = self.lease
        self.assertTrue(leader.release(
            self.conn, leader.project_scope(self.project_id), "m503-test",
            clock=self.clock))
        leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "holder-a",
            ttl_seconds=1, clock=self.clock)
        self.clock.value += timedelta(seconds=2)
        self.lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "holder-b",
            ttl_seconds=120, clock=self.clock)
        with self.assertRaises(leader.StaleLeaderError):
            wake.record_urgent_wake_escalation(
                self.conn, self.project_id, urgent.id, stale,
                detail=deferred.deferred_reason, clock=self.clock)
        self.assertEqual(self.wake_state(urgent.id), "pending")
        self.assertEqual(self.escalation_payloads(urgent.id), [])

    def test_reconciliation_blocked_wake_is_not_reused_for_escalation(
            self) -> None:
        self.set_ceo("idle")
        item = self.new_wake("task:rec-blocked")
        wake.admit_pending_wake(self.conn, self.project_id, item.id,
                               self.lease, clock=self.clock)
        wake.record_submission_result(
            self.conn, self.project_id, item.id, self.lease,
            outcome="unknown", detail="Herdr response lost", clock=self.clock)
        settled = wake.reconcile_wake_delivery(
            self.conn, self.project_id, item.id, self.lease,
            delivered=False, actor="operator",
            note="Herdr history shows no prompt",
            evidence_json='{"observed_prompts":[]}', clock=self.clock)
        self.assertEqual(settled.state, "blocked")
        self.assertEqual(settled.detail,
                         "reconciled as not submitted; automatic resend prohibited")
        with self.assertRaisesRegex(ValidationError, "blocked"):
            wake.record_urgent_wake_escalation(
                self.conn, self.project_id, item.id, self.lease,
                detail="urgent_wake_budget_exhausted:0/0", clock=self.clock)
        # The reconciliation reason is preserved verbatim; no escalation.
        self.assertEqual(self.wake_row(item.id)[0:2],
                         (settled.state, settled.detail))
        self.assertEqual(self.escalation_payloads(item.id), [])

    # -- accounting semantics -----------------------------------------------

    def test_unknown_counts_blocked_never_and_window_roll(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1)
        first = self.new_wake("task:a1")
        wake.admit_pending_wake(self.conn, self.project_id, first.id,
                               self.lease, clock=self.clock)
        self.assertEqual(wake.wake_budget_usage(
            self.conn, self.project_id, window_seconds=3600,
            clock=self.clock), 1)  # sending counts as reserved
        wake.record_submission_result(
            self.conn, self.project_id, first.id, self.lease,
            outcome="unknown", detail="Herdr response lost",
            clock=self.clock)
        self.assertEqual(wake.wake_budget_usage(
            self.conn, self.project_id, window_seconds=3600,
            clock=self.clock), 1)  # UNKNOWN may have been delivered
        settled = wake.reconcile_wake_delivery(
            self.conn, self.project_id, first.id, self.lease,
            delivered=False, actor="operator",
            note="Herdr history shows no prompt",
            evidence_json='{"observed_prompts":[]}', clock=self.clock)
        self.assertEqual(settled.state, "blocked")
        self.assertEqual(wake.wake_budget_usage(
            self.conn, self.project_id, window_seconds=3600,
            clock=self.clock), 0)  # proven-not-delivered consumed nothing
        # ...so a new wake may still use the intact allowance.
        second = self.new_wake("task:a2")
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, second.id, self.lease,
            clock=self.clock)
        self.assertEqual(admitted.wake.state, "sending")

    def test_window_roll_restores_allowance(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1, 3600)
        first = self.new_wake("task:w1")
        self.full_lifecycle(
            wake.admit_pending_wake(self.conn, self.project_id, first.id,
                                   self.lease, clock=self.clock))
        second = self.new_wake("task:w2")
        deferred = wake.admit_pending_wake(
            self.conn, self.project_id, second.id, self.lease,
            clock=self.clock)
        self.assertIsNone(deferred.command)
        self.advance()
        self.assertEqual(wake.wake_budget_usage(
            self.conn, self.project_id, window_seconds=3600,
            clock=self.clock), 0)
        admitted = wake.admit_pending_wake(
            self.conn, self.project_id, second.id, self.lease,
            clock=self.clock)
        self.assertEqual(admitted.wake.state, "sending")
        self.assertEqual(
            self.sending_payload(second.id)["wake_budget"]
            ["consumed_in_window"], 0)

    # -- dedupe/cursor preservation under denial ---------------------------

    def test_denials_do_not_move_cursors_or_duplicates(self) -> None:
        self.set_ceo("idle")
        self.record_budget(1)
        first = self.new_wake("task:c1")
        self.full_lifecycle(
            wake.admit_pending_wake(self.conn, self.project_id, first.id,
                                   self.lease, clock=self.clock))
        second = self.new_wake("task:c2")
        cursor_before = wake.get_cursor(self.conn, self.project_id)
        for _ in range(3):
            deferred = wake.admit_pending_wake(
                self.conn, self.project_id, second.id, self.lease,
                clock=self.clock)
            self.assertIsNone(deferred.command)
        cursor_after = wake.get_cursor(self.conn, self.project_id)
        self.assertEqual((cursor_before.scanned_seq,
                          cursor_before.acknowledged_seq,
                          cursor_before.processed_seq),
                         (cursor_after.scanned_seq,
                          cursor_after.acknowledged_seq,
                          cursor_after.processed_seq))
        # Denials wrote no events, no commands; the wake is still one row.
        kinds = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind LIKE 'wake.%' "
            "AND payload_json LIKE ?", (f"%{second.id}%",)).fetchone()[0]
        self.assertEqual(int(kinds), 0)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM wakes WHERE dedupe_key=?",
            ("task:c2",)).fetchone()[0]
        self.assertEqual(int(rows), 1)
        # Dedupe merging still works while deferred: a same-key event merges
        # into the SAME pending wake rather than forking a second row.
        self.new_wake("task:c2")
        rows = self.conn.execute(
            "SELECT COUNT(*), MAX(event_count) FROM wakes WHERE dedupe_key=?",
            ("task:c2",)).fetchone()
        self.assertEqual(int(rows[0]), 1)
        self.assertGreaterEqual(int(rows[1]), 2)


class FakeHerdr:
    """Transport double for the CLI's only external seam (``_herdr_adapter``)."""

    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.prompts: list[dict] = []

    def _occupant(self) -> dict:
        return {"pane_id": "w1:p1", "terminal_id": "terminal-m503",
                "name": "ceo", "agent": "codex", "agent_status": self.state,
                "agent_session": {"kind": "native", "value": "native-m503"}}

    def current_pane(self, pane_id: str) -> dict:
        return self._occupant()

    def get_agent(self, name: str) -> dict:
        return self._occupant()

    def prompt_agent(self, name, prompt, *, wait=False, timeout_ms=None) -> dict:
        self.prompts.append({"name": name, "wait": wait,
                            "timeout_ms": timeout_ms,
                            "prompt_len": len(prompt)})
        self.state = "working"
        return {"accepted": True}


class WakeBudgetCliM503Tests(unittest.TestCase):
    """Real ``teamctl`` callers: budget commands + urgent dispatch wiring."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repo"
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1] / ".agents" / "skills"
        shutil.copytree(source / "teamcraft-ceo",
                       self.root / ".agents/skills/teamcraft-ceo")
        skill = self.root / ".agents/skills/teamcraft"
        skill.mkdir(parents=True)
        shutil.copy2(source / "teamcraft/SKILL.md", skill / "SKILL.md")
        env = {"HERDR_ENV": "1",
               "HERDR_SOCKET_PATH": str(self.root / "herdr.sock")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn, _ = runtime_adapter.open_runtime(self.root)
        self.addCleanup(self.conn.close)
        state = {
            "version": runtime_adapter.STATE_VERSION,
            "team_id": "team-cli-m503",
            "project_root": str(self.root),
            "herdr_session": {"server_id": "srv-cli",
                             "socket_path": str(self.root / "herdr.sock")},
            "created_at": 1,
            "members": {"ceo": {
                "name": "ceo", "role": "ceo", "responsibility": "lead",
                "kind": "codex", "task": "lead the project",
                "ownership": [], "read_only": True, "cwd": str(self.root),
                "from_pane_id": "w1:p1", "pane_id": "w1:p1",
                "terminal_id": "terminal-m503",
                "agent_session": {"kind": "native", "value": "native-m503"},
                "agent_args": [], "opencode_config_content": None,
                "direction": None, "start_timeout_ms": 30_000,
                "phase": "prompt_submitted",
                "prompt_may_have_been_delivered": True,
                "live_state": "working", "error": None, "task_id": None,
                "attempt_id": None, "session_id": None,
                "created_at": 1, "updated_at": 1,
            }},
        }
        runtime_adapter.sync_state(self.conn, state)
        self.project_id = runtime_adapter.project_id_for_root(
            self.conn, str(self.root))
        lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "cli-setup",
            ttl_seconds=1_000_000_000)
        _, nonce = init.discover_and_issue(
            self.conn, self.project_id, "ceo", nonce_ttl_seconds=300)
        init.mark_bootstrap_unknown(self.conn, self.project_id, nonce,
                                   lease=lease)
        initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce,
            native_session_id="native-m503", session_state="working",
            lease=lease)
        self.tsid = initialized.teamcraft_session_id
        with storage.transaction(self.conn):
            self.conn.execute(
                "UPDATE members SET session_id=? WHERE name='ceo'",
                (self.tsid,))
        self.assertTrue(leader.release(
            self.conn, leader.project_scope(self.project_id), "cli-setup"))
        self.herdr = FakeHerdr()
        adapter_patcher = mock.patch.object(TEAMCTL, "_herdr_adapter",
                                           return_value=self.herdr)
        adapter_patcher.start()
        self.addCleanup(adapter_patcher.stop)
        self.parser = TEAMCTL.build_parser()

    # -- helpers ----------------------------------------------------------

    def invoke(self, argv: list[str]) -> dict:
        args = self.parser.parse_args(argv)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = args.handler(args)
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def set_ceo(self, state: str) -> None:
        row = self.conn.execute(
            "SELECT state FROM sessions WHERE id=?", (self.tsid,)).fetchone()
        if str(row[0]) != state:
            domain.observe_session(self.conn, self.tsid, state)

    def queue_wake(self, key: str, priority: int = 50) -> None:
        events.append_event(self.conn, "task.updated",
                           project_id=self.project_id,
                           payload_json=json.dumps({"dedupe_key": key,
                                                  "priority": priority}))

    def escalation_rows(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='wake.urgent_escalated'"
        ).fetchone()[0])

    def wake_state(self, dedupe_key: str) -> str:
        row = self.conn.execute(
            "SELECT state FROM wakes WHERE dedupe_key=?",
            (dedupe_key,)).fetchone()
        return str(row[0])

    # -- parser + budget commands -------------------------------------------

    def test_budget_commands_are_registered_with_safe_defaults(self) -> None:
        set_args = self.parser.parse_args([
            "wake-budget-set", "--cwd", str(self.root),
            "--max-wakes-per-window", "3",
            "--actor", "owner", "--reason", "cap CEO wakes hourly"])
        self.assertIs(set_args.handler, TEAMCTL.wake_budget_set)
        self.assertEqual(set_args.window_seconds, 3600)
        self.assertFalse(set_args.allow_urgent_interrupt)
        self.assertEqual(set_args.urgent_priority, 100)
        get_args = self.parser.parse_args(["wake-budget-get", "--cwd",
                                          str(self.root)])
        self.assertIs(get_args.handler, TEAMCTL.wake_budget_get)
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["wake-budget-set", "--cwd", str(self.root),
                                   "--max-wakes-per-window", "3"])

    def test_budget_set_then_get_roundtrip_is_durable(self) -> None:
        out = self.invoke([
            "wake-budget-set", "--cwd", str(self.root),
            "--max-wakes-per-window", "2", "--window-seconds", "600",
            "--allow-urgent-interrupt",
            "--actor", "owner", "--reason", "cap CEO wakes per 10 minutes"])
        self.assertEqual(out["max_wakes_per_window"], 2)
        self.assertEqual(out["window_seconds"], 600)
        self.assertTrue(out["allow_urgent_interrupt"])
        self.assertEqual(out["consumed_in_window"], 0)
        self.assertEqual(out["source"], "persisted")
        view = self.invoke(["wake-budget-get", "--cwd", str(self.root)])
        self.assertTrue(view["configured"])
        self.assertEqual(view["budget"]["max_wakes_per_window"], 2)
        self.assertEqual(view["remaining_in_window"], 2)
        # The configuration is durable core state, not CLI-local output: a
        # fresh core read sees exactly the same policy.
        current = wake.get_wake_budget(self.conn, self.project_id)
        self.assertEqual(current.budget.max_wakes_per_window, 2)
        self.assertTrue(current.policy.allow_urgent_interrupt)
        # Configuring never touches the transport or creates work.
        self.assertEqual(self.herdr.prompts, [])

    def test_unset_budget_reports_legacy_mode(self) -> None:
        view = self.invoke(["wake-budget-get", "--cwd", str(self.root)])
        self.assertFalse(view["configured"])
        self.assertIsNone(view["budget"])
        self.assertIsNone(view["consumed_in_window"])

    # -- urgent wiring through the real dispatch path -----------------------

    def test_urgent_bypass_delivers_to_working_ceo(self) -> None:
        self.invoke(["wake-budget-set", "--cwd", str(self.root),
                    "--max-wakes-per-window", "3", "--allow-urgent-interrupt",
                    "--actor", "owner", "--reason", "urgent interrupts on"])
        self.queue_wake("task:cli-urgent", priority=100)
        # Both the durable session and the live Herdr occupant are working:
        # only a Core-authorized urgent bypass may deliver here.
        out = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(out["status"], "submitted")
        self.assertTrue(out["urgent_bypass"])
        self.assertEqual(len(self.herdr.prompts), 1)
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind='wake.sending'"
            " ORDER BY seq DESC LIMIT 1").fetchone()
        payload = json.loads(str(row[0]))
        self.assertTrue(payload["urgent_bypass"])
        self.assertEqual(payload["wake_budget"]["source"], "persisted")

    def test_over_budget_urgent_records_human_escalation_once(self) -> None:
        self.invoke(["wake-budget-set", "--cwd", str(self.root),
                    "--max-wakes-per-window", "1", "--allow-urgent-interrupt",
                    "--actor", "owner", "--reason", "one wake per window"])
        self.set_ceo("idle")
        self.queue_wake("task:cli-first", priority=50)
        out = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(out["status"], "submitted")
        self.assertNotIn("urgent_bypass", out)  # legacy output shape preserved
        self.herdr.state = "working"
        self.set_ceo("working")
        self.queue_wake("task:cli-urgent", priority=100)
        deferred = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(deferred["reason"], "urgent_wake_budget_exhausted:1/1")
        self.assertEqual(deferred["urgent_escalation"], "recorded")
        # No prompt was attempted for the refused urgent Wake.
        self.assertEqual(len(self.herdr.prompts), 1)
        # The escalation is a needs-human transition, not a retryable defer:
        # the Wake is blocked with its auditable reason and can never be
        # silently delivered by a later dispatch tick.
        self.assertEqual(self.wake_state("task:cli-urgent"), "blocked")
        detail = self.conn.execute(
            "SELECT detail FROM wakes WHERE dedupe_key=?",
            ("task:cli-urgent",)).fetchone()[0]
        self.assertEqual(detail, "urgent_wake_budget_exhausted:1/1")
        self.assertEqual(self.escalation_rows(), 1)
        # A repeated dispatch attempt finds nothing pending: no duplicate
        # prompt, no duplicate escalation event, no state churn.
        again = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(again["status"], "idle")
        self.assertNotIn("urgent_escalation", again)
        self.assertEqual(self.escalation_rows(), 1)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.wake_state("task:cli-urgent"), "blocked")
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind='wake.urgent_escalated'"
            " ORDER BY seq DESC LIMIT 1").fetchone()
        payload = json.loads(str(row[0]))
        self.assertEqual(payload["escalation"], "needs_human")
        self.assertEqual(payload["wake_state"], "blocked")
        self.assertEqual(payload["wake_detail"],
                         "urgent_wake_budget_exhausted:1/1")
        self.assertEqual(payload["priority"], 100)
        self.assertEqual(payload["max_wakes_per_window"], 1)

    def test_wake_status_blocked_lists_escalated_wake(self) -> None:
        self.invoke(["wake-budget-set", "--cwd", str(self.root),
                    "--max-wakes-per-window", "0", "--allow-urgent-interrupt",
                    "--actor", "owner", "--reason", "freeze wakes"])
        self.queue_wake("task:cli-status", priority=100)
        out = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(out["status"], "deferred")
        self.assertEqual(out["reason"], "urgent_wake_budget_exhausted:0/0")
        self.assertEqual(out["urgent_escalation"], "recorded")
        blocked = self.invoke(["wake-status", "--cwd", str(self.root),
                               "--state", "blocked"])
        self.assertEqual(len(blocked["wakes"]), 1)
        item = blocked["wakes"][0]
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["dedupe_key"], "task:cli-status")
        self.assertEqual(item["detail"],
                         "urgent_wake_budget_exhausted:0/0")
        # The escalated Wake left the pending queue entirely.
        pending = self.invoke(["wake-status", "--cwd", str(self.root),
                               "--state", "pending"])
        self.assertEqual(pending["wakes"], [])
        self.assertEqual(self.herdr.prompts, [])

    def test_non_urgent_while_working_keeps_legacy_deferral(self) -> None:
        self.invoke(["wake-budget-set", "--cwd", str(self.root),
                    "--max-wakes-per-window", "3", "--allow-urgent-interrupt",
                    "--actor", "owner", "--reason", "urgent interrupts on"])
        self.queue_wake("task:cli-normal", priority=50)
        out = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(out, {"status": "deferred",
                              "wake_id": out["wake_id"],
                              "reason": "ceo_working"})
        self.assertNotIn("urgent_escalation", out)
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.escalation_rows(), 0)

    def test_legacy_dispatch_without_budget_is_unchanged(self) -> None:
        self.set_ceo("idle")
        self.queue_wake("task:cli-legacy", priority=50)
        out = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(out["status"], "submitted")
        self.assertEqual(sorted(out.keys()),
                         ["command_id", "status", "target_session_id",
                          "wake_id"])
        self.assertEqual(len(self.herdr.prompts), 1)
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind='wake.sending'"
            " ORDER BY seq DESC LIMIT 1").fetchone()
        payload = json.loads(str(row[0]))
        self.assertNotIn("wake_budget", payload)
        self.assertNotIn("urgent_bypass", payload)


    def test_urgent_escalation_event_never_becomes_a_wake(self) -> None:
        self.invoke(["wake-budget-set", "--cwd", str(self.root),
                    "--max-wakes-per-window", "0", "--allow-urgent-interrupt",
                    "--actor", "owner", "--reason", "freeze wakes"])
        self.queue_wake("task:cli-frozen", priority=100)
        out = self.invoke(["wake-dispatch", "--cwd", str(self.root)])
        self.assertEqual(out["reason"], "urgent_wake_budget_exhausted:0/0")
        self.assertEqual(out["urgent_escalation"], "recorded")
        self.assertEqual(self.herdr.prompts, [])
        check = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "cli-check",
            ttl_seconds=60)
        result = wake.ingest_events(self.conn, self.project_id, check)
        self.assertEqual(result.created_wakes, 0)
        self.assertEqual(result.merged_events, 0)
        self.assertTrue(leader.release(
            self.conn, leader.project_scope(self.project_id), "cli-check"))

    def test_cli_reason_constant_matches_core(self) -> None:
        self.assertEqual(TEAMCTL.URGENT_WAKE_BUDGET_EXHAUSTED_REASON,
                         wake.URGENT_WAKE_BUDGET_EXHAUSTED_REASON)

    def test_m503_guard_names_missing_core_surface(self) -> None:
        with mock.patch.object(TEAMCTL._wake, "record_wake_budget", None), \
             mock.patch.object(TEAMCTL._wake, "record_urgent_wake_escalation",
                              None):
            with self.assertRaises(TEAMCTL.TeamctlError) as caught:
                TEAMCTL._require_m503_core()
        message = str(caught.exception)
        self.assertIn("record_wake_budget", message)
        self.assertIn("record_urgent_wake_escalation", message)
        # And the guard passes for the resolved (current) core.
        TEAMCTL._require_m503_core()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
