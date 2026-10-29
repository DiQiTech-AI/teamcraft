"""M1-03 SQLite repository/migration v1 contract tests.

All databases live inside ``tempfile.TemporaryDirectory``.  Nothing here reads,
writes, or migrates the repository's real runtime directories, and no Herdr
session is contacted.

Beyond the baseline contract, this suite pins the M1-03 review findings:

* file URIs are percent-encoded, so ``#``/``?``/``%`` targets hit *exactly*
  the intended file and never a truncated decoy (round 1 P0-1);
* read-only gates are strict ``mode=ro`` only — ``immutable=1`` is banned
  from the module because it ignores committed WAL frames and returns stale
  content on live databases; when even strict read-only fails, every entry
  point fails closed (round 2 P0);
* a ``user_version`` committed into the WAL gates ``connect``/
  ``migrate_to_head``/``verify_database``/``restore_backup`` even when the
  main file is stale (round 2 P0);
* crash-mid-migration kills the *real* ``migrate_to_head`` mid-DDL via the
  auditable ``MIGRATION_STATEMENT_HOOK`` seam (round 2 B);
* restore cross-verifies the snapshot itself (user_version/schema_migrations,
  WAL-aware) against a complete manifest (all required fields present and
  consistent) and removes any half-created destination on failure;
* concurrent ``migrate_to_head`` calls converge via re-checks inside one
  ``BEGIN IMMEDIATE`` transaction (round 1 A);
* created/starting/active/unknown all occupy the single-attempt slot,
  case-insensitively (round 1 C);
* retention never deletes generation-unbound or protected evidence; remote
  mounts are rejected and ``unknown`` locations need the explicit auditable
  ``allow_unverified_location`` acknowledgement — WAL success is never
  treated as locality proof (round 1 D + round 2);
* ``_open_read_connection`` propagates caller errors instead of re-yielding
  (round 1 E);
* public write paths are refused by SQLite itself, not by a SQL-text parser:
  reads run on a strict ``mode=ro`` channel, the hidden writable connection
  carries a permanent ``set_authorizer`` denying every mutating action code
  outside the gate, chained cursors stay controlled facades, and
  WITH-prefixed DML / ``INSERT ... RETURNING`` / executemany / executescript
  / writable PRAGMA forms all fail before touching data — before *and*
  after a concurrent v99 bump; re-preparing identical write text outside
  the gate cannot smuggle through the statement cache (round 5);
* the page-level SQLite Online Backup API — which runs no SQL and fires no
  authorizer — is not exposed on the facade at all: ``a.backup(b)`` (or any
  path/None argument) raises ``BackupError`` without touching either file;
  ``backup_database``/``restore_backup`` copy only into freshly O_EXCL-created
  targets, never overwrite existing or too-new databases, and failed copies
  remove only files the failing call itself created (round 6);
* failure cleanup is inode-token based: every artefact (backup db, manifest,
  restore dest, WAL/SHM sidecars) is deleted only when this call created it
  AND the path still holds that same inode, so a process that wins the name
  race — before O_EXCL, mid-copy, or at manifest write — keeps its files
  byte-identical; the manifest is likewise created O_EXCL and written through
  its own fd (round 7);
* backup success now implies restore acceptance: a single shared predicate
  set (complete v1 tables + schema_migrations audit record + version
  agreement, applied to the source before copying and to the copy after)
  gates backup_database with exactly what restore_backup will demand, so a
  plain sqlite3-created empty database is refused identically by both sides
  and leaves nothing behind (round 8);
* WAL/SHM sidecars are never deleted by any cleanup path: the main file's
  inode token proves ownership of the main file alone, so an unprovable
  sidecar — e.g. one planted at target-wal during a failing copy by another
  process — is always kept, while the operation's own main artefact still
  passes its inode check before removal (round 9).
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from teamcraft import (
    SCHEMA_VERSION,
    BackupError,
    CorruptDatabaseError,
    MigrationError,
    SchemaTooNewError,
    StorageError,
    TeamCraftConnection,
    UnsupportedLocationError,
    WriteOutsideTransactionError,
    backup_database,
    connect,
    describe_path_support,
    migrate_to_head,
    plan_retention,
    restore_backup,
    storage,
    transaction,
    verify_database,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "schema_migrations",
    "projects",
    "tasks",
    "agents",
    "attempts",
    "sessions",
}


def _seed(conn: TeamCraftConnection | sqlite3.Connection, project_id: str = "p1") -> None:
    now = "2026-10-27T00:00:00+00:00"
    insert = (
        "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, ?)",
        (project_id, f"proj-{project_id}", f"/repos/{project_id}", now),
    )
    if getattr(conn, "in_transaction", False):
        conn.execute(*insert)
    else:
        with transaction(conn):
            conn.execute(*insert)


def _make_plain_db(path: Path, user_version: int, marker_table: str | None = None) -> None:
    """Create a database with a given user_version using a literal (non-URI) open."""

    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version={user_version}")
    if marker_table:
        conn.execute(f"CREATE TABLE {marker_table} (x TEXT)")
    conn.commit()
    conn.close()


def _sidecar_paths(path: Path) -> list[Path]:
    return [Path(str(path) + "-wal"), Path(str(path) + "-shm")]


class StorageM103Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.db = self.base / "teamcraft.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ basic
    def test_empty_database_migration_creates_v1_with_pragmas(self) -> None:
        report = migrate_to_head(self.db)
        self.assertTrue(report.fresh)
        self.assertEqual((report.from_version, report.to_version), (0, SCHEMA_VERSION))
        self.assertEqual(report.applied, (1,))

        conn = connect(self.db)
        self.assertIsInstance(conn, TeamCraftConnection)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(tables, EXPECTED_TABLES)
            rows = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
            self.assertEqual([r[0] for r in rows], [1])
            self.assertIn("+00:00", rows[0][1])  # UTC ISO-8601
            # Foreign keys are actually enforced, not just declared.
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, status, created_at, updated_at)"
                        " VALUES ('t-bad', 'nope', 'x', 'draft', 'now', 'now')"
                    )
        finally:
            conn.close()

    def test_repeat_migration_is_idempotent(self) -> None:
        first = migrate_to_head(self.db)
        conn = connect(self.db)
        _seed(conn)
        conn.close()

        second = migrate_to_head(self.db)
        third = migrate_to_head(self.db)
        self.assertFalse(second.fresh)
        self.assertEqual(second.applied, ())
        self.assertEqual(third.applied, ())

        conn = connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            conn.close()

    # ------------------------------------------------------- version protection
    def test_too_new_schema_is_refused_without_any_write(self) -> None:
        migrate_to_head(self.db)
        conn = connect(self.db)
        _seed(conn)
        with transaction(conn):
            conn.execute("PRAGMA user_version=99")
        conn.close()  # last-connection close checkpoints the WAL

        before = self.db.read_bytes()
        with self.assertRaises(SchemaTooNewError):
            migrate_to_head(self.db)
        with self.assertRaises(SchemaTooNewError):
            connect(self.db)

        after = self.db.read_bytes()
        self.assertEqual(after, before)  # guard did not touch the file
        ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            self.assertEqual(ro.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(ro.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
        finally:
            ro.close()

    def test_too_new_database_is_refused_before_any_wal_pragma(self) -> None:
        """The read-only gate must reject before WAL sidecars can be created."""
        _make_plain_db(self.db, 99, marker_table="later")  # journal=delete, no sidecars
        before = self.db.read_bytes()

        with self.assertRaises(SchemaTooNewError):
            connect(self.db)
        with self.assertRaises(SchemaTooNewError):
            migrate_to_head(self.db)

        self.assertEqual(self.db.read_bytes(), before)
        for sidecar in _sidecar_paths(self.db):
            self.assertFalse(sidecar.exists(), f"{sidecar} created by a refused open")
        # And read-only entry points genuinely see the *right* version.
        report = verify_database(self.db)
        self.assertEqual(report["user_version"], 99)
        self.assertFalse(report["ok"])

    def test_committed_wal_version_cannot_be_bypassed(self) -> None:
        """Review P0: v99 committed only into the WAL must gate every entry point.

        Main file stays at v1 (auto-checkpoint disabled, writer open); the old
        immutable=1 fallback would have read the stale main file, passed the
        gate, and handed out a writable connection actually sitting at v99.
        """
        migrate_to_head(self.db)
        # Raw sqlite connection = a rogue/legacy writer outside the facade.
        attacker = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            attacker.execute("PRAGMA wal_autocheckpoint=0")
            attacker.execute("PRAGMA user_version=99")  # committed into the WAL
            # Sanity: the truth (main+wal) reads 99 via strict ro; main alone is stale.
            ro = sqlite3.connect(storage._sqlite_uri(self.db, "mode=ro"), uri=True)
            try:
                self.assertEqual(ro.execute("PRAGMA user_version").fetchone()[0], 99)
            finally:
                ro.close()
            stale = sqlite3.connect(
                storage._sqlite_uri(self.db, "mode=ro&immutable=1"), uri=True
            )
            try:
                self.assertLess(
                    int(stale.execute("PRAGMA user_version").fetchone()[0]), 99
                )  # proof the refusal below cannot come from the main file alone
            finally:
                stale.close()

            with self.assertRaises(SchemaTooNewError):
                connect(self.db)
            with self.assertRaises(SchemaTooNewError):
                migrate_to_head(self.db)
            self.assertEqual(verify_database(self.db)["user_version"], 99)

            # Even when the environment makes strict ro fail, the answer is a
            # refusal — not a stale-immutable read, not a writable fallback.
            attempts: list[str] = []

            def fake_connect(database, *args, **kwargs):
                attempts.append(str(database))
                raise sqlite3.OperationalError("simulated ro-open failure")

            with mock.patch.object(storage.sqlite3, "connect", side_effect=fake_connect):
                with self.assertRaises(CorruptDatabaseError):
                    connect(self.db)
            self.assertTrue(attempts and all("mode=ro" in u for u in attempts))
            self.assertTrue(all("immutable" not in u for u in attempts))
        finally:
            attacker.close()

    # ------------------------------------------------------- TOCTOU (round 3)
    def test_toctou_upgrade_during_gate_caught_before_pragmas(self) -> None:
        """Review R3-1: gate sees v1, a second connection commits v99 mid-open.

        The stale gate answer must not yield a usable write entry point: the
        writable connection re-checks the version before any persistent pragma
        and refuses.
        """
        migrate_to_head(self.db)
        real_read = storage._read_user_version

        def racing_read(target: Path) -> int:
            version = real_read(target)  # honest v1 read at gate time
            attacker = sqlite3.connect(str(target))
            try:
                attacker.execute("PRAGMA user_version=99")  # committed by "another" process
                attacker.commit()
            finally:
                attacker.close()
            return version  # hand the gate its stale v1 answer, as the race would

        with mock.patch.object(storage, "_read_user_version", racing_read):
            with self.assertRaises(SchemaTooNewError):
                connect(self.db)
            with self.assertRaises(SchemaTooNewError):
                migrate_to_head(self.db)

        # The upgrade really landed, and no write path was handed out for it.
        ro = sqlite3.connect(storage._sqlite_uri(self.db, "mode=ro"), uri=True)
        try:
            self.assertEqual(ro.execute("PRAGMA user_version").fetchone()[0], 99)
        finally:
            ro.close()

    def test_transaction_gate_refuses_upgrade_after_connect(self) -> None:
        """Live two-connection interleave: upgrade after connect() returns.

        ``connect`` legitimately succeeded at v1.  A second connection then
        commits v99.  The write gate (``transaction``) re-checks under its
        exclusive lock and refuses before anything can be committed; the
        attempted insert never lands.
        """
        migrate_to_head(self.db)
        conn = connect(self.db)  # legitimately opened while the DB is at v1
        try:
            attacker = sqlite3.connect(str(self.db))
            try:
                attacker.execute("PRAGMA user_version=99")
                attacker.commit()
            finally:
                attacker.close()

            with self.assertRaises(SchemaTooNewError):
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, repo_path, created_at)"
                        " VALUES ('p-toctou', 'x', '/x', 'now')"
                    )

            # The connection remains usable (rollback path closed cleanly)...
            conn.execute("SELECT 1").fetchone()
            # ...and the guarded write never reached the database.
            probe = sqlite3.connect(str(self.db))
            try:
                self.assertEqual(
                    probe.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
                )
                self.assertEqual(probe.execute("PRAGMA user_version").fetchone()[0], 99)
            finally:
                probe.close()
        finally:
            conn.close()

    def test_public_write_entries_cannot_bypass_upgrade_gate(self) -> None:
        """Review R5: every public write path -- WITH-prefixed DML,
        INSERT...RETURNING, chained cursors, executemany, executescript and
        both writable-PRAGMA forms -- is refused by SQLite itself outside the
        transaction gate, before and after a concurrent schema bump, and
        nothing lands."""
        migrate_to_head(self.db)
        conn = connect(self.db)

        def attempts(suffix: str) -> list[tuple[str, Any]]:
            ins = (
                "INSERT INTO projects (id, name, repo_path, created_at)"
                f" VALUES ('p-bypass-{suffix}', 'x', '/x', 'now')"
            )
            return [
                ("execute INSERT", lambda: conn.execute(ins)),
                (
                    "WITH INSERT",
                    lambda: conn.execute(
                        "WITH src(pid, pname, ppath, pnow) AS (SELECT ?, ?, ?, ?)"
                        " INSERT INTO projects (id, name, repo_path, created_at)"
                        " SELECT pid, pname, ppath, pnow FROM src",
                        (f"p-bypass-{suffix}w", "x", "/x", "now"),
                    ),
                ),
                (
                    "WITH UPDATE",
                    lambda: conn.execute(
                        "WITH src AS (SELECT 'p1' AS pid) UPDATE projects"
                        " SET name='hacked' WHERE id=(SELECT pid FROM src)"
                    ),
                ),
                (
                    "WITH DELETE",
                    lambda: conn.execute(
                        "WITH src AS (SELECT 'p1' AS pid) DELETE FROM projects"
                        " WHERE id=(SELECT pid FROM src)"
                    ),
                ),
                ("INSERT RETURNING", lambda: conn.execute(ins + " RETURNING id")),
                (
                    "chained execute->execute",
                    lambda: conn.execute("SELECT 1").execute(ins),
                ),
                (
                    "chained cursor->execute->execute",
                    lambda: conn.cursor().execute("SELECT 1").execute(ins),
                ),
                (
                    "executemany INSERT",
                    lambda: conn.executemany(
                        "INSERT INTO projects (id, name, repo_path, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        [(f"p-bypass-{suffix}m", "x", "/x", "now")],
                    ),
                ),
                ("executescript", lambda: conn.executescript(ins + ";")),
                (
                    "cursor.executescript",
                    lambda: conn.cursor().executescript(ins + ";"),
                ),
                (
                    "PRAGMA user_version=99",
                    lambda: conn.execute("PRAGMA user_version=99"),
                ),
                (
                    "PRAGMA user_version(99)",
                    lambda: conn.execute("PRAGMA user_version(99)"),
                ),
                ("commit()", conn.commit),
                ("rollback()", conn.rollback),
            ]

        try:
            # 0) even at the current version every vector is refused, and
            #    public/iterative results stay controlled facades, never a raw
            #    sqlite3.Cursor write entry point.
            for name, call in attempts("v1"):
                with self.subTest(entry=name):
                    with self.assertRaises(WriteOutsideTransactionError):
                        call()
            self.assertIsInstance(conn.execute("SELECT 1"), storage._GatedCursor)
            self.assertIsInstance(conn.cursor(), storage._GatedCursor)
            self.assertIsInstance(
                conn.execute("SELECT 1").execute("SELECT 2"), storage._GatedCursor
            )
            # Legitimate reads keep working, WITH-prefixed SELECT included.
            self.assertEqual(
                conn.execute("WITH y AS (SELECT 1 AS n) SELECT n FROM y").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.cursor().execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

            probe0 = sqlite3.connect(str(self.db))
            try:
                self.assertEqual(
                    probe0.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
                )
                self.assertEqual(probe0.execute("PRAGMA user_version").fetchone()[0], 1)
            finally:
                probe0.close()

            # 1) a second connection commits the schema bump to v99.
            attacker = sqlite3.connect(str(self.db))
            try:
                attacker.execute("PRAGMA user_version=99")
                attacker.commit()
            finally:
                attacker.close()

            # 2) every public write vector still refuses after the bump...
            for name, call in attempts("v99"):
                with self.subTest(entry=f"after-upgrade {name}"):
                    with self.assertRaises(WriteOutsideTransactionError):
                        call()
            # ...and the one legitimate write path refuses via the version gate.
            with self.assertRaises(SchemaTooNewError):
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, repo_path, created_at)"
                        " VALUES ('p-late', 'x', '/x', 'now')"
                    )
            # Read capability survives on the stale-version connection.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)

            probe = sqlite3.connect(str(self.db))
            try:
                self.assertEqual(
                    probe.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
                )
                self.assertEqual(probe.execute("PRAGMA user_version").fetchone()[0], 99)
            finally:
                probe.close()
        finally:
            conn.close()

    def test_prepared_statement_cache_does_not_leak_writes(self) -> None:
        """A write text compiled legally inside the gate must stay refused
        outside it: SQLite re-runs its boundary on every re-prepare, and the
        default statement cache provides no smuggling path."""
        migrate_to_head(self.db)
        conn = connect(self.db)
        sql = (
            "INSERT INTO projects (id, name, repo_path, created_at)"
            " VALUES ('p-cache', 'c', '/c', 'now')"
        )
        try:
            with transaction(conn):
                conn.execute(sql)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
            )
            with self.assertRaises(WriteOutsideTransactionError):
                conn.execute(sql)
            with self.assertRaises(WriteOutsideTransactionError):
                conn.cursor().execute(sql)
            with self.assertRaises(WriteOutsideTransactionError):
                conn.execute("SELECT 1").execute(sql)
        finally:
            conn.close()
        probe = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                probe.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
            )
        finally:
            probe.close()

    def test_gated_transaction_excludes_upgrade_and_admits_legal_write(self) -> None:
        """While the v1 gate holds the write lock a concurrent upgrade cannot
        commit; the legitimate gated write commits instead."""
        migrate_to_head(self.db)
        conn = connect(self.db)
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO projects (id, name, repo_path, created_at)"
                    " VALUES ('p-gate', 'gated', '/gated', 'now')"
                )
                blocker = sqlite3.connect(str(self.db), isolation_level=None)
                try:
                    # Exclusive lock: the upgrade cannot even start its txn.
                    with self.assertRaises(sqlite3.OperationalError):
                        blocker.execute("BEGIN IMMEDIATE")
                finally:
                    blocker.close()
            # The gated write committed at the legit version...
            probe = sqlite3.connect(str(self.db))
            try:
                self.assertEqual(
                    probe.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
                )
                self.assertEqual(probe.execute("PRAGMA user_version").fetchone()[0], 1)
            finally:
                probe.close()
            # ...only after the gate released could an upgrade land, and then
            # the facade blocks all further writes for this open connection.
            late = sqlite3.connect(str(self.db))
            try:
                late.execute("PRAGMA user_version=99")
                late.commit()
            finally:
                late.close()
            with self.assertRaises(SchemaTooNewError):
                with transaction(conn):
                    pass
        finally:
            conn.close()

    def test_failed_connect_removes_only_files_it_created(self) -> None:
        """Review R4: cleanup must not delete files another process created;
        ownership comes from O_EXCL, not from a pre-check boolean."""
        real_connect = sqlite3.connect

        def selective(database, *args, **kwargs):
            # Read-only URI probes still work; the writable open fails hard.
            if isinstance(database, str) and database.startswith("file:"):
                return real_connect(database, *args, **kwargs)
            raise sqlite3.OperationalError("simulated writable-open failure")

        external = self.base / "external.db"
        seed = real_connect(str(external))  # created by "another process"
        seed.close()
        with mock.patch.object(storage.sqlite3, "connect", side_effect=selective):
            with self.assertRaises(StorageError):
                connect(external)
        self.assertTrue(external.exists())  # not ours: never unlinked

        fresh = self.base / "fresh.db"
        with mock.patch.object(storage.sqlite3, "connect", side_effect=selective):
            with self.assertRaises(StorageError):
                connect(fresh)
        self.assertFalse(fresh.exists())  # ours: cleaned up after the failure

    def test_connect_failure_cleanup_respects_concurrent_replacement(self) -> None:
        """R7 ownership tokens also guard connect(): if another process swaps
        our freshly created file mid-init, the failure cleanup keeps theirs;
        our own half-initialised file is still removed."""

        def racing_check(conn, stage):
            victim = self.base / "fresh-race.db"
            victim.unlink()
            victim.write_bytes(b"FOREIGN-DB-BYTES")
            raise SchemaTooNewError("simulated mid-init bump")

        fresh = self.base / "fresh-race.db"
        with mock.patch.object(
            storage, "_assert_writable_version", side_effect=racing_check
        ):
            with self.assertRaises(SchemaTooNewError):
                connect(fresh)
        self.assertTrue(fresh.exists())
        self.assertEqual(fresh.read_bytes(), b"FOREIGN-DB-BYTES")

        def plain_check(conn, stage):
            raise SchemaTooNewError("simulated mid-init bump")

        fresh2 = self.base / "fresh-own.db"
        with mock.patch.object(
            storage, "_assert_writable_version", side_effect=plain_check
        ):
            with self.assertRaises(SchemaTooNewError):
                connect(fresh2)
        self.assertFalse(fresh2.exists())

    def test_corrupted_database_is_refused_and_left_untouched(self) -> None:
        migrate_to_head(self.db)
        conn = connect(self.db)
        _seed(conn)
        conn.close()

        with self.db.open("r+b") as handle:  # destroy pages past the header
            handle.seek(100)
            handle.write(b"\x5a" * (self.db.stat().st_size - 100))
        before = self.db.read_bytes()

        with self.assertRaises(StorageError):
            migrate_to_head(self.db)
        self.assertEqual(self.db.read_bytes(), before)  # no silent rebuild

    def test_unversioned_junk_database_is_not_adopted(self) -> None:
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE widgets (x TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(MigrationError):
            migrate_to_head(self.db)

    # ---------------------------------------------------------- transactions
    def test_transaction_rolls_back_on_error(self) -> None:
        migrate_to_head(self.db)
        conn = connect(self.db)
        try:
            with self.assertRaises(RuntimeError):
                with transaction(conn):
                    _seed(conn, "p-rollback")
                    raise RuntimeError("boom")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
            )
            # Connection remains usable afterwards.
            with transaction(conn):
                _seed(conn, "p-ok")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_transaction_rejects_raw_sqlite_connection(self) -> None:
        """The public write gate must never accept an authorizer-free handle."""
        migrate_to_head(self.db)
        raw = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            with self.assertRaisesRegex(TypeError, "TeamCraftConnection"):
                with transaction(raw):  # type: ignore[arg-type]
                    self.fail("raw connection entered the public write gate")
            self.assertFalse(raw.in_transaction)
            self.assertEqual(
                raw.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
            )
        finally:
            raw.close()

    def test_deferred_transaction_rolls_back_and_refuses_too_new_schema(self) -> None:
        """immediate=False keeps rollback and version gates, but is deferred."""
        migrate_to_head(self.db)
        conn = connect(self.db)
        try:
            with self.assertRaises(RuntimeError):
                with transaction(conn, immediate=False):
                    _seed(conn, "p-deferred")
                    raise RuntimeError("rollback deferred transaction")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
            )

            attacker = sqlite3.connect(str(self.db))
            try:
                attacker.execute("PRAGMA user_version=99")
                attacker.commit()
            finally:
                attacker.close()
            with self.assertRaises(SchemaTooNewError):
                with transaction(conn, immediate=False):
                    self.fail("too-new deferred transaction entered write body")
        finally:
            conn.close()

    def test_subprocess_crash_rolls_back_uncommitted_transaction(self) -> None:
        migrate_to_head(self.db)
        script = (
            "import os, signal, sys; sys.path.insert(0, %r);"
            "from teamcraft import connect, transaction;"
            "c = connect(%r);"
            "ctx = transaction(c); ctx.__enter__();"
            "c.execute('INSERT INTO projects (id, name, repo_path, created_at)"
            " VALUES (\\'p-crash\\', \\'crash\\', \\'/r/crash\\', \\'now\\')');"
            "sys.stdout.flush();"
            "os.kill(os.getpid(), signal.SIGKILL)"
        ) % (str(REPO_ROOT), str(self.db))
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            -int(signal.SIGKILL),
            f"child should die from SIGKILL; stderr={completed.stderr!r}",
        )

        report = verify_database(self.db)
        self.assertTrue(report["ok"], report)
        conn = connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
            )
        finally:
            conn.close()

    # ------------------------------------------------------- concurrency
    def test_single_active_attempt_per_task(self) -> None:
        migrate_to_head(self.db)
        conn = connect(self.db)
        try:
            with transaction(conn):
                _seed(conn)
                now = "2026-10-27T00:00:00+00:00"
                conn.execute(
                    "INSERT INTO tasks (id, project_id, title, status, created_at, updated_at)"
                    " VALUES ('t1', 'p1', 'ship it', 'ready', ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "INSERT INTO agents (id, role, harness) VALUES ('a1', 'developer', 'codex')"
                )
                conn.execute(
                    "INSERT INTO attempts (id, task_id, agent_id, attempt_no, status)"
                    " VALUES ('at1', 't1', 'a1', 1, 'active')"
                )
            # A second active attempt for the same task must violate the DB.
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO attempts (id, task_id, agent_id, attempt_no, status)"
                        " VALUES ('at2', 't1', 'a1', 2, 'active')"
                    )
            # Closed attempts coexist with the active one.
            with transaction(conn):
                conn.execute(
                    "INSERT INTO attempts (id, task_id, agent_id, attempt_no, status)"
                    " VALUES ('at0', 't1', 'a1', 0, 'finished')"
                )
        finally:
            conn.close()

    def test_two_connections_contend_for_the_write_lock(self) -> None:
        migrate_to_head(self.db)
        writer = connect(self.db, busy_timeout_ms=5000)
        with transaction(writer):
            _seed(writer, "p-held")
            time.sleep(0.35)  # hold the write lock past the contender's timeout
            contender = connect(self.db, busy_timeout_ms=120)
            try:
                started = time.monotonic()
                with self.assertRaises(sqlite3.OperationalError):
                    with transaction(contender):
                        pass
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 2.0)  # honoured busy_timeout, not hang
            finally:
                contender.close()
        # After the winner commits, the loser sees the fact via constraints.
        contender = connect(self.db, busy_timeout_ms=2000)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(contender):
                    _seed(contender, "p-held")
        finally:
            contender.close()
        writer.close()

    # ------------------------------------------------------- ship/scout model
    def test_ship_and_scout_evidence_are_both_expressible(self) -> None:
        migrate_to_head(self.db)
        now = "2026-10-27T00:00:00+00:00"
        conn = connect(self.db)
        try:
            with transaction(conn):
                _seed(conn)
                conn.execute(
                    "INSERT INTO tasks (id, project_id, title, kind, status, created_at, updated_at)"
                    " VALUES ('t-ship', 'p1', 'ship task', 'ship', 'ready', ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "INSERT INTO tasks (id, project_id, title, kind, status, created_at, updated_at)"
                    " VALUES ('t-scout', 'p1', 'scout task', 'scout', 'ready', ?, ?)",
                    (now, now),
                )
                # ship without a commit yet (M4 fills the evidence chain):
                conn.execute(
                    "INSERT INTO attempts (id, task_id, attempt_no, status)"
                    " VALUES ('at-ship', 't-ship', 1, 'active')"
                )
                # scout with a report and no commit:
                conn.execute(
                    "INSERT INTO attempts (id, task_id, attempt_no, status, report_ref)"
                    " VALUES ('at-scout', 't-scout', 1, 'finished', 'reports/scout-1.md')"
                )
            row = conn.execute(
                "SELECT commit_sha, report_ref FROM attempts WHERE id='at-scout'"
            ).fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], "reports/scout-1.md")
            # Unknown delivery kinds are rejected at the database level.
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, kind, status,"
                        " created_at, updated_at) VALUES ('t-weird', 'p1', 'x', 'yolo',"
                        " 'ready', ?, ?)",
                        (now, now),
                    )
        finally:
            conn.close()

    # ---------------------------------------------------- backup / restore
    def test_backup_while_wal_live_then_restore_and_verify(self) -> None:
        migrate_to_head(self.db)
        live = connect(self.db)
        with transaction(live):
            _seed(live, "p-live")
        # Keep this connection open so the WAL sidecar is definitely live.
        self.assertTrue((self.db.parent / "teamcraft.db-wal").exists())

        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-0001")
        live.close()

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        self.assertEqual(manifest["app_version"], "0.2.0.dev0")
        self.assertEqual(manifest["applicable_generation"], "gen-0001")
        self.assertEqual(manifest["backup_sha256"], result.sha256)
        self.assertEqual(manifest["integrity_check"], "ok")

        restored_dir = self.base / "restored"
        restored_dir.mkdir()
        restored = restored_dir / "teamcraft.db"
        restore_result = restore_backup(result.backup_path, restored)
        self.assertEqual(restore_result.schema_version, SCHEMA_VERSION)
        self.assertEqual(restore_result.integrity, "ok")
        conn = connect(restored)
        try:
            ids = {row[0] for row in conn.execute("SELECT id FROM projects")}
            self.assertIn("p-live", ids)  # WAL content made it into the backup
        finally:
            conn.close()

    def test_backup_generation_defaults_to_none(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertIsNone(manifest["applicable_generation"])

    def test_restore_safety_gates(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir)

        # 1. Never overwrite an existing destination database.
        clash = self.base / "clash.db"
        clash.write_bytes(b"placeholder")
        with self.assertRaises(BackupError):
            restore_backup(result.backup_path, clash)

        # 2. Missing manifest is refused by default.
        no_manifest = self.base / "orphan.db"
        shutil.copyfile(result.backup_path, no_manifest)
        with self.assertRaises(BackupError):
            restore_backup(no_manifest, self.base / "out1.db")

        # 3. Tampered digest is refused.
        tampered = self.base / "tampered.db"
        shutil.copyfile(result.backup_path, tampered)
        with tampered.open("r+b") as handle:
            handle.seek(tampered.stat().st_size // 2)
            handle.write(b"\x00\x01\x02")
        manifest_path = tampered.with_name(tampered.name + ".manifest.json")
        shutil.copyfile(result.manifest_path, manifest_path)
        with self.assertRaises(BackupError):
            restore_backup(tampered, self.base / "out2.db")

    def test_restore_rejects_manifest_version_mismatch_and_cleans_target(self) -> None:
        """Review P0-2: manifest says v0, snapshot is v1 -> refuse, no fake success."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 0  # exactly the reported inconsistency
        result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        dest = self.base / "restored.db"
        with self.assertRaises(BackupError):
            restore_backup(result.backup_path, dest)
        self.assertFalse(dest.exists())  # nothing left that could look restored
        for sidecar in _sidecar_paths(dest):
            self.assertFalse(sidecar.exists())

    def test_restore_rejects_inconsistent_manifest_fields(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")
        base_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

        mutations = {
            "size": lambda m: m.update({"backup_size_bytes": int(m["backup_size_bytes"]) + 7}),
            "name": lambda m: m.update({"backup_file": "someone-elses.db"}),
            "integrity": lambda m: m.update({"integrity_check": "broken"}),
            "too_new_manifest": lambda m: m.update({"schema_version": 99}),
            "missing_version": lambda m: m.pop("schema_version"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                manifest = json.loads(json.dumps(base_manifest))
                mutate(manifest)
                target_dir = self.base / f"mut-{label}"
                target_dir.mkdir()
                backup_copy = target_dir / result.backup_path.name
                shutil.copyfile(result.backup_path, backup_copy)
                (target_dir / result.manifest_path.name).write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                dest = self.base / f"dest-{label}.db"
                with self.assertRaises(StorageError):
                    restore_backup(backup_copy, dest)
                self.assertFalse(dest.exists())

    def test_restore_rejects_non_integer_manifest_fields_before_create(self) -> None:
        """Bad manifest scalars are BackupError, never conversion leaks."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups-invalid-scalars"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")
        base_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

        invalid_values = (None, True, False, -1, 1.5, "1", "not-a-number", [], {})
        for field in ("backup_size_bytes", "schema_version"):
            for index, value in enumerate(invalid_values):
                with self.subTest(field=field, value=value):
                    case_dir = self.base / f"invalid-{field}-{index}"
                    case_dir.mkdir()
                    backup_copy = case_dir / result.backup_path.name
                    shutil.copyfile(result.backup_path, backup_copy)
                    manifest = dict(base_manifest)
                    manifest[field] = value
                    backup_copy.with_name(backup_copy.name + ".manifest.json").write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )
                    dest = case_dir / "restored.db"
                    with self.assertRaises(BackupError):
                        restore_backup(backup_copy, dest)
                    self.assertFalse(dest.exists())

        # A syntactically valid integer still must fit this build's schema.
        too_new_dir = self.base / "invalid-schema-too-new"
        too_new_dir.mkdir()
        too_new_backup = too_new_dir / result.backup_path.name
        shutil.copyfile(result.backup_path, too_new_backup)
        too_new_manifest = dict(base_manifest)
        too_new_manifest["schema_version"] = SCHEMA_VERSION + 1
        too_new_backup.with_name(too_new_backup.name + ".manifest.json").write_text(
            json.dumps(too_new_manifest), encoding="utf-8"
        )
        too_new_dest = too_new_dir / "restored.db"
        with self.assertRaises(BackupError):
            restore_backup(too_new_backup, too_new_dest)
        self.assertFalse(too_new_dest.exists())

    def test_restore_rejects_non_object_manifest_without_touching_target(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups-non-object"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")
        result.manifest_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

        dest = self.base / "non-object-target.db"
        with self.assertRaises(BackupError):
            restore_backup(result.backup_path, dest)
        self.assertFalse(dest.exists())

        existing = self.base / "existing-target.db"
        existing.write_bytes(b"owned by another operation")
        before = existing.read_bytes()
        with self.assertRaises(BackupError):
            restore_backup(result.backup_path, existing)
        self.assertEqual(existing.read_bytes(), before)

    def test_restore_validates_snapshot_itself_not_only_manifest(self) -> None:
        """A self-consistent manifest must still be refused when the *snapshot*
        is too new or incomplete: the snapshot is the truth."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")

        # (a) snapshot user_version bumped to 99, manifest regenerated honestly.
        sneaky = self.base / "sneaky.db"
        shutil.copyfile(result.backup_path, sneaky)
        conn = sqlite3.connect(str(sneaky))
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        conn.close()
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "backup_file": sneaky.name,
                "backup_sha256": storage._sha256(sneaky),
                "backup_size_bytes": sneaky.stat().st_size,
                "schema_version": 99,
            }
        )
        sneaky.with_name(sneaky.name + ".manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        dest = self.base / "dest-sneaky.db"
        with self.assertRaises(SchemaTooNewError):
            restore_backup(sneaky, dest)
        self.assertFalse(dest.exists())

        # (b) v1-flagged but incomplete snapshot (missing tables), honest digest.
        hollow = self.base / "hollow.db"
        conn = sqlite3.connect(str(hollow))
        conn.execute("CREATE TABLE only_junk (x TEXT)")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "backup_file": hollow.name,
                "backup_sha256": storage._sha256(hollow),
                "backup_size_bytes": hollow.stat().st_size,
                "schema_version": 1,
            }
        )
        hollow.with_name(hollow.name + ".manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        dest2 = self.base / "dest-hollow.db"
        with self.assertRaises(BackupError):
            restore_backup(hollow, dest2)
        self.assertFalse(dest2.exists())

    def test_restore_requires_complete_manifest(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")
        base_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

        for field in (
            "backup_file",
            "backup_sha256",
            "backup_size_bytes",
            "schema_version",
            "app_version",
            "created_at_utc",
            "origin_database",
            "integrity_check",
            "applicable_generation",
            "tool",
        ):
            with self.subTest(field):
                manifest = json.loads(json.dumps(base_manifest))
                del manifest[field]
                target_dir = self.base / f"req-{field}"
                target_dir.mkdir()
                backup_copy = target_dir / result.backup_path.name
                shutil.copyfile(result.backup_path, backup_copy)
                (target_dir / result.manifest_path.name).write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                dest = self.base / f"dest-req-{field}.db"
                with self.assertRaises(BackupError):
                    restore_backup(backup_copy, dest)
                self.assertFalse(dest.exists())

    def test_restore_refuses_backup_with_newer_committed_version_in_wal(self) -> None:
        """Main-file/WAL consistency: committed WAL v99 must beat stale main v1.

        The manifest is regenerated *honestly* against the stale main file —
        exactly the input a stale (e.g. immutable-style) snapshot read would
        bless into a successful restore.  Strict WAL-aware reads refuse it.
        """
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")

        waldb = self.base / "wal-ahead.db"
        shutil.copyfile(result.backup_path, waldb)
        w = sqlite3.connect(str(waldb))
        w.execute("PRAGMA journal_mode=WAL")
        w.commit()
        w.close()  # clean WAL conversion + checkpoint; main now authoritative v1
        w = sqlite3.connect(str(waldb), isolation_level=None)
        try:
            w.execute("PRAGMA wal_autocheckpoint=0")
            w.execute("PRAGMA user_version=99")  # committed, stays in the WAL

            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "backup_file": waldb.name,
                    "backup_sha256": storage._sha256(waldb),
                    "backup_size_bytes": waldb.stat().st_size,
                    "schema_version": 1,
                }
            )
            waldb.with_name(waldb.name + ".manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            dest = self.base / "dest-wal-ahead.db"
            with self.assertRaises(SchemaTooNewError):
                restore_backup(waldb, dest)
            self.assertFalse(dest.exists())
        finally:
            w.close()

    def test_restore_refuses_snapshot_without_migration_record(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir, generation="gen-1")

        unaudited = self.base / "unaudited.db"
        shutil.copyfile(result.backup_path, unaudited)
        conn = sqlite3.connect(str(unaudited))
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
        conn.close()

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "backup_file": unaudited.name,
                "backup_sha256": storage._sha256(unaudited),
                "backup_size_bytes": unaudited.stat().st_size,
            }
        )
        unaudited.with_name(unaudited.name + ".manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        dest = self.base / "dest-unaudited.db"
        with self.assertRaises(BackupError):
            restore_backup(unaudited, dest)
        self.assertFalse(dest.exists())

    def test_backup_failure_leaves_no_half_artefacts(self) -> None:
        # A nonexistent source is refused without creating anything.
        missing = self.base / "missing.db"
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        with self.assertRaises(BackupError):
            backup_database(missing, backup_dir)
        self.assertEqual(list(backup_dir.iterdir()), [])

        # Corrupting the source after the version gate surfaces a failure path
        # that must not leave half-written backups behind.
        migrate_to_head(self.db)
        with self.db.open("r+b") as handle:
            handle.seek(100)
            handle.write(b"\x5a" * (self.db.stat().st_size - 100))
        with self.assertRaises(StorageError):
            backup_database(self.db, backup_dir)
        self.assertEqual(list(backup_dir.iterdir()), [])

    # --------------------------------------------------- backup boundary (R6)
    def test_public_backup_cannot_overwrite_other_database(self) -> None:
        """Review R6: the Online Backup API bypasses SQL authorization, so
        the facade must refuse backup outright; the too-new dst keeps its
        bytes and version no matter what backup() is called with."""
        src = self.base / "src.db"
        dst = self.base / "dst.db"
        migrate_to_head(src)
        migrate_to_head(dst)
        seed = connect(dst)
        with transaction(seed):
            seed.execute(
                "INSERT INTO projects (id, name, repo_path, created_at)"
                " VALUES ('p-keep', 'keep', '/keep', 'now')"
            )
        seed.close()

        a = connect(src)
        b = connect(dst)
        try:
            # Another connection bumps dst to v99 while both facades are open.
            attacker = sqlite3.connect(str(dst))
            try:
                attacker.execute("PRAGMA user_version=99")
                attacker.commit()
            finally:
                attacker.close()
            dst_bytes = dst.read_bytes()

            for target in (b, dst, str(dst), None):
                with self.subTest(target=str(target)):
                    with self.assertRaises(BackupError):
                        a.backup(target)
            # The reverse direction (which would clobber src) is refused too.
            with self.assertRaises(BackupError):
                b.backup(a)
            # While both facades are open the refused attempts changed nothing.
            self.assertEqual(dst.read_bytes(), dst_bytes)
        finally:
            a.close()
            b.close()

        # The too-new version and the data both survived.
        probe = sqlite3.connect(str(dst))
        try:
            self.assertEqual(probe.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(
                probe.execute(
                    "SELECT name FROM projects WHERE id='p-keep'"
                ).fetchone()[0],
                "keep",
            )
        finally:
            probe.close()

    def test_backup_targets_race_and_failure_cleanup_own_files_only(self) -> None:
        """A foreign destination is refused untouched; a copy that fails after
        this call exclusively created its target removes only its own file."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir)

        # restore onto a foreign existing database: refused, bytes preserved.
        foreign = self.base / "foreign.db"
        migrate_to_head(foreign)
        foreign_bytes = foreign.read_bytes()
        with self.assertRaises(BackupError):
            restore_backup(result.backup_path, foreign)
        self.assertEqual(foreign.read_bytes(), foreign_bytes)

        # Simulated page-copy failure via the auditable test seam: the
        # exclusively-created destination must be cleaned up by the failing
        # call itself and surface as BackupError, leaving no half artefacts.
        def _fail_copy(src_conn: sqlite3.Connection, dst_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("simulated page-copy failure")

        half = self.base / "half-restore.db"
        with mock.patch.object(storage, "BACKUP_COPY_HOOK", _fail_copy):
            with self.assertRaises(BackupError):
                restore_backup(result.backup_path, half)
            self.assertFalse(half.exists())
            # backup_database too: no half artefacts in the directory.
            before = set(p.name for p in backup_dir.iterdir())
            with self.assertRaises(BackupError):
                backup_database(self.db, backup_dir)
            self.assertEqual(
                {n for n in (p.name for p in backup_dir.iterdir()) if n.endswith(".db")}
                - {n for n in before if n.endswith(".db")},
                set(),
            )

    # ------------------------------------------- concurrent cleanup ownership (R7)
    @staticmethod
    def _frozen_names(stem: str) -> tuple[str, str]:
        """Predictable backup/manifest names under frozen clock + urandom."""
        stamp = "20261029T120000"
        suffix = b"\x01\x02\x03".hex()
        name = f"{stem}-{stamp}-{suffix}.db"
        return name, name + ".manifest.json"

    def test_foreign_target_installed_after_check_survives_cleanup(self) -> None:
        """Review R7 repro: another process creates the target between the
        exists() pre-check and the O_EXCL creation; the refusal must not then
        delete the winner's file."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        backup_name, manifest_name = self._frozen_names(self.db.stem)
        target = backup_dir / backup_name

        class FrozenDateTime(storage.datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001 - mirror stdlib signature
                return cls(2026, 10, 29, 12, 0, 0)

        real_snapshot = storage._snapshot_versions

        def racing_snapshot(path):
            result = real_snapshot(path)
            # "Another process" wins the name right after our exists() check.
            other = storage.sqlite3.connect(str(target))
            try:
                other.execute("CREATE TABLE foreign_marker(x)")
                other.commit()
            finally:
                other.close()
            return result

        with mock.patch.object(storage, "datetime", FrozenDateTime), mock.patch.object(
            os, "urandom", lambda n: b"\x01\x02\x03"
        ), mock.patch.object(
            storage, "_snapshot_versions", side_effect=racing_snapshot
        ):
            with self.assertRaises(BackupError):
                backup_database(self.db, backup_dir)

        self.assertTrue(target.exists())  # the winner's file survives intact
        self.assertFalse((backup_dir / manifest_name).exists())
        probe = sqlite3.connect(str(target))
        try:
            self.assertEqual(
                probe.execute(
                    "SELECT name FROM sqlite_master WHERE name='foreign_marker'"
                ).fetchone()[0],
                "foreign_marker",
            )
        finally:
            probe.close()

    def test_manifest_race_preserves_foreign_manifest(self) -> None:
        """A manifest appearing mid-flight (between copy and manifest write)
        belongs to the other process: it is neither overwritten nor deleted,
        while our own unfinished backup is cleaned up."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        backup_name, manifest_name = self._frozen_names(self.db.stem)
        target = backup_dir / backup_name
        foreign_manifest = backup_dir / manifest_name
        marker = b'{"foreign": "manifest"}\n'

        class FrozenDateTime(storage.datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001 - mirror stdlib signature
                return cls(2026, 10, 29, 12, 0, 0)

        def plant_foreign_manifest(src_conn, dst_conn) -> None:
            # Concurrent archiver writes its manifest while our copy runs.
            with open(foreign_manifest, "wb") as handle:
                handle.write(marker)

        with mock.patch.object(storage, "datetime", FrozenDateTime), mock.patch.object(
            os, "urandom", lambda n: b"\x01\x02\x03"
        ), mock.patch.object(storage, "BACKUP_COPY_HOOK", plant_foreign_manifest):
            with self.assertRaises(BackupError):
                backup_database(self.db, backup_dir)

        self.assertEqual(foreign_manifest.read_bytes(), marker)  # never touched
        self.assertFalse(target.exists())  # our own artefact is cleaned up

    def test_restore_concurrent_replacement_survives_cleanup(self) -> None:
        """If the restore destination is replaced mid-copy by another process,
        the later verification failure must leave the replacement (and its
        planted sidecars) exactly as they are."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(self.db, backup_dir)
        dest = self.base / "restore-dest.db"
        foreign_wal = Path(str(dest) + "-wal")
        captured: dict[str, bytes] = {}
        real_verify = storage.verify_database

        def racing_verify(path):
            # Our copy has just finished and its handles are closed; another
            # process now replaces the destination and installs its sidecar.
            report = real_verify(path)
            assert report["ok"]
            dest.unlink()
            other = storage.sqlite3.connect(str(dest))
            try:
                other.execute("CREATE TABLE other(x)")  # fresh foreign db, v0
                other.commit()
            finally:
                other.close()
            foreign_wal.write_bytes(b"FOREIGN-WAL-MARKER")
            captured["dest"] = dest.read_bytes()
            # Our verification then observes a foreign database -> refuse.
            return {**report, "ok": False, "integrity": "foreign replacement"}

        with mock.patch.object(storage, "verify_database", side_effect=racing_verify):
            with self.assertRaises(BackupError):
                restore_backup(result.backup_path, dest)

        self.assertEqual(dest.read_bytes(), captured["dest"])
        self.assertEqual(foreign_wal.read_bytes(), b"FOREIGN-WAL-MARKER")
        probe = sqlite3.connect(str(dest))
        try:
            self.assertEqual(probe.execute("PRAGMA user_version").fetchone()[0], 0)
        finally:
            probe.close()
        # The verified backup itself is untouched.
        self.assertTrue(result.backup_path.exists())

    def test_cleanup_token_semantics(self) -> None:
        """Direct pinning of the ownership helpers used by all cleanup paths."""
        owned = self.base / "owned.db"
        fd = storage._exclusive_create(owned)
        self.assertIsNotNone(fd)
        token = storage._fd_token(fd)
        os.close(fd)
        owned.write_bytes(b"ours")
        # A second exclusive create at an existing path yields None (refuse).
        self.assertIsNone(storage._exclusive_create(owned))

        replaced = self.base / "replaced.db"
        fd2 = storage._exclusive_create(replaced)
        token2 = storage._fd_token(fd2)
        os.close(fd2)
        replaced.unlink()  # foreign process replaces at the same name
        replaced.write_bytes(b"foreign content")

        storage._discard_if_still_ours(owned, None)
        self.assertTrue(owned.exists())  # no token: never delete
        storage._discard_if_still_ours(replaced, token2)
        self.assertTrue(replaced.exists())  # different inode: foreign, keep
        self.assertEqual(replaced.read_bytes(), b"foreign content")

        wal = Path(str(owned) + "-wal")
        shm = Path(str(owned) + "-shm")
        wal.write_bytes(b"wal")
        shm.write_bytes(b"shm")
        storage._discard_if_still_ours(owned, token)
        self.assertFalse(owned.exists())  # ours (inode-matched): removed
        # R9: sidecar ownership can never be proven by the main-file token,
        # so sidecars are kept even when the main artefact is deleted.
        self.assertTrue(wal.exists())
        self.assertTrue(shm.exists())

    # ---------------------------------------- backup/restore acceptance parity (R8)
    def test_backup_refuses_unrestorable_databases_consistent_with_restore(self) -> None:
        """Review R8 repro: a sqlite3-created empty database must not be
        ``success``-backed up only to be refused at restore; both sides apply
        the same acceptance and nothing is created or deleted."""
        backup_dir = self.base / "backups"
        backup_dir.mkdir()

        plain_empty = self.base / "plain-empty.db"
        conn = sqlite3.connect(str(plain_empty))  # v0, zero tables
        conn.close()

        plain_tables = self.base / "plain-tables.db"
        conn = sqlite3.connect(str(plain_tables))  # v0, tables but no audit record
        conn.execute("CREATE TABLE whatever(x)")
        conn.commit()
        conn.close()

        for label, plain in (("empty v0", plain_empty), ("v0 tables no record", plain_tables)):
            with self.subTest(label):
                dest = self.base / f"restore-refusal-{plain.stem}.db"
                with self.assertRaises(BackupError):
                    backup_database(plain, backup_dir)
                with self.assertRaises(BackupError):
                    restore_backup(plain, dest)
                self.assertFalse(dest.exists())
                # The refused backups also left their backups directory empty.
                self.assertEqual(list(backup_dir.iterdir()), [])
                # The refused source database itself is untouched.
                self.assertTrue(plain.exists())

        # Positive control: a genuine v1 backup passes both sides.
        migrate_to_head(self.db)
        result = backup_database(self.db, backup_dir, generation="gen-r8")
        restored = self.base / "restored-r8.db"
        restore_result = restore_backup(result.backup_path, restored)
        self.assertEqual(restore_result.schema_version, SCHEMA_VERSION)
        self.assertEqual(restore_result.integrity, "ok")

    # ------------------------------------------- foreign sidecar survival (R9)
    @staticmethod
    def _plant_wal_and_fail(src_conn, dst_conn) -> None:
        row = dst_conn.execute("PRAGMA database_list").fetchone()
        dst_file = str(row[2])
        with open(dst_file + "-wal", "wb") as handle:
            handle.write(b"FOREIGN-WAL-BYTES")
        raise sqlite3.OperationalError("simulated copy abort after foreign sidecar")

    def test_foreign_sidecar_planted_during_failed_copy_is_never_deleted(self) -> None:
        """Review R9 repro: another process creates target-wal mid-copy and
        the copy then fails; cleanup must remove our own half-written
        database (inode-matched) but leave the foreign sidecar alone — at
        both the backup and restore levels."""
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()

        with mock.patch.object(storage, "BACKUP_COPY_HOOK", self._plant_wal_and_fail):
            with self.assertRaises(BackupError):
                backup_database(self.db, backup_dir)

        leftovers = sorted(p.name for p in backup_dir.iterdir())
        self.assertEqual([n for n in leftovers if n.endswith(".db")], [])
        self.assertEqual([n for n in leftovers if n.endswith(".manifest.json")], [])
        wals = [n for n in leftovers if n.endswith("-wal")]
        self.assertEqual(len(wals), 1)  # only the foreign sidecar remains
        self.assertEqual((backup_dir / wals[0]).read_bytes(), b"FOREIGN-WAL-BYTES")

        # Restore level: same guarantee for the destination's sidecars.
        result = backup_database(self.db, backup_dir)
        dest = self.base / "restore-sidecar.db"
        foreign_wal = Path(str(dest) + "-wal")
        with mock.patch.object(storage, "BACKUP_COPY_HOOK", self._plant_wal_and_fail):
            with self.assertRaises(BackupError):
                restore_backup(result.backup_path, dest)
        self.assertFalse(dest.exists())  # our own dest, inode-matched: removed
        self.assertTrue(foreign_wal.exists())  # unprovable ownership: kept
        self.assertEqual(foreign_wal.read_bytes(), b"FOREIGN-WAL-BYTES")

    # ------------------------------------------------------- URI targeting
    def test_uri_special_characters_target_exact_files(self) -> None:
        """``#``, ``?``, ``%`` (and space) in names must hit exactly the right file."""
        names = ["weird#1.db", "weird?2.db", "rate%20pct%.db", "space name.db"]
        for index, name in enumerate(names):
            with self.subTest(name):
                target = self.base / name
                report = migrate_to_head(target)
                self.assertTrue(report.fresh)
                # Exactly the requested file exists and is the v1 database.
                self.assertTrue(target.exists())
                report = verify_database(target)
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["user_version"], SCHEMA_VERSION)
                self.assertEqual(set(report["tables"]), EXPECTED_TABLES)
                conn = connect(target)
                _seed(conn, f"p-uri-{index}")
                conn.close()

        # No truncated decoy files were created beside them (the old bug opened
        # "weird" when asked for "weird#1.db").
        for decoy in ("weird", "weird?2", "rate%20pct%", "space", "weird%231.db"):
            self.assertFalse((self.base / decoy).exists(), f"stray decoy {decoy!r}")

        # End-to-end backup/restore with special characters throughout.
        special = self.base / "weird#1.db"
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        result = backup_database(special, backup_dir, generation="gen-#1")
        self.assertIn("#", result.backup_path.name)
        restored = self.base / "restored#out?.db"
        restore_result = restore_backup(result.backup_path, restored)
        self.assertEqual(restore_result.schema_version, SCHEMA_VERSION)
        conn = connect(restored)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_too_new_database_behind_hash_suffix_is_refused_not_decoy(self) -> None:
        """Review P0-1 repro: v99 target ``future#name.db`` beside v0 decoy ``future``."""
        decoy = self.base / "future"
        _make_plain_db(decoy, 0, marker_table="decoy_marker")
        target = self.base / "future#name.db"
        _make_plain_db(target, 99, marker_table="too_new_marker")

        decoy_before = decoy.read_bytes()
        target_before = target.read_bytes()

        with self.assertRaises(SchemaTooNewError):
            connect(target)
        with self.assertRaises(SchemaTooNewError):
            migrate_to_head(target)
        # Read-only inspection must see the *target's* version, not the decoy's.
        self.assertEqual(verify_database(target)["user_version"], 99)

        self.assertEqual(decoy.read_bytes(), decoy_before)  # decoy untouched
        self.assertEqual(target.read_bytes(), target_before)  # target untouched
        for path in (decoy, target):
            for sidecar in _sidecar_paths(path):
                self.assertFalse(sidecar.exists())

    def test_read_gate_never_falls_back_to_a_writable_or_immutable_open(self) -> None:
        """If strict read-only fails, the answer is failure — never rwc/immutable."""
        attempts: list[str] = []

        def fake_connect(database, *args, **kwargs):
            attempts.append(str(database))
            raise sqlite3.OperationalError("simulated: cannot open")

        with mock.patch.object(storage.sqlite3, "connect", side_effect=fake_connect):
            with self.assertRaises(CorruptDatabaseError):
                storage._read_user_version(self.base / "whatever.db")

        self.assertEqual(len(attempts), 1)  # mode=ro once, then fail closed
        for uri in attempts:
            self.assertTrue(uri.startswith("file:"))
            query = uri.split("?", 1)[1]
            self.assertIn("mode=ro", query)
            self.assertNotIn("mode=rw", query)
            self.assertNotIn("immutable", query)
        # Nothing was ever opened against the bare (non-URI) path either.
        self.assertNotIn(str(self.base / "whatever.db"), attempts)

    def test_module_never_uses_immutable_masquerade(self) -> None:
        """Guard: immutable=1 is banned from the storage module outright."""
        source = inspect.getsource(storage)
        self.assertNotIn("immutable", source)

    def test_open_read_connection_propagates_caller_errors(self) -> None:
        """Review E: no swallowing + second yield after the caller raises."""
        migrate_to_head(self.db)
        captured: list[sqlite3.Connection] = []
        with self.assertRaises(sqlite3.OperationalError):
            with storage._open_read_connection(self.db) as conn:
                captured.append(conn)
                raise sqlite3.OperationalError("caller-boom")
        self.assertEqual(len(captured), 1)
        # The connection was closed on the way out despite the error.
        with self.assertRaises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")

    # ------------------------------------------------- concurrent migration (A)
    def test_two_process_migration_converges(self) -> None:
        barrier = self.base / "barrier"
        barrier.mkdir()
        script = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from teamcraft import migrate_to_head\n"
            "barrier, idx, db = sys.argv[1], int(sys.argv[2]), sys.argv[3]\n"
            "open(os.path.join(barrier, 'ready-' + str(idx)), 'w').close()\n"
            "deadline = time.monotonic() + 15\n"
            "while not (os.path.exists(os.path.join(barrier, 'ready-0'))"
            " and os.path.exists(os.path.join(barrier, 'ready-1'))):\n"
            "    if time.monotonic() > deadline:\n"
            "        print('barrier-timeout', file=sys.stderr)\n"
            "        sys.exit(3)\n"
            "    time.sleep(0.005)\n"
            "try:\n"
            "    migrate_to_head(db, busy_timeout_ms=20000)\n"
            "except Exception as exc:\n"
            "    print('ERROR: ' + repr(exc), file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print('OK')\n"
        ) % str(REPO_ROOT)

        procs = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(barrier), str(i), str(self.db)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in (0, 1)
        ]
        outputs = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=60)
            outputs.append((proc.returncode, stdout, stderr))
        for rc, stdout, stderr in outputs:
            self.assertEqual(rc, 0, f"migrator failed: stdout={stdout!r} stderr={stderr!r}")

        # Both converged onto exactly one complete v1 schema.
        report = verify_database(self.db)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(report["tables"]), EXPECTED_TABLES)
        conn = sqlite3.connect(storage._sqlite_uri(self.db, "mode=ro"), uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1
            )
        finally:
            conn.close()

    # -------------------------------------------------- crash mid-migration (B)
    def test_crash_mid_migration_rolls_back_partial_schema(self) -> None:
        """SIGKILL inside the *real* migrate_to_head, past halfway of its DDL.

        The child process calls ``migrate_to_head`` itself; the production test
        seam ``MIGRATION_STATEMENT_HOOK`` fires inside the live migration
        transaction after the third DDL statement, signals the parent, and the
        parent kills mid-transaction.  This exercises the actual function's
        transaction boundary — not a hand-rolled emulation.
        """
        flag = self.base / "partial.flag"
        script = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from teamcraft import storage, migrate_to_head\n"
            "db, flag = sys.argv[1], sys.argv[2]\n"
            "def hook(index, statement):\n"
            "    if index == 2:  # three v1 DDL statements already executed\n"
            "        with open(flag, 'w') as fh:\n"
            "            fh.write('mid-migration')\n"
            "            fh.flush()\n"
            "            os.fsync(fh.fileno())\n"
            "        time.sleep(30)  # still inside BEGIN IMMEDIATE, uncommitted\n"
            "storage.MIGRATION_STATEMENT_HOOK = hook\n"
            "migrate_to_head(db)\n"  # the real entry point on a fresh database
        ) % str(REPO_ROOT)
        proc = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.db), str(flag)])

        deadline = time.monotonic() + 20
        while not flag.exists():
            if proc.poll() is not None:
                self.fail("child exited before reaching mid-migration")
            self.assertLess(time.monotonic(), deadline, "child never reached mid-migration")
            time.sleep(0.02)
        os.kill(proc.pid, signal.SIGKILL)  # die mid-transaction, partial DDL pending
        rc = proc.wait(timeout=15)
        self.assertEqual(rc, -int(signal.SIGKILL))

        # No partial schema is visible: uncommitted DDL must not survive, and
        # this read is strict mode=ro only (no immutable anywhere).
        report = verify_database(self.db)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["user_version"], 0)
        self.assertNotIn("projects", report["tables"])
        self.assertNotIn("schema_migrations", report["tables"])

        # Restarting the real migration succeeds from scratch.
        retry = migrate_to_head(self.db)
        self.assertTrue(retry.fresh)
        report = verify_database(self.db)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(report["tables"]), EXPECTED_TABLES)
        conn = connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1
            )
        finally:
            conn.close()

    # -------------------------------------------- occupying attempt statuses (C)
    def test_occupying_statuses_block_parallel_dispatch(self) -> None:
        migrate_to_head(self.db)
        now = "2026-10-27T00:00:00+00:00"
        conn = connect(self.db)
        try:
            with transaction(conn):
                _seed(conn)
            occupying = [
                "created",
                "starting",
                "active",
                "unknown",
                "CREATED",
                "STARTING",
                "ACTIVE",
                "UNKNOWN",
            ]
            for i, status in enumerate(occupying):
                with self.subTest(status):
                    task = f"t-occ-{i}"
                    with transaction(conn):
                        conn.execute(
                            "INSERT INTO tasks (id, project_id, title, status,"
                            " created_at, updated_at) VALUES (?, 'p1', ?, 'ready', ?, ?)",
                            (task, f"task {i}", now, now),
                        )
                        conn.execute(
                            "INSERT INTO attempts (id, task_id, attempt_no, status)"
                            " VALUES (?, ?, 1, ?)",
                            (f"at-occ-{i}", task, status),
                        )
                    # Any re-dispatch (active) while occupying must be refused.
                    with self.assertRaises(sqlite3.IntegrityError):
                        with transaction(conn):
                            conn.execute(
                                "INSERT INTO attempts (id, task_id, attempt_no, status)"
                                " VALUES (?, ?, 2, 'active')",
                                (f"at-occ-{i}-b", task),
                            )
                    # Even an identical-case occupying token conflicts.
                    with self.assertRaises(sqlite3.IntegrityError):
                        with transaction(conn):
                            conn.execute(
                                "INSERT INTO attempts (id, task_id, attempt_no, status)"
                                " VALUES (?, ?, 2, ?)",
                                (f"at-occ-{i}-c", task, status),
                            )

            # Closed attempts do not occupy the slot.
            closed_task = "t-closed"
            with transaction(conn):
                conn.execute(
                    "INSERT INTO tasks (id, project_id, title, status, created_at, updated_at)"
                    " VALUES (?, 'p1', 'closed', 'ready', ?, ?)",
                    (closed_task, now, now),
                )
                for j, status in enumerate(("finished", "failed", "succeeded")):
                    conn.execute(
                        "INSERT INTO attempts (id, task_id, attempt_no, status)"
                        " VALUES (?, ?, ?, ?)",
                        (f"at-closed-{j}", closed_task, j + 1, status),
                    )
                # Re-dispatch is allowed once nothing occupies the task.
                conn.execute(
                    "INSERT INTO attempts (id, task_id, attempt_no, status)"
                    " VALUES ('at-closed-new', ?, 4, 'active')",
                    (closed_task,),
                )

            # Occupancy is per task: another task may hold its own attempt.
            with transaction(conn):
                conn.execute(
                    "INSERT INTO tasks (id, project_id, title, status, created_at, updated_at)"
                    " VALUES ('t-other', 'p1', 'other', 'ready', ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "INSERT INTO attempts (id, task_id, attempt_no, status)"
                    " VALUES ('at-other-1', 't-other', 1, 'active')"
                )
        finally:
            conn.close()

    # ---------------------------------------------------------- retention (D)
    def test_retention_is_dry_run_by_default_and_conservative(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        results = [backup_database(self.db, backup_dir, generation="gen-1") for _ in range(3)]
        all_backups = {r.backup_path for r in results}
        unbound = backup_database(self.db, backup_dir)  # generation unknown

        # A protected audit snapshot and an unknown file must never be picked.
        protected = backup_dir / "audit-snapshot.db"
        shutil.copyfile(results[0].backup_path, protected)
        protected_manifest = protected.with_name(protected.name + ".manifest.json")
        manifest = json.loads(results[0].manifest_path.read_text(encoding="utf-8"))
        manifest["protect"] = True
        manifest["backup_file"] = protected.name  # manifest describes this copy
        protected_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        mystery = backup_dir / "mystery.db"
        mystery.write_bytes(b"no manifest here")

        plan = plan_retention(backup_dir, keep_last=2)
        self.assertEqual(len(plan), 1)
        self.assertIn(plan[0], all_backups)
        self.assertTrue(plan[0].exists())  # dry run deleted nothing
        self.assertTrue(protected.exists() and mystery.exists())

        deleted = plan_retention(backup_dir, keep_last=2, delete=True)
        self.assertEqual(len(deleted), 1)
        self.assertFalse(deleted[0].exists())
        self.assertFalse(
            deleted[0].with_name(deleted[0].name + ".manifest.json").exists()
        )
        self.assertTrue(protected.exists() and mystery.exists())
        self.assertTrue(unbound.backup_path.exists())  # generation-unbound: kept

    def test_retention_never_deletes_generation_unknown(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        unbound = [backup_database(self.db, backup_dir) for _ in range(3)]
        explicit_empty = backup_database(self.db, backup_dir, generation="")

        # keep_last=1 would normally delete the older two+... but every entry
        # here is generation-unbound, so nothing is even eligible.
        plan = plan_retention(backup_dir, keep_last=1)
        self.assertEqual(plan, [])
        deleted = plan_retention(backup_dir, keep_last=1, delete=True)
        self.assertEqual(deleted, [])
        for result in unbound:
            self.assertTrue(result.backup_path.exists())
            self.assertTrue(result.manifest_path.exists())
        self.assertTrue(explicit_empty.backup_path.exists())

    def test_retention_age_rule_selects_old_manifests(self) -> None:
        migrate_to_head(self.db)
        backup_dir = self.base / "backups"
        backup_dir.mkdir()
        recent = backup_database(self.db, backup_dir, generation="gen-new")

        old = backup_dir / "old-snapshot.db"
        shutil.copyfile(recent.backup_path, old)
        manifest = json.loads(recent.manifest_path.read_text(encoding="utf-8"))
        manifest["created_at_utc"] = "2020-01-01T00:00:00+00:00"
        manifest["backup_file"] = old.name  # manifest describes this copy
        old.with_name(old.name + ".manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        plan = plan_retention(backup_dir, older_than_days=30)
        self.assertEqual(plan, [old])

    # ------------------------------------------------------ local-disk (D)
    def test_describe_path_support_classifies_injected_mount_lines(self) -> None:
        bsd_remote = [
            "/dev/disk3s1s1 on / (apfs, local, journaled, sealfs)",
            "//bob@smb.local/share on /Volumes/share (smbfs, nodev, nosuid, mounted by bob)",
        ]
        self.assertEqual(
            describe_path_support("/Volumes/share/teamcraft.db", mount_lines=bsd_remote)[
                "status"
            ],
            "remote",
        )
        bsd_afp = [
            "/dev/disk3s1s1 on / (apfs, local, journaled)",
            "afp://nas/vol on /Volumes/nas (afpfs, nodev)",
        ]
        self.assertEqual(
            describe_path_support("/Volumes/nas/db.sqlite", mount_lines=bsd_afp)["status"],
            "remote",
        )
        linux_remote = ["/dev/vda1 / ext4 rw,relatime 0 0", "srv:/export /mnt/nfs nfs4 rw 0 0"]
        self.assertEqual(
            describe_path_support("/mnt/nfs/x.db", mount_lines=linux_remote)["status"], "remote"
        )
        linux_local = ["server:/export /mnt/nfs nfs4 rw 0 0", "/dev/vda1 / ext4 rw,relatime 0 0"]
        self.assertEqual(
            describe_path_support("/srv/app/x.db", mount_lines=linux_local)["status"], "local"
        )
        self.assertEqual(
            describe_path_support("/x.db", mount_lines=linux_local)["status"], "local"
        )
        unclassified = ["/dev/fuse0 /mnt/odd mysteryfs rw 0 0"]
        self.assertEqual(
            describe_path_support("/mnt/odd/x.db", mount_lines=unclassified)["status"], "unknown"
        )
        self.assertEqual(
            describe_path_support("/nowhere/x.db", mount_lines=["garbage"])["status"], "unknown"
        )
        self.assertEqual(
            describe_path_support("/x.db", mount_lines=[])["status"], "unknown"
        )

    def test_mount_entries_with_spaces_and_special_chars_are_remote(self) -> None:
        """Review R3-2: literal-space and escaped-space remote mounts parse."""
        bsd_literal_space = [
            "/dev/disk3s1 on / (apfs, local, journaled)",
            "//user@host/share on /Volumes/Remote Drive (smbfs, nodev)",
        ]
        self.assertEqual(
            describe_path_support(
                "/Volumes/Remote Drive/x.db", mount_lines=bsd_literal_space
            )["status"],
            "remote",
        )
        bsd_escaped_space = [
            "/dev/disk3s1 on / (apfs, local, journaled)",
            "//user@host/share on /Volumes/Remote\\040Drive (smbfs, nodev)",
        ]
        self.assertEqual(
            describe_path_support(
                "/Volumes/Remote Drive/y.db", mount_lines=bsd_escaped_space
            )["status"],
            "remote",
        )
        special_chars = [
            "/dev/disk3s1 on / (apfs, local, journaled)",
            "//user@host/share on /Volumes/we#ird?pc%25x (afpfs, nodev)",
        ]
        self.assertEqual(
            describe_path_support(
                "/Volumes/we#ird?pc%25x/db.sqlite", mount_lines=special_chars
            )["status"],
            "remote",
        )

    def test_nested_mounts_use_deepest_covering_entry(self) -> None:
        lines = [
            "/dev/disk3s1 on / (apfs, local, journaled)",
            "server:/export /Volumes/nest nfs4 rw,relatime 0 0",
            "/dev/disk9s1 on /Volumes/nest/deep (apfs, local)",
        ]
        self.assertEqual(
            describe_path_support("/Volumes/nest/a.db", mount_lines=lines)["status"],
            "remote",
        )
        self.assertEqual(
            describe_path_support("/Volumes/nest/deep/b.db", mount_lines=lines)["status"],
            "local",  # the deeper local mount shadows its remote parent
        )
        self.assertEqual(
            describe_path_support("/Volumes/nest/deeper/c.db", mount_lines=lines)["status"],
            "remote",
        )
        self.assertEqual(
            describe_path_support("/srv/app/d.db", mount_lines=lines)["status"], "local"
        )

    def test_unparseable_mount_covering_target_fails_closed(self) -> None:
        """R3-2: an entry that may cover the target but cannot be parsed must
        never resolve to local."""
        unparseable_spacey = [
            "/dev/disk3s1 on / (apfs, local, journaled)",
            "//user@host/share on /Volumes/Remote Drive mounted via automount",  # no (opts)
        ]
        result = describe_path_support(
            "/Volumes/Remote Drive/teamcraft.db", mount_lines=unparseable_spacey
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("unparsed", result["reason"])

        # Even when a *shallower* parseable local entry covers the target.
        mixed = [
            "//user@host/share on /Volumes/Remote Drive mounted via automount",
            "/dev/disk3s1 on /Volumes (apfs, local, journaled)",
        ]
        result = describe_path_support(
            "/Volumes/Remote Drive/db.sqlite", mount_lines=mixed
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("unparsed", result["reason"])

    def test_temp_directory_is_classified_local(self) -> None:
        """Test databases must sit on an explicitly local path (review requirement)."""
        support = describe_path_support(self.db)
        self.assertEqual(support["status"], "local", support)

    def test_connect_refuses_identifiable_remote_mount(self) -> None:
        fake = {
            "status": "remote",
            "method": "mount-table",
            "reason": "mount /Volumes/share fstype=smbfs",
        }
        with mock.patch.object(storage, "describe_path_support", return_value=fake):
            with self.assertRaises(UnsupportedLocationError):
                connect(self.db)
            with self.assertRaises(UnsupportedLocationError):
                migrate_to_head(self.db)
            # Remote cannot be acknowledged away either.
            with self.assertRaises(UnsupportedLocationError):
                connect(self.db, allow_unverified_location=True)
            with self.assertRaises(UnsupportedLocationError):
                migrate_to_head(self.db, allow_unverified_location=True)
        # Refusal happened before any file was created.
        self.assertFalse(self.db.exists())

    def test_unknown_location_requires_explicit_auditable_ack(self) -> None:
        """Unknown filesystem status is refused by default, not silently allowed.

        WAL support must not launder ``unknown`` into accepted; the caller has
        to opt in with the named, grep-able acknowledgement flag.
        """
        fake = {
            "status": "unknown",
            "method": "none",
            "reason": "mount table unavailable",
        }
        with mock.patch.object(storage, "describe_path_support", return_value=fake):
            with self.assertRaises(UnsupportedLocationError) as ctx:
                connect(self.db)
            self.assertIn("locality proof", str(ctx.exception))
            self.assertIn("allow_unverified_location", str(ctx.exception))
            with self.assertRaises(UnsupportedLocationError):
                migrate_to_head(self.db)
        self.assertFalse(self.db.exists())  # refused before file creation

        # With the explicit human acknowledgement the operation proceeds.
        conn = connect(self.db, allow_unverified_location=True)
        conn.close()
        report = migrate_to_head(self.db, allow_unverified_location=True)
        self.assertTrue(report.fresh)

    def test_wal_probe_wording_does_not_claim_local_proof(self) -> None:
        """Honesty guard: WAL success is capability evidence, not locality proof."""
        module_doc = " ".join(storage.__doc__.lower().split())
        self.assertIn("capability evidence", module_doc)
        connect_doc = " ".join(storage.connect.__doc__.lower().split())
        self.assertIn("capability probe", connect_doc)
        self.assertIn("not proof of locality", connect_doc)
        for claim in ("proves this is a local disk", "guarantees a local disk"):
            self.assertNotIn(claim, module_doc)
            self.assertNotIn(claim, connect_doc)

    # ------------------------------------------------------ path guards
    def test_invalid_paths_are_rejected(self) -> None:
        cases = [
            ("relative path", "relative/teamcraft.db"),
            ("missing parent", str(self.base / "nope" / "teamcraft.db")),
            ("/dev/null", "/dev/null"),
        ]
        for label, path in cases:
            with self.subTest(label):
                with self.assertRaises(UnsupportedLocationError):
                    connect(path)
        # Existing directory as the database target:
        with self.assertRaises(UnsupportedLocationError):
            connect(self.base)
        # Symlinked database file:
        real = self.base / "real.db"
        migrate_to_head(real)
        link = self.base / "link.db"
        link.symlink_to(real)
        with self.assertRaises(UnsupportedLocationError):
            connect(link)
        with self.assertRaises(UnsupportedLocationError):
            migrate_to_head(link)

    def test_verify_database_reports_healthy_v1(self) -> None:
        migrate_to_head(self.db)
        report = verify_database(self.db)
        self.assertTrue(report["ok"])
        self.assertEqual(report["integrity"], "ok")
        self.assertEqual(report["user_version"], SCHEMA_VERSION)
        self.assertEqual(set(report["tables"]), EXPECTED_TABLES)


if __name__ == "__main__":
    unittest.main()
