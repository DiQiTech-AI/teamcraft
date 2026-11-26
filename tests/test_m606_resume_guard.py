"""Regression: ``main()`` must honor ``--resume-handoff`` in its pre-I/O root guard.

Before the fix, ``main`` called ``validate_root`` without ``for_resume`` even
when ``--resume-handoff`` was supplied, so a populated phase-1 handoff root was
rejected as "not a fresh root" before the runner could continue it.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (Path(__file__).resolve().parents[1]
          / "scripts" / "m606_opencode_e2e.py")
SPEC = importlib.util.spec_from_file_location("m606_e2e_resume_guard", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class ResumeHandoffGuardTests(unittest.TestCase):
    def test_resume_handoff_accepts_a_populated_allocated_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m606-resume-guard-") as tmp:
            root = Path(tmp) / "run"
            (root / "evidence").mkdir(parents=True)
            (root / "evidence" / "handoff.json").write_text("{}", encoding="utf-8")
            # A resumable root must be journalled; otherwise the guard (rightly)
            # refuses to touch it.
            (root / "evidence" / "journal.jsonl").write_text("", encoding="utf-8")
            (root / "project").mkdir()
            # ``main`` catches RuntimeError (returning 3), so use an uncaught
            # exception to prove the root guard let us reach the runner.
            sentinel = AssertionError("reached-runner")
            with mock.patch.object(MOD, "M606LiveRun", side_effect=sentinel):
                with self.assertRaisesRegex(AssertionError, "reached-runner"):
                    MOD.main([
                        "--confirm-live-run", "--resume-handoff",
                        "--root", str(root), "--stop-after", "dispatch", "--json",
                    ])


if __name__ == "__main__":
    unittest.main()
