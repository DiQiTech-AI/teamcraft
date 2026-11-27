"""Read and verify the pinned TeamCraft CEO protocol artifact."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys


class ProtocolError(ValueError):
    """Protocol is missing, malformed, or differs from its pinned identity."""


VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
HEADER_RE = re.compile(rb"\A---\r?\nprotocol_version: ([0-9]+\.[0-9]+\.[0-9]+)\r?\n---\r?\n")

#: The only harnesses whose installed CEO Skill may pin the protocol.
#: Codex/OpenCode load the canonical ``.agents`` tree; Claude Code loads the
#: installer-managed byte-identical copy under ``.claude``.  Mapping a harness
#: to a root never changes the artifact contract: both trees are verified by
#: the same frontmatter/manifest/SHA-256 rules below.
SKILL_ROOTS: dict[str, tuple[str, ...]] = {
    "codex": (".agents", "skills", "teamcraft-ceo"),
    "opencode": (".agents", "skills", "teamcraft-ceo"),
    "claude": (".claude", "skills", "teamcraft-ceo"),
}


@dataclass(frozen=True)
class ProtocolSnapshot:
    version: str
    sha256: str
    content: bytes


def verify_protocol(directory: str | Path, *, expected_version: str | None = None,
                    expected_sha256: str | None = None) -> ProtocolSnapshot:
    root = Path(directory)
    protocol_path = root / "CEO_PROTOCOL.md"
    manifest_path = root / "protocol.manifest.json"
    if root.is_symlink() or protocol_path.is_symlink() or manifest_path.is_symlink():
        raise ProtocolError("protocol directory and files must not be symbolic links")
    try:
        content = protocol_path.read_bytes()
        content.decode("utf-8")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProtocolError(f"protocol artifact missing: {exc.filename}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"protocol artifact unreadable or malformed: {exc}") from exc
    match = HEADER_RE.match(content)
    if not match:
        raise ProtocolError("CEO_PROTOCOL.md must start with protocol_version front matter")
    version = match.group(1).decode("ascii")
    if not VERSION_RE.fullmatch(version):
        raise ProtocolError("protocol_version must use MAJOR.MINOR.PATCH")
    if not isinstance(manifest, dict) or set(manifest) != {"protocol_version", "sha256"}:
        raise ProtocolError("protocol manifest must contain only protocol_version and sha256")
    if manifest.get("protocol_version") != version:
        raise ProtocolError("protocol version differs from manifest")
    digest = hashlib.sha256(content).hexdigest()
    if not isinstance(manifest.get("sha256"), str) or manifest["sha256"] != digest:
        raise ProtocolError("protocol SHA-256 differs from manifest")
    if expected_version is not None and version != expected_version:
        raise ProtocolError("pinned protocol version unavailable; reinitialization required")
    if expected_sha256 is not None and digest != expected_sha256:
        raise ProtocolError("pinned protocol bytes unavailable; reinitialization required")
    return ProtocolSnapshot(version, digest, content)


def discover_protocol(project_root: str | Path, harness: str, *,
                      expected_version: str | None = None,
                      expected_sha256: str | None = None) -> ProtocolSnapshot:
    """Discover the installed CEO Skill for one supported harness.

    Codex and OpenCode resolve through the shared ``.agents/skills`` tree;
    Claude Code resolves through its installer-managed ``.claude/skills``
    copy.  Unknown harnesses stay rejected, and every supported harness must
    still satisfy the identical Skill frontmatter and pinned protocol
    version/SHA-256 contract (no relaxed pin).
    """
    try:
        parts = SKILL_ROOTS[harness]
    except (KeyError, TypeError):
        raise ProtocolError(f"unsupported M1-08 harness: {harness}") from None
    root = Path(project_root)
    skill_root = root
    path_chain = []
    for relative_part in parts:
        skill_root = skill_root / relative_part
        path_chain.append(skill_root)
    if any(path.is_symlink() for path in path_chain):
        raise ProtocolError(f"CEO Skill discovery path contains a symbolic link: {harness}")
    if (skill_root / "SKILL.md").is_symlink():
        raise ProtocolError(f"CEO Skill discovery path contains a symbolic link: {harness}")
    try:
        frontmatter = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProtocolError(f"CEO Skill is missing or unreadable for {harness}: {exc}") from exc
    sections = frontmatter.split("---\n", 2)
    if len(sections) < 3 or sections[0] != "" or "name: teamcraft-ceo\n" not in sections[1]:
        raise ProtocolError(f"CEO Skill discovery contract invalid for {harness}")
    return verify_protocol(skill_root / "references", expected_version=expected_version,
                           expected_sha256=expected_sha256)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in {1, 3}:
        print("usage: python -m teamcraft.ceo_protocol DIRECTORY [EXPECTED_VERSION EXPECTED_SHA256]", file=sys.stderr)
        return 2
    try:
        snapshot = verify_protocol(args[0], **({} if len(args) == 1 else {
            "expected_version": args[1], "expected_sha256": args[2]}))
    except ProtocolError as exc:
        print(f"CEO protocol invalid: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"protocol_version": snapshot.version,
                      "sha256": snapshot.sha256}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
