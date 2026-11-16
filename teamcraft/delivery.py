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

from . import commands, domain, events, github_adapter, leader, verification, workspaces, worktrees
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
    "DirectPrRefusal",
    "DirectPrResult",
    "MAX_DELIVERY_DIFF_BYTES",
    "PR_CONTRACT_VERSION",
    "PR_EVIDENCE_KIND",
    "PR_GIT_REMOTE",
    "PR_MERGE_APPROVAL_COMMAND_KIND",
    "PR_MERGE_APPROVAL_KIND",
    "PR_MERGE_COMMAND_KIND",
    "PR_MERGE_EVIDENCE_KIND",
    "PR_PREPARE_COMMAND_KIND",
    "PR_SYNC_COMMAND_KIND",
    "DIRECT_PR_MERGE_ACTION",
    "DIRECT_PR_READY",
    "CLEANUP_REFUSED_EVENT_KIND",
    "INTEGRATION_CHECK_EVENT_KIND",
    "INTEGRATION_REQUIRED_EVENT_KIND",
    "CleanupEligibility",
    "IntegrationStatus",
    "PrMergeApproval",
    "PrMergeEvidence",
    "PullRequestEvidence",
    "SHIP_COMMAND_KIND",
    "ShipEvidence",
    "approve_direct_pr_merge",
    "approve_local_delivery",
    "check_integration_state",
    "cleanup_attempt_worktree",
    "commit_evidence_is_current",
    "create_local_commit",
    "default_delivery_artifact_root",
    "delivery_approval_is_current",
    "ensure_integration_task",
    "evaluate_cleanup_eligibility",
    "get_commit_evidence",
    "get_delivery_approval",
    "get_pr_evidence",
    "get_pr_merge_evidence",
    "get_ship_evidence",
    "list_commit_evidence",
    "list_delivery_approvals",
    "list_pr_evidence",
    "list_pr_merge_approvals",
    "load_commit_evidence",
    "load_integration_requirement",
    "load_latest_pr_evidence",
    "load_pr_merge_approval",
    "load_pr_merge_evidence",
    "load_ship_evidence",
    "merge_direct_pr",
    "pr_evidence_is_current",
    "pr_merge_approval_is_current",
    "prepare_direct_pr",
    "reconcile_cleanup_worktree",
    "reconcile_commit_receipt",
    "reconcile_pr_merge",
    "reconcile_pr_receipt",
    "record_ship_evidence",
    "ship_evidence_is_current",
    "read_ship_diff",
    "sync_direct_pr",
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
        worktrees.validate_worktree_identity(
            conn, attempt_id, run_git=audited)
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


# ===========================================================================
# M4-05 — direct-PR delivery: durable PR evidence, explicit human merge
# approval, and fenced merge execution into the INTENDED base only.
#
# Mirrors the audited M4-04 pattern one level out: every GitHub effect rides a
# durable Command written BEFORE the effect, is fenced by the live project
# Leader lease, lands as append-only evidence rows with recomputable digests,
# and re-derives its facts from durable rows at every gate.  The two human
# edges stay separate: PR creation can only take a ship Task VERIFYING ->
# READY_TO_MERGE, and DONE requires an explicit Approval bound to exactly this
# PR/head/base plus a verified merge into the intended base.
# ===========================================================================


PR_CONTRACT_VERSION = domain.PR_DELIVERY_CONTRACT_VERSION
PR_EVIDENCE_KIND = domain.PR_EVIDENCE_EVENT_KIND
PR_MERGE_APPROVAL_KIND = domain.PR_MERGE_APPROVAL_EVENT_KIND
PR_MERGE_EVIDENCE_KIND = domain.PR_MERGE_EVIDENCE_EVENT_KIND
DIRECT_PR_READY = domain.DIRECT_PR_READY_EVENT
DIRECT_PR_MERGE_ACTION = domain.DIRECT_PR_MERGE_ACTION

#: Persistent Command kinds for the direct-PR slice.
PR_PREPARE_COMMAND_KIND = "delivery.pr_prepare"
PR_SYNC_COMMAND_KIND = "delivery.pr_sync"
PR_MERGE_APPROVAL_COMMAND_KIND = "delivery.approve_pr_merge"
PR_MERGE_COMMAND_KIND = "delivery.merge_pr"

#: The single remote name this slice may ever address.
PR_GIT_REMOTE = "origin"

#: Status-check conclusions that count as merge-ready (mirrors the adapter and
#: the domain gate; kept duplicated-per-layer like the digest rule).
_PR_PASSING = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

_PR_CHECKS_MAX = 200


class DirectPrRefusal(DeliveryRefusal):
    """A direct-PR step was refused fail-closed; nothing was written.

    ``kind`` extends the :class:`DeliveryRefusal` tags with ``gh`` (tooling /
    authentication unavailable), ``checks`` (required PR checks not passing),
    ``wrong-base`` (the PR/merge does not target the intended base) and
    ``approval`` (no current explicit human approval).
    """


@dataclass(frozen=True)
class PullRequestEvidence:
    """One immutable observation of a delivery pull request's facts."""

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
    commit_sha: str
    commit_evidence_id: str
    policy_digest: str
    repo: str
    pr_number: int
    pr_url: str
    head_branch: str
    head_sha: str
    base_ref: str
    base_oid: str
    mergeable: str
    checks: tuple[dict[str, Any], ...]
    checks_digest: str
    ready: bool
    source: str          # created | adopted | reconciled | synced
    command_id: str
    evidence_digest: str
    occurred_at: str

    @property
    def id(self) -> str:
        return self.event_id


@dataclass(frozen=True)
class PrMergeApproval:
    """One append-only human merge decision bound to exactly one PR evidence."""

    seq: int
    event_id: str
    command_id: str
    pr_evidence_id: str
    pr_evidence_digest: str
    project_id: str
    task_id: str
    attempt_id: str
    attempt_no: int
    task_version: int
    repo: str
    pr_number: int
    head_sha: str
    base_ref: str
    intended_base: str
    checks_digest: str
    proposed_action: str
    decision: str        # approve | reject
    actor: str
    reason: str
    task_status_after: str
    occurred_at: str

    @property
    def id(self) -> str:
        return self.event_id


@dataclass(frozen=True)
class PrMergeEvidence:
    """Durable proof of one merge outcome against the intended base."""

    seq: int
    event_id: str
    command_id: str
    pr_evidence_id: str
    approval_event_id: str
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
    policy_digest: str
    repo: str
    pr_number: int
    pr_url: str
    head_sha: str
    base_ref: str
    intended_base: str
    merge_commit_sha: str
    merged_at: str | None
    merged: bool
    method: str
    source: str          # executed | reconciled
    evidence_digest: str
    occurred_at: str

    @property
    def id(self) -> str:
        return self.event_id


@dataclass(frozen=True)
class DirectPrResult:
    """Outcome of one prepare/sync/reconcile pass of the direct-PR flow.

    ``outcome``: ``blocked`` (no gh / no auth: zero writes), ``pending_checks``
    (PR recorded, readiness facts not yet met), ``ready`` (this pass landed
    VERIFYING -> READY_TO_MERGE), ``ready_updated`` (facts refreshed while the
    Task already sat READY_TO_MERGE), ``unchanged`` (no fact movement),
    ``adopted``/``expired`` (reconcile resolutions).
    """

    outcome: str
    blocked: bool
    detail: str
    project_id: str
    task_id: str
    attempt_id: str
    task_status: str
    ready: bool
    pr_number: int | None
    pr_url: str
    command_id: str | None
    pr_evidence: PullRequestEvidence | None


# ---------------------------------------------------------------------------
# Direct-PR payload validation (fail closed on any shape/digest surprise)
# ---------------------------------------------------------------------------

_PR_EVIDENCE_FIELDS = frozenset({
    "allocation_id", "attempt_id", "attempt_no", "base_oid", "base_ref",
    "base_sha", "branch", "brief_digest", "brief_version", "checks_digest",
    "checks_json", "commit_evidence_id", "commit_sha", "command_id",
    "delivery_mode", "dispatch_task_version", "evidence_digest", "head_branch",
    "head_sha", "mergeable", "policy_digest", "pr_number", "pr_url",
    "project_id", "proposed_action", "ready", "repo", "schema_version",
    "source", "task_id", "task_version", "worker_report_digest",
    "worker_report_id", "worktree_path",
})
_PR_APPROVAL_FIELDS = frozenset({
    "actor", "attempt_id", "attempt_no", "base_ref", "checks_digest",
    "command_id", "decision", "head_sha", "intended_base",
    "pr_evidence_digest", "pr_evidence_id", "pr_number", "project_id",
    "proposed_action", "reason", "repo", "schema_version", "task_id",
    "task_status_after", "task_version",
})
_PR_MERGE_FIELDS = frozenset({
    "action", "approval_event_id", "attempt_id", "attempt_no", "base_ref",
    "brief_digest", "brief_version", "command_id", "delivery_mode",
    "dispatch_task_version", "evidence_digest", "head_sha", "intended_base",
    "merge_commit_sha", "merged", "merged_at", "method", "policy_digest",
    "pr_evidence_id", "pr_number", "pr_url", "project_id", "repo",
    "schema_version", "source", "task_id", "task_version",
    "worker_report_digest", "worker_report_id",
})


def _pr_checks(payload: dict, kind: str, where: str) -> list[dict[str, Any]]:
    raw = payload.get("checks_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise StorageError(f"{kind}.checks_json is invalid ({where})") from exc
    if not isinstance(raw, list) or len(raw) > _PR_CHECKS_MAX:
        raise StorageError(f"{kind}.checks_json is malformed ({where})")
    checks: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise StorageError(f"{kind}.checks_json entry is malformed ({where})")
        checks.append({
            "name": str(item.get("name") or ""),
            "status": str(item.get("status") or "").upper(),
            "conclusion": (None if item.get("conclusion") is None
                           else str(item["conclusion"]).upper()),
        })
    checks.sort(key=lambda c: (c["name"], c["status"]))
    if _sha256_hex(_canonical_json(checks)) != payload.get("checks_digest"):
        raise StorageError(f"{kind} checks digest does not recompute ({where})")
    return checks


def _checks_digest(checks: list[dict[str, Any]]) -> str:
    return _sha256_hex(_canonical_json(checks))


def _checks_passing(checks: list[dict[str, Any]]) -> bool:
    if not checks:
        return True
    return all(str(c.get("conclusion") or "").upper() in _PR_PASSING
               for c in checks)


def _pr_payload(**fields: Any) -> str:
    payload = dict(fields)
    payload["schema_version"] = PR_CONTRACT_VERSION
    payload["evidence_digest"] = _payload_digest(payload)
    return _canonical_json(payload)


def _pr_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                 payload_json: str) -> PullRequestEvidence:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"PR evidence payload is not valid JSON (seq={seq}): {exc}") from exc
    kind = "PR evidence"
    where = f"seq={seq}"
    _require_shape(payload, _PR_EVIDENCE_FIELDS, kind, where)
    if payload.get("schema_version") != PR_CONTRACT_VERSION:
        raise StorageError(f"unsupported {kind} schema_version ({where})")
    if _payload_digest(payload) != payload.get("evidence_digest"):
        raise StorageError(f"{kind} digest does not recompute ({where})")
    if payload.get("delivery_mode") != "direct_pr":
        raise StorageError(f"{kind}.delivery_mode must be direct_pr ({where})")
    if payload.get("proposed_action") != DIRECT_PR_MERGE_ACTION:
        raise StorageError(f"{kind}.proposed_action is unknown ({where})")
    if payload.get("source") not in ("created", "adopted", "reconciled",
                                     "synced"):
        raise StorageError(f"{kind}.source is invalid ({where})")
    checks = _pr_checks(payload, kind, where)
    head_branch = _require_str(payload, "head_branch", kind, where)
    _refuse_protected_branch(head_branch)
    if head_branch != _require_str(payload, "branch", kind, where):
        raise StorageError(f"{kind} head/branch disagree ({where})")
    if payload.get("ready") is not True and payload.get("ready") is not False:
        raise StorageError(f"{kind}.ready must be a boolean ({where})")
    mergeable = str(payload.get("mergeable") or "UNKNOWN")
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?",
                      (seq,)).fetchone()
    occurred_at = "" if row is None or row[0] is None else str(row[0])
    evidence = PullRequestEvidence(
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
        branch=head_branch,
        base_sha=_require_sha(payload, "base_sha", kind, where),
        commit_sha=_require_sha(payload, "commit_sha", kind, where),
        commit_evidence_id=_require_str(payload, "commit_evidence_id", kind, where),
        policy_digest=_require_digest64(payload, "policy_digest", kind, where),
        repo=_require_str(payload, "repo", kind, where),
        pr_number=_require_int(payload, "pr_number", kind, where, 1),
        pr_url=str(payload.get("pr_url") or ""),
        head_branch=head_branch,
        head_sha=_require_sha(payload, "head_sha", kind, where),
        base_ref=_require_str(payload, "base_ref", kind, where),
        base_oid=_require_sha(payload, "base_oid", kind, where),
        mergeable=mergeable,
        checks=tuple(checks),
        checks_digest=_require_digest64(payload, "checks_digest", kind, where),
        ready=bool(payload.get("ready")),
        source=str(payload["source"]),
        command_id=_require_str(payload, "command_id", kind, where),
        evidence_digest=_require_digest64(payload, "evidence_digest", kind, where),
        occurred_at=occurred_at,
    )
    if evidence.head_sha != evidence.commit_sha:
        raise StorageError(
            f"{kind} head {evidence.head_sha!r} is not the delivered commit"
            f" {evidence.commit_sha!r} ({where})")
    return evidence


def _pr_approval_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                         payload_json: str) -> PrMergeApproval:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"PR merge approval payload is not valid JSON (seq={seq}): {exc}") from exc
    kind = "PR merge approval"
    where = f"seq={seq}"
    _require_shape(payload, _PR_APPROVAL_FIELDS, kind, where)
    if payload.get("schema_version") != PR_CONTRACT_VERSION:
        raise StorageError(f"unsupported {kind} schema_version ({where})")
    decision = payload.get("decision")
    if decision not in ("approve", "reject"):
        raise StorageError(f"{kind}.decision is invalid ({where})")
    if payload.get("proposed_action") != DIRECT_PR_MERGE_ACTION:
        raise StorageError(f"{kind}.proposed_action is unknown ({where})")
    _require_digest64(payload, "checks_digest", kind, where)
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?",
                      (seq,)).fetchone()
    occurred_at = "" if row is None or row[0] is None else str(row[0])
    approval = PrMergeApproval(
        seq=seq, event_id=event_id,
        command_id=_require_str(payload, "command_id", kind, where),
        pr_evidence_id=_require_str(payload, "pr_evidence_id", kind, where),
        pr_evidence_digest=_require_digest64(
            payload, "pr_evidence_digest", kind, where),
        project_id=_require_str(payload, "project_id", kind, where),
        task_id=_require_str(payload, "task_id", kind, where),
        attempt_id=_require_str(payload, "attempt_id", kind, where),
        attempt_no=_require_int(payload, "attempt_no", kind, where, 1),
        task_version=_require_int(payload, "task_version", kind, where, 0),
        repo=_require_str(payload, "repo", kind, where),
        pr_number=_require_int(payload, "pr_number", kind, where, 1),
        head_sha=_require_sha(payload, "head_sha", kind, where),
        base_ref=_require_str(payload, "base_ref", kind, where),
        intended_base=_require_str(payload, "intended_base", kind, where),
        checks_digest=_require_digest64(payload, "checks_digest", kind, where),
        proposed_action=str(payload["proposed_action"]),
        decision=str(decision),
        actor=_require_str(payload, "actor", kind, where),
        reason=_require_str(payload, "reason", kind, where),
        task_status_after=_require_str(payload, "task_status_after", kind, where),
        occurred_at=occurred_at,
    )
    if approval.task_status_after != "ready_to_merge":
        raise StorageError(
            f"{kind}.task_status_after must be ready_to_merge — a merge"
            f" approval NEVER completes delivery by itself ({where})")
    if approval.intended_base != approval.base_ref:
        raise StorageError(
            f"{kind} binds base {approval.base_ref!r} as the intended base"
            f" {approval.intended_base!r} mismatch ({where})")
    return approval


