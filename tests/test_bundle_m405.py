"""M4-05 generated offline Core bundle and isolated import gates.

``scripts/install.sh`` deliberately treats the top-level ``teamcraft`` package
as the single source of truth and installs a verbatim copy below the runtime
skill's ``scripts/core`` directory.  That directory is generated and ignored
in the source checkout, so this suite exercises a real installation into a
temporary target rather than depending on local generated state.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
CANONICAL = REPOSITORY / "teamcraft"
INSTALLER = REPOSITORY / "scripts" / "install.sh"


class M405OfflineBundleTests(unittest.TestCase):
    def test_installer_bundles_m405_modules_verbatim_and_imports_offline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tc-m405-bundle-") as temporary:
            target = Path(temporary) / "target"
            installed = subprocess.run(
                ["bash", str(INSTALLER), "--target", str(target)],
                cwd=REPOSITORY,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                installed.returncode, 0,
                f"stdout:\n{installed.stdout}\nstderr:\n{installed.stderr}",
            )

            bundled = (target / ".agents" / "skills" / "teamcraft"
                       / "scripts" / "core" / "teamcraft")
            for name in (
                "delivery.py",
                "domain.py",
                "github_adapter.py",
                "runtime_adapter.py",
                "verification.py",
            ):
                self.assertEqual(
                    (bundled / name).read_bytes(),
                    (CANONICAL / name).read_bytes(),
                    f"installer did not copy canonical M4-05 dependency: {name}",
                )

            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path(temporary) / "home"),
                "PYTHONPATH": str(bundled.parent),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import teamcraft, teamcraft.delivery, "
                        "teamcraft.github_adapter; "
                        "from teamcraft import domain; "
                        "assert domain.DIRECT_PR_READY_EVENT == "
                        "'direct_pr_ready'; "
                        "assert domain.DIRECT_PR_MERGE_ACTION == "
                        "'merge_direct_pr'; "
                        "print(teamcraft.__file__)"
                    ),
                ],
                cwd=temporary,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(
            result.returncode, 0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertTrue(result.stdout.strip().startswith(str(bundled)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
