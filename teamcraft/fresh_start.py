"""M1-06 SQLite Fresh Start (Owner decision 2026-11-02, SQLite-only).

Owner confirmation (2026-11-02): this product has no production legacy system
and no legacy state that needs migration.  TeamCraft 2.0 is therefore a
**SQLite-only Fresh Start**: ``.teamcraft/teamcraft.db`` at the current schema
head is the state authority from project initialisation onward.  There is no
production JSON->SQLite migration, no JSON rollback path, no dual authority,
no old-writer freeze service and no cutover.  The former migration plan
(1477cc0, rehearsal-only) is superseded and labelled as such in
``doc/TEAMCRAFT_2_0_PLAN.md`` §11; git history is not rewritten.

This module is a thin wrapper over :mod:`teamcraft.storage` and adds NO
parallel integrity or concurrency machinery:

* :func:`initialize_project_root` — for a new project root, create
  ``.teamcraft/teamcraft.db`` via ``storage.migrate_to_head`` (transactional,
  concurrent-safe, idempotent by M1-03/M1-04 guarantees), gate readiness on
  ``storage.verify_database`` reporting integrity ok and a complete usable
  schema at head, and prove the result reopens cleanly through
  ``storage.connect``.  There is deliberately **no persisted Fresh Start
  marker**: readiness is exactly "``migrate_to_head`` succeeded and
  ``verify_database`` passes", so no extra protocol/status document can
  disagree with the database.  Because the schema DDL commits atomically
  inside ``migrate_to_head``, an interrupted initialization never leaves a
  *ready-looking* database: the result is either no database file, or — as
  the SIGKILL contract test demonstrates — a valid SQLite file rolled back to
  schema version 0.  Such a schema-zero remnant is explicitly NOT READY
  (``verify_database`` never reports head) and is refused or safely
  re-initialized by a later call; nothing is ever auto-deleted.

Refusals (all fail closed and never modify, move or delete the refused
artefact):

* relative/missing/non-directory or symlinked project root;
* a non-empty legacy ``state.json`` in ``.teamcraft/`` or the pre-rename
  ``.herdr-team/`` — explicit human disposition is required first, and this
  module never reads, rewrites, moves or deletes it (zero-byte residue is
  preserved untouched);
* a database path that exists but is a symlink, not a regular file, or
  non-empty without the SQLite header;
* a database that fails the post-migration verification gate
  (:class:`FreshStartIncompleteError`) — too-new schema and corrupt files are
  already refused inside ``storage`` before anything changes.

M1-07 (SQLite runtime integration: CLI/Command Service wiring) is
**UNAUTHORIZED**: nothing in the runtime imports this module, and this module
never contacts Herdr, sends prompts, or touches any state outside the
explicitly passed project root.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import storage
from .errors import StorageError, TeamCraftError, ValidationError

#: Current runtime directory for a project (2026-10-27 decision).
RUNTIME_DIRNAME = ".teamcraft"
#: Pre-rename runtime directory; non-empty legacy state in either directory
#: marks the root as NOT fresh and requires explicit human disposition.
LEGACY_RUNTIME_DIRNAME = ".herdr-team"
LEGACY_STATE_FILENAME = "state.json"
DATABASE_FILENAME = storage.DEFAULT_DATABASE_FILENAME
DATABASE_RELATIVE_PATH = f"{RUNTIME_DIRNAME}/{DATABASE_FILENAME}"

_SQLITE_MAGIC = b"SQLite format 3\x00"


class FreshStartError(TeamCraftError):
    """Base for Fresh Start refusals; refusals never modify existing state."""


class LegacyStatePresentError(FreshStartError):
    """A non-empty legacy JSON state exists; the root is not fresh.

    The legacy file itself is never read into, rewritten, moved or deleted —
    explicit human disposition is the only exit.
    """

    def __init__(self, message: str, *, path: str, size: int) -> None:
        super().__init__(message)
        self.path = path
        self.size = size


class UnsupportedDatabaseFileError(FreshStartError):
    """The target exists but is not a usable SQLite database file."""

    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(message)
        self.path = path


class FreshStartIncompleteError(FreshStartError):
    """Post-migration verification did not confirm a complete head schema."""


# ---------------------------------------------------------------------------
# Guards (checked before anything is opened or created)
# ---------------------------------------------------------------------------


def _require_absolute_root(value: str | os.PathLike[str]) -> Path:
    text = str(value)
    if not text.strip():
        raise ValidationError("project root must not be empty")
    root = Path(value)
    if not root.is_absolute():
        raise ValidationError(
            f"project root must be an explicit absolute path (got {text!r}); "
            "Fresh Start deliberately has no cwd/env default"
        )
    if root.is_symlink():
        raise ValidationError(f"project root must not be a symlink: {root}")
    if not root.exists():
        raise ValidationError(f"project root does not exist: {root}")
    if not root.is_dir():
        raise ValidationError(f"project root is not a directory: {root}")
    return root


def _assert_no_legacy_state(root: Path) -> None:
    """Fail closed on non-empty legacy JSON; zero-byte residue is preserved.

    Runtime directory components are inspected with ``lstat`` *before* any
    lookup through them, so symlinked or irregular runtime directories are
    refused without traversal.  Only ``FileNotFoundError`` counts as "no
    state"; any other ``OSError`` (e.g. ``PermissionError``) fails closed —
    an unreadable location must never be mistaken for a fresh one.
    """

    for dir_name in (RUNTIME_DIRNAME, LEGACY_RUNTIME_DIRNAME):
        candidate_dir = root / dir_name
        try:
            dir_info = candidate_dir.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise FreshStartError(
                f"cannot safely inspect runtime directory {candidate_dir}: {exc}"
            ) from exc
        if stat.S_ISLNK(dir_info.st_mode):
            raise ValidationError(
                f"runtime directory must not be a symlink: {candidate_dir}; "
                "Fresh Start refuses to traverse it and nothing was modified"
            )
        if not stat.S_ISDIR(dir_info.st_mode):
            raise ValidationError(
                f"runtime directory exists but is not a directory: {candidate_dir}"
            )
        candidate = candidate_dir / LEGACY_STATE_FILENAME
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise FreshStartError(
                f"cannot safely inspect legacy state {candidate}: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise LegacyStatePresentError(
                f"legacy state path is a symlink and will not be followed: "
                f"{candidate}; disposition it explicitly before Fresh Start",
                path=str(candidate),
                size=0,
            )
        if not stat.S_ISREG(info.st_mode):
            raise LegacyStatePresentError(
                f"legacy state path is not a regular file: {candidate}",
                path=str(candidate),
                size=int(info.st_size),
            )
        if info.st_size > 0:
            raise LegacyStatePresentError(
                f"non-empty legacy JSON state present: {candidate} "
                f"({info.st_size} bytes). SQLite-only Fresh Start refuses to "
                "initialize over legacy state; no import, move or delete is "
                "performed — disposition it explicitly first.",
                path=str(candidate),
                size=int(info.st_size),
            )


def _assert_database_file_usable(path: Path) -> None:
    """Safe refusal for irregular target files, before SQLite opens them."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return  # fresh creation is allowed
    if stat.S_ISLNK(info.st_mode):
        raise UnsupportedDatabaseFileError(
            f"database path must not be a symlink: {path}; refused without "
            "following it and nothing was modified",
            path=str(path),
        )
    if not stat.S_ISREG(info.st_mode):
        raise UnsupportedDatabaseFileError(
            f"database path exists but is not a regular file: {path}",
            path=str(path),
        )
    if info.st_size == 0:
        return  # empty file: SQLite treats it as a brand-new database
    with path.open("rb") as handle:
        header = handle.read(len(_SQLITE_MAGIC))
    if header != _SQLITE_MAGIC:
        raise UnsupportedDatabaseFileError(
            f"existing file at {path} is not a SQLite database "
            f"({info.st_size} bytes); Fresh Start refuses to overwrite or "
            "delete it — disposition it explicitly first",
            path=str(path),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FreshStartReport:
    """Outcome of one :func:`initialize_project_root` call."""

    project_root: str
    database_path: str
    created: bool
    reused: bool
    schema_version: int
    integrity: str
    at_head: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "database_path": self.database_path,
            "created": self.created,
            "reused": self.reused,
            "schema_version": self.schema_version,
            "integrity": self.integrity,
            "at_head": self.at_head,
        }


