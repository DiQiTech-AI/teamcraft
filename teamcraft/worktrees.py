"""M4-02 Worktree lifecycle (M0 review §9.4 M4-02).

Bounded question this module answers, fail-closed:

    "For one writable Attempt, where is ITS isolated Git workspace — which
    branch, at which path, against which verified base — and is that identity
    still exactly the one we recorded? When (and only when) may the lifecycle
    advance ALLOCATED -> CLEANABLE without ever risking a dirty, unmerged, or
    foreign workspace?"

Design decisions pinned here:

* **Namespaced resources.** Every writable Attempt gets its own directory
  ``<repo>/.teamcraft/worktrees/<attempt-id>`` and its own branch
  ``tc/<task-id>/<attempt-id>`` (roadmap naming).  Two Attempts therefore can
  never share a path or a branch, and the ``.teamcraft/`` namespace keeps the
  project repository's own status clean (it is the runtime directory the
  project's ``.gitignore`` already excludes).
* **Durable identity BEFORE the effect.** The allocation is persisted as an
  immutable ``events`` row (kind ``worktree_allocated``, deterministic
  ``event_id`` = SHA-256 of the Attempt ID, so the v1 ``events.event_id``
  UNIQUE constraint caps each Attempt at exactly ONE allocation record at the
  database level).  The lifecycle marker ``worktree_cleanable`` is the same
  one-slot-per-Attempt pattern.  No new table and no schema bump: the v3
  append-only triggers make both records permanent, and M4-01's evidence
  plumbing supplies the verified base SHA.  Deliberate consequence: a failure
  AFTER allocation (git effect, marker write, or process crash) leaves the
  durable record and any on-disk workspace exactly as they were — nothing here
  ever deletes or rolls back a workspace.
* **Exact-identity restart adoption.**  :func:`claim_worktree` adopts a
  workspace ONLY when every recorded coordinate matches live Git: the path is
  a registered worktree of the recorded repository, ``HEAD`` is the recorded
  branch, and the worktree-scoped markers
  (``teamcraft.allocationId/attemptId/taskId``) equal the recorded IDs.
  A missing marker set is repaired only when everything else matches (our own
  workspace with a lost config); ANY mismatch — foreign marker values, a
  different branch, an unregistered/occupied path, a different repository —
  raises :class:`WorktreeIdentityError` and the foreign state is preserved.
* **Safe ALLOCATED -> CLEANABLE.**  The transition requires: the Attempt is
  terminal (execution ended), the identity claim above passes, and the
  workspace is COMPLETELY clean per the M4-01 collection
  (:func:`teamcraft.workspaces.collect_workspace_state` — staged, unstaged,
  untracked, deleted, rename endpoints).  Dirty, unmerged (``GitStateError``),
  foreign/ambiguous, or still-running workspaces are refused and are NEVER
  treated as cleanable.  CLEANABLE is a durable marker only: this milestone
  deletes nothing (cleanup/deletion is M4-06).
* **Read-only policy.**  An Attempt whose Task claims no ownership has no
  writable territory, so it gets no worktree: allocation is refused here and
  the routing layer (``runtime_adapter.route_member_workspace``) keeps such
  members on the project root.

Out of scope (intentionally absent): M4-03 verifier, M4-04 commit evidence,
M4-05 GitHub PR.

M4-06 (this build) consumes the CLEANABLE marker with the pieces appended at
the bottom of this module:

* :func:`detect_base_drift` compares the recorded base with the LIVE base of
  the repository (read-only plumbing; ``merge``/``rebase`` verbs are never
  issued anywhere in this module);
* :func:`check_merge_conflicts` proves via ``git merge-tree`` (a purely
  in-memory integration probe — it never touches any worktree, index or ref)
  whether the Attempt branch integrates into the live base cleanly, conflicts,
  fast-forwards, or is already contained;
* :func:`finalize_cleaned_worktree` performs the ONLY deletion this package
  ever performs: ``git worktree remove`` (never ``--force``) of the EXACT
  recorded workspace, gated by the caller (``teamcraft.delivery``) on the
  full CLEANABLE + delivery-DONE + evidence-complete eligibility matrix, and
  lands a deterministic, append-only ``worktree_cleaned`` fact.  The branch
  and every commit on it are PRESERVED (the delivery record outlives the
  workspace); a crash between removal and the fact is adopted by reconcile,
  so retries converge instead of repeating or lying about the effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Iterable

from . import domain, events
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    IllegalTransitionError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from .ids import require_entity_id
from .storage import TeamCraftConnection
from .workspaces import (
    GitStateError,
    _default_run_git,
    collect_workspace_state,
    load_base_evidence,
    normalize_claims,
    verify_repo_root,
)
from .workspaces import GitRunner

__all__ = [
    "ALLOCATED_EVENT_KIND",
    "CLEANED_EVENT_KIND",
    "CLEANABLE_EVENT_KIND",
    "FORBIDDEN_EFFECT_VERBS",
    "INTEGRATION_CHECK_EVENT_KIND",
    "INTEGRATION_REQUIRED_EVENT_KIND",
    "MERGE_STATUS_ANCESTOR",
    "MERGE_STATUS_CLEAN",
    "MERGE_STATUS_CONFLICT",
    "MERGE_STATUS_EMPTY",
    "MERGE_STATUS_FAST_FORWARD",
    "MERGE_STATUS_UNKNOWN",
    "WORKTREE_NAMESPACE_DIR",
    "WORKTREES_CONTRACT_VERSION",
    "BaseDrift",
    "MergeCheck",
    "WorktreeAllocation",
    "WorktreeCleanedRecord",
    "WorktreeIdentityError",
    "WorktreeStatus",
    "allocate_attempt_worktree",
    "branch_sha",
    "check_merge_conflicts",
    "claim_worktree",
    "detect_base_drift",
    "discover_allocations",
    "finalize_cleaned_worktree",
    "inspect_worktree",
    "lifecycle_state",
    "load_allocation",
    "load_cleaned_record",
    "mark_worktree_cleanable",
    "require_allocation",
    "validate_worktree_identity",
    "worktree_branch",
    "worktree_path_for",
]

#: Bumped when the persisted allocation/cleanable payload semantics change
#: incompatibly (same role as WORKSPACES_CONTRACT_VERSION plays for M4-01).
WORKTREES_CONTRACT_VERSION = 1

#: Immutable lifecycle records ride the append-only ``events`` stream.
ALLOCATED_EVENT_KIND = "worktree_allocated"
CLEANABLE_EVENT_KIND = "worktree_cleanable"

#: M4-06 durable cleanup fact (one per Attempt; deterministic event id, so
#: replays converge onto the single record and can never duplicate it).
CLEANED_EVENT_KIND = "worktree_cleaned"

#: Resource namespace inside the project repository (already gitignored).
WORKTREE_NAMESPACE_DIR = ".teamcraft/worktrees"

#: Attempt/Task IDs are opaque entity strings by contract, but a resource
#: name must survive filesystem paths and Git ref names.  IDs that cannot be
#: used safely for naming are refused at THIS boundary (naming), not by the
#: general entity-ID contract.
_SAFE_NAME_RE_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)

_MARKER_ALLOCATION = "teamcraft.allocationId"
_MARKER_ATTEMPT = "teamcraft.attemptId"
_MARKER_TASK = "teamcraft.taskId"

_ALLOC_PAYLOAD_FIELDS = (
    "allocation_id", "attempt_id", "base_sha", "branch", "claims",
    "claims_digest", "project_id", "repo_root", "schema_version", "task_id",
    "worktree_path",
)
_CLEAN_PAYLOAD_FIELDS = (
    "allocation_id", "attempt_id", "attempt_status", "branch", "project_id",
    "repo_root", "schema_version", "task_id", "worktree_path",
)


class WorktreeIdentityError(StorageError):
    """A workspace could not be adopted as exactly the recorded identity.

    ``kind`` is a stable machine tag: ``missing`` (recorded path absent),
    ``unregistered`` (path is not a worktree of the recorded repository),
    ``occupied`` (path holds unknown content), ``foreign`` (the workspace
    carries identity markers for someone else), ``drift`` (branch/repo moved
    off the recorded identity).  The refusal is fail-closed: the workspace and
    its content are left untouched.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class WorktreeAllocation:
    """One durable ALLOCATED record: the Attempt's exclusive resources."""

    seq: int
    event_id: str
    allocation_id: str
    project_id: str
    task_id: str
    attempt_id: str
    repo_root: str
    worktree_path: str
    branch: str
    base_sha: str
    claims: tuple[str, ...]
    claims_digest: str
    created_at: str


