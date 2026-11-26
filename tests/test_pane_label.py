"""Pane label convention tests: ``<role> · <task>`` on member launch.

Covers the label derivation (explicit ``--label`` priority, default
derivation with role display names, empty-task fallback, 40-code-point
truncation) and the launch wiring: after a successful member start the
``rename_pane`` adapter method is called with the derived label, and a rename
failure is best-effort (launch still succeeds, nothing rolls back).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import types
import unittest
from pathlib import Path
from unittest import mock

from teamcraft.herdr_adapter import HerdrAdapter, HerdrError

SCRIPT = (Path(__file__).resolve().parents[1]
          / ".agents/skills/teamcraft/scripts/teamctl.py")


def _load_teamctl():
    spec = importlib.util.spec_from_file_location("teamctl_pane_label_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TEAMCTL = _load_teamctl()


class FakeHerdr:
    """Mirror of the FakeHerdr used by the adapter contract tests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, timeout, accepted):
        self.calls.append((list(argv), timeout, accepted))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RenamePaneAdapterTests(unittest.TestCase):
    def test_rename_pane_passes_label_as_a_single_argument(self):
        fake = FakeHerdr([('{"result":{"pane":{"pane_id":"p1"}}}', "", 0)])
        adapter = HerdrAdapter(fake)
        adapter.rename_pane("p1", "developer · M4-06 文档同步")
        self.assertEqual(
            fake.calls[0][0],
            ["herdr", "pane", "rename", "p1", "developer · M4-06 文档同步"],
        )


class PaneLabelDerivationTests(unittest.TestCase):
    def test_explicit_label_wins_over_derivation(self):
        self.assertEqual(
            TEAMCTL.derive_pane_label("developer", "Fix login", explicit="custom label"),
            "custom label",
        )

    def test_default_derivation_uses_role_display_and_first_line(self):
        self.assertEqual(
            TEAMCTL.derive_pane_label("ceo", "Plan the release\nThen staff"),
            "CEO · Plan the release",
        )
        self.assertEqual(
            TEAMCTL.derive_pane_label("cto", "Architecture\nNotes"),
            "CTO · Architecture",
        )
        self.assertEqual(
            TEAMCTL.derive_pane_label("developer", "M4-06 文档同步"),
            "developer · M4-06 文档同步",
        )

    def test_other_roles_keep_their_raw_name(self):
        for role in ("developer", "designer", "tester", "custom"):
            self.assertEqual(
                TEAMCTL.derive_pane_label(role, "T"),
                f"{role} · T",
            )

    def test_first_nonempty_line_skips_blank_lines(self):
        self.assertEqual(
            TEAMCTL.derive_pane_label("developer", "\n  \n\t\nReal task\nsecond"),
            "developer · Real task",
        )

    def test_truncation_at_40_code_points_is_multibyte_safe(self):
        task = "字" * 60
        label = TEAMCTL.derive_pane_label("developer", task)
        self.assertEqual(label, "developer · " + "字" * 40)
        # The summary portion is exactly 40 code points, never split mid-character.
        self.assertEqual(len(label.split(" · ", 1)[1]), 40)

    def test_empty_and_whitespace_only_task_degrades_to_role_display(self):
        self.assertEqual(TEAMCTL.derive_pane_label("developer", ""), "developer")
        self.assertEqual(TEAMCTL.derive_pane_label("tester", "   \n \t\n"), "tester")
        self.assertEqual(TEAMCTL.derive_pane_label("ceo", ""), "CEO")


class SetMemberPaneLabelTests(unittest.TestCase):
    def _member(self, **overrides):
        member = {
            "name": "api_dev", "role": "developer", "task": "Fix the login bug",
            "pane_id": "pane-1",
        }
        member.update(overrides)
        return member

    def test_calls_rename_pane_with_derived_label(self):
        adapter = mock.Mock()
        adapter.rename_pane.return_value = {}
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter):
            label = TEAMCTL.set_member_pane_label(self._member())
        adapter.rename_pane.assert_called_once_with(
            "pane-1", "developer · Fix the login bug"
        )
        self.assertEqual(label, "developer · Fix the login bug")

    def test_explicit_label_is_passed_through(self):
        adapter = mock.Mock()
        adapter.rename_pane.return_value = {}
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter):
            TEAMCTL.set_member_pane_label(self._member(), explicit="custom label")
        adapter.rename_pane.assert_called_once_with("pane-1", "custom label")

    def test_no_pane_id_skips_rename(self):
        adapter = mock.Mock()
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter):
            result = TEAMCTL.set_member_pane_label(self._member(pane_id=None))
        adapter.rename_pane.assert_not_called()
        self.assertIsNone(result)

    def test_rename_failure_is_best_effort_and_warns(self):
        adapter = mock.Mock()
        adapter.rename_pane.side_effect = TEAMCTL.TeamctlError("rename failed")
        err = io.StringIO()
        with mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter), \
                contextlib.redirect_stderr(err):
            result = TEAMCTL.set_member_pane_label(self._member())
        self.assertIsNone(result)
        self.assertIn("warning", err.getvalue())


