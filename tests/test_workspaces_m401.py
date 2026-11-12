"""M4-01 Workspace paths and Diff Gate tests (M0 review §9.4 M4-01).

Covers the acceptance matrix:
* dangerous claim/path rejection (absolute, ``..``, escapes, control chars,
  globs beyond trailing ``/**``, ``.git`` paths, unsafe case collisions);
* symlink escape refusal and safe in-root symlinks;
* case-alias behavior where the filesystem supports it (macOS-style volumes);
* quoted / newline / non-UTF-8 path names through NUL-safe plumbing;
* staged, unstaged, untracked, deleted, and rename source+target collection;
* exact and trailing ``/**`` ownership with component-boundary semantics;
* clean/unchanged repositories passing trivially;
* empty ownership claim sets failing closed with ``WorkspacePathError``
  (FINDING-M401-01: not ``TypeError``) before any Git is consulted;
* log-safe presentation escaping (FINDING-M401-02): ``Cc`` control characters
  such as a newline inside a collected unowned path render as single-line
  backslash escapes in violation tags and digest inputs, while ordinary Unicode
  stays readable, raw non-UTF-8 bytes keep their ``\\xNN`` spelling, and NUL
  delimited collection semantics stay byte-exact;
* fail-closed Git state (non-repo, sub-root, no HEAD, unmerged, git missing);
* durable immutable base-SHA evidence bound to Task/Attempt with
  version/immutability checks, replayed idempotently (schema v16).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unicodedata
import unittest
from dataclasses import replace
from pathlib import Path

from teamcraft import domain, events, storage, workspaces
from teamcraft.workspaces import (
    BASE_EVIDENCE_KIND,
    ChangeEntry,
    GitStateError,
    WorkspacePathError,
    capture_base_evidence,
    claim_covers,
    collect_workspace_state,
    evaluate_ownership,
    load_base_evidence,
    normalize_claim,
    normalize_claims,
    require_base_evidence,
    resolve_actual_path,
    run_diff_gate,
    verify_base_evidence,
    verify_repo_root,
)

GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false", "-c", "user.name=T",
    "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run([*GIT, *args], cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def init_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=repo)
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo


def _volume_is_case_insensitive(directory: Path) -> bool:
    probe = directory / "TcCaseProbe.txt"
    other = directory / "tccaseprobe.txt"
    probe.write_text("x", encoding="utf-8")
    try:
        return other.exists() and other.read_text(encoding="utf-8") == "x"
    finally:
        probe.unlink(missing_ok=True)


class ClaimGrammarTests(unittest.TestCase):
    """Pure claim canonicalization — every dangerous shape must be refused."""

    def assert_refused(self, claim, kind=None):
        with self.assertRaises(WorkspacePathError) as ctx:
            normalize_claim(claim)
        if kind is not None:
            self.assertEqual(ctx.exception.kind, kind,
                             f"{claim!r} -> {ctx.exception}")
        return ctx.exception

    def test_dangerous_claims_are_rejected(self):
        dangerous = [
            "/etc/passwd", "//server/share", "/absolute/file.py",
            "..", "../escape", "../../x", "a/../../x", "a/../b",
            "src/../src/app.py", "a//b", "a/./b", "./x",
            "back\\slash", "C:\\Users\\x", "*", "**", "a/**b", "a/**/**",
            "src/*.py", "a?b", "[abc].py", "src/{a,b}.py",
            "", "   ", ".", "./", "\x00nul", "evil\nname", "tab\there",
            ".git", ".git/config", "hooks/.git", "**/",
        ]
        for claim in dangerous:
            self.assert_refused(claim)

    def test_git_paths_are_never_ownable(self):
        for claim in (".git/**", "sub/.git/hooks", ".git/config"):
            exc = self.assert_refused(claim, kind="gitdir")
            self.assertIn(".git", str(exc))

    def test_traversal_claims_report_traversal_kind(self):
        self.assert_refused("a/../../b", kind="traversal")
        self.assert_refused("/abs/path", kind="absolute")

    def test_legal_claims_still_pass_and_canonicalize(self):
        # Existing legitimate Ownership shapes (dispatch-time grammar) must
        # keep passing unchanged.
        legal = {
            "teamcraft/workspaces.py": "teamcraft/workspaces.py",
            "teamcraft/storage.py": "teamcraft/storage.py",
            "tests/test_workspaces_m401.py": "tests/test_workspaces_m401.py",
            "doc/**": "doc/**",
            ".github/workflows": ".github/workflows",
            ".agents/skills/teamcraft/SKILL.md": ".agents/skills/teamcraft/SKILL.md",
            "src/deep/nested/file.js": "src/deep/nested/file.js",
            "dir with space/file.txt": "dir with space/file.txt",
            "文档/指南.md": "文档/指南.md",
            "dir/**": "dir/**",
        }
        for claim, canonical in legal.items():
            parsed = normalize_claim(claim)
            self.assertEqual(parsed.canonical, canonical)
            self.assertFalse(parsed.path.endswith("/"))

    def test_non_string_claim_rejected(self):
        for bad in (None, 3, ["src"], b"bytes"):
            self.assert_refused(bad, kind="format")

    def test_duplicate_claims_dedupe_and_case_collisions_refused(self):
        claims = normalize_claims(["src/**", "src/**", "README.md"])
        self.assertEqual([c.canonical for c in claims], ["src/**", "README.md"])
        with self.assertRaises(WorkspacePathError) as ctx:
            normalize_claims(["docs/**", "Docs/**"])
        self.assertEqual(ctx.exception.kind, "collision")
        with self.assertRaises(WorkspacePathError):
            normalize_claims(["README.md", "readme.md"])


class ClaimCoverageTests(unittest.TestCase):
    def test_exact_and_recursive_coverage(self):
        exact = normalize_claim("src/app.py")
        recursive = normalize_claim("src/**")
        self.assertTrue(claim_covers(exact, "src/app.py"))
        self.assertFalse(claim_covers(exact, "src/app2.py"))
        self.assertFalse(claim_covers(exact, "src"))
        self.assertTrue(claim_covers(recursive, "src/app.py"))
        self.assertTrue(claim_covers(recursive, "src/deep/nest.js"))
        self.assertTrue(claim_covers(recursive, "src"))  # directory-level change
        self.assertFalse(claim_covers(recursive, "src2/x"))   # boundary trap
        self.assertFalse(claim_covers(recursive, "srcx"))
        self.assertFalse(claim_covers(recursive, "other/src/app.py"))

    def test_evaluate_ownership_tags_unowned_and_case_aliases(self):
        claims = normalize_claims(["src/**", "README.md"])
        violations = evaluate_ownership(
            ["README.md", "src/app.py", "src/deep/x.py", "stray.txt"], claims)
        self.assertEqual(violations, ("unowned:stray.txt",))
        violations = evaluate_ownership(["Docs/readme.md"], claims)
        self.assertEqual(violations, ("unowned:Docs/readme.md",))
        violations = evaluate_ownership(["readme.md"], claims)
        self.assertEqual(violations, ("case-alias:readme.md",))
        # explicit opt-in stops case folding from being a violation
        violations = evaluate_ownership(["readme.md"], claims,
                                       allow_case_insensitive=True)
        self.assertEqual(violations, ())
        self.assertEqual(evaluate_ownership([], claims), ())


class RepoRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-root-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_root_must_exist_and_be_absolute(self):
        with self.assertRaises(WorkspacePathError):
            verify_repo_root(str(self.base / "nope"))
        with self.assertRaises(WorkspacePathError):
            verify_repo_root("relative/path")
        with self.assertRaises(WorkspacePathError):
            verify_repo_root("   ")

    def test_git_dir_root_refused(self):
        repo = init_repo(self.base)
        with self.assertRaises(WorkspacePathError) as ctx:
            verify_repo_root(str(repo / ".git"))
        self.assertEqual(ctx.exception.kind, "gitdir")


class ResolveActualPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-resolve-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = init_repo(self.base)
        self.root = verify_repo_root(str(self.repo))

    def test_missing_leaf_is_allowed_inside_root(self):
        actual = resolve_actual_path(self.root, "src/does-not-exist.py")
        self.assertFalse(actual.exists)
        self.assertTrue(actual.resolved.startswith(self.root + "/"))

    def test_symlink_escape_is_refused(self):
        outside = self.base / "outside-target"
        outside.mkdir()
        (outside / "secret").write_text("s", encoding="utf-8")
        (self.repo / "escape").symlink_to(outside)
        with self.assertRaises(WorkspacePathError) as ctx:
            resolve_actual_path(self.root, "escape/secret")
        self.assertEqual(ctx.exception.kind, "symlink-escape")
        # the symlink entry itself also escapes
        with self.assertRaises(WorkspacePathError):
            resolve_actual_path(self.root, "escape")

    def test_symlink_chain_escape_is_refused(self):
        outside = self.base / "outside2"
        outside.mkdir()
        (self.repo / "link-a").symlink_to(outside)
        (self.repo / "link-b").symlink_to("link-a")
        with self.assertRaises(WorkspacePathError) as ctx:
            resolve_actual_path(self.root, "link-b/file")
        self.assertEqual(ctx.exception.kind, "symlink-escape")

    def test_parent_pointing_symlink_refused(self):
        (self.repo / "up").symlink_to("..")
        with self.assertRaises(WorkspacePathError) as ctx:
            resolve_actual_path(self.root, "up/elsewhere")
        self.assertEqual(ctx.exception.kind, "symlink-escape")

    def test_internal_symlink_is_allowed(self):
        (self.repo / "src" / "real.txt").write_text("r", encoding="utf-8")
        (self.repo / "link").symlink_to("src")
        actual = resolve_actual_path(self.root, "link/real.txt")
        self.assertTrue(actual.exists)
        self.assertEqual(Path(actual.resolved),
                         Path(os.path.realpath(str(self.repo / "src" / "real.txt"))))
        self.assertTrue(actual.resolved.startswith(self.root))

    def test_dangerous_relative_forms_refused(self):
        for bad in ("../x", "a/../../x", "/abs", "back\\slash", "", "a//b",
                   ".", "..", ".git/hooks"):
            with self.assertRaises(WorkspacePathError, msg=repr(bad)):
                resolve_actual_path(self.root, bad)

    def test_case_alias_detected_where_supported(self):
        caseless = _volume_is_case_insensitive(self.base)
        if not caseless:
            self.skipTest("filesystem is case-sensitive; case-alias "
                         "detection not exercisable here")
        # The repo ships a lowercase ``docs`` directory; a differently-cased
        # reference names the same territory through a case alias.
        with self.assertRaises(WorkspacePathError) as ctx:
            resolve_actual_path(self.root, "DOCS")
        self.assertEqual(ctx.exception.kind, "case-alias")
        opt_in = resolve_actual_path(self.root, "DOCS",
                                    allow_case_insensitive=True)
        self.assertTrue(opt_in.exists)
        self.assertTrue(opt_in.case_alias)
        # exact spelling stays clean even on a case-insensitive volume
        clean = resolve_actual_path(self.root, "docs")
        self.assertFalse(clean.case_alias)


class FakeRunnerMixin:
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def fake_runner(self, root: str, canned: dict | None = None):
        defaults = {
            ("rev-parse", "--show-toplevel"): (root + "\n").encode(),
            ("rev-parse", "--is-bare-repository"): b"false\n",
            ("rev-parse", "--verify"): (self.SHA + "\n").encode(),
            ("symbolic-ref", "--short"): (b"main\n"),
            ("status", "--porcelain=v1"): b"",
            ("diff", "--cached"): b"",
            ("diff", "--name-status"): b"",
            ("ls-files", "--others"): b"",
        }
        table = {**defaults, **(canned or {})}

        def runner(args, cwd):
            key = tuple(args[:2])
            if key not in table:
                raise GitStateError(f"unexpected git call: {args}")
            value = table[key]
            if callable(value):
                value = value(args, cwd)
            if isinstance(value, Exception):
                raise value
            return value
        return runner


class PlumbingParseTests(FakeRunnerMixin, unittest.TestCase):
    """NUL-safe parsing of status/diff plumbing, independent of git itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-parse-")
        self.addCleanup(self._tmp.cleanup)
        self.root = str(Path(self._tmp.name).resolve())
        self.repo = init_repo(Path(self._tmp.name))
        self.root = verify_repo_root(str(self.repo))

    def collect(self, canned: dict | None = None):
        return collect_workspace_state(self.root,
                                     run_git=self.fake_runner(self.root, canned))

    def test_status_z_covers_all_entry_kinds(self):
        status = b"\0".join([
            b"M  README.md",
            b" M src/app.py",
            b"A  src/new.py",
            b"D  docs/gone.md",
            b"?? untracked.txt",
            b"R  b.py", b"a.py",
            b"",
        ])
        state = self.collect({("status", "--porcelain=v1"): status})
        by_path = {entry.path: entry for entry in state.changes}
        self.assertEqual(set(by_path), {"README.md", "src/app.py", "src/new.py",
                                       "docs/gone.md", "untracked.txt", "b.py", "a.py"})
        self.assertTrue(by_path["README.md"].staged)
        self.assertTrue(by_path["src/app.py"].unstaged)
        self.assertFalse(by_path["src/app.py"].staged)
        self.assertTrue(by_path["src/new.py"].added and by_path["src/new.py"].staged)
        self.assertTrue(by_path["docs/gone.md"].deleted and by_path["docs/gone.md"].staged)
        self.assertTrue(by_path["untracked.txt"].untracked)
        self.assertTrue(by_path["b.py"].renamed and by_path["b.py"].rename_source == "a.py")
        self.assertTrue(by_path["a.py"].renamed and by_path["a.py"].deleted
                        and by_path["a.py"].rename_target == "b.py")
        self.assertEqual(state.base_sha, self.SHA)
        self.assertEqual(state.branch, "main")

    def test_unmerged_state_fails_closed(self):
        status = b"UU conflicting.txt\x00"
        with self.assertRaises(GitStateError):
            self.collect({("status", "--porcelain=v1"): status})

    def test_quoted_and_newline_and_raw_bytes_paths_survive(self):
        status = b"\0".join([
            b'A  weird/"quoted".txt',
            b"A  li\nne\tname.txt",
            b"A  bad\xffname.bin",
            b"A  sp ace.txt",
            b"",
        ])
        state = self.collect({("status", "--porcelain=v1"): status})
        paths = {entry.path for entry in state.changes}
        self.assertEqual(paths, {'weird/"quoted".txt', "li\nne\tname.txt",
                                "bad\udcffname.bin", "sp ace.txt"})
        # no porcelain quoting artifacts: no octal escapes, no added quotes
        for path in paths:
            self.assertNotIn("\\3", path)
            self.assertNotIn("\\", path)

    def test_diff_z_rename_endpoints_merged_with_status(self):
        status = b"R  b.py\x00a.py\x00"
        cached = b"R100\x00a.py\x00b.py\x00"
        state = self.collect({("status", "--porcelain=v1"): status,
                              ("diff", "--cached"): cached})
        by_path = {entry.path: entry for entry in state.changes}
        self.assertEqual(by_path["a.py"].rename_target, "b.py")
        self.assertEqual(by_path["b.py"].rename_source, "a.py")
        self.assertTrue(by_path["a.py"].staged and by_path["b.py"].staged)

    def test_detached_head_reports_no_branch(self):
        state = self.collect({
            ("symbolic-ref", "--short"): GitStateError("detached"),
        })
        self.assertIsNone(state.branch)
        self.assertEqual(state.base_sha, self.SHA)

    def test_runner_failure_fails_closed(self):
        with self.assertRaises(GitStateError):
            self.collect({("rev-parse", "--verify"): GitStateError("boom")})

    def test_malformed_entries_refused(self):
        with self.assertRaises(GitStateError):
            self.collect({("status", "--porcelain=v1"): b"XY\x00\x00"})   # no path
        with self.assertRaises(GitStateError):
            self.collect({("status", "--porcelain=v1"): b"R  only-new\x00"})  # no source
        with self.assertRaises(GitStateError):
            self.collect({("diff", "--cached"): b"M\x00\x00"})
        with self.assertRaises(GitStateError):
            self.collect({("ls-files", "--others"): b"../escape\x00"})


