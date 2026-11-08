"""Request-correlation helpers (M3-01).

Every response carries ``X-Request-Id``.  A caller may supply its own id for
tracing; we only echo ids that are safely opaque so hostile header content
cannot reach logs or browsers unsanitized.
"""

from __future__ import annotations

import re
import uuid

REQUEST_ID_HEADER = "X-Request-Id"
MAX_REQUEST_ID_LENGTH = 128

# Opaque id: URL/JSON-safe characters only, bounded length.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def new_request_id() -> str:
    """Generate a fresh correlation id."""

    return uuid.uuid4().hex


def sanitize_request_id(value: str | None) -> str | None:
    """Return ``value`` when it is a safe opaque id, else ``None``."""

    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_LENGTH:
        return None
    if _REQUEST_ID_RE.match(candidate) is None:
        return None
    return candidate


def resolve_request_id(inbound: str | None) -> str:
    """Use a valid inbound id or generate a new one."""

    return sanitize_request_id(inbound) or new_request_id()
