"""M1-03 SQLite repository boundary: connection, migration v1, backup/restore.

Scope is deliberately narrow (M0 review 9.1 M1-03 / 8.2-1, 7, 13, 14):

* explicit-path connections with ``foreign_keys``, ``journal_mode=WAL`` and
  ``busy_timeout`` enforced;
* atomic schema migration v1 with version recording (``user_version`` plus an
  audit table), executed *inside one exclusive transaction with re-checks*, so
  concurrent migrators converge instead of colliding on "table exists";
* a hard refusal of any database whose schema version is newer than this
  build, enforced in three layers against TOCTOU: a fail-closed read-only
  gate *before* any write-capable open, a re-check on the writable connection
  *before* persistent pragmas, and re-checks under the exclusive write lock
  in ``transaction()``/``migrate_to_head``; ``connect`` returns a narrowed
  ``TeamCraftConnection`` facade that keeps callers off the writable handle
  entirely — reads run on a strict ``mode=ro`` channel, the hidden writable
  connection carries a permanent ``set_authorizer`` that refuses every
  mutating SQLite action code outside the gate (no SQL-text parsing is used
  as a security boundary), chained cursors are controlled facades, and
  ``executescript``/``commit``/``rollback`` are not exposed; the page-level
  Online Backup API bypasses SQL authorization and is therefore only offered
  through ``backup_database``/``restore_backup`` with O_EXCL-exclusive fresh
  targets that are never overwritten (review R6); durable writes run inside
  ``transaction()`` (deliberate reflection into private attributes
  is trusted-first-party scope, not claimed defended);
* base tables only: ``schema_migrations``, ``projects``, ``tasks``,
  ``agents``, ``attempts``, ``sessions``.  Task dependencies, events and
  commands (M1-04/M1-05), the full state machine, Watcher, Web, Worktree and
  Router are NOT part of this module;
* corrected PRD DDL points: a database-enforced single *occupying* attempt per
  task (created/starting/active/unknown all hold the slot — an unresolved
  attempt must never allow parallel re-dispatch) and nullable Commit/report
  evidence columns so both ship and scout samples are expressible;
* consistent backups via the SQLite Online Backup API (never a bare copy of
  the main ``.db`` while the WAL is live), manifests carrying schema/app
  versions, digests, size, filename and an applicable generation, restore that
  cross-verifies the *snapshot itself* against the manifest, retention as a
  conservative dry-run entry point that can never delete protected or
  generation-unbound recovery evidence, and half-written artefacts cleaned up on
  every failure path.

Local-disk boundary: a successful WAL enablement is capability evidence, NOT a
proof of a local disk.  Identifiable network/sync mounts (from the OS mount
table) are rejected outright.  Locations that cannot be classified (``unknown``)
are refused too, unless the caller passes the explicit, auditable
``allow_unverified_location=True`` acknowledgement — WAL support alone never
converts ``unknown`` into accepted.

Read-only gates use strict ``mode=ro`` only and fail closed when that open
fails.  SQLite's WAL-bypassing read-only URI option is never used here: it is
documented solely for provably static files, it ignores un-checkpointed WAL
frames, and on mutable databases it can silently return stale content (for
example an old ``user_version`` while the WAL holds a newer committed one).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .clock import SystemClock, require_utc
from .errors import (
    BackupError,
    CorruptDatabaseError,
    MigrationError,
    SchemaTooNewError,
    StorageError,
    UnsupportedLocationError,
    WriteOutsideTransactionError,
)

#: Highest schema version this build understands.
SCHEMA_VERSION = 1

#: Mirrors ``pyproject.toml`` until a single source of truth is chosen.
APP_VERSION = "0.2.0.dev0"

#: Default database filename inside the project runtime directory (8.2-1).
DEFAULT_DATABASE_FILENAME = "teamcraft.db"

#: Attempt statuses that occupy the task's single concurrent-attempt slot.
#: An UNKNOWN attempt keeps the slot: unknown must not be treated as zero and
#: must not permit re-dispatch (M0 review 8.2-11).  M1-04 owns the full state
#: machine and must keep or consciously replace this set together with
#: ``ux_attempts_single_occupying``.
OCCUPYING_ATTEMPT_STATUSES: tuple[str, ...] = (
    "created",
    "starting",
    "active",
    "unknown",
)

#: Delivery kinds recognised by the v1 ``tasks.kind`` CHECK (PLAN §4).
TASK_KINDS = ("ship", "scout")

#: Test seam: when set, called as ``hook(index, statement)`` after each v1 DDL
#: statement *inside* the live migration transaction (before COMMIT).  Used by
#: the crash-mid-migration regression to observe the real ``migrate_to_head``
#: transaction boundary; production code must leave this ``None``.  An exception
#: raised by the hook rolls the migration back like any other failure.
MIGRATION_STATEMENT_HOOK: Callable[[int, str], None] | None = None

_V1_TABLES = frozenset(
    {"schema_migrations", "projects", "tasks", "agents", "attempts", "sessions"}
)

_V1_STATEMENTS = (
    """
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
)""",
    """
CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  repo_path TEXT NOT NULL UNIQUE,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
)""",
    """
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  harness TEXT NOT NULL,
  model TEXT,
  provider TEXT,
  config_json TEXT NOT NULL DEFAULT '{}'
)""",
    """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL DEFAULT 'ship' CHECK (kind IN ('ship', 'scout')),
  priority INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL,
  risk TEXT NOT NULL DEFAULT 'normal',
  ownership_json TEXT NOT NULL DEFAULT '[]',
  version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""",
    """
CREATE TABLE attempts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  agent_id TEXT REFERENCES agents(id),
  attempt_no INTEGER NOT NULL,
  status TEXT NOT NULL,
  worktree_path TEXT,
  branch TEXT,
  lease_owner TEXT,
  lease_expires_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  commit_sha TEXT,
  report_ref TEXT,
  result_json TEXT,
  UNIQUE (task_id, attempt_no)
)""",
    """
