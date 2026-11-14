"""M4-04 Commit/Diff evidence and local-only acceptance (M0 review §9.4 M4-04).

Bounded question this module answers, fail-closed:

    "For one finished Attempt of a local-only ship Task, can we durably
    produce — and later re-prove — the exact delivery facts: the Commit made
    in THIS Attempt's own worktree on ITS own branch on top of the verified
    base, the COMPLETE base-to-Commit diff (digest + artifact), the passing
    M4-03 verifier profile/config evidence, and finally a human Approval that
    accepts EXACTLY that Commit/branch and only then moves the Task to DONE?"

Storage pattern (deliberate, M4-02 precedent): the three durable evidence
records ride the append-only ``events`` stream with DETERMINISTIC event ids —
``delivery_commit_evidence`` (one per Attempt), ``delivery_ship_evidence``
(one per Attempt) and ``delivery_approval`` (one per ship evidence).  The v3
no-update/no-delete triggers make them immutable, ``events.event_id UNIQUE``
caps each binding at exactly one record (replays can only converge, never
duplicate), and every payload is strict-validated fail-closed on load with a
recomputed ``evidence_digest``.  This milestone adds NO schema step; a later
phase can harden these same kinds with partial unique indexes/triggers
(schema v18) without moving any fact.

Pinned decisions:

* **Worker finish, verifier PASS and Approval stay separate.**  Worker
  completion only triggers verification (M2/M4-03 semantics); ship evidence
  merely PROPOSES ``accept_local_only``; DONE requires the explicit Approval
  in this module, whose facts the domain gate re-derives from durable rows.
* **Durable Command before Git effect.**  :func:`create_local_commit`
  commits its ``delivery.local_commit`` Command (pending→running) BEFORE any
  ``git add``/``git commit``.  A crash after the Git effect but before the
  receipt is recovered by :func:`reconcile_commit_receipt`, which adopts the
  EXISTING commit from Git + Attempt/Session/Event facts (never a second
  commit), or expires the Command when Git proves nothing happened.
  Ambiguity (multiple commits, foreign parent, dirty scene, no unresolved
  Command) always fails closed.
* **Full Diff Gate + identity before committing.**  Only after
  :func:`teamcraft.worktrees.claim_worktree` adopts the exact recorded
  identity and the COMPLETE M4-01 change set (staged/unstaged/untracked/
  deleted/rename endpoints) passes ownership against the allocation's claims
  does anything get staged.  Everything staged therefore was verified owned.
* **No push, no merge, no deletion, no protected branch.**  This module only
  ever runs local plumbing (status/diff/rev-parse/add/commit/merge-base);
  push/fetch/pull/merge/rebase/reset/clean/checkout/branch-deletion never
  appear in any argv it builds (proven by tests with an auditing GitRunner).
  The Attempt branch must be the derived ``tc/<task>/<attempt>`` branch and
  is refused if it names a protected branch.
* **Idempotent and fenced.**  Same idempotency key replays the durable
  result verbatim; a settled Commit/ship evidence is returned (never redone)
  on any later call for the same Attempt; every mutation requires the live
  project Leader lease and re-checks its bindings inside the fence.
* **Any drift invalidates.**  Task version bump (task/policy edits),
  verifier profile/config changes, Commit amend/new commits on the branch,
  worktree dirt or identity drift all fail the live re-checks used by
  approval and by the ``*_is_current`` currency helpers; the domain gate
  re-derives the same facts from durable rows at DONE time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

from . import commands, domain, events, leader, verification, workspaces, worktrees
from .clock import Clock
from .errors import (
    ConflictError,
    IllegalTransitionError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from .ids import require_entity_id
from .storage import TeamCraftConnection
from .workspaces import GitRunner, _default_run_git

__all__ = [
    "APPROVAL_COMMAND_KIND",
    "COMMIT_COMMAND_KIND",
    "DELIVERY_CONTRACT_VERSION",
    "DeliveryRefusal",
    "DeliveryFreshness",
    "CommitEvidence",
    "DeliveryApproval",
    "MAX_DELIVERY_DIFF_BYTES",
    "SHIP_COMMAND_KIND",
    "ShipEvidence",
    "approve_local_delivery",
    "commit_evidence_is_current",
    "create_local_commit",
    "default_delivery_artifact_root",
    "delivery_approval_is_current",
    "get_commit_evidence",
    "get_delivery_approval",
    "get_ship_evidence",
    "list_commit_evidence",
    "list_delivery_approvals",
    "list_ship_evidence",
    "load_commit_evidence",
    "load_ship_evidence",
    "reconcile_commit_receipt",
    "record_ship_evidence",
    "ship_evidence_is_current",
    "read_ship_diff",
]

#: Bumped when any persisted delivery payload semantics change incompatibly.
DELIVERY_CONTRACT_VERSION = domain.DELIVERY_CONTRACT_VERSION

#: Persistent Command kinds.
COMMIT_COMMAND_KIND = "delivery.local_commit"
SHIP_COMMAND_KIND = "delivery.ship_evidence"
APPROVAL_COMMAND_KIND = "delivery.approve_local"

#: Durable evidence event kinds (canonical names live in ``domain``).
COMMIT_EVIDENCE_KIND = domain.COMMIT_EVIDENCE_EVENT_KIND
SHIP_EVIDENCE_KIND = domain.SHIP_EVIDENCE_EVENT_KIND
APPROVAL_EVENT_KIND = domain.DELIVERY_APPROVAL_EVENT_KIND

#: The only action this milestone can propose or accept.
LOCAL_ONLY_ACCEPT_ACTION = domain.LOCAL_ONLY_ACCEPT_ACTION
LOCAL_ACCEPT_EVENT = domain.LOCAL_DELIVERY_ACCEPT_EVENT

#: A local-only diff must stay reviewable and storeable; oversized diffs are
#: refused (fail-closed), never truncated into a partial "complete" digest.
MAX_DELIVERY_DIFF_BYTES = 8 * 1024 * 1024
#: Upper bound of the human audit text on an approval.
MAX_DELIVERY_AUDIT_BYTES = 1024
#: Per-file entries embedded in the commit-evidence payload (the full list
#: always lives in the diff artifact; this is a bounded quick view).
_MAX_EMBEDDED_FILES = 200

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SEGMENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: Git verbs that local delivery must NEVER issue (review: no push/merge/
#: deletion; also forbid history rewrites that would invalidate evidence).
FORBIDDEN_GIT_VERBS = frozenset({
    "push", "pull", "fetch", "clone", "merge", "rebase", "reset", "clean",
    "checkout", "switch", "restore", "prune", "gc", "filter-branch",
    "remove", "mv", "tag", "notes",
})


class DeliveryRefusal(ValidationError):
    """A local-delivery step was refused fail-closed; nothing was written.

    ``kind`` is a stable machine tag: ``identity``, ``unowned``, ``dirty``,
    ``empty``, ``drift``, ``main-branch``, ``size``, ``verifier``,
    ``ambiguous``, ``policy``, ``stale``, ``settled``, ``mode``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class DeliveryFreshness:
    """Verdict of a live currency re-check for one durable evidence record."""

    ok: bool
    problems: tuple[str, ...]


@dataclass(frozen=True)
class CommitEvidence:
    """One immutable Commit/Diff record bound to an Attempt's resources."""

    seq: int
    event_id: str
    project_id: str
    task_id: str
    attempt_id: str
    attempt_no: int
    task_version: int
    dispatch_task_version: int
    brief_digest: str
    brief_version: int
    worker_report_id: int
    worker_report_digest: str
    allocation_id: str
    worktree_path: str
    branch: str
    base_sha: str
    parent_sha: str
    commit_sha: str
    diff_digest: str
    file_count: int
    claims: tuple[str, ...]
    claims_digest: str
    source: str            # created | reconciled
    command_id: str
    evidence_digest: str
    occurred_at: str


@dataclass(frozen=True)
class ShipEvidence:
    """One immutable ship Verification evidence: the full delivery binding."""

    seq: int
    event_id: str
    project_id: str
    task_id: str
    attempt_id: str
    attempt_no: int
    task_version: int
    dispatch_task_version: int
    brief_digest: str
    brief_version: int
    worker_report_id: int
    worker_report_digest: str
    delivery_mode: str
    proposed_action: str
    allocation_id: str
    worktree_path: str
    branch: str
    base_sha: str
    commit_sha: str
    commit_evidence_id: str
    diff_digest: str
    diff_bytes: int
    diff_relpath: str
    file_count: int
    verifiers: tuple[dict[str, Any], ...]
    verifier_digest: str
    policy_digest: str
    evidence_digest: str
    command_id: str
    occurred_at: str

    @property
    def id(self) -> str:
        """Canonical identity of this evidence: its durable event id."""
        return self.event_id


@dataclass(frozen=True)
class DeliveryApproval:
    """One append-only human decision bound to exactly one ship evidence."""

    seq: int
    event_id: str
    command_id: str
    ship_evidence_id: str
    project_id: str
    task_id: str
    attempt_id: str
    attempt_no: int
    task_version: int
    commit_sha: str
    branch: str
    diff_digest: str
    evidence_digest: str
    verifier_digest: str
    proposed_action: str
    decision: str          # accept | reject
    actor: str
    reason: str
    task_status_after: str
    occurred_at: str

    @property
    def id(self) -> str:
        """Canonical identity of this approval: its durable event id."""
        return self.event_id


# ---------------------------------------------------------------------------
# Small shared primitives
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload_digest(payload: dict) -> str:
    """SHA-256 over the payload's canonical JSON minus ``evidence_digest``.

    Mirrors ``domain._delivery_payload_digest``; both recomputations are
    pinned to agree by the M4-04 acceptance tests.
    """
    stripped = {key: value for key, value in payload.items()
                if key != "evidence_digest"}
    return _sha256_hex(_canonical_json(stripped))


def _tx(conn: TeamCraftConnection):
    return storage_transaction(conn) if not conn.in_transaction else _Joined(conn)


class _Joined:
    def __init__(self, conn: TeamCraftConnection):
        self.conn = conn

    def __enter__(self) -> TeamCraftConnection:
        return self.conn

    def __exit__(self, *args: Any) -> bool:
        return False


# Late-bound to keep the import surface stable for tests that patch it.
def storage_transaction(conn: TeamCraftConnection):
    from . import storage
    return storage.transaction(conn)


def _text(raw: bytes) -> str:
    return os.fsdecode(raw).strip()


def _git(runner: GitRunner, args: list[str], cwd: str) -> str:
    return _text(runner(args, cwd))


def _try_git(runner: GitRunner, args: list[str], cwd: str) -> str | None:
    try:
        return _git(runner, args, cwd)
    except workspaces.GitStateError:
        return None


def _safe_segment(entity: str, value: str) -> str:
    value = require_entity_id(value)
    if not value or any(char not in _SAFE_SEGMENT_CHARS for char in value):
        raise ValidationError(
            f"{entity} {value!r} cannot be used as an artifact path segment "
            "(letters, digits, '.', '_' and '-' only)")
    return value


def default_delivery_artifact_root(repo_root: str) -> str:
    """Default configured artifact root: ``<repo>/.teamcraft/deliveries``."""
    return os.path.join(repo_root, ".teamcraft", "deliveries")


def _refuse_protected_branch(branch: str) -> None:
    if (not branch or branch.lower() in domain.PROTECTED_BRANCH_NAMES
            or not branch.startswith("tc/")):
        raise DeliveryRefusal(
            "main-branch",
            f"local delivery only operates on the isolated Attempt branch "
            f"tc/<task>/<attempt>; refusing protected/foreign branch {branch!r}")


def _git_audit_safe(runner: GitRunner, args: list[str], cwd: str) -> bytes:
    """Every GitRunner usage funnels through here so the forbidden-verb audit
    is enforced centrally: local delivery performs local plumbing only."""
    if args and args[0] in FORBIDDEN_GIT_VERBS:
        raise DeliveryRefusal(
            "drift",
            f"local delivery never runs 'git {args[0]}' (no push/merge/"
            "history rewrite/deletion verbs are ever issued)")
    return runner(args, cwd)


