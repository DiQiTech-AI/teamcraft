#!/usr/bin/env bash
set -euo pipefail

TEAMCRAFT_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
TEAMCRAFT_SOURCE_ROOT=$(CDPATH= cd -- "$TEAMCRAFT_SCRIPT_DIR/.." && pwd -P)
export TEAMCRAFT_SOURCE_ROOT

exec python3 - "$@" <<'PY'
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


IGNORE_RULES = ("/.teamcraft/", "/opencode.json",
                "/.agents/skills/teamcraft/scripts/core/")

CLAUDE_PROTOCOL_VERSION = "1.0.0"
CLAUDE_MARKER = b"<!-- teamcraft-managed: claude-instructions v1 -->"
CLAUDE_INSTRUCTIONS = (
    CLAUDE_MARKER + b"\n"
    b"# TeamCraft\n\n"
    b"This file is managed by TeamCraft. It points Claude Code at the\n"
    b"installed TeamCraft skills. TeamCraft does not install or probe the\n"
    b"Claude Code binary.\n\n"
    b"- `.claude/skills/teamcraft/SKILL.md`\n"
    b"- `.claude/skills/teamcraft-ceo/SKILL.md`\n\n"
    b"Those skill trees are managed copies of `.agents/skills/` and must not\n"
    b"be edited by hand. Rerun `scripts/install.sh --force` to upgrade\n"
    b"TeamCraft-managed files.\n"
)


class InstallError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/install.sh",
        description="Install TeamCraft into a project without changing global settings.",
    )
    parser.add_argument(
        "--target",
        default=os.getcwd(),
        help="project directory to install into (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the planned changes without writing files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace differing TeamCraft-managed files; never replaces "
             "opencode.json or a user-owned CLAUDE.md",
    )
    return parser.parse_args()


def source_manifest(source_root: Path) -> list[tuple[Path, Path]]:
    roots = [source_root / ".agents" / "skills" / "teamcraft",
             source_root / ".agents" / "skills" / "teamcraft-ceo"]
    sources: list[tuple[Path, Path]] = []
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            raise InstallError(f"missing or unsafe source directory: {root}")
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
                continue
            if path.relative_to(root).parts[:2] == ("scripts", "core"):
                # Runtime-vendored core staging under scripts/core is generated
                # from ./teamcraft below; the package copy is the single
                # canonical core source and must never be dual-sourced here.
                continue
            if path.is_symlink():
                raise InstallError(f"source resources must not contain symlinks: {path}")
            if path.is_file():
                sources.append((path, path.relative_to(source_root)))

    # M1-07: bundle the importable Python Core next to the skill's teamctl so
    # the installed CLI resolves `teamcraft` WITHOUT a source-checkout
    # PYTHONPATH and WITHOUT any network access at runtime (smallest
    # reviewable offline design: a verbatim copy of the pure-stdlib package
    # under scripts/core/teamcraft, exactly where teamctl._bootstrap_core
    # looks for it).
    core_package = source_root / "teamcraft"
    if not core_package.is_dir() or core_package.is_symlink():
        raise InstallError(f"missing or unsafe core package directory: {core_package}")
    core_members = sorted(p for p in core_package.glob("*.py") if p.is_file())
    if "__init__.py" not in {p.name for p in core_members}:
        raise InstallError(f"core package lacks __init__.py: {core_package}")
    for path in core_members:
        if path.is_symlink():
            raise InstallError(f"core package must not contain symlinks: {path}")
        sources.append(
            (path,
             Path(".agents") / "skills" / "teamcraft" / "scripts" / "core"
             / "teamcraft" / path.name)
        )

    # M6-07: Claude Code reads skills from .claude/skills. Install managed
    # byte-identical copies of the canonical .agents skill trees so both
    # harnesses load the same protocol (1.0.0). Copies are used instead of
    # symlinks so the installed project stays portable and cannot escape the
    # target through a link. No Claude binary is installed or probed.
    agents_skill_sources = list(sources)
    for path, relative in agents_skill_sources:
        claude_relative = Path(".claude") / "skills" / Path(*relative.parts[2:])
        sources.append((path, claude_relative))

    profiles_dir = source_root / ".opencode" / "agents"
    profiles = sorted(profiles_dir.glob("herdr-*.md"))
    ceo_entry = profiles_dir / "teamcraft-ceo.md"
    if ceo_entry.is_file() and not ceo_entry.is_symlink():
        profiles.append(ceo_entry)
    if not profiles:
        raise InstallError(f"no OpenCode role profiles found in: {profiles_dir}")
    for path in profiles:
        if path.is_symlink() or not path.is_file():
            raise InstallError(f"invalid OpenCode role profile: {path}")
        sources.append((path, path.relative_to(source_root)))

    template = source_root / "opencode.example.json"
    if template.is_symlink() or not template.is_file():
        raise InstallError(f"missing or unsafe config template: {template}")
    sources.append((template, template.relative_to(source_root)))

    return sources