CREATE UNIQUE INDEX ux_attempts_single_occupying
  ON attempts (task_id) WHERE upper(status) IN ('CREATED', 'STARTING', 'ACTIVE', 'UNKNOWN')""",
    """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  attempt_id TEXT REFERENCES attempts(id),
  backend TEXT NOT NULL,
  native_session_id TEXT,
  pane_id TEXT,
  state TEXT NOT NULL,
  last_observed_at TEXT,
  created_at TEXT NOT NULL
)""",
)

_REMOTE_FSTYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb2",
        "afpfs",
        "webdav",
        "davfs",
        "curlfs",
        "sshfs",
        "fuse.sshfs",
        "fuse",
        "s3fs",
        "glusterfs",
        "ceph",
        "9p",
        "9ps",
    }
)
_LOCAL_FSTYPES = frozenset(
    {"apfs", "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "hfs", "hfsplus", "f2fs"}
)


def _utc_now_iso() -> str:
    return require_utc(SystemClock().now()).isoformat()


def _sqlite_uri(path: Path, params: str) -> str:
    """Build a file URI with the path percent-encoded (RFC 3986).

    ``#``, ``?`` and ``%`` in file names MUST NOT terminate or corrupt the
    path component; otherwise SQLite silently opens/creates a *different*
    file, defeating every safety gate.
    """

    encoded = (
        path.as_posix()
        .replace("%", "%25")
        .replace("#", "%23")
        .replace("?", "%3f")
        .replace(" ", "%20")
    )
    return f"file:{encoded}?{params}"


def _validate_database_path(path: str | os.PathLike[str]) -> Path:
    """Reject structurally invalid locations before touching SQLite."""

    candidate = Path(path)
    if not candidate.is_absolute():
        raise UnsupportedLocationError(f"database path must be absolute: {path!s}")
    resolved_shape = candidate.resolve(strict=False)
    if resolved_shape.parts[:2] == ("/", "dev"):
        raise UnsupportedLocationError(f"device paths are not supported: {path!s}")
    if candidate.name in ("", ".", ".."):
        raise UnsupportedLocationError(f"invalid database filename: {path!s}")
    parent = candidate.parent
    if not parent.is_dir():
        raise UnsupportedLocationError(
            f"database parent directory does not exist: {parent}"
        )
    if candidate.is_symlink():
        raise UnsupportedLocationError(
            f"database path must not be a symlink: {path!s}"
        )
    if candidate.exists():
        mode = candidate.stat().st_mode
        if not stat.S_ISREG(mode):
            raise UnsupportedLocationError(
                f"database path exists but is not a regular file: {path!s}"
            )
    if not os.access(parent, os.R_OK | os.W_OK | os.X_OK):
        raise UnsupportedLocationError(f"database directory is not usable: {parent}")
    if candidate.exists() and not os.access(candidate, os.R_OK | os.W_OK):
        raise UnsupportedLocationError(
            f"database file is not readable/writable: {path!s}"
        )
    return candidate


def _mount_lines() -> list[str]:
    """OS mount table lines (best effort, empty when unavailable)."""

    for program in ("/sbin/mount", "/bin/mount", "/usr/bin/mount", "mount"):
        try:
            result = subprocess.run(
                [program], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout.splitlines()
    try:
        return Path("/proc/mounts").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _unescape_mount_path(token: str) -> str:
    """Undo the octal escapes mount/proc use for whitespace and backslash."""

    for escape, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        token = token.replace(escape, char)
    return token


def _covers(target: str, point: str) -> bool:
    return target == point or target.startswith(point.rstrip("/") + "/") or point == "/"


def _unparsed_candidate_depth(target: str, line: str) -> int:
    """Depth of any path-like candidate inside an unparseable mount line.

    A line whose format we cannot parse might still describe a mount that
    covers ``target`` (e.g. a BSD-style line with spaces in the point and no
    option group).  Consecutive tokens forming a prefix path of ``target`` are
    taken as candidates; a positive depth means "this line may cover the
    target and we could not read its filesystem type" — which must never
    resolve to ``local``.
    """

    tokens = line.split()
    best = 0
    for start, token in enumerate(tokens):
        if not token.startswith("/"):
            continue
        candidate = token
        for follow in tokens[start + 1 :]:
            if _covers(target, candidate):
                best = max(best, len(candidate))
            if follow.startswith("(") or follow.endswith(")") or follow == "on":
                break
            candidate = f"{candidate} {follow}"
        if _covers(target, candidate):
            best = max(best, len(candidate))
    return best


def _classify_mount(path: Path, lines: list[str]) -> dict[str, str]:
    """Classify ``path`` against mount table lines.

    Returns status ``local`` / ``remote`` / ``unknown``.  The deepest
    (longest-prefix) covering mount wins, which is what nested mounts require.
    Mount points may contain spaces — BSD lines keep them literal and
    ``/proc/mounts`` escapes them as ``\\040``; both are handled.  Lines that
    cannot be parsed *and* may cover the target fail closed to ``unknown``,
    including downgrading an otherwise-local classification; ``unknown`` is
    never treated as proof of locality.
    """

    target = str(path)
    best_point: str | None = None
    best_dev = ""
    best_fstype = ""
    best_opts = ""
    unparsed_depth = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        # macOS/BSD form: "<dev> on <point> (<opts>)" — the mount point may
        # contain literal spaces, so match it non-greedily up to the options.
        match = re.match(r"^(\S+) on (.+?) \((.*)\)\s*$", line)
        if match:
            device = match.group(1)
            point = _unescape_mount_path(match.group(2))
            opts = match.group(3)
            fstype = opts.split(",")[0].strip()
        else:
            # /proc/mounts form: "<dev> <point> <fstype> <opts> 0 0"
            parts = line.split()
            if len(parts) >= 3 and parts[1] != "on":
                device = parts[0]
                point = _unescape_mount_path(parts[1])
                fstype = parts[2]
                opts = parts[3] if len(parts) > 3 else ""
            else:
                unparsed_depth = max(unparsed_depth, _unparsed_candidate_depth(target, line))
                continue
        if _covers(target, point) and (best_point is None or len(point) > len(best_point)):
            best_point, best_dev, best_fstype, best_opts = point, device, fstype, opts

    if best_point is None:
        if unparsed_depth:
            return {
                "status": "unknown",
                "method": "mount-table",
                "reason": "unparsed mount entry may cover target; cannot confirm locality",
            }
        return {"status": "unknown", "method": "mount-table", "reason": "no covering mount entry"}
    if best_fstype in _REMOTE_FSTYPES or not best_dev.startswith(("/dev/", "\\Device")):
        return {
            "status": "remote",
            "method": "mount-table",
            "reason": f"mount {best_point} device={best_dev} fstype={best_fstype}",
        }
    if unparsed_depth > len(best_point):
        # A closer mount exists that we could not parse: fail closed.
        return {
            "status": "unknown",
            "method": "mount-table",
            "reason": "unparsed mount entry deeper than best parsed coverage; cannot confirm locality",
        }
    if best_fstype in _LOCAL_FSTYPES and best_dev.startswith(("/dev/", "\\Device")):
        return {"status": "local", "method": "mount-table", "reason": f"mount {best_point} ({best_opts})"}
    return {
        "status": "unknown",
        "method": "mount-table",
        "reason": f"mount {best_point} fstype={best_fstype} not classifiable",
    }


def describe_path_support(
    path: str | os.PathLike[str], *, mount_lines: list[str] | None = None
) -> dict[str, str]:
    """Best-effort local-disk classification (never a locality guarantee)."""

    candidate = Path(path)
    lines = _mount_lines() if mount_lines is None else mount_lines
    if not lines:
        return {"status": "unknown", "method": "none", "reason": "mount table unavailable"}
    return _classify_mount(candidate, lines)


def _ensure_supportable(path: Path, *, allow_unverified_location: bool = False) -> None:
    """Locality gate for write-side opens (8.2-1).

    ``remote`` is always refused.  ``unknown`` is refused unless the caller
    supplies the explicit, auditable ``allow_unverified_location=True``
    acknowledgement; enabling WAL afterwards is *not* accepted as locality
    proof.
    """

    support = describe_path_support(path)
    status = support["status"]
    if status == "remote":
        raise UnsupportedLocationError(
            f"network/sync mount detected for {path}: {support['reason']}; "
            "SQLite single-writer databases must live on a local disk (8.2-1)"
        )
    if status == "unknown" and not allow_unverified_location:
        raise UnsupportedLocationError(
            f"cannot verify {path} sits on a local disk ({support['reason']}); "
            "WAL support alone is not locality proof. Pass "
            "allow_unverified_location=True only after an auditable human "
            "confirmation that this location is acceptable."
        )


@contextlib.contextmanager
def _open_read_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Strict ``mode=ro`` connection — fail closed, no fallback of any kind.

    SQLite's WAL-bypassing read-only URI option is deliberately never used: it
    ignores un-checkpointed WAL frames and SQLite documents it only for files
    that provably cannot change.  Against a live database it can return a stale
    main-file view (for example ``user_version`` 1 while the WAL holds a
    committed 99), which is exactly the class of bug this gate exists to
    prevent.  If the strict read-only open fails, the database is reported
    unreadable and the operation is refused; a read-write connection is NEVER
    opened as a stand-in for a read-only probe.
    """

    try:
        conn = sqlite3.connect(_sqlite_uri(path, "mode=ro"), uri=True)
    except sqlite3.Error as exc:
        raise CorruptDatabaseError(
            f"database cannot be opened strictly read-only (suspected corruption "
            f"or unreadable WAL state); refusing to proceed: {exc}"
        ) from exc
    try:
        yield conn
    finally:
        conn.close()