class _AuditedRunner:
    """Runner adapter that enforces :data:`FORBIDDEN_GIT_VERBS` and pins the
    worktree scope to the recorded workspace."""

    def __init__(self, runner: GitRunner) -> None:
        self._runner = runner

    def __call__(self, args: list[str], cwd: str) -> bytes:
        return _git_audit_safe(self._runner, args, cwd)


# ---------------------------------------------------------------------------
# Strict payload validation (fail closed on any shape/digest surprise)
# ---------------------------------------------------------------------------

_COMMIT_PAYLOAD_FIELDS = frozenset({
    "allocation_id", "attempt_id", "attempt_no", "base_sha", "branch",
    "brief_digest", "brief_version", "claims", "claims_digest", "commit_sha",
    "command_id", "diff_digest", "dispatch_task_version", "evidence_digest",
    "file_count",
    "files", "files_truncated", "parent_sha", "project_id", "schema_version",
    "source", "task_id", "task_version", "worker_report_digest",
    "worker_report_id", "worktree_path",
})
_SHIP_PAYLOAD_FIELDS = frozenset({
    "allocation_id", "attempt_id", "attempt_no", "base_sha", "branch",
    "brief_digest", "brief_version", "commit_evidence_id", "commit_sha",
    "command_id", "delivery_mode", "diff_bytes", "diff_digest", "diff_relpath",
    "dispatch_task_version", "evidence_digest", "file_count", "policy_digest",
    "project_id", "proposed_action", "schema_version", "task_id",
    "task_version", "verifier_digest", "verifiers_json",
    "worker_report_digest", "worker_report_id", "worktree_path",
})
_APPROVAL_PAYLOAD_FIELDS = frozenset({
    "actor", "attempt_id", "attempt_no", "branch", "brief_digest",
    "brief_version", "command_id", "commit_sha", "decision", "diff_digest",
    "evidence_digest", "project_id", "proposed_action", "reason",
    "schema_version", "ship_evidence_id", "task_id", "task_status_after",
    "task_version", "verifier_digest", "worker_report_digest",
    "worker_report_id",
})


def _require_shape(payload: Any, fields: frozenset[str], kind: str,
                   where: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise StorageError(
            f"{kind} payload has unexpected shape ({where}); expected exactly "
            f"{sorted(fields)}")
    if payload.get("schema_version") != DELIVERY_CONTRACT_VERSION:
        raise StorageError(
            f"unsupported {kind} schema_version: {payload.get('schema_version')!r}")
    return payload


def _require_str(payload: dict, key: str, kind: str, where: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise StorageError(f"{kind}.{key} must be a non-empty string ({where})")
    return value


def _require_int(payload: dict, key: str, kind: str, where: str,
                 minimum: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StorageError(
            f"{kind}.{key} must be an integer >= {minimum} ({where})")
    return int(value)


def _require_sha(payload: dict, key: str, kind: str, where: str) -> str:
    value = _require_str(payload, key, kind, where)
    if not _SHA_RE.match(value):
        raise StorageError(f"{kind}.{key} is not a commit digest ({where})")
    return value


def _require_digest64(payload: dict, key: str, kind: str, where: str) -> str:
    value = _require_str(payload, key, kind, where)
    if not _DIGEST64_RE.match(value):
        raise StorageError(f"{kind}.{key} is not a lowercase SHA-256 ({where})")
    return value


def _verify_claims_field(payload: dict, kind: str, where: str) -> tuple[list[str], str]:
    claims = payload.get("claims")
    if not isinstance(claims, list) or any(not isinstance(c, str) for c in claims):
        raise StorageError(f"{kind}.claims is malformed ({where})")
    try:
        canonical = [claim.canonical
                     for claim in workspaces.normalize_claims(claims)]
    except workspaces.WorkspacePathError as exc:
        raise StorageError(f"{kind}.claims are unsafe ({where}): {exc}") from exc
    digest = _sha256_hex(_canonical_json(canonical))
    if canonical != claims or payload.get("claims_digest") != digest:
        raise StorageError(f"{kind}.claims digest mismatch ({where})")
    return canonical, digest


def _commit_payload(**fields: Any) -> str:
    payload = dict(fields)
    payload["schema_version"] = DELIVERY_CONTRACT_VERSION
    payload["evidence_digest"] = _payload_digest(payload)
    return _canonical_json(payload)


def _commit_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                     payload_json: str) -> CommitEvidence:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"commit evidence payload is not valid JSON (seq={seq}): {exc}") from exc
    kind = "commit evidence"
    _require_shape(payload, _COMMIT_PAYLOAD_FIELDS, kind, f"seq={seq}")
    where = f"seq={seq}"
    if _payload_digest(payload) != payload.get("evidence_digest"):
        raise StorageError(f"{kind} digest does not recompute ({where})")
    if payload.get("source") not in ("created", "reconciled"):
        raise StorageError(f"{kind}.source is invalid ({where})")
    claims, claims_digest = _verify_claims_field(payload, kind, where)
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?",
                      (seq,)).fetchone()
    occurred_at = "" if row is None or row[0] is None else str(row[0])
    evidence = CommitEvidence(
        seq=seq, event_id=event_id,
        project_id=_require_str(payload, "project_id", kind, where),
        task_id=_require_str(payload, "task_id", kind, where),
        attempt_id=_require_str(payload, "attempt_id", kind, where),
        attempt_no=_require_int(payload, "attempt_no", kind, where, 1),
        task_version=_require_int(payload, "task_version", kind, where, 0),
        dispatch_task_version=_require_int(
            payload, "dispatch_task_version", kind, where, 0),
        brief_digest=_require_digest64(payload, "brief_digest", kind, where),
        brief_version=_require_int(payload, "brief_version", kind, where, 1),
        worker_report_id=_require_int(payload, "worker_report_id", kind, where, 1),
        worker_report_digest=_require_digest64(
            payload, "worker_report_digest", kind, where),
        allocation_id=_require_str(payload, "allocation_id", kind, where),
        worktree_path=_require_str(payload, "worktree_path", kind, where),
        branch=_require_str(payload, "branch", kind, where),
        base_sha=_require_sha(payload, "base_sha", kind, where),
        parent_sha=_require_sha(payload, "parent_sha", kind, where),
        commit_sha=_require_sha(payload, "commit_sha", kind, where),
        diff_digest=_require_digest64(payload, "diff_digest", kind, where),
        file_count=_require_int(payload, "file_count", kind, where, 1),
        claims=tuple(claims), claims_digest=claims_digest,
        source=str(payload["source"]),
        command_id=_require_str(payload, "command_id", kind, where),
        evidence_digest=_require_digest64(payload, "evidence_digest", kind, where),
        occurred_at=occurred_at,
    )
    if evidence.parent_sha != evidence.base_sha:
        raise StorageError(
            f"{kind} parent {evidence.parent_sha!r} is not the verified base "
            f"{evidence.base_sha!r} ({where})")
    _refuse_protected_branch(evidence.branch)
    return evidence


def _verifier_entries(payload: dict, kind: str, where: str) -> list[dict[str, Any]]:
    raw = payload.get("verifiers_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise StorageError(f"{kind}.verifiers_json is invalid ({where})") from exc
    if not isinstance(raw, list) or not raw:
        raise StorageError(
            f"{kind}.verifiers_json must be a non-empty list ({where})")
    entries: list[dict[str, Any]] = []
    expected = {"config_digest", "log_digest", "profile", "profile_version",
                "report_digest", "status", "verification_id", "exit_code"}
    for item in raw:
        if not isinstance(item, dict) or set(item) != expected:
            raise StorageError(
                f"{kind}.verifiers_json entry has unexpected shape ({where})")
        entry = {
            "profile": _require_str(item, "profile", kind, where),
            "profile_version": _require_int(item, "profile_version", kind, where, 1),
            "config_digest": _require_digest64(item, "config_digest", kind, where),
            "verification_id": _require_str(item, "verification_id", kind, where),
            "report_digest": _require_digest64(item, "report_digest", kind, where),
            "log_digest": (None if item.get("log_digest") is None else
                          _require_digest64(item, "log_digest", kind, where)),
            "exit_code": (None if item.get("exit_code") is None
                         else int(item["exit_code"])),
            "status": _require_str(item, "status", kind, where),
        }
        if entry["status"] != "pass":
            raise StorageError(
                f"{kind} verifier {entry['profile']!r} is not a pass ({where})")
        entries.append(entry)
    entries.sort(key=lambda item: str(item["profile"]))
    return entries


def _verifier_digest(entries: list[dict[str, Any]]) -> str:
    return _sha256_hex(_canonical_json(entries))


def _ship_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                   payload_json: str) -> ShipEvidence:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"ship evidence payload is not valid JSON (seq={seq}): {exc}") from exc
    kind = "ship evidence"
    where = f"seq={seq}"
    _require_shape(payload, _SHIP_PAYLOAD_FIELDS, kind, where)
    if _payload_digest(payload) != payload.get("evidence_digest"):
        raise StorageError(f"{kind} digest does not recompute ({where})")
    if payload.get("delivery_mode") != "local_only":
        raise StorageError(f"{kind}.delivery_mode must be local_only ({where})")
    if payload.get("proposed_action") != LOCAL_ONLY_ACCEPT_ACTION:
        raise StorageError(f"{kind}.proposed_action is unknown ({where})")
    if payload.get("diff_bytes", 0) > MAX_DELIVERY_DIFF_BYTES:
        raise StorageError(f"{kind} diff artifact exceeds the size cap ({where})")
    entries = _verifier_entries(payload, kind, where)
    if _verifier_digest(entries) != payload.get("verifier_digest"):
        raise StorageError(f"{kind} verifier digest does not recompute ({where})")
    diff_relpath = _require_str(payload, "diff_relpath", kind, where)
    if (diff_relpath.startswith("/") or ".." in diff_relpath
            or "\\" in diff_relpath or "\x00" in diff_relpath):
        raise StorageError(
            f"{kind}.diff_relpath must be artifact-root-relative ({where})")
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?",
                      (seq,)).fetchone()
    occurred_at = "" if row is None or row[0] is None else str(row[0])
    evidence = ShipEvidence(
        seq=seq, event_id=event_id,
        project_id=_require_str(payload, "project_id", kind, where),
        task_id=_require_str(payload, "task_id", kind, where),
        attempt_id=_require_str(payload, "attempt_id", kind, where),
        attempt_no=_require_int(payload, "attempt_no", kind, where, 1),
        task_version=_require_int(payload, "task_version", kind, where, 0),
        dispatch_task_version=_require_int(
            payload, "dispatch_task_version", kind, where, 0),
        brief_digest=_require_digest64(payload, "brief_digest", kind, where),
        brief_version=_require_int(payload, "brief_version", kind, where, 1),
        worker_report_id=_require_int(payload, "worker_report_id", kind, where, 1),
        worker_report_digest=_require_digest64(
            payload, "worker_report_digest", kind, where),
        delivery_mode="local_only",
        proposed_action=str(payload["proposed_action"]),
        allocation_id=_require_str(payload, "allocation_id", kind, where),
        worktree_path=_require_str(payload, "worktree_path", kind, where),
        branch=_require_str(payload, "branch", kind, where),
        base_sha=_require_sha(payload, "base_sha", kind, where),
        commit_sha=_require_sha(payload, "commit_sha", kind, where),
        commit_evidence_id=_require_str(payload, "commit_evidence_id", kind, where),
        diff_digest=_require_digest64(payload, "diff_digest", kind, where),
        diff_bytes=_require_int(payload, "diff_bytes", kind, where, 0),
        diff_relpath=diff_relpath,
        file_count=_require_int(payload, "file_count", kind, where, 1),
        verifiers=tuple(entries),
        verifier_digest=_require_digest64(payload, "verifier_digest", kind, where),
        policy_digest=_require_digest64(payload, "policy_digest", kind, where),
        evidence_digest=_require_digest64(payload, "evidence_digest", kind, where),
        command_id=_require_str(payload, "command_id", kind, where),
        occurred_at=occurred_at,
    )
    _refuse_protected_branch(evidence.branch)
    return evidence


