"""M4-01 Workspace paths and Diff Gate (M0 review §9.4 M4-01).

This module answers one bounded question with fail-closed semantics:

    "Given a verified repository root and a set of Ownership claims, what is
    the COMPLETE set of uncommitted paths (staged, unstaged, untracked,
    deleted, and both endpoints of every rename), and does every one of them
    fall inside the claimed territory? What is the base commit everything is
    measured against?"

Design decisions pinned here:

* **Path canonicalization.** Claims and actual paths are normalized to
  project-relative POSIX form against a verified repo root. Absolute paths,
  ``..`` traversal, backslashes, glob characters (only a trailing ``/**`` is
  allowed), control characters, empty/dot segments and ``.git`` paths are
  rejected before they can ever be compared. The claim grammar is exactly the
  legacy/scheduler grammar (``scheduler._normalize_claim``) so every existing
  legitimate Ownership claim keeps passing (M0 review §5 reuse table:
  "不改变现有 CLI 合法集合").
* **Symlink and case safety.** An actual path is resolved with ``realpath``
  through to its deepest existing component; any component that resolves
  outside the real repo root (symlink escape) is rejected. On case-insensitive
  volumes a name whose on-disk spelling differs byte-wise from the claim
  (a case alias) is rejected by default — the review lists "大小写变体" as a
  must-reject; callers may opt into case-folded matching explicitly.
* **Complete change collection.** ``git status --porcelain=v1 -z -uall
  --renames`` combined with ``git diff --cached --name-status -z -M``,
  ``git diff --name-status -z -M`` and ``git ls-files --others
  --exclude-standard -z``. All NUL-delimited plumbing: filenames containing
  quotes, spaces, newlines or non-UTF-8 bytes survive verbatim, and rename
  entries contribute BOTH endpoints. ``git diff --name-only`` alone would miss
  untracked files and rename sources, so it is deliberately not used. What
  collection keeps verbatim, presentation escapes: :func:`_display` renders
  control characters and raw non-UTF-8 bytes as single-line backslash spellings
  for violation tags and digests (FINDING-M401-02).
* **Fail-closed Git state.** Not a repository, a bare repository, a root that
  is not the toplevel (nested/sub-repo ambiguity), a repository without HEAD,
  unmerged conflict entries, a missing ``git`` binary, or a command timeout
  all raise :class:`GitStateError` instead of guessing.
* **Base-SHA evidence.** :func:`capture_base_evidence` binds the verified
  ``HEAD`` SHA to the correct Task/Attempt with the Task's version at capture
  time, and persists it as an append-only immutable ``events`` row
  (kind ``workspace_base_evidence``). Immutability is enforced where the
  database can: the v3 ``events`` no-update/no-delete triggers, plus the v16
  partial UNIQUE index (at most one base-evidence record per Attempt) and the
  v16 binding trigger (project/task/attempt columns and required payload
  fields must be present). Re-capturing identical evidence is an idempotent
  replay; a different base SHA for the same Attempt fails closed.

Scope ends at M4-01: no worktree lifecycle (M4-02), no verifier (M4-03), no
commit evidence (M4-04), and no GitHub integration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Sequence

from . import domain, events
from .clock import Clock
from .errors import ConflictError, NotFoundError, StorageError, ValidationError
from .ids import require_entity_id
from .storage import TeamCraftConnection, transaction

__all__ = [
    "BASE_EVIDENCE_KIND",
    "WORKSPACES_CONTRACT_VERSION",
    "ActualPath",
    "BaseEvidence",
    "BaseEvidenceCheck",
    "ChangeEntry",
    "Claim",
    "DiffGateResult",
    "GitStateError",
    "WorkspacePathError",
    "WorkspaceState",
    "capture_base_evidence",
    "claim_covers",
    "collect_workspace_state",
    "evaluate_ownership",
    "load_base_evidence",
    "normalize_claim",
    "normalize_claims",
    "require_base_evidence",
    "resolve_actual_path",
    "run_diff_gate",
    "verify_base_evidence",
    "verify_repo_root",
]

#: Bumped when the evidence payload/path-gate semantics change incompatibly.
WORKSPACES_CONTRACT_VERSION = 1

#: Event kind used for durable base-SHA evidence (schema v16 gates on it).
BASE_EVIDENCE_KIND = "workspace_base_evidence"

#: Git object digests accepted for a base SHA (SHA-1 and SHA-256 repositories).
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

#: Default wall-clock budget for one git plumbing invocation.
GIT_TIMEOUT_SECONDS = 30.0

GitRunner = Callable[[Sequence[str], str], bytes]


class WorkspacePathError(ValidationError):
    """A claim or actual path is unsafe and was refused fail-closed.

    ``kind`` is a stable machine tag: ``format``, ``traversal``,
    ``absolute``, ``symlink-escape``, ``case-alias``, ``gitdir`` or
    ``collision``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class GitStateError(StorageError):
    """The repository state is ambiguous or unreadable; the gate fails closed."""


