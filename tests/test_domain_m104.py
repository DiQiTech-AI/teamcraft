"""M1-04 step 1 domain contract tests (state machines, DAG, READY, CAS).

All databases live inside ``tempfile.TemporaryDirectory``.  Nothing here reads,
writes, or migrates the repository's real runtime directories (``.teamcraft``
included), and no Herdr session is contacted.

Pinned contracts:

* Task/Attempt/Session transition tables reject every illegal edge with a
  structured :class:`IllegalTransitionError` (entity/current/event/reason);
* a dependency edge that would close a cycle is refused with the concrete
  closed path (self-dependency included) and the graph stays unchanged;
* READY is derived ONLY from hard dependencies reaching ``done``, READY tasks
  drain back to PENDING when a dependency regresses, and the dispatch queue
  is ordered priority DESC, created_at ASC, id ASC;
* UNKNOWN attempts occupy the task's single slot: a violating dispatch maps
  the DB partial unique index to ``AttemptSlotOccupiedError`` and rolls back
  the Task CAS completely;
* optimistic versioning: stale ``expected_version`` -> VersionConflictError
  with expected/found; every accepted write — including every REAL dependency
  edge add/remove (CEO review M1-04 #1/#3) — bumps version exactly once,
  dependency CRUD itself demands the caller's ``expected_version`` first,
  while duplicate adds / missing-edge removals do not; two independent
  connections racing to dispatch leave exactly one attempt;
* READY->RUNNING only happens atomically with an attempt reservation:
  ``dispatch_reserved`` through generic ``apply_task_event`` raises reason
  ``RESERVATION_REQUIRED`` (CEO review M1-04 fix 2);
* dependency-satisfaction facts (``dependencies_satisfied`` and
  ``dispatch_reserved`` inside :func:`create_attempt`) are re-verified
  from ``task_dependencies`` inside the transaction — unfinished upstreams
  refuse READY claims and dispatch alike (CEO review M1-04 #2);
* Session observations (starting/idle/working/exited/missing) NEVER move a
  Task or Attempt row, and there is no session-signal Task event;
* domain services demand head: an audited-but-lagging v1 database raises
  ``SchemaOutOfDateError`` while raw storage writes still work there;
* domain writes are atomic across an outer ``storage.transaction`` (shared
  rollback), proving the APIs join rather than nest transactions;
* dependencies are same-Project only: cross-Project edges raise
  ``ValidationError`` before any write (same-Project adds remain legal and
  bump the owning Task exactly once) (CEO review M1-04 P1-A);
* execution completion gates terminal Task movement: ``worker_reported_done``
  and any transition into DONE/FAILED/CANCELLED refuse unless the current
  Attempt is ``finished`` — UNKNOWN demands a reconcile-class closure first,
  created/starting/active still occupy the slot, failed/cancelled never
  masquerade as success, and refusals write nothing (CEO review M1-04 P1-B).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import domain, storage
from teamcraft.errors import (
    AttemptSlotOccupiedError,
    ConflictError,
    DependencyCycleError,
    IllegalTransitionError,
    NotFoundError,
    SchemaOutOfDateError,
    ValidationError,
    VersionConflictError,
)


class FixedClock:
    """Deterministic injectable UTC clock."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: int = 60) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def _shed_v5_protocol_artifacts(conn) -> None:
    """Make a synthetic older-schema fixture match its claimed version."""
    for trigger in storage._V5_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
    for column in ("protocol_hash", "protocol_version"):
        if column in columns:
            conn.execute(f"ALTER TABLE attempts DROP COLUMN {column}")


def _shed_v6_m203_artifacts(conn) -> None:
    for trigger in storage._V6_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in storage._V6_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table in storage._V6_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "max_attempts" in task_columns:
        conn.execute("ALTER TABLE tasks DROP COLUMN max_attempts")


