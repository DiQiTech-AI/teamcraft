from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "scripts" / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def run_installer(
        self, target: Path, *arguments: str, expected_code: int = 0
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", str(INSTALLER), "--target", str(target), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            expected_code,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def test_first_install_copies_resources_and_creates_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)

            self.assertEqual(
                (target / ".agents/skills/teamcraft/SKILL.md").read_bytes(),
                (REPOSITORY / ".agents/skills/teamcraft/SKILL.md").read_bytes(),
            )
            self.assertTrue(target.joinpath(".opencode/agents/herdr-ceo.md").is_file())
            self.assertEqual(
                target.joinpath("opencode.json").read_bytes(),
                REPOSITORY.joinpath("opencode.example.json").read_bytes(),
            )
            ignore_lines = target.joinpath(".gitignore").read_text().splitlines()
            self.assertIn("/.herdr-team/", ignore_lines)
            self.assertIn("/opencode.json", ignore_lines)

    def test_repeated_install_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)
            before = {
                path.relative_to(target): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }

            result = self.run_installer(target)
            after = {
                path.relative_to(target): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertIn("0 change(s)", result.stdout)

    def test_existing_opencode_config_is_always_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            target.mkdir()
            custom = {"model": "custom/provider", "token": "local-only"}
            target.joinpath("opencode.json").write_text(json.dumps(custom))

            self.run_installer(target, "--force")

            self.assertEqual(
                json.loads(target.joinpath("opencode.json").read_text()), custom
            )

    def test_conflict_fails_before_any_files_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            conflict = target / ".opencode/agents/herdr-ceo.md"
            conflict.parent.mkdir(parents=True)
            conflict.write_text("user content\n")

            result = self.run_installer(target, expected_code=1)

            self.assertIn("managed file differs", result.stderr)
            self.assertEqual(conflict.read_text(), "user content\n")
            self.assertFalse(target.joinpath(".agents").exists())
            self.assertFalse(target.joinpath("opencode.json").exists())
            self.assertFalse(target.joinpath(".gitignore").exists())

    def test_force_replaces_only_managed_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)
            profile = target / ".opencode/agents/herdr-ceo.md"
            profile.write_text("outdated\n")
            local_config = target / "opencode.json"
            local_config.write_text('{"private": true}\n')

            self.run_installer(target, "--force")

            self.assertEqual(
                profile.read_bytes(),
                REPOSITORY.joinpath(".opencode/agents/herdr-ceo.md").read_bytes(),
            )
            self.assertEqual(local_config.read_text(), '{"private": true}\n')

    def test_dry_run_does_not_create_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"

            result = self.run_installer(target, "--dry-run")

            self.assertIn("would create", result.stdout)
            self.assertFalse(target.exists())

    def test_target_path_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project with spaces"

            self.run_installer(target)

            self.assertTrue(target.joinpath(".agents/skills/teamcraft/SKILL.md").is_file())

    def test_symbolic_link_destination_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "project"
            outside = base / "outside"
            target.mkdir()
            outside.mkdir()
            target.joinpath(".opencode").symlink_to(outside, target_is_directory=True)

            result = self.run_installer(target, expected_code=1)

            self.assertIn("symbolic-link destination", result.stderr)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse(target.joinpath(".agents").exists())
            self.assertFalse(target.joinpath("opencode.json").exists())

    def test_ignore_rules_override_existing_negations_and_remain_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            target.mkdir()
            original = "/opencode.json\n/.herdr-team/\n!opencode.json\n!.herdr-team/\n"
            ignore = target / ".gitignore"
            ignore.write_text(original)
            self.run_installer(target)
            updated = ignore.read_text()
            self.assertTrue(updated.startswith(original))
            self.assertTrue(updated.endswith("/.herdr-team/\n/opencode.json\n"))
            self.run_installer(target)
            self.assertEqual(ignore.read_text(), updated)

    def test_symlink_gitignore_is_rejected_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "project"
            target.mkdir()
            outside = base / "outside-ignore"
            outside.write_text("unchanged\n")
            target.joinpath(".gitignore").symlink_to(outside)
            self.run_installer(target, expected_code=1)
            self.assertEqual(outside.read_text(), "unchanged\n")
            self.assertFalse(target.joinpath(".agents").exists())


if __name__ == "__main__":
    unittest.main()