def _read_user_version(path: Path) -> int:
    with _open_read_connection(path) as conn:
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            raise CorruptDatabaseError(
                f"database header/version unreadable (suspected corruption): {exc}"
            ) from exc
        return int(row[0]) if row else 0


def _exclusive_create(path: Path) -> int | None:
    """Atomically create ``path`` with O_EXCL; return the open fd, or None.

    None means the file already existed (created by someone else): callers
    must refuse and must never delete it.  Any other OSError fails closed.
    """

    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        return None
    except OSError as exc:
        raise BackupError(f"cannot create {path}: {exc}") from exc


def _fd_token(fd: int) -> tuple[int, int]:
    """Ownership token ``(st_dev, st_ino)`` for a file this call just created."""

    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _still_ours(path: Path, token: tuple[int, int] | None) -> bool:
    """True iff ``path`` still holds the exact file this call created."""

    if token is None:
        return False
    try:
        st = os.stat(str(path))
    except FileNotFoundError:
        return False
    return (st.st_dev, st.st_ino) == token


def _discard_if_still_ours(path: Path, token: tuple[int, int] | None) -> None:
    """Delete a file only if it is still the exact file this call created.

    The inode token pins ownership of THAT file alone.  WAL/SHM sidecars at
    derived paths are deliberately never deleted (review R9): this call did
    not create them and cannot prove it did — another process may have opened
    the half-written database and produced them — and an unprovable sidecar
    is inert once its database file is gone (SQLite reconciles orphans on
    next open), so keeping it is the safe direction.  Only the main artefact
    this call opened into existence via O_EXCL is removed, and only while the
    path still holds the creation-time inode; a concurrent replacement is
    left untouched.
    Honest residual: on POSIX filesystems inode numbers can in principle be
    recycled; a same-name replacement landing on the recycled inode within one
    failed operation's window is excluded by design assumption, not by a
    cryptographic check.
    """

    if not _still_ours(path, token):
        return
    with contextlib.suppress(OSError):
        path.unlink()


#: Test-only seam invoked with ``(src, dst)`` immediately before the
#: page-level copy in :func:`_exclusive_online_copy`; ``None`` in production.
#: Same auditable pattern as :data:`MIGRATION_STATEMENT_HOOK` — it exists so
#: failure/cleanup paths can be exercised without corrupting real files.
BACKUP_COPY_HOOK: Callable[[sqlite3.Connection, sqlite3.Connection], None] | None = None


def _exclusive_online_copy(source: Path, target: Path) -> tuple[int, int]:
    """First-party Online Backup into an atomically, exclusively created file.

    The SQLite Online Backup API is page-level: it runs no SQL, fires no
    authorizer, and therefore must never be handed to callers (review R6) —
    ``a.backup(b)`` on two open connections could silently overwrite ``b``'s
    database, resetting even a too-new ``user_version``.  This helper is the
    only copy path:

    * the target file is created with ``O_EXCL`` *before* any copy work, so
      an existing file (any version, any owner) is never overwritten and a
      creation race can only fail loudly;
    * the source is read through the strict ``mode=ro`` channel, which still
      sees WAL-committed frames, so an un-checkpointed live database backs
      up consistently;
    * failures clean up via the creation-time inode token: the target file
      is removed only while the path still holds that same inode; a
      concurrent replacement is never deleted, and WAL/SHM sidecars are
      never deleted at all because their ownership cannot be proven (R9).

    Returns the ownership token so callers can apply the same ownership rule
    to later stages (verification, manifest).
    """

    creation_fd = _exclusive_create(target)
    if creation_fd is None:
        raise BackupError(f"refusing to overwrite existing database: {target}")
    token = _fd_token(creation_fd)
    os.close(creation_fd)
    try:
        with _open_read_connection(source) as src_conn:
            dst_conn = sqlite3.connect(str(target), isolation_level=None)
            try:
                hook = BACKUP_COPY_HOOK
                if hook is not None:
                    hook(src_conn, dst_conn)
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
    except BaseException as exc:
        _discard_if_still_ours(target, token)
        if isinstance(exc, sqlite3.Error):
            raise BackupError(f"online backup into {target} failed: {exc}") from exc
        raise
    return token


def _existing_tables(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return frozenset(str(row[0]) for row in rows)


def _existing_tables_safe(conn: sqlite3.Connection) -> frozenset[str]:
    try:
        return _existing_tables(conn)
    except sqlite3.Error as exc:
        raise CorruptDatabaseError(
            f"schema catalog unreadable (suspected corruption): {exc}"
        ) from exc


def _assert_writable_version(conn: sqlite3.Connection, stage: str) -> None:
    """Re-check ``user_version`` on an already-open connection.

    The read-only gate in :func:`connect` closes with a snapshot: between that
    probe and any later write another connection can commit a schema bump.
    Therefore every write-capable path re-reads the version *after* its own
    connection/lock exists: ``connect`` before any persistent pragma, and
    :func:`transaction`/:func:`migrate_to_head` under the exclusive write lock.
    """

    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise StorageError(f"schema version unreadable on live connection: {exc}") from exc
    version = int(row[0]) if row else 0
    if version > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"database schema version {version} is newer than this build "
            f"supports ({SCHEMA_VERSION}); refusing to write ({stage})"
        )