# ---------------------------------------------------------------------------
# Ownership claim canonicalization (pure; mirrors scheduler semantics)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One validated, canonicalized Ownership claim."""

    raw: str
    path: str
    recursive: bool
    parts: tuple[str, ...]

    @property
    def canonical(self) -> str:
        return self.path + ("/**" if self.recursive else "")


def _reject(kind: str, claim: Any, why: str) -> WorkspacePathError:
    return WorkspacePathError(kind, f"invalid ownership claim {claim!r}: {why}")


def normalize_claim(claim: str) -> Claim:
    """Validate and canonicalize one project-relative Ownership claim.

    Same grammar as the dispatch-time ownership claims (``/**`` recursive
    suffix allowed; everything else must be a clean relative POSIX path),
    hardened with the M4-01 additions: ``.git`` paths are never ownable and
    control characters are refused.
    """

    if not isinstance(claim, str):
        raise _reject("format", claim, "claim must be a string")
    if any(unicodedata.category(char) == "Cc" for char in claim):
        raise _reject("format", claim, "control characters are forbidden")
    raw = claim.strip()
    recursive = raw.endswith("/**")
    value = raw[:-3] if recursive else raw
    if not value or value in (".", "./"):
        raise _reject("format", claim, "path must not be empty or dot")
    if value.startswith("/") or "\\" in value:
        raise _reject("absolute" if value.startswith("/") else "format", claim,
                     "use a project-relative POSIX path")
    if re.match(r"^[A-Za-z]:", value):
        raise _reject("absolute", claim, "drive-qualified paths are forbidden")
    if any(char in value for char in "*?[]{}"):
        raise _reject("format", claim, "only a trailing /** wildcard is supported")
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise _reject("traversal", claim, "empty, dot, and traversal segments are forbidden")
    if any(not part.strip() or any(unicodedata.category(char) == "Cc" for char in part)
           for part in raw_parts):
        raise _reject("format", claim, "blank or control path segments are forbidden")
    parts = PurePosixPath(value).parts
    if not parts:
        raise _reject("format", claim, "path has no segments")
    if any(part == ".git" for part in parts):
        raise _reject("gitdir", claim, "'.git' internals are never ownable")
    return Claim(raw=claim, path="/".join(parts), recursive=recursive,
                parts=tuple(parts))


def normalize_claims(claims: Iterable[str]) -> tuple[Claim, ...]:
    """Normalize a claim set; reject unsafe case aliases/collisions.

    Byte-identical duplicates are deduplicated (same owner, harmless). Two
    claims whose paths differ only by case are an *unsafe collision*: on
    case-insensitive volumes they name the same territory while looking
    distinct to reviewers, so the whole set is refused fail-closed.
    """

    result: list[Claim] = []
    seen: set[str] = set()
    folded: dict[str, str] = {}
    for raw in claims:
        claim = normalize_claim(raw)
        if claim.canonical in seen:
            continue
        seen.add(claim.canonical)
        key = claim.path.casefold()
        prior = folded.get(key)
        if prior is not None and prior != claim.path:
            raise WorkspacePathError(
                "collision",
                f"ownership claims collide by case: {prior!r} vs {claim.path!r}",
            )
        folded[key] = claim.path
        result.append(claim)
    return tuple(result)


def claim_covers(claim: Claim, path: str, *, case_insensitive: bool = False) -> bool:
    """Exact and trailing ``/**`` prefix ownership, at component boundaries.

    ``src/**`` covers ``src`` itself and ``src/a/b.py`` but never ``src2/x``.
    With ``case_insensitive`` the comparison folds case (explicit opt-in for
    case-insensitive volumes); the default is byte-exact.
    """

    left = claim.path.casefold() if case_insensitive else claim.path
    right = path.casefold() if case_insensitive else path
    if claim.recursive:
        return right == left or right.startswith(left + "/")
    return right == left


