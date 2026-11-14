"""M2-09 durable Wake inbox tests; no Herdr prompts are sent."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import domain, events, leader, scheduler, storage, wake


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds=1):
        self.value += timedelta(seconds=seconds)


class WakeM209Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m209-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        project = domain.create_project(self.conn, "wake-test", str(Path(self.tmp.name) / "repo"),
                                        clock=self.clock)
        self.project_id = project.id
        self.lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "wake-test", ttl_seconds=60,
            clock=self.clock,
        )

    def event(self, kind, *, priority=None, dedupe_key=None, task_id=None, seconds=0):
        if seconds:
            self.clock.advance(seconds)
        payload = {}
        if priority is not None:
            payload["priority"] = priority
        if dedupe_key is not None:
            payload["dedupe_key"] = dedupe_key
        return events.append_event(
            self.conn, kind, project_id=self.project_id, task_id=task_id,
            payload_json=json.dumps(payload), clock=self.clock,
        )

    def test_schema_v10_is_complete_and_wake_integrity_triggers_are_present(self):
        self.assertEqual(storage.SCHEMA_VERSION, 17)
        audit = storage.verify_database(self.db)
        self.assertTrue(audit["at_head"])
        self.assertTrue({"wake_cursors", "wakes", "wake_events"} <= set(audit["tables"]))
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('trigger','index')"
        ).fetchall()
        names = {str(row[0]) for row in rows}
        self.assertTrue(storage._V10_TRIGGERS <= names)
        self.assertTrue(storage._V10_INDEXES <= names)

    def test_v11_database_migrates_forward_to_head(self):
        # Build a genuine v11 database by replaying the forward-only ladder,
        # then prove every later step applies cleanly from true v11 physical
        # state.
        v11_db = Path(self.tmp.name) / "v11-forward.db"
        raw = sqlite3.connect(v11_db)
        try:
            for version, statements in storage._MIGRATION_STEPS:
                if version > 11:
                    break
                for statement in statements:
                    raw.execute(statement)
                raw.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, "2026-11-06T00:00:00+00:00"),
                )
                raw.execute(f"PRAGMA user_version={version}")
            raw.commit()
        finally:
            raw.close()
        result = storage.migrate_to_head(v11_db, allow_unverified_location=True)
        self.assertEqual((result.from_version, result.to_version, result.applied),
                         (11, storage.SCHEMA_VERSION,
                          tuple(range(12, storage.SCHEMA_VERSION + 1))))
        self.assertTrue(storage.verify_database(v11_db)["at_head"])

    def test_ingest_merges_same_dedupe_window_and_keeps_highest_priority(self):
        first = self.event("watch.task_progress", priority=20, dedupe_key="task:abc")
        self.clock.advance(2)
        second = self.event("recovery.escalated", priority=100, dedupe_key="task:abc")
        result = wake.ingest_events(self.conn, self.project_id, self.lease,
                                    window_seconds=30, clock=self.clock)
        self.assertEqual((result.scanned_events, result.created_wakes, result.merged_events), (2, 1, 1))
        self.assertEqual(result.cursor.scanned_seq, second.seq)
        self.assertEqual(result.cursor.acknowledged_seq, 0)
        self.assertEqual(result.cursor.processed_seq, 0)
        self.assertEqual(len(result.wakes), 2)
        item = result.wakes[-1]
        self.assertEqual((item.first_seq, item.last_seq, item.event_count, item.priority),
                         (first.seq, second.seq, 2, 100))
        self.assertEqual(item.state, "pending")
        links = self.conn.execute(
            "SELECT event_seq FROM wake_events WHERE wake_id=? ORDER BY event_seq", (item.id,),
        ).fetchall()
        self.assertEqual([int(row[0]) for row in links], [first.seq, second.seq])

    def test_replay_and_restart_do_not_duplicate_wakes_or_event_links(self):
        self.event("task.updated", dedupe_key="task:1")
        first = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock)
        self.conn.close()
        self.conn = storage.connect(self.db)
        second = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock)
        self.assertEqual(first.created_wakes, 1)
        self.assertEqual((second.scanned_events, second.created_wakes, second.merged_events), (0, 0, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_events").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0)

    def test_different_time_windows_create_separate_wakes_and_ineligible_events_advance_cursor(self):
        first = self.event("task.updated", dedupe_key="task:1")
        self.clock.advance(31)
        second = self.event("task.updated", dedupe_key="task:1")
        ignored = self.event("wake.acknowledged", dedupe_key="internal")
        result = wake.ingest_events(self.conn, self.project_id, self.lease,
                                    window_seconds=30, clock=self.clock)
        self.assertEqual(result.cursor.scanned_seq, ignored.seq)
        self.assertEqual((result.created_wakes, result.merged_events), (2, 0))
        rows = wake.list_wakes(self.conn, self.project_id)
        self.assertEqual([item.first_seq for item in rows], [first.seq, second.seq])
        self.assertEqual({item.last_seq for item in rows}, {first.seq, second.seq})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_events").fetchone()[0], 2)

    def test_page_limit_persists_cursor_and_next_tick_finishes_remaining_events(self):
        self.event("task.updated", dedupe_key="one")
        self.clock.advance(1)
        self.event("task.updated", dedupe_key="two")
        first = wake.ingest_events(self.conn, self.project_id, self.lease, limit=1, clock=self.clock)
        second = wake.ingest_events(self.conn, self.project_id, self.lease, limit=1, clock=self.clock)
        self.assertEqual((first.scanned_events, second.scanned_events), (1, 1))
        self.assertLess(first.cursor.scanned_seq, second.cursor.scanned_seq)
        self.assertEqual(second.cursor.scanned_seq, events.current_seq(self.conn))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_events").fetchone()[0], 2)

    def test_stale_leader_rolls_back_new_wakes_and_scan_cursor(self):
        self.event("task.updated", dedupe_key="task:1")
        old = self.lease
        self.clock.advance(61)
        renewed = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "new-leader", ttl_seconds=60,
            clock=self.clock,
        )
        self.assertGreater(renewed.epoch, old.epoch)
        with self.assertRaises(leader.StaleLeaderError):
            wake.ingest_events(self.conn, self.project_id, old, clock=self.clock)
        self.assertIsNone(wake.get_cursor(self.conn, self.project_id))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0], 0)

    def test_cursor_schema_rejects_regression_and_out_of_order_ack_processing(self):
        wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock)
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "UPDATE wake_cursors SET acknowledged_seq=1 WHERE project_id=?",
                    (self.project_id,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "UPDATE wake_cursors SET scanned_seq=-1 WHERE project_id=?",
                    (self.project_id,),
                )

    def test_acknowledgement_and_processing_fail_closed_without_m208_receipt(self):
        self.event("task.updated", dedupe_key="task:1")
        result = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock)
        wake_id = result.wakes[0].id
        with self.assertRaises(Exception) as ack_error:
            wake.acknowledge_wake(self.conn, self.project_id, wake_id, self.lease,
                                  target_session_id="native", acknowledged_through_seq=1,
                                  clock=self.clock)
        self.assertIn("initialization", str(ack_error.exception).lower())
        with self.assertRaises(Exception) as process_error:
            wake.mark_processed(self.conn, self.project_id, wake_id, self.lease, clock=self.clock)
        self.assertIn("initialization", str(process_error.exception).lower())
        self.assertEqual(wake.list_wakes(self.conn, self.project_id)[0].state, "pending")
        cursor = wake.get_cursor(self.conn, self.project_id)
        self.assertEqual((cursor.acknowledged_seq, cursor.processed_seq), (0, 0))

    def test_admission_fails_closed_before_command_creation_without_m208_receipt(self):
        self.event("task.updated", dedupe_key="task:1")
        result = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock)
        wake_id = result.wakes[0].id
        with self.assertRaisesRegex(Exception, "initialization"):
            wake.admit_pending_wake(self.conn, self.project_id, wake_id, self.lease, clock=self.clock)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0)
        item = wake.list_wakes(self.conn, self.project_id)[0]
        self.assertEqual((item.state, item.target_session_id, item.command_id), ("pending", None, None))

    def test_scheduler_tick_persists_event_intake_before_ceo_gate_refusal(self):
        source = self.event("task.updated", dedupe_key="task:tick")
        runner = scheduler.Scheduler(self.conn, self.project_id, "wake-test",
                                     concurrency_limit=1, clock=self.clock)
        with self.assertRaises(Exception):
            runner.tick()
        cursor = wake.get_cursor(self.conn, self.project_id)
        self.assertEqual(cursor.scanned_seq, source.seq)
        self.assertEqual(wake.list_wakes(self.conn, self.project_id)[0].last_seq, source.seq)


if __name__ == "__main__":
    unittest.main()