def _merge_evidence_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                            payload_json: str) -> PrMergeEvidence:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"PR merge evidence payload is not valid JSON (seq={seq}): {exc}") from exc
    kind = "PR merge evidence"
    where = f"seq={seq}"
    _require_shape(payload, _PR_MERGE_FIELDS, kind, where)
    if payload.get("schema_version") != PR_CONTRACT_VERSION:
        raise StorageError(f"unsupported {kind} schema_version ({where})")
    if payload.get("delivery_mode") != "direct_pr":
        raise StorageError(f"{kind}.delivery_mode must be direct_pr ({where})")
    if payload.get("action") != DIRECT_PR_MERGE_ACTION:
        raise StorageError(f"{kind}.action is unknown ({where})")
    if payload.get("source") not in ("executed", "reconciled"):
        raise StorageError(f"{kind}.source is invalid ({where})")
    if payload.get("merged") is not True:
        raise StorageError(
            f"{kind} must record merged=true (a non-merge is an audit event,"
            f" never merge evidence) ({where})")
    row = conn.execute("SELECT occurred_at FROM events WHERE seq = ?",
                      (seq,)).fetchone()
    occurred_at = "" if row is None or row[0] is None else str(row[0])
    return PrMergeEvidence(
        seq=seq, event_id=event_id,
        command_id=_require_str(payload, "command_id", kind, where),
        pr_evidence_id=_require_str(payload, "pr_evidence_id", kind, where),
        approval_event_id=_require_str(payload, "approval_event_id", kind, where),
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
        policy_digest=_require_digest64(payload, "policy_digest", kind, where),
        repo=_require_str(payload, "repo", kind, where),
        pr_number=_require_int(payload, "pr_number", kind, where, 1),
        pr_url=str(payload.get("pr_url") or ""),
        head_sha=_require_sha(payload, "head_sha", kind, where),
        base_ref=_require_str(payload, "base_ref", kind, where),
        intended_base=_require_str(payload, "intended_base", kind, where),
        merge_commit_sha=_require_sha(payload, "merge_commit_sha", kind, where),
        merged_at=(None if payload.get("merged_at") is None
                   else str(payload["merged_at"])),
        merged=True,
        method=str(payload.get("method")),
        source=str(payload["source"]),
        evidence_digest=_require_digest64(payload, "evidence_digest", kind, where),
        occurred_at=occurred_at,
    )


# ---------------------------------------------------------------------------
# Durable readers
# ---------------------------------------------------------------------------


def _latest_event_row(conn: TeamCraftConnection, kind: str,
                      task_id: str) -> Any:
    return conn.execute(
        "SELECT seq, event_id, payload_json FROM events WHERE kind = ?"
        " AND task_id = ? ORDER BY seq DESC LIMIT 1", (kind, task_id),
    ).fetchone()


def get_pr_evidence(conn: TeamCraftConnection, event_id: str) -> PullRequestEvidence:
    row = _event_by_id(conn, PR_EVIDENCE_KIND, require_entity_id(event_id))
    return _pr_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def list_pr_evidence(conn: TeamCraftConnection,
                     task_id: str) -> list[PullRequestEvidence]:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events WHERE kind = ?"
        " AND task_id = ? ORDER BY seq ASC",
        (PR_EVIDENCE_KIND, require_entity_id(task_id)),
    ).fetchall()
    return [_pr_from_row(conn, int(r[0]), str(r[1]), str(r[2])) for r in rows]


def load_latest_pr_evidence(conn: TeamCraftConnection,
                            task_id: str) -> PullRequestEvidence | None:
    row = _latest_event_row(conn, PR_EVIDENCE_KIND, require_entity_id(task_id))
    if row is None:
        return None
    return _pr_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def load_pr_merge_approval(conn: TeamCraftConnection,
                          pr_evidence_id: str) -> PrMergeApproval | None:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events WHERE kind = ?"
        " AND json_extract(payload_json, '$.pr_evidence_id') = ?"
        " ORDER BY seq ASC",
        (PR_MERGE_APPROVAL_KIND, require_entity_id(pr_evidence_id)),
    ).fetchall()
    if len(rows) > 1:
        raise StorageError(
            f"{len(rows)} approvals for PR evidence {pr_evidence_id};"
            " storage invariant broken")
    if not rows:
        return None
    return _pr_approval_from_row(conn, int(rows[0][0]), str(rows[0][1]),
                                str(rows[0][2]))


def list_pr_merge_approvals(conn: TeamCraftConnection,
                            task_id: str) -> list[PrMergeApproval]:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events WHERE kind = ?"
        " AND task_id = ? ORDER BY seq ASC",
        (PR_MERGE_APPROVAL_KIND, require_entity_id(task_id)),
    ).fetchall()
    return [_pr_approval_from_row(conn, int(r[0]), str(r[1]), str(r[2]))
            for r in rows]


def get_pr_merge_evidence(conn: TeamCraftConnection,
                         event_id: str) -> PrMergeEvidence:
    row = _event_by_id(conn, PR_MERGE_EVIDENCE_KIND, require_entity_id(event_id))
    return _merge_evidence_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


def load_pr_merge_evidence(conn: TeamCraftConnection,
                           task_id: str) -> PrMergeEvidence | None:
    row = _latest_event_row(conn, PR_MERGE_EVIDENCE_KIND,
                            require_entity_id(task_id))
    if row is None:
        return None
    return _merge_evidence_from_row(conn, int(row[0]), str(row[1]), str(row[2]))


# ---------------------------------------------------------------------------
# Scoped Git + gh helpers
# ---------------------------------------------------------------------------


class _DirectPrGitRunner:
    """GitRunner wrapper for the direct-PR slice.

    Identical forbidden-verb policy as local delivery, plus EXACTLY two scoped
    additions: ``git push origin tc/<task>/<attempt>`` (the Attempt's own
    isolated branch only — never a protected branch, never ``--force``, never a
    different remote) and ``git ls-remote origin refs/heads/<branch>`` to read
    back the remote head fact.  Nothing else that could rewrite history, touch
    the base, or delete anything ever passes.
    """

    def __init__(self, runner: GitRunner, branch: str) -> None:
        _refuse_protected_branch(branch)
        self._runner = runner
        self.branch = branch

    def __call__(self, args: list[str], cwd: str) -> bytes:
        argv = list(args)
        if argv and argv[0] == "push":
            if argv != ["push", PR_GIT_REMOTE, self.branch]:
                raise DirectPrRefusal(
                    "main-branch",
                    "direct-PR delivery pushes only the exact Attempt branch"
                    f" {self.branch!r} to {PR_GIT_REMOTE!r}; refusing "
                    f"{argv!r}")
            return self._runner(argv, cwd)
        if argv and argv[0] == "ls-remote":
            if argv != ["ls-remote", PR_GIT_REMOTE, f"refs/heads/{self.branch}"]:
                raise DirectPrRefusal(
                    "drift",
                    f"direct-PR delivery reads only its own remote ref; refusing {argv!r}")
            return self._runner(argv, cwd)
        return _git_audit_safe(self._runner, argv, cwd)


def _remote_head(runner: _DirectPrGitRunner, cwd: str) -> str | None:
    """Remote sha of the Attempt branch, or ``None`` when it doesn't exist."""
    try:
        raw = runner(["ls-remote", PR_GIT_REMOTE, f"refs/heads/{runner.branch}"],
                     cwd)
    except workspaces.GitStateError as exc:
        raise DirectPrRefusal(
            "gh", f"could not read remote ref for {runner.branch!r}: {exc}")
    text = _text(raw)
    if not text:
        return None
    first = text.splitlines()[0].strip()
    sha = first.split("\t", 1)[0].strip().lower() if first else ""
    if not _SHA_RE.match(sha):
        raise DirectPrRefusal(
            "gh", f"unexpected ls-remote output for {runner.branch!r}: {text[:120]!r}")
    return sha


def _ensure_remote_branch(runner: _DirectPrGitRunner, cwd: str,
                         commit_sha: str) -> str:
    """Make the remote Attempt branch equal exactly ``commit_sha``.

    Returns ``pushed`` / ``already``.  A foreign remote head (any sha that is
    not the delivered commit) is a DRIFT refusal — this module never forces,
    never fetches, and never rewrites: a human/reconcile decision is required.
    """
    head = _remote_head(runner, cwd)
    if head == commit_sha:
        return "already"
    if head is not None:
        raise DirectPrRefusal(
            "drift",
            f"remote branch {runner.branch!r} sits at {head[:12]}, not the"
            f" delivered commit {commit_sha[:12]}; direct-PR delivery never"
            " force-pushes or rebases over remote history")
    runner(["push", PR_GIT_REMOTE, runner.branch], cwd)
    verify = _remote_head(runner, cwd)
    if verify != commit_sha:
        raise StorageError(
            f"push of {runner.branch!r} did not land the expected head"
            f" (wanted {commit_sha!r}, remote now {verify!r}); reconcile before"
            " creating the pull request")
    return "pushed"


def _direct_pr_policy(task: domain.Task) -> tuple[dict[str, Any], str, dict[str, Any]]:
    policy = _policy_of(task)
    config = domain.direct_pr_config(policy)
    if config is None:
        raise DirectPrRefusal(
            "mode",
            "direct-PR delivery requires an EXPLICIT persisted policy:"
            " delivery=direct_pr plus a direct_pr {repo, base} sub-policy on"
            " the Task (nothing is inferred, and defaults are refused)")
    return policy, _policy_digest(policy), config


def _pr_pin_problems(conn: TeamCraftConnection, *, task_id: str, attempt_id: str,
                    task_version: int, dispatch_ver: int, brief_digest: str,
                    report_id: int, report_digest: str,
                    expect_statuses: tuple[str, ...],
                    check_version: bool = True,
                    version_bump_ok: bool = False) -> list[str]:
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
    if task.status not in expect_statuses:
        problems.append(f"status: task is {task.status!r},"
                       f" expected one of {list(expect_statuses)}")
    if check_version:
        # The readiness CAS (verifying -> READY_TO_MERGE) is the ONE legitimate
        # version bump after PR evidence is formed; anything else is drift.
        allowed = {task_version}
        if version_bump_ok:
            allowed.add(task_version + 1)
        if task.version not in allowed:
            problems.append(f"version: task is {task.version}, evidence bound"
                           f" {sorted(allowed)}")
    if attempt.status != "finished" or attempt.task_id != task.id:
        problems.append(f"attempt: {attempt.status!r}")
    if brief.task_version != dispatch_ver or brief.brief_digest != brief_digest:
        problems.append("brief: pinned brief facts drifted")
    if (report is None or int(report[0]) != report_id
            or str(report[1]) != report_digest or str(report[2]) != "completed"):
        problems.append("worker report: latest final completed report differs")
    return problems


def _live_pr_facts(runner: github_adapter.GitHubRunner, cwd: str, *, repo: str,
                   evidence: PullRequestEvidence) -> tuple[github_adapter.PullRequestFacts | None, list[str]]:
    """Read the authoritative live facts for the recorded PR; collect problems."""
    problems: list[str] = []
    try:
        facts = github_adapter.read_pr(runner, cwd, repo=repo,
                                       number=evidence.pr_number)
    except github_adapter.GitHubInvocationError as exc:
        return None, [f"gh: PR #{evidence.pr_number} unreadable: {exc.stderr or exc}"]
    except github_adapter.GitHubError as exc:
        return None, [f"gh: {exc}"]
    if facts.head_sha != evidence.head_sha:
        problems.append(
            f"head: PR head moved to {facts.head_sha[:12]} from the bound"
            f" {evidence.head_sha[:12]} (drift invalidates this evidence)")
    if facts.base_ref != evidence.base_ref:
        problems.append(
            f"base: PR base is {facts.base_ref!r}, not the bound"
            f" {evidence.base_ref!r}")
    if facts.merged:
        problems.append("state: the PR is already merged; this evidence is stale")
    elif not facts.open:
        problems.append(f"state: the PR is {facts.state}, not OPEN")
    if facts.mergeable != "MERGEABLE":
        problems.append(f"mergeable: PR reports {facts.mergeable}")
    if not facts.checks_passing:
        problems.append("checks: required status checks are not passing")
    return facts, problems


def _pr_evidence_event_id(attempt_id: str, pr_number: int, head_sha: str,
                         checks_digest: str, mergeable: str, base_ref: str,
                         task_version: int) -> str:
    return "evt-dpe-" + _sha256_hex(
        f"pr-evidence|{attempt_id}|{pr_number}|{task_version}|{base_ref}|"
        f"{head_sha}|{checks_digest}|{mergeable}")[:40]


def _pr_approval_event_id(pr_evidence_id: str) -> str:
    return "evt-dpma-" + _sha256_hex(f"pr-merge-approval|{pr_evidence_id}")[:40]


def _merge_evidence_event_id(pr_evidence_id: str) -> str:
    return "evt-dpme-" + _sha256_hex(f"pr-merge|{pr_evidence_id}")[:40]


def _persist_pr_evidence(conn: TeamCraftConnection, *, task: domain.Task,
                         attempt: domain.Attempt, dispatch_ver: int,
                         brief_digest: str, brief_version: int, report_id: int,
                         report_digest: str, allocation: worktrees.WorktreeAllocation,
                         commit: CommitEvidence, policy_digest: str,
                         facts: github_adapter.PullRequestFacts, source: str,
                         command_id: str, clock: Clock | None) -> PullRequestEvidence:
    checks = [dict(c) for c in facts.checks]
    checks.sort(key=lambda c: (c["name"], c["status"]))
    ready = bool(facts.open and facts.mergeable == "MERGEABLE"
                 and _checks_passing(checks))
    payload_json = _pr_payload(
        allocation_id=allocation.allocation_id, attempt_id=attempt.id,
        attempt_no=attempt.attempt_no, base_oid=facts.base_sha or commit.base_sha,
        base_ref=facts.base_ref, base_sha=commit.base_sha,
        branch=facts.head_branch or allocation.branch,
        brief_digest=brief_digest, brief_version=brief_version,
        checks_digest=_checks_digest(checks),
        checks_json=_canonical_json(checks),
        commit_evidence_id=commit.event_id, commit_sha=commit.commit_sha,
        command_id=command_id, delivery_mode="direct_pr",
        dispatch_task_version=dispatch_ver, head_branch=facts.head_branch,
        head_sha=facts.head_sha, mergeable=facts.mergeable,
        policy_digest=policy_digest, pr_number=facts.number, pr_url=facts.url,
        project_id=task.project_id, proposed_action=DIRECT_PR_MERGE_ACTION,
        ready=ready, repo=facts.repo, source=source, task_id=task.id,
        task_version=task.version, worker_report_digest=report_digest,
        worker_report_id=report_id, worktree_path=allocation.worktree_path,
    )
    event, _inserted = events.append_event_idempotent(
        conn, PR_EVIDENCE_KIND, project_id=task.project_id, task_id=task.id,
        attempt_id=attempt.id, payload_json=payload_json,
        event_id=_pr_evidence_event_id(
            attempt.id, facts.number, facts.head_sha, _checks_digest(checks),
            facts.mergeable, facts.base_ref, task.version),
        clock=clock)
    return _pr_from_row(conn, int(event.seq), event.event_id, payload_json)


