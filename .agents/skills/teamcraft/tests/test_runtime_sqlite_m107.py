"""M1-07 CLI-level SQLite runtime contract (durable commands, leases, resume).

Drives the real ``teamctl.main`` against synthetic temp roots with Herdr fully
stubbed.  Where the compat suite freezes the frozen output/dict shapes, this
file pins the NEW runtime guarantees: every Herdr side effect is claimed in
``teamcraft.db`` BEFORE it executes, ambiguous outcomes become UNKNOWN and are
settled only by resume inspection (never a blind resend), a stale leader can
never commit success, and no JSON runtime state is ever created.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from test_compat_contract import (
    AGENT_SESSION,
    CHILD_PANE_ID,
    PARENT_PANE_ID,
    HerdrDouble,
    make_member,
    teamctl,
)


class _FarClock:
    """Fixed clock far enough ahead to expire any operation lease."""

    def now(self) -> datetime:
        return datetime(2027, 1, 1, tzinfo=timezone.utc)


class SqliteRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_resolved = self.root.resolve()
        self.socket = self.root / "herdr.sock"
        self.socket.touch()
        self.env = mock.patch.dict(os.environ, {
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": str(self.socket),
            "HERDR_PANE_ID": PARENT_PANE_ID,
        })
        self.env.start()
        self.session = teamctl.current_session_identity()
        self.store = teamctl.StateStore(self.root_resolved)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    # ---------------------------------------------------------- helpers

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = teamctl.main(argv)
        return code, out.getvalue(), err.getvalue()

    def launch(self, double):
        argv = ["launch", "--role", "developer", "--kind", "codex",
                "--name", "dev", "--task", "Implement the assigned scope.",
                "--ownership", "src/api/**", "--cwd", str(self.root_resolved)]
        with mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            return self.run_main(argv)

    def db(self, sql, params=()):
        conn = sqlite3.connect(self.store.db_path)
        try:
            return [tuple(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def stage_states(self, stage, member="dev"):
        return [str(row[0]) for row in self.db(
            "SELECT state FROM commands WHERE kind = ?"
            " AND json_extract(payload_json, '$.member') = ?"
            " ORDER BY requested_at, id",
            (stage, member),
        )]

    # ------------------------------------------- durability BEFORE the effect

    def test_launch_persists_confirmed_commands_and_never_writes_json(self):
        code, out, err = self.launch(HerdrDouble())
        self.assertEqual(code, 0, err)
        self.assertFalse((self.root_resolved / ".teamcraft"
                         / "state.json").exists())
        self.assertTrue(self.store.db_path.is_file())
        self.assertEqual(self.stage_states("pane.split"), ["confirmed"])
        self.assertEqual(self.stage_states("agent.start"), ["confirmed"])
        self.assertEqual(self.stage_states("agent.prompt"), ["confirmed"])
        events = self.db("SELECT COUNT(*) FROM events")[0][0]
        self.assertGreaterEqual(int(events), 6)
        view = self.store.read_unlocked()
        member = view["members"]["dev"]
        self.assertEqual(member["phase"], "prompt_submitted")
        self.assertIsNotNone(member["task_id"])
        self.assertIsNotNone(member["attempt_id"])
        self.assertIsNotNone(member["session_id"])

    def test_split_claim_is_committed_before_effect_executes(self):
        probe = {}

        class ClaimProbe(HerdrDouble):
            def __call__(self, arguments, **kwargs):
                args = [str(item) for item in arguments]
                if tuple(args[:2]) == ("pane", "split"):
                    conn = sqlite3.connect(self.store_ref.db_path)
                    try:
                        rows = conn.execute(
                            "SELECT state FROM commands WHERE kind ="
                            " 'pane.split'").fetchall()
                    finally:
                        conn.close()
                    probe["states_at_effect"] = [str(r[0]) for r in rows]
                return super().__call__(arguments, **kwargs)

        double = ClaimProbe()
        double.store_ref = self.store
        code, _out, err = self.launch(double)
        self.assertEqual(code, 0, err)
        # The claim was durable (committed) while the effect was still ahead.
        self.assertEqual(probe["states_at_effect"], ["running"])
        self.assertEqual(self.stage_states("pane.split"), ["confirmed"])

    # --------------------------------------- ambiguous outcomes and reconcile

    def test_lost_prompt_is_unknown_and_resume_never_resends(self):
        class PromptLoss(HerdrDouble):
            def __call__(self, arguments, **kwargs):
                args = [str(item) for item in arguments]
                if tuple(args[:2]) == ("agent", "prompt"):
                    raise teamctl.HerdrCommandError(args, 1, "connection reset")
                return super().__call__(arguments, **kwargs)

        code, _out, err = self.launch(PromptLoss())
        self.assertEqual(code, 1)
        self.assertIn("prompt outcome for dev is ambiguous", err)
        self.assertIn("do not resend automatically", err)
        self.assertEqual(self.stage_states("agent.prompt"), ["unknown"])
        view = self.store.read_unlocked()
        self.assertEqual(view["members"]["dev"]["phase"], "prompt_ambiguous")
        self.assertTrue(
            view["members"]["dev"]["prompt_may_have_been_delivered"])

        # Resume inspection must NOT blindly resend the prompt.
        double = HerdrDouble()
        argv = ["resume", "dev", "--cwd", str(self.root_resolved)]
        with mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["phase"], "prompt_ambiguous")
        self.assertIn("prompt-action", payload["next_action_required"])
        called = [call[:2] for call in double.calls]
        self.assertNotIn(["agent", "prompt"], called)
        self.assertEqual(self.stage_states("agent.prompt"), ["unknown"])

    def test_resume_send_creates_fresh_confirmed_prompt_command(self):
        class PromptLoss(HerdrDouble):
            def __call__(self, arguments, **kwargs):
                args = [str(item) for item in arguments]
                if tuple(args[:2]) == ("agent", "prompt"):
                    raise teamctl.HerdrCommandError(args, 1, "connection reset")
                return super().__call__(arguments, **kwargs)

        self.launch(PromptLoss())
        double = HerdrDouble()
        argv = ["resume", "dev", "--cwd", str(self.root_resolved),
                "--prompt-action=send"]
        with mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["phase"], "prompt_submitted")
        self.assertEqual(self.stage_states("agent.prompt"),
                        ["unknown", "confirmed"])
        self.assertIn(["agent", "prompt"],
                      [call[:2] for call in double.calls])

    def test_split_ambiguous_requires_explicit_retry_never_blind_resplit(self):
        class SplitLoss(HerdrDouble):
            def __call__(self, arguments, **kwargs):
                args = [str(item) for item in arguments]
                if tuple(args[:2]) == ("pane", "split"):
                    raise teamctl.HerdrCommandError(args, 1, "timeout")
                return super().__call__(arguments, **kwargs)

        code, _out, err = self.launch(SplitLoss())
        self.assertEqual(code, 1)
        self.assertIn("pane split outcome is ambiguous", err)
        self.assertEqual(self.stage_states("pane.split"), ["unknown"])

        # No pane_id was ever recorded, so live facts cannot confirm WHICH
        # pane the split produced: resume must give explicit guidance and
        # never re-split blindly; only an explicit retry re-dispatches.
        double = HerdrDouble()
        argv = ["resume", "dev", "--cwd", str(self.root_resolved)]
        with mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["phase"], "split_ambiguous")
        self.assertIn("split-action", payload["next_action_required"])
        self.assertFalse(any(call[:2] == ["pane", "split"]
                            for call in double.calls))
        self.assertEqual(self.stage_states("pane.split"), ["unknown"])

        double = HerdrDouble()
        argv = ["resume", "dev", "--cwd", str(self.root_resolved),
                "--split-action=retry"]
        with mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        self.assertEqual(code, 0, err)
        # inspect-mode resume never sends a prompt on its own: it stops at
        # agent_started for the CEO's explicit send/skip decision.
        self.assertEqual(json.loads(out)["phase"], "agent_started")
        # The retry is a NEW command (seq 2); the lost one stays unknown.
        self.assertEqual(self.stage_states("pane.split"),
                        ["unknown", "confirmed"])
        self.assertIn(["pane", "split"], [call[:2] for call in double.calls])

        double = HerdrDouble()
        argv = ["resume", "dev", "--cwd", str(self.root_resolved),
                "--prompt-action=send"]
        with mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["phase"], "prompt_submitted")

    # ------------------------------------------------------ lease / fencing

    def test_foreign_lease_holder_blocks_launch_without_herdr_effect(self):
        # Provision the runtime first (a brand-new root has no database yet).
        self.store.read_unlocked()
        conn = teamctl._adapter.connect_runtime(self.root_resolved)
        try:
            teamctl._adapter.leader.acquire_or_renew(
                conn, "member:dev", "pid:rival", ttl_seconds=3600)
        finally:
            conn.close()
        double = HerdrDouble()
        code, _out, err = self.launch(double)
        self.assertEqual(code, 1)
        self.assertIn("leader lease", err)
        self.assertFalse(any(call[:2] == ["pane", "split"]
                            for call in double.calls))

    def test_stale_leader_success_never_commits_and_records_unknown(self):
        class SplitThenTakeover(HerdrDouble):
            def __call__(self, arguments, **kwargs):
                args = [str(item) for item in arguments]
                result = super().__call__(arguments, **kwargs)
                if tuple(args[:2]) == ("pane", "split"):
                    conn = teamctl._adapter.connect_runtime(
                        self.store_ref.root_resolved)
                    try:
                        teamctl._adapter.leader.acquire_or_renew(
                            conn, "member:dev", "pid:thief", ttl_seconds=60,
                            clock=_FarClock())
                    finally:
                        conn.close()
                return result

        double = SplitThenTakeover()
        double.store_ref = self
        code, _out, err = self.launch(double)
        self.assertEqual(code, 1)
        self.assertIn("UNKNOWN", err)
        self.assertIn("resume", err)
        # Success never committed; the outcome waits for reconciliation.
        self.assertEqual(self.stage_states("pane.split"), ["unknown"])
        view = self.store.read_unlocked()
        # No success-flavoured phase either: the member stays at the last
        # pre-effect phase and resume reconciles the command from live facts.
        self.assertEqual(view["members"]["dev"]["phase"], "split_requested")

    # ------------------------------------------- legacy JSON fails closed (A)

    def test_legacy_herdr_team_refused_bytes_preserved_no_db(self):
        legacy = self.root_resolved / ".herdr-team"
        legacy.mkdir()
        state = legacy / "state.json"
        payload = b'{"legacy": true}\n'
        state.write_bytes(payload)
        double = HerdrDouble()
        argv = ["status", "--cwd", str(self.root_resolved)]
        with mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("legacy team state detected", err)
        self.assertIn("no files were changed", err)
        self.assertEqual(double.calls, [])
        self.assertEqual(state.read_bytes(), payload)
        self.assertFalse((self.root_resolved / ".teamcraft").exists())

    def test_nonempty_teamcraft_json_freezes_writes_bytes_preserved(self):
        teamctl.reserve_member(self.store, self.session,
                              make_member("dev", ["src/**"]))
        before = self.store.read_unlocked()
        json_state = self.root_resolved / ".teamcraft" / "state.json"
        json_state.write_bytes(b'{"version": 9}\n')
        double = HerdrDouble()
        argv = ["launch", "--role", "developer", "--kind", "codex",
                "--name", "late", "--task", "t", "--ownership", "docs/**",
                "--cwd", str(self.root_resolved)]
        with mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertIn("legacy JSON runtime state detected", err)
        self.assertEqual(json_state.read_bytes(), b'{"version": 9}\n')
        self.assertEqual(double.calls, [])
        # Reads stay frozen too: the guard refuses every operation, and the
        # authoritative rows are untouched (verified after manual cleanup).
        with self.assertRaises(teamctl.TeamctlError):
            self.store.read_unlocked()
        json_state.unlink()
        self.assertEqual(self.store.read_unlocked(), before)

    # ---------------------------------------------- session identity (B)

    def test_foreign_session_launch_refused_before_split(self):
        teamctl.reserve_member(self.store, self.session,
                              make_member("dev", ["src/**"]))
        before = self.store.read_unlocked()
        other = self.root / "other.sock"
        other.touch()
        double = HerdrDouble()
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": str(other)}), \
                mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, _out, err = self.run_main(
                ["launch", "--role", "developer", "--kind", "codex",
                 "--name", "ghost", "--task", "t", "--ownership", "src/**",
                 "--cwd", str(self.root_resolved)])
        self.assertEqual(code, 1)
        self.assertIn("different Herdr server session", err)
        self.assertFalse(any(call[:2] == ["pane", "split"]
                            for call in double.calls))
        self.assertEqual(self.store.read_unlocked(), before)

    # -------------------------------------------- terminal lifecycle (B)

    def test_complete_closes_commands_and_resume_refused_after(self):
        teamctl.reserve_member(
            self.store, self.session,
            make_member("dev", ["src/**"], pane_id=CHILD_PANE_ID,
                      terminal_id="term-2", agent_session=AGENT_SESSION,
                      phase="agent_started", live_state="idle"),
        )
        double = HerdrDouble()
        with mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(
                ["complete", "dev", "--cwd", str(self.root_resolved)])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIs(payload["pane_closed"], False)
        view = self.store.read_unlocked()
        self.assertEqual(view["members"]["dev"]["phase"], "completed")
        self.assertEqual(view["members"]["dev"]["ownership"], [])
        non_terminal = self.db(
            "SELECT COUNT(*) FROM commands WHERE state IN"
            " ('pending', 'running', 'unknown')")[0][0]
        self.assertEqual(int(non_terminal), 0)
        code, _out, err = self.run_main(
            ["resume", "dev", "--cwd", str(self.root_resolved)])
        self.assertEqual(code, 1)
        self.assertIn("already completed", err)


if __name__ == "__main__":
    unittest.main()
