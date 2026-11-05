"""M2-02 Scheduler Core focused contract tests."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import domain, leader, scheduler, storage

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = NOW

    def now(self):
        return self.value


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = Path(self.temp.name) / "db.sqlite"
        storage.migrate_to_head(db)
        self.conn = storage.connect(db)
        self.clock = Clock()
        self.project = domain.create_project(self.conn, "p", "/repo/p", clock=self.clock)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def task(self, name, *, priority=50, ownership=(), created_at=None):
        task = domain.create_task(
            self.conn, self.project.id, name, priority=priority,
            ownership_json=__import__("json").dumps(list(ownership)),
            clock=self.clock,
        )
        if created_at is not None:
            with storage.transaction(self.conn):
                self.conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (created_at, task.id))
        task = domain.get_task(self.conn, task.id)
        if task.status == "pending":
            task = domain.apply_task_event(self.conn, task.id, "dependencies_satisfied",
                                           expected_version=task.version, clock=self.clock)
        return domain.get_task(self.conn, task.id)

    def active_member(self, name, claims, *, task_id=None, read_only=False, phase="agent_started"):
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO members (name,role,responsibility,kind,task,ownership_json,read_only,cwd,phase,created_at,updated_at,task_id) "
                "VALUES (?, 'engineer','x','codex','x',?,?, '/repo/p',?,0,0,?)",
                (name, __import__("json").dumps(claims), int(read_only), phase, task_id),
            )

    def test_lease_competition_renewal_and_expired_epoch_fencing(self):
        a = scheduler.acquire_or_renew(self.conn, self.project.id, "one", ttl_seconds=10, clock=self.clock)
        with self.assertRaises(leader.LeaderBusyError):
            scheduler.acquire_or_renew(self.conn, self.project.id, "two", ttl_seconds=10, clock=self.clock)
        self.clock.value += timedelta(seconds=2)
        renewed = scheduler.acquire_or_renew(self.conn, self.project.id, "one", ttl_seconds=10, clock=self.clock)
        self.assertEqual(renewed.epoch, a.epoch)
        self.clock.value += timedelta(seconds=11)
        b = scheduler.acquire_or_renew(self.conn, self.project.id, "two", ttl_seconds=10, clock=self.clock)
        self.assertGreater(b.epoch, a.epoch)
        task = self.task("fenced", ownership=["src/fenced/**"])
        with self.assertRaises(leader.StaleLeaderError):
            scheduler.dispatch_ready(self.conn, self.project.id, renewed,
                                     concurrency_limit=1, clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")

    def test_deterministic_order_quota_and_explicit_deferred_reason(self):
        late = self.task("late", priority=7, created_at="2026-11-04T12:00:02+00:00")
        early_b = self.task("early-b", priority=7, created_at="2026-11-04T12:00:01+00:00")
        early_a = self.task("early-a", priority=7, created_at="2026-11-04T12:00:01+00:00")
        top = self.task("top", priority=80)
        result = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertEqual(result.selected_task_ids, (top.id, min(early_a.id, early_b.id)))
        deferred = {item.task_id: item.reason for item in result.deferred}
        self.assertEqual(deferred[max(early_a.id, early_b.id)], "concurrency_quota")
        self.assertEqual(deferred[late.id], "concurrency_quota")

    def test_ownership_conflicts_overlap_but_non_overlap_and_read_only_do_not(self):
        overlap = self.task("overlap", ownership=["src/shared/file.py"])
        independent = self.task("independent", ownership=["docs/**"])
        self.active_member("writer", ["src/shared/**"], task_id=overlap.id)
        result = scheduler.plan(self.conn, self.project.id, concurrency_limit=4)
        self.assertEqual(result.selected_task_ids, (independent.id,))
        self.assertEqual(result.deferred[0].task_id, overlap.id)
        self.assertEqual(result.deferred[0].reason, "ownership_conflict")
        with storage.transaction(self.conn):
            self.conn.execute("DELETE FROM members WHERE name='writer'")
        self.active_member("auditor", ["src/shared/**"], task_id=overlap.id, read_only=True)
        result = scheduler.plan(self.conn, self.project.id, concurrency_limit=4)
        self.assertIn(overlap.id, result.selected_task_ids)

    def test_null_task_active_writable_member_is_conservative_owner(self):
        blocked = self.task("null-owner-overlap", ownership=["src/private/a.py"])
        safe = self.task("null-owner-safe", ownership=["docs/**"])
        self.active_member("unlinked-writer", ["src/private/**"])
        # A task-linked member from another project must not leak into this plan.
        other = domain.create_project(self.conn, "other", "/repo/other", clock=self.clock)
        other_task = domain.create_task(self.conn, other.id, "other", ownership_json='["docs/**"]', clock=self.clock)
        domain.apply_task_event(self.conn, other_task.id, "dependencies_satisfied",
                                expected_version=other_task.version, clock=self.clock)
        self.active_member("other-writer", ["docs/**"], task_id=other_task.id)
        result = scheduler.plan(self.conn, self.project.id, concurrency_limit=4)
        reasons = {item.task_id: item.reason for item in result.deferred}
        self.assertEqual(reasons[blocked.id], "ownership_conflict")
        self.assertIn(safe.id, result.selected_task_ids)

    def test_read_only_active_member_with_occupying_attempt_does_not_own_paths(self):
        occupied = self.task("audit", ownership=["src/a/**"])
        domain.create_attempt(self.conn, occupied.id, expected_version=occupied.version, clock=self.clock)
        ready = self.task("writer", ownership=["src/a/file.py"])
        self.active_member("auditor", ["src/a/**"], task_id=occupied.id, read_only=True)
        at_limit = scheduler.plan(self.conn, self.project.id, concurrency_limit=1)
        deferred = {item.task_id: item.reason for item in at_limit.deferred}
        self.assertEqual(deferred[ready.id], "concurrency_quota")
        self.assertNotEqual(deferred[ready.id], "ownership_conflict")
        with_room = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertIn(ready.id, with_room.selected_task_ids)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (occupied.id,)
        ).fetchone()[0], 1)  # Occupancy remains in the quota calculation.

    def test_malformed_ownership_claims_fail_closed(self):
        bad = self.task("bad-ownership", ownership=[""])
        with self.assertRaisesRegex(ValueError, "invalid ownership claim"):
            scheduler.plan(self.conn, self.project.id, concurrency_limit=1)
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET ownership_json=? WHERE id=?",
                              ('["src/../secret"]', bad.id))
        with self.assertRaisesRegex(ValueError, "invalid ownership claim"):
            scheduler.plan(self.conn, self.project.id, concurrency_limit=1)

    def test_dispatch_reserves_with_fence_and_read_only_plan_does_not_mutate(self):
        first = self.task("first", ownership=["src/a/**"])
        second = self.task("second", ownership=["src/b/**"])
        before = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        p = scheduler.plan(self.conn, self.project.id, concurrency_limit=2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], before)
        lease = scheduler.acquire_or_renew(self.conn, self.project.id, "sched", clock=self.clock)
        dispatched = scheduler.dispatch_ready(self.conn, self.project.id, lease,
                                              concurrency_limit=2, clock=self.clock)
        self.assertEqual(len(dispatched.attempts), 2)
        self.assertEqual({a.task_id for a in dispatched.attempts}, {first.id, second.id})
        self.assertEqual(domain.get_task(self.conn, first.id).status, "running")

    def test_dispatch_enforces_attempt_cap_and_escalates_ready_and_running(self):
        ready = self.task("cap-ready")
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET max_attempts=1 WHERE id=?", (ready.id,))
            self.conn.execute(
                "INSERT INTO attempts(id,task_id,attempt_no,status) VALUES('old-ready',?,1,'failed')",
                (ready.id,),
            )
        lease = scheduler.acquire_or_renew(self.conn, self.project.id, "capped", clock=self.clock)
        result = scheduler.dispatch_ready(self.conn, self.project.id, lease, concurrency_limit=2, clock=self.clock)
        self.assertEqual(result.attempts, ())
        self.assertEqual(domain.get_task(self.conn, ready.id).status, "needs_human")

        running = self.task("cap-running")
        active = domain.create_attempt(self.conn, running.id, expected_version=running.version, clock=self.clock)
        domain.transition_attempt(self.conn, active.id, "start", clock=self.clock)
        domain.transition_attempt(self.conn, active.id, "activated", clock=self.clock)
        domain.transition_attempt(self.conn, active.id, "fail", clock=self.clock)
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET max_attempts=1 WHERE id=?", (running.id,))
        result = scheduler.dispatch_ready(self.conn, self.project.id, lease, concurrency_limit=2, clock=self.clock)
        self.assertEqual(domain.get_task(self.conn, running.id).status, "needs_human")

    def test_scheduler_long_lived_acquire_renew_tick_release_interface(self):
        task = self.task("tick", ownership=["src/tick/**"])
        service = scheduler.Scheduler(
            self.conn, self.project.id, "scheduler", concurrency_limit=1,
            ttl_seconds=20, clock=self.clock,
        )
        first = service.acquire_or_renew()
        self.assertEqual(service.renew().epoch, first.epoch)
        result = service.tick()
        self.assertEqual([attempt.task_id for attempt in result.attempts], [task.id])
        self.assertTrue(service.release())
        replacement = scheduler.Scheduler(
            self.conn, self.project.id, "replacement", concurrency_limit=1,
            ttl_seconds=20, clock=self.clock,
        ).acquire_or_renew()
        self.assertGreater(replacement.epoch, first.epoch)

    def test_plan_uses_one_read_snapshot_without_mutating(self):
        task = self.task("snapshot", ownership=["src/snapshot/**"])
        other = storage.connect(Path(self.temp.name) / "db.sqlite")
        try:
            original = scheduler._active_owners
            observed = []

            def add_owner_after_queue(conn, project_id):
                observed.append(conn.in_transaction)
                with storage.transaction(other):
                    other.execute(
                        "INSERT INTO members (name,role,responsibility,kind,task,ownership_json,read_only,cwd,phase,created_at,updated_at) "
                        "VALUES ('late-writer','engineer','x','codex','x','[\"src/snapshot/**\"]',0,'/repo/p','agent_started',0,0)"
                    )
                return original(conn, project_id)

            with mock.patch.object(scheduler, "_active_owners", side_effect=add_owner_after_queue):
                result = scheduler.plan(self.conn, self.project.id, concurrency_limit=1)
            self.assertEqual(observed, [True])
            # Queue was read before the concurrent ownership commit, and the
            # following ownership read stays in that same SQLite snapshot.
            self.assertIn(task.id, result.selected_task_ids)
            self.assertFalse(self.conn.in_transaction)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
