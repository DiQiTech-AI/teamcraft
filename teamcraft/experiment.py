"""M5-06 controlled comparison: cloud-only vs mixed mode evidence reducer.

PLAN §7.3 asks for one specific measurement — "pure cloud vs mixed mode
cost/quality/time on the SAME task set" — and the M5 exit principle adds two
hard conditions: the comparison must be reproducible, and it must not
overstate a benefit.  This module is therefore an **evidence reducer**, not a
live runner:

* it never invokes a harness, a provider, a model, or the network; it reads
  facts that were already persisted elsewhere (durable attempts, M5-04 usage
  records, M5-01 capability probes, approval commands) and reduces them;
* the task set and the acceptance rules are **pinned** before any observation
  is accepted, and both arms are reduced against that single pinned plan — the
  rules object is structurally shared, so an arm cannot quietly carry its own
  easier acceptance bar;
* observations that disagree with the pinned plan (unknown arm, task outside
  the pinned set, an attempt or usage record belonging to someone else) fail
  closed instead of being quietly dropped.

Three distinctions the roadmap refuses to conflate are carried end to end:

**Real vs synthetic.**  Every observation declares ``data_origin``: ``real``
(measured from this store's durable evidence) or ``synthetic`` (a local fixture
used to exercise the method).  A synthetic observation must carry a
``synthetic_note``; a real one may not.  A report whose arms are synthetic is
labelled ``synthetic_rehearsal`` and can NEVER make a claim — the arithmetic is
real, the numbers are not measurements, and nothing here pretends otherwise.
If one arm is real and the other synthetic the report says ``mixed_origin`` and
compares nothing at all.

**Unknown vs zero.**  Elapsed time, interventions, recoveries, tokens and cost
are null-aware.  ``None`` means nobody reported it and stays out of every
total; the strict figure is ``None`` whenever any pinned contributor is
missing, so a partial sum is never presented as a complete one (the same
posture :mod:`teamcraft.usage` and :mod:`teamcraft.escalation` take).  A
genuinely reported zero — e.g. a ``local_token_share`` of exactly ``0.0`` in
the cloud-only arm — is kept as zero and is a different value from ``None``.
A zero *baseline* is also not an error: it is reported as ``baseline_zero``
with the relative change left unknown, rather than as an infinite saving.

**Nothing is invented.**  No price, token count, outcome, attempt, or task is
fabricated: a synthetic arm demonstrates the pipeline, it does not become a
result.  What a live run still needs is computed and reported explicitly by
:func:`live_requirements` (which live harness/provider adapters must be
reachable, which cloud authorization must exist per task, which price
provenance must be registered) instead of being assumed satisfied.

Failed attempts, human interventions, lost-contact recoveries, elapsed time and
the usage records behind every figure are preserved in the report, including
for arms that lost — a run is never summarized by its winners.

Public contract pinned by ``tests/test_experiment_m506.py``:

* arms are exactly ``cloud_only`` and ``mixed``, with fixed locality profiles
  (``cloud_only`` may not consume local capacity; ``mixed`` must show both);
* :class:`AcceptanceRules` is pinned once per plan and shared by both arms;
  changing version, checks, config digest, task set, or arm profile changes
  ``plan_digest``, and identical inputs replay to the same digest;
* plans and observations are append-only events, idempotent on their digest
  and loud on identity collision;
* pinned tasks, referenced attempts, and referenced usage records must exist in
  the project (fail closed, no invented bindings);
* reducing the same durable evidence twice — or with observations supplied in a
  different order — yields the same ``report_digest``;
* every metric reports per-arm strict values, a delta only when comparable, a
  direction, and the reason codes why not;
* ``claim_allowed`` is True only for a fully covered, fully comparable,
  all-real, gap-free comparison;
* :func:`live_requirements` lists the live adapters and authorizations still
  required, and states ``live_ready`` False while any of them is missing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from . import events, providers, scheduler, storage, usage
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, StorageError, ValidationError
from .ids import require_entity_id

#: One pinned experiment plan per append.
PLAN_EVENT = "experiment.plan_pinned"
#: One arm/task observation per append.
OBSERVATION_EVENT = "experiment.observation_recorded"
#: One reduced comparison report per append (audit copy of a deterministic view).
COMPARISON_EVENT = "experiment.comparison_reduced"

#: The two arms of the roadmap §7.3 comparison, in canonical report order.
ARMS: tuple[str, ...] = ("cloud_only", "mixed")
ARM_SET = frozenset(ARMS)
#: The locality profile of an arm is part of its definition, not a knob: a run
#: that consumed local capacity was not cloud-only, however it is labelled.
ARM_REQUIRED_LOCALITIES: Mapping[str, tuple[str, ...]] = {
    "cloud_only": ("cloud",),
    "mixed": ("cloud", "local"),
}
#: ``real`` = measured from durable evidence in this store.
#: ``synthetic`` = local fixture, method rehearsal only, never a claim.
DATA_ORIGINS = frozenset({"real", "synthetic"})
#: Task-level acceptance outcome.  ``unknown`` is a first-class state: it is
#: never folded into ``failed`` (which would understate) or ``verified``.
OUTCOME_STATES = frozenset({"verified", "failed", "blocked", "unknown"})
#: Attempt-level results preserved verbatim (failures are evidence).
ATTEMPT_RESULTS = frozenset({"succeeded", "failed", "cancelled", "unknown"})
#: Difficulty stratum, so completion rates stay separable (PLAN §7.3).
DIFFICULTIES = frozenset({"low", "medium", "high", "unknown"})
#: Locality mirrors M5-04 usage records.
LOCALITIES = usage.USAGE_LOCALITIES

#: Metric direction.  ``informational`` metrics report a delta but never name a
#: better arm (counting more lost-contact recoveries as "worse" would be an
#: interpretation the roadmap does not license).
METRIC_DIRECTIONS: Mapping[str, str] = {
    "verified_task_count": "higher_is_better",
    "verified_rate": "higher_is_better",
    "median_verified_elapsed_ms": "lower_is_better",
    "total_elapsed_ms": "lower_is_better",
    "cloud_input_tokens": "lower_is_better",
    "cloud_output_tokens": "lower_is_better",
    "cloud_total_tokens": "lower_is_better",
    "cloud_tokens_per_verified_task": "lower_is_better",
    "local_token_share": "higher_is_better",
    "cloud_cost_microunits": "lower_is_better",
    "failed_attempt_count": "lower_is_better",
    "intervention_count": "lower_is_better",
    "recovery_count": "informational",
}
METRIC_NAMES: tuple[str, ...] = tuple(METRIC_DIRECTIONS)

# --- blocker / reason codes (stable, machine-readable) ----------------------
BLOCKER_TASK_COVERAGE = "TASK_COVERAGE_INCOMPLETE"
BLOCKER_OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN_PRESENT"
BLOCKER_USAGE_UNAVAILABLE = "USAGE_UNAVAILABLE"
BLOCKER_USAGE_PARTIAL = "USAGE_PARTIAL"
BLOCKER_LOCALITY_UNKNOWN = "LOCALITY_UNKNOWN_PRESENT"
BLOCKER_LOCALITY_MISMATCH = "ARM_LOCALITY_MISMATCH"
BLOCKER_COST_UNATTRIBUTED = "COST_UNATTRIBUTED"
BLOCKER_COST_PRICE_VERSION = "COST_PRICE_VERSION_MISSING"
BLOCKER_COST_MIXED_CURRENCY = "COST_MIXED_CURRENCIES"
BLOCKER_SYNTHETIC = "SYNTHETIC_DATA_PRESENT"
BLOCKER_MIXED_ORIGINS = "MIXED_DATA_ORIGINS"
BLOCKER_INTERVENTION_EVIDENCE = "INTERVENTION_EVIDENCE_GAP"

#: Metric-level reason codes (stable, and never presented as a result).
#: ``baseline_zero`` is a *known* zero baseline: the absolute delta stands but
#: the relative change is undefined, so no percentage saving is ever implied.
REASON_BASELINE_ZERO = "baseline_zero"
#: No task in the arm passed acceptance, so a per-verified-task ratio has no
#: denominator; that is reported, not turned into a zero.
REASON_NO_VERIFIED_TASK = "no_verified_task"
#: Some evidence in the arm left this dimension unreported (a declared count
#: nobody recorded), so the arm total is unknown rather than short by design.
REASON_PARTIAL_EVIDENCE = "partial_evidence"
#: The arms' evidence grades differ, so no metric may be compared across them.
REASON_GRADE_MISMATCH = "evidence_grade_mismatch"

#: Requirement kinds reported by :func:`live_requirements`.
REQUIREMENT_KINDS = frozenset({
    "harness_adapter", "provider_adapter", "price_provenance",
    "cloud_authorization",
})
REQUIREMENT_STATUSES = frozenset({
    "satisfied", "missing", "unknown", "unhealthy", "stale", "not_required",
})

_UNHEALTHY_HEALTH = frozenset({"unhealthy", "unknown", "unsupported"})

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+_-]{0,199}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_SECRET_TEXT = re.compile(
    r"\bBearer\s+\S+|\bbearer[-_][A-Za-z0-9_-]+|\bsk-[A-Za-z0-9_-]{8,}", re.I)
_SECRET_KEY = re.compile(
    r"\b(?:api[_-]?key|apikey|authorization|bearer|password|secret)\b", re.I)
_MAX_TEXT_BYTES = 400


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

def _now(clock: Clock | None) -> datetime:
    source = clock if clock is not None else SystemClock()
    return source.now().astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded safe identifier")
    return value


def _safe_text(value: Any, name: str) -> str:
    """Bounded, credential-free audit text (never a blank placeholder)."""

    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be non-empty audit text")
    text = value.strip()
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValidationError(f"{name} exceeds {_MAX_TEXT_BYTES} bytes")
    if _SECRET_TEXT.search(text):
        raise ValidationError(f"{name} contains credential-shaped text")
    return text


def _nullable_int(value: Any, name: str) -> int | None:
    """NULL stays NULL (unreported); a reported 0 is a real, known zero."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer or null")
    return value