def _shed_v8_m206_artifacts(conn) -> None:
    """Strip schema-v8/v9 additions from fixtures claiming schema v1-v7."""
    for trigger in storage._V8_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in storage._V8_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table in ("approvals", "verifications", "verification_configs"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for trigger in storage._V9_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in storage._V9_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    conn.execute("DROP TABLE IF EXISTS ceo_initializations")
    for trigger in storage._V10_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in storage._V10_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table in ("wake_events", "wakes", "wake_cursors"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for trigger in storage._V11_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    init_columns = {row[1] for row in conn.execute("PRAGMA table_info(ceo_initializations)")}
    if "receipt_source" in init_columns:
        conn.execute("ALTER TABLE ceo_initializations DROP COLUMN receipt_source")


class DomainM104Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m104-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"
        report = storage.migrate_to_head(self.db)
        self.assertTrue(report.fresh)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc))
        self.pid = domain.create_project(
            self.conn, "proj", "/repo/proj", clock=self.clock
        ).id

    # -- helpers ---------------------------------------------------------------

    def make_task(self, title: str, **kwargs) -> domain.Task:
        self.clock.advance()
        return domain.create_task(self.conn, self.pid, title, clock=self.clock, **kwargs)

    def ready_task(self, title: str, **kwargs) -> domain.Task:
        task = self.make_task(title, **kwargs)
        domain.recompute_ready(self.conn, clock=self.clock)
        task = domain.get_task(self.conn, task.id)
        assert task.status == "ready"
        return task

    def run_task(self, task: domain.Task) -> domain.Task:
        """Drive READY->RUNNING through the only legal door (CEO review
        M1-04 fix 2): create_attempt's atomic reservation, then re-read."""
        domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        task = domain.get_task(self.conn, task.id)
        assert task.status == "running"
        return task

    def settle_finished(self, task_id: str) -> domain.Attempt:
        """created->starting->active->finished (P1-B: execution must actually
        finish before the Task may claim completion)."""
        att = domain.occupying_attempt(self.conn, task_id)
        assert att is not None
        if att.status == "created":
            att = domain.transition_attempt(self.conn, att.id, "start", clock=self.clock)
        if att.status == "starting":
            att = domain.transition_attempt(self.conn, att.id, "activated", clock=self.clock)
        if att.status == "active":
            att = domain.transition_attempt(
                self.conn, att.id, "finish", result_json='{"ok": true}',
                clock=self.clock,
            )
        assert att.status == "finished"
        return att

    def settle_cancelled(self, task_id: str) -> domain.Attempt:
        """Settle an occupying attempt to cancelled without claiming success."""
        att = domain.occupying_attempt(self.conn, task_id)
        assert att is not None
        if att.status == "created":
            att = domain.transition_attempt(self.conn, att.id, "start", clock=self.clock)
        if att.status == "starting":
            att = domain.transition_attempt(self.conn, att.id, "abandon", clock=self.clock)
        assert att.status == "cancelled"
        return att

    def claim_done(self, task: domain.Task) -> domain.Task:
        """worker_reported_done using the caller's version view."""
        return domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock,
        )

    def claim_refused(self, task: domain.Task, reason: str) -> None:
        """Assert the completion claim is refused with this exact reason code."""
        with self.assertRaises(IllegalTransitionError) as ctx:
            self.claim_done(task)
        self.assertEqual(ctx.exception.reason_code, reason)

    def verification_service_required(self, task: domain.Task, event: str = "verification_passed") -> domain.Task:
        before = domain.get_task(self.conn, task.id)
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(self.conn, task.id, event,
                expected_version=before.version, clock=self.clock)
        self.assertEqual(ctx.exception.reason_code, "VERIFICATION_SERVICE_REQUIRED")
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.status, after.version), (before.status, before.version))
        return after

    def raw_task_update(self, task_id: str, status: str) -> None:
        """Test scaffold: drive a task to a status SQL-directly (simulates
        facts produced by later milestones, e.g. reconcile or delivery)."""
        with storage.transaction(self.conn):
            self.conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )

    def raw_attempt(self, task_id: str, status: str, attempt_no: int = 1) -> str:
        aid = f"rawatt{attempt_no:04d}{task_id[-8:]}"[:64]
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO attempts (id, task_id, agent_id, attempt_no, status)"
                " VALUES (?, ?, NULL, ?, ?)",
                (aid, task_id, attempt_no, status),
            )
        return aid

    # -- schema head -----------------------------------------------------------

    def test_fresh_database_passes_head_gate(self) -> None:
        domain.require_head(self.conn)  # must not raise
        # storage.verify_database reports the same head view
        info = storage.verify_database(self.db)
        self.assertTrue(info["at_head"])
        self.assertEqual(info["missing_head_tables"], [])

    def test_domain_refuses_lagging_v1_schema_but_storage_still_works(self) -> None:
        # Simulate an audited, un-upgraded v1 database (the historical state
        # before this build's forward migration was run).  M1-05: an honest
        # v1 sheds the v2/v3 artifacts too — a lingering v3 audit record is
        # (correctly) treated as an inconsistent schema by the write gate.
        with storage.transaction(self.conn):
            _shed_v5_protocol_artifacts(self.conn)
            _shed_v6_m203_artifacts(self.conn)
            _shed_v8_m206_artifacts(self.conn)
            self.conn.execute("DROP TABLE task_dependencies")
            self.conn.execute("DROP TABLE events")
            self.conn.execute("DROP TABLE commands")
            # M1-07 (v4): an honest v1 sheds the runtime artifacts too.
            self.conn.execute("DROP TABLE leader_lease")
            self.conn.execute("DROP TABLE runtime_meta")
            self.conn.execute("DROP TABLE members")
            self.conn.execute("DELETE FROM schema_migrations WHERE version > 1")
            self.conn.execute("PRAGMA user_version=1")
        with self.assertRaises(SchemaOutOfDateError):
            domain.get_task(self.conn, "whatever")
        # M1-03 contracts keep holding at v1: plain gated writes still work.
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id, name, repo_path, created_at)"
                " VALUES ('p-v1', 'v1', '/old/repo', '2026-10-31T00:00:00+00:00')"
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2
        )
        # And the forward migration upgrades in place afterwards.
        report = storage.migrate_to_head(self.db)
        self.assertFalse(report.fresh)
        self.assertEqual((report.from_version, report.to_version),
                       (1, storage.SCHEMA_VERSION))
        self.assertEqual(report.applied, tuple(range(2, storage.SCHEMA_VERSION + 1)))
        self.conn.close()
        self.conn = storage.connect(self.db)
        domain.require_head(self.conn)

    # -- projects / tasks basics -------------------------------------------------

    def test_duplicate_repo_path_conflicts_and_unknown_refs_are_not_found(self) -> None:
        with self.assertRaises(ConflictError):
            domain.create_project(self.conn, "dup", "/repo/proj", clock=self.clock)
        with self.assertRaises(NotFoundError):
            domain.create_task(self.conn, "missing-project", "t")
        with self.assertRaises(NotFoundError):
            domain.get_task(self.conn, "a" * 24)
        with self.assertRaises(ValidationError):
            self.make_task("bad priority", priority=101)
        with self.assertRaises(ValidationError):
            self.make_task("bad kind", kind="deploy")

    def test_task_happy_path_bumps_version_once_per_write(self) -> None:
        task = self.ready_task("ship-it")
        self.assertEqual((task.status, task.version), ("ready", 1))  # 0 + derive flip
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        self.assertEqual(attempt.status, "created")
        self.assertEqual(attempt.attempt_no, 1)
        task = domain.get_task(self.conn, task.id)
        self.assertEqual((task.status, task.version), ("running", 2))
        self.settle_finished(task.id)  # P1-B: finish the execution first
        task = domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock,
        )
        self.assertEqual(task.status, "verifying")
        self.verification_service_required(task)
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                expected_version=task.version, clock=self.clock)
        self.assertEqual(ctx.exception.reason_code, "M4_EVIDENCE_REQUIRED")

    def test_scout_cannot_bypass_verification_and_human_approval_service(self) -> None:
        task = self.ready_task("scout-docs", kind="scout")
        task = self.run_task(task)
        self.assertEqual(task.status, "running")
        self.settle_finished(task.id)  # P1-B: scout must finish its attempt too
        task = domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock,
        )
        task = self.verification_service_required(task)
        self.assertEqual(task.status, "verifying")

    def test_illegal_task_transitions_are_structured(self) -> None:
        task = self.make_task("nope")  # pending
        # dispatch_reserved is refused through the generic door on ANY status
        # with RESERVATION_REQUIRED (CEO review M1-04 fix 2)...
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(
                self.conn, task.id, "dispatch_reserved", expected_version=task.version
            )
        self.assertEqual(
            (ctx.exception.entity, ctx.exception.current, ctx.exception.event),
            ("task", "pending", "dispatch_reserved"),
        )
        self.assertEqual(ctx.exception.reason_code, "RESERVATION_REQUIRED")
        # ...and likewise from READY: the task must not move without a
        # reservation, and nothing is written.
        ready = self.ready_task("door")
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(
                self.conn, ready.id, "dispatch_reserved",
                expected_version=ready.version, clock=self.clock,
            )
        self.assertEqual(ctx.exception.reason_code, "RESERVATION_REQUIRED")
        untouched = domain.get_task(self.conn, ready.id)
        self.assertEqual((untouched.status, untouched.version), ("ready", ready.version))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0
        )
        # control: the reserved door still works
        self.assertEqual(self.run_task(ready).status, "running")
        # Ambiguous event: explicit target required, disallowed target refused.
        running = self.ready_task("amb")
        running = self.run_task(running)
        with self.assertRaises(ValidationError):
            domain.apply_task_event(
                self.conn, running.id, "runtime_unknown",
                expected_version=running.version, clock=self.clock,
            )
        with self.assertRaises(IllegalTransitionError) as bad:
            domain.apply_task_event(
                self.conn, running.id, "runtime_unknown",
                expected_version=running.version, target="done", clock=self.clock,
            )
        self.assertEqual(bad.exception.reason_code, "TARGET_NOT_ALLOWED")
        decided = domain.apply_task_event(
            self.conn, running.id, "runtime_unknown",
            expected_version=running.version, target="needs_human", clock=self.clock,
        )
        self.assertEqual(decided.status, "needs_human")

    def test_user_cancel_from_any_non_terminal_and_terminal_is_frozen(self) -> None:
        for kind_status in ("pending", "ready", "running", "blocked", "needs_human"):
            task = self.make_task(f"cancel-{kind_status}")
            if kind_status in ("ready", "running", "blocked", "needs_human"):
                domain.recompute_ready(self.conn, clock=self.clock)
                task = domain.get_task(self.conn, task.id)
            if kind_status == "running":
                task = self.run_task(task)
                # P1-B: a running task may not vanish while its Attempt still
                # holds the slot; the attempt must be settled first.
                with self.assertRaises(IllegalTransitionError) as ctx:
                    domain.apply_task_event(
                        self.conn, task.id, "user_cancel",
                        expected_version=task.version, clock=self.clock,
                    )
                self.assertEqual(ctx.exception.reason_code, "EXECUTION_NOT_FINISHED")
                self.settle_cancelled(task.id)
            elif kind_status == "blocked":
                task = self.run_task(task)
                self.settle_finished(task.id)  # P1-B: finish before claiming done
                task = domain.apply_task_event(
                    self.conn, task.id, "worker_reported_done",
                    expected_version=task.version, clock=self.clock,
                )
                self.raw_task_update(task.id, "blocked")
                task = domain.get_task(self.conn, task.id)
            elif kind_status == "needs_human":
                task = self.run_task(task)
                att = domain.occupying_attempt(self.conn, task.id)
                domain.transition_attempt(self.conn, att.id, "start", clock=self.clock)
                domain.transition_attempt(self.conn, att.id, "activated", clock=self.clock)
                task = domain.apply_task_event(
                    self.conn, task.id, "runtime_unknown",
                    expected_version=task.version, target="needs_human", clock=self.clock,
                )
                # Cancelling while the attempt is still active is refused...
                with self.assertRaises(IllegalTransitionError) as ctx:
                    domain.apply_task_event(
                        self.conn, task.id, "user_cancel",
                        expected_version=task.version, clock=self.clock,
                    )
                self.assertEqual(ctx.exception.reason_code, "EXECUTION_NOT_FINISHED")
                # ...and so is cancelling with an unresolved UNKNOWN; the
                # reconcile-class transition must close the fact first.
                domain.transition_attempt(
                    self.conn, att.id, "runtime_lost", clock=self.clock
                )
                with self.assertRaises(IllegalTransitionError) as ctx:
                    domain.apply_task_event(
                        self.conn, task.id, "user_cancel",
                        expected_version=task.version, clock=self.clock,
                    )
                self.assertEqual(
                    ctx.exception.reason_code,
                    "EXECUTION_UNRESOLVED_RECONCILE_REQUIRED",
                )
                domain.transition_attempt(
                    self.conn, att.id, "reconciled_failed",
                    result_json='{"evidence": "operator audit"}', clock=self.clock,
                )
            self.assertEqual(task.status, kind_status)
            task = domain.apply_task_event(
                self.conn, task.id, "user_cancel",
                expected_version=task.version, clock=self.clock,
            )
            self.assertEqual(task.status, "cancelled")
            # terminal: nothing may fire any more
            with self.assertRaises(IllegalTransitionError):
                domain.apply_task_event(
                    self.conn, task.id, "user_cancel",
                    expected_version=task.version, clock=self.clock,
                )

    # -- execution-completion guard (CEO review M1-04 P1-B) ---------------------

    def test_regression_done_with_occupying_attempt_is_blocked(self) -> None:
        """P1-B regression: reproduces the hole first, then proves it closed.

        At commit ccce94b a scout Task could walk running ->
        worker_reported_done -> verifying -> verification_passed -> DONE while
        its only Attempt still sat in CREATED (and likewise STARTING/ACTIVE/
        UNKNOWN): the scheduler called a Task complete whose execution never
        stopped.  The shared guard now refuses the claim in every occupying
        status, UNKNOWN demands a reconcile-class closure first (review
        8.2-11), and only a ``finished`` Attempt lets the Task complete.
        """
        task = self.ready_task("hole", kind="scout")
        task = self.run_task(task)  # Attempt lands in CREATED
        att = domain.occupying_attempt(self.conn, task.id)
        self.assertEqual(att.status, "created")
        # REPRO: completion claimed while the Attempt merely exists.
        self.claim_refused(task, "EXECUTION_NOT_FINISHED")
        # STARTING and ACTIVE are equally "not finished".
        domain.transition_attempt(self.conn, att.id, "start", clock=self.clock)
        self.claim_refused(task, "EXECUTION_NOT_FINISHED")
        domain.transition_attempt(self.conn, att.id, "activated", clock=self.clock)
        self.claim_refused(task, "EXECUTION_NOT_FINISHED")
        # UNKNOWN must never pass the ordinary path — reconcile first.
        domain.transition_attempt(self.conn, att.id, "runtime_lost", clock=self.clock)
        self.claim_refused(task, "EXECUTION_UNRESOLVED_RECONCILE_REQUIRED")
        untouched = domain.get_task(self.conn, task.id)
        self.assertEqual(
            (untouched.status, untouched.version), ("running", task.version)
        )  # refusals wrote nothing
        # reconciled_finished lifts the gate and the scout completes normally.
        domain.transition_attempt(
            self.conn, att.id, "reconciled_finished",
            result_json='{"evidence": "recovered output"}', clock=self.clock,
        )
        task = self.claim_done(task)
        self.assertEqual(task.status, "verifying")
        task = self.verification_service_required(task)
        self.assertEqual(task.status, "verifying")
        self.assertIsNone(domain.occupying_attempt(self.conn, task.id))

    def test_ship_completion_chain_requires_finished_attempt(self) -> None:
        task = self.ready_task("ship-chain")
        task = self.run_task(task)
        self.claim_refused(task, "EXECUTION_NOT_FINISHED")  # CREATED blocks
        self.settle_finished(task.id)
        task = self.claim_done(task)
        self.assertEqual(task.status, "verifying")
        self.verification_service_required(task)
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                expected_version=task.version, clock=self.clock)
        self.assertEqual(ctx.exception.reason_code, "M4_EVIDENCE_REQUIRED")

    def test_active_attempt_cannot_claim_done_and_version_is_untouched(self) -> None:
        task = self.ready_task("active-refusal")
        task = self.run_task(task)
        att = domain.occupying_attempt(self.conn, task.id)
        domain.transition_attempt(self.conn, att.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, att.id, "activated", clock=self.clock)
        self.claim_refused(task, "EXECUTION_NOT_FINISHED")
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.status, after.version), ("running", task.version))

    def test_failed_attempt_cannot_masquerade_as_success(self) -> None:
        task = self.ready_task("masquerade")
        task = self.run_task(task)
        att = domain.occupying_attempt(self.conn, task.id)
        domain.transition_attempt(self.conn, att.id, "start", clock=self.clock)
        domain.transition_attempt(
            self.conn, att.id, "start_failed",
            result_json='{"error": "boom"}', clock=self.clock,
        )
        self.claim_refused(task, "EXECUTION_NOT_SUCCESSFUL")
        # A settled (cancelled) attempt may end the Task non-successfully,
        # but never through a success claim:
        task = self.ready_task("masquerade-cancel")
        task = self.run_task(task)
        self.settle_cancelled(task.id)
        task = domain.apply_task_event(
            self.conn, task.id, "user_cancel",
            expected_version=task.version, clock=self.clock,
        )
        self.assertEqual(task.status, "cancelled")

    def test_recovery_after_failed_execution_reaches_verifying_via_new_attempt(self) -> None:
        task = self.ready_task("recovery")
        task = self.run_task(task)
        att = domain.occupying_attempt(self.conn, task.id)
        domain.transition_attempt(self.conn, att.id, "start", clock=self.clock)
        domain.transition_attempt(
            self.conn, att.id, "start_failed",
            result_json='{"error": "agent died"}', clock=self.clock,
        )
        self.claim_refused(task, "EXECUTION_NOT_SUCCESSFUL")
        # repair loop: needs_human -> reconciled back to READY, reserve a
        # second attempt, and only its finish unlocks the success path.
        task = domain.apply_task_event(
            self.conn, task.id, "runtime_unknown",
            expected_version=task.version, target="needs_human", clock=self.clock,
        )
        task = domain.apply_task_event(
            self.conn, task.id, "human_reconciled",
            expected_version=task.version, target="ready", clock=self.clock,
        )
        self.assertEqual(task.status, "ready")
        second = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        self.assertEqual(second.attempt_no, 2)
        self.settle_finished(task.id)
        task = self.claim_done(domain.get_task(self.conn, task.id))
        self.assertEqual(task.status, "verifying")
        self.verification_service_required(task)

    def test_ship_done_requires_m4_delivery_evidence(self) -> None:
        task = self.ready_task("frozen")
        task = self.run_task(task)
        self.settle_finished(task.id)
        task = self.claim_done(task)
        self.verification_service_required(task)
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                expected_version=task.version, clock=self.clock)
        self.assertEqual(ctx.exception.reason_code, "M4_EVIDENCE_REQUIRED")
        self.assertEqual(domain.get_task(self.conn, task.id).status, "verifying")
        self.assertIsNone(domain.occupying_attempt(self.conn, task.id))

    def test_outer_storage_transaction_commits_whole_execution_chain(self) -> None:
        """Domain writes JOIN an outer storage.transaction (CEO review P1-B):
        reservation, attempt lifecycle, and every Task event land in one
        COMMIT."""
        task = self.ready_task("tx-commit")
        with storage.transaction(self.conn):
            domain.create_attempt(
                self.conn, task.id, expected_version=task.version, clock=self.clock
            )
            self.settle_finished(task.id)
            running = domain.get_task(self.conn, task.id)
            verifying = domain.apply_task_event(
                self.conn, task.id, "worker_reported_done",
                expected_version=running.version, clock=self.clock,
            )
            self.assertEqual(verifying.status, "verifying")
            self.verification_service_required(verifying)
            with self.assertRaises(IllegalTransitionError) as ctx:
                domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                    expected_version=verifying.version, clock=self.clock)
            self.assertEqual(ctx.exception.reason_code, "M4_EVIDENCE_REQUIRED")
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.status, after.version), ("verifying", verifying.version))  # committed
        self.assertIsNone(domain.occupying_attempt(self.conn, task.id))

    def test_outer_storage_transaction_rolls_back_everything(self) -> None:
        task = self.ready_task("tx-rollback")
        with self.assertRaises(RuntimeError):
            with storage.transaction(self.conn):
                domain.create_attempt(
                    self.conn, task.id,
                    expected_version=task.version, clock=self.clock,
                )
                self.settle_finished(task.id)
                running = domain.get_task(self.conn, task.id)
                domain.apply_task_event(
                    self.conn, task.id, "worker_reported_done",
                    expected_version=running.version, clock=self.clock,
                )
                raise RuntimeError("operator abort")
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.status, after.version), ("ready", task.version))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task.id,)
            ).fetchone()[0],
            0,
        )  # attempt rows rolled back with the Task CAS
        self.assertIsNone(domain.occupying_attempt(self.conn, task.id))

    # -- optimistic versioning ----------------------------------------------------

    def test_stale_version_conflicts_without_writing(self) -> None:
        task = self.ready_task("cas")
        updated = domain.update_task(
            self.conn, task.id, expected_version=task.version,
            priority=90, clock=self.clock,
        )
        self.assertEqual((updated.priority, updated.version), (90, 2))
        with self.assertRaises(VersionConflictError) as ctx:
            domain.update_task(
                self.conn, task.id, expected_version=1, title="stale edit",
                clock=self.clock,
            )
        self.assertEqual(
            (ctx.exception.entity_id, ctx.exception.expected, ctx.exception.found),
            (task.id, 1, 2),
        )
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.title, after.priority, after.version),
                         ("cas", 90, 2))  # nothing written by the loser
        with self.assertRaises(ValidationError):
            domain.update_task(self.conn, task.id, expected_version=2)

    def test_two_connections_race_dispatch_exactly_one_wins(self) -> None:
        task = self.ready_task("race")
        other = storage.connect(self.db)
        self.addCleanup(other.close)
        winner = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        with self.assertRaises((VersionConflictError, IllegalTransitionError)):
            domain.create_attempt(other, task.id, expected_version=task.version)
        attempts = self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task.id,)
        ).fetchone()[0]
        self.assertEqual(attempts, 1)
        self.assertEqual(domain.occupying_attempt(self.conn, task.id).id, winner.id)
        # cross-connection optimistic conflict on plain edits too
        with self.assertRaises(VersionConflictError) as ctx:
            domain.update_task(other, task.id, expected_version=task.version, risk="high")
        self.assertEqual(ctx.exception.found, task.version + 1)

    # -- attempts -----------------------------------------------------------------

    def test_attempt_machine_happy_and_illegal_edges(self) -> None:
        task = self.ready_task("att")
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        self.assertIsNone(attempt.started_at)
        attempt = domain.transition_attempt(
            self.conn, attempt.id, "start", clock=self.clock
        )
        self.assertEqual(attempt.status, "starting")
        attempt = domain.transition_attempt(
            self.conn, attempt.id, "activated", clock=self.clock
        )
        self.assertIsNotNone(attempt.started_at)
        attempt = domain.transition_attempt(
            self.conn, attempt.id, "finish", result_json='{"ok": true}', clock=self.clock
        )
        self.assertEqual((attempt.status, attempt.finished_at is not None), ("finished", True))
        for event in ("finish", "fail", "start", "abandon", "runtime_lost"):
            with self.assertRaises(IllegalTransitionError) as ctx:
                domain.transition_attempt(self.conn, attempt.id, event, clock=self.clock)
            self.assertEqual(
                (ctx.exception.entity, ctx.exception.current, ctx.exception.event),
                ("attempt", "finished", event),
            )
        self.assertIsNone(domain.occupying_attempt(self.conn, task.id))

    def test_unknown_attempt_occupies_slot_and_rolls_back_the_cas(self) -> None:
        task = self.ready_task("unknown-slot")
        holder = self.raw_attempt(task.id, "unknown")
        with self.assertRaises(AttemptSlotOccupiedError) as ctx:
            domain.create_attempt(
                self.conn, task.id, expected_version=task.version, clock=self.clock
            )
        self.assertEqual(
            (ctx.exception.task_id, ctx.exception.occupying_id), (task.id, holder)
        )
        after = domain.get_task(self.conn, task.id)
        # whole transaction rolled back: still READY at the same version,
        # and no second attempt row exists.
        self.assertEqual((after.status, after.version), ("ready", task.version))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task.id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(domain.occupying_attempt(self.conn, task.id).id, holder)

    def test_unknown_closes_only_via_reconcile_then_releases_slot(self) -> None:
        task = self.ready_task("reconcile")
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        domain.transition_attempt(self.conn, attempt.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, attempt.id, "activated", clock=self.clock)
        attempt = domain.transition_attempt(
            self.conn, attempt.id, "runtime_lost", clock=self.clock
        )
        self.assertEqual(attempt.status, "unknown")
        for event in ("abandon", "fail", "finish", "start"):
            with self.assertRaises(IllegalTransitionError):
                domain.transition_attempt(self.conn, attempt.id, event, clock=self.clock)
        settled = domain.transition_attempt(
            self.conn, attempt.id, "reconciled_failed",
            result_json='{"evidence": "pane gone"}', clock=self.clock,
        )
        self.assertEqual(settled.status, "failed")
        self.assertIsNone(domain.occupying_attempt(self.conn, task.id))

    def test_dispatch_requires_ready_and_existing_agent(self) -> None:
        task = self.make_task("not-ready")  # pending
        with self.assertRaises(IllegalTransitionError):
            domain.create_attempt(
                self.conn, task.id, expected_version=task.version, clock=self.clock
            )
        ready = self.ready_task("fk")
        with self.assertRaises(NotFoundError):
            domain.create_attempt(
                self.conn, ready.id, expected_version=ready.version,
                agent_id="ghost-agent", clock=self.clock,
            )
        # FK failure rolled back atomically: task untouched, no attempt row.
        after = domain.get_task(self.conn, ready.id)
        self.assertEqual((after.status, after.version), ("ready", ready.version))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0
        )

    # -- dependency DAG -----------------------------------------------------------

    def test_dependency_edge_changes_bump_version_and_bounce_stale_writers(self) -> None:
        # CEO review M1-04 fixes 1+3: dependency CRUD demands the caller's
        # expected_version, and real edge changes travel on version bumps.
        task = self.ready_task("dep-cas")  # no deps -> ready at v1
        blocker = self.make_task("dep-blocker")
        before = task.version
        # stale view: refused, graph and version untouched
        with self.assertRaises(VersionConflictError) as ctx:
            domain.add_dependency(
                self.conn, task.id, blocker.id, expected_version=before - 1,
                clock=self.clock,
            )
        self.assertEqual(
            (ctx.exception.expected, ctx.exception.found), (before - 1, before)
        )
        self.assertEqual(domain.dependencies_of(self.conn, task.id), [])
        self.assertEqual(domain.get_task(self.conn, task.id).version, before)
        # fresh view: real add bumps exactly once
        self.assertTrue(
            domain.add_dependency(
                self.conn, task.id, blocker.id, expected_version=before,
                clock=self.clock,
            )
        )
        after_add = domain.get_task(self.conn, task.id)
        self.assertEqual(after_add.version, before + 1)
        self.assertEqual(after_add.status, "ready")  # churn travels on version
        with self.assertRaises(VersionConflictError) as ctx:
            domain.update_task(
                self.conn, task.id, expected_version=before, priority=80,
                clock=self.clock,
            )
        self.assertEqual(
            (ctx.exception.expected, ctx.exception.found), (before, before + 1)
        )
        current = domain.update_task(
            self.conn, task.id, expected_version=after_add.version, priority=80,
            clock=self.clock,
        )
        self.assertEqual(current.priority, 80)
        # duplicate add and missing-edge remove are no-ops: NO bump
        v = domain.get_task(self.conn, task.id).version
        self.assertFalse(
            domain.add_dependency(
                self.conn, task.id, blocker.id, expected_version=v, clock=self.clock
            )
        )
        self.assertEqual(domain.get_task(self.conn, task.id).version, v)
        self.assertFalse(
            domain.remove_dependency(
                self.conn, task.id, blocker.id + "x", expected_version=v
            )
        )
        self.assertEqual(domain.get_task(self.conn, task.id).version, v)
        # stale remove: refused, edge survives, no bump
        with self.assertRaises(VersionConflictError):
            domain.remove_dependency(
                self.conn, task.id, blocker.id, expected_version=v - 1
            )
        self.assertEqual(domain.dependencies_of(self.conn, task.id), [blocker.id])
        self.assertEqual(domain.get_task(self.conn, task.id).version, v)
        # fresh real remove bumps exactly once
        self.assertTrue(
            domain.remove_dependency(
                self.conn, task.id, blocker.id, expected_version=v
            )
        )
        self.assertEqual(domain.get_task(self.conn, task.id).version, v + 1)

    def test_dependencies_satisfied_refused_until_upstreams_done(self) -> None:
        # CEO review M1-04 #2 (PLAN 5.1 condition column): the event asserts
        # a FACT; submitting it must not bypass unfinished upstreams.
        a = self.make_task("gate-a")
        b = self.make_task("gate-b")
        domain.add_dependency(
            self.conn, a.id, b.id, expected_version=a.version, clock=self.clock
        )
        a = domain.get_task(self.conn, a.id)
        self.assertEqual(a.status, "pending")
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(
                self.conn, a.id, "dependencies_satisfied",
                expected_version=a.version, clock=self.clock,
            )
        self.assertEqual(
            (ctx.exception.entity, ctx.exception.current, ctx.exception.event),
            ("task", "pending", "dependencies_satisfied"),
        )
        self.assertEqual(ctx.exception.reason_code, "DEPENDENCIES_NOT_SATISFIED")
        untouched = domain.get_task(self.conn, a.id)
        self.assertEqual((untouched.status, untouched.version), ("pending", a.version))
        # once the upstream is DONE, the same event legitimately succeeds
        self.raw_task_update(b.id, "done")
        a = domain.get_task(self.conn, a.id)
        ready = domain.apply_task_event(
            self.conn, a.id, "dependencies_satisfied",
            expected_version=a.version, clock=self.clock,
        )
        self.assertEqual(ready.status, "ready")

    def test_dispatch_refused_when_deps_unsatisfied_despite_ready_status(self) -> None:
        # status='ready' is a derived claim; create_attempt must re-verify the
        # hard-dependency fact inside the transaction (cannot trust status or
        # ready_queue alone).
        task = self.ready_task("stale-ready")  # ready with zero deps
        blocker = self.make_task("late-blocker")  # pending, not done
        self.assertTrue(
            domain.add_dependency(
                self.conn, task.id, blocker.id, expected_version=task.version,
                clock=self.clock,
            )
        )
        task = domain.get_task(self.conn, task.id)
        self.assertEqual(task.status, "ready")  # stale readiness lingers
        self.assertNotIn(
            task.id, [t.id for t in domain.ready_queue(self.conn, self.pid)]
        )
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.create_attempt(
                self.conn, task.id, expected_version=task.version, clock=self.clock
            )
        self.assertEqual(ctx.exception.reason_code, "DEPENDENCIES_NOT_SATISFIED")
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.status, after.version), ("ready", task.version))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0
        )
        # the generic event door is closed even earlier by the reservation
        # guard (fix 2): dispatch_reserved never rides apply_task_event
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(
                self.conn, task.id, "dispatch_reserved",
                expected_version=task.version, clock=self.clock,
            )
        self.assertEqual(ctx.exception.reason_code, "RESERVATION_REQUIRED")
        # and once the blocker is DONE, dispatch legitimately proceeds
        self.raw_task_update(blocker.id, "done")
        task = domain.get_task(self.conn, task.id)
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        self.assertEqual(attempt.status, "created")

    def test_cycle_returns_concrete_closed_path(self) -> None:
        a = self.make_task("A")
        b = self.make_task("B")
        c = self.make_task("C")
        self.assertTrue(
            domain.add_dependency(
                self.conn, b.id, c.id, expected_version=b.version, clock=self.clock
            )
        )
        self.assertTrue(
            domain.add_dependency(
                self.conn, a.id, b.id, expected_version=a.version, clock=self.clock
            )
        )
        with self.assertRaises(DependencyCycleError) as ctx:
            domain.add_dependency(
                self.conn, c.id, a.id, expected_version=c.version, clock=self.clock
            )
        cycle = ctx.exception.cycle
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(cycle[0], c.id)
        self.assertEqual(set(cycle[:-1]), {a.id, b.id, c.id})
        # every consecutive pair must be a real edge (committed or refused-new)
        edges = {(b.id, c.id), (a.id, b.id)}
        for src, dst in zip(cycle, cycle[1:]):
            self.assertIn((src, dst), edges | {(c.id, a.id)})
        # graph unchanged by the refusal
        self.assertEqual(domain.dependencies_of(self.conn, c.id), [])

    def test_self_dependency_is_a_two_step_cycle(self) -> None:
        a = self.make_task("self")
        with self.assertRaises(DependencyCycleError) as ctx:
            domain.add_dependency(self.conn, a.id, a.id, expected_version=a.version)
        self.assertEqual(ctx.exception.cycle, [a.id, a.id])

    def test_dependency_add_is_idempotent_and_removal_reports_hit(self) -> None:
        a = self.make_task("X")
        b = self.make_task("Y")
        self.assertTrue(
            domain.add_dependency(
                self.conn, a.id, b.id, expected_version=a.version, clock=self.clock
            )
        )
        v1 = domain.get_task(self.conn, a.id).version  # bumped 0 -> 1
        self.assertFalse(
            domain.add_dependency(
                self.conn, a.id, b.id, expected_version=v1, clock=self.clock
            )
        )
        self.assertEqual(domain.get_task(self.conn, a.id).version, v1)
        self.assertEqual(domain.dependencies_of(self.conn, a.id), [b.id])
        self.assertTrue(
            domain.remove_dependency(self.conn, a.id, b.id, expected_version=v1)
        )
        v2 = domain.get_task(self.conn, a.id).version  # bumped exactly once
        self.assertEqual(v2, v1 + 1)
        self.assertFalse(
            domain.remove_dependency(self.conn, a.id, b.id, expected_version=v2)
        )
        self.assertEqual(domain.get_task(self.conn, a.id).version, v2)
        self.assertEqual(domain.dependencies_of(self.conn, a.id), [])
        with self.assertRaises(NotFoundError):
            domain.dependencies_of(self.conn, "z" * 24)

    def test_cross_project_dependency_rejected_before_any_write(self) -> None:
        """CEO review M1-04 P1-A: PLAN/ADR do not explicitly address
        cross-Project dependencies; this implementation conservatively
        rejects them, before any write, status, or version side effect;
        same-Project edges stay legal.
        """
        a = self.ready_task("home")
        foreign = domain.create_project(
            self.conn, "foreign", "/repo/foreign", clock=self.clock
        )
        b = domain.create_task(self.conn, foreign.id, "foreign-task", clock=self.clock)
        self.assertEqual(b.project_id, foreign.id)
        for owner, dep in ((a, b), (b, a)):  # either direction is unauthorized
            with self.assertRaises(ValidationError):
                domain.add_dependency(
                    self.conn, owner.id, dep.id,
                    expected_version=owner.version, clock=self.clock,
                )
            self.assertEqual(domain.dependencies_of(self.conn, owner.id), [])
            self.assertEqual(
                domain.get_task(self.conn, owner.id).version, owner.version
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0],
            0,
        )  # rejection wrote nothing anywhere
        # Same-Project control: the edge lands and bumps the owner exactly once.
        c = self.make_task("same-project")
        self.assertTrue(
            domain.add_dependency(
                self.conn, c.id, a.id, expected_version=c.version, clock=self.clock
            )
        )
        self.assertEqual(domain.dependencies_of(self.conn, c.id), [a.id])
        self.assertEqual(
            domain.get_task(self.conn, c.id).version, c.version + 1
        )
        # Stale expected_version conflicts before any write (CEO review #1).
        d = self.make_task("stale-edge")
        with self.assertRaises(VersionConflictError):
            domain.add_dependency(
                self.conn, d.id, c.id, expected_version=d.version + 7,
                clock=self.clock,
            )
        self.assertEqual(domain.dependencies_of(self.conn, d.id), [])
        self.assertEqual(domain.get_task(self.conn, d.id).version, d.version)

    def test_dependency_edits_follow_task_status(self) -> None:
        a = self.ready_task("locked")
        b = self.make_task("later")
        a = self.run_task(a)
        with self.assertRaises(ValidationError):
            domain.add_dependency(
                self.conn, a.id, b.id, expected_version=a.version, clock=self.clock
            )
        with self.assertRaises(ValidationError):
            domain.remove_dependency(
                self.conn, a.id, "anything", expected_version=a.version
            )
        # cannot depend on cancelled/failed tasks
        b = domain.apply_task_event(
            self.conn, b.id, "user_cancel",
            expected_version=b.version, clock=self.clock,
        )
        pending = self.make_task("pending-edge")
        with self.assertRaises(ValidationError):
            domain.add_dependency(
                self.conn, pending.id, b.id, expected_version=pending.version,
                clock=self.clock,
            )

    # -- READY derivation & ordering ------------------------------------------------

    def test_ready_derives_only_from_done_and_regresses(self) -> None:
        blocker = self.make_task("blocker")
        blocked = self.make_task("blocked")
        domain.add_dependency(
            self.conn, blocked.id, blocker.id, expected_version=blocked.version,
            clock=self.clock,
        )
        changes = domain.recompute_ready(self.conn, self.pid, clock=self.clock)
        self.assertNotIn(blocked.id, changes)  # dep not done: stays PENDING, no churn
        self.assertNotIn(blocked.id, {k for k, (o, n) in changes.items() if n == "ready"})
        self.assertIn(blocker.id, {k for k, (o, n) in changes.items() if n == "ready"})
        self.assertNotIn(blocked.id, [t.id for t in domain.ready_queue(self.conn, self.pid)])
        # dependency reaches done -> blocked becomes READY
        self.raw_task_update(blocker.id, "done")
        changes = domain.recompute_ready(self.conn, self.pid, clock=self.clock)
        self.assertEqual(changes[blocked.id], ("pending", "ready"))
        self.assertIn(blocked.id, [t.id for t in domain.ready_queue(self.conn, self.pid)])
        # dependency regresses -> back to PENDING, version churn visible
        self.raw_task_update(blocker.id, "pending")
        before = domain.get_task(self.conn, blocked.id)
        changes = domain.recompute_ready(self.conn, self.pid, clock=self.clock)
        self.assertEqual(changes[blocked.id], ("ready", "pending"))
        after = domain.get_task(self.conn, blocked.id)
        self.assertEqual(after.version, before.version + 1)
        # running tasks are never auto-derived
        self.raw_task_update(blocked.id, "running")
        domain.recompute_ready(self.conn, self.pid, clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, blocked.id).status, "running")

    def test_ready_queue_orders_by_priority_then_creation(self) -> None:
        low = self.ready_task("low", priority=10)
        mid1 = self.ready_task("mid-1", priority=50)
        high = self.ready_task("high", priority=90)
        mid2 = self.ready_task("mid-2", priority=50)
        ordered = [t.id for t in domain.ready_queue(self.conn, self.pid)]
        self.assertEqual(ordered, [high.id, mid1.id, mid2.id, low.id])
        # a task already holding an occupying attempt leaves the queue
        self.raw_attempt(low.id, "created", attempt_no=1)
        ordered = [t.id for t in domain.ready_queue(self.conn, self.pid)]
        self.assertNotIn(low.id, ordered)
        self.assertEqual(ordered, [high.id, mid1.id, mid2.id])

    # -- sessions: live signals are never completion ------------------------------

    def test_session_signals_never_touch_task_or_attempt_rows(self) -> None:
        task = self.ready_task("with-session")
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        session = domain.register_session(
            self.conn, attempt.id, "herdr",
            native_session_id="native-1", pane_id="pane-7", clock=self.clock,
        )
        self.assertEqual(session.state, "unknown")
        task_before = domain.get_task(self.conn, task.id)
        attempt_before = domain.get_attempt(self.conn, attempt.id)
        for state in ("starting", "idle", "working", "idle", "exited", "missing"):
            session = domain.observe_session(self.conn, session.id, state, clock=self.clock)
            self.assertEqual(session.state, state)
            self.assertIsNotNone(session.last_observed_at)
        task_after = domain.get_task(self.conn, task.id)
        attempt_after = domain.get_attempt(self.conn, attempt.id)
        # Session disappearance changes NOTHING about the Task identity/life
        # (M0 review 9.1 M1-04: "Session 消失不改变 Task ID") or the Attempt.
        self.assertEqual(task_after.id, task_before.id)
        self.assertEqual(
            (task_after.status, task_after.version, task_after.updated_at),
            (task_before.status, task_before.version, task_before.updated_at),
        )
        self.assertEqual(
            (attempt_after.status, attempt_after.finished_at),
            (attempt_before.status, attempt_before.finished_at),
        )
        # and there is no session-signal event on the Task machine
        for fake in ("session_idle", "session_done", "pane_gone", "heartbeat"):
            with self.assertRaises(IllegalTransitionError):
                domain.apply_task_event(
                    self.conn, task.id, fake, expected_version=task_after.version
                )

    def test_session_machine_requires_reprobe_before_revival(self) -> None:
        task = self.ready_task("sess-machine")
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version, clock=self.clock
        )
        session = domain.register_session(self.conn, attempt.id, "herdr", clock=self.clock)
        domain.observe_session(self.conn, session.id, "working", clock=self.clock)
        domain.observe_session(self.conn, session.id, "exited", clock=self.clock)
        with self.assertRaises(IllegalTransitionError):
            domain.observe_session(self.conn, session.id, "working", clock=self.clock)
        revived = domain.observe_session(self.conn, session.id, "unknown", clock=self.clock)
        self.assertEqual(revived.state, "unknown")
        with self.assertRaises(ValidationError):
            domain.observe_session(self.conn, session.id, "done")
        with self.assertRaises(NotFoundError):
            domain.register_session(self.conn, "ghost" * 8, "herdr")

    # -- transaction sharing (foundation for M1-05) --------------------------------

    def test_domain_writes_join_outer_transaction_and_rollback_together(self) -> None:
        with self.assertRaises(RuntimeError):
            with storage.transaction(self.conn):
                domain.create_task(self.conn, self.pid, "batch-1", clock=self.clock)
                domain.recompute_ready(self.conn, clock=self.clock)
                raise RuntimeError("boom mid-batch")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
        )
        self.conn.execute("PRAGMA integrity_check").fetchone()
        # and a successful outer batch commits as one unit
        with storage.transaction(self.conn):
            t1 = domain.create_task(self.conn, self.pid, "batch-a", clock=self.clock)
            domain.recompute_ready(self.conn, clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, t1.id).status, "ready")


