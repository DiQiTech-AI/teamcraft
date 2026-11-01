"""Identifier boundary for TeamCraft core entities.

M1-02 contract (unchanged, and it governs VALIDATION): entity IDs are
**opaque, non-empty strings** supplied by an injected generator.  No shape,
length, or character whitelist is imposed at the validation boundary — the
package deliberately refuses to turn representation preferences into contract
violations (CEO review M1-04 fix 4: the first cut here added a 64-char cap
and an alphanumeric/``-._`` charset that rejected IDs the established
contract accepted; this module restores compatibility).

M1-04 decision (narrowing the DEFAULT GENERATOR, not the contract): the
production :class:`IdFactory` emits canonical lowercase UUIDv4 strings,
optionally with a short cosmetic prefix (``task_``, ``att_``, ``sess_``).
Rationale:

* ``uuid.uuid4`` is in the standard library, so the choice adds no dependency
  and no coordination — IDs are creation-stable and cannot be re-derived from
  any runtime identity, which is exactly what "a Herdr Session disappearing
  must never change a Task ID" (M0 review 9.1 M1-04, ADR-001 8.2-3) requires;
* UUIDv4 collision probability is negligible at this scale, and the SQLite
  primary keys plus foreign keys make any duplicate loudly fail rather than
  silently alias;
* the factory is injected, so tests and future migrations can substitute
  deterministic or prefixed generators without touching callers; the factory
  is NOT called inside retry loops, keeping IDs stable across a
  rolled-back-and-retried transaction boundary at the call site (the caller
  regenerates only when it genuinely retries creation);
* prefixes and casing are purely cosmetic for humans and logs — IDs remain
  opaque strings and NO parsing of a prefix or shape is ever load-bearing;
  entity type is carried by columns.

:class:`is_uuid4_string` exists for diagnostics (recognising what THIS build's
default generator produces); it is advisory and never gates acceptance.
"""

from __future__ import annotations

import re
import uuid
from typing import NewType, Protocol

from .errors import ValidationError

EntityId = NewType("EntityId", str)

#: Canonical lowercase UUID string form (8-4-4-4-12 hex digits).  Advisory
#: only — see module docstring; acceptance never depends on this shape.
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

#: Characters allowed in namespace LABELS (the ``namespace`` argument and the
#: cosmetic factory prefix).  This is not entity-ID validation: namespaces are
#: construction-time labels, kept tidy so prefixes stay readable in logs.
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class IdFactory(Protocol):
    """Injectable identifier generator for future domain services."""

    def new(self, namespace: str) -> EntityId:
        """Return a new stable identifier for a domain namespace."""


class Uuid4IdFactory:
    """Production :class:`IdFactory`: canonical lowercase UUIDv4 strings.

    ``prefix`` is optional, human-facing only (see module docstring).  The
    default production factory is constructed without a prefix; namespaced
    variants exist for deployments that want type hints in logs.
    """

    def __init__(self, prefix: str | None = None) -> None:
        if prefix is not None:
            prefix = require_namespace(prefix)
        self._prefix = prefix

    @property
    def prefix(self) -> str | None:
        return self._prefix

    def new(self, namespace: str) -> EntityId:
        require_namespace(namespace)
        value = str(uuid.uuid4())
        if self._prefix is not None:
            value = f"{self._prefix}_{value}"
        return require_entity_id(value)


#: Shared default production factory (stateless; UUID4 needs no per-instance
#: state).  Callers may still inject their own instance or factory.
DEFAULT_ID_FACTORY = Uuid4IdFactory()


def require_namespace(namespace: str) -> str:
    """Validate a domain namespace label (e.g. ``task``, ``attempt``)."""

    if not isinstance(namespace, str) or not _NAMESPACE_RE.match(namespace):
        raise ValidationError(
            f"invalid ID namespace {namespace!r}: use letters, digits, '.', '_' or '-'"
        )
    return namespace


def require_entity_id(value: str) -> EntityId:
    """Validate the representation shared at package boundaries.

    The contract is the M1-02 one and nothing more: a non-empty string that
    is not just whitespace, returned unchanged as an :class:`EntityId`.
    Generators choose representation (the default emits UUIDv4 strings);
    validation must not reject IDs merely because they predate or differ
    from the current generator's shape.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValidationError("entity ID must be a non-empty string")
    return EntityId(value)


def is_uuid4_string(value: str) -> bool:
    """True for the canonical lowercase UUIDv4 text form this build produces.

    Advisory diagnostic only; :func:`require_entity_id` intentionally accepts
    more, and no acceptance path may rely on this predicate.
    """

    return isinstance(value, str) and bool(_UUID4_RE.match(value))
