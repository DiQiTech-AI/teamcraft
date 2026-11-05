from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import briefs, domain, events, storage, watcher
from teamcraft.errors import StorageError


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 5, tzinfo=timezone.utc)
    def now(self): return self.value
    def advance(self, seconds: int) -> None: self.value += timedelta(seconds=seconds)


@dataclass(frozen=True)
class Brief:
    task_id: str
    attempt_id: str
    deadline_at: str | None = None
    no_progress_timeout_seconds: int = 60
    poll_interval_seconds: int = 10
    brief_digest: str = "brief"


class WatcherM204Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "db.sqlite"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        project = domain.create_project(self.conn, "p", "/repo", clock=self.clock)
        task = domain.create_task(self.conn, project.id, "t", clock=self.clock)
        domain.recompute_ready(self.conn, clock=self.clock)
        task = domain.get_task(self.conn, task.id)
        self.attempt = domain.create_attempt(self.conn, task.id, expected_version=task.version, clock=self.clock)
        domain.transition_attempt(self.conn, self.attempt.id, "start", clock=self.clock)
        self.attempt = domain.transition_attempt(self.conn, self.attempt.id, "activated", clock=self.clock)
        self.session = domain.register_session(self.conn, self.attempt.id, "herdr", native_session_id="native", pane_id="pane", clock=self.clock)
        self.brief = Brief(task.id, self.attempt.id)

    def _brief(self, conn, attempt_id): return self.brief
    def _none(self, conn, attempt_id): return None
    def _observation(self, **kwargs):
        values = dict(session_id=self.session.id, state="working", pane_id="pane", native_session_id="native", identity_verified=True)
        values.update(kwargs)
        return watcher.SessionObservation(**values)

    def test_repeat_does_not_turn_logless_poll_into_activity_or_progress(self) -> None:
        first = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.clock.advance(10)
        second = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.assertIn("watch.agent_activity", first.events_inserted)
        self.assertEqual(second.events_inserted, ())
        self.assertIsNone(second.last_progress_at)
        self.assertEqual(events.current_seq(self.conn), 3)

    def test_identity_mismatch_fails_closed_to_unknown(self) -> None:
        result = watcher.record_observation(self.conn, self.attempt.id, self._observation(pane_id="wrong"), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.assertEqual(result.observed_state, "unknown")
        self.assertIsNone(result.agent_active)
        self.assertEqual(domain.get_session(self.conn, self.session.id).state, "unknown")

    def test_verified_working_emits_executing_once(self) -> None:
        watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.clock.advance(5)
        replay = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.assertIn("attempt.executing", self.conn.execute("SELECT kind FROM events WHERE attempt_id=?", (self.attempt.id,)).fetchall()[2])
        self.assertNotIn("attempt.executing", replay.events_inserted)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='attempt.executing'").fetchone()[0], 1)

    def test_idle_to_working_cycle_records_new_activity_with_predecessor(self) -> None:
        first = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.clock.advance(5)
        watcher.record_observation(self.conn, self.attempt.id, self._observation(state="idle"), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.clock.advance(5)
        second = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock, brief_provider=self._brief, progress_provider=self._none)
        self.assertNotEqual(first.last_activity_at, second.last_activity_at)
        activity = self.conn.execute("SELECT payload_json FROM events WHERE kind='watch.agent_activity' ORDER BY seq").fetchall()
        self.assertEqual(len(activity), 3)
        self.assertIn('"predecessor"', str(activity[-1][0]))

    def test_no_progress_and_deadline_thresholds_are_boundary_idempotent(self) -> None:
        deadline = (self.clock.now() + timedelta(seconds=20)).isoformat()
        self.brief = Brief(self.attempt.task_id, self.attempt.id, deadline_at=deadline,
            no_progress_timeout_seconds=20)
        before = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock,
            brief_provider=self._brief, progress_provider=self._none)
        self.assertFalse(before.stalled)
        self.assertFalse(before.deadline_exceeded)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind IN ('watch.no_progress_threshold', 'watch.deadline_exceeded')").fetchone()[0], 0)
        self.clock.advance(20)
        at_boundary = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock,
            brief_provider=self._brief, progress_provider=self._none)
        self.assertTrue(at_boundary.stalled)
        self.assertTrue(at_boundary.deadline_exceeded)
        self.assertEqual(set(at_boundary.events_inserted), {"watch.no_progress_threshold", "watch.deadline_exceeded"})
        self.clock.advance(5)
        repeated = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock,
            brief_provider=self._brief, progress_provider=self._none)
        self.assertNotIn("watch.no_progress_threshold", repeated.events_inserted)
        self.assertNotIn("watch.deadline_exceeded", repeated.events_inserted)
        counts = dict(self.conn.execute("SELECT kind, COUNT(*) FROM events WHERE kind IN ('watch.no_progress_threshold', 'watch.deadline_exceeded') GROUP BY kind"))
        self.assertEqual(counts, {"watch.deadline_exceeded": 1, "watch.no_progress_threshold": 1})

    def test_late_working_poll_cannot_follow_execution_finished(self) -> None:
        with storage.transaction(self.conn):
            domain.transition_attempt(self.conn, self.attempt.id, "finish", clock=self.clock)
            events.append_event(self.conn, "attempt.execution_finished", task_id=self.attempt.task_id,
                attempt_id=self.attempt.id, payload_json="{}", clock=self.clock)
        result = watcher.record_observation(self.conn, self.attempt.id, self._observation(), clock=self.clock,
            brief_provider=self._brief, progress_provider=self._none)
        self.assertNotIn("attempt.executing", result.events_inserted)
        kinds = [row[0] for row in self.conn.execute("SELECT kind FROM events WHERE attempt_id=? ORDER BY seq", (self.attempt.id,))]
        self.assertEqual(kinds[-1], "watch.agent_activity")
        self.assertEqual(kinds.count("attempt.executing"), 0)

    def test_default_adapter_probe_fails_closed_when_runtime_profile_missing(self) -> None:
        briefs.create_attempt_brief(self.conn, self.attempt.id, content="do work", clock=self.clock)
        self.clock.advance(60)
        class Adapter:
            def get_pane(self, value): raise AssertionError("must not call adapter without profile")
        result = watcher.poll_once(self.conn, self.attempt.id, adapter=Adapter(), clock=self.clock)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].observed_state, "unknown")

    def test_default_adapter_probe_checks_full_runtime_identity_before_working(self) -> None:
        briefs.create_attempt_brief(self.conn, self.attempt.id, content="do work", clock=self.clock)
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO members (name, role, responsibility, kind, task, cwd, pane_id, terminal_id, "
                "phase, task_id, attempt_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("worker", "worker", "test", "herdr", "t", "/repo", "pane", "term", "active", self.attempt.task_id, self.attempt.id, self.session.id, 0, 0),
            )
        self.clock.advance(60)
        class Adapter:
            def get_pane(self, pane): return {"pane_id": pane, "terminal_id": "term"}
            def get_agent(self, name): return {"pane_id": "pane", "agent_session": {"value": "native"}, "agent_status": "working"}
            def process_info(self, pane): return {"result": {"alive": True, "terminal_id": "term"}}
        result = watcher.poll_once(self.conn, self.attempt.id, adapter=Adapter(), clock=self.clock)
        self.assertEqual(result[0].observed_state, "working")
        self.assertIn("attempt.executing", result[0].events_inserted)

    def test_deterministic_event_key_returns_original_and_rejects_collision(self) -> None:
        one, inserted = events.append_event_idempotent(self.conn, "watch.test", event_id="fixed", payload_json='{"a":1}', clock=self.clock)
        self.clock.advance(10)
        two, replay = events.append_event_idempotent(self.conn, "watch.test", event_id="fixed", payload_json='{"a":1}', clock=self.clock)
        self.assertTrue(inserted)
        self.assertFalse(replay)
        self.assertEqual((one.seq, one.occurred_at), (two.seq, two.occurred_at))
        with self.assertRaises(StorageError):
            events.append_event_idempotent(self.conn, "watch.test", event_id="fixed", payload_json='{"a":2}', clock=self.clock)

    def test_default_m203_providers_and_poll_interval_probe(self) -> None:
        briefs.create_attempt_brief(self.conn, self.attempt.id, content="do work", clock=self.clock)
        calls = []
        def probe(adapter, snapshot):
            calls.append(snapshot["session_id"])
            return self._observation()
        # Session registration just observed now, so the 60-second default
        # cadence refuses I/O until due.
        self.assertEqual(watcher.poll_once(self.conn, self.attempt.id, adapter=object(), probe=probe, clock=self.clock), ())
        self.clock.advance(60)
        result = watcher.poll_once(self.conn, self.attempt.id, adapter=object(), probe=probe, clock=self.clock)
        self.assertEqual(calls, [self.session.id])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].last_progress_at)

    def test_two_connections_write_one_deterministic_event(self) -> None:
        barrier = threading.Barrier(2)
        outcome = []
        def write() -> None:
            conn = storage.connect(self.db)
            try:
                barrier.wait()
                event, inserted = events.append_event_idempotent(conn, "watch.concurrent", event_id="same-key", payload_json="{}")
                outcome.append((event.seq, inserted))
            finally:
                conn.close()
        threads = [threading.Thread(target=write), threading.Thread(target=write)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(inserted for _, inserted in outcome), [False, True])
        self.assertEqual({seq for seq, _ in outcome}, {1})
