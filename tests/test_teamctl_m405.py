"""M4-05 teamctl parser and capability guard tests."""

import importlib.util
from types import SimpleNamespace
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1]
          / ".agents/skills/teamcraft/scripts/teamctl.py")


def load_teamctl():
    spec = importlib.util.spec_from_file_location("teamctl_m405_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TEAMCTL = load_teamctl()


class TeamctlM405Tests(unittest.TestCase):
    def test_direct_pr_commands_are_registered(self):
        parser = TEAMCTL.build_parser()
        cases = {
            "pr-prepare": ["--member", "dev", "--idempotency-key", "k"],
            "pr-sync": ["--member", "dev", "--idempotency-key", "k"],
            "pr-status": ["--member", "dev", "--offline"],
            "pr-approval": [
                "--member", "dev", "--pr-evidence-id", "pr-1",
                "--decision", "approve", "--pr-number", "12",
                "--head-sha", "a" * 40, "--base", "main",
                "--actor", "human", "--reason", "reviewed",
                "--idempotency-key", "k"],
            "pr-merge": ["--member", "dev", "--pr-evidence-id", "pr-1",
                         "--idempotency-key", "k"],
            "pr-reconcile": ["--member", "dev", "--command-id", "cmd-1",
                             "--actor", "human", "--reason", "inspected"],
        }
        handlers = {
            "pr-prepare": TEAMCTL.pr_prepare, "pr-sync": TEAMCTL.pr_sync,
            "pr-status": TEAMCTL.pr_status, "pr-approval": TEAMCTL.pr_approval,
            "pr-merge": TEAMCTL.pr_merge,
            "pr-reconcile": TEAMCTL.pr_reconcile,
        }
        for name, argv in cases.items():
            with self.subTest(name=name):
                args = parser.parse_args([name, *argv])
                self.assertIs(args.handler, handlers[name])

    def test_approval_rejects_unknown_decision_at_parse_time(self):
        with self.assertRaises(SystemExit):
            TEAMCTL.build_parser().parse_args([
                "pr-approval", "--member", "dev",
                "--pr-evidence-id", "pr-1", "--decision", "accept",
                "--actor", "human", "--reason", "reviewed",
                "--idempotency-key", "k"])

    def test_reconcile_handler_fences_effect_and_forwards_scope(self):
        conn = mock.Mock()
        lease = SimpleNamespace(scope="project:p1", holder_id="holder", epoch=1)
        store = mock.Mock()
        store._connect.return_value = conn
        store.acquire_lease.return_value = lease
        args = SimpleNamespace(
            cwd="/project", member="dev", command_id="cmd-1",
            actor="human", reason="inspected")
        expected = {"resolution": "adopted", "member": "dev"}
        with mock.patch.object(TEAMCTL, "resolve_project_root",
                               return_value=Path("/project")), \
             mock.patch.object(TEAMCTL, "StateStore", return_value=store), \
             mock.patch.object(TEAMCTL, "_require_m405_core"), \
             mock.patch.object(
                 TEAMCTL._adapter, "reconcile_member_pr_effect",
                 return_value=expected) as reconcile, \
             mock.patch("builtins.print") as output:
            self.assertEqual(TEAMCTL.pr_reconcile(args), 0)
        reconcile.assert_called_once_with(
            conn, "dev", "cmd-1", lease=lease,
            actor="human", reason="inspected")
        conn.close.assert_called_once_with()
        store.release_lease.assert_called_once_with(lease)
        self.assertIn('"resolution": "adopted"', output.call_args.args[0])

    def test_capability_guard_names_missing_runtime_surface(self):
        with mock.patch.object(TEAMCTL._adapter, "reconcile_member_pr_effect",
                               new=None):
            with self.assertRaises(TEAMCTL.TeamctlError) as caught:
                TEAMCTL._require_m405_core()
        self.assertIn("reconcile_member_pr_effect", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
