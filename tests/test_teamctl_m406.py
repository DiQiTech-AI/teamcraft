"""M4-06 teamctl read-path parity tests.

These drive the real ``teamctl.py`` script against the real Core adapter and a
canonical ``<root>/.teamcraft`` runtime.  They pin the two READ-ONLY commands:

* ``integration-status`` never appends an audit Event on first or later reads,
  and returns one stable DTO schema before and after an explicit check;
* ``worktree-cleanup-status`` never repairs missing worktree Git identity
  markers: the refusal is reported, the scene is preserved, and no Event is
  written.

The deeper Core semantics live in ``tests/test_m406_integration_cleanup.py``;
here the CLI wiring itself is exercised end-to-end.  Every Git effect happens
in a TEMPORARY repository; the real ``.teamcraft/worktrees`` is never touched.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1]
          / ".agents/skills/teamcraft/scripts/teamctl.py")
M406_TEST = Path(__file__).with_name("test_m406_integration_cleanup.py")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TEAMCTL = _load_module(SCRIPT, "teamctl_m406_test")
M406 = _load_module(M406_TEST, "m406_core_fixture_for_teamctl")


class TeamctlM406ReadPathTests(M406.M406Fixture):
    """Real CLI + real Core adapter + isolated runtime root."""

    runtime_root_mode = True

    def run_cli(self, argv: list[str]) -> tuple[int, dict | str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = TEAMCTL.main(argv + ["--cwd", str(self.base)])
            except SystemExit as exc:  # pragma: no cover - argparse only
                code = int(exc.code or 0)
        raw = out.getvalue().strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return code, parsed, err.getvalue()

    def event_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0])

    def unset_markers(self, path: str) -> tuple[str, ...]:
        keys = ("teamcraft.allocationId", "teamcraft.attemptId",
                "teamcraft.taskId")
        for key in keys:
            M406.git("config", "--worktree", "--unset-all", key,
                     cwd=Path(path))
            self.assertIsNone(self.marker_value(path, key))
        return keys

    # -- integration-status ---------------------------------------------------

    def test_integration_status_is_read_only_and_dto_is_stable(self):
        task, attempt, _allocation = self.make_ship()
        self.bind_member(task, attempt, name="m406-cli-integration")
        before = self.event_count()

        code, first, _err = self.run_cli(
            ["integration-status", "--member", "m406-cli-integration"])
        self.assertEqual(code, 0)
        self.assertIsInstance(first, dict)
        assert isinstance(first, dict)
        self.assertIsNotNone(first["integration"])
        self.assertEqual(self.event_count(), before)
        first_dto = first["integration"]

        # The explicit check owns the durable observation...
        code, _check, _err = self.run_cli(
            ["integration-check", "--member", "m406-cli-integration"])
        self.assertEqual(code, 0)
        self.assertEqual(self.event_count(), before + 1)

        # ...while both status reads share one identical DTO shape and value.
        code, second, _err = self.run_cli(
            ["integration-status", "--member", "m406-cli-integration"])
        self.assertEqual(code, 0)
        self.assertIsInstance(second, dict)
        assert isinstance(second, dict)
        self.assertEqual(set(first_dto), set(second["integration"]))
        self.assertEqual(first_dto, second["integration"])
        self.assertNotIn("schema_version", second["integration"])
        self.assertNotIn("source_task_id", second["integration"])
        self.assertEqual(self.event_count(), before + 1)

    # -- worktree-cleanup-status ---------------------------------------------

    def test_cleanup_status_never_repairs_missing_markers(self):
        task, attempt, allocation, _commit, _ship = self.made_cleanable()
        self.bind_member(task, attempt, name="m406-cli-cleanup")
        path = allocation.worktree_path
        marker_keys = self.unset_markers(path)
        before = self.event_count()

        code, payload, err = self.run_cli(
            ["worktree-cleanup-status", "--member", "m406-cli-cleanup"])
        self.assertEqual(code, 0, err)
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        cleanup = payload["cleanup"]
        self.assertFalse(cleanup["eligible"])
        self.assertIn("marker", " | ".join(cleanup["reasons"]))
        self.assertEqual(self.event_count(), before)
        for key in marker_keys:
            self.assertIsNone(self.marker_value(path, key))
        # The workspace itself and its registration remain untouched.
        self.assertTrue(Path(path).exists())
        self.assertTrue(self.worktree_registered(path))


if __name__ == "__main__":
    unittest.main()