def verify_claude_protocol(source_root: Path) -> None:
    """Fail closed before any write if the canonical Claude protocol drifts.

    The .claude skill trees are byte-identical copies of .agents/skills, so a
    drifted canonical protocol would otherwise be copied verbatim. This check
    is install-time only: it reads local files and never invokes or probes the
    Claude Code binary.
    """

    references = (
        source_root / ".agents" / "skills" / "teamcraft-ceo" / "references"
    )
    manifest_path = references / "protocol.manifest.json"
    protocol_path = references / "CEO_PROTOCOL.md"
    for path in (manifest_path, protocol_path):
        if path.is_symlink() or not path.is_file():
            raise InstallError(f"missing or unsafe Claude protocol file: {path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError(
            f"invalid Claude protocol manifest: {manifest_path}"
        ) from error
    if manifest.get("protocol_version") != CLAUDE_PROTOCOL_VERSION:
        raise InstallError(
            f"Claude protocol version mismatch in {manifest_path}: "
            f"expected {CLAUDE_PROTOCOL_VERSION}, "
            f"found {manifest.get('protocol_version')!r}"
        )
    expected_sha256 = manifest.get("sha256")
    if not isinstance(expected_sha256, str):
        raise InstallError(
            f"Claude protocol manifest lacks a sha256 string: {manifest_path}"
        )
    actual = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise InstallError(
            f"Claude protocol hash drift in {protocol_path}: "
            f"expected {expected_sha256}, found {actual}"
        )


def validate_target_root(target: Path) -> None:
    if target.is_symlink():
        raise InstallError(f"target directory must not be a symlink: {target}")
    if target.exists() and not target.is_dir():
        raise InstallError(f"target is not a directory: {target}")


def validate_destination_path(target: Path, relative: Path) -> Path:
    current = target
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise InstallError(f"refusing symbolic-link destination: {current}")
        if current.exists() and index < len(parts) - 1 and not current.is_dir():
            raise InstallError(f"destination parent is not a directory: {current}")
    return current


