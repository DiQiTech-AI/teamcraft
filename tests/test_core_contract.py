from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from teamcraft import (
    ConfigurationError,
    SystemClock,
    TeamCraftConfig,
    ValidationError,
    load_config,
    require_entity_id,
    require_utc,
)


class CoreBoundaryTests(unittest.TestCase):
    def test_import_has_no_filesystem_side_effects(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(repository)
            result = subprocess.run(
                [sys.executable, "-B", "-c", "import teamcraft"],
                cwd=temporary,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_config_explicit_root_precedes_environment_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            explicit = base / "explicit"
            configured = load_config(
                explicit,
                environ={"TEAMCRAFT_PROJECT_ROOT": str(base / "environment")},
            )

            self.assertEqual(configured.project_root, explicit.resolve())
            self.assertEqual(
                configured.runtime_directory,
                explicit.resolve() / ".herdr-team",
            )
            self.assertFalse(explicit.exists())

    def test_config_environment_root_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            configured = load_config(environ={"TEAMCRAFT_PROJECT_ROOT": str(root)})
            self.assertEqual(configured, TeamCraftConfig(root.resolve()))

    def test_empty_environment_root_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_config(environ={"TEAMCRAFT_PROJECT_ROOT": "  "})

    def test_runtime_directory_keeps_legacy_state_name(self) -> None:
        config = TeamCraftConfig(Path("/project"))
        self.assertEqual(config.runtime_directory, Path("/project/.herdr-team"))

    def test_entity_ids_are_opaque_non_empty_strings(self) -> None:
        self.assertEqual(require_entity_id("task_01"), "task_01")
        with self.assertRaises(ValidationError):
            require_entity_id(" ")

    def test_clock_and_utc_validation(self) -> None:
        now = SystemClock().now()
        self.assertEqual(now.utcoffset(), timedelta(0))
        converted = require_utc(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(converted, datetime(2025, 12, 31, 16, tzinfo=timezone.utc))
        with self.assertRaises(ValidationError):
            require_utc(datetime(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