# ---------------------------------------------------------------------------
# PREPARE — idempotent PR creation/adoption; VERIFYING -> READY_TO_MERGE only
# ---------------------------------------------------------------------------


def prepare_direct_pr(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    run_git: GitRunner | None = None,
    run_gh: github_adapter.GitHubRunner | None = None,
    which: Any = None,
    clock: Clock | None = None,
) -> DirectPrResult:
    """Create (or adopt) THE pull request for one finished Attempt.

    Order is pinned like M4-04: probe (zero-write) → validate durable facts →
    durable Command (pending→running, committed) → scoped Git push of the
    Attempt branch → ``gh pr`` create/adopt → live read-back → fenced receipt.

    Possible outcomes (:class:`DirectPrResult`):
      * ``blocked`` — ``gh`` missing or unauthenticated; NOTHING was written
        (no Command, no event, no Git/gh call beyond the probe).
      * ``ready`` — PR open + mergeable + checks passing; Task CASed
        VERIFYING → READY_TO_MERGE.  This is the furthest prepare goes: it can
        NEVER complete the Task.
      * ``pending_checks`` — PR recorded but readiness facts not met yet;
        re-run :func:`sync_direct_pr` (or prepare again) to update facts.

    Refusals (nothing written): non-``direct_pr`` policy or missing explicit
    ``direct_pr`` repo/base config (``mode``), non-VERIFYING Task, missing
    commit/verifier evidence, base/commit/drift, foreign remote branch head
    (never force-pushed), protected branches.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    key = idempotency_key.strip()
    attempt_id = require_entity_id(attempt_id)
    gh = github_adapter.AuditedGhRunner(run_gh or github_adapter.default_run_gh)

    attempt0 = domain.get_attempt(conn, attempt_id)
    task0 = domain.get_task(conn, attempt0.task_id)
    policy0 = _policy_of(task0)
    if policy0.get("delivery") != "direct_pr":
        raise DirectPrRefusal(
            "mode",
            f"task policy delivery is {policy0.get('delivery')!r}; direct-PR"
            " preparation only runs for delivery=direct_pr")
    _config_probe = domain.direct_pr_config(policy0)
    if _config_probe is None:
        raise DirectPrRefusal(
            "mode",
            "explicit persisted direct_pr policy with repo and base is required"
            " (delivery=direct_pr alone does not enable execution)")

    # ---- probe BEFORE any durable write: no gh / no auth => zero residue --
    probe = github_adapter.probe_gh(gh, task_repo_root(conn, task0),
                                    resolve=which)
    if probe.blocked:
        return DirectPrResult(
            outcome="blocked", blocked=True, detail=probe.detail,
            project_id=task0.project_id, task_id=task0.id, attempt_id=attempt0.id,
            task_status=task0.status, ready=False, pr_number=None, pr_url="",
            command_id=None, pr_evidence=None)

    with _tx(conn):
        replayed = _replay_by_key(conn, PR_EVIDENCE_KIND,
                                  PR_PREPARE_COMMAND_KIND, key)
        if replayed is not None:
            evidence = _pr_from_row(conn, int(replayed[0]), str(replayed[1]),
                                   str(replayed[2]))
            if evidence.attempt_id != attempt_id:
                raise ConflictError(
                    "idempotency key already owns PR evidence for a different"
                    " Attempt; refusing the collision")
            leader.require_project_lease(conn, lease, evidence.project_id,
                                        clock=clock)
            task = domain.get_task(conn, evidence.task_id)
            outcome = "ready" if evidence.ready else "pending_checks"
            return DirectPrResult(
                outcome=outcome, blocked=False, detail="replayed durable PR evidence",
                project_id=evidence.project_id, task_id=evidence.task_id,
                attempt_id=evidence.attempt_id, task_status=task.status,
                ready=evidence.ready, pr_number=evidence.pr_number,
                pr_url=evidence.pr_url, command_id=evidence.command_id,
                pr_evidence=evidence)

        # A settled PR evidence row for THIS attempt proves the PR effect
        # already happened once — converge onto it BEFORE re-validating bound
        # facts, because the readiness CAS legitimately bumps the Task version
        # past the dispatch pin that _bound_attempt_facts demands.  Retrying
        # after readiness must never push, create, or duplicate anything.
        settled_existing = load_latest_pr_evidence(conn, task0.id)
        settled_commit = load_commit_evidence(conn, attempt_id)
        if (settled_existing is not None and settled_commit is not None
                and settled_existing.attempt_id == attempt_id
                and settled_existing.head_sha == settled_commit.commit_sha):
            leader.require_project_lease(conn, lease, task0.project_id,
                                        clock=clock)
            outcome = "ready" if settled_existing.ready else "pending_checks"
            return DirectPrResult(
                outcome=outcome, blocked=False,
                detail=(f"PR #{settled_existing.pr_number} already recorded for"
                      " this commit; no duplicate created"),
                project_id=task0.project_id, task_id=task0.id,
                attempt_id=attempt_id, task_status=task0.status,
                ready=settled_existing.ready,
                pr_number=settled_existing.pr_number,
                pr_url=settled_existing.pr_url,
                command_id=settled_existing.command_id,
                pr_evidence=settled_existing)

        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            _bound_attempt_facts(conn, attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        allocation = _bound_allocation(conn, task, attempt)
        _policy, policy_digest, config = _direct_pr_policy(task)
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
        verifiers, _required = _required_verifier_evidence(conn, task, attempt, None)
        _unresolved_same_key(conn, PR_PREPARE_COMMAND_KIND, key)
        _unresolved_other_key(conn, PR_PREPARE_COMMAND_KIND, attempt.id, key)

        existing = load_latest_pr_evidence(conn, task.id)
        if (existing is not None and existing.attempt_id == attempt.id
                and existing.head_sha == commit.commit_sha):
            # A PR already exists for this exact delivered commit: never create
            # a second one — converge onto the settled/adopted evidence.
            outcome = "ready" if existing.ready else "pending_checks"
            return DirectPrResult(
                outcome=outcome, blocked=False,
                detail=f"PR #{existing.pr_number} already recorded for this"
                      " commit; no duplicate created",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                task_status=task.status, ready=existing.ready,
                pr_number=existing.pr_number, pr_url=existing.pr_url,
                command_id=existing.command_id, pr_evidence=existing)
        if (existing is not None and existing.attempt_id == attempt.id
                and existing.head_sha != commit.commit_sha):
            raise ConflictError(
                "a PR evidence row exists for this Attempt at a different head"
                f" ({existing.head_sha[:12]} vs {commit.commit_sha[:12]});"
                " reconcile before proceeding")

        payload = {
            "project_id": task.project_id, "task_id": task.id,
            "task_version": task.version, "attempt_id": attempt.id,
            "attempt_no": attempt.attempt_no,
            "dispatch_task_version": dispatch_ver,
            "brief_digest": brief.brief_digest, "brief_version": brief.brief_version,
            "worker_report_id": report_id, "worker_report_digest": report_digest,
            "allocation_id": allocation.allocation_id,
            "worktree_path": allocation.worktree_path, "branch": allocation.branch,
            "base_sha": allocation.base_sha, "commit_sha": commit.commit_sha,
            "commit_evidence_id": commit.event_id, "policy_digest": policy_digest,
            "repo": config["repo"], "base": config["base"],
            "merge_method": config["merge_method"],
            "verifier_digest": _verifier_digest(verifiers),
        }
        digest = _sha256_hex(_canonical_json({
            "brief_digest": brief.brief_digest, "attempt_id": attempt.id,
            "commit_sha": commit.commit_sha,
            "policy_digest": policy_digest, "worker_report_digest": report_digest,
            "repo": config["repo"], "base": config["base"]}))
        command = commands.submit_command(
            conn, idempotency_key=key, kind=PR_PREPARE_COMMAND_KIND,
            payload_json=_canonical_json(payload),
            evidence_json=_evidence_envelope(task, attempt, dispatch_ver, digest),
            clock=clock).command
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "PR prepare Command is already running/uncertain; reconcile it"
                " before retry", entity="command", current=command.state,
                event="pr_prepare")

    git = _DirectPrGitRunner(run_git or _default_run_git, allocation.branch)
    repo_root = task_repo_root(conn, task)
    try:
        # ---- effects (outside any transaction): scoped push, then PR ------
        _ensure_remote_branch(git, repo_root, commit.commit_sha)
        adopted = github_adapter.find_open_pr(
            gh, repo_root, repo=config["repo"], head=allocation.branch,
            base=config["base"])
        if adopted is not None:
            source = "adopted"
            if adopted.head_sha != commit.commit_sha:
                raise DirectPrRefusal(
                    "drift",
                    f"open PR #{adopted.number} carries head"
                    f" {adopted.head_sha[:12]}, not the delivered commit"
                    f" {commit.commit_sha[:12]}; refusing to adopt a foreign PR")
            facts = adopted
        else:
            source = "created"
            try:
                facts = github_adapter.create_pr(
                    gh, repo_root, repo=config["repo"], head=allocation.branch,
                    base=config["base"],
                    title=f"[teamcraft] deliver {task.id} attempt {attempt.id}",
                    body=_pr_body(task, attempt, commit, config))
            except github_adapter.GitHubInvocationError as exc:
                commands.mark_command_unknown(
                    conn, command.id,
                    note=("pr create returned an error; the PR may or may not"
                        f" exist: {exc.stderr or exc}")[:1900], clock=clock)
                raise StorageError(
                    f"PR creation for attempt {attempt.id} was durably recorded"
                    " but gh reported an error; run reconcile_pr_receipt before"
                    " retry (never assume a duplicate)") from exc
            facts = github_adapter.read_pr(gh, repo_root, repo=config["repo"],
                                          number=facts.number)
            if facts.head_sha != commit.commit_sha or facts.base_ref != config["base"]:
                raise DirectPrRefusal(
                    "drift",
                    f"PR #{facts.number} reads back head"
                    f" {facts.head_sha[:12]} into {facts.base_ref!r}; expected"
                    f" {commit.commit_sha[:12]} into {config['base']!r}")

        checks = [dict(c) for c in facts.checks]
        checks.sort(key=lambda c: (c["name"], c["status"]))
        ready = bool(facts.open and facts.mergeable == "MERGEABLE"
                     and _checks_passing(checks))

        # ---- fenced receipt ----
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
                    and current_policy.get("delivery") == "direct_pr"
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
                    and current_commit.commit_sha == commit.commit_sha)
            if not fresh:
                raise ConflictError(
                    "delivery facts drifted while the PR was formed; refusing to"
                    " record the evidence")
            evidence = _persist_pr_evidence(
                conn, task=current_task, attempt=current_attempt,
                dispatch_ver=dispatch_ver, brief_digest=brief.brief_digest,
                brief_version=brief.brief_version, report_id=report_id,
                report_digest=report_digest, allocation=allocation,
                commit=commit, policy_digest=policy_digest, facts=facts,
                source=source, command_id=command.id, clock=clock)
            outcome = "pending_checks"
            status_after = current_task.status
            if ready:
                updated = domain.apply_task_event(
                    conn, task.id, DIRECT_PR_READY,
                    expected_version=current_task.version, clock=clock)
                status_after = updated.status
                outcome = "ready"
                if status_after != "ready_to_merge":
                    raise StorageError(
                        "direct-PR readiness did not land READY_TO_MERGE;"
                        " rolling the receipt back")
            events.append_event(conn, "delivery.pr_ready" if ready
                               else "delivery.pr_recorded",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                payload_json=_canonical_json({
                    "pr_evidence_id": evidence.event_id,
                    "command_id": command.id, "pr_number": evidence.pr_number,
                    "pr_url": evidence.pr_url, "repo": evidence.repo,
                    "head_branch": evidence.head_branch, "head_sha": evidence.head_sha,
                    "base_ref": evidence.base_ref, "intended_base": config["base"],
                    "mergeable": evidence.mergeable,
                    "checks_digest": evidence.checks_digest,
                    "ready": ready, "source": source,
                    "task_status_after": status_after,
                    "next": ("explicit human merge approval bound to this exact"
                            " PR/head/base is required before any merge") if ready
                           else "sync PR facts once checks complete"}),
                clock=clock)
            commands.confirm_command(conn, command.id, result_json=_canonical_json({
                "pr_evidence_id": evidence.event_id, "pr_number": evidence.pr_number,
                "pr_url": evidence.pr_url, "head_sha": evidence.head_sha,
                "base_ref": evidence.base_ref, "ready": ready,
                "task_status_after": status_after}),
                clock=clock)
            return DirectPrResult(
                outcome=outcome, blocked=False, detail=f"PR #{evidence.pr_number} {source}",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt.id,
                task_status=status_after, ready=ready, pr_number=evidence.pr_number,
                pr_url=evidence.pr_url, command_id=command.id,
                pr_evidence=evidence)
    except DirectPrRefusal:
        try:
            commands.mark_command_unknown(
                conn, command.id,
                note="direct-PR preparation refused after claim; effect state"
                   " must be reconciled against GitHub", clock=clock)
        except Exception:
            pass
        raise
    except Exception as exc:
        detail = str(exc).encode("utf-8")[:500].decode("utf-8", errors="ignore")
        try:
            commands.mark_command_unknown(
                conn, command.id,
                note=f"direct-PR preparation interrupted: {detail}", clock=clock)
        except Exception:
            pass
        raise


def _pr_body(task: domain.Task, attempt: domain.Attempt, commit: CommitEvidence,
             config: dict[str, Any]) -> str:
    return _canonical_json({
        "task_id": task.id, "attempt_id": attempt.id,
        "attempt_no": attempt.attempt_no, "task_version": task.version,
        "commit_sha": commit.commit_sha, "base_sha": commit.base_sha,
        "intended_base": config["base"], "merge_method": config["merge_method"],
        "provenance": "TeamCraft M4-05 direct-PR delivery",
        "note": "merge only via an explicit TeamCraft merge approval bound to"
               " the exact head/base"})


# ---------------------------------------------------------------------------
# SYNC — refresh head/base/check facts; readiness CAS when facts turn ready
# ---------------------------------------------------------------------------


def sync_direct_pr(
    conn: TeamCraftConnection,
    task_id: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    run_gh: github_adapter.GitHubRunner | None = None,
    which: Any = None,
    clock: Clock | None = None,
) -> DirectPrResult:
    """Re-read the durable PR's live facts and record a new evidence row.

    Never creates a PR and never merges.  When the recorded Task is still
    VERIFYING and the fresh facts are ready (open + mergeable + checks
    passing), this is what CASes VERIFYING → READY_TO_MERGE; when the Task is
    already READY_TO_MERGE it refreshes the audit trail (and drift becomes
    visible to :func:`pr_evidence_is_current`).  Any head/base movement, PR
    close/merge, policy change or Task drift lands as problems, never as a
    silent pass.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    key = idempotency_key.strip()
    task_id = require_entity_id(task_id)
    gh = github_adapter.AuditedGhRunner(run_gh or github_adapter.default_run_gh)

    task0 = domain.get_task(conn, task_id)
    if task0.kind != "ship":
        raise DirectPrRefusal("mode", "only ship Tasks have direct-PR evidence")
    _policy0, _pd0, config0 = _direct_pr_policy(task0)
    if task0.status not in ("verifying", "ready_to_merge"):
        raise DirectPrRefusal(
            "stale", f"task status {task0.status!r} has no live direct-PR to sync")
    existing0 = load_latest_pr_evidence(conn, task_id)
    if existing0 is None:
        raise NotFoundError("no durable PR evidence to sync; run prepare_direct_pr first")

    probe = github_adapter.probe_gh(gh, task_repo_root(conn, task0), resolve=which)
    if probe.blocked:
        return DirectPrResult(
            outcome="blocked", blocked=True, detail=probe.detail,
            project_id=task0.project_id, task_id=task0.id,
            attempt_id=existing0.attempt_id, task_status=task0.status,
            ready=False, pr_number=existing0.pr_number, pr_url=existing0.pr_url,
            command_id=None, pr_evidence=None)

    with _tx(conn):
        replayed = _replay_by_key(conn, PR_EVIDENCE_KIND, PR_SYNC_COMMAND_KIND, key)
        if replayed is not None:
            evidence = _pr_from_row(conn, int(replayed[0]), str(replayed[1]),
                                   str(replayed[2]))
            leader.require_project_lease(conn, lease, evidence.project_id, clock=clock)
            task = domain.get_task(conn, evidence.task_id)
            return DirectPrResult(
                outcome="ready" if evidence.ready else "pending_checks",
                blocked=False, detail="replayed durable PR sync",
                project_id=evidence.project_id, task_id=evidence.task_id,
                attempt_id=evidence.attempt_id, task_status=task.status,
                ready=evidence.ready, pr_number=evidence.pr_number,
                pr_url=evidence.pr_url, command_id=evidence.command_id,
                pr_evidence=evidence)

        task = domain.get_task(conn, task_id)
        attempt_id = existing0.attempt_id
        attempt = domain.get_attempt(conn, attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        problems = _pr_pin_problems(
            conn, task_id=task.id, attempt_id=attempt_id,
            task_version=existing0.task_version,
            dispatch_ver=existing0.dispatch_task_version,
            brief_digest=existing0.brief_digest,
            report_id=existing0.worker_report_id,
            report_digest=existing0.worker_report_digest,
            expect_statuses=("verifying", "ready_to_merge"),
            version_bump_ok=True)
        policy, policy_digest, config = _direct_pr_policy(task)
        if policy_digest != existing0.policy_digest:
            problems.append("policy: execution policy drifted after the PR evidence")
        if config["repo"] != existing0.repo or config["base"] != existing0.base_ref:
            problems.append("policy: direct_pr repo/base drifted from the recorded PR")
        commit = require_commit_evidence(conn, attempt_id)
        if (commit.event_id != existing0.commit_evidence_id
                or commit.commit_sha != existing0.commit_sha):
            problems.append("commit: evidence chain drifted")
        if problems:
            raise ConflictError("direct-PR sync refused: " + "; ".join(problems))
        _unresolved_same_key(conn, PR_SYNC_COMMAND_KIND, key)
        _unresolved_other_key(conn, PR_SYNC_COMMAND_KIND, attempt_id, key)
        command = commands.submit_command(
            conn, idempotency_key=key, kind=PR_SYNC_COMMAND_KIND,
            payload_json=_canonical_json({
                "project_id": task.project_id, "task_id": task.id,
                "task_version": task.version, "attempt_id": attempt_id,
                "attempt_no": attempt.attempt_no,
                "dispatch_task_version": existing0.dispatch_task_version,
                "brief_digest": existing0.brief_digest,
                "worker_report_id": existing0.worker_report_id,
                "worker_report_digest": existing0.worker_report_digest,
                "pr_evidence_id": existing0.event_id, "pr_number": existing0.pr_number,
                "repo": config["repo"], "intended_base": config["base"],
                "head_sha": existing0.head_sha, "policy_digest": policy_digest}),
            evidence_json=_evidence_envelope(task, attempt,
                                            existing0.dispatch_task_version,
                                            existing0.evidence_digest),
            clock=clock).command
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "PR sync Command is already running/uncertain; reconcile it"
                " before retry", entity="command", current=command.state,
                event="pr_sync")

    repo_root = task_repo_root(conn, task0)
    try:
        facts, live_problems = _live_pr_facts(gh, repo_root, repo=config["repo"],
                                              evidence=existing0)
        if facts is None:
            raise DirectPrRefusal("gh", "; ".join(live_problems))
        checks = [dict(c) for c in facts.checks]
        checks.sort(key=lambda c: (c["name"], c["status"]))
        ready = bool(facts.open and facts.mergeable == "MERGEABLE"
                     and _checks_passing(checks))
        unchanged = (facts.head_sha == existing0.head_sha
                     and facts.base_ref == existing0.base_ref
                     and facts.mergeable == existing0.mergeable
                     and _checks_digest(checks) == existing0.checks_digest
                     and facts.number == existing0.pr_number
                     and ready == existing0.ready)
        if live_problems and not ready:
            # Drift (moved head, closed PR, failing checks) is recorded as an
            # audit event only: stale facts can never form readiness.
            events.append_event(conn, "delivery.pr_drift_observed",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt_id,
                payload_json=_canonical_json({
                    "pr_evidence_id": existing0.event_id, "pr_number": existing0.pr_number,
                    "problems": live_problems, "command_id": command.id}),
                clock=clock)
            commands.confirm_command(conn, command.id, result_json=_canonical_json({
                "pr_number": existing0.pr_number, "ready": False,
                "problems": live_problems}), clock=clock)
            return DirectPrResult(
                outcome="pending_checks", blocked=False,
                detail="; ".join(live_problems), project_id=task.project_id,
                task_id=task.id, attempt_id=attempt_id, task_status=task.status,
                ready=False, pr_number=existing0.pr_number,
                pr_url=existing0.pr_url, command_id=command.id,
                pr_evidence=existing0)

        with _tx(conn):
            leader.require_project_lease(conn, lease, task.project_id, clock=clock)
            current_task = domain.get_task(conn, task.id)
            current_policy = _policy_of(current_task)
            if (current_task.version != task.version
                    or current_task.status != task.status
                    or _policy_digest(current_policy) != policy_digest):
                raise ConflictError(
                    "direct-PR facts drifted while syncing; refusing the update")
            evidence = existing0
            if not unchanged:
                evidence = _persist_pr_evidence(
                    conn, task=current_task, attempt=attempt,
                    dispatch_ver=existing0.dispatch_task_version,
                    brief_digest=existing0.brief_digest,
                    brief_version=existing0.brief_version,
                    report_id=existing0.worker_report_id,
                    report_digest=existing0.worker_report_digest,
                    allocation=worktrees.require_allocation(conn, attempt_id),
                    commit=commit, policy_digest=policy_digest, facts=facts,
                    source="synced", command_id=command.id, clock=clock)
            status_after = current_task.status
            outcome = "ready_updated" if status_after == "ready_to_merge" \
                else ("ready" if ready else "pending_checks")
            if ready and current_task.status == "verifying":
                updated = domain.apply_task_event(
                    conn, task.id, DIRECT_PR_READY,
                    expected_version=current_task.version, clock=clock)
                status_after = updated.status
                outcome = "ready"
            events.append_event(conn, "delivery.pr_synced",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt_id,
                payload_json=_canonical_json({
                    "pr_evidence_id": evidence.event_id, "command_id": command.id,
                    "pr_number": evidence.pr_number, "ready": ready,
                    "unchanged": unchanged, "head_sha": evidence.head_sha,
                    "base_ref": evidence.base_ref,
                    "mergeable": evidence.mergeable,
                    "checks_digest": evidence.checks_digest,
                    "task_status_after": status_after}),
                clock=clock)
            commands.confirm_command(conn, command.id, result_json=_canonical_json({
                "pr_evidence_id": evidence.event_id, "ready": ready,
                "task_status_after": status_after}), clock=clock)
            return DirectPrResult(
                outcome=outcome, blocked=False, detail=f"PR #{evidence.pr_number} synced",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt_id,
                task_status=status_after, ready=ready,
                pr_number=evidence.pr_number, pr_url=evidence.pr_url,
                command_id=command.id, pr_evidence=evidence)
    except Exception as exc:
        detail = str(exc).encode("utf-8")[:500].decode("utf-8", errors="ignore")
        try:
            commands.mark_command_unknown(
                conn, command.id, note=f"direct-PR sync interrupted: {detail}",
                clock=clock)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# APPROVE — explicit human merge decision bound to exactly one PR evidence