def same_file_contents(source: Path, destination: Path) -> bool:
    if source.stat().st_size != destination.stat().st_size:
        return False
    with source.open("rb") as left, destination.open("rb") as right:
        while True:
            left_chunk = left.read(1024 * 1024)
            right_chunk = right.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copymode(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write(destination: Path, content: bytes, mode: int | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            os.chmod(temporary, stat.S_IMODE(mode))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def gitignore_update(existing: bytes) -> tuple[bytes, list[str]]:
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InstallError(".gitignore must be UTF-8 text") from error

    lines = text.splitlines()
    last_negation = max((i for i, line in enumerate(lines) if line.startswith("!")), default=-1)
    missing = [
        rule for rule in IGNORE_RULES
        if not any(line == rule and i > last_negation for i, line in enumerate(lines))
    ]
    if not missing:
        return existing, []
    if text and not text.endswith(("\n", "\r")):
        text += "\n"
    text += "".join(f"{rule}\n" for rule in missing)
    return text.encode("utf-8"), missing


def install(args: argparse.Namespace) -> int:
    source_root = Path(os.environ["TEAMCRAFT_SOURCE_ROOT"]).resolve()
    target = Path(args.target).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = Path(os.path.normpath(target))

    validate_target_root(target)
    verify_claude_protocol(source_root)
    manifest = source_manifest(source_root)
    operations: list[tuple[str, Path, Path | bytes | None, int | None]] = []
    unchanged = 0

    # Preflight every managed file before making any change.
    for source, relative in manifest:
        destination = validate_destination_path(target, relative)
        if destination.exists():
            if not destination.is_file():
                raise InstallError(f"managed destination is not a regular file: {destination}")
            try:
                identical_path = source.samefile(destination)
            except OSError:
                identical_path = False
            if identical_path or same_file_contents(source, destination):
                unchanged += 1
                continue
            if relative == Path(".opencode/agents/teamcraft-ceo.md"):
                marker = b"<!-- teamcraft-managed: teamcraft-ceo-entry v1 -->"
                try:
                    is_managed = marker in destination.read_bytes().splitlines()[:3]
                except OSError:
                    is_managed = False
                if not is_managed:
                    raise InstallError(
                        f"user-owned CEO agent entry conflicts with TeamCraft: {destination}; "
                        "move it yourself or install into a different project"
                    )
            if not args.force:
                raise InstallError(
                    f"managed file differs: {destination}\n"
                    "rerun with --force to replace TeamCraft-managed files"
                )
            operations.append(("update", destination, source, None))
        else:
            operations.append(("create", destination, source, None))

    # M6-07: a minimal managed CLAUDE.md. Existing byte-identical content is
    # accepted; an unmarked user file is never replaced (even with --force);
    # a marked managed file may only be upgraded explicitly with --force.
    claude_file = validate_destination_path(target, Path("CLAUDE.md"))
    if claude_file.exists():
        if not claude_file.is_file():
            raise InstallError(f"CLAUDE.md is not a regular file: {claude_file}")
        existing_claude = claude_file.read_bytes()
        if existing_claude == CLAUDE_INSTRUCTIONS:
            unchanged += 1
        else:
            is_managed = CLAUDE_MARKER in existing_claude.splitlines()[:3]
            if not is_managed:
                raise InstallError(
                    f"user-owned CLAUDE.md conflicts with TeamCraft: {claude_file}; "
                    "move it yourself or install into a different project"
                )
            if not args.force:
                raise InstallError(
                    f"managed file differs: {claude_file}\n"
                    "rerun with --force to replace TeamCraft-managed files"
                )
            operations.append((
                "update", claude_file, CLAUDE_INSTRUCTIONS,
                stat.S_IMODE(claude_file.stat().st_mode),
            ))
    else:
        operations.append(("create", claude_file, CLAUDE_INSTRUCTIONS, 0o644))

    template = source_root / "opencode.example.json"
    local_config = validate_destination_path(target, Path("opencode.json"))
    if local_config.exists():
        if not local_config.is_file():
            raise InstallError(f"opencode.json is not a regular file: {local_config}")
        print(f"preserve  {local_config} (existing local configuration)")
    else:
        operations.append(("create", local_config, template, None))

    gitignore = validate_destination_path(target, Path(".gitignore"))
    if gitignore.exists() and not gitignore.is_file():
        raise InstallError(f".gitignore is not a regular file: {gitignore}")
    existing_ignore = gitignore.read_bytes() if gitignore.exists() else b""
    updated_ignore, missing_rules = gitignore_update(existing_ignore)
    if missing_rules:
        mode = gitignore.stat().st_mode if gitignore.exists() else 0o644
        action = "update" if gitignore.exists() else "create"
        operations.append((action, gitignore, updated_ignore, mode))

    prefix = "would " if args.dry_run else ""
    for action, destination, payload, mode in operations:
        print(f"{prefix}{action:<7} {destination}")
        if args.dry_run:
            continue
        if isinstance(payload, Path):
            atomic_copy(payload, destination)
        elif isinstance(payload, bytes):
            atomic_write(destination, payload, mode)
        else:
            raise AssertionError("missing operation payload")

    mode_name = "Dry run" if args.dry_run else "Installed"
    print(
        f"{mode_name} TeamCraft in {target}: "
        f"{len(operations)} change(s), {unchanged} managed file(s) already current."
    )
    return 0


def main() -> int:
    try:
        return install(parse_args())
    except InstallError as error:
        print(f"install.sh: error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"install.sh: error: {error}", file=sys.stderr)
        return 1


raise SystemExit(main())
PY
