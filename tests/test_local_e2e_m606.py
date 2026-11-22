"""M6-06 — disposable scripted local E2E fixture (preflight S0/S1/S4/S5).

Roadmap: M6-06 (Codex/OpenCode local cross-harness E2E), drafted from ``main``
at ``9735d7c`` and the scenario table in
``doc/M6_06_LOCAL_E2E_PREFLIGHT.md``.

WHAT THIS FIXTURE IS
    A *scripted* end-to-end rehearsal.  Every harness-side effect (the worker's
    change on its task branch, the brief, the WorkerReport) is authored by THIS
    test process.  Verification, Diff Gate, local delivery and cleanup are the
    real TeamCraft services, executed against a throw-away Git fixture and a
    throw-away SQLite file under ``TMPDIR``.

WHAT THIS FIXTURE IS NOT (the honesty contract)
    * Not real-agent evidence.  No Codex/OpenCode/herdr process is spawned and no
    harness ``--version`` probe is executed here; PATH presence is recorded as
    ``found``/``absent`` only.  The two REAL-host rows the roadmap wants (init
    handshake, dispatch/Wake over real herdr — PG-7, gates G1/G2/G3) stay
    ``pending`` and are written as such, never omitted and never implied passed.
    * ``prompt_ack`` and ``event_stream`` stay ``unknown``; no host-trusted
    Protocol/Skill receipt is claimed for any harness.
    * No Claude row (PG-1: the binary is not installed by TeamCraft).
    * No push anywhere: the fixture's ``origin`` is a local bare repository, and
    the test proves ``origin`` never gains a ref.
    * The live project runtime (``<repo>/.teamcraft/`` including the real
    ``teamcraft.db``) is never opened.  The path guard makes that structural
    rather than a promise: provisioning aborts if the run root resolves inside
    the protected repository root.

Acceptance matrix covered here
    S0  provision + path guard + fresh DB at head + ``run-manifest.json``
    S1  installer dry-run (zero writes) and real run into a disposable target,
        fail-before-write on a user-shaped conflict, ``verify_protocol`` in the
        target, and a project + Attempt pinning the exact protocol pair in the
        run-local DB;  negatives: pinned-hash mismatch, empty claim set
    S4  worker-side scripted commit evidence, local ``lint``/``test`` profiles
        whose verdict comes from REPO FACTS (a self-report that lies loses),
        verification rows with artifact-root-relative log paths and bounded
        digests;  negative: unowned path ⇒ Diff-Gate refusal stands
    S5  Diff Gate over the branch diff, DONE reachable only with commit+diff
        evidence, CLEANABLE + cleanup audit line, replay convergence, origin
        untouched (no push)
    UNKNOWN  an unresolved Attempt never passes: report/verify/clean are refused
        and only reconcile-class events can close it
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import datetime
from pathlib import Path
from typing import Any

from teamcraft import (
    briefs,
    ceo_protocol,
    delivery,
    diagnostics,
    domain,
    leader,
    storage,
    verification,
    worker_reports,
    workspaces,
    worktrees,
)
from teamcraft.errors import (
    DomainError,
    IllegalTransitionError,
    NotFoundError,
    ValidationError,
)

#: The protected repository: nothing this fixture writes may land inside it.
PROTECTED_ROOT = Path(__file__).resolve().parents[1]

#: Canonical protocol identity (doc/M6_06_LOCAL_E2E_PREFLIGHT.md §1.2).
CANONICAL_PROTOCOL_VERSION = "1.0.0"
CANONICAL_PROTOCOL_SHA256 = (
    "c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742")

#: Read-mostly marker the scripted worker must leave in its owned path.
FEATURE_MARKER = "M606-OWNED"

RUN_PREFIX = "m606-e2e-"

GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false",
    "-c", "user.name=M606 Fixture", "-c", "user.email=m606@example.invalid",
    "-c", "init.defaultBranch=main",
]

#: Git verbs that must NEVER appear in a local-only delivery effect.
FORBIDDEN_VERBS = frozenset({
    "push", "pull", "fetch", "clone", "merge", "rebase", "reset", "clean",
    "checkout", "switch", "restore", "prune", "gc", "filter-branch", "tag",
    "notes", "mv",
})


class PathGuardError(RuntimeError):
    """S0 abort: the run root resolved inside the protected repository."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def guard_outside_protected(path: Path) -> Path:
    """Return the realpath of ``path`` or abort (S0 path guard).

    The disposable scenario must live OUTSIDE ``PROTECTED_ROOT`` so no DB, Git
    or artifact effect can reach the live project runtime.
    """
    real = Path(os.path.realpath(str(path)))
    if real == PROTECTED_ROOT or str(real).startswith(
            str(PROTECTED_ROOT) + os.sep):
        raise PathGuardError(
            f"path guard tripped: run root {real} resolves inside the protected "
            f"repository {PROTECTED_ROOT}; refusing to provision")
    return real


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run([*GIT, *args], cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def count_payload_files(root: Path) -> int:
    """Files under ``root`` excluding Git internals (the installer's scope)."""
    return sum(1 for p in root.rglob("*")
               if p.is_file() and ".git" not in p.relative_to(root).parts)


def installer_env() -> dict[str, str]:
    """Environment for ``scripts/install.sh`` pinned to THIS interpreter."""
    env = dict(os.environ)
    bindir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = os.pathsep.join([bindir, env.get("PATH", "")])
    return env


def run_installer(target: Path, *, dry_run: bool) -> tuple[int, str, str]:
    """Invoke the repo installer against a disposable target (never the repo)."""
    script = PROTECTED_ROOT / "scripts" / "install.sh"
    argv = ["bash", str(script), "--target", str(target)]
    if dry_run:
        argv.append("--dry-run")
    proc = subprocess.run(argv, cwd=str(PROTECTED_ROOT), env=installer_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         check=False)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


class RecordingRunner:
    """GitRunner wrapper recording every argv for the verb audit."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd) -> bytes:
        self.calls.append(list(args))
        return workspaces._default_run_git(args, str(cwd))

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls if call]


class FailingMergeTreeRunner(RecordingRunner):
    """Turns the in-memory merge probe undecidable (exit 2, not 0/1)."""

    def __call__(self, args, cwd) -> bytes:
        if args and args[0] == "merge-tree":
            raise workspaces.GitStateError(
                "git merge-tree failed (exit 128): scripted undecidable probe")
        return super().__call__(args, cwd)


def scan_local_leaks(text: str) -> list[str]:
    """M6-03 shapes reused as a scanner (never as a scrubber) for S4 evidence."""
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


def harness_inventory() -> dict[str, str]:
    """PATH presence ONLY — no harness binary is executed by this fixture."""
    return {name: ("found" if shutil.which(name) else "absent")
            for name in ("codex", "opencode", "herdr", "claude")}


# ---------------------------------------------------------------------------
# The disposable run
# ---------------------------------------------------------------------------

class M606Run:
    """One fully isolated M6-06 run: tmp root, origin+clone, run-local DB."""

    def __init__(self, name: str, *, claims: tuple[str, ...] = ("src/**",),
                 install: bool = True) -> None:
        self.name = name
        self.claims = list(claims)
        self.rec = RecordingRunner()
        self._closed = False

        raw = Path(tempfile.mkdtemp(prefix=f"{RUN_PREFIX}{name}-"))
        self.root = guard_outside_protected(raw)
        (self.root / "evidence").mkdir(parents=True, exist_ok=True)

        # PRE-7 git fixture: bare origin + clone, seeded with a base commit.
        self.origin = self.root / "origin.git"
        self.project = self.root / "project"
        self._init_origin_and_clone()

        # S1: installer into the disposable target (the clone), then protocol.
        self.installed: dict[str, Any] = {"requested": False}
        if install:
            self.installed = self._installer_stage()

        self.protocol = ceo_protocol.verify_protocol(
            str(self.target_references),
            expected_version=CANONICAL_PROTOCOL_VERSION,
            expected_sha256=CANONICAL_PROTOCOL_SHA256)

        # PRE-6 fresh DB at head, entirely inside the run root.
        self.db = self.root / "teamcraft.db"
        self.migration = storage.migrate_to_head(
            self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.additionals: list[Any] = []

        self.project_row = domain.create_project(
            self.conn, f"m606-{name}", str(self.project))
        self.manifest = self._write_manifest()

    # -- provisioning ---------------------------------------------------------

    def _init_origin_and_clone(self) -> None:
        """Bare origin, seeded through a throwaway clone, then the real clone.

        The clone must be created AFTER the base commit exists so its own HEAD
        resolves; TeamCraft services require a non-empty repository.
        """
        git("init", "-q", "--bare", str(self.origin), cwd=self.root)
        seed = self.root / "seed"
        git("init", "-q", str(seed), cwd=self.root)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        (seed / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
        (seed / "src").mkdir()
        (seed / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
        (seed / "docs").mkdir()
        (seed / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "base", cwd=seed)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=seed)
        self.base_sha = git("rev-parse", "refs/heads/main", cwd=self.origin)
        shutil.rmtree(seed)
        git("clone", "-q", str(self.origin), str(self.project), cwd=self.root)
        self.clone_head = git("rev-parse", "HEAD", cwd=self.project)
        # A real operator's clone has an author identity; delivery refuses to
        # fabricate one, so the fixture configures it locally (never globally).
        git("config", "user.name", "M606 Fixture", cwd=self.project)
        git("config", "user.email", "m606@example.invalid", cwd=self.project)
        self.author_name = git("config", "--get", "user.name", cwd=self.project)
        self.author_email = git("config", "--get", "user.email",
                                cwd=self.project)

    def _installer_stage(self) -> dict[str, Any]:
        """PRE-4 dry-run (zero writes) then the S1 real run into the clone."""
        before = count_payload_files(self.project)
        dry_code, dry_out, _dry_err = run_installer(self.project, dry_run=True)
        after_dry = count_payload_files(self.project)
        planned = sum(1 for line in dry_out.splitlines() if "create" in line)
        code, out, err = run_installer(self.project, dry_run=False)
        if code != 0:
            raise AssertionError(f"installer failed: {err}")
        created = [line for line in out.splitlines() if "create" in line]
        references = self.target_references
        self._assert_managed_copies()
        return {
            "requested": True,
            "dry_run_returncode": dry_code,
            "dry_run_planned": planned,
            # Net new files the dry-run left behind: must be exactly zero.
            "dry_run_writes": after_dry - before,
            "returncode": code,
            "changes": len(created),
            "files_before": before,
            "files_after": count_payload_files(self.project),
            "protocol_verified_in_target": True,
            "references": str(references.relative_to(self.root)),
        }

    @property
    def target_references(self) -> Path:
        return (self.project / ".agents" / "skills" / "teamcraft-ceo"
                / "references")

    def _assert_managed_copies(self) -> None:
        """Every sampled managed copy must be BYTE-identical to canonical."""
        pairs = [
            (self.target_references / "CEO_PROTOCOL.md",
             PROTECTED_ROOT / ".agents" / "skills" / "teamcraft-ceo"
             / "references" / "CEO_PROTOCOL.md"),
            (self.target_references / "protocol.manifest.json",
             PROTECTED_ROOT / ".agents" / "skills" / "teamcraft-ceo"
             / "references" / "protocol.manifest.json"),
            (self.project / ".agents" / "skills" / "teamcraft-ceo" / "SKILL.md",
             PROTECTED_ROOT / ".agents" / "skills" / "teamcraft-ceo"
             / "SKILL.md"),
        ]
        for target, canonical in pairs:
            self.assertTrueish(target.is_file(), f"missing managed copy {target}")
            if target.read_bytes() != canonical.read_bytes():
                raise AssertionError(f"managed copy drift: {target}")

    @staticmethod
    def assertTrueish(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def _write_manifest(self) -> dict[str, Any]:
        """S0 evidence: everything a run must declare before it is trusted."""
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.manifest_run_id(),
            "milestone": "M6-06",
            "drafted_from": "9735d7c",
            "protected_root": str(PROTECTED_ROOT),
            "run_root": str(self.root),
            "outside_protected_root": True,
            "step_matrix": {
                "S0": "executed",
                "S1": "executed",
                "S2": "pending:G1",
                "S3": "pending:G1",
                "S4": "planned",
                "S5": "planned",
                "S6": "not-in-scope",
            },
            "evidence_class": "scripted",
            "real_agent_evidence": False,
            "prompt_ack": "unknown",
            "event_stream": "unknown",
            "host_trusted_receipt": False,
            "harness_legs": {"codex": "scripted", "opencode": "scripted",
                            "claude": "pending:PG-1"},
            "gates_open": ["G1", "G2", "G3", "G5", "G6"],
            "interpreter": {
                "python": sys.version.split()[0],
                "sqlite": storage.sqlite3.sqlite_version
                if hasattr(storage, "sqlite3") else None,
                "git": self.git_version(),
            },
            "harness_binaries": harness_inventory(),
            "database": {
                "path": str(self.db),
                "outside_protected_root": True,
                "fresh": self.migration.fresh,
                "from_version": self.migration.from_version,
                "to_version": self.migration.to_version,
                "schema_head": storage.SCHEMA_VERSION,
            },
            "protocol": {
                "protocol_version": self.protocol.version,
                "sha256": self.protocol.sha256,
                "canonical_match": self.protocol.sha256 == CANONICAL_PROTOCOL_SHA256,
            },
            "installer": self.installed,
            "git": {"origin": str(self.origin), "project": str(self.project),
                   "base_sha": self.base_sha, "clone_head": self.clone_head,
                   "author_name": self.author_name,
                   "author_email": self.author_email},
            "capabilities_non_claims": {
                "prompt_ack": "unknown",
                "event_stream": "unknown",
                "host_trusted_receipt": False,
                "direct_pr": "out-of-scope:M4-05",
                "claude": "pending:PG-1",
            },
        }
        path = self.root / "evidence" / "run-manifest.json"
        path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")
        return manifest

    def finalize(self, steps: dict[str, str], **extra: Any) -> dict[str, Any]:
        """Close the manifest with what ACTUALLY ran, never what was planned.

        Steps are only promoted to ``executed`` by the test that performed them,
        so the evidence file cannot claim more than it did.  The final block
        records the remote state proving no ref was ever pushed.
        """
        refs = self.origin_refs()
        manifest = json.loads(
            (self.root / "evidence" / "run-manifest.json").read_text(
                encoding="utf-8"))
        manifest["step_matrix"].update(steps)
        manifest["finished_at_utc"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds")
        manifest["final"] = {
            "origin_refs": refs,
            # Anything but the seeded main would mean a push happened.
            "pushed_refs": sorted(r for r in refs if r != "refs/heads/main"),
            "worker_leg": "scripted",
            "real_agent_spawned": False,
        }
        manifest.update(extra)
        self.manifest = manifest
        (self.root / "evidence" / "run-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        return manifest

    def manifest_run_id(self) -> str:
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def git_version() -> str:
        proc = subprocess.run(["git", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False)
        return (proc.stdout.decode("utf-8", "replace").strip()
                if proc.returncode == 0 else "unknown")

    # -- pipeline helpers -----------------------------------------------------

    def lease(self, holder: str = "m606-fixture") -> leader.Lease:
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_row.id), holder,
            ttl_seconds=3700)

    def script_ship(self, *, marker: str = FEATURE_MARKER,
                    extra_paths: dict[str, str] | None = None,
                    pin_protocol: bool = True):
        """Author the worker-side facts a real agent would have produced.

        SCRIPTED BY THE TEST: change in the owned path, immutable brief, final
        completed WorkerReport.  Returns (task, attempt, allocation, report).
        """
        task = domain.create_task(
            self.conn, self.project_row.id, f"m606 {self.name}",
            kind="ship", ownership_json=json.dumps(self.claims))
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version)
        task = domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"delivery": "local_only"}, actor="ceo",
            reason="m606 scripted scenario policy")
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version,
            **({"protocol_version": self.protocol.version,
                "protocol_hash": self.protocol.sha256} if pin_protocol else {}))
        domain.register_session(self.conn, attempt.id, "scripted",
                               native_session_id=f"scripted-{attempt.id}",
                               pane_id=f"pane-{self.name}", state="working")
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        allocation, _created = worktrees.allocate_attempt_worktree(
            self.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.project), run_git=self.rec)

        worktree = Path(allocation.worktree_path)
        (worktree / "src" / "feature.py").write_text(
            f"{marker}: shipped by the scripted fixture\n", encoding="utf-8")
        for relpath, body in (extra_paths or {}).items():
            stray = worktree / relpath
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_text(body, encoding="utf-8")

        brief = briefs.create_attempt_brief(self.conn, attempt.id,
                                           content=f"M6-06 scripted brief {self.name}")
        report = worker_reports.record_worker_report(self.conn, {
            "schema_version": 1, "task_id": task.id, "attempt_id": attempt.id,
            "task_version": brief.task_version, "brief_digest": brief.brief_digest,
            "report_kind": "final", "outcome": "completed",
            "milestone_id": "final",
            "summary": "SCRIPTED leg: fixture authored the change; no agent ran",
            "artifact_digest": None, "usage": {"total_tokens": None},
        })
        domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version)
        return (domain.get_task(self.conn, task.id),
                domain.get_attempt(self.conn, attempt.id), allocation, report)

    def approve_profiles(self, *, fail_marker: bool = False) -> None:
        """Approve the local ``lint`` + ``test`` profiles the way M4-03 does.

        The argvs inspect REPO FACTS in the Attempt's own worktree — a
        WorkerReport that claims more than the repository proves loses.
        """
        lint = (
            "import pathlib,sys;"
            "p=pathlib.Path('src/feature.py');"
            f"needle={FEATURE_MARKER!r};"
            "ok=p.is_file() and needle in p.read_text(encoding='utf-8');"
            "print('lint: src/feature.py ' + ('ok' if ok else 'MISSING/MARKER-ABSENT'));"
            "sys.exit(0 if ok else 1)")
        test = (
            "import pathlib,sys;"
            "d=pathlib.Path('docs/guide.md');"
            "ok=d.is_file() and d.read_text(encoding='utf-8')=='guide\\n';"
            "print('test: docs/guide.md ' + ('unchanged' if ok else 'MOVED'));"
            "sys.exit(0 if ok else 1)")
        for profile, script, key in (("lint", lint, "cfg-lint"),
                                    ("test", test, "cfg-test")):
            verification.configure_verifier_profile(
                self.conn, self.project_row.id, profile=profile,
                argv=[sys.executable, "-c", script], timeout_seconds=20,
                actor="human-reviewer", idempotency_key=f"{self.name}-{key}",
                lease=self.lease())

    def run_suite(self, attempt_id: str) -> dict[str, Any]:
        return verification.run_worktree_suite(
            self.conn, attempt_id, idempotency_key=f"suite-{attempt_id}",
            lease=self.lease(), profiles=["lint", "test"])

    def deliver(self, attempt_id: str) -> dict[str, Any]:
        """The only path to DONE: commit -> ship evidence -> human accept."""
        commit = delivery.create_local_commit(
            self.conn, attempt_id, idempotency_key=f"commit-{attempt_id}",
            lease=self.lease(), run_git=self.rec)
        ship = delivery.record_ship_evidence(
            self.conn, attempt_id, idempotency_key=f"ship-{attempt_id}",
            lease=self.lease(), run_git=self.rec)
        approval = delivery.approve_local_delivery(
            self.conn, ship.id, decision="accept", commit_sha=commit.commit_sha,
            branch=commit.branch, actor="human-reviewer",
            reason="M6-06 scripted rehearsal accepted LOCAL-ONLY (no push)",
            idempotency_key=f"approve-{attempt_id}", lease=self.lease(),
            run_git=self.rec)
        return {"commit": commit, "ship": ship, "approval": approval}

    # -- read-only probes -----------------------------------------------------

    def origin_refs(self) -> dict[str, str]:
        out = subprocess.run(
            [*GIT, "ls-remote", str(self.origin)], cwd=str(self.root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        refs: dict[str, str] = {}
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            # Only branch refs count: HEAD is a symref, not a branch.
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                refs[parts[1].strip()] = parts[0].strip()
        return refs

    def events(self, kind: str, attempt_id: str | None = None) -> list:
        sql = "SELECT seq, event_id, payload_json FROM events WHERE kind = ?"
        args: list[Any] = [kind]
        if attempt_id is not None:
            sql += " AND attempt_id = ?"
            args.append(attempt_id)
        return self.conn.execute(sql + " ORDER BY seq", args).fetchall()

    def payload_of(self, event_id: str) -> dict[str, Any]:
        """Raw payload JSON of one event row (for facts not on the dataclass)."""
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?",
            (event_id,)).fetchone()
        self.assertTrueish(row is not None, f"event not found: {event_id}")
        return json.loads(row[0])

    def artifact_root(self) -> str:
        return verification.default_artifact_root(str(self.project))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.conn.close()


# ---------------------------------------------------------------------------
# S0 — provision, path guard, fresh DB at head, run manifest
# ---------------------------------------------------------------------------

class S0ProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = M606Run("s0")
        self.addCleanup(self.run.close)

    def test_run_root_and_db_live_outside_the_protected_repository(self):
        root = Path(os.path.realpath(str(self.run.root)))
        self.assertNotEqual(root, PROTECTED_ROOT)
        self.assertFalse(str(root).startswith(str(PROTECTED_ROOT) + os.sep))
        db_real = Path(os.path.realpath(str(self.run.db)))
        self.assertFalse(str(db_real).startswith(str(PROTECTED_ROOT) + os.sep))
        # The live runtime DB is a different file entirely and was never opened.
        live = PROTECTED_ROOT / ".teamcraft" / "teamcraft.db"
        self.assertNotEqual(db_real, Path(os.path.realpath(str(live))))

    def test_guard_aborts_when_root_resolves_inside_the_repo(self):
        with self.assertRaises(PathGuardError):
            guard_outside_protected(PROTECTED_ROOT / ".teamcraft" / "m606-x")
        with self.assertRaises(PathGuardError):
            guard_outside_protected(PROTECTED_ROOT)

    def test_database_is_fresh_and_at_schema_head(self):
        self.assertTrue(self.run.migration.fresh)
        self.assertEqual(self.run.migration.from_version, 0)
        self.assertEqual(self.run.migration.to_version, storage.SCHEMA_VERSION)
        report = storage.verify_database(self.run.db)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["schema_usable"], report)
        self.assertEqual(report["user_version"], storage.SCHEMA_VERSION)
        self.assertEqual(report["integrity"], "ok")
        # A brand-new scenario DB carries no project rows.
        self.assertEqual(
            self.run.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

    def test_run_manifest_declares_identity_and_open_gates(self):
        path = self.run.root / "evidence" / "run-manifest.json"
        self.assertTrue(path.is_file())
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["milestone"], "M6-06")
        self.assertEqual(manifest["drafted_from"], "9735d7c")
        self.assertEqual(manifest["database"]["schema_head"],
                         storage.SCHEMA_VERSION)
        self.assertTrue(manifest["database"]["fresh"])
        self.assertTrue(manifest["interpreter"]["python"])
        self.assertEqual(manifest["git"]["base_sha"], self.run.base_sha)
        # The clone is a real, non-empty repo with a configured identity:
        # delivery would refuse to fabricate one.
        self.assertEqual(manifest["git"]["clone_head"], self.run.base_sha)
        self.assertEqual(manifest["git"]["author_email"], "m606@example.invalid")
        # The legs that need consent/model/route are recorded as pending.
        self.assertEqual(manifest["step_matrix"]["S2"], "pending:G1")
        self.assertEqual(manifest["step_matrix"]["S3"], "pending:G1")
        self.assertEqual(manifest["step_matrix"]["S4"], "planned")
        self.assertEqual(manifest["step_matrix"]["S5"], "planned")
        self.assertEqual(manifest["step_matrix"]["S6"], "not-in-scope")
        for gate in ("G1", "G2", "G3", "G5", "G6"):
            self.assertIn(gate, manifest["gates_open"])


# ---------------------------------------------------------------------------
# S1 — installer into the disposable target + protocol pinning
# ---------------------------------------------------------------------------

class S1InstallerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = M606Run("s1")
        self.addCleanup(self.run.close)

    def test_dry_run_planned_without_writing_then_real_run_matched(self):
        stage = self.run.installed
        self.assertTrue(stage["requested"])
        self.assertEqual(stage["dry_run_returncode"], 0)
        self.assertEqual(stage["dry_run_writes"], 0,
                         "dry-run must write nothing into the target")
        self.assertGreater(stage["dry_run_planned"], 0)
        self.assertEqual(stage["returncode"], 0)
        # The dry-run plan and the real change set are the same set of actions.
        self.assertEqual(stage["changes"], stage["dry_run_planned"])
        self.assertGreater(stage["files_after"], stage["files_before"])

    def test_managed_copies_are_byte_identical_to_canonical(self):
        references = self.run.target_references
        canonical = (PROTECTED_ROOT / ".agents" / "skills" / "teamcraft-ceo"
                     / "references")
        self.assertEqual((references / "CEO_PROTOCOL.md").read_bytes(),
                         (canonical / "CEO_PROTOCOL.md").read_bytes())
        self.assertEqual((references / "protocol.manifest.json").read_bytes(),
                         (canonical / "protocol.manifest.json").read_bytes())
        snapshot = ceo_protocol.verify_protocol(str(references))
        self.assertEqual(snapshot.version, CANONICAL_PROTOCOL_VERSION)
        self.assertEqual(snapshot.sha256, CANONICAL_PROTOCOL_SHA256)

    def test_pinned_hash_mismatch_aborts_as_protocol_drift(self):
        references = self.run.target_references
        with self.assertRaises(ceo_protocol.ProtocolError):
            ceo_protocol.verify_protocol(
                str(references), expected_version=CANONICAL_PROTOCOL_VERSION,
                expected_sha256="0" * 64)
        with self.assertRaises(ceo_protocol.ProtocolError):
            ceo_protocol.verify_protocol(
                str(references), expected_version="9.9.9",
                expected_sha256=CANONICAL_PROTOCOL_SHA256)

    def test_installer_fails_before_write_against_user_shaped_conflict(self):
        """A user-owned CEO entry must block the install with nothing written."""
        target = self.run.root / "conflict-target"
        entry = (target / ".opencode" / "agents" / "teamcraft-ceo.md")
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("# my own CEO agent\nnot managed by TeamCraft\n",
                        encoding="utf-8")
        code, _out, err = run_installer(target, dry_run=False)
        self.assertEqual(code, 1, err)
        self.assertIn("conflicts with TeamCraft", err)
        # fail-BEFORE-write: nothing else landed in that target.
        self.assertFalse((target / "opencode.json").exists())
        self.assertFalse((target / ".agents").exists())
        self.assertEqual(
            entry.read_text(encoding="utf-8"),
            "# my own CEO agent\nnot managed by TeamCraft\n")

    def test_project_and_ceo_attempt_pin_the_exact_protocol_pair(self):
        task, attempt, _allocation, _report = self.run.script_ship()
        row = self.run.conn.execute(
            "SELECT protocol_version, protocol_hash FROM attempts WHERE id=?",
            (attempt.id,)).fetchone()
        self.assertEqual(str(row[0]), CANONICAL_PROTOCOL_VERSION)
        self.assertEqual(str(row[1]), CANONICAL_PROTOCOL_SHA256)

    def test_protocol_pair_cannot_be_half_pinned(self):
        task = domain.create_task(self.run.conn, self.run.project_row.id,
                                 "half-pin", kind="ship",
                                 ownership_json=json.dumps(["src/**"]))
        task = domain.apply_task_event(self.run.conn, task.id,
                                      "dependencies_satisfied",
                                      expected_version=task.version)
        with self.assertRaises(ValidationError):
            domain.create_attempt(self.run.conn, task.id,
                                 expected_version=task.version,
                                 protocol_version=CANONICAL_PROTOCOL_VERSION)
        self.assertIsNone(
            self.run.conn.execute(
                "SELECT 1 FROM attempts WHERE task_id=?", (task.id,)).fetchone())

    def test_empty_claim_set_is_refused_by_the_diff_gate(self):
        from teamcraft.workspaces import WorkspacePathError
        with self.assertRaises(WorkspacePathError) as ctx:
            workspaces.run_diff_gate(self.run.project, [])
        self.assertEqual(getattr(ctx.exception, "kind", None), "format")


# ---------------------------------------------------------------------------
# S4 — verification verdict comes from repo facts
# ---------------------------------------------------------------------------

class S4VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = M606Run("s4")
        self.addCleanup(self.run.close)

    def test_local_profiles_pass_from_repo_facts_and_persist_bounded_artifacts(self):
        task, attempt, allocation, report = self.run.script_ship()
        self.assertEqual(task.status, "verifying")
        self.run.approve_profiles()
        suite = self.run.run_suite(attempt.id)
        self.assertEqual(suite["verdict"], "pass", suite)
        rows = verification.list_worktree_verifications(self.run.conn, attempt.id)
        self.assertEqual(sorted(row.profile for row in rows), ["lint", "test"])
        for row in rows:
            self.assertEqual(row.status, "pass", row)
            self.assertEqual(row.exit_code, 0)
            # Artifact-root-relative only, never absolute (storage v17 rule).
            self.assertFalse(os.path.isabs(row.log_relpath or ""))
            self.assertNotIn("..", (row.log_relpath or "").split("/"))
            self.assertTrue(row.log_relpath.startswith("worktree/"))
            self.assertEqual(row.worker_report_digest, report.report_digest)
            self.assertEqual(row.allocation_id, allocation.allocation_id)
            artifact = verification.read_worktree_artifact(self.run.conn, row.id)
            self.assertEqual(artifact["log_digest"], row.log_digest)
            body = (artifact["report"].get("stdout") or "") + \
                   (artifact["report"].get("stderr") or "")
            self.assertEqual(scan_local_leaks(body), [], body)
            self.assertIn(allocation.branch, json.dumps(artifact["report"]))
            self.assertNotIn(str(self.run.root), body)

    def test_missing_verifier_profile_never_becomes_a_default_pass(self):
        task, attempt, _allocation, _report = self.run.script_ship()
        with self.assertRaises(ValidationError):
            verification.run_worktree_verifier(
                self.run.conn, attempt.id, profile="test",
                idempotency_key="no-config", lease=self.run.lease())
        self.assertEqual(
            len(verification.list_worktree_verifications(self.run.conn,
                                                        attempt.id)), 0)

    def test_self_report_that_lies_loses_to_the_repo_fact(self):
        """Worker claims success, but the owned path lacks the marker.

        The verifier inspects the repository itself, so the honest verdict is
        FAIL and delivery is refused even though the WorkerReport says done.
        """
        task, attempt, _allocation, _report = self.run.script_ship(
            marker="SOMETHING-ELSE")
        self.assertEqual(task.status, "verifying")
        self.run.approve_profiles()
        suite = self.run.run_suite(attempt.id)
        self.assertEqual(suite["verdict"], "fail", suite)
        statuses = {name: value["status"]
                    for name, value in suite["profiles"].items()}
        self.assertEqual(statuses["lint"], "fail")
        self.assertEqual(domain.get_task(self.run.conn, task.id).status,
                         "needs_human")
        # Shipping is refused: either the verifier gate itself, or the
        # attempt/task binding no longer admits a ship after the failure.
        with self.assertRaises(ValidationError) as ctx:
            delivery.record_ship_evidence(self.run.conn, attempt.id,
                                         idempotency_key="ship-lie",
                                         lease=self.run.lease(),
                                         run_git=self.run.rec)
        if isinstance(ctx.exception, delivery.DeliveryRefusal):
            self.assertEqual(ctx.exception.kind, "verifier")
        self.assertIsNone(delivery.load_commit_evidence(self.run.conn,
                                                       attempt.id))
        self.assertIsNone(delivery.load_ship_evidence(self.run.conn,
                                                     attempt.id))

    def test_unowned_change_makes_the_diff_gate_refuse_and_stands(self):
        task, attempt, allocation, _report = self.run.script_ship(
            extra_paths={"docs/stray.md": "outside ownership\n"})
        gate = workspaces.run_diff_gate(allocation.worktree_path,
                                       list(allocation.claims),
                                       run_git=self.run.rec)
        self.assertFalse(gate.ok)
        self.assertIn("unowned:docs/stray.md", gate.violations)
        with self.assertRaises(delivery.DeliveryRefusal) as ctx:
            delivery.create_local_commit(self.run.conn, attempt.id,
                                       idempotency_key="commit-unowned",
                                       lease=self.run.lease(),
                                       run_git=self.run.rec)
        self.assertEqual(ctx.exception.kind, "unowned")
        # Nothing was committed and the scene is preserved for inspection.
        self.assertEqual(git("rev-parse", "HEAD",
                             cwd=Path(allocation.worktree_path)),
                         allocation.base_sha)
        self.assertIsNone(delivery.load_commit_evidence(self.run.conn,
                                                       attempt.id))
        self.assertTrue((Path(allocation.worktree_path) / "docs"
                         / "stray.md").is_file())


# ---------------------------------------------------------------------------
# S5 — Diff Gate, local-only delivery, CLEANABLE + cleanup
# ---------------------------------------------------------------------------

class S5DeliveryCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = M606Run("s5")
        self.addCleanup(self.run.close)

    def test_diff_gate_proves_the_branch_diff_then_done_local_only(self):
        task, attempt, allocation, report = self.run.script_ship()
        self.run.approve_profiles()
        self.assertEqual(self.run.run_suite(attempt.id)["verdict"], "pass")

        gate = workspaces.run_diff_gate(allocation.worktree_path,
                                       list(allocation.claims),
                                       run_git=self.run.rec)
        self.assertTrue(gate.ok, gate.violations)
        self.assertEqual(gate.base_sha, allocation.base_sha)
        self.assertEqual(gate.branch, allocation.branch)
        self.assertEqual([entry.path for entry in gate.changes],
                         ["src/feature.py"])

        delivered = self.run.deliver(attempt.id)
        commit, ship, approval = (delivered["commit"], delivered["ship"],
                                 delivered["approval"])
        done = domain.get_task(self.run.conn, task.id)
        self.assertEqual(done.status, "done")
        self.assertEqual(approval.task_status_after, "done")

        # Exact commit identity: one child of the verified base, owned paths.
        self.assertEqual(commit.base_sha, allocation.base_sha)
        self.assertEqual(commit.parent_sha, allocation.base_sha)
        self.assertEqual(commit.branch, allocation.branch)
        self.assertEqual(commit.file_count, 1)
        payload = self.run.payload_of(commit.event_id)
        self.assertEqual([f["path"] for f in payload["files"]],
                         ["src/feature.py"])
        self.assertFalse(payload["files_truncated"])
        self.assertEqual(sorted(payload["claims"]), sorted(allocation.claims))
        self.assertEqual(payload["parent_sha"], allocation.base_sha)
        self.assertEqual(commit.worker_report_digest, report.report_digest)
        self.assertEqual(commit.commit_sha,
                         git("rev-parse", "HEAD",
                            cwd=Path(allocation.worktree_path)))
        self.assertEqual(
            git("rev-list", "--count", f"{allocation.base_sha}..{commit.commit_sha}",
                cwd=self.run.project), "1")

        # Diff digest is reproducible from raw Git bytes, not merely stored.
        raw = subprocess.run(
            [*GIT, "diff", "--binary", f"{commit.base_sha}..{commit.commit_sha}"],
            cwd=str(self.run.project), stdout=subprocess.PIPE, check=True).stdout
        recomputed = hashlib.sha256(json.dumps(
            {"diff": raw.hex()}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertEqual(commit.diff_digest, recomputed)
        self.assertEqual(ship.diff_digest, recomputed)
        self.assertEqual(ship.diff_bytes, len(raw))
        self.assertFalse(os.path.isabs(ship.diff_relpath))
        evidence = self.run.finalize({
            "S4": "executed:scripted-worker",
            "S5": "executed:scripted-worker",
        })
        self.assertEqual(evidence["final"]["pushed_refs"], [])
        self.assertEqual(evidence["evidence_class"], "scripted")
        self.assertEqual(domain.get_project(self.run.conn, task.project_id)
                         .repo_path, str(self.run.project))

    def test_origin_never_gains_a_ref_local_only(self):
        task, attempt, _allocation, _report = self.run.script_ship()
        before = self.run.origin_refs()
        self.assertEqual(before, {"refs/heads/main": self.run.base_sha})
        self.run.approve_profiles()
        self.run.run_suite(attempt.id)
        self.run.deliver(attempt.id)
        after = self.run.origin_refs()
        self.assertEqual(after, before)
        self.assertFalse([ref for ref in after if ref.startswith("refs/heads/tc/")])
        # And the audited runner never used a forbidden verb.
        used = set(self.run.rec.verbs())
        self.assertFalse(used & FORBIDDEN_VERBS,
                        f"forbidden git verbs used: {sorted(used & FORBIDDEN_VERBS)}")
        self.assertEqual(task.kind, "ship")

    def test_cleanup_only_for_cleanable_and_audits_exactly_once(self):
        task, attempt, allocation, _report = self.run.script_ship()
        self.run.approve_profiles()
        self.run.run_suite(attempt.id)
        self.run.deliver(attempt.id)

        # CLEANABLE is the gate: it must be marked first, then eligibility
        # confirms it, and only then may the workspace be freed.
        pre = delivery.evaluate_cleanup_eligibility(
            self.run.conn, attempt.id, run_git=self.run.rec)
        self.assertFalse(pre.eligible)
        self.assertIn("worktree-not-cleanable", pre.reasons)
        worktrees.mark_worktree_cleanable(self.run.conn, attempt.id,
                                         run_git=self.run.rec)
        self.assertEqual(worktrees.lifecycle_state(self.run.conn, attempt.id),
                         "cleanable")
        verdict = delivery.evaluate_cleanup_eligibility(
            self.run.conn, attempt.id, run_git=self.run.rec)
        self.assertTrue(verdict.eligible, verdict.reasons)

        result = delivery.cleanup_attempt_worktree(
            self.run.conn, attempt.id, lease=self.run.lease(),
            run_git=self.run.rec)
        self.assertEqual(result["disposition"], "removed")
        self.assertFalse(Path(allocation.worktree_path).exists())
        # The delivered ref outlives the workspace.
        cleaned = result["cleaned"]
        self.assertEqual(cleaned["branch"], allocation.branch)
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{cleaned['branch']}",
                cwd=self.run.project),
            cleaned["commit_sha"])
        self.assertEqual(cleaned["attempt_id"], attempt.id)
        self.assertEqual(cleaned["allocation_id"], allocation.allocation_id)
        self.assertEqual(len(self.run.events(worktrees.CLEANED_EVENT_KIND,
                                            attempt.id)), 1)
        replay = delivery.cleanup_attempt_worktree(
            self.run.conn, attempt.id, lease=self.run.lease(),
            run_git=self.run.rec)
        self.assertEqual(replay["disposition"], "replayed")
        self.assertEqual(len(self.run.events(worktrees.CLEANED_EVENT_KIND,
                                            attempt.id)), 1)
        removals = [c for c in self.run.rec.calls if c[:2] == ["worktree",
                                                              "remove"]]
        self.assertEqual(len(removals), 1)
        self.assertNotIn("--force", removals[0])
        self.assertNotIn("-f", removals[0])

    def test_done_needs_evidence_not_a_status_edit(self):
        task, attempt, _allocation, _report = self.run.script_ship()
        self.run.approve_profiles()
        self.run.run_suite(attempt.id)
        # Approval without durable ship evidence cannot reach DONE: an unknown
        # ship id is refused outright (no such record) rather than trusted.
        with self.assertRaises((NotFoundError, delivery.DeliveryRefusal)):
            delivery.approve_local_delivery(
                self.run.conn, "shr-unknown", decision="accept",
                commit_sha="0" * 40, branch="nope", actor="human-reviewer",
                reason="attempted shortcut", idempotency_key="shortcut",
                lease=self.run.lease(), run_git=self.run.rec)
        self.assertEqual(domain.get_task(self.run.conn, task.id).status,
                         "verifying")
        self.assertIsNone(delivery.load_commit_evidence(self.run.conn,
                                                       attempt.id))
        self.assertIsNone(delivery.load_ship_evidence(self.run.conn, attempt.id))
        self.assertIsNone(worktrees.load_cleaned_record(self.run.conn,
                                                       attempt.id))


# ---------------------------------------------------------------------------
# UNKNOWN — an unresolved Attempt never passes
# ---------------------------------------------------------------------------

class UnknownClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = M606Run("unknown")
        self.addCleanup(self.run.close)

    def _lost(self):
        task = domain.create_task(self.run.conn, self.run.project_row.id,
                                 "lost runtime", kind="ship",
                                 ownership_json=json.dumps(["src/**"]))
        task = domain.apply_task_event(self.run.conn, task.id,
                                      "dependencies_satisfied",
                                      expected_version=task.version)
        task = domain.set_execution_policy(
            self.run.conn, task.id, expected_version=task.version,
            policy={"delivery": "local_only"}, actor="ceo",
            reason="m606 UNKNOWN probe")
        attempt = domain.create_attempt(
            self.run.conn, task.id, expected_version=task.version,
            protocol_version=self.run.protocol.version,
            protocol_hash=self.run.protocol.sha256)
        domain.register_session(self.run.conn, attempt.id, "scripted",
                               native_session_id=f"scripted-{attempt.id}",
                               pane_id="pane-lost", state="working")
        domain.transition_attempt(self.run.conn, attempt.id, "start")
        domain.transition_attempt(self.run.conn, attempt.id, "activated")
        allocation, _ = worktrees.allocate_attempt_worktree(
            self.run.conn, task_id=task.id, attempt_id=attempt.id,
            repo_root=str(self.run.project), run_git=self.run.rec)
        domain.transition_attempt(self.run.conn, attempt.id, "runtime_lost")
        return (domain.get_task(self.run.conn, task.id),
                domain.get_attempt(self.run.conn, attempt.id), allocation)

    def test_attempt_unknown_is_recorded_and_occupies_the_slot(self):
        _task, attempt, _allocation = self._lost()
        self.assertEqual(attempt.status, "unknown")
        # Never auto-retry: the UNKNOWN attempt still holds the slot, and a
        # second dispatch on the same Task is refused with nothing written.
        with self.assertRaises((domain.AttemptSlotOccupiedError,
                               IllegalTransitionError)) as ctx:
            domain.create_attempt(self.run.conn, attempt.task_id,
                                 expected_version=domain.get_task(
                                     self.run.conn, attempt.task_id).version)
        self.assertEqual(
            self.run.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ?",
                (attempt.task_id,)).fetchone()[0], 1)
        occupier = domain.occupying_attempt(self.run.conn, attempt.task_id)
        self.assertIsNotNone(occupier)
        self.assertEqual(occupier.id, attempt.id)
        # The refusal is a domain refusal, not a silent no-op.
        self.assertIsInstance(ctx.exception, DomainError)

    def test_unknown_attempt_cannot_report_verify_or_clean(self):
        task, attempt, allocation = self._lost()
        brief = briefs.create_attempt_brief(self.run.conn, attempt.id,
                                           content="late brief after UNKNOWN")
        with self.assertRaises(ValidationError):
            worker_reports.record_worker_report(self.run.conn, {
                "schema_version": 1, "task_id": task.id,
                "attempt_id": attempt.id, "task_version": brief.task_version,
                "brief_digest": brief.brief_digest, "report_kind": "final",
                "outcome": "completed", "milestone_id": "final",
                "summary": "claimed completion while UNKNOWN",
                "artifact_digest": None})
        with self.assertRaises(IllegalTransitionError) as ctx:
            domain.apply_task_event(self.run.conn, task.id,
                                  "worker_reported_done",
                                  expected_version=task.version)
        self.assertEqual(getattr(ctx.exception, "reason_code", None),
                         "EXECUTION_UNRESOLVED_RECONCILE_REQUIRED")
        self.run.approve_profiles()
        with self.assertRaises((ValidationError, IllegalTransitionError)):
            verification.run_worktree_verifier(
                self.run.conn, attempt.id, profile="lint",
                idempotency_key="verify-unknown", lease=self.run.lease())
        verdict = delivery.evaluate_cleanup_eligibility(
            self.run.conn, attempt.id, run_git=self.run.rec)
        self.assertFalse(verdict.eligible)
        self.assertIn("attempt-unknown", verdict.reasons)
        with self.assertRaises(delivery.DeliveryRefusal):
            delivery.cleanup_attempt_worktree(self.run.conn, attempt.id,
                                            lease=self.run.lease(),
                                            run_git=self.run.rec)
        # The scene is preserved and the refusal is auditable.
        self.assertTrue(Path(allocation.worktree_path).exists())
        self.assertEqual(
            len(self.run.events(delivery.CLEANUP_REFUSED_EVENT_KIND,
                               attempt.id)), 1)
        self.assertIsNone(worktrees.load_cleaned_record(self.run.conn,
                                                       attempt.id))

    def test_reconcile_is_the_only_door_and_it_closes_honestly(self):
        _task, attempt, _allocation = self._lost()
        settled = domain.transition_attempt(self.run.conn, attempt.id,
                                           "reconciled_failed")
        self.assertEqual(settled.status, "failed")
        verdict = delivery.evaluate_cleanup_eligibility(
            self.run.conn, attempt.id, run_git=self.run.rec)
        self.assertFalse(verdict.eligible)
        # A settled-but-failed Attempt still cannot deliver: no DONE shortcut.
        self.assertIn("delivery-task-running", verdict.reasons)

    def test_undecidable_merge_probe_is_unknown_not_clean(self):
        task, attempt, allocation, _report = self.run.script_ship()
        # Real divergence so the in-memory merge probe is the only answer.
        worktree = Path(allocation.worktree_path)
        (worktree / "src" / "feature.py").write_text(
            f"{FEATURE_MARKER}: diverged\n", encoding="utf-8")
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "-m", "branch work", cwd=worktree)
        (self.run.project / "docs" / "later.md").write_text("base moved\n",
                                                            encoding="utf-8")
        git("add", "-A", cwd=self.run.project)
        git("commit", "-q", "-m", "base advanced", cwd=self.run.project)

        failing = FailingMergeTreeRunner()
        status = delivery.check_integration_state(self.run.conn, attempt.id,
                                                 run_git=failing)
        self.assertTrue(status.base_drift)
        self.assertEqual(status.merge_status, worktrees.MERGE_STATUS_UNKNOWN)
        self.assertIn("undecidable", " | ".join(status.problems))
        self.assertTrue(status.integration_required)
        _s, created_task, created = delivery.ensure_integration_task(
            self.run.conn, attempt.id, run_git=failing)
        self.assertTrue(created)
        assert created_task is not None
        self.assertEqual(created_task.kind, "ship")
        self.assertFalse(set(FORBIDDEN_VERBS) & set(failing.verbs()))
        self.assertEqual(task.status, "verifying")


if __name__ == "__main__":
    unittest.main()