# ---------------------------------------------------------------------------
# Actual-path verification against the repo root (filesystem-backed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActualPath:
    """A repo-relative path verified against the real filesystem."""

    posix: str
    resolved: str
    exists: bool
    case_alias: bool


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def verify_repo_root(root: str | os.PathLike[str]) -> str:
    """Return the canonical ``realpath`` of a usable project root, or raise.

    The root must exist, be a directory, and not itself be a ``.git``
    directory. All downstream comparisons use the returned real path so a
    symlinked project directory cannot silently alias somewhere else.
    """

    if not isinstance(root, (str, os.PathLike)):
        raise WorkspacePathError("format", root and "repo root must be a path")
    candidate = os.fspath(root)
    if not candidate.strip():
        raise WorkspacePathError("format", "repo root must not be blank")
    if not os.path.isabs(candidate):
        raise WorkspacePathError("absolute", "repo root must be an absolute path")
    real = os.path.realpath(candidate)
    if not os.path.isdir(real):
        raise WorkspacePathError("format", f"repo root is not a directory: {candidate!r}")
    if os.path.basename(real) == ".git":
        raise WorkspacePathError("gitdir", "repo root must not be a '.git' directory")
    return real


def _disk_spelling(parent_real: str, name: str) -> str | None:
    """The directory entry matching ``name``, preferring a byte-exact hit.

    Returns ``None`` when no entry matches at all; returns the byte-differing
    spelling when only a case-folded match exists (case-insensitive volume).
    """

    try:
        entries = os.listdir(parent_real)
    except OSError:
        return None
    if name in entries:
        return name
    folded = name.casefold()
    for entry in entries:
        if entry.casefold() == folded:
            return entry
    return None


def resolve_actual_path(repo_root: str, relpath: str,
                       *, allow_case_insensitive: bool = False) -> ActualPath:
    """Resolve one project-relative path safely under ``repo_root`` (realpath).

    Rejects (as :class:`WorkspacePathError`) traversal-garbage segments,
    components that symlink outside the root, and byte-differing case aliases
    of existing on-disk names (unless ``allow_case_insensitive``). A missing
    leaf is allowed (deleted/untracked paths) as long as its deepest existing
    ancestor lies inside the root.
    """

    root = os.path.realpath(repo_root)
    parts = relpath.split("/")
    if (not relpath or relpath.startswith("/") or "\\" in relpath
            or any(part in ("", ".", "..") for part in parts)
            or any(part == ".git" for part in parts)):
        raise WorkspacePathError("format",
                                f"unsafe project-relative path {relpath!r}")
    resolved = root
    missing_tail: list[str] = []
    exists = True
    case_alias = False
    for index, segment in enumerate(parts):
        candidate = resolved + "/" + segment
        if os.path.islink(candidate) or os.path.exists(candidate):
            real = os.path.realpath(candidate)
            if not _is_within(real, root):
                raise WorkspacePathError(
                    "symlink-escape",
                    f"path {relpath!r} escapes the repo root via a symlink at "
                    f"{'/'.join(parts[: index + 1])!r}",
                )
            spelling = _disk_spelling(resolved, segment)
            if spelling is not None and spelling != segment:
                case_alias = True
                if not allow_case_insensitive:
                    raise WorkspacePathError(
                        "case-alias",
                        f"path {relpath!r} differs in case from its on-disk name "
                        f"{spelling!r} (unsafe case alias)",
                    )
            resolved = real
        else:
            exists = False
            missing_tail = parts[index:]
            break
    final = resolved + ("/" + "/".join(missing_tail) if missing_tail else "")
    if not _is_within(final, root):
        raise WorkspacePathError("absolute",
                                f"path {relpath!r} resolves outside the repo root")
    return ActualPath(posix="/".join(parts), resolved=final, exists=exists,
                      case_alias=case_alias)


# ---------------------------------------------------------------------------
# Complete change collection (NUL-safe Git plumbing/status combination)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeEntry:
    """One uncommitted path with every applicable change flag."""

    path: str
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False
    added: bool = False
    deleted: bool = False
    renamed: bool = False
    rename_source: str | None = None
    rename_target: str | None = None


@dataclass(frozen=True)
class WorkspaceState:
    """Verified repository identity, base commit and complete change set."""

    repo_root: str
    base_sha: str
    branch: str | None
    changes: tuple[ChangeEntry, ...]


def _default_run_git(args: Sequence[str], cwd: str,
                    timeout: float = GIT_TIMEOUT_SECONDS) -> bytes:
    command = ["git", "--no-pager", *args]
    try:
        proc = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise GitStateError("git executable is not available; failing closed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitStateError(
            f"git {' '.join(args[:2])} timed out after {timeout}s; failing closed"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:400]
        raise GitStateError(
            f"git {' '.join(args[:2])} failed (exit {proc.returncode}): {detail}"
        )
    return proc.stdout


def _text(raw: bytes) -> str:
    # surrogateescape keeps raw non-UTF-8 filename bytes recoverable verbatim.
    return os.fsdecode(raw)


def _nul_tokens(raw: bytes) -> list[str]:
    tokens = [_text(chunk) for chunk in raw.split(b"\0")]
    while tokens and tokens[-1] == "":
        tokens.pop()
    return tokens


