"""M5-04 usage data and price provenance (M0 review 9.5 M5-04; PLAN §7.2/FR-018).

Durable usage accounting for the three actor roles — CEO, Worker, Verifier —
as an append-only projection over the existing event store (the same
no-new-table pattern M5-01 used for capability snapshots).  Every recorded
fact is bound to its source and precision so later views (M5-05) and
experiments (M5-06) can distinguish three states the roadmap refuses to
conflate:

* **exact** — a quantity the harness/report actually reported;
* **estimated** — a derived figure, always labelled as such, never silently
  promoted to exact;
* **unavailable** — a quantity nobody reported; it stays NULL in storage and
  in every total.  Unknown is never zero, never free, never "good enough"
  (FR-018, the same discipline :mod:`teamcraft.escalation` applies to
  budgets).

Price provenance: a usage record carries ``currency`` and ``price_version``
only when the source supplied them, and a versioned :class:`PriceSnapshot`
can be recorded and referenced.  This module never invents a price: cost
totals aggregate only costs that were reported, grouped by currency and
price version so every number stays traceable; the single optional
derivation (:func:`estimate_cost_microunits`) requires a registered price
version, refuses whenever any input is missing, and its result is forever
labelled ``estimated``.

Public contract pinned by ``tests/test_usage_m504.py``:

* roles are exactly ``ceo`` / ``worker`` / ``verifier`` (filterable);
* locality is ``local`` / ``cloud`` / NULL (filterable, unknown stays NULL);
* token quantities (input/output/cache), response/Wake counts, elapsed time,
  cost, currency and price version are nullable and NULL-preserving;
* a record with no known token quantity must declare precision
  ``unavailable``; a known quantity must declare ``exact`` or ``estimated``;
* a reported cost keeps its currency when the source supplied one; without
  it the cost is still stored but buckets under
  :data:`UNATTRIBUTED_CURRENCY` and counts as a traceability gap — never
  attributed to an assumed currency and never dropped;
* writes are idempotent on the record digest and collide loudly when the
  same identity names different facts;
* totals report ``known_sum`` plus the count of NULL contributors, and a
  strict total is NULL unless every contributor was known — a sum over
  partial data is never presented as complete;
* mixing currencies in one strict cost total yields NULL, never a silent
  conversion; costs lacking a price version are counted as a traceability
  gap rather than dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from . import escalation, events, storage
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, StorageError, ValidationError
from .ids import require_entity_id

#: One durable usage record per append.
USAGE_EVENT = "usage.recorded"
#: One registered versioned price snapshot per append.
PRICE_SNAPSHOT_EVENT = "usage.price_snapshot"

#: The three audited roles (roadmap: record CEO, Worker, Verifier separately).
USAGE_ROLES = frozenset({"ceo", "worker", "verifier"})
#: exact = reported by the source; estimated = derived (labelled forever);
#: unavailable = nothing was reported (all token quantities NULL).
PRECISION_STATES = frozenset({"exact", "estimated", "unavailable"})
#: Locality mirrors escalation.LOCALITIES but is nullable (unknown).
USAGE_LOCALITIES = frozenset({"local", "cloud"})
#: Rate keys of a price snapshot: microunits per million tokens, nullable.
PRICE_RATE_KEYS = frozenset({"input", "output", "cache"})

#: Token quantities covered by the precision label.  ``total_tokens`` exists
#: because the WorkerReport wire schema carries it (input/output are absent
#: there); it is always cross-checked against the known parts.
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cache_tokens")
#: Every nullable quantitative dimension carried by a usage record.
USAGE_DIMENSIONS = (
    "input_tokens", "output_tokens", "total_tokens", "cache_tokens",
    "response_count", "wake_count", "elapsed_ms", "cost_microunits",
)
#: Bucket key for reported costs that arrived WITHOUT a currency.  They are
#: kept (dropping money would be its own falsification) but can never merge
#: into a real-currency total.
UNATTRIBUTED_CURRENCY = "UNATTRIBUTED"

#: Wake states that represent a delivered-or-possibly-delivered CEO
#: attention event (mirrors wake.py's budget-consuming states; duplicated
#: small and local so this module never imports the wake machinery).
_CONSUMING_WAKE_STATES = ("sending", "submitted", "acknowledged", "processed",
                         "unknown")

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,199}\Z")
_SAFE_PRICE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_SECRET_TEXT = re.compile(
    r"\bBearer\s+\S+|\bbearer[-_][A-Za-z0-9_-]+|\bsk-[A-Za-z0-9_-]{8,}",
    re.I)
# Whole-word credential markers only: usage payloads legitimately carry
# ``input_tokens``-style keys, which are quantities, not credentials.
_SECRET_KEY = re.compile(
    r"\b(?:api[_-]?key|apikey|authorization|bearer|password|secret|token)\b",
    re.I)

#: Sources that bill directly and therefore attest their own cost figures.
_DIRECT_BILLING_SOURCES = frozenset({"provider_billing", "usage_api"})


def _now(clock: Clock | None) -> datetime:
    source = clock if clock is not None else SystemClock()
    return source.now().astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _nullable_int(value: Any, name: str) -> int | None:
    """Null-aware non-negative integer: NULL stays NULL, never becomes 0."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer or null")
    return value


