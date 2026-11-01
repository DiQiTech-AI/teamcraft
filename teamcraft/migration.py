"""M1-06 Legacy v1 import rehearsal and cutover fault matrix (never a real cutover).

Authority: ``doc/TEAMCRAFT_2_0_M0_REVIEW.md`` §8.2 (single-writer migration),
§8.3 (SQLite cutover protocol + crash recovery) and §9.1 M1-06.  M1-06 builds
the *rehearsal* of that protocol on synthetic fixtures; M1-07 owns deploying the
guard into the real legacy entry point and performing the approved cutover.

============================================================
 HARD SAFETY BOUNDARIES (pinned by tests/test_migration_m106.py)
============================================================

1. **Explicit synthetic source only.**  Every path here must be passed
   explicitly and absolutely.  There is no default source, no cwd/env fallback,
   and no "find the project state" convenience.  Any path with a
   ``.teamcraft``/``.herdr-team`` runtime component (the real state layout) is
   refused *before* it is opened, so this module cannot read or overwrite the
   real ``.teamcraft/state.json``, the real ``.teamcraft/teamcraft.db``, or a
   real cutover marker.
2. **The legacy JSON is never mutated.**  Snapshotting, hashing and rehearsal
   only read the source (open ``O_RDONLY`` + ``flock(LOCK_SH)``-free plain
   read).  Byte preservation is verifiable: the fingerprint (size, inode,
   device, mtime_ns, SHA-256) plus a byte-identical backup copy in an explicit
   artifact directory.  Nothing here writes next to the source.
3. **SQLite is a disposable shadow.**  Unless a rehearsal actually reaches the
   durable ``db_authoritative`` commit inside a *temporarily created* database,
   ``legacy_cutover_state.authoritative`` stays ``'json'`` and the JSON file is
   the sole writer.  The shadow database carries no external side effects: this
   module never shells out, never contacts Herdr, never sends a prompt, never
   touches Git, and never creates a command row (no resume/resend is ever
   produced from imported history).
4. **Fail closed on fencing.**  A legacy writer is only considered fenced when
   the caller supplies evidence that every known legacy entry consults the
   freeze/generation marker *while holding* the legacy ``state.lock``
   immediately before each mutation (§8.3 step 2), and the quiescence proof saw
   a stable fingerprint for the whole window (§8.3 step 3).  Anything unknown
   (a loaded old process that does not know the marker, an un-enumerable
   writer, an unstable fingerprint, a corrupt marker, an un-usable lock)
   refuses cutover: JSON stays the sole writer, the shadow DB is not promoted,
   the deployed freeze marker is lifted, and the refusal is recorded as an
   event.
5. **Historical rows, not fake progress.**  Imported records never become
   ``DONE``/verified: legacy ``completed``/``released`` map to a Task in
   ``needs_human`` (acceptance evidence does not exist in v1), and ambiguous
   legacy phases map to an ``unknown`` Attempt, which keeps the single
   occupying slot so nothing can be silently re-dispatched.  Cross-session
   state imports as history with ``unknown`` Sessions and
   ``adopt/resume/resend = 0`` (8.2-2).

Module-local schema
------------------
The three tables below are created by this module inside an ordinary
``storage.transaction`` (additive; ``user_version`` is untouched because no
invariant of v1..v3 changes).  They live only in rehearsal databases.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any

from . import domain, storage
from . import events as core_events
from .clock import Clock, SystemClock
from .errors import MigrationError, ValidationError
from .ids import EntityId, require_entity_id

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The only legacy JSON generation this reader understands (``teamctl.py``
#: ``STATE_VERSION = 1``).  It is NOT a cutover generation (8.3 step 1).
LEGACY_STATE_VERSION = 1

LEGACY_STATE_FILENAME = "state.json"
LEGACY_LOCK_FILENAME = "state.lock"

#: Directory names that mark a *real* runtime layout.  Any path containing one
#: of these components is refused, so rehearsal can never land on production
#: state (safety rule 1).
RUNTIME_DIRECTORY_NAMES: frozenset[str] = frozenset({".teamcraft", ".herdr-team"})
PRODUCTION_DATABASE_FILENAME = "teamcraft.db"

#: Versions of the mapping itself.  Changing a mapping rule means bumping this,
#: which changes the plan digest and therefore forces a fresh rehearsal rather
#: than silently reusing an older rehearsal's rows.
MAPPER_VERSION = "m1-06.v1"
#: Marker document format for freeze/cutover markers (guard contract, 8.3-2).
MARKER_PROTOCOL = "teamcraft-cutover-marker"
MARKER_DOCUMENT_VERSION = 1
#: Authority modes used by markers and the DB authority row.
MODE_JSON = "json"
MODE_DB = "sqlite"
MARKER_FREEZE = "freeze"
MARKER_DB_AUTHORITATIVE = "db_authoritative"
MARKER_JSON_AUTHORITATIVE = "json_authoritative"
MARKER_MODES: tuple[str, ...] = (
    MARKER_FREEZE,
    MARKER_DB_AUTHORITATIVE,
    MARKER_JSON_AUTHORITATIVE,
)

#: Fixed namespace for deriving stable IDs from legacy records: repeated
#: rehearsals of the same source produce the SAME Task/Attempt/Session IDs,
#: which is what makes idempotence checkable and gives legacy records the
#: stable identity §9.1 M1-06 lists as a risk.
LEGACY_ID_NAMESPACE = uuid.UUID("3f2b8a1c-7c4d-5e6f-9a0b-1c2d3e4f5061")

#: Fault-injection stages.  ``fault(stage)`` is called at each boundary; a hook
#: that raises simulates a crash there (in-process), and a hook that blocks lets
#: a parent process SIGKILL the child at exactly that boundary.
STAGE_PLAN = "plan"
STAGE_SNAPSHOT = "snapshot"
STAGE_HASH_CHECK = "hash_check"
STAGE_FREEZE_MARKER = "freeze_marker"
STAGE_LOCK_ACQUIRE = "lock_acquire"
STAGE_QUIESCENCE = "quiescence"
STAGE_IMPORT_BEGIN = "import_begin"
STAGE_IMPORT_ROWS = "import_rows"
STAGE_BEFORE_COMMIT = "before_db_commit"
STAGE_AFTER_COMMIT = "after_db_commit"
#: Shadow import commit boundary (distinct from the cutover authority commit).
STAGE_AFTER_IMPORT_COMMIT = "after_import_commit"
STAGE_BEFORE_MARKER_PUBLISH = "before_marker_publish"
STAGE_AFTER_MARKER_PUBLISH = "after_marker_publish"
STAGE_RELEASE_LOCK = "release_lock"
CUTOVER_STAGES: tuple[str, ...] = (
    STAGE_PLAN,
    STAGE_SNAPSHOT,
    STAGE_HASH_CHECK,
    STAGE_FREEZE_MARKER,
    STAGE_LOCK_ACQUIRE,
    STAGE_QUIESCENCE,
    STAGE_IMPORT_BEGIN,
    STAGE_IMPORT_ROWS,
    STAGE_BEFORE_COMMIT,
    STAGE_AFTER_IMPORT_COMMIT,
    STAGE_AFTER_COMMIT,
    STAGE_BEFORE_MARKER_PUBLISH,
    STAGE_AFTER_MARKER_PUBLISH,
    STAGE_RELEASE_LOCK,
)

FaultHook = Callable[[str], None]

#: Actions a cross-Session import must never trigger on its own (8.2-2).
PROHIBITED_CROSS_SESSION_ACTIONS: tuple[str, ...] = (
    "adopt_pane",
    "resume_attempt",
    "resend_prompt",
)

# Legacy phase -> (task status, attempt status).  Never a terminal Task
# status: v1 has no verification/delivery evidence, so nothing imports as
# done/cancelled-by-verification (8.2-4, 8.4).
_LEGACY_PHASE_MAP: dict[str, tuple[str, str]] = {
    "reserved": ("pending", "created"),
    "split_requested": ("pending", "created"),
    "split_ambiguous": ("running", "unknown"),
    "pane_created": ("pending", "created"),
    "start_requested": ("pending", "starting"),
    "start_ambiguous": ("running", "unknown"),
    "agent_started": ("pending", "starting"),
    "prompt_submitting": ("running", "unknown"),
    "prompt_ambiguous": ("running", "unknown"),
    "prompt_submitted": ("running", "active"),
    "prompt_skipped": ("pending", "created"),
    "completed": ("needs_human", "finished"),
    "released": ("needs_human", "cancelled"),
}
#: Phases whose attempt keeps the occupying slot and must be reconciled by a
#: human/M2 reconcile, never auto-retried.
AMBIGUOUS_LEGACY_PHASES = frozenset(
    {
        "split_ambiguous",
        "start_ambiguous",
        "prompt_submitting",
        "prompt_ambiguous",
    }
)
#: Live signals we may carry over verbatim.  Anything else (``blocked``,
#: ``done``, absent, unrecognized) imports as ``unknown`` instead of being
#: guessed into a liveness state (8.2-4: live signals are not progress).
_LIVE_STATE_MAP: dict[str, str] = {
    "working": "working",
    "idle": "idle",
    "missing": "missing",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MigrationSafetyError(MigrationError):
    """Base for M1-06 refusals; every refusal is fail-closed."""


class ProductionStateRefusal(MigrationSafetyError):
    """The caller pointed at a real runtime layout; rehearsal refuses to open it."""


class LegacyStateInvalid(MigrationSafetyError):
    """The legacy v1 document is corrupt, unsupported, or structurally unusable."""


class LockUnavailable(MigrationSafetyError):
    """The legacy ``state.lock`` could not be taken: a writer may still be live."""


class MarkerCorrupt(MigrationSafetyError):
    """A freeze/cutover marker exists but cannot be trusted (fail closed)."""


class CutoverRefused(MigrationSafetyError):
    """Cutover cancelled: JSON stays the sole writer, SQLite stays a shadow.

    ``reasons`` are machine-readable codes; ``authoritative`` is the authority
    the system is left in (always ``'json'`` on refusal) and
    ``dispatch_paused`` reports whether a freeze marker still blocks entries.
    """

    def __init__(
        self,
        message: str,
        *,
        reasons: Sequence[str],
        authoritative: str = MODE_JSON,
        dispatch_paused: bool = False,
        generation: str | None = None,
        marker_lifted: bool = False,
    ) -> None:
        super().__init__(message)
        self.reasons = list(reasons)
        self.authoritative = authoritative
        self.dispatch_paused = dispatch_paused
        self.generation = generation
        self.marker_lifted = marker_lifted


class RollbackBlocked(MigrationSafetyError):
    """A rollback would have to guess which side held side effects."""


# ---------------------------------------------------------------------------
# Path guards (safety rule 1)
# ---------------------------------------------------------------------------


def require_absolute_path(value: str | os.PathLike[str], *, what: str) -> Path:
    """Return an absolute path, refusing relative/defaulted locations."""

    text = str(value)
    if not text.strip():
        raise ValidationError(f"{what} must not be empty")
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError(
            f"{what} must be an explicit absolute path (got {text!r}); "
            "this module deliberately has no cwd/env default"
        )
    return path


def assert_not_runtime_path(path: str | os.PathLike[str], *, what: str) -> Path:
    """Refuse any path inside a real ``.teamcraft``/``.herdr-team`` runtime.

    Both the lexical and resolved forms are checked so a symlink through a
    runtime directory cannot smuggle production state in.
    """

    candidate = Path(path)
    for view in (candidate, candidate.resolve(strict=False)):
        names = set(view.parts)
        if names & RUNTIME_DIRECTORY_NAMES:
            raise ProductionStateRefusal(
                f"{what} points into a real runtime directory: {view}. "
                "M1-06 rehearsal only operates on explicit synthetic fixtures; "
                "production state is untouched and no migration was performed."
            )
    if candidate.name == PRODUCTION_DATABASE_FILENAME:
        raise ProductionStateRefusal(
            f"{what} uses the production database filename "
            f"{PRODUCTION_DATABASE_FILENAME!r}; rehearsal databases must use an "
            "explicitly named disposable path inside a test fixture directory."
        )
    return candidate


def _guarded_file_path(
    value: str | os.PathLike[str], *, what: str
) -> Path:
    path = require_absolute_path(value, what=what)
    assert_not_runtime_path(path, what=what)
    if path.is_symlink():
        raise ValidationError(f"{what} must not be a symlink: {path}")
    return path


def _guarded_existing_file(value: str | os.PathLike[str], *, what: str) -> Path:
    path = _guarded_file_path(value, what=what)
    parent = path.parent
    if parent.is_symlink():
        raise ValidationError(f"{what} parent must not be a symlink: {parent}")
    if not parent.is_dir():
        raise ValidationError(f"{what} parent directory does not exist: {parent}")
    if not path.exists():
        raise ValidationError(f"{what} does not exist: {path}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValidationError(f"{what} must be a regular file: {path}")
    return path


def _ensure_directory(value: str | os.PathLike[str], *, what: str) -> Path:
    path = require_absolute_path(value, what=what)
    assert_not_runtime_path(path, what=what)
    if path.is_symlink():
        raise ValidationError(f"{what} must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValidationError(f"{what} is not a directory: {path}")
    return path


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_iso(clock: Clock | None) -> str:
    active: Clock = clock if clock is not None else SystemClock()
    return active.now().astimezone(timezone.utc).isoformat()


def legacy_entity_id(kind: str, team_id: str, discriminator: str) -> EntityId:
    """Stable, re-derivable ID for a legacy record (idempotent rehearsals).

    ``uuid5`` over the mapper version plus the legacy coordinates: the same
    source always maps to the same domain IDs, while a mapper-rule change
    (version bump) or a different team/name produces a different ID.
    """

    require_entity_id(discriminator)
    material = f"{MAPPER_VERSION}\0{kind}\0{team_id}\0{discriminator}"
    return EntityId(str(uuid.uuid5(LEGACY_ID_NAMESPACE, material)))


# ---------------------------------------------------------------------------
# Legacy v1 reader (frozen: this is the v1 contract, read-only)
# ---------------------------------------------------------------------------


def validate_legacy_state(raw: bytes, *, source: str = "<bytes>") -> dict[str, Any]:
    """Parse and strictly validate a Legacy v1 ``state.json`` payload.

    Fail-closed on every structural defect: non-UTF-8 bytes, non-JSON, non
    object root, ``version != 1``, missing/blank ``team_id``, non-dict
    ``members``, non-string member keys, non-dict members, blank names, and
    name/key mismatches.  Unknown extra keys/roles/phases are NOT rejected
    here; they are retained verbatim and flagged by the mapper.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LegacyStateInvalid(f"legacy state is not valid UTF-8: {source}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LegacyStateInvalid(f"legacy state is not valid JSON: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LegacyStateInvalid(
            f"legacy state root must be a JSON object, got "
            f"{type(payload).__name__}: {source}"
        )
    version = payload.get("version")
    if version != LEGACY_STATE_VERSION:
        raise LegacyStateInvalid(
            f"unsupported legacy state version {version!r} (only "
            f"{LEGACY_STATE_VERSION} is understood): {source}"
        )
    team_id = payload.get("team_id")
    if not isinstance(team_id, str) or not team_id.strip():
        raise LegacyStateInvalid(f"legacy state has no usable team_id: {source}")
    root = payload.get("project_root")
    if not isinstance(root, str) or not root.strip():
        raise LegacyStateInvalid(f"legacy state has no usable project_root: {source}")
    members = payload.get("members")
    if members is None:
        members = {}
    if not isinstance(members, dict):
        raise LegacyStateInvalid(
            f"legacy state members must be an object, got "
            f"{type(members).__name__}: {source}"
        )
    for key, member in members.items():
        if not isinstance(key, str) or not key.strip():
            raise LegacyStateInvalid(f"legacy member key must be a non-blank string: {source}")
        if not isinstance(member, dict):
            raise LegacyStateInvalid(f"legacy member {key!r} is not an object: {source}")
        name = member.get("name", key)
        if not isinstance(name, str) or not name.strip():
            raise LegacyStateInvalid(f"legacy member {key!r} has no usable name: {source}")
        if name != key:
            raise LegacyStateInvalid(
                f"legacy member key {key!r} disagrees with member.name {name!r}: {source}"
            )
    session = payload.get("herdr_session")
    if session is not None and not isinstance(session, dict):
        raise LegacyStateInvalid(
            f"legacy herdr_session must be an object, got "
            f"{type(session).__name__}: {source}"
        )
    return payload


def hash_legacy_state(json_path: str | os.PathLike[str]) -> str:
    """SHA-256 of the source bytes (the §8.3 stability fingerprint)."""

    path = _guarded_existing_file(json_path, what="legacy state path")
    with path.open("rb") as handle:
        return _sha256_bytes(handle.read())


@dataclass(frozen=True)
class SourceFingerprint:
    """Content + identity facts about the legacy file (8.3 steps 1/3/6)."""

    path: str
    size: int
    sha256: str
    inode: int
    device: int
    mtime_ns: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "inode": self.inode,
            "device": self.device,
            "mtime_ns": self.mtime_ns,
        }

    def same_stability_window(self, other: SourceFingerprint) -> bool:
        """True when inode/mtime/size/hash all match (quiescence evidence)."""

        return (
            self.inode == other.inode
            and self.device == other.device
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
            and self.sha256 == other.sha256
        )

    def content_matches(self, other: SourceFingerprint) -> bool:
        return self.sha256 == other.sha256