def initialize_project_root(
    project_root: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = 10000,
) -> FreshStartReport:
    """Bring ``project_root`` to a verified SQLite Fresh Start state.

    Readiness is exactly: ``storage.migrate_to_head`` completed and
    ``storage.verify_database`` reports integrity ok, schema usable and at
    head, and the database reopens through ``storage.connect``.  Idempotent:
    an already-initialized root is re-verified and reused with no row, schema
    or file changes.  Concurrent first inits converge inside
    ``migrate_to_head`` (single ``BEGIN IMMEDIATE`` winner/loser semantics).
    Failures are fail closed and never modify pre-existing files; retrying
    converges.  Never contacts Herdr and never performs any JSON migration or
    cutover.
    """

    root = _require_absolute_root(project_root)
    _assert_no_legacy_state(root)

    runtime = root / RUNTIME_DIRNAME
    try:
        runtime_info = runtime.lstat()
    except FileNotFoundError:
        runtime_info = None
    if runtime_info is not None:
        if stat.S_ISLNK(runtime_info.st_mode):
            raise ValidationError(f"{RUNTIME_DIRNAME} must not be a symlink: {runtime}")
        if not stat.S_ISDIR(runtime_info.st_mode):
            raise ValidationError(
                f"{RUNTIME_DIRNAME} exists but is not a directory: {runtime}"
            )
    try:
        runtime.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise FreshStartError(
            f"cannot create the {RUNTIME_DIRNAME} directory in project root "
            f"{root}: {exc} (is the project root writable?)"
        ) from exc

    db_path = runtime / DATABASE_FILENAME
    _assert_database_file_usable(db_path)

    # Transactional, concurrent-safe create/adopt (M1-03 guarantees reused).
    migration = storage.migrate_to_head(db_path, busy_timeout_ms=busy_timeout_ms)
    verification = storage.verify_database(db_path)
    if not (
        verification["ok"]
        and verification["schema_usable"]
        and verification["at_head"]
        and verification["integrity"] == "ok"
    ):
        failed = [
            key
            for key in ("ok", "schema_usable", "at_head", "integrity")
            if not (
                verification[key] if key != "integrity" else verification[key] == "ok"
            )
        ]
        raise FreshStartIncompleteError(
            f"database at {db_path} did not pass the Fresh Start schema/"
            f"integrity gate (failed: {failed}); nothing was published and "
            "the database file was left in place untouched"
        )

    # Readiness also requires a runtime-style reopen (WAL, FK, write gate)
    # against the verified head database; no writes happen during the probe.
    try:
        conn = storage.connect(db_path)
    except StorageError as exc:
        raise FreshStartIncompleteError(
            f"verified database at {db_path} could not be reopened for the "
            f"runtime boundary: {exc}"
        ) from exc
    conn.close()

    return FreshStartReport(
        project_root=str(root),
        database_path=str(db_path),
        created=migration.fresh,
        reused=not migration.fresh,
        schema_version=int(verification["user_version"]),
        integrity=str(verification["integrity"]),
        at_head=bool(verification["at_head"]),
    )