def _hex64(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValidationError(f"{name} must be a lowercase sha256 hex digest")
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


def _unique_names(values: Any, name: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValidationError(f"{name} must be a sequence of safe identifiers")
    names = tuple(_safe_text(item, name) for item in values)
    if not names:
        raise ValidationError(f"{name} must not be empty")
    if len(set(names)) != len(names):
        raise ValidationError(f"{name} must not contain duplicates")
    return names


def _require_project(conn: storage.TeamCraftConnection, project_id: str) -> None:
    if conn.execute("SELECT 1 FROM projects WHERE id = ?",
                   (require_entity_id(project_id),)).fetchone() is None:
        raise NotFoundError(f"project not found: {project_id}")


# ---------------------------------------------------------------------------
# pinning: acceptance rules, task set, arms
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AcceptanceRules:
    """The acceptance bar shared by BOTH arms of one experiment.

    The plan holds exactly one of these, so "same acceptance rules" is a
    structural property rather than a convention the caller has to honour.  The
    ``config_digest`` optionally binds the rules to a durable verifier config
    (M4 ``verifier_profiles.config_digest``), which keeps the bar traceable to
    the checks that actually ran.
    """

    rules_version: str
    checks: tuple[str, ...]
    config_digest: str | None = None

    def __post_init__(self) -> None:
        _safe_name(self.rules_version, "rules_version")
        object.__setattr__(self, "checks", _unique_names(self.checks, "checks"))
        if self.config_digest is not None:
            _hex64(self.config_digest, "config_digest")
        _reject_secrets(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "rules_version": self.rules_version,
            "checks": list(self.checks),
            "config_digest": self.config_digest,
        }

    @property
    def rules_digest(self) -> str:
        return _digest(self.payload())

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "AcceptanceRules":
        try:
            return cls(
                rules_version=str(value["rules_version"]),
                checks=tuple(str(item) for item in value["checks"]),
                config_digest=None if value.get("config_digest") is None
                else str(value["config_digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed acceptance rules payload") from exc


@dataclass(frozen=True)
class PinnedTask:
    """One task of the pinned task set.

    ``acceptance_digest`` is the caller's binding over that task's acceptance
    criteria (e.g. the digest of its acceptance JSON / brief); it is part of the
    plan digest so a silently edited acceptance criterion changes the plan and
    stops the comparison from replaying.
    """

    task_id: str
    acceptance_digest: str
    difficulty: str = "unknown"

    def __post_init__(self) -> None:
        require_entity_id(self.task_id)
        _hex64(self.acceptance_digest, "acceptance_digest")
        if self.difficulty not in DIFFICULTIES:
            raise ValidationError("difficulty must be low, medium, high, or unknown")

    def payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "acceptance_digest": self.acceptance_digest,
            "difficulty": self.difficulty,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "PinnedTask":
        try:
            return cls(
                task_id=str(value["task_id"]),
                acceptance_digest=str(value["acceptance_digest"]),
                difficulty=str(value.get("difficulty", "unknown")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed pinned task payload") from exc


@dataclass(frozen=True)
class ArmSpec:
    """What an arm is *configured* to use — declared, never probed here.

    ``required_localities`` is fixed by the arm definition: passing anything
    other than the canonical profile for that arm is a refusal, because
    re-labelling a cloud-only run as "mixed" is exactly the kind of
    self-serving comparison the M5 exit principle forbids.
    """

    arm: str
    harness: str
    providers: tuple[str, ...]
    models: tuple[str, ...]
    cloud_escalation_requires_approval: bool = True
    required_localities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.arm not in ARM_SET:
            raise ValidationError("arm must be cloud_only or mixed")
        _safe_name(self.harness, "harness")
        object.__setattr__(self, "providers", _unique_names(self.providers, "providers"))
        object.__setattr__(self, "models", _unique_names(self.models, "models"))
        if not isinstance(self.cloud_escalation_requires_approval, bool):
            raise ValidationError("cloud_escalation_requires_approval must be a boolean")
        canonical = ARM_REQUIRED_LOCALITIES[self.arm]
        if self.required_localities:
            requested = tuple(str(item) for item in self.required_localities)
            if any(item not in LOCALITIES for item in requested):
                raise ValidationError("required_localities entries must be local or cloud")
            if set(requested) != set(canonical):
                raise ValidationError(
                    f"arm {self.arm} has a fixed locality profile {list(canonical)};"
                    f" {list(sorted(set(requested)))} would relabel the comparison")
        # Always the canonical tuple: the digest must not depend on whether the
        # caller happened to spell out the profile it is required to have.
        object.__setattr__(self, "required_localities", canonical)

    @property
    def wants_cloud(self) -> bool:
        return "cloud" in self.required_localities

    def payload(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "harness": self.harness,
            "providers": list(self.providers),
            "models": list(self.models),
            "cloud_escalation_requires_approval": self.cloud_escalation_requires_approval,
            "required_localities": list(self.required_localities),
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "ArmSpec":
        try:
            return cls(
                arm=str(value["arm"]),
                harness=str(value["harness"]),
                providers=tuple(str(item) for item in value["providers"]),
                models=tuple(str(item) for item in value["models"]),
                cloud_escalation_requires_approval=bool(
                    value.get("cloud_escalation_requires_approval", True)),
                required_localities=tuple(
                    str(item) for item in value.get("required_localities", ())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed arm spec payload") from exc


@dataclass(frozen=True)
class ExperimentPlan:
    """The pinned experiment: same task set, same acceptance rules, two arms."""

    title: str
    acceptance: AcceptanceRules
    tasks: tuple[PinnedTask, ...]
    arms: tuple[ArmSpec, ...]
    pinned_at: str

    def __post_init__(self) -> None:
        _safe_text(self.title, "title")
        if not isinstance(self.acceptance, AcceptanceRules):
            raise ValidationError("acceptance must be an AcceptanceRules")
        tasks = tuple(self.tasks)
        if not tasks:
            raise ValidationError("a controlled comparison needs a non-empty task set")
        if any(not isinstance(task, PinnedTask) for task in tasks):
            raise ValidationError("tasks must be PinnedTask entries")
        ids = [task.task_id for task in tasks]
        if len(set(ids)) != len(ids):
            raise ValidationError("the pinned task set contains duplicate task ids")
        # Canonical order: reproducibility must not depend on how the caller
        # happened to iterate its task collection.
        object.__setattr__(self, "tasks", tuple(sorted(tasks, key=lambda t: t.task_id)))
        arms = tuple(self.arms)
        if any(not isinstance(arm, ArmSpec) for arm in arms):
            raise ValidationError("arms must be ArmSpec entries")
        names = [arm.arm for arm in arms]
        if set(names) != ARM_SET or len(names) != len(ARM_SET):
            raise ValidationError("a controlled comparison pins exactly cloud_only and mixed")
        object.__setattr__(self, "arms", tuple(sorted(arms, key=lambda a: ARMS.index(a.arm))))
        _parse_time(self.pinned_at, "pinned_at")
        _reject_secrets(self.payload())

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks)

    def task(self, task_id: str) -> PinnedTask | None:
        for pinned in self.tasks:
            if pinned.task_id == task_id:
                return pinned
        return None

    def arm_spec(self, arm: str) -> ArmSpec:
        for spec in self.arms:
            if spec.arm == arm:
                return spec
        raise ValidationError(f"arm not pinned by this plan: {arm}")

    def covers(self, arm: str, task_id: str) -> bool:
        return arm in ARM_SET and self.task(task_id) is not None

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "title": self.title,
            "acceptance": self.acceptance.payload(),
            "tasks": [task.payload() for task in self.tasks],
            "arms": [arm.payload() for arm in self.arms],
            "pinned_at": self.pinned_at,
        }

    @property
    def plan_digest(self) -> str:
        return _digest(self.payload())

    @property
    def plan_id(self) -> str:
        return "exp-" + self.plan_digest[:40]

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "ExperimentPlan":
        try:
            return cls(
                title=str(value["title"]),
                acceptance=AcceptanceRules.from_payload(value["acceptance"]),
                tasks=tuple(PinnedTask.from_payload(item) for item in value["tasks"]),
                arms=tuple(ArmSpec.from_payload(item) for item in value["arms"]),
                pinned_at=str(value["pinned_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed experiment plan payload") from exc


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttemptEvidence:
    """One attempt as observed, failures included and never dropped.

    ``locality`` is nullable: an attempt whose execution locality nobody
    recorded stays unknown and is counted under ``unknown`` rather than being
    assumed local (which would flatter a mixed-mode run) or cloud.
    """

    attempt_id: str
    result: str
    locality: str | None = None
    elapsed_ms: int | None = None

    def __post_init__(self) -> None:
        require_entity_id(self.attempt_id)
        if self.result not in ATTEMPT_RESULTS:
            raise ValidationError("attempt result must be succeeded, failed, cancelled, or unknown")
        if self.locality is not None and self.locality not in LOCALITIES:
            raise ValidationError("attempt locality must be local, cloud, or null")
        _nullable_int(self.elapsed_ms, "attempt.elapsed_ms")

    @property
    def failed(self) -> bool:
        return self.result == "failed"

    def payload(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "result": self.result,
            "locality": self.locality,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "AttemptEvidence":
        try:
            return cls(
                attempt_id=str(value["attempt_id"]),
                result=str(value["result"]),
                locality=None if value.get("locality") is None else str(value["locality"]),
                elapsed_ms=value.get("elapsed_ms"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed attempt evidence payload") from exc


@dataclass(frozen=True)
class TaskObservation:
    """One arm's result for one pinned task.

    Provenance is mandatory and non-defaulted: the caller must say whether this
    is a measurement (``real``) or a fixture (``synthetic``), because the whole
    credibility of the report depends on that label being deliberate.
    """

    arm: str
    task_id: str
    data_origin: str
    outcome: str
    evidence_ref: str
    observed_at: str
    attempts: tuple[AttemptEvidence, ...] = ()
    usage_ids: tuple[str, ...] = ()
    elapsed_ms: int | None = None
    intervention_count: int | None = None
    recovery_count: int | None = None
    synthetic_note: str | None = None

    def __post_init__(self) -> None:
        if self.arm not in ARM_SET:
            raise ValidationError("observation arm must be cloud_only or mixed")
        require_entity_id(self.task_id)
        if self.data_origin not in DATA_ORIGINS:
            raise ValidationError(
                "data_origin must be real or synthetic; provenance is never defaulted")
        if self.outcome not in OUTCOME_STATES:
            raise ValidationError("outcome must be verified, failed, blocked, or unknown")
        _safe_text(self.evidence_ref, "evidence_ref")
        _parse_time(self.observed_at, "observed_at")
        attempts = tuple(self.attempts)
        if any(not isinstance(item, AttemptEvidence) for item in attempts):
            raise ValidationError("attempts must be AttemptEvidence entries")
        ids = [item.attempt_id for item in attempts]
        if len(set(ids)) != len(ids):
            raise ValidationError("attempts contain duplicate attempt ids")
        object.__setattr__(self, "attempts", tuple(sorted(attempts, key=lambda a: a.attempt_id)))
        usage_ids = tuple(self.usage_ids)
        if any(not isinstance(item, str) for item in usage_ids):
            raise ValidationError("usage_ids must be usage record ids")
        if len(set(usage_ids)) != len(usage_ids):
            raise ValidationError("usage_ids contain duplicates")
        object.__setattr__(self, "usage_ids", tuple(sorted(usage_ids)))
        _nullable_int(self.elapsed_ms, "elapsed_ms")
        _nullable_int(self.intervention_count, "intervention_count")
        _nullable_int(self.recovery_count, "recovery_count")
        # Outcome claims need the evidence that backs them, and failures are
        # preserved rather than edited out of a losing arm.
        if self.outcome == "verified" \
                and not any(item.result == "succeeded" for item in attempts):
            raise ValidationError(
                "outcome verified requires a succeeded attempt; a self-report is"
                " not acceptance evidence")
        if self.outcome == "failed" and not any(item.failed for item in attempts):
            raise ValidationError(
                "outcome failed requires a failed attempt to point at")
        if self.data_origin == "synthetic":
            if self.synthetic_note is None:
                raise ValidationError(
                    "synthetic observations must carry a synthetic_note explaining"
                    " what fixture produced them")
            _safe_text(self.synthetic_note, "synthetic_note")
        elif self.synthetic_note is not None:
            raise ValidationError(
                "synthetic_note belongs to synthetic observations; a real"
                " measurement cites durable evidence instead")
        _reject_secrets(self.payload())

    @property
    def is_real(self) -> bool:
        return self.data_origin == "real"

    @property
    def failed_attempts(self) -> tuple[str, ...]:
        return tuple(item.attempt_id for item in self.attempts if item.failed)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "arm": self.arm,
            "task_id": self.task_id,
            "data_origin": self.data_origin,
            "outcome": self.outcome,
            "evidence_ref": self.evidence_ref,
            "observed_at": self.observed_at,
            "attempts": [item.payload() for item in self.attempts],
            "usage_ids": list(self.usage_ids),
            "elapsed_ms": self.elapsed_ms,
            "intervention_count": self.intervention_count,
            "recovery_count": self.recovery_count,
            "synthetic_note": self.synthetic_note,
        }

    @property
    def observation_digest(self) -> str:
        return _digest(self.payload())

    @property
    def observation_id(self) -> str:
        return "obs-" + self.observation_digest[:40]

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "TaskObservation":
        try:
            return cls(
                arm=str(value["arm"]),
                task_id=str(value["task_id"]),
                data_origin=str(value["data_origin"]),
                outcome=str(value["outcome"]),
                evidence_ref=str(value["evidence_ref"]),
                observed_at=str(value["observed_at"]),
                attempts=tuple(AttemptEvidence.from_payload(item)
                              for item in value.get("attempts", ())),
                usage_ids=tuple(str(item) for item in value.get("usage_ids", ())),
                elapsed_ms=value.get("elapsed_ms"),
                intervention_count=value.get("intervention_count"),
                recovery_count=value.get("recovery_count"),
                synthetic_note=None if value.get("synthetic_note") is None
                else str(value["synthetic_note"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed observation payload") from exc


# ---------------------------------------------------------------------------
# null-aware value shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ratio:
    """A share whose denominator can be zero or missing — never silently 0.0.

    ``value`` is None when nothing was observed (denominator unknown/zero):
    an empty arm has an *unknown* success rate, not a 0 % one.  ``strict_value``
    additionally requires full pinned coverage and no unknown contributor.
    """

    numerator: int | None
    denominator: int | None
    complete: bool = False

    def __post_init__(self) -> None:
        _nullable_int(self.numerator, "ratio.numerator")
        _nullable_int(self.denominator, "ratio.denominator")
        if not isinstance(self.complete, bool):
            raise ValidationError("ratio.complete must be a boolean")

    @property
    def defined(self) -> bool:
        return self.numerator is not None and bool(self.denominator)

    @property
    def value(self) -> float | None:
        if not self.defined:
            return None
        return self.numerator / self.denominator

    @property
    def strict_value(self) -> float | None:
        if not self.defined or not self.complete:
            return None
        return self.numerator / self.denominator

    @property
    def denominator_is_zero(self) -> bool:
        """A known zero denominator — distinct from an unknown one."""

        return self.numerator is not None and self.denominator == 0

    def payload(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "complete": self.complete,
            "value": self.value,
            "strict_value": self.strict_value,
            "denominator_is_zero": self.denominator_is_zero,
        }


@dataclass(frozen=True)
class MetricValue:
    """One arm's contribution to one metric, with its completeness posture."""

    amount: float | int | None
    complete: bool = False
    unknown_count: int = 0
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.amount is not None and isinstance(self.amount, bool):
            raise ValidationError("metric amount must be a number or null")
        if self.amount is not None and self.amount < 0:
            raise ValidationError("metric amount must not be negative")
        _nullable_int(self.unknown_count, "metric.unknown_count")
        if not isinstance(self.complete, bool):
            raise ValidationError("metric.complete must be a boolean")
        object.__setattr__(self, "reasons", tuple(sorted(set(self.reasons))))

    def payload(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "complete": self.complete,
            "unknown_count": self.unknown_count,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class MetricComparison:
    """One metric across the two arms, with the reason it may not be compared.

    ``delta`` is always ``mixed - cloud_only`` (the roadmap's question is what
    mixed mode changes relative to going all-cloud).  It is None unless both
    arms produced a strict value from the same evidence grade.  ``relative``
    stays None on a known zero baseline (``baseline_zero``): dividing by zero is
    not an infinite improvement, it is an undefined one.
    """

    name: str
    direction: str
    cloud_only: MetricValue
    mixed: MetricValue
    delta: float | int | None
    relative: float | None
    comparable: bool
    better_arm: str | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.name not in METRIC_DIRECTIONS:
            raise ValidationError(f"unknown comparison metric: {self.name}")
        if self.direction != METRIC_DIRECTIONS[self.name]:
            raise ValidationError(f"metric {self.name} has a fixed direction")
        if self.better_arm not in (None, "tie", *ARMS):
            raise ValidationError("better_arm must be cloud_only, mixed, tie, or null")
        object.__setattr__(self, "reasons", tuple(sorted(set(self.reasons))))

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "cloud_only": self.cloud_only.payload(),
            "mixed": self.mixed.payload(),
            "delta": self.delta,
            "relative": self.relative,
            "comparable": self.comparable,
            "better_arm": self.better_arm,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Blocker:
    """One machine-readable reason a comparison cannot support a claim."""

    code: str
    arm: str | None
    detail: str

    def __post_init__(self) -> None:
        _safe_name(self.code, "blocker code")
        if self.arm is not None and self.arm not in ARM_SET:
            raise ValidationError("blocker arm must be cloud_only or mixed")
        _safe_text(self.detail, "blocker detail")

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "arm": self.arm, "detail": self.detail}


@dataclass(frozen=True)
class ArmSummary:
    """Everything one arm did — wins, losses, interventions, and its gaps."""

    arm: str
    required_localities: tuple[str, ...]
    tasks_total: int
    tasks_observed: int
    missing_task_ids: tuple[str, ...]
    outcomes: Mapping[str, int]
    verified_rate: Ratio
    attempts_total: int
    failed_attempt_count: int
    failed_attempt_ids: tuple[str, ...]
    interventions: usage.DimensionTotal
    recoveries: usage.DimensionTotal
    elapsed: usage.DimensionTotal
    total_elapsed_ms: int | None
    median_verified_elapsed_ms: float | None
    strict_median_verified_elapsed_ms: float | None
    usage_record_count: int
    usage_ids: tuple[str, ...]
    usage_totals: usage.UsageTotals
    locality_evidence: Mapping[str, int]
    unattributed_locality_count: int
    locality_violations: tuple[str, ...]
    cloud_input_tokens: usage.DimensionTotal
    cloud_output_tokens: usage.DimensionTotal
    cloud_total_tokens: usage.DimensionTotal
    local_total_tokens: usage.DimensionTotal
    local_token_share: Ratio
    cloud_cost: MetricValue
    verified_by_difficulty: Mapping[str, Ratio]
    data_origins: tuple[str, ...]
    real_count: int
    synthetic_count: int
    intervention_evidence_gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.arm not in ARM_SET:
            raise ValidationError("arm summary arm must be cloud_only or mixed")
        object.__setattr__(self, "outcomes", dict(self.outcomes))
        object.__setattr__(self, "locality_evidence", dict(self.locality_evidence))
        object.__setattr__(self, "verified_by_difficulty",
                          dict(self.verified_by_difficulty))
        for name in ("missing_task_ids", "failed_attempt_ids", "usage_ids",
                     "locality_violations", "data_origins",
                     "intervention_evidence_gaps", "required_localities"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def coverage_complete(self) -> bool:
        return self.tasks_observed == self.tasks_total and self.tasks_total > 0

    def payload(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "required_localities": list(self.required_localities),
            "tasks_total": self.tasks_total,
            "tasks_observed": self.tasks_observed,
            "missing_task_ids": list(self.missing_task_ids),
            "coverage_complete": self.coverage_complete,
            "outcomes": dict(self.outcomes),
            "verified_rate": self.verified_rate.payload(),
            "attempts_total": self.attempts_total,
            "failed_attempt_count": self.failed_attempt_count,
            "failed_attempt_ids": list(self.failed_attempt_ids),
            "interventions": self.interventions.payload(),
            "recoveries": self.recoveries.payload(),
            "elapsed": self.elapsed.payload(),
            "total_elapsed_ms": self.total_elapsed_ms,
            "median_verified_elapsed_ms": self.median_verified_elapsed_ms,
            "strict_median_verified_elapsed_ms": self.strict_median_verified_elapsed_ms,
            "usage_record_count": self.usage_record_count,
            "usage_ids": list(self.usage_ids),
            "usage_totals": self.usage_totals.payload(),
            "locality_evidence": dict(self.locality_evidence),
            "unattributed_locality_count": self.unattributed_locality_count,
            "locality_violations": list(self.locality_violations),
            "cloud_input_tokens": self.cloud_input_tokens.payload(),
            "cloud_output_tokens": self.cloud_output_tokens.payload(),
            "cloud_total_tokens": self.cloud_total_tokens.payload(),
            "local_total_tokens": self.local_total_tokens.payload(),
            "local_token_share": self.local_token_share.payload(),
            "cloud_cost": self.cloud_cost.payload(),
            "verified_by_difficulty": {key: value.payload() for key, value
                                      in self.verified_by_difficulty.items()},
            "data_origins": list(self.data_origins),
            "real_count": self.real_count,
            "synthetic_count": self.synthetic_count,
            "intervention_evidence_gaps": list(self.intervention_evidence_gaps),
        }


@dataclass(frozen=True)
class ComparisonReport:
    """The reduced, digest-stable answer to "cloud-only vs mixed"."""

    plan_id: str
    plan_digest: str
    observed_at: str
    data_class: str
    explanatory_power: str
    arm_summaries: Mapping[str, ArmSummary]
    metrics: tuple[MetricComparison, ...]
    claim_allowed: bool
    blockers: tuple[Blocker, ...]

    def __post_init__(self) -> None:
        if self.data_class not in DATA_ORIGINS | {"mixed", "none"}:
            raise ValidationError("data_class must be real, synthetic, mixed, or none")
        if self.explanatory_power not in {"measured", "partial_measurement",
                                          "synthetic_rehearsal", "mixed_origin",
                                          "empty"}:
            raise ValidationError("unknown explanatory_power state")
        object.__setattr__(self, "arm_summaries", dict(self.arm_summaries))
        if set(self.arm_summaries) != ARM_SET:
            raise ValidationError("a comparison must summarize both arms")
        object.__setattr__(self, "metrics", tuple(self.metrics))
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.claim_allowed and self.blockers:
            raise ValidationError("a claim cannot be allowed while blockers remain")
        if self.claim_allowed and self.data_class != "real":
            raise ValidationError("only all-real evidence may support a claim")

    def summary(self, arm: str) -> ArmSummary:
        if arm not in self.arm_summaries:
            raise ValidationError(f"arm not present in report: {arm}")
        return self.arm_summaries[arm]

    def metric(self, name: str) -> MetricComparison:
        for item in self.metrics:
            if item.name == name:
                return item
        raise ValidationError(f"metric not present in report: {name}")

    @property
    def coverage_complete(self) -> bool:
        return all(summary.coverage_complete for summary in self.arm_summaries.values())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "observed_at": self.observed_at,
            "data_class": self.data_class,
            "explanatory_power": self.explanatory_power,
            "arm_summaries": {arm: summary.payload()
                              for arm, summary in self.arm_summaries.items()},
            "metrics": [item.payload() for item in self.metrics],
            "claim_allowed": self.claim_allowed,
            "blockers": [item.payload() for item in self.blockers],
        }

    @property
    def report_digest(self) -> str:
        return _digest(self.payload())

    @property
    def report_id(self) -> str:
        return "cmp-" + self.report_digest[:40]


# ---------------------------------------------------------------------------
# durable pinning (append-only projection over the event store)
# ---------------------------------------------------------------------------

def _require_pinned_tasks(conn: storage.TeamCraftConnection, project_id: str,
                          plan: ExperimentPlan) -> None:
    """A pinned task set must name real tasks of this project."""

    for pinned in plan.tasks:
        row = conn.execute("SELECT project_id FROM tasks WHERE id = ?",
                          (pinned.task_id,)).fetchone()
        if row is None or str(row[0]) != project_id:
            raise NotFoundError(
                f"pinned task not found in project: {pinned.task_id}")


def pin_plan(conn: storage.TeamCraftConnection, project_id: str,
             plan: ExperimentPlan) -> events.Event:
    """Persist the pinned plan; identical plans replay, drift collides loudly."""

    if not isinstance(plan, ExperimentPlan):
        raise ValidationError("pin_plan requires an ExperimentPlan")
    _require_project(conn, project_id)
    _require_pinned_tasks(conn, project_id, plan)
    payload = {"schema_version": 1, "plan_id": plan.plan_id,
               "plan": plan.payload(), "plan_digest": plan.plan_digest}
    _reject_secrets(payload)
    try:
        event, _inserted = events.append_event_idempotent(
            conn, PLAN_EVENT, project_id=project_id,
            payload_json=_canonical(payload),
            event_id="evt-exp-plan-" + plan.plan_digest[:40])
    except StorageError as exc:
        raise ConflictError(
            f"plan identity {plan.plan_id} already names a different experiment"
        ) from exc
    return event


def load_plan(conn: storage.TeamCraftConnection, project_id: str,
              plan_id: str) -> ExperimentPlan:
    """Load a pinned plan by id and re-verify its digest."""

    require_entity_id(plan_id)
    _require_project(conn, project_id)
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE project_id = ? AND kind = ?"
        " ORDER BY seq ASC", (project_id, PLAN_EVENT)).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row[0]))
            plan = ExperimentPlan.from_payload(payload["plan"])
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise ConflictError("stored experiment plan is malformed") from exc
        if payload.get("plan_digest") != plan.plan_digest:
            raise ConflictError("stored experiment plan digest mismatch")
        if plan.plan_id == plan_id:
            return plan
    raise NotFoundError(f"experiment plan not found: {plan_id}")


def list_plans(conn: storage.TeamCraftConnection,
               project_id: str) -> tuple[ExperimentPlan, ...]:
    """All distinct pinned plans for a project, oldest pin first."""

    _require_project(conn, project_id)
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE project_id = ? AND kind = ?"
        " ORDER BY seq ASC", (project_id, PLAN_EVENT)).fetchall()
    plans: list[ExperimentPlan] = []
    seen: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(str(row[0]))
            plan = ExperimentPlan.from_payload(payload["plan"])
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise ConflictError("stored experiment plan is malformed") from exc
        if payload.get("plan_digest") != plan.plan_digest:
            raise ConflictError("stored experiment plan digest mismatch")
        if plan.plan_id not in seen:
            seen.add(plan.plan_id)
            plans.append(plan)
    return tuple(plans)


def _bind_attempt(conn: storage.TeamCraftConnection, project_id: str,
                  task_id: str, attempt_id: str) -> None:
    row = conn.execute(
        "SELECT t.project_id, a.task_id FROM attempts a JOIN tasks t ON t.id = a.task_id"
        " WHERE a.id = ?", (attempt_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"attempt not found: {attempt_id}")
    if str(row[0]) != project_id or str(row[1]) != task_id:
        raise ValidationError(
            f"attempt {attempt_id} does not belong to task {task_id} of this project")


def record_observation(conn: storage.TeamCraftConnection, project_id: str,
                       plan: ExperimentPlan,
                       observation: TaskObservation) -> events.Event:
    """Append one arm/task observation against a pinned plan.

    Everything the observation points at must already be durable: an unknown
    usage id, an attempt from another task, or a slot outside the pinned
    task/arm matrix fails closed rather than being dropped, so the report can
    only ever be built from evidence that exists.
    """

    if not isinstance(plan, ExperimentPlan):
        raise ValidationError("record_observation requires an ExperimentPlan")
    if not isinstance(observation, TaskObservation):
        raise ValidationError("record_observation requires a TaskObservation")
    _require_project(conn, project_id)
    pinned = _load_plan_event(conn, project_id, plan.plan_id)
    if pinned is None:
        raise NotFoundError(f"experiment plan not pinned: {plan.plan_id}")
    if pinned.plan_digest != plan.plan_digest:
        raise ConflictError("plan supplied for observation differs from the pinned plan")
    if not plan.covers(observation.arm, observation.task_id):
        raise ValidationError(
            f"observation ({observation.arm}, {observation.task_id}) is outside the"
            " pinned task/arm matrix")
    for evidence in observation.attempts:
        _bind_attempt(conn, project_id, observation.task_id, evidence.attempt_id)
    for usage_id in observation.usage_ids:
        record = usage.get_usage(conn, project_id, usage_id)
        if record.task_id is not None and record.task_id != observation.task_id:
            raise ValidationError(
                f"usage record {usage_id} belongs to task {record.task_id},"
                " not the observed task")
    payload = {"schema_version": 1, "plan_id": plan.plan_id,
               "observation_id": observation.observation_id,
               "arm": observation.arm, "task_id": observation.task_id,
               "observation": observation.payload(),
               "observation_digest": observation.observation_digest}
    _reject_secrets(payload)
    try:
        event, _inserted = events.append_event_idempotent(
            conn, OBSERVATION_EVENT, project_id=project_id,
            task_id=observation.task_id, payload_json=_canonical(payload),
            event_id="evt-exp-obs-" + observation.observation_digest[:40])
    except StorageError as exc:
        raise ConflictError(
            f"observation identity {observation.observation_id} already names"
            " different facts") from exc
    return event


def _load_plan_event(conn: storage.TeamCraftConnection, project_id: str,
                     plan_id: str) -> ExperimentPlan | None:
    try:
        return load_plan(conn, project_id, plan_id)
    except NotFoundError:
        return None


def load_observations(conn: storage.TeamCraftConnection, project_id: str,
                      plan_id: str) -> tuple[TaskObservation, ...]:
    """All durable observations pinned to one plan, ordered by (arm, task)."""

    require_entity_id(plan_id)
    _require_project(conn, project_id)
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE project_id = ? AND kind = ?"
        " ORDER BY seq ASC", (project_id, OBSERVATION_EVENT)).fetchall()
    found: list[TaskObservation] = []
    for row in rows:
        try:
            payload = json.loads(str(row[0]))
        except json.JSONDecodeError as exc:
            raise ConflictError("stored observation is malformed") from exc
        if payload.get("plan_id") != plan_id:
            continue
        try:
            observation = TaskObservation.from_payload(payload["observation"])
        except ValidationError as exc:
            raise ConflictError("stored observation is malformed") from exc
        if payload.get("observation_digest") != observation.observation_digest:
            raise ConflictError("stored observation digest mismatch")
        found.append(observation)
    return tuple(sorted(found, key=lambda o: (ARMS.index(o.arm), o.task_id)))


def record_comparison(conn: storage.TeamCraftConnection, project_id: str,
                      report: ComparisonReport) -> events.Event:
    """Keep a durable audit copy of a reduced report (idempotent on digest)."""

    if not isinstance(report, ComparisonReport):
        raise ValidationError("record_comparison requires a ComparisonReport")
    _require_project(conn, project_id)
    payload = {"schema_version": 1, "report_id": report.report_id,
               "plan_id": report.plan_id, "report": report.payload(),
               "report_digest": report.report_digest}
    _reject_secrets(payload)
    try:
        event, _inserted = events.append_event_idempotent(
            conn, COMPARISON_EVENT, project_id=project_id,
            payload_json=_canonical(payload),
            event_id="evt-exp-cmp-" + report.report_digest[:40])
    except StorageError as exc:
        raise ConflictError("comparison report identity collision") from exc
    return event


# ---------------------------------------------------------------------------
# durable fact lookups used by the reducer (read-only)
# ---------------------------------------------------------------------------

def _durable_intervention_count(conn: storage.TeamCraftConnection, project_id: str,
                                task_id: str) -> int:
    """Durable human-intervention evidence for one task.

    Counted as recorded accept/reject decisions on verification results plus
    confirmed cloud-escalation approvals.  This is a *floor* on interventions
    anyone can point at: declaring more interventions than the store can back is
    surfaced as a gap, while declaring fewer is the understatement this check
    exists to catch.
    """

    approvals = int(conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE project_id = ? AND task_id = ?",
        (project_id, task_id)).fetchone()[0])
    rows = conn.execute(
        "SELECT payload_json, result_json FROM commands WHERE kind = ?"
        " AND state = 'confirmed' AND json_extract(payload_json, '$.project_id') = ?"
        " AND json_extract(payload_json, '$.task_id') = ?",
        (scheduler.CLOUD_APPROVAL_COMMAND_KIND, project_id, task_id)).fetchall()
    cloud = 0
    for payload_json, result_json in rows:
        try:
            result = json.loads(str(result_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(result, dict) and result.get("approved") is True:
            cloud += 1
    return approvals + cloud


def _dimension_over(records: Sequence[usage.UsageRecord], name: str) -> usage.DimensionTotal:
    known = [getattr(record, name) for record in records
             if getattr(record, name) is not None]
    return usage.DimensionTotal(
        known_sum=sum(known) if known else None,
        known_count=len(known),
        unknown_count=len(records) - len(known),
    )


def _record_token_total(record: usage.UsageRecord) -> int | None:
    """Best defensible per-record token total — never a guess.

    A reported ``total_tokens`` wins; otherwise input+output when both are
    known; cache is excluded because it may already be inside the input count,
    and anything else stays unknown.
    """

    if record.total_tokens is not None:
        return record.total_tokens
    if record.input_tokens is not None and record.output_tokens is not None:
        return record.input_tokens + record.output_tokens
    return None


def _token_total_over(records: Sequence[usage.UsageRecord]) -> usage.DimensionTotal:
    values = [_record_token_total(record) for record in records]
    known = [value for value in values if value is not None]
    return usage.DimensionTotal(
        known_sum=sum(known) if known else None,
        known_count=len(known),
        unknown_count=len(values) - len(known),
    )


def _cost_value(records: Sequence[usage.UsageRecord]) -> MetricValue:
    """Strict cloud spend over ``records``, only if it is fully attributable.

    Requires: at least one costed record, a cost (or an explicit unknown) on
    every record, one single currency, and a price version on every costed
    record.  Anything less returns None plus the reason codes, because a
    partially priced total is exactly the "50 % saving" mirage the roadmap
    warns about.
    """

    if not records:
        return MetricValue(amount=None, reasons=(BLOCKER_USAGE_UNAVAILABLE,))
    reasons: list[str] = []
    amounts: list[int] = []
    currencies: set[str] = set()
    unknown = 0
    for record in records:
        if record.cost_microunits is None:
            unknown += 1
            continue
        amounts.append(record.cost_microunits)
        if record.currency is None:
            reasons.append(BLOCKER_COST_UNATTRIBUTED)
        else:
            currencies.add(record.currency)
        if record.price_version is None:
            reasons.append(BLOCKER_COST_PRICE_VERSION)
    if unknown:
        reasons.append(BLOCKER_USAGE_PARTIAL)
    if len(currencies) > 1:
        reasons.append(BLOCKER_COST_MIXED_CURRENCY)
    complete = (bool(amounts) and unknown == 0 and len(currencies) == 1
                and BLOCKER_COST_UNATTRIBUTED not in reasons
                and BLOCKER_COST_PRICE_VERSION not in reasons)
    return MetricValue(
        amount=sum(amounts) if complete else None,
        complete=complete,
        unknown_count=unknown,
        reasons=tuple(sorted(set(reasons))),
    )


def _locality_counts(observations: Sequence[TaskObservation],
                     records: Sequence[usage.UsageRecord]) -> tuple[dict[str, int], int]:
    """Where the arm's evidence says work actually ran.

    Attempts (per-attempt locality, the dispatch-time fact) and usage records
    (per-record locality, the billing-side fact) are counted separately and
    merged, so a claim that "everything was cloud" is checked from two sides.
    ``unknown`` counts evidence whose locality nobody recorded.
    """

    counts = {"cloud": 0, "local": 0, "unknown": 0}
    for observation in observations:
        for evidence in observation.attempts:
            counts[evidence.locality or "unknown"] += 1
    for record in records:
        counts[record.locality or "unknown"] += 1
    unattributed = counts["unknown"]
    return counts, unattributed


def _locality_violations(arm: str, counts: Mapping[str, int]) -> tuple[str, ...]:
    required = set(ARM_REQUIRED_LOCALITIES[arm])
    violations: list[str] = []
    for locality in sorted(set(LOCALITIES) - required):
        if counts.get(locality, 0):
            violations.append(f"arm {arm} consumed {locality} capacity")
    for locality in sorted(required):
        if not counts.get(locality, 0):
            violations.append(
                f"arm {arm} shows no {locality} evidence to justify its label")
    return tuple(violations)


# ---------------------------------------------------------------------------
# the reducer
# ---------------------------------------------------------------------------

def _summarize_arm(conn: storage.TeamCraftConnection, project_id: str,
                   plan: ExperimentPlan, arm: str,
                   observations: Sequence[TaskObservation]) -> ArmSummary:
    spec = plan.arm_spec(arm)
    tasks_total = len(plan.tasks)
    observed_ids = {observation.task_id for observation in observations}
    missing = tuple(task_id for task_id in plan.task_ids if task_id not in observed_ids)

    outcomes = {state: 0 for state in sorted(OUTCOME_STATES)}
    for observation in observations:
        outcomes[observation.outcome] += 1

    attempts = [evidence for observation in observations for evidence in observation.attempts]
    failed_ids = tuple(sorted({evidence.attempt_id for evidence in attempts if evidence.failed}))

    interventions = _dimension_over_counts(
        [observation.intervention_count for observation in observations])
    recoveries = _dimension_over_counts(
        [observation.recovery_count for observation in observations])
    elapsed = _dimension_over_counts(
        [observation.elapsed_ms for observation in observations])

    verified_elapsed = [observation.elapsed_ms for observation in observations
                        if observation.outcome == "verified"
                        and observation.elapsed_ms is not None]
    verified_count = outcomes["verified"]
    median_value = float(median(verified_elapsed)) if verified_elapsed else None
    strict_median = median_value if len(verified_elapsed) == tasks_total else None

    usage_ids = sorted({usage_id for observation in observations
                        for usage_id in observation.usage_ids})
    records = [usage.get_usage(conn, project_id, usage_id) for usage_id in usage_ids]
    totals = usage.UsageTotals.compute(records)

    counts, unattributed_locality = _locality_counts(observations, records)
    violations = _locality_violations(arm, counts)
    cloud_records = [record for record in records if record.locality == "cloud"]
    local_records = [record for record in records if record.locality == "local"]
    cloud_total = _token_total_over(cloud_records)
    local_total = _token_total_over(local_records)
    # An arm that demonstrably used no local capacity has a local share of 0.0,
    # not an unknown: "nothing local" is a reported fact once every piece of
    # evidence in the arm carries a locality.  Without that proof the share
    # stays unknown rather than defaulting to zero.
    cloud_amount = cloud_total.strict_total
    if cloud_amount is None and records and not unattributed_locality \
            and not cloud_records:
        cloud_amount = 0
    local_amount = local_total.strict_total
    if local_amount is None and records and not unattributed_locality \
            and not local_records:
        local_amount = 0
    share_denominator = None
    if cloud_amount is not None and local_amount is not None:
        share_denominator = cloud_amount + local_amount
    share = Ratio(
        numerator=local_amount,
        denominator=share_denominator,
        complete=(cloud_amount is not None and local_amount is not None
                  and unattributed_locality == 0 and bool(share_denominator)),
    )

    gaps: list[str] = []
    for observation in observations:
        declared = observation.intervention_count
        if declared is None or declared == 0:
            continue
        durable = _durable_intervention_count(conn, project_id, observation.task_id)
        if durable < declared:
            gaps.append(
                f"{observation.task_id}: declared {declared} intervention(s),"
                f" durable evidence backs {durable}")

    origins = tuple(sorted({observation.data_origin for observation in observations}))
    unknown_outcomes = outcomes["unknown"]
    rate = Ratio(
        numerator=verified_count if observations else None,
        denominator=len(observations) or None,
        complete=(not missing and unknown_outcomes == 0 and bool(observations)),
    )

    by_difficulty: dict[str, Ratio] = {}
    for difficulty in sorted(DIFFICULTIES):
        ids = [task.task_id for task in plan.tasks if task.difficulty == difficulty]
        if not ids:
            continue
        done = [task.task_id for task in plan.tasks
                if task.difficulty == difficulty
                and any(o.task_id == task.task_id and o.outcome == "verified"
                       for o in observations)]
        seen = [task.task_id for task in plan.tasks
                if task.difficulty == difficulty and task.task_id in observed_ids]
        by_difficulty[difficulty] = Ratio(
            numerator=len(done), denominator=len(seen) or None,
            complete=len(seen) == len(ids) and bool(ids))

    total_elapsed = elapsed.strict_total if not missing else None

    return ArmSummary(
        arm=arm,
        required_localities=spec.required_localities,
        tasks_total=tasks_total,
        tasks_observed=len(observations),
        missing_task_ids=missing,
        outcomes=outcomes,
        verified_rate=rate,
        attempts_total=len(attempts),
        failed_attempt_count=len(failed_ids),
        failed_attempt_ids=failed_ids,
        interventions=interventions,
        recoveries=recoveries,
        elapsed=elapsed,
        total_elapsed_ms=total_elapsed,
        median_verified_elapsed_ms=median_value,
        strict_median_verified_elapsed_ms=strict_median,
        usage_record_count=len(records),
        usage_ids=tuple(usage_ids),
        usage_totals=totals,
        locality_evidence=counts,
        unattributed_locality_count=unattributed_locality,
        locality_violations=violations,
        cloud_input_tokens=_dimension_over(cloud_records, "input_tokens"),
        cloud_output_tokens=_dimension_over(cloud_records, "output_tokens"),
        cloud_total_tokens=cloud_total,
        local_total_tokens=local_total,
        local_token_share=share,
        cloud_cost=_cost_value(cloud_records),
        verified_by_difficulty=by_difficulty,
        data_origins=origins,
        real_count=sum(1 for o in observations if o.is_real),
        synthetic_count=sum(1 for o in observations if not o.is_real),
        intervention_evidence_gaps=tuple(gaps),
    )


def _dimension_over_counts(values: Sequence[int | None]) -> usage.DimensionTotal:
    """Null-aware total over observation-level counts (unknown stays unknown)."""

    known = [value for value in values if value is not None]
    return usage.DimensionTotal(
        known_sum=sum(known) if known else None,
        known_count=len(known),
        unknown_count=len(values) - len(known),
    )


_USAGE_METRICS = frozenset({
    "cloud_input_tokens", "cloud_output_tokens", "cloud_total_tokens",
    "cloud_tokens_per_verified_task", "local_token_share",
    "cloud_cost_microunits",
})


def _arm_value(summary: ArmSummary, name: str, *, coverage_complete: bool) -> MetricValue:
    """One arm's strict contribution to one metric, with reason codes.

    Coverage/outcome/locality/intervention reasons apply to every metric: an arm
    whose picture is incomplete cannot produce a comparable figure.  Usage
    reasons attach only to the token/cost metrics — an arm may legitimately have
    no usage telemetry and still have an auditable failed-attempt count.
    """

    usage_metric = name in _USAGE_METRICS
    reasons: list[str] = []
    if summary.missing_task_ids:
        reasons.append(f"{BLOCKER_TASK_COVERAGE}:{len(summary.missing_task_ids)}")
    if summary.outcomes["unknown"]:
        reasons.append(f"{BLOCKER_OUTCOME_UNKNOWN}:{summary.outcomes['unknown']}")
    if summary.locality_violations:
        reasons.append(BLOCKER_LOCALITY_MISMATCH)
    if summary.intervention_evidence_gaps:
        reasons.append(BLOCKER_INTERVENTION_EVIDENCE)
    if usage_metric:
        if summary.usage_record_count == 0:
            reasons.append(BLOCKER_USAGE_UNAVAILABLE)
        if summary.unattributed_locality_count:
            reasons.append(
                f"{BLOCKER_LOCALITY_UNKNOWN}:{summary.unattributed_locality_count}")

    amount: float | int | None = None
    unknown = 0
    if name == "verified_task_count":
        amount = summary.outcomes["verified"] if coverage_complete else None
        unknown = len(summary.missing_task_ids) + summary.outcomes["unknown"]
    elif name == "verified_rate":
        amount = summary.verified_rate.strict_value
        if amount is None:
            reasons.append(BLOCKER_TASK_COVERAGE)
    elif name == "median_verified_elapsed_ms":
        amount = summary.strict_median_verified_elapsed_ms
        unknown = summary.tasks_total - summary.outcomes["verified"]
        if amount is None:
            reasons.append(BLOCKER_TASK_COVERAGE)
    elif name == "total_elapsed_ms":
        amount = summary.total_elapsed_ms
        unknown = summary.elapsed.unknown_count + len(summary.missing_task_ids)
        if amount is None:
            reasons.append(BLOCKER_USAGE_PARTIAL)
    elif name == "cloud_input_tokens":
        amount = summary.cloud_input_tokens.strict_total
        unknown = summary.cloud_input_tokens.unknown_count
    elif name == "cloud_output_tokens":
        amount = summary.cloud_output_tokens.strict_total
        unknown = summary.cloud_output_tokens.unknown_count
    elif name == "cloud_total_tokens":
        amount = summary.cloud_total_tokens.strict_total
        unknown = summary.cloud_total_tokens.unknown_count
    elif name == "cloud_tokens_per_verified_task":
        tokens = summary.cloud_total_tokens.strict_total
        verified = summary.outcomes["verified"] if coverage_complete else None
        amount = (tokens / verified) if tokens is not None and verified else None
        unknown = summary.cloud_total_tokens.unknown_count
        if amount is None:
            if tokens is not None and coverage_complete and verified == 0:
                reasons.append(REASON_NO_VERIFIED_TASK)
            else:
                reasons.append(BLOCKER_USAGE_PARTIAL)
    elif name == "local_token_share":
        amount = summary.local_token_share.strict_value
        if amount is None:
            reasons.append(BLOCKER_USAGE_PARTIAL)
    elif name == "cloud_cost_microunits":
        # The cost value carries its own attribution reasons (currency, price
        # version); the arm-level reasons below still apply on top of them.
        cost = summary.cloud_cost
        amount, unknown = cost.amount, cost.unknown_count
        reasons.extend(cost.reasons)
    elif name == "failed_attempt_count":
        amount = summary.failed_attempt_count if coverage_complete else None
        unknown = len(summary.missing_task_ids)
    elif name == "intervention_count":
        amount = summary.interventions.strict_total if coverage_complete else None
        unknown = summary.interventions.unknown_count + len(summary.missing_task_ids)
        if amount is None:
            reasons.append(REASON_PARTIAL_EVIDENCE)
    elif name == "recovery_count":
        amount = summary.recoveries.strict_total if coverage_complete else None
        unknown = summary.recoveries.unknown_count + len(summary.missing_task_ids)
        if amount is None:
            reasons.append(REASON_PARTIAL_EVIDENCE)
    else:  # pragma: no cover - METRIC_NAMES is the single source of truth
        raise ValidationError(f"unknown comparison metric: {name}")
    complete = amount is not None and not reasons
    return MetricValue(amount=amount, complete=complete, unknown_count=unknown,
                       reasons=tuple(sorted(set(reasons))))


def _compare(name: str, baseline: MetricValue, other: MetricValue,
             grade_ok: bool, grade_reasons: tuple[str, ...]) -> MetricComparison:
    reasons = tuple(f"cloud_only:{item}" for item in baseline.reasons) \
        + tuple(f"mixed:{item}" for item in other.reasons) + grade_reasons
    # Both arms must have produced a complete, gap-free figure for the same
    # metric.  A number that exists but carries a known gap is shown (amount is
    # not hidden) yet is never turned into a delta anyone could quote.
    comparable = baseline.complete and other.complete and grade_ok
    delta: float | int | None = None
    relative: float | None = None
    better: str | None = None
    if comparable:
        delta = other.amount - baseline.amount
        if isinstance(delta, float) or isinstance(baseline.amount, float):
            delta = float(delta)
        if baseline.amount == 0:
            # A known-zero baseline: an absolute delta is meaningful, a
            # percentage is not (and "infinite saving" is not a result).
            reasons += (REASON_BASELINE_ZERO,)
        else:
            relative = delta / abs(baseline.amount)
        if delta == 0:
            better = "tie"
        elif METRIC_DIRECTIONS[name] == "lower_is_better":
            better = "mixed" if delta < 0 else "cloud_only"
        elif METRIC_DIRECTIONS[name] == "higher_is_better":
            better = "mixed" if delta > 0 else "cloud_only"
        # ``informational`` metrics deliberately never name a winner.
    return MetricComparison(
        name=name, direction=METRIC_DIRECTIONS[name], cloud_only=baseline,
        mixed=other, delta=delta, relative=relative, comparable=comparable,
        better_arm=better, reasons=tuple(reasons))


def reduce_comparison(conn: storage.TeamCraftConnection, project_id: str,
                      plan: ExperimentPlan,
                      observations: Iterable[TaskObservation], *,
                      observed_at: str | None = None,
                      clock: Clock | None = None) -> ComparisonReport:
    """Reduce arm/task observations into the pinned, comparable report.

    Deterministic: the same durable evidence, in any arrival order, reduces to
    the same ``report_digest``.  Pass an explicit ``observed_at`` (or a fixed
    clock) when you want the digest itself to be reproducible across runs.
    """

    if not isinstance(plan, ExperimentPlan):
        raise ValidationError("reduce_comparison requires an ExperimentPlan")
    _require_project(conn, project_id)
    stamp = observed_at if observed_at is not None else _iso(_now(clock))
    _parse_time(stamp, "observed_at")

    bound: dict[str, list[TaskObservation]] = {arm: [] for arm in ARMS}
    seen: set[tuple[str, str]] = set()
    for observation in observations:
        if not isinstance(observation, TaskObservation):
            raise ValidationError("observations must be TaskObservation entries")
        if observation.arm not in ARM_SET:
            raise ValidationError(
                f"observation arm is not pinned by this experiment: {observation.arm}")
        if plan.task(observation.task_id) is None:
            raise ValidationError(
                f"observation task is outside the pinned task set: {observation.task_id}")
        key = (observation.arm, observation.task_id)
        if key in seen:
            raise ConflictError(
                f"duplicate observation for ({observation.arm}, {observation.task_id})")
        seen.add(key)
        for evidence in observation.attempts:
            _bind_attempt(conn, project_id, observation.task_id, evidence.attempt_id)
        for usage_id in observation.usage_ids:
            record = usage.get_usage(conn, project_id, usage_id)
            if record.task_id is not None and record.task_id != observation.task_id:
                raise ValidationError(
                    f"usage record {usage_id} belongs to task {record.task_id},"
                    " not the observed task")
        bound[observation.arm].append(observation)

    summaries = {arm: _summarize_arm(conn, project_id, plan, arm, bound[arm])
                 for arm in ARMS}

    origins = {origin for summary in summaries.values() for origin in summary.data_origins}
    if not origins:
        data_class = "none"
    elif origins == {"real"}:
        data_class = "real"
    elif origins == {"synthetic"}:
        data_class = "synthetic"
    else:
        data_class = "mixed"

    grade_reasons: tuple[str, ...] = ()
    if data_class == "synthetic":
        grade_reasons = (BLOCKER_SYNTHETIC,)
    elif data_class == "mixed":
        grade_reasons = (BLOCKER_MIXED_ORIGINS, REASON_GRADE_MISMATCH)

    coverage_complete = all(summary.coverage_complete for summary in summaries.values())
    metrics = tuple(
        _compare(
            name,
            _arm_value(summaries["cloud_only"], name,
                       coverage_complete=coverage_complete),
            _arm_value(summaries["mixed"], name,
                       coverage_complete=coverage_complete),
            grade_ok=data_class in {"real", "synthetic"},
            grade_reasons=grade_reasons)
        for name in METRIC_NAMES)

    blockers: list[Blocker] = []

    def _add(code: str, arm: str | None, detail: str) -> None:
        item = Blocker(code=code, arm=arm, detail=detail)
        if not any(existing.payload() == item.payload() for existing in blockers):
            blockers.append(item)

    for arm in ARMS:
        summary = summaries[arm]
        if summary.missing_task_ids:
            _add(BLOCKER_TASK_COVERAGE, arm,
                 f"{len(summary.missing_task_ids)} pinned task(s) unobserved:"
                 f" {list(summary.missing_task_ids)}")
        if summary.outcomes["unknown"]:
            _add(BLOCKER_OUTCOME_UNKNOWN, arm,
                 f"{summary.outcomes['unknown']} task(s) have an unknown outcome;"
                 " unknown is not counted as failure or success")
        if summary.usage_record_count == 0:
            _add(BLOCKER_USAGE_UNAVAILABLE, arm,
                 "no durable usage record backs this arm; cost and token totals"
                 " are unavailable, not zero")
        elif any(getattr(summary, dim).unknown_count
                 for dim in ("cloud_input_tokens", "cloud_output_tokens")):
            _add(BLOCKER_USAGE_PARTIAL, arm,
                 "some cloud usage records reported no token quantity")
        if summary.unattributed_locality_count:
            _add(BLOCKER_LOCALITY_UNKNOWN, arm,
                 f"{summary.unattributed_locality_count} evidence item(s) recorded"
                 " no locality; they cannot be attributed to cloud or local")
        for violation in summary.locality_violations:
            _add(BLOCKER_LOCALITY_MISMATCH, arm, violation)
        for gap in summary.intervention_evidence_gaps:
            _add(BLOCKER_INTERVENTION_EVIDENCE, arm, gap)
        for reason in summary.cloud_cost.reasons:
            if reason == BLOCKER_USAGE_UNAVAILABLE:
                continue  # already reported more precisely by the usage check
            _add(reason, arm,
                 f"cloud spend for this arm is not totalable ({reason}); it is"
                 " reported as unknown, never as zero")

    if data_class == "synthetic":
        _add(BLOCKER_SYNTHETIC, None,
             "every observation is synthetic; this run rehearses the method and"
             " measures nothing")
    elif data_class == "mixed":
        _add(BLOCKER_MIXED_ORIGINS, None,
             "arms mix real and synthetic observations; no comparable measurement"
             " exists between them")
    elif data_class == "none":
        _add(BLOCKER_TASK_COVERAGE, None,
             "no observation was recorded for this plan")

    claim_allowed = data_class == "real" and not blockers

    if data_class == "none":
        power = "empty"
    elif data_class == "mixed":
        power = "mixed_origin"
    elif data_class == "synthetic":
        power = "synthetic_rehearsal"
    else:
        power = "measured" if claim_allowed else "partial_measurement"

    return ComparisonReport(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        observed_at=stamp,
        data_class=data_class,
        explanatory_power=power,
        arm_summaries=summaries,
        metrics=metrics,
        claim_allowed=claim_allowed,
        blockers=tuple(blockers),
    )


def run_comparison(conn: storage.TeamCraftConnection, project_id: str,
                   plan_id: str, *, observed_at: str | None = None,
                   clock: Clock | None = None) -> ComparisonReport:
    """Reduce whatever is durably pinned and observed for ``plan_id``."""

    plan = load_plan(conn, project_id, plan_id)
    return reduce_comparison(conn, project_id, plan, load_observations(conn, project_id, plan_id),
                            observed_at=observed_at, clock=clock)


# ---------------------------------------------------------------------------
# what a live run still needs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Requirement:
    """One unresolved prerequisite for turning a plan into a live run.

    ``authorization_required`` marks the items that need a human decision or a
    credential this codebase will not read or invent (cloud API authorization,
    escalation approval).  Nothing here claims any of them is satisfied without
    durable evidence in the store.
    """

    kind: str
    arm: str | None
    subject: str
    status: str
    authorization_required: bool
    blocking: bool
    action: str

    def __post_init__(self) -> None:
        if self.kind not in REQUIREMENT_KINDS:
            raise ValidationError(f"unknown requirement kind: {self.kind}")
        if self.arm is not None and self.arm not in ARM_SET:
            raise ValidationError("requirement arm must be cloud_only or mixed")
        if self.status not in REQUIREMENT_STATUSES:
            raise ValidationError(f"unknown requirement status: {self.status}")
        _safe_text(self.subject, "requirement subject")
        _safe_text(self.action, "requirement action")
        for flag in ("authorization_required", "blocking"):
            if not isinstance(getattr(self, flag), bool):
                raise ValidationError(f"{flag} must be a boolean")

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "arm": self.arm,
            "subject": self.subject,
            "status": self.status,
            "authorization_required": self.authorization_required,
            "blocking": self.blocking,
            "action": self.action,
        }


@dataclass(frozen=True)
class RequirementsReport:
    plan_id: str
    plan_digest: str
    checked_at: str
    live_ready: bool
    requirements: tuple[Requirement, ...]

    def __post_init__(self) -> None:
        _parse_time(self.checked_at, "checked_at")
        object.__setattr__(self, "requirements", tuple(self.requirements))
        expected = not any(item.blocking and item.status != "satisfied"
                           for item in self.requirements)
        if self.live_ready != expected:
            raise ValidationError(
                "live_ready must be False while any blocking requirement is unresolved")

    def pending(self) -> tuple[Requirement, ...]:
        return tuple(item for item in self.requirements
                     if item.blocking and item.status != "satisfied")

    def authorizations(self) -> tuple[Requirement, ...]:
        return tuple(item for item in self.requirements
                     if item.authorization_required and item.status != "satisfied")

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "checked_at": self.checked_at,
            "live_ready": self.live_ready,
            "requirements": [item.payload() for item in self.requirements],
            "pending": [item.payload() for item in self.pending()],
            "authorizations_required": [item.payload() for item in self.authorizations()],
        }

    @property
    def requirements_digest(self) -> str:
        return _digest(self.payload())


def _probe_status(existing: providers.CapabilitySnapshot | None, now: datetime) -> str:
    if existing is None:
        return "missing"
    if existing.health in _UNHEALTHY_HEALTH:
        return existing.health if existing.health in REQUIREMENT_STATUSES else "unknown"
    if _parse_time(existing.valid_until, "valid_until") <= now:
        return "stale"
    return "satisfied"


def live_requirements(conn: storage.TeamCraftConnection, project_id: str,
                     plan: ExperimentPlan, *, clock: Clock | None = None
                     ) -> RequirementsReport:
    """What is still required before this plan can be run for real.

    Read-only: it consults recorded M5-01 probes, M5-04 price snapshots, and
    durable cloud-approval commands.  It never probes, never authorizes, and
    never assumes a provider is reachable because a plan mentions it.
    """

    if not isinstance(plan, ExperimentPlan):
        raise ValidationError("live_requirements requires an ExperimentPlan")
    _require_project(conn, project_id)
    now = _now(clock)
    probes = {(snapshot.entity_type, snapshot.name): snapshot
              for snapshot in providers.latest_capability_probes(conn, project_id)}
    requirements: list[Requirement] = []

    for spec in plan.arms:
        arm = spec.arm
        harness_status = _probe_status(probes.get(("harness", spec.harness)), now)
        requirements.append(Requirement(
            kind="harness_adapter", arm=arm, subject=spec.harness,
            status=harness_status, authorization_required=False,
            blocking=harness_status != "satisfied",
            action=("live harness adapter reported healthy" if harness_status == "satisfied"
                    else f"run a live '{spec.harness}' capability probe against the real"
                        f" harness (providers.probe_harness) for arm {arm}")))

        for provider_name in spec.providers:
            status = _probe_status(probes.get(("provider", provider_name)), now)
            needs_auth = spec.wants_cloud
            requirements.append(Requirement(
                kind="provider_adapter", arm=arm, subject=provider_name,
                status=status, authorization_required=needs_auth,
                blocking=status != "satisfied",
                action=(f"provider {provider_name} reported healthy"
                       if status == "satisfied" else
                       f"a live {provider_name} provider adapter plus reachable"
                       " endpoint and health evidence are required"
                       + (" (cloud credentials must be authorized by the operator;"
                          " this layer never reads or stores keys)"
                          if needs_auth else ""))))

        for model in spec.models:
            priced = usage.latest_price_snapshot(conn, project_id, model) is not None
            requirements.append(Requirement(
                kind="price_provenance", arm=arm, subject=model,
                status="satisfied" if priced else "missing",
                authorization_required=False, blocking=not priced,
                action=(f"price registered for {model}" if priced else
                       f"register a versioned price snapshot for {model}"
                       " (usage.record_price_snapshot); without it cost stays"
                       " unknown, never zero")))

        if spec.wants_cloud and spec.cloud_escalation_requires_approval:
            unapproved = [task.task_id for task in plan.tasks
                         if _durable_intervention_count_cloud(conn, project_id, task.task_id) == 0]
            status = "satisfied" if not unapproved else "missing"
            requirements.append(Requirement(
                kind="cloud_authorization", arm=arm,
                subject=f"{len(plan.tasks)} pinned task(s)",
                status=status, authorization_required=True, blocking=status != "satisfied",
                action=("durable cloud-escalation approval exists for every pinned task"
                       if status == "satisfied" else
                       f"{len(unapproved)} task(s) still need an explicit"
                       f" confirmed cloud-escalation approval (arm {arm}):"
                       f" {unapproved[:5]}")))
        elif spec.wants_cloud:
            requirements.append(Requirement(
                kind="cloud_authorization", arm=arm,
                subject=f"{len(plan.tasks)} pinned task(s)",
                status="not_required", authorization_required=True, blocking=False,
                action="plan declares cloud_escalation_requires_approval=False;"
                      " operator-supplied cloud credentials are still required"))

    pending = [item for item in requirements if item.blocking and item.status != "satisfied"]
    return RequirementsReport(
        plan_id=plan.plan_id, plan_digest=plan.plan_digest,
        checked_at=_iso(now), live_ready=not pending,
        requirements=tuple(requirements))


def _durable_intervention_count_cloud(conn: storage.TeamCraftConnection,
                                      project_id: str, task_id: str) -> int:
    """Durable confirmed cloud-escalation approvals for one task."""

    rows = conn.execute(
        "SELECT result_json FROM commands WHERE kind = ? AND state = 'confirmed'"
        " AND json_extract(payload_json, '$.project_id') = ?"
        " AND json_extract(payload_json, '$.task_id') = ?",
        (scheduler.CLOUD_APPROVAL_COMMAND_KIND, project_id, task_id)).fetchall()
    count = 0
    for (result_json,) in rows:
        try:
            result = json.loads(str(result_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(result, dict) and result.get("approved") is True:
            count += 1
    return count


__all__ = [
    "ARMS",
    "ARM_REQUIRED_LOCALITIES",
    "ARM_SET",
    "ATTEMPT_RESULTS",
    "AcceptanceRules",
    "ArmSpec",
    "ArmSummary",
    "AttemptEvidence",
    "BLOCKER_COST_MIXED_CURRENCY",
    "BLOCKER_COST_PRICE_VERSION",
    "BLOCKER_COST_UNATTRIBUTED",
    "BLOCKER_INTERVENTION_EVIDENCE",
    "BLOCKER_LOCALITY_MISMATCH",
    "BLOCKER_LOCALITY_UNKNOWN",
    "BLOCKER_MIXED_ORIGINS",
    "BLOCKER_OUTCOME_UNKNOWN",
    "BLOCKER_SYNTHETIC",
    "BLOCKER_TASK_COVERAGE",
    "BLOCKER_USAGE_PARTIAL",
    "BLOCKER_USAGE_UNAVAILABLE",
    "Blocker",
    "COMPARISON_EVENT",
    "ComparisonReport",
    "DIFFICULTIES",
    "DATA_ORIGINS",
    "ExperimentPlan",
    "MetricComparison",
    "MetricValue",
    "OBSERVATION_EVENT",
    "OUTCOME_STATES",
    "PLAN_EVENT",
    "PinnedTask",
    "REQUIREMENT_KINDS",
    "REQUIREMENT_STATUSES",
    "Ratio",
    "REASON_BASELINE_ZERO",
    "REASON_GRADE_MISMATCH",
    "REASON_NO_VERIFIED_TASK",
    "REASON_PARTIAL_EVIDENCE",
    "Requirement",
    "RequirementsReport",
    "TaskObservation",
    "load_observations",
    "load_plan",
    "list_plans",
    "live_requirements",
    "pin_plan",
    "record_comparison",
    "record_observation",
    "reduce_comparison",
    "run_comparison",
]
