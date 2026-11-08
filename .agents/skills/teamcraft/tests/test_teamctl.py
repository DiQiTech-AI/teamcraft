from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "teamctl.py"
SPEC = importlib.util.spec_from_file_location("herdr_teamctl", SCRIPT)
assert SPEC and SPEC.loader
teamctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(teamctl)


def make_member(name: str, ownership: list[str], **changes):
    member = {
        "name": name,
        "role": "developer",
        "responsibility": "Implement the assigned scope.",
        "kind": "codex",
        "task": "Implement and verify.",
        "ownership": ownership,
        "read_only": False,
        "cwd": "/project",
        "from_pane_id": "w1:p1",
        "pane_id": None,
        "terminal_id": None,
        "agent_session": None,
        "agent_args": [],
        "opencode_config_content": None,
        "direction": None,
        "start_timeout_ms": 30_000,
        "phase": "reserved",
        "prompt_may_have_been_delivered": False,
        "live_state": "missing",
        "created_at": 1,
        "updated_at": 1,
        "error": None,
    }
    member.update(changes)
    return member


class TeamctlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.socket = self.root / "herdr.sock"
        self.socket.touch()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HERDR_ENV": "1",
                "HERDR_SOCKET_PATH": str(self.socket),
                "HERDR_PANE_ID": "w1:p1",
            },
        )
        self.env.start()
        # These in-process lifecycle tests target member behavior. The real
        # project-wide CEO gate is asserted in the dedicated M2-08 tests.
        self.ceo_gate = mock.patch.object(teamctl.StateStore,
            "require_ceo_initialized", return_value=None)
        self.ceo_gate.start()
        self.session = teamctl.current_session_identity()
        self.store = teamctl.StateStore(self.root)

    def tearDown(self):
        self.ceo_gate.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_state_restores_after_new_store_instance(self):
        teamctl.reserve_member(
            self.store, self.session, make_member("dev", ["src/api/**"])
        )
        restored = teamctl.StateStore(self.root)
        with restored.locked() as state:
            self.assertEqual(state["members"]["dev"]["phase"], "reserved")
            self.assertEqual(state["herdr_session"], self.session)

    def test_member_view_accepts_core_iso_timestamps_and_remains_updatable(self):
        teamctl.reserve_member(
            self.store, self.session, make_member("dev", ["src/api/**"])
        )
        conn = self.store._connect()
        try:
            with teamctl._storage.transaction(conn) as writable:
                writable.execute(
                    "UPDATE members SET created_at=?, updated_at=? WHERE name=?",
                    (
                        "2026-11-07T13:03:25+00:00",
                        "2026-11-07T13:03:26.016668+00:00",
                        "dev",
                    ),
                )
        finally:
            conn.close()

        with self.store.locked() as state:
            member = state["members"]["dev"]
            self.assertEqual(member["created_at"], 1794056605)
            self.assertEqual(member["updated_at"], 1794056606)

        updated = teamctl.update_member(
            self.store, self.session, "dev", phase="pane_created",
            pane_id="w1:p2", terminal_id="term-2",
        )
        self.assertEqual(updated["phase"], "pane_created")
        self.assertEqual(updated["pane_id"], "w1:p2")

    def test_ownership_conflict_then_release(self):
        teamctl.reserve_member(
            self.store, self.session, make_member("one", ["src/api/**"])
        )
        with self.assertRaisesRegex(teamctl.TeamctlError, "overlaps"):
            teamctl.reserve_member(
                self.store, self.session, make_member("two", ["src/api/users.py"])
            )
        teamctl.update_member(
            self.store, self.session, "one", phase="released", ownership=[]
        )
        teamctl.reserve_member(
            self.store, self.session, make_member("two", ["src/api/users.py"])
        )

    def test_concurrent_reservations_allow_only_one_overlap(self):
        def reserve(name):
            try:
                teamctl.reserve_member(
                    self.store, self.session, make_member(name, ["src/**"])
                )
                return "ok"
            except teamctl.TeamctlError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, ("left", "right")))
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])

    def test_ownership_validation_and_read_only(self):
        self.assertEqual(
            teamctl.normalize_ownership(["src/api/**", "tests/api.py"], False),
            ["src/api/**", "tests/api.py"],
        )
        for invalid in (".", "./", "./src/**", "../src/**", "/tmp/x", "src/*.py"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(teamctl.TeamctlError):
                    teamctl.normalize_ownership([invalid], False)
        with self.assertRaises(teamctl.TeamctlError):
            teamctl.normalize_ownership(["src/**"], True)
        self.assertEqual(
            teamctl.normalize_ownership(["设计 资源/页面/**"], False),
            ["设计 资源/页面/**"],
        )

    def test_member_lifecycle_lock_rejects_concurrent_operation(self):
        with self.store.member_operation("dev"):
            with self.assertRaisesRegex(teamctl.TeamctlError, "already running"):
                with self.store.member_operation("dev"):
                    self.fail("nested lifecycle lock unexpectedly succeeded")

    def test_moved_pane_resolves_by_name_and_identity(self):
        session = {"agent": "codex", "kind": "id", "value": "s1"}
        member = make_member(
            "dev", ["src/**"], pane_id="w1:p2", terminal_id="term-1",
            agent_session=session,
        )

        def fake(args, **_kwargs):
            if args[:2] == ["pane", "current"]:
                raise teamctl.HerdrCommandError(["herdr", *args], 1, "not found")
            if args[:2] == ["agent", "get"]:
                return {"result": {"agent": {
                    "name": "dev", "agent": "codex", "pane_id": "w2:p8",
                    "terminal_id": "term-1", "agent_session": session,
                    "agent_status": "idle",
                }}}
            self.fail(args)

        with mock.patch.object(teamctl, "run_herdr", side_effect=fake):
            pane, agent = teamctl.verify_recorded_occupant(member)
        self.assertEqual(pane["pane_id"], "w2:p8")
        self.assertIs(pane, agent)

    def test_resume_reuses_recorded_shell_after_partial_start(self):
        member = make_member(
            "dev", ["src/**"], pane_id="w1:p2", terminal_id="term-2",
            phase="start_ambiguous",
        )
        teamctl.reserve_member(self.store, self.session, member)
        calls = []

        def fake(args, **_kwargs):
            calls.append(args)
            if args[:2] == ["pane", "current"]:
                return {"result": {"pane": {
                    "pane_id": "w1:p2", "terminal_id": "term-2"
                }}}
            if args[:2] == ["pane", "process-info"]:
                return {"result": {"process_info": {
                    "shell_pid": 10, "foreground_processes": []
                }}}
            if args[:2] == ["agent", "start"]:
                return {"result": {}}
            if args[:2] == ["agent", "get"]:
                return {"result": {"agent": {
                    "name": "dev", "agent": "codex", "pane_id": "w1:p2",
                    "terminal_id": "term-2", "agent_session": {"value": "s2"},
                    "agent_status": "idle",
                }}}
            self.fail(args)

        args = argparse.Namespace(
            cwd=str(self.root), name="dev", prompt_action="inspect",
            split_action="inspect", wait=False, timeout_ms=1000,
        )
        output = io.StringIO()
        with mock.patch.object(teamctl, "preflight_kind"), mock.patch.object(
            teamctl, "run_herdr", side_effect=fake
        ), redirect_stdout(output):
            teamctl.resume(args)
        self.assertTrue(any(call[:2] == ["agent", "start"] for call in calls))
        self.assertFalse(any(call[:2] == ["pane", "split"] for call in calls))
        self.assertFalse(any(call[:2] == ["agent", "prompt"] for call in calls))
        self.assertIn("use --prompt-action send", json.loads(output.getvalue())["next_action_required"])

    def test_split_requested_requires_explicit_retry(self):
        teamctl.reserve_member(
            self.store,
            self.session,
            make_member("dev", ["src/**"], phase="split_requested"),
        )
        args = argparse.Namespace(
            cwd=str(self.root), name="dev", prompt_action="inspect",
            split_action="inspect", wait=False, timeout_ms=1000,
        )
        output = io.StringIO()
        with mock.patch.object(teamctl, "preflight_kind"), mock.patch.object(
            teamctl, "run_herdr"
        ) as herdr, redirect_stdout(output):
            teamctl.resume(args)
        herdr.assert_not_called()
        self.assertIn("--split-action retry", json.loads(output.getvalue())["next_action_required"])

    def test_recovery_reconcile_refuses_unrelated_command_before_claim(self):
        member = {"name": "worker", "task_id": "task-1", "attempt_id": "attempt-1"}
        lease = object()
        store = mock.Mock()
        command = mock.Mock(kind="task.pause", payload_json="{}", evidence_json="{}", state="pending")
        args = argparse.Namespace(cwd=str(self.root), name="worker", command_id="unrelated",
                                  resolution="active", actor="human", reason="inspect")
        with mock.patch.object(teamctl, "load_member_for_mutation", return_value=(store, None, None, member)), \
             mock.patch.object(teamctl, "_recovery_lease", return_value=lease), \
             mock.patch.object(teamctl, "_claim_recovery") as claim, \
             mock.patch.object(teamctl._recovery.commands, "get_command", return_value=command), \
             mock.patch.object(teamctl, "resolve_project_root", return_value=self.root):
            with self.assertRaisesRegex(teamctl.TeamctlError, "Command kind"):
                teamctl.recovery_reconcile(args)
        claim.assert_not_called()
        store.release_lease.assert_called_once_with(lease)

    def test_recovery_cli_required_arguments_are_parse_errors(self):
        with self.assertRaises(SystemExit) as exc:
            teamctl.main(["recovery-reconcile", "worker"])
        self.assertEqual(exc.exception.code, 2)

    def test_m206_verifier_commands_parse_expected_arguments(self):
        parser = teamctl.build_parser()
        configure = parser.parse_args([
            "verifier-configure", "--cwd", str(self.root), "--executable", "/usr/bin/python3",
            "--arg=-c", "--arg=print('ok')", "--rules-version", "rules-1",
            "--timeout-seconds", "30", "--actor", "human", "--idempotency-key", "cfg-1",
        ])
        self.assertEqual(configure.handler, teamctl.verification_configure)
        self.assertEqual([configure.executable, *configure.arg], ["/usr/bin/python3", "-c", "print('ok')"])
        self.assertEqual(configure.timeout_seconds, 30)

        run = parser.parse_args(["verify", "--attempt-id", "attempt-1", "--idempotency-key", "run-1"])
        approval = parser.parse_args([
            "verification-approval", "--verification-id", "verification-1", "--decision", "accept",
            "--actor", "human", "--reason", "reviewed", "--idempotency-key", "approval-1",
        ])
        status = parser.parse_args(["verification-status", "--task-id", "task-1"])
        artifact = parser.parse_args(["verification-artifact", "--verification-id", "verification-1"])
        self.assertEqual(run.handler, teamctl.verification_run)
        self.assertEqual(approval.handler, teamctl.verification_approve)
        self.assertEqual(status.handler, teamctl.verification_status)
        self.assertEqual(artifact.handler, teamctl.verification_artifact)

        with self.assertRaises(SystemExit) as exc:
            parser.parse_args(["verification-approval", "--verification-id", "verification-1",
                               "--decision", "invalid", "--actor", "human", "--reason", "x",
                               "--idempotency-key", "approval-2"])
        self.assertEqual(exc.exception.code, 2)

    def test_verification_status_precomputes_current_flags_before_close_and_serializes_both(self):
        class GuardedConn:
            def __init__(self): self.closed = False; self.reads = 0
            def execute(self, *args, **kwargs):
                if self.closed: raise AssertionError("SQLite read attempted after connection close")
                self.reads += 1
            def close(self): self.closed = True

        conn = GuardedConn()
        store = mock.Mock()
        store._connect.return_value = conn
        verifications = [
            SimpleNamespace(id="v-yes", command_id="c-1", attempt_id="a-1", status="pass",
                exit_code=0, artifact_digest="a" * 64, config_version=1, rules_version="r1"),
            SimpleNamespace(id="v-no", command_id="c-2", attempt_id="a-2", status="stale",
                exit_code=1, artifact_digest="b" * 64, config_version=1, rules_version="r1"),
        ]
        approvals = [
            SimpleNamespace(id="ap-yes", verification_id="v-yes", decision="accept",
                current=True, actor="human", reason="ok"),
            SimpleNamespace(id="ap-no", verification_id="v-no", decision="reject",
                current=False, actor="human", reason="stale"),
        ]
        def read_rows(result):
            def read(connection, task_id):
                self.assertFalse(connection.closed, "status reader used a closed connection")
                connection.execute("SELECT 1")
                return result
            return read
        def is_current(connection, item):
            self.assertFalse(connection.closed, "current check ran after the connection closed")
            connection.execute("SELECT 1")
            return item.id == "v-yes"

        args = argparse.Namespace(cwd=str(self.root), task_id="task-1")
        output = io.StringIO()
        with mock.patch.object(teamctl, "StateStore", return_value=store), \
             mock.patch.object(teamctl, "resolve_project_root", return_value=self.root), \
             mock.patch.object(teamctl._verification, "list_verifications", side_effect=read_rows(verifications)), \
             mock.patch.object(teamctl._verification, "verification_is_current", side_effect=is_current), \
             mock.patch.object(teamctl._verification, "list_approvals", side_effect=read_rows(approvals)), \
             mock.patch.object(teamctl._verification, "list_verification_commands", side_effect=read_rows([])), \
             redirect_stdout(output):
            teamctl.verification_status(args)

        self.assertTrue(conn.closed)
        self.assertEqual(conn.reads, 5)
        payload = json.loads(output.getvalue())
        self.assertEqual([item["current"] for item in payload["verifications"]], [True, False])
        self.assertEqual([item["current"] for item in payload["approvals"]], [True, False])

    def test_m208_ordinary_launch_fails_before_reserve_split_start_or_prompt(self):
        lease = object()
        class Store:
            def acquire_lease(self, *args): return lease
            def require_ceo_initialized(self): raise teamctl.TeamctlError("CEO initialization is not trusted")
            def release_lease(self, observed): self.asserted_lease = observed
        store = Store()
        args = argparse.Namespace(
            name="worker", start_timeout_ms=30_000, wait=False, timeout_ms=10_000,
            cwd=str(self.root), from_pane="current", role="developer", kind="codex", read_only=False,
            ownership=["src/**"], agent_arg=[], direction=None,
        )
        with mock.patch.object(teamctl, "require_herdr"), \
             mock.patch.object(teamctl, "validate_common_launch", return_value=(self.root, "task", "responsibility", ["src/**"])), \
             mock.patch.object(teamctl, "preflight_kind"), \
             mock.patch.object(teamctl, "ensure_live_name_available"), \
             mock.patch.object(teamctl, "resolve_pane", return_value={"pane_id": "w1:p1"}), \
             mock.patch.object(teamctl, "current_session_identity", return_value=self.session), \
             mock.patch.object(teamctl, "StateStore", return_value=store), \
             mock.patch.object(teamctl, "reserve_member") as reserve, \
             mock.patch.object(teamctl, "split_for_member") as split, \
             mock.patch.object(teamctl, "start_member") as start, \
             mock.patch.object(teamctl, "prompt_member") as prompt:
            with self.assertRaisesRegex(teamctl.TeamctlError, "not trusted"):
                teamctl._launch_operation(args)
        reserve.assert_not_called()
        split.assert_not_called()
        start.assert_not_called()
        prompt.assert_not_called()
        self.assertIs(store.asserted_lease, lease)

    def test_m208_reserved_launch_fails_before_any_herdr_lifecycle_effect(self):
        class Conn:
            closed = False
            def close(self): self.closed = True
        conn = Conn()
        class Store:
            def ensure_project(self): return "project-1"
            def _connect(self): return conn
            def require_ceo_initialized(self): raise teamctl.TeamctlError("CEO initialization is not trusted")
        store = Store()
        lease = SimpleNamespace(scope=teamctl._leader.project_scope("project-1"), epoch=1)
        member = make_member("worker", ["src/**"])
        with mock.patch.object(teamctl, "require_herdr"), \
             mock.patch.object(teamctl._leader, "require_project_lease"), \
             mock.patch.object(teamctl, "reserve_member") as reserve, \
             mock.patch.object(teamctl, "split_for_member") as split, \
             mock.patch.object(teamctl, "start_member") as start, \
             mock.patch.object(teamctl, "prompt_member") as prompt:
            with self.assertRaisesRegex(teamctl.TeamctlError, "not trusted"):
                teamctl.launch_reserved(store, self.session, member, task_id="task-1",
                    attempt_id="attempt-1", lease=lease)
        self.assertTrue(conn.closed)
        reserve.assert_not_called()
        split.assert_not_called()
        start.assert_not_called()
        prompt.assert_not_called()

    def test_m208_ceo_role_is_the_only_launch_path_that_skips_initialized_gate(self):
        lease = object()
        class StopAtReserve(Exception): pass
        class Store:
            def acquire_lease(self, *args): return lease
            def require_ceo_initialized(self): raise AssertionError("CEO bootstrap must be allowed as the initial path")
            def release_lease(self, observed): self.asserted_lease = observed
        store = Store()
        args = argparse.Namespace(
            name="ceo", start_timeout_ms=30_000, wait=False, timeout_ms=10_000,
            cwd=str(self.root), from_pane="current", role="ceo", kind="codex", read_only=True,
            ownership=[], agent_arg=[], direction=None,
        )
        with mock.patch.object(teamctl, "require_herdr"), \
             mock.patch.object(teamctl, "validate_common_launch", return_value=(self.root, "initialize", "lead", [])), \
             mock.patch.object(teamctl, "preflight_kind"), \
             mock.patch.object(teamctl, "ensure_live_name_available"), \
             mock.patch.object(teamctl, "resolve_pane", return_value={"pane_id": "w1:p1"}), \
             mock.patch.object(teamctl, "current_session_identity", return_value=self.session), \
             mock.patch.object(teamctl, "StateStore", return_value=store), \
             mock.patch.object(teamctl, "codex_ceo_agent_args",
                               return_value=["-c", "socket-allow"]), \
             mock.patch.object(teamctl, "reserve_member", side_effect=StopAtReserve) as reserve:
            with self.assertRaises(StopAtReserve):
                teamctl._launch_operation(args)
        reserve.assert_called_once()
        self.assertEqual(reserve.call_args.args[2]["agent_args"], ["-c", "socket-allow"])
        self.assertIs(store.asserted_lease, lease)

    def test_diQi_worker_resume_requires_project_initialization_before_lifecycle_effects(self):
        member = make_member("worker", ["src/**"], phase="start_ambiguous")
        lease = object()
        store = mock.Mock()
        store.acquire_lease.return_value = lease
        store.require_ceo_initialized.side_effect = teamctl.TeamctlError("project CEO is uninitialized")
        state = {"members": {"worker": member}}
        args = argparse.Namespace(cwd=str(self.root), name="worker", wait=False,
            timeout_ms=1000, split_action="inspect", prompt_action="inspect")
        with mock.patch.object(teamctl, "require_herdr"), \
             mock.patch.object(teamctl, "resolve_project_root", return_value=self.root), \
             mock.patch.object(teamctl, "load_member_for_mutation", return_value=(store, self.session, state, member)), \
             mock.patch.object(teamctl, "preflight_kind"), \
             mock.patch.object(teamctl, "verify_recorded_occupant") as occupant, \
             mock.patch.object(teamctl, "split_for_member") as split, \
             mock.patch.object(teamctl, "start_member") as start, \
             mock.patch.object(teamctl, "prompt_member") as prompt:
            with self.assertRaisesRegex(teamctl.TeamctlError, "uninitialized"):
                teamctl._resume_operation(args)
        store.require_ceo_initialized.assert_called_once_with()
        occupant.assert_not_called()
        split.assert_not_called()
        start.assert_not_called()
        prompt.assert_not_called()
        store.release_lease.assert_called_once_with(lease)

    def test_diQi_ceo_bootstrap_checks_every_challenge_bound_occupant_identity_before_prompt(self):
        class Conn:
            closed = False
            def close(self): self.closed = True

        member = make_member("ceo", [], role="ceo", read_only=True,
            pane_id="pane-1", terminal_id="terminal-1", session_id="tc-session-1",
            agent_session={"kind": "native", "value": "native-1"},
            phase="agent_started", live_state="working")
        challenge = {"nonce": "one-use", "project_id": "p-1", "team_id": "team-1",
            "teamcraft_session_id": "tc-session-1", "herdr_session_id": "native-1",
            "terminal_id": "terminal-1", "protocol_version": "1.0.0",
            "protocol_hash": "a" * 64, "runtime_skill_hash": "b" * 64,
            "snapshot_digest": "c" * 64}
        args = ("ceo", "prompt text")
        pane = {"pane_id": "pane-1", "terminal_id": "terminal-1", "agent": "codex"}
        agent = {"name": "ceo", "agent": "codex", "pane_id": "pane-1",
            "terminal_id": "terminal-1", "agent_session": {"kind": "native", "value": "native-1"}}

        # The verifier must reject every challenge-bound drift, including
        # TeamCraft Session, pane, terminal, member name, Harness kind, and
        # native Agent Session.
        drift_cases = (
            ("teamcraft session", {"session_id": "tc-session-moved"}, {}, {}, False),
            ("pane", {}, {"pane_id": "pane-moved"}, {"pane_id": "pane-moved"}, True),
            ("terminal", {}, {"terminal_id": "terminal-moved"}, {"terminal_id": "terminal-moved"}, True),
            ("name", {}, {}, {"name": "other"}, True),
            ("kind", {}, {}, {"agent": "opencode"}, True),
            ("native session", {}, {}, {"agent_session": {"kind": "native", "value": "native-moved"}}, True),
        )
        for label, member_delta, pane_delta, agent_delta, should_inspect in drift_cases:
            with self.subTest(drift=label):
                conn = Conn()
                live_pane, live_agent = pane | pane_delta, agent | agent_delta
                store = mock.Mock()
                store._connect.return_value = conn
                store.prepare_prompt_brief.return_value = {}
                store.stage_claim.return_value = SimpleNamespace(id="cmd-1", payload_json="{}")
                store.admit_ceo_bootstrap_effect.return_value = None
                herdr = mock.Mock()
                occupant = mock.Mock(return_value=(live_pane, live_agent))
                with mock.patch.object(teamctl, "_adapter") as adapter, \
                     mock.patch.object(teamctl, "update_member", side_effect=lambda *_a, **_kw: member | member_delta | {"phase": "prompt_submitting"}), \
                     mock.patch.object(teamctl, "build_prompt", return_value="bootstrap prompt"), \
                     mock.patch.object(teamctl, "_admit_effect"), \
                     mock.patch.object(teamctl, "verify_recorded_occupant", occupant), \
                     mock.patch.object(teamctl, "_herdr_adapter", return_value=herdr):
                    adapter.team_view.return_value = {"members": {"ceo": member}}
                    adapter.prepare_ceo_bootstrap.return_value = challenge
                    with self.assertRaises(teamctl.TeamctlError):
                        teamctl.prompt_member(member | member_delta, store, self.session,
                            wait=False, timeout_ms=1000, lease=object())
                self.assertTrue(conn.closed)
                self.assertEqual(occupant.called, should_inspect)
                herdr.prompt_agent.assert_not_called()

    def test_m208_first_prompt_binds_native_session_only_after_submission(self):
        class Conn:
            def close(self): pass

        member = make_member(
            "ceo", [], role="ceo", read_only=True, pane_id="pane-1",
            terminal_id="terminal-1", session_id="tc-session-1",
            agent_session=None, phase="agent_started", live_state="idle",
        )
        challenge = {
            "nonce": "one-use", "project_id": "p-1", "team_id": "team-1",
            "teamcraft_session_id": "tc-session-1", "herdr_session_id": None,
            "terminal_id": "terminal-1", "protocol_version": "1.0.0",
            "protocol_hash": "a" * 64, "runtime_skill_hash": "b" * 64,
            "snapshot_digest": "c" * 64,
        }
        pane = {"pane_id": "pane-1", "terminal_id": "terminal-1", "agent": "codex"}
        before = {"name": "ceo", "agent": "codex", "pane_id": "pane-1",
                  "terminal_id": "terminal-1", "agent_status": "idle"}
        after = before | {"agent_session": {"kind": "id", "value": "native-after-prompt"}}
        store = mock.Mock()
        store._connect.return_value = Conn()
        store.prepare_prompt_brief.return_value = {}
        store.stage_claim.return_value = SimpleNamespace(id="cmd-1", payload_json="{}")
        herdr = mock.Mock()
        # The first post-submit get races Session allocation; the target
        # recheck observes the newly allocated identity moments later.
        herdr.get_agent.return_value = before

        def updated(_store, _session, _name, **changes):
            return member | changes

        with mock.patch.object(teamctl, "_adapter") as adapter, \
             mock.patch.object(teamctl, "update_member", side_effect=updated), \
             mock.patch.object(teamctl, "build_prompt", return_value="bootstrap prompt"), \
             mock.patch.object(teamctl, "_admit_effect"), \
             mock.patch.object(teamctl, "verify_recorded_occupant",
                               side_effect=[(pane, before), (pane, before), (pane, after)]), \
             mock.patch.object(teamctl, "BOOTSTRAP_NATIVE_SESSION_POLL_SECONDS", 0), \
             mock.patch.object(teamctl, "_herdr_adapter", return_value=herdr):
            adapter.team_view.return_value = {"members": {"ceo": member}}
            adapter.prepare_ceo_bootstrap.return_value = challenge
            result = teamctl.prompt_member(
                member, store, self.session, wait=False, timeout_ms=1000,
                lease=object(),
            )

        self.assertEqual(result["phase"], "prompt_submitted")
        self.assertEqual(result["agent_session"], after["agent_session"])
        herdr.prompt_agent.assert_called_once_with(
            "ceo", "bootstrap prompt", wait=False, timeout_ms=1000,
        )
        store.record_ceo_bootstrap_submission_assumed.assert_called_once_with(
            "one-use", native_session_id="native-after-prompt",
            agent_session=after["agent_session"],
            session_state="idle", lease=mock.ANY,
        )

    def test_m208_submitted_prompt_without_native_session_confirms_transport_only(self):
        class Conn:
            def close(self): pass

        member = make_member(
            "ceo", [], role="ceo", read_only=True, pane_id="pane-1",
            terminal_id="terminal-1", session_id="tc-session-1",
            agent_session=None, phase="agent_started", live_state="idle",
        )
        challenge = {
            "nonce": "one-use", "project_id": "p-1", "team_id": "team-1",
            "teamcraft_session_id": "tc-session-1", "herdr_session_id": None,
            "terminal_id": "terminal-1", "protocol_version": "1.0.0",
            "protocol_hash": "a" * 64, "runtime_skill_hash": "b" * 64,
            "snapshot_digest": "c" * 64,
        }
        pane = {"pane_id": "pane-1", "terminal_id": "terminal-1", "agent": "codex"}
        agent = {"name": "ceo", "agent": "codex", "pane_id": "pane-1",
                 "terminal_id": "terminal-1", "agent_status": "idle"}
        store = mock.Mock()
        store._connect.return_value = Conn()
        store.prepare_prompt_brief.return_value = {}
        store.stage_claim.return_value = SimpleNamespace(id="cmd-1", payload_json="{}")
        herdr = mock.Mock()
        herdr.get_agent.return_value = agent
        with mock.patch.object(teamctl, "_adapter") as adapter, \
             mock.patch.object(teamctl, "update_member",
                               side_effect=lambda *_a, **changes: member | changes) as update, \
             mock.patch.object(teamctl, "build_prompt", return_value="bootstrap prompt"), \
             mock.patch.object(teamctl, "_admit_effect"), \
             mock.patch.object(teamctl, "verify_recorded_occupant",
                               side_effect=[(pane, agent), (pane, agent)]), \
             mock.patch.object(teamctl, "BOOTSTRAP_NATIVE_SESSION_WAIT_SECONDS", 0), \
             mock.patch.object(teamctl, "_herdr_adapter", return_value=herdr):
            adapter.team_view.return_value = {"members": {"ceo": member}}
            adapter.prepare_ceo_bootstrap.return_value = challenge
            with self.assertRaisesRegex(teamctl.TeamctlError, "do not resend"):
                teamctl.prompt_member(
                    member, store, self.session, wait=False, timeout_ms=1000,
                    lease=object(),
                )
        herdr.prompt_agent.assert_called_once()
        store.record_prompt_delivery.assert_not_called()
        store.stage_unknown.assert_not_called()
        store.record_ceo_bootstrap_submission_assumed.assert_not_called()
        confirmed = [
            call for call in update.mock_calls
            if call.kwargs.get("confirm_command_id") == "cmd-1"
        ]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].kwargs["phase"], "prompt_submitted")
        self.assertEqual(
            confirmed[0].kwargs["prompt_delivery_state"], "submitted",
        )

    def test_m208_native_session_capability_does_not_enable_weak_identity_fallback(self):
        member = make_member(
            "ceo", [], role="ceo", kind="custom-harness", read_only=True,
            pane_id="pane-1", terminal_id="terminal-1",
            session_id="tc-session-1", agent_session=None,
        )
        challenge = {
            "teamcraft_session_id": "tc-session-1", "herdr_session_id": None,
            "terminal_id": "terminal-1",
        }
        pane = {"pane_id": "pane-1", "terminal_id": "terminal-1",
                "agent": "custom-harness"}
        agent = {"name": "ceo", "agent": "custom-harness", "pane_id": "pane-1",
                 "terminal_id": "terminal-1", "agent_status": "idle"}
        with mock.patch.object(teamctl, "verify_recorded_occupant",
                               return_value=(pane, agent)), \
             mock.patch.object(teamctl, "BOOTSTRAP_NATIVE_SESSION_WAIT_SECONDS", 0):
            with self.assertRaisesRegex(
                teamctl.TeamctlError, "capability is unverified",
            ):
                teamctl._wait_for_bootstrap_native_session(member, challenge)
        self.assertEqual(
            teamctl._native_session_capability("codex"),
            "integration_report_required",
        )

    def test_m208_observes_delayed_native_session_on_exact_ceo(self):
        member = make_member(
            "ceo", [], role="ceo", read_only=True, kind="codex",
            pane_id="w1:p2", terminal_id="term-2",
            session_id="tc-session", agent_session=None,
        )
        record = SimpleNamespace(
            terminal_id="term-2",
            herdr_session_identity_json=json.dumps(
                self.session, sort_keys=True, separators=(",", ":"),
            ),
        )
        agent_session = {
            "agent": "codex", "kind": "id", "source": "herdr:codex",
            "value": "native-late",
        }
        pane = {"pane_id": "w1:p2", "terminal_id": "term-2", "agent": "codex"}
        agent = {
            "name": "ceo", "agent": "codex", "pane_id": "w1:p2",
            "terminal_id": "term-2", "agent_status": "idle",
            "agent_session": agent_session,
        }
        with mock.patch.object(
            teamctl, "verify_recorded_occupant", return_value=(pane, agent),
        ):
            observed_agent, identity = teamctl._observe_native_session_identity(
                member, record,
            )
        self.assertIs(observed_agent, agent)
        self.assertEqual(identity["native_session_id"], "native-late")
        self.assertEqual(identity["agent_session"], agent_session)
        self.assertEqual(identity["pane_id"], "w1:p2")
        self.assertEqual(
            identity["herdr_session_identity_hash"],
            teamctl._session_identity_hash(self.session),
        )

    def test_m208_delayed_native_session_observation_rejects_drift(self):
        member = make_member(
            "ceo", [], role="ceo", read_only=True, kind="codex",
            pane_id="w1:p2", terminal_id="term-2",
            session_id="tc-session", agent_session=None,
        )
        record = SimpleNamespace(
            terminal_id="term-2",
            herdr_session_identity_json=json.dumps(
                self.session, sort_keys=True, separators=(",", ":"),
            ),
        )
        pane = {"pane_id": "w1:p2", "terminal_id": "term-2", "agent": "codex"}
        agent = {
            "name": "ceo", "agent": "codex", "pane_id": "w1:p2",
            "terminal_id": "different-terminal", "agent_status": "idle",
            "agent_session": {"kind": "id", "value": "native-late"},
        }
        with mock.patch.object(
            teamctl, "verify_recorded_occupant", return_value=(pane, agent),
        ), self.assertRaisesRegex(teamctl.TeamctlError, "differs"):
            teamctl._observe_native_session_identity(member, record)

        socket_record = SimpleNamespace(
            terminal_id="term-2",
            herdr_session_identity_json=json.dumps(
                dict(self.session, socket_path="/tmp/different.sock"),
                sort_keys=True, separators=(",", ":"),
            ),
        )
        with self.assertRaisesRegex(teamctl.TeamctlError, "socket identity changed"):
            teamctl._observe_native_session_identity(member, socket_record)

    def test_m208_live_process_observation_binds_pid_start_socket_pane_and_harness(self):
        member = make_member(
            "ceo", [], role="ceo", read_only=True, kind="codex",
            cwd=str(self.root), pane_id="w1:p2", terminal_id="term-2",
            session_id="tc-session", agent_session=None,
        )
        pane = {"pane_id": "w1:p2", "terminal_id": "term-2", "agent": "codex"}
        agent = {"name": "ceo", "agent": "codex", "pane_id": "w1:p2",
                 "terminal_id": "term-2", "agent_status": "idle"}
        process = {"result": {"process_info": {
            "pane_id": "w1:p2", "shell_pid": 10,
            "foreground_processes": [{
                "pid": 42, "name": "codex", "argv0": "codex",
                "argv": ["codex", "--model", "test"], "cwd": str(self.root),
            }],
        }}}
        herdr = mock.Mock()
        herdr.process_info.return_value = process
        expected_herdr = json.dumps(
            self.session, sort_keys=True, separators=(",", ":"),
        )
        with mock.patch.object(teamctl, "verify_recorded_occupant",
                               return_value=(pane, agent)), \
             mock.patch.object(teamctl, "_herdr_adapter", return_value=herdr), \
             mock.patch.object(teamctl, "_process_started_at",
                               return_value="2026-11-06T11:00:00+00:00"):
            observed = teamctl._observe_live_process_identity(
                member, expected_herdr_identity_json=expected_herdr,
            )
        self.assertEqual(observed["pid"], 42)
        self.assertEqual(observed["process_start"], "2026-11-06T11:00:00+00:00")
        self.assertEqual(observed["pane_id"], "w1:p2")
        self.assertEqual(observed["terminal_id"], "term-2")
        self.assertEqual(observed["harness"], "codex")
        self.assertEqual(
            observed["herdr_session_identity_hash"],
            teamctl._session_identity_hash(self.session),
        )

    def test_m208_live_process_identity_drift_requires_human(self):
        base = {
            "pid": 42, "process_start": "2026-11-06T11:00:00+00:00",
            "process_name": "codex", "argv": ["codex"], "cwd": str(self.root),
            "pane_id": "w1:p2", "terminal_id": "term-2", "harness": "codex",
            "herdr_session_identity_hash": "a" * 64,
        }
        record = SimpleNamespace(
            live_process_identity_json=json.dumps(base, sort_keys=True),
            herdr_session_identity_json=json.dumps(self.session, sort_keys=True),
        )
        member = make_member("ceo", [], role="ceo")
        for key, replacement in (
            ("pid", 43),
            ("process_start", "2026-11-06T11:00:01+00:00"),
            ("pane_id", "w1:p3"),
            ("terminal_id", "term-3"),
            ("harness", "opencode"),
            ("herdr_session_identity_hash", "b" * 64),
        ):
            with self.subTest(key=key):
                observed = dict(base)
                observed[key] = replacement
                with mock.patch.object(
                    teamctl, "_observe_live_process_identity", return_value=observed,
                ), self.assertRaisesRegex(teamctl.TeamctlError, "NEEDS_HUMAN"):
                    teamctl._require_live_process_identity(member, record)

    def test_m208_live_process_socket_or_process_metadata_drift_is_rejected(self):
        member = make_member(
            "ceo", [], role="ceo", read_only=True, kind="codex",
            cwd=str(self.root), pane_id="w1:p2", terminal_id="term-2",
            session_id="tc-session", agent_session=None,
        )
        pane = {"pane_id": "w1:p2", "terminal_id": "term-2", "agent": "codex"}
        agent = {"name": "ceo", "agent": "codex", "pane_id": "w1:p2",
                 "terminal_id": "term-2", "agent_status": "idle"}
        herdr = mock.Mock()
        herdr.process_info.return_value = {"result": {"process_info": {
            "foreground_processes": [{"pid": 42, "name": "python",
                "argv0": "python", "argv": ["python"], "cwd": str(self.root)}],
        }}}
        with mock.patch.object(teamctl, "verify_recorded_occupant",
                               return_value=(pane, agent)), \
             mock.patch.object(teamctl, "_herdr_adapter", return_value=herdr):
            with self.assertRaisesRegex(teamctl.TeamctlError, "does not match"):
                teamctl._observe_live_process_identity(member)
            # Same Harness/process identity; only the Herdr socket differs.
            herdr.process_info.return_value = {"result": {"process_info": {
                "foreground_processes": [{"pid": 42, "name": "codex",
                    "argv0": "codex", "argv": ["codex"], "cwd": str(self.root)}],
            }}}
            with self.assertRaisesRegex(teamctl.TeamctlError, "socket identity changed"):
                teamctl._observe_live_process_identity(
                    member, expected_herdr_identity_json=json.dumps(
                        dict(self.session, socket_path="/tmp/different-herdr.sock"),
                        sort_keys=True, separators=(",", ":"),
                    ),
                )

    def test_m208_submitted_prompt_with_post_send_identity_drift_stays_ambiguous(self):
        class Conn:
            def close(self): pass

        member = make_member(
            "ceo", [], role="ceo", read_only=True, pane_id="pane-1",
            terminal_id="terminal-1", session_id="tc-session-1",
            agent_session=None, phase="agent_started", live_state="idle",
        )
        challenge = {
            "nonce": "one-use", "project_id": "p-1", "team_id": "team-1",
            "teamcraft_session_id": "tc-session-1", "herdr_session_id": None,
            "terminal_id": "terminal-1", "protocol_version": "1.0.0",
            "protocol_hash": "a" * 64, "runtime_skill_hash": "b" * 64,
            "snapshot_digest": "c" * 64,
        }
        before_pane = {"pane_id": "pane-1", "terminal_id": "terminal-1", "agent": "codex"}
        before_agent = {"name": "ceo", "agent": "codex", "pane_id": "pane-1",
                        "terminal_id": "terminal-1", "agent_status": "idle"}
        drifted_pane = before_pane | {"terminal_id": "replacement-terminal"}
        drifted_agent = before_agent | {
            "terminal_id": "replacement-terminal",
            "agent_session": {"kind": "id", "value": "replacement-native"},
        }
        store = mock.Mock()
        store._connect.return_value = Conn()
        store.prepare_prompt_brief.return_value = {}
        store.stage_claim.return_value = SimpleNamespace(id="cmd-1", payload_json="{}")
        herdr = mock.Mock()
        herdr.get_agent.return_value = drifted_agent
        with mock.patch.object(teamctl, "_adapter") as adapter, \
             mock.patch.object(teamctl, "update_member",
                               side_effect=lambda *_a, **changes: member | changes), \
             mock.patch.object(teamctl, "build_prompt", return_value="bootstrap prompt"), \
             mock.patch.object(teamctl, "_admit_effect"), \
             mock.patch.object(teamctl, "verify_recorded_occupant", side_effect=[
                 (before_pane, before_agent), (drifted_pane, drifted_agent),
             ]), \
             mock.patch.object(teamctl, "_herdr_adapter", return_value=herdr):
            adapter.team_view.return_value = {"members": {"ceo": member}}
            adapter.prepare_ceo_bootstrap.return_value = challenge
            with self.assertRaisesRegex(teamctl.TeamctlError, "do not resend"):
                teamctl.prompt_member(
                    member, store, self.session, wait=False, timeout_ms=1000,
                    lease=object(),
                )
        herdr.prompt_agent.assert_called_once()
        store.stage_unknown.assert_not_called()
        store.record_ceo_bootstrap_submission_assumed.assert_not_called()

    def test_m208_ceo_reconcile_native_session_cli_binds_delayed_identity_and_reports_portable(self):
        member = make_member(
            "ceo", [], role="ceo", read_only=True, kind="codex",
            pane_id="w1:p2", terminal_id="term-2", session_id="tc-session-1",
            agent_session=None, phase="prompt_submitted", live_state="unknown",
            prompt_may_have_been_delivered=True,
            error="CEO native Session initialization pending: timed out",
        )
        state = {
            "version": 1, "team_id": "team-1", "project_root": str(self.root),
            "herdr_session": self.session, "created_at": 1,
            "members": {"ceo": member},
        }
        initialization = SimpleNamespace(
            state="unknown", receipt_source="unverified", native_session_id=None,
            terminal_id="term-2",
            herdr_session_identity_json=json.dumps(
                self.session, sort_keys=True, separators=(",", ":"),
            ),
        )
        agent = {
            "name": "ceo", "agent": "codex", "pane_id": "w1:p2",
            "terminal_id": "term-2", "agent_status": "idle",
            "agent_session": {"kind": "id", "source": "herdr:codex",
                             "value": "native-late"},
        }
        pane = {"pane_id": "w1:p2", "terminal_id": "term-2", "agent": "codex"}
        store = mock.Mock()
        store.reconcile_ceo_native_session.return_value = SimpleNamespace(
            state="initialized", identity_mode="native",
            native_session_id="native-late",
        )
        lease = store.acquire_lease.return_value
        args = argparse.Namespace(cwd=str(self.root), name="ceo", actor="operator",
                                 reason="Herdr reported the delayed native Session")
        with mock.patch.object(teamctl, "load_member_for_mutation",
                              return_value=(store, self.session, state, member)), \
             mock.patch.object(teamctl._adapter, "project_id_for_root",
                              return_value="p-1"), \
             mock.patch.object(teamctl._adapter.ceo_initialization, "get_initialization",
                              return_value=initialization), \
             mock.patch.object(teamctl, "verify_recorded_occupant",
                              return_value=(pane, agent)), \
             mock.patch.object(teamctl, "update_member",
                              side_effect=lambda *_a, **changes: member | changes) as update:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(teamctl.ceo_reconcile_native_session(args), 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["initialization_state"], "initialized")
        self.assertEqual(report["identity_mode"], "native")
        self.assertEqual(report["native_session_id"], "native-late")
        self.assertTrue(report["portable"])
        self.assertEqual(report["phase"], "prompt_submitted")
        self.assertEqual(report["agent_state"], "idle")
        store.acquire_lease.assert_called_once_with("ceo", 60)
        store.reconcile_ceo_native_session.assert_called_once_with(
            "ceo", native_session_id="native-late",
            agent_session=agent["agent_session"], session_state="idle",
            pane_id="w1:p2", terminal_id="term-2", harness="codex",
            herdr_session_identity_hash=teamctl._session_identity_hash(self.session),
            actor="operator", reason=args.reason, lease=lease,
        )
        bound = [call for call in update.mock_calls if "agent_session" in call.kwargs]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0].kwargs["agent_session"], agent["agent_session"])
        self.assertIsNone(bound[0].kwargs["error"])
        self.assertEqual(bound[0].kwargs["phase"], "prompt_submitted")
        store.release_lease.assert_called_once_with(lease)

    def test_m208_ceo_reconcile_native_session_cli_refuses_non_ceo_before_lease(self):
        member = make_member(
            "dev", ["src/a.py"], pane_id="w1:p2", terminal_id="term-2",
            session_id="tc-session-1", phase="prompt_submitted",
        )
        state = {
            "version": 1, "team_id": "team-1", "project_root": str(self.root),
            "herdr_session": self.session, "created_at": 1,
            "members": {"dev": member},
        }
        store = mock.Mock()
        args = argparse.Namespace(cwd=str(self.root), name="dev", actor="operator",
                                 reason="attempted identity bind")
        with mock.patch.object(teamctl, "load_member_for_mutation",
                              return_value=(store, self.session, state, member)):
            with self.assertRaisesRegex(teamctl.TeamctlError, "CEO-only"):
                teamctl.ceo_reconcile_native_session(args)
        store.reconcile_ceo_native_session.assert_not_called()
        store.acquire_lease.assert_not_called()
        store.release_lease.assert_not_called()

    def test_m208_prompt_reconcile_cli_reports_transport_confirmed_without_initialization(self):
        member = make_member(
            "ceo", [], role="ceo", read_only=True, kind="codex",
            pane_id="w1:p2", terminal_id="term-2", session_id="tc-session-1",
            agent_session=None, phase="prompt_ambiguous",
            prompt_may_have_been_delivered=True, live_state="unknown",
            error="prompt outcome ambiguous: response lost",
        )
        state = {
            "version": 1, "team_id": "team-1", "project_root": str(self.root),
            "herdr_session": self.session, "created_at": 1,
            "members": {"ceo": member},
        }
        store = mock.Mock()
        store.reconcile_submitted_prompt.return_value = SimpleNamespace(
            id="cmd-1", state="confirmed",
        )
        lease = store.acquire_lease.return_value
        args = argparse.Namespace(cwd=str(self.root), name="ceo", command_id="cmd-1",
                                 actor="operator",
                                 reason="durable delivery row proves submission")
        with mock.patch.object(teamctl, "load_member_for_mutation",
                              return_value=(store, self.session, state, member)), \
             mock.patch.object(teamctl, "update_member",
                              side_effect=lambda *_a, **changes: member | changes) as update:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(teamctl.prompt_reconcile_submitted(args), 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["prompt_command_id"], "cmd-1")
        self.assertEqual(report["prompt_command_state"], "confirmed")
        # Transport reconciliation must never claim initialization progress.
        self.assertIs(report["initialization_changed"], False)
        self.assertEqual(report["phase"], "prompt_submitted")
        store.reconcile_submitted_prompt.assert_called_once_with(
            "ceo", "cmd-1", lease=lease, actor="operator", reason=args.reason,
        )
        settled = [call for call in update.mock_calls
                   if call.kwargs.get("phase") == "prompt_submitted"]
        self.assertEqual(len(settled), 1)
        self.assertTrue(settled[0].kwargs["prompt_may_have_been_delivered"])
        self.assertIn("remain pending", settled[0].kwargs["error"])
        store.release_lease.assert_called_once_with(lease)

    def test_m208_identity_reconcile_commands_parse_required_arguments(self):
        parser = teamctl.build_parser()
        native = parser.parse_args([
            "ceo-reconcile-native-session", "ceo", "--cwd", str(self.root),
            "--actor", "operator", "--reason", "bind delayed native Session",
        ])
        self.assertIs(native.handler, teamctl.ceo_reconcile_native_session)
        live = parser.parse_args([
            "ceo-reconcile-live-process", "ceo", "--cwd", str(self.root),
            "--actor", "operator", "--reason", "bind this process only",
        ])
        self.assertIs(live.handler, teamctl.ceo_reconcile_live_process)
        transport = parser.parse_args([
            "prompt-reconcile-submitted", "ceo", "--cwd", str(self.root),
            "--command-id", "cmd-1", "--actor", "operator",
            "--reason", "durable submitted evidence",
        ])
        self.assertIs(transport.handler, teamctl.prompt_reconcile_submitted)
        # Every explicit reconciliation demands actor and reason.
        for argv in (
            ["ceo-reconcile-native-session", "ceo", "--cwd", str(self.root),
             "--actor", "operator"],
            ["ceo-reconcile-live-process", "ceo", "--cwd", str(self.root),
             "--reason", "bind"],
            ["prompt-reconcile-submitted", "ceo", "--cwd", str(self.root),
             "--command-id", "cmd-1", "--actor", "operator"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as exc:
                parser.parse_args(argv)
            self.assertEqual(exc.exception.code, 2)

    def test_ambiguous_prompt_is_not_resent_on_default_resume(self):
        agent_session = {"value": "same"}
        teamctl.reserve_member(
            self.store,
            self.session,
            make_member(
                "dev", ["src/**"], pane_id="w1:p2", terminal_id="term-2",
                agent_session=agent_session, phase="prompt_ambiguous",
                prompt_may_have_been_delivered=True,
            ),
        )
        calls = []

        def fake(args, **_kwargs):
            calls.append(args)
            return {"result": {"pane": {
                "name": "dev", "agent": "codex", "pane_id": "w1:p2",
                "terminal_id": "term-2", "agent_session": agent_session,
                "agent_status": "idle",
            }}}

        args = argparse.Namespace(
            cwd=str(self.root), name="dev", prompt_action="inspect",
            split_action="inspect", wait=False, timeout_ms=1000,
        )
        output = io.StringIO()
        with mock.patch.object(teamctl, "preflight_kind"), mock.patch.object(
            teamctl, "run_herdr", side_effect=fake
        ), redirect_stdout(output):
            teamctl.resume(args)
        self.assertFalse(any(call[:2] == ["agent", "prompt"] for call in calls))
        self.assertIn("next_action_required", json.loads(output.getvalue()))

    def test_unrelated_occupant_is_rejected(self):
        member = make_member(
            "dev", ["src/**"], pane_id="w1:p2", terminal_id="term-2"
        )
        payload = {"result": {"pane": {
            "name": "other", "agent": "codex", "pane_id": "w1:p2",
            "terminal_id": "term-2", "agent_status": "idle",
        }}}
        with mock.patch.object(teamctl, "run_herdr", return_value=payload):
            with self.assertRaisesRegex(teamctl.TeamctlError, "unrelated"):
                teamctl.verify_recorded_occupant(member)

    def test_pane_current_without_name_resolves_exact_named_agent(self):
        session = {"agent": "opencode", "kind": "id", "value": "s1"}
        member = make_member(
            "dev", ["src/**"], kind="opencode", pane_id="w1:p2",
            terminal_id="term-2", agent_session=session,
        )

        def fake(args, **_kwargs):
            if args[:2] == ["pane", "current"]:
                return {"result": {"pane": {
                    "agent": "opencode", "pane_id": "w1:p2",
                    "terminal_id": "term-2", "agent_session": session,
                    "agent_status": "working",
                }}}
            if args[:2] == ["agent", "get"]:
                return {"result": {"agent": {
                    "name": "dev", "agent": "opencode", "pane_id": "w1:p2",
                    "terminal_id": "term-2", "agent_session": session,
                    "agent_status": "idle",
                }}}
            self.fail(args)

        with mock.patch.object(teamctl, "run_herdr", side_effect=fake):
            pane, agent = teamctl.verify_recorded_occupant(member)
        self.assertNotIn("name", pane)
        self.assertEqual(agent["name"], "dev")
        self.assertEqual(agent["agent_status"], "idle")

    def test_missing_current_agent_session_is_unverified(self):
        member = make_member(
            "dev", ["src/**"], pane_id="w1:p2", terminal_id="term-2",
            agent_session={"value": "recorded"},
        )
        payload = {"result": {"pane": {
            "name": "dev", "agent": "codex", "pane_id": "w1:p2",
            "terminal_id": "term-2", "agent_status": "idle",
        }}}
        with mock.patch.object(teamctl, "run_herdr", return_value=payload):
            with self.assertRaisesRegex(teamctl.TeamctlError, "cannot be verified"):
                teamctl.verify_recorded_occupant(member)

    def test_status_surfaces_blocked_state(self):
        teamctl.reserve_member(
            self.store,
            self.session,
            make_member("dev", ["src/**"], pane_id="w1:p2", terminal_id="term-2"),
        )
        payload = {"result": {"pane": {
            "name": "dev", "agent": "codex", "pane_id": "w1:p2",
            "terminal_id": "term-2", "agent_status": "blocked",
        }}}
        args = argparse.Namespace(cwd=str(self.root))
        output = io.StringIO()
        with mock.patch.object(teamctl, "run_herdr", return_value=payload), redirect_stdout(output):
            teamctl.status(args)
        member = json.loads(output.getvalue())["members"][0]
        self.assertEqual(member["agent_state"], "blocked")
        self.assertTrue(member["blocked"])

    def test_status_does_not_report_stale_live_state_on_inspection_error(self):
        teamctl.reserve_member(
            self.store,
            self.session,
            make_member(
                "dev", ["src/**"], pane_id="w1:p2", terminal_id="term-2",
                live_state="working",
            ),
        )
        args = argparse.Namespace(cwd=str(self.root))
        output = io.StringIO()
        with mock.patch.object(
            teamctl, "verify_recorded_occupant",
            side_effect=teamctl.TeamctlError("unverified"),
        ), redirect_stdout(output):
            teamctl.status(args)
        member = json.loads(output.getvalue())["members"][0]
        self.assertEqual(member["agent_state"], "unknown")
        self.assertEqual(member["inspection_error"], "unverified")

    def test_invalid_kind_fails_preflight(self):
        with mock.patch.object(teamctl, "supported_kinds", return_value={"codex"}):
            with self.assertRaisesRegex(teamctl.TeamctlError, "unsupported"):
                teamctl.preflight_kind("invalid")

    def test_opencode_split_uses_project_role_override(self):
        member = make_member(
            "dev", ["src/**"], kind="opencode", cwd=str(self.root),
            opencode_config_content='{"provider":{"local":{}},"default_agent":"herdr-developer"}',
        )
        teamctl.reserve_member(self.store, self.session, member)
        calls = []

        def fake(args, **_kwargs):
            calls.append(args)
            if args[:2] == ["pane", "current"]:
                return {"result": {"pane": {"pane_id": "w1:p1"}}}
            if args[:2] == ["pane", "layout"]:
                return {"result": {"layout": {"panes": [
                    {"pane_id": "w1:p1", "rect": {"width": 140, "height": 40}}
                ]}}}
            if args[:2] == ["pane", "split"]:
                return {"result": {"pane": {
                    "pane_id": "w1:p2", "terminal_id": "term-2"
                }}}
            self.fail(args)

        with mock.patch.object(teamctl, "run_herdr", side_effect=fake):
            teamctl.split_for_member(member, self.store, self.session)
        split = next(call for call in calls if call[:2] == ["pane", "split"])
        env_value = split[split.index("--env") + 1]
        self.assertIn('"default_agent":"herdr-developer"', env_value)
        self.assertIn('"provider":{"local":{}}', env_value)

    def test_opencode_config_merges_existing_content_and_requires_profile(self):
        profile_dir = self.root / ".opencode" / "agents"
        profile_dir.mkdir(parents=True)
        (profile_dir / "herdr-developer.md").write_text("profile", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_CONFIG_CONTENT": '{"provider":{"local":{"x":1}}}'},
        ):
            value = json.loads(teamctl.opencode_config_content(self.root, "developer"))
        self.assertEqual(value["provider"]["local"]["x"], 1)
        self.assertEqual(value["default_agent"], "herdr-developer")
        with self.assertRaisesRegex(teamctl.TeamctlError, "profile is missing"):
            teamctl.opencode_config_content(self.root, "tester")

    def test_codex_ceo_agent_args_allow_only_the_live_herdr_socket(self):
        socket_path = self.root / 'herdr "team".sock'
        with mock.patch.dict(
            os.environ, {"HERDR_SOCKET_PATH": str(socket_path)}, clear=False,
        ), mock.patch.object(Path, "is_socket", return_value=True):
            result = teamctl.codex_ceo_agent_args(
                "ceo", "codex", ["--no-alt-screen"],
            )

        expected_profile = (
            'permissions.teamcraft-ceo={extends=":workspace",'
            'network={enabled=true,unix_sockets={'
            f'{json.dumps(str(socket_path.resolve(strict=False)))}="allow"'
            '}}}'
        )
        self.assertEqual(result, [
            "-c", "features.network_proxy=true",
            "-c", expected_profile,
            "-c", 'default_permissions="teamcraft-ceo"',
            "--no-alt-screen",
        ])
        self.assertEqual(
            teamctl.codex_ceo_agent_args("developer", "codex", ["--search"]),
            ["--search"],
        )
        self.assertEqual(
            teamctl.codex_ceo_agent_args("ceo", "opencode", ["--standalone"]),
            ["--standalone"],
        )

    def test_codex_ceo_agent_args_reject_invalid_socket_paths(self):
        with mock.patch.dict(
            os.environ, {"HERDR_SOCKET_PATH": "relative/herdr.sock"}, clear=False,
        ), self.assertRaisesRegex(teamctl.TeamctlError, "must be absolute"):
            teamctl.codex_ceo_agent_args("ceo", "codex", [])

        with mock.patch.dict(
            os.environ, {"HERDR_SOCKET_PATH": str(self.socket)}, clear=False,
        ), self.assertRaisesRegex(teamctl.TeamctlError, "live Herdr Unix socket"):
            teamctl.codex_ceo_agent_args("ceo", "codex", [])

    def test_release_working_member_requires_force(self):
        teamctl.reserve_member(
            self.store,
            self.session,
            make_member("dev", ["src/**"], pane_id="w1:p2", terminal_id="term-2"),
        )
        live = {
            "name": "dev", "agent": "codex", "pane_id": "w1:p2",
            "terminal_id": "term-2", "agent_status": "working",
        }
        args = argparse.Namespace(
            cwd=str(self.root), name="dev", command="release", force=False
        )
        for current_state in ("working", "blocked", "unknown"):
            live["agent_status"] = current_state
            with self.subTest(state=current_state), mock.patch.object(
                teamctl, "verify_recorded_occupant", return_value=(live, live)
            ):
                with self.assertRaisesRegex(teamctl.TeamctlError, f"is {current_state}"):
                    teamctl._finish_member_operation(args)
        args.force = True
        with mock.patch.object(
            teamctl, "verify_recorded_occupant", return_value=(live, live)
        ), redirect_stdout(io.StringIO()):
            teamctl._finish_member_operation(args)
        with self.store.locked() as state:
            self.assertEqual(state["members"]["dev"]["phase"], "released")

    def test_unicode_decode_error_is_actionable(self):
        with self.assertRaisesRegex(teamctl.TeamctlError, "valid UTF-8"):
            teamctl._decode(b"\xff", "stdout")

    def test_worker_prompt_forbids_edits_outside_ownership(self):
        prompt = teamctl.build_prompt(make_member("dev", ["src/api/**"]))
        self.assertIn("Do not edit any file outside", prompt)


if __name__ == "__main__":
    unittest.main()