def fingerprint_source(json_path: str | os.PathLike[str]) -> SourceFingerprint:
    path = _guarded_existing_file(json_path, what="legacy state path")
    with path.open("rb") as handle:
        raw = handle.read()
    info = path.stat()
    return SourceFingerprint(
        path=str(path),
        size=len(raw),
        sha256=_sha256_bytes(raw),
        inode=int(info.st_ino),
        device=int(info.st_dev),
        mtime_ns=int(info.st_mtime_ns),
    )


@dataclass(frozen=True)
class LegacySnapshot:
    """An immutable, byte-faithful view of a legacy state file.

    ``backup_path`` is a byte-identical copy published into an explicit
    artifact directory (never next to the source).  ``source_unchanged()``
    re-reads and compares the fingerprint, which is how a caller proves the
    original JSON bytes were not overwritten.
    """

    fingerprint: SourceFingerprint
    state: dict[str, Any]
    raw_bytes: bytes
    backup_path: str | None = None
    captured_at: str = ""

    @property
    def sha256(self) -> str:
        return self.fingerprint.sha256

    @property
    def team_id(self) -> str:
        value = self.state.get("team_id")
        return str(value)

    @property
    def members(self) -> dict[str, dict[str, Any]]:
        raw = self.state.get("members") or {}
        return {str(k): dict(v) for k, v in raw.items()}

    @property
    def recorded_session(self) -> dict[str, Any] | None:
        value = self.state.get("herdr_session")
        if value is None:
            return None
        return {str(k): v for k, v in value.items()}

    def source_unchanged(self) -> bool:
        try:
            current = fingerprint_source(self.fingerprint.path)
        except (OSError, ValidationError):
            return False
        return self.fingerprint.same_stability_window(current)

    def backup_is_byte_identical(self) -> bool:
        if self.backup_path is None:
            return False
        with Path(self.backup_path).open("rb") as handle:
            return _sha256_bytes(handle.read()) == self.sha256


def capture_snapshot(
    json_path: str | os.PathLike[str],
    *,
    artifact_dir: str | os.PathLike[str] | None = None,
    clock: Clock | None = None,
) -> LegacySnapshot:
    """Read-only snapshot (plus optional byte-identical backup artifact)."""

    path = _guarded_existing_file(json_path, what="legacy state path")
    with path.open("rb") as handle:
        raw = handle.read()
    info = path.stat()
    state = validate_legacy_state(raw, source=str(path))
    backup: str | None = None
    if artifact_dir is not None:
        directory = _ensure_directory(artifact_dir, what="artifact directory")
        if directory == path.parent:
            raise ValidationError(
                "artifact directory must not be the source directory: rehearsal "
                "backups never live next to the legacy state file"
            )
        target = directory / f"legacy-snapshot-{_sha256_bytes(raw)[:16]}.json"
        backup = str(_publish_bytes(target, raw))
    fingerprint = SourceFingerprint(
        path=str(path),
        size=len(raw),
        sha256=_sha256_bytes(raw),
        inode=int(info.st_ino),
        device=int(info.st_dev),
        mtime_ns=int(info.st_mtime_ns),
    )
    return LegacySnapshot(
        fingerprint=fingerprint,
        state=state,
        raw_bytes=raw,
        backup_path=backup,
        captured_at=_utc_iso(clock),
    )


def _publish_bytes(target: Path, payload: bytes) -> Path:
    """temp + fsync + rename + dir fsync: a crash never leaves a partial file."""

    directory = target.parent
    temp = directory / f".{target.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(target))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(str(temp))
        raise
    directory_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return target


# ---------------------------------------------------------------------------
# Session identity (read-only stat, never a Herdr call)
# ---------------------------------------------------------------------------