def _approval_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                       payload_json: str) -> DeliveryApproval:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"delivery approval payload is not valid JSON (seq={seq}): {exc}") from exc
    kind = "delivery approval"
    where = f"seq={seq}"
    _require_shape(payload, _APPROVAL_PAYLOAD_FIELDS, kind, where)
    decision = payload.get("decision")
    if decision not in ("accept", "reject"):
        raise StorageError(f"{kind}.decision is invalid ({where})")
    if payload.get("proposed_action") != LOCAL_ONLY_ACCEPT_ACTION:
        raise StorageError(f"{kind}.proposed_action is unknown ({where})")
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?",
                      (seq,)).fetchone()
    occurred_at = "" if row is None or row[0] is None else str(row[0])
    approval = DeliveryApproval(
        seq=seq, event_id=event_id,
        command_id=_require_str(payload, "command_id", kind, where),
        ship_evidence_id=_require_str(payload, "ship_evidence_id", kind, where),
        project_id=_require_str(payload, "project_id", kind, where),
        task_id=_require_str(payload, "task_id", kind, where),
        attempt_id=_require_str(payload, "attempt_id", kind, where),
        attempt_no=_require_int(payload, "attempt_no", kind, where, 1),
        task_version=_require_int(payload, "task_version", kind, where, 0),
        commit_sha=_require_sha(payload, "commit_sha", kind, where),
        branch=_require_str(payload, "branch", kind, where),
        diff_digest=_require_digest64(payload, "diff_digest", kind, where),
        evidence_digest=_require_digest64(payload, "evidence_digest", kind, where),
        verifier_digest=_require_digest64(payload, "verifier_digest", kind, where),
        proposed_action=str(payload["proposed_action"]),
        decision=str(decision),
        actor=_require_str(payload, "actor", kind, where),
        reason=_require_str(payload, "reason", kind, where),
        task_status_after=_require_str(payload, "task_status_after", kind, where),
        occurred_at=occurred_at,
    )
    if approval.task_status_after not in ("done", "verifying"):
        raise StorageError(f"{kind}.task_status_after is invalid ({where})")
    if decision == "accept" and approval.task_status_after != "done":
        raise StorageError(
            f"{kind} accept must record task_status_after=done ({where})")
    return approval


# ---------------------------------------------------------------------------
# Durable readers
# ---------------------------------------------------------------------------


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


def _event_by_id(conn: TeamCraftConnection, kind: str, event_id: str) -> Any:
    row = conn.execute(
        "SELECT seq, event_id, payload_json FROM events"
        " WHERE kind = ? AND event_id = ?", (kind, event_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"{kind} not found: {event_id}")
    return row


def _command_idempotency(conn: TeamCraftConnection, command_id: str) -> str:
    row = conn.execute("SELECT idempotency_key FROM commands WHERE id = ?",
                      (command_id,)).fetchone()
    if row is None:
        raise StorageError(f"delivery evidence references a missing command {command_id}")
    return str(row[0])


def load_commit_evidence(conn: TeamCraftConnection,
                        attempt_id: str) -> CommitEvidence | None:
    require_entity_id(attempt_id)
    row = _event_row(conn, COMMIT_EVIDENCE_KIND, attempt_id)
    if row is None:
        return None
    return _commit_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def require_commit_evidence(conn: TeamCraftConnection,
                           attempt_id: str) -> CommitEvidence:
    evidence = load_commit_evidence(conn, attempt_id)
    if evidence is None:
        raise NotFoundError(f"no commit evidence recorded for attempt {attempt_id}")
    return evidence


def get_commit_evidence(conn: TeamCraftConnection,
                       event_id: str) -> CommitEvidence:
    row = _event_by_id(conn, COMMIT_EVIDENCE_KIND, require_entity_id(event_id))
    return _commit_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def list_commit_evidence(conn: TeamCraftConnection,
                        task_id: str) -> list[CommitEvidence]:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events"
        " WHERE kind = ? AND task_id = ? ORDER BY seq ASC",
        (COMMIT_EVIDENCE_KIND, require_entity_id(task_id)),
    ).fetchall()
    return [_commit_from_row(conn, int(r[0]), str(r[1]), str(r[2])) for r in rows]


def load_ship_evidence(conn: TeamCraftConnection,
                      attempt_id: str) -> ShipEvidence | None:
    require_entity_id(attempt_id)
    row = _event_row(conn, SHIP_EVIDENCE_KIND, attempt_id)
    if row is None:
        return None
    return _ship_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def require_ship_evidence(conn: TeamCraftConnection,
                         attempt_id: str) -> ShipEvidence:
    evidence = load_ship_evidence(conn, attempt_id)
    if evidence is None:
        raise NotFoundError(f"no ship evidence recorded for attempt {attempt_id}")
    return evidence


def get_ship_evidence(conn: TeamCraftConnection, event_id: str) -> ShipEvidence:
    row = _event_by_id(conn, SHIP_EVIDENCE_KIND, require_entity_id(event_id))
    return _ship_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def list_ship_evidence(conn: TeamCraftConnection,
                      task_id: str) -> list[ShipEvidence]:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events"
        " WHERE kind = ? AND task_id = ? ORDER BY seq ASC",
        (SHIP_EVIDENCE_KIND, require_entity_id(task_id)),
    ).fetchall()
    return [_ship_from_row(conn, int(r[0]), str(r[1]), str(r[2])) for r in rows]


def get_delivery_approval(conn: TeamCraftConnection,
                         event_id: str) -> DeliveryApproval:
    row = _event_by_id(conn, APPROVAL_EVENT_KIND, require_entity_id(event_id))
    return _approval_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def load_delivery_approval(conn: TeamCraftConnection,
                          ship_evidence_id: str) -> DeliveryApproval | None:
    row = conn.execute(
        "SELECT seq, event_id, payload_json FROM events"
        " WHERE kind = ? AND json_extract(payload_json, '$.ship_evidence_id') = ?"
        " ORDER BY seq ASC",
        (APPROVAL_EVENT_KIND, require_entity_id(ship_evidence_id)),
    ).fetchall()
    if len(row) > 1:
        raise StorageError(
            f"{len(row)} approvals for ship evidence {ship_evidence_id};"
            " storage invariant broken")
    if not row:
        return None
    return _approval_from_row(conn, int(row[0][0]), str(row[0][1]), str(row[0][2]))


def list_delivery_approvals(conn: TeamCraftConnection,
                           task_id: str) -> list[DeliveryApproval]:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events"
        " WHERE kind = ? AND task_id = ? ORDER BY seq ASC",
        (APPROVAL_EVENT_KIND, require_entity_id(task_id)),
    ).fetchall()
    return [_approval_from_row(conn, int(r[0]), str(r[1]), str(r[2])) for r in rows]


# ---------------------------------------------------------------------------
# Live Git verification of the bound commit + diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DiffFacts:
    diff_bytes: bytes
    diff_digest: str
    files: tuple[dict[str, Any], ...]
    file_count: int


def _committed_diff(runner: GitRunner, worktree_path: str, base_sha: str,
                    commit_sha: str, claims: Iterable[str]) -> _DiffFacts:
    """Prove and capture the COMPLETE base→commit diff, fail-closed.

    * exactly ONE commit above base, whose parent is the verified base
      (amend/rebase/merge or a wrong base is drift — refused);
    * every committed path (incl. both rename endpoints) falls inside the
      owned claims;
    * the raw ``git diff --binary`` output is the complete diff artifact; its
      digest is the diff digest, size-capped (never truncated silently).
    """
    if commit_sha == base_sha:
        raise DeliveryRefusal("empty",
                             "commit and base are identical; there is nothing to deliver")
    parent = _try_git(runner, ["rev-parse", "--verify", f"{commit_sha}^"],
                     worktree_path)
    if parent is None or parent != base_sha:
        raise DeliveryRefusal(
            "drift",
            f"commit {commit_sha[:12]} is not a direct child of the verified "
            f"base {base_sha[:12]} (parent={parent!r}); amend/rebase/merge or "
            "a wrong base is refused")
    merge_base = _try_git(runner, ["merge-base", base_sha, commit_sha],
                         worktree_path)
    if merge_base != base_sha:
        raise DeliveryRefusal(
            "drift",
            f"merge-base of {base_sha[:12]} and {commit_sha[:12]} is "
            f"{merge_base!r}, not the verified base")
    count_raw = _try_git(runner, ["rev-list", "--count",
                                  f"{base_sha}..{commit_sha}"], worktree_path)
    if count_raw is None or not count_raw.isdigit() or int(count_raw) != 1:
        raise DeliveryRefusal(
            "drift",
            f"base..commit contains {count_raw!r} commits; local-only delivery "
            "accepts exactly one commit per Attempt")
    raw_names = runner(["diff", "--name-status", "-z", "-M",
                        f"{base_sha}..{commit_sha}"], worktree_path)
    sink = workspaces._ChangeAccumulator()
    # A committed range is neither index nor worktree state; ``staged=True``
    # only selects which flag column the parser fills — paths and rename
    # endpoints are identical either way, and only those feed ownership.
    workspaces._parse_name_status_z(raw_names, sink, staged=True)
    entries = sink.entries()
    claim_objs = workspaces.normalize_claims(claims)
    violations = workspaces.evaluate_ownership([e.path for e in entries],
                                               claim_objs)
    if violations:
        preview = ", ".join(violations[:5])
        raise DeliveryRefusal(
            "unowned",
            f"committed paths fall outside Ownership "
            f"({len(violations)}): {preview}")
    diff = runner(["diff", "--binary", f"{base_sha}..{commit_sha}"],
                 worktree_path)
    if not diff or not entries:
        raise DeliveryRefusal("empty",
                             "base-to-commit diff is empty; there is nothing to deliver")
    if len(diff) > MAX_DELIVERY_DIFF_BYTES:
        raise DeliveryRefusal(
            "size",
            f"complete diff is {len(diff)} bytes, exceeding the "
            f"{MAX_DELIVERY_DIFF_BYTES}-byte review cap; refusing a "
            "partial/truncated delivery digest")
    files = tuple({
        "path": workspaces._display(e.path),
        "added": e.added, "deleted": e.deleted, "renamed": e.renamed,
        "rename_source": (workspaces._display(e.rename_source)
                         if e.rename_source else None),
        "rename_target": (workspaces._display(e.rename_target)
                         if e.rename_target else None),
    } for e in entries[:_MAX_EMBEDDED_FILES])
    return _DiffFacts(diff_bytes=diff, diff_digest=_sha256_hex(
        _canonical_json({"diff": diff.hex()})), files=files,
        file_count=len(entries))


def _ensure_diff_artifact(root_real: str, relpath: str,
                         diff: _DiffFacts) -> None:
    """Persist the COMPLETE diff exactly once.

    ``_write_artifact`` is O_EXCL: a lost-receipt replay that re-forms
    identical ship evidence finds its own file already present and converges
    (byte-identical content re-proves the same digest); a FOREIGN file at
    that deterministic path fails closed — nothing is ever overwritten.
    """
    try:
        verification._write_artifact(root_real, relpath, diff.diff_bytes)
        return
    except FileExistsError:
        pass
    target = verification._contained_artifact_path(root_real, relpath)
    try:
        with open(target, "rb") as handle:
            existing = handle.read()
    except OSError as exc:
        raise StorageError(
            f"diff artifact {relpath!r} exists but is unreadable: {exc}") from exc
    if _sha256_hex(_canonical_json({"diff": existing.hex()})) != diff.diff_digest:
        raise StorageError(
            f"diff artifact {relpath!r} already exists with different content;"
            " refusing to overwrite or reuse a foreign artifact")