class GitIntegrationTests(unittest.TestCase):
    """Real-git behavior: complete collection and gate verdicts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-git-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = init_repo(self.base)
        self.head = git("rev-parse", "HEAD", cwd=self.repo)

    def test_clean_repo_passes_with_empty_change_set(self):
        result = run_diff_gate(str(self.repo), ["src/**", "README.md", "docs/**"])
        self.assertTrue(result.ok)
        self.assertEqual(result.changes, ())
        self.assertEqual(result.violations, ())
        self.assertEqual(result.base_sha, self.head)
        self.assertEqual(result.branch, "main")

    def test_staged_unstaged_untracked_deleted_all_collected(self):
        (self.repo / "untracked.txt").write_text("u\n", encoding="utf-8")
        (self.repo / "src" / "app.py").write_text("changed\n", encoding="utf-8")
        os.remove(self.repo / "docs" / "guide.md")
        (self.repo / "staged_new.txt").write_text("s\n", encoding="utf-8")
        git("add", "staged_new.txt", cwd=self.repo)
        state = collect_workspace_state(str(self.repo))
        by_path = {entry.path: entry for entry in state.changes}
        self.assertEqual(set(by_path), {"untracked.txt", "src/app.py",
                                       "docs/guide.md", "staged_new.txt"})
        self.assertTrue(by_path["untracked.txt"].untracked)
        self.assertTrue(by_path["src/app.py"].unstaged)
        self.assertTrue(by_path["docs/guide.md"].deleted and
                        by_path["docs/guide.md"].unstaged)
        self.assertTrue(by_path["staged_new.txt"].staged and
                        by_path["staged_new.txt"].added)

    def test_staged_rename_both_endpoints_in_gate(self):
        git("mv", "src/app.py", "src/core.py", cwd=self.repo)
        result = run_diff_gate(str(self.repo), ["src/core.py"])
        paths = {entry.path for entry in result.changes}
        self.assertEqual(paths, {"src/core.py", "src/app.py"})
        self.assertFalse(result.ok)  # rename SOURCE sits outside ownership
        self.assertIn("unowned:src/app.py", result.violations)
        both = run_diff_gate(str(self.repo), ["src/core.py", "src/app.py"])
        self.assertTrue(both.ok)

    def test_worktree_rename_endpoints_via_delete_and_untracked(self):
        os.rename(self.repo / "src" / "app.py", self.repo / "src" / "moved.py")
        result = run_diff_gate(str(self.repo), ["src/**"])
        paths = {entry.path for entry in result.changes}
        self.assertEqual(paths, {"src/app.py", "src/moved.py"})
        self.assertTrue(result.ok)
        strict = run_diff_gate(str(self.repo), ["src/moved.py"])
        self.assertIn("unowned:src/app.py", strict.violations)

    def test_weird_filenames_round_trip_exact(self):
        names = ['"quoted".txt', "li\nne.txt", "sp ace.txt",
                 "ünïcode-文件.txt"]
        weird = self.repo / "weird"
        weird.mkdir()
        for name in names:
            (weird / name).write_text("x\n", encoding="utf-8")
        raw_name = os.fsdecode(b"raw\xff\xff.bin")
        try:
            with open(os.fsencode(weird / raw_name), "wb") as handle:
                handle.write(b"x\n")
        except OSError:
            # Some platforms (macOS/APFS) refuse to CREATE names that are not
            # valid UTF-8; their NUL-safe round-trip is pinned portably by
            # PlumbingParseTests.test_quoted_and_newline_and_raw_bytes_paths_survive.
            raw_name = None
        if raw_name is not None:
            names.append(raw_name)
        git("add", "-A", cwd=self.repo)
        state = collect_workspace_state(str(self.repo))
        collected = {entry.path for entry in state.changes}
        self.assertEqual(collected, {f"weird/{name}" for name in names})
        result = run_diff_gate(str(self.repo), ["weird/**"])
        self.assertTrue(result.ok, msg=str(result.violations))

    def test_gate_blocks_untracked_addition_outside_ownership(self):
        (self.repo / "sneaky.py").write_text("x", encoding="utf-8")
        result = run_diff_gate(str(self.repo), ["src/**", "README.md"])
        self.assertFalse(result.ok)
        self.assertEqual(result.violations, ("unowned:sneaky.py",))

    def test_ignored_files_are_excluded(self):
        (self.repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        git("add", ".gitignore", cwd=self.repo)
        git("commit", "-q", "-m", "ignore", cwd=self.repo)
        (self.repo / "runtime.log").write_text("noise\n", encoding="utf-8")
        state = collect_workspace_state(str(self.repo))
        paths = {entry.path for entry in state.changes}
        self.assertEqual(paths, set())

    def test_symlink_into_ownership_still_escape_checked(self):
        outside = self.base / "escape-target"
        outside.mkdir()
        (self.repo / "src" / "evil").symlink_to(outside)
        result = run_diff_gate(str(self.repo), ["src/**"])
        self.assertFalse(result.ok)
        self.assertTrue(any(tag.startswith("symlink-escape:src/evil")
                            for tag in result.violations), str(result.violations))

    def test_case_variant_violation_where_volume_is_case_insensitive(self):
        if not _volume_is_case_insensitive(self.base):
            self.skipTest("filesystem is case-sensitive here")
        (self.repo / "docs" / "extra.md").write_text("e\n", encoding="utf-8")
        result = run_diff_gate(str(self.repo), ["Docs/**"])
        self.assertFalse(result.ok)
        self.assertTrue(any(tag.startswith("case-alias:") for tag in result.violations),
                        str(result.violations))
        honest = run_diff_gate(str(self.repo), ["docs/**"])
        self.assertTrue(honest.ok, msg=str(honest.violations))

    def test_nested_repository_surface_collected_as_directory(self):
        nest = self.repo / "nest"
        nest.mkdir()
        subprocess.run([*GIT, "init", "-q"], cwd=str(nest), check=True)
        (nest / "inside.txt").write_text("i", encoding="utf-8")
        state = collect_workspace_state(str(self.repo))
        paths = {entry.path for entry in state.changes}
        self.assertEqual(paths, {"nest"})
        result = run_diff_gate(str(self.repo), ["nest/**"])
        self.assertTrue(result.ok)

    def test_non_repository_root_fails_closed(self):
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(GitStateError):
            collect_workspace_state(str(plain))

    def test_subdirectory_is_not_a_valid_root(self):
        with self.assertRaises(GitStateError) as ctx:
            collect_workspace_state(str(self.repo / "src"))
        self.assertIn("top level", str(ctx.exception))

    def test_symlinked_project_root_is_accepted(self):
        alias = self.base / "alias"
        alias.symlink_to(self.repo)
        state = collect_workspace_state(str(alias))
        self.assertEqual(state.base_sha, self.head)

    def test_empty_repository_without_head_fails_closed(self):
        fresh = self.base / "fresh"
        fresh.mkdir()
        git("init", "-q", cwd=fresh)
        with self.assertRaises(GitStateError):
            collect_workspace_state(str(fresh))

    def test_unmerged_conflict_fails_closed(self):
        branch_file = self.repo / "conflict.txt"
        branch_file.write_text("base\n", encoding="utf-8")
        git("add", "conflict.txt", cwd=self.repo)
        git("commit", "-q", "-m", "conflict base", cwd=self.repo)
        git("checkout", "-q", "-b", "side", cwd=self.repo)
        branch_file.write_text("side\n", encoding="utf-8")
        git("commit", "-q", "-am", "side change", cwd=self.repo)
        git("checkout", "-q", "main", cwd=self.repo)
        branch_file.write_text("main\n", encoding="utf-8")
        git("commit", "-q", "-am", "main change", cwd=self.repo)
        proc = subprocess.run([*GIT, "merge", "side"], cwd=str(self.repo),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             check=False)
        self.assertNotEqual(proc.returncode, 0, "expected a conflicted merge")
        with self.assertRaises(GitStateError) as ctx:
            collect_workspace_state(str(self.repo))
        self.assertIn("unmerged", str(ctx.exception))

    def test_missing_git_binary_fails_closed(self):
        env = dict(os.environ, PATH="")
        saved = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            with self.assertRaises(GitStateError):
                collect_workspace_state(str(self.repo))
        finally:
            os.environ.clear()
            os.environ.update(saved)


class EmptyClaimsGateTests(unittest.TestCase):
    """FINDING-M401-01: ``claims=[]`` must fail closed, not raise TypeError.

    ``run_diff_gate(root, claims=[])`` used to call ``WorkspacePathError`` with
    three positional arguments (a constructor that takes ``(kind, message)``),
    so the intended fail-closed refusal itself blew up with ``TypeError``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-empty-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = init_repo(self.base)

    def test_empty_claim_set_raises_workspace_path_error_not_type_error(self):
        for empty in ([], (), frozenset(), (c for c in [])):
            with self.subTest(claims=type(empty).__name__):
                # Guards the defect directly: the wrong-arity bug surfaced as a
                # TypeError *during* the raise, so it would fail right here.
                with self.assertRaises(WorkspacePathError) as ctx:
                    run_diff_gate(str(self.repo), empty)
                exc = ctx.exception
                self.assertEqual(exc.kind, "format")
                self.assertIn("at least one ownership claim is required", str(exc))
                self.assertIsInstance(exc, workspaces.ValidationError)

    def test_empty_claims_fail_before_git_and_persist_nothing(self):
        # Fail-closed happens up front: even a root that cannot be read as a
        # repository reports the claim error, and no Git is consulted at all.
        plain = self.base / "plain"
        plain.mkdir()

        def runner(args, cwd):  # pragma: no cover - must never be called
            raise AssertionError(f"git must not run for an empty claim set: {args}")

        for root in (str(self.repo), str(plain)):
            with self.subTest(root=Path(root).name):
                with self.assertRaises(WorkspacePathError) as ctx:
                    run_diff_gate(root, [], run_git=runner)
                self.assertEqual(ctx.exception.kind, "format")
                with self.assertRaises(WorkspacePathError):
                    run_diff_gate(root, (), run_git=runner)


