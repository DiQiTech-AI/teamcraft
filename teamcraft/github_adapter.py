"""M4-05 GitHub PR adapter (M0 review §9.4 M4-05).

Bounded question this module answers, fail-closed:

    "For one TeamCraft delivery, can we tell — WITHOUT ever talking to the
    network in tests and WITHOUT a shell — whether ``gh`` is even usable, and,
    when it is, create/adopt exactly one pull request, read back its live
    head/base/check facts, and merge it into the INTENDED base — while making
    missing-tooling and missing-authentication a safe BLOCKED outcome rather
    than a crash, a partial write, or a false DONE?"

Design pinned by this module (mirrors :mod:`teamcraft.delivery`'s audited
Git-runner discipline):

* **Injectable runner.**  Every GitHub effect funnels through a
  ``GitHubRunner = (argv: Sequence[str], cwd: str) -> bytes`` seam.  The
  production default (:func:`default_run_gh`) execs the ``gh`` CLI with
  ``shell=False`` and a hard timeout; tests inject a deterministic fake so the
  suite NEVER opens a socket, spawns ``gh``, or needs credentials.
* **No shell, ever.**  ``default_run_gh`` builds an argv list and passes it
  straight to :func:`subprocess.run`; there is no string-interpolated command
  line and no ``shell=True`` anywhere.  Callers (``teamcraft.delivery``) are
  required to pass already-validated atoms (repo ``owner/name``, branch names,
  opaque PR numbers, digests) — this adapter is the last line that refuses a
  syntactically dangerous atom before exec.
* **Probe before effect.**  :func:`probe_gh` reports presence and
  authentication as a value (never an exception) so the caller can turn "no
  ``gh``" or "not authenticated" into a clean BLOCKED verdict with zero writes.
* **Idempotent by construction.**  :func:`find_open_pr` lets the caller ask
  "does a PR already exist for exactly this head→base?" before it creates one,
  which is what lets a lost-response retry ADOPT the same PR instead of
  filing a duplicate.
* **Checks are facts, not guesses.**  head SHA, base ref/SHA, mergeability and
  the status-check rollup are parsed into structured :class:`PullRequestFacts`;
  "ready" (open + mergeable + no failing/pending check) is a derived verdict,
  never a caller-supplied claim.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

__all__ = [
    "ALLOWED_GH_COMMANDS",
    "ALLOWED_PR_SUBCOMMANDS",
    "FORBIDDEN_PR_SUBCOMMANDS",
    "GH_BINARY",
    "GH_TIMEOUT_SECONDS",
    "GhProbe",
    "GitHubError",
    "GitHubInvocationError",
    "GitHubRunner",
    "MergeResult",
    "PullRequestFacts",
    "AuditedGhRunner",
    "create_pr",
    "default_run_gh",
    "find_open_pr",
    "merge_pr",
    "normalize_repo",
    "probe_gh",
    "read_pr",
]

#: ``runner(argv, cwd) -> stdout_bytes``.  argv[0] is a bare verb (e.g. "pr",
#: "auth"), NOT including the ``gh`` binary: :func:`default_run_gh` prepends
#: it.  This keeps the seam identical in shape to ``workspaces.GitRunner``.
GitHubRunner = Callable[[Sequence[str], str], bytes]

#: The GitHub CLI binary name resolved via ``PATH`` (never a fixed path).
GH_BINARY = "gh"
#: Hard upper bound on any single ``gh`` invocation (wall clock seconds).  A
#: hung command fails closed as :class:`GitHubError`; it is never retried here.
GH_TIMEOUT_SECONDS = 60

#: Top-level ``gh`` commands this adapter is permitted to issue.  Everything
#: else (release, gist, codespace, repo edit ...) is out of scope and refused
#: before exec — the adapter is a PR tool, not a general GitHub driver.
ALLOWED_GH_COMMANDS = frozenset({"auth", "pr"})
#: Complete allowlist for the bounded PR adapter.  ``gh`` adds subcommands over
#: time, so refusing everything outside this set keeps a newly installed CLI
#: from silently broadening TeamCraft's external-effect surface.
ALLOWED_PR_SUBCOMMANDS = frozenset({"list", "create", "view", "merge"})
#: ``gh pr`` subcommands the adapter must NEVER run: none of them belong to a
#: create/read/merge delivery slice, and ``checkout`` would mutate a local tree.
#: This named set documents known dangerous operations; the allowlist above is
#: authoritative and also refuses unknown or newly introduced operations.
FORBIDDEN_PR_SUBCOMMANDS = frozenset({
    "checkout", "close", "reopen", "review", "ready", "comment", "edit",
    "diff", "status",
})

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,250}$")
_PR_JSON_FIELDS = (
    "number", "url", "headRefName", "headRefOid", "baseRefName", "baseRefOid",
    "state", "mergeable", "statusCheckRollup", "mergedAt", "mergeCommit",
)

# Status-check rollup conclusion values that still let a PR be considered
# mergeable-ready.  Anything else (FAILURE, PENDING, IN_PROGRESS, QUEUED,
# CANCELLED, TIMED_OUT, ACTION_REQUIRED, STARTUP_*) is "not passing".
_PASSING_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


class GitHubError(Exception):
    """Adapter-level refusal: ``gh`` missing/unusable, malformed input, or a
    hard timeout.  Callers treat this as a fail-closed BLOCKED signal."""


class GitHubInvocationError(GitHubError):
    """``gh`` ran (or the fake ran) but reported a non-zero exit / bad output.

    ``stderr`` is bounded and only for the caller's diagnostics; it may be
    shown to an operator but must never be treated as an authoritative fact.
    """

    def __init__(self, message: str, *, exit_code: int | None = None,
                 stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr[:500]


@dataclass(frozen=True)
class GhProbe:
    """Result of a bounded presence + authentication probe (never raises)."""

    present: bool
    authenticated: bool
    account: str | None
    detail: str

    @property
    def blocked(self) -> bool:
        return not (self.present and self.authenticated)


@dataclass(frozen=True)
class PullRequestFacts:
    """Structured, verified facts about one pull request."""

    repo: str
    number: int
    url: str
    head_branch: str
    head_sha: str
    base_ref: str
    base_sha: str
    state: str
    mergeable: str
    checks: tuple[dict[str, Any], ...]
    merged: bool
    merged_at: str | None
    merge_commit_sha: str | None

    @property
    def checks_passing(self) -> bool:
        if not self.checks:
            return True
        return all(
            str(item.get("conclusion") or "").upper() in _PASSING_CONCLUSIONS
            for item in self.checks
        )

    @property
    def open(self) -> bool:
        return self.state == "OPEN"

    @property
    def merge_ready(self) -> bool:
        """Open + mergeable + every check passing: the READY_TO_MERGE facts."""
        return self.open and self.mergeable == "MERGEABLE" and self.checks_passing


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a merge attempt; authoritative confirmation comes from a
    follow-up :func:`read_pr` (``merged`` here is the CLI's own claim)."""

    merged: bool
    merge_commit_sha: str | None
    merged_at: str | None
    raw: str