@dataclass(frozen=True)
class WorktreeStatus:
    """Live verdict for one allocation: recorded state + verified reality."""

    attempt_id: str
    state: str | None  # None | "allocated" | "cleanable"
    exists: bool
    problems: tuple[str, ...]
    dirty: bool | None
    change_count: int | None


# ---------------------------------------------------------------------------
# Naming (pure)
# ---------------------------------------------------------------------------


def _naming_safe(entity: str, value: str) -> str:
    value = require_entity_id(value)
    if not value or any(char not in _SAFE_NAME_RE_OK for char in value):
        raise ValidationError(
            f"{entity} id {value!r} cannot be used as a worktree resource "
            "name (letters, digits, '.', '_' and '-' only)"
        )
    return value


def worktree_branch(task_id: str, attempt_id: str) -> str:
    """Roadmap branch name ``tc/<task>/<attempt>`` (unique per Attempt)."""

    task = _naming_safe("task", task_id)
    attempt = _naming_safe("attempt", attempt_id)
    return f"tc/{task}/{attempt}"


def worktree_path_for(repo_root: str, attempt_id: str) -> str:
    """Namespaced absolute worktree directory for one Attempt."""

    attempt = _naming_safe("attempt", attempt_id)
    return os.path.join(repo_root, WORKTREE_NAMESPACE_DIR, attempt)


def _utc_iso(clock: Clock | None) -> str:
    clock = clock if clock is not None else SystemClock()
    return clock.now().astimezone(timezone.utc).isoformat()


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _allocation_event_id(attempt_id: str) -> str:
    return "evt-wta-" + _sha256_hex(f"worktree-allocation|{attempt_id}")[:40]


def _cleanable_event_id(attempt_id: str) -> str:
    return "evt-wtc-" + _sha256_hex(f"worktree-cleanable|{attempt_id}")[:40]


# ---------------------------------------------------------------------------
# Git plumbing helpers (all fail closed via GitStateError unless stated)
# ---------------------------------------------------------------------------


def _git(runner: GitRunner, args: list[str], cwd: str) -> str:
    return _text(runner(args, cwd))


def _text(raw: bytes) -> str:
    return os.fsdecode(raw).strip()


def _try_git(runner: GitRunner, args: list[str], cwd: str) -> str | None:
    """Run a probe command; ``None`` means 'no such thing' (exit nonzero)."""

    try:
        return _git(runner, args, cwd)
    except GitStateError:
        return None


def _registered_worktrees(runner: GitRunner, root: str) -> set[str]:
    """Realpaths of every worktree registered with ``root``'s repository."""

    raw = _git(runner, ["worktree", "list", "--porcelain"], root)
    paths: set[str] = set()
    for line in raw.splitlines():
        if line.startswith("worktree "):
            paths.add(os.path.realpath(line[len("worktree "):].strip()))
    return paths


def _common_dir(runner: GitRunner, cwd: str) -> str:
    out = _git(runner, ["rev-parse", "--path-format=absolute",
                        "--git-common-dir"], cwd)
    if not os.path.isabs(out):  # defensive on ancient plumbing
        out = os.path.join(cwd, out)
    return os.path.realpath(out)


def _read_marker(runner: GitRunner, path: str, key: str) -> str | None:
    return _try_git(runner, ["config", "--worktree", "--get", key], path)


def _write_markers(runner: GitRunner, path: str, allocation: "WorktreeAllocation") -> None:
    # git requires the worktreeConfig extension before --worktree writes are
    # allowed in a multi-worktree repository; enable it lazily (a repo without
    # any config.worktree files behaves exactly as before).
    _git(runner, ["config", "extensions.worktreeConfig", "true"], path)
    for key, value in ((_MARKER_ALLOCATION, allocation.allocation_id),
                      (_MARKER_ATTEMPT, allocation.attempt_id),
                      (_MARKER_TASK, allocation.task_id)):
        _git(runner, ["config", "--worktree", key, value], path)


# ---------------------------------------------------------------------------
# Payload canonicalization / strict load
# ---------------------------------------------------------------------------


def _claims_payload(claims: Iterable[str]) -> tuple[list[str], str]:
    canonical = [claim.canonical for claim in normalize_claims(claims)]
    return canonical, _sha256_hex(_canonical_json(canonical))


def _alloc_payload(allocation_id: str, *, project_id: str, task_id: str,
                   attempt_id: str, repo_root: str, worktree_path: str,
                   branch: str, base_sha: str,
                   claims: tuple[str, ...]) -> str:
    return _canonical_json({
        "allocation_id": allocation_id,
        "attempt_id": attempt_id,
        "base_sha": base_sha,
        "branch": branch,
        "claims": list(claims),
        "claims_digest": _claims_payload(claims)[1],
        "project_id": project_id,
        "repo_root": repo_root,
        "schema_version": WORKTREES_CONTRACT_VERSION,
        "task_id": task_id,
        "worktree_path": worktree_path,
    })


def _clean_payload(allocation: WorktreeAllocation, attempt_status: str) -> str:
    return _canonical_json({
        "allocation_id": allocation.allocation_id,
        "attempt_id": allocation.attempt_id,
        "attempt_status": attempt_status,
        "branch": allocation.branch,
        "project_id": allocation.project_id,
        "repo_root": allocation.repo_root,
        "schema_version": WORKTREES_CONTRACT_VERSION,
        "task_id": allocation.task_id,
        "worktree_path": allocation.worktree_path,
    })