class EvidenceFixtureMixin(FakeRunnerMixin):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-evidence-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.db = self.base / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.repo = init_repo(self.base)
        self.head = git("rev-parse", "HEAD", cwd=self.repo)
        self.project_id = domain.create_project(self.conn, "proj", str(self.repo)).id
        self.task_id = domain.create_task(
            self.conn, self.project_id, "M4-01 work",
            ownership_json=json.dumps(["src/**", "README.md"])).id

    def add_attempt(self, attempt_id="att-1", task_id=None):
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO attempts (id, task_id, attempt_no, status)"
                " VALUES (?, ?, ?, ?)",
                (attempt_id, task_id or self.task_id, 1, "created"))

    def capture(self, attempt_id="att-1", **kwargs):
        kwargs.setdefault("repo_root", str(self.repo))
        return capture_base_evidence(self.conn, task_id=self.task_id,
                                    attempt_id=attempt_id, **kwargs)

    def raw(self):
        # External (bypassing the gated connection) fixture surgery; FKs stay
        # at the sqlite default (off) so deliberate dangling references are
        # constructible for drift tests.
        return sqlite3.connect(str(self.db))


class BaseEvidenceTests(EvidenceFixtureMixin, unittest.TestCase):
    def test_capture_binds_base_sha_to_task_and_attempt(self):
        self.add_attempt()
        evidence, created = self.capture()
        self.assertTrue(created)
        self.assertEqual(evidence.base_sha, self.head)
        self.assertEqual(evidence.task_id, self.task_id)
        self.assertEqual(evidence.attempt_id, "att-1")
        self.assertEqual(evidence.project_id, self.project_id)
        self.assertEqual(evidence.task_version, 0)
        self.assertEqual(list(evidence.claims), ["src/**", "README.md"])
        self.assertEqual(evidence.branch, "main")
        row = self.conn.execute(
            "SELECT kind, project_id, task_id, attempt_id, payload_json"
            " FROM events WHERE seq = ?", (evidence.seq,)).fetchone()
        self.assertEqual(row[0], BASE_EVIDENCE_KIND)
        self.assertEqual((row[1], row[2], row[3]),
                         (self.project_id, self.task_id, "att-1"))
        payload = json.loads(row[4])
        self.assertEqual(payload["base_sha"], self.head)
        self.assertEqual(payload["repo_root"], verify_repo_root(str(self.repo)))
        self.assertEqual(payload["schema_version"],
                         workspaces.WORKSPACES_CONTRACT_VERSION)

    def test_replay_is_idempotent_and_immutable(self):
        self.add_attempt()
        first, created = self.capture()
        self.assertTrue(created)
        replay, created_again = self.capture()
        self.assertFalse(created_again)
        self.assertEqual(replay.seq, first.seq)
        self.assertEqual(replay.evidence_digest, first.evidence_digest)
        # HEAD moves: a different base SHA must never replace the record.
        (self.repo / "src" / "later.py").write_text("l\n", encoding="utf-8")
        git("add", "src/later.py", cwd=self.repo)
        git("commit", "-q", "-m", "advance", cwd=self.repo)
        with self.assertRaises(workspaces.ConflictError):
            self.capture()
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?",
            (BASE_EVIDENCE_KIND,)).fetchone()[0]
        self.assertEqual(rows, 1)
        still = load_base_evidence(self.conn, "att-1")
        self.assertEqual(still.base_sha, first.base_sha)

    def test_events_triggers_block_raw_update_and_delete(self):
        self.add_attempt()
        evidence, _ = self.capture()
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "UPDATE events SET payload_json='{}' WHERE seq=?",
                    (evidence.seq,))
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute("DELETE FROM events WHERE seq=?",
                                (evidence.seq,))
        self.assertIsNotNone(load_base_evidence(self.conn, "att-1"))

    def test_unique_index_blocks_a_second_evidence_row(self):
        self.add_attempt()
        _evidence, _ = self.capture()
        forged = ('{"schema_version":1,"base_sha":"%s","branch":null,'
                  '"claims":["src/**"],"claims_digest":"%s",'
                  '"initial_change_count":0,"initial_changes_digest":"%s",'
                  '"project_id":"%s","repo_root":"%s","task_id":"%s",'
                  '"task_version":0,"attempt_id":"att-1"}'
                  % (self.head, "0" * 64, "0" * 64, self.project_id,
                     verify_repo_root(str(self.repo)), self.task_id))
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO events (event_id, kind, project_id, task_id,"
                    " attempt_id, payload_json, occurred_at)"
                    " VALUES ('evt-forged', ?, ?, ?, ?, ?, '2026-11-12T00:00:00+00:00')",
                    (BASE_EVIDENCE_KIND, self.project_id, self.task_id, "att-1", forged))

    def test_binding_trigger_refuses_unbound_or_incomplete_evidence(self):
        payload = json.dumps({"schema_version": 1, "base_sha": self.head,
                             "repo_root": str(self.repo), "task_version": 0})
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO events (event_id, kind, project_id, task_id,"
                    " attempt_id, payload_json, occurred_at)"
                    " VALUES ('evt-x1', ?, 'p', 't', NULL, ?, '2026-11-12T00:00:00+00:00')",
                    (BASE_EVIDENCE_KIND, payload))
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO events (event_id, kind, project_id, task_id,"
                    " attempt_id, payload_json, occurred_at)"
                    " VALUES ('evt-x2', ?, 'p', 't', 'a', '{}',"
                    " '2026-11-12T00:00:00+00:00')", (BASE_EVIDENCE_KIND,))

    def test_legacy_kinds_are_unaffected_by_evidence_gates(self):
        with storage.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO events (event_id, kind, project_id, task_id,"
                " attempt_id, payload_json, occurred_at)"
                " VALUES ('evt-legacy', 'some.other.kind', NULL, NULL, NULL,"
                " '{}', '2026-11-12T00:00:00+00:00')")

    def test_wrong_bindings_and_bad_claims_fail_closed(self):
        self.add_attempt("att-1")
        self.add_attempt("att-2", task_id=domain.create_task(
            self.conn, self.project_id, "other",
            ownership_json=json.dumps(["docs/**"])).id)
        with self.assertRaises(domain.ValidationError):
            capture_base_evidence(self.conn, task_id=self.task_id,
                                 attempt_id="att-2", repo_root=str(self.repo))
        with self.assertRaises(domain.NotFoundError):
            capture_base_evidence(self.conn, task_id=self.task_id,
                                 attempt_id="att-missing", repo_root=str(self.repo))
        with self.assertRaises(workspaces.ValidationError):
            self.capture(claims=["../escape"])
        with self.assertRaises(workspaces.ValidationError):
            capture_base_evidence(self.conn, task_id=self.task_id,
                                 attempt_id="att-1", repo_root=str(self.repo),
                                 claims=[])
        rows = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(rows, 0, "failed captures must persist nothing")

    def test_capture_requires_readable_git_state(self):
        self.add_attempt()
        plain = self.base / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(GitStateError):
            capture_base_evidence(self.conn, task_id=self.task_id,
                                 attempt_id="att-1", repo_root=str(plain))

    def test_load_and_require_missing(self):
        self.assertIsNone(load_base_evidence(self.conn, "att-none"))
        with self.assertRaises(domain.NotFoundError):
            require_base_evidence(self.conn, "att-none")

    def test_tampered_claims_digest_fails_closed_at_load(self):
        self.add_attempt()
        raw = self.raw()
        try:
            raw.execute(
                "INSERT INTO events (event_id, kind, project_id, task_id,"
                " attempt_id, payload_json, occurred_at)"
                " VALUES ('evt-tamp', ?, ?, ?, ?, ?, '2026-11-12T00:00:00+00:00')",
                (BASE_EVIDENCE_KIND, self.project_id, self.task_id, "att-1",
                 json.dumps({"schema_version": 1, "base_sha": self.head,
                           "branch": "main", "claims": ["src/**"],
                           "claims_digest": "f" * 64,
                           "initial_change_count": 0,
                           "initial_changes_digest": "e" * 64,
                           "project_id": self.project_id,
                           "repo_root": verify_repo_root(str(self.repo)),
                           "task_id": self.task_id, "task_version": 0,
                           "attempt_id": "att-1"}, sort_keys=True)))
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(storage.StorageError):
            load_base_evidence(self.conn, "att-1")

    def test_verify_reports_drift(self):
        self.add_attempt()
        evidence, _ = self.capture()
        check = verify_base_evidence(self.conn, evidence)
        self.assertTrue(check.ok, msg=str(check.problems))
        # commit advances HEAD -> base-moved
        (self.repo / "README.md").write_text("drift\n", encoding="utf-8")
        git("commit", "-q", "-am", "drift", cwd=self.repo)
        check = verify_base_evidence(self.conn, evidence)
        self.assertFalse(check.ok)
        self.assertIn("base-moved", check.problems)
        # task version bump -> task-version-drift
        moved = load_base_evidence(self.conn, "att-1")
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE tasks SET version = version + 1 WHERE id = ?",
                             (self.task_id,))
        check = verify_base_evidence(self.conn, moved)
        self.assertIn("task-version-drift", check.problems)
        # vanished workspace root -> repo-root-missing
        check = verify_base_evidence(self.conn,
                                    replace(moved, repo_root=str(self.base / "gone")))
        self.assertIn("repo-root-missing", check.problems)

    def test_attempt_binding_drift_detected(self):
        self.add_attempt()
        evidence, _ = self.capture()
        raw = self.raw()
        try:
            raw.execute("UPDATE attempts SET task_id='other-task' WHERE id='att-1'")
            raw.commit()
        finally:
            raw.close()
        check = verify_base_evidence(self.conn, evidence)
        self.assertFalse(check.ok)
        self.assertIn("attempt-task-mismatch", check.problems)

    def test_explicit_claims_override_task_ownership(self):
        self.add_attempt()
        evidence, created = self.capture(claims=["src/**"])
        self.assertTrue(created)
        self.assertEqual(list(evidence.claims), ["src/**"])
        self.assertEqual(len(evidence.claims_digest), 64)

    def test_initial_change_snapshot_recorded(self):
        self.add_attempt()
        (self.repo / "src" / "dirty.py").write_text("d\n", encoding="utf-8")
        evidence, _ = self.capture()
        self.assertEqual(evidence.initial_change_count, 1)
        self.assertEqual(len(evidence.initial_changes_digest), 64)


