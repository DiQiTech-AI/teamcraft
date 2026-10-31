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
  Online Backup API bypasses SQL authorization, so backups run ONLY through
  ``backup_database``/``restore_backup``, which copy into a private 0700
  staging file, verify fully, and publish the verified inode with
  ``os.link`` — EEXIST makes overwriting any pre-existing public file
  impossible (review R6 + P-fixes 10; the P-fixes 9 rename-pivot cannot
  reach staging writes); durable writes run inside
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
  the main ``.db`` while the WAL is live), performed in a private 0700
  staging file and published ONLY via ``os.link`` so a pre-existing public
  target can never be overwritten or replaced (EEXIST refusal is atomic and
  unconditional); manifests carrying schema/app versions, digests, size,
  filename and an applicable generation; restore that cross-verifies the
  *snapshot itself* against the manifest before publishing the verified
  staged inode the same way; retention as a conservative dry-run entry point
  that can never delete protected or generation-unbound recovery evidence
  (``delete=True`` is refused outright); staging directories are RETAINED
  on success AND on failure — nothing in this module ever unlinks or
  truncates, and because publication hard-links the staged inode, success
  retention costs zero extra data blocks.  OTHER SAME-UID PROCESSES ARE A
  DECLARED TRUST BOUNDARY for this local single-machine library (that
  world already controls every file this library can touch; no POSIX path
  scheme defends against it and we do not claim to), while never-overwrite
  (os.link EEXIST), verify-before-publish, identity-checked publication and
  fail-closed retention hold regardless (reviews P-fixes 10 + 14).

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


def _retain_created_file(path: Path, token: tuple[int, int] | None) -> None:
    """Conservatively retain a database or manifest artefact this call
    created but could not finish — NEVER unlink it (reviews R7, R9,
    P-fixes 6, P-fixes 7).

    Every path here (fresh databases, backup targets and manifests, restore
    destinations) becomes publicly visible the instant ``O_EXCL`` creates
    it, and from that moment ANY check-then-delete is non-atomic: even
    with a matching creation-time inode token, another local process can
    open the SAME inode and commit real data into it — or replace the path
    outright — before our ``unlink()`` runs, and no re-check of inodes,
    sizes, timestamps, or digests can close that last window.  Both
    patterns were reproduced with precise ``Path.unlink`` injections;
    random file names are no protection either, since any local process
    can still open and adopt a path it can see.  Retention is therefore
    the only sound failure policy for databases and manifests alike.

    Guarantee: a failed operation NEVER deletes anything.  Callers must
    remember the other half: a retained artefact is FAILURE EVIDENCE, not
    a successful backup or restore — these paths raise, so no
    ``BackupResult``/``RestoreResult`` exists, an interrupted backup's
    manifest is absent or unbound (and unmanifested files are invisible to
    ``plan_retention`` by design), and leftovers are inert junk awaiting
    human review or removal by a future provably-atomic mechanism (M1-06).
    ``plan_retention`` refuses real deletion for exactly this reason.

    ``token`` remains part of the signature as the creation-decision
    record (this call did or did not win the O_EXCL race); the outcome no
    longer depends on it — nothing is ever removed here.
    """

    return None  # deliberate: retention IS the fix; unlink is the bug class


#: Test-only seam invoked with ``(src, dst)`` immediately before the
#: page-level copy in :func:`_exclusive_online_copy`; ``None`` in production.
#: Same auditable pattern as :data:`MIGRATION_STATEMENT_HOOK` — it exists so
#: failure/cleanup paths can be exercised without corrupting real files.
BACKUP_COPY_HOOK: Callable[[sqlite3.Connection, sqlite3.Connection], None] | None = None


