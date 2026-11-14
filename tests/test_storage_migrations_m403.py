"""M4-03 schema-v17 migration and integrity contract tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from teamcraft import storage
from teamcraft.errors import StorageError


class M403StorageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m403-storage-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)

    def _downgrade_to_v16(self) -> None:
        raw = sqlite3.connect(self.db)
        try:
            for trigger in storage._V17_TRIGGERS:
                raw.execute(f'DROP TRIGGER "{trigger}"')
            for index in storage._V17_INDEXES:
                raw.execute(f'DROP INDEX "{index}"')
            raw.execute("DROP TABLE worktree_verifications")
            raw.execute("DROP TABLE verifier_profiles")
            raw.execute("DELETE FROM schema_migrations WHERE version=17")
            raw.execute("PRAGMA user_version=16")
            raw.commit()
        finally:
            raw.close()

    def test_v16_upgrades_atomically_to_complete_v17(self):
        self._downgrade_to_v16()
        report = storage.migrate_to_head(
            self.db, allow_unverified_location=True)
        self.assertEqual((report.from_version, report.to_version, report.applied),
                         (16, 17, (17,)))
        verified = storage.verify_database(self.db)
        self.assertTrue(verified["at_head"])
        self.assertEqual(verified["user_version"], 17)
        self.assertTrue(storage._V17_TABLES.issubset(set(verified["tables"])))
        self.assertEqual(verified["missing_version_triggers"], [])
        self.assertEqual(verified["missing_version_indexes"], [])

    def test_preexisting_v17_table_refuses_upgrade_without_partial_publish(self):
        self._downgrade_to_v16()
        raw = sqlite3.connect(self.db)
        raw.execute("CREATE TABLE verifier_profiles (foreign_data TEXT)")
        raw.commit()
        raw.close()
        with self.assertRaises(StorageError):
            storage.migrate_to_head(self.db, allow_unverified_location=True)
        raw = sqlite3.connect(self.db)
        try:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 16)
            self.assertIsNone(raw.execute(
                "SELECT 1 FROM schema_migrations WHERE version=17").fetchone())
            self.assertIsNone(raw.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='worktree_verifications'").fetchone())
            self.assertEqual(raw.execute(
                "PRAGMA table_info(verifier_profiles)").fetchall()[0][1],
                "foreign_data")
        finally:
            raw.close()


if __name__ == "__main__":
    unittest.main()