class SchemaV16Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-schema-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"

    def test_fresh_database_carries_v16_objects(self):
        report = storage.migrate_to_head(self.db)
        self.assertTrue(report.fresh)
        self.assertEqual(report.to_version, storage.SCHEMA_VERSION)
        self.assertEqual(storage.SCHEMA_VERSION, 16)
        info = storage.verify_database(self.db)
        self.assertTrue(info["ok"])
        self.assertEqual(info["missing_head_objects"], [])
        self.assertEqual(info["missing_head_tables"], [])
        names = {row[0] for row in sqlite3.connect(str(self.db)).execute(
            "SELECT name FROM sqlite_master WHERE type IN ('trigger','index')")}
        self.assertTrue(storage._V16_TRIGGERS <= names)
        self.assertTrue(storage._V16_INDEXES <= names)

    def test_no_new_tables_keeps_v1_head_contracts(self):
        # M4-01 must not add tables/columns: the pre-existing exact table-set
        # contracts and fixture shed lists stay exactly as they were.
        self.assertNotIn(16, storage._TABLES_BY_VERSION)
        self.assertNotIn(16, storage._COLUMNS_BY_VERSION)
        report = storage.migrate_to_head(self.db)
        tables = set(storage.verify_database(self.db)["tables"])
        self.assertNotIn("workspace_base_evidence", tables)
        self.assertEqual(report.to_version, 16)

    def test_upgrade_from_v15_applies_step_16(self):
        storage.migrate_to_head(self.db)
        raw = sqlite3.connect(str(self.db))
        for name in sorted(storage._V16_TRIGGERS):
            raw.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name in sorted(storage._V16_INDEXES):
            raw.execute(f"DROP INDEX IF EXISTS {name}")
        raw.execute("DELETE FROM schema_migrations WHERE version > 15")
        raw.execute("PRAGMA user_version=15")
        raw.commit()
        raw.close()
        report = storage.migrate_to_head(self.db)
        self.assertEqual((report.from_version, report.to_version, report.applied),
                         (15, 16, (16,)))
        self.assertTrue(storage.verify_database(self.db)["ok"])

    def test_missing_v16_objects_fail_the_write_gate(self):
        storage.migrate_to_head(self.db)
        conn = storage.connect(self.db)
        self.addCleanup(conn.close)
        raw = sqlite3.connect(str(self.db))
        raw.execute("DROP INDEX ux_workspace_base_evidence_attempt")
        raw.commit()
        raw.close()
        with self.assertRaises(storage.StorageError) as ctx:
            with storage.transaction(conn):
                events.append_event(conn, "any.kind")
        self.assertIn("ux_workspace_base_evidence_attempt", str(ctx.exception))

    def test_honest_old_fixture_with_residual_v16_objects_converges(self):
        # A peer fixture that sheds v15 artifacts but (unknowingly) keeps the
        # v16 objects must still upgrade cleanly: v16 DDL is idempotent.
        storage.migrate_to_head(self.db)
        raw = sqlite3.connect(str(self.db))
        raw.execute("DROP TABLE IF EXISTS scheduler_controls")
        columns = {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}
        for column in ("paused", "execution_policy_json"):
            if column in columns:
                raw.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
        raw.execute("DELETE FROM schema_migrations WHERE version > 14")
        raw.execute("PRAGMA user_version=14")
        raw.commit()
        raw.close()
        report = storage.migrate_to_head(self.db)
        self.assertEqual((report.from_version, report.to_version), (14, 16))
        self.assertEqual(report.applied, (15, 16))