def _alloc_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                   payload_json: str) -> WorktreeAllocation:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"worktree allocation payload is not valid JSON (seq={seq}): {exc}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != set(_ALLOC_PAYLOAD_FIELDS):
        raise StorageError(f"worktree allocation payload has unexpected shape (seq={seq})")
    if payload.get("schema_version") != WORKTREES_CONTRACT_VERSION:
        raise StorageError(
            f"unsupported worktree allocation schema_version: "
            f"{payload.get('schema_version')!r}")
    strings = ("allocation_id", "attempt_id", "base_sha", "branch",
               "project_id", "repo_root", "task_id", "worktree_path")
    if not all(isinstance(payload.get(k), str) and payload[k] for k in strings):
        raise StorageError(f"worktree allocation binding fields are malformed (seq={seq})")
    claims = payload.get("claims")
    if not isinstance(claims, list) or any(not isinstance(c, str) for c in claims):
        raise StorageError(f"worktree allocation claims are malformed (seq={seq})")
    try:
        canonical, digest = _claims_payload(claims)
    except Exception as exc:
        raise StorageError(f"worktree allocation claims are unsafe (seq={seq}): {exc}") from exc
    if canonical != claims or payload.get("claims_digest") != digest:
        raise StorageError(f"worktree allocation claims digest mismatch (seq={seq})")
    # Identity pinning: the recorded resource coordinates must be exactly what
    # THIS ids-and-namespace naming derives today.  A payload that names any
    # other branch/path/base was tampered with or predates the contract.
    if payload["branch"] != worktree_branch(payload["task_id"], payload["attempt_id"]):
        raise StorageError(f"worktree allocation branch is not the derived identity (seq={seq})")
    if payload["worktree_path"] != worktree_path_for(payload["repo_root"], payload["attempt_id"]):
        raise StorageError(f"worktree allocation path is not the derived identity (seq={seq})")
    base_sha = payload["base_sha"]
    if not (len(base_sha) in (40, 64) and all(c in "0123456789abcdef" for c in base_sha)):
        raise StorageError(f"worktree allocation base_sha is not a commit digest (seq={seq})")
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?", (seq,)).fetchone()
    created_at = "" if row is None or row[0] is None else str(row[0])
    return WorktreeAllocation(
        seq=seq, event_id=event_id, allocation_id=payload["allocation_id"],
        project_id=payload["project_id"], task_id=payload["task_id"],
        attempt_id=payload["attempt_id"], repo_root=payload["repo_root"],
        worktree_path=payload["worktree_path"], branch=payload["branch"],
        base_sha=base_sha, claims=tuple(claims), claims_digest=digest,
        created_at=created_at,
    )


def _event_row(conn: TeamCraftConnection, kind: str, attempt_id: str) -> Any:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events"
        " WHERE kind = ? AND attempt_id = ? ORDER BY seq ASC",
        (kind, attempt_id),
    ).fetchall()
    if len(rows) > 1:
        raise StorageError(
            f"{len(rows)} {kind} records for attempt {attempt_id};"
            " storage invariant broken")
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Reads: durable lifecycle state
# ---------------------------------------------------------------------------


def load_allocation(conn: TeamCraftConnection, attempt_id: str) -> WorktreeAllocation | None:
    """The durable ALLOCATED record for one Attempt, or ``None``.

    Malformed/tampered payloads and duplicated records fail closed
    (:class:`StorageError`) — never a silent pick.
    """

    require_entity_id(attempt_id)
    row = _event_row(conn, ALLOCATED_EVENT_KIND, attempt_id)
    if row is None:
        return None
    return _alloc_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def require_allocation(conn: TeamCraftConnection, attempt_id: str) -> WorktreeAllocation:
    allocation = load_allocation(conn, attempt_id)
    if allocation is None:
        raise NotFoundError(f"no worktree allocation recorded for attempt {attempt_id}")
    return allocation


def lifecycle_state(conn: TeamCraftConnection, attempt_id: str) -> str | None:
    """``"allocated"`` | ``"cleanable"`` | ``None`` (never allocated)."""

    require_entity_id(attempt_id)
    if _event_row(conn, CLEANABLE_EVENT_KIND, attempt_id) is not None:
        return "cleanable"
    if load_allocation(conn, attempt_id) is not None:
        return "allocated"
    return None


def discover_allocations(
    conn: TeamCraftConnection, project_id: str | None = None
) -> list[tuple[WorktreeAllocation, str]]:
    """Restart discovery: every recorded allocation with its derived state."""

    where = "WHERE kind = ?"
    params: list[Any] = [ALLOCATED_EVENT_KIND]
    if project_id is not None:
        where += " AND project_id = ?"
        params.append(require_entity_id(project_id))
    rows = conn.execute(
        "SELECT seq, event_id, payload_json, attempt_id FROM events "
        + where + " ORDER BY seq ASC", params,
    ).fetchall()
    out: list[tuple[WorktreeAllocation, str]] = []
    for row in rows:
        allocation = _alloc_from_row(conn, int(row[0]), str(row[1]), str(row[2]))
        cleanable = _event_row(conn, CLEANABLE_EVENT_KIND, str(row[3])) is not None
        out.append((allocation, "cleanable" if cleanable else "allocated"))
    return out


# ---------------------------------------------------------------------------
# Live identity verification
# ---------------------------------------------------------------------------


