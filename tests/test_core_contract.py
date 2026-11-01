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
from teamcraft.ids import DEFAULT_ID_FACTORY, Uuid4IdFactory, is_uuid4_string


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
                explicit.resolve() / ".teamcraft",
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

    def test_runtime_directory_uses_teamcraft_name(self) -> None:
        config = TeamCraftConfig(Path("/project"))
        self.assertEqual(config.runtime_directory, Path("/project/.teamcraft"))

    def test_entity_ids_are_opaque_non_empty_strings(self) -> None:
        self.assertEqual(require_entity_id("task_01"), "task_01")
        # M1-04 fix-4 regression: the contract is "opaque, non-empty string"
        # and nothing more.  IDs the M1-02 contract accepted must keep being
        # accepted — no length cap, no character whitelist beyond non-empty.
        previously_accepted = [
            "task.01",
            "TASK-Upper-9",
            "id with spaces",
            "path/like/id",
            "colon:id",
            "中文任務標識",
            "emoji-🚀-id",
            "legacy/member/name",
            "wild#tag",
            "query?mark",
            "percent%sign",
            "0",
            "-leading-dash",
            "_leading_underscore",
            "trailing-",
            "x" * 64,
            "x" * 65,
            "a" * 500,
        ]
        for value in previously_accepted:
            self.assertEqual(require_entity_id(value), value, value)
        with self.assertRaises(ValidationError):
            require_entity_id(" ")
        with self.assertRaises(ValidationError):
            require_entity_id("")
        for bad in (None, 123, ["x"], b"bytes"):
            with self.assertRaises(ValidationError):
                require_entity_id(bad)

    def test_default_id_factory_emits_uuid4_without_narrowing_the_contract(self) -> None:
        # UUID4 is the default GENERATOR representation; acceptance never
        # depends on that shape (advisory predicate only).
        value = DEFAULT_ID_FACTORY.new("task")
        self.assertTrue(is_uuid4_string(value))
        self.assertEqual(require_entity_id(value), value)
        prefixed = Uuid4IdFactory(prefix="task").new("task")
        self.assertTrue(prefixed.startswith("task_"))
        self.assertTrue(is_uuid4_string(prefixed[len("task_") :]))
        self.assertEqual(require_entity_id(prefixed), prefixed)
        # namespace LABELS stay tidy construction-time identifiers
        with self.assertRaises(ValidationError):
            Uuid4IdFactory(prefix="bad prefix")
        self.assertFalse(is_uuid4_string("task_01"))

    def test_clock_and_utc_validation(self) -> None:
        now = SystemClock().now()
        self.assertEqual(now.utcoffset(), timedelta(0))
        converted = require_utc(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(converted, datetime(2025, 12, 31, 16, tzinfo=timezone.utc))
        with self.assertRaises(ValidationError):
            require_utc(datetime(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