class ControlCharDisplayTests(unittest.TestCase):
    """FINDING-M401-02: presentation escaping for hostile path spellings.

    ``_display`` used to repair undecodable surrogate bytes only, so a literal
    newline survived verbatim and split a violation tag across log lines. It
    must now render every ``Cc`` control character as a backslash escape while
    keeping ordinary Unicode readable and raw bytes visible.
    """

    def test_cc_control_characters_become_single_line_escapes(self):
        hostile = {
            "li\nne.txt": "li\\nne.txt",
            "carriage\rreturn.txt": "carriage\\rreturn.txt",
            "tab\there.txt": "tab\\there.txt",
            "nul\x00byte.bin": "nul\\x00byte.bin",
            "bel\x07wort.bin": "bel\\x07wort.bin",
            "escape\x1b[31m.txt": "escape\\x1b[31m.txt",
            "form\x0cfeed.txt": "form\\x0cfeed.txt",
            "del\x7fkey.txt": "del\\x7fkey.txt",
            # C1 range: U+0085 (NEL) is a line break for some log viewers.
            "nel\x85line.txt": "nel\\x85line.txt",
            "c1\x9ftail.txt": "c1\\x9ftail.txt",
            # Zl/Zp: valid UTF-8, but str.splitlines() and several viewers
            # treat them as line breaks, so they are escaped too.
            "ls\u2028break.txt": "ls\\u2028break.txt",
            "ps\u2029break.txt": "ps\\u2029break.txt",
        }
        for raw, expected in hostile.items():
            rendered = workspaces._display(raw)
            self.assertEqual(rendered, expected)
            self.assertNotIn("\n", rendered)
            self.assertNotIn("\r", rendered)
            self.assertEqual(len(rendered.splitlines()), 1)
            rendered.encode("utf-8")  # must never blow up on encode

    def test_every_hostile_character_renders_single_line_and_utf8_safe(self):
        # Exhaustive property check over the whole unsafe range plus ordinary
        # Unicode and raw byte surrogates.
        corpus = [chr(code) for code in range(0x00, 0x100)]
        corpus += ["\u2028", "\u2029", "\u200b", "中", "🚀", "é", " ", "\\",
                  "\x7f", "\x9f"]
        corpus += [os.fsdecode(bytes([byte])) for byte in range(0x80, 0x100)]
        for char in corpus:
            rendered = workspaces._display("pre" + char + "post")
            self.assertEqual(len(rendered.splitlines()), 1, repr(char))
            rendered.encode("utf-8")
            if unicodedata.category(char) in workspaces._UNSAFE_CATEGORIES:
                self.assertNotIn(char, rendered)
                self.assertTrue(rendered.startswith("pre\\") or "\\post" in rendered,
                              repr((char, rendered)))
            elif unicodedata.category(char) == "Cs":
                # A lone surrogate is one raw filename byte: \xNN spelling.
                expected_bytes = char.encode("utf-8", "surrogateescape")
                self.assertEqual(rendered, "pre" + "".join(
                    f"\\x{byte:02x}" for byte in expected_bytes) + "post")
            elif char == "\\":
                self.assertEqual(rendered, "pre\\\\post")
            else:
                self.assertEqual(rendered, "pre" + char + "post")

    def test_ordinary_unicode_is_preserved_verbatim(self):
        for path in ("文档/指南.md", "café/naïve.py", "rocket-🚀.md",
                    "dir with space/file.txt", 'quote"and\'tick.txt',
                    "/smash", "a:b;c|d.txt", "emoji-😀😀.bin"):
            self.assertEqual(workspaces._display(path), path)

    def test_raw_non_utf8_bytes_keep_their_escaped_spelling(self):
        # os.fsdecode() carries raw bytes as lone surrogates; they must render
        # as the \xNN spelling of the byte they stand for (as before) and the
        # result must be UTF-8 encodable so digests/JSON cannot fail.
        for raw_bytes, expected in (
            (b"bad\xffname.bin", "bad\\xffname.bin"),
            (b"caf\xe9.txt", "caf\\xe9.txt"),
            (b"\x80\x81.bin", "\\x80\\x81.bin"),
            (b"tail\xff", "tail\\xff"),
        ):
            rendered = workspaces._display(os.fsdecode(raw_bytes))
            self.assertEqual(rendered, expected)
            rendered.encode("utf-8")
        # and inside a JSON payload, which previously could not encode at all
        json.dumps({"path": workspaces._display(os.fsdecode(b"raw\xff.bin"))},
                   ensure_ascii=False).encode("utf-8")

    def test_backslash_is_escaped_so_rendering_is_injective(self):
        # A literal backslash + 'n' must not collide with a real newline, or a
        # crafted name could impersonate another path's tag.
        self.assertEqual(workspaces._display("back\\slash.txt"), "back\\\\slash.txt")
        self.assertEqual(workspaces._display("fake\\nnewline.txt"),
                       "fake\\\\nnewline.txt")
        self.assertNotEqual(workspaces._display("a\nb"),
                           workspaces._display("a\\nb"))
        self.assertNotEqual(workspaces._display("a\x00b"),
                           workspaces._display("a\\x00b"))

    def test_control_char_free_paths_render_unchanged(self):
        for path in ("README.md", "teamcraft/workspaces.py", "src/deep/x.py",
                    "docs"):
            self.assertEqual(workspaces._display(path), path)


