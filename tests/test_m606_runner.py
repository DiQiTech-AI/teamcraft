"""M6-06 — offline validation of ``scripts/m606_opencode_e2e.py``.

WHAT THIS PROVES
    The runner's SAFETY LOGIC, not a stub's compliance.  Three transports are
    faked — the Herdr pane, the oMLX HTTP GET, and the harness ``--version``
    subprocess — because those are precisely what a test must not touch (a live
    pane, a live model route).  Everything else is the real thing: real disposable
    Git (bare origin + clone + worktrees), real SQLite at schema head, real
    capability-probe durability, real verifier profiles, real Diff Gate, real
    delivery/cleanup eligibility, and the runner's own guards, gates,
    postconditions and provenance journal.

THE TWO-PHASE SHAPE (why the tests look like this)
    An attempt worktree does not exist until the runner creates it, and an
    operator cannot cd a pane into a directory that does not exist yet.  So the
    gate "the pane's cwd is exactly the allocated worktree" is *unreachable*
    inside the allocating invocation.  The runner therefore refuses live dispatch
    in one invocation and hands off (``--stop-after allocate`` →
    ``--resume-handoff``).  Every test here follows that real operator sequence:
    allocate, then place the fake pane in the allocated cwd, then resume.  No test
    patches the gate to make it pass.

WHAT THIS NEVER DOES
    * Spawns no real ``herdr`` and touches no live pane; one test asserts the real
      Herdr client issues ZERO commands when a verb is refused.
    * Opens no live runtime DB; the project's ``.teamcraft/teamcraft.db`` is
      bracketed by size/mtime around a run.
    * Pushes nothing: the only origin is a local bare repo in the disposable root,
      and tests assert it never gains a ref.
    * Removes nothing it did not create: roots made by this test process are
      removed by this test process; foreign roots are left alone.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import m606_opencode_e2e as m606  # noqa: E402
from teamcraft import (  # noqa: E402
    ceo_initialization,
    domain,
    providers,
    runtime_adapter,
    storage,
    wake,
)

MODEL = m606.DEFAULT_MODEL
OTHER_MODEL = "Qwen3.8-Flash-Next-4bit-mtp"
PANE = "w9:p1"
SESSION = "ses_fake_opencode_0001"
TERMINAL = "term_fake_0001"


# ---------------------------------------------------------------------------
# Fakes: transport only
# ---------------------------------------------------------------------------

def fake_omlx_getter(url: str, timeout: float) -> tuple[int, bytes]:
    """A healthy loopback catalog serving the pinned model (no socket)."""
    assert "127.0.0.1" in url, f"getter asked for a non-loopback URL: {url}"
    body = {"object": "list", "version": "0.1.0",
            "data": [{"id": MODEL}, {"id": OTHER_MODEL}]}
    return 200, json.dumps(body).encode("utf-8")


def dead_omlx_getter(url: str, timeout: float) -> tuple[int, bytes]:
    return 0, b""


def fake_harness_runner(argv: tuple[str, ...], cwd: str,
                        timeout: float) -> tuple[int, str]:
    return 0, "1.2.3\n"


class FakeHerdr:
    """Stand-in for one operator-owned pane.  Records, never acts."""

    def __init__(self, *, pane: str = PANE, agent: str = "opencode",
                 agent_status: str = "idle", cwd: str | None = None,
                 sessions: tuple[str, ...] = (SESSION,),
                 foreground_cwd: str | None = None, screen: str = "",
                 status_overrides: dict[str, str] | None = None,
                 submit_outcome: str = "accepted",
                 expected_agent: str = "opencode") -> None:
        self.pane = pane
        self.agent = agent
        self.expected_agent = expected_agent
        self.agent_status = agent_status
        self.cwd = cwd
        self.sessions = sessions
        self.foreground_cwd = foreground_cwd
        self.screen = screen
        self.status_overrides = status_overrides or {}
        self.submit_outcome = submit_outcome
        #: optional (nth, outcome) plan: the Nth submit (1-based) reports
        #: something other than the default, so a specific leg's transport can be
        #: made unresolved while earlier legs succeed.
        self.fail_submit_on: tuple[int, str] | None = None
        self.identity_reads = 0
        self.submits: list[tuple[str, str]] = []
        self.reads: list[str] = []

    def _identity(self, pane_id: str, session: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "workspace_id": "w9", "tab_id": "w9:t1", "tab_label": "m606",
            "pane_id": pane_id, "terminal_id": TERMINAL, "agent": self.agent,
            "agent_name": "worker", "agent_status": self.agent_status,
            "session_kind": "id", "session_source": "herdr:opencode",
            "session_id": session, "cwd": self.cwd or "",
            "observed_at": "2026-11-22T00:00:00+00:00",
        }
        if self.foreground_cwd:
            out["foreground_cwd"] = self.foreground_cwd
        return out

    def identity(self, pane_id: str) -> dict[str, Any]:
        """Mirror the real client's identity contract, not just its shape.

        ``Herdr.identity`` refuses a wrong agent and a missing native session id;
        reproducing that here keeps the fake from silently authorizing states the
        real client would reject.  :class:`RealClientIdentityTests` exercises the
        real implementation directly.
        """
        self.identity_reads += 1
        if pane_id != self.pane:
            raise m606.PostconditionError(
                f"pane {pane_id} is not present in the live snapshot")
        idx = min(self.identity_reads - 1, len(self.sessions) - 1)
        out = self._identity(pane_id, self.sessions[idx])
        if out["agent"] != self.expected_agent:
            raise m606.PostconditionError(
                f"pane {pane_id} hosts agent {out['agent']!r}, not "
                f"{self.expected_agent!r}")
        if not out["session_id"]:
            raise m606.PostconditionError(
                f"pane {pane_id} exposes no native session id")
        return out

    def topology_digest(self) -> str:
        return "topo-fake-unchanged"

    def status(self) -> dict[str, str]:
        base = {"server.status": "running", "client.version": "0.9.3",
                "server.version": "0.9.3", "server.private_protocol": "22",
                "server.endpoint_compatible": "yes",
                "server.private_protocol_compatible": "yes",
                "update.restart_needed": "no"}
        base.update(self.status_overrides)
        return base

    def read_pane(self, pane_id: str, limit: int = 4000) -> tuple[int, str]:
        self.reads.append(pane_id)
        return 0, self.screen

    def submit(self, target: str, prompt: str, *, wait: bool = False,
               until: tuple[str, ...] = (),
               timeout_ms: int = 5000) -> dict[str, Any]:
        self.submits.append((target, prompt))
        ident = self._identity(target, self.sessions[0])
        outcome = self.submit_outcome
        if self.fail_submit_on is not None and len(self.submits) == self.fail_submit_on[0]:
            outcome = self.fail_submit_on[1]
        code = 0 if outcome == "accepted" else 1
        return {
            "submitted_at": "2026-11-22T00:00:00+00:00",
            "argv_shape": ["herdr", "agent", "prompt", target, "<TEXT>"],
            "prompt_sha256": m606.sha256_bytes(prompt.encode("utf-8")),
            "prompt_chars": len(prompt), "returncode": code,
            "transport_observation": outcome,
            "wait_requested": wait, "until": list(until),
            "timeout_ms": timeout_ms if wait else None,
            "output_head": outcome, "identity_before": ident,
            "identity_after": ident, "session_stable": True,
            "prompt_ack": "unknown", "event_stream": "unknown",
            "host_trusted_receipt": False,
        }

    def stand_in(self, path: str | Path) -> None:
        """Simulate the operator having cd'd the pane into ``path``."""
        self.cwd = str(path)


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------

