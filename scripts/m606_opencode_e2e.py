#!/usr/bin/env python3
"""M6-06 — reusable OpenCode LIVE end-to-end runner for a disposable host root.

WHAT THIS IS
    A repeatable runner for the *harness-host* scenario of
    ``doc/M6_06_LOCAL_E2E_PREFLIGHT.md`` §3 (S0…S6) with the honesty contract of
    ``doc/M6_06_HOST_GATE_RESULTS.md``.  It provisions an isolated Git + SQLite
    world, pins the CEO protocol and one local model, drives the OpenCode leg
    through Herdr, verifies and delivers locally, and captures **postcondition**
    evidence — every claim is re-read from an authoritative source (git, SQLite,
    the Herdr socket, file digests) instead of being asserted from intent.

WHAT IT CANNOT DO (enforced, not merely promised)
    * ``--plan`` (the DEFAULT) creates nothing, opens no DB, sends no input and
      issues no Herdr command at all — it only prints the plan and validates
      arguments and guards.
    * Topology is operator-owned.  The runner **never** creates, closes, focuses,
      renames, resizes or attaches panes/tabs/sessions/workspaces: those verbs
      are refused by :data:`FORBIDDEN_HERDR`.  It can only read identity and,
      with explicit dispatch consent, submit a prompt to an **existing** agent.
    * No cloud fallback, ever.  Only a literal-IP loopback oMLX URL is accepted,
      the pinned model must be present in that loopback catalog, and no credential
      is read, accepted or propagated.  An unhealthy local route aborts.
    * Delivery is reported as ``submitted`` (or ``unknown``) and
      ``human_accepted=false`` unless a *separate* acknowledgement is supplied.
      The delivery path contains no push at all; the only push in this file is
      the bootstrap seed → the run's own local bare origin, and ``audit_git``
      proves that target is local, unforced, and this run's own.
    * Cleanup removes ONLY what this invocation created, tracked in an on-disk
      journal, so a crashed run stays precise and pre-existing garbage from other
      processes is never swept.

USAGE
    .venv/bin/python scripts/m606_opencode_e2e.py --plan --root /tmp/m606-live-x
    .venv/bin/python scripts/m606_opencode_e2e.py --preflight --root /tmp/m606-live-x
    .venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root <root> \
        --stop-after provision
    # phase 1 — allocate and hand off (no prompt is submitted here):
    .venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root <root> \
        --stop-after allocate --keep-root
    # operator: cd an existing OpenCode pane into evidence/handoff.json's
    #           allocated_cwd, confirm it is idle, attest its model, note its
    #           native session id
    # phase 2 — re-prove everything, gate the pane, submit ONCE:
    .venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root <root> \
        --resume-handoff --consent-dispatch --herdr-pane w3:p2Q \
        --pane-model-evidence attestation.json --expect-session-id ses_xxxx
    # offline rehearsal of the whole pipeline, no pane and no model at all:
    .venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root <root> \
        --scripted-worker-change
    # the worker's final report arrived after dispatch was recorded: continue the
    # attempt to verification/delivery WITHOUT issuing any prompt.  This is the
    # only supported way past a recorded dispatch — never re-run --consent-dispatch
    # to "pick up" a dispatched attempt, that sends a second real prompt.
    .venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root <root> \
        --resume-handoff --post-dispatch --worker-report-file report.json \
        --stop-after deliver
    .venv/bin/python scripts/m606_opencode_e2e.py --cleanup-only --root <root>
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from teamcraft import (  # noqa: E402  (path bootstrap must precede imports)
    briefs,
    ceo_initialization,
    ceo_protocol,
    delivery,
    diagnostics,
    domain,
    leader,
    providers,
    runtime_adapter,
    storage,
    verification,
    wake,
    worker_reports,
    workspaces,
    worktrees,
)

# ---------------------------------------------------------------------------
# Pins and policy constants
# ---------------------------------------------------------------------------

#: Canonical CEO protocol (doc/M6_06_LOCAL_E2E_PREFLIGHT.md §1.2).
CANONICAL_PROTOCOL_VERSION = "1.0.0"
CANONICAL_PROTOCOL_SHA256 = (
    "c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742")

#: Loopback oMLX.  Only a literal IP is acceptable (never ``localhost``): the
#: trust decision must not depend on mutable hosts/DNS resolution.
LOOPBACK_OMLX_URL = "http://127.0.0.1:8000/v1"

#: Model pinned for the OpenCode leg; must appear in the loopback catalog.
DEFAULT_MODEL = "Qwen3.8-Flash-Next-oQ4e-mtp"

RUN_PREFIX = "m606-live-"
MARKER = "M606-LIVE"

#: M2-01 treats both ``idle`` (OpenCode) and ``done`` (Codex) as settled.  The
#: runner gates on quiescence, not on one harness's spelling of it.
SETTLED_STATUSES = frozenset({"idle", "done"})

#: CLI stop points: ``ceo`` is deliberately absent — the CEO leg is part of the
#: phase-2 dispatch phase and has no meaning as a stop point for phase 1.
STOP_POINTS = ("provision", "allocate", "dispatch", "deliver", "cleanup", "close")

#: Stage label for the post-dispatch continuation.  It is NOT in :data:`STAGES`:
#: the continuation re-enters the scenario *after* dispatch, so it must never be
#: counted as having executed dispatch itself, and an abort during its proof marks
#: no new stage as executed.
CONTINUATION_STAGE = "post-dispatch"

#: Why a continuation exists, stated once and reused in help, plan and manifest.
POST_DISPATCH_NOTE = (
    "a dispatched attempt is continued, never re-dispatched: the prompt already "
    "went out once, and resending it would be a second real action against an "
    "agent that may already be working")

#: Read-only Herdr invocations.  Two-token form for subcommand groups.
READ_ONLY_HERDR = frozenset({
    ("status", ""), ("session", "list"), ("tab", "list"), ("tab", "get"),
    ("pane", "list"), ("pane", "get"), ("pane", "read"), ("pane", "current"),
    ("pane", "layout"), ("pane", "process-info"),
    ("agent", "list"), ("agent", "get"), ("agent", "read"), ("agent", "wait"),
    ("api", "snapshot"), ("api", "schema"),
})

#: Consent-gated: submits input into an EXISTING agent.  Never with a new pane.
DISPATCH_HERDR = frozenset({("agent", "prompt"), ("agent", "send-keys")})

#: Structurally unavailable: topology mutation, lifecycle and self updates.
FORBIDDEN_HERDR = frozenset({
    "create", "close", "focus", "rename", "resize", "zoom", "split", "kill",
    "attach", "detach", "stop", "start", "delete", "add", "remove", "set",
    "new", "update", "reload-config", "reset-keys", "send-text",
})

#: Git verbs that must never appear in a local-only delivery.
FORBIDDEN_GIT = frozenset({
    "push", "pull", "fetch", "clone", "merge", "rebase", "reset", "clean",
    "checkout", "switch", "restore", "prune", "gc", "filter-branch", "tag",
    "notes", "mv",
})

#: Leg names whose disposition is "submitted" unless separately acknowledged.
DELIVERY_SUBMITTED = "submitted"
DELIVERY_ACCEPTED = "accepted"
DELIVERY_UNKNOWN = "unknown"

#: Conservative model-id shape, used only to report what a terminal screen shows.
MODEL_ID_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._-]*-[A-Za-z0-9._-]+\b")

STAGES = ("provision", "allocate", "ceo", "dispatch", "deliver", "cleanup",
          "close")

#: PG-7 (CEO bootstrap) placement, stated once.
#:
#: The canonical-CEO challenge cannot be pre-staged: ``discover_and_issue`` hands
#: out the nonce plaintext exactly once (only its hash is stored, and it expires),
#: the fence must be applied immediately before a real prompt, and acceptance pins
#: the *post-submit* native session id.  So the CEO leg lives in phase 2, after
#: the operator has designated a live pane — never in phase 1, and never replayed.
CEO_STAGE_NOTE = ("PG-7 CEO bootstrap is phase-2 only: the one-shot nonce, the "
                 "pre-prompt fence, and the post-submit session pin all require a "
                 "live designated pane")

#: Why live dispatch is a TWO-PHASE operation, stated once and enforced.
#:
#: An attempt worktree does not exist until ``allocate`` creates it, and an
#: operator cannot cd a pane into a directory that does not exist yet.  So the
#: gate "the pane's cwd is exactly the allocated worktree" can never be
#: satisfied inside the invocation that allocates it.  Rather than relax the gate
#: (which would let a prompt land in the wrong tree) or silently pretend it
#: passed, live dispatch is refused in a single invocation and the run hands off:
#:
#:   phase 1  --confirm-live-run --stop-after allocate …
#:            → creates the worktree, writes evidence/handoff.json naming the
#:              exact cwd, branch, base sha and what to attest
#:   (operator)  cd the existing OpenCode pane into that cwd, note its model
#:   phase 2  --confirm-live-run --resume-handoff --consent-dispatch
#:            --herdr-pane <pane> --pane-model-evidence <attestation.json>
#:            --expect-session-id <session>
#:            → re-validates everything from durable state, then submits ONCE.
HANDOFF_REQUIRED = (
    "live dispatch cannot run in the same invocation that allocates the "
    "worktree: the operator has no window to place the pane in the allocated "
    "cwd, so the cwd gate would be unverifiable. Run phase 1 "
    "(--stop-after allocate), place the existing pane in the allocated cwd from "
    "evidence/handoff.json, then phase 2 (--resume-handoff --consent-dispatch "
    "--herdr-pane <pane> --pane-model-evidence <file> --expect-session-id <id>)")


class GuardError(RuntimeError):
    """A safety guard refused the request; no mutation had happened."""


class PostconditionError(RuntimeError):
    """A re-read of authoritative state did not match the claimed outcome."""


class CleanupError(RuntimeError):
    """A cleanup target failed validation, or a resource survived cleanup."""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)


def jsonable(obj: Any) -> Any:
    """Normalize values so a postcondition can always be written as JSON.

    Sets and Paths appear naturally in git observations; silently dropping them
    would weaken the record, so they are converted deterministically instead.
    """
    if isinstance(obj, (set, frozenset)):
        return sorted(jsonable(v) for v in obj)
    if isinstance(obj, os.PathLike):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def plain(obj: Any) -> Any:
    """Best-effort JSON-safe view of a dataclass/namespace for evidence."""
    import dataclasses
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return {k: v for k, v in vars(obj).items()
            if isinstance(v, (str, int, float, bool, list, dict, type(None)))}


def field(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute among ``names`` (schema tolerance)."""
    for name in names:
        if getattr(obj, name, None) is not None:
            return getattr(obj, name)
    return default


def real(path: Path | str) -> str:
    return os.path.realpath(str(path))


#: The placeholder :func:`sanitize` substitutes for the run root in written
#: artifacts.  An absolute path that left the runner is stored redacted, so a
#: reader comparing a recorded path to a live one must expand this back first.
ROOT_PLACEHOLDER = "<ROOT>"


def unredact_root(value: str, root: Path | str) -> str:
    """Expand a stored ``<ROOT>/…`` path back against ``root``.

    Sanitization is lossless for the root prefix, so this recovers the exact
    path the runner recorded; a value that was never redacted passes through
    unchanged.  Resolution is purely textual — it touches no filesystem.
    """
    text = str(value)
    if text == ROOT_PLACEHOLDER:
        return str(root)
    if text.startswith(ROOT_PLACEHOLDER + "/"):
        return str(root) + text[len(ROOT_PLACEHOLDER):]
    return text


