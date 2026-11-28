"""M3-04 CLI/API parity tests for teamctl.py.

The point of the M3-04 command API is that every applied effect is a Core
fact — the CLI and the HTTP surface must therefore drive the SAME transitions
against the SAME durable state.  These tests execute the real teamctl script
(no fakes around Core) inside a temporary project root and assert that what it
writes is exactly what :mod:`teamcraft.domain`/:mod:`teamcraft.scheduler`
define, with the same idempotency, versioning, refusal, and lease-free gate
semantics the /api/v1 routes guarantee (those routes are pinned in
``tests/test_api_m304.py``; here the CLI side faces the same core facts).
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from teamcraft import (
    domain,
    fresh_start,
    runtime_adapter,
    scheduler,
    storage,
)

SCRIPT = (Path(__file__).resolve().parents[1]
          / ".agents/skills/teamcraft/scripts/teamctl.py")


def _load_teamctl():
    spec = importlib.util.spec_from_file_location("teamctl_m304_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TEAMCTL = _load_teamctl()


class TeamctlParityTests(unittest.TestCase):
    """task-pause / task-resume / task-policy / scheduler gate, CLI side."""

    def setUp(self) -> None:
        # canonicalized like the CLI resolves it (macOS /var -> /private/var)
        self.root = str(Path(tempfile.mkdtemp(prefix="tc-teamctl-")).resolve())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = Path(self.root) / ".teamcraft" / "teamcraft.db"

    # -- helpers -----------------------------------------------------------

    def core(self):
        return storage.connect(str(self.db_path))

    def run_cli(self, argv: list[str]) -> tuple[int, dict | str, str]:
        """Run teamctl.main; returns (exit_code, stdout_json_or_raw, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = TEAMCTL.main(argv + ["--cwd", self.root])
            except SystemExit as exc:  # argparse refusal (bad choices etc.)
                code = int(exc.code or 0)
        raw = out.getvalue().strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return code, parsed, err.getvalue()

    def make_task(self, title: str) -> tuple[str, int, str]:
        """Seed (task_id, current_version, project_id) the core way.

        A fresh Task starts ``pending``; :func:`domain.recompute_ready` moves
        it to ``ready`` exactly like the dispatcher does, so parity assertions
        run against the same derived facts the runtime would see.
        """
        conn = self.core()
        try:
            project_id = runtime_adapter.ensure_project(conn, self.root)
            task = domain.create_task(conn, project_id, title)
            domain.recompute_ready(conn, project_id)
            current = domain.get_task(conn, task.id)
            self.assertEqual(current.status, "ready")
            return task.id, current.version, project_id
        finally:
            conn.close()

    def command_rows(self) -> list[tuple]:
        conn = self.core()
        try:
            return [
                (str(r[0]), str(r[1]), str(r[2]))
                for r in conn.execute(
                    "SELECT id, kind, state FROM commands ORDER BY requested_at, id"
                ).fetchall()
            ]
        finally:
            conn.close()

    def lease_rows(self) -> int:
        conn = self.core()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM leader_lease").fetchone()[0])
        finally:
            conn.close()

    # -- task pause / resume ----------------------------------------------

    def test_pause_resume_apply_replay_and_rederive_ready(self) -> None:
        task_id, version, _ = self.make_task("gate-me")
        self.assertEqual(version, 1)  # pending -> ready bumped once

        code, out, err = self.run_cli([
            "task-pause", "--task-id", task_id, "--expected-version", "1",
            "--actor", "cli", "--reason", "hold", "--idempotency-key", "cli-p"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["execution"], "applied")
        self.assertEqual(out["kind"], "task.pause")
        self.assertEqual(out["state"], "confirmed")
        self.assertEqual(out["paused"], True)
        self.assertEqual(out["version"], 2)
        self.assertFalse(out["replayed"])

        conn = self.core()
        try:
            task = domain.get_task(conn, task_id)
            self.assertTrue(bool(task.paused))
            self.assertEqual(task.version, 2)
            kinds = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT kind FROM events WHERE task_id = ?",
                (task_id,)).fetchall()]
            self.assertIn("task.paused", kinds)
            result = conn.execute(
                "SELECT result_json FROM commands WHERE id = ?",
                (out["command_id"],)).fetchone()
            payload = json.loads(str(result[0]))
            self.assertEqual(payload["task_id"], task_id)
            self.assertTrue(payload["paused"])
        finally:
            conn.close()

        # replay: same key + identical args -> labelled no-op, one row only.
        rows_before = self.command_rows()
        code, replay, err = self.run_cli([
            "task-pause", "--task-id", task_id, "--expected-version", "1",
            "--actor", "cli", "--reason", "hold", "--idempotency-key", "cli-p"])
        self.assertEqual(code, 0, err)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["command_id"], out["command_id"])
        self.assertEqual(self.command_rows(), rows_before)

        # resume re-derives READY from dependency facts (no deps -> ready).
        code, out2, err = self.run_cli([
            "task-resume", "--task-id", task_id, "--expected-version", "2",
            "--actor", "cli", "--reason", "go", "--idempotency-key", "cli-r"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out2["kind"], "task.resume")
        self.assertEqual(out2["state"], "confirmed")
        self.assertEqual(out2["paused"], False)
        conn = self.core()
        try:
            task = domain.get_task(conn, task_id)
            self.assertEqual((task.status, task.paused, task.version),
                             ("ready", False, 3))
        finally:
            conn.close()

    def test_cli_pause_gates_the_same_core_surfaces_as_the_api(self) -> None:
        task_id, version, project_id = self.make_task("dispatch-gate")
        conn = self.core()
        try:
            # unpaused: the planner sees it (task with no deps is ready).
            self.assertIn(task_id, [t.id for t in domain.ready_queue(conn, project_id)])
        finally:
            conn.close()

        code, _, err = self.run_cli([
            "task-pause", "--task-id", task_id, "--expected-version", str(version),
            "--actor", "cli", "--reason", "hold", "--idempotency-key", "cli-g"])
        self.assertEqual(code, 0, err)

        conn = self.core()
        try:
            # the SAME gates the API route relies on now refuse the task:
            self.assertNotIn(task_id,
                            [t.id for t in domain.ready_queue(conn, project_id)])
            with self.assertRaises(Exception) as ctx:
                domain.create_attempt(conn, task_id, expected_version=version + 1)
            self.assertEqual(getattr(ctx.exception, "reason_code", None),
                             "TASK_PAUSED")
        finally:
            conn.close()

    def test_refusals_exit_nonzero_without_residue(self) -> None:
        task_id, version, _ = self.make_task("refusals")
        code, _, err = self.run_cli([
            "task-pause", "--task-id", task_id, "--expected-version", "1",
            "--actor", "cli", "--reason", "hold", "--idempotency-key", "cli-rf"])
        self.assertEqual(code, 0, err)
        rows_before = self.command_rows()

        # stale version -> Core CAS gate refuses, exit 1, nothing added.
        code, raw, err = self.run_cli([
            "task-pause", "--task-id", task_id, "--expected-version", "0",
            "--actor", "cli", "--reason", "hold", "--idempotency-key", "cli-rf2"])
        self.assertEqual(code, 1)
        # the durable command bound task_version 0; the claim re-checks the
        # evidence under the write lock and refuses (nothing is executed).
        self.assertIn("evidence_stale", err.lower())
        # already paused under a fresh key -> TASK_ALREADY_PAUSED refusal.
        code, raw, err = self.run_cli([
            "task-pause", "--task-id", task_id, "--expected-version", "2",
            "--actor", "cli", "--reason", "hold", "--idempotency-key", "cli-rf3"])
        self.assertEqual(code, 1)
        self.assertIn("TASK_ALREADY_PAUSED", err)
        # same key, different content -> idempotency conflict.
        code, raw, err = self.run_cli([
            "task-resume", "--task-id", task_id, "--expected-version", "1",
            "--actor", "cli", "--reason", "changed", "--idempotency-key", "cli-rf"])
        self.assertEqual(code, 1)
        self.assertEqual(self.command_rows(), rows_before)
        conn = self.core()
        try:
            task = domain.get_task(conn, task_id)
            self.assertEqual((bool(task.paused), task.version), (True, 2))
        finally:
            conn.close()

    # -- execution policy --------------------------------------------------

    def test_policy_applies_mirrors_and_replays(self) -> None:
        task_id, version, _ = self.make_task("policy")
        code, out, err = self.run_cli([
            "task-policy", "--task-id", task_id, "--expected-version", "1",
            "--delivery", "local_only", "--timeout-seconds", "900",
            "--max-attempts", "5", "--idempotency-key", "cli-pol"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["kind"], "task.policy_update")
        self.assertEqual(out["state"], "confirmed")
        self.assertEqual(out["policy"],
                         {"delivery": "local_only",
                          "timeout_seconds": 900, "max_attempts": 5})
        conn = self.core()
        try:
            task = domain.get_task(conn, task_id)
            self.assertEqual(task.max_attempts, 5)  # enforced-column mirror
            self.assertEqual(json.loads(task.execution_policy_json),
                             {"delivery": "local_only",
                              "timeout_seconds": 900, "max_attempts": 5})
            kinds = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT kind FROM events WHERE task_id = ?",
                (task_id,)).fetchall()]
            self.assertIn("task.policy_updated", kinds)
        finally:
            conn.close()

        code, replay, err = self.run_cli([
            "task-policy", "--task-id", task_id, "--expected-version", "1",
            "--delivery", "local_only", "--timeout-seconds", "900",
            "--max-attempts", "5", "--idempotency-key", "cli-pol"])
        self.assertEqual(code, 0, err)
        self.assertTrue(replay["replayed"])

        # the closed delivery set: argparse refuses "worktree" up front and
        # core would refuse it too; either way nothing persists.
        code, _, _ = self.run_cli([
            "task-policy", "--task-id", task_id, "--expected-version", "1",
            "--delivery", "worktree", "--idempotency-key", "cli-pol-bad"])
        self.assertEqual(code, 2)
        conn = self.core()
        try:
            task = domain.get_task(conn, task_id)
            self.assertEqual(json.loads(task.execution_policy_json),
                             {"delivery": "local_only",
                              "timeout_seconds": 900, "max_attempts": 5})
        finally:
            conn.close()

    def test_policy_out_of_range_refused_without_residue(self) -> None:
        task_id, version, _ = self.make_task("policy-range")
        rows_before = self.command_rows()
        code, _, err = self.run_cli([
            "task-policy", "--task-id", task_id, "--expected-version", "1",
            "--timeout-seconds", "0", "--idempotency-key", "cli-pol-oor"])
        self.assertEqual(code, 1)
        self.assertEqual(self.command_rows(), rows_before)
        conn = self.core()
        try:
            self.assertEqual(
                json.loads(domain.get_task(conn, task_id).execution_policy_json),
                {})
        finally:
            conn.close()

    # -- scheduler dispatch gate -------------------------------------------

    def test_scheduler_gate_persists_plan_and_stays_lease_free(self) -> None:
        task_id, version, project_id = self.make_task("sched-gate")

        code, out, err = self.run_cli([
            "scheduler-pause", "--actor", "ops", "--reason", "freeze",
            "--idempotency-key", "cli-sp"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["kind"], "scheduler.pause")
        self.assertEqual(out["state"], "confirmed")
        self.assertEqual(out["paused"], True)
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["project_id"], project_id)

        conn = self.core()
        try:
            plan = scheduler.plan(conn, project_id, concurrency_limit=1)
            self.assertEqual(plan.selected_task_ids, ())
            self.assertIn(
                scheduler.DeferredTask(task_id, "scheduler_paused"),
                plan.deferred,
            )
        finally:
            conn.close()
        # lease-free by construction: the gate never touches leader_lease.
        self.assertEqual(self.lease_rows(), 0)

        code, out2, err = self.run_cli([
            "scheduler-resume", "--actor", "ops", "--reason", "thaw",
            "--idempotency-key", "cli-sr"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out2["paused"], False)
        self.assertEqual(out2["version"], 2)
        conn = self.core()
        try:
            plan = scheduler.plan(conn, project_id, concurrency_limit=1)
            self.assertIn(task_id, plan.selected_task_ids)
        finally:
            conn.close()
        self.assertEqual(self.lease_rows(), 0)

    def test_cross_surface_single_source_of_truth(self) -> None:
        """CLI writes are plain Core facts: core sees them and can invert."""
        task_id, version, project_id = self.make_task("cross")
        code, _, err = self.run_cli([
            "task-pause", "--task-id", task_id, "--expected-version", "1",
            "--actor", "cli", "--reason", "hold", "--idempotency-key", "cli-x"])
        self.assertEqual(code, 0, err)
        conn = self.core()
        try:
            # Core-side read AND core-side resume see the CLI's write.
            self.assertTrue(bool(domain.get_task(conn, task_id).paused))
            resumed = domain.resume_task(conn, task_id, expected_version=2,
                                        actor="core", reason="same plane")
            self.assertEqual((resumed.status, bool(resumed.paused)),
                             ("ready", False))
        finally:
            conn.close()


    # -- M3-04 CLI/API parity: create/edit/priority/dependencies/wake -------

    def test_create_task_applies_replays_and_is_traceable(self) -> None:
        code, out, err = self.run_cli([
            "create-task", "--title", "cli parity", "--description", "d",
            "--kind", "scout", "--priority", "61", "--risk", "high",
            "--max-attempts", "2", "--acceptance", "ac1",
            "--ownership", "src/**", "--idempotency-key", "cli-ct"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["execution"], "applied")
        self.assertEqual(out["kind"], "task.create")
        self.assertEqual(out["state"], "confirmed")
        task = out["task"]
        self.assertEqual(task["title"], "cli parity")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["version"], 0)
        self.assertEqual(task["priority"], 61)
        conn = self.core()
        try:
            live = domain.get_task(conn, task["task_id"])
            self.assertEqual((live.kind, live.risk, live.max_attempts),
                             ("scout", "high", 2))
            self.assertEqual(json.loads(live.acceptance_json), ["ac1"])
            self.assertEqual(json.loads(live.ownership_json), ["src/**"])
        finally:
            conn.close()
        code, replay, err = self.run_cli([
            "create-task", "--title", "cli parity", "--description", "d",
            "--kind", "scout", "--priority", "61", "--risk", "high",
            "--max-attempts", "2", "--acceptance", "ac1",
            "--ownership", "src/**", "--idempotency-key", "cli-ct"])
        self.assertEqual(code, 0, err)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["command_id"], out["command_id"])

    def test_dependencies_atomic_at_create_and_editable_after(self) -> None:
        code, up, err = self.run_cli([
            "create-task", "--title", "upstream", "--idempotency-key", "cli-up"])
        self.assertEqual(code, 0, err)
        up_id = up["task"]["task_id"]
        code, out, err = self.run_cli([
            "create-task", "--title", "downstream", "--depends-on", up_id,
            "--idempotency-key", "cli-dn"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["dependencies"], [up_id])
        task_id = out["task"]["task_id"]
        self.assertEqual(out["task"]["version"], 1)  # edge bump at creation

        code, rem, err = self.run_cli([
            "remove-dependency", "--task-id", task_id, "--depends-on-id", up_id,
            "--expected-version", "1", "--idempotency-key", "cli-rm"])
        self.assertEqual(code, 0, err)
        self.assertTrue(rem["removed"])
        self.assertEqual(rem["task"]["version"], 2)
        replay = self.run_cli([
            "remove-dependency", "--task-id", task_id, "--depends-on-id", up_id,
            "--expected-version", "1", "--idempotency-key", "cli-rm"])[1]
        self.assertTrue(replay["replayed"])

        code, add, err = self.run_cli([
            "add-dependency", "--task-id", task_id, "--depends-on-id", up_id,
            "--expected-version", "2", "--idempotency-key", "cli-add"])
        self.assertEqual(code, 0, err)
        self.assertTrue(add["added"])
        # a self-edge is refused by Core's cycle rule, nothing persisted
        rows_before = self.command_rows()
        code, _, err = self.run_cli([
            "add-dependency", "--task-id", task_id, "--depends-on-id", task_id,
            "--expected-version", "3", "--idempotency-key", "cli-self"])
        self.assertEqual(code, 1)
        self.assertEqual(self.command_rows(), rows_before)

    def test_edit_and_set_priority_are_the_same_conversion(self) -> None:
        task_id, version, _ = self.make_task("priority-parity")
        code, out, err = self.run_cli([
            "edit-task", "--task-id", task_id,
            "--expected-version", str(version), "--priority", "77",
            "--idempotency-key", "cli-pri1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["kind"], "task.update")
        self.assertEqual(out["evidence"],
                         {"previous_version": version, "version": version + 1})
        code, out2, err = self.run_cli([
            "set-priority", "--task-id", task_id,
            "--expected-version", str(version + 1), "--priority", "42",
            "--idempotency-key", "cli-pri2"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out2["kind"], "task.update")
        conn = self.core()
        try:
            self.assertEqual(domain.get_task(conn, task_id).priority, 42)
        finally:
            conn.close()
        # stale version refuses with exit 1 and zero residue
        rows_before = self.command_rows()
        code, _, err = self.run_cli([
            "set-priority", "--task-id", task_id,
            "--expected-version", str(version + 1), "--priority", "9",
            "--idempotency-key", "cli-pri-stale"])
        self.assertEqual(code, 1)
        self.assertEqual(self.command_rows(), rows_before)
        # aliases resolve to the same handler
        parser = TEAMCTL.build_parser()
        args = parser.parse_args(["task-create", "--title", "x",
                                  "--idempotency-key", "k"])
        self.assertIs(args.handler, TEAMCTL.create_task)
        args = parser.parse_args(["dependency-add", "--task-id", "t",
                                  "--depends-on-id", "d",
                                  "--expected-version", "0",
                                  "--idempotency-key", "k"])
        self.assertIs(args.handler, TEAMCTL.add_dependency)

    def test_wake_request_queued_replayed_and_project_traceable(self) -> None:
        task_id, version, project_id = self.make_task("wake-target")
        code, out, err = self.run_cli([
            "wake-request", "--actor", "operator", "--reason", "please look",
            "--task-id", task_id, "--expected-task-version", str(version),
            "--idempotency-key", "cli-wake"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out["execution"], "queued")
        self.assertEqual(out["kind"], "ceo.wake_request")
        self.assertEqual(out["state"], "pending")
        self.assertIsNotNone(out["expires_at"])
        self.assertEqual(out["project_id"], project_id)
        code, replay, err = self.run_cli([
            "wake-request", "--actor", "operator", "--reason", "please look",
            "--task-id", task_id, "--expected-task-version", str(version),
            "--idempotency-key", "cli-wake"])
        self.assertEqual(code, 0, err)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["command_id"], out["command_id"])
        # task binding must be given as a pair
        code, _, err = self.run_cli([
            "wake-request", "--actor", "a", "--reason", "r",
            "--task-id", task_id, "--idempotency-key", "cli-wake-bad"])
        self.assertEqual(code, 1)
        conn = self.core()
        try:
            orphan = int(conn.execute(
                "SELECT COUNT(*) FROM events WHERE project_id IS NULL"
                " AND (kind LIKE 'command.%' OR kind = 'ceo.wake_requested')"
            ).fetchone()[0])
            self.assertEqual(orphan, 0)
        finally:
            conn.close()

    def test_cli_command_events_are_all_project_traceable(self) -> None:
        """EV-LINK-PROJ, CLI side: no command event orphaned from its project."""
        code, out, err = self.run_cli([
            "create-task", "--title", "trace sweep", "--acceptance", "ac",
            "--ownership", "src/api/**", "--idempotency-key", "cli-ts-a"])
        self.assertEqual(code, 0, err)
        task_id = out["task"]["task_id"]
        self.assertEqual(self.run_cli([
            "edit-task", "--task-id", task_id, "--expected-version", "0",
            "--title", "renamed", "--idempotency-key", "cli-ts-b"])[0], 0)
        self.assertEqual(self.run_cli([
            "scheduler-pause", "--actor", "ops", "--reason", "freeze",
            "--idempotency-key", "cli-ts-c"])[0], 0)
        self.assertEqual(self.run_cli([
            "scheduler-resume", "--actor", "ops", "--reason", "thaw",
            "--idempotency-key", "cli-ts-d"])[0], 0)
        self.assertEqual(self.run_cli([
            "wake-request", "--actor", "op", "--reason", "look",
            "--idempotency-key", "cli-ts-e"])[0], 0)
        conn = self.core()
        try:
            total, nulls = conn.execute(
                "SELECT COUNT(*),"
                " SUM(CASE WHEN project_id IS NULL THEN 1 ELSE 0 END)"
                " FROM events WHERE kind LIKE 'command.%'").fetchone()
        finally:
            conn.close()
        # create(3) + edit(3) + scheduler pause/resume(6) + wake(1)
        self.assertGreaterEqual(int(total), 13)  # sweep really produced traffic
        self.assertEqual(int(nulls), 0)


class TeamctlOpenCodeLaunchTests(unittest.TestCase):
    """Regression coverage for OpenCode's interactive launch boundary."""

    def test_opencode_model_is_configured_for_the_selected_role(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with mock.patch.dict("os.environ", {"OPENCODE_CONFIG_CONTENT": "{}"},
                             clear=False):
            raw = TEAMCTL.opencode_config_content(
                root, "developer", model="deepseek/deepseek-flash"
            )
        config = json.loads(raw)
        self.assertEqual(config["default_agent"], "herdr-developer")
        self.assertEqual(config["model"], "deepseek/deepseek-flash")
        self.assertEqual(
            config["agents"]["herdr-developer"]["model"],
            "deepseek/deepseek-flash",
        )

    def test_start_retries_only_an_explicit_busy_response(self) -> None:
        member = {
            "name": "dev", "kind": "opencode", "pane_id": "w1:p2",
            "terminal_id": "term-2", "start_timeout_ms": 30_000,
        }
        busy = TEAMCTL.HerdrCommandError(
            ["herdr"], 1, '{"error":{"code":"agent_pane_busy"}}',
        )
        for error, expected_calls in [(busy, 2), (TEAMCTL.CommandTimeout(["herdr"], 1), 1)]:
            with self.subTest(error=error):
                store = mock.Mock()
                adapter = mock.Mock()
                adapter.start_agent.side_effect = [error, None]
                adapter.get_pane.return_value = {"terminal_id": "term-2"}
                adapter.get_agent.return_value = {
                    "pane_id": "w1:p2", "terminal_id": "term-2", "agent_status": "idle",
                }
                with (
                    mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter),
                    mock.patch.object(TEAMCTL, "update_member"),
                    mock.patch.object(TEAMCTL, "wait_for_shell_pane") as wait_for_shell,
                    mock.patch.object(TEAMCTL.time, "sleep"),
                ):
                    if error is busy:
                        TEAMCTL.start_member(member, store, {}, lease=mock.Mock())
                        wait_for_shell.assert_called_once()
                        store.stage_unknown.assert_not_called()
                    else:
                        with self.assertRaisesRegex(TEAMCTL.TeamctlError, "ambiguous"):
                            TEAMCTL.start_member(member, store, {}, lease=mock.Mock())
                        wait_for_shell.assert_not_called()
                        store.stage_unknown.assert_called_once()
                self.assertEqual(adapter.start_agent.call_count, expected_calls)
                self.assertEqual(store.admit_effect.call_count, expected_calls)
                store.stage_claim.assert_called_once()

    def test_shell_readiness_waits_without_starting_an_agent(self) -> None:
        unavailable = {"result": {"process_info": {
            "shell_pid": 10,
            "foreground_processes": [{"pid": 22, "name": "zsh"}],
        }}}
        ready = {"result": {"process_info": {
            "shell_pid": 10,
            "foreground_processes": [{"pid": 10, "name": "zsh"}],
        }}}
        adapter = mock.Mock()
        adapter.process_info.side_effect = [unavailable, ready]
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter), \
                mock.patch.object(TEAMCTL.time, "sleep") as sleep:
            TEAMCTL.wait_for_shell_pane("w1:p2", 30_000)
        self.assertEqual(adapter.process_info.call_count, 2)
        sleep.assert_called_once()

    def test_shell_readiness_deadline_does_not_start_an_agent(self) -> None:
        unavailable = {"result": {"process_info": {
            "shell_pid": 10,
            "foreground_processes": [{"pid": 22, "name": "zsh"}],
        }}}
        adapter = mock.Mock()
        adapter.process_info.return_value = unavailable
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter), \
                mock.patch.object(TEAMCTL.time, "monotonic", side_effect=[0.0, 11.0]), \
                mock.patch.object(TEAMCTL.time, "sleep") as sleep:
            with self.assertRaisesRegex(TEAMCTL.TeamctlError, "available shell"):
                TEAMCTL.wait_for_shell_pane("w1:p2", 30_000)
        self.assertEqual(adapter.process_info.call_count, 1)
        sleep.assert_not_called()


class TeamctlParserTests(unittest.TestCase):
    """--help must never crash: parser construction is core-version-tolerant.

    Tester defect: direct script invocation resolves Core via
    ``_bootstrap_core`` (installed package or the offline ``scripts/core``
    bundle).  A bundle predating M3-04 has no ``domain.DELIVERY_MODES``, and
    the parser eagerly referenced it — every CLI call died at startup with an
    AttributeError, even ``--help``.  The parser now reads the constant through
    ``_delivery_choices()`` (canonical Core value when present, closed
    fallback otherwise), and runtime capability lives behind
    ``_require_m304_core()`` in the handlers.
    """

    NEW_COMMANDS = (
        "create-task", "task-create", "edit-task", "task-edit",
        "set-priority", "task-priority", "add-dependency", "dependency-add",
        "remove-dependency", "dependency-remove", "task-pause", "task-resume",
        "task-policy", "scheduler-pause", "scheduler-resume", "wake-request",
        "ceo-wake-request",
    )

    EXPECTED_HANDLERS = {
        "create-task": "create_task", "task-create": "create_task",
        "edit-task": "edit_task", "task-edit": "edit_task",
        "set-priority": "set_priority", "task-priority": "set_priority",
        "add-dependency": "add_dependency",
        "dependency-add": "add_dependency",
        "remove-dependency": "remove_dependency",
        "dependency-remove": "remove_dependency",
        "task-pause": "task_pause", "task-resume": "task_resume",
        "task-policy": "task_policy",
        "scheduler-pause": "scheduler_pause",
        "scheduler-resume": "scheduler_resume",
        "wake-request": "wake_request",
        "ceo-wake-request": "wake_request",
    }

    MINIMAL_ARGS = {
        "create-task": ["--title", "t", "--idempotency-key", "k"],
        "edit-task": ["--task-id", "t", "--expected-version", "0",
                     "--title", "x", "--idempotency-key", "k"],
        "set-priority": ["--task-id", "t", "--expected-version", "0",
                        "--priority", "5", "--idempotency-key", "k"],
        "add-dependency": ["--task-id", "t", "--depends-on-id", "d",
                          "--expected-version", "0", "--idempotency-key", "k"],
        "remove-dependency": ["--task-id", "t", "--depends-on-id", "d",
                             "--expected-version", "0", "--idempotency-key", "k"],
        "task-pause": ["--task-id", "t", "--expected-version", "0",
                      "--actor", "a", "--reason", "r", "--idempotency-key", "k"],
        "task-resume": ["--task-id", "t", "--expected-version", "0",
                       "--actor", "a", "--reason", "r", "--idempotency-key", "k"],
        "task-policy": ["--task-id", "t", "--expected-version", "0",
                       "--delivery", "ship", "--idempotency-key", "k"],
        "scheduler-pause": ["--actor", "a", "--reason", "r",
                           "--idempotency-key", "k"],
        "scheduler-resume": ["--actor", "a", "--reason", "r",
                            "--idempotency-key", "k"],
        "wake-request": ["--actor", "a", "--reason", "r",
                        "--idempotency-key", "k"],
    }

    def test_build_parser_constructs_and_lists_full_surface(self) -> None:
        parser = TEAMCTL.build_parser()  # must not raise (the tester defect)
        text = parser.format_help()
        for name in self.NEW_COMMANDS:
            self.assertIn(name, text)

    def test_main_help_exits_zero_in_process(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            TEAMCTL.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("create-task", out.getvalue())
        self.assertIn("wake-request", out.getvalue())

    def test_script_direct_invocation_help_exits_zero(self) -> None:
        """The exact tester command: `python3 <script> --help` from repo root.

        Runs the real subprocess with its own Core resolution — exactly the
        path that used to die with AttributeError against a stale bundle.
        """
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        for name in ("create-task", "edit-task", "set-priority",
                    "add-dependency", "remove-dependency", "wake-request",
                    "task-policy", "scheduler-pause"):
            self.assertIn(name, proc.stdout)

    ALIAS_OF = {
        "task-create": "create-task", "task-edit": "edit-task",
        "task-priority": "set-priority",
        "dependency-add": "add-dependency",
        "dependency-remove": "remove-dependency",
        "ceo-wake-request": "wake-request",
    }

    def test_every_new_subcommand_parses_and_routes_to_handler(self) -> None:
        parser = TEAMCTL.build_parser()
        for name, handler_name in self.EXPECTED_HANDLERS.items():
            canonical = self.ALIAS_OF.get(name, name)
            args = parser.parse_args([name, *self.MINIMAL_ARGS[canonical]])
            self.assertIs(args.handler, getattr(TEAMCTL, handler_name), name)

    def test_delivery_choices_follow_canonical_core_constant(self) -> None:
        # with the canonical repo core, choices are exactly DELIVERY_MODES
        self.assertEqual(TEAMCTL._delivery_choices(),
                         tuple(sorted(domain.DELIVERY_MODES)))
        # with a pre-M3-04 core (constant absent), construction still succeeds
        # on the closed fallback set — parser never hard-crashes.
        with mock.patch.object(TEAMCTL, "_domain", types.SimpleNamespace()):
            self.assertEqual(TEAMCTL._delivery_choices(),
                             TEAMCTL._DELIVERY_MODES_FALLBACK)
            self.assertEqual(set(TEAMCTL._DELIVERY_MODES_FALLBACK),
                             set(domain.DELIVERY_MODES))

    def test_stale_core_guard_is_actionable_not_attribute_error(self) -> None:
        """Handlers fail with a reinstall hint, not AttributeError, on old core."""
        stub = types.SimpleNamespace()  # predates every M3-04 API
        root = str(Path(tempfile.mkdtemp(prefix="tc-stalecore-")).resolve())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fresh_start.initialize_project_root(root)
        argv_by_handler = {
            "task_pause": ["--task-id", "t", "--expected-version", "0",
                          "--actor", "a", "--reason", "r",
                          "--idempotency-key", "k"],
            "scheduler_pause": ["--actor", "a", "--reason", "r",
                               "--idempotency-key", "k"],
            "task_policy": ["--task-id", "t", "--expected-version", "0",
                           "--delivery", "ship", "--idempotency-key", "k"],
            "create_task": ["--title", "t", "--idempotency-key", "k"],
            "edit_task": ["--task-id", "t", "--expected-version", "0",
                         "--title", "x", "--idempotency-key", "k"],
            "add_dependency": ["--task-id", "t", "--depends-on-id", "d",
                              "--expected-version", "0", "--idempotency-key", "k"],
            "wake_request": ["--actor", "a", "--reason", "r",
                            "--idempotency-key", "k"],
        }
        parser = TEAMCTL.build_parser()
        with mock.patch.object(TEAMCTL, "_domain", stub), \
                mock.patch.object(TEAMCTL, "_scheduler", stub), \
                mock.patch.object(TEAMCTL, "_wake", stub):
            for handler_name, argv in argv_by_handler.items():
                args = parser.parse_args(
                    [handler_name.replace("_", "-"), *argv, "--cwd", root])
                with self.assertRaises(TEAMCTL.TeamctlError) as ctx:
                    args.handler(args)
                message = str(ctx.exception)
                self.assertIn("too old", message)
                self.assertIn("install.sh", message)


class PaneLabelTransportTests(unittest.TestCase):
    """The CLI adapter exposes rename_pane and routes it through Herdr."""

    def test_rename_pane_issues_pane_rename_with_one_label_argument(self):
        calls: list[list[str]] = []

        def runner(argv, timeout, accepted):
            calls.append(list(argv))
            return (
                '{"result":{"pane":{"pane_id":"p9","label":"developer · Fix"}}}',
                "",
                0,
            )

        adapter = TEAMCTL._TeamctlHerdrAdapter(runner)
        payload = adapter.rename_pane("p9", "developer · Fix")
        self.assertEqual(
            calls, [["herdr", "pane", "rename", "p9", "developer · Fix"]]
        )
        self.assertEqual(payload["result"]["pane"]["pane_id"], "p9")

    def test_rename_pane_translates_herdr_error_to_teamctl_error(self):
        def runner(argv, timeout, accepted):
            raise TEAMCTL.HerdrError("command_failed", "pane not found")

        adapter = TEAMCTL._TeamctlHerdrAdapter(runner)
        with self.assertRaises(TEAMCTL.TeamctlError):
            adapter.rename_pane("p9", "label")

    def test_set_member_pane_label_writes_derived_label(self):
        adapter = mock.Mock()
        member = {
            "name": "api_dev", "role": "developer",
            "task": "Fix the login bug", "pane_id": "pane-1",
        }
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter):
            label = TEAMCTL.set_member_pane_label(member)
        self.assertEqual(label, "developer · Fix the login bug")
        adapter.rename_pane.assert_called_once_with(
            "pane-1", "developer · Fix the login bug"
        )


class _AbsentLegacyOccupantAdapter:
    """Pane and Agent are both gone; the whole Herdr inventory is empty."""

    def current_pane(self, pane_id):
        raise TEAMCTL.TeamctlError(
            json.dumps({"error": {"code": "pane_not_found", "message": "gone"}})
        )

    def get_agent(self, name):
        raise TEAMCTL.TeamctlError(
            json.dumps({"error": {"code": "agent_not_found", "message": "gone"}})
        )

    def list_agents(self):
        return []

    def request(self, argv):
        if argv == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w3"}]}}
        if argv == ["pane", "list", "--workspace", "w3"]:
            return {"result": {"panes": []}}
        raise AssertionError(f"unexpected Herdr request: {argv}")


class StopEvidenceNoPaneTests(unittest.TestCase):
    """D8: absent recorded identity must not masquerade as a live check."""

    def _member(self):
        return {
            "name": "legacy_dev",
            "pane_id": "w3:p9",
            "terminal_id": "term-9",
            "agent_session": {"kind": "id", "value": "native-9"},
        }

    def test_absent_identity_is_labelled_as_recorded_absence(self):
        with mock.patch.object(
            TEAMCTL, "_herdr_adapter", return_value=_AbsentLegacyOccupantAdapter()
        ):
            evidence = TEAMCTL._stop_evidence(
                self._member(), require_live_agent=False
            )
        # The recovery gate contract is unchanged ...
        self.assertIs(evidence["identity_verified"], True)
        self.assertIs(evidence["process_alive"], False)
        self.assertEqual(evidence["terminal_id"], "term-9")
        self.assertEqual(evidence["native_session_id"], "native-9")
        # ... but the semantics are now explicit and non-misleading.
        self.assertEqual(evidence["identity_mode"], "recorded_identity_absent")
        self.assertIs(evidence["recorded_identity_absent"], True)

    def test_live_identity_collision_still_fails_closed(self):
        adapter = _AbsentLegacyOccupantAdapter()
        adapter.list_agents = lambda: [{"name": "legacy_dev"}]
        with mock.patch.object(
            TEAMCTL, "_herdr_adapter", return_value=adapter
        ), self.assertRaisesRegex(TEAMCTL.TeamctlError, "still appears"):
            TEAMCTL._stop_evidence(self._member(), require_live_agent=False)


class _RoutingFakeStore:
    """Only the StateStore surface _launch_operation touches in these tests."""

    def __init__(self) -> None:
        self.released = []

    def acquire_lease(self, name, ttl_seconds):
        return object()

    def release_lease(self, lease) -> None:
        self.released.append(lease)

    def require_ceo_initialized(self) -> None:
        return None

    @contextlib.contextmanager
    def locked(self):
        yield {"team_id": "team-m502-routing"}


class TeamctlRoutingM502Tests(unittest.TestCase):
    """Opt-in routing must be fail-closed and inert on the default path."""

    def setUp(self) -> None:
        self.root = str(Path(tempfile.mkdtemp(prefix="tc-route-")).resolve())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = Path(self.root) / ".teamcraft" / "teamcraft.db"

    def _args(self, *, kind="opencode", extra=()):
        return TEAMCTL.build_parser().parse_args([
            "launch", "--role", "developer", "--kind", kind,
            "--name", "dev1", "--task", "route wiring",
            "--ownership", "src/**", "--cwd", self.root, *extra,
        ])

    def _route_event_count(self) -> int:
        conn = storage.connect(str(self.db_path))
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = 'route.decided'"
            ).fetchone()[0])
        finally:
            conn.close()

    def _snapshot(self, entity_type, name, *, models=(), health="healthy",
                  reason="ok"):
        now = datetime.now(timezone.utc)
        from teamcraft import providers
        return providers.CapabilitySnapshot(
            entity_type, name, "1.0",
            "openai_compatible" if entity_type == "provider" else None,
            tuple(models), {"skill_rules": "supported"},
            {"inference_request": "supported"}, health, reason, "test",
            (now - timedelta(seconds=1)).isoformat(),
            (now + timedelta(seconds=300)).isoformat(),
        )

    def test_providers_probe_records_all_and_marks_claude_unprobed(self) -> None:
        harness_snapshots = {
            "opencode": self._snapshot("harness", "opencode"),
            "claude": self._snapshot(
                "harness", "claude", health="unsupported",
                reason="adapter_pending",
            ),
        }
        omlx = self._snapshot("provider", "omlx", models=("qwen3:8b",))
        out = io.StringIO()
        with mock.patch.object(
            TEAMCTL._providers, "probe_harness",
            side_effect=lambda name, root: harness_snapshots[name],
        ) as probe_harness, mock.patch.object(
            TEAMCTL._providers, "probe_omlx", return_value=omlx,
        ):
            with contextlib.redirect_stdout(out):
                code = TEAMCTL.main([
                    "providers-probe", "--harness", "opencode",
                    "--harness", "claude", "--cwd", self.root,
                ])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(
            [item["name"] for item in result["snapshots"]],
            ["opencode", "claude", "omlx"],
        )
        claude = result["snapshots"][1]
        self.assertFalse(claude["probed"])
        self.assertEqual(claude["reason"], "adapter_pending")
        self.assertIn("not probed", claude["probe_note"])
        self.assertIn("claude was not probed", result["notes"][0])
        for item in result["snapshots"]:
            for key in ("kind", "name", "health", "reason", "version",
                        "models", "snapshot_digest"):
                self.assertIn(key, item)
        self.assertEqual(probe_harness.call_args_list[0].args[0], "opencode")
        self.assertEqual(probe_harness.call_args_list[1].args[0], "claude")

    def _member(self):
        return {
            "name": "dev1", "role": "developer", "kind": "opencode",
            "phase": "agent_started", "live_state": "idle", "read_only": False,
            "ownership": ["src/**"], "pane_id": "w1:p2",
            "terminal_id": "term-2", "attempt_id": "att-route",
        }

    def _patch_lifecycle(self, member):
        patches = {
            "StateStore": mock.patch.object(
                TEAMCTL, "StateStore", return_value=_RoutingFakeStore()
            ),
            "current_session_identity": mock.patch.object(
                TEAMCTL, "current_session_identity", return_value={}
            ),
            "require_herdr": mock.patch.object(TEAMCTL, "require_herdr"),
            "preflight_kind": mock.patch.object(TEAMCTL, "preflight_kind"),
            "ensure_live_name_available": mock.patch.object(
                TEAMCTL, "ensure_live_name_available"
            ),
            "resolve_pane": mock.patch.object(
                TEAMCTL, "resolve_pane", return_value={"pane_id": "w1:p1"}
            ),
            "reserve_member": mock.patch.object(
                TEAMCTL, "reserve_member", return_value=member
            ),
            "split_for_member": mock.patch.object(
                TEAMCTL, "split_for_member", return_value=member
            ),
            "start_member": mock.patch.object(
                TEAMCTL, "start_member", return_value=member
            ),
            "set_member_pane_label": mock.patch.object(
                TEAMCTL, "set_member_pane_label"
            ),
            "prompt_member": mock.patch.object(
                TEAMCTL, "prompt_member", return_value=member
            ),
            "opencode_config_content": mock.patch.object(
                TEAMCTL, "opencode_config_content", return_value="{}"
            ),
        }
        return patches

    def test_route_requires_opencode_kind_before_any_effect(self) -> None:
        args = self._args(kind="codex", extra=["--route"])
        with mock.patch.object(TEAMCTL, "reserve_member") as reserve, \
                mock.patch.object(TEAMCTL, "split_for_member") as split:
            with self.assertRaisesRegex(
                TEAMCTL.TeamctlError, "--route requires --kind opencode"
            ):
                TEAMCTL._launch_operation(args)
        reserve.assert_not_called()
        split.assert_not_called()

    def test_missing_catalog_fails_before_reserve_or_split(self) -> None:
        args = self._args(extra=["--route"])
        with mock.patch.object(TEAMCTL, "reserve_member") as reserve, \
                mock.patch.object(TEAMCTL, "split_for_member") as split:
            with self.assertRaisesRegex(TEAMCTL.TeamctlError, "route catalog"):
                TEAMCTL._launch_operation(args)
        reserve.assert_not_called()
        split.assert_not_called()

    def test_missing_probes_fails_before_reserve_or_split(self) -> None:
        catalog = Path(self.root) / ".teamcraft" / "routes.json"
        catalog.write_text(json.dumps([{
            "harness": "opencode", "provider": "omlx", "model": "qwen3:8b",
            "locality": "local", "assurance": 2, "cost_rank": 0,
        }]), encoding="utf-8")
        conn = storage.connect(str(self.db_path))
        try:
            runtime_adapter.ensure_project(conn, self.root)
        finally:
            conn.close()
        args = self._args(extra=["--route"])
        with mock.patch.object(TEAMCTL, "reserve_member") as reserve, \
                mock.patch.object(TEAMCTL, "split_for_member") as split:
            with self.assertRaisesRegex(TEAMCTL.TeamctlError, "no route selected"):
                TEAMCTL._launch_operation(args)
        reserve.assert_not_called()
        split.assert_not_called()

    def test_override_without_reason_is_refused_before_effects(self) -> None:
        args = self._args(extra=[
            "--route", "--opencode-model", "omlx/qwen3:8b",
        ])
        with mock.patch.object(TEAMCTL, "reserve_member") as reserve:
            with self.assertRaisesRegex(
                TEAMCTL.TeamctlError, "requires --route-override-reason"
            ):
                TEAMCTL._launch_operation(args)
        reserve.assert_not_called()

    def test_default_launch_never_decides_or_commits(self) -> None:
        member = self._member()
        patches = self._patch_lifecycle(member)
        route_core = mock.Mock()
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            mocks = {name: stack.enter_context(patch)
                     for name, patch in patches.items()}
            stack.enter_context(mock.patch.object(
                TEAMCTL, "_dispatch_routing", route_core
            ))
            with contextlib.redirect_stdout(out):
                code = TEAMCTL._launch_operation(self._args())
        self.assertEqual(code, 0)
        route_core.decide.assert_not_called()
        route_core.commit.assert_not_called()
        mocks["prompt_member"].assert_called_once()
        self.assertEqual(self._route_event_count(), 0)
        # The CLI's machine-readable result must be captured here instead of
        # leaking into unittest's stdout; exactly one JSON record, no extras.
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(json.loads(lines[0])["name"], "dev1")

    def test_commit_failure_suppresses_prompt(self) -> None:
        member = self._member()
        outcome = types.SimpleNamespace(
            opencode_model="omlx/qwen3:8b",
            decision=types.SimpleNamespace(reason_code="selected_local_first"),
            decision_digest="d" * 64,
        )
        patches = self._patch_lifecycle(member)
        with contextlib.ExitStack() as stack:
            mocks = {name: stack.enter_context(patch)
                     for name, patch in patches.items()}
            stack.enter_context(mock.patch.object(
                TEAMCTL, "_decide_launch_route", return_value=outcome
            ))
            stack.enter_context(mock.patch.object(
                TEAMCTL, "_commit_launch_route",
                side_effect=TEAMCTL.TeamctlError("route commit refused"),
            ))
            with self.assertRaisesRegex(
                TEAMCTL.TeamctlError, "route commit refused"
            ):
                TEAMCTL._launch_operation(self._args(extra=["--route"]))
        mocks["reserve_member"].assert_called_once()
        mocks["split_for_member"].assert_called_once()
        mocks["start_member"].assert_called_once()
        mocks["prompt_member"].assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
