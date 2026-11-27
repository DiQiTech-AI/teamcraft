from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "scripts" / "install.sh"
CLAUDE_MARKER = "<!-- teamcraft-managed: claude-instructions v1 -->"
PROTOCOL_SHA256 = "c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742"


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

    def assert_skill_trees_match(self, target: Path) -> None:
        """The .claude copies must be byte-identical to the .agents install."""

        def managed_files(root: Path) -> dict[str, bytes]:
            return {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
                and path.name != ".DS_Store"
            }

        agents = managed_files(target / ".agents" / "skills")
        claude = managed_files(target / ".claude" / "skills")
        self.assertEqual(sorted(agents), sorted(claude))
        for relative, content in agents.items():
            self.assertEqual(content, claude[relative], relative)

    def test_first_install_copies_resources_and_creates_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)

            self.assertEqual(
                (target / ".agents/skills/teamcraft/SKILL.md").read_bytes(),
                (REPOSITORY / ".agents/skills/teamcraft/SKILL.md").read_bytes(),
            )
            self.assertTrue(target.joinpath(".opencode/agents/herdr-ceo.md").is_file())
            self.assertTrue(target.joinpath(".opencode/agents/teamcraft-ceo.md").is_file())
            self.assertTrue(target.joinpath(".agents/skills/teamcraft-ceo/SKILL.md").is_file())
            self.assertTrue(target.joinpath(
                ".agents/skills/teamcraft-ceo/references/protocol.manifest.json"
            ).is_file())
            self.assertEqual(
                target.joinpath("opencode.json").read_bytes(),
                REPOSITORY.joinpath("opencode.example.json").read_bytes(),
            )
            ignore_lines = target.joinpath(".gitignore").read_text().splitlines()
            self.assertIn("/.teamcraft/", ignore_lines)
            self.assertIn("/opencode.json", ignore_lines)

    def test_claude_first_install_copies_skills_and_managed_claude_md(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)

            self.assert_skill_trees_match(target)
            self.assertTrue(
                target.joinpath(".claude/skills/teamcraft/SKILL.md").is_file()
            )
            self.assertTrue(
                target.joinpath(
                    ".claude/skills/teamcraft-ceo/references/protocol.manifest.json"
                ).is_file()
            )

            # Bundled core is generated from ./teamcraft; the Claude copy must
            # match the .agents copy and the canonical package.
            claude_core = target / ".claude/skills/teamcraft/scripts/core/teamcraft"
            agents_core = target / ".agents/skills/teamcraft/scripts/core/teamcraft"
            self.assertEqual(
                (claude_core / "ceo_protocol.py").read_bytes(),
                (REPOSITORY / "teamcraft/ceo_protocol.py").read_bytes(),
            )
            self.assertEqual(
                (claude_core / "ceo_protocol.py").read_bytes(),
                (agents_core / "ceo_protocol.py").read_bytes(),
            )

            # Protocol 1.0.0 / c5c358bf…a742 must travel byte-identically.
            protocol = (
                target
                / ".claude/skills/teamcraft-ceo/references/CEO_PROTOCOL.md"
            )
            manifest = json.loads(
                target.joinpath(
                    ".claude/skills/teamcraft-ceo/references/protocol.manifest.json"
                ).read_text()
            )
            self.assertEqual(manifest["protocol_version"], "1.0.0")
            self.assertEqual(manifest["sha256"], PROTOCOL_SHA256)
            self.assertEqual(
                hashlib.sha256(protocol.read_bytes()).hexdigest(), PROTOCOL_SHA256
            )
            self.assertEqual(
                protocol.read_bytes(),
                REPOSITORY.joinpath(
                    ".agents/skills/teamcraft-ceo/references/CEO_PROTOCOL.md"
                ).read_bytes(),
            )

            claude_md = target / "CLAUDE.md"
            text = claude_md.read_text()
            self.assertTrue(text.startswith(CLAUDE_MARKER + "\n"))
            self.assertIn(".claude/skills/teamcraft/SKILL.md", text)
            self.assertIn(".claude/skills/teamcraft-ceo/SKILL.md", text)

    def test_claude_dry_run_reports_paths_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"

            result = self.run_installer(target, "--dry-run")

            self.assertIn("would create", result.stdout)
            self.assertIn(str(target / ".claude/skills/teamcraft/SKILL.md"),
                          result.stdout)
            self.assertIn(str(target / ".claude/skills/teamcraft-ceo/SKILL.md"),
                          result.stdout)
            self.assertIn(str(target / "CLAUDE.md"), result.stdout)
            self.assertFalse(target.exists())

    def test_user_claude_md_conflict_fails_before_any_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            target.mkdir()
            claude_md = target / "CLAUDE.md"
            claude_md.write_text("user claude rules\n")

            result = self.run_installer(target, "--force", expected_code=1)

            self.assertIn("user-owned CLAUDE.md conflicts", result.stderr)
            self.assertEqual(claude_md.read_text(), "user claude rules\n")
            self.assertFalse((target / ".agents").exists())
            self.assertFalse((target / ".claude").exists())
            self.assertFalse((target / "opencode.json").exists())
            self.assertFalse((target / ".gitignore").exists())

    def test_managed_claude_md_upgrade_requires_explicit_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "project"
            reference = base / "reference"
            self.run_installer(target)
            self.run_installer(reference)
            expected = (reference / "CLAUDE.md").read_bytes()

            claude_md = target / "CLAUDE.md"
            claude_md.write_text(f"{CLAUDE_MARKER}\nstale managed content\n")
            result = self.run_installer(target, expected_code=1)
            self.assertIn("managed file differs", result.stderr)
            self.assertEqual(
                claude_md.read_text(),
                f"{CLAUDE_MARKER}\nstale managed content\n",
            )

            self.run_installer(target, "--force")
            self.assertEqual(claude_md.read_bytes(), expected)

    def test_claude_skill_conflict_requires_force_and_restores_canonical_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)
            skill = target / ".claude/skills/teamcraft/SKILL.md"
            other = target / ".claude/skills/teamcraft-ceo/SKILL.md"
            other_before = other.read_bytes()
            skill.write_text("user edit\n")

            result = self.run_installer(target, expected_code=1)

            self.assertIn("managed file differs", result.stderr)
            self.assertEqual(skill.read_text(), "user edit\n")
            self.assertEqual(other.read_bytes(), other_before)

            self.run_installer(target, "--force")
            self.assertEqual(
                skill.read_bytes(),
                target.joinpath(".agents/skills/teamcraft/SKILL.md").read_bytes(),
            )
            self.assertEqual(other.read_bytes(), other_before)

    def test_installer_does_not_install_or_invoke_claude_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "project"
            fake_bin = base / "bin"
            fake_bin.mkdir()
            sentinel = base / "claude-was-called"
            fake_claude = fake_bin / "claude"
            fake_claude.write_text(
                f"#!/bin/sh\ntouch {sentinel}\nexit 0\n", encoding="utf-8"
            )
            fake_claude.chmod(0o755)
            env = dict(os.environ, PATH=f"{fake_bin}:/usr/bin:/bin")

            result = subprocess.run(
                ["bash", str(INSTALLER), "--target", str(target)],
                text=True, capture_output=True, check=False, env=env,
            )

            self.assertEqual(
                result.returncode, 0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertFalse(sentinel.exists(), "installer invoked the claude CLI")
            self.assertFalse(
                [path for path in target.rglob("*")
                 if path.is_file() and path.name == "claude"]
            )
            self.assertTrue((target / "CLAUDE.md").is_file())

    def test_repeated_install_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)
            self.assertTrue(
                target.joinpath(".claude/skills/teamcraft/SKILL.md").is_file()
            )
            self.assertTrue(target.joinpath("CLAUDE.md").is_file())
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

    def test_user_ceo_entry_and_rules_are_preserved_and_conflict_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            custom_entry = target / ".opencode/agents/teamcraft-ceo.md"
            custom_entry.parent.mkdir(parents=True)
            custom_entry.write_text("user agent\n")
            (target / "AGENTS.md").write_text("user rules\n")
            (target / "CLAUDE.md").write_text("claude rules\n")
            result = self.run_installer(target, "--force", expected_code=1)
            self.assertIn("user-owned CEO agent entry conflicts", result.stderr)
            self.assertEqual(custom_entry.read_text(), "user agent\n")
            self.assertEqual((target / "AGENTS.md").read_text(), "user rules\n")
            self.assertEqual((target / "CLAUDE.md").read_text(), "claude rules\n")
            self.assertFalse((target / ".agents/skills").exists())
            self.assertFalse((target / "opencode.json").exists())

    def test_installer_preserves_agents_md_and_rejects_unmanaged_claude_md(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            target.mkdir()
            (target / "AGENTS.md").write_text("custom agents\n")
            (target / "CLAUDE.md").write_text("custom claude\n")

            result = self.run_installer(target, expected_code=1)

            self.assertIn("user-owned CLAUDE.md conflicts", result.stderr)
            self.assertEqual((target / "AGENTS.md").read_text(), "custom agents\n")
            self.assertEqual((target / "CLAUDE.md").read_text(), "custom claude\n")
            self.assertFalse((target / ".agents").exists())
            self.assertFalse((target / ".claude").exists())

    def test_force_updates_only_agent_entry_with_teamcraft_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)
            entry = target / ".opencode/agents/teamcraft-ceo.md"
            entry.write_text("<!-- teamcraft-managed: teamcraft-ceo-entry v1 -->\nold\n")
            self.run_installer(target, "--force")
            self.assertEqual(entry.read_bytes(),
                             REPOSITORY.joinpath(".opencode/agents/teamcraft-ceo.md").read_bytes())

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

    def test_installed_cli_resolves_bundled_core_offline(self) -> None:
        """Gate E: the installed target must import and run WITHOUT the repo.

        Not a file-presence claim: this executes the installed teamctl with
        PYTHONPATH stripped and a clean environment, so success proves the
        bundled ``scripts/core/teamcraft`` copy is what resolves the import
        (exactly where ``teamctl._bootstrap_core`` searches) and no network
        or source checkout is involved.
        """

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            self.run_installer(target)

            core_dir = (target / ".agents" / "skills" / "teamcraft"
                       / "scripts" / "core" / "teamcraft")
            for module in ("__init__.py", "storage.py", "leader.py",
                          "runtime_adapter.py", "commands.py", "domain.py",
                          "ceo_protocol.py", "herdr_adapter.py"):
                self.assertTrue((core_dir / module).is_file(),
                              f"bundled core missing {module}")

            installed_cli = (target / ".agents" / "skills" / "teamcraft"
                            / "scripts" / "teamctl.py")
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path(temporary) / "fake-home"),
            }
            result = subprocess.run(
                [sys.executable, str(installed_cli), "--help"],
                cwd=str(target), env=env, text=True, capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0,
                             f"stdout:\n{result.stdout}\n"
                             f"stderr:\n{result.stderr}")
            self.assertIn("usage", result.stdout.lower())
            # The import must have come from the bundled copy, not the repo.
            probe = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, "
                 "r'%s'); import teamcraft; print(teamcraft.__file__)"
                 % str(core_dir.parent)],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertTrue(probe.stdout.strip().startswith(str(core_dir)))

            protocol_env = dict(env, PYTHONPATH=str(core_dir.parent))
            protocol = subprocess.run(
                [sys.executable, "-m", "teamcraft.ceo_protocol", str(
                    target / ".agents/skills/teamcraft-ceo/references")],
                cwd=str(target), env=protocol_env, text=True, capture_output=True,
                check=False,
            )
            self.assertEqual(protocol.returncode, 0, protocol.stderr)
            self.assertIn('"protocol_version": "1.0.0"', protocol.stdout)

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

    def test_symbolic_link_claude_destination_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "project"
            outside = base / "outside"
            target.mkdir()
            outside.mkdir()
            target.joinpath(".claude").symlink_to(outside, target_is_directory=True)

            result = self.run_installer(target, expected_code=1)

            self.assertIn("symbolic-link destination", result.stderr)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse(target.joinpath(".agents").exists())
            self.assertFalse(target.joinpath("CLAUDE.md").exists())
            self.assertFalse(target.joinpath("opencode.json").exists())

    def test_ignore_rules_override_existing_negations_and_remain_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            target.mkdir()
            original = "/opencode.json\n/.teamcraft/\n!opencode.json\n!.teamcraft/\n"
            ignore = target / ".gitignore"
            ignore.write_text(original)
            self.run_installer(target)
            updated = ignore.read_text()
            self.assertTrue(updated.startswith(original))
            self.assertTrue(updated.endswith(
                "/.teamcraft/\n/opencode.json\n"
                "/.agents/skills/teamcraft/scripts/core/\n"))
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
