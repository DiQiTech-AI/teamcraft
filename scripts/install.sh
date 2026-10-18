#!/usr/bin/env bash
set -euo pipefail

TEAMCRAFT_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
TEAMCRAFT_SOURCE_ROOT=$(CDPATH= cd -- "$TEAMCRAFT_SCRIPT_DIR/.." && pwd -P)
export TEAMCRAFT_SOURCE_ROOT

exec python3 - "$@" <<'PY'
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


IGNORE_RULES = ("/.herdr-team/", "/opencode.json")


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
        help="replace differing TeamCraft-managed files; never replaces opencode.json",
    )
    return parser.parse_args()


def source_manifest(source_root: Path) -> list[tuple[Path, Path]]:
    roots = [source_root / ".agents" / "skills" / "herdr-team"]
    sources: list[Path] = []
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            raise InstallError(f"missing or unsafe source directory: {root}")
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
                continue
            if path.is_symlink():
                raise InstallError(f"source resources must not contain symlinks: {path}")
            if path.is_file():
                sources.append(path)

    profiles_dir = source_root / ".opencode" / "agents"
    profiles = sorted(profiles_dir.glob("herdr-*.md"))
    if not profiles:
        raise InstallError(f"no OpenCode role profiles found in: {profiles_dir}")
    for path in profiles:
        if path.is_symlink() or not path.is_file():
            raise InstallError(f"invalid OpenCode role profile: {path}")
        sources.append(path)

    template = source_root / "opencode.example.json"
    if template.is_symlink() or not template.is_file():
        raise InstallError(f"missing or unsafe config template: {template}")
    sources.append(template)

    return [(path, path.relative_to(source_root)) for path in sources]


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
            if not args.force:
                raise InstallError(
                    f"managed file differs: {destination}\n"
                    "rerun with --force to replace TeamCraft-managed files"
                )
            operations.append(("update", destination, source, None))
        else:
            operations.append(("create", destination, source, None))

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
