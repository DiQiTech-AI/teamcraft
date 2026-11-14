"""M4-03 runtime-adapter tests for member-scoped verifier routing."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from teamcraft import leader, runtime_adapter as ra, verification
from teamcraft.runtime_adapter import RuntimeAdapterError


def member(name: str, *, read_only: bool = False):
    return {
        "name": name, "role": "tester" if read_only else "developer",
        "responsibility": f"own {name}", "kind": "opencode",
        "task": f"deliver {name}", "ownership": [] if read_only else ["src/**"],
        "read_only": read_only, "cwd": None, "from_pane_id": "w1:p1",
        "pane_id": "w1:p2", "terminal_id": None, "agent_session": None,
        "agent_args": [], "opencode_config_content": None, "direction": None,
        "start_timeout_ms": 30_000, "phase": "pane_created",
        "prompt_may_have_been_delivered": False, "live_state": None,
        "error": None, "task_id": None, "attempt_id": None,
        "session_id": None, "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


class M403RuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m403-adapter-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "project"
        self.root.mkdir()
        self.conn, _ = ra.open_runtime(self.root)
        self.addCleanup(self.conn.close)
        self.state = {
            "version": ra.STATE_VERSION, "team_id": "team-m403",
            "project_root": str(self.root),
            "herdr_session": {"server_id": "srv", "socket_path": "/sock"},
            "created_at": int(time.time()),
            "members": {
                "dev": member("dev"),
                "reader": member("reader", read_only=True),
            },
        }
        for value in self.state["members"].values():
            value["cwd"] = str(self.root)
        ra.sync_state(self.conn, self.state)

    def lease(self):
        project_id = self.conn.execute(
            "SELECT project_id FROM tasks WHERE id=?",
            (self.state["members"]["dev"]["task_id"],)).fetchone()[0]
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(str(project_id)), "adapter-test",
            ttl_seconds=60)

    def fake_result(self) -> verification.WorktreeVerification:
        task_id = self.state["members"]["dev"]["task_id"]
        attempt_id = self.state["members"]["dev"]["attempt_id"]
        return verification.WorktreeVerification(
            id="verification-1", command_id="command-1",
            project_id=self.conn.execute(
                "SELECT project_id FROM tasks WHERE id=?",
                (task_id,)).fetchone()[0],
            task_id=task_id, attempt_id=attempt_id, attempt_no=1,
            task_version=2, dispatch_task_version=1,
            brief_digest="a" * 64, worker_report_id=1,
            worker_report_digest="b" * 64, allocation_id="allocation-1",
            worktree_path="/tmp/worktree", branch="tc/task/attempt",
            base_sha="c" * 40, claims=("src/**",), profile="test",
            profile_version=1, config_digest="d" * 64,
            argv=("python3", "-m", "pytest"), status="pass", exit_code=0,
            report=b"{}", report_digest="e" * 64,
            log_relpath="worktree/attempt/result-test.log",
            log_digest="f" * 64, created_at="2026-01-01T00:00:00+00:00",
        )

    def test_writable_member_routes_exact_attempt_and_returns_evidence_dto(self):
        expected = self.fake_result()
        lease = self.lease()
        with mock.patch.object(
            verification, "run_worktree_verifier", return_value=expected,
        ) as runner:
            result = ra.verify_member_workspace(
                self.conn, "dev", profile="test", idempotency_key="run-1",
                lease=lease, artifact_root="/tmp/artifacts",
            )
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[1], expected.attempt_id)
        self.assertEqual(runner.call_args.kwargs["profile"], "test")
        self.assertEqual(result["verification"]["verification_id"], expected.id)
        self.assertEqual(result["verification"]["argv"], list(expected.argv))
        self.assertEqual(result["verification"]["status"], "pass")

    def test_read_only_member_is_refused_before_core_execution(self):
        with mock.patch.object(verification, "run_worktree_verifier") as runner:
            with self.assertRaises(RuntimeAdapterError):
                ra.verify_member_workspace(
                    self.conn, "reader", profile="test",
                    idempotency_key="reader-run", lease=self.lease())
        runner.assert_not_called()

    def test_suite_and_profile_configuration_forward_member_project(self):
        lease = self.lease()
        with mock.patch.object(
            verification, "run_worktree_suite",
            return_value={"verdict": "pass", "profiles": {}},
        ) as suite:
            result = ra.run_member_workspace_verifier_suite(
                self.conn, "dev", idempotency_key="suite-1", lease=lease,
                profiles=["lint", "test"])
        self.assertEqual(result["suite"]["verdict"], "pass")
        self.assertEqual(suite.call_args.args[1],
                         self.state["members"]["dev"]["attempt_id"])
        self.assertEqual(suite.call_args.kwargs["profiles"], ["lint", "test"])

        with mock.patch.object(
            verification, "configure_verifier_profile",
            return_value={"profile": "lint", "profile_version": 1},
        ) as configure:
            configured = ra.configure_project_verifier_profile(
                self.conn, "dev", profile="lint", argv=["ruff", "check"],
                timeout_seconds=30, actor="human", idempotency_key="cfg-1",
                lease=lease)
        self.assertEqual(configured["profile"], "lint")
        project_id = self.conn.execute(
            "SELECT project_id FROM tasks WHERE id=?",
            (self.state["members"]["dev"]["task_id"],)).fetchone()[0]
        self.assertEqual(configure.call_args.args[1], project_id)


if __name__ == "__main__":
    unittest.main()