#: SQLite authorizer action codes that never mutate database contents.  The
#: authorizer callback runs inside SQLite's own prepare/step machinery, so the
#: enforcement below is SQLite's, not a SQL-text parser: probes on SQLite
#: 3.53.1 confirm it fires for plain ``INSERT``/``UPDATE``/``DELETE``,
#: ``WITH ... INSERT/UPDATE/DELETE`` prefixes, ``INSERT ... RETURNING``,
#: DDL, ATTACH/DETACH/VACUUM/REINDEX/ANALYZE, transaction and savepoint
#: control, and PRAGMA (with the pragma name supplied by SQLite).
_READ_OK_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)
#: Exact SQLite/CPython error messages proving SQLite itself refused a write
#: (authorizer DENY, VDBE authorization failure, or a write attempted on the
#: mode=ro channel).  These are used only to *map* SQLite's already-made
#: refusal into a StorageError subtype -- they never decide permission, and no
#: SQL statement text is parsed for security anywhere in this module.
_AUTH_DENIED_MARKERS = (
    "not authorized",
    "authorization denied",
    "attempt to write a readonly database",
)


def _authorization_denied(exc: sqlite3.Error) -> bool:
    text = str(exc).strip().lower()
    return any(marker in text for marker in _AUTH_DENIED_MARKERS)


class _GatedCursor:
    """Controlled cursor facade; the write channel is re-resolved per call.

    Chaining never yields a raw ``sqlite3.Cursor`` write path: while no
    :func:`teamcraft.transaction` gate is open, statements run on the strict
    ``mode=ro`` channel where SQLite itself refuses writes and persistent
    PRAGMA assignments, and the writable connection carries a permanent
    authorizer that denies every mutating action while the gate is closed.
    So even a cursor object held across gate boundaries cannot write once the
    gate has closed.
    """

    def __init__(self, owner: "TeamCraftConnection") -> None:
        self._owner = owner
        self._cursor: sqlite3.Cursor | None = None

    def execute(self, sql: str, parameters: Any = None) -> "_GatedCursor":
        self._cursor = self._owner._execute(sql, parameters, "cursor.execute")
        return self

    def executemany(self, sql: str, parameters_seq: Any) -> "_GatedCursor":
        self._cursor = self._owner._executemany(
            sql, parameters_seq, "cursor.executemany"
        )
        return self

    def executescript(self, sql: str) -> Any:
        raise WriteOutsideTransactionError(
            "cursor.executescript is not exposed: scripts may write and "
            "implicitly commit (even mid-script), which no per-statement gate "
            "can contain; execute statements individually inside "
            "teamcraft.transaction()"
        )

    def close(self) -> None:
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None

    def _bound(self) -> sqlite3.Cursor:
        if self._cursor is None:
            raise sqlite3.ProgrammingError(
                "no statement has been executed on this cursor"
            )
        return self._cursor

    def fetchone(self) -> Any:
        return self._bound().fetchone()

    def fetchmany(self, size: int | None = None) -> Any:
        if size is None:
            return self._bound().fetchmany()
        return self._bound().fetchmany(size)

    def fetchall(self) -> Any:
        return self._bound().fetchall()

    def __iter__(self) -> Any:
        return iter(self._bound())

    @property
    def description(self) -> Any:
        return self._bound().description

    @property
    def rowcount(self) -> int:
        return self._bound().rowcount

    @property
    def lastrowid(self) -> Any:
        return self._bound().lastrowid