class LaunchCallsRenameTests(unittest.TestCase):
    """The launch flow must call ``rename_pane`` after a successful start."""

    def _started_member(self):
        return {
            "name": "api_dev", "role": "developer", "kind": "codex",
            "task": "Fix the login bug", "responsibility": "code",
            "ownership": ["src/**"], "read_only": False, "cwd": "/work",
            "phase": "agent_started", "pane_id": "pane-1",
            "terminal_id": "term-1", "agent_session": None,
            "live_state": "idle", "error": None,
        }

    def _run_launch(self, adapter, *, label=None):
        started = self._started_member()

        class FakeStore:
            def __init__(self, member):
                self.state = {"team_id": "team-1",
                              "members": {member["name"]: member}}
            def acquire_lease(self, name, ttl):
                return object()
            def release_lease(self, lease):
                pass
            def require_ceo_initialized(self):
                return None
            def locked(self):
                @contextlib.contextmanager
                def held():
                    yield self.state
                return held()

        args = types.SimpleNamespace(
            role="developer", kind="codex", name="api_dev",
            task="Fix the login bug", label=label,
            agent_arg=[], opencode_model=None, from_pane=None,
            direction=None, start_timeout_ms=30_000,
            wait=False, timeout_ms=120_000,
            read_only=False, responsibility=None,
            ownership=["src/**"], cwd="/work",
        )
        store = FakeStore(started)
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(TEAMCTL, "require_herdr", return_value=None),
            mock.patch.object(
                TEAMCTL, "validate_common_launch",
                return_value=("/work", "Fix the login bug", "code", ["src/**"]),
            ),
            mock.patch.object(TEAMCTL, "launch_agent_args", return_value=[]),
            mock.patch.object(TEAMCTL, "preflight_kind"),
            mock.patch.object(TEAMCTL, "ensure_live_name_available"),
            mock.patch.object(TEAMCTL, "resolve_pane", return_value={"pane_id": "parent"}),
            mock.patch.object(
                TEAMCTL, "current_session_identity",
                return_value={"socket_path": "/x"},
            ),
            mock.patch.object(TEAMCTL, "StateStore", return_value=store),
            mock.patch.object(TEAMCTL, "reserve_member", return_value=started),
            mock.patch.object(TEAMCTL, "split_for_member", return_value=started),
            mock.patch.object(TEAMCTL, "start_member", return_value=started),
            mock.patch.object(TEAMCTL, "prompt_member", return_value=started),
            mock.patch.object(TEAMCTL, "_herdr_adapter", return_value=adapter),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = TEAMCTL._launch_operation(args)
        return code, err.getvalue()

    def test_launch_flow_renames_pane_after_start(self):
        rename_calls = []

        class RecordingAdapter:
            def rename_pane(self, pane_id, label):
                rename_calls.append((pane_id, label))
                return {}

        code, _ = self._run_launch(RecordingAdapter())
        self.assertEqual(code, 0)
        self.assertEqual(rename_calls, [("pane-1", "developer · Fix the login bug")])

    def test_launch_flow_honors_explicit_label(self):
        rename_calls = []

        class RecordingAdapter:
            def rename_pane(self, pane_id, label):
                rename_calls.append((pane_id, label))
                return {}

        code, _ = self._run_launch(RecordingAdapter(), label="API 修复（补丁）")
        self.assertEqual(code, 0)
        self.assertEqual(rename_calls, [("pane-1", "API 修复（补丁）")])

    def test_launch_flow_rename_failure_does_not_fail_launch(self):
        class FailingAdapter:
            def rename_pane(self, pane_id, label):
                raise TEAMCTL.TeamctlError("boom")

        code, err = self._run_launch(FailingAdapter())
        self.assertEqual(code, 0)
        self.assertIn("warning", err)


if __name__ == "__main__":
    unittest.main()
