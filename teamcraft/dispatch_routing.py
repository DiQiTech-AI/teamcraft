"""M5-02 Router -> dispatch wiring (opt-in, fail closed).

The router in :mod:`teamcraft.router` is a pure decision layer and the
capability registry in :mod:`teamcraft.providers` only observes facts.  This
module is the deliberately small bridge between them and the main dispatch
path:

* a project-owned, strictly validated route catalog;
* one deterministic decision over the latest durable capability snapshots;
* one durable ``route.decided`` audit event committed together with the
  immutable Attempt capability binding.

Nothing here probes a harness/provider, reads credentials, spawns a process,
or silently falls back to a default model.  A failed decision raises
:class:`RouteUnavailable` and the caller must stop.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import events, providers, router, storage
from .errors import ConflictError, NotFoundError, StorageError, TeamCraftError, ValidationError

#: New audit event kind for a committed dispatch route decision.
ROUTE_DECIDED_EVENT = "route.decided"
#: Exact catalog schema.  Unknown keys are refused rather than ignored.
CATALOG_FIELDS = frozenset({
    "harness", "provider", "model", "locality", "assurance", "cost_rank",
})
CATALOG_FILENAME = "routes.json"

#: Catalog components are joined as ``provider/model`` on the launch path, so
#: a ``/`` here would let ``decide`` choose a route that ``launch`` must reject.
#: Whitespace and empty values are excluded by the same anchored pattern.
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,199}\Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_identifier(value: Any, field: str) -> str:
    if isinstance(value, str):
        providers._reject_secrets(value, field)
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValidationError(
            f"{field} must be a bounded safe identifier"
            " (letters, digits, '.', '_', '+', ':', '-' only; no '/')"
        )
    return value


@dataclass(frozen=True)
class CatalogEntry:
    """One strictly typed route catalog entry."""

    harness: str
    provider: str
    model: str
    locality: str
    assurance: int
    cost_rank: int

    def __post_init__(self) -> None:
        _safe_identifier(self.harness, "catalog harness")
        _safe_identifier(self.provider, "catalog provider")
        _safe_identifier(self.model, "catalog model")
        if self.locality not in router.LOCALITIES:
            raise ValidationError("catalog locality must be local or cloud")
        if (isinstance(self.assurance, bool)
                or not isinstance(self.assurance, int)
                or not 0 <= self.assurance <= 3):
            raise ValidationError("catalog assurance must be an integer from 0 to 3")
        if (isinstance(self.cost_rank, bool)
                or not isinstance(self.cost_rank, int)
                or self.cost_rank < 0):
            raise ValidationError("catalog cost_rank must be an integer >= 0")

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _reject_catalog_secrets(value: Any, path: str = "route_catalog") -> None:
    """Reject credential-shaped keys/text anywhere in the untrusted catalog."""

    providers._reject_secrets(value, path)


def parse_catalog(value: Any) -> tuple[CatalogEntry, ...]:
    """Parse a decoded catalog array with a closed, exact schema."""

    _reject_catalog_secrets(value)
    if not isinstance(value, list):
        raise ValidationError("route catalog must be a JSON array")
    if not value:
        raise ValidationError("route catalog must not be empty")
    entries: list[CatalogEntry] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError(f"route catalog[{index}] must be an object")
        keys = set(item)
        unknown = sorted(keys - CATALOG_FIELDS)
        if unknown:
            raise ValidationError(
                f"route catalog[{index}] has unknown field(s): {', '.join(unknown)}"
            )
        missing = sorted(CATALOG_FIELDS - keys)
        if missing:
            raise ValidationError(
                f"route catalog[{index}] is missing field(s): {', '.join(missing)}"
            )
        entries.append(CatalogEntry(
            harness=item["harness"], provider=item["provider"],
            model=item["model"], locality=item["locality"],
            assurance=item["assurance"], cost_rank=item["cost_rank"],
        ))
    return tuple(entries)


def load_catalog(path: str | Path) -> tuple[CatalogEntry, ...]:
    """Read and strictly validate ``path``; every failure is a ValidationError."""

    location = Path(path).expanduser()
    try:
        text = location.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read route catalog {location}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValidationError(f"route catalog {location} is not valid UTF-8") from exc
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(f"route catalog {location} is not valid JSON") from exc
    return parse_catalog(value)


def catalog_digest(catalog: Iterable[CatalogEntry]) -> str:
    # Route membership is a set: canonical ordering keeps the digest stable
    # when a project rewrites the same routes in a different file order.
    payloads = [entry.payload() for entry in catalog]
    payloads.sort(key=_canonical)
    return _digest(payloads)


@dataclass(frozen=True)
class RouteOutcome:
    """A successful, replayable decision plus its exact capability snapshots."""

    decision: router.RouteDecision
    harness: providers.CapabilitySnapshot
    provider: providers.CapabilitySnapshot
    model: str
    catalog_digest: str = ""
    missing: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def opencode_model(self) -> str:
        return f"{self.provider.name}/{self.model}"

    @property
    def decision_digest(self) -> str:
        return self.decision.decision_digest


class RouteUnavailable(TeamCraftError):
    """Fail-closed routing refusal with replayable diagnostic payload."""

    def __init__(
        self,
        decision: router.RouteDecision,
        *,
        considered: Sequence[str] = (),
        missing: Sequence[str] = (),
        skipped: Sequence[str] = (),
        catalog_digest: str = "",
    ) -> None:
        self.decision = decision
        self.reason_code = decision.reason_code
        self.effective_risk = decision.effective_risk
        self.considered = tuple(considered or decision.considered)
        self.missing = tuple(missing)
        self.skipped = tuple(skipped)
        self.catalog_digest = catalog_digest
        detail = (
            f"no route selected ({decision.reason_code});"
            f" risk={decision.effective_risk};"
            f" considered={list(self.considered)}"
        )
        if self.missing:
            detail += f"; missing_snapshots={list(self.missing)}"
        if self.skipped:
            detail += f"; skipped_routes={list(self.skipped)}"
        super().__init__(detail)

    def payload(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "effective_risk": self.effective_risk,
            "considered": list(self.considered),
            "missing": list(self.missing),
            "skipped": list(self.skipped),
            "catalog_digest": self.catalog_digest,
            "decision": self.decision.payload(),
        }

    @property
    def decision_payload(self) -> dict[str, Any]:
        return self.decision.payload()


def _route_label(entry: CatalogEntry) -> str:
    return _canonical([entry.harness, entry.provider, entry.model])


def decide(
    conn: storage.TeamCraftConnection,
    project_id: str,
    *,
    catalog: Sequence[CatalogEntry],
    risk: str = "normal",
    tags: Sequence[str] = (),
    required_capabilities: Sequence[str] = (),
    policy: router.RouterPolicy = router.RouterPolicy(),
    override: router.ManualOverride | None = None,
    now: datetime | None = None,
) -> RouteOutcome:
    """Choose a route from the latest durable probes, or fail closed."""

    entries = tuple(catalog)
    if not all(isinstance(entry, CatalogEntry) for entry in entries):
        entries = parse_catalog(list(entries))
    if not entries:
        raise ValidationError("route catalog must not be empty")
    if override is not None and not isinstance(override, router.ManualOverride):
        raise ValidationError("route override must be a router.ManualOverride")
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValidationError("now must be a timezone-aware datetime")

    dig = catalog_digest(entries)
    snapshots = {
        (snapshot.entity_type, snapshot.name): snapshot
        for snapshot in providers.latest_capability_probes(conn, project_id)
    }
    candidates: list[router.RouteCandidate] = []
    missing: set[str] = set()
    skipped: list[str] = []
    for entry in entries:
        harness = snapshots.get(("harness", entry.harness))
        provider = snapshots.get(("provider", entry.provider))
        if harness is None:
            missing.add(f"harness:{entry.harness}")
        if provider is None:
            missing.add(f"provider:{entry.provider}")
        if harness is None or provider is None:
            # A catalog route without BOTH durable snapshots does not participate;
            # it is surfaced in the failure payload instead of being guessed.
            skipped.append(_route_label(entry))
            continue
        candidates.append(router.RouteCandidate(
            harness_snapshot=harness,
            provider_snapshot=provider,
            model=entry.model,
            locality=entry.locality,
            assurance=entry.assurance,
            cost_rank=entry.cost_rank,
        ))

    request = router.RouteRequest(
        risk=risk,
        required_capabilities=tuple(required_capabilities),
        task_tags=tuple(tags),
        override=override,
    )
    decision = router.route(request, candidates, policy=policy, now=current)
    considered = tuple(decision.considered)
    missing_list = tuple(sorted(missing))
    skipped_list = tuple(sorted(skipped))
    selection = decision.selection
    if selection is None:
        raise RouteUnavailable(
            decision, considered=considered, missing=missing_list,
            skipped=skipped_list, catalog_digest=dig,
        )

    matching = [item for item in candidates if item.selection == selection]
    if len(matching) != 1:
        # The router only selects candidates it was given; anything else means
        # the decision cannot be bound to exact snapshots.  Never continue.
        raise RouteUnavailable(
            decision, considered=considered, missing=missing_list,
            skipped=skipped_list, catalog_digest=dig,
        )
    selected = matching[0]
    if override is not None:
        audit = decision.override_audit
        # The router applies overrides only after health/capability/risk gates.
        # A requested-but-unapplied override must not be treated as selection.
        if audit is None or not audit.applied:
            raise RouteUnavailable(
                decision, considered=considered, missing=missing_list,
                skipped=skipped_list, catalog_digest=dig,
            )
    return RouteOutcome(
        decision=decision,
        harness=selected.harness_snapshot,
        provider=selected.provider_snapshot,
        model=selected.model,
        catalog_digest=dig,
        missing=missing_list,
        skipped=skipped_list,
    )


@contextlib.contextmanager
def _tx(conn: storage.TeamCraftConnection) -> Iterator[storage.TeamCraftConnection]:
    if conn.in_transaction:
        yield conn
    else:
        with storage.transaction(conn) as gated:
            yield gated


def _route_event_id(attempt_id: str, decision_digest: str) -> str:
    scoped = hashlib.sha256(
        f"{attempt_id}\0{decision_digest}".encode("utf-8")
    ).hexdigest()
    return "evt-route-" + scoped[:40]


def _event_from_row(row: Any) -> events.Event:
    return events.Event(
        seq=int(row[0]), event_id=str(row[1]), kind=str(row[2]),
        project_id=None if row[3] is None else str(row[3]),
        task_id=None if row[4] is None else str(row[4]),
        attempt_id=None if row[5] is None else str(row[5]),
        payload_json=str(row[6]), occurred_at=str(row[7]),
    )


def commit(
    conn: storage.TeamCraftConnection,
    attempt_id: str,
    outcome: RouteOutcome,
    *,
    clock: Any = None,
) -> events.Event:
    """Bind the Attempt capabilities and append exactly one ``route.decided``.

    Binding runs first; any bind failure propagates before the audit event is
    written (one transaction makes that atomic).  The same Attempt + same
    decision replays to the existing event; a different decision is a conflict.
    """

    if not isinstance(outcome, RouteOutcome):
        raise ValidationError("route commit requires a RouteOutcome")
    if outcome.decision.selection is None:
        raise ValidationError("route commit requires a selected route")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValidationError("attempt_id must be a non-empty string")
    row = conn.execute(
        "SELECT a.task_id, t.project_id FROM attempts a"
        " JOIN tasks t ON t.id = a.task_id WHERE a.id = ?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"attempt not found: {attempt_id}")
    task_id, project_id = str(row[0]), str(row[1])
    digest = outcome.decision_digest
    if not outcome.catalog_digest:
        raise ValidationError("route outcome is missing its catalog digest")

    with _tx(conn):
        existing_rows = conn.execute(
            "SELECT seq, event_id, kind, project_id, task_id, attempt_id,"
            " payload_json, occurred_at FROM events"
            " WHERE attempt_id = ? AND kind = ? ORDER BY seq ASC",
            (attempt_id, ROUTE_DECIDED_EVENT),
        ).fetchall()
        for existing_row in existing_rows:
            existing = _event_from_row(existing_row)
            try:
                payload = json.loads(existing.payload_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ConflictError(
                    "stored route decision audit is malformed"
                ) from exc
            if payload.get("decision_digest") == digest:
                return existing
            raise ConflictError(
                "route decision is immutable for this Attempt"
            )

        # Validate the durable snapshots and model membership BEFORE writing
        # any audit event.  If this raises, the transaction leaves no trace.
        providers.bind_attempt_capabilities(
            conn, attempt_id, harness=outcome.harness,
            provider=outcome.provider, model=outcome.model, clock=clock,
        )
        selection = asdict(outcome.decision.selection)
        payload = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "selection": selection,
            "decision": outcome.decision.payload(),
            "decision_digest": digest,
            "catalog_digest": outcome.catalog_digest,
        }
        providers._reject_secrets(payload, "route.decided")
        event_id = _route_event_id(attempt_id, digest)
        try:
            event, _ = events.append_event_idempotent(
                conn, ROUTE_DECIDED_EVENT, project_id=project_id,
                task_id=task_id, attempt_id=attempt_id,
                payload_json=_canonical(payload), event_id=event_id,
                clock=clock,
            )
        except StorageError as exc:
            raise ConflictError(
                "route decision is immutable for this Attempt"
            ) from exc
        return event


__all__ = [
    "CATALOG_FIELDS", "CATALOG_FILENAME", "CatalogEntry", "ROUTE_DECIDED_EVENT",
    "RouteOutcome", "RouteUnavailable", "catalog_digest", "commit", "decide",
    "load_catalog", "parse_catalog",
]
