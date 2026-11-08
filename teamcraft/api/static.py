"""Static resource path boundary (M3-01).

PLAN §9.2-4: file access is confined to controlled paths; path traversal and
symlink escape are rejected.  ``resolve_static_path`` is deliberately stricter
than "the OS happened to resolve it for us":

* rejects NUL bytes, absolute paths, drive letters, backslashes and any ``..``
  segment in either raw or percent-decoded form (defense against double
  encoding);
* hidden/dotfile segments are refused (``.git``, ``.env`` and — importantly —
  the ``.teamcraft`` token directory are never servable);
* the fully ``resolve()``d target (symlinks followed) must remain inside the
  resolved root, so a symlink pointing outside is rejected;
* only regular files are served; directories are 404, not browsable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .errors import NOT_FOUND, PATH_FORBIDDEN, ApiError

_NUL_RE = re.compile(r"%00|\x00", re.IGNORECASE)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class StaticPathError(ApiError):
    """A requested static path violated the confinement boundary."""

    def __init__(self, message: str, *, details: object = None) -> None:
        super().__init__(403, PATH_FORBIDDEN, message, details=details)


def _segments(value: str) -> list[str]:
    return [part for part in value.replace("\\", "/").split("/") if part not in ("", ".")]


def resolve_static_path(root: str | os.PathLike[str], requested: str) -> Path:
    """Resolve ``requested`` inside ``root`` or raise :class:`StaticPathError`.

    The returned path is fully resolved (symlinks followed) and guaranteed to
    be a regular file inside the resolved root.  A path that is well-formed
    but missing raises ``ApiError`` 404 so clients get the correct semantics.
    """

    if requested is None:
        raise StaticPathError("static path must not be null")
    raw = str(requested)
    if _NUL_RE.search(raw):
        raise StaticPathError("static path contains a NUL byte")

    decoded = unquote(raw)
    if _NUL_RE.search(decoded):
        raise StaticPathError("static path contains a NUL byte")

    for value in (raw, decoded):
        if value.startswith(("/", "\\")) or _DRIVE_RE.match(value):
            raise StaticPathError("static path must be relative to the static root")
        parts = _segments(value)
        if not parts:
            raise StaticPathError("static path must not be empty")
        if any(part == ".." for part in parts):
            raise StaticPathError("static path must not traverse parent directories")
        if any(part.startswith(".") for part in parts):
            raise StaticPathError("static path may not reference hidden files")

    root_path = Path(root)
    if not root_path.is_dir():
        raise ApiError(404, NOT_FOUND, "No static resources are configured.")

    resolved_root = root_path.resolve()
    candidate = resolved_root.joinpath(*_segments(decoded))
    try:
        resolved_target = candidate.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - platform-dependent
        raise StaticPathError("static path could not be resolved") from exc

    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        # Symlink or reparse escape: the resolved location left the root.
        raise StaticPathError("static path escapes the static root")

    if not resolved_target.exists():
        raise ApiError(404, NOT_FOUND, "The requested static resource does not exist.")
    if not resolved_target.is_file():
        raise ApiError(404, NOT_FOUND, "Static directories are not browsable.")
    return resolved_target


def register_static_routes(
    app: FastAPI, root: str | os.PathLike[str] | None
) -> None:
    """Mount ``GET /static/{asset_path}`` confined to ``root`` (if provided)."""

    if root is None:
        return
    static_root = Path(root)

    @app.get("/static/{asset_path:path}")
    async def static_asset(asset_path: str) -> FileResponse:
        target = resolve_static_path(static_root, asset_path)
        return FileResponse(target)
