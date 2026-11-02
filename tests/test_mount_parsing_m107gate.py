"""M1-07 gate fix regression: util-linux mount-table parsing (CI run 41461763132).

Root cause pinned here: ``storage._classify_mount`` parsed only the macOS
``dev on POINT (opts)`` layout, while util-linux ``mount`` prints
``dev on POINT type FSTYPE (opts)``; the non-greedy point capture swallowed
``type ext4`` into the mount point, so no Linux line ever covered a temp-dir
target and every write-side open failed closed with "no covering mount entry".

These tests drive ``describe_path_support`` with SYNTHETIC mount lines only
(pure classification, no subprocess, no real state), and additionally require
the real mount table of the CI runner (macOS or ubuntu) to classify the
workspace-local temp root as ``local`` — the exact capability the mandatory
ubuntu/Python 3.10 leg lacked.

Safety properties preserved and asserted: remote fstypes stay refused,
unknown/unparseable forms never resolve to ``local``, and only genuinely
``/dev/``-backed local filesystems are accepted.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from teamcraft import storage


def _support(path: str, lines: list[str]) -> dict[str, str]:
    return storage.describe_path_support(path, mount_lines=lines)


class UtilLinuxMountParsingTests(unittest.TestCase):
    TARGET = "/tmp/teamcraft-m104-x/teamcraft.db"

    def test_utillinux_root_is_local(self) -> None:
        lines = [
            "/dev/sda1 on / type ext4 (rw,relatime)",
            "/dev/sda1 on /home type ext4 (rw,relatime)",
        ]
        result = _support(self.TARGET, lines)
        self.assertEqual(result["status"], "local", result)
        self.assertIn("mount /", result["reason"])

    def test_utillinux_deepest_covering_mount_wins(self) -> None:
        lines = [
            "/dev/sda1 on / type ext4 (rw,relatime)",
            "/dev/sdb1 on /tmp type ext4 (rw,nosuid,nodev)",
        ]
        result = _support(self.TARGET, lines)
        self.assertEqual(result["status"], "local", result)
        self.assertIn("mount /tmp", result["reason"])

    def test_utillinux_nfs_stays_remote(self) -> None:
        lines = [
            "/dev/sda1 on / type ext4 (rw,relatime)",
            "fileserver:/share on /mnt/nas type nfs "
            "(rw,relatime,vers=4.2,addr=10.0.0.5)",
        ]
        result = _support("/mnt/nas/teamcraft.db", lines)
        self.assertEqual(result["status"], "remote", result)

    def test_utillinux_cifs_stays_remote(self) -> None:
        lines = [
            "/dev/sda1 on / type ext4 (rw,relatime)",
            "//nas/share on /mnt/smb type cifs (rw,vers=3.0)",
        ]
        result = _support("/mnt/smb/state/teamcraft.db", lines)
        self.assertEqual(result["status"], "remote", result)

    def test_tmpfs_never_resolves_local(self) -> None:
        lines = [
            "/dev/sda1 on / type ext4 (rw,relatime)",
            "tmpfs on /tmp type tmpfs (rw,nosuid,nodev)",
        ]
        result = _support(self.TARGET, lines)
        self.assertNotEqual(result["status"], "local", result)


class OtherFormPreservationTests(unittest.TestCase):
    TARGET = "/tmp/teamcraft-m104-x/teamcraft.db"

    def test_proc_mounts_form_still_local(self) -> None:
        lines = ["/dev/sda1 / ext4 rw,relatime 0 0"]
        self.assertEqual(_support(self.TARGET, lines)["status"], "local")

    def test_macos_bsd_form_still_local(self) -> None:
        lines = [
            "/dev/disk3s1 on / (apfs, local, journaled, case-sensitive)",
        ]
        self.assertEqual(_support("/tmp/x/teamcraft.db", lines)["status"], "local")

    def test_macos_point_with_space_still_parses(self) -> None:
        lines = [
            "/dev/disk4s2 on /Volumes/my\\040disk (apfs, local, journaled)",
        ]
        result = _support("/Volumes/my disk/db.sqlite", lines)
        self.assertEqual(result["status"], "local", result)

    def test_no_covering_entry_stays_unknown(self) -> None:
        lines = ["/dev/sda1 on /opt type ext4 (rw)"]
        result = _support("/tmp/teamcraft-x", lines)
        self.assertEqual(result["status"], "unknown", result)
        self.assertNotEqual(result["status"], "local")

    def test_columnar_findmnt_style_fails_closed_not_local(self) -> None:
        # Column order is target-first; the parser must not misread the
        # SOURCE column as the mount point and claim locality.  Whatever it
        # concludes, "local" without a proper line shape is forbidden.
        lines = [
            "TARGET SOURCE FSTYPE OPTIONS",
            "/ /dev/sda1 ext4 rw,relatime",
        ]
        result = _support(self.TARGET, lines)
        self.assertNotEqual(result["status"], "local", result)


class RealMountTableSmokeTests(unittest.TestCase):
    def test_real_table_classifies_local_temp_root(self) -> None:
        # End-to-end signal for BOTH CI legs (ubuntu/3.10 and macOS/3.13):
        # the runner temp root sits on a real local disk, so after this fix
        # the REAL mount table must classify it as local (pre-fix, the Linux
        # leg returned unknown for every one of these paths).
        with tempfile.TemporaryDirectory(prefix="teamcraft-m107-gate-") as tmp:
            result = storage.describe_path_support(tmp)
            self.assertEqual(result["status"], "local", result)
            self.assertEqual(result["method"], "mount-table", result)
            self.assertTrue(Path(tmp).is_dir())


if __name__ == "__main__":
    unittest.main()