def _verify_workspace_binding(conn: TeamCraftConnection, attempt_id: str,
                             allocation: worktrees.WorktreeAllocation,
                             runner: GitRunner, *,
                             expect_commit_sha: str | None = None,
                             require_clean: bool) -> list[str]:
    """Non-raising aggregate of live identity/base/HEAD/clean checks."""
    problems: list[str] = []
    audited = _AuditedRunner(runner)
    try:
        worktrees.claim_worktree(conn, attempt_id, run_git=audited)
    except (worktrees.WorktreeIdentityError, workspaces.GitStateError,
            StorageError, NotFoundError) as exc:
        problems.append(f"identity: {exc}")
        return problems
    try:
        state = workspaces.collect_workspace_state(allocation.worktree_path,
                                                  run_git=audited)
    except workspaces.GitStateError as exc:
        problems.append(f"git-state: {exc}")
        return problems
    if state.branch != allocation.branch:
        problems.append(f"branch: live {state.branch!r} != recorded "
                       f"{allocation.branch!r}")
    if expect_commit_sha is None:
        if state.base_sha != allocation.base_sha:
            problems.append(f"base: live HEAD {state.base_sha!r} != recorded "
                           f"base {allocation.base_sha!r}")
    else:
        if state.base_sha != expect_commit_sha:
            problems.append(f"head: live HEAD {state.base_sha!r} != bound "
                           f"commit {expect_commit_sha!r} (the Commit moved — "
                           "amend/rebase/additional commit is drift)")
    if require_clean and state.changes:
        preview = ", ".join(entry.path for entry in state.changes[:5])
        problems.append(f"dirty: {len(state.changes)} uncommitted change(s) "
                       f"[{preview}]")
    return problems


# ---------------------------------------------------------------------------
# Shared fact gathering
# ---------------------------------------------------------------------------


def _bound_attempt_facts(conn: TeamCraftConnection, attempt_id: str):
    """Task/Attempt/Brief/WorkerReport pins (worker finish only *triggers*
    delivery — it never proves it; Task must sit in VERIFYING)."""
    task, attempt, brief, report_id, dispatch_task_version, report_digest = \
        verification._bound_facts(conn, attempt_id)
    if task.kind != "ship":
        raise DeliveryRefusal(
            "mode",
            f"task {task.id} is kind {task.kind!r}; local delivery applies to "
            "ship Tasks (scouts deliver reports via M2-06)")
    return task, attempt, brief, report_id, dispatch_task_version, report_digest


def _policy_of(task: domain.Task) -> dict[str, Any]:
    try:
        policy = json.loads(task.execution_policy_json or "{}")
    except (TypeError, ValueError):
        policy = None
    return policy if isinstance(policy, dict) else {}


def _policy_digest(policy: dict[str, Any]) -> str:
    return _sha256_hex(_canonical_json(policy))


def _bound_allocation(conn: TeamCraftConnection, task: domain.Task,
                     attempt: domain.Attempt) -> worktrees.WorktreeAllocation:
    allocation = worktrees.require_allocation(conn, attempt.id)
    if (allocation.task_id != task.id or allocation.attempt_id != attempt.id
            or allocation.project_id != task.project_id):
        raise ConflictError("worktree allocation binds a different Task/Attempt/Project")
    derived = worktrees.worktree_branch(task.id, attempt.id)
    if allocation.branch != derived:
        raise ConflictError(
            f"allocation branch {allocation.branch!r} is not the derived "
            f"Attempt branch {derived!r}")
    _refuse_protected_branch(allocation.branch)
    return allocation


def _evidence_envelope(task: domain.Task, attempt: domain.Attempt,
                       dispatch_task_version: int, digest: str) -> str:
    return _canonical_json({
        "project_id": task.project_id, "task_id": task.id,
        "task_version": task.version, "attempt_id": attempt.id,
        "attempt_no": attempt.attempt_no, "digest": digest})


def _replay_by_key(conn: TeamCraftConnection, kind: str, command_kind: str,
                   idempotency_key: str) -> Any:
    return conn.execute(
        "SELECT e.seq, e.event_id, e.payload_json FROM events e"
        " JOIN commands c ON c.id = json_extract(e.payload_json, '$.command_id')"
        " WHERE e.kind = ? AND c.kind = ? AND c.idempotency_key = ?",
        (kind, command_kind, idempotency_key),
    ).fetchone()


