"""M1-01 offline compatibility contract fixtures for scripts/install.sh.

Freezes installer behaviour the original 10 tests in ``test_install.py`` did
not pin: stderr error prefix and exit codes, dry-run summary shape, dry-run
still failing preflight on conflicts, relative ``--target`` resolution,
``--force`` zero-change re-runs, preservation of foreign files, source
manifest filtering (pycache/.DS_Store/.pyc) and symlink rejection inside the
source tree, and file-as-parent destination conflicts.

Offline only: the installer is run against temporary directories and a fake
source root; nothing global is touched and no network access occurs.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "scripts" / "install.sh"
SUMMARY_RE = re.compile(r": \d+ change\(s\), \d+ managed file\(s\) already current\.$")


class InstallerCompatContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_installer(self, target: Path, *arguments: str, cwd: Path | None = None,
                     installer: Path | None = None, expected_code: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", str(installer or INSTALLER), "--target", str(target), *arguments],
            text=True,
            capture_output=True,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
        self.assertEqual(
            result.returncode, expected_code,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def build_fake_source_root(self) -> Path:
        """Minimal TeamCraft-shaped source tree owned by this test."""
        root = self.base / "fakeroot"
        skill = root / ".agents" / "skills" / "herdr-team"
        (skill / "scripts").mkdir(parents=True)
        (skill / "tests").mkdir(parents=True)
        (root / ".opencode" / "agents").mkdir(parents=True)
        (root / "scripts").mkdir(parents=True)
        shutil.copyfile(INSTALLER, root / "scripts" / "install.sh")
        (skill / "SKILL.md").write_text("# fake skill\n", encoding="utf-8")
        (skill / "scripts" / "teamctl.py").write_text("print('fake')\n", encoding="utf-8")
        (skill / "tests" / "test_fake.py").write_text("pass\n", encoding="utf-8")
        (root / ".opencode" / "agents" / "herdr-ceo.md").write_text(
            "---\nmode: primary\n---\nCEO\n", encoding="utf-8",
        )
        (root / "opencode.example.json").write_text('{"fake": true}\n', encoding="utf-8")
        return root

    def test_error_prefix_and_non_directory_target(self):
        target = self.base / "blocker"
        target.write_text("not a directory\n", encoding="utf-8")
        result = self.run_installer(target, expected_code=1)
        self.assertTrue(result.stderr.startswith("install.sh: error: "), result.stderr)
        self.assertIn("target is not a directory", result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "not a directory\n")

    def test_dry_run_summaries_creates_without_touching_disk(self):
        target = self.base / "project"
        result = self.run_installer(target, "--dry-run")
        would_create = [line for line in result.stdout.splitlines()
                       if line.startswith("would create")]
        self.assertGreaterEqual(len(would_create), 3)  # skill + profile + config
        self.assertTrue(SUMMARY_RE.search(result.stdout), result.stdout)
        self.assertIn("Dry run", result.stdout)
        self.assertFalse(target.exists())

    def test_dry_run_still_fails_preflight_on_conflict(self):
        """Dry run must not bypass the all-or-nothing conflict gate."""
        target = self.base / "project"
        conflict = target / ".opencode" / "agents" / "herdr-ceo.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("local edits\n", encoding="utf-8")
        result = self.run_installer(target, "--dry-run", expected_code=1)
        self.assertIn("managed file differs", result.stderr)
        self.assertEqual(conflict.read_text(encoding="utf-8"), "local edits\n")
        self.assertFalse((target / ".agents").exists())
        self.assertFalse((target / "opencode.json").exists())
        self.assertFalse((target / ".gitignore").exists())

    def test_relative_target_resolves_against_cwd(self):
        cwd = self.base / "cwd"
        cwd.mkdir()
        result = subprocess.run(
            ["bash", str(INSTALLER), "--target", "relproj"],
            text=True, capture_output=True, check=False, cwd=str(cwd),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = cwd / "relproj"
        self.assertTrue(installed.joinpath(".agents/skills/herdr-team/SKILL.md").is_file())
        self.assertTrue(installed.joinpath("opencode.json").is_file())

    def test_force_without_conflict_reports_zero_changes(self):
        target = self.base / "project"
        self.run_installer(target)
        result = self.run_installer(target, "--force")
        self.assertIn("0 change(s)", result.stdout)
        self.assertTrue(SUMMARY_RE.search(result.stdout))

    def test_foreign_files_survive_force_reinstall(self):
        target = self.base / "project"
        self.run_installer(target)
        foreign = target / "src" / "app.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("user code\n", encoding="utf-8")
        ignore = target / ".gitignore"
        ignore.write_text(ignore.read_text(encoding="utf-8") + "build/\n",
                         encoding="utf-8")
        self.run_installer(target, "--force")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "user code\n")
        ignore_lines = ignore.read_text(encoding="utf-8").splitlines()
        self.assertIn("build/", ignore_lines)
        self.assertIn("/.herdr-team/", ignore_lines)  # no duplicate rule appended
        self.assertEqual(ignore_lines.count("/.herdr-team/"), 1)

    def test_source_manifest_excludes_pycache_and_junk(self):
        root = self.build_fake_source_root()
        skill = root / ".agents" / "skills" / "herdr-team"
        (skill / "__pycache__").mkdir()
        (skill / "__pycache__" / "teamctl.cpython-313.pyc").write_bytes(b"\x00")
        (skill / ".DS_Store").write_bytes(b"junk")
        (skill / "stale.pyo").write_bytes(b"\x00")
        target = self.base / "project"
        result = self.run_installer(target, installer=root / "scripts" / "install.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = [path.relative_to(target).as_posix()
                    for path in target.rglob("*") if path.is_file()]
        self.assertIn(".agents/skills/herdr-team/SKILL.md", installed)
        self.assertIn(".agents/skills/herdr-team/tests/test_fake.py", installed)
        self.assertIn(".opencode/agents/herdr-ceo.md", installed)
        self.assertIn("opencode.json", installed)
        self.assertFalse([p for p in installed if "__pycache__" in p
                         or p.endswith((".pyc", ".pyo", ".DS_Store"))])

    def test_source_tree_symlink_rejected(self):
        root = self.build_fake_source_root()
        link = root / ".agents" / "skills" / "herdr-team" / "evil.txt"
        link.symlink_to(root / "opencode.example.json")
        target = self.base / "project"
        result = self.run_installer(target, installer=root / "scripts" / "install.sh",
                                   expected_code=1)
        self.assertIn("must not contain symlinks", result.stderr)
        self.assertFalse(target.joinpath(".agents").exists())

    def test_file_as_destination_parent_rejected_before_writes(self):
        target = self.base / "project"
        target.mkdir()
        (target / ".opencode").write_text("not a directory\n", encoding="utf-8")
        result = self.run_installer(target, expected_code=1)
        self.assertIn("destination parent is not a directory", result.stderr)
        self.assertFalse((target / ".agents").exists())
        self.assertFalse((target / "opencode.json").exists())
        self.assertEqual((target / ".opencode").read_text(encoding="utf-8"),
                         "not a directory\n")


if __name__ == "__main__":
    unittest.main()
