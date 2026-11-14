"""M4-04 teamctl CLI parity tests.

These drive the real ``teamctl.py`` script and prove the local-delivery surface
reaches the SAME Core entry points as the runtime adapter, with clear command
names, fenced project leases, idempotency keys, JSON output, and fail-closed
refusals.  Deep Core semantics (drift/verifier/protected-branch) are covered by
``tests/test_delivery_m404.py``; here the adapter is observed at the CLI
boundary so parity is about wiring, not duplicated safety logic.

Gap noted deliberately: a full happy-path commit->ship->accept needs a live
dispatched Attempt (worker report + passing worktree verifier + local_only
policy), which is the M4-05/06 dispatch lifecycle; these tests therefore bind
and assert the adapter/Core call, while the stale-Core guard and refusal
mapping are exercised end-to-end against a real initialized runtime root.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from teamcraft import delivery, domain, fresh_start, leader, runtime_adapter

SCRIPT = (Path(__file__).resolve().parents[1]
          / ".agents/skills/teamcraft/scripts/teamctl.py")


def _load_teamctl():
    spec = importlib.util.spec_from_file_location("teamctl_m404_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TEAMCTL = _load_teamctl()


class TeamctlDeliveryParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = str(Path(tempfile.mkdtemp(prefix="tc-teamctl-m404-")).resolve())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = Path(self.root) / ".teamcraft" / "teamcraft.db"

    def core(self):
        from teamcraft import storage
        return storage.connect(str(self.db_path))

    def run_cli(self, argv: list[str]) -> tuple[int, dict | str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = TEAMCTL.main(argv + ["--cwd", self.root])
            except SystemExit as exc:
                code = int(exc.code or 0)
        raw = out.getvalue().strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return code, parsed, err.getvalue()

    def lease_rows(self) -> int:
        """Live (held) fences.  Released rows are RETAINED for epoch history,
        so a well-behaved command leaves zero HELD leases, not zero rows."""
        conn = self.core()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM leader_lease WHERE state = 'held'"
            ).fetchone()[0])
        finally:
            conn.close()

    def _seed_member(self) -> tuple[str, str, str]:
        """Create project + a dispatched member row with task/attempt identity."""
        conn = self.core()
        try:
            project_id = runtime_adapter.ensure_project(conn, self.root)
            task = domain.create_task(conn, project_id, "ship-me")
            domain.recompute_ready(conn, project_id)
            return task.id, f"attempt-{task.id}", project_id
        finally:
            conn.close()

    def _seed_synced_member(self, name: str = "dev") -> None:
        """Record a real member row (with Task/Attempt lineage) via sync_state,
        so read-only delivery inspection runs the genuine adapter+Core path."""
        conn = self.core()
        try:
            project_id = runtime_adapter.ensure_project(conn, self.root)
            task = domain.create_task(conn, project_id, f"deliver {name}")
            domain.recompute_ready(conn, project_id)
        finally:
            conn.close()
        member_row = {
            "name": name, "role": "developer", "responsibility": "ship",
            "kind": "opencode", "task": f"deliver {name}",
            "ownership": ["src/**"], "read_only": False, "cwd": self.root,
            "from_pane_id": "w1:p1", "pane_id": "w1:p2", "terminal_id": None,
            "agent_session": None, "agent_args": [],
            "opencode_config_content": None, "direction": None,
            "start_timeout_ms": 30_000, "phase": "pane_created",
            "prompt_may_have_been_delivered": False, "live_state": None,
            "error": None, "task_id": None, "attempt_id": None,
            "session_id": None, "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
        state = {"version": runtime_adapter.STATE_VERSION,
                 "team_id": "team-m404-cli", "project_root": self.root,
                 "herdr_session": {"server_id": "srv", "socket_path": "/sock"},
                 "created_at": int(time.time()),
                 "members": {name: member_row}}
        conn = self.core()
        try:
            runtime_adapter.sync_state(conn, state)
        finally:
            conn.close()

    # -- command surface names/help -----------------------------------------

    def test_new_delivery_commands_are_registered(self):
        text = TEAMCTL.build_parser().format_help()
        for name in ("commit-create", "commit-reconcile", "ship-evidence",
                    "delivery-status", "delivery-diff", "delivery-accept",
                    "delivery-reject"):
            self.assertIn(name, text)

    def test_command_aliases_route_to_handlers(self):
        parser = TEAMCTL.build_parser()
        pairs = {
            "delivery-commit": "commit_create",
            "delivery-ship-evidence": "ship_record",
            "accept-local-delivery": "delivery_accept",
            "reject-local-delivery": "delivery_reject",
        }
        for name, handler in pairs.items():
            argv = [name, "--member", "dev", "--idempotency-key", "k"]
            if "accept" in name or "reject" in name:
                argv += ["--ship-evidence-id", "s", "--commit-sha", "d" * 40,
                       "--branch", "tc/t/a", "--actor", "a", "--reason", "r"]
            elif name == "delivery-commit":
                argv += ["--message", "m"]
            args = parser.parse_args(argv)
            self.assertIs(args.handler, getattr(TEAMCTL, handler), name)

    def test_accept_requires_exact_commit_and_branch(self):
        parser = TEAMCTL.build_parser()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["delivery-accept", "--member", "dev",
                                  "--ship-evidence-id", "s", "--actor", "a",
                                  "--reason", "r", "--idempotency-key", "k"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("--commit-sha", err.getvalue())
        self.assertIn("--branch", err.getvalue())

    # -- parity: CLI reaches the same adapter/Core entry points -----------

    def test_commit_create_forwards_identity_fence_and_json(self):
        task_id, attempt_id, _project = self._seed_member()
        captured = {}

        def fake(conn, member, **kw):
            captured["member"] = member
            captured.update(kw)
            return {"member": member, "source": "created",
                    "commit": {"commit_sha": "d" * 40}}

        with mock.patch.object(TEAMCTL._adapter, "create_member_local_commit",
                              side_effect=fake):
            code, out, err = self.run_cli([
                "commit-create", "--member", "dev", "--idempotency-key", "c1",
                "--message", "feat: local"])
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["member"], "dev")
        self.assertEqual(captured["idempotency_key"], "c1")
        self.assertIsInstance(captured["lease"], leader.Lease)
        self.assertEqual(out["commit"]["commit_sha"], "d" * 40)
        # fenced lease released: no residue on disk.
        self.assertEqual(self.lease_rows(), 0)

    def test_ship_and_status_and_diff_reach_adapter(self):
        calls = []
        with mock.patch.object(TEAMCTL._adapter, "record_member_ship_evidence",
                              side_effect=lambda c, m, **k: (calls.append(("ship", m)),
                                                           {"member": m, "proposed_action":
                                                            delivery.LOCAL_ONLY_ACCEPT_ACTION})[1]), \
                mock.patch.object(TEAMCTL._adapter, "member_delivery_status",
                                side_effect=lambda c, m, **k: (calls.append(("status", m)),
                                                            {"member": m, "bound": True})[1]), \
                mock.patch.object(TEAMCTL._adapter, "member_ship_diff",
                                side_effect=lambda c, m, **k: (calls.append(("diff", m)),
                                                            {"member": m, "diff": "+x"})[1]):
            code1, o1, e1 = self.run_cli(["ship-evidence", "--member", "dev",
                                         "--idempotency-key", "s1"])
            code2, o2, e2 = self.run_cli(["delivery-status", "--member", "dev"])
            code3, o3, e3 = self.run_cli(["delivery-diff", "--member", "dev"])
        self.assertEqual((code1, code2, code3), (0, 0, 0), (e1, e2, e3))
        self.assertEqual(o1["proposed_action"], delivery.LOCAL_ONLY_ACCEPT_ACTION)
        self.assertTrue(o2["bound"])
        self.assertEqual(o3["diff"], "+x")
        self.assertEqual(calls, [("ship", "dev"), ("status", "dev"), ("diff", "dev")])

    def test_reconcile_and_accept_forward_actor_reason_and_fence(self):
        seen = {}

        def fake_reconcile(conn, member, command_id, **kw):
            seen["reconcile"] = (member, command_id, kw)
            return {"member": member, "resolution": "adopted",
                    "command": {"command_id": command_id}, "commit": None}

        def fake_decide(conn, member, **kw):
            seen["decide_" + kw["decision"]] = (member, kw)
            return {"member": member, "decision": kw["decision"],
                    "task_status_after": "done" if kw["decision"] == "accept" else "verifying",
                    "approval": {"approval_id": "a1"}}

        with mock.patch.object(TEAMCTL._adapter, "reconcile_member_commit_receipt",
                              side_effect=fake_reconcile), \
                mock.patch.object(TEAMCTL._adapter, "decide_member_local_delivery",
                                side_effect=fake_decide):
            code_a, out_a, err_a = self.run_cli([
                "commit-reconcile", "--member", "dev", "--command-id", "cmd-9",
                "--actor", "ops", "--reason", "lost"])
            code_ok, out_ok, err_ok = self.run_cli([
                "delivery-accept", "--member", "dev", "--ship-evidence-id", "s1",
                "--commit-sha", "d" * 40, "--branch", "tc/t/a", "--actor", "human",
                "--reason", "ok", "--idempotency-key", "acc-1"])
            code_no, out_no, err_no = self.run_cli([
                "delivery-reject", "--member", "dev", "--ship-evidence-id", "s1",
                "--actor", "human", "--reason", "nope", "--idempotency-key", "rej-1"])
        self.assertEqual((code_a, code_ok, code_no), (0, 0, 0), (err_a, err_ok, err_no))
        self.assertEqual(out_a["resolution"], "adopted")
        self.assertEqual(out_ok["task_status_after"], "done")
        self.assertEqual(out_no["task_status_after"], "verifying")
        # accept echoes exact commit + branch into the adapter
        self.assertEqual(seen["decide_accept"][1]["commit_sha"], "d" * 40)
        self.assertEqual(seen["decide_accept"][1]["branch"], "tc/t/a")
        self.assertIsInstance(seen["decide_accept"][1]["lease"], leader.Lease)
        self.assertEqual(seen["decide_reject"][1]["commit_sha"], None)
        self.assertEqual(self.lease_rows(), 0)

    # -- fail-closed mapping -----------------------------------------------

    def test_delivery_status_real_read_path_no_evidence(self):
        # Genuine, no-mock call through adapter + Core delivery read paths.
        self._seed_synced_member("dev")
        code, out, err = self.run_cli(["delivery-status", "--member", "dev"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out["bound"])
        # Nothing shipped yet: fail-closed view, not a fabricated PASS.
        self.assertIsNone(out["commit"])
        self.assertIsNone(out["ship"])
        self.assertIsNone(out["proposed_action"])

    def test_ship_evidence_missing_commit_fails_closed(self):
        # Real Core path: no durable commit evidence -> NotFound, exit non-zero.
        self._seed_synced_member("dev")
        code, _out, err = self.run_cli([
            "ship-evidence", "--member", "dev", "--idempotency-key", "s1"])
        self.assertEqual(code, 1)
        self.assertTrue(err.strip())

    def test_delivery_diff_missing_evidence_fails_closed(self):
        self._seed_synced_member("dev")
        code, _out, err = self.run_cli(["delivery-diff", "--member", "dev"])
        self.assertEqual(code, 1)
        self.assertTrue(err.strip())

    def test_report_only_refusal_exits_nonzero_with_kind(self):
        def boom(conn, member, **kw):
            raise delivery.DeliveryRefusal("mode", "task policy is report_only")

        with mock.patch.object(TEAMCTL._adapter, "create_member_local_commit",
                              side_effect=boom):
            code, _out, err = self.run_cli([
                "commit-create", "--member", "dev", "--idempotency-key", "k"])
        self.assertEqual(code, 1)
        self.assertIn("report_only", err)
        self.assertIn("(mode)", err)
        self.assertEqual(self.lease_rows(), 0)

    def test_stale_verifier_ship_refusal_fails_closed(self):
        def boom(conn, member, **kw):
            raise delivery.DeliveryRefusal("verifier", "verifier evidence is stale")

        with mock.patch.object(TEAMCTL._adapter, "record_member_ship_evidence",
                              side_effect=boom):
            code, _out, err = self.run_cli([
                "ship-evidence", "--member", "dev", "--idempotency-key", "k"])
        self.assertEqual(code, 1)
        self.assertIn("(verifier)", err)

    def test_stale_core_guard_is_actionable_not_attribute_error(self):
        # A Core older than M4-04 (delivery module absent) fails with a
        # reinstall hint on every M4-04 command, not an AttributeError.
        with mock.patch.object(TEAMCTL, "_delivery", None):
            with self.assertRaises(TEAMCTL.TeamctlError) as ctx:
                TEAMCTL._require_m404_core()
        message = str(ctx.exception)
        self.assertIn("too old", message)
        self.assertIn("install.sh", message)
        # and through the real command handler: exit code 1, same hint.
        with mock.patch.object(TEAMCTL, "_delivery", None):
            code, _out, err = self.run_cli([
                "ship-evidence", "--member", "dev", "--idempotency-key", "k"])
        self.assertEqual(code, 1)
        self.assertIn("too old", err)


if __name__ == "__main__":
    unittest.main()