def _unresolved_other_key(conn: TeamCraftConnection, command_kind: str,
                         attempt_id: str, idempotency_key: str,
                         extra: dict[str, Any] | None = None) -> None:
    rows = conn.execute(
        "SELECT id, idempotency_key, payload_json FROM commands WHERE kind = ?"
        " AND state IN ('pending','running','unknown') ORDER BY requested_at",
        (command_kind,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row[2]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConflictError(
                f"an unresolved {command_kind} Command has malformed payload") from exc
        if (isinstance(payload, dict) and payload.get("attempt_id") == attempt_id
                and str(row[1]) != idempotency_key
                and all(payload.get(k) == v for k, v in (extra or {}).items())):
            raise ConflictError(
                f"this Attempt has an unresolved {command_kind} Command"
                f" ({row[0]}); inspect/reconcile it before another attempt"
                " (never run the same effect twice)")


def _unresolved_same_key(conn: TeamCraftConnection, command_kind: str,
                         idempotency_key: str) -> None:
    """Refuse a second executor for one already claimed external effect.

    A settled evidence row is handled by ``_replay_by_key`` before this check.
    Therefore a remaining Command with the same key is an in-flight or
    uncertain effect, and executing it again would turn idempotency into a
    duplicate Git commit.  Recovery must inspect/reconcile that Command.
    """
    row = conn.execute(
        "SELECT id, state FROM commands WHERE kind = ? AND idempotency_key = ?",
        (command_kind, idempotency_key),
    ).fetchone()
    if row is not None:
        raise IllegalTransitionError(
            f"{command_kind} Command {row[0]} for this idempotency key is"
            f" {row[1]!r} without settled evidence; inspect/reconcile it"
            " before retry (never execute the same effect twice)",
            entity="command", current=str(row[1]), event="delivery_replay",
        )


# ---------------------------------------------------------------------------
# COMMIT — fenced, idempotent commit creation with durable Command first
# ---------------------------------------------------------------------------


def _commit_event_id(attempt_id: str) -> str:
    return "evt-dce-" + _sha256_hex(f"delivery-commit|{attempt_id}")[:40]


def _ship_event_id(attempt_id: str) -> str:
    return "evt-dse-" + _sha256_hex(f"delivery-ship|{attempt_id}")[:40]


def _approval_event_id(ship_evidence_id: str) -> str:
    return "evt-dap-" + _sha256_hex(f"delivery-approval|{ship_evidence_id}")[:40]


def _persist_commit_evidence(conn: TeamCraftConnection, *, attempt: domain.Attempt,
                             task: domain.Task, brief: Any, report_id: int,
                             dispatch_task_version: int, report_digest: str,
                             allocation: worktrees.WorktreeAllocation,
                             diff: _DiffFacts, commit_sha: str, source: str,
                             command_id: str, clock: Clock | None) -> CommitEvidence:
    """Record the immutable commit evidence + mirror the SHA onto the Attempt.

    Must run inside the caller's transaction; the payload is deterministic
    (no clock) so a concurrent/racing writer converges onto the SAME event
    row instead of duplicating the record.
    """
    payload = _commit_payload(
        allocation_id=allocation.allocation_id, attempt_id=attempt.id,
        attempt_no=attempt.attempt_no, base_sha=allocation.base_sha,
        branch=allocation.branch, brief_digest=brief.brief_digest,
        brief_version=brief.brief_version, claims=list(allocation.claims),
        claims_digest=allocation.claims_digest, commit_sha=commit_sha,
        command_id=command_id, dispatch_task_version=dispatch_task_version,
        diff_digest=diff.diff_digest,
        file_count=diff.file_count, files=[dict(f) for f in diff.files],
        files_truncated=diff.file_count > _MAX_EMBEDDED_FILES,
        parent_sha=allocation.base_sha, project_id=task.project_id,
        source=source, task_id=task.id, task_version=task.version,
        worker_report_digest=report_digest, worker_report_id=report_id,
        worktree_path=allocation.worktree_path,
    )
    event, _inserted = events.append_event_idempotent(
        conn, COMMIT_EVIDENCE_KIND, project_id=task.project_id,
        task_id=task.id, attempt_id=attempt.id, payload_json=payload,
        event_id=_commit_event_id(attempt.id), clock=clock)
    stored = _commit_from_row(conn, int(event.seq), event.event_id, payload)
    # Mirror the delivered SHA onto the Attempt row (resource fact, never a
    # status change).  CAS: bind once; identical replay ok; different SHA loud.
    cursor = conn.execute(
        "UPDATE attempts SET commit_sha = ? WHERE id = ? AND commit_sha IS NULL",
        (commit_sha, attempt.id))
    if cursor.rowcount == 0:
        current = conn.execute("SELECT commit_sha FROM attempts WHERE id = ?",
                               (attempt.id,)).fetchone()
        if current is None or str(current[0]) != commit_sha:
            raise ConflictError(
                f"attempt {attempt.id} already pins commit"
                f" {current!r}; refusing to re-point it to {commit_sha!r}")
    return stored


def create_local_commit(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    message: str | None = None,
    artifact_root: str | None = None,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> CommitEvidence:
    """Create (or adopt) THE commit for one finished Attempt's worktree.

    Order is pinned: validate every durable fact → durable Command
    (pending→running, committed) → Git effect (stage verified-owned changes,
    commit on the Attempt's own branch) → post-effect proof → fenced receipt.
    Replays:

    * same idempotency key, settled receipt  -> the stored evidence;
    * same key, interrupted after the Git effect -> reconciled adoption of
      the existing commit (never a second commit);
    * any key after a settled evidence -> the settled evidence (the worktree
      is clean; nothing is redone).

    Refusals write nothing and touch no Git state: not VERIFYING / missing
    WorkerReport (:class:`ValidationError` via the shared bound-facts gate),
    read-only/no allocation (``NotFoundError``), unowned or empty change set,
    base drift, foreign/ambiguous workspace, protected branch, missing git
    identity, oversized diff (:class:`DeliveryRefusal` kinds
    ``unowned``/``empty``/``drift``/``identity``/``main-branch``/``size``).
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    if message is not None and (not isinstance(message, str)
                               or not message.strip()
                               or len(message.encode("utf-8")) > 4096):
        raise ValidationError("commit message must be non-blank and at most 4096 bytes")
    key = idempotency_key.strip()
    runner = _AuditedRunner(run_git or _default_run_git)
    attempt_id = require_entity_id(attempt_id)

    with _tx(conn):
        # Replay first: an identical key already owns a durable receipt.
        replayed = _replay_by_key(conn, COMMIT_EVIDENCE_KIND,
                                  COMMIT_COMMAND_KIND, key)
        if replayed is not None:
            evidence = _commit_from_row(conn, int(replayed[0]), str(replayed[1]),
                                       str(replayed[2]))
            if evidence.attempt_id != attempt_id:
                raise ConflictError(
                    "idempotency key already owns commit evidence for a"
                    " different Attempt; refusing the collision")
            leader.require_project_lease(conn, lease, evidence.project_id,
                                        clock=clock)
            return evidence

        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            _bound_attempt_facts(conn, attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        allocation = _bound_allocation(conn, task, attempt)
        policy = _policy_of(task)
        if policy.get("delivery") == "report_only":
            raise DeliveryRefusal(
                "mode",
                "task policy is report_only; there is nothing to commit — "
                "local delivery requires delivery=local_only (or a later "
                "direct_pr/ship flow)")

        existing = load_commit_evidence(conn, attempt.id)
        if existing is not None:
            # Settled: the commit happened once, ever.  Any key converges.
            if existing.commit_sha == existing.base_sha:
                raise StorageError("settled commit evidence is inconsistent")
            return existing

        _unresolved_same_key(conn, COMMIT_COMMAND_KIND, key)
        _unresolved_other_key(conn, COMMIT_COMMAND_KIND, attempt.id, key)

        # ---- pre-effect live checks (fail closed, zero residue) ----
        problems = _verify_workspace_binding(conn, attempt.id, allocation,
                                            runner, require_clean=False)
        if problems:
            raise DeliveryRefusal("identity" if "identity" in problems[0]
                                 else "drift", "; ".join(problems))
        gate = workspaces.run_diff_gate(allocation.worktree_path,
                                       allocation.claims, run_git=runner)
        if not gate.ok:
            preview = ", ".join(gate.violations[:5])
            raise DeliveryRefusal(
                "unowned",
                f"full ownership Diff Gate failed before commit "
                f"({gate.violation_count if hasattr(gate, 'violation_count') else len(gate.violations)}"
                f"): {preview}")
        if not gate.changes:
            raise DeliveryRefusal(
                "empty",
                "workspace holds no uncommitted owned changes; a local-only "
                "delivery must carry at least one deliverable change")
        author_name = _try_git(runner, ["config", "--get", "user.name"],
                               allocation.worktree_path)
        author_email = _try_git(runner, ["config", "--get", "user.email"],
                                allocation.worktree_path)
        if (not author_name or not author_email
                or _try_git(runner, ["var", "GIT_AUTHOR_IDENT"],
                            allocation.worktree_path) is None):
            raise DeliveryRefusal(
                "identity",
                "git author name/email are not configured in the project "
                "repository; refusing to fabricate a commit identity")

        payload = {"project_id": task.project_id, "task_id": task.id,
                   "task_version": task.version, "attempt_id": attempt.id,
                   "attempt_no": attempt.attempt_no,
                   "dispatch_task_version": dispatch_ver,
                   "brief_digest": brief.brief_digest,
                   "worker_report_id": report_id,
                   "worker_report_digest": report_digest,
                   "allocation_id": allocation.allocation_id,
                   "worktree_path": allocation.worktree_path,
                   "branch": allocation.branch, "base_sha": allocation.base_sha,
                   "claims_digest": allocation.claims_digest,
                   "message": message or ""}
        digest = _sha256_hex(_canonical_json({
            "brief_digest": brief.brief_digest, "attempt_id": attempt.id,
            "allocation_id": allocation.allocation_id,
            "worker_report_digest": report_digest,
            "config": policy, "base_sha": allocation.base_sha}))
        command = commands.submit_command(
            conn, idempotency_key=key, kind=COMMIT_COMMAND_KIND,
            payload_json=_canonical_json(payload),
            evidence_json=_evidence_envelope(task, attempt, dispatch_ver, digest),
            clock=clock).command
        raced = load_commit_evidence(conn, attempt.id)
        if raced is not None:
            return raced
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "Commit Command is already running/uncertain; reconcile it "
                "before retry", entity="command", current=command.state,
                event="local_commit_run")

    # ---- outside any transaction: the Git effect ----
    worktree = allocation.worktree_path
    commit_argv = ["commit", "-m", message or
                   f"[teamcraft] deliver {task.id} attempt {attempt.id}"]
    staged_sha = _try_git(runner, ["rev-parse", "--verify", "HEAD"], worktree)
    if staged_sha != allocation.base_sha:
        # The world moved between fence and effect; refuse, keep evidence of
        # the mismatch for reconcile, change nothing.
        raise DeliveryRefusal(
            "drift",
            f"worktree HEAD moved to {staged_sha!r} before staging; expected "
            f"base {allocation.base_sha!r}")
    runner(["add", "-A"], worktree)
    try:
        runner(commit_argv, worktree)
    except workspaces.GitStateError as exc:
        # The commit attempt failed.  Whether anything landed is decided by
        # reconcile — do NOT guess here, and never delete anything.
        landed = _try_git(runner, ["rev-parse", "--verify", "HEAD"], worktree)
        if landed is not None and landed != allocation.base_sha:
            raise StorageError(
                f"commit command for attempt {attempt.id} was durably recorded"
                f" and a commit ({landed[:12]}) landed, but the receipt failed:"
                f" {exc}; run reconcile_commit_receipt to adopt or expire it")
        raise StorageError(
            f"commit command for attempt {attempt.id} was durably recorded"
            f" but 'git commit' failed ({exc}); reconcile the Command once"
            " its real effect is established") from exc

    # ---- post-effect proof (fail-closed; receipt only for a proven commit) ----
    commit_sha = _try_git(runner, ["rev-parse", "--verify", "HEAD"], worktree)
    if commit_sha is None:
        raise StorageError(
            f"could not read HEAD after commit for attempt {attempt.id};"
            " reconcile the recorded Command")
    branch_now = _try_git(runner, ["symbolic-ref", "--short", "-q", "HEAD"],
                         worktree)
    if branch_now != allocation.branch:
        raise DeliveryRefusal(
            "drift",
            f"worktree is on branch {branch_now!r} after commit; expected "
            f"{allocation.branch!r}")
    diff = _committed_diff(runner, worktree, allocation.base_sha, commit_sha,
                          allocation.claims)
    try:
        status_after = runner(["status", "--porcelain=v1", "-z",
                              "--untracked-files=all", "--renames",
                              "--ignore-submodules=none"], worktree)
    except workspaces.GitStateError as exc:
        raise StorageError(f"could not read worktree status after commit: {exc}")
    leftover = workspaces._ChangeAccumulator()
    workspaces._parse_status_z(status_after, leftover)
    if leftover.entries():
        raise StorageError(
            "worktree still holds uncommitted changes after commit; the "
            "delivery staging was incomplete — preserved for inspection")

    # ---- fenced receipt ----
    try:
        with _tx(conn):
            leader.require_project_lease(conn, lease, task.project_id, clock=clock)
            current_task = domain.get_task(conn, task.id)
            current_attempt = domain.get_attempt(conn, attempt.id)
            current_brief = verification.briefs.get_attempt_brief(conn, attempt.id)
            report_row = conn.execute(
                "SELECT id, report_digest, outcome FROM worker_reports WHERE"
                " attempt_id = ? AND report_kind = 'final' ORDER BY id DESC LIMIT 1",
                (attempt.id,),
            ).fetchone()
            current_alloc = worktrees.require_allocation(conn, attempt.id)
            fresh = (current_task.version == task.version
                    and current_task.status == "verifying"
                    and current_attempt.status == "finished"
                    and current_attempt.attempt_no == attempt.attempt_no
                    and current_brief.brief_digest == brief.brief_digest
                    and current_brief.task_version == dispatch_ver
                    and report_row is not None and int(report_row[0]) == report_id
                    and str(report_row[1]) == report_digest
                    and str(report_row[2]) == "completed"
                    and current_alloc.allocation_id == allocation.allocation_id)
            if not fresh:
                raise ConflictError(
                    "commit facts drifted while the commit ran; refusing to"
                    " record the receipt (inspect the workspace and reconcile)")
            evidence = _persist_commit_evidence(
                conn, attempt=attempt, task=current_task, brief=current_brief,
                report_id=report_id, dispatch_task_version=dispatch_ver,
                report_digest=report_digest, allocation=allocation, diff=diff,
                commit_sha=commit_sha, source="created", command_id=command.id,
                clock=clock)
            events.append_event(conn, "delivery.commit_recorded",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                payload_json=_canonical_json({
                    "commit_evidence_id": evidence.event_id,
                    "command_id": command.id, "commit_sha": commit_sha,
                    "base_sha": allocation.base_sha, "branch": allocation.branch,
                    "diff_digest": diff.diff_digest, "file_count": diff.file_count,
                    "source": "created"}),
                clock=clock)
            commands.confirm_command(conn, command.id, result_json=_canonical_json({
                "commit_evidence_id": evidence.event_id, "commit_sha": commit_sha,
                "diff_digest": diff.diff_digest, "file_count": diff.file_count}),
                clock=clock)
            return evidence
    except Exception as exc:
        detail = str(exc).encode("utf-8")[:500].decode("utf-8", errors="ignore")
        try:
            commands.mark_command_unknown(
                conn, command.id,
                note=f"commit effect may have landed but receipt failed: {detail}",
                clock=clock)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Reconcile a lost commit receipt (Git + Attempt/Session/Event facts)
# ---------------------------------------------------------------------------


def reconcile_commit_receipt(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    lease: leader.Lease,
    actor: str,
    reason: str,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Close an interrupted Commit Command against observed Git facts.

    * a commit exactly on top of the verified base in THIS Attempt's own,
      identity-verified, now-clean worktree is ADOPTED as the durable
      evidence (``source=reconciled``) — Git proves the effect, so no second
      commit is ever produced;
    * a worktree still sitting exactly on the base proves nothing happened:
      the Command expires and a fresh create may follow under a new key;
    * anything else (multiple commits, foreign parent, dirty scene, foreign
      identity, missing worker/attempt facts, already-reconciled) fails
      closed and leaves the Command uncertain for further investigation.
    """
    if not isinstance(actor, str) or not actor.strip() or len(actor.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES:
        raise ValidationError(f"actor is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES:
        raise ValidationError(f"reason is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    runner = _AuditedRunner(run_git or _default_run_git)
    with _tx(conn):
        command = commands.get_command(conn, command_id)
        if command.kind != COMMIT_COMMAND_KIND:
            raise IllegalTransitionError(
                "command is not a local commit run", entity="command",
                current=command.state, event="local_commit_reconcile")
        try:
            payload = json.loads(command.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("commit Command payload is malformed") from exc
        attempt_id = payload.get("attempt_id") if isinstance(payload, dict) else None
        project_id = payload.get("project_id") if isinstance(payload, dict) else None
        if not attempt_id or not project_id:
            raise ValidationError(
                "commit reconcile requires an attempt- and project-scoped Command")
        leader.require_project_lease(conn, lease, str(project_id), clock=clock)
        existing = conn.execute(
            "SELECT event_id FROM events WHERE kind = ? AND json_extract("
            "payload_json, '$.command_id') = ?",
            (COMMIT_EVIDENCE_KIND, command.id),
        ).fetchone()
        if existing is not None:
            raise ConflictError(
                "this Command already owns durable commit evidence; accepted"
                " results are immutable and must not be reconciled away")
        if command.state in ("confirmed", "expired"):
            return {"resolution": command.state, "command": command,
                    "commit_evidence": None}
        if command.state == "pending":
            raise IllegalTransitionError(
                "command was never claimed/executed; expire it via reconcile"
                " only from running/unknown", entity="command",
                current=command.state, event="local_commit_reconcile")

        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            _bound_attempt_facts(conn, str(attempt_id))
        allocation = _bound_allocation(conn, task, attempt)
        # Reconcile judges what Git ACTUALLY shows: HEAD may legitimately sit
        # on the base (nothing landed -> expire) or on the one delivered commit
        # (effect landed -> adopt).  Binding checks therefore pin against the
        # OBSERVED head; dirt is judged on the adoption path alone (an
        # interrupted staging scene must still be expirable).
        head = _try_git(runner, ["rev-parse", "--verify", "HEAD"],
                        allocation.worktree_path)
        if head is None:
            raise StorageError("cannot read worktree HEAD during reconcile")
        problems = _verify_workspace_binding(conn, attempt.id, allocation,
                                            runner, expect_commit_sha=head,
                                            require_clean=False)
        if problems:
            raise DeliveryRefusal("identity",
                                 "reconcile refused: " + "; ".join(problems))
        if head == allocation.base_sha:
            note = (f"commit receipt reconciled not-landed by {actor.strip()}:"
                   f" {reason.strip()} (HEAD still at verified base)")[:1900]
            settled = commands.reconcile_command(conn, command.id,
                                                outcome="expired", note=note,
                                                clock=clock)
            events.append_event(conn, "delivery.commit_reconciled",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                payload_json=_canonical_json({
                    "command_id": command.id, "resolution": "expired",
                    "actor": actor.strip(),
                    "fact": "HEAD still equals the verified base; no commit"
                           " effect happened"}),
                clock=clock)
            return {"resolution": "expired", "command": settled,
                    "commit_evidence": None}

        # A commit exists above the base: adoption requires worker/attempt/
        # session/event facts tying THIS attempt to the effect.
        finished_fact = conn.execute(
            "SELECT seq FROM events WHERE kind = 'attempt.execution_finished'"
            " AND attempt_id = ? LIMIT 1", (attempt.id,),
        ).fetchone()
        claimed_fact = conn.execute(
            "SELECT seq FROM events WHERE kind = 'command.claimed'"
            " AND json_extract(payload_json, '$.command_id') = ? LIMIT 1",
            (command.id,),
        ).fetchone()
        session_fact = conn.execute(
            "SELECT id, backend, native_session_id, pane_id, state"
            " FROM sessions WHERE attempt_id = ?"
            " AND native_session_id IS NOT NULL"
            " ORDER BY created_at ASC LIMIT 1", (attempt.id,),
        ).fetchone()
        if (finished_fact is None or claimed_fact is None
                or session_fact is None or not str(session_fact[1]).strip()
                or not str(session_fact[2]).strip()):
            raise DeliveryRefusal(
                "ambiguous",
                "cannot tie the existing commit to this Attempt's worker run"
                " (native Session/execution-finished/claim event facts"
                " incomplete);"
                " refusing to adopt an ambiguous commit")
        diff = _committed_diff(runner, allocation.worktree_path,
                              allocation.base_sha, head, allocation.claims)
        leftover_raw = runner(["status", "--porcelain=v1", "-z",
                               "--untracked-files=all", "--renames",
                               "--ignore-submodules=none"], allocation.worktree_path)
        leftover = workspaces._ChangeAccumulator()
        workspaces._parse_status_z(leftover_raw, leftover)
        if leftover.entries():
            raise DeliveryRefusal(
                "dirty",
                "worktree holds additional uncommitted changes beside the"
                f" existing commit ({len(leftover.entries())}); the scene is"
                " preserved and ambiguous recovery is refused")
        evidence = _persist_commit_evidence(
            conn, attempt=attempt, task=task, brief=brief,
            report_id=report_id, dispatch_task_version=dispatch_ver,
            report_digest=report_digest, allocation=allocation, diff=diff,
            commit_sha=head, source="reconciled", command_id=command.id,
            clock=clock)
        note = (f"commit receipt reconciled adopted by {actor.strip()}:"
               f" {reason.strip()}")[:1900]
        settled = commands.reconcile_command(
            conn, command.id, outcome="confirmed", note=note,
            result_json=_canonical_json({
                "commit_evidence_id": evidence.event_id, "commit_sha": head,
                "diff_digest": diff.diff_digest,
                "file_count": diff.file_count, "source": "reconciled"}),
            clock=clock)
        events.append_event(conn, "delivery.commit_reconciled",
            project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
            payload_json=_canonical_json({
                "command_id": command.id, "resolution": "adopted",
                "commit_evidence_id": evidence.event_id,
                "commit_sha": head, "diff_digest": diff.diff_digest,
                "actor": actor.strip(),
                "attempt_status": attempt.status,
                "session_fact": [str(session_fact[0]), str(session_fact[1]),
                                 str(session_fact[2]),
                                 (str(session_fact[3])
                                  if session_fact[3] is not None else None),
                                 str(session_fact[4])],
                "facts": ["git commit directly on verified base",
                         "attempt finished with WorkerReport",
                         "attempt.execution_finished event",
                         "command.claimed event"]}),
            clock=clock)
        return {"resolution": "adopted", "command": settled,
                "commit_evidence": evidence}


# ---------------------------------------------------------------------------
# SHIP evidence — immutable binding of the full delivery chain
# ---------------------------------------------------------------------------


def _required_verifier_evidence(conn: TeamCraftConnection, task: domain.Task,
                                attempt: domain.Attempt, artifact_root: str | None,
                                ) -> tuple[list[dict[str, Any]], dict[str, tuple[int, str]]]:
    """Prove every required M4-03 profile passed with current config."""
    rows = verification.list_worktree_verifications(conn, attempt.id)
    for row in rows:
        if row.status in ("fail", "error", "stale"):
            raise DeliveryRefusal(
                "verifier",
                f"worktree verification {row.id} for profile {row.profile!r}"
                f" ended {row.status!r}; failing/errored/stale verifier"
                " evidence can never ship (a fix must arrive as a new bounded"
                " Attempt)")
    required = verification.required_profile_snapshot(conn, task.project_id)
    if not required:
        raise DeliveryRefusal(
            "verifier",
            "project has no approved-and-enabled verifier profiles; a missing"
            " verifier cannot verify anything and local-only acceptance is"
            " refused fail-closed")
    by_profile: dict[str, verification.WorktreeVerification] = {}
    for row in rows:
        if row.status == "pass":
            by_profile.setdefault(row.profile, row)
    entries: list[dict[str, Any]] = []
    for profile, (version, config_digest) in sorted(required.items()):
        passing = by_profile.get(profile)
        if passing is None:
            raise DeliveryRefusal(
                "verifier",
                f"required verifier profile {profile!r} has no PASS result for"
                " this Attempt (missing/failed verifier fails closed)")
        if (passing.profile_version != version
                or passing.config_digest != config_digest):
            raise DeliveryRefusal(
                "verifier",
                f"profile {profile!r} passed under version"
                f" {passing.profile_version}/{passing.config_digest} but the"
                f" current approved config is {version}/{config_digest};"
                " stale config evidence cannot ship")
        if not verification.worktree_verification_is_current(conn, passing):
            raise DeliveryRefusal(
                "verifier",
                f"profile {profile!r} verdict is no longer current against"
                " live Task/Brief/WorkerReport/allocation facts")
        if passing.log_relpath is not None and passing.log_digest is not None:
            root = (artifact_root if artifact_root is not None
                    else verification.default_artifact_root(
                        task_repo_root(conn, task)))
            try:
                artifact = verification.read_worktree_artifact(
                    conn, passing.id, artifact_root=root)
            except (NotFoundError, StorageError) as exc:
                raise DeliveryRefusal(
                    "verifier",
                    f"persisted verifier artifact for profile {profile!r} is"
                    f" unreadable or tampered: {exc}") from exc
            if artifact["log_digest"] != passing.log_digest:
                raise DeliveryRefusal(
                    "verifier",
                    f"persisted verifier artifact digest mismatch for profile"
                    f" {profile!r}")
        entries.append({
            "profile": profile, "profile_version": version,
            "config_digest": config_digest,
            "verification_id": passing.id,
            "report_digest": passing.report_digest,
            "log_digest": passing.log_digest,
            "exit_code": passing.exit_code,
            "status": "pass",
        })
    return entries, required


def task_repo_root(conn: TeamCraftConnection, task: domain.Task) -> str:
    return domain.get_project(conn, task.project_id).repo_path


def allocation_root(conn: TeamCraftConnection, task: domain.Task) -> str:
    return task_repo_root(conn, task)


def record_ship_evidence(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> ShipEvidence:
    """Bind the complete, immutable ship Verification evidence for a
    local-only Task and PROPOSE ``accept_local_commit``.

    The evidence pins: Task/Attempt/Brief versions + the WorkerReport
    (id+digest), the allocation/base/branch identity, the Commit SHA, the
    COMPLETE base-to-Commit diff digest plus its artifact file, the passing
    verifier profile/config bindings (current versions), and the canonical
    execution-policy digest.  Refusals (nothing written):

    * not VERIFYING / no completed final WorkerReport / scout kind;
    * delivery mode not ``local_only`` (report_only delivers nothing;
      direct_pr/ship wait for M4-05/M4-06);
    * no durable commit evidence (:class:`NotFoundError`);
    * verifier missing/failed/stale/config-drift; commit/base/branch drift;
      worktree dirty; unowned committed paths; oversized diff.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    key = idempotency_key.strip()
    runner = _AuditedRunner(run_git or _default_run_git)
    attempt_id = require_entity_id(attempt_id)

    with _tx(conn):
        replayed = _replay_by_key(conn, SHIP_EVIDENCE_KIND, SHIP_COMMAND_KIND, key)
        if replayed is not None:
            evidence = _ship_from_row(conn, int(replayed[0]), str(replayed[1]),
                                     str(replayed[2]))
            if evidence.attempt_id != attempt_id:
                raise ConflictError(
                    "idempotency key already owns ship evidence for a"
                    " different Attempt; refusing the collision")
            leader.require_project_lease(conn, lease, evidence.project_id,
                                        clock=clock)
            return evidence

        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            _bound_attempt_facts(conn, attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        allocation = _bound_allocation(conn, task, attempt)
        policy = _policy_of(task)
        if policy.get("delivery") != "local_only":
            raise DeliveryRefusal(
                "mode",
                f"task policy delivery is {policy.get('delivery')!r};"
                " ship evidence is formed only for delivery=local_only"
                " (report_only needs no commit; direct_pr/ship wait for"
                " M4-05/M4-06)")
        existing = load_ship_evidence(conn, attempt.id)
        if existing is not None:
            return existing
        _unresolved_other_key(conn, SHIP_COMMAND_KIND, attempt.id, key)
        commit = require_commit_evidence(conn, attempt.id)
        if (commit.task_id != task.id or commit.attempt_no != attempt.attempt_no
                or commit.task_version != task.version
                or commit.brief_digest != brief.brief_digest
                or commit.worker_report_id != report_id
                or commit.worker_report_digest != report_digest
                or commit.allocation_id != allocation.allocation_id):
            raise ConflictError(
                "commit evidence bindings no longer match the live Task/"
                "Attempt/Brief/WorkerReport facts")
        verifiers, required = _required_verifier_evidence(conn, task, attempt,
                                                          None)
        policy_digest = _policy_digest(policy)
        payload_bindings = {
            "project_id": task.project_id, "task_id": task.id,
            "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "dispatch_task_version": dispatch_ver,
            "brief_digest": brief.brief_digest, "brief_version": brief.brief_version,
            "worker_report_id": report_id, "worker_report_digest": report_digest,
            "delivery_mode": "local_only", "proposed_action": LOCAL_ONLY_ACCEPT_ACTION,
            "allocation_id": allocation.allocation_id,
            "worktree_path": allocation.worktree_path, "branch": allocation.branch,
            "base_sha": allocation.base_sha, "commit_sha": commit.commit_sha,
            "commit_evidence_id": commit.event_id,
            "policy_digest": policy_digest,
        }
        # The Command durably pins the bindings BEFORE the live diff/artifact
        # proof; diff/artifact fields are finalized into the evidence payload
        # only after Git proves them (see final_payload below).
        digest = _sha256_hex(_canonical_json({
            "brief_digest": brief.brief_digest,
            "commit_sha": commit.commit_sha,
            "diff_digest": commit.diff_digest,
            "verifier_digest": _verifier_digest(verifiers),
            "allocation_id": allocation.allocation_id,
            "worker_report_digest": report_digest,
            "policy_digest": policy_digest}))
        command = commands.submit_command(
            conn, idempotency_key=key, kind=SHIP_COMMAND_KIND,
            payload_json=_canonical_json(payload_bindings),
            evidence_json=_evidence_envelope(task, attempt, dispatch_ver, digest),
            clock=clock).command
        raced = load_ship_evidence(conn, attempt.id)
        if raced is not None:
            return raced
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "Ship evidence Command is already running/uncertain; reconcile"
                " it before retry", entity="command", current=command.state,
                event="ship_evidence_record")

    # ---- outside the transaction: live proof of commit + complete diff ----
    problems = _verify_workspace_binding(conn, attempt.id, allocation, runner,
                                        expect_commit_sha=commit.commit_sha,
                                        require_clean=True)
    if problems:
        raise DeliveryRefusal(
            "drift", "ship evidence refused: " + "; ".join(problems))
    diff = _committed_diff(runner, allocation.worktree_path,
                          allocation.base_sha, commit.commit_sha,
                          allocation.claims)
    if diff.diff_digest != commit.diff_digest:
        raise DeliveryRefusal(
            "drift",
            "the base-to-commit diff recomputed live no longer matches the"
            " recorded diff digest; the delivered content changed")

    # ---- durable artifact: the COMPLETE diff, contained and digest-pinned --
    root = verification._ensure_artifact_root(
        default_delivery_artifact_root(task_repo_root(conn, task)))
    attempt_segment = _safe_segment("attempt_id", attempt.id)
    diff_relpath = "ship/{}/{}-{}.diff".format(
        attempt_segment, commit.commit_sha[:12], _ship_event_id(attempt.id)[-8:])
    _ensure_diff_artifact(root, diff_relpath, diff)

    try:
        with _tx(conn):
            leader.require_project_lease(conn, lease, task.project_id, clock=clock)
            current_task = domain.get_task(conn, task.id)
            current_attempt = domain.get_attempt(conn, attempt.id)
            current_brief = verification.briefs.get_attempt_brief(conn, attempt.id)
            report_row = conn.execute(
                "SELECT id, report_digest, outcome FROM worker_reports WHERE"
                " attempt_id = ? AND report_kind = 'final' ORDER BY id DESC LIMIT 1",
                (attempt.id,),
            ).fetchone()
            current_alloc = worktrees.require_allocation(conn, attempt.id)
            current_commit = require_commit_evidence(conn, attempt.id)
            current_policy = _policy_of(current_task)
            fresh = (current_task.version == task.version
                    and current_task.status == "verifying"
                    and current_task.kind == "ship"
                    and current_policy.get("delivery") == "local_only"
                    and _policy_digest(current_policy) == policy_digest
                    and current_attempt.status == "finished"
                    and current_attempt.attempt_no == attempt.attempt_no
                    and current_brief.brief_digest == brief.brief_digest
                    and current_brief.task_version == dispatch_ver
                    and report_row is not None and int(report_row[0]) == report_id
                    and str(report_row[1]) == report_digest
                    and str(report_row[2]) == "completed"
                    and current_alloc.allocation_id == allocation.allocation_id
                    and current_commit.event_id == commit.event_id
                    and current_commit.commit_sha == commit.commit_sha
                    and current_commit.diff_digest == commit.diff_digest)
            if not fresh:
                raise ConflictError(
                    "delivery facts drifted while the ship evidence was formed;"
                    " refusing to record it")
            verifiers_now, _ = _required_verifier_evidence(conn, current_task,
                                                           current_attempt, None)
            if _verifier_digest(verifiers_now) != _verifier_digest(verifiers):
                raise ConflictError(
                    "verifier profile configuration drifted while recording"
                    " ship evidence")
            payload_json = _canonical_json({**payload_bindings,
                "diff_digest": diff.diff_digest,
                "diff_bytes": len(diff.diff_bytes),
                "diff_relpath": diff_relpath,
                "file_count": diff.file_count,
                "verifiers_json": _canonical_json(verifiers_now),
                "verifier_digest": _verifier_digest(verifiers_now),
                "schema_version": DELIVERY_CONTRACT_VERSION,
                "command_id": command.id})
            final_payload = json.loads(payload_json)
            final_payload["evidence_digest"] = _payload_digest(final_payload)
            payload_json = _canonical_json(final_payload)
            event, _inserted = events.append_event_idempotent(
                conn, SHIP_EVIDENCE_KIND, project_id=task.project_id,
                task_id=task.id, attempt_id=attempt.id, payload_json=payload_json,
                event_id=_ship_event_id(attempt.id), clock=clock)
            evidence = _ship_from_row(conn, int(event.seq), event.event_id,
                                     payload_json)
            events.append_event(conn, "delivery.ship_evidence_recorded",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                payload_json=_canonical_json({
                    "ship_evidence_id": evidence.event_id,
                    "command_id": command.id, "commit_sha": evidence.commit_sha,
                    "branch": evidence.branch, "diff_digest": evidence.diff_digest,
                    "proposed_action": LOCAL_ONLY_ACCEPT_ACTION,
                    "delivery_mode": "local_only",
                    "next": "human Approval of exactly this Commit/branch is"
                           " required before DONE"}),
                clock=clock)
            commands.confirm_command(conn, command.id, result_json=_canonical_json({
                "ship_evidence_id": evidence.event_id,
                "evidence_digest": evidence.evidence_digest,
                "commit_sha": evidence.commit_sha,
                "diff_digest": evidence.diff_digest,
                "verifier_digest": evidence.verifier_digest}),
                clock=clock)
            return evidence
    except Exception as exc:
        detail = str(exc).encode("utf-8")[:500].decode("utf-8", errors="ignore")
        try:
            commands.mark_command_unknown(
                conn, command.id,
                note=f"ship evidence recording interrupted: {detail}", clock=clock)
        except Exception:
            pass
        raise


def read_ship_diff(conn: TeamCraftConnection, ship_evidence_id: str,
                  *, artifact_root: str | None = None) -> dict[str, Any]:
    """Load + verify the persisted COMPLETE diff artifact of ship evidence."""
    evidence = get_ship_evidence(conn, ship_evidence_id)
    root = artifact_root if artifact_root is not None else \
        default_delivery_artifact_root(task_repo_root(
            conn, domain.get_task(conn, evidence.task_id)))
    root_real = verification._ensure_artifact_root(root)
    target = verification._contained_artifact_path(root_real,
                                                  evidence.diff_relpath)
    if not os.path.isfile(target):
        raise NotFoundError(
            f"ship diff artifact is missing: {evidence.diff_relpath!r}")
    with open(target, "rb") as handle:
        content = handle.read()
    if len(content) != evidence.diff_bytes:
        raise StorageError("ship diff artifact size does not match the evidence")
    if _sha256_hex(_canonical_json({"diff": content.hex()})) != evidence.diff_digest:
        raise StorageError(
            "ship diff artifact digest mismatch; the artifact was tampered with")
    return {"ship_evidence_id": evidence.id, "commit_sha": evidence.commit_sha,
            "base_sha": evidence.base_sha, "branch": evidence.branch,
            "diff_relpath": evidence.diff_relpath,
            "diff_digest": evidence.diff_digest, "diff_bytes": len(content),
            "file_count": evidence.file_count,
            "proposed_action": evidence.proposed_action,
            "diff": content.decode("utf-8", errors="replace")}


# ---------------------------------------------------------------------------
# Approval — append-only acceptance of EXACTLY the bound Commit/branch
# ---------------------------------------------------------------------------


def approve_local_delivery(
    conn: TeamCraftConnection,
    ship_evidence_id: str,
    *,
    decision: str,
    commit_sha: str | None,
    branch: str | None,
    actor: str,
    reason: str,
    idempotency_key: str,
    lease: leader.Lease,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> DeliveryApproval:
    """Append an idempotent human decision bound to one ship evidence.

    An ``accept`` MUST echo the evidence's exact Commit SHA and branch —
    accepting a different (or unspecified) commit/branch is refused — and
    only an accepted, still-current chain CASes the local-only Task
    VERIFYING -> DONE (the domain gate re-derives every fact from durable
    rows inside that CAS).  A ``reject`` records the decision, leaves the
    Task VERIFYING, and closes this evidence version for any later decision
    (a fix arrives as a new bounded Attempt with fresh evidence).  Any drift
    after the evidence was formed — task version, policy, verifier config,
    commit amend/movement, dirty workspace — invalidates the old PASS and
    the approval opportunity alike.
    """
    if decision not in ("accept", "reject"):
        raise ValidationError("decision must be accept or reject")
    if (not isinstance(actor, str) or not actor.strip()
            or len(actor.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"actor is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"reason is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    if decision == "accept" and (not isinstance(commit_sha, str)
                                 or not commit_sha.strip()
                                 or not isinstance(branch, str)
                                 or not branch.strip()):
        raise ValidationError(
            "acceptance must explicitly echo the bound commit_sha and branch")
    key = idempotency_key.strip()
    runner = _AuditedRunner(run_git or _default_run_git)

    with _tx(conn):
        replayed = _replay_by_key(conn, APPROVAL_EVENT_KIND,
                                  APPROVAL_COMMAND_KIND, key)
        if replayed is not None:
            approval = _approval_from_row(conn, int(replayed[0]),
                                         str(replayed[1]), str(replayed[2]))
            if (approval.ship_evidence_id != ship_evidence_id
                    or approval.decision != decision
                    or approval.actor != actor.strip()
                    or approval.reason != reason.strip()):
                raise ConflictError(
                    "idempotency key already owns a different delivery"
                    " approval decision; refusing the collision")
            leader.require_project_lease(conn, lease, approval.project_id,
                                        clock=clock)
            return approval

        evidence = get_ship_evidence(conn, ship_evidence_id)
        task = domain.get_task(conn, evidence.task_id)
        attempt = domain.get_attempt(conn, evidence.attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        if task.kind != "ship" or task.status != "verifying":
            raise IllegalTransitionError(
                "delivery approval requires a ship Task in VERIFYING",
                entity="task", current=task.status, event="delivery_approval")
        if task.version != evidence.task_version:
            raise ConflictError(
                "the ship evidence is bound to Task version"
                f" {evidence.task_version}, but the Task is now version"
                f" {task.version}; evidence/task drift invalidates the PASS")
        allocation = _bound_allocation(conn, task, attempt)
        if (attempt.status != "finished"
                or attempt.attempt_no != evidence.attempt_no
                or allocation.allocation_id != evidence.allocation_id):
            raise ConflictError(
                "delivery approval: Attempt/allocation bindings no longer"
                " match the ship evidence")
        prior = load_delivery_approval(conn, evidence.id)
        if prior is not None:
            if (prior.decision == decision and prior.actor == actor.strip()
                    and prior.reason == reason.strip()):
                return prior
            raise ConflictError(
                "this ship evidence version already has an Approval"
                f" ({prior.decision!r}); a settled decision is immutable —"
                " drift or fixes require a new bounded Attempt with fresh"
                " evidence")
        policy = _policy_of(task)
        if (policy.get("delivery") != "local_only"
                or _policy_digest(policy) != evidence.policy_digest):
            raise ConflictError(
                "the execution policy changed after the ship evidence was"
                " formed; stale-policy acceptance is refused")
        _unresolved_other_key(conn, APPROVAL_COMMAND_KIND, attempt.id, key)

        # Live Git proof at decision time: exactly the bound commit, clean.
        problems = _verify_workspace_binding(conn, attempt.id, allocation,
                                            runner,
                                            expect_commit_sha=evidence.commit_sha,
                                            require_clean=True)
        if problems:
            raise DeliveryRefusal(
                "drift", "approval refused: " + "; ".join(problems))
        diff = _committed_diff(runner, allocation.worktree_path,
                               allocation.base_sha, evidence.commit_sha,
                               allocation.claims)
        if diff.diff_digest != evidence.diff_digest:
            raise DeliveryRefusal(
                "drift",
                "the complete base-to-commit diff changed after the ship"
                " evidence was formed")
        # Verifier profile config currency (the snapshot pinned at evidence
        # time must still equal the live required snapshot).
        pinned = {str(item["profile"]):
                  (int(item["profile_version"]), str(item["config_digest"]))
                  for item in evidence.verifiers}
        current_required = verification.required_profile_snapshot(
            conn, task.project_id)
        if current_required != pinned:
            raise ConflictError(
                "verifier profile configuration drifted after the ship"
                " evidence was formed; old PASS/Approval is invalid")
        if decision == "accept":
            if str(commit_sha).strip() != evidence.commit_sha:
                raise ConflictError(
                    "acceptance must name exactly the bound Commit:"
                    f" echoed {commit_sha!r} != bound {evidence.commit_sha!r}")
            if str(branch).strip() != evidence.branch:
                raise ConflictError(
                    "acceptance must name exactly the bound branch:"
                    f" echoed {branch!r} != bound {evidence.branch!r}")

        payload_bindings = {
            "project_id": task.project_id, "task_id": task.id,
            "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "ship_evidence_id": evidence.id,
            "commit_sha": evidence.commit_sha, "branch": evidence.branch,
            "diff_digest": evidence.diff_digest,
            "evidence_digest": evidence.evidence_digest,
            "verifier_digest": evidence.verifier_digest,
            "proposed_action": LOCAL_ONLY_ACCEPT_ACTION,
            "decision": decision, "actor": actor.strip(),
            "reason": reason.strip(),
        }
        digest = _sha256_hex(_canonical_json({
            "evidence_digest": evidence.evidence_digest,
            "commit_sha": evidence.commit_sha, "branch": evidence.branch,
            "verifier_digest": evidence.verifier_digest,
            "decision": decision}))
        command = commands.submit_command(
            conn, idempotency_key=key, kind=APPROVAL_COMMAND_KIND,
            payload_json=_canonical_json(payload_bindings),
            evidence_json=_evidence_envelope(task, attempt,
                                            evidence.dispatch_task_version,
                                            digest),
            clock=clock).command
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "Approval Command is already running/uncertain; reconcile it"
                " before retry", entity="command", current=command.state,
                event="delivery_approval")

        # In-transaction re-settle of the decision window (defense-in-depth
        # over the top-of-tx checks): whatever snapshot semantics the driver
        # applies, a decision that another writer already landed must refuse
        # HERE, before this attempt's own Approval row is appended.
        raced_approval = load_delivery_approval(conn, evidence.id)
        fresh_task = domain.get_task(conn, task.id)
        if (raced_approval is not None
                or fresh_task.status != "verifying"
                or fresh_task.version != task.version):
            raise ConflictError(
                "a delivery decision for this evidence raced this one; the"
                " settled decision is the durable one")

        # The durable Approval row is appended BEFORE the CAS and commits with
        # it in this one IMMEDIATE transaction: the domain acceptance gate
        # re-derives its facts from durable rows, so it must find this exact
        # accept record — and a failed CAS rolls the record back with it,
        # never leaving a standalone "accepted" claim without DONE.
        task_status_after = "done" if decision == "accept" else task.status
        approval_payload = {**payload_bindings,
                           "brief_digest": evidence.brief_digest,
                           "brief_version": evidence.brief_version,
                           "worker_report_id": evidence.worker_report_id,
                           "worker_report_digest": evidence.worker_report_digest,
                           "task_status_after": task_status_after,
                           "schema_version": DELIVERY_CONTRACT_VERSION,
                           "command_id": command.id}
        # ``evidence_digest`` is the immutable identity of the ShipEvidence
        # being approved.  Do not replace it with a digest of this Approval
        # payload: the domain acceptance gate joins the Approval to the exact
        # ship evidence through this value.  The append-only event plus the
        # strict cross-field checks below provide the Approval record's own
        # integrity boundary.
        event, _inserted = events.append_event_idempotent(
            conn, APPROVAL_EVENT_KIND, project_id=task.project_id,
            task_id=task.id, attempt_id=attempt.id,
            payload_json=_canonical_json(approval_payload),
            event_id=_approval_event_id(evidence.id), clock=clock)
        approval = _approval_from_row(conn, int(event.seq), event.event_id,
                                     _canonical_json(approval_payload))
        if decision == "accept":
            # The ONLY path to DONE for local-only: domain re-checks every
            # durable fact (this just-appplied Approval row, chain digests,
            # pins, verifier currency, policy) inside this same transaction
            # before the CAS.
            updated = domain.apply_task_event(
                conn, task.id, LOCAL_ACCEPT_EVENT,
                expected_version=task.version, clock=clock)
            if updated.status != "done":
                raise StorageError(
                    "local delivery acceptance did not land the Task in DONE;"
                    " rolling the approval back")
        events.append_event(conn,
            "delivery.approval_accepted" if decision == "accept"
            else "delivery.approval_rejected",
            project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
            payload_json=_canonical_json({
                "approval_id": approval.id, "command_id": command.id,
                "ship_evidence_id": evidence.id, "decision": decision,
                "commit_sha": evidence.commit_sha, "branch": evidence.branch,
                "diff_digest": evidence.diff_digest,
                "evidence_digest": evidence.evidence_digest,
                "actor": actor.strip(), "reason": reason.strip(),
                "task_status_after": task_status_after}),
            clock=clock)
        commands.confirm_command(conn, command.id, result_json=_canonical_json({
            "approval_id": approval.id, "decision": decision,
            "task_status": task_status_after,
            "commit_sha": evidence.commit_sha, "branch": evidence.branch}),
            clock=clock)
        return approval


# ---------------------------------------------------------------------------
# Currency helpers (any drift invalidates old PASS/Approval)
# ---------------------------------------------------------------------------


def _task_pin_problems(conn: TeamCraftConnection, task_id: str, attempt_id: str,
                      task_version: int, dispatch_ver: int, brief_digest: str,
                      report_id: int, report_digest: str, *,
                      after_accept: bool) -> list[str]:
    problems: list[str] = []
    try:
        task = domain.get_task(conn, task_id)
        attempt = domain.get_attempt(conn, attempt_id)
        brief = verification.briefs.get_attempt_brief(conn, attempt_id)
    except (NotFoundError, ValidationError):
        return ["bindings: task/attempt/brief missing"]
    report = conn.execute(
        "SELECT id, report_digest, outcome FROM worker_reports WHERE attempt_id"
        " = ? AND report_kind = 'final' ORDER BY id DESC LIMIT 1",
        (attempt_id,),
    ).fetchone()
    expected_status = "done" if after_accept else "verifying"
    expected_version = task_version + (1 if after_accept else 0)
    if task.status != expected_status:
        problems.append(f"status: task is {task.status!r}, expected {expected_status!r}")
    if task.version != expected_version:
        problems.append(f"version: task is {task.version}, evidence bound {task_version}"
                       + (f" (+1 for acceptance) = {expected_version}" if after_accept else ""))
    if attempt.status != "finished" or attempt.task_id != task.id:
        problems.append(f"attempt: {attempt.status!r}")
    if brief.task_version != dispatch_ver or brief.brief_digest != brief_digest:
        problems.append("brief: pinned brief facts drifted")
    if (report is None or int(report[0]) != report_id
            or str(report[1]) != report_digest or str(report[2]) != "completed"):
        problems.append("worker report: latest final completed report differs")
    return problems


def commit_evidence_is_current(conn: TeamCraftConnection,
                              evidence: CommitEvidence, *,
                              live_git: bool = True,
                              run_git: GitRunner | None = None
                              ) -> DeliveryFreshness:
    """Whether the durable Commit evidence still matches live facts."""
    problems = _task_pin_problems(conn, evidence.task_id, evidence.attempt_id,
                                  evidence.task_version,
                                  evidence.dispatch_task_version,
                                  evidence.brief_digest,
                                  evidence.worker_report_id,
                                  evidence.worker_report_digest,
                                  after_accept=False)
    allocation = None
    try:
        allocation = worktrees.require_allocation(conn, evidence.attempt_id)
        if allocation.allocation_id != evidence.allocation_id:
            problems.append("allocation: identity changed")
    except (NotFoundError, StorageError) as exc:
        problems.append(f"allocation: {exc}")
    if live_git and allocation is not None:
        runner = _AuditedRunner(run_git or _default_run_git)
        try:
            problems.extend(_verify_workspace_binding(
                conn, evidence.attempt_id, allocation, runner,
                expect_commit_sha=evidence.commit_sha, require_clean=True))
            diff = _committed_diff(runner, evidence.worktree_path,
                                  evidence.base_sha, evidence.commit_sha,
                                  allocation.claims)
            if diff.diff_digest != evidence.diff_digest:
                problems.append("diff: complete base-to-commit digest changed")
        except (DeliveryRefusal, StorageError, NotFoundError) as exc:
            problems.append(f"git: {exc}")
    return DeliveryFreshness(ok=not problems, problems=tuple(problems))


def ship_evidence_is_current(conn: TeamCraftConnection,
                            evidence: ShipEvidence, *,
                            live_git: bool = True,
                            after_accept: bool = False,
                            run_git: GitRunner | None = None
                            ) -> DeliveryFreshness:
    """Whether the ship evidence PASS is still current.

    ``after_accept=True`` is the post-DONE view used by
    :func:`delivery_approval_is_current`: the bound facts must still hold, but
    the Task legitimately sits DONE at ``task_version + 1`` instead of
    VERIFYING.
    """
    problems = _task_pin_problems(conn, evidence.task_id, evidence.attempt_id,
                                  evidence.task_version,
                                  evidence.dispatch_task_version,
                                  evidence.brief_digest,
                                  evidence.worker_report_id,
                                  evidence.worker_report_digest,
                                  after_accept=after_accept)
    try:
        task = domain.get_task(conn, evidence.task_id)
        policy = _policy_of(task)
        if (policy.get("delivery") != "local_only"
                or _policy_digest(policy) != evidence.policy_digest):
            problems.append("policy: delivery policy drifted")
        commit = require_commit_evidence(conn, evidence.attempt_id)
        if (commit.event_id != evidence.commit_evidence_id
                or commit.commit_sha != evidence.commit_sha
                or commit.diff_digest != evidence.diff_digest):
            problems.append("commit: evidence chain drifted")
    except (NotFoundError, StorageError, ValidationError) as exc:
        problems.append(f"chain: {exc}")
    pinned = {str(item["profile"]):
              (int(item["profile_version"]), str(item["config_digest"]))
              for item in evidence.verifiers}
    try:
        current_required = verification.required_profile_snapshot(
            conn, evidence.project_id)
        if current_required != pinned:
            problems.append(
                "verifier: required profile configuration drifted "
                f"(pinned {sorted(pinned.items())} vs current"
                f" {sorted(current_required.items())})")
    except StorageError as exc:
        problems.append(f"verifier: {exc}")
    if live_git:
        runner = _AuditedRunner(run_git or _default_run_git)
        try:
            allocation = worktrees.require_allocation(conn, evidence.attempt_id)
            problems.extend(_verify_workspace_binding(
                conn, evidence.attempt_id, allocation, runner,
                expect_commit_sha=evidence.commit_sha, require_clean=True))
            diff = _committed_diff(runner, evidence.worktree_path,
                                  evidence.base_sha, evidence.commit_sha,
                                  allocation.claims)
            if diff.diff_digest != evidence.diff_digest:
                problems.append("diff: complete base-to-commit digest changed")
        except (DeliveryRefusal, StorageError, NotFoundError) as exc:
            problems.append(f"git: {exc}")
    return DeliveryFreshness(ok=not problems, problems=tuple(problems))


def delivery_approval_is_current(conn: TeamCraftConnection,
                                approval: DeliveryApproval, *,
                                live_git: bool = True,
                                run_git: GitRunner | None = None
                                ) -> DeliveryFreshness:
    """Whether an accepted/rejected decision still stands against live facts.

    An accept is current only while the Task sits DONE at exactly
    ``task_version + 1`` on the bound commit/branch with a still-current
    evidence chain (any later Task mutation reopens it via
    :func:`teamcraft.domain.update_task`; verifier/profile/commit drift shows
    up here as invalidation).
    """
    evidence = get_ship_evidence(conn, approval.ship_evidence_id)
    problems: list[str] = []
    if approval.evidence_digest != evidence.evidence_digest:
        problems.append("evidence: approval no longer binds this evidence version")
    if approval.commit_sha != evidence.commit_sha or approval.branch != evidence.branch:
        problems.append("binding: approval commit/branch differ from evidence")
    if evidence.verifier_digest != approval.verifier_digest:
        problems.append("verifier: verifier bindings drifted from approval")
    problems.extend(_task_pin_problems(
        conn, approval.task_id, approval.attempt_id, approval.task_version,
        evidence.dispatch_task_version, evidence.brief_digest,
        evidence.worker_report_id, evidence.worker_report_digest,
        after_accept=approval.decision == "accept"))
    chain = ship_evidence_is_current(conn, evidence, live_git=live_git,
                                    after_accept=approval.decision == "accept",
                                    run_git=run_git)
    problems.extend(chain.problems)
    return DeliveryFreshness(ok=not problems, problems=tuple(problems))