def _safe_change_path(raw: str) -> str:
    """Validate one plumbing-reported path (already repo-root relative)."""

    path = raw.rstrip("/")
    if not path:
        raise GitStateError(f"git reported an empty path {raw!r}; failing closed")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise GitStateError(
            f"git reported a non-canonical path {raw!r}; refusing to guess")
    if any(part == ".git" for part in parts):
        raise GitStateError(f"git reported a '.git' internal path {raw!r}")
    return path


class _ChangeAccumulator:
    def __init__(self) -> None:
        self._flags: dict[str, dict[str, Any]] = {}

    def touch(self, path: str, **flags: Any) -> None:
        entry = self._flags.setdefault(path, {})
        for key, value in flags.items():
            if value is None:
                continue
            if isinstance(value, bool):
                entry[key] = bool(entry.get(key)) or value
            else:
                entry.setdefault(key, value)

    def entries(self) -> tuple[ChangeEntry, ...]:
        out: list[ChangeEntry] = []
        for path in sorted(self._flags):
            flags = self._flags[path]
            out.append(ChangeEntry(
                path=path,
                staged=bool(flags.get("staged")),
                unstaged=bool(flags.get("unstaged")),
                untracked=bool(flags.get("untracked")),
                added=bool(flags.get("added")),
                deleted=bool(flags.get("deleted")),
                renamed=bool(flags.get("renamed")),
                rename_source=flags.get("rename_source"),
                rename_target=flags.get("rename_target"),
            ))
        return tuple(out)


def _parse_status_z(raw: bytes, sink: _ChangeAccumulator) -> None:
    tokens = _nul_tokens(raw)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith("##"):  # --branch header, if ever enabled
            continue
        if len(token) < 3 or token[2] != " ":
            raise GitStateError(f"malformed porcelain status entry {token!r}")
        xy, path = token[:2], token[3:]
        if "U" in xy or xy in ("AA", "DD", "AU", "UA", "DU", "UD"):
            raise GitStateError(
                f"repository has unmerged paths ({path!r}: {xy!r}); "
                "refusing an ambiguous git state")
        staged = xy[0] not in " .?"
        unstaged = xy[1] not in " .?"
        untracked = xy == "??"
        path = _safe_change_path(path)
        if xy[0] in "RC" or xy[1] in "RC":
            if index >= len(tokens):
                raise GitStateError("porcelain rename entry is missing its source path")
            source = _safe_change_path(tokens[index])
            index += 1
            sink.touch(path, staged=staged, unstaged=unstaged, added=xy[0] == "A",
                      renamed=True, rename_source=source)
            sink.touch(source, deleted=True, renamed=True, rename_target=path)
            continue
        sink.touch(path, staged=staged, unstaged=unstaged, untracked=untracked,
                   added=xy[0] == "A" or xy[1] == "A",
                   deleted=xy[0] == "D" or xy[1] == "D")


def _parse_name_status_z(raw: bytes, sink: _ChangeAccumulator, *, staged: bool) -> None:
    tokens = _nul_tokens(raw)
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        letter = status[0]
        if letter in ("R", "C"):
            if index + 1 >= len(tokens):
                raise GitStateError("diff rename entry is missing its endpoints")
            old = _safe_change_path(tokens[index])
            new = _safe_change_path(tokens[index + 1])
            index += 2
            sink.touch(new, staged=staged, unstaged=not staged, added=True,
                      renamed=True, rename_source=old)
            sink.touch(old, staged=staged, unstaged=not staged, deleted=True,
                      renamed=True, rename_target=new)
            continue
        if index >= len(tokens):
            raise GitStateError("diff name-status entry is missing its path")
        path = _safe_change_path(tokens[index])
        index += 1
        sink.touch(path, staged=staged, unstaged=not staged,
                   added=letter == "A", deleted=letter == "D")