# ---------------------------------------------------------------------------
# argv construction safety (last line before exec)
# ---------------------------------------------------------------------------


def normalize_repo(repo: str) -> str:
    """Validate a GitHub ``owner/name`` slug; refuse anything shell-ish."""

    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise GitHubError(
            "repo must be a GitHub 'owner/name' slug (letters, digits, '.', "
            "'_' and '-' around a single '/'); refusing unsafe repo value")
    # ".."/"." are shape-valid under the character class but are traversal
    # atoms, never account names: refuse them too (defense in depth).
    if any(segment in (".", "..") for segment in repo.split("/")):
        raise GitHubError(f"repo {repo!r} contains traversal segments")
    return repo


def _require_branch(branch: str, field: str) -> str:
    if (not isinstance(branch, str) or not branch
            or any(ch.isspace() or ch in "\x00'\"$`;|&<>()" for ch in branch)
            or not _BRANCH_RE.match(branch)):
        raise GitHubError(f"{field} is not a safe git ref: {branch!r}")
    if any(segment in (".", "..") for segment in branch.split("/")):
        raise GitHubError(f"{field} {branch!r} contains traversal segments")
    return branch


def _require_number(number: Any, field: str) -> int:
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise GitHubError(f"{field} must be a positive integer")
    return int(number)


def _guard(argv: list[str]) -> list[str]:
    """Enforce the allowed-command/subcommand policy on a built argv."""

    if not argv:
        raise GitHubError("empty gh argv")
    command = str(argv[0])
    if command not in ALLOWED_GH_COMMANDS:
        raise GitHubError(f"gh subcommand {command!r} is not permitted here")
    if command == "pr":
        verb = "" if len(argv) < 2 else str(argv[1])
        if verb not in ALLOWED_PR_SUBCOMMANDS:
            shown = verb or "<missing>"
            raise GitHubError(
                f"gh pr {shown} is not permitted in the delivery adapter "
                "(only list/create/view/merge are issued)")
    return argv


# ---------------------------------------------------------------------------
# the default runner (production path; tests never call it)
# ---------------------------------------------------------------------------