def git(repo: Path, *args: str, check: bool = True) -> tuple[int, str]:
    argv = ["git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
            "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
            *args]
    proc = subprocess.run(argv, cwd=str(repo), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)
    out = proc.stdout.decode("utf-8", "surrogateescape").strip()
    if check and proc.returncode != 0:
        raise PostconditionError(
            f"git {' '.join(args)} failed ({proc.returncode}) in {repo}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return proc.returncode, out


def leak_shapes(text: str) -> list[str]:
    """Secret/path shapes via the M6-03 scanner patterns (scan, never scrub)."""
    hits: list[str] = []
    if diagnostics._POSIX_PATH_RE.search(text):
        hits.append("path")
    if diagnostics._WINDOWS_PATH_RE.search(text):
        hits.append("path")
    for pattern in diagnostics._SECRET_SHAPE_PATTERNS:
        if pattern.search(text):
            hits.append("secret")
            break
    if diagnostics._KV_SECRET_RE.search(text):
        hits.append("secret")
    return sorted(set(hits))


# ---------------------------------------------------------------------------
# Isolation guards
# ---------------------------------------------------------------------------

def guard_outside_repo(path: Path | str, what: str) -> str:
    """Resolve ``path`` and refuse anything at/inside the project repository."""
    resolved = real(path)
    repo = real(REPO_ROOT)
    if resolved == repo or resolved.startswith(repo + os.sep):
        raise GuardError(
            f"{what} targets {resolved}, which is the live project repository "
            f"({repo}) or inside it; the disposable scenario must never touch "
            f"the project tree, its worktrees or its runtime DB")
    return resolved


def runtime_db_targets(root: Path | str) -> list[str]:
    """Any argument that resolves to a live ``.teamcraft`` runtime location."""
    resolved = real(root)
    repo = real(REPO_ROOT)
    hits: list[str] = []
    if resolved == repo + os.sep + ".teamcraft" or resolved.startswith(
            repo + os.sep + ".teamcraft" + os.sep):
        hits.append(resolved)
    live_db = Path(repo) / ".teamcraft" / "teamcraft.db"
    if live_db.exists() and resolved == real(live_db):
        hits.append(resolved)
    return hits


def validate_root(raw_root: str, *, allow_non_temp: bool,
                  for_cleanup: bool = False,
                  for_resume: bool = False) -> Path:
    """Validate the explicit isolated root.  Every refusal is pre-I/O.

    The root must be explicitly supplied, absolute, outside this repository,
    outside any live ``.teamcraft`` runtime, outside the repo's registered
    worktrees, not a filesystem corner (``/``, ``$HOME``, temp base, ``/tmp``),
    and either absent or an empty directory.  ``for_cleanup`` relaxes ONLY the
    emptiness rule, because reclaiming a populated root is the whole point of
    ``--cleanup-only`` — and even then nothing is deleted without the journal.
    """
    if not raw_root or not raw_root.strip():
        raise GuardError("--root is required and must be an explicit non-empty "
                        "path; there is no default disposable location")
    candidate = Path(raw_root).expanduser()
    if not candidate.is_absolute():
        raise GuardError(f"--root must be an absolute path, got {raw_root!r}")
    resolved = guard_outside_repo(candidate, "--root")
    if runtime_db_targets(resolved):
        raise GuardError(f"--root resolves onto a live runtime location: {resolved}")

    base = real(tempfile.gettempdir())
    protected_named = {base, real(Path.home()), os.sep, real("/tmp"), "/private"}
    if resolved in protected_named:
        raise GuardError(f"--root {resolved} is a shared/system location; "
                        "refusing to treat it as disposable")
    if len(resolved.rstrip(os.sep).split(os.sep)) < 3:
        raise GuardError(f"--root {resolved} is too shallow to be disposable")
    if not resolved.startswith(base + os.sep) and not allow_non_temp:
        raise GuardError(
            f"--root {resolved} is outside the system temp base {base}; pass "
            f"--allow-non-temp-root only when you deliberately target another "
            f"disposable volume")

    repo_worktrees = {real(p) for p in repo_worktree_paths()}
    if resolved in repo_worktrees or any(
            r.startswith(resolved + os.sep) for r in repo_worktrees):
        raise GuardError(f"--root {resolved} overlaps a registered worktree of "
                        "this repository")

    if Path(resolved).exists():
        if not Path(resolved).is_dir():
            raise GuardError(f"--root {resolved} exists and is not a directory")
        leftovers = sorted(p.name for p in Path(resolved).iterdir())
        if leftovers and not (for_cleanup or for_resume):
            # An allocated phase-1 root is a live handoff, not debris: telling the
            # operator to reclaim it would invite deleting the very scenario they
            # are about to continue.  Point at --resume-handoff when we can prove
            # the root is ours and awaiting phase 2.
            if (Path(resolved) / "evidence" / "handoff.json").is_file():
                raise GuardError(
                    f"--root {resolved} is an allocated phase-1 handoff "
                    f"({leftovers[:4]}…), not a fresh root: continue it with "
                    f"--resume-handoff (see its evidence/handoff.json for the "
                    f"pane, cwd and attestation it is waiting on). Use "
                    f"--cleanup-only only if you intend to discard that handoff")
            raise GuardError(
                f"--root {resolved} is not empty ({leftovers[:6]}…); pass a "
                f"fresh directory, or use --cleanup-only to reclaim a journalled "
                f"earlier run")
        if leftovers and (for_cleanup or for_resume):
            # Reclaim/resume mode still refuses to touch a root we cannot
            # account for.
            if not (Path(resolved) / "evidence" / "journal.jsonl").is_file():
                raise GuardError(
                    f"--root {resolved} is populated ({leftovers[:6]}…) but has "
                    f"no journal; without proof of what was created, nothing is "
                    f"removed and nothing is resumed")
    return Path(resolved)


def repo_worktree_paths() -> list[Path]:
    proc = subprocess.run(["git", "worktree", "list", "--porcelain"],
                         cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False)
    paths: list[Path] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree "):]))
    return paths


def validate_loopback(url: str) -> str:
    """Literal-IP loopback http only, no userinfo/query/fragment, no https."""
    try:
        endpoint = urllib.parse.urlsplit(url)
        port = endpoint.port
        ip = ipaddress.ip_address(endpoint.hostname or "")
    except ValueError as exc:
        raise GuardError("malformed oMLX endpoint or nonliteral host") from exc
    if (endpoint.scheme != "http" or not ip.is_loopback or not port
            or endpoint.username is not None or endpoint.password is not None
            or "?" in url or "#" in url
            or endpoint.path.rstrip("/") != "/v1"
            or any(character.isspace() for character in url)):
        raise GuardError("oMLX endpoint must be literal loopback HTTP with an "
                         "explicit port and /v1, without credentials or query")
    return url


# ---------------------------------------------------------------------------
# Evidence (postconditions, digests, honest non-claims)
# ---------------------------------------------------------------------------

def secret_shapes(text: str) -> list[str]:
    """Credential shapes only — applied to every artifact we persist."""
    hits: list[str] = []
    for pattern in diagnostics._SECRET_SHAPE_PATTERNS:
        if pattern.search(text):
            hits.append("secret-shape")
            break
    if diagnostics._KV_SECRET_RE.search(text):
        hits.append("kv-secret")
    return hits


def sanitize(text: str, root: str) -> tuple[str, dict[str, int]]:
    """Rewrite run-root-absolute paths as ``<ROOT>/…`` and mask secrets.

    Redaction is counted and reported in the evidence index: nothing is
    silently rewritten.
    """
    counts = {"path_redactions": 0, "secret_redactions": 0}

    def path_sub(match: re.Match[str]) -> str:
        found = match.group(0)
        if found.startswith(root):
            counts["path_redactions"] += 1
            return "<ROOT>" + found[len(root):]
        return found

    out = diagnostics._POSIX_PATH_RE.sub(path_sub, text)
    for pattern in diagnostics._SECRET_SHAPE_PATTERNS:
        out = pattern.sub(lambda m: (counts.__setitem__(
            "secret_redactions", counts["secret_redactions"] + 1),
            "***REDACTED***")[1], out)
    out = diagnostics._KV_SECRET_RE.sub(
        lambda m: (counts.__setitem__("secret_redactions",
                                     counts["secret_redactions"] + 1),
                   f"{m.group(1)}: ***REDACTED***")[1], out)
    return out, counts


class Evidence:
    """Writes ``evidence/`` artifacts and records digest-bound postconditions.

    Two-tier hygiene: structural artifacts (paths the runner itself chose) are
    scanned for credential shapes only; artifacts carrying text from outside the
    runner — harness output, operator acknowledgement, verifier output — are
    sanitized first, and any surviving secret shape withholds the artifact.
    """

    #: Content that originated outside this runner.
    UNTRUSTED = frozenset({"dispatch.json", "prompt-ack.json",
                           "verification.json", "wake.json"})

    def __init__(self, root: Path, run_id: str, *, resume: bool = False) -> None:
        self.root = root
        self.dir = root / "evidence"
        self.run_id = run_id
        self.postconditions: list[dict[str, Any]] = []
        self.files: dict[str, str] = {}
        self.redactions: dict[str, dict[str, int]] = {}
        if resume:
            # Phase 2 of a handoff: the directory and its earlier artifacts are
            # ours, created by phase 1 of this same scenario; re-attach and keep
            # appending.  Absent journal ⇒ not ours, so refuse.
            if not (self.dir / "journal.jsonl").is_file():
                raise GuardError(
                    f"cannot resume: no journal in {self.dir}; a run may only "
                    f"continue evidence it can prove it started")
            prior = self.dir / "postconditions.json"
            if prior.is_file():
                self.postconditions = json.loads(prior.read_text("utf-8"))
            for path in sorted(self.dir.glob("*.json")):
                self.files[path.name] = sha256_file(path)
        elif self.dir.exists():
            # The ledger legitimately creates this directory first to hold its
            # journal. Anything else means we are not the owner: refuse.
            strays = sorted(p.name for p in self.dir.iterdir()
                           if p.name != "journal.jsonl")
            if strays:
                raise GuardError(
                    f"evidence dir {self.dir} already holds foreign content "
                    f"{strays}; refusing to write into a run we did not create")
        else:
            self.dir.mkdir(parents=True)

    def save_postconditions(self) -> None:
        """Persist postconditions so a later phase can extend, not replace, them."""
        self.write("postconditions.json", self.postconditions)

    def write(self, name: str, payload: Any, *,
              untrusted: bool = False) -> Path:
        path = self.dir / name
        try:
            text = (payload if isinstance(payload, str)
                    else json.dumps(payload, sort_keys=True, indent=2) + "\n")
        except TypeError as exc:
            # An artifact we cannot serialize is a defect in this runner, not a
            # reason to lose the whole run's report: fail with a clear message.
            raise PostconditionError(
                f"evidence {name} is not JSON-serializable ({exc}); the run's "
                f"report would be incomplete, so it is not claimed") from exc
        if untrusted or name in self.UNTRUSTED:
            text, counts = sanitize(text, str(self.root))
            if any(counts.values()):
                self.redactions[name] = counts
        secrets = secret_shapes(text)
        if secrets:
            # Refuse to persist credential-shaped content at all.
            raise PostconditionError(
                f"evidence {name} carries {secrets}; artifact withheld — the run "
                f"is inconclusive until the source is clean")
        path.write_text(text, encoding="utf-8")
        self.files[name] = sha256_file(path)
        return path

    def post(self, step: str, claim: str, *, expected: Any, observed: Any,
             detail: str = "") -> bool:
        """Record a re-read observation; raise when it contradicts the claim."""
        # A bool expectation is an assertion about truthiness (expected=False is
        # a real assertion, not "ignored"); anything else compares by equality.
        ok = (bool(observed) == expected) if isinstance(expected, bool) \
            else (expected == observed)
        entry = {"step": step, "claim": claim, "expected": jsonable(expected),
                 "observed": jsonable(observed), "ok": ok, "at": now_iso()}
        if detail:
            entry["detail"] = detail
        self.postconditions.append(entry)
        # Persist on every assertion: a phase that dies mid-way still leaves the
        # evidence of what it had already proven.
        self.save_postconditions()
        if not ok:
            self.write("postconditions.json", self.postconditions)
            raise PostconditionError(
                f"[{step}] {claim}: expected {expected!r}, observed {observed!r}"
                + (f" ({detail})" if detail else ""))
        return True

    def index(self) -> Path:
        return self.write("evidence-index.json", {
            "run_id": self.run_id,
            "generated_at": now_iso(),
            "files": self.files,
            "redactions": self.redactions,
            "postconditions_total": len(self.postconditions),
            "postconditions_passed": sum(1 for p in self.postconditions
                                        if p["ok"]),
        })


# ---------------------------------------------------------------------------
# Journal-backed resource ledger: clean exactly what THIS invocation created
# ---------------------------------------------------------------------------

class Ledger:
    """Provenance-bound cleanup with an on-disk journal.

    Each created resource is appended to ``journal.jsonl`` *after* it exists, so
    a crash mid-creation still leaves a precise record.  Removal validates every
    target against the journal (plus realpath identity and the isolation rules)
    and refuses anything else, so roots left by OTHER processes are structurally
    untouchable.  Already-absent resources count as removed, so cleanup is
    idempotent and safely resumable via ``--cleanup-only``.
    """

    def __init__(self, root: Path, run_id: str, journal: Path | None = None,
                 *, owns_root: bool = False) -> None:
        self.root = root
        self.run_id = run_id
        self.journal = journal or (root / "evidence" / "journal.jsonl")
        self.entries: list[dict[str, Any]] = []
        self.done: set[str] = set()
        self.owns_root = owns_root
        self.git_calls: list[list[str]] = []

    # -- journal ------------------------------------------------------------

    def load_journal(self) -> list[dict[str, Any]]:
        """Replay a previous run's journal for --cleanup-only."""
        entries: list[dict[str, Any]] = []
        if not self.journal.is_file():
            raise CleanupError(
                f"no journal at {self.journal}; refusing to guess what to remove "
                f"— a run that cannot prove what it created must delete nothing")
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        self.entries = entries
        # The journal is the proof: if it recorded a root it created, reclaim it.
        self.owns_root = any(e.get("kind") == "root" for e in entries)
        return entries

    def _append(self, record: dict[str, Any]) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(canonical(record) + "\n")

    # -- creation -----------------------------------------------------------

    def adopt_root(self, path: Path) -> Path:
        """Claim a root this invocation created (it was validated empty)."""
        self.owns_root = True
        self._record("root", path)
        return path

    def track(self, path: Path, kind: str = "dir") -> Path:
        self._record(kind, path)
        return path

    def _record(self, kind: str, path: Path) -> None:
        r = real(path)
        guard_outside_repo(r, f"tracked {kind}")
        entry = {"kind": kind, "path": r, "run_id": self.run_id, "at": now_iso()}
        self.entries.append(entry)
        self._append(entry)

    def track_git_worktree_add(self, worktree_path: str, branch: str,
                              base_sha: str) -> None:
        entry = {"kind": "worktree", "path": real(worktree_path),
                 "branch": branch, "base_sha": base_sha,
                 "run_id": self.run_id, "at": now_iso()}
        self.entries.append(entry)
        self._append(entry)

    def untrack_path(self, path: Path) -> None:
        self.done.add(real(path))

    # -- validation ---------------------------------------------------------

    def _validate(self, target: str) -> None:
        recorded = {real(e["path"]) for e in self.entries}
        if target not in recorded:
            raise CleanupError(
                f"refusing to remove {target}: not created by this run "
                f"(journal covers {sorted(recorded)}); resources from other "
                f"processes are never swept")
        if os.path.islink(target):
            raise CleanupError(f"refusing to remove {target}: it is a symlink")
        guard_outside_repo(target, f"cleanup target {target}")
        base = real(tempfile.gettempdir())
        home = real(Path.home())
        if target in {base, home, os.sep, real("/tmp")}:
            raise CleanupError(f"refusing to remove shared location {target}")
        if not target.startswith(self.root_real + os.sep) and target != self.root_real:
            raise CleanupError(
                f"refusing to remove {target}: outside this run root "
                f"{self.root_real}")
        if len(target.rstrip(os.sep).split(os.sep)) < 3:
            raise CleanupError(f"refusing implausibly shallow target {target}")

    @property
    def root_real(self) -> str:
        return real(self.root)

    # -- removal ------------------------------------------------------------

    def remove_worktrees(self) -> list[str]:
        """Remove git worktrees this run added (never --force), then branches."""
        removed: list[str] = []
        for entry in reversed(self.entries):
            if entry["kind"] != "worktree":
                continue
            wt = real(entry["path"])
            if wt in self.done:
                continue
            self._validate(wt)
            if not Path(wt).exists():
                self.done.add(wt)
                removed.append(wt)
                continue
            argv = ["worktree", "remove", wt]
            self.git_calls.append(argv)
            code, err = self._git(self.root, *argv, check=False)
            if code != 0:
                # Unmerged/uncommitted content is a legitimate refusal: keep it.
                self.done.add(wt)
                removed.append(f"{wt} (retained: {err[:120]})")
                continue
            self.done.add(wt)
            removed.append(wt)
            if entry.get("branch"):
                self.git_calls.append(["branch", "-D", entry["branch"]])
                self._git(self.root, "branch", "-D", entry["branch"],
                         check=False)
        return removed

    def _git(self, repo: Path, *args: str, check: bool = True) -> tuple[int, str]:
        return git(repo, *args, check=check)

    @staticmethod
    def _remove_any(target: str) -> None:
        """Remove a directory OR a file (the journal tracks both)."""
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        elif os.path.exists(target) or os.path.islink(target):
            os.remove(target)

    def dispose(self) -> dict[str, Any]:
        """Remove journalled resources in reverse order.  Idempotent.

        Individual failures are collected, not short-circuited: one stuck path
        must not strand everything after it.
        """
        problems: list[str] = []
        worktrees_removed = self.remove_worktrees()
        for entry in reversed(self.entries):
            kind, target = entry["kind"], real(entry["path"])
            if kind == "worktree" or target in self.done:
                continue
            try:
                self._validate(target)
            except CleanupError as exc:
                problems.append(str(exc))
                continue
            if not Path(target).exists():
                self.done.add(target)
                continue
            if kind == "root" and not self.owns_root:
                continue
            try:
                self._remove_any(target)
            except OSError as exc:
                problems.append(f"{target}: {exc}")
                continue
            self.done.add(target)
        if self.owns_root and self.root_real not in self.done:
            try:
                self._validate(self.root_real)
                self._remove_any(self.root_real)
                self.done.add(self.root_real)
            except (CleanupError, OSError) as exc:
                problems.append(str(exc))
        still = [e["path"] for e in self.entries
                 if e["kind"] != "worktree" and real(e["path"]) not in self.done
                 and Path(real(e["path"])).exists()]
        result = {"run_id": self.run_id, "removed": sorted(self.done),
                  "worktrees": worktrees_removed, "retained": still,
                  "journal": str(self.journal)}
        if still or problems:
            raise CleanupError(
                "cleanup incomplete; retained for inspection: "
                + "; ".join(problems + still))
        return result


# ---------------------------------------------------------------------------
# Herdr: read-only identity, consent-gated submit
# ---------------------------------------------------------------------------

class GitAudit:
    """Runner passed into TeamCraft services so THEIR git calls are auditable.

    Without this, the delivery/cleanup code would issue git commands through its
    own default runner and the local-only claim would be unverifiable.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd) -> bytes:
        self.calls.append([str(cwd), *[str(a) for a in args]])
        return workspaces._default_run_git(args, str(cwd))

    def verbs(self) -> list[str]:
        return sorted({c[1] for c in self.calls if len(c) > 1})

    def force_flags(self) -> list[list[str]]:
        return [c for c in self.calls
                if "--force" in c or any(a == "-f" for a in c[2:])]


class Herdr:
    """Audited Herdr client.  Topology mutation is structurally impossible."""

    def __init__(self, *, allow_dispatch: bool,
                 expected_agent: str = "opencode") -> None:
        self.allow_dispatch = allow_dispatch
        self.expected_agent = expected_agent
        self.calls: list[list[str]] = []

    def _classify(self, args: list[str]) -> str:
        head = args[0] if args else ""
        second = args[1] if len(args) > 1 else ""
        if head in FORBIDDEN_HERDR or second in FORBIDDEN_HERDR:
            return "forbidden"
        if (head, second) in READ_ONLY_HERDR:
            return "read"
        if (head, second) in DISPATCH_HERDR:
            return "dispatch"
        return "unknown"

    def run(self, *args: str, timeout: float = 30.0) -> tuple[int, str]:
        kind = self._classify(list(args))
        if kind == "forbidden":
            raise GuardError(
                f"herdr {' '.join(args)}: topology/lifecycle verbs are forbidden; "
                f"this runner never creates, closes, focuses or attaches panes")
        if kind == "unknown":
            raise GuardError(f"herdr {' '.join(args)}: not on the read-only "
                            "allowlist; refusing to guess")
        if kind == "dispatch" and not self.allow_dispatch:
            raise GuardError(
                f"herdr {' '.join(args)}: submitting input to an agent requires "
                f"--confirm-live-run AND --consent-dispatch (gate G1)")
        self.calls.append([*args])
        proc = subprocess.run(["herdr", *args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=timeout, check=False)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def snapshot(self) -> dict[str, Any]:
        code, out = self.run("api", "snapshot")
        if code != 0:
            raise PostconditionError(f"herdr api snapshot failed: {out[:200]}")
        return json.loads(out)["result"]["snapshot"]

    def identity(self, pane_id: str, *,
                 expected_agent: str | None = None) -> dict[str, Any]:
        """Bind one existing pane to its native agent session, pre-dispatch."""
        snap = self.snapshot()
        panes = {p.get("pane_id"): p for p in snap.get("panes", [])}
        agents = {a.get("pane_id"): a for a in snap.get("agents", [])}
        if pane_id not in panes:
            raise PostconditionError(
                f"pane {pane_id} is not present in the live snapshot "
                f"({sorted(p for p in panes if p)}); the runner targets an "
                f"existing pane and never creates one")
        agent = agents.get(pane_id) or {}
        session = agent.get("agent_session") or {}
        tab = next((t for t in snap.get("tabs", [])
                    if t.get("tab_id") == panes[pane_id].get("tab_id")), {})
        binding = {
            "workspace_id": panes[pane_id].get("workspace_id"),
            "tab_id": panes[pane_id].get("tab_id"),
            "tab_label": tab.get("label"),
            "pane_id": pane_id,
            "terminal_id": agent.get("terminal_id"),
            "agent": agent.get("agent"),
            "agent_name": agent.get("name"),
            "agent_status": agent.get("agent_status"),
            "session_kind": session.get("kind"),
            "session_source": session.get("source"),
            "session_id": session.get("value"),
            "cwd": agent.get("cwd") or panes[pane_id].get("cwd"),
            "observed_at": now_iso(),
        }
        want = expected_agent or self.expected_agent
        if binding["agent"] != want:
            raise PostconditionError(
                f"pane {pane_id} hosts agent {binding['agent']!r}, not "
                f"{want!r}; refusing to prompt the wrong agent")
        if not binding["session_id"]:
            raise PostconditionError(
                f"pane {pane_id} exposes no native session id; identity cannot be "
                f"bound, so dispatch evidence would be unverifiable")
        return binding

    def read_pane(self, pane_id: str, limit: int = 4000) -> tuple[int, str]:
        """Read a pane's terminal text (read-only; supplements attestation)."""
        return self.run("pane", "read", pane_id)

    def status(self) -> dict[str, str]:
        """Parse ``herdr status`` into flat ``section.key`` fields."""
        code, out = self.run("status")
        if code != 0:
            raise PostconditionError(f"herdr status failed: {out[:200]}")
        fields: dict[str, str] = {}
        section = ""
        for line in out.splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if line[:1] in (" ", "\t") and section:
                fields[f"{section}.{key}"] = value
            else:
                section = key
                if value:
                    fields[key] = value
        return fields

    def topology_digest(self) -> str:
        """Digest of the structural snapshot (identity, minus volatile counters)."""
        snap = self.snapshot()
        stable = {
            "workspaces": sorted(str(w.get("workspace_id"))
                                for w in snap.get("workspaces", [])),
            "tabs": sorted((str(t.get("tab_id")), str(t.get("pane_count")))
                          for t in snap.get("tabs", [])),
            "panes": sorted(str(p.get("pane_id")) for p in snap.get("panes", [])),
            "agents": sorted((str(a.get("pane_id")), str(a.get("agent")),
                             str((a.get("agent_session") or {}).get("value")))
                          for a in snap.get("agents", [])),
        }
        return sha256_bytes(canonical(stable).encode("utf-8"))

    def submit(self, target: str, prompt: str, *, wait: bool = False,
               until: tuple[str, ...] = (), timeout_ms: int = 5000,
               expected_agent: str | None = None,
               ) -> dict[str, Any]:
        """Submit to an EXISTING agent via ``herdr agent prompt <T> <TEXT>``.

        Herdr's own contract is explicit that this "does not track turns" and
        that ``--wait`` only observes a state after submission — so the result is
        a *transport observation*, never a host-trusted acknowledgement:
        ``prompt_ack`` stays ``unknown`` unless the operator supplies evidence.
        The prompt text itself is not persisted; only its digest and length are.
        """
        if not self.allow_dispatch:
            raise GuardError("dispatch not consented (see --consent-dispatch)")
        before = self.identity(target, expected_agent=expected_agent)
        argv = ["agent", "prompt", target, prompt]
        if wait:
            argv.append("--wait")
            for state in until:
                argv += ["--until", state]
            argv += ["--timeout", str(timeout_ms)]
        # ``--wait --timeout`` may legitimately run for minutes; the subprocess
        # transport timeout must exceed the Herdr wait, or Python kills it and
        # the submit is misclassified as unknown.
        code, out = self.run(
            *argv, timeout=(timeout_ms / 1000.0) + 60.0 if wait else 30.0)
        after = self.identity(target, expected_agent=expected_agent)

        # Classify herdr's documented refusal/stall outcomes by observation.
        lowered = out.lower()
        outcome = "accepted" if code == 0 else "rejected"
        for token in ("agent_blocked", "agent_prompt_stalled", "timeout"):
            if token in lowered:
                outcome = token
                break
        else:
            if code != 0 and "not found" in lowered:
                outcome = "target_not_found"
        return {
            "submitted_at": now_iso(),
            # argv shape recorded WITHOUT the prompt text (which may quote repo
            # content); the digest binds the exact bytes that were sent.
            "argv_shape": ["herdr", "agent", "prompt", target, "<TEXT>"]
            + ([] if not wait else ["--wait", "--timeout", str(timeout_ms)]
               + ([] if not until else ["--until", ",".join(until)])),
            "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
            "prompt_chars": len(prompt),
            "returncode": code,
            "transport_observation": outcome,
            "wait_requested": wait,
            "until": list(until),
            "timeout_ms": timeout_ms if wait else None,
            "output_head": out[:400],
            "identity_before": before,
            "identity_after": after,
            "session_stable": before["session_id"] == after["session_id"],
            # Observation of an exit code or a state change is NOT an ack.
            "prompt_ack": "unknown",
            "event_stream": "unknown",
            "host_trusted_receipt": False,
        }


# ---------------------------------------------------------------------------
# Host gate checks (loopback only)
# ---------------------------------------------------------------------------

def check_omlx(url: str, model: str, *, getter: Any = None) -> dict[str, Any]:
    """Require a healthy loopback oMLX serving the pinned model. No fallback.

    ``getter`` is a transport seam (an HTTP GET) for offline validation; the
    health, catalog and loopback-source rules are applied identically either way.
    """
    validate_loopback(url)
    snapshot = providers.probe_omlx(url, getter=getter)
    result = {
        "url": url, "health": snapshot.health, "reason_code": snapshot.reason_code,
        "source": snapshot.source, "models": list(snapshot.models),
        "pinned_model_present": model in snapshot.models,
        "capabilities": dict(snapshot.capabilities),
    }
    if snapshot.health != "healthy":
        raise GuardError(
            f"loopback oMLX at {url} is {snapshot.health} "
            f"({snapshot.reason_code}); the run aborts — this runner has NO "
            f"cloud fallback and will not substitute another provider")
    if not result["pinned_model_present"]:
        raise GuardError(
            f"pinned model {model!r} is not served by loopback oMLX "
            f"(catalog: {sorted(snapshot.models)}); refusing to fall back to a "
            f"cloud route or an unlisted model")
    if snapshot.name != "omlx" or not any(
            m in snapshot.source for m in ("127.0.0.1", "::1")):
        raise GuardError(
            f"provider snapshot is not a loopback oMLX source "
            f"({snapshot.name!r} @ {snapshot.source!r})")
    return result


def check_protocol(references_dir: Path, version: str, sha256: str) -> dict[str, Any]:
    """Pin the exact protocol pair; any drift aborts."""
    snapshot = ceo_protocol.verify_protocol(
        str(references_dir), expected_version=version, expected_sha256=sha256)
    return {"protocol_version": snapshot.version, "sha256": snapshot.sha256,
            "references": str(references_dir), "pinned_match": True}


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

class M606LiveRun:
    """Disposable OpenCode live E2E: provision → dispatch → deliver → cleanup."""

    def __init__(self, args: argparse.Namespace, *, herdr: Any = None,
                 omlx_getter: Any = None, harness_runner: Any = None,
                 executable_lookup: Any = None) -> None:
        """Accept injected host probes for offline validation.

        They replace only the *transport* (a pane, an HTTP GET, a subprocess),
        never a rule: every snapshot obtained through them is validated by the
        same loopback / health / model checks, and every pane gate still runs.
        Offline validation therefore proves real logic, not a stub's compliance.
        """
        self.args = args
        self.executable_lookup = executable_lookup or shutil.which
        self.resuming = bool(getattr(args, "resume_handoff", False))
        #: Post-dispatch continuation: this invocation re-enters a scenario that
        #: ALREADY dispatched, and is structurally incapable of submitting input.
        self.continuing = bool(getattr(args, "post_dispatch", False))
        self.root = validate_root(args.root, allow_non_temp=args.allow_non_temp_root,
                                 for_cleanup=bool(args.cleanup_only),
                                 for_resume=self.resuming)
        self.model = args.model
        self.omlx_url = args.omlx_url
        self.protocol_version = args.protocol_version
        self.protocol_sha256 = args.protocol_sha256
        self.run_id = sha256_bytes(
            f"{self.root}|{now_iso()}|{os.getpid()}".encode())[:16]
        self.stage = "init"
        self.evidence: Evidence | None = None
        self.ledger = Ledger(self.root, self.run_id)
        self.herdr = herdr or Herdr(
            allow_dispatch=bool(args.consent_dispatch and args.confirm_live_run),
            expected_agent=args.herdr_agent)
        self.omlx_getter = omlx_getter
        self.harness_runner = harness_runner
        self.conn: Any = None
        self.task: Any = None
        self.attempt: Any = None
        self.allocation: Any = None
        self.brief: Any = None
        #: PG-7 leg state: "not-requested" | "executed" | "already-initialized"
        #: | "refused" — reported in the manifest so a skipped leg is never
        #: silently counted as an executed stage.
        self.ceo_stage = "not-requested"
        self.ceo_report: dict[str, Any] | None = None
        #: one-use secrets (the CEO nonce) that must never reach disk; finalise
        #: sweeps every persisted artifact for them and reports the result.
        self._ephemeral: list[str] = []
        self.bootstrap_calls: list[list[str]] = []
        self.git_audit = GitAudit()
        self._finalised = False
        self.model_proof: dict[str, Any] = {}
        self.worker_report: Any = None
        self.probes: dict[str, Any] = {}
        self.dispatch_state: str | None = None
        # "live-agent" only once a prompt has actually been submitted; the
        # scripted harness leg stamps "scripted" so the two can never be confused.
        self.evidence_class = "none"
        #: Transport lock held by the post-dispatch continuation: while it is set,
        #: :meth:`_submit_to_pane` refuses, so this process cannot prompt a pane.
        self._submit_lock = False
        self._submit_count = 0
        #: Continuation reporting: how the prior dispatch was proved, how the
        #: WorkerReport was bound, and how the Task reached ``verifying``.
        self.continuation_report: dict[str, Any] | None = None
        self.worker_done: dict[str, Any] = {}
        self.worker_done_projection = "not-attempted"

    # -- helpers ------------------------------------------------------------

    @property
    def project(self) -> Path:
        return self.root / "project"

    @property
    def origin(self) -> Path:
        return self.root / "origin.git"

    @property
    def references(self) -> Path:
        return (self.project / ".agents" / "skills" / "teamcraft-ceo"
                / "references")

    def lease(self) -> Any:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_row.id),
            f"m606-live-{self.run_id}", ttl_seconds=3700)

    def _submit_to_pane(self, pane_id: str, prompt: str, *, wait: bool,
                        until: tuple[str, ...], timeout_ms: int,
                        expected_agent: str | None = None) -> dict[str, Any]:
        """The ONLY door from this runner to Herdr input, and it can be locked.

        :meth:`Herdr.submit` already refuses without dispatch consent, but a
        lock held by this object makes the guarantee independent of how the
        transport was constructed or injected: a post-dispatch continuation
        physically cannot send a prompt, and ``_submit_count`` proves afterwards
        that it did not.
        """
        if self._submit_lock:
            raise GuardError(
                "this invocation is a post-dispatch continuation: it submits no "
                "prompt to any pane (" + POST_DISPATCH_NOTE + ")")
        self._submit_count += 1
        return self.herdr.submit(pane_id, prompt, wait=wait, until=until,
                                timeout_ms=timeout_ms,
                                expected_agent=expected_agent)

    def git(self, *args: str, check: bool = True) -> tuple[int, str]:
        """A delivery-path git call: strictly no push/clone/merge/reset/rewrite."""
        code, out = git(self.project, *args, check=check)
        self.ledger.git_calls.append([str(self.project), *args])
        return code, out

    def bgit(self, cwd: Path, *args: str, check: bool = True) -> tuple[int, str]:
        """A bootstrap git call, recorded separately for the local-only audit."""
        code, out = git(cwd, *args, check=check)
        self.bootstrap_calls.append([str(cwd), *args])
        return code, out

    def _remote_target(self, cwd: str, token: str) -> str | None:
        """Resolve a remote NAME (e.g. ``origin``) to its filesystem path."""
        code, url = git(Path(cwd), "remote", "get-url", token, check=False)
        return url if code == 0 and url else None

    def audit_git(self, where: str) -> dict[str, Any]:
        """Prove local-only behavior from the recorded argv, not from intent.

        Delivery-path calls may not use any powerful verb at all.  Bootstrap
        calls may ``init``/``clone``/``push`` ONLY against this run's own local
        bare origin (remote names are resolved before judging), never a URL,
        and never forced.
        """
        delivery_used = {c[1] for c in self.ledger.git_calls if len(c) > 1}
        delivery_used |= set(self.git_audit.verbs())
        bad = delivery_used & FORBIDDEN_GIT
        if bad:
            raise PostconditionError(
                f"{where}: delivery path issued forbidden git verbs "
                f"{sorted(bad)}; a local-only run never pushes, merges, resets "
                f"or rewrites")
        forced = self.git_audit.force_flags()
        if forced:
            raise PostconditionError(
                f"{where}: forced git operation(s) issued: {forced[:3]}")
        offenders: list[str] = []
        local_origin = real(self.origin)
        for call in self.bootstrap_calls:
            if len(call) < 2:
                continue
            cwd, verb, rest = call[0], call[1], call[2:]
            if verb in {"merge", "rebase", "reset", "filter-branch", "clean",
                        "gc", "prune", "tag", "notes"}:
                offenders.append(f"powerful verb {verb}: {' '.join(call)}")
            if "--force" in call or any(a == "-f" for a in rest):
                offenders.append(f"forced operation: {' '.join(call)}")
            if verb in ("push", "clone"):
                token = next((a for a in rest if not a.startswith("-")), "")
                if not token:
                    offenders.append(f"{verb} with no target: {' '.join(call)}")
                    continue
                if "://" in token or token.startswith("git@"):
                    offenders.append(f"{verb} to an external URL: "
                                   f"{' '.join(call)}")
                    continue
                resolved = self._remote_target(cwd, token) or token
                if real(resolved) != local_origin:
                    offenders.append(
                        f"{verb} target {token!r} (→ {real(resolved)}) is not "
                        f"this run's local origin {local_origin}")
        if offenders:
            raise PostconditionError(
                f"{where}: git activity exceeded the local-only envelope: "
                f"{offenders[:4]}")
        return {"delivery_verbs": sorted(delivery_used),
                "bootstrap_verbs": sorted({c[1] for c in self.bootstrap_calls
                                          if len(c) > 1}),
                "bootstrap_calls": len(self.bootstrap_calls),
                "local_origin_only": True,
                "origin": str(self.origin)}

    # -- G0: read-only host preflight -----------------------------------------

    def preflight(self) -> dict[str, Any]:
        self.stage = "preflight"
        out: dict[str, Any] = {"stage": self.stage, "writable": False}
        out["omlx"] = check_omlx(self.omlx_url, self.model,
                                getter=self.omlx_getter)
        out["protocol_canonical"] = check_protocol(
            REPO_ROOT / ".agents" / "skills" / "teamcraft-ceo" / "references",
            self.protocol_version, self.protocol_sha256)
        binaries = {n: (self.executable_lookup(n) or "absent")
                    for n in ("opencode", "codex", "herdr", "claude")}
        out["binaries"] = binaries
        if binaries["opencode"] == "absent":
            raise GuardError("opencode is not on PATH; the OpenCode leg cannot run")
        if binaries["herdr"] == "absent":
            raise GuardError("herdr is not on PATH; identity cannot be bound")
        if binaries["claude"] != "absent":
            # Not a blocker, but recorded: Claude legs stay out of scope (PG-1).
            out["claude_present_note"] = ("claude binary present; Claude legs "
                                         "remain out of scope (PG-1)")
        out["herdr_status"] = self.herdr.status()
        status = out["herdr_status"]
        # G3 transport precondition: matched client/server, compatible protocol,
        # nothing pending restart. Any of these failing means dispatch evidence
        # could not be trusted, so the run aborts here rather than mid-leg.
        required_yes = ("server.endpoint_compatible",
                        "server.private_protocol_compatible",)
        for key in required_yes:
            if status.get(key) != "yes":
                raise GuardError(
                    f"herdr {key}={status.get(key)!r}: client/server protocol is "
                    f"not compatible, so dispatch identity could not be trusted")
        if status.get("update.restart_needed") != "no":
            raise GuardError(
                f"herdr update.restart_needed={status.get('update.restart_needed')!r}"
                f"; a pending restart makes session behavior unverifiable")
        if status.get("server.status") != "running":
            raise GuardError(f"herdr server is {status.get('server.status')!r}")
        if status.get("client.version") != status.get("server.version"):
            raise GuardError(
                f"herdr client {status.get('client.version')!r} != server "
                f"{status.get('server.version')!r}; version skew invalidates the "
                f"session contract")
        out["herdr_protocol"] = (status.get("server.private_protocol")
                                or status.get("client.protocol"))
        out["herdr_topology_digest"] = self.herdr.topology_digest()
        out["root"] = str(self.root)
        out["root_would_be_created"] = not self.root.exists()
        out["repo_dirty_entries"] = len(subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, text=True, check=False).stdout.splitlines())
        return out

    # -- G1: provision -------------------------------------------------------

    def provision(self) -> dict[str, Any]:
        self.stage = "provision"
        # The route gate runs BEFORE anything is created: a dead loopback or an
        # absent pinned model must abort a pristine filesystem, not leave a
        # half-built world behind (and never falls back to a cloud route).
        route = check_omlx(self.omlx_url, self.model, getter=self.omlx_getter)
        self.root.mkdir(parents=True, exist_ok=False)
        self.ledger.adopt_root(self.root)
        ev = Evidence(self.root, self.run_id)
        self.evidence = ev
        ev.write("route-gate.json", {
            **route, "model": self.model,
            "capabilities_bound_in": "allocate",
            "cloud_fallback": "not-permitted",
            "gated_before": "any filesystem or database write",
        })
        ev.write("isolation.json", {
            "run_id": self.run_id, "root": str(self.root),
            "outside_repo": True, "repo_root": str(REPO_ROOT),
            "created_at": now_iso(), "owns_root": True,
        })

        # Git world: bare origin + seeded base + clone.  Every push/clone here
        # targets this run's own local paths; the audit below proves it.
        self.bgit(self.root, "init", "-q", "--bare", str(self.origin))
        self.ledger.track(self.origin, "bare-repo")
        seed = self.root / "seed"
        seed.mkdir()
        self.ledger.track(seed, "seed")
        self.bgit(seed, "init", "-q")
        self.bgit(seed, "remote", "add", "origin", str(self.origin))
        (seed / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
        (seed / "src").mkdir()
        (seed / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
        (seed / "docs").mkdir()
        (seed / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
        self.bgit(seed, "add", "-A")
        self.bgit(seed, "-c", "user.name=M606 Live",
                  "-c", "user.email=m606@example.invalid", "commit", "-q",
                  "-m", "base")
        self.bgit(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.base_sha = git(self.origin, "rev-parse", "refs/heads/main")[1]
        self.bgit(self.root, "clone", "-q", str(self.origin), str(self.project))
        self.ledger.track(self.project, "clone")
        # An operator's clone has an author identity; delivery refuses to
        # fabricate one, so it is configured locally (never globally).
        git(self.project, "config", "user.name", "M606 Live")
        git(self.project, "config", "user.email", "m606@example.invalid")
        git_audit = self.audit_git("provision")
        ev.write("git-locality.json", {
            **git_audit,
            "note": ("bootstrap push/clone are permitted only against this "
                    "run's own local bare origin; the delivery path may never "
                    "push, and the seed push exists solely to give the clone a "
                    "base commit"),
        })
        ev.post("provision", "seed push targeted the local bare origin only",
                expected=True, observed=git_audit["local_origin_only"])
        ev.post("provision", "clone HEAD equals the seeded base",
                expected=self.base_sha,
                observed=git(self.project, "rev-parse", "HEAD")[1])

        # Managed install into the disposable clone only, then protocol pin.
        installer = REPO_ROOT / "scripts" / "install.sh"
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent),
                                       env.get("PATH", "")])
        dry = subprocess.run(["bash", str(installer), "--target",
                             str(self.project), "--dry-run"], cwd=str(REPO_ROOT),
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, check=False)
        real_run = subprocess.run(["bash", str(installer), "--target",
                                  str(self.project)], cwd=str(REPO_ROOT), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, check=False)
        if real_run.returncode != 0:
            raise PostconditionError(
                f"installer into {self.project} failed: {real_run.stderr[:300]}")
        ev.write("installer.json", {
            "dry_run_returncode": dry.returncode,
            "dry_run_planned": sum(1 for line in dry.stdout.splitlines()
                                  if "create" in line),
            "run_returncode": real_run.returncode,
            "changes": sum(1 for line in real_run.stdout.splitlines()
                           if "create" in line),
            "target": str(self.project),
        })
        ev.post("provision", "installer dry-run wrote nothing",
                expected=dry.returncode, observed=0)
        self.protocol = check_protocol(self.references, self.protocol_version,
                                     self.protocol_sha256)
        ev.write("protocol-verify.json", self.protocol)

        # Run-local DB at head (never the live runtime DB).
        self.db = self.root / "teamcraft.db"
        self.migration = storage.migrate_to_head(self.db,
                                                 allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.ledger.track(self.db, "db")
        self.project_row = domain.create_project(
            self.conn, f"m606-live-{self.run_id[:8]}", str(self.project))

        # Re-read the route from a fresh probe (the gate above proved it before
        # any write; this confirms it still holds after the DB exists).
        # Probes are recorded and bound later in capture_and_bind_capabilities(),
        # so an Attempt binds to exactly the snapshots that are durably
        # registered — never to a later re-probe.
        provider = self._provider_snapshot()
        ev.post("provision", "provider still loopback oMLX after provisioning",
                expected="healthy", observed=provider.health)
        ev.post("provision", "pinned model still in loopback catalog",
                expected=True, observed=(self.model in provider.models))
        ev.post("provision", "catalog presence alone is not pane proof",
                expected=False, observed=bool(self.model_proof.get("attested")))
        ev.post("provision", "fresh DB at schema head",
                expected=storage.SCHEMA_VERSION, observed=self.migration.to_version)

        self.manifest = {
            "schema_version": 1, "run_id": self.run_id, "milestone": "M6-06",
            "created_at": now_iso(), "root": str(self.root),
            "protected_root": str(REPO_ROOT),
            "stage_matrix": {**{s: "pending" for s in STAGES},
                            "provision": "executed"},
            "protocol": self.protocol, "model": self.model,
            "omlx_url": self.omlx_url,
            "database": {"path": str(self.db), "fresh": self.migration.fresh,
                        "from": self.migration.from_version,
                        "to": self.migration.to_version,
                        "schema_head": storage.SCHEMA_VERSION},
            "git": {"origin": str(self.origin), "project": str(self.project),
                   "base_sha": self.base_sha},
            # Nothing has run yet: no agent evidence, no pane model proof.
            "evidence_class": self.evidence_class,
            "pane_model_proof": {"status": "pending", "attested": False},
            "evidence_class": "none",
            "pane_model_proof": {"status": "pending", "attested": False},
            "delivery": {"disposition": DELIVERY_UNKNOWN,
                        "human_accepted": False, "ack_source": None},
            "non_claims": {
                "prompt_ack": "unknown", "event_stream": "unknown",
                "host_trusted_receipt": False, "push": False,
                "claude": "pending:PG-1", "cloud": "not-permitted",
            },
        }
        # The manifest carries the postconditions recorded SO FAR, so a
        # provision-only run still shows the evidence it produced.
        ev.post("provision", "root is outside the project repository",
                expected=False,
                observed=str(self.root).startswith(str(REPO_ROOT) + os.sep))
        self.manifest["postconditions"] = ev.postconditions
        ev.write("run-manifest.json", self.manifest)
        return {"stage": self.stage, "root": str(self.root),
                "base_sha": self.base_sha, "protocol": self.protocol,
                "manifest": self.manifest}

    # -- worker leg: real (dispatch) or explicitly scripted --------------------

    def run_worker_leg(self) -> dict[str, Any]:
        return (self.scripted_worker_leg() if self.args.scripted_worker_change
                else self.dispatch())

    def provision_attempt_resources(self) -> tuple[Any, Any, Any, Any]:
        """Task → attempt → allocated worktree → session → brief (run-local DB)."""
        pane = self.args.herdr_pane
        task = domain.create_task(
            self.conn, self.project_row.id,
            f"m606 {'scripted' if self.args.scripted_worker_change else 'live'}"
            f" read-mostly leg",
            kind="ship", ownership_json=json.dumps(["src/**"]))
        task = domain.apply_task_event(self.conn, task.id,
                                      "dependencies_satisfied",
                                      expected_version=task.version)
        task = domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"delivery": "local_only"}, actor="ceo",
            reason="M6-06 local-only scenario policy")
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version,
            protocol_version=self.protocol["protocol_version"],
            protocol_hash=self.protocol["sha256"])
        allocation, _created = worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.project), run_git=self.git_audit)
        self.ledger.track_git_worktree_add(allocation.worktree_path,
                                          allocation.branch,
                                          allocation.base_sha)
        # Session binding semantics differ by phase, and are never overstated:
        # the scripted harness genuinely works in-process; a live attempt has no
        # confirmed pane/session until phase 2 gates and submits it.
        if self.args.scripted_worker_change:
            native, pane_id, state = f"scripted-{attempt.id}", None, "working"
        elif pane:
            native = self.herdr.identity(pane)["session_id"]
            pane_id, state = pane, "idle"
        else:
            native, pane_id, state = None, None, "unknown"
        domain.register_session(self.conn, attempt.id, "opencode",
                               native_session_id=native, pane_id=pane_id,
                               state=state)
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        brief = briefs.create_attempt_brief(
            self.conn, attempt.id,
            content=("Read-mostly task: describe the structure of docs/ in this "
                    "repository and add one line to src/feature.py containing "
                    f"{MARKER}. Do not edit any other path."))
        return task, attempt, allocation, brief

    def finish_attempt_if_open(self, attempt_id: str) -> str:
        """Move the attempt to ``finished`` unless the report path did it already.

        ``worker_reported_done`` may itself settle the attempt, so this reads the
        current state instead of forcing a transition that could be illegal.
        """
        current = domain.get_attempt(self.conn, attempt_id).status
        if current == "finished":
            return "already-finished"
        domain.transition_attempt(self.conn, attempt_id, "finish")
        return "finished-now"

    def write_worker_report(self, task: Any, attempt: Any, brief: Any,
                           summary: str) -> Any:
        report = worker_reports.record_worker_report(self.conn, {
            "schema_version": 1, "task_id": task.id, "attempt_id": attempt.id,
            "task_version": brief.task_version, "brief_digest": brief.brief_digest,
            "report_kind": "final", "outcome": "completed",
            "milestone_id": "final", "summary": summary,
            "artifact_digest": None, "usage": {"total_tokens": None},
        })
        domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version)
        self.worker_report = report
        return report

    def scripted_worker_leg(self) -> dict[str, Any]:
        """OFFLINE leg: THIS harness writes the change and reports as the worker.

        Only valid for harness validation. ``evidence_class`` is stamped
        ``scripted`` so no downstream reader can mistake it for real-agent
        evidence, and it is mutually exclusive with dispatch consent.
        """
        self.stage = "dispatch"
        ev = self._need_evidence()
        marker = self.args.scripted_marker or MARKER
        task, attempt, allocation, brief = self.provision_attempt_resources()
        self.capture_and_bind_capabilities(attempt.id)
        worktree = Path(allocation.worktree_path)
        (worktree / "src" / "feature.py").write_text(
            f"{marker}: authored by the scripted harness leg\n", encoding="utf-8")
        report = self.write_worker_report(
            task, attempt, brief,
            f"SCRIPTED leg: this harness wrote {marker!r}; no agent ran")
        how = self.finish_attempt_if_open(attempt.id)
        self._task, self._attempt, self._allocation = (
            domain.get_task(self.conn, task.id),
            domain.get_attempt(self.conn, attempt.id), allocation)
        self.attempt, self.task, self.allocation = self._attempt, self._task, \
            allocation
        self.dispatch_state = "scripted-complete"
        self.evidence_class = "scripted"
        record = {
            "leg": "scripted", "evidence_class": "scripted",
            "real_agent_evidence": False, "task_id": task.id,
            "attempt_id": attempt.id,
            "task_status_after": self._task.status,
            "attempt_status_after": self._attempt.status,
            "attempt_finish_path": how,
            "worker_report_digest": report.report_digest,
            "brief_digest": brief.brief_digest,
            "marker_written": marker,
            "worktree_path": allocation.worktree_path,
        }
        ev.write("scripted-worker-leg.json", record)
        ev.post("dispatch", "scripted leg never claims a real agent",
                expected=False, observed=record["real_agent_evidence"])
        return record

    def ingest_worker_report(self) -> Any:
        """Persist an externally produced WorkerReport, if one was supplied."""
        if not self.args.worker_report_file:
            return None
        path = Path(self.args.worker_report_file).expanduser()
        if not path.is_file():
            raise GuardError(f"--worker-report-file {path} does not exist")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GuardError(f"worker report file is not valid JSON: {exc}")
        self.worker_report = worker_reports.record_worker_report(self.conn, data)
        return self.worker_report

    def _project_worker_done_on_task(self, task: Any) -> str:
        """Bring the Task to ``verifying`` through the domain's own event.

        ``worker_reported_done`` is refused by :mod:`teamcraft.domain` unless the
        Task's current Attempt is genuinely ``finished`` — and only a durable FINAL
        WorkerReport can finish it.  So the projection follows the evidence: no
        code path here can assert completion that the store does not already
        support.
        """
        now = domain.get_task(self.conn, task.id)
        if now.status == "verifying":
            self.worker_done_projection = "already-projected"
        elif now.status == "running":
            domain.apply_task_event(self.conn, task.id, "worker_reported_done",
                                   expected_version=now.version)
            self.worker_done_projection = "applied-from-durable-final-report"
        else:
            self.worker_done_projection = f"not-eligible:{now.status}"
        return str(domain.get_task(self.conn, task.id).status)

    # -- phase 1: allocate and hand off ---------------------------------------

    def allocate(self) -> dict[str, Any]:
        """Create the attempt worktree, bind capabilities, hand off to a human.

        Nothing is submitted here, and nothing can be: live dispatch requires a
        separate resumed invocation, because the operator needs the window between
        phases to place an existing pane in the allocated cwd and to attest the
        model that pane is actually running.
        """
        self.stage = "allocate"
        ev = self._need_evidence()
        task, attempt, allocation, brief = self.provision_attempt_resources()
        binding = self.capture_and_bind_capabilities(attempt.id)
        self.task, self.attempt, self.allocation, self.brief = (
            task, attempt, allocation, brief)
        cwd = real(allocation.worktree_path)
        handoff = {
            "phase": "allocated-awaiting-operator",
            "run_id": self.run_id, "created_at": now_iso(),
            "root": str(self.root), "project_id": self.project_row.id,
            "task_id": task.id, "attempt_id": attempt.id,
            "allocation_id": allocation.allocation_id,
            "allocated_cwd": cwd, "branch": allocation.branch,
            "base_sha": allocation.base_sha,
            "claims": list(allocation.claims),
            "brief_digest": brief.brief_digest,
            "protocol": self.protocol, "model": self.model,
            "omlx_url": self.omlx_url,
            "capability_digests": {
                "provider": binding["provider_digest"],
                "harness": binding["harness_digest"]},
            "operator_actions": [
                f"cd an existing OpenCode pane into {cwd}",
                "confirm that pane is idle and note its native session id",
                "write an attestation JSON naming pane_id, terminal_id, "
                "session_id, agent, and the model that pane is running",
                "re-run with --resume-handoff --consent-dispatch --herdr-pane "
                "<pane> --pane-model-evidence <file> --expect-session-id <id>",
                "optional (PG-7): add --ceo-pane <pane> "
                "--ceo-expect-session-id <ceo-session> to bootstrap the canonical "
                "CEO here first; add --wake-deliver to actually deliver the "
                "resulting Wake to that CEO pane",
            ],
            "submit_allowed_in_this_invocation": False,
            "why": HANDOFF_REQUIRED,
        }
        ev.write("handoff.json", handoff)
        ev.post("allocate", "handoff names the exact allocated cwd",
                expected=cwd, observed=handoff["allocated_cwd"])
        ev.post("allocate", "phase 1 submitted no prompt",
                expected=False, observed=self.dispatch_state is not None)
        ev.post("allocate", "phase 1 created no worker evidence",
                expected="none", observed=self.evidence_class)
        return handoff

    # -- phase 2: resume, re-validate, then gate and submit -----------------

    def _resume_load(self) -> tuple[Any, Any, Any, Any, dict[str, Any],
                                   dict[str, Any], "Evidence"]:
        """Re-open the run that already exists on disk and re-prove its facts.

        Returns ``(task, attempt, allocation, brief, binding, handoff, evidence)``.
        Shared by both phase-2 paths — the dispatching one and the post-dispatch
        continuation — because both must fail on exactly the same durable drift.
        Nothing here contacts a pane: the caller decides whether a prompt happens.
        """
        path = self.root / "evidence" / "handoff.json"
        if not path.is_file():
            raise GuardError(
                f"--resume-handoff but no handoff at {path}: phase 1 "
                "(--stop-after allocate) must complete first")
        handoff = json.loads(path.read_text("utf-8"))
        self.run_id = str(handoff.get("run_id") or self.run_id)
        self.ledger = Ledger(self.root, self.run_id)
        self.ledger.load_journal()
        self.evidence = Evidence(self.root, self.run_id, resume=True)
        ev = self.evidence
        prior = self.root / "evidence" / "run-manifest.json"
        if not prior.is_file():
            raise GuardError(f"resume: no run manifest at {prior}")
        self.manifest = json.loads(prior.read_text("utf-8"))
        if self.manifest.get("run_id") != self.run_id:
            raise GuardError(
                f"resume: manifest run_id {self.manifest.get('run_id')!r} does "
                f"not match handoff run_id {self.run_id!r}")
        self.manifest["resumed_at"] = now_iso()

        db = self.root / "teamcraft.db"
        if not db.is_file():
            raise GuardError(f"resume: the run-local database {db} is missing")
        self.db = db
        self.conn = storage.connect(db, allow_unverified_location=True)
        self.project_row = self._load_project(str(handoff["project_id"]))
        self.protocol = check_protocol(self.references, self.protocol_version,
                                     self.protocol_sha256)
        self.base_sha = str(handoff["base_sha"])
        task = domain.get_task(self.conn, str(handoff["task_id"]))
        attempt = domain.get_attempt(self.conn, str(handoff["attempt_id"]))
        allocation = worktrees.load_allocation(self.conn, attempt.id)
        brief = briefs.get_attempt_brief(self.conn, attempt.id)
        if allocation is None:
            raise GuardError("resume: the allocation record is gone")
        drift = [f"{label}: {want!r} -> {got!r}" for label, want, got in (
            ("allocated cwd", str(handoff["allocated_cwd"]),
             real(allocation.worktree_path)),
            ("branch", str(handoff["branch"]), allocation.branch),
            ("base sha", str(handoff["base_sha"]), allocation.base_sha),
            ("brief digest", str(handoff["brief_digest"]), brief.brief_digest),
            ("model", str(handoff["model"]), self.model),
        ) if want != got]
        if drift:
            raise GuardError(
                "resume: durable facts drifted between phases "
                f"({'; '.join(drift)}); this handoff is void")
        if not Path(allocation.worktree_path).exists():
            raise GuardError(
                f"resume: allocated worktree {allocation.worktree_path} no longer "
                f"exists; refusing to prompt a pane whose workspace is gone")
        binding = self.verify_durable_binding(handoff)
        self.task, self.attempt, self.allocation, self.brief = (
            task, attempt, allocation, brief)
        ev.write("resume-verification.json", {
            "phase": "resumed", "at": now_iso(), "run_id": self.run_id,
            "task_id": task.id, "attempt_id": attempt.id,
            "allocation_id": allocation.allocation_id,
            "allocated_cwd": real(allocation.worktree_path),
            "durable_facts_match": True,
            "consent": bool(self.args.consent_dispatch),
            "mode": CONTINUATION_STAGE if self.continuing else "dispatch",
        })
        return task, attempt, allocation, brief, binding, handoff, ev

    def _read_prior_dispatch(self) -> dict[str, Any] | None:
        """The recorded dispatch evidence, or None when there is none."""
        ev = self._need_evidence()
        path = ev.dir / "dispatch.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"state": "unreadable"}
        return data if isinstance(data, dict) else {"state": "unreadable"}

    def resume(self) -> dict[str, Any]:
        """Re-prove every durable fact, then gate the pane and submit once."""
        self.stage = "dispatch"
        task, attempt, allocation, brief, binding, _handoff, ev = \
            self._resume_load()
        if not self.args.consent_dispatch:
            # Read-only gate rehearsal: prove the gates WITHOUT writing input.
            gate = self.gate_dispatch_target()
            proof = self.pane_model_proof(gate)
            report = {"phase": "resumed", "submit": "not-consented",
                     "pane_gate": gate, "pane_model_proof": proof,
                     "would_submit": bool(proof.get("attested")),
                     "note": ("all gates evaluated read-only; submitting requires "
                            "--consent-dispatch")}
            prior = self._read_prior_dispatch()
            if prior is not None and prior.get("state") in ("submitted", "unknown"):
                # Integrity: a rehearsal must never overwrite the record of a
                # dispatch that already happened — that record is the only proof
                # the prompt went out exactly once.
                report["prior_dispatch_state"] = str(prior["state"])
                report["overwrote_dispatch_evidence"] = False
                report["note"] += (
                    f"; a dispatch is already recorded ({prior['state']}) and was "
                    "left intact — continue it with --post-dispatch, never by "
                    "dispatching again")
                ev.write("dispatch-rehearsal.json", report)
                return report
            ev.write("dispatch.json", report)
            return report
        return self._submit_core(task, attempt, allocation, brief, binding, ev)

    # -- post-dispatch continuation ------------------------------------------
    #
    # The situation this exists for: dispatch completed and is on record, and the
    # worker's final report arrives later (an agent finished after the run
    # returned, or delivery failed after dispatch).  Every alternative would mean
    # re-running the dispatching invocation, i.e. sending a SECOND prompt to an
    # agent that may already be doing the work.  So this path:
    #   * locks its transport (``_submit_lock``) before anything else, so no
    #     prompt can leave this process by any route;
    #   * re-proves the prior dispatch from durable artifacts AND the run-local
    #     database, refusing a missing, unreadable or UNKNOWN record;
    #   * binds the external final WorkerReport to that exact attempt;
    #   * then hands over to the ordinary verification/delivery stage.
    # ----------------------------------------------------------------------

    def resume_post_dispatch(self) -> dict[str, Any]:
        """Continue an already-dispatched attempt WITHOUT submitting anything."""
        self.stage = CONTINUATION_STAGE
        self._submit_lock = True        # nothing may leave this process
        task, attempt, allocation, brief, _binding, handoff, ev = \
            self._resume_load()
        proof: dict[str, Any] = {}
        failure: dict[str, str] | None = None
        try:
            proof = self._prove_prior_dispatch(ev, handoff, task, attempt,
                                              allocation, brief)
            self._adopt_final_report(ev, task, attempt, brief)
        except Exception as exc:
            # Record WHY the continuation refused, then let it propagate: the
            # abort reason must stay the primary one, not be masked by reporting.
            failure = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            report = {
                "stage": CONTINUATION_STAGE,
                "prompts_submitted_by_this_invocation": self._submit_count,
                "dispatch_replayed": self._submit_count > 0,
                "prior_dispatch": proof,
                "worker_report": dict(self.worker_done),
                "attempt_status": domain.get_attempt(self.conn, attempt.id).status,
                "task_status": domain.get_task(self.conn, task.id).status,
                "proof_complete": failure is None,
                "failure": failure,
                "note": POST_DISPATCH_NOTE,
            }
            ev.write("post-dispatch.json", report)
            self.continuation_report = report
            # The no-prompt invariant is asserted on BOTH paths — even a
            # continuation that refused its proof submitted nothing, and the
            # transport lock is what guarantees ``_submit_count`` stayed 0.
            ev.post("post-dispatch", "this continuation issued zero prompts",
                    expected=0, observed=self._submit_count)
            ev.post("post-dispatch", "the recorded dispatch was not replayed",
                    expected=False, observed=report["dispatch_replayed"])
        return report

    def _prove_prior_dispatch(self, ev: Evidence, handoff: dict[str, Any],
                             task: Any, attempt: Any, allocation: Any,
                             brief: Any) -> dict[str, Any]:
        """Require durable, unambiguous proof that THIS attempt was dispatched."""
        recorded = self._read_prior_dispatch()
        if recorded is None:
            raise GuardError(
                f"post-dispatch: no dispatch evidence under {ev.dir}"
                "/dispatch.json — this attempt was never dispatched here, and this "
                "mode never dispatches (use --resume-handoff --consent-dispatch for "
                "the one permitted dispatch)")
        state = str(recorded.get("state") or "missing")
        if state == "unknown":
            raise GuardError(
                "post-dispatch: the recorded dispatch is UNKNOWN (the transport "
                "outcome was unresolved); an unresolved effect must be reconciled, "
                "never continued as if it had succeeded and never re-sent")
        if state != "submitted":
            raise GuardError(
                f"post-dispatch: recorded dispatch state is {state!r}, not "
                "'submitted' — nothing is continued from an unaccepted transport")
        if int(recorded.get("returncode", -1) or 0) != 0:
            raise GuardError(
                "post-dispatch: the recorded dispatch did not return success "
                f"(returncode {recorded.get('returncode')!r})")
        if str(recorded.get("transport_observation") or "") != "accepted":
            raise GuardError(
                "post-dispatch: transport observation in the record is "
                f"{recorded.get('transport_observation')!r}, not 'accepted'")
        if recorded.get("topology_unchanged") is not True:
            raise GuardError(
                "post-dispatch: the recorded dispatch does not prove the topology "
                "was unchanged, so its target pane is unverifiable")

        # Exact binding: the record must describe THIS attempt, from THIS root.
        mismatches = [
            f"{label}: dispatch {got!r} != durable {want!r}"
            for label, want, got in (
                ("task id", task.id, str(recorded.get("task_id") or "")),
                ("attempt id", attempt.id, str(recorded.get("attempt_id") or "")),
                ("allocation id", allocation.allocation_id,
                 str(recorded.get("allocation_id") or "")),
                ("brief digest", brief.brief_digest,
                 str(recorded.get("brief_digest") or "")),
                ("branch", allocation.branch, str(recorded.get("branch") or "")),
                ("worktree", real(allocation.worktree_path),
                 real(unredact_root(str(recorded.get("worktree_path") or ""),
                                   self.root))),
                ("model", self.model, str(recorded.get("model") or "")),
                ("run id", self.run_id, str(handoff.get("run_id") or "")),
            ) if want != got
        ]
        if mismatches:
            raise GuardError(
                "post-dispatch: dispatch evidence does not bind this attempt ("
                + "; ".join(mismatches) + ")")

        # The prompt must have carried its binding, and the pane proof must stand.
        pb_path = ev.dir / "prompt-binding.json"
        if not pb_path.is_file():
            raise GuardError(
                f"post-dispatch: {pb_path} is missing — without the prompt-binding "
                "record there is no proof the dispatched prompt carried this "
                "allocation, so the change cannot be attributed")
        pb = json.loads(pb_path.read_text(encoding="utf-8"))
        carries = [k for k, v in pb.items()
                   if k.startswith("carries_") and v is not True]
        if pb.get("prompt_sha256") != recorded.get("prompt_sha256"):
            raise GuardError(
                "post-dispatch: prompt digest differs between dispatch.json and "
                "prompt-binding.json — the two records do not describe one prompt")
        ev.post("post-dispatch", "the dispatched prompt carried its allocation "
                                "binding", expected=[], observed=sorted(carries))
        if str(pb.get("carries_brief_digest")) != "True":
            raise GuardError("post-dispatch: prompt did not carry the brief digest")

        pp_path = ev.dir / "pane-model-proof.json"
        if not pp_path.is_file():
            raise GuardError("post-dispatch: pane model proof is missing; the "
                            "dispatch target's model was never attested")
        pp = json.loads(pp_path.read_text(encoding="utf-8"))
        if not pp.get("attested") or str(pp.get("status")) != "operator-attested":
            raise GuardError(
                "post-dispatch: pane model proof is not operator-attested "
                f"(status {pp.get('status')!r})")
        ev.post("post-dispatch", "pane model proof stayed non-host-trusted",
                expected=False, observed=bool(pp.get("host_trusted")))

        # Database corroboration: the confirmed session the dispatch registered.
        rows = self.conn.execute(
            "SELECT native_session_id, pane_id, state FROM sessions"
            " WHERE attempt_id=? ORDER BY created_at", (attempt.id,)).fetchall()
        confirmed = [r for r in rows if r[0]]
        if not confirmed:
            raise GuardError(
                f"post-dispatch: no confirmed native session is registered for "
                f"attempt {attempt.id} in {self.db}; a dispatch that never bound a "
                "session cannot be attributed to a worker")
        gate_rec = recorded.get("pane_gate") if isinstance(
            recorded.get("pane_gate"), dict) else {}
        bound = str(gate_rec.get("session_id") or "")
        if bound and str(confirmed[-1][0]) != bound:
            raise GuardError(
                f"post-dispatch: dispatched session {bound!r} is not the session "
                f"registered in the database ({str(confirmed[-1][0])!r})")
        proof = {
            "state": state,
            "transport_observation": str(recorded["transport_observation"]),
            "prompt_sha256": str(recorded["prompt_sha256"]),
            "pane_id": str(gate_rec.get("pane_id") or recorded.get("pane_id") or ""),
            "bound_native_session": str(confirmed[-1][0]),
            "prompt_binding_complete": not carries,
            "identity_fields_match": True,
            "topology_unchanged": True,
        }
        # Provenance is carried FORWARD, not re-earned: the change was produced
        # under the earlier dispatch that this method just re-proved.  The
        # manifest keeps both facts apart — evidence_class names the origin,
        # ``continuation.prompts_submitted_by_this_invocation`` stays at zero.
        self.evidence_class = "live-agent"
        self.model_proof = pp
        ev.post("post-dispatch", "dispatch proof is complete and unambiguous",
                expected=True,
                observed=all((state == "submitted", not carries,
                            bool(confirmed), bound == str(confirmed[-1][0]))))
        return proof

    def _adopt_final_report(self, ev: Evidence, task: Any, attempt: Any,
                           brief: Any) -> dict[str, Any]:
        """Bind the externally produced final WorkerReport to this attempt.

        The report is the only thing that may finish the attempt here; the runner
        never authors a worker's claims in this mode (``--scripted-worker-change``
        is refused by the argument rules).
        """
        if not self.args.worker_report_file:
            raise GuardError(
                "post-dispatch: --worker-report-file is required — continuing a "
                "dispatched attempt without the worker's own durable final report "
                "would deliver on nothing")
        path = Path(self.args.worker_report_file).expanduser()
        if not path.is_file():
            raise GuardError(f"post-dispatch: --worker-report-file {path} does not"
                            " exist")
        parsed = worker_reports.parse_worker_report(
            path.read_text(encoding="utf-8"))
        drift = [f"{label}: report {got!r} != durable {want!r}"
                for label, want, got in (
                    ("task id", task.id, parsed["task_id"]),
                    ("attempt id", attempt.id, parsed["attempt_id"]),
                    ("task version", brief.task_version, parsed["task_version"]),
                    ("brief digest", brief.brief_digest, parsed["brief_digest"]),
                ) if want != got]
        if drift:
            raise GuardError("post-dispatch: WorkerReport does not bind this"
                            " attempt (" + "; ".join(drift) + ")")
        if parsed["report_kind"] != "final":
            raise GuardError(
                f"post-dispatch: report_kind is {parsed['report_kind']!r}; only a"
                " FINAL report can close an execution for delivery")
        if parsed.get("outcome") not in ("completed", "blocked", "failed"):
            raise GuardError(
                "post-dispatch: a final report must state an outcome of"
                " completed|blocked|failed")

        digest = str(parsed["report_digest"])
        before = {str(r[0]) for r in self.conn.execute(
            "SELECT report_digest FROM worker_reports WHERE attempt_id=?",
            (attempt.id,)).fetchall()}
        already = digest in before
        if already:
            # The store already holds this exact report, so an earlier pass (this
            # same attempt) was continued to completion.  Re-submitting would trip
            # the domain's staleness guard — the Task version moved once worker-done
            # was projected — so the idempotent action is to recognise the existing
            # row, NOT replay the write.  Binding was proved against the immutable
            # brief above, and the prior row is itself proof of acceptance.
            progress = None
        else:
            progress = self.ingest_worker_report()
        after = {str(r[0]) for r in self.conn.execute(
            "SELECT report_digest FROM worker_reports WHERE attempt_id=?",
            (attempt.id,)).fetchall()}
        fresh = sorted(after - before)
        attempt_now = domain.get_attempt(self.conn, attempt.id)
        if attempt_now.status != "finished":
            raise GuardError(
                f"post-dispatch: attempt is {attempt_now.status!r} after the final"
                " report — the domain must finish the execution before delivery")
        if parsed["outcome"] != "completed":
            self.worker_done = {
                "report_digest": digest,
                "outcome": parsed["outcome"],
                "already_recorded_before_this_run": already,
                "newly_recorded_by_this_run": bool(fresh),
            }
            raise GuardError(
                f"post-dispatch: final WorkerReport outcome is "
                f"{parsed['outcome']!r}; report retained and delivery refused")

        # The Task reaches ``verifying`` only through the domain's own event,
        # which refuses unless the current Attempt is genuinely finished: the
        # projection follows the evidence, it never replaces it.
        status = self._project_worker_done_on_task(task)
        projection = self.worker_done_projection
        task_now = domain.get_task(self.conn, task.id)
        if status != "verifying":
            raise GuardError(
                f"post-dispatch: task is {status!r}, not 'verifying'; "
                "delivery would be unverifiable")

        self.worker_done = {
            "file": str(path),
            "file_sha256": sha256_file(path),
            "report_digest": digest,
            "outcome": str(parsed["outcome"]),
            "already_recorded_before_this_run": already,
            "newly_recorded_by_this_run": bool(fresh),
            "rows_for_attempt": len(after),
            "task_projection": projection,
            "ack_source": None,
        }
        ev.post("post-dispatch", "exactly one final WorkerReport row for the"
                                " attempt", expected=1,
                observed=int(self.conn.execute(
                    "SELECT COUNT(*) FROM worker_reports WHERE attempt_id=?"
                    " AND report_kind='final'", (attempt.id,)).fetchone()[0]))
        ev.post("post-dispatch", "the report bound this attempt exactly",
                expected=[attempt.id, brief.brief_digest],
                observed=[parsed["attempt_id"], parsed["brief_digest"]])
        ev.post("post-dispatch", "the task reached verifying from the report,"
                                " not from an assertion",
                expected="verifying", observed=task_now.status)
        return self.worker_done


    def _load_project(self, project_id: str) -> Any:
        project = domain.get_project(self.conn, project_id)
        if real(str(project.repo_path)) != real(self.project):
            raise GuardError(
                f"resume: project {project_id} points at {project.repo_path!r}, "
                f"not this root's project {self.project}; wrong database or a "
                f"moved root")
        return project

    def verify_durable_binding(self, handoff: dict[str, Any]) -> dict[str, Any]:
        """Prove phase 1's probes are still durable, without re-probing."""
        durable = self._durable_probe_digests(self.conn, self.project_row.id)
        want = handoff.get("capability_digests") or {}
        binding: dict[str, Any] = {"attempt_id": str(handoff["attempt_id"]),
                                   "model": self.model,
                                   "revalidated_on_resume": True}
        for label in ("provider", "harness"):
            digest = str(want.get(label) or "")
            if digest not in durable:
                raise GuardError(
                    f"resume: the {label} probe digest recorded in phase 1 "
                    f"({digest[:16]}…) is no longer registered as an event; the "
                    f"capability binding is unverifiable")
            binding[f"{label}_digest"] = digest
        binding["durable_before_bind"] = True
        ev = self._need_evidence()
        ev.write("capability-binding-resume.json", binding)
        return binding

    # -- PG-7: canonical CEO identity, one-shot challenge, fenced prompt,
    #         honest assumed_default acceptance
    #
    # Everything below runs against the run-local database only, and reaches a
    # pane exclusively through the same gated submit path as the worker leg.
    # -----------------------------------------------------------------------

    def _ceo_record(self) -> Any:
        return ceo_initialization.get_initialization(self.conn,
                                                    self.project_row.id)

    def gate_ceo_target(self) -> dict[str, Any]:
        """Prove the designated CEO pane is free, stable, and inside this world.

        The domain's own CEO bindings deliberately do not inspect a pane cwd, so
        the runner adds the check: a prompt that establishes project-wide CEO
        authority must not land in an arbitrary directory.  The canonical CEO
        location is the disposable project root; the allocated worktree is also
        accepted, but only when the CEO is the very pane the worker leg targets.
        """
        pane = self.args.ceo_pane
        if not pane:
            raise GuardError("--ceo-pane is required for the PG-7 CEO leg")
        ceo_agent = getattr(self.args, "ceo_agent", None) or self.args.herdr_agent
        first = self.herdr.identity(pane, expected_agent=ceo_agent)
        second = self.herdr.identity(pane, expected_agent=ceo_agent)
        ev = self._need_evidence()
        ev.write("ceo-pane-identity.json", {"first": first, "second": second})
        if not first.get("session_id") or first["session_id"] != second.get(
                "session_id"):
            raise GuardError(
                f"CEO pane {pane} session identity is absent or unstable "
                f"({first.get('session_id')!r} vs {second.get('session_id')!r}); "
                f"an initialization could not be attributed to a session later")
        expect = self.args.ceo_expect_session_id or (
            self.args.expect_session_id
            if pane == self.args.herdr_pane else None)
        if not expect:
            raise GuardError(
                f"CEO pane {pane} has no operator-named session to match; pass "
                f"--ceo-expect-session-id")
        if first["session_id"] != expect:
            raise GuardError(
                f"CEO pane session {first['session_id']!r} != the designated "
                f"{expect!r}; refusing to initialize a CEO the operator did not "
                f"name")
        if first.get("agent_status") not in SETTLED_STATUSES:
            raise GuardError(
                f"CEO pane {pane} is {first.get('agent_status')!r}, not settled "
                f"(idle/done); a bootstrap prompt would interleave with the "
                f"CEO's current work")
        same_pane = pane == self.args.herdr_pane
        allowed = [real(str(self.project))]
        if same_pane and self.allocation is not None:
            allowed.append(real(self.allocation.worktree_path))
        cwd = real(first.get("cwd") or "")
        if cwd not in allowed:
            raise GuardError(
                f"CEO pane {pane} cwd is {cwd or '(unknown)'!r}, outside this "
                f"run's disposable world; the CEO prompt must stand in "
                f"{allowed[0]!r}"
                + (f" or {allowed[1]!r}" if len(allowed) > 1 else ""))
        return {**first, "expected_session_id": expect,
                "same_pane_as_worker": same_pane,
                "cwd_kind": ("project_root" if cwd == allowed[0]
                            else "allocated_worktree"),
                "was_idle": True, "session_stable": True}

    def _ceo_team_state(self, gate: dict[str, Any]) -> dict[str, Any]:
        """Authoritative runtime state for the canonical CEO member.

        Built from this run's facts plus the just-gated live pane identity, then
        written through :func:`teamcraft.runtime_adapter.sync_state` — the only
        sanctioned writer, which enforces team-identity immutability, one CEO per
        project, and legal Task/Attempt/Session lineage.  No row is hand-rolled.
        """
        name = self.args.ceo_name
        native = str(gate["session_id"])
        herdr_session = {
            "workspace_id": str(gate.get("workspace_id") or ""),
            "tab_id": str(gate.get("tab_id") or ""),
            "pane_id": str(gate["pane_id"]),
            "terminal_id": str(gate.get("terminal_id") or ""),
            "kind": str(gate.get("session_kind") or "id"),
            "source": str(gate.get("session_source") or "herdr:opencode"),
            "value": native,
        }
        cwd = (real(str(self.project)) if gate["cwd_kind"] == "project_root"
               else real(str(gate["cwd"])))
        member = {
            "name": name, "role": "ceo",
            "responsibility": ("coordinate this disposable project; answer the "
                              "initialization challenge; own no source path"),
            "kind": str(gate.get("agent") or "opencode"),
            "task": ("PG-7 disposable: establish the canonical CEO identity for "
                    "this project"),
            "ownership": [], "read_only": False, "cwd": cwd,
            "from_pane_id": None, "pane_id": str(gate["pane_id"]),
            "terminal_id": str(gate.get("terminal_id") or ""),
            "agent_session": {"kind": herdr_session["kind"],
                             "source": herdr_session["source"],
                             "value": native},
            "agent_args": [], "direction": None,
            "start_timeout_ms": 30_000,
            # ``pane_created`` is the earliest lineage phase that creates the
            # Attempt and registers the Session: it asserts that a pane exists and
            # is occupied, which is exactly what was observed — nothing more.
            "phase": "pane_created", "prompt_may_have_been_delivered": False,
            "live_state": "idle", "error": None,
            "task_id": None, "attempt_id": None, "session_id": None,
            "created_at": int(time.time()),
        }
        return {
            "team_id": f"m606-{self.run_id[:12]}",
            "project_root": str(self.project),
            "created_at": int(time.time()),
            "herdr_session": herdr_session,
            "members": {name: member},
        }

    def ceo_bootstrap_leg(self) -> dict[str, Any]:
        """One PG-7 bootstrap: identity -> challenge -> fence -> prompt -> accept.

        Honest by construction:

        * acceptance records ``assumed_default`` — the domain's explicit temporary
          default — and never ``host_verified``; the column is immutable after it
          is written, so this cannot be quietly upgraded later;
        * the nonce plaintext is used to build the prompt and is never persisted
          (only its digest, which is what the database itself stores);
        * the challenge is fenced to ``unknown`` BEFORE the prompt, so a crash or
          a lost submit leaves a durable, non-replayable fact rather than an
          invitation to resend;
        * an agent self-claim is retained as unverified audit material that can
          neither initialize the project nor upgrade the receipt.
        """
        ev = self._need_evidence()
        name = self.args.ceo_name
        pid = self.project_row.id
        lease = self.lease()
        gate = self.gate_ceo_target()

        runtime_adapter.sync_state(self.conn, self._ceo_team_state(gate))
        ceos = self.conn.execute(
            "SELECT name, role, kind, task_id, attempt_id, session_id,"
            " terminal_id FROM members WHERE role='ceo'").fetchall()
        ev.post("ceo", "exactly one canonical CEO member is registered",
                expected=1, observed=len(ceos))
        ceo = ceos[0]
        ev.post("ceo", "CEO harness kind supports the protocol discovery contract",
                expected=True,
                observed=str(ceo[2]) in ceo_initialization.SUPPORTED_CEO_HARNESSES,
                detail=f"observed harness kind {ceo[2]!r}")
        ev.post("ceo", "CEO binds a TeamCraft Session, terminal and Attempt",
                expected=True,
                observed=all(v is not None for v in (ceo[4], ceo[5], ceo[6])))
        attempt = domain.get_attempt(self.conn, str(ceo[4]))
        ev.post("ceo", "CEO attempt carries the immutable protocol pin",
                expected=[self.protocol["protocol_version"],
                         self.protocol["sha256"]],
                observed=[attempt.protocol_version, attempt.protocol_hash])
        meta = self.conn.execute(
            "SELECT team_id, project_root FROM runtime_meta WHERE id=1").fetchone()
        ev.post("ceo", "team identity points at this run's disposable project",
                expected=str(self.project), observed=str(meta[1]))

        existing = self._ceo_record()
        if existing is not None and existing.state == "initialized":
            # An initialized CEO is never re-bootstrapped, and a challenge is
            # never re-issued for it: report and move on without any prompt.
            self.ceo_stage = "already-initialized"
            report = {"stage": "ceo", "action": "not-replayed",
                      "state": existing.state,
                      "receipt_source": existing.receipt_source,
                      "identity_mode": existing.identity_mode,
                      "ceo_acknowledged": False,
                      "note": ("the project already has an initialized CEO; this "
                              "runner does not re-bootstrap or re-send")}
            ev.write("ceo-bootstrap.json", report)
            self.ceo_report = report
            return report

        record, nonce = ceo_initialization.discover_and_issue(self.conn, pid,
                                                             name)
        if nonce is None:
            raise GuardError(
                f"CEO initialization is {record.state}; no fresh challenge was "
                "issued, and one may never be re-sent for a consumed nonce")
        fields = ceo_initialization.bootstrap_prompt_fields(record, nonce)
        stored = self.conn.execute(
            "SELECT nonce_hash, nonce_expires_at FROM ceo_initializations"
            " WHERE project_id=?", (pid,)).fetchone()
        self._ephemeral.append(nonce)
        ev.write("ceo-challenge.json", {
            "ceo_name": name, "state": record.state, "team_id": record.team_id,
            "teamcraft_session_id": record.teamcraft_session_id,
            "terminal_id": record.terminal_id,
            "native_session_id": record.native_session_id,
            "protocol_version": record.protocol_version,
            "protocol_hash": record.protocol_hash,
            "runtime_skill_hash": record.runtime_skill_hash,
            "snapshot_digest": record.snapshot_digest,
            "nonce_sha256": str(stored[0]), "nonce_chars": len(nonce),
            "nonce_expires_at": str(stored[1]),
            "prompt_field_names": sorted(fields),
            "note": ("only the challenge digest is recorded; the plaintext exists "
                    "solely in the prompt bytes handed to the transport"),
        })
        ev.post("ceo", "recorded challenge digest equals the stored hash",
                expected=str(stored[0]),
                observed=sha256_bytes(nonce.encode("utf-8")))
        ev.post("ceo", "the challenge is not expired at issue time",
                expected=True,
                observed=str(stored[1]) > now_iso(), detail=str(stored[1]))

        # Fence first: the durable pre-I/O boundary makes a lost submit provably
        # recoverable instead of resendable.
        runtime_adapter.admit_ceo_bootstrap_effect(self.conn, pid, nonce,
                                                  lease=lease)
        fenced = self._ceo_record()
        ev.post("ceo", "challenge fenced to 'unknown' BEFORE any prompt",
                expected="unknown", observed=fenced.state)

        prompt = "\n".join([
            f"TeamCraft CEO initialization challenge ({name}) for project "
            f"{fields['project_id']}.",
            "Echo these binding values back verbatim as your first reply; do not "
            "run any command, edit any file, or contact any network endpoint.",
            *[f"{key}: {fields[key]}" for key in sorted(fields)],
            f"Model in use for this disposable project (loopback only): "
            f"{self.model}",
        ])
        submit = self._submit_to_pane(
            gate["pane_id"], prompt, wait=self.args.submit_wait,
            until=tuple(self.args.submit_until),
            timeout_ms=self.args.submit_timeout_ms,
            expected_agent=getattr(self.args, "ceo_agent", None) or self.args.herdr_agent)
        observation = submit["transport_observation"]
        transport = ("submitted" if submit["returncode"] == 0
                     and observation == "accepted" else "unknown")
        submit["state"] = transport
        after = self.herdr.identity(
            gate["pane_id"],
            expected_agent=getattr(self.args, "ceo_agent", None)
            or self.args.herdr_agent)
        same_occupant = after.get("session_id") == gate["session_id"]
        ev.write("ceo-submit.json", {
            **submit, "pane_gate": gate, "post_submit_identity": after,
            "occupant_rechecked": True, "occupant_same": same_occupant,
        })

        status = str(after.get("agent_status") or "")
        session_state = {"idle": "idle", "done": "idle",
                         "working": "working"}.get(status)
        if transport != "submitted" or not same_occupant or session_state is None:
            # The challenge stays fenced at 'unknown'.  Nothing is assumed, no
            # acceptance is recorded, and no further prompt may be sent here.
            self.ceo_stage = "refused"
            report = {"stage": "ceo", "action": "halted", "transport": transport,
                      "occupant_same": same_occupant,
                      "post_submit_status": status or "(unknown)",
                      "ceo_state": fenced.state,
                      "receipt_source": "unverified",
                      "ceo_initialized": False, "ceo_acknowledged": False,
                      "duplicate_sends": 0,
                      "note": ("the fenced challenge stays 'unknown' for "
                              "reconciliation; assumed_default requires a "
                              "confirmed submit and the same live occupant, and "
                              "the nonce is never re-sent")}
            ev.write("ceo-bootstrap.json", report)
            self.ceo_report = report
            raise GuardError(
                f"CEO bootstrap did not complete (transport={transport}, "
                f"same_occupant={same_occupant}, status={status!r}); halting "
                "before any further prompt — reconcile the fenced challenge "
                "instead of resending it")

        accepted = runtime_adapter.record_ceo_bootstrap_submission_assumed(
            self.conn, pid, nonce, native_session_id=str(after["session_id"]),
            agent_session={"kind": str(after.get("session_kind") or "id"),
                          "source": str(after.get("session_source")
                                       or "herdr:opencode"),
                          "value": str(after["session_id"])},
            session_state=session_state, lease=lease)
        ev.post("ceo", "CEO initialized under the explicit default assumption",
                expected="initialized", observed=accepted.state)
        ev.post("ceo", "provenance is assumed_default, never host_verified",
                expected="assumed_default", observed=accepted.receipt_source)
        ev.post("ceo", "identity mode is the portable 'native' mode",
                expected="native", observed=accepted.identity_mode)
        opened = ceo_initialization.require_initialized(self.conn, pid)
        ev.post("ceo", "initialization gate is now open for Scheduler/Wake",
                expected="initialized", observed=opened.state)
        ev.post("ceo", "CEO state is written to the run-local DB, never the "
                        "host's live runtime DB",
                expected=True,
                observed=(str(self.db).startswith(str(self.root))
                        and self.db != REPO_ROOT / ".teamcraft" / "teamcraft.db"),
                detail=str(self.db))

        # A consumed challenge must be inert.  This probe is safe precisely
        # because the record is initialized: the API refuses before any write.
        refused: str | None = None
        try:
            runtime_adapter.record_ceo_bootstrap_submission_assumed(
                self.conn, pid, nonce, native_session_id=str(after["session_id"]),
                session_state=session_state, lease=lease)
        except (ceo_initialization.CEOInitializationError,
                runtime_adapter.RuntimeAdapterError) as exc:
            refused = str(exc)[:160]
        ev.post("ceo", "a second acceptance of the same challenge is refused",
                expected=True, observed=refused is not None,
                detail=refused or "not refused — unexpected")

        claim: dict[str, Any] = {"supplied": False}
        if self.args.ceo_claim_file:
            path = Path(self.args.ceo_claim_file).expanduser()
            if not path.is_file():
                raise GuardError(f"--ceo-claim-file {path} does not exist")
            raw = path.read_text(encoding="utf-8")
            try:
                json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GuardError(f"CEO claim file is not valid JSON: {exc}")
            before = self._ceo_record()
            claimed = runtime_adapter.record_ceo_agent_claim(self.conn, pid, raw,
                                                            lease=lease)
            ev.post("ceo", "an agent self-claim cannot initialize the project",
                    expected=before.state, observed=claimed.state)
            ev.post("ceo", "an agent self-claim cannot upgrade the receipt",
                    expected="assumed_default", observed=claimed.receipt_source)
            claim = {"supplied": True, "file": str(path),
                     "digest": sha256_file(path),
                     "state_after": claimed.state,
                     "receipt_source_after": claimed.receipt_source,
                     "retained_as": "unverified audit material",
                     "can_initialize": False}

        leaked = [n for n in sorted(ev.files)
                  if nonce in (ev.dir / n).read_text(encoding="utf-8")]
        ev.post("ceo", "the nonce plaintext was not persisted in any evidence",
                expected=[], observed=leaked)

        report = {
            "stage": "ceo", "action": "bootstrapped", "ceo_name": name,
            "pane": gate["pane_id"],
            "same_pane_as_worker": gate["same_pane_as_worker"],
            "cwd_kind": gate["cwd_kind"],
            "challenge": {"nonce_sha256": str(stored[0]),
                         "nonce_chars": len(nonce),
                         "nonce_expires_at": str(stored[1]),
                         "plaintext_persisted": False},
            "fenced_before_submit": True, "transport": transport,
            "occupant_rechecked_same": True,
            "state": accepted.state,
            "receipt_source": accepted.receipt_source,
            "identity_mode": accepted.identity_mode,
            "gate_open_for_wake": True, "replay_refused": refused,
            "host_trusted": False, "ceo_acknowledged": False,
            "duplicate_sends": 1, "claim": claim,
            "note": ("assumed_default is the domain's explicit temporary default: "
                    "it records that the transport succeeded and the same live "
                    "occupant was rechecked, NOT that a host-trusted protocol or "
                    "skill receipt was observed"),
        }
        ev.write("ceo-bootstrap.json", report)
        self.ceo_stage = "executed"
        self.ceo_report = report
        return report

    # -- G2: live dispatch ---------------------------------------------------

    def dispatch(self) -> dict[str, Any]:
        """LIVE dispatch: only reachable as phase 2 (``--resume-handoff``).

        This refusal is deliberate, not a limitation to design away: in the same
        invocation that allocates the worktree, no operator could have placed a
        pane in that cwd, so the cwd gate could not be honestly satisfied.
        """
        if not self.resuming:
            raise GuardError(HANDOFF_REQUIRED)
        return self.resume()

    def _submit_core(self, task: Any, attempt: Any, allocation: Any,
                    brief: Any, binding: dict[str, Any],
                    ev: Evidence) -> dict[str, Any]:
        topology_before = self.herdr.topology_digest()
        # PG-7 first: the Wake admission below requires an open CEO gate, and a
        # bootstrap whose transport is unresolved halts the whole leg before the
        # worker prompt is ever considered.
        if self.args.ceo_pane:
            self.ceo_bootstrap_leg()
        # Gate FIRST, and reuse the identity the gate verified.  A separate
        # pre-gate read would shift the observation window and could mask a
        # session that flickers exactly when it matters.
        gate = self.gate_dispatch_target()
        identity_before = {k: gate[k] for k in
                          ("pane_id", "terminal_id", "agent", "agent_status",
                           "session_id", "cwd", "workspace_id", "tab_id")}
        ev.write("identity-binding.json", identity_before)
        proof = self.pane_model_proof(gate)
        if not proof.get("attested"):
            raise GuardError(
                "pane-local model is unproven"
                + (f" ({proof.get('reason')})" if proof.get("reason") else
                   " (no --pane-model-evidence supplied)")
                + f"; the loopback catalog only proves what the server serves, not"
                  f" what pane {gate['pane_id']} has selected — refusing to "
                  f"submit rather than assuming the model")

        # Now the pane is proven idle, in the allocated cwd, on the pinned model:
        # record the CONFIRMED session binding (phase 1 could only record an
        # unconfirmed placeholder, since no pane had been designated yet).
        domain.register_session(
            self.conn, attempt.id, "opencode",
            native_session_id=gate["session_id"], pane_id=gate["pane_id"],
            state="idle")

        # The prompt carries the allocation it belongs to: an agent told to edit
        # "src/feature.py" without the worktree path can act in the wrong tree.
        prompt = "\n".join([
            f"M6-06 disposable task {task.id} / attempt {attempt.id}",
            f"Allocated worktree (work ONLY here): {allocation.worktree_path}",
            f"Task branch: {allocation.branch}",
            f"Base commit: {allocation.base_sha}",
            f"Owned paths: {', '.join(allocation.claims)}",
            f"Brief digest: {brief.brief_digest}",
            f"Protocol: {self.protocol['protocol_version']} "
            f"{self.protocol['sha256']}",
            f"Model (local loopback only): {self.model}",
            "",
            brief.content,
            "",
            "Constraints: work only inside the allocated worktree above; do not "
            "push; do not touch paths outside the owned list; reply with one "
            "short summary line when done.",
        ])
        submit = self._submit_to_pane(
            self.args.herdr_pane, prompt, wait=self.args.submit_wait,
            until=tuple(self.args.submit_until),
            timeout_ms=self.args.submit_timeout_ms)
        topology_after = self.herdr.topology_digest()

        # A lost submit is UNKNOWN, never a retry: quarantine and halt this leg.
        observation = submit["transport_observation"]
        state = ("submitted" if submit["returncode"] == 0
                 and observation == "accepted" else "unknown")
        submit["state"] = state
        if self.args.ack_evidence_file:
            ack_path = Path(self.args.ack_evidence_file).expanduser()
            submit["prompt_ack"] = "acknowledged-by-operator"
            submit["ack_evidence_digest"] = sha256_file(ack_path)
            ev.write("prompt-ack.json", {
                "source": str(ack_path), "digest": sha256_file(ack_path),
                "text_head": ack_path.read_text(encoding="utf-8")[:400],
            })

        wake_report = self.wake_leg(ev, state)

        ev.write("dispatch.json", {
            **submit, "task_id": task.id, "attempt_id": attempt.id,
            "allocation_id": allocation.allocation_id,
            "worktree_path": allocation.worktree_path, "branch": allocation.branch,
            "brief_digest": brief.brief_digest, "model": self.model,
            "pane_gate": gate, "pane_model_proof": proof,
            "capability_binding": binding,
            "topology_digest_before": topology_before,
            "topology_digest_after": topology_after,
            "topology_unchanged": topology_before == topology_after,
            "wake": wake_report,
        })
        ev.post("dispatch", "pane was idle before the prompt",
                expected=True, observed=gate["was_idle"])
        ev.post("dispatch", "pane cwd is exactly the allocated worktree",
                expected=True,
                observed=real(gate["cwd"]) == real(allocation.worktree_path))
        ev.post("dispatch", "pane-local model attested as the pinned model",
                expected=self.model, observed=proof["model"])
        ev.post("dispatch", "capability probes were durable before bind",
                expected=True, observed=binding["durable_before_bind"])
        ev.post("dispatch", "herdr accepted the submission (else UNKNOWN: leg "
                            "halts, never auto-retries)",
                expected="accepted", observed=observation)
        ev.post("dispatch", "native session id stable across submit",
                expected=True, observed=submit["session_stable"])
        ev.post("dispatch", "topology unchanged by dispatch",
                expected=True, observed=(topology_before == topology_after))
        ev.post("dispatch", "prompt_ack stays unknown without operator evidence",
                expected="unknown",
                observed="unknown" if not self.args.ack_evidence_file
                else "acknowledged-by-operator")
        # Prove the prompt carried its allocation WITHOUT persisting the text:
        # the digests below are recomputed from the same string that was sent.
        prompt_binding = {
            "prompt_sha256": submit["prompt_sha256"],
            "prompt_chars": submit["prompt_chars"],
            "carries_alloc_path": allocation.worktree_path in prompt,
            "carries_branch": allocation.branch in prompt,
            "carries_base_sha": allocation.base_sha in prompt,
            "carries_brief_digest": brief.brief_digest in prompt,
            "carries_pinned_model": self.model in prompt,
            "carries_protocol": self.protocol["sha256"] in prompt,
        }
        ev.write("prompt-binding.json", prompt_binding)
        ev.post("dispatch", "prompt carried the allocated path and brief binding",
                expected=[True] * 6,
                observed=[prompt_binding["carries_alloc_path"],
                        prompt_binding["carries_branch"],
                        prompt_binding["carries_base_sha"],
                        prompt_binding["carries_brief_digest"],
                        prompt_binding["carries_pinned_model"],
                        prompt_binding["carries_protocol"]])
        self.dispatch_state = state
        self.model_proof = proof
        self.evidence_class = "live-agent"
        self.attempt, self.task, self.allocation = (
            domain.get_attempt(self.conn, attempt.id),
            domain.get_task(self.conn, task.id), allocation)
        return {"stage": self.stage, "state": state, "attempt_id": attempt.id,
                "task_id": task.id, "identity": identity_before,
                "submit": submit, "wake": wake_report,
                "pane_gate": gate, "pane_model_proof": proof,
                "capability_binding": binding,
                "prompt_binding": prompt_binding,
                "topology_digest_before": topology_before,
                "topology_digest_after": topology_after,
                "topology_unchanged": topology_before == topology_after,
                "worktree_path": allocation.worktree_path,
                "branch": allocation.branch}

    def wake_leg(self, ev: Evidence, submit_state: str) -> dict[str, Any]:
        """One ``ceo.wake``: intake -> admit -> (optionally) deliver -> record.

        Three things this refuses to do, deliberately:

        * it never borrows the *worker* prompt's transport outcome as the Wake's
          outcome — admission is a durable pre-I/O boundary, not a delivery;
        * it never calls ``acknowledge_wake``: CEO acknowledgement requires an
          explicit CEO response, and Herdr 0.9.3 reports ``prompt_ack`` and
          ``event_stream`` as unsupported, so ``ceo_acknowledged`` stays false and
          the acknowledgement cursor must stay where it was;
        * it never resends: an undelivered Wake is left in its durable state for a
          sender to reconcile.
        """
        lease = self.lease()
        try:
            ingested = wake.ingest_events(self.conn, self.project_row.id, lease)
        except ceo_initialization.CEOInitializationGateClosed as exc:
            report = {"wake_id": None, "state": "blocked:ceo-initialization",
                      "reason": str(exc)[:200], "ceo_acknowledged": False,
                      "duplicate_sends": 0,
                      "note": ("wake intake/admission requires the CEO "
                              "initialization gate to be open; the submitted "
                              "prompt is unaffected and is still recorded as "
                              "submitted/unknown")}
            ev.write("wake.json", report)
            return report
        pending = wake.list_wakes(self.conn, self.project_row.id,
                                 states=("pending",))
        cursor_after_intake = wake.get_cursor(self.conn, self.project_row.id)
        ev.post("wake", "intake advanced a durable cursor (not in-memory state)",
                expected=True, observed=cursor_after_intake is not None,
                detail=(f"scanned={ingested.scanned_events} "
                       f"created={ingested.created_wakes} "
                       f"merged={ingested.merged_events}"))
        if not pending:
            report = {"wake_id": None, "state": "no-pending-wake",
                      "ingested": plain(ingested), "ceo_acknowledged": False,
                      "note": "no eligible event produced a pending wake row"}
            ev.write("wake.json", report)
            return report
        wake_id = field(pending[0], "wake_id", "id")
        try:
            admission = wake.admit_pending_wake(self.conn, self.project_row.id,
                                              str(wake_id), lease)
        except ceo_initialization.CEOInitializationGateClosed as exc:
            report = {"wake_id": wake_id, "state": "pending:admission-blocked",
                      "reason": str(exc)[:200], "ingested": plain(ingested),
                      "worker_transport_reference": submit_state,
                      "transport": "not-attempted",
                      "outcome": None, "ceo_acknowledged": False,
                      "duplicate_sends": 0,
                      "note": ("the Wake row is durable; admitting it needs CEO "
                              "initialization (roadmap PG-7). Nothing was sent, so "
                              "no transport outcome is claimed here")}
            ev.write("wake.json", report)
            return report

        bound = getattr(admission.wake, "state", None)
        deferred = getattr(admission, "deferred_reason", None)
        cursor_before = wake.get_cursor(self.conn, self.project_row.id)
        if bound != "sending":
            # Budget/policy deferred it: no Command was bound, so there is no
            # transport to record and nothing may be reported as sent.
            report = {"wake_id": wake_id, "state": bound,
                      "deferred_reason": deferred,
                      "decision_digest": getattr(admission, "decision_digest",
                                                None),
                      "ingested": plain(ingested),
                      "transport": "not-attempted:deferred", "outcome": None,
                      "ceo_acknowledged": False, "duplicate_sends": 0,
                      "note": ("admission was denied by budget/policy; the Wake "
                              "stays pending and no outcome is recorded")}
            ev.write("wake.json", report)
            ev.post("wake", "a deferred Wake is not reported as delivered",
                    expected=False, observed=(bound == "submitted"))
            return report

        ev.post("wake", "admission bound an idempotent Command (pre-I/O)",
                expected="sending", observed=bound)

        if not self.args.wake_deliver:
            report = {"wake_id": wake_id, "state": bound,
                      "command_bound": True, "ingested": plain(ingested),
                      "transport": "admitted-not-delivered", "outcome": None,
                      "ceo_acknowledged": False, "duplicate_sends": 0,
                      "note": ("admit_pending_wake is the durable pre-I/O "
                              "boundary and does not contact Herdr; this run did "
                              "not deliver a Wake prompt (--wake-deliver not "
                              "given), so NO transport outcome is recorded and the "
                              "row stays 'sending' for a sender to reconcile — "
                              "never re-sent blindly")}
            ev.write("wake.json", report)
            stored = self.conn.execute(
                "SELECT state FROM wakes WHERE id=?", (str(wake_id),)).fetchone()
            ev.post("wake", "durable Wake state matches the reported state",
                    expected=str(stored[0]), observed=report["state"])
            self._assert_no_fake_ack(ev, report)
            return report

        # The PG-7 bootstrap prompt necessarily leaves the CEO working.  M2-09
        # defers a Wake while the CEO is busy, so wait bounded for it to settle
        # instead of aborting the whole run; if it never settles, record an
        # honest deferral and leave the durable Wake pending.
        deadline = time.monotonic() + 120.0
        while True:
            status = self.herdr.identity(
                self.args.ceo_pane,
                expected_agent=getattr(self.args, "ceo_agent", None)
                or self.args.herdr_agent,
            ).get("agent_status")
            if status in SETTLED_STATUSES:
                break
            if time.monotonic() >= deadline:
                report = {
                    "wake_id": wake_id, "state": bound, "command_bound": True,
                    "ingested": plain(ingested),
                    "transport": "deferred:ceo_working", "outcome": None,
                    "ceo_acknowledged": False, "duplicate_sends": 0,
                    "note": ("the CEO pane stayed busy past the bounded wait; the "
                             "Wake remains pending and is NOT re-sent blindly "
                             "(M2-09 defer)"),
                }
                ev.write("wake.json", report)
                self._assert_no_fake_ack(ev, report)
                return report
            time.sleep(2.0)

        # Explicit, consented delivery to the canonical CEO pane.
        ceo_gate = self.gate_ceo_target()
        top = self.herdr.topology_digest()
        prompt = "\n".join([
            f"TeamCraft Wake for {self.args.ceo_name}: "
            f"{ingested.created_wakes} new event(s) since the last scan.",
            f"Project: {self.project_row.id}",
            f"Wake id: {wake_id}",
            "Acknowledge by reviewing pending items; this notification carries no "
            "instructions to edit files, commit, or contact any network endpoint.",
        ])
        sent = self._submit_to_pane(
            ceo_gate["pane_id"], prompt,
            wait=self.args.submit_wait,
            until=tuple(self.args.submit_until),
            timeout_ms=self.args.submit_timeout_ms,
            expected_agent=getattr(self.args, "ceo_agent", None) or self.args.herdr_agent)
        delivered = (sent["returncode"] == 0
                     and sent["transport_observation"] == "accepted")
        outcome = "submitted" if delivered else "unknown"
        recorded = wake.record_submission_result(
            self.conn, self.project_row.id, str(wake_id), lease,
            outcome=outcome,
            detail=(None if delivered else
                    f"wake transport observation was "
                    f"{sent['transport_observation']!r}; effect may have happened, "
                    f"must be reconciled and must not be re-sent"),
            receipt_json=json.dumps({"transport_observation":
                                    sent["transport_observation"],
                                    "returncode": sent["returncode"]},
                                   sort_keys=True))
        report = {"wake_id": wake_id, "state": field(recorded, "state",
                                                    default=None),
                  "command_bound": True, "ingested": plain(ingested),
                  "transport": "delivered" if delivered else "unresolved",
                  "outcome": outcome,
                  "delivery": {**{k: sent[k] for k in
                                 ("prompt_sha256", "prompt_chars", "returncode",
                                  "transport_observation")},
                              "target_pane": ceo_gate["pane_id"],
                              "topology_unchanged":
                                  top == self.herdr.topology_digest()},
                  "ceo_acknowledged": False, "duplicate_sends": 1}
        ev.write("wake.json", report)
        ev.post("wake", ("a confirmed transport is recorded as submitted, "
                         "otherwise unknown (never a retry)"),
                expected=outcome,
                observed=self.conn.execute(
                    "SELECT state FROM wakes WHERE id=?",
                    (str(wake_id),)).fetchone()[0])
        self._assert_no_fake_ack(ev, report)
        return report

    def _assert_no_fake_ack(self, ev: Evidence, report: dict[str, Any]) -> None:
        """Prove no acknowledgement was fabricated: the cursor must not have moved.

        ``acknowledged_seq`` only advances through ``acknowledge_wake``, which the
        runner never calls.  If it ever moved, an ack was invented somewhere.
        """
        cursor = wake.get_cursor(self.conn, self.project_row.id)
        acked = None if cursor is None else int(getattr(cursor,
                                                       "acknowledged_seq", 0))
        ev.post("wake", "no CEO acknowledgement was fabricated "
                        "(acknowledged cursor stayed at 0)",
                expected=0, observed=acked,
                detail=f"wake state: {report.get('state')}, "
                     f"transport: {report.get('transport')}")

    # -- capability probes: durable BEFORE bind -------------------------------

    def _provider_snapshot(self) -> Any:
        return providers.probe_omlx(self.omlx_url, getter=self.omlx_getter)

    def _harness_snapshot(self) -> Any:
        return providers.probe_harness("opencode", str(self.project),
                                      runner=self.harness_runner)

    @staticmethod
    def _durable_probe_digests(conn: Any, project_id: str) -> set[str]:
        rows = conn.execute(
            "SELECT json_extract(payload_json,'$.snapshot_digest') FROM events"
            " WHERE project_id=? AND kind=?",
            (project_id, providers.CAPABILITY_PROBE_EVENT)).fetchall()
        return {str(r[0]) for r in rows}

    def capture_and_bind_capabilities(self, attempt_id: str) -> dict[str, Any]:
        """Probe → persist durably → prove durability → bind. Never bind first.

        ``bind_attempt_capabilities`` only accepts snapshots whose digests are
        already registered as probe events, so recording and binding must reuse
        the SAME objects: re-probing in between would produce different TTL
        timestamps and a different digest, and the bind would fail closed.
        """
        provider = self._provider_snapshot()
        harness = self._harness_snapshot()
        providers.record_capability_probe(self.conn, self.project_row.id, provider)
        providers.record_capability_probe(self.conn, self.project_row.id, harness)

        durable = self._durable_probe_digests(self.conn, self.project_row.id)
        for label, snap in (("harness", harness), ("provider", provider)):
            self._need_evidence().post(
                "capabilities", f"{label} probe persisted durably before bind",
                expected=True, observed=snap.snapshot_digest in durable)
        if provider.name != "omlx" or not any(
                host in provider.source for host in ("127.0.0.1", "::1")):
            raise GuardError(
                f"provider snapshot is not loopback oMLX ({provider.name!r} @ "
                f"{provider.source!r}); no cloud route may be bound")
        if provider.health != "healthy":
            raise GuardError(
                f"loopback provider is {provider.health} "
                f"({provider.reason_code}); refusing to bind an unhealthy route")
        if self.model not in provider.models:
            raise GuardError(
                f"pinned model {self.model!r} absent from the recorded provider "
                f"catalog; refusing to bind a model the route does not list")
        providers.bind_attempt_capabilities(
            self.conn, attempt_id, harness=harness, provider=provider,
            model=self.model)
        self.probes = {"provider": provider.payload(),
                       "harness": harness.payload()}
        binding = {
            "attempt_id": attempt_id,
            "harness": harness.name, "provider": provider.name,
            "model": self.model,
            "harness_digest": harness.snapshot_digest,
            "provider_digest": provider.snapshot_digest,
            "durable_before_bind": True,
            "cloud_fallback": "not-permitted",
        }
        self._need_evidence().write("capability-binding.json", binding)
        return binding

    # -- pane gating before any prompt -----------------------------------------

    def gate_dispatch_target(self) -> dict[str, Any]:
        """Prove the pane is idle, stable, and bound to THIS run's worktree.

        Prompts are never injected into a busy pane, a different directory, or a
        session whose identity flickers between reads: any failure here is
        fail-closed BEFORE submission, so nothing needs unwinding.
        """
        pane = self.args.herdr_pane
        if not pane:
            raise GuardError("--herdr-pane is required before dispatch")
        first = self.herdr.identity(pane)
        second = self.herdr.identity(pane)
        evidence = self._need_evidence()
        evidence.write("pane-identity.json", {"first": first, "second": second})
        if not first.get("session_id") or first["session_id"] != second.get(
                "session_id"):
            raise GuardError(
                f"pane {pane} session identity is absent or unstable "
                f"({first.get('session_id')!r} vs {second.get('session_id')!r}); "
                f"a dispatch could not be attributed afterwards")
        if self.args.expect_session_id and \
                first["session_id"] != self.args.expect_session_id:
            raise GuardError(
                f"pane session {first['session_id']!r} != --expect-session-id "
                f"{self.args.expect_session_id!r}; the operator designated a "
                f"different pane")
        if first.get("agent_status") not in SETTLED_STATUSES:
            raise GuardError(
                f"pane {pane} is {first.get('agent_status')!r}, not settled "
                f"(idle/done); a prompt would interleave with whatever is "
                f"already running")
        want = real(self.allocation.worktree_path)
        pane_cwd = real(first.get("cwd") or "")
        fg_cwd = real(first.get("foreground_cwd") or "") if first.get(
            "foreground_cwd") else None
        if not pane_cwd or pane_cwd != want:
            raise GuardError(
                f"pane {pane} cwd is {pane_cwd or '(unknown)'!r}, not this run's "
                f"allocated worktree {want!r}; refusing to prompt a pane that is "
                f"not standing in the disposable worktree")
        if fg_cwd not in (None, want):
            raise GuardError(
                f"pane {pane} foreground cwd {fg_cwd!r} differs from the "
                f"allocated worktree {want!r}")
        gate = {**first, "allocated_worktree": want,
                "cwd_matches_allocation": True, "session_stable": True,
                "was_idle": True}
        return gate

    def pane_model_proof(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Prove the pane itself is on the pinned local model, else fail closed.

        The loopback HTTP catalog proves what the SERVER serves, never what this
        pane has selected, so catalog presence alone is insufficient: dispatch
        requires an operator attestation whose pane/terminal/session fields match
        the live identity we just read. The result is recorded as
        ``operator-attested`` — explicitly NOT host-trusted.
        """
        evidence = self._need_evidence()
        raw_path = self.args.pane_model_evidence
        observation: dict[str, Any] = {}
        try:
            code, screen = self.herdr.read_pane(binding["pane_id"])
            text = screen or ""
            observation = {
                "screen_captured": code == 0,
                "screen_mentions_pinned_model": self.model in text,
                "model_ids_observed_on_screen": sorted(
                    set(MODEL_ID_RE.findall(text))),
                "terminal_lines": len(text.splitlines()),
                "conclusive": False,
                "note": ("terminal screen text is a non-conclusive observation; "
                        "it supplements but never replaces attestation"),
            }
            evidence.write("pane-screen-observation.json",
                          {"pane_id": binding["pane_id"], **observation},
                          untrusted=True)
        except GuardError as exc:
            evidence.write("pane-screen-observation.json", {
                "pane_id": binding["pane_id"], "screen_captured": False,
                "reason": str(exc)[:200], "conclusive": False})

        if not raw_path:
            return {"status": "missing", "attested": False,
                    "cloud_fallback": "not-permitted", **observation}
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise GuardError(
                f"--pane-model-evidence {path} does not exist; dispatch fails "
                f"closed rather than assuming the pane's model")
        try:
            attestation = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GuardError(f"pane model evidence is not valid JSON: {exc}")
        declared = str(attestation.get("model", ""))
        if declared != self.model:
            raise GuardError(
                f"pane attests model {declared!r}, not the pinned {self.model!r}; "
                f"refusing to submit (no model substitution)")
        for field_name, live_field in (("pane_id", "pane_id"),
                                      ("terminal_id", "terminal_id"),
                                      ("session_id", "session_id"),
                                      ("agent", "agent")):
            claimed = attestation.get(field_name)
            if claimed is not None and str(claimed) != str(binding.get(live_field)):
                raise GuardError(
                    f"pane model evidence claims {field_name}={claimed!r} but the "
                    f"live pane is {binding.get(live_field)!r}; the attestation "
                    f"does not belong to this pane")
        provenance = str(attestation.get("provenance") or "")
        if not provenance:
            raise GuardError(
                "pane model evidence lacks 'provenance'; an unattributed claim is "
                "not evidence")
        proof = {
            "status": "operator-attested",
            "attested": True,
            "model": declared,
            "provenance": provenance,
            "evidence_file": str(path),
            "evidence_sha256": sha256_file(path),
            "identity_fields_matched": [f for f, k in
                                       (("pane_id", "pane_id"),
                                        ("terminal_id", "terminal_id"),
                                        ("session_id", "session_id"),
                                        ("agent", "agent"))
                                       if attestation.get(f) is not None],
            "host_trusted": False,
            **observation,
        }
        evidence.write("pane-model-proof.json", proof)
        return proof

    # -- deliver ------------------------------------------------------------

    def approve_profiles(self) -> None:
        """Approve local ``lint``/``test`` profiles that read REPO FACTS.

        The verdict therefore comes from the repository, not from the worker's
        self-report: a WorkerReport claiming more than the tree proves loses.
        """
        lint = (
            "import pathlib,sys;"
            "p=pathlib.Path('src/feature.py');"
            f"needle={MARKER!r};"
            "ok=p.is_file() and needle in p.read_text(encoding='utf-8');"
            "print('lint: src/feature.py ' + ('ok' if ok else 'MISSING/MARKER-ABSENT'));"
            "sys.exit(0 if ok else 1)")
        test = (
            "import pathlib,sys;"
            "d=pathlib.Path('docs/guide.md');"
            "ok=d.is_file() and d.read_text(encoding='utf-8')=='guide\\n';"
            "print('test: docs/guide.md ' + ('unchanged' if ok else 'MOVED'));"
            "sys.exit(0 if ok else 1)")
        for profile, script, key in (("lint", lint, "lint"), ("test", test, "test")):
            verification.configure_verifier_profile(
                self.conn, self.project_row.id, profile=profile,
                argv=[sys.executable, "-c", script], timeout_seconds=30,
                actor="human-reviewer", idempotency_key=f"{self.run_id}-{key}",
                lease=self.lease())

    def deliver(self) -> dict[str, Any]:
        self.stage = "deliver"
        ev = self._need_evidence()
        if self.attempt is None or self.allocation is None:
            raise GuardError(
                "deliver: no attempt/allocation in this invocation — run allocate "
                "(phase 1) and, for a live leg, resume (phase 2) first; delivery "
                "never invents a workspace")
        attempt_id = self.attempt.id

        # Delivery needs the worker's own durable final report; nothing here
        # fabricates one on the worker's behalf.
        if self.worker_report is None:
            self.ingest_worker_report()
        if self.worker_report is None:
            raise GuardError(
                f"no durable final WorkerReport for {attempt_id}: delivery would "
                f"be unverifiable. Wait for the agent to report, or pass "
                f"--worker-report-file <json>; this runner never authors a "
                f"worker's claims except under --scripted-worker-change "
                f"(which is labelled scripted)")
        attempt_now = domain.get_attempt(self.conn, attempt_id)
        if attempt_now.status != "finished":
            raise GuardError(
                f"attempt {attempt_id} is {attempt_now.status!r}, not finished; "
                f"verification and delivery require a finished attempt")
        # The Task reaches ``verifying`` only through the domain's own event, and
        # that event needs the finished Attempt above: a durable final WorkerReport
        # is what authorises verification, in either phase-2 path.
        task_status = self._project_worker_done_on_task(self.task)
        if task_status != "verifying":
            raise GuardError(
                f"deliver: task is {task_status!r}, not 'verifying'; delivery"
                f" would be unverifiable (worker-done projection:"
                f" {self.worker_done_projection})")

        # Verifiers first: ship evidence is only admissible on a local pass.
        self.approve_profiles()
        suite = verification.run_worktree_suite(
            self.conn, attempt_id, idempotency_key=f"suite-{attempt_id}",
            lease=self.lease(), profiles=["lint", "test"])
        ev.write("verification.json", {
            "verdict": suite["verdict"],
            "profiles": {name: state["status"]
                        for name, state in suite["profiles"].items()},
        })
        if suite["verdict"] != "pass":
            raise PostconditionError(
                f"local verifier verdict is {suite['verdict']!r}; delivery is "
                f"refused because the repository does not prove the change")
        gate = workspaces.run_diff_gate(self.allocation.worktree_path,
                                        list(self.allocation.claims),
                                        run_git=self.git_audit)
        if not gate.ok:
            ev.write("diff-gate.json", {"ok": False,
                                       "violations": list(gate.violations)})
            raise PostconditionError(
                f"Diff Gate refused: {list(gate.violations)[:6]} — the refusal "
                f"stands; delivery does not proceed")
        ev.write("diff-gate.json", {
            "ok": True, "base_sha": gate.base_sha, "branch": gate.branch,
            "paths": [c.path for c in gate.changes],
        })
        commit = delivery.create_local_commit(
            self.conn, attempt_id, idempotency_key=f"commit-{attempt_id}",
            lease=self.lease(), run_git=self.git_audit)
        ship = delivery.record_ship_evidence(
            self.conn, attempt_id, idempotency_key=f"ship-{attempt_id}",
            lease=self.lease(), run_git=self.git_audit)
        git_audit = self.audit_git("deliver")

        # Default posture: SUBMITTED, not accepted. Acceptance requires a
        # separate, attributed acknowledgement.
        disposition = DELIVERY_SUBMITTED
        accepted = False
        ack_source: str | None = None
        if self.args.human_accept:
            if not (self.args.accept_actor and self.args.accept_reason):
                raise GuardError(
                    "--human-accept requires --accept-actor and --accept-reason; "
                    "acceptance is a separate, attributed acknowledgement")
            approval = delivery.approve_local_delivery(
                self.conn, ship.id, decision="accept",
                commit_sha=commit.commit_sha, branch=commit.branch,
                actor=self.args.accept_actor, reason=self.args.accept_reason,
                idempotency_key=f"accept-{attempt_id}",
                lease=self.lease(), run_git=self.git_audit)
            accepted = approval.task_status_after == "done"
            disposition = DELIVERY_ACCEPTED if accepted else DELIVERY_UNKNOWN
            ack_source = f"human:{self.args.accept_actor}"
        ev.write("delivery.json", {
            "commit_sha": commit.commit_sha, "base_sha": commit.base_sha,
            "parent_sha": commit.parent_sha, "branch": commit.branch,
            "file_count": commit.file_count,
            "diff_digest": commit.diff_digest, "diff_bytes": ship.diff_bytes,
            "diff_relpath": ship.diff_relpath,
            "disposition": disposition, "human_accepted": accepted,
            "ack_source": ack_source, "git_audit": git_audit,
        })
        # Postconditions re-read from git and SQLite, not from intent.
        head = git(Path(self.allocation.worktree_path), "rev-parse", "HEAD")[1]
        ev.post("deliver", "commit sha is the branch head",
                expected=commit.commit_sha, observed=head)
        ev.post("deliver", "exactly one commit above the verified base",
                expected="1", observed=git(self.project, "rev-list", "--count",
                                          f"{commit.base_sha}..{commit.commit_sha}")[1])
        refs = self.origin_refs()
        ev.post("deliver", "origin gained no ref (no push)",
                expected=["refs/heads/main"], observed=sorted(refs))
        task_after = domain.get_task(self.conn, self.task.id)
        expected_status = "done" if accepted else "verifying"
        ev.post("deliver", "task status reflects acknowledgement level",
                expected=expected_status, observed=task_after.status)
        self.disposition = disposition
        return {"stage": self.stage, "disposition": disposition,
                "commit_sha": commit.commit_sha, "refs": refs}

    def origin_refs(self) -> dict[str, str]:
        code, out = git(self.root, "ls-remote", str(self.origin), check=False)
        refs: dict[str, str] = {}
        for line in out.splitlines():
            sha, sep, ref = line.partition("\t")
            if sep and ref.startswith("refs/heads/"):
                refs[ref] = sha
        return refs

    # -- cleanup ------------------------------------------------------------

    def cleanup(self) -> dict[str, Any]:
        """Reclaim the attempt workspace ONLY if the domain grants eligibility.

        ``evaluate_cleanup_eligibility`` requires a terminal attempt, a CLEANABLE
        workspace and a ``done`` task.  A merely *submitted* delivery is not
        accepted work, so its workspace is PRESERVED — that is the safe outcome,
        not a failure, and this stage reports it as such instead of forcing
        removal.  Root removal is then skipped too, because the preserved
        workspace lives inside it and stays available for inspection; an operator
        can still reclaim the whole root explicitly via ``--cleanup-only``.
        """
        self.stage = "cleanup"
        ev = self._need_evidence()
        attempt_id = self.attempt.id
        worktrees.mark_worktree_cleanable(self.conn, attempt_id,
                                         run_git=self.git_audit)
        verdict = delivery.evaluate_cleanup_eligibility(
            self.conn, attempt_id, run_git=self.git_audit)
        audit = self.audit_git("cleanup")

        if not verdict.eligible:
            report = {
                "stage": self.stage, "action": "preserved",
                "eligible": False, "state": verdict.state,
                "reasons": list(verdict.reasons), "git_audit": audit,
                "worktree_path": self.allocation.worktree_path,
                "root_retained": True,
                "note": ("workspace preserved: cleanup eligibility requires a "
                       "terminal attempt + CLEANABLE workspace + done task, and "
                       "delivery here is only "
                       f"{getattr(self, 'disposition', DELIVERY_UNKNOWN)}; "
                       "reclaim explicitly with --cleanup-only once reviewed"),
            }
            ev.write("worktree-cleanup.json", report)
            ev.post("cleanup", "ineligible workspace preserved, not deleted",
                    expected=True,
                    observed=Path(self.allocation.worktree_path).exists())
            self.cleanup_preserved = True
            self.cleanup_report = report
            return report

        result = delivery.cleanup_attempt_worktree(
            self.conn, attempt_id, lease=self.lease(),
            run_git=self.git_audit)
        ev.write("worktree-cleanup.json", {
            "stage": self.stage, "action": "removed", "eligible": True,
            "state": verdict.state, "disposition": result["disposition"],
            "cleaned": result["cleaned"], "git_audit": audit,
        })
        ev.post("cleanup", "worktree path removed",
                expected=False,
                observed=Path(self.allocation.worktree_path).exists())
        self.ledger.untrack_path(Path(self.allocation.worktree_path))
        # Refs survive the workspace; the local origin keeps the delivery.
        refs = self.origin_refs()
        ev.post("cleanup", "delivered branch still resolvable locally",
                expected=result["cleaned"]["commit_sha"],
                observed=git(self.project, "rev-parse", "--verify",
                            f"refs/heads/{result['cleaned']['branch']}")[1])
        self.cleanup_preserved = False
        self.cleanup_report = {"stage": self.stage, "action": "removed",
                               "disposition": result["disposition"], "refs": refs}
        return self.cleanup_report

    # -- close --------------------------------------------------------------

    def finalise(self) -> dict[str, Any]:
        ev = self.evidence
        if ev is None:
            return {"stage": "close", "note": "nothing provisioned; no evidence"}
        disposition = getattr(self, "disposition", DELIVERY_UNKNOWN)
        # Stage completion by declared ORDER, not alphabetical string compare.
        reached = STAGES.index(self.stage) if self.stage in STAGES else -1
        self.manifest["stage_matrix"].update(
            {s: "executed" for i, s in enumerate(STAGES)
             if i <= reached and s != "ceo"})
        # The CEO leg is not implied by reaching dispatch: it runs only when the
        # operator designated a CEO pane, and its own outcome is reported.
        self.manifest["stage_matrix"]["ceo"] = self.ceo_stage
        self.manifest["ceo"] = self.ceo_report or {
            "stage": "ceo", "action": self.ceo_stage,
            "why": ("no --ceo-pane designated; PG-7 not attempted, so Scheduler "
                   "and Wake admission remain gated" if self.ceo_stage ==
                   "not-requested" else self.ceo_stage)}
        self.manifest["stage_reached"] = self.stage
        self.manifest["delivery"] = {
            "disposition": disposition,
            "human_accepted": disposition == DELIVERY_ACCEPTED,
            "ack_source": ("human" if disposition == DELIVERY_ACCEPTED else None),
        }
        self.manifest["evidence_class"] = self.evidence_class
        # A continuation changes what was DONE, never what was SENT: keep the two
        # facts visibly separate so no reader can mistake delivery for dispatch.
        self.manifest["continuation"] = self.continuation_report or {
            "mode": ("post-dispatch" if self.continuing else "none"),
            "prompts_submitted_by_this_invocation": self._submit_count,
            "dispatch_replayed": self._submit_count > 0,
        }
        self.manifest["worker_done_projection"] = self.worker_done_projection
        if self.continuing:
            self.manifest["evidence_origin"] = (
                "prior-dispatch-evidence (this invocation submitted no prompt)")
        self.manifest["pane_model_proof"] = {
            "status": self.model_proof.get("status", "not-attempted"),
            "attested": bool(self.model_proof.get("attested")),
            "host_trusted": False,
            "model": self.model_proof.get("model"),
        }
        wake_claim = "not-attempted"
        if (ev.dir / "wake.json").is_file():
            try:
                wake_claim = str(json.loads(
                    (ev.dir / "wake.json").read_text(encoding="utf-8")
                ).get("transport") or "not-attempted")
            except json.JSONDecodeError:
                wake_claim = "unreadable"
        ceo_receipt = {
            "executed": "assumed_default (transport succeeded; NOT host-verified)",
            "already-initialized": "pre-existing record, not re-assumed",
            "refused": "unverified — challenge left fenced at 'unknown'",
        }.get(self.ceo_stage, "not-attempted (no --ceo-pane)")
        self.manifest["non_claims"] = {
            "prompt_ack": "unknown" if not self.args.ack_evidence_file
            else "acknowledged-by-operator",
            "event_stream": "unknown",
            "host_trusted_receipt": False,
            "push": False, "claude": "pending:PG-1",
            "cloud": "not-permitted",
            "real_agent_ack": "unknown",
            "ceo_receipt": ceo_receipt,
            "ceo_acknowledgement": "not-asserted (never inferred from transport)",
            "wake_transport": wake_claim,
            "pg7": ("bootstrap+wake-executed" if self.ceo_stage == "executed"
                   else f"open:{self.ceo_stage}"),
            # A continuation is defined by what it did NOT do; state both facts
            # so no reader mistakes verification/delivery for a fresh dispatch.
            "dispatch_replay": (
                "none — a continuation submits no prompt" if self.continuing
                else "one-dispatch-per-attempt-by-construction"),
            "worker_completion": ("sourced from the external final WorkerReport"
                                 if self.worker_report is not None or self.worker_done
                                 else "no worker report on record"),
        }
        # One-use secrets must never reach disk.  Tokens are named only by digest
        # prefix in the report, so the sweep itself cannot leak them.
        ephemeral: dict[str, Any] = {}
        for token in self._ephemeral:
            hits = [name for name in sorted(ev.files)
                   if token in (ev.dir / name).read_text(encoding="utf-8")]
            if hits:
                ephemeral[sha256_bytes(token.encode("utf-8"))[:12] + "…"] = hits
        ev.post("close", "no one-use secret (the CEO nonce) reached disk",
                expected={}, observed=ephemeral)
        self.manifest["ephemeral_leak_scan"] = {
            "tokens_checked": len(self._ephemeral),
            "tokens": [sha256_bytes(t.encode("utf-8"))[:12] + "…"
                       for t in self._ephemeral],
            "leaks": ephemeral,
        }
        self.manifest["cleanup"] = getattr(self, "cleanup_report",
                                          {"action": "not-attempted"})
        self.manifest["finished_at"] = now_iso()
        self.manifest["postconditions"] = ev.postconditions
        ev.write("run-manifest.json", self.manifest)
        # Final hygiene sweep across everything this run persisted.
        residual: dict[str, Any] = {}
        for name in sorted(ev.files):
            hits = secret_shapes((ev.dir / name).read_text(encoding="utf-8"))
            if hits:
                residual[name] = hits
        self.manifest["bundle_scan"] = {
            "files_scanned": len(ev.files),
            "secret_hits": residual,
            "bundle_withheld": bool(residual),
            "redactions": ev.redactions,
        }
        ev.write("run-manifest.json", self.manifest)
        ev.index()
        return {"stage": "close", "disposition": disposition,
                "postconditions": len(ev.postconditions),
                "bundle_withheld": bool(residual)}

    def close(self, *, remove_roots: bool) -> dict[str, Any]:
        if self._finalised:
            return {"stage": "closed", "idempotent": True}
        report: dict[str, Any] = {"stage": "close"}
        try:
            report["finalise"] = self.finalise()
        except Exception as exc:      # noqa: BLE001 — report must still close
            report["finalise_error"] = f"{type(exc).__name__}: {exc}"
            print(f"WARNING: could not write the final manifest: {exc}",
                  file=sys.stderr)
        finally:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:      # cleanup must proceed regardless
                    pass
            if remove_roots:
                report["ledger"] = self.ledger.dispose()
            else:
                report["retained_root"] = str(self.root)
        self._finalised = True
        return report

    def _need_evidence(self) -> Evidence:
        if self.evidence is None:
            raise GuardError("no evidence context: provision must run first "
                            "(--stop-after provision or earlier)")
        return self.evidence

    # -- orchestration ------------------------------------------------------

    def execute(self) -> dict[str, Any]:
        """Run the stages, always closing, and never losing the primary reason.

        Cleanup runs even when a stage aborts, but a cleanup problem can never
        mask the abort that caused it: both are reported, and ``ok`` says so.
        """
        report: dict[str, Any] = {"run_id": self.run_id, "root": str(self.root),
                                 "mode": "live", "ok": False}

        def stages() -> None:
            if self.resuming:
                # Phase 2: the world already exists; every fact is re-proved.
                # A continuation takes the no-prompt path instead of dispatching.
                if self.continuing:
                    report["continuation"] = self.resume_post_dispatch()
                else:
                    self.resume()
            else:
                report["preflight"] = self.preflight()
                self.provision()
                report["provisioned"] = True
                if self.args.stop_after == "provision":
                    return
                self.allocate()
                report["handoff"] = str(self.root / "evidence" / "handoff.json")
                if self.args.stop_after == "allocate":
                    return
                # Reaching here without resuming means a live dispatch was asked
                # for in the allocating invocation: run_worker_leg refuses it.
                self.run_worker_leg()
            if self.args.stop_after == "dispatch":
                return
            self.deliver()
            if self.args.stop_after == "deliver":
                return
            self.cleanup()

        abort: Exception | None = None
        try:
            stages()
            report["ok"] = True
        except Exception as exc:      # noqa: BLE001 — reported, never swallowed
            abort = exc
            report["aborted"] = {"type": type(exc).__name__, "message": str(exc),
                                "stage": self.stage}
        try:
            # Provenance rule: an invocation may remove only what IT created.
            # A resumed phase-2 never created the root, so it leaves the world
            # (and its evidence) in place — a refused gate must not destroy the
            # scenario the operator is mid-way through.  Explicit reclaim stays
            # available via --cleanup-only.
            keep = (self.args.keep_root
                   or bool(getattr(self, "cleanup_preserved", False))
                   or self.resuming)
            report["root_retained"] = keep
            report["close"] = self.close(remove_roots=not keep)
        except CleanupError as exc:
            report["cleanup_error"] = str(exc)
            print(f"WARNING: cleanup incomplete: {exc}", file=sys.stderr)
        if abort is not None:
            report["stage_reached"] = self.stage
            print(json.dumps(report, sort_keys=True, indent=True)[:2000],
                  file=sys.stderr)
            raise abort
        return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="m606_opencode_e2e.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true",
                    help="DEFAULT: print the plan and validate guards; creates "
                         "nothing, opens no DB, issues no Herdr command")
    mode.add_argument("--preflight", action="store_true",
                    help="read-only host checks (loopback oMLX, protocol, herdr "
                         "identity); creates nothing")
    mode.add_argument("--confirm-live-run", action="store_true",
                    help="execute the disposable scenario (G1 partial: still no "
                         "agent submit without --consent-dispatch)")
    mode.add_argument("--cleanup-only", action="store_true",
                    help="reclaim exactly the journalled resources of a previous "
                         "run at --root, then stop")

    p.add_argument("--root", required=True,
                   help="explicit isolated root; must be absolute, outside this "
                        "repository, and empty or absent")
    p.add_argument("--omlx-url", default=LOOPBACK_OMLX_URL,
                   help="loopback oMLX base URL (literal IP only)")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="pinned model id; must be served by the loopback catalog")
    p.add_argument("--protocol-version", default=CANONICAL_PROTOCOL_VERSION)
    p.add_argument("--protocol-sha256", default=CANONICAL_PROTOCOL_SHA256)
    p.add_argument("--stop-after", choices=STOP_POINTS, default="cleanup")
    worker = p.add_mutually_exclusive_group()
    worker.add_argument("--consent-dispatch", action="store_true",
                      help="GATE G1: allow submitting one prompt to an EXISTING "
                           "agent pane (needs --confirm-live-run, --herdr-pane "
                           "and --pane-model-evidence)")
    worker.add_argument("--scripted-worker-change", action="store_true",
                      help="OFFLINE validation leg: this harness authors the "
                           "owned-path change and files the WorkerReport itself, "
                           "stamped evidence_class=scripted / "
                           "real_agent_evidence=false. Mutually exclusive with "
                           "--consent-dispatch: a scripted leg cannot pretend to "
                           "be agent evidence")
    p.add_argument("--resume-handoff", action="store_true",
                   help="phase 2: continue the scenario already provisioned and "
                        "allocated at --root, re-proving every durable fact "
                        "before gating the pane (the ONLY way to reach a live "
                        "prompt)")
    p.add_argument("--post-dispatch", action="store_true",
                   help="continue an attempt whose dispatch is ALREADY on record:"
                       " verify and deliver it from an external final WorkerReport"
                       " (--worker-report-file). Issues NO prompt (needs"
                       " --confirm-live-run --resume-handoff; refuses"
                       " --consent-dispatch / --ceo-pane / --wake-deliver /"
                       " --scripted-worker-change and any pane designation)")
    p.add_argument("--scripted-marker", default=None,
                   help=f"text the scripted leg writes (default {MARKER}); "
                        "use a WRONG marker to prove verifiers refuse")
    p.add_argument("--worker-report-file", default=None,
                   help="path to an externally produced final WorkerReport JSON "
                        "to persist before delivery (required for a live leg "
                        "unless --scripted-worker-change)")
    p.add_argument("--pane-model-evidence", default=None,
                   help="operator attestation JSON naming the pane/terminal/"
                        "session/agent and the model that pane is running; "
                        "REQUIRED before any prompt (fail-closed)")
    p.add_argument("--expect-session-id", default=None,
                   help="the exact native session id the operator designates for "
                        "this leg; a mismatch refuses dispatch")
    p.add_argument("--herdr-agent", default="opencode",
                   help="expected agent kind on the target pane (identity check)")
    p.add_argument("--ceo-agent", default=None,
                   help="expected agent kind on the CEO pane (defaults to "
                        "--herdr-agent); set this for a cross-harness CEO leg")
    p.add_argument("--herdr-pane", default=None,
                   help="existing pane id used as the prompt TARGET, e.g. w3:p2Q "
                        "(from `herdr agent list`); never created by this runner")
    p.add_argument("--ceo-pane", default=None,
                   help="PG-7: existing pane designated as the project's canonical "
                        "CEO. Enables the CEO bootstrap leg (issue challenge -> "
                        "fence -> one prompt -> assumed_default acceptance). "
                        "Phase 2 only; the runner never creates a pane")
    p.add_argument("--ceo-name", default="ceo",
                   help="canonical CEO member name (Wake targets this name); must "
                        "be the only CEO in the project")
    p.add_argument("--ceo-expect-session-id", default=None,
                   help="exact native session id the operator designates as the "
                        "CEO; defaults to --expect-session-id when the CEO is the "
                        "same pane as the worker")
    p.add_argument("--ceo-claim-file", default=None,
                   help="optional agent self-claim JSON to retain as UNVERIFIED "
                        "audit material; it can never initialize a project nor "
                        "upgrade the receipt source")
    p.add_argument("--wake-deliver", action="store_true",
                   help="actually deliver one Wake prompt to the CEO pane and "
                        "record its transport outcome; without it the Wake is "
                        "admitted (durable pre-I/O boundary) and honestly left "
                        "undelivered instead of borrowing the worker's outcome")
    p.add_argument("--submit-wait", action="store_true",
                   help="ask herdr to observe a post-submit state (transport "
                        "observation only; still not an acknowledgement)")
    p.add_argument("--submit-until", action="append", default=[],
                   choices=["idle", "working", "blocked", "done", "unknown"],
                   help="state(s) to wait for with --submit-wait (repeatable)")
    p.add_argument("--submit-timeout-ms", type=int, default=5000,
                   help="herdr --wait timeout in milliseconds")
    p.add_argument("--ack-evidence-file", default=None,
                   help="optional operator-supplied acknowledgement artifact; "
                        "without it prompt_ack stays unknown")
    p.add_argument("--human-accept", action="store_true",
                   help="separate acknowledgement that promotes submitted→accepted")
    p.add_argument("--accept-actor", default=None)
    p.add_argument("--accept-reason", default=None)
    p.add_argument("--keep-root", action="store_true",
                   help="leave the disposable root in place for inspection "
                        "(default: remove what this run created)")
    p.add_argument("--allow-non-temp-root", action="store_true",
                   help="permit --root outside the system temp base (still never "
                        "inside this repository)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def validate_pins(version: str, sha: str) -> dict[str, str]:
    """Pin shape check — run even in inert modes, before any I/O."""
    import re
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version or ""):
        raise GuardError(f"--protocol-version must be MAJOR.MINOR.PATCH, got "
                        f"{version!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise GuardError(f"--protocol-sha256 must be 64 lowercase hex, got "
                        f"{sha!r}")
    if version != CANONICAL_PROTOCOL_VERSION or sha != CANONICAL_PROTOCOL_SHA256:
        # Not fatal, but recorded: the run is pinning a non-canonical pair.
        print(f"NOTE: pins differ from canonical {CANONICAL_PROTOCOL_VERSION}/"
              f"{CANONICAL_PROTOCOL_SHA256[:16]}… — expected for a forked "
              f"protocol, unexpected otherwise", file=sys.stderr)
    return {"protocol_version": version, "protocol_sha256": sha}


def validate_arg_combinations(args: argparse.Namespace) -> dict[str, Any]:
    """Refuse unsafe flag combinations in EVERY mode, before any I/O.

    These are the acceptance gates expressed as argument rules, so an
    under-specified invocation fails at parse time rather than mid-run.
    """
    notes: list[str] = []
    if args.post_dispatch:
        # A continuation is defined by what it cannot do: it submits nothing.  Any
        # flag that implies a prompt, or that would author the evidence it is meant
        # to consume, contradicts the mode and is refused before any I/O.
        if not args.confirm_live_run:
            raise GuardError("--post-dispatch writes to the disposable run, so it"
                            " requires --confirm-live-run")
        if not args.resume_handoff:
            raise GuardError("--post-dispatch continues an attempt already on disk,"
                            " so it requires --resume-handoff")
        if args.consent_dispatch:
            raise GuardError("--post-dispatch and --consent-dispatch contradict each"
                            " other: the continuation exists precisely so that a"
                            " dispatched attempt is never dispatched twice")
        if args.scripted_worker_change:
            raise GuardError("--post-dispatch consumes the worker's own final"
                            " report; --scripted-worker-change would author it here")
        if args.ceo_pane:
            raise GuardError("--post-dispatch submits no prompt, so it cannot run"
                            " the PG-7 CEO bootstrap (--ceo-pane)")
        if args.wake_deliver:
            raise GuardError("--post-dispatch submits no prompt, so it cannot"
                            " deliver a Wake (--wake-deliver)")
        if not args.worker_report_file:
            raise GuardError("--post-dispatch requires --worker-report-file: the"
                            " external final WorkerReport is the only thing that may"
                            " close the execution it continues")
        if args.stop_after in ("provision", "allocate", "dispatch"):
            raise GuardError(
                f"--post-dispatch with --stop-after {args.stop_after} is"
                " contradictory: the continuation begins after dispatch")
        if args.pane_model_evidence or args.expect_session_id or args.herdr_pane:
            raise GuardError("--post-dispatch takes no pane designation"
                            " (--herdr-pane / --expect-session-id /"
                            " --pane-model-evidence): designating a target implies a"
                            " prompt, and this mode sends none")
        notes.append("post-dispatch continuation: zero prompts, prior dispatch must"
                    " be durably 'submitted', external FINAL WorkerReport must bind"
                    " this attempt exactly (no duplicate dispatch, no UNKNOWN)")
    if args.ceo_pane:
        # PG-7 leg: it submits a prompt, so it inherits every consent rule and
        # adds its own.  A scripted leg has no real pane and no real agent, so
        # recording an initialization against it would fabricate PG-7 evidence.
        if not args.confirm_live_run:
            raise GuardError("--ceo-pane runs the PG-7 CEO bootstrap, which "
                            "submits a prompt; it requires --confirm-live-run")
        if args.scripted_worker_change:
            raise GuardError("--ceo-pane cannot combine with "
                            "--scripted-worker-change: a harness-authored leg has "
                            "no live CEO, and initializing under it would invent "
                            "PG-7 evidence")
        if not args.consent_dispatch:
            raise GuardError("--ceo-pane requires --consent-dispatch: the CEO "
                            "challenge is answered by a real prompt")
        if args.ceo_pane == args.herdr_pane:
            if not args.expect_session_id and not args.ceo_expect_session_id:
                raise GuardError(
                    "--ceo-pane reusing the worker pane needs "
                    "--expect-session-id (or --ceo-expect-session-id): the "
                    "designated CEO session must be named by the operator")
        elif not args.ceo_expect_session_id:
            raise GuardError("--ceo-pane designates a second pane, so it needs "
                            "--ceo-expect-session-id naming that CEO's native "
                            "session id")
        notes.append("PG-7 CEO leg: canonical identity -> one-shot challenge -> "
                    "fence -> one prompt -> assumed_default (never host_verified)")
    if args.ceo_expect_session_id and not args.ceo_pane:
        raise GuardError("--ceo-expect-session-id requires --ceo-pane")
    if args.ceo_claim_file and not args.ceo_pane:
        raise GuardError("--ceo-claim-file requires --ceo-pane: a claim without a "
                        "designated CEO has nothing to attach to")
    if args.wake_deliver:
        if not args.ceo_pane:
            raise GuardError("--wake-deliver requires --ceo-pane: a Wake is a "
                            "delivery TO the canonical CEO, and no CEO is "
                            "designated")
        notes.append("wake transport: one prompt to the CEO pane, outcome recorded "
                    "as submitted|unknown; CEO acknowledgement is never inferred")
    if args.resume_handoff:
        if not args.confirm_live_run:
            raise GuardError("--resume-handoff continues a run on disk, so it "
                            "requires --confirm-live-run")
        if args.scripted_worker_change:
            raise GuardError("--resume-handoff drives the LIVE leg; it cannot be "
                            "combined with --scripted-worker-change")
        if args.stop_after in ("provision", "allocate"):
            raise GuardError(
                f"--resume-handoff with --stop-after {args.stop_after} is "
                "contradictory: phase 2 begins after allocation")
    if args.consent_dispatch:
        if not args.confirm_live_run:
            raise GuardError("--consent-dispatch without --confirm-live-run: "
                            "submitting input requires both explicit flags")
        if not args.resume_handoff:
            raise GuardError(
                "--consent-dispatch requires --resume-handoff: " + HANDOFF_REQUIRED)
        if not args.herdr_pane:
            raise GuardError("--consent-dispatch requires --herdr-pane: the "
                            "runner targets one operator-designated existing pane")
        if not args.pane_model_evidence:
            raise GuardError(
                "--consent-dispatch requires --pane-model-evidence: the pane's "
                "actual local model must be attested before any prompt (the "
                "loopback catalog proves the server, not the pane)")
        if not args.expect_session_id:
            raise GuardError(
                "--consent-dispatch requires --expect-session-id: the operator "
                "must name the exact native session the prompt may enter")
    if args.scripted_worker_change:
        if not args.confirm_live_run:
            raise GuardError("--scripted-worker-change writes to disk, so it "
                            "still requires --confirm-live-run")
        notes.append("scripted leg: harness-authored change, stamped "
                    "evidence_class=scripted (NOT real agent evidence)")
    if args.human_accept and not (args.accept_actor and args.accept_reason):
        raise GuardError("--human-accept requires --accept-actor and "
                       "--accept-reason; acceptance is a separate, attributed "
                       "acknowledgement")
    if args.submit_wait and not args.consent_dispatch:
        raise GuardError("--submit-wait only applies to a dispatched prompt")
    return {"arg_rules_passed": True, "notes": notes}


def plan_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": "plan",
        "would_execute": False,
        "created_or_mutation_issued": "none",
        "root": args.root,
        "pins": {"protocol_version": args.protocol_version,
                 "protocol_sha256": args.protocol_sha256, "model": args.model,
                 "omlx_url": args.omlx_url},
        "gates": {
            "G1_agent_submit": "released only in phase 2 (--resume-handoff + "
            "--consent-dispatch)" if (args.confirm_live_run and
                                     args.consent_dispatch and
                                     args.resume_handoff)
            else "not-released",
            "G2_local_model_route": "required-healthy-at-run-start,"
            "checked-before-any-write",
            "G3_herdr_identity": "read-only-bind-required",
            "G4_pane_idle_and_cwd": "idle-existing-pane-in-allocated-worktree,"
            "session-stable-across-two-reads",
            "G5_pane_local_model": "operator-attestation-required,"
            "catalog-presence-insufficient,fail-closed",
            "G6_capability_probes": "durable-before-bind_attempt_capabilities",
            "G7_ceo_bootstrap": (
                "requested — phase 2 only, fenced, assumed_default"
                if args.ceo_pane else f"not-requested ({CEO_STAGE_NOTE})"),
            "G7_ceo_bootstrap_policy": (
                "canonical-CEO-via-sync_state,one-shot-nonce,fence-before-prompt,"
                "assumed_default-only,never-host_verified,never-resent,"
                "agent-claim-is-audit-only"),
            "G8_wake": ("delivered-to-ceo-pane,submitted|unknown"
                        if args.wake_deliver else
                        "intake+admission-only,admitted-not-delivered,"
                        "never-borrows-worker-outcome,ceo-ack-never-fabricated"),
            "G9_post_dispatch_continuation": (
                "requested — no prompt; prior dispatch must be durably 'submitted';"
                " external FINAL WorkerReport must bind this attempt exactly"
                if args.post_dispatch else
                f"available (--post-dispatch); {POST_DISPATCH_NOTE}"),
            "pg7_ceo": {
                "why_phase2_only": CEO_STAGE_NOTE,
                "command": ("--confirm-live-run --resume-handoff --consent-dispatch"
                           " --herdr-pane <worker-pane> --pane-model-evidence <f>"
                           " --expect-session-id <worker-session>"
                           " --ceo-pane <ceo-pane>"
                           " --ceo-expect-session-id <ceo-session>"
                           " [--wake-deliver --ceo-claim-file <agent-echo.json>]"),
                "identity": "one canonical CEO member named 'ceo' (kind opencode), "
                           "bound session+terminal+attempt with the protocol pin",
                "receipt": "assumed_default — transport success, not a host receipt",
                "acknowledgement": "never inferred; acknowledged cursor must stay 0",
            },
            "operator_handoff": {
                "why": "an operator cannot cd a pane into a worktree before it "
                     "exists, so the cwd gate is unverifiable in the allocating "
                     "invocation",
                "phase_1": "--confirm-live-run --stop-after allocate",
                "between_phases": "place the existing pane in handoff.json's "
                                 "allocated_cwd, attest its model, note its session",
                "phase_2": "--confirm-live-run --resume-handoff --consent-dispatch "
                         "--herdr-pane <pane> --pane-model-evidence <file> "
                         "--expect-session-id <id>",
                "single_invocation_live_dispatch": "refused by design",
                "after_dispatch_report_arrived": (
                    "--confirm-live-run --resume-handoff --post-dispatch"
                    " --worker-report-file <final-report.json>"
                    " --stop-after deliver  (no prompt; " + POST_DISPATCH_NOTE + ")"),
            },
            "worker_evidence": ("scripted (harness-authored, not real agent)"
                               if args.scripted_worker_change else
                               "external WorkerReport required for delivery"),
            "human_accept": "separate-acknowledgement-required"
            if not args.human_accept else "requested",
        },
        "stages": [
            "preflight: loopback oMLX + canonical protocol + herdr identity",
            "provision: root, bare origin+clone, installer, protocol pin, "
            "fresh DB at head, capability + model binding",
            "ceo (phase 2, PG-7, opt-in): canonical CEO identity → one-shot "
            "challenge → fence → one prompt → assumed_default acceptance",
            "dispatch: identity bind, one prompt into an EXISTING pane, wake",
            "deliver: Diff Gate → local commit → ship evidence "
            f"→ disposition {DELIVERY_SUBMITTED}",
            "cleanup: CLEANABLE → worktree removal → journalled root removal",
            "close: manifest with honest non-claims + evidence index",
        ],
        "hard_rules": [
            "topology mutation verbs structurally forbidden",
            "no cloud fallback; loopback only; no credentials read",
            "delivery is submitted/unknown unless --human-accept",
            "cleanup removes only journalled paths of this invocation",
            "project repo, its worktrees and its live DB are untouchable",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = (lambda obj: print(json.dumps(obj, sort_keys=True, indent=2))) \
        if args.json else (lambda obj: [print(f"{k}: {v}")
                                       for k, v in obj.items()])
    try:
        # Guards run in EVERY mode, including the inert default: validation is
        # pure computation, so an unsafe target is refused before anything else.
        validate_pins(args.protocol_version, args.protocol_sha256)
        validated_root = validate_root(args.root,
                                      allow_non_temp=args.allow_non_temp_root,
                                      for_cleanup=bool(args.cleanup_only),
                                      for_resume=bool(args.resume_handoff))
        validate_loopback(args.omlx_url)
        arg_rules = validate_arg_combinations(args)

        if args.plan or not (args.preflight or args.confirm_live_run
                             or args.cleanup_only):
            # Inert by default: guards validated, nothing created, nothing sent.
            payload = plan_payload(args)
            payload["root_validated"] = str(validated_root)
            payload["root_would_be_created"] = not validated_root.exists()
            emit(payload)
            return 0

        run = M606LiveRun(args)

        if args.cleanup_only:
            if not validated_root.exists():
                # Nothing to remove and nothing was ever removed by someone else
                # in a way we can see: report success without deleting anything.
                emit({"mode": "cleanup-only", "already_cleaned": True,
                    "root": str(validated_root), "removed": [],
                    "note": "the run root is already absent; no deletion was "
                           "needed or performed"})
                return 0
            journal = run.root / "evidence" / "journal.jsonl"
            ledger = Ledger(run.root, run.run_id, journal)
            ledger.load_journal()
            emit({"mode": "cleanup-only", **ledger.dispose()})
            return 0

        if args.preflight:
            emit(run.preflight())
            return 0

        emit(run.execute())
        return 0
    except (GuardError, PostconditionError, CleanupError) as exc:
        print(f"ABORT [{type(exc).__name__}]: {exc}", file=sys.stderr)
        return 2
    except (domain.AttemptSlotOccupiedError, ceo_protocol.ProtocolError,
            ceo_initialization.CEOInitializationError,
            workspaces.WorkspacePathError, RuntimeError) as exc:
        print(f"ABORT [{type(exc).__name__}]: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