def _exclusive_online_copy(
    source: Path,
    target: Path,
    *,
    creation_fd: int,
    expect_sha256: str | None = None,
    expect_version: int | None = None,
) -> str:
    """First-party Online Backup into a caller-provided STAGING inode (P-fixes 11).

    .. warning::
        INTERNAL ONLY.  ``creation_fd`` MUST be the descriptor of a staging
        file the caller just created with ``O_CREAT | O_EXCL`` inside a
        private 0700 staging directory: the kernel proved the name was free
        at create time, so the inode is ours.  SQLite opens that STAGED
        PATH — never a public target; public names stay untouched until the
        separate atomic link publication (:func:`_publish_atomically`) AFTER
        full verification.  Under the declared same-uid trust model (reviews
        P-fixes 10 + 14) path games against staging names are out of
        scope; everything unprovable fails closed.

    The SQLite Online Backup API is page-level: it runs no SQL, fires no
    authorizer, and therefore must never be handed to callers (review R6) —
    ``a.backup(b)`` on two open connections could silently overwrite ``b``'s
    database, resetting even a too-new ``user_version``.  This helper is the
    only copy path:

    * the destination is the caller-created private staged path (its inode
      is our O_EXCL creation); public targets are never opened for
      writing;
    * the source is read through the strict ``mode=ro`` channel, which still
      sees WAL-committed frames, so an un-checkpointed live database backs
      up consistently;
    * a WAL-sourced page copy lands as a self-consistent WAL-marked file —
      every committed frame is already embedded in the copied pages — and
      verifies natively through SQLite (pinned by the WAL roundtrip tests);
      NO header rewriting is performed anywhere (review P-fixes 14);
    * the copy is BOUND to what the caller verified (reviews P-fixes 2, 8):
      when ``expect_version``/``expect_sha256`` are given, the source version
      is re-read through the open source connection and the bytes actually
      copied are hashed through ``creation_fd`` (after the writing connection
      closed and flushed) — a digest no path replacement can redirect; any
      mismatch aborts with :class:`BackupError` instead of reporting success
      for content nobody verified;
    * failures RETAIN everything (review P-fixes 7): nothing here is ever
      unlinked; the staging leftovers are inert failure evidence, never a
      backup.

    Returns the sha256 of the actually-copied bytes so callers can bind
    manifests and publish proofs to what was really written, not to a later
    re-read of any path.
    """

    try:
        with _open_read_connection(source) as src_conn:
            if expect_version is not None:
                live_version = int(
                    src_conn.execute("PRAGMA user_version").fetchone()[0]
                )
                if live_version != expect_version:
                    raise BackupError(
                        f"backup source {source} reports user_version {live_version} "
                        f"at copy time but {expect_version} was verified earlier; "
                        "the file behind this path changed, refusing to copy"
                    )
            try:
                # The destination is our private staged path inside the
                # private 0700 staging dir; public targets are never
                # opened for writing (review P-fixes 14).
                dst_conn = sqlite3.connect(str(target), isolation_level=None)
            except sqlite3.Error as exc:
                raise BackupError(
                    f"cannot open staging destination {target}: {exc}"
                ) from exc
            try:
                hook = BACKUP_COPY_HOOK
                if hook is not None:
                    hook(src_conn, dst_conn)
                src_conn.backup(dst_conn)
            finally:
                # Close BEFORE hashing: flushes every page and checkpoints
                # any WAL so the bytes visible through the fd are complete.
                dst_conn.close()
    except BaseException as exc:
        # Retain-on-failure (P-fixes 7): the staging file keeps whatever it
        # received; nothing is deleted, and no public name was ever touched.
        if isinstance(exc, sqlite3.Error):
            raise BackupError(f"online backup into staging failed: {exc}") from exc
        raise
    # P-fixes 8: digest OUR inode through the descriptor held since the
    # O_EXCL creation - os.pread on an open fd never re-resolves the name,
    # so this binds the actually-copied bytes; it also replaces the
    # pragma_page/database_list hashing that silently re-read the namespace
    # (no page TVF exists in this SQLite build).
    copied_sha = _hash_fd(creation_fd)
    with contextlib.suppress(OSError):
        os.fsync(creation_fd)
    if expect_sha256 is not None and copied_sha != expect_sha256:
        # The bytes we just produced are not the bytes that were verified.
        # Refuse rather than deliver an unverified snapshot (review P-fixes
        # 2); the mismatched staging copy is RETAINED as failure evidence
        # (P-fixes 7) and nothing is published.
        raise BackupError(
            f"copied snapshot digest {copied_sha} does not match the verified "
            f"digest {expect_sha256}; the content behind {source} changed "
            "between verification and copy, refusing to proceed"
        )
    return copied_sha


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


