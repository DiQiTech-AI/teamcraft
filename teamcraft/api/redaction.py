"""Log redaction for secrets (M3-01).

Acceptance: *"logs never print the token"*.  Defense in depth:

1. middleware/endpoints never log secret values in the first place;
2. :class:`SecretRedactingFilter` scrubs every registered secret from any
   record that passes through it, so a future accidental ``logger.debug(token)``
   (or a third-party library echoing headers) cannot leak the session token;
3. :func:`install_redaction` attaches the filter to the app logger *and* to
   every root-logger handler, which also covers propagated uvicorn access and
   error logs.
"""

from __future__ import annotations

import logging
import traceback as traceback_module
from collections.abc import Iterable
from typing import Any

REDACTION_MARKER = "[REDACTED]"


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of each secret in ``text``.

    Longest secrets are replaced first so overlapping values collapse into a
    single marker.  Empty/None secrets are ignored.
    """

    if not isinstance(text, str) or not text:
        return text
    result = text
    for secret in sorted(
        (s for s in secrets if isinstance(s, str) and s),
        key=len,
        reverse=True,
    ):
        if secret in result:
            result = result.replace(secret, REDACTION_MARKER)
    return result


def redact_value(value: Any, secrets: Iterable[str]) -> Any:
    """Recursively scrub secrets from a JSON-shaped value.

    Keys and string values anywhere inside dicts/lists are passed through
    :func:`redact_secrets`; other scalars are returned unchanged.  This is
    the response-side counterpart of the log filter: if a secret somehow
    reaches a payload (e.g. embedded in historical free text), it leaves the
    API as ``REDACTION_MARKER`` instead of plaintext.
    """

    material = [s for s in secrets if isinstance(s, str) and s]
    if not material:
        return value
    if isinstance(value, str):
        return redact_secrets(value, material)
    if isinstance(value, dict):
        return {
            redact_secrets(key, material) if isinstance(key, str) else key:
                redact_value(item, material)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, material) for item in value]
    return value


class SecretRedactingFilter(logging.Filter):
    """Logging filter that erases registered secrets from records."""

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self._secrets: set[str] = set()

    def register(self, *secrets: str) -> None:
        self._secrets.update(s for s in secrets if isinstance(s, str) and s)

    @property
    def secrets(self) -> frozenset[str]:
        """Snapshot of currently registered secret values (for redacting data)."""

        return frozenset(self._secrets)

    def clear(self) -> None:
        self._secrets.clear()

    @property
    def registered_count(self) -> int:
        """How many distinct secret values are being scrubbed."""

        return len(self._secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed user args
            return True
        redacted = redact_secrets(message, self._secrets)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        # Exception blocks (tracebacks) are formatted separately from msg and
        # can carry the same secrets, so scrub them too.
        if record.exc_info:
            try:
                text = "".join(
                    traceback_module.format_exception(*record.exc_info)
                )
            except Exception:  # pragma: no cover - defensive
                text = None
            if text is not None:
                scrubbed = redact_secrets(text, self._secrets)
                if scrubbed != text:
                    record.exc_text = scrubbed
                    record.exc_info = None
        if record.exc_text:
            scrubbed = redact_secrets(record.exc_text, self._secrets)
            if scrubbed != record.exc_text:
                record.exc_text = scrubbed
        if record.stack_info:
            scrubbed = redact_secrets(str(record.stack_info), self._secrets)
            if scrubbed != record.stack_info:
                record.stack_info = scrubbed
        return True


def install_redaction(
    logger: logging.Logger, secrets: Iterable[str] = ()
) -> SecretRedactingFilter:
    """Attach a redacting filter to ``logger`` and all root handlers.

    Reuses an existing :class:`SecretRedactingFilter` when one is already
    attached (repeated ``create_app`` calls must not stack duplicate filters),
    and returns the shared filter so callers can register further secrets — e.g.
    after a token rotation.
    """

    def _shared(obj):
        for existing in getattr(obj, "filters", ()):
            if isinstance(existing, SecretRedactingFilter):
                existing.register(*secrets)
                return existing
        return None

    redacting = _shared(logger)
    if redacting is None:
        redacting = SecretRedactingFilter()
        redacting.register(*secrets)
        logger.addFilter(redacting)
    root = logging.getLogger()
    for handler in (root, *list(root.handlers)):
        existing = _shared(handler)
        if existing is None:
            handler.addFilter(redacting)
        else:
            # keep every already-installed filter up to date as well
            existing.register(*secrets)
    return redacting