def _safe_name(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded safe identifier")
    return value


def _reject_secrets(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ValidationError(f"{path} contains a forbidden credential-shaped field")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        raise ValidationError(f"{path} contains credential-shaped text")


# ---------------------------------------------------------------------------
# Usage records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageRecord:
    """One actor-role's usage facts for one unit of work, NULL-preserving.

    ``role`` separates CEO from Worker and Verifier (roadmap 9.5 M5-04).
    Every quantity is nullable; NULL means *nobody reported it* and is
    distinct from an explicitly reported zero.  ``precision`` labels the
    token quantities as reported (``exact``), derived (``estimated``), or
    missing (``unavailable``); it is validated against the quantities so a
    record cannot claim exactness for numbers it does not have.
    """

    role: str
    source: str
    precision: str
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    locality: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_tokens: int | None = None
    response_count: int | None = None
    wake_count: int | None = None
    elapsed_ms: int | None = None
    cost_microunits: int | None = None
    currency: str | None = None
    price_version: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if self.role not in USAGE_ROLES:
            raise ValidationError("usage role must be ceo, worker, or verifier")
        _safe_name(self.source, "source")
        if self.precision not in PRECISION_STATES:
            raise ValidationError(
                "usage precision must be exact, estimated, or unavailable")
        _safe_name(self.harness, "harness", optional=True)
        _safe_name(self.provider, "provider", optional=True)
        _safe_name(self.model, "model", optional=True)
        if self.locality is not None and self.locality not in USAGE_LOCALITIES:
            raise ValidationError("usage locality must be local, cloud, or null")
        for name in USAGE_DIMENSIONS:
            _nullable_int(getattr(self, name), f"usage.{name}")
        # A reported cost without a currency is stored, never silently
        # attributed to (or dropped for) some assumed currency; totals expose
        # it under UNATTRIBUTED_CURRENCY as a traceability gap.
        if self.currency is not None and not _CURRENCY.fullmatch(self.currency):
            raise ValidationError("currency must be an ISO-4217 uppercase code or null")
        if self.price_version is not None \
                and not _SAFE_PRICE_VERSION.fullmatch(self.price_version):
            raise ValidationError("price_version must be a bounded safe identifier or null")
        # total_tokens is audit evidence, never a substitute for missing
        # directional parts — and a total below its known parts is a loud
        # contradiction, mirroring escalation.TokenUsage.
        if self.total_tokens is not None \
                and (self.input_tokens or 0) + (self.output_tokens or 0) \
                > self.total_tokens:
            raise ValidationError("usage.total_tokens contradicts known parts")
        known = any(getattr(self, name) is not None for name in _TOKEN_FIELDS)
        if self.precision == "unavailable" and known:
            raise ValidationError(
                "precision unavailable contradicts known token quantities")
        if self.precision != "unavailable" and not known:
            raise ValidationError(
                f"precision {self.precision} requires at least one known"
                " token quantity; report unavailable instead")
        # Exact cost from a non-billing source without a price version is
        # still storable (the source attests it), and totals surface it as an
        # unpriced-provenance gap rather than rejecting the count facts.
        if not self.recorded_at:
            raise ValidationError("recorded_at is required")
        _parse_time(self.recorded_at, "recorded_at")
        if self.task_id is not None:
            require_entity_id(self.task_id)
        if self.attempt_id is not None:
            require_entity_id(self.attempt_id)
        _reject_secrets(self.payload())

    @property
    def source_attests_cost(self) -> bool:
        """Whether the source bills directly and so attests its own figures."""

        return self.source in _DIRECT_BILLING_SOURCES

    @property
    def cost_unattributed(self) -> bool:
        """True when a cost was reported but no currency came with it."""

        return self.cost_microunits is not None and self.currency is None

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "role": self.role,
            "source": self.source,
            "precision": self.precision,
            "harness": self.harness,
            "provider": self.provider,
            "model": self.model,
            "locality": self.locality,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_tokens": self.cache_tokens,
            "response_count": self.response_count,
            "wake_count": self.wake_count,
            "elapsed_ms": self.elapsed_ms,
            "cost_microunits": self.cost_microunits,
            "currency": self.currency,
            "price_version": self.price_version,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "recorded_at": self.recorded_at,
        }

    @property
    def record_digest(self) -> str:
        return _digest(self.payload())

    @property
    def usage_id(self) -> str:
        """Stable identity: the same facts replay to the same id."""

        return "usage-" + self.record_digest[:40]

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "UsageRecord":
        try:
            record = cls(
                role=str(value["role"]),
                source=str(value["source"]),
                precision=str(value["precision"]),
                harness=None if value.get("harness") is None else str(value["harness"]),
                provider=None if value.get("provider") is None else str(value["provider"]),
                model=None if value.get("model") is None else str(value["model"]),
                locality=None if value.get("locality") is None else str(value["locality"]),
                input_tokens=value.get("input_tokens"),
                output_tokens=value.get("output_tokens"),
                total_tokens=value.get("total_tokens"),
                cache_tokens=value.get("cache_tokens"),
                response_count=value.get("response_count"),
                wake_count=value.get("wake_count"),
                elapsed_ms=value.get("elapsed_ms"),
                cost_microunits=value.get("cost_microunits"),
                currency=None if value.get("currency") is None else str(value["currency"]),
                price_version=None if value.get("price_version") is None else str(value["price_version"]),
                task_id=None if value.get("task_id") is None else str(value["task_id"]),
                attempt_id=None if value.get("attempt_id") is None else str(value["attempt_id"]),
                recorded_at=str(value["recorded_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed usage record payload") from exc
        return record


# ---------------------------------------------------------------------------
# Price snapshots (provenance for any price later attached to usage)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceSnapshot:
    """A versioned, source-attributed price list for one model.

    Rates are microunits per million tokens and individually nullable: a
    rate nobody published stays NULL and blocks the derivations that need
    it, rather than defaulting to zero.
    """

    model: str
    currency: str
    price_version: str
    rates: Mapping[str, int | None] = field(default_factory=dict)
    source: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        _safe_name(self.model, "model")
        if not _CURRENCY.fullmatch(self.currency or ""):
            raise ValidationError("currency must be an ISO-4217 uppercase code")
        if not _SAFE_PRICE_VERSION.fullmatch(self.price_version or ""):
            raise ValidationError("price_version must be a bounded safe identifier")
        _safe_name(self.source, "source")
        if set(self.rates) != set(PRICE_RATE_KEYS):
            raise ValidationError("rates must cover exactly input, output, cache")
        for key in sorted(PRICE_RATE_KEYS):
            _nullable_int(self.rates[key], f"rates.{key}")
        if not self.observed_at:
            raise ValidationError("observed_at is required")
        _parse_time(self.observed_at, "observed_at")
        _reject_secrets(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "model": self.model,
            "currency": self.currency,
            "price_version": self.price_version,
            "rates": {key: self.rates[key] for key in sorted(PRICE_RATE_KEYS)},
            "source": self.source,
            "observed_at": self.observed_at,
        }

    @property
    def snapshot_digest(self) -> str:
        return _digest(self.payload())

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "PriceSnapshot":
        try:
            return cls(
                model=str(value["model"]),
                currency=str(value["currency"]),
                price_version=str(value["price_version"]),
                rates={key: value["rates"].get(key) for key in PRICE_RATE_KEYS},
                source=str(value["source"]),
                observed_at=str(value["observed_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed price snapshot payload") from exc


# ---------------------------------------------------------------------------
# Totals — auditable, null-aware, no invented money
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DimensionTotal:
    """One quantity summed over a record set WITHOUT substituting NULLs.

    ``known_sum`` adds only reported values (None when nothing was known —
    NOT zero); ``unknown_count`` exposes how many contributors were NULL.
    ``strict_total`` is the auditable answer: None whenever any contributor
    was unknown, so a partial sum is never presented as a complete one.
    """

    known_sum: int | None
    known_count: int
    unknown_count: int

    @property
    def complete(self) -> bool:
        """True only when at least one record contributed and none was NULL."""

        return self.unknown_count == 0 and self.known_count > 0

    @property
    def available(self) -> bool:
        """False when the dimension was NULL in every record (unavailable)."""

        return self.known_count > 0

    @property
    def strict_total(self) -> int | None:
        return self.known_sum if self.complete else None

    def payload(self) -> dict[str, Any]:
        return {
            "known_sum": self.known_sum,
            "known_count": self.known_count,
            "unknown_count": self.unknown_count,
            "available": self.available,
            "complete": self.complete,
            "strict_total": self.strict_total,
        }


@dataclass(frozen=True)
class UsageTotals:
    """Aggregate over a filtered record set, keeping every gap visible."""

    record_count: int
    exact_count: int
    estimated_count: int
    unavailable_count: int
    dimensions: Mapping[str, DimensionTotal]
    #: Reported costs grouped by currency; costs without a currency collect
    #: under :data:`UNATTRIBUTED_CURRENCY` and never merge into a real-
    #: currency total.  ``price_versions`` lists exactly which registered
    #: versions contributed to attributed costs.
    cost_by_currency: Mapping[str, DimensionTotal]
    price_versions: tuple[str, ...]
    #: Reported costs that arrived without a currency (attribution gap).
    unattributed_cost_count: int
    #: Reported costs that arrived without a price version (traceability gap).
    missing_price_version_cost_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", dict(self.dimensions))
        object.__setattr__(self, "cost_by_currency", dict(self.cost_by_currency))

    @property
    def mixed_currencies(self) -> bool:
        """More than one real currency contributed (unattributed excluded —
        it is a gap, never a currency)."""

        return len([key for key in self.cost_by_currency
                    if key != UNATTRIBUTED_CURRENCY]) > 1

    @property
    def has_traceability_gap(self) -> bool:
        return (self.unattributed_cost_count > 0
                or self.missing_price_version_cost_count > 0)

    def dimension(self, name: str) -> DimensionTotal:
        if name not in USAGE_DIMENSIONS:
            raise ValidationError(f"unknown usage dimension: {name}")
        return self.dimensions[name]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "record_count": self.record_count,
            "exact_count": self.exact_count,
            "estimated_count": self.estimated_count,
            "unavailable_count": self.unavailable_count,
            "dimensions": {name: total.payload()
                          for name, total in self.dimensions.items()},
            "cost_by_currency": {currency: total.payload()
                                for currency, total in self.cost_by_currency.items()},
            "price_versions": list(self.price_versions),
            "unattributed_cost_count": self.unattributed_cost_count,
            "missing_price_version_cost_count": self.missing_price_version_cost_count,
            "mixed_currencies": self.mixed_currencies,
            "has_traceability_gap": self.has_traceability_gap,
        }

    @classmethod
    def compute(cls, records: tuple[UsageRecord, ...] | list[UsageRecord]) -> "UsageTotals":
        rows = list(records)
        dimensions: dict[str, DimensionTotal] = {}
        for name in USAGE_DIMENSIONS:
            known = [getattr(record, name) for record in rows
                     if getattr(record, name) is not None]
            dimensions[name] = DimensionTotal(
                known_sum=sum(known) if known else None,
                known_count=len(known),
                unknown_count=len(rows) - len(known),
            )
        cost_by_currency: dict[str, list[int]] = {}
        versions: set[str] = set()
        unattributed = 0
        missing_version = 0
        for record in rows:
            if record.cost_microunits is None:
                continue
            key = record.currency or UNATTRIBUTED_CURRENCY
            cost_by_currency.setdefault(key, []).append(record.cost_microunits)
            if record.currency is None:
                unattributed += 1
            if record.price_version is None:
                missing_version += 1
            else:
                versions.add(record.price_version)
        costs: dict[str, DimensionTotal] = {}
        for currency, amounts in cost_by_currency.items():
            costs[currency] = DimensionTotal(
                known_sum=sum(amounts), known_count=len(amounts),
                unknown_count=0)
        return cls(
            record_count=len(rows),
            exact_count=sum(1 for r in rows if r.precision == "exact"),
            estimated_count=sum(1 for r in rows if r.precision == "estimated"),
            unavailable_count=sum(1 for r in rows if r.precision == "unavailable"),
            dimensions=dimensions,
            cost_by_currency=costs,
            price_versions=tuple(sorted(versions)),
            unattributed_cost_count=unattributed,
            missing_price_version_cost_count=missing_version,
        )


# ---------------------------------------------------------------------------
# Persistence (append-only projection over the event store)
# ---------------------------------------------------------------------------

def _require_project(conn: storage.TeamCraftConnection, project_id: str) -> None:
    if conn.execute("SELECT 1 FROM projects WHERE id = ?",
                   (require_entity_id(project_id),)).fetchone() is None:
        raise NotFoundError(f"project not found: {project_id}")


def _require_bindings(conn: storage.TeamCraftConnection, project_id: str,
                      record: UsageRecord) -> None:
    """Task/Attempt references on a record must exist and be nested correctly."""

    if record.task_id is not None:
        row = conn.execute("SELECT project_id FROM tasks WHERE id = ?",
                          (record.task_id,)).fetchone()
        if row is None or str(row[0]) != project_id:
            raise NotFoundError(f"task not found in project: {record.task_id}")
    if record.attempt_id is not None:
        row = conn.execute(
            "SELECT t.project_id FROM attempts a JOIN tasks t ON t.id = a.task_id"
            " WHERE a.id = ?", (record.attempt_id,)).fetchone()
        if row is None or str(row[0]) != project_id:
            raise NotFoundError(f"attempt not found in project: {record.attempt_id}")
        if record.task_id is not None and record.attempt_id is not None:
            owner = conn.execute("SELECT task_id FROM attempts WHERE id = ?",
                                (record.attempt_id,)).fetchone()
            if str(owner[0]) != record.task_id:
                raise ValidationError("attempt_id does not belong to task_id")


def record_usage(conn: storage.TeamCraftConnection, project_id: str,
                 record: UsageRecord) -> events.Event:
    """Durably append one usage record; identical facts replay idempotently.

    The event id is derived from the record digest, so the same facts can be
    submitted repeatedly with no duplicates, while a *different* record under
    the same identity (same task/attempt/role locality reinterpretation) is a
    new digest and a new event — and any manual id collision fails closed.
    """

    _require_project(conn, project_id)
    _require_bindings(conn, project_id, record)
    payload = {"schema_version": 1, "usage_id": record.usage_id,
               "record": record.payload(), "record_digest": record.record_digest}
    _reject_secrets(payload)
    try:
        event, _inserted = events.append_event_idempotent(
            conn, USAGE_EVENT, project_id=project_id,
            task_id=record.task_id, attempt_id=record.attempt_id,
            payload_json=_canonical(payload),
            event_id="evt-usage-" + record.record_digest[:40])
    except StorageError as exc:
        raise ConflictError(
            f"usage identity {record.usage_id} already names different facts"
        ) from exc
    return event


def record_price_snapshot(conn: storage.TeamCraftConnection, project_id: str,
                          snapshot: PriceSnapshot) -> events.Event:
    """Register a versioned price list; replays of the same list are no-ops."""

    _require_project(conn, project_id)
    payload = {"schema_version": 1, "snapshot": snapshot.payload(),
               "snapshot_digest": snapshot.snapshot_digest}
    _reject_secrets(payload)
    try:
        event, _inserted = events.append_event_idempotent(
            conn, PRICE_SNAPSHOT_EVENT, project_id=project_id,
            payload_json=_canonical(payload),
            event_id="evt-price-" + snapshot.snapshot_digest[:40])
    except StorageError as exc:
        raise ConflictError("price snapshot identity collision") from exc
    return event


def _decode_usage_row(payload_json: str) -> UsageRecord:
    try:
        payload = json.loads(str(payload_json))
        record = UsageRecord.from_payload(payload["record"])
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
        raise ConflictError("stored usage record is malformed") from exc
    if payload.get("record_digest") != record.record_digest:
        raise ConflictError("stored usage record digest mismatch")
    return record


def list_usage(conn: storage.TeamCraftConnection, project_id: str, *,
               role: str | None = None, locality: str | None = None,
               precision: str | None = None,
               task_id: str | None = None) -> tuple[UsageRecord, ...]:
    """All durable usage records for a project, oldest first, filterable.

    Filters implement the roadmap's screening requirements (CEO vs Worker vs
    Verifier, local vs cloud, exact vs estimated).  Passing ``role``/
    ``locality``/``precision`` narrows the set; ``locality=None`` means "all
    localities, including records whose locality is unknown" — unknown is a
    value, never an exclusion or an assumption.
    """

    _require_project(conn, project_id)
    if role is not None and role not in USAGE_ROLES:
        raise ValidationError("role filter must be ceo, worker, or verifier")
    if locality is not None and locality not in USAGE_LOCALITIES:
        raise ValidationError("locality filter must be local or cloud")
    if precision is not None and precision not in PRECISION_STATES:
        raise ValidationError("precision filter must be exact, estimated, or unavailable")
    if task_id is not None:
        task_id = require_entity_id(task_id)
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE project_id = ? AND kind = ?"
        " ORDER BY seq ASC", (project_id, USAGE_EVENT)).fetchall()
    records: list[UsageRecord] = []
    for row in rows:
        record = _decode_usage_row(str(row[0]))
        if role is not None and record.role != role:
            continue
        if locality is not None and record.locality != locality:
            continue
        if precision is not None and record.precision != precision:
            continue
        if task_id is not None and record.task_id != task_id:
            continue
        records.append(record)
    return tuple(records)


def get_usage(conn: storage.TeamCraftConnection, project_id: str,
              usage_id: str) -> UsageRecord:
    """Fetch one durable usage record by its digest-derived id."""

    require_entity_id(usage_id)
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE project_id = ? AND kind = ?"
        " ORDER BY seq ASC", (project_id, USAGE_EVENT)).fetchall()
    for row in rows:
        record = _decode_usage_row(str(row[0]))
        if record.usage_id == usage_id:
            return record
    raise NotFoundError(f"usage record not found: {usage_id}")


def usage_totals(conn: storage.TeamCraftConnection, project_id: str, *,
                 role: str | None = None, locality: str | None = None,
                 task_id: str | None = None) -> UsageTotals:
    """Auditable totals over the durable records matching the filters."""

    return UsageTotals.compute(list_usage(
        conn, project_id, role=role, locality=locality, task_id=task_id))


def latest_price_snapshots(conn: storage.TeamCraftConnection,
                          project_id: str) -> tuple[PriceSnapshot, ...]:
    """Newest registered snapshot per (model, currency, price version)."""

    _require_project(conn, project_id)
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE project_id = ? AND kind = ?"
        " ORDER BY seq DESC", (project_id, PRICE_SNAPSHOT_EVENT)).fetchall()
    latest: dict[tuple[str, str, str], PriceSnapshot] = {}
    for row in rows:
        try:
            snapshot = PriceSnapshot.from_payload(
                json.loads(str(row[0]))["snapshot"])
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise ConflictError("stored price snapshot is malformed") from exc
        latest.setdefault(
            (snapshot.model, snapshot.currency, snapshot.price_version), snapshot)
    return tuple(latest[key] for key in sorted(latest))


def latest_price_snapshot(conn: storage.TeamCraftConnection, project_id: str,
                          model: str, *, currency: str | None = None
                          ) -> PriceSnapshot | None:
    """Newest observed snapshot for ``model`` (optionally pinned to one
    currency), or None when no price was ever registered.  None means
    *unavailable* — callers must not substitute a price of their own."""

    candidates = [snapshot for snapshot in latest_price_snapshots(conn, project_id)
                  if snapshot.model == model
                  and (currency is None or snapshot.currency == currency)]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (_parse_time(s.observed_at, "observed_at"),
                                         s.price_version))


