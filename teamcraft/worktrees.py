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
M4-05 GitHub PR, M4-06 cleanup/deletion.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    "CLEANABLE_EVENT_KIND",
    "WORKTREE_NAMESPACE_DIR",
    "WORKTREES_CONTRACT_VERSION",
    "WorktreeAllocation",
    "WorktreeIdentityError",
    "WorktreeStatus",
    "allocate_attempt_worktree",
    "claim_worktree",
    "discover_allocations",
    "inspect_worktree",
    "lifecycle_state",
    "load_allocation",
    "mark_worktree_cleanable",
    "require_allocation",
    "worktree_branch",
    "worktree_path_for",
]

#: Bumped when the persisted allocation/cleanable payload semantics change
#: incompatibly (same role as WORKSPACES_CONTRACT_VERSION plays for M4-01).
WORKTREES_CONTRACT_VERSION = 1

#: Immutable lifecycle records ride the append-only ``events`` stream.
ALLOCATED_EVENT_KIND = "worktree_allocated"
CLEANABLE_EVENT_KIND = "worktree_cleanable"

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
    if present:
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
    elif repair:
        # Everything else matches exactly (path, repo, registration, branch):
        # our own workspace whose worktree-scoped config was lost.  Re-pin it.
        _write_markers(runner, path, allocation)


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