class TeamCraftConnection:
    """Narrowed public facade: two channels, one SQLite-enforced write gate.

    A raw ``sqlite3.Connection`` hands out ungated write paths (``execute``,
    ``executemany``, chained cursors, ``executescript``, ``commit``) — a
    schema bump committed by another process after :func:`connect` could
    still be written through them.  The facade removes that surface by
    construction rather than by statement parsing:

    * **Read channel**: reads run on a strict ``mode=ro`` connection (WAL
      readers still see everything other connections have committed).  Writes
      and persistent PRAGMA assignments — including ``WITH ... INSERT``
      prefixes, ``INSERT ... RETURNING`` and ``PRAGMA name(value)`` forms —
      fail there at SQLite's own execution boundary.
    * **Writable channel**: never handed to callers.  A permanent
      ``sqlite3.set_authorizer`` denies every mutating action code
      (INSERT/UPDATE/DELETE, DDL, ATTACH/DETACH, transaction/savepoint
      control, PRAGMA, ...) while the gate is closed; the authorizer runs
      inside SQLite at prepare/step time, so even an escaped cursor cannot
      write.  Re-preparing identical SQL re-invokes it (no prepared-statement
      cache leak; pinned by regression test).
    * **Gate**: :func:`teamcraft.transaction` acquires ``BEGIN IMMEDIATE``,
      re-checks ``user_version`` under the lock, and only then opens the
      writable channel for the duration of the block.  ``executescript``,
      ``commit`` and ``rollback`` are not exposed at all — commit boundaries
      belong to :func:`transaction`.  Neither is ``backup``: the SQLite
      Online Backup API is page-level, bypasses SQL authorization, and could
      overwrite another database straight through the gate (review R6), so
      copying lives in :func:`backup_database` instead (atomic O_EXCL target,
      never overwrites, version-checked).

    All public execution methods return :class:`_GatedCursor`, and chained
    calls re-resolve the channel against the *current* gate state, so normal
    chaining never yields a raw write entry point.  SQLite refusals
    (``not authorized`` / ``authorization denied`` / read-only channel) are
    surfaced as :class:`WriteOutsideTransactionError`.

    Honest scope: this is a boundary against ordinary public use, not
    against deliberate reflection into private attributes (``_rw``,
    ``_ro``, ``set_authorizer(None)``) — that surface belongs to trusted
    first-party code and review, and is not claimed as defended here.
    """

    def __init__(
        self, rw: sqlite3.Connection, path: str, busy_timeout_ms: int
    ) -> None:
        self._rw = rw
        self.path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._ro: sqlite3.Connection | None = None
        self._write_depth = 0  # open teamcraft.transaction() gate
        self._internal_depth = 0  # first-party primitives (BEGIN/COMMIT/DDL)
        self._row_factory: Any = None
        rw.set_authorizer(self._authorize)

    # -- SQLite-enforced boundary ---------------------------------------------
    def _authorize(
        self,
        action: int,
        arg1: Any,
        arg2: Any,
        database: Any,
        trigger: Any,
    ) -> int:
        if self._write_depth or self._internal_depth:
            return sqlite3.SQLITE_OK
        if action in _READ_OK_ACTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    @property
    def _gate_open(self) -> bool:
        return self._write_depth > 0

    @contextlib.contextmanager
    def _privileged(self) -> Iterator[None]:
        """First-party primitives only (transaction/migrate internals)."""

        self._internal_depth += 1
        try:
            yield
        finally:
            self._internal_depth -= 1

    def _open_write_gate(self) -> None:
        self._write_depth += 1

    def _close_write_gate(self) -> None:
        self._write_depth = max(0, self._write_depth - 1)

    # -- channel routing -------------------------------------------------------
    def _read_channel(self) -> sqlite3.Connection:
        if self._ro is None:
            self._ro = sqlite3.connect(
                _sqlite_uri(Path(self.path), "mode=ro"), uri=True
            )
            self._ro.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
            # Mirror connection-local settings so pragma reads on the facade
            # describe the facade, not one arbitrary channel (reads on a
            # read-only channel are advisory; writes still go through _rw).
            self._ro.execute("PRAGMA foreign_keys=ON")
            if self._row_factory is not None:
                self._ro.row_factory = self._row_factory
        return self._ro

    def _execute(
        self, sql: str, parameters: Any, entrypoint: str
    ) -> sqlite3.Cursor:
        conn = self._rw if self._gate_open else self._read_channel()
        try:
            if parameters is None:
                return conn.execute(sql)
            return conn.execute(sql, parameters)
        except sqlite3.Error as exc:
            if not self._gate_open and _authorization_denied(exc):
                raise WriteOutsideTransactionError(
                    f"{entrypoint}: writes outside teamcraft.transaction() are "
                    "refused at the SQLite execution boundary (the read "
                    "channel is mode=ro and the writable connection denies "
                    f"mutating actions while the gate is closed): {self.path}"
                ) from exc
            raise

    def _executemany(
        self, sql: str, parameters_seq: Any, entrypoint: str
    ) -> sqlite3.Cursor:
        conn = self._rw if self._gate_open else self._read_channel()
        try:
            return conn.executemany(sql, parameters_seq)
        except sqlite3.Error as exc:
            if not self._gate_open and _authorization_denied(exc):
                raise WriteOutsideTransactionError(
                    f"{entrypoint}: writes outside teamcraft.transaction() are "
                    "refused at the SQLite execution boundary (the read "
                    "channel is mode=ro and the writable connection denies "
                    f"mutating actions while the gate is closed): {self.path}"
                ) from exc
            raise

    # -- public surface ----------------------------------------------------------
    def execute(self, sql: str, parameters: Any = None) -> _GatedCursor:
        facade_cursor = _GatedCursor(self)
        facade_cursor._cursor = self._execute(sql, parameters, "execute")
        return facade_cursor

    def executemany(self, sql: str, parameters_seq: Any) -> _GatedCursor:
        facade_cursor = _GatedCursor(self)
        facade_cursor._cursor = self._executemany(
            sql, parameters_seq, "executemany"
        )
        return facade_cursor

    def executescript(self, sql: str) -> Any:
        raise WriteOutsideTransactionError(
            "executescript is not exposed: scripts may write and implicitly "
            "commit (even mid-script), which no per-statement gate can "
            "contain; execute statements individually inside "
            "teamcraft.transaction()"
        )

    def cursor(self) -> _GatedCursor:
        return _GatedCursor(self)

    def backup(self, target: Any = None, **kwargs: Any) -> None:
        raise BackupError(
            "backup() is not exposed on the facade: the SQLite Online Backup "
            "API is page-level and bypasses SQL authorization entirely, so a "
            "public handle could overwrite other databases regardless of the "
            "write gate or schema version (review R6). Use "
            "teamcraft.backup_database(), which creates its target atomically "
            "(O_EXCL), never overwrites an existing or too-new database, and "
            "cleans up only files it created itself"
        )

    def close(self) -> None:
        try:
            self._rw.close()
        finally:
            if self._ro is not None:
                self._ro.close()
                self._ro = None

    def commit(self) -> None:
        raise WriteOutsideTransactionError(
            "commit is not exposed: teamcraft.transaction() owns commit boundaries"
        )

    def rollback(self) -> None:
        raise WriteOutsideTransactionError(
            "rollback is not exposed: teamcraft.transaction() owns commit boundaries"
        )

    @property
    def in_transaction(self) -> bool:
        return bool(self._rw.in_transaction)

    @property
    def total_changes(self) -> int:
        return int(self._rw.total_changes)

    @property
    def isolation_level(self) -> Any:
        return self._rw.isolation_level

    @property
    def row_factory(self) -> Any:
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._row_factory = value
        self._rw.row_factory = value
        if self._ro is not None:
            self._ro.row_factory = value


def _inner_connection(conn: TeamCraftConnection) -> sqlite3.Connection:
    """The underlying writable sqlite connection, first-party internals only.

    Public code receives :class:`TeamCraftConnection`; this accessor exists
    only for first-party migration/transaction internals.  Accepting a raw
    ``sqlite3.Connection`` here would silently bypass the facade's authorizer
    and write gate.
    """

    if not isinstance(conn, TeamCraftConnection):
        raise TypeError("expected TeamCraftConnection")
    return conn._rw