class M104UpgradePathTests(unittest.TestCase):
    """The completed v1 -> v2 forward upgrade: in place, data-intact, atomic."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m104-up-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"

    def _downgrade_to_v1(self) -> None:
        """Simulate a pre-M1-04 audited v1 database (rows included).

        M1-05 adaptation: head is v3 now, so an honest v1 must also shed the
        v2/v3 artifacts (events/commands + audit rows 2 and 3); the audit
        consistency gate correctly refuses writes if a v3 record lingers.
        """
        storage.migrate_to_head(self.db)
        conn = storage.connect(self.db)
        try:
            with storage.transaction(conn):
                _shed_v5_protocol_artifacts(conn)
                _shed_v6_m203_artifacts(conn)
                _shed_v8_m206_artifacts(conn)
                conn.execute("DROP TABLE task_dependencies")
                conn.execute("DROP TABLE events")
                conn.execute("DROP TABLE commands")
                # M1-07 (v4): an honest v1 sheds the runtime artifacts too.
                conn.execute("DROP TABLE leader_lease")
                conn.execute("DROP TABLE runtime_meta")
                conn.execute("DROP TABLE members")
                conn.execute("DELETE FROM schema_migrations WHERE version > 1")
                conn.execute("PRAGMA user_version=1")
            with storage.transaction(conn):
                now = "2026-10-30T00:00:00+00:00"
                conn.execute(
                    "INSERT INTO projects (id, name, repo_path, created_at)"
                    " VALUES ('p1', 'legacy', '/legacy/repo', ?)", (now,)
                )
                conn.execute(
                    "INSERT INTO tasks (id, project_id, title, status, version,"
                    " created_at, updated_at)"
                    " VALUES ('t1', 'p1', 'legacy task', 'pending', 0, ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "INSERT INTO attempts (id, task_id, attempt_no, status)"
                    " VALUES ('a1', 't1', 1, 'unknown')"
                )
        finally:
            conn.close()

    def test_v1_database_upgrades_in_place_with_rows_intact(self) -> None:
        self._downgrade_to_v1()
        report = storage.migrate_to_head(self.db)
        self.assertFalse(report.fresh)
        self.assertEqual(
            (report.from_version, report.to_version, report.applied),
            (1, storage.SCHEMA_VERSION,
             tuple(range(2, storage.SCHEMA_VERSION + 1))),
        )
        info = storage.verify_database(self.db)
        self.assertTrue(info["ok"])
        self.assertTrue(info["at_head"])
        conn = storage.connect(self.db)
        try:
            # legacy rows untouched by the upgrade
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT status, version FROM tasks WHERE id='t1'").fetchone(),
                ("pending", 0),
            )
            # the single-occupying partial index survived and still bites
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='ux_attempts_single_occupying'"
            ).fetchone()
            self.assertIsNotNone(index_sql)
            with self.assertRaises(sqlite3.IntegrityError):
                with storage.transaction(conn):
                    conn.execute(
                        "INSERT INTO attempts (id, task_id, attempt_no, status)"
                        " VALUES ('a2', 't1', 2, 'created')"
                    )
            # migration audit records every ladder step
            self.assertEqual(
                [r[0] for r in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )],
                list(range(1, storage.SCHEMA_VERSION + 1)),
            )
        finally:
            conn.close()
        # domain services run on the upgraded database
        conn = storage.connect(self.db)
        try:
            domain.require_head(conn)
            self.assertEqual(domain.get_task(conn, "t1").status, "pending")
        finally:
            conn.close()

    def test_upgrade_crash_leaves_database_exactly_at_v1(self) -> None:
        self._downgrade_to_v1()
        calls: list[str] = []

        def hook(index: int, statement: str) -> None:
            calls.append(statement)
            if "task_dependencies" in statement:
                raise RuntimeError("crash mid v2 step")

        storage.MIGRATION_STATEMENT_HOOK = hook
        try:
            with self.assertRaises(RuntimeError):
                storage.migrate_to_head(self.db)
        finally:
            storage.MIGRATION_STATEMENT_HOOK = None
        self.assertTrue(calls)  # the v2 DDL was really attempted
        info = storage.verify_database(self.db)
        self.assertTrue(info["ok"])
        self.assertFalse(info["at_head"])
        self.assertEqual(info["user_version"], 1)
        self.assertEqual(info["schema_migrations_recorded"], 1)
        self.assertEqual(
            info["missing_head_tables"],
            ["approvals", "attempt_briefs", "ceo_initializations", "commands", "events", "leader_lease", "members",
             "prompt_deliveries", "runtime_meta", "task_dependencies", "verification_configs",
             "verifications", "wake_cursors", "wake_events", "wakes", "worker_reports"],
        )
        # retrying the upgrade afterwards converges (idempotent recovery)
        report = storage.migrate_to_head(self.db)
        self.assertEqual((report.from_version, report.to_version),
                       (1, storage.SCHEMA_VERSION))


if __name__ == "__main__":
    unittest.main()
