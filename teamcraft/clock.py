"""UTC clock boundary shared by future domain services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from .errors import ValidationError


class Clock(Protocol):
    """Injectable source of timezone-aware UTC timestamps."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""


class SystemClock:
    """Production clock using the standard library system clock."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def require_utc(value: datetime) -> datetime:
    """Validate and normalize an aware datetime to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