def _verify_identity(conn: TeamCraftConnection, allocation: WorktreeAllocation,
                     runner: GitRunner, *, repair: bool) -> None:
    """Adopt-or-refuse: the workspace must be EXACTLY the recorded identity."""

    path = allocation.worktree_path
    if not os.path.exists(path):
        raise WorktreeIdentityError(
            "missing",
            f"recorded worktree path does not exist: {path!r} (attempt "
            f"{allocation.attempt_id}); refusing to fabricate a workspace — "
            "the durable allocation is preserved for investigation")
    if not os.path.isdir(path):
        raise WorktreeIdentityError("occupied",
                                   f"recorded worktree path is not a directory: {path!r}")
    inside = _try_git(runner, ["rev-parse", "--is-inside-work-tree"], path)
    if inside != "true":
        raise WorktreeIdentityError(
            "occupied",
            f"path {path!r} holds content that is not a Git worktree; refusing "
            "to adopt an unknown directory")
    root_common = _common_dir(runner, allocation.repo_root)
    wt_common = _common_dir(runner, path)
    if wt_common != root_common:
        raise WorktreeIdentityError(
            "drift",
            f"worktree {path!r} belongs to a different repository "
            f"({wt_common!r} vs {root_common!r}); identity ambiguous")
    registered = _registered_worktrees(runner, allocation.repo_root)
    if os.path.realpath(path) not in registered:
        raise WorktreeIdentityError(
            "unregistered",
            f"path {path!r} is not registered as a worktree of "
            f"{allocation.repo_root!r}; refusing an ambiguous registration")
    head_branch = _try_git(runner, ["symbolic-ref", "--short", "-q", "HEAD"], path)
    if head_branch != allocation.branch:
        raise WorktreeIdentityError(
            "drift",
            f"worktree {path!r} is on branch {head_branch!r}, not the recorded "
            f"{allocation.branch!r}; refusing to adopt a moved workspace")
    markers = {key: _read_marker(runner, path, key)
               for key in (_MARKER_ALLOCATION, _MARKER_ATTEMPT, _MARKER_TASK)}
    expected = {
        _MARKER_ALLOCATION: allocation.allocation_id,
        _MARKER_ATTEMPT: allocation.attempt_id,
        _MARKER_TASK: allocation.task_id,
    }
    present = {key: value for key, value in markers.items() if value is not None}
    mismatched = {key: (value, expected[key])
                  for key, value in present.items() if value != expected[key]}
    if mismatched:
        detail = ", ".join(f"{key}={found!r}!={want!r}"
                          for key, (found, want) in sorted(mismatched.items()))
        raise WorktreeIdentityError(
            "foreign",
            f"worktree {path!r} carries identity markers that do not match "
            f"the allocation ({detail}); foreign or ambiguous identity — "
            "never adopted, never cleanable")
    missing = tuple(sorted(set(expected) - set(present)))
    if missing and not present and repair:
        # Everything else matches exactly (path, repo, registration, branch):
        # our own workspace whose worktree-scoped config was lost.  Re-pin it.
        _write_markers(runner, path, allocation)
    elif missing:
        raise WorktreeIdentityError(
            "markers-missing",
            f"worktree {path!r} lacks required identity marker(s) "
            f"{', '.join(missing)}; read-only inspection never repairs or "
            "adopts incomplete identity")


def validate_worktree_identity(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    run_git: GitRunner | None = None,
) -> WorktreeAllocation:
    """Validate the exact recorded identity without repairing any marker.

    This is the inspection-only counterpart to :func:`claim_worktree`.
    Missing, partial, foreign, or mismatched markers are reported as identity
    errors and no Git config, event, or database state is written.
    """

    runner = run_git or _default_run_git
    allocation = require_allocation(conn, attempt_id)
    verify_repo_root(allocation.repo_root)
    _verify_identity(conn, allocation, runner, repair=False)
    return allocation


def claim_worktree(conn: TeamCraftConnection, attempt_id: str, *,
                   run_git: GitRunner | None = None) -> WorktreeAllocation:
    """Restart/retry adoption of the EXACT recorded worktree identity.

    Only the recorded path/branch/repo/markers are adopted; a lost marker set
    on an otherwise exact match is repaired.  Any mismatch raises
    :class:`WorktreeIdentityError` fail-closed, leaving the foreign state and
    the durable record untouched.
    """

    runner = run_git or _default_run_git
    allocation = require_allocation(conn, attempt_id)
    verify_repo_root(allocation.repo_root)
    _verify_identity(conn, allocation, runner, repair=True)
    return allocation


def inspect_worktree(conn: TeamCraftConnection, attempt_id: str, *,
                     run_git: GitRunner | None = None) -> WorktreeStatus:
    """Non-mutating live view of one allocation (restart/audit surface)."""

    runner = run_git or _default_run_git
    allocation = load_allocation(conn, attempt_id)
    if allocation is None:
        return WorktreeStatus(attempt_id=attempt_id, state=None, exists=False,
                             problems=("no-allocation",), dirty=None,
                             change_count=None)
    cleanable = _event_row(conn, CLEANABLE_EVENT_KIND, attempt_id) is not None
    state = "cleanable" if cleanable else "allocated"
    problems: list[str] = []
    exists = os.path.isdir(allocation.worktree_path)
    dirty: bool | None = None
    change_count: int | None = None
    if not exists:
        problems.append("workspace-missing")
    else:
        try:
            _verify_identity(conn, allocation, runner, repair=False)
        except (WorktreeIdentityError, GitStateError) as exc:
            kind = getattr(exc, "kind", "git-unreadable")
            problems.append(f"identity:{kind}")
        if not problems:
            try:
                workspace = collect_workspace_state(allocation.worktree_path,
                                                  run_git=runner)
            except GitStateError as exc:
                # unmerged / unreadable git state: fail closed, never cleanable.
                problems.append(f"git:{exc}")
            else:
                change_count = len(workspace.changes)
                dirty = change_count > 0
    return WorktreeStatus(attempt_id=attempt_id, state=state, exists=exists,
                         problems=tuple(problems), dirty=dirty,
                         change_count=change_count)


# ---------------------------------------------------------------------------
# ALLOCATE
# ---------------------------------------------------------------------------


