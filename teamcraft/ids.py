"""Opaque identifier boundary without choosing the M1-03 storage format."""

from __future__ import annotations

from typing import NewType, Protocol

from .errors import ValidationError


EntityId = NewType("EntityId", str)


class IdFactory(Protocol):
    """Injectable identifier generator for future domain services."""

    def new(self, namespace: str) -> EntityId:
        """Return a new stable identifier for a domain namespace."""


def require_entity_id(value: str) -> EntityId:
    """Validate the representation shared at package boundaries.

    The UUID/ULID/prefixed-ID choice remains an M1-03 design decision. M1-02
    only fixes that IDs are opaque, non-empty strings and generator-injected.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValidationError("entity ID must be a non-empty string")
    return EntityId(value)