def estimate_cost_microunits(record: UsageRecord, snapshot: PriceSnapshot
                             ) -> int | None:
    """Conservative estimated cost for one record under one price version.

    Returns an *estimate* (never claimed exact), or None whenever anything
    required is missing — a missing rate, a missing usage quantity, a model
    or currency mismatch, or a record that already carries an exact reported
    cost (a bill always beats a derivation).  Partial usage is refused
    outright: estimating from known dimensions only would understate the true
    spend and pass off the gap as a number.  Rounding is CEILING, keeping
    the estimate on the safe side for budget purposes.
    """

    if not isinstance(record, UsageRecord) or not isinstance(snapshot, PriceSnapshot):
        raise ValidationError("estimate requires a UsageRecord and a PriceSnapshot")
    if record.cost_microunits is not None and record.precision == "exact":
        return None
    if record.model is None or record.model != snapshot.model:
        return None
    if record.currency is not None and record.currency != snapshot.currency:
        return None
    total = 0
    for field_name, rate_key in (("input_tokens", "input"),
                                ("output_tokens", "output"),
                                ("cache_tokens", "cache")):
        amount = getattr(record, field_name)
        rate = snapshot.rates[rate_key]
        # Any NULL quantity or NULL rate makes the whole estimate
        # untrustable — missing is missing, never zero.
        if amount is None or rate is None:
            return None
        total += amount * rate
    # microunits-per-million-tokens rates applied to token counts, rounded
    # up to whole microunits.
    return -(-total // 1_000_000)


def task_token_usage(conn: storage.TeamCraftConnection, project_id: str,
                     task_id: str, *, locality: str | None = None
                     ) -> escalation.TokenUsage:
    """Null-aware durable usage for one Task — the scheduler M5-03 seam.

    This is the aggregation :func:`teamcraft.scheduler._durable_token_usage`
    documented for "when M5-04 lands usage rows": it sums per-task records
    WITHOUT substituting nulls.  A dimension is known only when every
    contributing record reported it; any NULL keeps the dimension unknown so
    the budget layer keeps its conservative posture.  No records at all is
    all-NULL (unavailable), exactly the pre-M5-04 behavior the seam
    expected.
    """

    records = list_usage(conn, project_id, task_id=task_id, locality=locality)
    if not records:
        return escalation.TokenUsage()
    def _dimension(name: str) -> int | None:
        amounts = [getattr(record, name) for record in records]
        if any(amount is None for amount in amounts):
            return None
        return int(sum(amount for amount in amounts if amount is not None))

    used_input = _dimension("input_tokens")
    used_output = _dimension("output_tokens")
    cost = _dimension("cost_microunits")
    if cost is not None:
        currencies = {record.currency for record in records
                      if record.cost_microunits is not None}
        # One honest currency or nothing: a mix, or an unattributed cost,
        # never collapses into a budget number.
        if len(currencies) > 1 or currencies == {None}:
            cost = None
    total: int | None = None
    if used_input is not None and used_output is not None:
        total = used_input + used_output
    else:
        total = _dimension("total_tokens")
    return escalation.TokenUsage(
        input_tokens=used_input, output_tokens=used_output,
        total_tokens=total, cost_microunits=cost)


# ---------------------------------------------------------------------------
# CEO / Wake projection (durable wake ledger as the usage source)
# ---------------------------------------------------------------------------

def ceo_wake_usage(conn: storage.TeamCraftConnection, project_id: str, *,
                   target_ceo_name: str = "ceo",
                   since: str | None = None, until: str | None = None,
                   clock: Clock | None = None) -> UsageRecord:
    """Project the durable wake ledger into one CEO usage view.

    The authentic source of CEO attention usage is the M2-09/M5-03 ``wakes``
    table — the same ledger the wake budget spends from.  Every
    delivered-or-possibly-delivered wake (states sending/submitted/
    acknowledged/processed/unknown; ``pending`` and reconciled-``blocked``
    never count) inside the optional ``[since, until]`` window contributes to
    ``wake_count``.  CEO token/cost/elapsed measurements stay NULL: the
    current harnesses expose none, and unavailable is recorded as
    unavailable, never as zero.

    This is a *read model*: it appends nothing, because re-deriving the same
    window twice must not double-count.  When a durable per-delivery CEO
    ledger row is wanted, the right hook is wake.py's submission path (see
    slice notes), not repeated snapshots here.  Pass an explicit ``until``
    timestamp to obtain a reproducible view (otherwise ``recorded_at`` — and
    therefore the digest — carries the call clock).
    """

    _require_project(conn, project_id)
    if not isinstance(target_ceo_name, str) or not target_ceo_name.strip():
        raise ValidationError("target_ceo_name must be a non-empty string")
    for name, value in (("since", since), ("until", until)):
        if value is not None:
            _parse_time(value, name)
    placeholders = ",".join("?" for _ in _CONSUMING_WAKE_STATES)
    sql = ("SELECT COUNT(*) FROM wakes WHERE project_id=?"
           " AND target_ceo_name=? AND state IN (" + placeholders + ")")
    params: list[Any] = [project_id, target_ceo_name, *_CONSUMING_WAKE_STATES]
    if since is not None:
        sql += " AND COALESCE(submitted_at, updated_at) >= ?"
        params.append(since)
    if until is not None:
        sql += " AND COALESCE(submitted_at, updated_at) <= ?"
        params.append(until)
    wake_count = int(conn.execute(sql, params).fetchone()[0])
    return UsageRecord(
        role="ceo", source="wake_ledger", precision="unavailable",
        wake_count=wake_count,
        task_id=None, attempt_id=None,
        recorded_at=_iso(_now(clock)),
    )


__all__ = [
    "PRICE_RATE_KEYS",
    "PRICE_SNAPSHOT_EVENT",
    "PRECISION_STATES",
    "USAGE_DIMENSIONS",
    "USAGE_EVENT",
    "USAGE_LOCALITIES",
    "USAGE_ROLES",
    "UNATTRIBUTED_CURRENCY",
    "DimensionTotal",
    "PriceSnapshot",
    "UsageRecord",
    "UsageTotals",
    "ceo_wake_usage",
    "estimate_cost_microunits",
    "get_usage",
    "latest_price_snapshot",
    "latest_price_snapshots",
    "list_usage",
    "record_price_snapshot",
    "record_usage",
    "task_token_usage",
    "usage_totals",
]
