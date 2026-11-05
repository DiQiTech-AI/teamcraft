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
        self.session = teamctl.current_session_identity()
        self.store = teamctl.StateStore(self.root)

    def tearDown(self):
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
