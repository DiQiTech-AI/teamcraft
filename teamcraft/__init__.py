"""Stable, side-effect-free boundaries for the TeamCraft core.

The existing project-local ``teamctl.py`` remains the operational CLI during
M1. This package intentionally contains no scheduler, storage, or service
startup code yet.
"""

from .clock import Clock, SystemClock, require_utc
from .config import TeamCraftConfig, load_config
from .errors import ConfigurationError, TeamCraftError, ValidationError
from .ids import EntityId, IdFactory, require_entity_id

__all__ = [
    "Clock",
    "ConfigurationError",
    "EntityId",
    "IdFactory",
    "SystemClock",
    "TeamCraftConfig",
    "TeamCraftError",
    "ValidationError",
    "load_config",
    "require_entity_id",
    "require_utc",
]