def allocate_attempt_worktree(
    conn: TeamCraftConnection,
    *,
    task_id: str,
    attempt_id: str,
    repo_root: str,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> tuple[WorktreeAllocation, bool]:
    """Deliver the isolated worktree + branch for one writable Attempt.

    Returns ``(allocation, created)``.  Re-allocation of the same Attempt
    re-adopts the exact recorded identity (``created=False``).  The durable
    allocation event is committed BEFORE the Git effect, so a failure after
    allocation preserves the durable record — and any workspace already on
    disk — for restart discovery; this function never deletes anything.

    Refusals (nothing written, nothing deleted):
    * attempt/task binding or terminal attempt -> ValidationError/Illegal;
    * Task claims no territory (read-only Attempt) -> ValidationError
      ``READ_ONLY_ATTEMPT_NO_WORKTREE``;
    * M4-01 base evidence recorded and the live base moved -> ConflictError;
    * target path registered/occupied without our record, or branch exists at
      a different SHA -> WorktreeIdentityError/ConflictError.
    """

    runner = run_git or _default_run_git
    task_id = require_entity_id(task_id)
    attempt_id = require_entity_id(attempt_id)
    root = verify_repo_root(repo_root)

    attempt = domain.get_attempt(conn, attempt_id)
    if attempt.task_id != task_id:
        raise ValidationError(
            f"attempt {attempt_id} belongs to task {attempt.task_id!r},"
            f" not {task_id!r}")
    if attempt.status in domain.TERMINAL_ATTEMPT_STATUSES:
        raise IllegalTransitionError(
            f"cannot allocate a worktree for terminal attempt {attempt_id} "
            f"({attempt.status})", entity="attempt", current=attempt.status,
            event="allocate_worktree", reason_code="ATTEMPT_TERMINAL")

    # Read-only policy: an Attempt with no owned territory gets no writable
    # workspace (routing keeps such members on the project root).
    evidence = load_base_evidence(conn, attempt_id)
    if evidence is not None:
        claims: tuple[str, ...] = evidence.claims
        if evidence.task_id != task_id or evidence.attempt_id != attempt_id:
            raise ValidationError("base evidence binds a different Task/Attempt")
        if os.path.realpath(evidence.repo_root) != root:
            raise ConflictError(
                f"base evidence repo root {evidence.repo_root!r} does not "
                f"match {root!r}")
    else:
        claims = tuple(claim.canonical
                      for claim in normalize_claims(json.loads(
                          domain.get_task(conn, task_id).ownership_json or "[]")))
    if not claims:
        raise ValidationError(
            f"attempt {attempt_id} of task {task_id} claims no ownership;"
            " read-only members get no writable worktree"
            " (READ_ONLY_ATTEMPT_NO_WORKTREE)")

    branch = worktree_branch(task_id, attempt_id)
    path = worktree_path_for(root, attempt_id)
    task = domain.get_task(conn, task_id)

    existing = load_allocation(conn, attempt_id)
    if existing is not None:
        # Restart/retry: adopt only the exact recorded identity.
        _verify_identity(conn, existing, runner, repair=True)
        _sync_attempt_resources(conn, attempt, existing)
        return existing, False

    workspace = collect_workspace_state(root, run_git=runner)
    base_sha = workspace.base_sha
    if evidence is not None:
        if evidence.base_sha != base_sha:
            raise ConflictError(
                f"verified base evidence SHA {evidence.base_sha!r} no longer "
                f"matches live HEAD {base_sha!r}; refusing to allocate on an"
                " unverified base")

    # Refuse paths/branches that are already taken by someone else.
    registered = _registered_worktrees(runner, root)
    if os.path.realpath(path) in registered:
        raise WorktreeIdentityError(
            "foreign",
            f"target worktree path {path!r} is already a registered worktree"
            " without an allocation record for this Attempt; refusing to take"
            " over a foreign workspace")
    if os.path.exists(path):
        raise WorktreeIdentityError(
            "occupied",
            f"target worktree path {path!r} already holds unknown content;"
            " refusing to overwrite or remove it")
    branch_tip = _try_git(runner, ["rev-parse", "--verify",
                                   f"refs/heads/{branch}"], root)
    if branch_tip is not None and branch_tip != base_sha:
        raise ConflictError(
            f"branch {branch!r} already exists at {branch_tip!r}, not the"
            f" verified base {base_sha!r}; refusing an ambiguous branch reuse")

    allocation_id = "wta-" + _sha256_hex(
        f"{attempt_id}|{root}|{branch}|{base_sha}")[:32]

    payload = _alloc_payload(allocation_id, project_id=task.project_id,
                             task_id=task_id, attempt_id=attempt_id,
                             repo_root=root, worktree_path=path, branch=branch,
                             base_sha=base_sha, claims=claims)
    event, inserted = events.append_event_idempotent(
        conn, ALLOCATED_EVENT_KIND, project_id=task.project_id,
        task_id=task_id, attempt_id=attempt_id, payload_json=payload,
        event_id=_allocation_event_id(attempt_id), clock=clock)
    if not inserted:
        # Concurrent allocation won: adopt the winner only if it is byte-identical
        # (append_event_idempotent already failed closed on different content),
        # then verify the winner's workspace identity exactly.
        winner = _alloc_from_row(conn, event.seq, event.event_id, payload)
        _verify_identity(conn, winner, runner, repair=True)
        _sync_attempt_resources(conn, attempt, winner)
        return winner, False

    allocation = _alloc_from_row(conn, event.seq, event.event_id, payload)
    # Durable record is committed.  Everything below is the Git effect: any
    # failure from here on PRESERVES both the record and any partial workspace
    # (never deleted, never silently retried into a different identity).
    try:
        if branch_tip is None:
            _git(runner, ["worktree", "add", path, "-b", branch, base_sha], root)
        else:
            _git(runner, ["worktree", "add", path, branch], root)
        _write_markers(runner, path, allocation)
        _verify_identity(conn, allocation, runner, repair=False)
    except (GitStateError, WorktreeIdentityError) as exc:
        raise StorageError(
            f"worktree allocation for attempt {attempt_id} was durably"
            f" recorded but the Git effect failed ({exc}); the workspace and"
            " the durable allocation are preserved for restart discovery"
        ) from exc
    _sync_attempt_resources(conn, attempt, allocation)
    return allocation, True


def _sync_attempt_resources(conn: TeamCraftConnection, attempt: domain.Attempt,
                            allocation: WorktreeAllocation) -> None:
    """Mirror the resource identity onto the Attempt row (durable, replay-safe)."""

    domain.set_attempt_resources(conn, attempt.id,
                                worktree_path=allocation.worktree_path,
                                branch=allocation.branch)


# ---------------------------------------------------------------------------
# ALLOCATED -> CLEANABLE (safe, durable marker; deletion is M4-06)
# ---------------------------------------------------------------------------


def mark_worktree_cleanable(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> WorktreeAllocation:
    """Safely transition one ALLOCATED worktree to CLEANABLE.

    Refusals (fail-closed, nothing marked, workspace content preserved):
    * no allocation -> NotFoundError;
    * attempt not terminal -> IllegalTransitionError ``ATTEMPT_NOT_TERMINAL``
      (a possibly-still-writing worker must never free its workspace);
    * identity not exactly the recorded one -> WorktreeIdentityError
      (foreign/ambiguous workspaces are never cleanable);
    * ANY uncommitted change (staged/unstaged/untracked/deleted/rename
      endpoints) -> IllegalTransitionError ``WORKTREE_DIRTY``;
    * unmerged or unreadable git state -> GitStateError.

    Already-CLEANABLE replays idempotently.  There is NO transition back and
    NOTHING is deleted: cleanup is M4-06 and consumes this marker only.
    """

    runner = run_git or _default_run_git
    allocation = require_allocation(conn, attempt_id)
    attempt = domain.get_attempt(conn, attempt_id)
    if attempt.status not in domain.TERMINAL_ATTEMPT_STATUSES:
        raise IllegalTransitionError(
            f"attempt {attempt_id} is {attempt.status!r}; only a terminal"
            " Attempt may release its worktree toward CLEANABLE",
            entity="worktree", current="allocated", event="mark_cleanable",
            reason_code="ATTEMPT_NOT_TERMINAL")

    existing = _event_row(conn, CLEANABLE_EVENT_KIND, attempt_id)
    if existing is not None:
        payload = json.loads(str(existing[2]))
        if not isinstance(payload, dict) or (
                payload.get("allocation_id") != allocation.allocation_id
                or payload.get("branch") != allocation.branch
                or payload.get("worktree_path") != allocation.worktree_path):
            raise StorageError(
                f"cleanable record for attempt {attempt_id} does not bind this"
                " allocation; storage invariant broken")
        return allocation

    # Exact identity first: dirty-but-foreign must report foreign, dirty-but-
    # ours reports dirty, unmerged reports unmerged — all refusals.
    _verify_identity(conn, allocation, runner, repair=True)
    try:
        workspace = collect_workspace_state(allocation.worktree_path,
                                           run_git=runner)
    except GitStateError as exc:
        raise GitStateError(
            f"worktree {allocation.worktree_path!r} has ambiguous git state"
            f" ({exc}); never treated as cleanable") from exc
    if workspace.changes:
        preview = ", ".join(entry.path for entry in workspace.changes[:5])
        raise IllegalTransitionError(
            f"worktree {allocation.worktree_path!r} holds "
            f"{len(workspace.changes)} uncommitted change(s) [{preview}"
            f"{'' if len(workspace.changes) <= 5 else ', ...'}]; a failed"
            " worker's dirty scene is preserved and never marked cleanable",
            entity="worktree", current="allocated", event="mark_cleanable",
            reason_code="WORKTREE_DIRTY")

    payload = _clean_payload(allocation, attempt.status)
    event, _ = events.append_event_idempotent(
        conn, CLEANABLE_EVENT_KIND, project_id=allocation.project_id,
        task_id=allocation.task_id, attempt_id=attempt_id,
        payload_json=payload, event_id=_cleanable_event_id(attempt_id),
        clock=clock)
    stored = _event_row(conn, CLEANABLE_EVENT_KIND, attempt_id)
    if stored is None or str(stored[1]) != event.event_id:
        raise StorageError(
            f"cleanable record for attempt {attempt_id} did not persist as"
            " expected; storage invariant broken")
    return allocation


# ---------------------------------------------------------------------------
# M4-06 — base drift + merge-conflict detection (read-only Git probes)
# ---------------------------------------------------------------------------

#: Merge-check statuses (stable machine vocabulary).
MERGE_STATUS_CLEAN = "clean"              # merges without conflict (divergent)
MERGE_STATUS_FAST_FORWARD = "fast_forward"  # branch strictly ahead of base
MERGE_STATUS_ANCESTOR = "ancestor"      # branch content already in base
MERGE_STATUS_EMPTY = "empty"            # branch == base; nothing to integrate
MERGE_STATUS_CONFLICT = "conflict"      # proven conflicting (never auto-merged)
MERGE_STATUS_UNKNOWN = "unknown"        # undecidable -> fail closed, not clean

#: Integration requirement event kinds (append-only audit stream).
INTEGRATION_CHECK_EVENT_KIND = "integration_check"
INTEGRATION_REQUIRED_EVENT_KIND = "integration_required"

#: Git verbs the M4-06 surface may NEVER issue (same posture as M4-04: no
#: push/merge/rebase/history rewrite; cleanup's only effect verb is the
#: narrowly-guarded ``worktree remove`` below).
FORBIDDEN_EFFECT_VERBS = frozenset({
    "push", "pull", "fetch", "clone", "merge", "rebase", "reset", "clean",
    "checkout", "switch", "restore", "prune", "gc", "filter-branch", "tag",
    "notes", "mv",
})


@dataclass(frozen=True)
class BaseDrift:
    """Recorded base vs the repository's live base for one Attempt."""

    attempt_id: str
    branch: str
    worktree_path: str
    recorded_base_sha: str
    live_base_sha: str | None   # None when the live base is unreadable
    drifted: bool               # True only when live base PROVES movement
    problems: tuple[str, ...]


@dataclass(frozen=True)
class MergeCheck:
    """Whether the Attempt branch provably integrates into ``base_sha``."""

    attempt_id: str
    branch: str
    base_sha: str | None
    head_sha: str | None
    status: str   # MERGE_STATUS_*
    conflict_files: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return self.status in (MERGE_STATUS_CLEAN, MERGE_STATUS_FAST_FORWARD,
                              MERGE_STATUS_ANCESTOR, MERGE_STATUS_EMPTY)

    @property
    def conflicting(self) -> bool:
        return self.status == MERGE_STATUS_CONFLICT


def branch_sha(runner: GitRunner, repo_root: str, branch: str) -> str | None:
    """Tip of ``branch`` inside ``repo_root`` (None when the ref is absent)."""

    return _try_git(runner, ["rev-parse", "--verify", f"refs/heads/{branch}"],
                   repo_root)


def detect_base_drift(conn: TeamCraftConnection, attempt_id: str, *,
                     run_git: GitRunner | None = None) -> BaseDrift:
    """Compare the recorded base against the repository's live HEAD.

    Read-only (``rev-parse`` only).  An unreadable live base is reported as
    ``problems`` with ``drifted=False`` — callers must treat undecidable as
    never-clean when making destructive decisions.
    """

    runner = run_git or _default_run_git
    allocation = require_allocation(conn, attempt_id)
    problems: tuple[str, ...] = ()
    live: str | None = None
    try:
        root = verify_repo_root(allocation.repo_root)
        live = _try_git(runner, ["rev-parse", "--verify", "HEAD"], root)
        if live is None:
            problems = ("live-base-unreadable",)
    except GitStateError as exc:
        problems = (f"live-base-unreadable: {exc}",)
    drifted = live is not None and live != allocation.base_sha
    return BaseDrift(
        attempt_id=attempt_id, branch=allocation.branch,
        worktree_path=allocation.worktree_path,
        recorded_base_sha=allocation.base_sha, live_base_sha=live,
        drifted=drifted, problems=problems)


def _merge_tree_raw(runner: GitRunner, base: str, head: str,
                   cwd: str) -> tuple[int | None, str, str]:
    """Run ``git merge-tree --write-tree`` and recover its exit posture.

    Returns ``(exit_code, stdout_text, error_text)``.  ``git merge-tree``
    exits 0 for a clean merge and 1 for conflicts (both write a real tree
    object; nothing in the worktree/index/refs moves).  A runner whose
    failure text carries no exit code yields ``None`` — the caller must then
    report UNKNOWN, never CLEAN.
    """

    try:
        out = runner(["merge-tree", "--write-tree", "--name-only", base, head],
                     cwd)
        return 0, _text(out), ""
    except GitStateError as exc:
        text = str(exc)
        match = re.search(r"exit (\d+)", text)
        return (int(match.group(1)) if match else None), "", text


def _conflict_paths(runner: GitRunner, ours: str, theirs: str, ancestor: str,
                   cwd: str) -> tuple[str, ...]:
    """Best-effort conflicted-path list via the legacy merge-tree form.

    The legacy ``git merge-tree <ours> <theirs> <ancestor>`` form exits 0
    even with conflicts and prints them to stdout ("changed in both" blocks
    whose ``base/our/their`` lines carry ``mode oid path``).  Diagnostics
    only — the verdict itself came from the ``--write-tree`` exit code.
    """

    try:
        raw = runner(["merge-tree", ours, theirs, ancestor], cwd)
    except GitStateError:
        return ()
    paths: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        match = re.match(r"^\s+(?:our|their)\s+\S+\s+\S+\s+(\S+)\s*$", line)
        if match and match.group(1) not in paths:
            paths.append(match.group(1))
    return tuple(paths[:20])


def check_merge_conflicts(conn: TeamCraftConnection, attempt_id: str, *,
                         base_sha: str | None = None,
                         run_git: GitRunner | None = None) -> MergeCheck:
    """Prove whether the Attempt branch integrates into ``base_sha``.

    ``base_sha`` defaults to the repository's live HEAD.  Fast-foward /
    already-ancestor / empty cases need no merge simulation at all; a truly
    divergent branch is probed with ``git merge-tree --write-tree`` (purely
    in-memory).  Exit 0 -> CLEAN, exit 1 -> CONFLICT (with best-effort path
    diagnostics), anything else -> UNKNOWN.  Nothing is ever merged,
    overwritten, or written to disk here.
    """

    runner = run_git or _default_run_git
    allocation = require_allocation(conn, attempt_id)
    root = verify_repo_root(allocation.repo_root)
    head = branch_sha(runner, root, allocation.branch)
    base = base_sha or _try_git(runner, ["rev-parse", "--verify", "HEAD"],
                                root)
    if head is None:
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=base, head_sha=None,
                         status=MERGE_STATUS_UNKNOWN, conflict_files=(),
                         problems=("attempt-branch-missing",))
    if base is None:
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=None, head_sha=head,
                         status=MERGE_STATUS_UNKNOWN, conflict_files=(),
                         problems=("live-base-unreadable",))
    if head == base:
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=base, head_sha=head, status=MERGE_STATUS_EMPTY,
                         conflict_files=(), problems=())
    merge_base = _try_git(runner, ["merge-base", base, head], root)
    if merge_base == base:
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=base, head_sha=head,
                         status=MERGE_STATUS_FAST_FORWARD, conflict_files=(),
                         problems=())
    if merge_base == head:
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=base, head_sha=head,
                         status=MERGE_STATUS_ANCESTOR, conflict_files=(),
                         problems=())
    if merge_base is None:
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=base, head_sha=head,
                         status=MERGE_STATUS_UNKNOWN, conflict_files=(),
                         problems=("merge-base-unreadable",))
    exit_code, _out, err = _merge_tree_raw(runner, base, head, root)
    if exit_code == 0:
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=base, head_sha=head, status=MERGE_STATUS_CLEAN,
                         conflict_files=(), problems=())
    if exit_code == 1:
        files = _conflict_paths(runner, head, base, merge_base, root)
        return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                         base_sha=base, head_sha=head,
                         status=MERGE_STATUS_CONFLICT, conflict_files=files,
                         problems=(f"merge-tree-conflict: {err[:200]}",))
    return MergeCheck(attempt_id=attempt_id, branch=allocation.branch,
                     base_sha=base, head_sha=head, status=MERGE_STATUS_UNKNOWN,
                     conflict_files=(),
                     problems=((f"merge-tree-undecidable(exit={exit_code}):"
                               f" {err[:200]}"),))