def collect_workspace_state(root: str | os.PathLike[str], *,
                           run_git: GitRunner | None = None) -> WorkspaceState:
    """Capture verified repo root, base SHA and the complete change set.

    Fails closed (:class:`GitStateError`) for non-repositories, bare
    repositories, roots that are not the toplevel, missing HEAD, unmerged
    entries, command failures or timeouts.
    """

    runner = run_git or _default_run_git
    root_real = verify_repo_root(root)

    def git(args: Sequence[str]) -> bytes:
        return runner(args, root_real)

    top = _text(git(["rev-parse", "--show-toplevel"])).strip()
    if not top:
        raise GitStateError("git rev-parse --show-toplevel returned nothing")
    top_real = os.path.realpath(top)
    if top_real != root_real:
        raise GitStateError(
            f"project root {root_real!r} is not the repository top level "
            f"({top_real!r}); refusing nested/sub-repository ambiguity")
    bare = _text(git(["rev-parse", "--is-bare-repository"])).strip()
    if bare == "true":
        raise GitStateError("bare repositories have no worktree Diff Gate state")
    base_sha = _text(git(["rev-parse", "--verify", "HEAD"])).strip()
    if not _SHA_RE.match(base_sha):
        raise GitStateError(f"HEAD is not a valid commit digest: {base_sha!r}")
    branch: str | None
    try:
        branch = _text(git(["symbolic-ref", "--short", "-q", "HEAD"])).strip() or None
    except GitStateError:
        branch = None  # detached HEAD is unambiguous about its commit

    sink = _ChangeAccumulator()
    _parse_status_z(
        git(["status", "--porcelain=v1", "-z", "--untracked-files=all",
             "--renames", "--ignore-submodules=none"]), sink)
    _parse_name_status_z(git(["diff", "--cached", "--name-status", "-z", "-M"]),
                         sink, staged=True)
    _parse_name_status_z(git(["diff", "--name-status", "-z", "-M"]), sink,
                         staged=False)
    for token in _nul_tokens(git(["ls-files", "--others", "--exclude-standard", "-z"])):
        sink.touch(_safe_change_path(token), untracked=True)
    return WorkspaceState(repo_root=root_real, base_sha=base_sha, branch=branch,
                         changes=sink.entries())


# ---------------------------------------------------------------------------
# Ownership comparison and the Diff Gate
# ---------------------------------------------------------------------------


#: Backslash spellings for the whitespace-ish characters that show up most
#: often in file names. Every other unsafe character gets a numeric spelling.
_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t",
                    "\u2028": "\\u2028", "\u2029": "\\u2029"}

#: Categories that must never appear literally in a rendered path: Cc controls
#: (U+0000-U+001F, U+007F-U+009F) plus the Zl/Zp line and paragraph
#: separators, which ``str.splitlines()`` and several log viewers treat as a
#: line break even though they are not Cc.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def _escape_char(char: str) -> str:
    named = _CONTROL_ESCAPES.get(char)
    if named is not None:
        return named
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def _display(path: str) -> str:
    """Render a possibly-hostile path safely for messages, tags and digests.

    FINDING-M401-02: the previous implementation only repaired undecodable
    surrogate bytes, so a path containing a literal newline (perfectly legal on
    POSIX, and preserved verbatim by the NUL-safe plumbing) split a violation
    tag — and the digest input built from it — across several log lines,
    letting one change masquerade as another.

    Guarantees for every returned string:

    * **Single line, log-safe.** Every ``Cc`` control character (including the
      C1 range, whose ``U+0085`` some viewers treat as a line break) and every
      ``Zl``/``Zp`` separator becomes a backslash escape: ``\\n``, ``\\r``,
      ``\\t``, otherwise ``\\xNN``/``\\uXXXX``. A literal backslash becomes
      ``\\\\`` so the escaping is injective and a crafted name can never forge
      the escaped spelling of a different path.
    * **Ordinary Unicode preserved.** Printable non-ASCII (``文档``, ``é``) and
      emoji pass through verbatim; no transliteration, no quoting.
    * **Raw bytes still visible.** A lone surrogate carried by
      ``os.fsdecode``/``surrogateescape`` renders as the ``\\xNN`` spelling of
      the raw filename byte it stands for, exactly as before.
    * **Always UTF-8 encodable**, so digests and JSON payloads cannot blow up.
    """

    rendered: list[str] = []
    for char in path:
        if unicodedata.category(char) in _UNSAFE_CATEGORIES:
            rendered.append(_escape_char(char))
            continue
        if char == "\\":
            rendered.append("\\\\")
            continue
        try:
            char.encode("utf-8")
        except UnicodeEncodeError:
            raw = char.encode("utf-8", "surrogateescape")
            rendered.append("".join(f"\\x{byte:02x}" for byte in raw))
        else:
            rendered.append(char)
    return "".join(rendered)


@dataclass(frozen=True)
class DiffGateResult:
    """Verdict of one Diff Gate evaluation; ``ok`` is fail-closed by default."""

    ok: bool
    base_sha: str
    branch: str | None
    changes: tuple[ChangeEntry, ...]
    violations: tuple[str, ...]
    claims: tuple[str, ...]