def connect(
    path: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = 5000,
    allow_unverified_location: bool = False,
) -> TeamCraftConnection:
    """Open a validated local database with FK, WAL and busy timeout.

    Too-new-schema protection is layered, because a read-only probe alone is
    a snapshot that a concurrent upgrade can race past (TOCTOU):

    1. strict read-only gate *before* any write-capable open;
    2. a re-check on the writable connection *before* any persistent pragma
       (``journal_mode``/``foreign_keys`` are never applied to an
       unexpectedly-new database);
    3. re-checks under the exclusive write lock inside :func:`transaction`
       and :func:`migrate_to_head`.

    Guarantee boundary (honest scope): the returned
    :class:`TeamCraftConnection` keeps callers off the writable handle.  Reads
    run on a strict ``mode=ro`` channel; every mutating action on the hidden
    writable connection (DML, DDL, ATTACH/VACUUM, transaction/savepoint
    control, PRAGMA) is refused by a permanent SQLite authorizer while the
    write gate is closed, and ``executescript``/``commit``/``rollback`` are
    not exposed at all.  Durable writes only run inside :func:`transaction`,
    which re-checks the version under the write lock.  Chained cursors
    returned by public methods are controlled facades, so normal chaining
    cannot reach a raw write entry point.  Deliberate reflection into private
    attributes (``_rw``/``_ro``/``set_authorizer(None)``) is out of scope and
    not claimed defended.  ``remote`` locations are always refused and
    ``unknown`` locations require the explicit auditable
    ``allow_unverified_location=True`` acknowledgement; WAL enablement is a
    capability probe, not proof of locality.
    """

    resolved = _validate_database_path(path)
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must not be negative")
    _ensure_supportable(resolved.parent, allow_unverified_location=allow_unverified_location)
    if resolved.exists():
        version = _read_user_version(resolved)
        if version > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"database schema version {version} is newer than this build "
                f"supports ({SCHEMA_VERSION}); refusing to write"
            )
    # Creation ownership is decided atomically (O_EXCL) and pinned to the
    # created inode: only a file *this call* created — and while the path
    # still holds that same inode — may be cleaned up on failure, so a
    # concurrent replacement is never deleted (review R7).
    owned_token: tuple[int, int] | None = None
    try:
        creation_fd = os.open(str(resolved), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        owned_token = None
    except OSError as exc:
        raise UnsupportedLocationError(
            f"cannot create database file {resolved}: {exc}"
        ) from exc
    else:
        owned_token = _fd_token(creation_fd)
        os.close(creation_fd)
    try:
        conn = sqlite3.connect(str(resolved), isolation_level=None)
    except sqlite3.Error as exc:
        if _still_ours(resolved, owned_token):
            with contextlib.suppress(OSError):
                resolved.unlink()
        raise StorageError(f"cannot open connection for {resolved}: {exc}") from exc
    try:
        # Layer 2 of the TOCTOU defence: never configure a database that
        # out-grew this build between the ro gate and this open.
        _assert_writable_version(
            conn, "re-checked on the writable connection before persistent pragmas"
        )
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        # Concurrent first-time WAL conversion can surface a transient I/O error
        # to the loser of the race; retry briefly, but still fail closed when the
        # filesystem genuinely cannot sustain WAL.
        journal: tuple[Any, ...] | None = None
        for attempt in range(10):
            try:
                journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if journal and str(journal[0]).lower() == "wal":
                    break
            except sqlite3.Error:
                journal = None
            time.sleep(0.05 * (attempt + 1))
        if not journal or str(journal[0]).lower() != "wal":
            journal_value = journal[0] if journal else "error"
            raise UnsupportedLocationError(
                f"filesystem does not enable WAL (got journal_mode={journal_value!r}); "
                "network or exotic filesystems are outside the support boundary "
                "(WAL success is capability evidence, not proof of locality)"
            )
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        conn.close()
        if _still_ours(resolved, owned_token):
            with contextlib.suppress(OSError):
                resolved.unlink()
        raise StorageError(f"cannot initialise connection for {resolved}: {exc}") from exc
    except BaseException:
        # Every non-sqlite exit (SchemaTooNewError, UnsupportedLocationError,
        # KeyboardInterrupt, ...) must still release the connection; cleanup
        # only ever removes a file this call atomically created.
        conn.close()
        if _still_ours(resolved, owned_token):
            with contextlib.suppress(OSError):
                resolved.unlink()
        raise
    return TeamCraftConnection(conn, str(resolved), busy_timeout_ms)


@contextlib.contextmanager
def transaction(
    conn: TeamCraftConnection, *, immediate: bool = True
) -> Iterator[TeamCraftConnection]:
    """The module's write gate: lock, version re-check, then open the gate.

    Ordering is the security contract: by default ``BEGIN IMMEDIATE`` first
    takes the write lock, ``user_version`` is re-read under that lock (a bump
    committed by another process *after* :func:`connect` aborts here with
    :class:`SchemaTooNewError`), and only then is the facade's writable
    channel opened for the body of the block.  ``immediate=False`` deliberately
    uses SQLite's deferred ``BEGIN`` semantics; callers must treat ``BUSY`` as
    a failed whole transaction and retry from the beginning.  On exit the gate
    closes before the commit primitive runs.  The public API accepts only
    :class:`TeamCraftConnection`; migration code uses its own private,
    privileged transaction boundary.  A prepared statement compiled inside
    the gate does not smuggle a later write through: identical text
    re-prepared outside the gate is refused by the read channel and by the
    authorizer (pinned by regression test).
    """

    inner = _inner_connection(conn)
    gated = conn
    with gated._privileged():
        inner.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        with gated._privileged():
            stage = (
                "re-checked under the write lock"
                if immediate
                else "re-checked inside the deferred transaction before first write"
            )
            _assert_writable_version(inner, stage)
        gated._open_write_gate()
        try:
            yield conn
        finally:
            gated._close_write_gate()
        with gated._privileged():
            inner.execute("COMMIT")
    except BaseException:
        with gated._privileged():
            with contextlib.suppress(sqlite3.Error):
                inner.execute("ROLLBACK")
        raise


@dataclass(frozen=True)
class MigrationReport:
    fresh: bool
    from_version: int
    to_version: int
    applied: tuple[int, ...]


def migrate_to_head(
    path: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = 10000,
    allow_unverified_location: bool = False,
) -> MigrationReport:
    """Create/upgrade to :data:`SCHEMA_VERSION` atomically and concurrently safe.

    Version judgement and DDL live inside ONE ``BEGIN IMMEDIATE`` transaction
    with a fresh re-read under the exclusive write lock: two migrators racing
    on an empty database converge (winner creates, loser observes v1 and
    no-ops) instead of hitting "table already exists".  Repeated runs are
    idempotent; newer schemas and unversioned junk are refused without any
    modification.
    """

    conn = connect(
        path,
        busy_timeout_ms=busy_timeout_ms,
        allow_unverified_location=allow_unverified_location,
    )
    inner = _inner_connection(conn)
    try:
        # Migration primitives (BEGIN, DDL, user_version bump, COMMIT) run as
        # first-party internals; the public write gate stays closed so no
        # caller-visible write path exists during migration.
        with conn._privileged():
            inner.execute("BEGIN IMMEDIATE")
            try:
                _assert_writable_version(
                    inner,
                    "re-checked inside the migration transaction under the write lock",
                )
                user_version = int(inner.execute("PRAGMA user_version").fetchone()[0])
                tables = _existing_tables_safe(inner)
                if user_version == SCHEMA_VERSION:
                    missing = _V1_TABLES - tables
                    if missing:
                        raise MigrationError(
                            f"user_version={SCHEMA_VERSION} but tables are missing: "
                            f"{sorted(missing)}; refusing to continue"
                        )
                    recorded = inner.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()
                    if recorded is None or int(recorded[0]) != SCHEMA_VERSION:
                        raise MigrationError(
                            f"user_version={SCHEMA_VERSION} disagrees with "
                            f"schema_migrations record {recorded!r}"
                        )
                    inner.execute("COMMIT")
                    return MigrationReport(
                        fresh=False,
                        from_version=user_version,
                        to_version=SCHEMA_VERSION,
                        applied=(),
                    )
                if user_version != 0:
                    raise MigrationError(
                        f"no known upgrade path from schema version {user_version} "
                        f"to {SCHEMA_VERSION}"
                    )
                if tables:
                    raise MigrationError(
                        "file already contains unversioned tables; refusing to adopt "
                        "an unknown database as v1"
                    )
                for index, statement in enumerate(_V1_STATEMENTS):
                    inner.execute(statement)
                    hook = MIGRATION_STATEMENT_HOOK
                    if hook is not None:
                        hook(index, statement)
                inner.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (1, _utc_now_iso()),
                )
                inner.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                inner.execute("COMMIT")
                return MigrationReport(
                    fresh=True, from_version=0, to_version=SCHEMA_VERSION, applied=(1,)
                )
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    inner.execute("ROLLBACK")
                raise
    finally:
        conn.close()