# ---------------------------------------------------------------------------
# M4-06 — CLEANABLE -> CLEANED (the one narrowly-guarded deletion surface)
# ---------------------------------------------------------------------------


_CLEANED_PAYLOAD_FIELDS = (
    "allocation_id", "attempt_id", "base_sha", "branch", "claims_digest",
    "commit_sha", "delivery_mode", "project_id", "repo_root",
    "schema_version", "source", "task_id", "worktree_path",
)


@dataclass(frozen=True)
class WorktreeCleanedRecord:
    """One immutable CLEANED fact: the workspace was released, refs kept."""

    seq: int
    event_id: str
    allocation_id: str
    project_id: str
    task_id: str
    attempt_id: str
    repo_root: str
    worktree_path: str
    branch: str
    base_sha: str
    commit_sha: str
    delivery_mode: str
    claims_digest: str
    source: str            # created | reconciled | replayed
    occurred_at: str


def _cleaned_event_id(attempt_id: str) -> str:
    return "evt-wtkd-" + _sha256_hex(f"worktree-cleaned|{attempt_id}")[:40]


def _cleaned_payload(allocation: WorktreeAllocation, *, commit_sha: str,
                     delivery_mode: str) -> str:
    # Deliberately stable: no clock/no crash-position marker, so a retry or a
    # reconcile converges onto the SAME event row (append_event_idempotent
    # only ever refuses when *content* differs, which proves two genuinely
    # different facts, not a replay of one).
    return _canonical_json({
        "allocation_id": allocation.allocation_id,
        "attempt_id": allocation.attempt_id,
        "base_sha": allocation.base_sha,
        "branch": allocation.branch,
        "claims_digest": allocation.claims_digest,
        "commit_sha": commit_sha,
        "delivery_mode": delivery_mode,
        "project_id": allocation.project_id,
        "repo_root": allocation.repo_root,
        "schema_version": WORKTREES_CONTRACT_VERSION,
        "source": "cleanup",
        "task_id": allocation.task_id,
        "worktree_path": allocation.worktree_path,
    })