class NewlineViolationGateTests(FakeRunnerMixin, unittest.TestCase):
    """A newline inside a collected unowned path stays on one log line."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-escape-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = init_repo(self.base)
        self.root = verify_repo_root(str(self.repo))

    def collect_with(self, canned: dict):
        return collect_workspace_state(self.root,
                                     run_git=self.fake_runner(self.root, canned))

    def test_collected_path_keeps_the_literal_newline(self):
        # Collection semantics are untouched: the name survives byte-exactly.
        status = b"A  escape\nme.txt\x00"
        state = self.collect_with({("status", "--porcelain=v1"): status})
        paths = {entry.path for entry in state.changes}
        self.assertEqual(paths, {"escape\nme.txt"})

    def test_unowned_newline_path_yields_one_escaped_single_line_tag(self):
        status = b"A  escape\nme.txt\x00"
        claims = ["src/**", "README.md"]
        violations = run_diff_gate(str(self.repo), claims,
                                   run_git=self.fake_runner(self.root,
                                                          {("status", "--porcelain=v1"):
                                                           status})).violations
        self.assertEqual(violations, ("unowned:escape\\nme.txt",))
        for tag in violations:
            self.assertEqual(len(tag.splitlines()), 1, tag)
            self.assertNotIn("\n", tag)
            self.assertNotIn("\r", tag)
            tag.encode("utf-8")
        # the tag also survives being logged as JSON on a single line
        line = json.dumps({"violations": list(violations)}, ensure_ascii=False)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertEqual(json.loads(line)["violations"], list(violations))
        # JSON doubles the backslash, and the newline never appears raw
        self.assertIn("unowned:escape\\\\nme.txt", line)
        self.assertNotIn("escape\nme.txt", line)

    def test_evaluate_ownership_escapes_control_chars_in_tags(self):
        claims = normalize_claims(["src/**"])
        violations = evaluate_ownership(
            ["two\nlines.txt", "tab\there.txt", "nul\x00.bin",
             "raw\xffname.bin".replace("\xff", "\udcff"), "src/owned.py"],
            claims)
        self.assertEqual(violations, ("unowned:nul\\x00.bin",
                                     "unowned:raw\\xffname.bin",
                                     "unowned:tab\\there.txt",
                                     "unowned:two\\nlines.txt"))
        self.assertTrue(all(len(tag.splitlines()) == 1 for tag in violations))

    def test_every_violation_kind_escapes_the_path(self):
        # symlink-escape/case-alias tags come from run_diff_gate too; a hostile
        # name must be escaped there as well, not just for `unowned:`.
        outside = self.base / "outside-target"
        outside.mkdir()
        try:
            (self.repo / "evil\nlink").symlink_to(outside)
        except OSError:  # pragma: no cover - platform refuses newline names
            self.skipTest("filesystem refuses newline in file names")
        result = run_diff_gate(str(self.repo), ["src/**"])
        self.assertFalse(result.ok)
        # Both the resolution failure and the coverage miss are reported, and
        # BOTH carry the escaped (single-line) spelling of the name.
        self.assertEqual(result.violations, ("symlink-escape:evil\\nlink",
                                            "unowned:evil\\nlink"))
        self.assertTrue(all(len(tag.splitlines()) == 1 for tag in result.violations))
        self.assertTrue(all("\n" not in tag for tag in result.violations))

    def test_digest_inputs_are_escaped_and_utf8_encodable(self):
        changes = (
            ChangeEntry(path="escape\nme.txt", untracked=True),
            ChangeEntry(path="b.py", renamed=True,
                      rename_source="raw\udcff/a\nb.py",
                      rename_target="c\td.py"),
        )
        digest = workspaces._changes_digest(changes)
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # hex
        # The digest is taken over the *escaped* rendering, so a moved/replayed
        # capture of the same workspace agrees and the input is one line.
        expected_input = [
            {"path": "escape\\nme.txt", "staged": False, "unstaged": False,
             "untracked": True, "added": False, "deleted": False,
             "renamed": False, "rename_source": None, "rename_target": None},
            {"path": "b.py", "staged": False, "unstaged": False,
             "untracked": False, "added": False, "deleted": False,
             "renamed": True, "rename_source": "raw\\xff/a\\nb.py",
             "rename_target": "c\\td.py"},
        ]
        expected = workspaces._sha256_hex(workspaces._canonical_json(
            sorted(expected_input, key=workspaces._canonical_json)))
        self.assertEqual(digest, expected)
        joined = workspaces._canonical_json(
            sorted(expected_input, key=workspaces._canonical_json))
        self.assertEqual(len(joined.splitlines()), 1)
        joined.encode("utf-8")
        # equivalent spellings produce identical digests
        same = workspaces._changes_digest((
            ChangeEntry(path="escape\nme.txt", untracked=True),
            ChangeEntry(path="b.py", renamed=True,
                      rename_source=os.fsdecode(b"raw\xff/a\nb.py"),
                      rename_target="c\td.py"),
        ))
        self.assertEqual(digest, same)
        # and the digest does not depend on the order records arrive in
        self.assertEqual(digest, workspaces._changes_digest(tuple(reversed(changes))))

    def test_multi_change_digest_is_computable_and_canonical(self):
        # Two or more changes used to raise TypeError (dicts are not orderable).
        changes = tuple(
            ChangeEntry(path=f"dir/{name}.py", untracked=True)
            for name in ("beta", "alpha", "gamma\nnewline", "delta\ttab"))
        digest = workspaces._changes_digest(changes)
        self.assertEqual(len(digest), 64)
        int(digest, 16)
        self.assertEqual(digest, workspaces._changes_digest(
            tuple(sorted(changes, key=lambda e: e.path, reverse=True))))
        self.assertNotEqual(digest, workspaces._changes_digest(changes[:2]))

    def test_nul_delimited_collection_semantics_are_unchanged(self):
        # NUL is the record separator: it never survives inside a collected
        # path, trailing separators are trimmed, and multi-line names stay
        # intact as ONE record each.
        raw = (b"A  one\nline.txt\x00?? ta\tb.txt\x00M  README.md\x00"
               b"raw\xff.bin\x00\x00")
        tokens = workspaces._nul_tokens(raw)
        self.assertEqual(tokens, ["A  one\nline.txt", "?? ta\tb.txt",
                                 "M  README.md", "raw\udcff.bin"])
        self.assertTrue(all("\0" not in token for token in tokens))
        status = b"\x00".join([b"A  one\nline.txt", b"?? ta\tb.txt", b""])
        state = self.collect_with({("status", "--porcelain=v1"): status,
                                  ("ls-files", "--others"):
                                  b"one\nline.txt\x00ta\tb.txt\x00"})
        by_path = {entry.path: entry for entry in state.changes}
        self.assertEqual(set(by_path), {"one\nline.txt", "ta\tb.txt"})
        self.assertTrue(all("\0" not in path for path in by_path))
        self.assertTrue(by_path["one\nline.txt"].added)
        self.assertTrue(by_path["ta\tb.txt"].untracked)


class EscapedEvidenceIntegrationTests(EvidenceFixtureMixin, unittest.TestCase):
    """The escaped digest survives the durable base-evidence path."""

    def test_capture_with_several_changes_including_newline_name(self):
        # Two or more changes used to make the digest raise TypeError; a
        # newline-bearing name must not split the persisted payload either.
        name = "src\nweird.py"
        try:
            (self.repo / name).write_text("x\n", encoding="utf-8")
        except OSError:  # pragma: no cover - platform refuses newline names
            self.skipTest("filesystem refuses newline in file names")
        (self.repo / "stray.txt").write_text("y\n", encoding="utf-8")
        self.add_attempt()
        evidence, created = self.capture()
        self.assertTrue(created)
        self.assertEqual(evidence.initial_change_count, 2)
        self.assertEqual(len(evidence.initial_changes_digest), 64)
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE seq = ?",
            (evidence.seq,)).fetchone()[0]
        self.assertEqual(len(row.splitlines()), 1)
        payload = json.loads(row)
        self.assertEqual(payload["initial_changes_digest"],
                         evidence.initial_changes_digest)
        # replay of the identical workspace is idempotent (digest is stable)
        replay, created_again = self.capture()
        self.assertFalse(created_again)
        self.assertEqual(replay.initial_changes_digest,
                         evidence.initial_changes_digest)


class RealGitControlCharGateTests(unittest.TestCase):
    """End-to-end on a real repository: a newline name cannot split the tag."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m401-escape-git-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = init_repo(self.base)

    def test_newline_named_untracked_file_reports_single_line_violation(self):
        name = "sneaky\nnewline.txt"
        try:
            (self.repo / name).write_text("x\n", encoding="utf-8")
        except OSError:  # pragma: no cover - platform refuses newline names
            self.skipTest("filesystem refuses newline in file names")
        state = collect_workspace_state(str(self.repo))
        self.assertEqual({entry.path for entry in state.changes}, {name})
        result = run_diff_gate(str(self.repo), ["src/**", "README.md", "docs/**"])
        self.assertFalse(result.ok)
        self.assertEqual(result.violations, ("unowned:sneaky\\nnewline.txt",))
        self.assertEqual(str(result.violations).count("\\n"), 1)
        for tag in result.violations:
            self.assertEqual(len(tag.splitlines()), 1)

    def test_multi_change_digest_from_live_state_is_single_line(self):
        # A real dirty workspace (several changes, one with a newline name) must
        # produce a digest at all, and its rendered input must stay one line.
        name = "docs\nnotes.md"
        try:
            (self.repo / name).write_text("x\n", encoding="utf-8")
        except OSError:  # pragma: no cover - platform refuses newline names
            self.skipTest("filesystem refuses newline in file names")
        (self.repo / "plain.txt").write_text("y\n", encoding="utf-8")
        state = collect_workspace_state(str(self.repo))
        self.assertEqual({entry.path for entry in state.changes}, {name, "plain.txt"})
        digest = workspaces._changes_digest(state.changes)
        self.assertEqual(len(digest), 64)
        int(digest, 16)
        rendered = json.dumps(
            [{"path": workspaces._display(entry.path)} for entry in state.changes],
            ensure_ascii=False)
        self.assertEqual(len(rendered.splitlines()), 1)
        # _display escapes the newline to backslash-n; JSON doubles it again.
        self.assertIn("docs\\\\nnotes.md", rendered)
        self.assertNotIn("docs\nnotes.md", rendered)


if __name__ == "__main__":
    unittest.main()