def session_identity(socket_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Reproduce the legacy identity tuple without contacting Herdr."""

    path = Path(socket_path).expanduser()
    identity: dict[str, Any] = {"socket_path": str(path.resolve(strict=False))}
    try:
        info = path.stat()
    except OSError:
        identity["socket_device"] = None
        identity["socket_inode"] = None
    else:
        identity["socket_device"] = int(info.st_dev)
        identity["socket_inode"] = int(info.st_ino)
    return identity


def sessions_match(
    recorded: Mapping[str, Any] | None, current: Mapping[str, Any] | None
) -> bool:
    """Conservative Session match: unknown on either side is NOT a match."""

    if not recorded or not current:
        return False
    keys = ("socket_path", "socket_device", "socket_inode")
    for key in keys:
        left = recorded.get(key)
        right = current.get(key)
        if left is None or right is None or left != right:
            return False
    return True


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportRecord:
    """One legacy member mapped into Task/Attempt/Session/Agent placeholders."""

    legacy_name: str
    agent_id: str
    task_id: str
    attempt_id: str
    session_id: str
    task_status: str
    attempt_status: str
    session_state: str
    task_kind: str
    read_only: bool
    ownership: tuple[str, ...]
    cross_session: bool
    legacy_phase: str | None
    flags: tuple[str, ...]
    legacy_payload: dict[str, Any]
    mapping: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "legacy_name": self.legacy_name,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "session_id": self.session_id,
            "task_status": self.task_status,
            "attempt_status": self.attempt_status,
            "session_state": self.session_state,
            "task_kind": self.task_kind,
            "read_only": self.read_only,
            "ownership": list(self.ownership),
            "cross_session": self.cross_session,
            "legacy_phase": self.legacy_phase,
            "flags": list(self.flags),
            "adopt": False,
            "resume": False,
            "resend_prompt": False,
        }


@dataclass(frozen=True)
class ImportPlan:
    """Everything a rehearsal will write, computed before any DB is touched."""

    generation: str
    snapshot: LegacySnapshot
    project_id: str
    project_name: str
    repo_path: str
    cross_session: bool
    records: tuple[ImportRecord, ...]
    unknowns: tuple[str, ...]
    mapper_version: str = MAPPER_VERSION
    current_session: dict[str, Any] | None = None
    prohibited_actions: tuple[str, ...] = ()

    def core(self) -> dict[str, Any]:
        return {
            "mapper_version": self.mapper_version,
            "project": {"id": self.project_id, "name": self.project_name,
                       "repo_path": self.repo_path},
            "cross_session": self.cross_session,
            "current_session": self.current_session,
            "records": [record.as_dict() for record in self.records],
            "unknowns": list(self.unknowns),
        }

    @property
    def plan_sha256(self) -> str:
        return _sha256_bytes(_canonical_json(self.core()).encode("utf-8"))

    @property
    def source_sha256(self) -> str:
        return self.snapshot.sha256


def new_cutover_generation(*, clock: Clock | None = None) -> str:
    """A fresh, single-use generation token (8.3 step 1: never reused)."""

    stamp = _utc_iso(clock).replace(":", "").replace("+", "_")
    return f"cut_{stamp}_{uuid.uuid4().hex[:12]}"


def map_snapshot(
    snapshot: LegacySnapshot,
    *,
    current_session: Mapping[str, Any] | None = None,
    generation: str | None = None,
) -> ImportPlan:
    """Deterministically map a v1 snapshot onto v3 domain rows.

    ``current_session`` is the rehearsal's own Herdr identity tuple.  When the
    recorded session does not match it (or cannot be established) the plan is
    cross-session: Sessions import as ``unknown`` and the plan carries the
    explicit prohibition on adopting a pane, resuming an attempt, or resending
    a prompt (8.2-2).
    """

    state = snapshot.state
    team_id = snapshot.team_id
    recorded_session = state.get("herdr_session")
    cross_session = not sessions_match(recorded_session, current_session)
    repo_path = str(state["project_root"])
    unknowns: list[str] = []
    records: list[ImportRecord] = []
    for name in sorted(snapshot.members):
        member = snapshot.members[name]
        phase = member.get("phase")
        flags: list[str] = []
        if not isinstance(phase, str) or not phase.strip():
            task_status, attempt_status = "needs_human", "unknown"
            flags.append("missing_phase")
            unknowns.append(f"{name}: missing phase")
        elif phase in _LEGACY_PHASE_MAP:
            task_status, attempt_status = _LEGACY_PHASE_MAP[phase]
        else:
            task_status, attempt_status = "needs_human", "unknown"
            flags.append("unknown_phase")
            unknowns.append(f"{name}: unknown phase {phase!r}")
        if phase in AMBIGUOUS_LEGACY_PHASES:
            flags.append("ambiguous_execution_never_auto_resend")
        raw_ownership = member.get("ownership")
        if isinstance(raw_ownership, list):
            ownership = tuple(str(item) for item in raw_ownership)
        else:
            ownership = ()
            if raw_ownership is not None:
                flags.append("unusable_ownership")
                unknowns.append(f"{name}: ownership is not a list")
        read_only = bool(member.get("read_only", False))
        if not read_only and not ownership and task_status != "needs_human":
            flags.append("editing_member_without_ownership")
        kind = member.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            flags.append("unknown_harness")
            kind = None
            unknowns.append(f"{name}: unknown harness kind")
        task_kind = "scout" if read_only else "ship"
        live = member.get("live_state")
        session_state = _LIVE_STATE_MAP.get(str(live)) if live is not None else None
        if session_state is None:
            if live is not None and str(live) != "unknown":
                flags.append(f"unmapped_live_state:{live}")
            session_state = "unknown"
        if cross_session:
            session_state = "unknown"
            flags.append("cross_session_quarantine")
        records.append(
            ImportRecord(
                legacy_name=name,
                agent_id=legacy_entity_id("agent", team_id, f"{name}:{kind or '?'}"),
                task_id=legacy_entity_id("task", team_id, name),
                attempt_id=legacy_entity_id("attempt", team_id, name),
                session_id=legacy_entity_id("session", team_id, name),
                task_status=task_status,
                attempt_status=attempt_status,
                session_state=session_state,
                task_kind=task_kind,
                read_only=read_only,
                ownership=ownership,
                cross_session=cross_session,
                legacy_phase=phase if isinstance(phase, str) else None,
                flags=tuple(flags),
                legacy_payload=json.loads(json.dumps(member, ensure_ascii=False)),
                mapping={
                    "legacy.name": name,
                    "legacy.role": member.get("role"),
                    "legacy.kind": kind,
                    "legacy.task": member.get("task"),
                    "legacy.responsibility": member.get("responsibility"),
                    "legacy.ownership": list(ownership),
                    "legacy.read_only": read_only,
                    "legacy.phase": phase,
                    "legacy.live_state": live,
                    "legacy.pane_id": member.get("pane_id"),
                    "legacy.terminal_id": member.get("terminal_id"),
                    "legacy.agent_session": member.get("agent_session"),
                    "legacy.created_at": member.get("created_at"),
                    "legacy.updated_at": member.get("updated_at"),
                    "domain.task.status": task_status,
                    "domain.attempt.status": attempt_status,
                    "domain.session.state": session_state,
                },
            )
        )
    prohibited = PROHIBITED_CROSS_SESSION_ACTIONS if cross_session else ()
    return ImportPlan(
        generation=generation or new_cutover_generation(),
        snapshot=snapshot,
        project_id=legacy_entity_id("project", team_id, repo_path),
        project_name=f"legacy-{team_id[:8]}",
        repo_path=repo_path,
        cross_session=cross_session,
        records=tuple(records),
        unknowns=tuple(unknowns),
        current_session=dict(current_session) if current_session is not None else None,
        prohibited_actions=prohibited,
    )


def plan_rehearsal(
    *,
    json_path: str | os.PathLike[str],
    current_session: Mapping[str, Any] | None = None,
    generation: str | None = None,
    artifact_dir: str | os.PathLike[str] | None = None,
    clock: Clock | None = None,
    fault: FaultHook | None = None,
) -> ImportPlan:
    """Read-only plan: snapshot + hash + mapping.  Writes no database at all."""

    if fault is not None:
        fault(STAGE_PLAN)
    snapshot = capture_snapshot(json_path, artifact_dir=artifact_dir, clock=clock)
    if fault is not None:
        fault(STAGE_SNAPSHOT)
    plan = map_snapshot(snapshot, current_session=current_session, generation=generation)
    if fault is not None:
        fault(STAGE_HASH_CHECK)
    # Prove the source we mapped is still the source on disk (byte stability).
    if plan.source_sha256 != hash_legacy_state(json_path):
        raise CutoverRefused(
            "legacy source changed between snapshot and hash check; plan discarded",
            reasons=["SOURCE_CHANGED_DURING_PLANNING"],
            generation=plan.generation,
        )
    return plan


# ---------------------------------------------------------------------------
# Rehearsal database
# ---------------------------------------------------------------------------

_REHEARSAL_TABLE = """
CREATE TABLE IF NOT EXISTS legacy_migration_rehearsals (
  generation TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  source_inode INTEGER NOT NULL,
  source_device INTEGER NOT NULL,
  source_size INTEGER NOT NULL,
  source_mtime_ns INTEGER NOT NULL,
  source_sha256 TEXT NOT NULL,
  plan_sha256 TEXT NOT NULL,
  mapper_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('imported', 'refused')),
  cross_session INTEGER NOT NULL DEFAULT 0,
  dispatch_paused INTEGER NOT NULL DEFAULT 0,
  plan_json TEXT NOT NULL,
  refusal_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)"""

_RECORDS_TABLE = """
CREATE TABLE IF NOT EXISTS legacy_migration_records (
  id TEXT PRIMARY KEY,
  generation TEXT NOT NULL,
  legacy_team_id TEXT NOT NULL,
  legacy_name TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  task_status TEXT NOT NULL,
  attempt_status TEXT NOT NULL,
  session_state TEXT NOT NULL,
  cross_session INTEGER NOT NULL DEFAULT 0,
  adopt INTEGER NOT NULL DEFAULT 0 CHECK (adopt = 0),
  resume INTEGER NOT NULL DEFAULT 0 CHECK (resume = 0),
  resend_prompt INTEGER NOT NULL DEFAULT 0 CHECK (resend_prompt = 0),
  flags_json TEXT NOT NULL,
  legacy_payload_json TEXT NOT NULL,
  mapping_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (generation, legacy_name)
)"""

_AUTHORITY_TABLE = """
CREATE TABLE IF NOT EXISTS legacy_cutover_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  authoritative TEXT NOT NULL CHECK (authoritative IN ('json', 'sqlite')),
  generation TEXT,
  source_sha256 TEXT,
  updated_at TEXT NOT NULL
)"""


def open_rehearsal_database(db_path: str | os.PathLike[str]) -> storage.TeamCraftConnection:
    """Open (creating if needed) a disposable rehearsal DB at head schema."""

    path = _rehearsal_db_path(db_path)
    storage.migrate_to_head(path)
    conn = storage.connect(path)
    with storage.transaction(conn):
        conn.execute(_REHEARSAL_TABLE)
        conn.execute(_RECORDS_TABLE)
        conn.execute(_AUTHORITY_TABLE)
        if conn.execute("SELECT 1 FROM legacy_cutover_state WHERE id = 1").fetchone() is None:
            conn.execute(
                "INSERT INTO legacy_cutover_state (id, authoritative, generation,"
                " source_sha256, updated_at) VALUES (1, ?, NULL, NULL, ?)",
                (MODE_JSON, _utc_iso(None)),
            )
    return conn


def _rehearsal_db_path(db_path: str | os.PathLike[str]) -> Path:
    path = require_absolute_path(db_path, what="rehearsal database path")
    assert_not_runtime_path(path, what="rehearsal database path")
    return path


def _close(conn: storage.TeamCraftConnection) -> None:
    with contextlib.suppress(Exception):
        conn.close()


def read_authority_row(conn: storage.TeamCraftConnection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT authoritative, generation, source_sha256, updated_at"
        " FROM legacy_cutover_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "authoritative": str(row[0]),
        "generation": None if row[1] is None else str(row[1]),
        "source_sha256": None if row[2] is None else str(row[2]),
        "updated_at": str(row[3]),
    }


def read_rehearsal_rows(conn: storage.TeamCraftConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT generation, source_path, source_sha256, plan_sha256, status,"
        " cross_session, dispatch_paused, refusal_json, created_at"
        " FROM legacy_migration_rehearsals ORDER BY created_at, generation"
    ).fetchall()
    return [
        {
            "generation": str(row[0]),
            "source_path": str(row[1]),
            "source_sha256": str(row[2]),
            "plan_sha256": str(row[3]),
            "status": str(row[4]),
            "cross_session": bool(row[5]),
            "dispatch_paused": bool(row[6]),
            "refusal_json": None if row[7] is None else str(row[7]),
            "created_at": str(row[8]),
        }
        for row in rows
    ]


def find_imported_rehearsal(
    conn: storage.TeamCraftConnection, *, source_sha256: str, plan_sha256: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT generation, status, plan_sha256, source_sha256"
        " FROM legacy_migration_rehearsals"
        " WHERE source_sha256 = ? AND plan_sha256 = ? AND status = 'imported'",
        (source_sha256, plan_sha256),
    ).fetchone()
    if row is None:
        return None
    return {"generation": str(row[0]), "status": str(row[1])}


@dataclass(frozen=True)
class RehearsalReport:
    generation: str
    source_sha256: str
    plan_sha256: str
    record_count: int
    cross_session: bool
    authoritative: str
    db_is_shadow: bool
    reused: bool
    source_bytes_preserved: bool
    external_side_effects: tuple[str, ...] = ()
    prohibited_actions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "source_sha256": self.source_sha256,
            "plan_sha256": self.plan_sha256,
            "record_count": self.record_count,
            "cross_session": self.cross_session,
            "authoritative": self.authoritative,
            "db_is_shadow": self.db_is_shadow,
            "reused": self.reused,
            "source_bytes_preserved": self.source_bytes_preserved,
            "external_side_effects": list(self.external_side_effects),
            "prohibited_actions": list(self.prohibited_actions),
        }


def _ensure_project_row(conn: storage.TeamCraftConnection, plan: ImportPlan, now: str) -> str:
    row = conn.execute(
        "SELECT id FROM projects WHERE repo_path = ?", (plan.repo_path,)
    ).fetchone()
    if row is not None:
        return str(row[0])
    conn.execute(
        "INSERT INTO projects (id, name, repo_path, config_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (plan.project_id, plan.project_name, plan.repo_path,
         _canonical_json({"imported_from": "legacy_v1", "mapper_version": MAPPER_VERSION}),
         now),
    )
    return plan.project_id


def _insert_domain_rows(
    conn: storage.TeamCraftConnection, plan: ImportPlan, now: str
) -> int:
    """Insert the imported Task/Attempt/Session/Agent rows for one plan.

    Historical rows are written directly (status values are validated against
    the domain enums): an import is a recorded past fact, not a live state
    transition, so pushing it through the dispatch/verification event chain
    would invent progress the v1 file never proved.  The DB's own constraints
    still apply, including ``ux_attempts_single_occupying``.
    """

    project_id = _ensure_project_row(conn, plan, now)
    count = 0
    for record in plan.records:
        if record.task_status not in domain.TASK_STATUSES:
            raise LegacyStateInvalid(f"unmapped task status {record.task_status!r}")
        if record.attempt_status not in domain.ATTEMPT_STATUSES:
            raise LegacyStateInvalid(f"unmapped attempt status {record.attempt_status!r}")
        if record.session_state not in domain.SESSION_STATES:
            raise LegacyStateInvalid(f"unmapped session state {record.session_state!r}")
        conn.execute(
            "INSERT INTO agents (id, role, harness, model, provider, config_json)"
            " VALUES (?, ?, ?, NULL, NULL, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (record.agent_id, str(record.mapping.get("legacy.role") or "custom"),
             str(record.mapping.get("legacy.kind") or "unknown"),
             _canonical_json({"imported_from": "legacy_v1",
                            "responsibility": record.mapping.get("legacy.responsibility")})),
        )
        conn.execute(
            "INSERT INTO tasks (id, project_id, title, description, acceptance_json,"
            " kind, priority, status, risk, ownership_json, version, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (record.task_id, project_id, f"[legacy] {record.legacy_name}",
             str(record.mapping.get("legacy.task") or ""),
             _canonical_json([]), record.task_kind, domain.DEFAULT_PRIORITY,
             record.task_status, "normal", _canonical_json(list(record.ownership)),
             now, now),
        )
        conn.execute(
            "INSERT INTO attempts (id, task_id, agent_id, attempt_no, status,"
            " worktree_path, branch, lease_owner, lease_expires_at, started_at,"
            " finished_at, commit_sha, report_ref, result_json)"
            " VALUES (?, ?, ?, 1, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (record.attempt_id, record.task_id, record.agent_id,
             record.attempt_status,
             _canonical_json({"imported_from": "legacy_v1",
                            "legacy_phase": record.legacy_phase,
                            "flags": list(record.flags)})),
        )
        conn.execute(
            "INSERT INTO sessions (id, attempt_id, backend, native_session_id,"
            " pane_id, state, last_observed_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (record.session_id, record.attempt_id,
             str(record.mapping.get("legacy.kind") or "unknown"),
             record.mapping.get("legacy.agent_session"),
             record.mapping.get("legacy.pane_id"),
             record.session_state, now),
        )
        conn.execute(
            "INSERT INTO legacy_migration_records (id, generation, legacy_team_id,"
            " legacy_name, agent_id, task_id, attempt_id, session_id, task_status,"
            " attempt_status, session_state, cross_session, adopt, resume,"
            " resend_prompt, flags_json, legacy_payload_json, mapping_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?)"
            " ON CONFLICT(generation, legacy_name) DO NOTHING",
            (legacy_entity_id("record", plan.snapshot.team_id,
                             f"{plan.generation}:{record.legacy_name}"),
             plan.generation, plan.snapshot.team_id, record.legacy_name,
             record.agent_id, record.task_id, record.attempt_id, record.session_id,
             record.task_status, record.attempt_status, record.session_state,
             1 if record.cross_session else 0,
             _canonical_json(list(record.flags)),
             _canonical_json(record.legacy_payload),
             _canonical_json(record.mapping), now),
        )
        count += 1
    return count


@dataclass(frozen=True)
class _ImportOutcome:
    reused: bool
    record_count: int


def _import_plan(
    conn: storage.TeamCraftConnection, plan: ImportPlan, *, clock: Clock | None,
    fault: FaultHook | None = None,
) -> _ImportOutcome:
    """One rehearsal import: domain rows + rehearsal row + event, one COMMIT."""

    now = _utc_iso(clock)
    existing = find_imported_rehearsal(
        conn, source_sha256=plan.source_sha256, plan_sha256=plan.plan_sha256
    )
    if existing is not None:
        return _ImportOutcome(reused=True, record_count=len(plan.records))
    if fault is not None:
        fault(STAGE_IMPORT_BEGIN)
    with storage.transaction(conn):
        project_id = _ensure_project_row(conn, plan, now)
        count = _insert_domain_rows(conn, plan, now)
        conn.execute(
            "INSERT INTO legacy_migration_rehearsals (generation, source_path,"
            " source_inode, source_device, source_size, source_mtime_ns,"
            " source_sha256, plan_sha256, mapper_version, status, cross_session,"
            " dispatch_paused, plan_json, refusal_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?, 0, ?, NULL, ?, ?)"
            " ON CONFLICT(generation) DO UPDATE SET status = 'imported',"
            " updated_at = excluded.updated_at",
            (plan.generation, plan.snapshot.fingerprint.path,
             plan.snapshot.fingerprint.inode, plan.snapshot.fingerprint.device,
             plan.snapshot.fingerprint.size, plan.snapshot.fingerprint.mtime_ns,
             plan.source_sha256, plan.plan_sha256, MAPPER_VERSION,
             1 if plan.cross_session else 0,
             _canonical_json(plan.core()), now, now),
        )
        core_events.append_event(
            conn,
            "migration.rehearsal.imported",
            project_id=project_id,
            payload_json=_canonical_json({
                "generation": plan.generation,
                "source_sha256": plan.source_sha256,
                "plan_sha256": plan.plan_sha256,
                "record_count": count,
                "cross_session": plan.cross_session,
                "mapper_version": MAPPER_VERSION,
                "authoritative": MODE_JSON,
                "shadow_only": True,
            }),
            clock=clock,
        )
        if fault is not None:
            fault(STAGE_IMPORT_ROWS)
        if fault is not None:
            fault(STAGE_BEFORE_COMMIT)
    if fault is not None:
        fault(STAGE_AFTER_IMPORT_COMMIT)
    return _ImportOutcome(reused=False, record_count=count)


def _record_refusal(
    conn: storage.TeamCraftConnection, plan: ImportPlan, reasons: Sequence[str],
    *, clock: Clock | None,
) -> None:
    now = _utc_iso(clock)
    with storage.transaction(conn):
        conn.execute(
            "INSERT INTO legacy_migration_rehearsals (generation, source_path,"
            " source_inode, source_device, source_size, source_mtime_ns,"
            " source_sha256, plan_sha256, mapper_version, status, cross_session,"
            " dispatch_paused, plan_json, refusal_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'refused', ?, 0, ?, ?, ?, ?)"
            " ON CONFLICT(generation) DO UPDATE SET status = 'refused',"
            " refusal_json = excluded.refusal_json, updated_at = excluded.updated_at",
            (plan.generation, plan.snapshot.fingerprint.path,
             plan.snapshot.fingerprint.inode, plan.snapshot.fingerprint.device,
             plan.snapshot.fingerprint.size, plan.snapshot.fingerprint.mtime_ns,
             plan.source_sha256, plan.plan_sha256, MAPPER_VERSION,
             1 if plan.cross_session else 0,
             _canonical_json(plan.core()),
             _canonical_json({"reasons": list(reasons)}), now, now),
        )
        core_events.append_event(
            conn,
            "migration.cutover.refused",
            payload_json=_canonical_json({
                "generation": plan.generation,
                "reasons": list(reasons),
                "authoritative": MODE_JSON,
            }),
            clock=clock,
        )


def run_rehearsal(
    *,
    plan: ImportPlan,
    db_path: str | os.PathLike[str],
    clock: Clock | None = None,
    fault: FaultHook | None = None,
) -> RehearsalReport:
    """Import ``plan`` into a disposable shadow DB.  Never promotes the DB.

    The source JSON is re-hashed before and after the import; any change aborts
    with :class:`CutoverRefused` (reason ``SOURCE_UNSTABLE``) and the shadow
    database stays un-imported.  Repeating the same plan is idempotent: no new
    rows, no new events, identical report apart from ``reused``.
    """

    before = fingerprint_source(plan.snapshot.fingerprint.path)
    if not before.content_matches(plan.snapshot.fingerprint):
        conn = open_rehearsal_database(db_path)
        try:
            _record_refusal(conn, plan, ["SOURCE_STALE_AT_REHEARSAL"], clock=clock)
        finally:
            _close(conn)
        raise CutoverRefused(
            f"legacy source hash changed before rehearsal: {before.sha256} != "
            f"{plan.source_sha256}",
            reasons=["SOURCE_STALE_AT_REHEARSAL"],
            generation=plan.generation,
        )
    conn = open_rehearsal_database(db_path)
    try:
        outcome = _import_plan(conn, plan, clock=clock, fault=fault)
        authority = read_authority_row(conn)
        authoritative = (authority or {}).get("authoritative", MODE_JSON)
        after = fingerprint_source(plan.snapshot.fingerprint.path)
        if not after.content_matches(before):
            # Rows exist in the disposable shadow, but this generation is marked
            # refused so nothing downstream can treat it as a completed import.
            # The legacy JSON is still the only writer and we never wrote to it.
            now = _utc_iso(clock)
            with storage.transaction(conn):
                conn.execute(
                    "UPDATE legacy_migration_rehearsals SET status = 'refused',"
                    " refusal_json = ?, updated_at = ? WHERE generation = ?",
                    (_canonical_json({"reasons": ["SOURCE_UNSTABLE"]}), now, plan.generation),
                )
                core_events.append_event(
                    conn,
                    "migration.rehearsal.aborted",
                    payload_json=_canonical_json({
                        "generation": plan.generation,
                        "reasons": ["SOURCE_UNSTABLE"],
                        "authoritative": MODE_JSON,
                        "shadow_rows_kept_as_evidence": True,
                    }),
                    clock=clock,
                )
            raise CutoverRefused(
                "legacy source changed during the rehearsal import; this"
                " generation is marked refused and JSON stays the sole writer",
                reasons=["SOURCE_UNSTABLE"],
                generation=plan.generation,
            )
        if not outcome.reused:
            # A fresh import must have produced exactly one rehearsal row.
            matched = find_imported_rehearsal(
                conn, source_sha256=plan.source_sha256, plan_sha256=plan.plan_sha256
            )
            if matched is None:
                raise MigrationError("rehearsal import did not persist its audit row")
        report = RehearsalReport(
            generation=plan.generation,
            source_sha256=plan.source_sha256,
            plan_sha256=plan.plan_sha256,
            record_count=outcome.record_count,
            cross_session=plan.cross_session,
            authoritative=str(authoritative),
            db_is_shadow=str(authoritative) != MODE_DB,
            reused=outcome.reused,
            source_bytes_preserved=before.same_stability_window(after),
            prohibited_actions=plan.prohibited_actions,
        )
    finally:
        _close(conn)
    if report.authoritative == MODE_DB:  # pragma: no cover - defensive
        raise MigrationError("run_rehearsal must never leave a DB authoritative")
    return report


# ---------------------------------------------------------------------------
# Markers and the legacy write guard contract (8.3 step 2; M1-07 deploys it)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerState:
    mode: str
    generation: str
    source_sha256: str | None
    created_at: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "generation": self.generation,
            "source_sha256": self.source_sha256,
            "created_at": self.created_at,
            "path": self.path,
        }


def _marker_path(marker_path: str | os.PathLike[str]) -> Path:
    return _guarded_file_path(marker_path, what="cutover marker path")


def deploy_marker(
    marker_path: str | os.PathLike[str],
    *,
    mode: str,
    generation: str,
    source_sha256: str | None = None,
    clock: Clock | None = None,
) -> MarkerState:
    """Durably publish a freeze/cutover marker at an explicit path."""

    if mode not in MARKER_MODES:
        raise ValidationError(f"unknown marker mode: {mode!r}")
    path = _marker_path(marker_path)
    state = MarkerState(
        mode=mode,
        generation=generation,
        source_sha256=source_sha256,
        created_at=_utc_iso(clock),
        path=str(path),
    )
    document = _canonical_json({
        "protocol": MARKER_PROTOCOL,
        "document_version": MARKER_DOCUMENT_VERSION,
        "mode": state.mode,
        "generation": state.generation,
        "source_sha256": state.source_sha256,
        "created_at": state.created_at,
    }).encode("utf-8") + b"\n"
    _publish_bytes(path, document)
    return state


def read_marker(
    marker_path: str | os.PathLike[str]
) -> MarkerState | None:
    """Read a marker; a corrupt or unknown marker raises (guard fails closed)."""

    path = _marker_path(marker_path)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarkerCorrupt(f"cutover marker is unreadable: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise MarkerCorrupt(f"cutover marker is not an object: {path}")
    if document.get("protocol") != MARKER_PROTOCOL:
        raise MarkerCorrupt(f"cutover marker has an unknown protocol: {path}")
    if document.get("document_version") != MARKER_DOCUMENT_VERSION:
        raise MarkerCorrupt(f"cutover marker document version unsupported: {path}")
    mode = document.get("mode")
    if mode not in MARKER_MODES:
        raise MarkerCorrupt(f"cutover marker mode is unrecognized: {mode!r} ({path})")
    generation = document.get("generation")
    if not isinstance(generation, str) or not generation.strip():
        raise MarkerCorrupt(f"cutover marker lacks a generation: {path}")
    source_hash = document.get("source_sha256")
    return MarkerState(
        mode=str(mode),
        generation=str(generation),
        source_sha256=None if source_hash is None else str(source_hash),
        created_at=str(document.get("created_at") or ""),
        path=str(path),
    )


def remove_marker(marker_path: str | os.PathLike[str]) -> bool:
    path = _marker_path(marker_path)
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        return False
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


class LegacyStateLock:
    """The legacy ``state.lock`` capability (``fcntl.flock``).

    Same semantics as the legacy writer's lock: per open-file-description, so
    contention is real across processes *and* across threads that open their
    own descriptor.  Acquisition uses a non-blocking poll with a deadline: a
    lock we cannot take means a writer may still be live, which is exactly the
    condition that must refuse cutover rather than wait forever.
    """

    def __init__(
        self, lock_path: str | os.PathLike[str], *, timeout_seconds: float = 5.0
    ) -> None:
        self.path = _guarded_file_path(lock_path, what="legacy lock path")
        if self.path.parent.is_symlink():
            raise ValidationError(f"legacy lock parent must not be a symlink: {self.path.parent}")
        if timeout_seconds < 0:
            raise ValidationError("timeout_seconds must not be negative")
        self.timeout_seconds = timeout_seconds
        self._handle: Any = None
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def acquire(self) -> LegacyStateLock:
        if self._owned:
            return self
        directory = self.path.parent
        if not directory.is_dir():
            raise LockUnavailable(f"legacy lock directory does not exist: {directory}")
        handle = open(str(self.path), "a+b")  # noqa: SIM115 - managed ownership
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise LockUnavailable(
                        f"legacy state lock is held by another writer: {self.path}"
                    ) from None
                time.sleep(0.005)
            else:
                self._handle = handle
                self._owned = True
                return self

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                self._handle.close()
        self._handle = None
        self._owned = False

    def __enter__(self) -> LegacyStateLock:  # noqa: PYI034 - Self needs Python 3.11
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@dataclass(frozen=True)
class GuardDecision:
    allow: bool
    reason_code: str
    marker: MarkerState | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow,
            "reason_code": self.reason_code,
            "marker": self.marker.as_dict() if self.marker else None,
        }


def guard_legacy_write(
    *,
    marker_path: str | os.PathLike[str],
    lock: LegacyStateLock,
    expected_generation: str | None = None,
) -> GuardDecision:
    """The M1-07 guard contract, evaluated exactly as the review requires.

    A legacy mutation is allowed only when the caller already holds the legacy
    ``state.lock`` (so the check cannot be raced by a freeze placed between the
    check and the write) and the marker at ``marker_path`` says JSON is
    authoritative.  Missing marker == JSON authoritative (pre-cutover default).
    Everything else — no lock capability, frozen, DB authoritative, corrupt
    marker, generation mismatch — denies the write.
    """

    if not lock.owned:
        return GuardDecision(allow=False, reason_code="LOCK_NOT_HELD")
    try:
        marker = read_marker(marker_path)
    except MarkerCorrupt:
        return GuardDecision(allow=False, reason_code="MARKER_CORRUPT")
    if marker is None:
        return GuardDecision(allow=True, reason_code="ALLOW_NO_MARKER")
    if marker.mode == MARKER_FREEZE:
        return GuardDecision(allow=False, reason_code="FROZEN", marker=marker)
    if marker.mode == MARKER_DB_AUTHORITATIVE:
        if expected_generation is not None and marker.generation != expected_generation:
            return GuardDecision(allow=False, reason_code="GENERATION_MISMATCH", marker=marker)
        return GuardDecision(allow=False, reason_code="DB_AUTHORITATIVE", marker=marker)
    if expected_generation is not None and marker.generation != expected_generation:
        return GuardDecision(allow=False, reason_code="GENERATION_MISMATCH", marker=marker)
    return GuardDecision(allow=True, reason_code="ALLOW_JSON_WRITER", marker=marker)


# ---------------------------------------------------------------------------
# Quiescence proof + fence assessment (8.3 step 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuiescenceProof:
    stable: bool
    reasons: tuple[str, ...]
    baseline: dict[str, Any]
    final: dict[str, Any]
    observations: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "stable": self.stable,
            "reasons": list(self.reasons),
            "baseline": self.baseline,
            "final": self.final,
            "observations": [dict(item) for item in self.observations],
        }


def prove_quiescence(
    *,
    json_path: str | os.PathLike[str],
    baseline: SourceFingerprint,
    lock: LegacyStateLock,
    extra_observations: int = 1,
    observation_interval_seconds: float = 0.01,
    fault: FaultHook | None = None,
) -> QuiescenceProof:
    """Compare inode/mtime/size/hash across a quiet window while holding the lock.

    The caller must hold ``lock`` for the whole window: releasing it mid-proof
    would let a deployed-guard writer slip a mutation in, which is the race
    §8.3 step 2 closes.  A fingerprint change means an old writer is live: the
    proof comes back ``stable=False`` with ``LEGACY_WRITER_OBSERVED``.
    """

    if not lock.owned:
        return QuiescenceProof(
            stable=False,
            reasons=("LOCK_NOT_HELD",),
            baseline=baseline.as_dict(),
            final={},
        )
    if fault is not None:
        fault(STAGE_QUIESCENCE)
    reasons: list[str] = []
    observations: list[dict[str, Any]] = []
    current = fingerprint_source(json_path)
    observations.append({"at": "start", **current.as_dict()})
    if not current.content_matches(baseline):
        reasons.append("SOURCE_CHANGED_BEFORE_FREEZE")
    for _ in range(max(0, extra_observations)):
        time.sleep(observation_interval_seconds)
        sample = fingerprint_source(json_path)
        observations.append({"at": "sample", **sample.as_dict()})
        if not sample.same_stability_window(current):
            reasons.append("LEGACY_WRITER_OBSERVED")
        current = sample
    if (
        not current.same_stability_window(baseline)
        and "LEGACY_WRITER_OBSERVED" not in reasons
    ):
        reasons.append("LEGACY_WRITER_OBSERVED")
    return QuiescenceProof(
        stable=not reasons,
        reasons=tuple(reasons),
        baseline=baseline.as_dict(),
        final=current.as_dict(),
        observations=tuple(observations),
    )


#: How a known legacy entry behaves during rehearsal.
WRITER_GUARDED = "guarded"
WRITER_LOADED_UNAWARE = "loaded_old_code_unaware"
WRITER_UNKNOWN_PROCESS = "unknown_process"
WRITER_MODES: tuple[str, ...] = (WRITER_GUARDED, WRITER_LOADED_UNAWARE, WRITER_UNKNOWN_PROCESS)


@dataclass(frozen=True)
class FenceEvidence:
    """What the operator can actually prove about legacy writers.

    ``writer_modes`` lists every entry that can mutate the legacy JSON (e.g.
    ``teamctl launch/resume/complete/release``), each with ``guarded`` (checks
    the marker under the lock) or one of the unsafe modes.  Anything other than
    fully guarded + enumerated + quiescent means the old writer cannot be
    fenced, and cutover must be refused (8.3).
    """

    writer_modes: tuple[str, ...] = ()
    writers_enumerated: bool = False
    quiescent: bool = False
    notes: tuple[str, ...] = ()

    @property
    def can_fence(self) -> bool:
        return (
            self.writers_enumerated
            and self.quiescent
            and bool(self.writer_modes)
            and all(mode == WRITER_GUARDED for mode in self.writer_modes)
        )

    def reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.writers_enumerated:
            reasons.append("WRITERS_NOT_ENUMERATED")
        if not self.writer_modes:
            reasons.append("NO_KNOWN_WRITER_EVIDENCE")
        if any(mode == WRITER_LOADED_UNAWARE for mode in self.writer_modes):
            reasons.append("LOADED_OLD_WRITER_NOT_FENCEABLE")
        if any(mode == WRITER_UNKNOWN_PROCESS for mode in self.writer_modes):
            reasons.append("UNKNOWN_LEGACY_PROCESS")
        if not self.quiescent:
            reasons.append("QUIESCENCE_NOT_PROVEN")
        return tuple(reasons)

    def with_quiescence(self, quiescent: bool) -> FenceEvidence:
        """Copy with the quiescence verdict folded in (proof supplies it)."""

        return FenceEvidence(
            writer_modes=self.writer_modes,
            writers_enumerated=self.writers_enumerated,
            quiescent=quiescent,
            notes=self.notes,
        )


@dataclass(frozen=True)
class FenceAssessment:
    allow_cutover: bool
    reasons: tuple[str, ...]
    json_is_sole_writer: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    quiescence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow_cutover": self.allow_cutover,
            "reasons": list(self.reasons),
            "json_is_sole_writer": self.json_is_sole_writer,
            "evidence": dict(self.evidence),
            "quiescence": dict(self.quiescence),
        }


def evaluate_fence(
    evidence: FenceEvidence, proof: QuiescenceProof | None = None
) -> FenceAssessment:
    """Combine quiescence + writer evidence into a cutover gate decision."""

    reasons = list(evidence.reasons())
    if proof is not None and not proof.stable:
        for reason in proof.reasons:
            if reason not in reasons:
                reasons.append(reason)
    allow = evidence.can_fence and (proof is None or proof.stable)
    return FenceAssessment(
        allow_cutover=allow,
        reasons=tuple(reasons),
        json_is_sole_writer=not allow,
        evidence={
            "writer_modes": list(evidence.writer_modes),
            "writers_enumerated": evidence.writers_enumerated,
            "quiescent": evidence.quiescent,
            "notes": list(evidence.notes),
        },
        quiescence=proof.as_dict() if proof is not None else {},
    )


# ---------------------------------------------------------------------------
# Cutover rehearsal (8.3 steps 1-5) with fault injection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CutoverRehearsalReport:
    generation: str
    outcome: str
    authoritative: str
    marker_mode: str | None
    record_count: int
    source_sha256: str
    cutover_source_sha256: str | None
    reasons: tuple[str, ...]
    marker_lifted: bool
    dispatch_paused: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "outcome": self.outcome,
            "authoritative": self.authoritative,
            "marker_mode": self.marker_mode,
            "record_count": self.record_count,
            "source_sha256": self.source_sha256,
            "cutover_source_sha256": self.cutover_source_sha256,
            "reasons": list(self.reasons),
            "marker_lifted": self.marker_lifted,
            "dispatch_paused": self.dispatch_paused,
        }


def _publish_db_authority(
    conn: storage.TeamCraftConnection, plan: ImportPlan, *, clock: Clock | None
) -> bool:
    """Write the durable ``sqlite`` authority row + event (one COMMIT).

    Returns ``False`` when this exact generation is already promoted, so a
    retried cutover stays idempotent (no duplicate authority event).
    """

    now = _utc_iso(clock)
    with storage.transaction(conn):
        current = read_authority_row(conn)
        if current is None:
            raise MigrationError("cutover authority row missing; refusing to promote")
        if current["authoritative"] == MODE_DB:
            if current["generation"] not in (None, plan.generation):
                raise CutoverRefused(
                    "another generation already holds durable DB authority;"
                    " refusing a second promotion",
                    reasons=["ALREADY_CUTOVER_OTHER_GENERATION"],
                    generation=str(current["generation"]),
                    dispatch_paused=True,
                )
            if current["generation"] == plan.generation and current["source_sha256"] == plan.source_sha256:
                return False
        cursor = conn.execute(
            "UPDATE legacy_cutover_state SET authoritative = ?, generation = ?,"
            " source_sha256 = ?, updated_at = ? WHERE id = 1",
            (MODE_DB, plan.generation, plan.source_sha256, now),
        )
        if cursor.rowcount != 1:
            raise MigrationError("cutover authority row missing; refusing to promote")
        conn.execute(
            "UPDATE legacy_migration_rehearsals SET dispatch_paused = 0,"
            " updated_at = ? WHERE generation = ?",
            (now, plan.generation),
        )
        core_events.append_event(
            conn,
            "migration.cutover.committed",
            project_id=plan.project_id,
            payload_json=_canonical_json({
                "generation": plan.generation,
                "source_sha256": plan.source_sha256,
                "authoritative": MODE_DB,
            }),
            clock=clock,
        )
    return True


def _note_rehearsal_refusal(
    conn: storage.TeamCraftConnection, plan: ImportPlan, reasons: Sequence[str],
    *, clock: Clock | None,
) -> None:
    """Persist a refusal audit row + event (never silently dropped)."""

    _record_refusal(conn, plan, reasons, clock=clock)


def rehearse_cutover(
    *,
    plan: ImportPlan,
    db_path: str | os.PathLike[str],
    lock_path: str | os.PathLike[str],
    marker_path: str | os.PathLike[str],
    fence: FenceEvidence,
    lock_timeout_seconds: float = 5.0,
    clock: Clock | None = None,
    fault: FaultHook | None = None,
) -> CutoverRehearsalReport:
    """Rehearse the §8.3 sequence end to end against synthetic state.

    Ordering (identical to the production protocol, so the fault matrix is
    meaningful): prepare generation -> deploy freeze marker -> take the legacy
    ``state.lock`` -> prove quiescence -> import -> *still holding that same
    lock* re-check the source hash and marker generation -> commit
    ``sqlite`` authority -> publish the ``db_authoritative`` marker durably ->
    release the lock.

    Refusal paths always leave JSON authoritative, keep the DB a shadow, and
    lift the freeze marker this call deployed so the legacy writer resumes.
    """

    if fault is not None:
        fault(STAGE_HASH_CHECK)
    baseline = fingerprint_source(plan.snapshot.fingerprint.path)
    if not baseline.content_matches(plan.snapshot.fingerprint):
        conn = open_rehearsal_database(db_path)
        try:
            reasons = ("SOURCE_STALE_AT_CUTOVER",)
            _note_rehearsal_refusal(conn, plan, reasons, clock=clock)
        finally:
            _close(conn)
        raise CutoverRefused(
            "legacy source hash no longer matches the planned snapshot",
            reasons=["SOURCE_STALE_AT_CUTOVER"],
            generation=plan.generation,
        )
    # A rehearsal never re-cuts a state that is already switched: an existing
    # ``db_authoritative`` marker (or a DB that already claims authority) means
    # either this same generation already completed (idempotent no-op) or a
    # different generation did (refuse; never trample the marker).
    try:
        pre_existing = read_marker(marker_path)
    except MarkerCorrupt as exc:
        raise CutoverRefused(
            f"existing cutover marker is corrupt: {exc}",
            reasons=["MARKER_CORRUPT"],
            generation=plan.generation,
            dispatch_paused=True,
        ) from exc
    if Path(db_path).exists():
        prior = choose_authority(db_path=db_path, marker_path=marker_path)
        if prior.source == MODE_DB and pre_existing is not None:
            if pre_existing.generation == plan.generation:
                conn = open_rehearsal_database(db_path)
                try:
                    outcome = _import_plan(conn, plan, clock=clock, fault=None)
                    authority = read_authority_row(conn)
                finally:
                    _close(conn)
                return CutoverRehearsalReport(
                    generation=plan.generation,
                    outcome="already_cutover",
                    authoritative=str((authority or {}).get("authoritative", MODE_DB)),
                    marker_mode=MARKER_DB_AUTHORITATIVE,
                    record_count=outcome.record_count,
                    source_sha256=plan.source_sha256,
                    cutover_source_sha256=baseline.sha256,
                    reasons=("ALREADY_CUTOVER",),
                    marker_lifted=False,
                    dispatch_paused=False,
                )
            raise CutoverRefused(
                "this state was already cut over by another generation"
                f" ({pre_existing.generation}); refusing a second switch",
                reasons=["ALREADY_CUTOVER_OTHER_GENERATION"],
                generation=plan.generation,
                dispatch_paused=True,
            )
        if prior.reason_code == "AUTHORITY_DISAGREEMENT":
            raise CutoverRefused(
                "a previous cutover left DB and marker disagreeing; reconcile it"
                " (complete_interrupted_cutover or rollback) before a new attempt",
                reasons=["AUTHORITY_DISAGREEMENT"],
                generation=prior.generation or plan.generation,
                dispatch_paused=True,
            )
    # Step 2: deploy the freeze marker before anything else can be trusted.
    deploy_marker(
        marker_path,
        mode=MARKER_FREEZE,
        generation=plan.generation,
        source_sha256=plan.source_sha256,
        clock=clock,
    )
    if fault is not None:
        fault(STAGE_FREEZE_MARKER)
    lock = LegacyStateLock(lock_path, timeout_seconds=lock_timeout_seconds)
    try:
        lock.acquire()
    except LockUnavailable:
        remove_marker(marker_path)
        conn = open_rehearsal_database(db_path)
        try:
            _note_rehearsal_refusal(conn, plan, ["LEGACY_LOCK_UNAVAILABLE"], clock=clock)
        finally:
            _close(conn)
        raise CutoverRefused(
            f"cannot take the legacy state lock: {lock.path}; an old writer may"
            " still be live, so cutover is cancelled and JSON stays the writer",
            reasons=["LEGACY_LOCK_UNAVAILABLE"],
            generation=plan.generation,
            marker_lifted=True,
        )
    try:
        # The lock capability, once taken, must be given back on EVERY exit
        # path (success, refusal, or injected fault at either boundary).
        if fault is not None:
            fault(STAGE_LOCK_ACQUIRE)
        # A deployed guard must now deny legacy writes.
        decision = guard_legacy_write(
            marker_path=marker_path, lock=lock, expected_generation=plan.generation
        )
        if decision.allow:
            reasons = ("GUARD_CONTRACT_NOT_ENFORCED",)
            conn = open_rehearsal_database(db_path)
            try:
                _note_rehearsal_refusal(conn, plan, reasons, clock=clock)
            finally:
                _close(conn)
            remove_marker(marker_path)
            raise CutoverRefused(
                "legacy write guard did not block writes under the freeze marker;"
                " cutover cancelled",
                reasons=list(reasons),
                generation=plan.generation,
                dispatch_paused=False,
                marker_lifted=True,
            )
        proof = prove_quiescence(
            json_path=plan.snapshot.fingerprint.path,
            baseline=baseline,
            lock=lock,
            fault=fault,
        )
        assessment = evaluate_fence(fence.with_quiescence(proof.stable), proof)
        if not assessment.allow_cutover:
            reasons = assessment.reasons or ("FENCE_NOT_PROVEN",)
            conn = open_rehearsal_database(db_path)
            try:
                _note_rehearsal_refusal(conn, plan, reasons, clock=clock)
            finally:
                _close(conn)
            remove_marker(marker_path)
            raise CutoverRefused(
                "legacy writers cannot be reliably fenced: cutover cancelled, JSON"
                " remains the sole writer and SQLite stays a disposable shadow: "
                + ", ".join(reasons),
                reasons=list(reasons),
                generation=plan.generation,
                marker_lifted=True,
            )
        conn = open_rehearsal_database(db_path)
        try:
            outcome = _import_plan(conn, plan, clock=clock, fault=fault)
            # Step 5: final checks and promotion happen under the SAME lock.
            final_hash = hash_legacy_state(plan.snapshot.fingerprint.path)
            if final_hash != plan.source_sha256:
                reasons = ("SOURCE_CHANGED_BEFORE_COMMIT",)
                _note_rehearsal_refusal(conn, plan, reasons, clock=clock)
                remove_marker(marker_path)
                raise CutoverRefused(
                    "legacy JSON changed between the hash check and the final"
                    " verification; import rolled back, JSON stays authoritative",
                    reasons=list(reasons),
                    generation=plan.generation,
                    marker_lifted=True,
                )
            marker = read_marker(marker_path)
            if marker is None or marker.generation != plan.generation:
                reasons = ("MARKER_GENERATION_MISMATCH",)
                _note_rehearsal_refusal(conn, plan, reasons, clock=clock)
                raise CutoverRefused(
                    "freeze marker disappeared or its generation changed during the"
                    " import; cutover cancelled",
                    reasons=list(reasons),
                    generation=plan.generation,
                    dispatch_paused=marker is not None,
                    marker_lifted=marker is None,
                )
            promoted = _publish_db_authority(conn, plan, clock=clock)
            # Durable DB commit reached, marker still says ``freeze``: a crash in
            # this window is a split-brain and must be reported needs_human.
            if fault is not None:
                fault(STAGE_AFTER_COMMIT)
            if fault is not None:
                fault(STAGE_BEFORE_MARKER_PUBLISH)
            deploy_marker(
                marker_path,
                mode=MARKER_DB_AUTHORITATIVE,
                generation=plan.generation,
                source_sha256=plan.source_sha256,
                clock=clock,
            )
            if fault is not None:
                fault(STAGE_AFTER_MARKER_PUBLISH)
            authority = read_authority_row(conn)
            report = CutoverRehearsalReport(
                generation=plan.generation,
                outcome=(
                    "rehearsed_db_authoritative" if promoted else "already_cutover"
                ),
                authoritative=str((authority or {}).get("authoritative", MODE_DB)),
                marker_mode=MARKER_DB_AUTHORITATIVE,
                record_count=outcome.record_count,
                source_sha256=plan.source_sha256,
                cutover_source_sha256=final_hash,
                reasons=(),
                marker_lifted=False,
                dispatch_paused=False,
            )
        finally:
            _close(conn)
    finally:
        # The capability MUST go back on every exit path: a fault raised at
        # the release boundary must not strand the fd or keep the flock held
        # in this process (a real crash would let the OS drop it; an injected
        # exception is not process death, so we release explicitly).
        try:
            if fault is not None:
                fault(STAGE_RELEASE_LOCK)
        finally:
            lock.release()
    return report


# ---------------------------------------------------------------------------
# Restart authority choice (8.3 step 3/6, 8.2-14)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityDecision:
    source: str
    reason_code: str
    needs_human: bool
    generation: str | None
    dispatch_paused: bool
    json_writes_permitted: bool
    db_side_effects_permitted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reason_code": self.reason_code,
            "needs_human": self.needs_human,
            "generation": self.generation,
            "dispatch_paused": self.dispatch_paused,
            "json_writes_permitted": self.json_writes_permitted,
            "db_side_effects_permitted": self.db_side_effects_permitted,
        }


def choose_authority(
    *, db_path: str | os.PathLike[str], marker_path: str | os.PathLike[str]
) -> AuthorityDecision:
    """Pick the single authoritative generation after a crash or restart.

    Read-only on both sides.  DB authority requires BOTH the durable
    ``sqlite`` row and a matching ``db_authoritative`` marker: any other
    combination either keeps JSON as the writer or stops with
    ``needs_human`` (never guessing, never merging the two sides).
    """

    path = _rehearsal_db_path(db_path)
    if not path.exists():
        try:
            marker = read_marker(marker_path)
        except MarkerCorrupt:
            return AuthorityDecision(
                source=MODE_JSON, reason_code="MARKER_CORRUPT_NO_DB", needs_human=True,
                generation=None, dispatch_paused=True, json_writes_permitted=False,
                db_side_effects_permitted=False,
            )
        return AuthorityDecision(
            source=MODE_JSON,
            reason_code="NO_DATABASE",
            needs_human=marker is not None and marker.mode == MARKER_DB_AUTHORITATIVE,
            generation=marker.generation if marker else None,
            dispatch_paused=marker is not None and marker.mode != MARKER_JSON_AUTHORITATIVE,
            json_writes_permitted=marker is None or marker.mode == MARKER_JSON_AUTHORITATIVE,
            db_side_effects_permitted=False,
        )
    conn = storage.connect(path)
    try:
        authority = read_authority_row(conn)
    finally:
        _close(conn)
    try:
        marker = read_marker(marker_path)
    except MarkerCorrupt:
        return AuthorityDecision(
            source=MODE_JSON,
            reason_code="MARKER_CORRUPT",
            needs_human=True,
            generation=(authority or {}).get("generation"),
            dispatch_paused=True,
            json_writes_permitted=False,
            db_side_effects_permitted=False,
        )
    db_says_db = bool(authority) and authority["authoritative"] == MODE_DB
    marker_says_db = bool(marker) and marker.mode == MARKER_DB_AUTHORITATIVE
    db_generation = (authority or {}).get("generation")
    if db_says_db and marker_says_db and db_generation != marker.generation:
        # Both sides claim the DB but disagree about WHICH cutover: never pick
        # one silently.
        return AuthorityDecision(
            source="none",
            reason_code="AUTHORITY_GENERATION_MISMATCH",
            needs_human=True,
            generation=db_generation,
            dispatch_paused=True,
            json_writes_permitted=False,
            db_side_effects_permitted=False,
        )
    if db_says_db and marker_says_db and db_generation == marker.generation:
        return AuthorityDecision(
            source=MODE_DB,
            reason_code="DB_AND_MARKER_AGREE",
            needs_human=False,
            generation=db_generation,
            dispatch_paused=False,
            json_writes_permitted=False,
            db_side_effects_permitted=True,
        )
    if db_says_db != marker_says_db:
        # Exactly one side claims the DB is authoritative: split-brain.  Stop.
        return AuthorityDecision(
            source="none",
            reason_code="AUTHORITY_DISAGREEMENT",
            needs_human=True,
            generation=db_generation or (marker.generation if marker else None),
            dispatch_paused=True,
            json_writes_permitted=False,
            db_side_effects_permitted=False,
        )
    if marker is not None and marker.mode == MARKER_FREEZE:
        return AuthorityDecision(
            source=MODE_JSON,
            reason_code="REFUSED_OR_IN_FLIGHT_FREEZE",
            needs_human=False,
            generation=marker.generation,
            dispatch_paused=True,
            json_writes_permitted=False,
            db_side_effects_permitted=False,
        )
    return AuthorityDecision(
        source=MODE_JSON,
        reason_code="JSON_AUTHORITATIVE",
        needs_human=False,
        generation=db_generation,
        dispatch_paused=False,
        json_writes_permitted=True,
        db_side_effects_permitted=False,
    )


# ---------------------------------------------------------------------------
# Post-cutover watch + rollback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleWriterObservation:
    verdict: str
    recorded_sha256: str | None
    current_sha256: str
    dispatch_paused: bool
    requires_human_reconciliation: bool
    action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "recorded_sha256": self.recorded_sha256,
            "current_sha256": self.current_sha256,
            "dispatch_paused": self.dispatch_paused,
            "requires_human_reconciliation": self.requires_human_reconciliation,
            "action": self.action,
        }


def observe_post_cutover(
    *,
    db_path: str | os.PathLike[str],
    json_path: str | os.PathLike[str],
    marker_path: str | os.PathLike[str],
    lock: LegacyStateLock,
) -> StaleWriterObservation:
    """8.3 step 6: any unexplained JSON change after cutover pauses dispatch.

    The change is never merged into the DB and never re-adopted: the observation
    only freezes dispatch and records evidence for human reconciliation.
    """

    if not lock.owned:
        raise MigrationSafetyError("post-cutover observation requires the legacy lock")
    current = hash_legacy_state(json_path)
    if not _rehearsal_db_path(db_path).exists():
        return StaleWriterObservation(
            verdict="not_cutover",
            recorded_sha256=None,
            current_sha256=current,
            dispatch_paused=False,
            requires_human_reconciliation=False,
            action="no_rehearsal_database",
        )
    marker = read_marker(marker_path)
    authority = choose_authority(db_path=db_path, marker_path=marker_path)
    recorded = authority.generation
    conn = storage.connect(_rehearsal_db_path(db_path))
    try:
        row = read_authority_row(conn)
        recorded_hash = (row or {}).get("source_sha256")
    finally:
        _close(conn)
    if authority.source != MODE_DB:
        return StaleWriterObservation(
            verdict="not_cutover",
            recorded_sha256=recorded_hash,
            current_sha256=current,
            dispatch_paused=authority.dispatch_paused,
            requires_human_reconciliation=authority.needs_human,
            action="json_writes_expected",
        )
    if recorded_hash is not None and recorded_hash != current:
        conn = open_rehearsal_database(db_path)
        try:
            now = _utc_iso(None)
            with storage.transaction(conn):
                conn.execute(
                    "UPDATE legacy_migration_rehearsals SET dispatch_paused = 1,"
                    " updated_at = ? WHERE generation = ?",
                    (now, recorded),
                )
                core_events.append_event(
                    conn,
                    "migration.stale_writer_detected",
                    project_id=None,
                    payload_json=_canonical_json({
                        "generation": recorded,
                        "recorded_sha256": recorded_hash,
                        "current_sha256": current,
                        "dispatch_paused": True,
                        "requires_human_reconciliation": True,
                        "marker_mode": marker.mode if marker else None,
                        "json_merged_into_db": False,
                    }),
                )
        finally:
            _close(conn)
        return StaleWriterObservation(
            verdict="stale_writer",
            recorded_sha256=recorded_hash,
            current_sha256=current,
            dispatch_paused=True,
            requires_human_reconciliation=True,
            action="pause_dispatch_and_reconcile_manually",
        )
    return StaleWriterObservation(
        verdict="clean",
        recorded_sha256=recorded_hash,
        current_sha256=current,
        dispatch_paused=False,
        requires_human_reconciliation=False,
        action="none",
    )


@dataclass(frozen=True)
class MarkerCompletion:
    """Result of finishing an interrupted marker publication (8.3 step 5)."""

    completed: bool
    reason_code: str
    generation: str | None
    authority: AuthorityDecision

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "reason_code": self.reason_code,
            "generation": self.generation,
            "authority": self.authority.as_dict(),
        }


def complete_interrupted_cutover(
    *,
    db_path: str | os.PathLike[str],
    marker_path: str | os.PathLike[str],
    lock_path: str | os.PathLike[str],
    json_path: str | os.PathLike[str],
    clock: Clock | None = None,
    lock_timeout_seconds: float = 5.0,
) -> MarkerCompletion:
    """Re-publish a lost ``db_authoritative`` marker after a crash there.

    Legitimate ONLY when the DB commit is durable, the legacy lock can be
    taken, and the source hash still equals the hash recorded at commit time.
    Any other situation is left exactly as it is for a human: the marker is
    never published speculatively, and the legacy JSON is never rewritten.
    """

    decision = choose_authority(db_path=db_path, marker_path=marker_path)
    if decision.source == MODE_DB:
        return MarkerCompletion(True, "ALREADY_COMPLETE", decision.generation, decision)
    if decision.reason_code != "AUTHORITY_DISAGREEMENT":
        return MarkerCompletion(
            False, "NOT_AN_INTERRUPTED_CUTOVER", decision.generation, decision
        )
    lock = LegacyStateLock(lock_path, timeout_seconds=lock_timeout_seconds)
    try:
        lock.acquire()
    except LockUnavailable as exc:
        raise CutoverRefused(
            "cannot complete the interrupted cutover: the legacy lock is held,"
            " so a writer may be live and the marker must not be published",
            reasons=["LEGACY_LOCK_UNAVAILABLE"],
            generation=decision.generation,
            dispatch_paused=True,
        ) from exc
    try:
        conn = storage.connect(_rehearsal_db_path(db_path))
        try:
            row = read_authority_row(conn)
        finally:
            _close(conn)
        recorded_hash = (row or {}).get("source_sha256")
        generation = (row or {}).get("generation")
        if (row or {}).get("authoritative") != MODE_DB or generation is None:
            return MarkerCompletion(False, "DB_NOT_AUTHORITATIVE", decision.generation, decision)
        marker = read_marker(marker_path)
        if marker is not None and marker.generation != generation:
            return MarkerCompletion(False, "MARKER_GENERATION_MISMATCH", generation, decision)
        if recorded_hash is None:
            return MarkerCompletion(False, "NO_RECORDED_SOURCE_HASH", generation, decision)
        if hash_legacy_state(json_path) != recorded_hash:
            # The JSON moved on: publishing the marker would hide a stale write.
            return MarkerCompletion(False, "SOURCE_CHANGED_SINCE_COMMIT", generation, decision)
        deploy_marker(
            marker_path,
            mode=MARKER_DB_AUTHORITATIVE,
            generation=generation,
            source_sha256=recorded_hash,
            clock=clock,
        )
        conn = storage.connect(_rehearsal_db_path(db_path))
        try:
            core_events.append_event(
                conn,
                "migration.cutover.marker_completed",
                payload_json=_canonical_json({
                    "generation": generation,
                    "source_sha256": recorded_hash,
                    "authoritative": MODE_DB,
                    "json_rewritten": False,
                }),
                clock=clock,
            )
        finally:
            _close(conn)
        updated = choose_authority(db_path=db_path, marker_path=marker_path)
        return MarkerCompletion(True, "MARKER_REPUBLISHED", generation, updated)
    finally:
        lock.release()


@dataclass(frozen=True)
class RollbackResult:
    authoritative: str
    generation: str | None
    marker_mode: str
    dispatch_paused: bool
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "authoritative": self.authoritative,
            "generation": self.generation,
            "marker_mode": self.marker_mode,
            "dispatch_paused": self.dispatch_paused,
            "notes": list(self.notes),
        }


def rollback_to_json(
    *,
    db_path: str | os.PathLike[str],
    marker_path: str | os.PathLike[str],
    generation: str,
    clock: Clock | None = None,
) -> RollbackResult:
    """Roll a rehearsed cutover back to JSON authority (8.2-14).

    Nothing is written back into the legacy JSON: the shadow DB stays as it is
    (audit evidence), authority returns to JSON, and the marker records
    ``json_authoritative`` so the legacy entry point resumes writing while the
    DB takes no side effects.
    """

    authority = choose_authority(db_path=db_path, marker_path=marker_path)
    if authority.source != MODE_DB:
        raise RollbackBlocked(
            "refusing to roll back: the database is not the confirmed authority"
            f" (source={authority.source}, reason={authority.reason_code}); a"
            " split-brain or in-flight freeze needs human reconciliation first"
        )
    if authority.generation != generation:
        raise RollbackBlocked(
            f"rollback generation {generation!r} does not match the committed"
            f" cutover generation {authority.generation!r}"
        )
    conn = storage.connect(_rehearsal_db_path(db_path))
    try:
        now = _utc_iso(clock)
        with storage.transaction(conn):
            conn.execute(
                "UPDATE legacy_cutover_state SET authoritative = ?, generation = ?,"
                " source_sha256 = NULL, updated_at = ? WHERE id = 1",
                (MODE_JSON, generation, now),
            )
            core_events.append_event(
                conn,
                "migration.rollback.to_json",
                payload_json=_canonical_json({
                    "generation": generation,
                    "authoritative": MODE_JSON,
                    "json_rewritten": False,
                    "shadow_db_retained_as_evidence": True,
                }),
                clock=clock,
            )
    finally:
        _close(conn)
    deploy_marker(
        marker_path,
        mode=MARKER_JSON_AUTHORITATIVE,
        generation=generation,
        clock=clock,
    )
    return RollbackResult(
        authoritative=MODE_JSON,
        generation=generation,
        marker_mode=MARKER_JSON_AUTHORITATIVE,
        dispatch_paused=False,
        notes=("legacy_json_resumed_as_sole_writer", "shadow_db_retained_as_evidence"),
    )


__all__ = [
    "AMBIGUOUS_LEGACY_PHASES",
    "CUTOVER_STAGES",
    "LEGACY_ID_NAMESPACE",
    "LEGACY_LOCK_FILENAME",
    "LEGACY_STATE_FILENAME",
    "LEGACY_STATE_VERSION",
    "MAPPER_VERSION",
    "MARKER_DB_AUTHORITATIVE",
    "MARKER_FREEZE",
    "MARKER_JSON_AUTHORITATIVE",
    "MARKER_MODES",
    "MARKER_PROTOCOL",
    "MODE_DB",
    "MODE_JSON",
    "PRODUCTION_DATABASE_FILENAME",
    "PROHIBITED_CROSS_SESSION_ACTIONS",
    "RUNTIME_DIRECTORY_NAMES",
    "STAGE_AFTER_COMMIT",
    "STAGE_AFTER_IMPORT_COMMIT",
    "STAGE_AFTER_MARKER_PUBLISH",
    "STAGE_BEFORE_COMMIT",
    "STAGE_BEFORE_MARKER_PUBLISH",
    "STAGE_FREEZE_MARKER",
    "STAGE_HASH_CHECK",
    "STAGE_IMPORT_BEGIN",
    "STAGE_IMPORT_ROWS",
    "STAGE_LOCK_ACQUIRE",
    "STAGE_PLAN",
    "STAGE_QUIESCENCE",
    "STAGE_RELEASE_LOCK",
    "STAGE_SNAPSHOT",
    "WRITER_GUARDED",
    "WRITER_LOADED_UNAWARE",
    "WRITER_MODES",
    "WRITER_UNKNOWN_PROCESS",
    "AuthorityDecision",
    "CutoverRefused",
    "CutoverRehearsalReport",
    "FenceAssessment",
    "FenceEvidence",
    "GuardDecision",
    "ImportPlan",
    "ImportRecord",
    "LegacySnapshot",
    "LegacyStateInvalid",
    "LegacyStateLock",
    "LockUnavailable",
    "MarkerCompletion",
    "MarkerCorrupt",
    "MarkerState",
    "MigrationSafetyError",
    "ProductionStateRefusal",
    "QuiescenceProof",
    "RehearsalReport",
    "RollbackBlocked",
    "RollbackResult",
    "SourceFingerprint",
    "StaleWriterObservation",
    "assert_not_runtime_path",
    "capture_snapshot",
    "choose_authority",
    "complete_interrupted_cutover",
    "deploy_marker",
    "evaluate_fence",
    "find_imported_rehearsal",
    "guard_legacy_write",
    "hash_legacy_state",
    "legacy_entity_id",
    "map_snapshot",
    "new_cutover_generation",
    "observe_post_cutover",
    "open_rehearsal_database",
    "plan_rehearsal",
    "prove_quiescence",
    "read_authority_row",
    "read_marker",
    "read_rehearsal_rows",
    "rehearse_cutover",
    "remove_marker",
    "require_absolute_path",
    "rollback_to_json",
    "run_rehearsal",
    "session_identity",
    "sessions_match",
    "validate_legacy_state",
]