def _cleaned_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                     payload_json: str) -> WorktreeCleanedRecord:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"worktree cleaned payload is not valid JSON (seq={seq}): {exc}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != set(
            _CLEANED_PAYLOAD_FIELDS):
        raise StorageError(f"worktree cleaned payload has unexpected shape (seq={seq})")
    if payload.get("schema_version") != WORKTREES_CONTRACT_VERSION:
        raise StorageError(
            f"unsupported worktree cleaned schema_version:"
            f" {payload.get('schema_version')!r}")
    strings = ("allocation_id", "attempt_id", "base_sha", "branch",
               "commit_sha", "delivery_mode", "project_id", "repo_root",
               "task_id", "worktree_path", "claims_digest", "source")
    if not all(isinstance(payload.get(k), str) and payload[k] for k in strings):
        raise StorageError(f"worktree cleaned binding fields are malformed (seq={seq})")
    if payload["source"] != "cleanup":
        raise StorageError(f"worktree cleaned source is invalid (seq={seq})")
    for key in ("base_sha", "commit_sha"):
        value = payload[key]
        if not (len(value) in (40, 64) and all(
                c in "0123456789abcdef" for c in value)):
            raise StorageError(
                f"worktree cleaned {key} is not a commit digest (seq={seq})")
    digest = payload["claims_digest"]
    if not (len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise StorageError(f"worktree cleaned claims digest malformed (seq={seq})")
    if payload["branch"] != worktree_branch(payload["task_id"], payload["attempt_id"]):
        raise StorageError(f"worktree cleaned branch is not the derived identity (seq={seq})")
    if payload["worktree_path"] != worktree_path_for(payload["repo_root"], payload["attempt_id"]):
        raise StorageError(f"worktree cleaned path is not the derived identity (seq={seq})")
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?",
                      (seq,)).fetchone()
    occurred_at = "" if row is None or row[0] is None else str(row[0])
    return WorktreeCleanedRecord(
        seq=seq, event_id=event_id, allocation_id=payload["allocation_id"],
        project_id=payload["project_id"], task_id=payload["task_id"],
        attempt_id=payload["attempt_id"], repo_root=payload["repo_root"],
        worktree_path=payload["worktree_path"], branch=payload["branch"],
        base_sha=payload["base_sha"], commit_sha=payload["commit_sha"],
        delivery_mode=payload["delivery_mode"], claims_digest=digest,
        source=payload["source"], occurred_at=occurred_at)


