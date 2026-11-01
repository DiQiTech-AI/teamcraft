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
  rollback), proving the APIs join rather than nest transactions.
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
        # before this build's forward migration was run).
        with storage.transaction(self.conn):
            self.conn.execute("DROP TABLE task_dependencies")
            self.conn.execute("DELETE FROM schema_migrations WHERE version = 2")
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
        self.assertEqual((report.from_version, report.to_version), (1, 2))
        self.assertEqual(report.applied, (2,))
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
        task = domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock,
        )
        self.assertEqual(task.status, "verifying")
        task = domain.apply_task_event(
            self.conn, task.id, "verification_passed",
            expected_version=task.version, clock=self.clock,
        )
        self.assertEqual(task.status, "ready_to_merge")
        task = domain.apply_task_event(
            self.conn, task.id, "approved_and_delivered",
            expected_version=task.version, clock=self.clock,
        )
        self.assertEqual((task.status, task.version), ("done", 5))

    def test_scout_verification_passed_completes_task(self) -> None:
        task = self.ready_task("scout-docs", kind="scout")
        task = self.run_task(task)
        self.assertEqual(task.status, "running")
        task = domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=task.version, clock=self.clock,
        )
        task = domain.apply_task_event(
            self.conn, task.id, "verification_passed",
            expected_version=task.version, clock=self.clock,
        )
        self.assertEqual(task.status, "done")

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
            elif kind_status == "blocked":
                task = self.run_task(task)
                task = domain.apply_task_event(
                    self.conn, task.id, "worker_reported_done",
                    expected_version=task.version, clock=self.clock,
                )
                task = domain.apply_task_event(
                    self.conn, task.id, "verification_failed",
                    expected_version=task.version, target="blocked", clock=self.clock,
                )
            elif kind_status == "needs_human":
                task = self.run_task(task)
                task = domain.apply_task_event(
                    self.conn, task.id, "runtime_unknown",
                    expected_version=task.version, target="needs_human", clock=self.clock,
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
        """Simulate a pre-M1-04 audited v1 database (rows included)."""
        storage.migrate_to_head(self.db)
        conn = storage.connect(self.db)
        try:
            with storage.transaction(conn):
                conn.execute("DROP TABLE task_dependencies")
                conn.execute("DELETE FROM schema_migrations WHERE version = 2")
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
            (1, 2, (2,)),
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
            # migration audit records both steps
            self.assertEqual(
                [r[0] for r in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )],
                [1, 2],
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
        self.assertEqual(info["missing_head_tables"], ["task_dependencies"])
        # retrying the upgrade afterwards converges (idempotent recovery)
        report = storage.migrate_to_head(self.db)
        self.assertEqual((report.from_version, report.to_version), (1, 2))


if __name__ == "__main__":
    unittest.main()