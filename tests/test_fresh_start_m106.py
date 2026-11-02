"""M1-06 SQLite Fresh Start validation contract tests (Owner 2026-11-02).

Everything here runs against synthetic project roots inside
``tempfile.TemporaryDirectory`` (run with ``TMPDIR="$PWD/.tmp-m106"`` so all
data stays repository-local).  Nothing reads or writes the real
``.teamcraft/state.json`` or ``.teamcraft/teamcraft.db`` of this repository,
no Herdr session is contacted, and no agent receives a prompt.

Pinned contracts (SQLite-only Fresh Start; PLAN §11 policy, §9.1 M1-06):

* ``initialize_project_root`` creates ``.teamcraft/teamcraft.db`` at the
  complete current schema (version + integrity verified via
  ``storage.verify_database``) and reopens it through ``storage.connect``;
* repeated initialization is idempotent — same inode, identical row content,
  identical schema audit, nothing rewritten;
* an existing valid database is adopted: its rows are untouched;
* unsupported (too-new schema), non-SQLite, internally corrupt, symlinked or
  irregular database files are refused WITHOUT modification or deletion;
* a non-empty legacy ``state.json`` (``.teamcraft/`` or ``.herdr-team/``)
  fails closed with bytes preserved; zero-byte residue is preserved too;
* unwritable project roots give a clear error naming the directory;
* every refusal happens before any durable write; a real SIGKILL at the live
  DDL boundary (``storage.MIGRATION_STATEMENT_HOOK``) leaves either no
  database or a schema-version-0 remnant that ``verify_database`` never
  reports as head/usable — never an apparently-ready partial database — and
  a retry converges;
* concurrent first initialization converges onto one verified database;
* conservative path/symlink checks (root, runtime dir, database);
* the legacy migration-only module (``teamcraft.migration``, 1477cc0) is gone
    and readiness has exactly one authority: the verified SQLite schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from teamcraft import fresh_start as fs
from teamcraft import storage
from teamcraft.errors import SchemaTooNewError, ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
SQLITE_MAGIC = b"SQLite format 3\x00"
DB_MODE = 0o600


def _fingerprint(path: Path) -> tuple[int, int, int, int, str]:
    info = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return (
        int(info.st_ino),
        int(info.st_dev),
        int(info.st_size),
        int(info.st_mtime_ns),
        digest.hexdigest(),
    )


def _dump_tables(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Read-only dump of every user table (content preservation evidence)."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            name: sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {name}"))
            for name in names
        }
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _journal_mode(db_path: Path) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()


class FreshStartTempCase(unittest.TestCase):
    """Shared temp-root plumbing; every root lives under TMPDIR only."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-fresh-")
        self.base = Path(self._tmp.name).resolve()
        # Boundary pin: all fixtures must stay repository-local (TMPDIR).
        self.assertTrue(
            str(self.base).startswith(str(Path(tempfile.gettempdir()).resolve()))
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_root(self, name: str = "project") -> Path:
        root = self.base / name
        root.mkdir()
        return root

    def db(self, root: Path) -> Path:
        return root / fs.RUNTIME_DIRNAME / fs.DATABASE_FILENAME


class CreationAndVerificationTests(FreshStartTempCase):
    def test_creates_runtime_dir_and_database_for_new_root(self) -> None:
        root = self.make_root()
        report = fs.initialize_project_root(root)
        self.assertTrue(report.created)
        self.assertFalse(report.reused)
        self.assertTrue(report.at_head)
        self.assertEqual(report.integrity, "ok")
        self.assertEqual(report.schema_version, storage.SCHEMA_VERSION)
        self.assertEqual(report.database_path, str(self.db(root)))
        runtime = root / fs.RUNTIME_DIRNAME
        self.assertTrue(runtime.is_dir())
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode) & 0o077, 0)
        db = self.db(root)
        self.assertTrue(db.is_file())
        self.assertEqual(stat.S_IMODE(db.stat().st_mode), DB_MODE)
        self.assertEqual(_journal_mode(db), "wal")

    def test_current_schema_complete_and_version_recorded(self) -> None:
        root = self.make_root()
        fs.initialize_project_root(root)
        db = self.db(root)
        report = storage.verify_database(db)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["schema_usable"], report)
        self.assertTrue(report["at_head"], report)
        self.assertEqual(report["user_version"], storage.SCHEMA_VERSION)
        self.assertEqual(report["integrity"], "ok")
        self.assertEqual(report["missing_head_tables"], [])
        self.assertEqual(report["missing_head_objects"], [])
        self.assertEqual(report["schema_migrations_recorded"], storage.SCHEMA_VERSION)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            recorded = sorted(
                int(row[0])
                for row in conn.execute("SELECT version FROM schema_migrations")
            )
            self.assertEqual(recorded, list(range(1, storage.SCHEMA_VERSION + 1)))
        finally:
            conn.close()

    def test_repeated_init_is_idempotent(self) -> None:
        root = self.make_root()
        first = fs.initialize_project_root(root)
        db = self.db(root)
        inode_before = db.stat().st_ino
        dump_before = _dump_tables(db)
        migrations_before = len(dump_before.get("schema_migrations", []))
        second = fs.initialize_project_root(root)
        self.assertTrue(second.reused)
        self.assertFalse(second.created)
        self.assertEqual(second.schema_version, first.schema_version)
        self.assertEqual(second.integrity, first.integrity)
        self.assertEqual(db.stat().st_ino, inode_before)
        self.assertEqual(_dump_tables(db), dump_before)
        self.assertEqual(
            len(_dump_tables(db).get("schema_migrations", [])), migrations_before
        )
        self.assertEqual(_user_version(db), storage.SCHEMA_VERSION)

    def test_reopen_ready_database_supports_runtime_writes(self) -> None:
        root = self.make_root()
        fs.initialize_project_root(root)
        conn = storage.connect(self.db(root))
        try:
            with storage.transaction(conn):
                conn.execute(
                    "INSERT INTO projects (id, name, repo_path, config_json,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        "prj_fresh_1",
                        "p",
                        str(root),
                        "{}",
                        "2026-11-02T00:00:00+00:00",
                    ),
                )
            row = conn.execute(
                "SELECT name FROM projects WHERE id = ?", ("prj_fresh_1",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(str(row[0]), "p")
        finally:
            conn.close()
        report = storage.verify_database(self.db(root))
        self.assertTrue(report["ok"] and report["at_head"], report)

    def test_report_as_dict_is_json_serialisable(self) -> None:
        root = self.make_root()
        report = fs.initialize_project_root(root)
        payload = json.loads(json.dumps(report.as_dict()))
        self.assertEqual(payload["project_root"], str(root))
        self.assertTrue(payload["at_head"])


class PreservationTests(FreshStartTempCase):
    def test_existing_valid_database_rows_unchanged(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        db = self.db(root)
        storage.migrate_to_head(db)
        conn = storage.connect(db)
        try:
            with storage.transaction(conn):
                conn.execute(
                    "INSERT INTO projects (id, name, repo_path, config_json,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        "prj_keep_1",
                        "keep",
                        "/synthetic/keep",
                        "{}",
                        "2026-11-01T00:00:00+00:00",
                    ),
                )
        finally:
            conn.close()
        dump_before = _dump_tables(db)
        inode_before = db.stat().st_ino
        report = fs.initialize_project_root(root)
        self.assertTrue(report.reused)
        self.assertFalse(report.created)
        self.assertEqual(db.stat().st_ino, inode_before)
        self.assertEqual(_dump_tables(db), dump_before)
        self.assertEqual(_user_version(db), storage.SCHEMA_VERSION)

    def test_nonempty_legacy_state_refused_and_preserved(self) -> None:
        for dirname in (fs.RUNTIME_DIRNAME, fs.LEGACY_RUNTIME_DIRNAME):
            with self.subTest(dir=dirname):
                root = self.make_root(f"legacy-{dirname.strip('.')}")
                directory = root / dirname
                directory.mkdir()
                legacy = directory / fs.LEGACY_STATE_FILENAME
                payload = json.dumps(
                    {"version": 1, "team_id": "team-x", "project_root": str(root)}
                ).encode("utf-8")
                legacy.write_bytes(payload)
                before = _fingerprint(legacy)
                with self.assertRaises(fs.LegacyStatePresentError) as caught:
                    fs.initialize_project_root(root)
                self.assertEqual(caught.exception.path, str(legacy))
                self.assertEqual(caught.exception.size, len(payload))
                # Bytes preserved; no database created; nothing deleted.
                self.assertEqual(_fingerprint(legacy), before)
                self.assertFalse(self.db(root).exists())

    def test_legacy_state_symlink_refused_without_following_target(self) -> None:
        root = self.make_root()
        outside = self.base / "outside-secret.json"
        payload = b'{"version": 1, "secret": "do-not-touch"}'
        outside.write_bytes(payload)
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        legacy = runtime / fs.LEGACY_STATE_FILENAME
        legacy.symlink_to(outside)
        before = _fingerprint(outside)
        with self.assertRaises(fs.LegacyStatePresentError):
            fs.initialize_project_root(root)
        self.assertEqual(_fingerprint(outside), before)
        self.assertTrue(legacy.is_symlink())
        self.assertFalse(self.db(root).exists())

    def test_zero_byte_legacy_residue_preserved_and_init_succeeds(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        legacy = runtime / fs.LEGACY_STATE_FILENAME
        legacy.write_bytes(b"")
        report = fs.initialize_project_root(root)
        self.assertTrue(report.at_head)
        # Preserved, not deleted, not treated as state.
        self.assertTrue(legacy.exists())
        self.assertEqual(legacy.stat().st_size, 0)


class UnsupportedAndCorruptDatabaseTests(FreshStartTempCase):
    def test_rejects_too_new_schema_without_modifying(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        db = self.db(root)
        conn = sqlite3.connect(db)
        try:
            conn.execute(f"PRAGMA user_version = {storage.SCHEMA_VERSION + 1}")
        finally:
            conn.close()
        before = _fingerprint(db)
        with self.assertRaises(SchemaTooNewError):
            fs.initialize_project_root(root)
        self.assertEqual(_fingerprint(db), before)

    def test_rejects_non_sqlite_garbage_without_modifying(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        db = self.db(root)
        db.write_bytes(b"not a database at all, just junk bytes")
        before = _fingerprint(db)
        with self.assertRaises(fs.UnsupportedDatabaseFileError) as caught:
            fs.initialize_project_root(root)
        self.assertEqual(caught.exception.path, str(db))
        self.assertEqual(_fingerprint(db), before)

    def test_rejects_internally_corrupt_database_and_keeps_file(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        db = self.db(root)
        db.write_bytes(SQLITE_MAGIC + os.urandom(8192))
        before = _fingerprint(db)
        # Header passes, SQLite itself refuses the body: the refusal surfaces
        # as a Fresh Start refusal or the pinned storage-layer error class;
        # the contract is "refused + file byte-identical afterwards".
        with self.assertRaises((fs.FreshStartError, storage.StorageError)):
            fs.initialize_project_root(root)
        self.assertEqual(_fingerprint(db), before)

    def test_rejects_directory_at_database_path(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        (runtime / fs.DATABASE_FILENAME).mkdir()
        with self.assertRaises(fs.UnsupportedDatabaseFileError):
            fs.initialize_project_root(root)

    def test_rejects_fifo_at_database_path(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        fifo = runtime / fs.DATABASE_FILENAME
        os.mkfifo(fifo)
        with self.assertRaises(fs.UnsupportedDatabaseFileError):
            fs.initialize_project_root(root)
        self.assertTrue(fifo.is_fifo())  # not deleted

    def test_adopts_empty_existing_database_file(self) -> None:
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        db = self.db(root)
        db.write_bytes(b"")  # zero-length: a brand-new database to SQLite
        report = fs.initialize_project_root(root)
        self.assertTrue(report.created)
        self.assertTrue(report.at_head)


class PathSafetyTests(FreshStartTempCase):
    def test_relative_or_empty_root_refused(self) -> None:
        with self.assertRaises(ValidationError):
            fs.initialize_project_root("relative/project")
        with self.assertRaises(ValidationError):
            fs.initialize_project_root("   ")

    def test_missing_root_refused(self) -> None:
        with self.assertRaises(ValidationError):
            fs.initialize_project_root(self.base / "does-not-exist")

    def test_symlinked_project_root_refused_and_target_untouched(self) -> None:
        real = self.make_root("real")
        link = self.base / "link-to-project"
        link.symlink_to(real)
        with self.assertRaises(ValidationError):
            fs.initialize_project_root(link)
        self.assertFalse((real / fs.RUNTIME_DIRNAME).exists())

    def test_symlinked_runtime_dir_refused_and_target_untouched(self) -> None:
        root = self.make_root()
        outside = self.base / "escape-target"
        outside.mkdir()
        (root / fs.RUNTIME_DIRNAME).symlink_to(outside)
        with self.assertRaises(ValidationError):
            fs.initialize_project_root(root)
        self.assertFalse((outside / fs.DATABASE_FILENAME).exists())

    def test_symlinked_database_refused_without_touching_target(self) -> None:
        # The symlink target is a perfectly valid head database elsewhere;
        # Fresh Start still refuses to adopt it through a symlink.
        other_root = self.make_root("other")
        fs.initialize_project_root(other_root)
        target_db = self.db(other_root)
        target_before = _fingerprint(target_db)
        root = self.make_root()
        runtime = root / fs.RUNTIME_DIRNAME
        runtime.mkdir()
        (runtime / fs.DATABASE_FILENAME).symlink_to(target_db)
        with self.assertRaises(fs.UnsupportedDatabaseFileError):
            fs.initialize_project_root(root)
        self.assertEqual(_fingerprint(target_db), target_before)

    def test_nonempty_runtime_file_blocks_with_clear_error(self) -> None:
        root = self.make_root()
        (root / fs.RUNTIME_DIRNAME).write_bytes(b"not a directory")
        with self.assertRaises(ValidationError):
            fs.initialize_project_root(root)

    def test_unwritable_project_root_clear_error(self) -> None:
        root = self.make_root("locked-project")
        root.chmod(0o500)
        try:
            with self.assertRaises(fs.FreshStartError) as caught:
                fs.initialize_project_root(root)
            message = str(caught.exception)
            self.assertIn(str(root), message)
            self.assertIn("writable", message)
            self.assertFalse(self.db(root).exists())
        finally:
            root.chmod(0o700)


class FaultInjectionTests(FreshStartTempCase):
    """Crash/refusal matrix: nothing durable survives a failed init.

    The Fresh Start API deliberately exposes no in-process fault seam; crash
    injection therefore goes through ``storage.MIGRATION_STATEMENT_HOOK`` and
    a real ``SIGKILL`` inside the live migration transaction (the same audited
    pattern as ``tests/test_storage_m103.py``), exercising the actual
    ``initialize_project_root`` code path.
    """

    def test_sigkill_mid_migration_never_leaves_apparently_ready_db(self) -> None:
        root = self.make_root("crash-init")
        db = self.db(root)
        flag = self.base / "mid-migration.flag"
        script = (
            "import os, sys, time\n"
            "barrier, idx, root, flag, repo = (\n"
            "    sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]\n"
            ")\n"
            "sys.path.insert(0, repo)\n"
            "from teamcraft import storage\n"
            "from teamcraft.fresh_start import initialize_project_root\n"
            "def hook(index, statement):\n"
            "    if index == 2:  # three DDL statements already executed\n"
            "        with open(flag, 'w') as fh:\n"
            "            fh.write('mid-migration')\n"
            "            fh.flush()\n"
            "            os.fsync(fh.fileno())\n"
            "        time.sleep(30)  # still inside BEGIN IMMEDIATE, uncommitted\n"
            "storage.MIGRATION_STATEMENT_HOOK = hook\n"
            "initialize_project_root(root)\n"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                "unused-barrier",
                "0",
                str(root),
                str(flag),
                str(REPO_ROOT),
            ]
        )
        deadline = time.monotonic() + 20
        while not flag.exists():
            if proc.poll() is not None:
                self.fail("child exited before reaching mid-migration")
            self.assertLess(time.monotonic(), deadline, "child never reached mid-migration")
            time.sleep(0.02)
        os.kill(proc.pid, signal.SIGKILL)  # die mid-transaction, partial DDL pending
        self.assertEqual(proc.wait(timeout=15), -int(signal.SIGKILL))

        # Residue is NEVER ready-looking: uncommitted DDL does not survive and
        # the schema cannot claim head (strict mode=ro read only).
        self.assertTrue(db.exists())  # retained, never auto-deleted
        report = storage.verify_database(db)
        self.assertEqual(report["user_version"], 0, report)
        self.assertFalse(report["at_head"], report)
        self.assertFalse(report["schema_usable"], report)

        # Restart converges through the public API.
        retry = fs.initialize_project_root(root)
        self.assertTrue(retry.created)
        self.assertTrue(retry.at_head)
        self.assertEqual(retry.schema_version, storage.SCHEMA_VERSION)
        after = storage.verify_database(db)
        self.assertTrue(after["ok"] and after["at_head"], after)

    def test_refusals_happen_before_any_durable_write(self) -> None:
        # (a) legacy state in the pre-rename directory is refused BEFORE the
        # current runtime directory is even created.
        root_a = self.make_root("refuse-a")
        legacy_dir = root_a / fs.LEGACY_RUNTIME_DIRNAME
        legacy_dir.mkdir()
        (legacy_dir / fs.LEGACY_STATE_FILENAME).write_bytes(b'{"version": 1}')
        with self.assertRaises(fs.LegacyStatePresentError):
            fs.initialize_project_root(root_a)
        self.assertFalse((root_a / fs.RUNTIME_DIRNAME).exists())
        self.assertEqual(
            (legacy_dir / fs.LEGACY_STATE_FILENAME).read_bytes(), b'{"version": 1}'
        )

        # (b) a junk file at the database position is refused untouched: the
        # migration never got the chance to write anything into it.
        root_b = self.make_root("refuse-b")
        runtime_b = root_b / fs.RUNTIME_DIRNAME
        runtime_b.mkdir()
        junk = runtime_b / fs.DATABASE_FILENAME
        junk.write_bytes(b"junk bytes, not sqlite")
        before = _fingerprint(junk)
        with self.assertRaises(fs.UnsupportedDatabaseFileError):
            fs.initialize_project_root(root_b)
        self.assertEqual(_fingerprint(junk), before)

        # (c) an unwritable project root produces no database artefact.
        root_c = self.make_root("refuse-c")
        root_c.chmod(0o500)
        try:
            with self.assertRaises(fs.FreshStartError):
                fs.initialize_project_root(root_c)
            self.assertFalse(self.db(root_c).exists())
        finally:
            root_c.chmod(0o700)


class ConcurrencyTests(FreshStartTempCase):
    def test_concurrent_first_init_subprocesses_converge(self) -> None:
        root = self.make_root("concurrent")
        db = self.db(root)
        barrier = self.base / "barrier"
        barrier.mkdir()
        script = (
            "import os, sys, time\n"
            "barrier, idx, root, repo = (\n"
            "    sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]\n"
            ")\n"
            "sys.path.insert(0, repo)\n"
            "from teamcraft import initialize_project_root\n"
            "open(os.path.join(barrier, 'ready-' + str(idx)), 'w').close()\n"
            "deadline = time.monotonic() + 15\n"
            "while not (os.path.exists(os.path.join(barrier, 'ready-0'))"
            " and os.path.exists(os.path.join(barrier, 'ready-1'))):\n"
            "    if time.monotonic() > deadline:\n"
            "        print('barrier-timeout', file=sys.stderr)\n"
            "        sys.exit(3)\n"
            "    time.sleep(0.005)\n"
            "try:\n"
            "    initialize_project_root(root, busy_timeout_ms=20000)\n"
            "except Exception as exc:\n"
            "    print('ERROR: ' + repr(exc), file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print('OK')\n"
        )
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    script,
                    str(barrier),
                    str(i),
                    str(root),
                    str(REPO_ROOT),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in (0, 1)
        ]
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=60)
            self.assertEqual(
                proc.returncode,
                0,
                f"initializer failed: stdout={stdout!r} stderr={stderr!r}",
            )
        # Exactly one authority database, verified complete at head, and the
        # schema ladder exists exactly once.
        report = storage.verify_database(db)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["at_head"], report)
        self.assertTrue(report["schema_usable"], report)
        self.assertEqual(report["schema_migrations_recorded"], storage.SCHEMA_VERSION)
        self.assertEqual(
            sorted(
                p.name
                for p in (root / fs.RUNTIME_DIRNAME).iterdir()
                if p.name.endswith(".db")
            ),
            [fs.DATABASE_FILENAME],
        )
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            ladder = sorted(
                int(row[0])
                for row in conn.execute("SELECT version FROM schema_migrations")
            )
            self.assertEqual(ladder, list(range(1, storage.SCHEMA_VERSION + 1)))
        finally:
            conn.close()
        # A further sequential init reuses without change (idempotent merge).
        follow_up = fs.initialize_project_root(root)
        self.assertTrue(follow_up.reused)


class SupersededModuleTests(unittest.TestCase):
    def test_legacy_migration_module_removed(self) -> None:
        import importlib.util

        # The migration-only module (legacy JSON rehearsal, 1477cc0) is
        # superseded by SQLite-only Fresh Start and must not return.
        self.assertIsNone(importlib.util.find_spec("teamcraft.migration"))

    def test_fresh_start_exports(self) -> None:
        import teamcraft

        for name in (
            "initialize_project_root",
            "FreshStartReport",
            "FreshStartError",
            "LegacyStatePresentError",
            "UnsupportedDatabaseFileError",
            "FreshStartIncompleteError",
        ):
            self.assertIn(name, teamcraft.__all__)
            self.assertTrue(hasattr(teamcraft, name))


if __name__ == "__main__":
    unittest.main()