def load_cleaned_record(conn: TeamCraftConnection,
                        attempt_id: str) -> WorktreeCleanedRecord | None:
    """The durable CLEANED fact for one Attempt (one per Attempt, ever)."""

    require_entity_id(attempt_id)
    row = _event_row(conn, CLEANED_EVENT_KIND, attempt_id)
    if row is None:
        return None
    return _cleaned_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def _cleanup_git(runner: GitRunner, args: list[str], cwd: str) -> str:
    """The only Git effects M4-06 may issue: guarded ``worktree remove``.

    ``--force``/``-f`` are refused unconditionally — Git's own refusal of a
    dirty workspace is part of the safety story, never bypassed.
    """

    if args and args[0] == "worktree":
        if len(args) < 2 or args[1] != "remove" or "-f" in args or "--force" in args:
            raise StorageError(
                "worktree cleanup only issues 'git worktree remove' without"
                " force; refusing this command")
        _git(runner, args, cwd)
        return ""
    if args and args[0] in FORBIDDEN_EFFECT_VERBS:
        raise StorageError(
            f"worktree cleanup never runs 'git {args[0]}' (no push/merge/"
            "history rewrite/other deletion verbs are ever issued)")
    return _git(runner, args, cwd)


def finalize_cleaned_worktree(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    commit_sha: str,
    delivery_mode: str,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> tuple[WorktreeCleanedRecord, str]:
    """Release EXACTLY the recorded CLEANABLE workspace; keep every ref.

    Returns ``(record, disposition)`` where disposition is
    ``replayed`` (the durable CLEANED fact already existed), ``removed``
    (this call performed the guarded ``git worktree remove``) or ``adopted``
    (the removal had already landed but the fact did not — crash recovery
    adopts the on-disk truth after re-proving every identity fact).

    Safety (fail-closed, in order):
    * CLEANED replays validate their binding against the live allocation and
      converge onto the stored record;
    * the allocation must still be CLEANABLE and its Attempt terminal;
    * exact identity (path/registration/repo/branch; markers repaired only if
      everything else matches): foreign, moved, dirty, or unmerged workspaces
      are refused and PRESERVED;
    * ``git worktree remove`` without ``--force`` lets Git itself refuse any
      residue it sees;
    * post-effect proof: the path is gone and deregistered, while the branch
      ref still points at the delivered Commit (nothing is ever deleted from
      Git history);
    * one deterministic ``worktree_cleaned`` event per Attempt makes every
      retry converge.
    """

    runner = run_git or _default_run_git
    allocation = require_allocation(conn, attempt_id)
    attempt = domain.get_attempt(conn, attempt_id)
    if attempt.status not in domain.TERMINAL_ATTEMPT_STATUSES:
        raise IllegalTransitionError(
            f"attempt {attempt_id} is {attempt.status!r}; a non-terminal"
            " Attempt's workspace is never removable",
            entity="worktree", current="allocated", event="cleanup",
            reason_code="ATTEMPT_NOT_TERMINAL")

    existing = load_cleaned_record(conn, attempt_id)
    if existing is not None:
        if (existing.allocation_id != allocation.allocation_id
                or existing.commit_sha != commit_sha
                or existing.worktree_path != allocation.worktree_path
                or existing.branch != allocation.branch):
            raise StorageError(
                f"cleaned record for attempt {attempt_id} does not bind this"
                " allocation/commit; storage invariant broken")
        return existing, "replayed"

    if lifecycle_state(conn, attempt_id) != "cleanable":
        raise IllegalTransitionError(
            f"worktree for attempt {attempt_id} is not CLEANABLE; nothing may"
            " be released", entity="worktree",
            current=str(lifecycle_state(conn, attempt_id)), event="cleanup",
            reason_code="WORKTREE_NOT_CLEANABLE")

    root = verify_repo_root(allocation.repo_root)
    tip = branch_sha(runner, root, allocation.branch)
    if tip != commit_sha:
        raise IllegalTransitionError(
            f"branch {allocation.branch!r} tip is {tip!r}, not the delivered"
            f" commit {commit_sha!r}; undelivered commits are never cleaned"
            " away", entity="worktree", current="branch-moved",
            event="cleanup", reason_code="UNDELIVERED_COMMITS")

    path = allocation.worktree_path
    effect = "adopted"
    if os.path.exists(path):
        # Live workspace: it must be exactly ours, completely clean, and Git
        # itself gets the final veto (no force, ever).
        _verify_identity(conn, allocation, runner, repair=False)
        try:
            workspace = collect_workspace_state(path, run_git=runner)
        except GitStateError as exc:
            raise StorageError(
                f"worktree {path!r} has ambiguous git state ({exc}); cleanup"
                " refuses and preserves the scene") from exc
        if workspace.changes:
            preview = ", ".join(entry.path for entry in workspace.changes[:5])
            raise IllegalTransitionError(
                f"worktree {path!r} holds {len(workspace.changes)} uncommitted"
                f" change(s) [{preview}]; a dirty scene is preserved and never"
                " cleaned", entity="worktree", current="dirty",
                event="cleanup", reason_code="WORKTREE_DIRTY")
        try:
            _cleanup_git(runner, ["worktree", "remove", path], root)
        except (GitStateError, StorageError) as exc:
            raise StorageError(
                f"git refused to remove worktree {path!r} ({exc}); the"
                " workspace is preserved and nothing was recorded") from exc
        effect = "removed"
    else:
        # Crash-recovery adoption: the removal landed, the fact did not.
        # Re-prove every fact reachable without the workspace.
        registered = _registered_worktrees(runner, root)
        if os.path.realpath(path) in registered:
            raise StorageError(
                f"path {path!r} is still registered with {root!r} while its"
                " directory is gone; run 'git worktree prune' manually is NOT"
                " performed automatically — investigate first")

    if os.path.exists(path):
        raise StorageError(
            f"worktree {path!r} still exists after removal; refusing to"
            " record CLEANED")
    if branch_sha(runner, root, allocation.branch) != commit_sha:
        raise StorageError(
            f"branch {allocation.branch!r} moved during cleanup; refusing to"
            " record CLEANED — preserved for diagnosis")

    payload = _cleaned_payload(allocation, commit_sha=commit_sha,
                              delivery_mode=delivery_mode)
    event, _inserted = events.append_event_idempotent(
        conn, CLEANED_EVENT_KIND, project_id=allocation.project_id,
        task_id=allocation.task_id, attempt_id=attempt_id,
        payload_json=payload, event_id=_cleaned_event_id(attempt_id),
        clock=clock)
    record = _cleaned_from_row(conn, int(event.seq), event.event_id, payload)
    return record, effect
