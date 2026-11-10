"""M2-05 schema v6 to v7 migration and head validation."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from teamcraft import storage


class SchemaV7Tests(unittest.TestCase):
    def test_v6_rows_receive_default_and_v7_constraint_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            storage.migrate_to_head(path)
            raw = sqlite3.connect(path)
            raw.execute("INSERT INTO projects(id,name,repo_path,created_at) VALUES('p','p','/p','now')")
            raw.execute("INSERT INTO tasks(id,project_id,title,status,created_at,updated_at) VALUES('t','p','t','pending','now','now')")
            for trigger in storage._V8_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V8_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {index}")
            for table in ("approvals", "verifications", "verification_configs"):
                raw.execute(f"DROP TABLE IF EXISTS {table}")
            for trigger in storage._V9_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V9_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {index}")
            raw.execute("DROP TABLE IF EXISTS ceo_initializations")
            for trigger in storage._V10_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V10_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {index}")
            for table in ("wake_events", "wakes", "wake_cursors"):
                raw.execute(f"DROP TABLE IF EXISTS {table}")
            for trigger in storage._V11_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            columns = {row[1] for row in raw.execute("PRAGMA table_info(ceo_initializations)")}
            if "receipt_source" in columns:
                raw.execute("ALTER TABLE ceo_initializations DROP COLUMN receipt_source")
            raw.execute("ALTER TABLE tasks DROP COLUMN max_attempts")
            # M3-04 (v15): honest v6 sheds the dispatch gate artifacts too.
            raw.execute("DROP TABLE IF EXISTS scheduler_controls")
            task_columns = {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}
            for column in ("paused", "execution_policy_json"):
                if column in task_columns:
                    raw.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
            raw.execute("DELETE FROM schema_migrations WHERE version>6")
            raw.execute("PRAGMA user_version=6")
            raw.commit(); raw.close()
            storage.migrate_to_head(path)
            conn = storage.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT max_attempts FROM tasks WHERE id='t'").fetchone()[0], 3)
                with self.assertRaises(sqlite3.IntegrityError):
                    with storage.transaction(conn):
                        conn.execute("UPDATE tasks SET max_attempts=0 WHERE id='t'")
            finally:
                conn.close()

    def test_malformed_v7_missing_check_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            storage.migrate_to_head(path)
            raw = sqlite3.connect(path)
            ddl = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()[0]
            raw.close()
            # Python and CLI SQLite builds can both enable defensive mode,
            # making writable_schema unavailable even in a test process.  The
            # schema text is stored verbatim in the database page, so replace
            # the one unique CHECK fragment in-place after every connection is
            # closed. Equal-length spaces keep the file layout intact while
            # removing the constraint from the stored DDL.
            needle = b" CHECK (max_attempts >= 1)"
            data = path.read_bytes()
            self.assertEqual(data.count(needle), 1)
            offset = data.index(needle)
            with path.open("r+b") as database:
                database.seek(offset)
                database.write(b" " * len(needle))
                database.flush()
            report = storage.verify_database(path)
            self.assertFalse(report["schema_usable"])
            self.assertIn("tasks.max_attempts:check", report["missing_head_columns"])


if __name__ == "__main__": unittest.main()
