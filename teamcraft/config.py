"""Pure configuration-loading boundary for the future TeamCraft core."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from .errors import ConfigurationError


PROJECT_ROOT_ENV = "TEAMCRAFT_PROJECT_ROOT"
RUNTIME_DIRECTORY_NAME = ".teamcraft"


@dataclass(frozen=True)
class TeamCraftConfig:
    """Configuration needed before storage and services are introduced."""

    project_root: Path

    @property
    def runtime_directory(self) -> Path:
        """Return the project-local TeamCraft runtime path."""

        return self.project_root / RUNTIME_DIRECTORY_NAME


def load_config(
    project_root: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> TeamCraftConfig:
    """Load config without creating files or starting background services.

    An explicit project root takes precedence over ``TEAMCRAFT_PROJECT_ROOT``;
    the current directory is the final fallback.
    """

    environment = os.environ if environ is None else environ
    raw_root = (
        project_root
        if project_root is not None
        else environment.get(PROJECT_ROOT_ENV)
    )
    if raw_root is None:
        root = Path.cwd()
    else:
        if not str(raw_root).strip():
            raise ConfigurationError(f"{PROJECT_ROOT_ENV} must not be empty")
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
    return TeamCraftConfig(project_root=root.resolve(strict=False))
