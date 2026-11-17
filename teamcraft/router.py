"""M5-02 deterministic Harness/Provider/Model routing.

The router is deliberately a pure decision layer.  It consumes the immutable
M5-01 capability snapshots supplied by its caller and performs no probes,
database writes, secret lookup, or provider invocation.  An explicit ``now``
is required so replaying the same inputs also replays health/staleness checks.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .errors import ValidationError
from .providers import CapabilitySnapshot

LOCALITIES = frozenset({"local", "cloud"})
RISK_LEVELS = frozenset({"normal", "high", "critical"})
DEFAULT_HIGH_RISK_TAGS = frozenset({"auth", "authentication", "credentials",
                                    "deploy", "deployment", "keys", "secrets"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,199}\Z")
_SECRET_TEXT = re.compile(
    r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+",
    re.I,
)


def _safe_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValidationError(f"{field} must be a bounded safe identifier")
    return value


def _safe_audit_text(value: str, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must not be blank")
    text = value.strip()
    if len(text.encode("utf-8")) > limit:
        raise ValidationError(f"{field} exceeds {limit} bytes")
    if _SECRET_TEXT.search(text):
        raise ValidationError(f"{field} contains credential-shaped text")
    return text


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


@dataclass(frozen=True)
class RouteSelection:
    harness: str
    provider: str
    model: str

    def __post_init__(self) -> None:
        _safe_identifier(self.harness, "harness")
        _safe_identifier(self.provider, "provider")
        _safe_identifier(self.model, "model")

    @property
    def candidate_id(self) -> str:
        # M5-01 safe identifiers deliberately permit ``/`` (models and
        # providers commonly use namespace/name).  A slash-joined string is
        # therefore ambiguous: ("a/b", "c", "d") collides with
        # ("a", "b/c", "d").  Canonical JSON preserves the exact tuple
        # boundaries while remaining stable, compact, and inspectable.
        return _canonical([self.harness, self.provider, self.model])


@dataclass(frozen=True)
class RouteCandidate:
    """One selectable route backed by exact M5-01 snapshots.

    ``assurance`` is a project-owned ordinal (0..3).  It is intentionally
    independent of price so risk gates can run before any cost preference.
    ``cost_rank`` is only a deterministic relative preference; the router
    never treats missing usage or price as zero/free.
    """

    harness_snapshot: CapabilitySnapshot
    provider_snapshot: CapabilitySnapshot
    model: str
    locality: str
    assurance: int
    cost_rank: int

    def __post_init__(self) -> None:
        if self.harness_snapshot.entity_type != "harness":
            raise ValidationError("candidate harness snapshot must be a harness")
        if self.provider_snapshot.entity_type != "provider":
            raise ValidationError("candidate provider snapshot must be a provider")
        _safe_identifier(self.model, "model")
        if self.locality not in LOCALITIES:
            raise ValidationError("candidate locality must be local or cloud")
        if (isinstance(self.assurance, bool) or not isinstance(self.assurance, int)
                or not 0 <= self.assurance <= 3):
            raise ValidationError("candidate assurance must be an integer from 0 to 3")
        if (isinstance(self.cost_rank, bool) or not isinstance(self.cost_rank, int)
                or self.cost_rank < 0):
            raise ValidationError("candidate cost_rank must be an integer >= 0")

    @property
    def selection(self) -> RouteSelection:
        return RouteSelection(self.harness_snapshot.name,
                              self.provider_snapshot.name, self.model)

    @property
    def supported_capabilities(self) -> frozenset[str]:
        mappings = (
            self.harness_snapshot.discovery,
            self.harness_snapshot.capabilities,
            self.provider_snapshot.discovery,
            self.provider_snapshot.capabilities,
        )
        return frozenset(str(key) for mapping in mappings
                         for key, state in mapping.items()
                         if state == "supported")

    def is_healthy(self, *, now: datetime) -> bool:
        return (
            bool(self.harness_snapshot.effective_health(now=now)["healthy"])
            and bool(self.provider_snapshot.effective_health(now=now)["healthy"])
            and self.model in self.provider_snapshot.models
        )


@dataclass(frozen=True)
class ManualOverride:
    selection: RouteSelection
    actor: str
    reason: str

    def __post_init__(self) -> None:
        _safe_audit_text(self.actor, "override actor", limit=128)
        _safe_audit_text(self.reason, "override reason", limit=512)

    def audit(self, *, applied: bool) -> "OverrideAudit":
        return OverrideAudit(self.actor.strip(), self.reason.strip(),
                             self.selection, applied)


@dataclass(frozen=True)
class OverrideAudit:
    actor: str
    reason: str
    requested_selection: RouteSelection
    applied: bool


@dataclass(frozen=True)
class RouteRequest:
    risk: str = "normal"
    required_capabilities: tuple[str, ...] = ()
    task_tags: tuple[str, ...] = ()
    override: ManualOverride | None = None

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValidationError("route risk must be normal, high, or critical")
        for value in self.required_capabilities:
            _safe_identifier(value, "required capability")
        for value in self.task_tags:
            _safe_identifier(value, "task tag")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValidationError("required capabilities must not contain duplicates")
        if len(set(self.task_tags)) != len(self.task_tags):
            raise ValidationError("task tags must not contain duplicates")


@dataclass(frozen=True)
class RouterPolicy:
    local_first: bool = True
    high_risk_min_assurance: int = 2
    critical_min_assurance: int = 3
    high_risk_tags: frozenset[str] = DEFAULT_HIGH_RISK_TAGS

    def __post_init__(self) -> None:
        if not isinstance(self.local_first, bool):
            raise ValidationError("local_first must be boolean")
        for field, value in (
            ("high_risk_min_assurance", self.high_risk_min_assurance),
            ("critical_min_assurance", self.critical_min_assurance),
        ):
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 0 <= value <= 3):
                raise ValidationError(f"{field} must be an integer from 0 to 3")
        if self.critical_min_assurance < self.high_risk_min_assurance:
            raise ValidationError("critical assurance cannot be below high-risk assurance")
        for value in self.high_risk_tags:
            _safe_identifier(value, "high-risk tag")


@dataclass(frozen=True)
class RouteDecision:
    selection: RouteSelection | None
    reason_code: str
    effective_risk: str
    required_capabilities: tuple[str, ...]
    considered: tuple[str, ...]
    override_audit: OverrideAudit | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "selection": None if self.selection is None else asdict(self.selection),
            "reason_code": self.reason_code,
            "effective_risk": self.effective_risk,
            "required_capabilities": list(self.required_capabilities),
            "considered": list(self.considered),
            "override_audit": (None if self.override_audit is None
                               else asdict(self.override_audit)),
        }

    @property
    def decision_digest(self) -> str:
        return hashlib.sha256(_canonical(self.payload()).encode("utf-8")).hexdigest()


def _effective_risk(request: RouteRequest, policy: RouterPolicy) -> str:
    if request.risk == "critical":
        return "critical"
    normalized_tags = frozenset(tag.lower() for tag in request.task_tags)
    policy_tags = frozenset(tag.lower() for tag in policy.high_risk_tags)
    if request.risk == "high" or normalized_tags.intersection(policy_tags):
        return "high"
    return "normal"


def _decision(selection: RouteSelection | None, reason_code: str, *,
              effective_risk: str, required: tuple[str, ...],
              considered: tuple[str, ...], override: ManualOverride | None = None,
              override_applied: bool = False) -> RouteDecision:
    return RouteDecision(
        selection=selection,
        reason_code=reason_code,
        effective_risk=effective_risk,
        required_capabilities=required,
        considered=considered,
        override_audit=(None if override is None
                        else override.audit(applied=override_applied)),
    )


def route(request: RouteRequest, candidates: Iterable[RouteCandidate], *,
          policy: RouterPolicy = RouterPolicy(), now: datetime) -> RouteDecision:
    """Choose one eligible route using stable, replayable ordering.

    The filtering order is part of the public contract: current health and
    model presence, required capabilities, then risk assurance.  An override
    is evaluated only after these gates and therefore cannot weaken them.
    Invalid overrides fail closed rather than silently falling back.
    """

    current = _aware_utc(now)
    required = tuple(sorted(request.required_capabilities))
    ordered = tuple(sorted(candidates, key=lambda item: item.selection.candidate_id))
    ids = tuple(item.selection.candidate_id for item in ordered)
    if len(set(ids)) != len(ids):
        raise ValidationError("route candidates must have unique selections")
    effective_risk = _effective_risk(request, policy)

    healthy = tuple(item for item in ordered if item.is_healthy(now=current))
    if not healthy:
        return _decision(None, "no_healthy_candidates", effective_risk=effective_risk,
                         required=required, considered=ids, override=request.override)

    capable = tuple(item for item in healthy
                    if set(required).issubset(item.supported_capabilities))
    if not capable:
        return _decision(None, "capabilities_unsatisfied", effective_risk=effective_risk,
                         required=required, considered=ids, override=request.override)

    minimum = (policy.critical_min_assurance if effective_risk == "critical"
               else policy.high_risk_min_assurance if effective_risk == "high"
               else 0)
    assured = tuple(item for item in capable if item.assurance >= minimum)
    if not assured:
        return _decision(None, "high_risk_assurance_unsatisfied",
                         effective_risk=effective_risk, required=required,
                         considered=ids, override=request.override)

    if request.override is not None:
        matching = tuple(item for item in ordered
                         if item.selection == request.override.selection)
        if not matching:
            return _decision(None, "override_target_not_found",
                             effective_risk=effective_risk, required=required,
                             considered=ids, override=request.override)
        eligible_selections = {item.selection for item in assured}
        if request.override.selection not in eligible_selections:
            return _decision(None, "override_target_ineligible",
                             effective_risk=effective_risk, required=required,
                             considered=ids, override=request.override)
        return _decision(request.override.selection, "selected_manual_override",
                         effective_risk=effective_risk, required=required,
                         considered=ids, override=request.override,
                         override_applied=True)

    locality_rank = {"local": 0, "cloud": 1} if policy.local_first else {
        "local": 0, "cloud": 0}
    if effective_risk in {"high", "critical"}:
        ranking = lambda item: (-item.assurance, locality_rank[item.locality],
                                item.cost_rank, item.selection.candidate_id)
    else:
        ranking = lambda item: (locality_rank[item.locality], item.cost_rank,
                                -item.assurance, item.selection.candidate_id)
    selected = min(assured, key=ranking)
    if effective_risk in {"high", "critical"}:
        reason = "selected_high_risk_assurance"
    elif policy.local_first and selected.locality == "local":
        reason = "selected_local_first"
    else:
        reason = "selected_deterministic_best"
    return _decision(selected.selection, reason, effective_risk=effective_risk,
                     required=required, considered=ids)


__all__ = [
    "DEFAULT_HIGH_RISK_TAGS", "LOCALITIES", "RISK_LEVELS", "ManualOverride",
    "OverrideAudit",
    "RouteCandidate", "RouteDecision", "RouteRequest", "RouteSelection",
    "RouterPolicy", "route",
]