def default_run_gh(args: Sequence[str], cwd: str) -> bytes:
    """Exec ``gh`` for ``args`` under ``cwd`` with ``shell=False``.

    The binary is resolved through ``PATH``; a missing binary raises
    :class:`GitHubError` (presence is normally decided earlier by
    :func:`probe_gh`).  There is no shell interpolation, a hard timeout bounds
    every call, and stdin/stderr are captured (never inherited) so a hung or
    chatty ``gh`` cannot stall the caller or leak into the audit trail.
    """

    argv = _guard(list(args))
    binary = shutil.which(GH_BINARY)
    if binary is None:
        raise GitHubError(f"{GH_BINARY} is not installed or not on PATH")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed binary + argv, shell=False
            [binary, *argv],
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(
            f"gh invocation timed out after {GH_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise GitHubError(f"could not execute {GH_BINARY}: {exc}") from exc
    if proc.returncode != 0:
        raise GitHubInvocationError(
            f"gh {' '.join(argv)} exited {proc.returncode}",
            exit_code=int(proc.returncode),
            stderr=(proc.stderr or b"").decode("utf-8", "replace").strip(),
        )
    return proc.stdout or b""


class AuditedGhRunner:
    """Wraps a runner to enforce the argv policy centrally (parity with the
    delivery ``_AuditedRunner``)."""

    def __init__(self, runner: GitHubRunner) -> None:
        self._runner = runner

    def __call__(self, args: Sequence[str], cwd: str) -> bytes:
        return self._runner(_guard(list(args)), cwd)


def _run(runner: GitHubRunner, args: list[str], cwd: str) -> bytes:
    return AuditedGhRunner(runner)(args, cwd)


def _json_output(runner: GitHubRunner, args: list[str], cwd: str) -> Any:
    raw = _run(runner, args, cwd)
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise GitHubInvocationError(
            f"gh {' '.join(args)} returned non-JSON output") from exc


# ---------------------------------------------------------------------------
# facts parsing
# ---------------------------------------------------------------------------


def _normalize_checks(rollup: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(rollup, list):
        return ()
    checks: list[dict[str, Any]] = []
    for item in rollup:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("context") or "")
        status = str(item.get("status") or "")
        conclusion = item.get("conclusion")
        checks.append({
            "name": name,
            "status": status.upper(),
            "conclusion": (None if conclusion is None
                          else str(conclusion).upper()),
        })
    checks.sort(key=lambda c: (c["name"], c["status"]))
    return tuple(checks)


def _facts_from(obj: Any, repo: str) -> PullRequestFacts:
    if not isinstance(obj, dict):
        raise GitHubInvocationError("gh pr output is not a JSON object")
    merge_commit = obj.get("mergeCommit")
    merge_sha = None
    if isinstance(merge_commit, dict):
        merge_sha = merge_commit.get("oid")
    elif isinstance(merge_commit, str):
        merge_sha = merge_commit
    merged_at = obj.get("mergedAt")
    state = str(obj.get("state") or "")
    return PullRequestFacts(
        repo=repo,
        number=_require_number(obj.get("number"), "pr number"),
        url=str(obj.get("url") or ""),
        head_branch=str(obj.get("headRefName") or ""),
        head_sha=str(obj.get("headRefOid") or "").lower(),
        base_ref=str(obj.get("baseRefName") or ""),
        base_sha=str(obj.get("baseRefOid") or "").lower(),
        state=state,
        mergeable=str(obj.get("mergeable") or "UNKNOWN"),
        checks=_normalize_checks(obj.get("statusCheckRollup")),
        merged=state == "MERGED" or bool(merged_at),
        merged_at=(None if merged_at is None else str(merged_at)),
        merge_commit_sha=(None if merge_sha is None else str(merge_sha).lower()),
    )


def _repo_args(repo: str | None) -> list[str]:
    if repo is None:
        return []
    return ["--repo", normalize_repo(repo)]


# ---------------------------------------------------------------------------
# public adapter surface
# ---------------------------------------------------------------------------


def probe_gh(runner: GitHubRunner, cwd: str, *,
             resolve: Callable[[str], str | None] | None = None) -> GhProbe:
    """Bounded presence + auth probe.  NEVER raises for a missing/unauthed
    ``gh`` — it returns a :class:`GhProbe` the caller turns into BLOCKED.

    ``resolve`` overrides the ``PATH`` lookup (tests inject a fake resolver to
    simulate a missing binary deterministically); production leaves it ``None``
    and resolves through :func:`shutil.which`.
    """

    which = shutil.which if resolve is None else resolve
    try:
        binary = which(GH_BINARY)
    except Exception:  # pragma: no cover - defensive on exotic PATH
        binary = None
    if binary is None:
        return GhProbe(present=False, authenticated=False, account=None,
                      detail=f"{GH_BINARY} is not installed or not on PATH")
    try:
        raw = runner(["auth", "status"], cwd)
    except GitHubInvocationError as exc:
        return GhProbe(present=True, authenticated=False, account=None,
                      detail=f"gh is not authenticated: {exc.stderr or exc}")
    except GitHubError as exc:
        return GhProbe(present=True, authenticated=False, account=None,
                      detail=str(exc))
    text = (raw or b"").decode("utf-8", "replace")
    # A successful `gh auth status` is exit 0 (the fake returns text); detect
    # the logged-in account line when present, otherwise just authenticated.
    account: str | None = None
    for line in text.splitlines():
        lowered = line.lower()
        if "logged in" in lowered and "account" in lowered:
            account = line.strip()
            break
    return GhProbe(present=True, authenticated=True, account=account,
                   detail="gh authenticated")


def find_open_pr(runner: GitHubRunner, cwd: str, *, repo: str | None,
                head: str, base: str) -> PullRequestFacts | None:
    """Look for an OPEN PR from exactly ``head`` into ``base``.

    Returns the single matching PR, or ``None`` when there is none.  Raises
    :class:`GitHubInvocationError` if more than one matches (an ambiguous scene
    the caller must resolve rather than pick a winner from).
    """

    _require_branch(head, "head")
    _require_branch(base, "base")
    args = ["pr", "list", "--head", head, "--base", base, "--state", "open",
            "--json", ",".join(_PR_JSON_FIELDS), "--limit", "5", *_repo_args(repo)]
    data = _json_output(runner, args, cwd)
    if data is None:
        return None
    if not isinstance(data, list):
        raise GitHubInvocationError("gh pr list returned non-list output")
    matches = [item for item in data
               if isinstance(item, dict)
               and str(item.get("baseRefName") or "") == base
               and str(item.get("headRefName") or "") == head]
    if not matches:
        return None
    if len(matches) > 1:
        raise GitHubInvocationError(
            f"{len(matches)} open PRs match head={head!r} into base={base!r};"
            " refusing to pick one")
    return _facts_from(matches[0], repo or "")


def create_pr(runner: GitHubRunner, cwd: str, *, repo: str | None, head: str,
              base: str, title: str, body: str) -> PullRequestFacts:
    """Create a PR (head -> base).  The caller must have already called
    :func:`find_open_pr` to avoid duplicates; this function does not dedupe."""

    _require_branch(head, "head")
    _require_branch(base, "base")
    if not isinstance(title, str) or not title.strip():
        raise GitHubError("pr title must be a non-empty string")
    args = ["pr", "create", "--head", head, "--base", base,
            "--title", title.strip(), "--body", body or "",
            "--json", ",".join(_PR_JSON_FIELDS), *_repo_args(repo)]
    data = _json_output(runner, args, cwd)
    return _facts_from(data, repo or "")


def read_pr(runner: GitHubRunner, cwd: str, *, repo: str | None,
            number: int) -> PullRequestFacts:
    """Read back the authoritative current facts for PR ``number``."""

    number = _require_number(number, "pr number")
    args = ["pr", "view", str(number), "--json",
            ",".join(_PR_JSON_FIELDS), *_repo_args(repo)]
    return _facts_from(_json_output(runner, args, cwd), repo or "")


def merge_pr(runner: GitHubRunner, cwd: str, *, repo: str | None, number: int,
             head_sha: str, method: str = "merge") -> MergeResult:
    """Merge PR ``number`` with a head-commit guard.

    ``--match-head-commit`` pins the merge to the exact head SHA we approved:
    if GitHub's head moved, ``gh`` refuses and nothing merges.  The caller must
    STILL confirm the result with :func:`read_pr` before treating it as done —
    a CLI's own "merged" claim is not durable proof by itself.
    """

    number = _require_number(number, "pr number")
    if not re.match(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$", head_sha or ""):
        raise GitHubError("merge head_sha must be a lowercase commit digest")
    if method not in ("merge", "squash", "rebase"):
        raise GitHubError(f"unsupported merge method {method!r}")
    args = ["pr", "merge", str(number), f"--{method}",
            "--match-head-commit", head_sha, *_repo_args(repo)]
    try:
        raw = _run(runner, args, cwd)
    except GitHubInvocationError as exc:
        return MergeResult(merged=False, merge_commit_sha=None, merged_at=None,
                          raw=(exc.stderr or str(exc)))
    text = (raw or b"").decode("utf-8", "replace").strip()
    return MergeResult(merged=True, merge_commit_sha=None, merged_at=None,
                      raw=text)