def _migration_record(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


def _assert_schema_consistent(conn: sqlite3.Connection, stage: str) -> None:
    """Ordinary transactions require a coherent, audited v1 schema.

    Version-range alone let a database through whose ``user_version`` says v1
    while ``schema_migrations`` is missing, empty, or disagrees (review
    P-fixes 5): such a database cannot prove which migration actually produced
    it.  An UNINITIALISED database (version 0) is now refused outright
    (review P-fixes 8): the public write gate has no legitimate v0 write —
    the old v0 pass-through let ``transaction()`` CREATE TABLE and COMMIT on
    a virgin file, producing unversioned junk that ``migrate_to_head`` must
    then refuse to adopt.  Publishing v1 is exclusively the job of the
    private, privileged migration transaction inside
    :func:`migrate_to_head`, which runs its own BEGIN IMMEDIATE boundary
    and never calls this function.  Mirrors
    :func:`_assert_restorable_snapshot` for live databases; the too-new case
    keeps :class:`SchemaTooNewError`.
    """

    _assert_writable_version(conn, stage)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == 0:
        raise StorageError(
            "database is not initialised (user_version=0): the public write "
            "gate never writes to uninitialised databases; run "
            f"teamcraft.migrate_to_head() to publish v1 first ({stage})"
        )
    try:
        tables = _existing_tables(conn)
    except sqlite3.Error as exc:
        raise StorageError(f"schema catalog unreadable on live connection: {exc}") from exc
    missing = _V1_TABLES - tables
    if missing:
        raise StorageError(
            f"schema is incomplete for ordinary writes, missing tables: "
            f"{sorted(missing)}; run teamcraft.migrate_to_head() ({stage})"
        )
    recorded = _migration_record(conn)
    if recorded is None:
        raise StorageError(
            f"schema_migrations carries no record although user_version={version}; "
            f"refusing to write against an unaudited schema ({stage})"
        )
    if recorded != version:
        raise StorageError(
            f"user_version={version} disagrees with schema_migrations "
            f"record {recorded}; refusing to write against an inconsistent "
            f"schema ({stage})"
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
#: Action codes that are refused on every facade channel regardless of the
#: gate depth (review P-fixes 1).  ``mode=ro`` binds the main file only, so
#: ATTACH was a hole where the "read-only" channel could reach writable
#: neighbouring databases and a bare COMMIT statement persisted the result;
#: DETACH and VACUUM have no first-party use inside a TeamCraft session.
#: The SQLite Online Backup API used by ``backup_database``/``restore_backup``
#: runs on private raw connections and fires no authorizer at all, so these
#: refusals never remove needed migration/backup capability.
#: Action codes refused on every facade channel regardless of gate depth
#: (review P-fixes 1).  ``mode=ro`` binds the main file only, so ATTACH was
#: a hole where the "read-only" channel could reach writable neighbouring
#: databases and a bare COMMIT statement persisted the result; DETACH has no
#: first-party use either.  We deny ONLY codes CPython verifiably exports
#: (SQLITE_ATTACH/SQLITE_DETACH — probed equal to the ABI values 24/25 on
#: this build): numeric guessing is unsafe because other action numbers vary
#: between SQLite versions (e.g. code 23 is emitted by ordinary DDL here, so
#: hard-coding "VACUUM=23" would break migration).  VACUUM stays refused
#: outside the gate simply because it is not in the read allowlist; the
#: Online Backup API used by backup/restore runs on private raw connections
#: and fires no authorizer, so needed migration capability is untouched.
_NEVER_OK_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
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
      fail there at SQLite's own execution boundary.  Because ``mode=ro``
      binds the main file only, the read channel carries the same permanent
      authorizer as the writable one (review P-fixes 1): it cannot ATTACH a
      writable neighbour database and persist through it with a bare COMMIT.
    * **Writable channel**: never handed to callers.  A permanent
      ``sqlite3.set_authorizer`` denies every mutating action code
      (INSERT/UPDATE/DELETE, DDL, ATTACH/DETACH, transaction/savepoint
      control, PRAGMA, ...) while the gate is closed; the authorizer runs
      inside SQLite at prepare/step time, so even an escaped cursor cannot
      write.  Re-preparing identical SQL re-invokes it (no prepared-statement
      cache leak; pinned by regression test).  ATTACH/DETACH/VACUUM are
      denied on BOTH channels even with the gate open: a schema-blessed
      transaction is no licence to mutate attached foreign databases.
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
        if action in _NEVER_OK_ACTIONS:
            # ATTACH/DETACH/VACUUM are denied at SQLite's execution boundary
            # on every facade channel, gate open or closed (review P-fixes 1):
            # ATTACH turned the mode=ro read channel into a write path to
            # foreign databases (the ro flag binds main only), and gate-open
            # sessions get no schema-blessing to mutate attached databases.
            return sqlite3.SQLITE_DENY
        if self._write_depth or self._internal_depth:
            return sqlite3.SQLITE_OK
        if action in _READ_OK_ACTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _authorize_ro(
        self,
        action: int,
        arg1: Any,
        arg2: Any,
        database: Any,
        trigger: Any,
    ) -> int:
        """Authorizer for the strict ``mode=ro`` channel (review P-fixes 1).

        ATTACH/DETACH stay denied regardless of anything else — SQLite's
        ``mode=ro`` flag binds the main file only, so ATTACH was the write
        hole into neighbouring databases.  Named PRAGMA forms are let through
        to the channel's PHYSICAL read-only enforcement: persistent
        assignments (``user_version=``, ``journal_mode=``, ...) fail there
        with "attempt to write a readonly database" and get mapped to
        :class:`WriteOutsideTransactionError`, while legitimate read pragmas
        (``integrity_check``, ``user_version``, ``foreign_keys`` reads,
        ``journal_mode`` reads) keep working — blanket-denaming them would
        only break diagnostics, since they were never write paths.
        """

        if action in _NEVER_OK_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_OK
        return self._authorize(action, arg1, arg2, database, trigger)

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
            # mode=ro binds the MAIN file only: without its own authorizer,
            # this channel could ATTACH a writable neighbour, write through
            # it, and persist with a bare COMMIT statement (review P-fixes 1).
            # The same permanent authorizer as _rw closes that hole; it is
            # installed after the channel's own setup pragmas above.
            self._ro.set_authorizer(self._authorize_ro)
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
            "teamcraft.backup_database()/restore_backup(): they copy into a "
            "private staging file, verify fully, and publish only via "
            "os.link, which can never overwrite an existing target "
            "(review P-fixes 10)"
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
    # created inode: cleanup candidates are limited to files this call
    # created.  Since review P-fixes 6 the failure policy is conservative
    # RETENTION (never unlink): between any ownership check and the unlink,
    # another process can adopt this same inode and commit into it, and no
    # check closes that window (see _retain_created_file).
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
        _retain_created_file(resolved, owned_token)
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
        _retain_created_file(resolved, owned_token)
        raise StorageError(f"cannot initialise connection for {resolved}: {exc}") from exc
    except BaseException:
        # Every non-sqlite exit (SchemaTooNewError, UnsupportedLocationError,
        # KeyboardInterrupt, ...) must still release the connection; the file
        # this call created is conservatively RETAINED, never unlinked
        # (review P-fixes 6: check-then-unlink cannot exclude adoption of
        # the same inode by another process).
        conn.close()
        _retain_created_file(resolved, owned_token)
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
    authorizer (pinned by regression test).  Uninitialised databases
    (``user_version=0``) are refused: only the private migration boundary may
    publish v1 (review P-fixes 8).
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
            _assert_schema_consistent(inner, stage)
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
    """Read-only inspection used after restore (8.2-13).

    Two distinct verdicts, deliberately not conflated (review P-fixes 5):

    * ``ok`` + ``integrity`` + ``user_version``: SQLite's own
      ``PRAGMA integrity_check`` plus the version range — is the FILE a
      structurally sound SQLite database this build can talk to (an empty
      v0 file is legitimately ``ok`` and re-migratable);
    * ``schema_usable``: is it a usable, audited TeamCraft schema — all v1
      tables present, ``schema_migrations`` carries a record, and that record
      agrees with ``user_version``.  A pristine empty ``sqlite3`` database is
      integrity-``ok`` but NOT schema-usable.

    Write-side callers gate on BOTH (an intact file is not yet a usable
    schema); diagnostics can tell the two failure classes apart.
    """

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
        recorded = _migration_record(conn)
    missing = sorted(_V1_TABLES - tables)
    schema_usable = (
        not missing
        and recorded is not None
        and recorded == version
        and 0 <= version <= SCHEMA_VERSION
    )
    ok = integrity == "ok" and 0 <= version <= SCHEMA_VERSION
    return {
        "ok": ok,
        "integrity": integrity,
        "user_version": version,
        "tables": sorted(tables),
        "missing_tables": missing,
        "schema_migrations_recorded": recorded,
        "schema_usable": schema_usable,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_fd(fd: int) -> str:
    """SHA-256 of OUR inode's current bytes, read through an open descriptor.

    The descriptor has been held since the ``O_EXCL`` creation of the file,
    so these bytes are bound to the very inode this call created and wrote
    into: a concurrent replacement of the NAME cannot redirect an ``os.pread``
    on an already-open fd, and no path is re-entered at digest time (review
    P-fixes 8).  This replaces the previous ``pragma_page`` attempt: this
    SQLite build exposes no ``pragma_page`` table-valued function at all, so
    the old code silently fell through to hashing ``PRAGMA database_list``'s
    path — a namespace re-read that can digest a concurrent replacement and
    contradicted the binding this digest exists to provide.  Callers must
    close the writing sqlite connection first so every page is flushed to
    the file (WAL is checkpointed on close).
    """

    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, 1024 * 256, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
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


#: Target-publication mechanism (P-fixes 14: user-authorized P10 model).
#:
#: TRUST MODEL (explicit, user-accepted 2026-10-31): this is a local
#: single-machine library; OTHER SAME-UID PROCESSES ARE DECLARED TRUSTED.
#: Same-uid path games — renaming/pivoting names around our private staging,
#: planting sidecars, or replacing contents at public names after a
#: successful publish — are OUT OF SCOPE by that declaration (they can
#: neither be reliably defended against nor reliably detected from
#: userspace, so we do not pretend otherwise).  The conservative anomaly
#: checks below (path identity, st_nlink, post-publish inode+digest proof)
#: remain because they are cheap accident detection, but safety claims do
#: NOT rest on them.  Journal/WAL sidecars beside the staged copy are
#: deliberately tolerated: copying a WAL-marked source legitimately leaves
#: SQLite's own transient WAL/shm files in the private staging dir, and
#: what we publish is exactly the main inode we verified and digest-bound
#: (sidecars are never published).
#:
#: Guarantees that hold unconditionally (independent of the trust model):
#:
#: * private staging: copies land ONLY inside a fresh 0700 staging dir in
#:   the target's own directory (same filesystem; files created with
#:   O_CREAT | O_EXCL so the kernel proved the names were free); no public
#:   name is ever opened for writing;
#: * full verification BEFORE publishing: integrity_check, v1 schema
#:   completeness and exact version agreement run against the staged file
#:   while it is still private.  WAL sources land as self-consistent
#:   WAL-marked files (the page-level copy embeds every committed frame)
#:   and verify natively — NO header rewriting is used anywhere (review
#:   P-fixes 14);
#: * non-overwriting atomic publish: the final name appears ONLY via
#:   os.link — the content is already complete in the staged inode, the
#:   link fails atomically with EEXIST, so a pre-existing public file is
#:   never overwritten or replaced, and a crash before the link leaves NO
#:   final name at all;
#: * post-publish identity proof (public name resolves to the verified
#:   inode and digests to the verified bytes): anomaly DETECTION only —
#:   mismatch fails closed and retains everything (revocation is never
#:   attempted, and none of it is a load-bearing claim);
#: * retain-never-delete: nothing is ever unlinked or truncated (reviews
#:   P-fixes 7/12); staging dirs are RETAINED on success AND failure —
#:   because publication hard-links the staged inode, retention costs zero
#:   extra data blocks (directory entries only);
#: * plan_retention stays dry-run only: delete=True is refused (review
#:   P-fixes 6), because no check-then-unlink sequence is atomic against
#:   cross-process replacement; leftover cleanup is operator/M1-06 work.
TARGET_PUBLICATION_MECHANISM = (
    "verify-in-private-0700-staging + os.link non-overwriting atomic "
    "publish; same-uid-trusted local model (path games out of scope by "
    "declaration); verify-before-publish; retain-never-delete (success "
    "staging retained, zero-copy hard link); retention delete=True "
    "unavailable (dry-run only)"
)

_STAGING_PREFIX = ".tc-staging-"


def _create_staging_dir(parent: Path) -> Path:
    """Create a private 0700 staging directory (same fs as its parent).

    Names are unpredictable but never relied upon for security (same-uid
    processes are declared trusted for this local library; the real
    guarantees are private-staging-only writes, verify-before-publish and
    the non-overwriting link publish, see
    :data:`TARGET_PUBLICATION_MECHANISM`); the dir is RETAINED on success
    AND on failure — nothing in this module unlinks (P-fixes 7/14).
    """

    for _attempt in range(5):
        candidate = parent / f"{_STAGING_PREFIX}{datetime.now().strftime('%Y%m%dT%H%M%S')}-{os.urandom(4).hex()}"
        try:
            os.mkdir(str(candidate), 0o700)
            return candidate
        except FileExistsError:
            continue
        except OSError as exc:
            raise BackupError(f"cannot create staging directory in {parent}: {exc}") from exc
    raise BackupError(f"cannot allocate a free staging directory in {parent}")


def _write_all(fd: int, payload: bytes) -> None:
    """Write the whole payload through ``fd`` (test seam for crash injection)."""

    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])


def _link_publish(staged: Path, dest: Path, *, what: str) -> None:
    """Create ``dest`` atomically as a hard link to the verified staging inode.

    P-fixes 14 (user-authorized P10 model): the payload is already complete
    inside the private staging inode when this runs, so the public name
    appears ONLY when the link succeeds — a crash before this call leaves NO
    final name at all, and EEXIST makes overwriting any pre-existing public
    file impossible.  The link shares the staged inode, so our
    retain-never-delete policy costs zero extra data blocks (review
    P-fixes 12's retention economics carry over).
    """

    try:
        os.link(str(staged), str(dest))
    except FileExistsError as exc:
        raise BackupError(
            f"{what} target already exists; refusing to overwrite: {dest}"
        ) from exc
    except OSError as exc:
        raise BackupError(f"failed to publish {what} to {dest}: {exc}") from exc


def _assert_staging_intact(staged: Path, expect_inode: tuple[int, int], *, what: str) -> None:
    """Cheap pre-publish sanity gate: identity and adoption.

    * the path must still resolve to the inode we created, verified and
      hashed (a renamed-away or plugged-in path fails closed);
    * ``st_nlink`` must still be 1 — a visible hard-link adoption of our
      staged inode is refused (we would otherwise ship an inode whose
      namespace position someone else shares; the other name is never
      deleted).

    Journal/WAL sidecars beside the staged file are deliberately NOT
    refused (review P-fixes 14): copying a WAL-marked source legitimately
    leaves SQLite's own transient WAL/shm files in the private staging
    directory; publication ships ONLY the main inode we verified through
    our own descriptor, sidecars included or not.  The same-uid world is
    declared trusted (:data:`TARGET_PUBLICATION_MECHANISM`); this gate is
    accident detection, not adversarial proof.
    """

    st = os.stat(str(staged))
    if (st.st_dev, st.st_ino) != expect_inode:
        raise BackupError(
            f"{what} staging path no longer resolves to the verified inode "
            f"(expected {expect_inode}, found {(st.st_dev, st.st_ino)}); "
            "swap detected, refusing to publish (nothing deleted)"
        )
    if st.st_nlink != 1:
        raise BackupError(
            f"{what} staging inode has been adopted (st_nlink={st.st_nlink}); "
            "refusing to publish or destroy it (nothing deleted)"
        )


def _publish_atomically(
    staging_file: Path,
    directory: Path,
    name: str,
    *,
    expect_inode: tuple[int, int],
    expect_sha: str,
    what: str,
) -> None:
    """Atomically publish the fully-written, fully-verified staging file.

    P-fixes 14 (user-authorized P10 model, carrying P-fixes 12 forward):
    writing the payload into the
    FINAL name and then discovering an error (or crashing) mid-write left an
    incomplete file under the name a later restore would consider, which is
    not "publish only after full verification".  Therefore:

    * every byte is written to a PRIVATE staging file first (crash there =
      no final name exists at all, and the evidence is retained);
    * the final public name is created ONLY by ``os.link`` — atomic, and
      EEXIST-refused against any pre-existing file (never overwrite);
    * identity/adoption checks bracket the publish: before (path still
      ours, nlink==1) and after (the public name must resolve to the
      verified inode with the verified digest — mismatch fails closed and
      everything is retained); transient SQLite sidecars in private
      staging are tolerated by design (review P-fixes 14).
    """

    dest = directory / name
    # Conservative anomaly hygiene: refuse early on swap/adoption/sidecar
    # signs (cheap accident detection under the declared same-uid trust
    # model; not the load-bearing safety argument).
    _assert_staging_intact(staging_file, expect_inode, what=what)
    # Atomic, non-overwriting publication (review P-fixes 14): the bytes
    # were complete and verified in private staging BEFORE any public name
    # exists; the public name appears only via os.link, whose EEXIST
    # refuses incumbents; a crash before this call leaves no final name.
    _link_publish(staging_file, dest, what=what)
    # Post-publish identity proof: anomaly DETECTION only (it can neither
    # revoke the name nor be relied upon to; mismatch fails closed and
    # retains everything).
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError as exc:
        raise BackupError(
            f"published {what} cannot be re-verified (directory unreadable); "
            f"failing closed, nothing deleted: {exc}"
        ) from exc
    try:
        try:
            fd = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd
            )
        except OSError as exc:
            raise BackupError(
                f"published {what} vanished or is not a plain file at {dest}; "
                f"failing closed, nothing deleted: {exc}"
            ) from exc
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != expect_inode:
                raise BackupError(
                    f"published {what} at {dest} is not the verified inode "
                    f"(expected {expect_inode}, found {(st.st_dev, st.st_ino)}); "
                    "post-publish swap detected, failing closed (nothing deleted)"
                )
            if _hash_fd(fd) != expect_sha:
                raise BackupError(
                    f"published {what} at {dest} no longer digests to the "
                    "verified value; post-publish mutation detected, failing "
                    "closed (nothing deleted)"
                )
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def backup_database(
    source_path: str | os.PathLike[str],
    dest_dir: str | os.PathLike[str],
    *,
    generation: str | None = None,
    app_version: str = APP_VERSION,
    allow_unverified_location: bool = False,
) -> BackupResult:
    """Consistent online backup with staged verification + hardlink publish.

    Flow (review P-fixes 10): accept the source under restore-grade
    criteria (R8) → copy via the SQLite Online Backup API into a private
    0700 staging file created O_EXCL by THIS call → digest the copied bytes
    through that creation fd (fd-bound, never path-resolved) → verify the
    staged snapshot fully (integrity, v1 completeness, version agreement) →
    publish the verified staged inode at the public name with ``os.link``,
    whose EEXIST semantics make overwriting any pre-existing file
    impossible → re-assert publish identity (same inode + digest via an
    O_NOFOLLOW fd) → write and publish the manifest the same way.  A
    BackupResult is returned ONLY after every check passed; a refusal
    publishes nothing more than already-published names, and NOTHING is
    ever unlinked (retain-on-failure, P-fixes 7).

    Trust boundary and guarantees (review P-fixes 14, user-authorized P10
    model): OTHER SAME-UID PROCESSES ARE DECLARED TRUSTED for this local
    single-machine library — name games against private staging, or public
    content changed AFTER a successful publish, are out of scope by that
    declaration (cheap anomaly checks remain, but nothing claims to defend
    the same-uid world, and post-publish tampering stays detectable later
    via the manifest digest chain).  Guarantees that hold unconditionally:
    never overwrite a pre-existing public target (os.link EEXIST), ship
    only bytes that passed full verification while still private, retain
    everything on every failure, RETAIN the staging dir on SUCCESS too
    (the published hard link shares the staged inode, so retention costs
    zero extra data blocks), and plan_retention stays dry-run — delete=True
    is refused.  Details: :data:`TARGET_PUBLICATION_MECHANISM`.

    The manifest records schema version, app version, digest, size, filename,
    integrity result and the applicable cutover generation (``None`` means
    unbound; retention will never delete unbound evidence; the generation
    protocol itself is M1-06 work).
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

    db_fd: int | None = None
    try:
        # Acceptance gate first (strict mode=ro, WAL-aware): the source must
        # already satisfy the exact criteria restore_backup will later apply,
        # so a "successful" backup is always a restorable snapshot (R8).
        _ensure_supportable(source.parent, allow_unverified_location=allow_unverified_location)
        version, recorded, missing = _snapshot_versions(source)
        _assert_restorable_snapshot(
            version, recorded, missing, subject="backup source"
        )
        staging_dir = _create_staging_dir(destination_dir)
        staged_db = staging_dir / backup_path.name
        staged_manifest = staging_dir / manifest_path.name
        db_fd = os.open(str(staged_db), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        db_inode = _fd_token(db_fd)
        # The copy writes ONLY into this private O_EXCL-created staged file
        # inside the private 0700 staging dir; public names are never opened
        # for writing (reviews P-fixes 10 + 14; same-uid path games are out
        # of scope under the declared trust model).
        copied_sha = _exclusive_online_copy(source, staged_db, creation_fd=db_fd)
        # Full verification of the STAGED FILE (still private) BEFORE
        # anything is published: integrity, v1 completeness and exact
        # version agreement.  WAL-sourced copies verify natively as
        # self-consistent WAL files (pinned by the WAL roundtrip tests);
        # this module contains no header rewriting (review P-fixes 14).
        report = verify_database(staged_db)
        if not (report["ok"] and report["schema_usable"]):
            raise BackupError(f"backup failed verification: {report}")
        integrity = str(report["integrity"])
        staged_version, staged_recorded, staged_missing = _snapshot_versions(staged_db)
        if staged_version != version:
            raise BackupError(
                f"backup snapshot user_version {staged_version} does not match "
                f"the source snapshot version {version}; refusing"
            )
        _assert_restorable_snapshot(
            staged_version, staged_recorded, staged_missing,
            subject="backup snapshot",
        )
        manifest = {
            "backup_file": backup_path.name,
            "backup_sha256": copied_sha,
            "backup_size_bytes": os.fstat(db_fd).st_size,
            "schema_version": version,
            "app_version": app_version,
            "created_at_utc": _utc_now_iso(),
            "origin_database": source.name,
            "integrity_check": integrity,
            "applicable_generation": generation,
            "tool": "teamcraft.storage",
        }
        # Publish atomically (P-fixes 12/14): the verified staging inode was
        # written completely BEFORE any public name exists; the public name
        # appears only via os.link (EEXIST = refuse, never overwrite), with
        # adoption/swap checks before the link and an inode+digest proof
        # after it.  A crash anywhere earlier leaves NO final name.
        _publish_atomically(
            staged_db,
            destination_dir,
            backup_path.name,
            expect_inode=db_inode,
            expect_sha=copied_sha,
            what="backup database",
        )
        payload = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        # The manifest is fully written and digest-proven inside our private
        # staging directory first, then link-published the same way.
        staged_manifest_fd = os.open(
            str(staged_manifest), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
        )
        try:
            _write_all(staged_manifest_fd, payload)
            os.fsync(staged_manifest_fd)
            manifest_inode = _fd_token(staged_manifest_fd)
            manifest_digest = _hash_fd(staged_manifest_fd)
            if manifest_digest != hashlib.sha256(payload).hexdigest():
                raise BackupError(
                    "staged manifest bytes do not digest to the payload we "
                    "built; failing closed before publishing (nothing deleted)"
                )
            _publish_atomically(
                staged_manifest,
                destination_dir,
                manifest_path.name,
                expect_inode=manifest_inode,
                expect_sha=manifest_digest,
                what="backup manifest",
            )
        finally:
            os.close(staged_manifest_fd)
    except BaseException:
        # Failure policy (reviews R7/P-fixes 7/12): NOTHING is deleted and
        # nothing is truncated — the staging directory keeps the staged
        # database (with whatever bytes the copy reached) and any staged
        # manifest as crash-recovery evidence; any already-published name
        # stays in place, unclaimed.  The raise means no BackupResult
        # exists, and published-but-unmanifested files are invisible to
        # plan_retention; leftover removal is human work (or M1-06), never
        # a check-then-unlink or truncate here.
        raise
    finally:
        if db_fd is not None:
            with contextlib.suppress(OSError):
                os.close(db_fd)
    return BackupResult(
        backup_path=backup_path,
        manifest_path=manifest_path,
        sha256=copied_sha,
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
    """Restore a verified backup into a *new* destination (P-fixes 10).

    Truth hierarchy: the backup snapshot itself (user_version +
    schema_migrations) is authoritative for what the backup contains; the
    manifest must agree with it on schema version, digest, size and filename,
    carry every required audit field, and report a clean integrity check.
    Snapshot reads are strict ``mode=ro`` — committed WAL frames included —
    so a stale main-file view can never smuggle a wrong version through.

    The copy runs ONLY into a private 0700 staging file (O_EXCL, this call's
    inode) beside the destination; the manifest digest and snapshot version
    are bound through the creation fd of the copied bytes; integrity,
    schema-usability and version agreement are verified on the STAGED file;
    and only then is the verified inode published at ``dest_path`` via
    ``os.link`` — whose EEXIST semantics make overwriting a pre-existing
    destination impossible, race included — followed by the post-publish
    inode+digest identity proof (fail closed).  A RestoreResult is returned
    only after every check passed.  NOTHING is ever unlinked: a failed
    restore leaves staging (and, if the publish had already succeeded,
    nothing more) in place as inert evidence, and post-restore domain
    reconciliation (DB/Herdr/Git) remains the caller's duty before dispatch
    resumes (8.2-13).  The declared same-uid trust boundary (same-uid
    processes are TRUSTED for this local library; path games are out of
    scope by declaration) and the unconditional guarantees — never-overwrite
    link publish (EEXIST), verify-before-publish, retain-never-delete with
    success staging retained — are documented on
    :data:`TARGET_PUBLICATION_MECHANISM` (reviews P-fixes 10 + 14).
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
        # Bind through a NOFOLLOW read fd (review P-fixes 11): the size and
        # digest checked here are of the inode we opened, not of whatever
        # path position resolves to at check time; the copy step re-binds
        # the digest onto the actually-copied bytes through the staged fd.
        bfd = os.open(str(backup), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            expected_size = _manifest_integer(manifest, "backup_size_bytes")
            if expected_size != os.fstat(bfd).st_size:
                raise BackupError("manifest size does not match backup file")
            if _hash_fd(bfd) != manifest.get("backup_sha256"):
                raise BackupError("manifest digest does not match backup file")
        finally:
            os.close(bfd)
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
    # P-fixes 2 + 10: hand the verified identity (manifest digest + snapshot
    # version) into the copy step so verification binds the bytes that are
    # ACTUALLY copied into the staging creation fd; a source replaced after
    # the checks above aborts the restore instead of being reported
    # successful.
    bound_sha: str | None = None
    if manifest is not None:
        bound_sha = str(manifest.get("backup_sha256"))
    dest_fd: int | None = None
    try:
        staging_dir = _create_staging_dir(dest.parent)
        staged_dest = staging_dir / dest.name
        dest_fd = os.open(str(staged_dest), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        dest_inode = _fd_token(dest_fd)
        copied_sha = _exclusive_online_copy(
            backup,
            staged_dest,
            creation_fd=dest_fd,
            expect_sha256=bound_sha,
            expect_version=snapshot_user_version,
        )
        # Full verification of the STAGED destination (still private)
        # before publication: integrity, schema usability, exact
        # snapshot-version agreement and v1 completeness.
        report = verify_database(staged_dest)
        if not (report["ok"] and report["schema_usable"]):
            raise BackupError(f"restore failed verification: {report}")
        integrity = str(report["integrity"])
        staged_version, staged_recorded, staged_missing = _snapshot_versions(staged_dest)
        if staged_version != snapshot_user_version:
            raise BackupError(
                f"restored database user_version {staged_version} does not "
                f"match the verified backup snapshot version "
                f"{snapshot_user_version}; refusing"
            )
        _assert_restorable_snapshot(
            staged_version, staged_recorded, staged_missing,
            subject="restored database",
        )
        # Publish atomically (P-fixes 12/14): the staged destination holds the
        # complete verified bytes before any public name exists; the public
        # destination appears only via os.link (EEXIST = refuse — a
        # pre-existing destination, including one created during a race, is
        # never overwritten), bracketed by adoption/swap checks before the
        # link and an inode+digest proof after it.
        _publish_atomically(
            staged_dest,
            dest.parent,
            dest.name,
            expect_inode=dest_inode,
            expect_sha=copied_sha,
            what="restore destination",
        )
    except BaseException:
        # Failure policy (reviews R7 + P-fixes 7/12): NOTHING is deleted
        # and nothing is truncated.  Staging — the only place our writes
        # ever land — is retained whole as crash-recovery evidence; the
        # public destination was either never created (any failure before
        # the link leaves NO final name) or was link-published only after
        # full verification with the fd-bound content proof.  No
        # RestoreResult is returned on failure, so nothing can be *reported*
        # as a successful restore; leftover cleanup belongs to human review
        # or a future provably-safe mechanism (M1-06).
        raise
    finally:
        if dest_fd is not None:
            with contextlib.suppress(OSError):
                os.close(dest_fd)
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
    """Conservative retention PLANNING entry point (dry-run only).

    Never deletable (8.2-13 audit protection): entries with no readable
    manifest (unknown provenance), manifests flagged ``protect: true``, and
    entries whose ``applicable_generation`` is missing/None (generation-
    unknown recovery evidence cannot be proven superseded).  Only
    generation-bound, unprotected backups participate in keep_last/age
    selection.  Real defaults belong to M1-06 policy work.

    Actual deletion is currently REFUSED (review P-fixes 6): no portable,
    cross-process-safe primitive can make "verify, then unlink" atomic —
    even an inode-token + digest re-check leaves a final window in which a
    replacement (reproduced via ``Path.unlink`` injection) lands before the
    unlink and the policy destroys another process's file.  ``delete=True``
    therefore raises :class:`BackupError` (fail closed); the selection is
    returned unchanged for inspection via ``delete=False``.  Re-enabling
    deletion belongs to M1-06 behind an atomic, ownership-proving mechanism.
    """

    directory = Path(backup_dir)
    if not directory.is_dir():
        raise BackupError(f"backup directory missing: {directory}")
    if keep_last is None and older_than_days is None:
        raise ValueError("plan_retention requires keep_last and/or older_than_days")
    # Negative retention knobs are always nonsense (keep_last=-1 would select
    # EVERY entry).  Reject loudly (review P-fixes 3).
    if isinstance(keep_last, bool) or (keep_last is not None and keep_last < 0):
        raise ValueError(f"keep_last must be a non-negative integer, got {keep_last!r}")
    if isinstance(older_than_days, bool) or (
        older_than_days is not None and older_than_days < 0
    ):
        raise ValueError(
            f"older_than_days must be a non-negative integer, got {older_than_days!r}"
        )

    entries: list[tuple[str, Path, Path]] = []
    for backup_file in sorted(directory.glob("*.db")):
        manifest_path = backup_file.with_name(backup_file.name + ".manifest.json")
        if not manifest_path.is_file():
            continue  # unknown provenance: never touch
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        if manifest.get("protect") is True:
            continue
        if manifest.get("applicable_generation") in (None, ""):
            continue  # generation-unknown recovery evidence: never auto-delete
        if manifest.get("backup_file") != backup_file.name:
            continue  # manifest does not describe this file: conservative skip
        entries.append((str(manifest.get("created_at_utc", "")), backup_file, manifest_path))
    entries.sort(key=lambda item: item[0])

    selected: list[tuple[str, Path, Path]] = []
    if keep_last is not None and len(entries) > keep_last:
        selected.extend(entries[: len(entries) - keep_last])
    if older_than_days is not None:
        cutoff = datetime.now().timestamp() - older_than_days * 86400
        chosen_paths = {entry[1] for entry in selected}
        for entry in entries:
            if entry[1] in chosen_paths:
                continue
            try:
                when = datetime.fromisoformat(entry[0]).timestamp()
            except (TypeError, ValueError):
                continue  # unparseable timestamp: conservative, keep it
            if when < cutoff:
                selected.append(entry)
                chosen_paths.add(entry[1])

    seen: set[Path] = set()
    unique = [entry[1] for entry in selected if not (entry[1] in seen or seen.add(entry[1]))]

    # Actual deletion is refused (review P-fixes 6): "verify everything,
    # then unlink" is not atomic against cross-process races — a replacement
    # landing between the LAST re-check and the unlink (reproduced with a
    # Path.unlink injection) would still delete another process's file, and
    # no further re-checking can close that last window.  Until the M1-06
    # retention protocol provides an atomic, ownership-proving mechanism,
    # delete=True fails closed and only dry-run selection is offered.
    if delete:
        raise BackupError(
            "plan_retention(delete=True) is refused: check-then-unlink cannot "
            "be made atomic against cross-process replacement, so real "
            "deletion stays disabled (fail closed) until the retention "
            "protocol can prove ownership at removal time (M1-06). Call with "
            "delete=False for the dry-run selection"
        )
    return unique
