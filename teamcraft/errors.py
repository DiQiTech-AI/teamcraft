"""Shared exception hierarchy for TeamCraft core boundaries."""


class TeamCraftError(Exception):
    """Base class for errors callers may handle without parsing messages."""


class ConfigurationError(TeamCraftError):
    """Configuration is missing or invalid."""


class ValidationError(TeamCraftError):
    """A value violates a core boundary contract."""