class Tray:
    """Roots created BY THIS PROCESS, removed BY THIS PROCESS."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="m606-runner-test-"))
        self.roots: list[Path] = []
        self.runs: list[Any] = []

    def root(self, name: str) -> Path:
        path = self.base / name
        self.roots.append(path)
        return path

    def track(self, run: Any) -> Any:
        self.runs.append(run)
        return run

    def close(self) -> None:
        for run in self.runs:
            conn = getattr(run, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:      # noqa: BLE001 — teardown best effort
                    pass
        for path in self.roots + [self.base]:
            shutil.rmtree(path, ignore_errors=True)


def parse_args(root: Path, *extra: str) -> Any:
    return m606.build_parser().parse_args(
        ["--confirm-live-run", "--root", str(root), *extra])


def attestation(tray: Tray, name: str, *, model: str = MODEL,
                pane: str | None = PANE, terminal: str | None = TERMINAL,
                session: str | None = SESSION, agent: str | None = "opencode",
                provenance: str | None = "operator read the pane status line",
                ) -> Path:
    payload: dict[str, Any] = {"model": model}
    for key, value in (("pane_id", pane), ("terminal_id", terminal),
                       ("session_id", session), ("agent", agent),
                       ("provenance", provenance)):
        if value is not None:
            payload[key] = value
    path = tray.base / f"{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_run(tray: Tray, root: Path, *extra: str, herdr: FakeHerdr,
             getter: Any = fake_omlx_getter) -> m606.M606LiveRun:
    return tray.track(m606.M606LiveRun(
        parse_args(root, *extra), herdr=herdr, omlx_getter=getter,
        harness_runner=fake_harness_runner))


def phase1(tray: Tray, name: str, herdr: FakeHerdr,
           *extra: str) -> tuple[Path, dict[str, Any]]:
    """Provision + allocate, and return (root, handoff) for the operator."""
    root = tray.root(name)
    run = make_run(tray, root, "--stop-after", "allocate", "--keep-root", *extra,
                  herdr=herdr)
    report = run.execute()
    assert report["ok"], f"phase 1 failed: {report.get('aborted')}"
    handoff = json.loads((root / "evidence" / "handoff.json").read_text("utf-8"))
    return root, handoff


def phase2(tray: Tray, root: Path, herdr: FakeHerdr, *, attest: str,
           consent: bool = True, session: str = SESSION,
           model: str | None = None,
           extra: tuple[str, ...] = ()) -> m606.M606LiveRun:
    args = ["--resume-handoff", "--herdr-pane", PANE,
            "--pane-model-evidence", attest, "--expect-session-id", session]
    if consent:
        args.append("--consent-dispatch")
    if model:
        args += ["--model", model]
    return make_run(tray, root, *args, *extra, herdr=herdr)


def evidence(run: Any, name: str) -> dict[str, Any]:
    return json.loads((run.root / "evidence" / name).read_text("utf-8"))


def evidence_text(run: Any, name: str) -> str:
    return (run.root / "evidence" / name).read_text("utf-8")


def origin_refs(run: Any) -> dict[str, str]:
    return run.origin_refs()


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, check=False)
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# A. Inert by default; targeted paths refused
# ---------------------------------------------------------------------------

class InertModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tray = Tray()

    def tearDown(self) -> None:
        self.tray.close()

    def test_default_mode_creates_nothing_and_sends_nothing(self) -> None:
        root = self.tray.base / "inert"
        out = io.StringIO()
        with redirect_stdout(out):
            code = m606.main(["--root", str(root), "--json"])
        self.assertEqual(code, 0)
        self.assertFalse(root.exists(), "plan mode created a root")
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["created_or_mutation_issued"], "none")
        self.assertIs(payload["would_execute"], False)
        self.assertTrue(payload["gates"]["G1_agent_submit"].startswith(
            "not-released"))
        self.assertEqual(payload["gates"]["operator_handoff"][
                         "single_invocation_live_dispatch"],
                         "refused by design")

    def test_root_guards_refuse_targeted_locations(self) -> None:
        cases = {
            "the project repository": str(REPO),
            "a repo worktree": str(REPO / ".teamcraft" / "worktrees" / "x"),
            "a repo path reached by ..": str(REPO / ".." / REPO.name),
            "$HOME": str(Path.home()),
            "/tmp": "/tmp",
            "the temp base itself": str(Path(tempfile.gettempdir())),
            "a relative path": "m606-relative-root",
            "empty": "",
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(m606.GuardError):
                    m606.validate_root(value, allow_non_temp=False)

    def test_populated_root_without_journal_is_refused(self) -> None:
        root = self.tray.root("populated")
        root.mkdir(parents=True)
        (root / "someone-elses-file").write_text("x", encoding="utf-8")
        with self.assertRaises(m606.GuardError) as ctx:
            m606.validate_root(str(root), allow_non_temp=False)
        self.assertIn("not empty", str(ctx.exception))
        with self.assertRaises(m606.GuardError) as ctx:
            m606.validate_root(str(root), allow_non_temp=False, for_cleanup=True)
        self.assertIn("no journal", str(ctx.exception))
        with self.assertRaises(m606.GuardError):
            m606.validate_root(str(root), allow_non_temp=False, for_resume=True)
        self.assertTrue((root / "someone-elses-file").exists(),
                        "a refusal must not have deleted anything")

    def test_a_root_awaiting_phase_two_points_at_resume_not_deletion(self) -> None:
        """An allocated handoff root must never be described as cleanup fodder."""
        root = self.tray.root("allocated-then-forgotten")
        (root / "evidence").mkdir(parents=True)
        (root / "evidence" / "journal.jsonl").write_text("", encoding="utf-8")
        (root / "evidence" / "handoff.json").write_text(
            json.dumps({"phase": "allocated-awaiting-operator"}), encoding="utf-8")
        with self.assertRaises(m606.GuardError) as ctx:
            m606.validate_root(str(root), allow_non_temp=False)
        message = str(ctx.exception)
        self.assertIn("--resume-handoff", message)
        self.assertIn("phase-1 handoff", message)
        self.assertNotIn("not empty", message)
        # Cleanup stays available, but only as an explicit, deliberate choice.
        self.assertIn("only if you intend to discard", message)

    def test_loopback_url_guards(self) -> None:
        bad = ("https://127.0.0.1:8000/v1", "http://localhost:8000/v1",
               "http://192.168.1.10:8000/v1", "http://u:t@127.0.0.1:8000/v1",
               "http://127.0.0.1", "http://127.0.0.1:8000",
               "http://127.0.0.1:8000/v1?key=1", "socks5://127.0.0.1:8000/v1")
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(m606.GuardError):
                    m606.validate_loopback(url)
        self.assertEqual(m606.validate_loopback(m606.LOOPBACK_OMLX_URL),
                         m606.LOOPBACK_OMLX_URL)

    def test_dead_route_and_unlisted_model_abort_without_fallback(self) -> None:
        with self.assertRaises(m606.GuardError) as ctx:
            m606.check_omlx(m606.LOOPBACK_OMLX_URL, MODEL,
                           getter=dead_omlx_getter)
        self.assertIn("no cloud fallback", str(ctx.exception).lower())
        with self.assertRaises(m606.GuardError) as ctx:
            m606.check_omlx(m606.LOOPBACK_OMLX_URL, "gpt-5-turbo",
                           getter=fake_omlx_getter)
        self.assertIn("not served by loopback", str(ctx.exception))

    def test_arg_combination_rules(self) -> None:
        parser = m606.build_parser()
        root = str(self.tray.base / "args")
        ref = str(Path(root) / "att.json")
        refused = {
            "consent without live-run":
                ["--root", root, "--consent-dispatch", "--resume-handoff",
                 "--herdr-pane", PANE, "--pane-model-evidence", ref,
                 "--expect-session-id", SESSION],
            "consent without resume (single-invocation live dispatch)":
                ["--confirm-live-run", "--root", root, "--consent-dispatch",
                 "--herdr-pane", PANE, "--pane-model-evidence", ref,
                 "--expect-session-id", SESSION],
            "consent without pane":
                ["--confirm-live-run", "--root", root, "--consent-dispatch",
                 "--resume-handoff", "--pane-model-evidence", ref,
                 "--expect-session-id", SESSION],
            "consent without pane-model evidence":
                ["--confirm-live-run", "--root", root, "--consent-dispatch",
                 "--resume-handoff", "--herdr-pane", PANE,
                 "--expect-session-id", SESSION],
            "consent without expect-session-id":
                ["--confirm-live-run", "--root", root, "--consent-dispatch",
                 "--resume-handoff", "--herdr-pane", PANE,
                 "--pane-model-evidence", ref],
            "human-accept without attribution":
                ["--root", root, "--human-accept"],
            "submit-wait without consent":
                ["--root", root, "--submit-wait"],
            "resume without live-run":
                ["--root", root, "--resume-handoff"],
            "resume with scripted leg":
                ["--confirm-live-run", "--root", root, "--resume-handoff",
                 "--scripted-worker-change"],
            "resume stopping before it resumes":
                ["--confirm-live-run", "--root", root, "--resume-handoff",
                 "--stop-after", "allocate"],
            "ceo-pane without live-run":
                ["--root", root, "--resume-handoff", "--consent-dispatch",
                 "--herdr-pane", PANE, "--pane-model-evidence", ref,
                 "--expect-session-id", SESSION, "--ceo-pane", PANE],
            "ceo-pane with a scripted leg (fabricates a CEO)":
                ["--confirm-live-run", "--root", root, "--scripted-worker-change",
                 "--ceo-pane", PANE],
            "ceo-pane without dispatch consent":
                ["--confirm-live-run", "--root", root, "--resume-handoff",
                 "--herdr-pane", PANE, "--pane-model-evidence", ref,
                 "--expect-session-id", SESSION, "--ceo-pane", PANE],
            "separate CEO pane without its own named session":
                ["--confirm-live-run", "--root", root, "--resume-handoff",
                 "--consent-dispatch", "--herdr-pane", PANE,
                 "--pane-model-evidence", ref, "--expect-session-id", SESSION,
                 "--ceo-pane", "w9:p2"],
            "wake delivery without a designated CEO":
                ["--confirm-live-run", "--root", root, "--resume-handoff",
                 "--consent-dispatch", "--herdr-pane", PANE,
                 "--pane-model-evidence", ref, "--expect-session-id", SESSION,
                 "--wake-deliver"],
            "CEO session id without a CEO pane":
                ["--confirm-live-run", "--root", root, "--resume-handoff",
                 "--consent-dispatch", "--herdr-pane", PANE,
                 "--pane-model-evidence", ref, "--expect-session-id", SESSION,
                 "--ceo-expect-session-id", SESSION],
            "CEO claim file without a CEO pane":
                ["--confirm-live-run", "--root", root, "--resume-handoff",
                 "--consent-dispatch", "--herdr-pane", PANE,
                 "--pane-model-evidence", ref, "--expect-session-id", SESSION,
                 "--ceo-claim-file", ref],
        }
        for label, argv in refused.items():
            with self.subTest(case=label):
                with self.assertRaises(m606.GuardError):
                    m606.validate_arg_combinations(parser.parse_args(argv))
        with self.assertRaises(SystemExit):
            # scripted evidence and live dispatch consent cannot both be claimed
            parser.parse_args(["--confirm-live-run", "--root", root,
                              "--scripted-worker-change", "--consent-dispatch"])
        for label, argv in {
            "scripted leg": ["--confirm-live-run", "--root", root,
                            "--scripted-worker-change"],
            "phase 1": ["--confirm-live-run", "--root", root, "--stop-after",
                        "allocate"],
            "phase 2": ["--confirm-live-run", "--root", root, "--resume-handoff",
                        "--consent-dispatch", "--herdr-pane", PANE,
                        "--pane-model-evidence", ref, "--expect-session-id",
                        SESSION],
        }.items():
            with self.subTest(allowed=label):
                m606.validate_arg_combinations(parser.parse_args(argv))

    def test_forbidden_herdr_verbs_issue_zero_commands(self) -> None:
        forbidden = [("tab", "create"), ("tab", "close"), ("pane", "focus"),
                     ("pane", "rename"), ("pane", "resize"), ("pane", "split"),
                     ("session", "attach"), ("workspace", "add"),
                     ("server", "stop"), ("app", "update")]
        for allow in (False, True):
            herdr = m606.Herdr(allow_dispatch=allow)
            for verb in forbidden:
                with self.subTest(verb=verb, allow_dispatch=allow):
                    with self.assertRaises(m606.GuardError):
                        herdr.run(*verb)
            self.assertEqual(herdr.calls, [],
                            "a refused verb must not reach the CLI")
        unconsented = m606.Herdr(allow_dispatch=False)
        with self.assertRaises(m606.GuardError):
            unconsented.run("agent", "prompt", PANE, "text")
        self.assertEqual(unconsented.calls, [])


# ---------------------------------------------------------------------------
# B. Provision: real disposable Git + SQLite, local-only proof
# ---------------------------------------------------------------------------

class ProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tray = Tray()

    def tearDown(self) -> None:
        self.tray.close()

    def provision(self, name: str, *extra: str,
                 herdr: FakeHerdr | None = None,
                 getter: Any = fake_omlx_getter) -> m606.M606LiveRun:
        run = make_run(self.tray, self.tray.root(name),
                      "--stop-after", "provision", "--keep-root", *extra,
                      herdr=herdr or FakeHerdr(), getter=getter)
        run.provision()
        return run

    def test_provision_records_passing_postconditions(self) -> None:
        run = self.provision("prov")
        manifest = evidence(run, "run-manifest.json")
        self.assertEqual([p for p in manifest["postconditions"] if not p["ok"]],
                         [], "provision postconditions failed")
        self.assertGreaterEqual(len(manifest["postconditions"]), 6)
        self.assertEqual(manifest["database"]["to"], storage.SCHEMA_VERSION)
        self.assertEqual(manifest["evidence_class"], "none")
        self.assertIs(manifest["pane_model_proof"]["attested"], False)
        self.assertEqual(manifest["delivery"]["disposition"],
                         m606.DELIVERY_UNKNOWN)
        self.assertEqual(manifest["non_claims"]["cloud"], "not-permitted")
        self.assertEqual(list(origin_refs(run)), ["refs/heads/main"],
                         "origin holds only the seeded base branch")
        self.assertTrue((run.root / "evidence" / "postconditions.json").is_file())

    def test_git_audit_proves_local_only_and_unforced(self) -> None:
        run = self.provision("audit")
        audit = evidence(run, "git-locality.json")
        self.assertIs(audit["local_origin_only"], True)
        self.assertEqual(audit["origin"], str(run.origin))
        self.assertEqual(audit["delivery_verbs"], [])
        self.assertIn("push", audit["bootstrap_verbs"])
        self.assertEqual(run.git_audit.force_flags(), [])
        self.assertNotIn("--force", json.dumps(audit))

    def test_dead_route_aborts_before_any_write(self) -> None:
        root = self.tray.root("unhealthy")
        run = make_run(self.tray, root, "--stop-after", "provision",
                      herdr=FakeHerdr(), getter=dead_omlx_getter)
        with self.assertRaises(m606.GuardError) as ctx:
            run.provision()
        self.assertIn("no cloud fallback", str(ctx.exception).lower())
        self.assertFalse(root.exists(),
                        "an unhealthy route must abort a pristine filesystem")

    def test_model_absent_from_catalog_aborts_before_any_write(self) -> None:
        root = self.tray.root("wrongmodel")
        run = make_run(self.tray, root, "--stop-after", "provision",
                      "--model", "gpt-5-turbo", herdr=FakeHerdr())
        with self.assertRaises(m606.GuardError) as ctx:
            run.provision()
        self.assertIn("not served by loopback", str(ctx.exception))
        self.assertFalse(root.exists())

    def test_allocate_writes_a_handoff_with_the_exact_allocated_cwd(self) -> None:
        herdr = FakeHerdr()
        root, handoff = phase1(self.tray, "handoff", herdr)
        self.assertEqual(handoff["phase"], "allocated-awaiting-operator")
        self.assertIs(handoff["submit_allowed_in_this_invocation"], False)
        self.assertEqual(handoff["allocated_cwd"],
                         m606.real(handoff["allocated_cwd"]))
        # The allocated cwd really exists and is a worktree of the clone.
        self.assertTrue(Path(handoff["allocated_cwd"]).is_dir())
        self.assertEqual(git(Path(handoff["allocated_cwd"]), "rev-parse",
                            "--abbrev-ref", "HEAD"), handoff["branch"])
        self.assertEqual(herdr.submits, [], "phase 1 submits nothing")
        self.assertNotIn("src/feature.py",
                        git(Path(handoff["allocated_cwd"]), "status",
                            "--porcelain") or "")
        # Nothing was delivered or cleaned in phase 1.
        self.assertFalse((root / "evidence" / "delivery.json").exists())
        self.assertFalse((root / "evidence" / "worktree-cleanup.json").exists())

    def test_live_runtime_db_is_untouched(self) -> None:
        db = REPO / ".teamcraft" / "teamcraft.db"
        if not db.exists():
            self.skipTest("no live runtime DB on this host")
        before = db.stat()
        self.provision("dbproof")
        after = db.stat()
        self.assertEqual((before.st_size, before.st_mtime),
                         (after.st_size, after.st_mtime),
                         "the live runtime DB changed during an offline run")


# ---------------------------------------------------------------------------
# C. Pane gates: reachable only in phase 2, and strict there
# ---------------------------------------------------------------------------

class PaneGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tray = Tray()
        self.ack = attestation(self.tray, "model-proof")

    def tearDown(self) -> None:
        self.tray.close()

    def _gated_root(self, name: str, herdr: FakeHerdr,
                   stand_in: str | None = "alloc") -> tuple[Path, dict]:
        """Phase 1, then place the pane where the caller says it stands."""
        root, handoff = phase1(self.tray, name, herdr)
        if stand_in == "alloc":
            herdr.stand_in(handoff["allocated_cwd"])
        elif stand_in == "clone":
            herdr.stand_in(root / "project")
        elif stand_in is None:
            pass
        return root, handoff

    def test_valid_gates_allow_exactly_one_submit(self) -> None:
        herdr = FakeHerdr()
        root, handoff = self._gated_root("gates-ok", herdr)
        run = phase2(self.tray, root, herdr, attest=str(self.ack),
                    extra=("--keep-root",))
        report = run.resume()
        self.assertEqual(len(herdr.submits), 1, "exactly one prompt, no retry")
        self.assertEqual(report["state"], "submitted")
        self.assertIs(report["pane_gate"]["cwd_matches_allocation"], True)
        self.assertIs(report["pane_gate"]["was_idle"], True)
        self.assertIs(report["pane_gate"]["session_stable"], True)
        self.assertEqual(report["pane_model_proof"]["status"],
                         "operator-attested")
        self.assertIs(report["pane_model_proof"]["host_trusted"], False)
        self.assertIs(report["capability_binding"]["durable_before_bind"], True)
        self.assertIs(report["topology_unchanged"], True)
        self.assertEqual(report["submit"]["prompt_ack"], "unknown")
        self.assertEqual(report["submit"]["state"], "submitted")
        self.assertIs(report["submit"]["host_trusted_receipt"], False)
        # The confirmed session binding is recorded only now, in phase 2.
        rows = run.conn.execute(
            "SELECT native_session_id, pane_id FROM sessions"
            " WHERE attempt_id=? ORDER BY created_at, id",
            (handoff["attempt_id"],)).fetchall()
        self.assertTrue(any(tuple(r)[:2] == (SESSION, PANE) for r in rows),
                        "phase 2 must record the confirmed pane/session")

    def test_wrong_cwd_prevents_submit(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("gate-cwd", herdr, stand_in="clone")
        phase2(self.tray, root, herdr, attest=str(self.ack))
        with self.assertRaises(m606.GuardError) as ctx:
            self._resume(root, herdr)
        self.assertIn("cwd", str(ctx.exception))
        self.assertEqual(herdr.submits, [],
                        "a pane outside the allocated worktree must not be "
                        "prompted")

    def _resume(self, root: Path, herdr: FakeHerdr, **kw: Any) -> Any:
        run = phase2(self.tray, root, herdr, attest=str(self.ack), **kw)
        return run.resume()

    def test_no_cwd_at_all_prevents_submit(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("gate-nocwd", herdr, stand_in=None)
        with self.assertRaises(m606.GuardError) as ctx:
            self._resume(root, herdr)
        self.assertIn("cwd", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_busy_pane_prevents_submit(self) -> None:
        herdr = FakeHerdr()
        root, handoff = self._gated_root("gate-busy", herdr)
        herdr.agent_status = "working"
        with self.assertRaises(m606.GuardError) as ctx:
            self._resume(root, herdr)
        self.assertIn("not idle", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_unstable_session_prevents_submit(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("gate-flicker", herdr)
        herdr.sessions = (SESSION, "ses_flickered_0002")
        run = phase2(self.tray, root, herdr, attest=str(self.ack))
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("unstable", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_expect_session_id_mismatch_prevents_submit(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("gate-others-session", herdr)
        run = phase2(self.tray, root, herdr, attest=str(self.ack),
                    session="ses_someone_else_entirely")
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("--expect-session-id", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_foreign_foreground_cwd_prevents_submit(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("gate-fg", herdr)
        herdr.foreground_cwd = str(Path(tempfile.gettempdir()))
        with self.assertRaises(m606.GuardError) as ctx:
            self._resume(root, herdr)
        self.assertIn("foreground cwd", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_wrong_agent_prevents_submit(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("gate-agent", herdr)
        herdr.agent = "codex"
        with self.assertRaises(m606.PostconditionError):
            self._resume(root, herdr)
        self.assertEqual(herdr.submits, [])

    def test_missing_attestation_fails_closed(self) -> None:
        herdr = FakeHerdr(screen=f">>> model {MODEL}")
        root, _ = self._gated_root("proof-missing", herdr)
        run = make_run(self.tray, root, "--resume-handoff", "--consent-dispatch",
                      "--herdr-pane", PANE, "--expect-session-id", SESSION,
                      herdr=herdr)
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("unproven", str(ctx.exception))
        self.assertEqual(herdr.submits, [],
                        "catalog presence alone must never authorize a prompt")

    def test_nonexistent_attestation_file_fails_closed(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("proof-missingfile", herdr)
        run = phase2(self.tray, root, herdr,
                    attest=str(self.tray.base / "nope.json"))
        with self.assertRaises(m606.GuardError):
            run.resume()
        self.assertEqual(herdr.submits, [])

    def test_wrong_model_attestation_fails_closed(self) -> None:
        other = attestation(self.tray, "other-model", model="llama-cloud-xl")
        herdr = FakeHerdr()
        root, _ = self._gated_root("proof-wrongmodel", herdr)
        run = phase2(self.tray, root, herdr, attest=str(other))
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("not the pinned", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_attestation_for_a_different_pane_fails_closed(self) -> None:
        foreign = attestation(self.tray, "foreign-pane", pane="w9:p77")
        herdr = FakeHerdr()
        root, _ = self._gated_root("proof-foreignpane", herdr)
        run = phase2(self.tray, root, herdr, attest=str(foreign))
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("does not belong to this pane", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_attestation_for_a_different_session_fails_closed(self) -> None:
        """An attestation naming another session is not evidence for this pane."""
        foreign = attestation(self.tray, "foreign-session",
                             session="ses_not_the_one_we_read")
        herdr = FakeHerdr()
        root, _ = self._gated_root("proof-foreignsession", herdr)
        # --expect-session-id matches the LIVE pane, so it is the attestation
        # mismatch alone that must stop the submit.
        run = make_run(self.tray, root, "--resume-handoff", "--consent-dispatch",
                      "--herdr-pane", PANE, "--pane-model-evidence", str(foreign),
                      "--expect-session-id", SESSION, herdr=herdr)
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("does not belong to this pane", str(ctx.exception))
        self.assertIn("session_id", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_unattributed_attestation_fails_closed(self) -> None:
        no_prov = attestation(self.tray, "no-provenance", provenance=None)
        herdr = FakeHerdr()
        root, _ = self._gated_root("proof-noprov", herdr)
        run = phase2(self.tray, root, herdr, attest=str(no_prov))
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("provenance", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_screen_observation_is_recorded_as_non_conclusive(self) -> None:
        herdr = FakeHerdr(screen=f">>> using {MODEL}\nready")
        root, _ = self._gated_root("screen-ok", herdr)
        run = phase2(self.tray, root, herdr, attest=str(self.ack))
        report = run.resume()
        proof = report["pane_model_proof"]
        self.assertIs(proof["screen_captured"], True)
        self.assertIs(proof["conclusive"], False)
        self.assertIs(proof["screen_mentions_pinned_model"], True)
        self.assertIn(MODEL, proof["model_ids_observed_on_screen"])
        self.assertIs(proof["host_trusted"], False)
        self.assertEqual(proof["status"], "operator-attested")
        obs = evidence(run, "pane-screen-observation.json")
        self.assertIs(obs["conclusive"], False)
        self.assertIn("supplements", obs["note"])


# ---------------------------------------------------------------------------
# D. Capability probes must be durable before binding
# ---------------------------------------------------------------------------

    def test_prompt_carries_binding_and_text_is_never_persisted(self) -> None:
        herdr = FakeHerdr()
        root, handoff = self._gated_root("prompt-hygiene", herdr)
        run = phase2(self.tray, root, herdr, attest=str(self.ack))
        run.resume()
        _, prompt = herdr.submits[0]
        for path in sorted((run.root / "evidence").glob("*.json")):
            self.assertNotIn(prompt, path.read_text("utf-8"),
                            f"{path.name} persisted the full prompt text")
        binding = evidence(run, "prompt-binding.json")
        self.assertEqual(list(binding), sorted(binding))
        for key in ("carries_alloc_path", "carries_branch", "carries_base_sha",
                    "carries_brief_digest", "carries_pinned_model",
                    "carries_protocol"):
            self.assertIs(binding[key], True, f"prompt omitted {key}")
        self.assertEqual(binding["prompt_sha256"],
                         m606.sha256_bytes(prompt.encode("utf-8")),
                         "the recorded digest must bind the exact bytes sent")
        self.assertIn(handoff["allocated_cwd"], prompt)
        self.assertIn(handoff["brief_digest"], prompt)

    def test_a_lost_submit_is_unknown_and_never_retried(self) -> None:
        herdr = FakeHerdr(submit_outcome="agent_blocked")
        root, _ = self._gated_root("submit-blocked", herdr)
        run = phase2(self.tray, root, herdr, attest=str(self.ack))
        with self.assertRaises(m606.PostconditionError) as ctx:
            run.resume()
        self.assertIn("accepted", str(ctx.exception))
        self.assertEqual(len(herdr.submits), 1,
                        "a refused submit must not be retried")
        self.assertEqual(evidence(run, "dispatch.json")["state"], "unknown")

    def test_phase2_without_consent_verifies_gates_but_submits_nothing(
            self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("phase2-noconsent", herdr)
        run = make_run(self.tray, root, "--resume-handoff", "--herdr-pane", PANE,
                      "--pane-model-evidence", str(self.ack),
                      "--expect-session-id", SESSION, herdr=herdr)
        report = run.resume()
        self.assertEqual(report["submit"], "not-consented")
        self.assertIs(report["would_submit"], True)
        self.assertIs(report["pane_gate"]["cwd_matches_allocation"], True)
        self.assertEqual(herdr.submits, [])

    def test_resume_detects_drift_and_refuses(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._gated_root("resume-drift", herdr)
        # Operator asks for a different model than phase 1 pinned.
        run = phase2(self.tray, root, herdr, attest=str(self.ack),
                    model=OTHER_MODEL)
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("drifted", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_resume_refuses_when_the_allocated_worktree_is_gone(self) -> None:
        herdr = FakeHerdr()
        root, handoff = self._gated_root("resume-noworktree", herdr)
        shutil.rmtree(handoff["allocated_cwd"], ignore_errors=True)
        run = phase2(self.tray, root, herdr, attest=str(self.ack))
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("no longer exists", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_resume_requires_a_handoff(self) -> None:
        herdr = FakeHerdr()
        root = self.tray.root("resume-nohandoff")
        run = make_run(self.tray, root, "--stop-after", "provision",
                      "--keep-root", herdr=herdr)
        run.provision()
        run2 = phase2(self.tray, root, herdr, attest=str(self.ack))
        with self.assertRaises(m606.GuardError) as ctx:
            run2.resume()
        self.assertIn("no handoff", str(ctx.exception))

    def test_single_invocation_live_dispatch_is_refused(self) -> None:
        """The operator handoff is structural, not advisory."""
        herdr = FakeHerdr()
        root = self.tray.root("single-run")
        herdr.stand_in(root / "project")
        run = make_run(self.tray, root, "--consent-dispatch", "--herdr-pane",
                      PANE, "--pane-model-evidence", str(self.ack),
                      "--expect-session-id", SESSION, herdr=herdr)
        with self.assertRaises(m606.GuardError) as ctx:
            run.dispatch()
        self.assertIn("cannot run in the same invocation", str(ctx.exception))
        self.assertEqual(herdr.submits, [])

    def test_preflight_enforces_herdr_transport_rules(self) -> None:
        ok = FakeHerdr()
        run = make_run(self.tray, self.tray.root("preflight-ok"), herdr=ok)
        out = run.preflight()
        self.assertEqual(out["herdr_protocol"], "22")
        self.assertIs(out["root_would_be_created"], True)
        for index, (label, override) in enumerate({
                "endpoint incompatible": {"server.endpoint_compatible": "no"},
                "protocol incompatible":
                    {"server.private_protocol_compatible": "no"},
                "restart pending": {"update.restart_needed": "yes"},
                "version skew": {"client.version": "0.9.2"},
                "server not running": {"server.status": "stopped"},
        }.items()):
            with self.subTest(case=label):
                bad = FakeHerdr(status_overrides=override)
                r = make_run(self.tray, self.tray.root(f"pf-{index}"), herdr=bad)
                with self.assertRaises(m606.GuardError):
                    r.preflight()


# ---------------------------------------------------------------------------
# E. Verification, delivery, cleanup — offline but real services
# ---------------------------------------------------------------------------


class CapabilityOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tray = Tray()

    def tearDown(self) -> None:
        self.tray.close()

    def test_probes_are_durable_before_bind(self) -> None:
        run = make_run(self.tray, self.tray.root("cap-order"), "--stop-after",
                      "provision", "--keep-root", herdr=FakeHerdr())
        run.provision()
        task, attempt, _alloc, _brief = run.provision_attempt_resources()
        binding = run.capture_and_bind_capabilities(attempt.id)
        self.assertIs(binding["durable_before_bind"], True)
        durable = run._durable_probe_digests(run.conn, run.project_row.id)
        self.assertIn(binding["provider_digest"], durable)
        self.assertIn(binding["harness_digest"], durable)

    def test_binding_an_unregistered_snapshot_is_refused(self) -> None:
        """The durability rule is the domain's, not a stub's convenience."""
        run = make_run(self.tray, self.tray.root("cap-negative"),
                      "--stop-after", "provision", "--keep-root",
                      herdr=FakeHerdr())
        run.provision()
        _task, attempt, _alloc, _brief = run.provision_attempt_resources()
        fresh_provider = run._provider_snapshot()      # never recorded
        recorded_harness = run._harness_snapshot()
        providers.record_capability_probe(run.conn, run.project_row.id,
                                        recorded_harness)
        with self.assertRaises(Exception) as ctx:
            providers.bind_attempt_capabilities(
                run.conn, attempt.id, harness=recorded_harness,
                provider=fresh_provider, model=MODEL)
        self.assertIn("not durably registered", str(ctx.exception))

    def test_provision_does_not_bind_before_an_attempt_exists(self) -> None:
        run = make_run(self.tray, self.tray.root("cap-nobind"), "--stop-after",
                      "provision", "--keep-root", herdr=FakeHerdr())
        run.provision()
        self.assertFalse((run.root / "evidence"
                         / "capability-binding.json").exists())
        self.assertTrue((run.root / "evidence" / "route-gate.json").is_file())

    def test_allocate_binds_and_phase2_revalidates_without_reprobing(self) -> None:
        herdr = FakeHerdr()
        root, handoff = phase1(self.tray, "cap-revalidate", herdr)
        digests = handoff["capability_digests"]
        self.assertTrue(digests["provider"] and digests["harness"])
        herdr.stand_in(handoff["allocated_cwd"])

        class ExplodingGetter:
            def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
                raise AssertionError("phase 2 must not re-probe the provider")

        run = make_run(self.tray, root, "--resume-handoff", "--herdr-pane", PANE,
                      "--pane-model-evidence", str(attestation(self.tray, "ack2")),
                      "--expect-session-id", SESSION, "--consent-dispatch",
                      herdr=herdr, getter=ExplodingGetter())
        report = run.resume()
        self.assertEqual(report["capability_binding"]["provider_digest"],
                         digests["provider"])
        self.assertIs(report["capability_binding"]["revalidated_on_resume"], True)


class RealClientIdentityTests(unittest.TestCase):
    """The real ``Herdr.identity`` contract, with a canned snapshot.

    No Herdr process is spawned: only ``snapshot()`` is stubbed, so the identity
    rules under test are the shipped ones.
    """

    class Canned(m606.Herdr):
        def __init__(self, snapshot: dict[str, Any]) -> None:
            super().__init__(allow_dispatch=False)
            self._snapshot = snapshot

        def snapshot(self) -> dict[str, Any]:
            return self._snapshot

    def _snapshot(self, **overrides: Any) -> dict[str, Any]:
        pane = {"pane_id": "w9:p1", "tab_id": "w9:t1", "workspace_id": "w9",
                "cwd": "/somewhere/allocated"}
        agent = {"pane_id": "w9:p1", "agent": "opencode", "name": "worker",
                 "agent_status": "idle", "terminal_id": "term_1",
                 "cwd": "/somewhere/allocated",
                 "agent_session": {"kind": "id", "source": "herdr:opencode",
                                  "value": "ses_abc"}}
        for key, value in overrides.items():
            if value is None:
                continue
            if key == "agent":
                agent["agent"] = value
            elif key == "session":
                agent["agent_session"] = value
            elif key == "panes":
                return {"panes": value, "agents": [agent], "tabs": []}
        return {"panes": [pane], "agents": [agent], "tabs": []}

    def test_identity_binds_a_matching_opencode_pane(self) -> None:
        bound = self.Canned(self._snapshot()).identity("w9:p1")
        self.assertEqual(bound["agent"], "opencode")
        self.assertEqual(bound["session_id"], "ses_abc")
        self.assertEqual(bound["cwd"], "/somewhere/allocated")
        self.assertEqual(bound["agent_status"], "idle")

    def test_wrong_agent_is_refused(self) -> None:
        with self.assertRaises(m606.PostconditionError) as ctx:
            self.Canned(self._snapshot(agent="codex")).identity("w9:p1")
        self.assertIn("not 'opencode'", str(ctx.exception))

    def test_missing_native_session_is_refused(self) -> None:
        with self.assertRaises(m606.PostconditionError) as ctx:
            self.Canned(self._snapshot(session={})).identity("w9:p1")
        self.assertIn("no native session id", str(ctx.exception))

    def test_absent_pane_is_refused(self) -> None:
        with self.assertRaises(m606.PostconditionError) as ctx:
            self.Canned(self._snapshot(panes=[])).identity("w9:p1")
        self.assertIn("not present in the live snapshot", str(ctx.exception))

    def test_wrong_expected_agent_is_refused(self) -> None:
        client = self.Canned(self._snapshot())
        client.expected_agent = "codex"
        with self.assertRaises(m606.PostconditionError):
            client.identity("w9:p1")

class DeliveryCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tray = Tray()

    def tearDown(self) -> None:
        self.tray.close()

    def _scripted(self, name: str, *extra: str,
                  keep: bool = True) -> m606.M606LiveRun:
        args = ["--scripted-worker-change"] + (["--keep-root"] if keep else [])
        run = make_run(self.tray, self.tray.root(name), *args, *extra,
                      herdr=FakeHerdr())
        return run

    def test_scripted_leg_is_labelled_not_real_agent_evidence(self) -> None:
        run = self._scripted("scripted-label")
        run.provision()
        record = run.run_worker_leg()
        self.assertEqual(record["evidence_class"], "scripted")
        self.assertIs(record["real_agent_evidence"], False)
        self.assertEqual(run.evidence_class, "scripted")
        self.assertIsNone(run.model_proof.get("attested"))

    def test_scripted_delivery_is_submitted_and_origin_gains_no_ref(self) -> None:
        run = self._scripted("deliver-submitted")
        run.provision()
        run.run_worker_leg()
        report = run.deliver()
        self.assertEqual(report["disposition"], m606.DELIVERY_SUBMITTED)
        self.assertEqual(list(origin_refs(run)), ["refs/heads/main"],
                         "a local-only delivery must never reach origin")
        delivery = evidence(run, "delivery.json")
        self.assertIs(delivery["human_accepted"], False)
        self.assertIsNone(delivery["ack_source"])
        self.assertEqual(domain.get_task(run.conn, run.task.id).status,
                         "verifying")
        self.assertEqual(domain.get_attempt(run.conn, run.attempt.id).status,
                         "finished")
        closed = run.close(remove_roots=False)
        self.assertEqual(closed["finalise"]["disposition"],
                         m606.DELIVERY_SUBMITTED)
        self.assertEqual(evidence(run, "run-manifest.json")["evidence_class"],
                         "scripted")

    def test_lying_change_is_refused_by_verifiers(self) -> None:
        """A change that lacks the required marker cannot be delivered."""
        run = self._scripted("deliver-lie", "--scripted-marker",
                             "I-CLAIMED-I-DID-IT")
        run.provision()
        run.run_worker_leg()
        with self.assertRaises(m606.PostconditionError) as ctx:
            run.deliver()
        self.assertIn("verifier verdict", str(ctx.exception))
        self.assertEqual(list(origin_refs(run)), ["refs/heads/main"])
        above = git(run.project, "rev-list", "--count",
                   f"{run.allocation.base_sha}..{run.allocation.branch}")
        self.assertEqual(above, "0", "a refused change must not have committed")
        self.assertEqual(evidence(run, "verification.json")["verdict"], "fail")

    def test_delivery_requires_an_allocated_attempt(self) -> None:
        run = self._scripted("deliver-noattempt")
        run.provision()
        with self.assertRaises(m606.GuardError) as ctx:
            run.deliver()
        self.assertIn("no attempt/allocation", str(ctx.exception))

    def test_delivery_requires_a_worker_report(self) -> None:
        """An allocated attempt with no durable WorkerReport cannot deliver."""
        herdr = FakeHerdr()
        run = make_run(self.tray, self.tray.root("deliver-noreport"),
                      "--stop-after", "allocate", "--keep-root", herdr=herdr)
        run.provision()
        run.allocate()
        with self.assertRaises(m606.GuardError) as ctx:
            run.deliver()
        self.assertIn("WorkerReport", str(ctx.exception))
        self.assertEqual(list(origin_refs(run)), ["refs/heads/main"])

    def test_unaccepted_workspace_is_preserved_then_reclaimable(self) -> None:
        run = self._scripted("cleanup-preserve", keep=False)
        report = run.execute()
        self.assertIs(report["ok"], True)
        cleanup = evidence(run, "worktree-cleanup.json")
        self.assertEqual(cleanup["action"], "preserved")
        self.assertIn("delivery-task-verifying", cleanup["reasons"])
        self.assertTrue(Path(run.allocation.worktree_path).exists(),
                        "unaccepted work must be preserved, not deleted")
        self.assertIs(report["root_retained"], True,
                      "a preserved workspace lives in the root; keep the root")
        self.assertEqual(run.manifest["cleanup"]["action"], "preserved")
        out = io.StringIO()
        with redirect_stdout(out):
            code = m606.main(["--cleanup-only", "--root", str(run.root),
                             "--json"])
        self.assertEqual(code, 0)
        self.assertFalse(run.root.exists(),
                        "journal reclaim must remove the root it created")

    def test_accepted_path_completes_and_removes_workspace(self) -> None:
        run = self._scripted("cleanup-accept", "--human-accept",
                             "--accept-actor", "human-reviewer",
                             "--accept-reason", "repo facts prove the change",
                             keep=False)
        report = run.execute()
        self.assertIs(report["ok"], True)
        self.assertIs(report["root_retained"], False)
        self.assertEqual(report["close"]["finalise"]["disposition"],
                         m606.DELIVERY_ACCEPTED)
        self.assertFalse(run.root.exists(),
                        "an accepted+cleaned run removes its own root")

    def test_cleanup_only_refuses_without_a_journal(self) -> None:
        root = self.tray.root("cleanup-nojournal")
        (root / "evidence").mkdir(parents=True)
        marker = root / "evidence" / "keep-me.txt"
        marker.write_text("foreign", encoding="utf-8")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = m606.main(["--cleanup-only", "--root", str(root)])
        self.assertEqual(code, 2, "no journal ⇒ nothing may be removed")
        self.assertIn("no journal", err.getvalue())
        self.assertTrue(marker.exists())

    def test_journal_cleanup_is_idempotent_and_replayable(self) -> None:
        run = self._scripted("cleanup-replay", keep=False)
        report = run.execute()
        # Unaccepted delivery ⇒ workspace preserved ⇒ root retained.
        self.assertIs(report["root_retained"], True)
        for attempt in range(2):
            out = io.StringIO()
            with redirect_stdout(out):
                code = m606.main(["--cleanup-only", "--root", str(run.root),
                                 "--json"])
            self.assertEqual(code, 0, f"replay {attempt} failed")
            payload = json.loads(out.getvalue())
            if attempt == 0:
                self.assertFalse(payload.get("already_cleaned", False))
            else:
                self.assertIs(payload["already_cleaned"], True)
        self.assertFalse(run.root.exists())

    def test_journal_refuses_to_sweep_foreign_siblings(self) -> None:
        run = self._scripted("cleanup-foreign", keep=False)
        run.execute()
        sibling = run.root.parent / "not-mine"
        sibling.mkdir()
        keep = sibling / "keep.txt"
        keep.write_text("foreign", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = m606.main(["--cleanup-only", "--root", str(run.root),
                             "--json"])
        self.assertEqual(code, 0)
        self.assertFalse(run.root.exists())
        self.assertTrue(keep.exists(), "a foreign sibling must never be swept")

    def test_resumed_phase_never_removes_a_root_it_did_not_create(self) -> None:
        herdr = FakeHerdr()
        root, handoff = phase1(self.tray, "resume-keeps-root", herdr)
        herdr.stand_in(handoff["allocated_cwd"])
        run = phase2(self.tray, root, herdr,
                    attest=str(attestation(self.tray, "ack-keep")),
                    extra=("--stop-after", "dispatch"))
        report = run.execute()
        self.assertIs(report["ok"], True)
        self.assertIs(report["root_retained"], True,
                      "phase 2 did not create the root, so it may not delete it")
        self.assertTrue(root.exists())


# ---------------------------------------------------------------------------
# F. Honest non-claims survive into the manifest
# ---------------------------------------------------------------------------

class ManifestHonestyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tray = Tray()

    def tearDown(self) -> None:
        self.tray.close()

    def _scripted(self) -> m606.M606LiveRun:
        run = make_run(self.tray, self.tray.root("manifest"),
                      "--scripted-worker-change", "--keep-root",
                      herdr=FakeHerdr())
        run.provision()
        run.run_worker_leg()
        run.deliver()
        return run

    def test_non_claims_never_overstate(self) -> None:
        run = self._scripted()
        run.close(remove_roots=False)
        claims = evidence(run, "run-manifest.json")["non_claims"]
        self.assertEqual(claims["prompt_ack"], "unknown")
        self.assertEqual(claims["event_stream"], "unknown")
        self.assertIs(claims["host_trusted_receipt"], False)
        self.assertIs(claims["push"], False)
        self.assertEqual(claims["cloud"], "not-permitted")
        self.assertEqual(claims["claude"], "pending:PG-1")

    def test_bundle_scan_reports_what_it_scanned(self) -> None:
        run = self._scripted()
        run.close(remove_roots=False)
        scan = evidence(run, "run-manifest.json")["bundle_scan"]
        self.assertGreater(scan["files_scanned"], 0)
        self.assertIs(scan["bundle_withheld"], False)
        self.assertEqual(scan["secret_hits"], {})

    def test_stage_matrix_follows_declared_order(self) -> None:
        run = self._scripted()
        run.close(remove_roots=False)
        matrix = evidence(run, "run-manifest.json")["stage_matrix"]
        self.assertEqual(matrix["provision"], "executed")
        self.assertEqual(matrix["deliver"], "executed")
        self.assertEqual(matrix["cleanup"], "pending")

    def test_persisted_artifacts_carry_no_credential_shapes(self) -> None:
        run = self._scripted()
        run.close(remove_roots=False)
        for path in sorted((run.root / "evidence").glob("*.json")):
            text = path.read_text("utf-8")
            self.assertEqual(m606.secret_shapes(text), [],
                            f"{path.name} carries credential-shaped content")


# ---------------------------------------------------------------------------
# G. PG-7 CEO bootstrap and Wake integration (disposable DB only)
#
# These exercise the REAL lifecycle APIs — runtime_adapter.sync_state,
# ceo_initialization.discover_and_issue / mark_bootstrap_unknown /
# record_bootstrap_submitted_assumed, wake.ingest_events / admit_pending_wake /
# record_submission_result — against the run-local SQLite file, with only the pane
# transport faked.  Nothing here patches a gate to pass.
# ---------------------------------------------------------------------------

def ceo_state_dict(project: Path, *, pane: str = PANE, session: str = SESSION,
                   terminal: str = TERMINAL, name: str = "ceo",
                   kind: str = "opencode") -> dict[str, Any]:
    """A canonical-CEO runtime state, built independently of the runner.

    Written by the tests so that ``sync_state``'s own rules (one CEO per project,
    immutable team identity, lineage legality) are checked against something that
    is not the code under test's own assumptions.
    """
    herdr_session = {"workspace_id": "w9", "tab_id": "w9:t1", "pane_id": pane,
                     "terminal_id": terminal, "kind": "id",
                     "source": "herdr:opencode", "value": session}
    member = {"name": name, "role": "ceo",
              "responsibility": "coordinate", "kind": kind,
              "task": "coordinate the disposable project", "ownership": [],
              "read_only": False, "cwd": str(project), "from_pane_id": None,
              "pane_id": pane, "terminal_id": terminal,
              "agent_session": {"kind": "id", "source": "herdr:opencode",
                               "value": session},
              "agent_args": [], "direction": None, "start_timeout_ms": 30_000,
              "phase": "pane_created", "prompt_may_have_been_delivered": False,
              "live_state": "idle", "error": None, "task_id": None,
              "attempt_id": None, "session_id": None, "created_at": 1}
    return {"team_id": "m606-test-team", "project_root": str(project),
            "created_at": 1, "herdr_session": herdr_session,
            "members": {name: member}}


def q(conn: Any, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def ceo_row(conn: Any) -> tuple[Any, ...] | None:
    rows = q(conn, "SELECT state, receipt_source, identity_mode,"
                   " canonical_ceo_name, nonce_consumed_at, protocol_version,"
                   " protocol_hash, nonce_hash FROM ceo_initializations")
    return rows[0] if rows else None


def wake_state(conn: Any, wake_id: str) -> str | None:
    rows = q(conn, "SELECT state FROM wakes WHERE id=?", wake_id)
    return rows[0][0] if rows else None


def ack_cursor(conn: Any, project_id: str) -> int:
    cursor = wake.get_cursor(conn, project_id)
    return 0 if cursor is None else int(getattr(cursor, "acknowledged_seq", 0))


def nonce_leaks(root: Path, nonce_hash: str) -> list[str]:
    """Return evidence files holding a token that hashes to the stored nonce.

    The plaintext is never available to the test, so this proves absence the only
    way it can be proved: every 40-60 char token persisted on disk is hashed and
    compared against the digest the database itself stores.
    """
    token = re.compile(r"[A-Za-z0-9_-]{40,60}")
    hits = []
    for path in sorted((root / "evidence").glob("*.json")):
        for candidate in set(token.findall(path.read_text("utf-8"))):
            if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == nonce_hash:
                hits.append(path.name)
    return hits


class CeoBootstrapTests(unittest.TestCase):
    """PG-7: canonical CEO identity, one-shot challenge, fence, honest receipt."""

    def setUp(self) -> None:
        self.tray = Tray()
        self.ack = attestation(self.tray, "ceo-model-proof")

    def tearDown(self) -> None:
        self.tray.close()

    def _allocated(self, herdr: FakeHerdr, name: str) -> tuple[Path, dict]:
        root, handoff = phase1(self.tray, name, herdr)
        herdr.stand_in(handoff["allocated_cwd"])
        return root, handoff

    def _ceo_run(self, root: Path, herdr: FakeHerdr, *, session: str = SESSION,
                 ceo_pane: str = PANE, ceo_session: str | None = None,
                 wake_deliver: bool = False, claim: Path | None = None,
                 attest: str | None = None,
                 extra: tuple[str, ...] = ()) -> m606.M606LiveRun:
        args = ["--resume-handoff", "--consent-dispatch", "--herdr-pane", PANE,
                "--pane-model-evidence", attest or str(self.ack),
                "--expect-session-id", session, "--ceo-pane", ceo_pane]
        if ceo_session:
            args += ["--ceo-expect-session-id", ceo_session]
        if wake_deliver:
            args.append("--wake-deliver")
        if claim:
            args += ["--ceo-claim-file", str(claim)]
        return make_run(self.tray, root, *args, *extra, herdr=herdr)

    def test_ceo_bootstrap_reaches_initialized_under_assumed_default(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-ok")
        run = self._ceo_run(root, herdr, extra=("--stop-after", "dispatch"))
        report = run.resume()
        pid = run.project_row.id

        self.assertEqual(run.ceo_stage, "executed")
        self.assertEqual(report["state"], "submitted")
        # Canonical identity: exactly one CEO, bound session/terminal/attempt.
        ceos = q(run.conn, "SELECT name, role, kind, terminal_id, task_id,"
                           " attempt_id, session_id FROM members"
                           " WHERE role='ceo'")
        self.assertEqual(len(ceos), 1, "exactly one canonical CEO")
        self.assertEqual(ceos[0][0], "ceo")
        self.assertEqual(ceos[0][2], "opencode")
        self.assertTrue(all(v is not None for v in ceos[0][3:]),
                        "CEO must bind terminal, task, attempt and session")
        self.assertEqual(
            q(run.conn, "SELECT project_root FROM runtime_meta WHERE id=1")[0][0],
            str(run.project), "team identity points at the disposable project")
        self.assertEqual(
            q(run.conn, "SELECT protocol_version, protocol_hash FROM attempts"
                        " WHERE id=?", ceos[0][5])[0],
            ("1.0.0", m606.CANONICAL_PROTOCOL_SHA256),
            "the CEO attempt carries the immutable protocol pin")

        row = ceo_row(run.conn)
        self.assertIsNotNone(row, "an initialization record must exist")
        state, receipt, mode, name, consumed, _pv, _ph, _nh = row[:8]
        self.assertEqual((state, receipt, mode, name),
                         ("initialized", "assumed_default", "native", "ceo"))
        self.assertIsNotNone(consumed, "the challenge must be marked consumed")
        # The gate the Scheduler/Wake paths consult is genuinely open now.
        self.assertEqual(ceo_initialization.require_initialized(run.conn, pid)
                         .state, "initialized")
        # Three distinct prompts, each sent once: CEO, worker, wake.
        digests = [m606.sha256_bytes(p.encode()) for _, p in herdr.submits]
        self.assertEqual(len(herdr.submits), 2,
                         "without --wake-deliver only CEO + worker are sent")
        self.assertEqual(len(set(digests)), len(digests), "no prompt is repeated")
        self.assertIs(report["wake"]["ceo_acknowledged"], False)
        self.assertEqual(ack_cursor(run.conn, pid), 0,
                        "no CEO acknowledgement may be fabricated")
        self.assertFalse(nonce_leaks(root, row[7]),
                        "the nonce plaintext must never reach disk")
        challenge = evidence(run, "ceo-challenge.json")
        self.assertEqual(challenge["nonce_sha256"], row[7])
        self.assertNotIn("nonce", challenge, "only the digest is recorded")
        self.assertEqual(challenge["state"], "discovered")
        bootstrap = evidence(run, "ceo-bootstrap.json")
        self.assertIs(bootstrap["fenced_before_submit"], True)
        self.assertIs(bootstrap["host_trusted"], False)
        self.assertIs(bootstrap["duplicate_sends"], 1)

    def test_busy_ceo_pane_issues_no_challenge_at_all(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-busy")
        herdr.agent_status = "working"
        run = self._ceo_run(root, herdr)
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("not idle", str(ctx.exception))
        self.assertIsNone(ceo_row(run.conn),
                        "no nonce may be issued for a busy pane")
        self.assertEqual(herdr.submits, [])

    def test_ceo_session_mismatch_issues_no_challenge(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-others-session")
        run = self._ceo_run(root, herdr, ceo_session="ses_not_the_designated_one")
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("did not name", str(ctx.exception))
        self.assertIsNone(ceo_row(run.conn))
        self.assertEqual(herdr.submits, [])

    def test_ceo_pane_outside_the_disposable_world_is_refused(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-foreign-cwd")
        herdr.stand_in(Path(tempfile.gettempdir()))
        run = self._ceo_run(root, herdr)
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("disposable world", str(ctx.exception))
        self.assertIsNone(ceo_row(run.conn))
        self.assertEqual(herdr.submits, [])

    def test_a_lost_ceo_submit_leaves_the_challenge_fenced(self) -> None:
        """Fencing precedes the prompt, so an unresolved submit is recoverable."""
        herdr = FakeHerdr(submit_outcome="agent_blocked")
        root, _ = self._allocated(herdr, "ceo-lost-submit")
        run = self._ceo_run(root, herdr)
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("did not complete", str(ctx.exception))
        pid = run.project_row.id
        state, receipt, _mode, _name, consumed, *_rest = ceo_row(run.conn)
        self.assertEqual(state, "unknown",
                         "the challenge stays fenced, it is not reset or replayed")
        self.assertIsNone(consumed)
        self.assertEqual(receipt, "unverified",
                         "nothing may be assumed from an unresolved transport")
        with self.assertRaises(ceo_initialization.CEOInitializationGateClosed):
            ceo_initialization.require_initialized(run.conn, pid)
        self.assertEqual(len(herdr.submits), 1,
                        "the worker prompt must not follow an unresolved bootstrap")
        report = evidence(run, "ceo-bootstrap.json")
        self.assertEqual(report["action"], "halted")
        self.assertEqual(report["transport"], "unknown")
        self.assertIs(report["ceo_initialized"], False)
        self.assertEqual(report["duplicate_sends"], 0)

    def test_a_consumed_challenge_cannot_be_reissued(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-no-resend")
        run = self._ceo_run(root, herdr)
        run.resume()
        pid = run.project_row.id
        record, nonce = ceo_initialization.discover_and_issue(run.conn, pid, "ceo")
        self.assertIsNone(nonce, "an initialized CEO never receives a new nonce")
        self.assertEqual(record.state, "initialized")
        self.assertEqual(len(herdr.submits), 2, "no further prompt was sent")

    def test_agent_claim_never_initializes_nor_upgrades_the_receipt(
            self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-claim")
        claim = self.tray.base / "ceo-echo.json"
        claim.write_text(json.dumps({"project_id": "echoed",
                                    "protocol_version": "1.0.0",
                                    "nonce": "an-agent-echoed-value"}),
                         encoding="utf-8")
        run = self._ceo_run(root, herdr, claim=claim)
        run.resume()
        row = ceo_row(run.conn)
        self.assertEqual(row[0], "initialized")
        self.assertEqual(row[1], "assumed_default",
                         "a self-claim may not upgrade the receipt source")
        report = evidence(run, "ceo-bootstrap.json")
        self.assertEqual(report["claim"]["retained_as"],
                         "unverified audit material")
        self.assertIs(report["claim"]["can_initialize"], False)
        # The provenance the domain recorded is immutable at the schema level:
        # it can never be erased to 'unverified' (nor quietly restated) later.
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with storage.transaction(run.conn):
                run.conn.execute(
                    "UPDATE ceo_initializations SET receipt_source='unverified'")
                run.conn.commit()
        self.assertIn("immutable", str(ctx.exception))

    def test_missing_claim_file_fails_closed_before_any_claim_is_recorded(
            self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-claim-missing")
        run = self._ceo_run(root, herdr,
                           claim=self.tray.base / "absent-echo.json")
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("does not exist", str(ctx.exception))
        # The bootstrap itself succeeded; only the optional claim was refused.
        self.assertEqual(ceo_row(run.conn)[0], "initialized")

    def test_claim_file_with_invalid_json_is_refused(self) -> None:
        herdr = FakeHerdr()
        root, _ = self._allocated(herdr, "ceo-claim-broken")
        bad = self.tray.base / "broken-echo.json"
        bad.write_text("{not json", encoding="utf-8")
        run = self._ceo_run(root, herdr, claim=bad)
        with self.assertRaises(m606.GuardError) as ctx:
            run.resume()
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_ceo_leg_happens_before_the_worker_prompt(self) -> None:
        """Ordering is observable in durable state, not just in a report flag."""
        herdr = FakeHerdr(submit_outcome="agent_blocked")
        root, _ = self._allocated(herdr, "ceo-order")
        run = self._ceo_run(root, herdr)
        with self.assertRaises(m606.GuardError):
            run.resume()
        # The single submit that happened was the CEO challenge, and the CEO
        # record advanced to 'unknown' — which only the pre-prompt fence does.
        self.assertEqual(len(herdr.submits), 1)
        self.assertEqual(ceo_row(run.conn)[0], "unknown")
        self.assertEqual(q(run.conn, "SELECT COUNT(*) FROM attempts"
                                    " WHERE protocol_version IS NOT NULL")[0][0], 2,
                         "CEO attempt created alongside the worker attempt")
        self.assertFalse((root / "evidence" / "dispatch.json").exists(),
                        "the worker leg must not have run")


class WakeIntegrationTests(unittest.TestCase):
    """Wake intake/admission/delivery, recorded without overstating anything."""

    def setUp(self) -> None:
        self.tray = Tray()
        self.ack = attestation(self.tray, "wake-model-proof")

    def tearDown(self) -> None:
        self.tray.close()

    def _bootstrapped(self, name: str, *, wake_deliver: bool,
                     herdr: FakeHerdr | None = None
                     ) -> tuple[m606.M606LiveRun, FakeHerdr, Path]:
        herdr = herdr or FakeHerdr()
        root, _ = phase1(self.tray, name, herdr)
        herdr.stand_in(json.loads((root / "evidence"
                                  / "handoff.json").read_text())["allocated_cwd"])
        args = ["--resume-handoff", "--consent-dispatch", "--herdr-pane", PANE,
                "--pane-model-evidence", str(self.ack),
                "--expect-session-id", SESSION, "--ceo-pane", PANE]
        if wake_deliver:
            args.append("--wake-deliver")
        run = make_run(self.tray, root, *args, "--stop-after", "dispatch",
                      herdr=herdr)
        run.resume()
        return run, herdr, root

    def test_admission_without_delivery_is_reported_as_undelivered(
            self) -> None:
        run, herdr, _root = self._bootstrapped("wake-admit-only",
                                               wake_deliver=False)
        report = evidence(run, "wake.json")
        pid = run.project_row.id
        self.assertEqual(report["transport"], "admitted-not-delivered")
        self.assertIsNone(report["outcome"],
                         "no transport outcome may be claimed for an undelivered "
                         "wake — the worker's outcome is not the wake's")
        self.assertEqual(report["duplicate_sends"], 0)
        self.assertEqual(wake_state(run.conn, report["wake_id"]), "sending",
                         "admission leaves a durable 'sending' row for a sender")
        self.assertEqual(len(herdr.submits), 2, "CEO + worker only")
        self.assertEqual(ack_cursor(run.conn, pid), 0)

    def test_delivered_wake_records_submitted_but_never_acknowledged(
            self) -> None:
        run, herdr, _root = self._bootstrapped("wake-deliver",
                                               wake_deliver=True)
        report = evidence(run, "wake.json")
        pid = run.project_row.id
        self.assertEqual(report["transport"], "delivered")
        self.assertEqual(report["outcome"], "submitted")
        self.assertEqual(wake_state(run.conn, report["wake_id"]), "submitted")
        self.assertIs(report["ceo_acknowledged"], False)
        self.assertEqual(ack_cursor(run.conn, pid), 0,
                         "transport success is not CEO acknowledgement")
        self.assertEqual(len(herdr.submits), 3)
        self.assertNotEqual(report["delivery"]["prompt_sha256"],
                           evidence(run, "dispatch.json")["prompt_sha256"],
                           "the wake prompt is its own message, not a re-send")

    def test_an_unresolved_wake_delivery_is_unknown_not_a_retry(self) -> None:
        """A wake whose transport is unresolved lands on 'unknown', once."""
        herdr = FakeHerdr()
        herdr.fail_submit_on = (3, "agent_blocked")   # CEO, worker, then the wake
        run, _herdr, _root = self._bootstrapped("wake-unresolved",
                                               wake_deliver=True, herdr=herdr)
        report = evidence(run, "wake.json")
        pid = run.project_row.id
        self.assertEqual(report["outcome"], "unknown")
        self.assertEqual(report["transport"], "unresolved")
        self.assertEqual(wake_state(run.conn, report["wake_id"]), "unknown",
                        "an unresolved effect must be durable, not optimistic")
        self.assertIs(report["ceo_acknowledged"], False)
        self.assertEqual(report["duplicate_sends"], 1,
                        "the runner never re-sends an unresolved wake")
        self.assertEqual(len(herdr.submits), 3, "CEO + worker + one wake")
        self.assertEqual(ack_cursor(run.conn, pid), 0)
        detail = run.conn.execute(
            "SELECT detail FROM wakes WHERE id=?",
            (report["wake_id"],)).fetchone()[0]
        self.assertIn("reconciled", str(detail))
        self.assertIn("must not be re-sent", str(detail))

    def test_a_second_wake_cannot_be_admitted_while_one_is_unresolved(
            self) -> None:
        """The domain refuses a second in-flight delivery; nothing is claimed."""
        run, herdr, _root = self._bootstrapped("wake-inflight",
                                              wake_deliver=False)
        self.assertEqual(len(herdr.submits), 2)
        first = evidence(run, "wake.json")["wake_id"]
        self.assertEqual(wake_state(run.conn, first), "sending")
        # Same run, fresh intake: a second wake exists, but delivery of it must
        # wait for the first to be reconciled — the honest report says so.
        report = run.wake_leg(run.evidence, "submitted")
        self.assertEqual(report["state"], "pending")
        self.assertEqual(report["transport"], "not-attempted:deferred")
        self.assertIsNone(report["outcome"])
        self.assertIn("wake_sending", str(report["deferred_reason"]),
                      "the deferral must name the in-flight wake")
        self.assertEqual(report["duplicate_sends"], 0)
        self.assertEqual(len(herdr.submits), 2,
                        "a deferred wake must not touch the transport at all")
        self.assertEqual(wake_state(run.conn, first), "sending",
                        "the first wake's durable state is untouched")

    def test_deferred_admission_claims_nothing(self) -> None:
        run, _herdr, _root = self._bootstrapped("wake-deferred",
                                                wake_deliver=False)
        pid = run.project_row.id
        if not [r for r in q(run.conn, "SELECT id FROM wakes"
                                      " WHERE state='pending'")]:
            self.skipTest("no pending wake left to defer")
        lease = run.lease()
        wake.record_wake_budget(run.conn, pid, lease, max_wakes_per_window=0,
                               actor="test", reason="exhaust the allowance")
        report = run.wake_leg(run.evidence, "submitted")
        self.assertEqual(report["transport"], "not-attempted:deferred")
        self.assertIsNone(report["outcome"])
        self.assertEqual(report["state"], "pending",
                         "a deferred wake stays pending; nothing is sent")
        self.assertEqual(report["duplicate_sends"], 0)
        self.assertIs(report["ceo_acknowledged"], False)

    def test_without_a_designated_ceo_the_wake_stays_gated(self) -> None:
        herdr = FakeHerdr()
        root, _ = phase1(self.tray, "wake-no-ceo", herdr)
        herdr.stand_in(json.loads((root / "evidence"
                                  / "handoff.json").read_text())["allocated_cwd"])
        run = make_run(self.tray, root, "--resume-handoff", "--consent-dispatch",
                      "--herdr-pane", PANE, "--pane-model-evidence", str(self.ack),
                      "--expect-session-id", SESSION, "--stop-after", "dispatch",
                      herdr=herdr)
        run.resume()
        report = evidence(run, "wake.json")
        self.assertEqual(report["state"], "pending:admission-blocked")
        self.assertEqual(report["transport"], "not-attempted")
        self.assertIsNone(report["outcome"],
                         "no outcome may be recorded for a wake that was gated")
        self.assertIsNone(ceo_row(run.conn))
        self.assertEqual(len(herdr.submits), 1, "only the worker prompt was sent")
        self.assertEqual(run.ceo_stage, "not-requested",
                         "no CEO leg may run without a designated CEO pane")
        run.close(remove_roots=False)
        claims = evidence(run, "run-manifest.json")["non_claims"]
        self.assertEqual(claims["wake_transport"], "not-attempted")
        self.assertEqual(claims["pg7"], "open:not-requested")
        self.assertIn("not-attempted (no --ceo-pane)", claims["ceo_receipt"])

    def test_acknowledge_wake_is_never_called(self) -> None:
        calls: list[Any] = []
        original = wake.acknowledge_wake

        def spy(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            raise AssertionError("the runner must never acknowledge a wake")

        m606.wake.acknowledge_wake = spy          # type: ignore[assignment]
        try:
            self._bootstrapped("wake-no-ack", wake_deliver=True)
        finally:
            m606.wake.acknowledge_wake = original  # type: ignore[assignment]
        self.assertEqual(calls, [],
                         "CEO acknowledgement must never be inferred or fabricated")

    def test_manifest_reports_ceo_and_wake_honestly(self) -> None:
        run, _herdr, root = self._bootstrapped("wake-manifest",
                                               wake_deliver=True)
        run.close(remove_roots=False)
        manifest = evidence(run, "run-manifest.json")
        claims = manifest["non_claims"]
        self.assertEqual(claims["ceo_receipt"],
                         "assumed_default (transport succeeded; NOT host-verified)")
        self.assertEqual(claims["wake_transport"], "delivered")
        self.assertIn("not-asserted", claims["ceo_acknowledgement"])
        self.assertEqual(claims["pg7"], "bootstrap+wake-executed")
        self.assertIs(claims["host_trusted_receipt"], False)
        self.assertEqual(manifest["stage_matrix"]["ceo"], "executed")
        self.assertEqual(manifest["ceo"]["receipt_source"], "assumed_default")
        self.assertIs(manifest["ceo"]["host_trusted"], False)
        scan = manifest["ephemeral_leak_scan"]
        self.assertEqual(scan["tokens_checked"], 1)
        self.assertEqual(scan["leaks"], {})
        # One-use secrets are named only by digest prefix, never in the clear.
        self.assertTrue(all(t.endswith("…" ) and len(t) == 13
                            for t in scan["tokens"]))
        self.assertTrue((root / "evidence" / "ceo-bootstrap.json").is_file())


class FixedClock:
    """A clock whose reading the test controls (the domain calls ``.now()``)."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