# ---------------------------------------------------------------------------


def approve_direct_pr_merge(
    conn: TeamCraftConnection,
    pr_evidence_id: str,
    *,
    decision: str,
    pr_number: int | None,
    head_sha: str | None,
    base: str | None,
    actor: str,
    reason: str,
    idempotency_key: str,
    lease: leader.Lease,
    run_gh: github_adapter.GitHubRunner | None = None,
    which: Any = None,
    clock: Clock | None = None,
) -> PrMergeApproval:
    """Append an idempotent human merge decision for one recorded PR.

    An ``approve`` MUST echo the evidence's exact PR number, head SHA and base
    — anything else is refused.  Approval does NOT merge and does NOT complete
    the Task: it only records that a human authorizes exactly this PR/head/base
    merge, leaving the Task READY_TO_MERGE.  Only :func:`merge_direct_pr`
    performs the fenced effect, and only the domain completion gate (re-deriving
    this Approval + verified merge evidence + the intended-base match from
    durable rows) can move the Task to DONE.

    The live PR must still be open, mergeable, checks-passing, and at the bound
    head/base at decision time; any drift invalidates this approval
    opportunity (a fix arrives as a new bounded Attempt with fresh evidence).
    A ``reject`` records the decision, leaves the Task READY_TO_MERGE, and
    closes this evidence version for any later decision.
    """
    if decision not in ("approve", "reject"):
        raise ValidationError("decision must be approve or reject")
    if (not isinstance(actor, str) or not actor.strip()
            or len(actor.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"actor is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"reason is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    if decision == "approve" and (pr_number is None or not head_sha or not base):
        raise ValidationError(
            "merge approval must explicitly echo the bound pr_number, head_sha"
            " and base")
    key = idempotency_key.strip()
    gh = github_adapter.AuditedGhRunner(run_gh or github_adapter.default_run_gh)

    probe = None
    with _tx(conn):
        replayed = _replay_by_key(conn, PR_MERGE_APPROVAL_KIND,
                                  PR_MERGE_APPROVAL_COMMAND_KIND, key)
        if replayed is not None:
            approval = _pr_approval_from_row(conn, int(replayed[0]),
                                            str(replayed[1]), str(replayed[2]))
            if (approval.pr_evidence_id != pr_evidence_id
                    or approval.decision != decision
                    or approval.actor != actor.strip()
                    or approval.reason != reason.strip()):
                raise ConflictError(
                    "idempotency key already owns a different PR merge"
                    " approval; refusing the collision")
            leader.require_project_lease(conn, lease, approval.project_id,
                                        clock=clock)
            return approval

        evidence = get_pr_evidence(conn, pr_evidence_id)
        task = domain.get_task(conn, evidence.task_id)
        attempt = domain.get_attempt(conn, evidence.attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        if task.kind != "ship" or task.status != "ready_to_merge":
            raise IllegalTransitionError(
                "direct-PR merge approval requires a ship Task in"
                " READY_TO_MERGE (PR creation never completes delivery and"
                " approval never merges)",
                entity="task", current=task.status, event="pr_merge_approval")
        latest = load_latest_pr_evidence(conn, task.id)
        if latest is None or latest.event_id != evidence.event_id:
            raise ConflictError(
                "only the LATEST PR evidence row for this Task may be approved;"
                " newer PR facts supersede this approval target")
        if task.version != evidence.task_version + 1:
            raise ConflictError(
                "the Task version moved after this PR evidence was formed"
                f" (evidence bound {evidence.task_version}, task is"
                f" {task.version}); stale evidence cannot be approved")
        _policy, policy_digest, config = _direct_pr_policy(task)
        if policy_digest != evidence.policy_digest:
            raise ConflictError(
                "the execution policy changed after the PR evidence was formed;"
                " stale-policy approval is refused")
        problems = _pr_pin_problems(
            conn, task_id=task.id, attempt_id=evidence.attempt_id,
            task_version=evidence.task_version,
            dispatch_ver=evidence.dispatch_task_version,
            brief_digest=evidence.brief_digest,
            report_id=evidence.worker_report_id,
            report_digest=evidence.worker_report_digest,
            expect_statuses=("ready_to_merge",), version_bump_ok=True)
        if problems:
            raise ConflictError("merge approval refused: " + "; ".join(problems))
        prior = load_pr_merge_approval(conn, evidence.id)
        if prior is not None:
            if (prior.decision == decision and prior.actor == actor.strip()
                    and prior.reason == reason.strip()):
                return prior
            raise ConflictError(
                "this PR evidence version already has an Approval"
                f" ({prior.decision!r}); a settled decision is immutable —"
                " drift or fixes require a new bounded Attempt with fresh"
                " evidence")
        _unresolved_other_key(conn, PR_MERGE_APPROVAL_COMMAND_KIND,
                              evidence.attempt_id, key)
        if decision == "approve":
            if int(pr_number) != evidence.pr_number:
                raise ConflictError(
                    f"approval must echo the bound PR number"
                    f" {evidence.pr_number}, got {pr_number!r}")
            if str(head_sha).lower() != evidence.head_sha:
                raise ConflictError(
                    f"approval must echo the bound head SHA"
                    f" {evidence.head_sha}, got {head_sha!r}")
            if str(base).strip() != evidence.base_ref:
                raise ConflictError(
                    f"approval must echo the bound base"
                    f" {evidence.base_ref!r}, got {base!r}")
            if config["base"] != evidence.base_ref:
                raise ConflictError(
                    f"the PR base {evidence.base_ref!r} is not the intended base"
                    f" {config['base']!r} persisted in the policy; merging into"
                    " an unintended base is never approvable")

    # Live GitHub proof at decision time (outside the transaction).
    repo_root = task_repo_root(conn, task)
    probe = github_adapter.probe_gh(gh, repo_root, resolve=which)
    if probe.blocked:
        raise DirectPrRefusal("gh", f"cannot record a merge decision: {probe.detail}")
    facts, live_problems = _live_pr_facts(gh, repo_root, repo=evidence.repo,
                                         evidence=evidence)
    if facts is None or live_problems:
        raise DirectPrRefusal(
            "drift", "merge approval refused: "
            + "; ".join(live_problems or ["PR unreadable"]))

    with _tx(conn):
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        current_task = domain.get_task(conn, task.id)
        if (current_task.version != task.version
                or current_task.status != "ready_to_merge"):
            raise ConflictError(
                "the Task moved while the approval was being recorded;"
                " refusing the decision")
        if load_pr_merge_approval(conn, evidence.id) is not None:
            raise ConflictError(
                "a merge approval for this evidence raced this one; the settled"
                " decision is the durable one")
        payload_bindings = {
            "project_id": task.project_id, "task_id": task.id,
            "attempt_id": evidence.attempt_id, "attempt_no": evidence.attempt_no,
            "task_version": task.version,
            "pr_evidence_id": evidence.event_id,
            "pr_evidence_digest": evidence.evidence_digest,
            "repo": evidence.repo, "pr_number": evidence.pr_number,
            "head_sha": evidence.head_sha, "base_ref": evidence.base_ref,
            "intended_base": config["base"],
            "checks_digest": evidence.checks_digest,
            "proposed_action": DIRECT_PR_MERGE_ACTION,
            "decision": decision, "actor": actor.strip(),
            "reason": reason.strip(),
            # A merge approval NEVER completes the Task; DONE only arrives via
            # verified merge evidence through the domain gate.
            "task_status_after": "ready_to_merge",
        }
        digest = _sha256_hex(_canonical_json({
            "pr_evidence_digest": evidence.evidence_digest,
            "head_sha": evidence.head_sha, "base_ref": evidence.base_ref,
            "decision": decision}))
        command = commands.submit_command(
            conn, idempotency_key=key, kind=PR_MERGE_APPROVAL_COMMAND_KIND,
            payload_json=_canonical_json(payload_bindings),
            evidence_json=_evidence_envelope(task, attempt,
                                            evidence.dispatch_task_version, digest),
            clock=clock).command
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "PR merge approval Command is already running/uncertain;"
                " reconcile it before retry", entity="command",
                current=command.state, event="pr_merge_approval")
        approval_payload = {**payload_bindings,
                           "schema_version": PR_CONTRACT_VERSION,
                           "command_id": command.id}
        event, _inserted = events.append_event_idempotent(
            conn, PR_MERGE_APPROVAL_KIND, project_id=task.project_id,
            task_id=task.id, attempt_id=evidence.attempt_id,
            payload_json=_canonical_json(approval_payload),
            event_id=_pr_approval_event_id(evidence.id), clock=clock)
        approval = _pr_approval_from_row(conn, int(event.seq), event.event_id,
                                        _canonical_json(approval_payload))
        events.append_event(conn,
            "delivery.pr_merge_approval_recorded" if decision == "approve"
            else "delivery.pr_merge_approval_rejected",
            project_id=task.project_id, task_id=task.id,
            attempt_id=evidence.attempt_id,
            payload_json=_canonical_json({
                "approval_id": approval.id, "command_id": command.id,
                "pr_evidence_id": evidence.id, "pr_number": evidence.pr_number,
                "pr_url": evidence.pr_url, "decision": decision,
                "head_sha": evidence.head_sha, "intended_base": config["base"],
                "checks_digest": evidence.checks_digest,
                "actor": actor.strip(), "reason": reason.strip(),
                "task_status_after": "ready_to_merge",
                "next": ("merge_direct_pr is now permitted for exactly this"
                        " PR/head/base under a current Leader lease")
                       if decision == "approve"
                       else "new bounded Attempt required for any fix"}),
            clock=clock)
        commands.confirm_command(conn, command.id, result_json=_canonical_json({
            "approval_id": approval.id, "decision": decision,
            "task_status_after": "ready_to_merge",
            "pr_number": evidence.pr_number, "head_sha": evidence.head_sha,
            "intended_base": config["base"]}),
            clock=clock)
        return approval


# ---------------------------------------------------------------------------
# MERGE — fenced execution of an approved merge into the INTENDED base only
# ---------------------------------------------------------------------------


def merge_direct_pr(
    conn: TeamCraftConnection,
    pr_evidence_id: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    run_gh: github_adapter.GitHubRunner | None = None,
    which: Any = None,
    clock: Clock | None = None,
) -> PrMergeEvidence:
    """Execute the approved merge of exactly one PR into its intended base.

    Nothing about this call is trusted: the durable Approval must exist and
    bind this exact PR/head/intended base at the Task's CURRENT version; the
    live PR must still be open/mergeable/checks-passing at the bound head and
    base; the merge runs head-guarded (``--match-head-commit``); and DONE is
    recorded ONLY after an authoritative read-back proves the PR merged into the
    INTENDED base with a merge commit.  Any mismatch refuses or leaves the
    effect for :func:`reconcile_pr_merge` — never a blind retry, never a
    second merge, never DONE on the CLI's own say-so.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key.strip()) > 200:
        raise ValidationError("idempotency_key is required and must be at most 200 characters")
    key = idempotency_key.strip()
    gh = github_adapter.AuditedGhRunner(run_gh or github_adapter.default_run_gh)

    with _tx(conn):
        replayed = _replay_by_key(conn, PR_MERGE_EVIDENCE_KIND,
                                  PR_MERGE_COMMAND_KIND, key)
        if replayed is not None:
            evidence = _merge_evidence_from_row(conn, int(replayed[0]),
                                               str(replayed[1]), str(replayed[2]))
            leader.require_project_lease(conn, lease, evidence.project_id,
                                        clock=clock)
            if evidence.pr_evidence_id != pr_evidence_id:
                raise ConflictError(
                    "idempotency key already owns merge evidence for a"
                    " different PR evidence; refusing the collision")
            return evidence

        pr = get_pr_evidence(conn, pr_evidence_id)
        task = domain.get_task(conn, pr.task_id)
        attempt = domain.get_attempt(conn, pr.attempt_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        if task.kind != "ship" or task.status != "ready_to_merge":
            raise IllegalTransitionError(
                "direct-PR merge requires a ship Task in READY_TO_MERGE",
                entity="task", current=task.status, event="pr_merge")
        latest = load_latest_pr_evidence(conn, task.id)
        if latest is None or latest.event_id != pr.event_id:
            raise DirectPrRefusal(
                "stale", "only the latest PR evidence may be merged; newer PR"
                         " facts supersede this merge target")
        approval = load_pr_merge_approval(conn, pr.id)
        if approval is None:
            raise DirectPrRefusal(
                "approval", "no durable human merge Approval exists for this PR"
                           " evidence; merging without an explicit approval is"
                           " refused")
        if approval.decision != "approve":
            raise DirectPrRefusal(
                "approval", f"the recorded decision for this PR is"
                           f" {approval.decision!r}; only approve may merge")
        if approval.task_version != task.version:
            raise DirectPrRefusal(
                "approval",
                f"the Approval is bound to Task version {approval.task_version}"
                f" but the Task is now {task.version}; Task drift invalidates"
                " the approval")
        settled = load_pr_merge_evidence(conn, task.id)
        if settled is not None:
            raise ConflictError(
                "this Task already has durable merge evidence"
                f" ({settled.event_id}); a merge is never executed twice")
        _policy, policy_digest, config = _direct_pr_policy(task)
        if policy_digest != pr.policy_digest:
            raise DirectPrRefusal(
                "stale", "the execution policy changed after the PR evidence"
                        " was formed; stale-policy merge is refused")
        if config["base"] != pr.base_ref or config["repo"] != pr.repo:
            raise DirectPrRefusal(
                "wrong-base",
                f"the persisted intended base/repo is {config['base']!r} in"
                f" {config['repo']!r} but this PR targets {pr.base_ref!r} in"
                f" {pr.repo!r}; merging into an unintended base is refused")
        if (approval.head_sha != pr.head_sha
                or approval.intended_base != pr.base_ref
                or approval.pr_evidence_digest != pr.evidence_digest):
            raise DirectPrRefusal(
                "approval", "the Approval no longer binds this exact PR/head/base"
                           " — head/base/evidence drift invalidates it")
        problems = _pr_pin_problems(
            conn, task_id=task.id, attempt_id=pr.attempt_id,
            task_version=pr.task_version,
            dispatch_ver=pr.dispatch_task_version,
            brief_digest=pr.brief_digest, report_id=pr.worker_report_id,
            report_digest=pr.worker_report_digest,
            expect_statuses=("ready_to_merge",), version_bump_ok=True)
        if problems:
            raise ConflictError("merge refused: " + "; ".join(problems))
        _unresolved_same_key(conn, PR_MERGE_COMMAND_KIND, key)
        _unresolved_other_key(conn, PR_MERGE_COMMAND_KIND, pr.attempt_id, key)
        command = commands.submit_command(
            conn, idempotency_key=key, kind=PR_MERGE_COMMAND_KIND,
            payload_json=_canonical_json({
                "project_id": task.project_id, "task_id": task.id,
                "task_version": task.version, "attempt_id": pr.attempt_id,
                "attempt_no": pr.attempt_no,
                "dispatch_task_version": pr.dispatch_task_version,
                "brief_digest": pr.brief_digest,
                "brief_version": pr.brief_version,
                "worker_report_id": pr.worker_report_id,
                "worker_report_digest": pr.worker_report_digest,
                "policy_digest": policy_digest,
                "pr_evidence_id": pr.event_id, "approval_event_id": approval.id,
                "pr_number": pr.pr_number, "repo": pr.repo,
                "head_sha": pr.head_sha, "intended_base": config["base"],
                "merge_method": config["merge_method"]}),
            evidence_json=_evidence_envelope(task, attempt,
                                            pr.dispatch_task_version,
                                            pr.evidence_digest),
            clock=clock).command
        if command.state == "pending":
            commands.claim_command(conn, command.id, clock=clock)
        else:
            raise IllegalTransitionError(
                "PR merge Command is already running/uncertain; reconcile it"
                " before retry", entity="command", current=command.state,
                event="pr_merge")

    repo_root = task_repo_root(conn, task)
    try:
        probe = github_adapter.probe_gh(gh, repo_root, resolve=which)
        if probe.blocked:
            # Nothing was sent: the claimed Command expires cleanly.
            commands.reconcile_command(
                conn, command.id, outcome="expired",
                note=f"merge not attempted ({probe.detail}); no GitHub effect"
                   " happened", clock=clock)
            raise DirectPrRefusal("gh", probe.detail)
        # Pre-effect live proof: still exactly what the human approved.
        facts, live_problems = _live_pr_facts(gh, repo_root, repo=pr.repo,
                                              evidence=pr)
        if facts is not None and facts.base_ref != config["base"]:
            # A base that is not the intended one is the M4-05 headline risk;
            # classify it precisely, before any generic drift wording.
            commands.reconcile_command(
                conn, command.id, outcome="expired",
                note=f"merge refused: PR base {facts.base_ref!r} is not the"
                   f" intended base {config['base']!r}; nothing merged",
                clock=clock)
            raise DirectPrRefusal(
                "wrong-base",
                f"the PR's live base is {facts.base_ref!r}, not the intended"
                f" base {config['base']!r}; merging into an unintended base is"
                " refused before the effect")
        if facts is None or live_problems:
            detail = "; ".join(live_problems or ["PR unreadable"])
            commands.reconcile_command(
                conn, command.id, outcome="expired",
                note=f"merge not attempted (live check refused): {detail}"[:1900],
                clock=clock)
            raise DirectPrRefusal(
                "drift" if facts is not None else "gh",
                f"merge refused before effect: {detail}")

        result = github_adapter.merge_pr(
            gh, repo_root, repo=pr.repo, number=pr.pr_number,
            head_sha=pr.head_sha, method=config["merge_method"])
        if not result.merged:
            commands.reconcile_command(
                conn, command.id, outcome="expired",
                note=("gh refused the merge (head guard/checks/permissions):"
                    f" {result.raw}")[:1900], clock=clock)
            raise DirectPrRefusal(
                "checks",
                f"gh refused the merge; nothing landed: {result.raw[:200]}")

        # Authoritative read-back: the CLI's own claim is never enough.
        after = github_adapter.read_pr(gh, repo_root, repo=pr.repo,
                                      number=pr.pr_number)
        if not after.merged:
            raise StorageError(
                f"gh reported merged=true for PR #{pr.pr_number} but the"
                " authoritative read-back still shows it open; reconcile the"
                " recorded merge Command")
        if after.base_ref != config["base"]:
            # A merge that landed on the WRONG base is durable truth, but it
            # NEVER completes delivery: audit it, refuse, leave DONE
            # unreachable at this Task version, and keep the Command uncertain
            # for human review.  It is recorded as a dedicated audit kind —
            # never as merge evidence, which by contract describes only a
            # merge into the intended base.
            with _tx(conn):
                leader.require_project_lease(conn, lease, task.project_id,
                                            clock=clock)
                events.append_event(conn, "delivery_pr_wrong_base_merge",
                    project_id=task.project_id, task_id=task.id,
                    attempt_id=pr.attempt_id,
                    payload_json=_canonical_json({
                        "schema_version": PR_CONTRACT_VERSION,
                        "command_id": command.id, "pr_evidence_id": pr.id,
                        "approval_event_id": approval.id,
                        "pr_number": pr.pr_number, "head_sha": pr.head_sha,
                        "merged_into": after.base_ref,
                        "intended_base": config["base"],
                        "merge_commit_sha": after.merge_commit_sha,
                        "merged_at": after.merged_at,
                        "note": "merged into an unintended base; the Task can"
                              " NEVER reach DONE on this evidence"}),
                    clock=clock)
            commands.mark_command_unknown(
                conn, command.id,
                note=("PR merged into"
                    f" {after.base_ref!r}, NOT the intended base"
                    f" {config['base']!r}; DONE refused — human review required"),
                clock=clock)
            raise DirectPrRefusal(
                "wrong-base",
                f"PR #{pr.pr_number} merged into {after.base_ref!r} instead of"
                f" the intended base {config['base']!r}; delivery is NOT"
                " complete and the Task stays READY_TO_MERGE")
        if after.head_sha != pr.head_sha:
            raise StorageError(
                f"PR #{pr.pr_number} read back head {after.head_sha[:12]} after"
                f" merge, not the approved {pr.head_sha[:12]}; reconcile the"
                " merge Command")

        with _tx(conn):
            leader.require_project_lease(conn, lease, task.project_id, clock=clock)
            current_task = domain.get_task(conn, task.id)
            current_attempt = domain.get_attempt(conn, pr.attempt_id)
            current_approval = load_pr_merge_approval(conn, pr.id)
            current_policy = _policy_of(current_task)
            if (current_task.version != task.version
                    or current_task.status != "ready_to_merge"
                    or current_attempt.status != "finished"
                    or current_attempt.attempt_no != pr.attempt_no
                    or current_policy.get("delivery") != "direct_pr"
                    or _policy_digest(current_policy) != policy_digest
                    or current_approval is None
                    or current_approval.id != approval.id
                    or current_approval.task_version != task.version):
                raise ConflictError(
                    "merge facts drifted while the merge ran; refusing to"
                    " record completion (inspect and reconcile)")
            merge_commit = (after.merge_commit_sha or "").lower()
            if not _SHA_RE.match(merge_commit):
                raise StorageError(
                    "authoritative merge read-back carries no merge commit SHA;"
                    " refusing DONE — reconcile the merge Command")
            payload_json = _pr_payload(
                action=DIRECT_PR_MERGE_ACTION, approval_event_id=approval.id,
                attempt_id=pr.attempt_id, attempt_no=pr.attempt_no,
                base_ref=after.base_ref, brief_digest=pr.brief_digest,
                brief_version=pr.brief_version, command_id=command.id,
                delivery_mode="direct_pr",
                dispatch_task_version=pr.dispatch_task_version,
                head_sha=pr.head_sha, intended_base=config["base"],
                merge_commit_sha=merge_commit,
                merged=True, merged_at=after.merged_at,
                method=config["merge_method"], policy_digest=policy_digest,
                pr_evidence_id=pr.event_id, pr_number=pr.pr_number,
                pr_url=after.url or pr.pr_url, project_id=task.project_id,
                repo=pr.repo, source="executed", task_id=task.id,
                task_version=task.version,
                worker_report_digest=pr.worker_report_digest,
                worker_report_id=pr.worker_report_id)
            event, _ins = events.append_event_idempotent(
                conn, PR_MERGE_EVIDENCE_KIND, project_id=task.project_id,
                task_id=task.id, attempt_id=pr.attempt_id,
                payload_json=payload_json,
                event_id=_merge_evidence_event_id(pr.id), clock=clock)
            evidence = _merge_evidence_from_row(conn, int(event.seq),
                                               event.event_id, payload_json)
            events.append_event(conn, "delivery.pr_merged",
                project_id=task.project_id, task_id=task.id,
                attempt_id=pr.attempt_id,
                payload_json=_canonical_json({
                    "merge_evidence_id": evidence.id, "command_id": command.id,
                    "pr_evidence_id": pr.id, "approval_id": approval.id,
                    "pr_number": pr.pr_number, "head_sha": pr.head_sha,
                    "intended_base": config["base"],
                    "merged_into": evidence.base_ref,
                    "merge_commit_sha": evidence.merge_commit_sha,
                    "method": evidence.method}),
                clock=clock)
            # The ONLY path to DONE for direct-PR: the domain gate re-derives
            # merge evidence + approval binding + intended-base match + pins
            # from durable rows inside this same transaction before the CAS.
            updated = domain.apply_task_event(
                conn, task.id, "approved_and_delivered",
                expected_version=task.version, clock=clock)
            if updated.status != "done":
                raise StorageError(
                    "direct-PR merge did not land DONE; rolling the merge"
                    " evidence back")
            commands.confirm_command(conn, command.id, result_json=_canonical_json({
                "merge_evidence_id": evidence.id, "pr_number": pr.pr_number,
                "merge_commit_sha": evidence.merge_commit_sha,
                "merged_into": evidence.base_ref, "task_status_after": "done"}),
                clock=clock)
            return evidence
    except DirectPrRefusal:
        raise
    except Exception as exc:
        detail = str(exc).encode("utf-8")[:500].decode("utf-8", errors="ignore")
        try:
            commands.mark_command_unknown(
                conn, command.id,
                note=f"direct-PR merge interrupted; effect state uncertain:"
                   f" {detail}", clock=clock)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Reconcile — close interrupted PR effects against observed GitHub facts
# ---------------------------------------------------------------------------


def reconcile_pr_receipt(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    lease: leader.Lease,
    actor: str,
    reason: str,
    run_git: GitRunner | None = None,
    run_gh: github_adapter.GitHubRunner | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Close an interrupted prepare/sync Command against live GitHub facts.

    * an OPEN PR from the Attempt branch into the intended base at exactly the
      delivered commit head is ADOPTED as durable evidence
      (``source=reconciled``) — GitHub proves the effect, so a second PR is
      never created; readiness CASes VERIFYING → READY_TO_MERGE when the
      adopted facts are ready;
    * no matching PR (and no foreign remote head) proves the effect did not
      happen: the Command expires and a fresh prepare may follow under a new
      key;
    * ambiguous scenes — multiple matching PRs, a foreign head, drifted pins,
      unreadable GitHub — fail closed and leave the Command uncertain.
    """
    if (not isinstance(actor, str) or not actor.strip()
            or len(actor.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"actor is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"reason is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    gh = github_adapter.AuditedGhRunner(run_gh or github_adapter.default_run_gh)
    with _tx(conn):
        command = commands.get_command(conn, command_id)
        if command.kind not in (PR_PREPARE_COMMAND_KIND, PR_SYNC_COMMAND_KIND):
            raise IllegalTransitionError(
                "command is not a direct-PR prepare/sync run", entity="command",
                current=command.state, event="pr_reconcile")
        try:
            payload = json.loads(command.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("PR Command payload is malformed") from exc
        if not isinstance(payload, dict):
            raise ValidationError("PR Command payload must be a JSON object")
        attempt_id = str(payload.get("attempt_id") or "")
        task_id = str(payload.get("task_id") or "")
        if not attempt_id or not task_id:
            raise ValidationError(
                "PR reconcile requires an attempt- and task-scoped Command")
        task = domain.get_task(conn, task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        existing = conn.execute(
            "SELECT event_id FROM events WHERE kind = ? AND json_extract("
            "payload_json, '$.command_id') = ?",
            (PR_EVIDENCE_KIND, command.id),
        ).fetchone()
        if existing is not None:
            raise ConflictError(
                "this Command already owns durable PR evidence; accepted"
                " results are immutable and must not be reconciled away")
        if command.state in ("confirmed", "expired"):
            return {"resolution": command.state, "command": command,
                    "pr_evidence": None}
        if command.state == "pending":
            raise IllegalTransitionError(
                "command was never claimed/executed; reconcile only from"
                " running/unknown", entity="command", current=command.state,
                event="pr_reconcile")
        _policy, policy_digest, config = _direct_pr_policy(task)
        attempt = domain.get_attempt(conn, attempt_id)
        allocation = _bound_allocation(conn, task, attempt)
        commit = require_commit_evidence(conn, attempt_id)
        commit_sha = str(payload.get("commit_sha") or commit.commit_sha)
        if commit_sha != commit.commit_sha:
            raise ConflictError(
                "the pinned commit no longer matches the delivered commit"
                " evidence; refusing to reconcile against drifted facts")
        problems = _pr_pin_problems(
            conn, task_id=task_id, attempt_id=attempt_id,
            task_version=int(payload.get("task_version") or -1),
            dispatch_ver=int(payload.get("dispatch_task_version") or -1),
            brief_digest=str(payload.get("brief_digest") or ""),
            report_id=int(payload.get("worker_report_id") or -1),
            report_digest=str(payload.get("worker_report_digest") or ""),
            expect_statuses=("verifying",))
        if policy_digest != str(payload.get("policy_digest") or ""):
            problems.append("policy: policy drifted since the Command was written")
        if problems:
            raise ConflictError("PR reconcile refused: " + "; ".join(problems))

    repo_root = task_repo_root(conn, task)
    git = _DirectPrGitRunner(run_git or _default_run_git, allocation.branch)
    remote = _remote_head(git, repo_root)
    if remote is not None and remote != commit_sha:
        raise DirectPrRefusal(
            "ambiguous",
            f"remote branch sits at {remote[:12]} rather than the delivered"
            f" {commit_sha[:12]}; refusing to judge a foreign remote scene")
    try:
        found = github_adapter.find_open_pr(gh, repo_root, repo=config["repo"],
                                           head=allocation.branch,
                                           base=config["base"])
    except github_adapter.GitHubError as exc:
        raise DirectPrRefusal(
            "ambiguous", f"cannot judge PR existence from GitHub: {exc}")

    if found is None:
        note = (f"pr receipt reconciled not-landed by {actor.strip()}:"
               f" {reason.strip()} (no open PR head->intended base)")[:1900]
        with _tx(conn):
            settled = commands.reconcile_command(conn, command.id,
                                                outcome="expired", note=note,
                                                clock=clock)
            events.append_event(conn, "delivery.pr_reconciled",
                project_id=task.project_id, task_id=task.id, attempt_id=attempt_id,
                payload_json=_canonical_json({
                    "command_id": command.id, "resolution": "expired",
                    "actor": actor.strip(),
                    "fact": "no open pull request exists for"
                           f" {allocation.branch!r} -> {config['base']!r};"
                           " no PR effect happened"}),
                clock=clock)
        return {"resolution": "expired", "command": settled,
                "pr_evidence": None}

    if found.head_sha != commit_sha or found.base_ref != config["base"]:
        raise DirectPrRefusal(
            "ambiguous",
            f"open PR #{found.number} carries head {found.head_sha[:12]} into"
            f" {found.base_ref!r}, not the delivered {commit_sha[:12]} into"
            f" {config['base']!r}; refusing to adopt a foreign PR")

    with _tx(conn):
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        current_task = domain.get_task(conn, task_id)
        if current_task.version != task.version or current_task.status not in (
                "verifying", "ready_to_merge"):
            raise ConflictError(
                "Task moved while reconciling the PR receipt; refusing adoption")
        evidence = _persist_pr_evidence(
            conn, task=current_task, attempt=attempt,
            dispatch_ver=int(payload.get("dispatch_task_version") or -1),
            brief_digest=str(payload.get("brief_digest") or ""),
            brief_version=int(payload.get("brief_version") or 1),
            report_id=int(payload.get("worker_report_id") or -1),
            report_digest=str(payload.get("worker_report_digest") or ""),
            allocation=allocation, commit=commit, policy_digest=policy_digest,
            facts=found, source="reconciled", command_id=command.id, clock=clock)
        checks = [dict(c) for c in found.checks]
        checks.sort(key=lambda c: (c["name"], c["status"]))
        ready = bool(found.open and found.mergeable == "MERGEABLE"
                     and _checks_passing(checks))
        status_after = current_task.status
        if ready and current_task.status == "verifying":
            updated = domain.apply_task_event(
                conn, task_id, DIRECT_PR_READY,
                expected_version=current_task.version, clock=clock)
            status_after = updated.status
        note = (f"pr receipt reconciled adopted by {actor.strip()}:"
               f" {reason.strip()}")[:1900]
        settled = commands.reconcile_command(
            conn, command.id, outcome="confirmed", note=note,
            result_json=_canonical_json({
                "pr_evidence_id": evidence.event_id, "pr_number": found.number,
                "head_sha": found.head_sha, "base_ref": found.base_ref,
                "ready": ready, "source": "reconciled",
                "task_status_after": status_after}),
            clock=clock)
        events.append_event(conn, "delivery.pr_reconciled",
            project_id=task.project_id, task_id=task.id, attempt_id=attempt_id,
            payload_json=_canonical_json({
                "command_id": command.id, "resolution": "adopted",
                "pr_evidence_id": evidence.event_id, "pr_number": found.number,
                "pr_url": found.url, "head_sha": found.head_sha,
                "base_ref": found.base_ref, "actor": actor.strip(),
                "ready": ready, "task_status_after": status_after,
                "facts": [f"open PR #{found.number} head->intended base",
                        "remote branch equals the delivered commit",
                        "attempt finished with WorkerReport",
                        "command.claimed event"]}),
            clock=clock)
        return {"resolution": "adopted", "command": settled,
                "pr_evidence": evidence}


def reconcile_pr_merge(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    lease: leader.Lease,
    actor: str,
    reason: str,
    run_gh: github_adapter.GitHubRunner | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Close an interrupted merge Command against the authoritative PR facts.

    * merged into the INTENDED base at the approved head: the merge evidence is
      adopted (``source=reconciled``) and the Task CASes to DONE through the
      same durable-fact gate an executed merge uses — never twice;
    * merged into any OTHER base: durable audit records the truth, the Command
      expires, and DONE stays unreachable at this Task version;
    * still open/closed unmerged: the merge did not happen — the Command
      expires and a fresh merge may follow under a new key once the facts are
      right again.
    """
    if (not isinstance(actor, str) or not actor.strip()
            or len(actor.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"actor is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.encode("utf-8")) > MAX_DELIVERY_AUDIT_BYTES):
        raise ValidationError(f"reason is required and must be at most {MAX_DELIVERY_AUDIT_BYTES} bytes")
    gh = github_adapter.AuditedGhRunner(run_gh or github_adapter.default_run_gh)
    with _tx(conn):
        command = commands.get_command(conn, command_id)
        if command.kind != PR_MERGE_COMMAND_KIND:
            raise IllegalTransitionError(
                "command is not a direct-PR merge run", entity="command",
                current=command.state, event="pr_merge_reconcile")
        try:
            payload = json.loads(command.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("merge Command payload is malformed") from exc
        if not isinstance(payload, dict):
            raise ValidationError("merge Command payload must be a JSON object")
        task_id = str(payload.get("task_id") or "")
        pr_evidence_id = str(payload.get("pr_evidence_id") or "")
        approval_event_id = str(payload.get("approval_event_id") or "")
        if not task_id or not pr_evidence_id or not approval_event_id:
            raise ValidationError(
                "merge reconcile requires task/PR-evidence/approval bindings")
        task = domain.get_task(conn, task_id)
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        existing = conn.execute(
            "SELECT event_id FROM events WHERE kind = ? AND json_extract("
            "payload_json, '$.command_id') = ?",
            (PR_MERGE_EVIDENCE_KIND, command.id),
        ).fetchone()
        if existing is not None:
            raise ConflictError(
                "this Command already owns durable merge evidence; accepted"
                " results are immutable")
        if command.state in ("confirmed", "expired"):
            return {"resolution": command.state, "command": command,
                    "merge_evidence": None}
        if command.state == "pending":
            raise IllegalTransitionError(
                "merge command was never claimed/executed; reconcile only from"
                " running/unknown", entity="command", current=command.state,
                event="pr_merge_reconcile")
        settled_merge = load_pr_merge_evidence(conn, task_id)
        if settled_merge is not None:
            raise ConflictError(
                "this Task already has durable merge evidence"
                f" ({settled_merge.event_id}); never reconcile a second merge")
        pr = get_pr_evidence(conn, pr_evidence_id)
        approval = load_pr_merge_approval(conn, pr.id)
        if (approval is None or approval.id != approval_event_id
                or approval.decision != "approve"):
            raise DirectPrRefusal(
                "approval",
                "the merge Command's bound Approval is missing or not an"
                    " approve; an unapproved merge effect can never complete")
        if approval.task_version != int(payload.get("task_version") or -1):
            raise ConflictError(
                "merge Command/Approval task-version bindings disagree")

    repo_root = task_repo_root(conn, task)
    facts = github_adapter.read_pr(gh, repo_root, repo=pr.repo,
                                   number=pr.pr_number)
    intended = str(payload.get("intended_base") or "")
    head_sha = str(payload.get("head_sha") or "")

    if not facts.merged:
        note = (f"pr merge reconciled not-landed by {actor.strip()}:"
               f" {reason.strip()} (PR #{pr.pr_number} state {facts.state!r})")[:1900]
        with _tx(conn):
            settled = commands.reconcile_command(conn, command.id,
                                                outcome="expired", note=note,
                                                clock=clock)
            events.append_event(conn, "delivery.pr_merge_reconciled",
                project_id=task.project_id, task_id=task.id,
                attempt_id=pr.attempt_id,
                payload_json=_canonical_json({
                    "command_id": command.id, "resolution": "expired",
                    "pr_number": pr.pr_number, "pr_state": facts.state,
                    "actor": actor.strip(),
                    "fact": "the PR is not merged; no merge effect happened"}),
                clock=clock)
        return {"resolution": "expired", "command": settled,
                "merge_evidence": None}

    if facts.base_ref != intended or facts.head_sha != head_sha:
        with _tx(conn):
            events.append_event(conn, "delivery_pr_wrong_base_merge",
                project_id=task.project_id, task_id=task.id,
                attempt_id=pr.attempt_id,
                payload_json=_canonical_json({
                    "schema_version": PR_CONTRACT_VERSION,
                    "command_id": command.id, "pr_evidence_id": pr.id,
                    "approval_event_id": approval.id,
                    "pr_number": pr.pr_number, "head_sha": facts.head_sha,
                    "approved_head_sha": head_sha,
                    "merged_into": facts.base_ref, "intended_base": intended,
                    "merge_commit_sha": facts.merge_commit_sha,
                    "merged_at": facts.merged_at,
                    "actor": actor.strip(),
                    "note": "merged outside the intended base/head; the Task can"
                          " NEVER reach DONE on this evidence"}),
                clock=clock)
            settled = commands.reconcile_command(
                conn, command.id, outcome="expired",
                note=("reconciled wrong-base merge by"
                    f" {actor.strip()}: merged into {facts.base_ref!r}, not"
                    f" {intended!r}; DONE refused")[:1900], clock=clock)
        return {"resolution": "wrong_base", "command": settled,
                "merge_evidence": None}

    with _tx(conn):
        leader.require_project_lease(conn, lease, task.project_id, clock=clock)
        current_task = domain.get_task(conn, task_id)
        if current_task.status != "ready_to_merge" or current_task.version != task.version:
            raise ConflictError(
                "Task moved while reconciling the merge; refusing adoption")
        _policy, policy_digest, config = _direct_pr_policy(current_task)
        if config["base"] != intended or policy_digest != pr.policy_digest:
            raise ConflictError(
                "intended base/policy drifted while reconciling the merge")
        merge_commit = (facts.merge_commit_sha or "").lower()
        if not _SHA_RE.match(merge_commit):
            raise StorageError(
                "authoritative merge read-back carries no merge commit SHA;"
                " reconcile cannot adopt without proof of the merge commit")
        payload_json = _pr_payload(
            action=DIRECT_PR_MERGE_ACTION, approval_event_id=approval.id,
            attempt_id=pr.attempt_id, attempt_no=pr.attempt_no,
            base_ref=facts.base_ref, brief_digest=pr.brief_digest,
            brief_version=pr.brief_version, command_id=command.id,
            delivery_mode="direct_pr",
            dispatch_task_version=int(payload.get("dispatch_task_version") or -1),
            head_sha=head_sha, intended_base=intended,
            merge_commit_sha=merge_commit, merged=True, merged_at=facts.merged_at,
            method=str(payload.get("merge_method") or "merge"),
            policy_digest=policy_digest, pr_evidence_id=pr.event_id,
            pr_number=pr.pr_number, pr_url=facts.url or pr.pr_url,
            project_id=task.project_id, repo=pr.repo, source="reconciled",
            task_id=task_id, task_version=current_task.version,
            worker_report_digest=pr.worker_report_digest,
            worker_report_id=pr.worker_report_id)
        event, _ins = events.append_event_idempotent(
            conn, PR_MERGE_EVIDENCE_KIND, project_id=task.project_id,
            task_id=task_id, attempt_id=pr.attempt_id,
            payload_json=payload_json,
            event_id=_merge_evidence_event_id(pr.id), clock=clock)
        evidence = _merge_evidence_from_row(conn, int(event.seq), event.event_id,
                                           payload_json)
        events.append_event(conn, "delivery.pr_merge_reconciled",
            project_id=task.project_id, task_id=task_id, attempt_id=pr.attempt_id,
            payload_json=_canonical_json({
                "command_id": command.id, "resolution": "adopted",
                "merge_evidence_id": evidence.id, "pr_number": pr.pr_number,
                "merge_commit_sha": merge_commit, "merged_into": facts.base_ref,
                "intended_base": intended, "actor": actor.strip(),
                "facts": ["PR merged into the intended base",
                        "head equals the approved head",
                        "durable human Approval present",
                        "command.claimed event"]}),
            clock=clock)
        updated = domain.apply_task_event(
            conn, task_id, "approved_and_delivered",
            expected_version=current_task.version, clock=clock)
        if updated.status != "done":
            raise StorageError(
                "reconciled merge did not land DONE; rolling the adoption back")
        settled = commands.reconcile_command(
            conn, command.id, outcome="confirmed",
            note=(f"pr merge reconciled adopted by {actor.strip()}:"
                 f" {reason.strip()}")[:1900],
            result_json=_canonical_json({
                "merge_evidence_id": evidence.id, "pr_number": pr.pr_number,
                "merge_commit_sha": merge_commit, "merged_into": facts.base_ref,
                "task_status_after": "done"}),
            clock=clock)
        return {"resolution": "adopted", "command": settled,
                "merge_evidence": evidence}


# ---------------------------------------------------------------------------
# Direct-PR currency helpers (head/base/check/policy/Task drift invalidates)
# ---------------------------------------------------------------------------


def pr_evidence_is_current(conn: TeamCraftConnection,
                          evidence: PullRequestEvidence, *,
                          live_gh: bool = True,
                          run_gh: github_adapter.GitHubRunner | None = None,
                          which: Any = None) -> DeliveryFreshness:
    """Whether durable PR evidence still matches live Task/GitHub facts."""
    problems: list[str] = []
    try:
        task = domain.get_task(conn, evidence.task_id)
    except (NotFoundError, ValidationError):
        return DeliveryFreshness(ok=False, problems=("task: missing",))
    if task.status not in ("verifying", "ready_to_merge"):
        problems.append(f"status: task is {task.status!r}")
    elif task.status == "ready_to_merge" and task.version != evidence.task_version + 1:
        problems.append(
            f"version: ready Task is v{task.version} but evidence formed at"
            f" v{evidence.task_version} without the readiness CAS")
    elif task.status == "verifying" and task.version != evidence.task_version:
        problems.append(f"version: task moved to v{task.version}")
    problems.extend(_pr_pin_problems(
        conn, task_id=evidence.task_id, attempt_id=evidence.attempt_id,
        task_version=evidence.task_version,
        dispatch_ver=evidence.dispatch_task_version,
        brief_digest=evidence.brief_digest,
        report_id=evidence.worker_report_id,
        report_digest=evidence.worker_report_digest,
        expect_statuses=("verifying", "ready_to_merge"), check_version=False))
    try:
        _policy, policy_digest, config = _direct_pr_policy(task)
        if policy_digest != evidence.policy_digest:
            problems.append("policy: execution policy drifted")
        elif (config["repo"] != evidence.repo
                or config["base"] != evidence.base_ref):
            problems.append("policy: direct_pr repo/base drifted from the evidence")
    except DeliveryRefusal as exc:
        problems.append(f"policy: {exc}")
    try:
        commit = require_commit_evidence(conn, evidence.attempt_id)
        if (commit.event_id != evidence.commit_evidence_id
                or commit.commit_sha != evidence.commit_sha):
            problems.append("commit: evidence chain drifted")
    except (NotFoundError, StorageError) as exc:
        problems.append(f"commit: {exc}")
    latest = load_latest_pr_evidence(conn, evidence.task_id)
    if latest is not None and latest.event_id != evidence.event_id:
        problems.append("evidence: a newer PR observation supersedes this row")
    if live_gh and task.status in ("verifying", "ready_to_merge"):
        gh = github_adapter.AuditedGhRunner(run_gh or github_adapter.default_run_gh)
        probe = github_adapter.probe_gh(gh, task_repo_root(conn, task),
                                        resolve=which)
        if probe.blocked:
            problems.append(f"gh: {probe.detail}")
        else:
            _facts, live_problems = _live_pr_facts(
                gh, task_repo_root(conn, task), repo=evidence.repo,
                evidence=evidence)
            problems.extend(live_problems)
    return DeliveryFreshness(ok=not problems, problems=tuple(problems))


def pr_merge_approval_is_current(
    conn: TeamCraftConnection,
    approval: PrMergeApproval, *,
    live_gh: bool = True,
    run_gh: github_adapter.GitHubRunner | None = None,
    which: Any = None,
) -> DeliveryFreshness:
    """Whether an approved/rejected merge decision still stands against facts.

    An approval is current while its Task sits READY_TO_MERGE at exactly the
    bound version with the bound PR unchanged; after a verified merge it is
    current only while the Task is DONE at ``version + 1`` with merge evidence
    naming this approval and the intended base.  Head/base/check/policy/Task
    drift shows up here as invalidation, exactly like M4-04 approvals.
    """
    problems: list[str] = []
    try:
        pr = get_pr_evidence(conn, approval.pr_evidence_id)
    except (NotFoundError, StorageError) as exc:
        return DeliveryFreshness(ok=False, problems=(f"evidence: {exc}",))
    if approval.pr_evidence_digest != pr.evidence_digest:
        problems.append("evidence: approval no longer binds this evidence version")
    if (approval.head_sha != pr.head_sha or approval.base_ref != pr.base_ref
            or approval.pr_number != pr.pr_number):
        problems.append("binding: approval head/base/PR differ from evidence")
    if approval.intended_base != pr.base_ref:
        problems.append("binding: intended base does not equal the PR base")
    task = domain.get_task(conn, approval.task_id)
    merge = load_pr_merge_evidence(conn, approval.task_id)
    if merge is None:
        if task.status != "ready_to_merge" or task.version != approval.task_version:
            problems.append(
                f"status: task is {task.status!r} v{task.version}, approval"
                f" bound ready v{approval.task_version}")
        latest = load_latest_pr_evidence(conn, approval.task_id)
        if latest is None or latest.event_id != pr.event_id:
            problems.append("evidence: approval targets a superseded PR row")
        if approval.decision == "approve" and live_gh:
            gh = github_adapter.AuditedGhRunner(run_gh
                                               or github_adapter.default_run_gh)
            probe = github_adapter.probe_gh(gh, task_repo_root(conn, task),
                                           resolve=which)
            if probe.blocked:
                problems.append(f"gh: {probe.detail}")
            else:
                _facts, live_problems = _live_pr_facts(
                    gh, task_repo_root(conn, task), repo=pr.repo, evidence=pr)
                problems.extend(live_problems)
    else:
        if (merge.approval_event_id != approval.id
                or merge.pr_evidence_id != pr.event_id):
            problems.append("merge: merge evidence does not name this approval")
        if merge.intended_base != approval.intended_base:
            problems.append("merge: intended base drifted")
        if task.status != "done" or task.version != approval.task_version + 1:
            problems.append(
                f"status: merged task is {task.status!r} v{task.version},"
                f" expected done v{approval.task_version + 1}")
    if approval.decision == "approve":
        freshness = pr_evidence_is_current(conn, pr, live_gh=False)
        problems.extend(freshness.problems)
    return DeliveryFreshness(ok=not problems, problems=tuple(problems))


# ===========================================================================
# M4-06 — integration conflict detection + evidence-gated safe cleanup.
#
# Bounded questions this slice answers, fail-closed:
#
#   "Has the base this Attempt was branched from moved, and does its branch
#   integrate cleanly?  If integration work is required, where is the ONE
#   explicit Task that says so?  And which workspace may be released — ONLY
#   when it is exactly OURS, completely clean, carries no undelivered
#   commits, and its delivery is DONE with complete current evidence?"
#
# Pinned decisions:
#
# * NEVER integrate automatically.  Drift and conflicts are DETECTED (live
#   ``rev-parse``/``merge-base`` and the in-memory ``git merge-tree`` probe
#   through M4-02/M4-01 plumbing) and surfaced as durable facts; the response
#   is an explicit integration Task, never a silent merge, rebase, reset, or
#   overwrite (those verbs stay inside FORBIDDEN_GIT_VERBS / FORBIDDEN_EFFECT_VERBS).
# * Integration requirement records are idempotent per Attempt: one durable
#   ``integration_required`` event keys the ONE integration Task
#   (deterministic Task id).  Re-running detection appends digest-keyed
#   ``integration_check`` audit rows (identical facts converge, changed facts
#   land as new observations) but can never create a second Task.
# * Cleanup (workspace release) is gated on the FULL matrix — CLEANABLE
#   lifecycle, exact identity claim, clean worktree, terminal Attempt, DONE
#   delivery Task, current approval/merge facts matching the delivered
#   Commit, and branch tip == delivered commit (no undelivered commits).  Any
#   refusal preserves the workspace AND lands a durable
#   ``worktree_cleanup_refused`` audit fact; the only removal verb is the
#   narrowly-guarded ``git worktree remove`` (never ``--force``) inside
#   :func:`teamcraft.worktrees.finalize_cleaned_worktree`, whose crash window
#   after the effect is adopted by reconcile.  Branches and commits are never
#   deleted: the delivery record outlives the workspace.
# ===========================================================================


CLEANUP_REFUSED_EVENT_KIND = "worktree_cleanup_refused"

#: Durable integration audit kinds (canonical names live alongside the Git
#: probes in :mod:`teamcraft.worktrees`; re-exported here for callers of the
#: delivery-facing surface).
INTEGRATION_CHECK_EVENT_KIND = worktrees.INTEGRATION_CHECK_EVENT_KIND
INTEGRATION_REQUIRED_EVENT_KIND = worktrees.INTEGRATION_REQUIRED_EVENT_KIND


@dataclass(frozen=True)
class IntegrationStatus:
    """One detection pass: recorded vs live base + integration verdict."""

    attempt_id: str
    task_id: str
    project_id: str
    branch: str
    recorded_base_sha: str
    live_base_sha: str | None
    base_drift: bool
    merge_status: str
    conflict_files: tuple[str, ...]
    integration_required: bool
    integration_task_id: str | None
    problems: tuple[str, ...]


class _FixedIdFactory:
    """IdFactory emitting one deterministic id (single-shot Task creation)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def new(self, namespace: str) -> str:
        return self._value


def _integration_check_event_id(payload_json: str) -> str:
    return ("evt-itchk-"
            + _sha256_hex(f"integration-check|{payload_json}")[:40])


def _integration_required_event_id(attempt_id: str) -> str:
    return ("evt-itgr-"
            + _sha256_hex(f"integration-required|{attempt_id}")[:40])


def _integration_task_id(attempt_id: str) -> str:
    return "itg-" + _sha256_hex(f"integration-task|{attempt_id}")[:24]


_INTEGRATION_CHECK_FIELDS = frozenset({
    "attempt_id", "branch", "conflict_files", "integration_required",
    "integration_task_id", "live_base_sha", "merge_status", "problems",
    "project_id", "recorded_base_sha", "schema_version", "source_task_id",
})


def load_integration_requirement(
    conn: TeamCraftConnection, attempt_id: str
) -> tuple[str, str] | None:
    """``(integration_task_id, event_id)`` when one was ever required.

    Malformed requirement records fail closed (StorageError) — a corrupt
    audit trail is never silently ignored.
    """
    require_entity_id(attempt_id)
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events WHERE kind = ?"
        " AND attempt_id = ? ORDER BY seq ASC",
        (INTEGRATION_REQUIRED_EVENT_KIND, attempt_id),
    ).fetchall()
    if len(rows) > 1:
        raise StorageError(
            f"{len(rows)} integration-required records for attempt"
            f" {attempt_id}; storage invariant broken")
    if not rows:
        return None
    try:
        payload = json.loads(str(rows[0][2]))
    except (TypeError, ValueError) as exc:
        raise StorageError("integration-required payload is not valid JSON") from exc
    if (not isinstance(payload, dict)
            or payload.get("schema_version") != DELIVERY_CONTRACT_VERSION
            or payload.get("attempt_id") != attempt_id):
        raise StorageError("integration-required payload has unexpected shape")
    task_id = payload.get("integration_task_id")
    if not isinstance(task_id, str) or not task_id:
        raise StorageError("integration-required payload lacks its Task binding")
    return task_id, str(rows[0][1])


def check_integration_state(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
    audit: bool = True,
) -> IntegrationStatus:
    """Detect base drift + merge conflicts for one Attempt (read-only).

    Issues only ``rev-parse``/``merge-base``/``merge-tree`` plumbing — no
    worktree, index, ref, or remote moves.  With ``audit`` (default) every
    pass lands a digest-keyed ``integration_check`` fact: identical
    observations converge onto one row, changed facts append new ones, so the
    detection history itself is auditable and replay-safe.
    """
    runner = _AuditedRunner(run_git or _default_run_git)
    allocation = worktrees.require_allocation(conn, attempt_id)
    task = domain.get_task(conn, allocation.task_id)
    drift = worktrees.detect_base_drift(conn, attempt_id, run_git=runner)
    merge = worktrees.check_merge_conflicts(conn, attempt_id,
                                           base_sha=drift.live_base_sha,
                                           run_git=runner)
    required = bool(drift.drifted or merge.conflicting)
    requirement = load_integration_requirement(conn, attempt_id)
    problems = tuple(drift.problems) + tuple(merge.problems)
    status = IntegrationStatus(
        attempt_id=attempt_id, task_id=task.id, project_id=task.project_id,
        branch=allocation.branch,
        recorded_base_sha=allocation.base_sha,
        live_base_sha=drift.live_base_sha, base_drift=drift.drifted,
        merge_status=merge.status, conflict_files=merge.conflict_files,
        integration_required=required,
        integration_task_id=requirement[0] if requirement else None,
        problems=problems)
    if audit:
        payload = _canonical_json({
            "attempt_id": attempt_id, "branch": status.branch,
            "conflict_files": list(status.conflict_files),
            "integration_required": required,
            "integration_task_id": status.integration_task_id,
            "live_base_sha": status.live_base_sha,
            "merge_status": status.merge_status, "problems": list(problems),
            "project_id": status.project_id,
            "recorded_base_sha": status.recorded_base_sha,
            "schema_version": DELIVERY_CONTRACT_VERSION,
            "source_task_id": task.id,
        })
        events.append_event_idempotent(
            conn, INTEGRATION_CHECK_EVENT_KIND, project_id=task.project_id,
            task_id=task.id, attempt_id=attempt_id, payload_json=payload,
            event_id=_integration_check_event_id(payload), clock=clock)
    return status


def ensure_integration_task(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> tuple[IntegrationStatus, domain.Task | None, bool]:
    """Detection + the explicit, idempotent integration Task when required.

    Returns ``(status, task, created)``.  When integration is not required no
    Task is created (``task=None, created=False``) — but if a requirement was
    EVER recorded it is still returned (the Task stands until a human closes
    it; detection never auto-cancels).  The requirement event and the Task
    commit in one transaction: a crash leaves either both or neither, and a
    retry converges onto the same deterministic Task id.  Conflicts are only
    ever REPORTED here — nothing merges, rebases, or overwrites.
    """
    runner = _AuditedRunner(run_git or _default_run_git)
    status = check_integration_state(conn, attempt_id, run_git=runner,
                                     clock=clock)
    existing = load_integration_requirement(conn, attempt_id)
    if status.integration_task_id is None and existing is not None:
        # A requirement predates this pass (e.g. drift since healed).
        return status, domain.get_task(conn, existing[0]), False
    if not status.integration_required:
        return status, None, False

    source_task = domain.get_task(conn, status.task_id)
    integration_task_id = _integration_task_id(attempt_id)
    with _tx(conn):
        raced = load_integration_requirement(conn, attempt_id)
        if raced is not None:
            return status, domain.get_task(conn, raced[0]), False
        trigger = ("merge_conflict" if status.conflict_files else "base_drift")
        acceptance = [
            (f"resolve {status.merge_status} between {status.branch} and live"
             f" base {status.live_base_sha}" if status.conflict_files else
             f"reintegrate {status.branch} onto live base"
             f" {status.live_base_sha}"),
            ("re-verify the integrated result and re-deliver with fresh"
             " evidence; never force-overwrite"),
        ]
        task = domain.create_task(
            conn, source_task.project_id,
            f"Integrate {source_task.title} after {trigger}",
            description=(
                f"M4-06 integration follow-up for attempt {attempt_id} "
                f"(branch {status.branch}): recorded base "
                f"{status.recorded_base_sha}, live base "
                f"{status.live_base_sha}, merge probe {status.merge_status}"
                + (f"; conflicting paths: "
                   f"{', '.join(status.conflict_files)}"
                   if status.conflict_files else "")
                + ". Integration is explicit human-directed work: this Task "
                  "never authorizes automatic merges or overwrites."),
            acceptance_json=json.dumps(acceptance, ensure_ascii=False),
            ownership_json=json.dumps(list(_allocation_claims(
                conn, attempt_id))),
            risk="high",
            id_factory=_FixedIdFactory(integration_task_id),
            clock=clock,
        )
        payload = _canonical_json({
            "attempt_id": attempt_id,
            "integration_task_id": task.id,
            "project_id": source_task.project_id,
            "schema_version": DELIVERY_CONTRACT_VERSION,
            "source_task_id": source_task.id,
            "trigger": trigger,
        })
        event, _inserted = events.append_event_idempotent(
            conn, INTEGRATION_REQUIRED_EVENT_KIND,
            project_id=source_task.project_id, task_id=source_task.id,
            attempt_id=attempt_id, payload_json=payload,
            event_id=_integration_required_event_id(attempt_id), clock=clock)
        if event.event_id != _integration_required_event_id(attempt_id):
            raise StorageError(
                "integration requirement did not persist as expected;"
                " storage invariant broken")
    return status, task, True


def _allocation_claims(conn: TeamCraftConnection,
                       attempt_id: str) -> tuple[str, ...]:
    return worktrees.require_allocation(conn, attempt_id).claims


# ---------------------------------------------------------------------------
# Evidence-gated cleanup eligibility + safe release
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupEligibility:
    """Whether one workspace may be released, and why not if it may not."""

    attempt_id: str
    eligible: bool
    state: str   # cleaned | eligible | refused
    reasons: tuple[str, ...]
    cleaned: worktrees.WorktreeCleanedRecord | None


def _current_delivery_facts(
    conn: TeamCraftConnection, task: domain.Task, attempt_id: str, *,
    run_git: GitRunner, live_git: bool = True,
) -> tuple[list[str], str | None, str]:
    """(problems, delivered commit sha, delivery mode) for the DONE gate.

    A DONE ship Task must still stand on a CURRENT evidence chain:
    ``local_only`` needs the accepted, still-current Approval on exactly the
    bound Commit; ``direct_pr`` needs merged evidence whose approval chain is
    current (evaluated without live gh calls so restart has no network
    dependency); other modes have no cleanup story in this milestone and are
    refused fail-closed.  ``live_git=False`` (reconcile) keeps every DURABLE
    pin but skips workspace-bound re-proofs — the workspace is already gone,
    and the effect-time branch-tip re-proof covers commit integrity.
    """
    problems: list[str] = []
    policy = _policy_of(task)
    mode = str(policy.get("delivery") or "")
    ship = load_ship_evidence(conn, attempt_id)
    if mode == "local_only":
        if ship is None:
            problems.append("ship-evidence-missing")
            return problems, None, mode
        approval = load_delivery_approval(conn, ship.id)
        if approval is None:
            problems.append("approval-missing")
        elif approval.decision != "accept":
            problems.append(f"approval-{approval.decision}")
        else:
            fresh = delivery_approval_is_current(conn, approval,
                                                live_git=live_git,
                                                run_git=run_git)
            if not fresh.ok:
                problems.extend(fresh.problems)
        return problems, ship.commit_sha, mode
    if mode == "direct_pr":
        merge = load_pr_merge_evidence(conn, task.id)
        if merge is None or merge.attempt_id != attempt_id:
            problems.append("merge-evidence-missing")
            return problems, None, mode
        if not merge.merged:
            problems.append("merge-evidence-not-merged")
        approval = load_pr_merge_approval(conn, merge.pr_evidence_id)
        if approval is None or approval.decision != "approve":
            problems.append("merge-approval-missing")
        else:
            fresh = pr_merge_approval_is_current(conn, approval, live_gh=False)
            if not fresh.ok:
                problems.extend(fresh.problems)
        return problems, merge.head_sha, mode
    problems.append(f"delivery-mode-{mode or 'unset'}-not-cleanup-supported")
    return problems, None, mode


def evaluate_cleanup_eligibility(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    run_git: GitRunner | None = None,
    workspace_expected: bool = True,
    live_git: bool = True,
) -> CleanupEligibility:
    """The full CLEANABLE + DONE + evidence + identity matrix, read-only.

    Nothing is deleted here; every failing check contributes a stable reason
    tag.  Dirty, unmerged, unknown, unregistered, foreign, moved, still
    running, undelivered, unapproved, or DONE-but-stale workspaces are all
    PRESERVED (``eligible=False``).

    ``workspace_expected=False`` (reconcile) admits a MISSING workspace — the
    crash-after-effect window — and with it ``live_git=False`` keeps only the
    durable evidence pins; the branch-tip re-proof still runs.
    """
    runner = _AuditedRunner(run_git or _default_run_git)
    allocation = worktrees.load_allocation(conn, attempt_id)
    if allocation is None:
        return CleanupEligibility(attempt_id=attempt_id, eligible=False,
                                  state="refused",
                                  reasons=("no-allocation",), cleaned=None)
    cleaned = worktrees.load_cleaned_record(conn, attempt_id)
    if cleaned is not None:
        return CleanupEligibility(attempt_id=attempt_id, eligible=True,
                                  state="cleaned", reasons=("already-cleaned",),
                                  cleaned=cleaned)
    reasons: list[str] = []
    attempt = domain.get_attempt(conn, attempt_id)
    if attempt.status not in domain.TERMINAL_ATTEMPT_STATUSES:
        reasons.append(f"attempt-{attempt.status}")
    if worktrees.lifecycle_state(conn, attempt_id) != "cleanable":
        reasons.append("worktree-not-cleanable")
    task = domain.get_task(conn, allocation.task_id)
    if task.status != "done":
        reasons.append(f"delivery-task-{task.status}")
        return CleanupEligibility(attempt_id=attempt_id, eligible=False,
                                  state="refused",
                                  reasons=tuple(reasons), cleaned=None)
    if not os.path.exists(allocation.worktree_path):
        # The workspace is already gone.  A plain cleanup must NOT fabricate
        # the fact (something outside our flow removed it); only reconcile
        # may adopt — and only when every DURABLE fact re-proves and the
        # branch still pins exactly the delivered Commit.
        if workspace_expected:
            reasons.append("workspace-missing")
        else:
            registered = worktrees._registered_worktrees(
                runner, allocation.repo_root)
            if os.path.realpath(allocation.worktree_path) in registered:
                reasons.append("workspace-unpruned-registration")
        delivery_problems, commit_sha, _mode = _current_delivery_facts(
            conn, task, attempt_id, run_git=runner, live_git=False)
        reasons.extend(delivery_problems)
        if commit_sha is not None:
            tip = worktrees.branch_sha(runner, allocation.repo_root,
                                      allocation.branch)
            if tip != commit_sha:
                reasons.append(
                    f"undelivered-commits: branch tip {tip!r} != delivered "
                    f"{commit_sha!r}")
        if reasons:
            return CleanupEligibility(attempt_id=attempt_id, eligible=False,
                                      state="refused", reasons=tuple(reasons),
                                      cleaned=None)
        return CleanupEligibility(attempt_id=attempt_id, eligible=True,
                                  state="eligible", reasons=(), cleaned=None)

    delivery_problems, commit_sha, _mode = _current_delivery_facts(
        conn, task, attempt_id, run_git=runner, live_git=live_git)
    reasons.extend(delivery_problems)
    if not reasons:
        # Identity + cleanliness + no undelivered commits, live.
        try:
            worktrees.validate_worktree_identity(
                conn, attempt_id, run_git=runner)
        except (worktrees.WorktreeIdentityError, workspaces.GitStateError,
               StorageError, NotFoundError) as exc:
            kind = getattr(exc, "kind", None) or "identity-unreadable"
            reasons.append(f"identity-{kind}")
            reasons.append(f"identity-detail: {exc}")
        else:
            try:
                state = workspaces.collect_workspace_state(
                    allocation.worktree_path, run_git=runner)
            except workspaces.GitStateError as exc:
                reasons.append(f"git-state: {exc}")
            else:
                if state.changes:
                    preview = ", ".join(entry.path for entry in state.changes[:5])
                    reasons.append(
                        f"workspace-dirty({len(state.changes)}): {preview}")
                tip = worktrees.branch_sha(runner, allocation.repo_root,
                                          allocation.branch)
                if commit_sha is not None and tip != commit_sha:
                    reasons.append(
                        f"undelivered-commits: branch tip {tip!r} != "
                        f"delivered {commit_sha!r}")
    if reasons:
        return CleanupEligibility(attempt_id=attempt_id, eligible=False,
                                  state="refused", reasons=tuple(reasons),
                                  cleaned=None)
    return CleanupEligibility(attempt_id=attempt_id, eligible=True,
                              state="eligible", reasons=(), cleaned=None)


def _refuse_cleanup(conn: TeamCraftConnection, attempt_id: str,
                    reasons: tuple[str, ...], *, clock: Clock | None) -> None:
    """Durable audit fact for a refused cleanup (never silent)."""
    allocation = worktrees.load_allocation(conn, attempt_id)
    task_id = allocation.task_id if allocation is not None else None
    payload = _canonical_json({
        "attempt_id": attempt_id,
        "project_id": allocation.project_id if allocation else None,
        "reasons": list(reasons),
        "schema_version": DELIVERY_CONTRACT_VERSION,
        "task_id": task_id,
    })
    events.append_event(
        conn, CLEANUP_REFUSED_EVENT_KIND,
        project_id=allocation.project_id if allocation else None,
        task_id=task_id, attempt_id=attempt_id, payload_json=payload,
        clock=clock)


def cleanup_attempt_worktree(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    lease: leader.Lease,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
    reconcile: bool = False,
) -> dict[str, Any]:
    """Release exactly the CLEANABLE workspace when the gate is fully green.

    Order: fence on the project Leader lease → full eligibility matrix →
    delivered Commit re-derived from durable evidence → guarded removal in
    :func:`teamcraft.worktrees.finalize_cleaned_worktree` (exact identity +
    clean + branch-tip re-proof at effect time; ``git worktree remove``
    without force; deterministic CLEANED fact; crash-after-effect adopted
    when ``reconcile=True`` — or reported for reconcile otherwise).  Every
    refusal lands ``worktree_cleanup_refused`` audit and preserves the scene.
    """
    runner = _AuditedRunner(run_git or _default_run_git)
    attempt_id = require_entity_id(attempt_id)
    allocation = worktrees.require_allocation(conn, attempt_id)
    leader.require_project_lease(conn, lease, allocation.project_id,
                                 clock=clock)
    eligibility = evaluate_cleanup_eligibility(
        conn, attempt_id, run_git=runner,
        workspace_expected=not reconcile, live_git=not reconcile)
    if eligibility.state == "cleaned":
        assert eligibility.cleaned is not None
        return {"attempt_id": attempt_id, "disposition": "replayed",
                "cleaned": _cleaned_dict(eligibility.cleaned)}
    if not eligibility.eligible:
        _refuse_cleanup(conn, attempt_id, eligibility.reasons, clock=clock)
        raise DeliveryRefusal(
            "settled" if "already-cleaned" in eligibility.reasons else "drift",
            "cleanup refused for attempt"
            f" {attempt_id}: " + "; ".join(eligibility.reasons))

    task = domain.get_task(conn, allocation.task_id)
    ship = load_ship_evidence(conn, attempt_id)
    commit = load_commit_evidence(conn, attempt_id)
    mode = str(_policy_of(task).get("delivery") or "")
    if mode == "direct_pr":
        merge = load_pr_merge_evidence(conn, task.id)
        commit_sha = merge.head_sha if merge is not None else None
    else:
        commit_sha = (ship.commit_sha if ship is not None
                     else (commit.commit_sha if commit is not None else None))
    if commit_sha is None:
        reasons = ("delivered-commit-unresolvable",)
        _refuse_cleanup(conn, attempt_id, reasons, clock=clock)
        raise DeliveryRefusal("ambiguous",
                             f"cleanup refused for attempt {attempt_id}:"
                             " delivered commit could not be derived from"
                             " durable evidence")
    try:
        record, disposition = worktrees.finalize_cleaned_worktree(
            conn, attempt_id, commit_sha=commit_sha, delivery_mode=mode,
            run_git=runner, clock=clock)
    except (IllegalTransitionError, StorageError) as exc:
        # The effect did NOT land (git refused / facts moved): preserve the
        # scene, audit the failure, and re-raise verbatim — the same call
        # succeeds (or converges onto CLEANED) once the cause is fixed.
        _refuse_cleanup(conn, attempt_id, (f"finalize-failed: {exc}",),
                        clock=clock)
        raise
    return {"attempt_id": attempt_id, "disposition": disposition,
            "cleaned": _cleaned_dict(record)}


def reconcile_cleanup_worktree(
    conn: TeamCraftConnection,
    attempt_id: str,
    *,
    lease: leader.Lease,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Recover a cleanup that landed without its fact (crash window).

    Identical gates to :func:`cleanup_attempt_worktree`, plus adoption: when
    the recorded path is already gone (removed by the interrupted run) and
    every remaining identity/delivery fact re-proves, the CLEANED record is
    written from disk truth (``disposition="adopted"``).  Path present but
    the first cleanup pass failed → same fail-closed refusal as cleanup.
    """
    return cleanup_attempt_worktree(conn, attempt_id, lease=lease,
                                   run_git=run_git, clock=clock,
                                   reconcile=True)


def _cleaned_dict(record: worktrees.WorktreeCleanedRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "seq": record.seq,
        "project_id": record.project_id,
        "task_id": record.task_id,
        "attempt_id": record.attempt_id,
        "allocation_id": record.allocation_id,
        "worktree_path": record.worktree_path,
        "branch": record.branch,
        "base_sha": record.base_sha,
        "commit_sha": record.commit_sha,
        "delivery_mode": record.delivery_mode,
        "occurred_at": record.occurred_at,
    }
