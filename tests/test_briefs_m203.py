import tempfile
import unittest
from pathlib import Path

from teamcraft import briefs, domain, storage
from teamcraft.errors import ConflictError


class BriefM203Tests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "brief.db"
        storage.migrate_to_head(self.path, allow_unverified_location=True)
        self.conn = storage.connect(self.path, allow_unverified_location=True)
        p = domain.create_project(self.conn, "p", "/tmp/m203-brief")
        t = domain.create_task(self.conn, p.id, title="t")
        t = domain.apply_task_event(self.conn, t.id, "dependencies_satisfied", expected_version=t.version)
        self.attempt = domain.create_attempt(self.conn, t.id, expected_version=t.version)

    def tearDown(self): self.conn.close()

    def test_snapshot_is_fixed_and_default_poll_is_bound(self):
        brief = briefs.create_attempt_brief(self.conn, self.attempt.id, content="work")
        self.assertEqual(brief.poll_interval_seconds, 60)
        self.assertEqual(brief.no_progress_timeout_seconds, 3600)
        self.assertEqual(brief, briefs.create_attempt_brief(self.conn, self.attempt.id, content="work"))
        with self.assertRaises(ConflictError):
            briefs.create_attempt_brief(self.conn, self.attempt.id, content="changed")
        with self.assertRaises(Exception):
            with storage.transaction(self.conn):
                self.conn.execute("UPDATE attempt_briefs SET content='x' WHERE attempt_id=?", (self.attempt.id,))

    def test_deadline_rejects_naive_and_invalid_iso(self):
        for deadline in ("2026-11-05T12:00:00", "not-a-timestamp"):
            with self.assertRaises(Exception):
                briefs.create_attempt_brief(self.conn, self.attempt.id, content="work", deadline_at=deadline)