def evaluate_ownership(change_paths: Iterable[str], claims: Sequence[Claim], *,
                      allow_case_insensitive: bool = False) -> tuple[str, ...]:
    """Return sorted violation tags for paths no claim covers exactly/prefix.

    Tags: ``unowned:<path>`` (no coverage) and ``case-alias:<path>``
    (coverage would require case folding while strict matching is active).
    """

    violations: set[str] = set()
    for path in change_paths:
        if any(claim_covers(claim, path) for claim in claims):
            continue
        if any(claim_covers(claim, path, case_insensitive=True) for claim in claims):
            if not allow_case_insensitive:
                violations.add(f"case-alias:{_display(path)}")
            continue
        violations.add(f"unowned:{_display(path)}")
    return tuple(sorted(violations))


def run_diff_gate(root: str | os.PathLike[str], claims: Iterable[str], *,
                  run_git: GitRunner | None = None,
                  allow_case_insensitive: bool = False) -> DiffGateResult:
    """Full M4-01 Diff Gate: complete change set vs. Ownership, fail-closed.

    Invalid claims raise :class:`WorkspacePathError`. An empty claim set fails
    closed the same way (kind ``format``): with no claimed territory there is
    nothing that could own a change, so the gate never consults Git. Ambiguous
    Git state raises :class:`GitStateError`. Paths that escape the repository
    (symlinks) or alias by case are reported as violations, not silently
    absorbed; an empty change set (clean repository) passes trivially.
    """

    normalized = normalize_claims(claims)
    if not normalized:
        raise WorkspacePathError(
            "format",
            "at least one ownership claim is required; refusing a Diff Gate "
            f"with no claimed territory (got {claims!r})",
        )
    state = collect_workspace_state(root, run_git=run_git)
    violations: set[str] = set()
    paths: list[str] = []
    for entry in state.changes:
        paths.append(entry.path)
        try:
            actual = resolve_actual_path(state.repo_root, entry.path,
                                        allow_case_insensitive=allow_case_insensitive)
        except WorkspacePathError as exc:
            violations.add(f"{exc.kind}:{_display(entry.path)}")
            continue
        if actual.case_alias and not allow_case_insensitive:
            violations.add(f"case-alias:{_display(entry.path)}")
    violations |= set(evaluate_ownership(paths, normalized,
                                         allow_case_insensitive=allow_case_insensitive))
    return DiffGateResult(
        ok=not violations,
        base_sha=state.base_sha,
        branch=state.branch,
        changes=state.changes,
        violations=tuple(sorted(violations)),
        claims=tuple(claim.canonical for claim in normalized),
    )


# ---------------------------------------------------------------------------
# Durable, immutable base-SHA evidence (schema v16 + append-only events)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseEvidence:
    """One persisted base-SHA record bound to Task/Attempt/Task version."""

    seq: int
    event_id: str
    project_id: str
    task_id: str
    attempt_id: str
    task_version: int
    base_sha: str
    branch: str | None
    repo_root: str
    claims: tuple[str, ...]
    claims_digest: str
    initial_change_count: int
    initial_changes_digest: str
    captured_at: str
    evidence_digest: str


@dataclass(frozen=True)
class BaseEvidenceCheck:
    """Result of re-validating persisted evidence against live state."""

    ok: bool
    problems: tuple[str, ...]


_EVIDENCE_FIELDS = (
    "attempt_id", "base_sha", "branch", "claims", "claims_digest",
    "initial_change_count", "initial_changes_digest", "project_id", "repo_root",
    "schema_version", "task_id", "task_version",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _claims_payload(claims: Sequence[Claim]) -> tuple[list[str], str]:
    canonical = [claim.canonical for claim in claims]
    return canonical, _sha256_hex(_canonical_json(canonical))


def _changes_digest(changes: Sequence[ChangeEntry]) -> str:
    # Every path string that feeds the digest goes through _display, so the
    # digest input is single-line log-safe and UTF-8 encodable even for rename
    # endpoints holding control characters or raw non-UTF-8 bytes.
    rendered = [
        {"path": _display(entry.path), "staged": entry.staged,
         "unstaged": entry.unstaged, "untracked": entry.untracked,
         "added": entry.added, "deleted": entry.deleted,
         "renamed": entry.renamed,
         "rename_source": _display(entry.rename_source) if entry.rename_source else None,
         "rename_target": _display(entry.rename_target) if entry.rename_target else None}
        for entry in changes]
    # Order-independent canonical form: dicts are not orderable, so the records
    # are sorted by their own canonical JSON (sorting dicts directly raised
    # TypeError as soon as a workspace had two or more changes).
    return _sha256_hex(_canonical_json(sorted(rendered, key=_canonical_json)))


class _Joined:
    """Join an already-open transaction (mirrors the worker_reports pattern)."""

    def __init__(self, conn: TeamCraftConnection) -> None:
        self.conn = conn

    def __enter__(self) -> TeamCraftConnection:
        return self.conn

    def __exit__(self, *exc_info: Any) -> bool:
        return False


def _evidence_row(conn: TeamCraftConnection, attempt_id: str) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT seq, event_id, payload_json FROM events"
        " WHERE kind = ? AND attempt_id = ? ORDER BY seq ASC",
        (BASE_EVIDENCE_KIND, attempt_id),
    ).fetchall()
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


