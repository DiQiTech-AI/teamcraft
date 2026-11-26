"""Fail-closed CLI evidence for legacy UNKNOWN Attempt reconciliation."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


TEAMCTL_PATH = (Path(__file__).resolve().parents[1]
                / ".agents/skills/teamcraft/scripts/teamctl.py")


def load_teamctl():
    spec = importlib.util.spec_from_file_location(
        "teamctl_legacy_unknown_test", TEAMCTL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TEAMCTL = load_teamctl()


class FakeHerdr:
    def __init__(self, *, agents=None, panes=None, malformed_workspaces=False):
        self.agents = [] if agents is None else agents
        self.panes = [] if panes is None else panes
        self.malformed_workspaces = malformed_workspaces

    def current_pane(self, _pane_id):
        raise TEAMCTL.TeamctlError(json.dumps({
            "error": {"code": "pane_not_found", "message": "pane not found"}
        }))

    def get_agent(self, _name):
        raise TEAMCTL.TeamctlError(json.dumps({
            "error": {"code": "agent_not_found", "message": "agent not found"}
        }))

    def list_agents(self):
        return self.agents

    def request(self, argv):
        if argv == ["workspace", "list"]:
            if self.malformed_workspaces:
                return {"result": {"workspaces": "invalid"}}
            return {"result": {"workspaces": [{"workspace_id": "w3"}]}}
        if argv == ["pane", "list", "--workspace", "w3"]:
            return {"result": {"panes": self.panes}}
        raise AssertionError(f"unexpected Herdr request: {argv}")


class LegacyStoppedEvidenceTests(unittest.TestCase):
    def member(self):
        return {
            "name": "legacy_dev",
            "kind": "opencode",
            "pane_id": "w3:pK",
            "terminal_id": "term_old",
            "agent_session": {"kind": "id", "value": "session_old"},
        }

    def evidence(self, fake):
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=fake):
            return TEAMCTL._stop_evidence(
                self.member(), require_live_agent=False
            )

    def test_absent_recorded_identity_is_verified_as_stopped(self):
        self.assertEqual(self.evidence(FakeHerdr()), {
            "identity_verified": True,
            "process_alive": False,
            "pane_id": "w3:pK",
            "terminal_id": "term_old",
            "native_session_id": "session_old",
            # D8: absence proves the recorded identity is gone, not that a
            # live identity was verified.
            "identity_mode": "recorded_identity_absent",
            "recorded_identity_absent": True,
        })

    def test_any_live_identity_collision_fails_closed(self):
        collisions = (
            {"name": "legacy_dev"},
            {"pane_id": "w3:pK"},
            {"terminal_id": "term_old"},
            {"agent_session": {"kind": "id", "value": "session_old"}},
        )
        for live in collisions:
            with self.subTest(live=live), self.assertRaisesRegex(
                TEAMCTL.TeamctlError, "still appears"
            ):
                self.evidence(FakeHerdr(agents=[live]))

    def test_shell_pane_terminal_collision_and_malformed_inventory_fail_closed(self):
        with self.assertRaisesRegex(TEAMCTL.TeamctlError, "still appears"):
            self.evidence(FakeHerdr(panes=[{"terminal_id": "term_old"}]))
        with self.assertRaisesRegex(TEAMCTL.TeamctlError, "workspace inventory"):
            self.evidence(FakeHerdr(malformed_workspaces=True))


if __name__ == "__main__":
    unittest.main()