class CeoDomainGuaranteesTests(unittest.TestCase):
    """The guarantees the PG-7 leg relies on, proven against the real domain."""

    def setUp(self) -> None:
        self.tray = Tray()

    def tearDown(self) -> None:
        self.tray.close()

    def _world(self, name: str) -> tuple[m606.M606LiveRun, str]:
        herdr = FakeHerdr()
        run = make_run(self.tray, self.tray.root(name), "--stop-after",
                      "allocate", "--keep-root", herdr=herdr)
        run.provision()
        run.allocate()
        pid = str(run.project_row.id)
        runtime_adapter.sync_state(run.conn, ceo_state_dict(run.project))
        return run, pid

    def test_an_expired_challenge_can_never_be_accepted(self) -> None:
        run, pid = self._world("ceo-expiry")
        _record, nonce = ceo_initialization.discover_and_issue(run.conn, pid, "ceo")
        self.assertIsNotNone(nonce)
        lease = run.lease()
        runtime_adapter.admit_ceo_bootstrap_effect(run.conn, pid, nonce,
                                                  lease=lease)
        later = FixedClock(datetime.now(timezone.utc) + timedelta(minutes=10))
        # The domain itself refuses the replay…
        with self.assertRaises(ceo_initialization.CEOInitializationReplayError):
            ceo_initialization.record_bootstrap_submitted_assumed(
                run.conn, pid, nonce, native_session_id=SESSION,
                session_state="idle", lease=lease, clock=later)
        # …and so does the runner-facing adapter, in its own vocabulary.
        with self.assertRaises(runtime_adapter.RuntimeAdapterError) as ctx:
            runtime_adapter.record_ceo_bootstrap_submission_assumed(
                run.conn, pid, nonce, native_session_id=SESSION,
                session_state="idle", lease=lease, clock=later)
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(ceo_row(run.conn)[0], "unknown",
                        "an expired challenge stays fenced, it is never reset")
        with self.assertRaises(ceo_initialization.CEOInitializationGateClosed):
            ceo_initialization.require_initialized(run.conn, pid)

    def test_two_canonical_ceos_are_refused(self) -> None:
        run, _pid = self._world("ceo-second")
        state = ceo_state_dict(run.project)
        state["members"]["ceo-two"] = dict(state["members"]["ceo"],
                                          name="ceo-two")
        with self.assertRaises(runtime_adapter.RuntimeAdapterError) as ctx:
            runtime_adapter.sync_state(run.conn, state)
        self.assertIn("only one canonical CEO", str(ctx.exception))
        self.assertEqual([r[0] for r in q(run.conn, "SELECT name FROM members"
                                                   " WHERE role='ceo'")], ["ceo"],
                         "the rejected state wrote nothing")
        # A "CEO" observed in some other pane/terminal cannot be adopted either:
        # the durable team identity is pinned to the Herdr session it was created
        # in, and existing history may not silently disappear.
        other = ceo_state_dict(run.project, pane="w9:p2", terminal="term_two",
                               name="ceo-two")
        with self.assertRaises(runtime_adapter.RuntimeAdapterError):
            runtime_adapter.sync_state(run.conn, other)
        self.assertEqual(len(q(run.conn, "SELECT name FROM members"
                                        " WHERE role='ceo'")), 1)

    def test_team_identity_is_immutable_once_set(self) -> None:
        run, _pid = self._world("ceo-team-immutable")
        state = ceo_state_dict(run.project)
        state["team_id"] = "a-different-team"
        with self.assertRaises(runtime_adapter.RuntimeAdapterError):
            runtime_adapter.sync_state(run.conn, state)

    def test_a_second_ceo_harness_kind_is_refused(self) -> None:
        run, pid = self._world("ceo-bad-kind")
        with self.assertRaises(ceo_initialization.CEOInitializationError):
            ceo_initialization.discover_and_issue(run.conn, pid, "not-the-ceo")

    def test_initialized_record_survives_a_reconnect(self) -> None:
        """The gate opens from durable state, not from in-memory goodwill."""
        herdr = FakeHerdr()
        root, _ = phase1(self.tray, "ceo-durable", herdr)
        herdr.stand_in(json.loads((root / "evidence"
                                  / "handoff.json").read_text())["allocated_cwd"])
        run = make_run(self.tray, root, "--resume-handoff", "--consent-dispatch",
                      "--herdr-pane", PANE, "--pane-model-evidence",
                      str(attestation(self.tray, "dur-proof")),
                      "--expect-session-id", SESSION, "--ceo-pane", PANE,
                      "--stop-after", "dispatch", herdr=herdr)
        run.resume()
        pid = run.project_row.id
        run.conn.close()
        fresh = storage.connect(root / "teamcraft.db",
                               allow_unverified_location=True)
        self.tray.track(type("C", (), {"conn": fresh})())
        self.assertEqual(ceo_initialization.require_initialized(fresh, pid)
                         .receipt_source, "assumed_default")
        self.assertEqual(ceo_row(fresh)[0], "initialized")


if __name__ == "__main__":
    unittest.main()