def _evidence_from_row(conn: TeamCraftConnection, seq: int, event_id: str,
                       payload_json: str) -> BaseEvidence:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise StorageError(f"base evidence payload is not valid JSON (seq={seq}): {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != set(_EVIDENCE_FIELDS):
        raise StorageError(f"base evidence payload has unexpected shape (seq={seq})")
    if payload.get("schema_version") != WORKSPACES_CONTRACT_VERSION:
        raise StorageError(
            f"unsupported base evidence schema_version: {payload.get('schema_version')!r}")
    base_sha = payload.get("base_sha")
    if not isinstance(base_sha, str) or not _SHA_RE.match(base_sha):
        raise StorageError(f"base evidence carries an invalid base_sha (seq={seq})")
    claims = payload.get("claims")
    if not isinstance(claims, list) or any(not isinstance(c, str) for c in claims):
        raise StorageError(f"base evidence claims are malformed (seq={seq})")
    try:
        normalize_claims(claims)
    except WorkspacePathError as exc:
        raise StorageError(f"base evidence claims are unsafe (seq={seq}): {exc}") from exc
    canonical_claims, claims_digest = _claims_payload(
        [normalize_claim(c) for c in claims])
    if canonical_claims != claims or payload.get("claims_digest") != claims_digest:
        raise StorageError(f"base evidence claims digest mismatch (seq={seq})")
    task_version = payload.get("task_version")
    if isinstance(task_version, bool) or not isinstance(task_version, int) or task_version < 0:
        raise StorageError(f"base evidence task_version is invalid (seq={seq})")
    count = payload.get("initial_change_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise StorageError(f"base evidence initial_change_count is invalid (seq={seq})")
    task_id = payload.get("task_id")
    project_id = payload.get("project_id")
    attempt_id = payload.get("attempt_id")
    repo_root = payload.get("repo_root")
    branch = payload.get("branch")
    if not all(isinstance(v, str) and v for v in (task_id, project_id, attempt_id, repo_root)):
        raise StorageError(f"base evidence binding fields are malformed (seq={seq})")
    if branch is not None and not isinstance(branch, str):
        raise StorageError(f"base evidence branch is malformed (seq={seq})")
    row = conn.execute(
        "SELECT occurred_at FROM events WHERE seq = ?", (seq,)).fetchone()
    captured_at = "" if row is None or row[0] is None else str(row[0])
    return BaseEvidence(
        seq=seq, event_id=event_id, project_id=project_id, task_id=task_id,
        attempt_id=attempt_id, task_version=task_version, base_sha=base_sha,
        branch=branch, repo_root=repo_root, claims=tuple(claims),
        claims_digest=claims_digest, initial_change_count=count,
        initial_changes_digest=str(payload.get("initial_changes_digest")),
        captured_at=captured_at, evidence_digest=_sha256_hex(payload_json),
    )


def capture_base_evidence(
    conn: TeamCraftConnection,
    *,
    task_id: str,
    attempt_id: str,
    repo_root: str | os.PathLike[str],
    claims: Iterable[str] | None = None,
    run_git: GitRunner | None = None,
    clock: Clock | None = None,
) -> tuple[BaseEvidence, bool]:
    """Persist the immutable base SHA for one Attempt; replay is idempotent.

    ``claims`` defaults to the Task's recorded Ownership. The Task version at
    capture time is bound into the payload. Returns ``(evidence, created)``:
    a re-capture of byte-identical evidence replays the original row
    (``created=False``); any different content for the same Attempt raises
    :class:`ConflictError` — the evidence is immutable by contract and is
    additionally pinned by the schema v16 unique index and binding trigger on
    top of the append-only ``events`` triggers.
    """

    require_entity_id(task_id)
    require_entity_id(attempt_id)
    claim_set = normalize_claims(claims) if claims is not None else None
    ctx = transaction(conn) if not conn.in_transaction else _Joined(conn)
    with ctx:
        attempt = domain.get_attempt(conn, attempt_id)
        if attempt.task_id != task_id:
            raise ValidationError(
                f"attempt {attempt_id} belongs to task {attempt.task_id!r},"
                f" not {task_id!r}")
        task = domain.get_task(conn, attempt.task_id)
        if claim_set is None:
            claim_set = normalize_claims(json.loads(task.ownership_json))
        if not claim_set:
            raise ValidationError(
                "cannot capture base evidence without ownership claims")
        state = collect_workspace_state(repo_root, run_git=run_git)
        payload_claims, claims_digest = _claims_payload(claim_set)
        payload = {
            "attempt_id": attempt.id,
            "base_sha": state.base_sha,
            "branch": state.branch,
            "claims": payload_claims,
            "claims_digest": claims_digest,
            "initial_change_count": len(state.changes),
            "initial_changes_digest": _changes_digest(state.changes),
            "project_id": task.project_id,
            "repo_root": state.repo_root,
            "schema_version": WORKSPACES_CONTRACT_VERSION,
            "task_id": task.id,
            "task_version": task.version,
        }
        canonical = _canonical_json(payload)
        existing = _evidence_row(conn, attempt.id)
        if len(existing) > 1:
            raise StorageError(
                f"{len(existing)} base evidence rows for attempt {attempt.id};"
                " storage invariant broken")
        if existing:
            seq, event_id, payload_json = existing[0]
            if payload_json == canonical:
                return _evidence_from_row(conn, seq, event_id, payload_json), False
            raise ConflictError(
                f"base evidence for attempt {attempt.id} is immutable:"
                " a different base SHA/claim set is already recorded")
        event_id = ("evt-wbe-" + _sha256_hex(
            f"{attempt.id}|{state.repo_root}|{state.base_sha}|{task.version}")[:32])
        try:
            event = events.append_event(
                conn, BASE_EVIDENCE_KIND, project_id=task.project_id,
                task_id=task.id, attempt_id=attempt.id, payload_json=canonical,
                event_id=event_id, clock=clock)
        except StorageError as exc:
            # UNIQUE(evt-wbe-…)|ux_workspace_base_evidence_attempt violated:
            # re-read and converge to replay-or-conflict, never silent overwrite.
            rows = _evidence_row(conn, attempt.id)
            if rows:
                seq, winner_id, payload_json = rows[0]
                if payload_json == canonical:
                    return _evidence_from_row(conn, seq, winner_id, payload_json), False
                raise ConflictError(
                    f"base evidence for attempt {attempt.id} is immutable:"
                    " a concurrent capture already recorded different content") from exc
            raise
        return _evidence_from_row(conn, event.seq, event.event_id, canonical), True


def load_base_evidence(conn: TeamCraftConnection, attempt_id: str) -> BaseEvidence | None:
    """Return the persisted base evidence for one Attempt, or ``None``.

    Malformed/tampered payloads fail closed (:class:`StorageError`); more than
    one record for the same Attempt is an invariant violation, never a pick.
    """

    require_entity_id(attempt_id)
    rows = _evidence_row(conn, attempt_id)
    if not rows:
        return None
    if len(rows) > 1:
        raise StorageError(
            f"{len(rows)} base evidence rows for attempt {attempt_id};"
            " storage invariant broken")
    seq, event_id, payload_json = rows[0]
    return _evidence_from_row(conn, seq, event_id, payload_json)


def require_base_evidence(conn: TeamCraftConnection, attempt_id: str) -> BaseEvidence:
    evidence = load_base_evidence(conn, attempt_id)
    if evidence is None:
        raise NotFoundError(f"no base evidence recorded for attempt {attempt_id}")
    return evidence


def verify_base_evidence(conn: TeamCraftConnection, evidence: BaseEvidence, *,
                        run_git: GitRunner | None = None) -> BaseEvidenceCheck:
    """Re-validate persisted evidence against live Task/Attempt/Git state.

    Reports (does not raise for) binding drift: attempt/task mismatch, task
    version drift, repo root disappearance, and base-commit movement. Later
    milestones (M4-02..M4-04) consume this verdict.
    """

    problems: list[str] = []
    try:
        attempt = domain.get_attempt(conn, evidence.attempt_id)
    except NotFoundError:
        attempt = None
    if attempt is None:
        problems.append("attempt-missing")
    elif attempt.task_id != evidence.task_id:
        problems.append("attempt-task-mismatch")
    try:
        task = domain.get_task(conn, evidence.task_id)
    except NotFoundError:
        task = None
    if task is None:
        problems.append("task-missing")
    elif task.version != evidence.task_version:
        problems.append("task-version-drift")
    if task is not None and task.project_id != evidence.project_id:
        problems.append("project-mismatch")
    try:
        root = verify_repo_root(evidence.repo_root)
    except WorkspacePathError:
        root = None
    if root is None:
        problems.append("repo-root-missing")
    else:
        try:
            state = collect_workspace_state(root, run_git=run_git)
        except GitStateError:
            state = None
        if state is None:
            problems.append("git-state-unreadable")
        elif state.base_sha != evidence.base_sha:
            problems.append("base-moved")
    return BaseEvidenceCheck(ok=not problems, problems=tuple(problems))
