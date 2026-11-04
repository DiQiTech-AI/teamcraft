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

from teamcraft import scheduler

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
        # CI runners have no herdr executable.  Stub ONLY the
        # executable-presence gate (preflight_kind -> supported_kinds' real
        # `herdr agent` subprocess + shutil.which PATH probe), which carries
        # no runtime state effect.  Every actual Herdr effect still flows
        # through the per-test run_herdr double and is asserted there.
        self.gate = mock.patch.object(teamctl, "preflight_kind")
        self.gate.start()
        self.session = teamctl.current_session_identity()
        self.store = teamctl.StateStore(self.root_resolved)

    def tearDown(self) -> None:
        self.gate.stop()
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
        project_id = self.db("SELECT id FROM projects WHERE repo_path=?",
                             (str(self.root_resolved),))[0][0]
        self.assertIsNotNone(project_id)
        rows = self.db("SELECT scope, state FROM leader_lease")
        self.assertEqual(rows, [(teamctl._leader.project_scope(project_id), "released")])

    def test_effect_confirmation_rolls_back_with_member_state_write(self):
        double = HerdrDouble()
        sync_state = teamctl._adapter.sync_state

        def fail_on_pane_created(conn, state, **kwargs):
            if state["members"]["dev"]["phase"] == "pane_created":
                raise teamctl._adapter.RuntimeAdapterError(
                    "injected member-state write failure"
                )
            return sync_state(conn, state, **kwargs)

        # The external split succeeds, then the following DB member-state
        # write fails. Command confirmation and member state must share the
        # transaction, leaving the command unresolved for resume inspection.
        with mock.patch.object(teamctl._adapter, "sync_state",
                               side_effect=fail_on_pane_created):
            code, _out, err = self.launch(double)
        self.assertEqual(code, 1)
        self.assertIn("injected member-state write failure", err)
        self.assertIn(["pane", "split"], [call[:2] for call in double.calls])
        self.assertEqual(self.stage_states("pane.split"), ["running"])
        self.assertEqual(self.store.read_unlocked()["members"]["dev"]["phase"],
                         "split_requested")

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
        project_id = self.store.ensure_project()
        conn = teamctl._adapter.connect_runtime(self.root_resolved)
        try:
            teamctl._adapter.leader.acquire_or_renew(
                conn, teamctl._leader.project_scope(project_id), "pid:rival", ttl_seconds=3600)
        finally:
            conn.close()
        double = HerdrDouble()
        code, _out, err = self.launch(double)
        self.assertEqual(code, 1)
        self.assertIn("leader lease", err)
        self.assertFalse(any(call[:2] == ["pane", "split"]
                            for call in double.calls))

    def test_scheduler_and_one_shot_share_project_lease_both_orders(self):
        # Acquire on a fresh root: this must create/reuse the Project before
        # either executor derives its shared lease scope.
        project_id = self.store.ensure_project()
        self.assertEqual(self.db("SELECT COUNT(*) FROM projects WHERE repo_path=?",
                                 (str(self.root_resolved),))[0][0], 1)
        conn = self.store._connect()
        try:
            sched_first = scheduler.Scheduler(
                conn, project_id, "scheduler:first", concurrency_limit=2,
                ttl_seconds=60,
            )
            scheduler_lease = sched_first.acquire_or_renew()
            with self.assertRaises(teamctl.TeamctlError):
                self.store.acquire_lease("dev", 60)
            sched_first.release()

            cli_lease = self.store.acquire_lease("dev", 60)
            sched_second = scheduler.Scheduler(
                conn, project_id, "scheduler:second", concurrency_limit=2,
                ttl_seconds=60,
            )
            with self.assertRaises(teamctl._leader.LeaderBusyError):
                sched_second.acquire_or_renew()
            self.store.release_lease(cli_lease)

            other = teamctl._adapter.domain.create_project(
                conn, "independent", str(self.root_resolved / "independent-project"),
            )
            other_lease = scheduler.Scheduler(
                conn, other.id, "scheduler:other-project", concurrency_limit=1,
                ttl_seconds=60,
            ).acquire_or_renew()
            self.assertNotEqual(other_lease.scope, scheduler_lease.scope)
            teamctl._leader.release(conn, other_lease.scope, other_lease.holder_id,
                                    epoch=other_lease.epoch)
        finally:
            conn.close()

    def test_scheduler_lease_flows_through_member_helper_without_release(self):
        member = make_member(
            "dev", ["src/api/**"], pane_id=CHILD_PANE_ID,
            phase="pane_created",
        )
        teamctl.reserve_member(self.store, self.session, member)
        project_id = self.store.ensure_project()
        conn = self.store._connect()
        try:
            service = scheduler.Scheduler(
                conn, project_id, "scheduler:lifecycle", concurrency_limit=1,
                ttl_seconds=120,
            )
            lease = service.acquire_or_renew()
            with mock.patch.object(teamctl, "run_herdr", side_effect=HerdrDouble()):
                result = teamctl.start_member(
                    member, self.store, self.session, lease=lease,
                )
            self.assertEqual(result["phase"], "agent_started")
            current = teamctl._leader.current_lease(conn, lease.scope)
            self.assertIsNotNone(current)
            self.assertEqual(current.epoch, lease.epoch)
            scopes = self.db("SELECT scope FROM leader_lease")
            self.assertEqual(scopes, [(lease.scope,)])
            service.release()
        finally:
            conn.close()

    def test_scheduler_reserved_attempt_runs_through_confirmed_member_lifecycle(self):
        project_id = self.store.ensure_project()
        conn = self.store._connect()
        try:
            task = teamctl._adapter.domain.create_task(
                conn, project_id, "scheduler-owned task",
                ownership_json='["src/scheduled/**"]',
            )
            task = teamctl._adapter.domain.apply_task_event(
                conn, task.id, "dependencies_satisfied", expected_version=task.version,
            )
            service = scheduler.Scheduler(
                conn, project_id, "scheduler:dispatch", concurrency_limit=1,
                ttl_seconds=180,
            )
            reserved = service.tick()
            self.assertEqual(len(reserved.attempts), 1)
            attempt = reserved.attempts[0]
            member = make_member("scheduled", ["src/scheduled/**"])
            with mock.patch.object(teamctl, "run_herdr", side_effect=HerdrDouble()):
                launched = teamctl.launch_reserved(
                    self.store, self.session, member, task_id=task.id,
                    attempt_id=attempt.id, lease=service.lease,
                    wait=False, timeout_ms=1000,
                )
            self.assertEqual((launched["task_id"], launched["attempt_id"]),
                             (task.id, attempt.id))
            self.assertEqual(
                self.db("SELECT COUNT(*) FROM tasks WHERE project_id=?", (project_id,))[0][0],
                1,
            )
            self.assertEqual(
                self.db("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,))[0][0],
                1,
            )
            self.assertEqual(self.stage_states("pane.split", "scheduled"), ["confirmed"])
            self.assertEqual(self.stage_states("agent.start", "scheduled"), ["confirmed"])
            self.assertEqual(self.stage_states("agent.prompt", "scheduled"), ["confirmed"])
            self.assertEqual(
                teamctl._adapter.domain.get_attempt(conn, attempt.id).status, "active",
            )
            self.assertIsNotNone(teamctl._leader.current_lease(conn, service.lease.scope))
            service.release()
        finally:
            conn.close()

    def test_lease_takeover_after_stage_claim_blocks_herdr_effect(self):
        self.store.ensure_project()
        original_claim = teamctl.StateStore.stage_claim

        def claim_then_takeover(store, name, stage, *, lease, payload):
            command = original_claim(store, name, stage, lease=lease, payload=payload)
            if stage == "pane.split":
                conn = store._connect()
                try:
                    teamctl._leader.acquire_or_renew(
                        conn, lease.scope, "pid:takeover", ttl_seconds=3600,
                        clock=_FarClock(),
                    )
                finally:
                    conn.close()
            return command

        double = HerdrDouble()
        with mock.patch.object(teamctl.StateStore, "stage_claim", autospec=True,
                               side_effect=claim_then_takeover), \
                mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, _out, err = self.launch(double)
        self.assertEqual(code, 1)
        self.assertIn("Herdr call was not invoked", err)
        self.assertFalse(any(call[:2] == ["pane", "split"] for call in double.calls))
        self.assertEqual(self.stage_states("pane.split"), ["unknown"])

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
                            conn, teamctl._leader.project_scope(teamctl._adapter.project_id_for_root(conn, str(self.store_ref.root_resolved))), "pid:thief", ttl_seconds=60,
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

    def test_stale_lease_never_commits_member_state(self):
        # CEO gate D follow-up: member-state changes are authoritative writes.
        teamctl.reserve_member(self.store, self.session,
                             make_member("dev", ["src/**"]))
        lease = self.store.acquire_lease("dev", 30)
        conn = teamctl._adapter.connect_runtime(self.root_resolved)
        try:
            teamctl._adapter.leader.acquire_or_renew(
                conn, teamctl._leader.project_scope(teamctl._adapter.project_id_for_root(conn, str(self.root_resolved))), "pid:thief", ttl_seconds=3600,
                clock=_FarClock())
        finally:
            conn.close()
        # An explicitly stale lease can never commit a member-state change.
        with self.assertRaisesRegex(teamctl.TeamctlError,
                                   "leader lease was lost"):
            teamctl.update_member(self.store, self.session, "dev",
                                  lease=lease, phase="pane_created",
                                  pane_id="w-9")
        view = self.store.read_unlocked()
        self.assertEqual(view["members"]["dev"]["phase"], "reserved")
        self.assertIsNone(view["members"]["dev"]["pane_id"])
        # The lease-free auto-admission path is refused too while a foreign
        # holder is live: no back door around the fence.
        with self.assertRaisesRegex(teamctl.TeamctlError, "leader lease"):
            teamctl.update_member(self.store, self.session, "dev",
                                 phase="pane_created")
        view = self.store.read_unlocked()
        self.assertEqual(view["members"]["dev"]["phase"], "reserved")

    def test_write_unlocked_without_member_identity_cannot_mutate(self):
        # CEO follow-up: the identity-free write bypass must not exist.
        teamctl.reserve_member(self.store, self.session,
                             make_member("dev", ["src/**"]))
        state = self.store.read_unlocked()
        state["members"]["dev"]["phase"] = "released"  # tampered working copy
        # Identity has no default anymore: omitting it is a hard TypeError.
        with self.assertRaises(TypeError):
            self.store.write_unlocked(state)
        for bad in ("", "   ", None, "ghost"):
            with self.subTest(member=repr(bad)), \
                    self.assertRaises(teamctl.TeamctlError):
                self.store.write_unlocked(state, member=bad)
        # None of the refused attempts touched the authoritative store.
        view = self.store.read_unlocked()
        self.assertEqual(view["members"]["dev"]["phase"], "reserved")
        # Honest identity still works (initialization/reservation preserved).
        teamctl.update_member(self.store, self.session, "dev",
                             phase="pane_created", pane_id="w-3")
        self.assertEqual(self.store.read_unlocked()["members"]["dev"]["phase"],
                        "pane_created")

    def test_write_unlocked_binds_member_record_and_lease_scope(self):
        teamctl.reserve_member(self.store, self.session,
                              make_member("dev", ["src/**"]))
        state = self.store.read_unlocked()
        with self.subTest("record identity mismatch"):
            state["members"]["dev"]["name"] = "other"
            with self.assertRaisesRegex(teamctl.TeamctlError,
                                        "does not match"):
                self.store.write_unlocked(state, member="dev")
            state["members"]["dev"]["name"] = "dev"
        with self.subTest("lease scope mismatch"):
            conn = self.store._connect()
            try:
                foreign = teamctl._leader.acquire_or_renew(
                    conn, teamctl._leader.project_scope("other-project"),
                    "foreign", ttl_seconds=30,
                )
            finally:
                conn.close()
            try:
                with self.assertRaisesRegex(teamctl.TeamctlError,
                                            "requires project lease scope"):
                    self.store.write_unlocked(state, member="dev", lease=foreign)
            finally:
                self.store.release_lease(foreign)
        self.assertEqual(self.store.read_unlocked()["members"]["dev"]["phase"],
                         "reserved")

    def test_empty_members_rejects_invented_identity_before_provisioning(self):
        # CEO follow-up on b6f9660: the membership check is unconditional, so
        # an invented identity can never ride an empty members dict — and the
        # refusal happens before any mutation or DB provisioning.
        blank = teamctl.new_state(self.root_resolved, self.session)
        self.assertEqual(blank["members"], {})
        with self.assertRaisesRegex(teamctl.TeamctlError,
                                   "not part of the written state"):
            self.store.write_unlocked(blank, member="ghost")
        # Nothing was provisioned by the refused attempt.
        self.assertFalse((self.root_resolved / ".teamcraft"
                         / "teamcraft.db").exists())
        # Honest initialization is preserved: reserve puts the member in the
        # dict it writes, so the unconditional check admits it.
        teamctl.reserve_member(self.store, self.session,
                             make_member("dev", ["src/**"]))
        self.assertEqual(self.store.read_unlocked()["members"]["dev"]["phase"],
                         "reserved")

    def test_foreign_live_lease_blocks_resume_reconciliation(self):
        class SplitLoss(HerdrDouble):
            def __call__(self, arguments, **kwargs):
                args = [str(item) for item in arguments]
                if tuple(args[:2]) == ("pane", "split"):
                    raise teamctl.HerdrCommandError(args, 1, "timeout")
                return super().__call__(arguments, **kwargs)

        self.launch(SplitLoss())  # durable split command ends UNKNOWN
        conn = teamctl._adapter.connect_runtime(self.root_resolved)
        try:
            teamctl._adapter.leader.acquire_or_renew(
                conn, teamctl._leader.project_scope(teamctl._adapter.project_id_for_root(conn, str(self.root_resolved))), "pid:rival", ttl_seconds=3600)
        finally:
            conn.close()
        double = HerdrDouble()
        with mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(
                ["resume", "dev", "--cwd", str(self.root_resolved)])
        self.assertEqual(code, 1)
        self.assertIn("leader lease", err)
        # Admission failed before any Herdr probe, so nothing was reconciled.
        self.assertEqual(double.calls, [])
        self.assertEqual(self.stage_states("pane.split"), ["unknown"])
        view = self.store.read_unlocked()
        self.assertEqual(view["members"]["dev"]["phase"], "split_ambiguous")

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
