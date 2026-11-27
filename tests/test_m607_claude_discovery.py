"""M6-07: Claude CEO protocol discovery through the managed ``.claude`` tree.

The installer writes byte-identical copies of the canonical ``.agents`` Skill
into ``.claude/skills``.  These tests prove that discovery resolves the right
tree per harness while the pinned version/SHA-256 contract, missing-artifact
refusal, drift refusal, and symlink refusal all stay unchanged.  Nothing here
invokes a real Claude binary; ``probe_harness("claude")`` keeps its historical
static ``adapter_pending`` semantics.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
import unittest

from teamcraft import providers
from teamcraft.ceo_protocol import ProtocolError, discover_protocol, verify_protocol


REPO = Path(__file__).resolve().parents[1]
CANONICAL_SKILL = REPO / ".agents/skills/teamcraft-ceo"
ARTIFACTS = CANONICAL_SKILL / "references"
PINNED_VERSION = "1.0.0"
PINNED_SHA256 = "c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742"


def install_skill(root: Path, tree: str) -> Path:
    """Materialise the installer layout (``tree`` is ``.agents`` or ``.claude``)."""

    skill = root / tree / "skills" / "teamcraft-ceo"
    shutil.copytree(CANONICAL_SKILL, skill)
    return skill


def upgrade(refs: Path, version: str = "1.1.0") -> bytes:
    """Rewrite the protocol bytes + manifest to a self-consistent new version."""

    changed = (refs / "CEO_PROTOCOL.md").read_bytes().replace(
        b"protocol_version: 1.0.0",
        f"protocol_version: {version}".encode("ascii"),
        1,
    )
    (refs / "CEO_PROTOCOL.md").write_bytes(changed)
    (refs / "protocol.manifest.json").write_text(
        json.dumps(
            {"protocol_version": version,
             "sha256": hashlib.sha256(changed).hexdigest()}
        ),
        encoding="utf-8",
    )
    return changed


class ClaudeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m607-discovery-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_claude_managed_layout_discovers_the_same_pinned_protocol(self):
        install_skill(self.root, ".claude")
        self.assertFalse((self.root / ".agents").exists())

        snapshot = discover_protocol(self.root, "claude")
        self.assertEqual((snapshot.version, snapshot.sha256),
                         (PINNED_VERSION, PINNED_SHA256))
        self.assertEqual(verify_protocol(ARTIFACTS), snapshot)

    def test_codex_and_opencode_still_resolve_the_agents_tree(self):
        install_skill(self.root, ".agents")
        self.assertFalse((self.root / ".claude").exists())

        canonical = verify_protocol(ARTIFACTS)
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                self.assertEqual(discover_protocol(self.root, harness), canonical)

        # The inverse also holds: a Claude copy is required for Claude.
        with self.assertRaisesRegex(ProtocolError, "missing or unreadable"):
            discover_protocol(self.root, "claude")

    def test_harness_roots_are_independent(self):
        install_skill(self.root, ".agents")
        install_skill(self.root, ".claude")
        upgrade(self.root / ".claude/skills/teamcraft-ceo/references")

        # A drifted Claude copy never changes the Codex/OpenCode answer.
        canonical = verify_protocol(ARTIFACTS)
        self.assertEqual(discover_protocol(self.root, "codex"), canonical)
        self.assertEqual(discover_protocol(self.root, "opencode"), canonical)
        self.assertEqual(discover_protocol(self.root, "claude").version, "1.1.0")

    def test_unknown_harness_is_rejected(self):
        install_skill(self.root, ".claude")
        for harness in ("gemini", "claude-code", ""):
            with self.subTest(harness=harness):
                with self.assertRaisesRegex(ProtocolError, "unsupported"):
                    discover_protocol(self.root, harness)

    def test_missing_or_malformed_claude_skill_is_refused(self):
        with self.assertRaisesRegex(ProtocolError, "missing or unreadable"):
            discover_protocol(self.root, "claude")

        skill = self.root / ".claude/skills/teamcraft-ceo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("not a skill\n", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "discovery contract invalid"):
            discover_protocol(self.root, "claude")

    def test_claude_expected_pin_drift_fails_closed(self):
        install_skill(self.root, ".claude")
        snapshot = discover_protocol(self.root, "claude")

        with self.assertRaisesRegex(ProtocolError, "pinned protocol version"):
            discover_protocol(self.root, "claude", expected_version="1.1.0")
        with self.assertRaisesRegex(ProtocolError, "pinned protocol bytes"):
            discover_protocol(self.root, "claude", expected_sha256="0" * 64)
        self.assertEqual(
            discover_protocol(self.root, "claude",
                              expected_version=snapshot.version,
                              expected_sha256=snapshot.sha256),
            snapshot,
        )

    def test_symlinked_claude_discovery_chain_is_refused(self):
        cases = ("tree", "skills", "skill", "skill_md", "references")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                skill = install_skill(root, ".claude")
                outside = root / "outside"
                outside.mkdir()
                if case == "tree":
                    moved = outside / ".claude"
                    (root / ".claude").rename(moved)
                    (root / ".claude").symlink_to(moved, target_is_directory=True)
                elif case == "skills":
                    moved = outside / "skills"
                    (root / ".claude/skills").rename(moved)
                    (root / ".claude/skills").symlink_to(moved, target_is_directory=True)
                elif case == "skill":
                    moved = outside / "teamcraft-ceo"
                    skill.rename(moved)
                    skill.symlink_to(moved, target_is_directory=True)
                elif case == "skill_md":
                    moved = outside / "SKILL.md"
                    (skill / "SKILL.md").rename(moved)
                    (skill / "SKILL.md").symlink_to(moved)
                else:
                    moved = outside / "references"
                    (skill / "references").rename(moved)
                    (skill / "references").symlink_to(moved, target_is_directory=True)
                with self.assertRaisesRegex(ProtocolError, "symbolic link"):
                    discover_protocol(root, "claude")

    def test_claude_probe_keeps_pending_static_semantics(self):
        install_skill(self.root, ".claude")
        snapshot = providers.probe_harness("claude", self.root)
        self.assertEqual(snapshot.name, "claude")
        self.assertEqual(snapshot.health, "unsupported")
        self.assertEqual(snapshot.reason_code, "adapter_pending")
        self.assertEqual(snapshot.source, "static_policy")
        self.assertEqual(snapshot.capabilities.get("event_stream"), "unknown")
        self.assertEqual(snapshot.capabilities.get("prompt_ack"), "unknown")


if __name__ == "__main__":
    unittest.main()