def verify_database(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read-only integrity + version inspection used after restore (8.2-13)."""

    resolved = _validate_database_path(path)
    if not resolved.exists():
        raise StorageError(f"database does not exist: {resolved}")
    version = _read_user_version(resolved)
    with _open_read_connection(resolved) as conn:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = str(row[0]) if row else "unknown"
            tables = _existing_tables_safe(conn)
        except sqlite3.Error as exc:
            raise CorruptDatabaseError(f"integrity check failed to run: {exc}") from exc
    ok = integrity == "ok" and 0 <= version <= SCHEMA_VERSION
    return {
        "ok": ok,
        "integrity": integrity,
        "user_version": version,
        "tables": sorted(tables),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_versions(path: Path) -> tuple[int, int | None, frozenset[str]]:
    """(user_version, schema_migrations_max, missing_v1_tables) from the snapshot itself."""

    with _open_read_connection(path) as conn:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = _existing_tables_safe(conn)
        recorded: int | None = None
        if "schema_migrations" in tables:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            recorded = int(row[0]) if row and row[0] is not None else None
        # Completeness of a v1 snapshot is a hard consistency requirement.
        missing = _V1_TABLES - tables
    return user_version, recorded, missing


def _assert_restorable_snapshot(
    version: int, recorded: int | None, missing: frozenset[str], *, subject: str
) -> None:
    """Single source of truth for what counts as a restorable v1 snapshot.

    ``backup_database`` applies this to the live source before copying and to
    the freshly copied snapshot after copying; ``restore_backup`` applies the
    identical predicates to the backup.  Therefore a backup can only be
    reported successful when restoring it would pass the same schema/version
    acceptance (review R8: an empty ``sqlite3``-created database must not be
    ``success``-backed up only to be refused at restore for lacking
    ``schema_migrations`` and the v1 tables).
    """

    if missing:
        raise BackupError(f"{subject} is incomplete, missing tables: {sorted(missing)}")
    if recorded is None:
        raise BackupError(
            f"{subject} has no schema_migrations record; refusing to use an "
            "unaudited database"
        )
    if version > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"{subject} user_version {version} is newer than this build supports "
            f"({SCHEMA_VERSION})"
        )
    if recorded != version:
        raise BackupError(
            f"{subject} user_version={version} disagrees with "
            f"schema_migrations={recorded}"
        )


@dataclass(frozen=True)
class BackupResult:
    backup_path: Path
    manifest_path: Path
    sha256: str
    schema_version: int
    generation: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_path": str(self.backup_path),
            "manifest_path": str(self.manifest_path),
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "generation": self.generation,
        }


#: Fields every restore must find in a backup manifest (audit completeness).
_REQUIRED_MANIFEST_FIELDS = (
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
)


def _manifest_integer(
    manifest: dict[str, Any], field: str, *, minimum: int = 0
) -> int:
    """Return a strictly typed manifest integer or fail as backup metadata.

    JSON booleans are Python integers, and ``int()`` also accepts numeric
    strings and truncates floats.  None of those are valid for a persisted
    backup manifest: accepting them would make the on-disk schema ambiguous.
    """

    value = manifest.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupError(
            f"manifest {field} must be a JSON integer, got {value!r}"
        )
    if value < minimum:
        raise BackupError(
            f"manifest {field} must be at least {minimum}, got {value}"
        )
    return value


def backup_database(
    source_path: str | os.PathLike[str],
    dest_dir: str | os.PathLike[str],
    *,
    generation: str | None = None,
    app_version: str = APP_VERSION,
    allow_unverified_location: bool = False,
) -> BackupResult:
    """Consistent online backup via the SQLite Online Backup API.

    Never overwrites an existing backup.  The manifest records schema version,
    app version, digest, size, filename, integrity result and the applicable
    cutover generation (``None`` means unbound; retention will never delete
    unbound evidence; the generation protocol itself is M1-06 work).  Any
    failure cleans up half-written artefacts.
    """

    source = _validate_database_path(source_path)
    if not source.exists():
        raise BackupError(f"backup source does not exist: {source}")
    destination_dir = Path(dest_dir)
    if not destination_dir.is_dir():
        raise BackupError(f"backup destination directory missing: {destination_dir}")
    _ensure_supportable(destination_dir, allow_unverified_location=allow_unverified_location)

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    suffix = os.urandom(3).hex()
    backup_path = destination_dir / f"{source.stem}-{stamp}-{suffix}.db"
    manifest_path = backup_path.with_name(backup_path.name + ".manifest.json")
    if backup_path.exists() or manifest_path.exists():
        raise BackupError(f"backup targets unexpectedly exist: {backup_path}")

    backup_token: tuple[int, int] | None = None
    manifest_token: tuple[int, int] | None = None
    try:
        # Acceptance gate first (strict mode=ro, WAL-aware): the source must
        # already satisfy the exact criteria restore_backup will later apply,
        # so a "successful" backup is always a restorable snapshot (R8).
        _ensure_supportable(source.parent, allow_unverified_location=allow_unverified_location)
        version, recorded, missing = _snapshot_versions(source)
        _assert_restorable_snapshot(
            version, recorded, missing, subject="backup source"
        )
        # Online copy through the first-party exclusive path: the target is
        # atomically created (O_EXCL) and can never be an existing database,
        # so no caller of backup_database can overwrite or downgrade one.
        # If the exclusive creation lost a race, nothing was created here and
        # nothing below (or in the except) may delete the winner's file.
        backup_token = _exclusive_online_copy(source, backup_path)
        report = verify_database(backup_path)
        if not report["ok"]:
            raise BackupError(f"backup failed integrity verification: {report}")
        # Re-apply the identical acceptance to the copy itself: what we ship
        # must pass restore, not merely look intact.
        snap_version, snap_recorded, snap_missing = _snapshot_versions(backup_path)
        if snap_version != version:
            raise BackupError(
                f"backup snapshot version {snap_version} drifted from source "
                f"{version} during copy"
            )
        _assert_restorable_snapshot(
            snap_version, snap_recorded, snap_missing, subject="backup snapshot"
        )
        digest = _sha256(backup_path)
        manifest = {
            "backup_file": backup_path.name,
            "backup_sha256": digest,
            "backup_size_bytes": backup_path.stat().st_size,
            "schema_version": version,
            "app_version": app_version,
            "created_at_utc": _utc_now_iso(),
            "origin_database": source.name,
            "integrity_check": report["integrity"],
            "applicable_generation": generation,
            "tool": "teamcraft.storage",
        }
        # Manifest is also created atomically and exclusively, then written via
        # the same fd: a manifest that appeared at this path concurrently
        # belongs to another process and is neither overwritten nor deleted.
        manifest_fd = _exclusive_create(manifest_path)
        if manifest_fd is None:
            raise BackupError(
                f"backup manifest unexpectedly created by another process; "
                f"refusing to overwrite: {manifest_path}"
            )
        manifest_token = _fd_token(manifest_fd)
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with os.fdopen(manifest_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except BaseException:
        # Ownership rule (review R7): every artefact is deleted only when this
        # call created it and the path still holds that same inode; files a
        # concurrent process won the race with are left exactly as they are.
        _discard_if_still_ours(backup_path, backup_token)
        _discard_if_still_ours(manifest_path, manifest_token)
        raise
    return BackupResult(
        backup_path=backup_path,
        manifest_path=manifest_path,
        sha256=digest,
        schema_version=version,
        generation=generation,
    )


@dataclass(frozen=True)
class RestoreResult:
    restored_path: Path
    schema_version: int
    integrity: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "restored_path": str(self.restored_path),
            "schema_version": self.schema_version,
            "integrity": self.integrity,
        }


def restore_backup(
    backup_path: str | os.PathLike[str],
    dest_path: str | os.PathLike[str],
    *,
    require_manifest: bool = True,
    allow_unverified_location: bool = False,
) -> RestoreResult:
    """Restore a verified backup into a *new* destination.

    Truth hierarchy: the backup snapshot itself (user_version +
    schema_migrations) is authoritative for what the backup contains; the
    manifest must agree with it on schema version, digest, size and filename,
    carry every required audit field, and report a clean integrity check.
    Snapshot reads are strict ``mode=ro`` — committed WAL frames included —
    so a stale main-file view can never smuggle a wrong version through.  Any
    disagreement — or a too-new version on either side — aborts the restore,
    and a destination file created during the failed attempt is removed so
    nothing can be mistaken for a successful restore.  Post-restore domain
    reconciliation (DB/Herdr/Git) remains the caller's duty before dispatch
    resumes (8.2-13).
    """

    backup = _validate_database_path(backup_path)
    if not backup.exists():
        raise BackupError(f"backup does not exist: {backup}")
    dest = _validate_database_path(dest_path)
    if dest.exists():
        raise BackupError(f"refusing to overwrite existing database: {dest}")
    _ensure_supportable(dest.parent, allow_unverified_location=allow_unverified_location)

    manifest_path = backup.with_name(backup.name + ".manifest.json")
    manifest: dict[str, Any] | None = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError(f"manifest unreadable: {exc}") from exc
        if not isinstance(manifest, dict):
            raise BackupError("manifest root must be a JSON object")
    elif require_manifest:
        raise BackupError(f"manifest missing next to backup: {manifest_path.name}")

    if manifest is not None:
        missing_fields = [field for field in _REQUIRED_MANIFEST_FIELDS if field not in manifest]
        if missing_fields:
            raise BackupError(
                f"manifest is missing required fields: {sorted(missing_fields)}"
            )
        if manifest.get("backup_file") != backup.name:
            raise BackupError(
                f"manifest backup_file {manifest.get('backup_file')!r} does not "
                f"match backup {backup.name!r}"
            )
        expected_size = _manifest_integer(manifest, "backup_size_bytes")
        if expected_size != backup.stat().st_size:
            raise BackupError("manifest size does not match backup file")
        if _sha256(backup) != manifest.get("backup_sha256"):
            raise BackupError("manifest digest does not match backup file")
        if str(manifest.get("integrity_check")) != "ok":
            raise BackupError(
                f"manifest integrity_check is {manifest.get('integrity_check')!r}, "
                "not 'ok'"
            )

    snapshot_user_version, snapshot_recorded, missing = _snapshot_versions(backup)
    # Identical predicate set that backup_database applied before shipping
    # this snapshot (single source of truth, review R8).
    _assert_restorable_snapshot(
        snapshot_user_version,
        snapshot_recorded,
        missing,
        subject="backup snapshot",
    )
    if manifest is not None:
        manifest_version = _manifest_integer(manifest, "schema_version")
        if manifest_version > SCHEMA_VERSION:
            raise BackupError(
                f"manifest schema_version {manifest_version} is newer than this "
                f"build supports ({SCHEMA_VERSION})"
            )
        if manifest_version != snapshot_user_version:
            raise BackupError(
                f"manifest schema_version {manifest_version!r} does not match the "
                f"backup snapshot user_version {snapshot_user_version}"
            )

    integrity = "unknown"
    dest_token: tuple[int, int] | None = None
    try:
        # Atomic exclusive creation inside the helper: an existing dest (the
        # racy gap between the check above and creation) is refused without
        # being touched, and copy failures clean up only this call's file.
        dest_token = _exclusive_online_copy(backup, dest)
        report = verify_database(dest)
        if not report["ok"]:
            raise BackupError(f"restored database failed verification: {report}")
        if int(report["user_version"]) != snapshot_user_version:
            raise BackupError(
                "restored database version drifted from the verified snapshot"
            )
        integrity = str(report["integrity"])
    except BaseException:
        # Ownership rule (review R7/R9): dest is removed only while the path
        # still holds the inode this call created; WAL/SHM sidecars are never
        # deleted because their ownership cannot be proven, and a concurrent
        # replacement at the path belongs to the other process.
        _discard_if_still_ours(dest, dest_token)
        raise
    return RestoreResult(
        restored_path=dest,
        schema_version=int(snapshot_user_version),
        integrity=integrity,
    )


def plan_retention(
    backup_dir: str | os.PathLike[str],
    *,
    keep_last: int | None = None,
    older_than_days: int | None = None,
    delete: bool = False,
) -> list[Path]:
    """Conservative retention *entry point*; dry-run unless ``delete=True``.

    Never deletable (8.2-13 audit protection): entries with no readable
    manifest (unknown provenance), manifests flagged ``protect: true``, and
    entries whose ``applicable_generation`` is missing/None (generation-
    unknown recovery evidence cannot be proven superseded).  Only
    generation-bound, unprotected backups participate in keep_last/age
    selection.  Real defaults belong to M1-06 policy work.
    """

    directory = Path(backup_dir)
    if not directory.is_dir():
        raise BackupError(f"backup directory missing: {directory}")
    if keep_last is None and older_than_days is None:
        raise ValueError("plan_retention requires keep_last and/or older_than_days")

    entries: list[tuple[str, Path, Path, dict[str, Any]]] = []
    for backup_file in sorted(directory.glob("*.db")):
        manifest_path = backup_file.with_name(backup_file.name + ".manifest.json")
        if not manifest_path.is_file():
            continue  # unknown provenance: never touch
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if manifest.get("protect") is True:
            continue
        if manifest.get("applicable_generation") in (None, ""):
            continue  # generation-unknown recovery evidence: never auto-delete
        if manifest.get("backup_file") != backup_file.name:
            continue  # manifest does not describe this file: conservative skip
        entries.append(
            (str(manifest.get("created_at_utc", "")), backup_file, manifest_path, manifest)
        )
    entries.sort(key=lambda item: item[0])

    selected: list[Path] = []
    if keep_last is not None and len(entries) > keep_last:
        selected.extend(entry[1] for entry in entries[: len(entries) - keep_last])
    if older_than_days is not None:
        cutoff = datetime.now().timestamp() - older_than_days * 86400
        for created, backup_file, _, _ in entries:
            if backup_file in selected:
                continue
            try:
                when = datetime.fromisoformat(created).timestamp()
            except (TypeError, ValueError):
                continue  # unparseable timestamp: conservative, keep it
            if when < cutoff:
                selected.append(backup_file)

    seen: set[Path] = set()
    unique = [p for p in selected if not (p in seen or seen.add(p))]
    if delete:
        for backup_file in unique:
            manifest_path = backup_file.with_name(backup_file.name + ".manifest.json")
            backup_file.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
    return unique
