"""M4-05 bounded GitHub adapter tests (M0 review §9.4 M4-05).

Every assertion here runs against an injected fake ``gh`` runner: this suite
NEVER spawns ``gh``, never opens a socket, and never needs credentials.  It
pins the adapter contract the M4-05 Core depends on:

* presence + authentication reported as VALUES (never exceptions), so missing
  tooling becomes a safe BLOCKED outcome upstream;
* argv is always a list handed to a ``shell=False`` exec path — no
  string-interpolated command line — and syntactically dangerous atoms
  (repo slugs, refs, PR numbers, digests) are refused BEFORE exec;
* the allowed top-level commands are exactly ``auth``/``pr`` and PR verbs are
  exactly ``list``/``create``/``view``/``merge``; every other PR verb is
  refused before the runner, including unknown verbs added by future CLIs;
* PR facts parse into structured head/base/check/merge records, ``ready`` is
  a DERIVED verdict (open + mergeable + checks passing), never a claim;
* ambiguous multi-match lookups and hard timeouts fail closed.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from teamcraft import github_adapter as gh
from teamcraft.github_adapter import (
    ALLOWED_PR_SUBCOMMANDS,
    FORBIDDEN_PR_SUBCOMMANDS,
    GitHubError,
    GitHubInvocationError,
)


def _j(value) -> bytes:
    return json.dumps(value).encode("utf-8")


def pr_json(**over):
    base = {
        "number": 7, "url": "https://github.example/o/r/pull/7",
        "headRefName": "tc/task1/attempt1",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"name": "ci/test", "status": "COMPLETED",
                               "conclusion": "SUCCESS"}],
        "mergedAt": None, "mergeCommit": None,
    }
    base.update(over)
    return base


class ScriptedRunner:
    """Deterministic fake gh runner recording every argv it receives."""

    def __init__(self, responses=None, *, auth_ok=True):
        # responses: list of (argv_prefix_matcher, result) consumed in order,
        # where result is bytes | Exception | callable(argv) -> bytes.
        self.responses = list(responses or [])
        self.auth_ok = auth_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd: str) -> bytes:
        argv = list(argv)
        self.calls.append(argv)
        if self.responses:
            head = self.responses[0]
            matcher, result = head
            if matcher is None or argv[:len(matcher)] == matcher:
                self.responses.pop(0)
                if isinstance(result, Exception):
                    raise result
                if callable(result):
                    return result(argv)
                return result
        if argv and argv[0] == "auth":
            if not self.auth_ok:
                raise GitHubInvocationError("gh auth status exit 1", exit_code=1,
                                          stderr="not logged in to any host")
            return ("github.com\n  \u2713 Logged in to github.com account tester"
                    " (keyring)\n").encode("utf-8")
        raise AssertionError(f"unexpected gh argv: {argv!r}")


# ---------------------------------------------------------------------------
# probe: presence + auth as values
# ---------------------------------------------------------------------------


class ProbeTests(unittest.TestCase):
    def test_missing_binary_is_present_false_and_never_raises(self):
        runner = ScriptedRunner()
        probe = gh.probe_gh(runner, ".", resolve=lambda name: None)
        self.assertFalse(probe.present)
        self.assertFalse(probe.authenticated)
        self.assertTrue(probe.blocked)
        # A missing binary is decided without invoking gh at all.
        self.assertEqual(runner.calls, [])

    def test_unauthenticated_probe_is_blocked_never_raises(self):
        runner = ScriptedRunner(auth_ok=False)
        probe = gh.probe_gh(runner, ".", resolve=lambda name: "/usr/bin/gh")
        self.assertTrue(probe.present)
        self.assertFalse(probe.authenticated)
        self.assertTrue(probe.blocked)
        self.assertIn("not authenticated", probe.detail)
        self.assertEqual(runner.calls, [["auth", "status"]])

    def test_authenticated_probe_reports_account(self):
        runner = ScriptedRunner()
        probe = gh.probe_gh(runner, ".", resolve=lambda name: "/usr/bin/gh")
        self.assertFalse(probe.blocked)
        self.assertIsNotNone(probe.account)
        self.assertIn("account tester", probe.account or "")

    def test_hard_error_probe_is_blocked(self):
        runner = ScriptedRunner([(["auth"], GitHubError("gh exploded"))])
        probe = gh.probe_gh(runner, ".", resolve=lambda name: "/usr/bin/gh")
        self.assertTrue(probe.blocked)
        self.assertTrue(probe.present)


# ---------------------------------------------------------------------------
# argv safety (last line before exec)
# ---------------------------------------------------------------------------


class ArgvSafetyTests(unittest.TestCase):
    def test_repo_slug_validation(self):
        self.assertEqual(gh.normalize_repo("owner/repo-name.v2"),
                       "owner/repo-name.v2")
        for bad in ("owner", "owner/repo extra", "own;rm -rf/repo", "",
                   "../repo", "owner/repo|curl evil", None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(GitHubError):
                    gh.normalize_repo(bad)

    def test_find_open_pr_refuses_unsafe_refs_before_exec(self):
        runner = ScriptedRunner()
        for bad in ("main; id", "main && curl evil", "main `x`", "main\nleak",
                   "", "  ", "a" * 251):
            with self.subTest(bad=bad):
                with self.assertRaises(GitHubError):
                    gh.find_open_pr(runner, ".", repo="o/r", head=bad, base="main")
                with self.assertRaises(GitHubError):
                    gh.find_open_pr(runner, ".", repo="o/r", head="ok", base=bad)
        self.assertEqual(runner.calls, [])

    def test_merge_requires_digest_and_method_allowlist(self):
        runner = ScriptedRunner()
        with self.assertRaises(GitHubError):
            gh.merge_pr(runner, ".", repo="o/r", number=1, head_sha="deadbeef")
        with self.assertRaises(GitHubError):
            gh.merge_pr(runner, ".", repo="o/r", number=1, head_sha="A" * 40)
        with self.assertRaises(GitHubError):
            gh.merge_pr(runner, ".", repo="o/r", number=0, head_sha="a" * 40)
        with self.assertRaises(GitHubError):
            gh.merge_pr(runner, ".", repo="o/r", number=1, head_sha="a" * 64,
                       method="force")
        self.assertEqual(runner.calls, [])

    def test_every_unsupported_pr_subcommand_never_executes(self):
        runner = ScriptedRunner()
        audited = gh.AuditedGhRunner(runner)
        unsupported = set(FORBIDDEN_PR_SUBCOMMANDS) | {
            "checks", "lock", "unlock", "totally-new-command", "",
        }
        for verb in sorted(unsupported):
            with self.subTest(verb=verb):
                with self.assertRaises(GitHubError):
                    argv = ["pr"] if not verb else ["pr", verb, "7"]
                    audited(argv, ".")
        self.assertEqual(runner.calls, [])

    def test_only_required_pr_subcommands_and_auth_status_reach_runner(self):
        calls = []

        def runner(argv, cwd):
            calls.append(list(argv))
            return b"ok"

        audited = gh.AuditedGhRunner(runner)
        for verb in sorted(ALLOWED_PR_SUBCOMMANDS):
            self.assertEqual(audited(["pr", verb], "."), b"ok")
        self.assertEqual(audited(["auth", "status"], "."), b"ok")
        self.assertEqual(
            calls,
            [["pr", verb] for verb in sorted(ALLOWED_PR_SUBCOMMANDS)]
            + [["auth", "status"]],
        )

    def test_default_runner_refuses_unsupported_pr_verbs_before_resolution(self):
        with mock.patch.object(gh.shutil, "which") as resolve, \
                mock.patch.object(subprocess, "run") as run:
            for verb in ("close", "checks", "totally-new-command"):
                with self.subTest(verb=verb):
                    with self.assertRaises(GitHubError):
                        gh.default_run_gh(["pr", verb, "7"], "/tmp")
            with self.assertRaises(GitHubError):
                gh.default_run_gh(["pr"], "/tmp")
        resolve.assert_not_called()
        run.assert_not_called()

    def test_unknown_top_level_commands_refused(self):
        audited = gh.AuditedGhRunner(ScriptedRunner())
        for cmd in ("release", "gist", "repo", "api", "codespace", "workflow"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(GitHubError):
                    audited([cmd, "x"], ".")

    def test_default_run_gh_missing_binary_fails_closed_without_shell(self):
        with mock.patch.object(gh.shutil, "which", return_value=None):
            with self.assertRaises(GitHubError):
                gh.default_run_gh(["auth", "status"], ".")

    def test_default_run_gh_execs_shell_false_argv_list(self):
        seen = {}

        class FakeProc:
            returncode = 0
            stdout = b"ok"
            stderr = b""

        def fake_run(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return FakeProc()

        with mock.patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(subprocess, "run", fake_run):
            out = gh.default_run_gh(["pr", "view", "7"], "/tmp")
        self.assertEqual(out, b"ok")
        self.assertEqual(seen["command"], ["/usr/bin/gh", "pr", "view", "7"])
        self.assertIs(seen["kwargs"]["shell"], False)
        self.assertIs(seen["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertIn("timeout", seen["kwargs"])

    def test_default_run_gh_nonzero_exit_carries_code_and_stderr(self):
        class FakeProc:
            returncode = 1
            stdout = b""
            stderr = b"pull request is not mergeable"

        with mock.patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(subprocess, "run", lambda *a, **k: FakeProc()):
            with self.assertRaises(GitHubInvocationError) as caught:
                gh.default_run_gh(["pr", "merge", "7"], "/tmp")
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertIn("not mergeable", caught.exception.stderr)

    def test_default_run_gh_timeout_is_hard_failure(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(["gh"], timeout=1)

        with mock.patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(subprocess, "run", boom):
            with self.assertRaises(GitHubError):
                gh.default_run_gh(["pr", "view", "7"], "/tmp")


# ---------------------------------------------------------------------------
# facts + operations
# ---------------------------------------------------------------------------


class FactsTests(unittest.TestCase):
    def test_find_open_pr_none_on_empty(self):
        runner = ScriptedRunner([(["pr", "list"], _j([]))])
        self.assertIsNone(gh.find_open_pr(runner, ".", repo="o/r",
                                         head="tc/t/a", base="main"))
        self.assertEqual(runner.calls[0][:2], ["pr", "list"])

    def test_find_open_pr_returns_single_match(self):
        runner = ScriptedRunner([(["pr", "list"], _j([pr_json()]))])
        facts = gh.find_open_pr(runner, ".", repo="o/r",
                                head="tc/task1/attempt1", base="main")
        self.assertIsNotNone(facts)
        self.assertEqual(facts.number, 7)
        self.assertEqual(facts.head_sha, "a" * 40)
        self.assertTrue(facts.merge_ready)

    def test_find_open_pr_ambiguous_refuses_to_pick(self):
        runner = ScriptedRunner([(["pr", "list"],
                                 _j([pr_json(), pr_json(number=8)]))])
        with self.assertRaises(GitHubInvocationError):
            gh.find_open_pr(runner, ".", repo="o/r", head="tc/task1/attempt1",
                           base="main")

    def test_find_open_pr_filters_foreign_rows(self):
        runner = ScriptedRunner([(["pr", "list"], _j([
            pr_json(headRefName="other"), pr_json(baseRefName="release")]))])
        self.assertIsNone(gh.find_open_pr(runner, ".", repo="o/r",
                                         head="tc/task1/attempt1", base="main"))

    def test_create_pr_builds_list_argv_and_parses(self):
        runner = ScriptedRunner([(["pr", "create"],
                                 lambda argv: _j(pr_json()))])
        facts = gh.create_pr(runner, ".", repo="o/r", head="tc/t/a",
                            base="main", title=" deliver ", body="why")
        argv = runner.calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "pr")
        self.assertEqual(argv[1], "create")
        self.assertIn("--head", argv)
        self.assertIn("--repo", argv)
        # Title is trimmed before exec.
        self.assertEqual(argv[argv.index("--title") + 1], "deliver")
        self.assertEqual(facts.head_branch, "tc/task1/attempt1")

    def test_non_json_output_fails_closed(self):
        runner = ScriptedRunner([(["pr", "view"], b"not json at all")])
        with self.assertRaises(GitHubInvocationError):
            gh.read_pr(runner, ".", repo="o/r", number=7)

    def test_check_rollup_derivations(self):
        facts = gh._facts_from(pr_json(statusCheckRollup=[
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]), "o/r")
        self.assertFalse(facts.checks_passing)
        self.assertFalse(facts.merge_ready)
        pending = gh._facts_from(pr_json(statusCheckRollup=[
            {"name": "test", "status": "IN_PROGRESS", "conclusion": None},
        ]), "o/r")
        self.assertFalse(pending.checks_passing)
        neutral = gh._facts_from(pr_json(statusCheckRollup=[
            {"name": "scan", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        ]), "o/r")
        self.assertTrue(neutral.checks_passing)
        merged = gh._facts_from(pr_json(
            state="MERGED", mergedAt="2026-11-14T00:00:00Z",
            mergeCommit={"oid": "c" * 40}), "o/r")
        self.assertTrue(merged.merged)
        self.assertEqual(merged.merge_commit_sha, "c" * 40)
        self.assertFalse(merged.open)
        self.assertFalse(merged.merge_ready)

    def test_merge_head_guard_refusal_is_value_not_exception(self):
        runner = ScriptedRunner([(["pr", "merge"],
                                 GitHubInvocationError("gh pr merge exit 1",
                                                   exit_code=1,
                                                   stderr="head does not match"))])
        result = gh.merge_pr(runner, ".", repo="o/r", number=7,
                            head_sha="a" * 40)
        self.assertFalse(result.merged)
        self.assertIn("head does not match", result.raw)
        # The head guard must always be part of the merge argv.
        argv = runner.calls[0]
        self.assertEqual(argv[:2], ["pr", "merge"])
        self.assertIn("--match-head-commit", argv)

    def test_merge_success_claims_merged_but_readback_decides(self):
        runner = ScriptedRunner([(["pr", "merge"], b"Merged pull request #7\n")])
        result = gh.merge_pr(runner, ".", repo="o/r", number=7,
                            head_sha="a" * 40, method="squash")
        self.assertTrue(result.merged)
        self.assertIsNone(result.merge_commit_sha)  # CLI claim is not proof
        self.assertIn("--squash", runner.calls[0])

    def test_repo_flag_plumbed_through(self):
        runner = ScriptedRunner([
            (["pr", "list"], _j([])),
            (["pr", "view"], _j(pr_json())),
        ])
        gh.find_open_pr(runner, ".", repo=None, head="tc/t/a", base="main")
        self.assertNotIn("--repo", runner.calls[0])
        gh.read_pr(runner, ".", repo="o/r", number=7)
        self.assertEqual(runner.calls[1][runner.calls[1].index("--repo") + 1],
                       "o/r")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
