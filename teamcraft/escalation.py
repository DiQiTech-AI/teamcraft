"""M5-03 bounded failure escalation, cloud authorization, and Wake budgets.

This is deliberately a pure decision layer, mirroring the M5-02 Router's
contract.  It performs no database reads or writes, no Herdr calls, no
provider or cloud invocation, and no environment or secret lookup.  Callers
(the Scheduler/Wake Dispatcher/CEO command surface) supply facts already
persisted elsewhere and are solely responsible for the durable effects this
module's decisions may authorize:

* Attempt budgets reuse M2-05's finite ``max_attempts`` semantics.  Nothing
  here creates an Attempt; a permitted retry still goes through the existing
  fenced dispatch path, and exhaustion maps to NEEDS_HUMAN (never a further
  implicit attempt).
* UNKNOWN is never an auto-retryable failure (PLAN 5.2, M0 review 8.2-7/11).
  An ``unknown`` Attempt must be settled by the M2-05 reconcile path with
  verified evidence first; this layer refuses both local retry and cloud
  escalation while ambiguity is unresolved, and it never reclassifies UNKNOWN
  as a zero-cost retryable failure.
* Token usage is null-aware (FR-018): unknown usage is never interpreted as
  zero, free, or as full remaining budget.  A budget dimension whose usage
  (or projected call size) is unknown becomes ``unknown``, and cloud
  escalation is conservatively denied on ``unknown`` exactly as on
  ``exceeded`` — while keeping the two states distinct in the audit payload.
  A null limit is "unconstrained", which is distinct from proven headroom and
  is always reported alongside ``usage_known`` so a pass without usage proof
  stays visible.
* Cloud escalation requires BOTH an explicit, target-bound approval AND a
  budget that is not denied by the conservative evaluation.  Without either,
  cloud is disabled and the caller falls back to BLOCKED/NEEDS_HUMAN.
* Wake decisions enforce the M2-09 merge-window deferral under an explicit
  per-window budget.  An urgent item that cannot be sent within budget
  escalates to a human instead of being silently delayed or dropped, and an
  unreachable CEO session is isolated rather than replaced.

Every decision carries stable snake_case reason codes plus a SHA-256 digest
over its canonical JSON payload, so identical inputs replay to identical
audit evidence (same convention as ``router.RouteDecision``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .domain import ATTEMPT_STATUSES, OCCUPYING_ATTEMPT_STATUSES
from .domain import SESSION_STATES, TERMINAL_ATTEMPT_STATUSES
from .errors import ValidationError
from .router import LOCALITIES, RouteSelection

#: Durable final WorkerReport outcomes (worker_reports schema v1).
WORKER_OUTCOMES = frozenset({"completed", "blocked", "failed"})

#: Conservative failure classification used by every decision in this module.
#: ``unknown`` is a first-class classification that never authorizes work;
#: ``unreported`` is the M2-05 "finished without a final report" shape, which
#: requires an explicit human disposition, not an implicit retry.
FAILURE_CLASSIFICATIONS = frozenset(
    {"in_flight", "unknown", "unreported", "completed", "retryable_failure"}
)

#: Budget dimension states.  ``unbounded`` means the operator set no cap for
#: that dimension; it is neither headroom nor consumption.
BUDGET_DIMENSION_STATES = frozenset(
    {"within", "exceeded", "unknown", "unbounded"}
)
BUDGET_AGGREGATE_STATES = frozenset({"within", "exceeded", "unknown"})

#: The canonical evaluation order for budget dimensions.  ``total_tokens`` is
#: intentionally excluded from cap comparison: it overlaps the per-direction
#: quantities and would double-count headroom.
BUDGET_DIMENSIONS = ("input_tokens", "output_tokens", "cost_microunits")

#: Wake priority is the M2-09 integer 0..100 (higher wins).
WAKE_PRIORITY_MIN = 0
WAKE_PRIORITY_MAX = 100
DEFAULT_URGENT_WAKE_PRIORITY = 100

#: Credential-shaped text must never enter an audit payload (router convention).
_SECRET_TEXT = re.compile(
    r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+",
    re.I,
)

#: CEO Session states from which a wake may reach the CEO at all.  Anything
#: else (unknown/missing/exited/starting) is isolated, matching wake.py:
#: this layer never infers readiness and never starts a replacement session.
CEO_SENDABLE_STATES = frozenset({"idle"})
CEO_DEFERRED_STATES = frozenset({"working"})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _nullable_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field_name} must be a non-negative integer or null")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    resolved = _nullable_non_negative_int(value, field_name)
    if resolved is None:
        raise ValidationError(f"{field_name} is required")
    return resolved


def _positive_int(value: Any, field_name: str) -> int:
    resolved = _non_negative_int(value, field_name)
    if resolved < 1:
        raise ValidationError(f"{field_name} must be an integer >= 1")
    return resolved


def _safe_audit_text(value: str, field_name: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must not be blank")
    text = value.strip()
    if len(text.encode("utf-8")) > limit:
        raise ValidationError(f"{field_name} exceeds {limit} bytes")
    if _SECRET_TEXT.search(text):
        raise ValidationError(f"{field_name} contains credential-shaped text")
    return text


def _safe_window_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z", value):
        raise ValidationError("window_id must be a bounded safe identifier")
    return value


# ---------------------------------------------------------------------------
# Token usage and conservative budget evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenUsage:
    """One usage observation where every quantity is null-aware (FR-018).

    A null amount means *unknown* — never zero, never free.  ``total_tokens``
    is carried for audit but never used to substitute missing directional
    quantities.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_microunits: int | None = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens",
                     "cost_microunits"):
            _nullable_non_negative_int(getattr(self, name), f"usage.{name}")
        # Contradictory evidence (a total below the sum of its known parts)
        # must fail loudly instead of quietly inflating headroom.
        if (self.total_tokens is not None
                and (self.input_tokens or 0) + (self.output_tokens or 0)
                > self.total_tokens):
            raise ValidationError("usage.total_tokens contradicts known parts")

    @property
    def is_known(self) -> bool:
        """Whether ANY cap-relevant quantity was actually reported."""
        return any(getattr(self, name) is not None
                   for name in BUDGET_DIMENSIONS)

    def amount(self, dimension: str) -> int | None:
        if dimension not in BUDGET_DIMENSIONS:
            raise ValidationError(f"unknown budget dimension: {dimension}")
        return getattr(self, dimension)

    def payload(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_microunits": self.cost_microunits,
            "known": self.is_known,
        }


@dataclass(frozen=True)
class BudgetLimits:
    """Per-task cloud caps.  ``None`` means *unconstrained*, not unlimited
    proven headroom; the aggregate never claims more than this."""

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_microunits: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens",
                     "max_cost_microunits"):
            _nullable_non_negative_int(getattr(self, name), f"limits.{name}")

    def cap(self, dimension: str) -> int | None:
        return getattr(self, f"max_{dimension}")

    def payload(self) -> dict[str, Any]:
        return {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_microunits": self.max_cost_microunits,
        }


@dataclass(frozen=True)
class BudgetEvaluation:
    """Per-dimension + aggregate budget states under conservative accounting.

    ``aggregate`` collapses in strict severity order ``exceeded > unknown >
    within``; a dimension with no cap is ``unbounded`` and never rescues an
    ``unknown`` or ``exceeded`` sibling.  ``sufficient`` is true only for a
    fully ``within`` aggregate, so unknown usage can never authorize cloud.
    """

    dimensions: Mapping[str, str]
    aggregate: str
    usage_known: bool
    limits: BudgetLimits

    def __post_init__(self) -> None:
        states = dict(self.dimensions)
        if set(states) != set(BUDGET_DIMENSIONS):
            raise ValidationError("budget dimensions must cover the canonical set")
        if any(state not in BUDGET_DIMENSION_STATES for state in states.values()):
            raise ValidationError("budget dimension state is unrecognized")
        if self.aggregate not in BUDGET_AGGREGATE_STATES:
            raise ValidationError("budget aggregate state is unrecognized")
        object.__setattr__(self, "dimensions", states)

    @property
    def sufficient(self) -> bool:
        return self.aggregate == "within"

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "dimensions": dict(self.dimensions),
            "aggregate": self.aggregate,
            "usage_known": self.usage_known,
            "limits": self.limits.payload(),
        }

    @property
    def decision_digest(self) -> str:
        return _digest(self.payload())


def evaluate_budget(limits: BudgetLimits, used: TokenUsage,
                    projected: TokenUsage | None = None) -> BudgetEvaluation:
    """Evaluate cloud headroom without ever assuming a null quantity away.

    For each capped dimension the state is ``exceeded`` when
    ``used + projected > cap``, ``unknown`` when either side is null, and
    ``within`` otherwise.  An uncapped dimension is ``unbounded``.  Passing
    ``projected=None`` means "no further call is being modelled"; it does NOT
    mean the next call is free, and unknown *used* still yields ``unknown``.
    """

    if not isinstance(limits, BudgetLimits):
        raise ValidationError("limits must be a BudgetLimits")
    if not isinstance(used, TokenUsage):
        raise ValidationError("used must be a TokenUsage")
    if projected is not None and not isinstance(projected, TokenUsage):
        raise ValidationError("projected must be a TokenUsage or None")

    dimensions: dict[str, str] = {}
    for name in BUDGET_DIMENSIONS:
        cap = limits.cap(name)
        if cap is None:
            dimensions[name] = "unbounded"
            continue
        used_amount = used.amount(name)
        projected_amount = 0 if projected is None else projected.amount(name)
        if used_amount is None or projected_amount is None:
            dimensions[name] = "unknown"
        elif used_amount + projected_amount > cap:
            dimensions[name] = "exceeded"
        else:
            dimensions[name] = "within"
    states = set(dimensions.values())
    if "exceeded" in states:
        aggregate = "exceeded"
    elif "unknown" in states:
        aggregate = "unknown"
    else:
        aggregate = "within"
    return BudgetEvaluation(dimensions=dimensions, aggregate=aggregate,
                           usage_known=used.is_known, limits=limits)


# ---------------------------------------------------------------------------
# Attempt failure classification and the finite attempt budget
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureFact:
    """The settled facts of the most recent Attempt for one Task.

    ``outcome`` is the durable final WorkerReport outcome when one exists;
    null means no report has been persisted yet.  Contradictory combinations
    (e.g. an outcome on a non-finished Attempt) are rejected at construction
    so a decision can never be built on impossible inputs.
    """

    attempt_status: str
    outcome: str | None = None

    def __post_init__(self) -> None:
        if self.attempt_status not in ATTEMPT_STATUSES:
            raise ValidationError("attempt_status must be a canonical Attempt status")
        if self.outcome is not None and self.outcome not in WORKER_OUTCOMES:
            raise ValidationError("outcome must be a canonical WorkerReport outcome")
        if self.outcome is not None and self.attempt_status != "finished":
            raise ValidationError("an outcome is only meaningful on a finished Attempt")

    @property
    def classification(self) -> str:
        if self.attempt_status == "unknown":
            return "unknown"
        if self.attempt_status in OCCUPYING_ATTEMPT_STATUSES:
            return "in_flight"
        if self.attempt_status == "finished":
            if self.outcome == "completed":
                return "completed"
            if self.outcome in {"blocked", "failed"}:
                return "retryable_failure"
            return "unreported"
        # failed / cancelled: an explicit terminal failure the caller may
        # retry under the finite budget (still approval-gated below).
        return "retryable_failure"

    def payload(self) -> dict[str, Any]:
        return {
            "attempt_status": self.attempt_status,
            "outcome": self.outcome,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class AttemptBudget:
    """Mirror of the M2-05 finite Attempt budget for one Task."""

    max_attempts: int
    attempts_used: int

    def __post_init__(self) -> None:
        self_max = _positive_int(self.max_attempts, "max_attempts")
        used = _non_negative_int(self.attempts_used, "attempts_used")
        if used > self_max:
            raise ValidationError("attempts_used cannot exceed max_attempts")

    @property
    def remaining(self) -> int:
        return self.max_attempts - self.attempts_used

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


ATTEMPT_ACTIONS = frozenset(
    {"none", "wait", "reconcile", "await_approval", "retry", "needs_human"}
)


@dataclass(frozen=True)
class AttemptDecision:
    """Recommendation for the next step against one Task's attempt budget.

    ``new_attempt_permitted`` is the ONLY affirmative signal, and even then
    this layer creates nothing: the caller must still run the fenced dispatch
    path.  UNKNOWN never sets it, and exhaustion never does.
    """

    action: str
    reason_code: str
    new_attempt_permitted: bool
    attempts_remaining: int
    classification: str | None

    def __post_init__(self) -> None:
        if self.action not in ATTEMPT_ACTIONS:
            raise ValidationError("attempt decision action is unrecognized")
        if self.classification is not None \
                and self.classification not in FAILURE_CLASSIFICATIONS:
            raise ValidationError("attempt decision classification is unrecognized")
        if self.new_attempt_permitted and self.action != "retry":
            raise ValidationError("only a retry decision may permit a new attempt")

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "action": self.action,
            "reason_code": self.reason_code,
            "new_attempt_permitted": self.new_attempt_permitted,
            "attempts_remaining": self.attempts_remaining,
            "classification": self.classification,
        }

    @property
    def decision_digest(self) -> str:
        return _digest(self.payload())


def decide_attempt(budget: AttemptBudget, latest: FailureFact | None, *,
                   retry_approved: bool = False) -> AttemptDecision:
    """Decide the bounded next step from the latest Attempt's facts.

    ``retry_approved`` must reflect a durable, human ``recovery.retry_approved``
    Command already recorded through M2-05; it is an input observation, not a
    promise this layer can fulfill.  Ordering is fixed and fail-closed:
    in-flight waits, UNKNOWN reconciles, unreported escalates, exhaustion
    goes to NEEDS_HUMAN, and only an explicit approval opens a bounded retry.
    """

    if not isinstance(budget, AttemptBudget):
        raise ValidationError("budget must be an AttemptBudget")
    if not isinstance(retry_approved, bool):
        raise ValidationError("retry_approved must be boolean")

    def _make(action: str, reason: str, *, permitted: bool = False,
              fact: FailureFact | None = None) -> AttemptDecision:
        return AttemptDecision(
            action=action, reason_code=reason, new_attempt_permitted=permitted,
            attempts_remaining=budget.remaining,
            classification=None if fact is None else fact.classification,
        )

    if latest is None:
        return _make("none", "no_attempt_on_record")
    classification = latest.classification
    if classification == "in_flight":
        return _make("wait", "attempt_in_flight", fact=latest)
    if classification == "unknown":
        # UNKNOWN is never counted as an auto-retryable failure: it holds the
        # slot and only verified reconciliation may settle it (M2-05).
        return _make("reconcile", "unknown_never_auto_retry", fact=latest)
    if classification == "unreported":
        return _make("needs_human", "final_report_missing", fact=latest)
    if classification == "completed":
        # Execution finished; verification/delivery gates decide the Task —
        # a retry here would fabricate a failure that has no evidence.
        return _make("none", "attempt_completed_verify_separately", fact=latest)
    if budget.exhausted:
        return _make("needs_human", "attempt_budget_exhausted", fact=latest)
    if not retry_approved:
        return _make("await_approval", "retry_approval_required", fact=latest)
    return _make("retry", "bounded_retry_authorized", permitted=True, fact=latest)


# ---------------------------------------------------------------------------
# Cloud escalation authorization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CloudApproval:
    """A durable, human cloud-escalation approval bound to one exact route.

    Like ``router.ManualOverride`` the actor/reason pair is bounded audit text
    and credential-shaped content is rejected before it can reach an audit
    payload.
    """

    selection: RouteSelection
    actor: str
    reason: str

    def __post_init__(self) -> None:
        _safe_audit_text(self.actor, "approval actor", limit=128)
        _safe_audit_text(self.reason, "approval reason", limit=512)

    def payload(self) -> dict[str, Any]:
        return {
            "selection": asdict(self.selection),
            "actor": self.actor.strip(),
            "reason": self.reason.strip(),
        }


@dataclass(frozen=True)
class CloudEscalationRequest:
    """All facts required to authorize (or refuse) one cloud invocation."""

    selection: RouteSelection
    locality: str
    limits: BudgetLimits = field(default=BudgetLimits())
    used: TokenUsage = field(default=TokenUsage())
    projected: TokenUsage | None = None
    approval: CloudApproval | None = None
    latest: FailureFact | None = None

    def __post_init__(self) -> None:
        if self.locality not in LOCALITIES:
            raise ValidationError("cloud escalation locality must be local or cloud")
        if not isinstance(self.limits, BudgetLimits):
            raise ValidationError("limits must be a BudgetLimits")
        if not isinstance(self.used, TokenUsage):
            raise ValidationError("used must be a TokenUsage")


CLOUD_ACTIONS = frozenset({"authorized", "denied"})


@dataclass(frozen=True)
class CloudDecision:
    action: str
    reason_code: str
    budget: BudgetEvaluation
    approval_applied: bool
    usage_known: bool

    def __post_init__(self) -> None:
        if self.action not in CLOUD_ACTIONS:
            raise ValidationError("cloud decision action is unrecognized")
        if self.action == "authorized" and (self.budget.sufficient is False
                                            or self.approval_applied is False):
            raise ValidationError("authorized cloud decision requires approval and budget")

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "action": self.action,
            "reason_code": self.reason_code,
            "budget": self.budget.payload(),
            "approval_applied": self.approval_applied,
            "usage_known": self.usage_known,
        }

    @property
    def decision_digest(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True)
class EscalationPolicy:
    """Budget/escalation posture for the pure decision layer.

    * ``cloud_escalation_requires_approval`` defaults to True (PLAN 7.2).
      Disabling it is still not enough to call cloud: budget sufficiency is
      always enforced.
    * ``urgent_priority`` is the M2-09 wake-priority floor for an urgent item.
    * ``allow_urgent_interrupt`` is the dedicated policy M2-09 held back for:
      it must be explicitly enabled before an urgent wake may bypass a
      working-CEO deferral, and even then it respects the wake budget.
    """

    cloud_escalation_requires_approval: bool = True
    urgent_priority: int = DEFAULT_URGENT_WAKE_PRIORITY
    allow_urgent_interrupt: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.cloud_escalation_requires_approval, bool):
            raise ValidationError("cloud_escalation_requires_approval must be boolean")
        if not isinstance(self.allow_urgent_interrupt, bool):
            raise ValidationError("allow_urgent_interrupt must be boolean")
        if (isinstance(self.urgent_priority, bool)
                or not isinstance(self.urgent_priority, int)
                or not WAKE_PRIORITY_MIN <= self.urgent_priority <= WAKE_PRIORITY_MAX):
            raise ValidationError("urgent_priority must be an integer from 0 to 100")


def authorize_cloud_escalation(request: CloudEscalationRequest, *,
                               policy: EscalationPolicy = EscalationPolicy(),
                               ) -> CloudDecision:
    """Gate one cloud invocation on approval AND budget, fail-closed.

    Fixed evaluation order (part of the public contract): locality, unresolved
    UNKNOWN, approval presence, approval/route binding, then the conservative
    budget.  ``denied`` never distinguishes "cheap" from "affordable": on any
    refusal the caller keeps the Task BLOCKED or takes it to NEEDS_HUMAN;
    nothing here calls a provider.
    """

    if not isinstance(request, CloudEscalationRequest):
        raise ValidationError("request must be a CloudEscalationRequest")
    if not isinstance(policy, EscalationPolicy):
        raise ValidationError("policy must be an EscalationPolicy")

    def _denied(reason: str, evaluation: BudgetEvaluation,
               *, approval_applied: bool = False) -> CloudDecision:
        return CloudDecision(
            action="denied", reason_code=reason, budget=evaluation,
            approval_applied=approval_applied,
            usage_known=evaluation.usage_known,
        )

    budget = evaluate_budget(request.limits, request.used, request.projected)
    if request.locality != "cloud":
        return _denied("target_locality_not_cloud", budget)
    if request.latest is not None \
            and request.latest.classification == "unknown":
        # Ambiguity is reconciled locally first; escalating it to a paid
        # cloud run would multiply unknown side effects instead of settling them.
        return _denied("unresolved_unknown_attempt", budget)
    approval_ok = request.approval is not None \
        and request.approval.selection == request.selection
    if policy.cloud_escalation_requires_approval:
        if request.approval is None:
            return _denied("approval_required", budget)
        if not approval_ok:
            return _denied("approval_target_mismatch", budget)
    elif not approval_ok and request.approval is not None:
        # Even with approval not strictly required, a present approval must
        # still bind the exact route it was granted for.
        return _denied("approval_target_mismatch", budget)
    if budget.aggregate == "exceeded":
        return _denied("budget_exceeded", budget, approval_applied=True)
    if budget.aggregate == "unknown":
        # Null usage is conservatively denied — never summed as zero, never
        # assumed free — but stays distinct from a proven exhaustion.
        return _denied("budget_unknown_conservative_denied", budget,
                       approval_applied=True)
    return CloudDecision(
        action="authorized", reason_code="cloud_escalation_authorized",
        budget=budget, approval_applied=request.approval is not None,
        usage_known=budget.usage_known,
    )


# ---------------------------------------------------------------------------
# CEO Wake budget decisions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WakeBudget:
    """Finite CEO wake allowance for one merge window.

    The caller (Wake Dispatcher) tracks the window and supplies the current
    count; this layer never reads clocks or databases.  ``window_id`` binds
    the audit to the persisted window it was evaluated against.
    """

    max_wakes_per_window: int
    wakes_sent_in_window: int
    window_id: str | None = None

    def __post_init__(self) -> None:
        limit = _non_negative_int(self.max_wakes_per_window,
                                 "max_wakes_per_window")
        sent = _non_negative_int(self.wakes_sent_in_window,
                                "wakes_sent_in_window")
        if sent > limit:
            raise ValidationError("wakes_sent_in_window cannot exceed the window limit")
        _safe_window_id(self.window_id)

    @property
    def remaining(self) -> int:
        return self.max_wakes_per_window - self.wakes_sent_in_window

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


@dataclass(frozen=True)
class WakeRequest:
    """One merged wake candidate plus the observable CEO session state."""

    priority: int
    ceo_state: str
    budget: WakeBudget
    coalesced_events: int = 1

    def __post_init__(self) -> None:
        if (isinstance(self.priority, bool)
                or not isinstance(self.priority, int)
                or not WAKE_PRIORITY_MIN <= self.priority <= WAKE_PRIORITY_MAX):
            raise ValidationError("wake priority must be an integer from 0 to 100")
        if self.ceo_state not in SESSION_STATES:
            raise ValidationError("ceo_state must be a canonical Session state")
        if not isinstance(self.budget, WakeBudget):
            raise ValidationError("budget must be a WakeBudget")
        _non_negative_int(self.coalesced_events, "coalesced_events")


WAKE_ACTIONS = frozenset({"deliver", "urgent_bypass", "defer", "needs_human"})


@dataclass(frozen=True)
class WakeDecision:
    """Whether one merged wake may consume budget now, wait, or escalate.

    ``defer`` keeps the wake pending inside its existing M2-09 window — it
    never drops events or advances any cursor.  ``needs_human`` is the
    urgent-but-unaffordable outcome: an urgent event must surface to a person
    rather than be silently delayed behind a budget it cannot pass.
    """

    action: str
    reason_code: str
    urgent: bool
    consumes_budget: bool
    budget_remaining: int

    def __post_init__(self) -> None:
        if self.action not in WAKE_ACTIONS:
            raise ValidationError("wake decision action is unrecognized")
        if self.consumes_budget != (self.action in {"deliver", "urgent_bypass"}):
            raise ValidationError("only delivered wakes may consume wake budget")

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "action": self.action,
            "reason_code": self.reason_code,
            "urgent": self.urgent,
            "consumes_budget": self.consumes_budget,
            "budget_remaining": self.budget_remaining,
        }

    @property
    def decision_digest(self) -> str:
        return _digest(self.payload())


def decide_wake(request: WakeRequest, *,
                policy: EscalationPolicy = EscalationPolicy()) -> WakeDecision:
    """Apply urgent-within-budget rules to one merged wake candidate.

    Ordering (fail-closed): an unreachable CEO isolates first (urgent items
    escalate; ordinary ones defer without touching budget), then a working
    CEO defers unless the dedicated urgent-interrupt policy AND budget allow
    the bypass, then the per-window budget decides delivery — exhaustion
    defers ordinary wakes but pushes urgent wakes to a human.
    """

    if not isinstance(request, WakeRequest):
        raise ValidationError("request must be a WakeRequest")
    if not isinstance(policy, EscalationPolicy):
        raise ValidationError("policy must be an EscalationPolicy")

    urgent = request.priority >= policy.urgent_priority
    budget = request.budget
    remaining = budget.remaining

    def _make(action: str, reason: str) -> WakeDecision:
        return WakeDecision(
            action=action, reason_code=reason, urgent=urgent,
            consumes_budget=action in {"deliver", "urgent_bypass"},
            budget_remaining=remaining,
        )

    if request.ceo_state not in CEO_SENDABLE_STATES | CEO_DEFERRED_STATES:
        # unknown / missing / exited / starting: isolate exactly like the
        # M2-09 admission gate; no replacement session is ever implied.
        if urgent:
            return _make("needs_human", f"ceo_state_{request.ceo_state}")
        return _make("defer", f"ceo_state_{request.ceo_state}")
    if request.ceo_state in CEO_DEFERRED_STATES:
        if urgent and policy.allow_urgent_interrupt:
            if budget.exhausted:
                return _make("needs_human", "urgent_wake_budget_exhausted")
            return _make("urgent_bypass", "urgent_interrupt_within_budget")
        return _make("defer", "ceo_working")
    # CEO idle: the window budget is the final authority.
    if budget.exhausted:
        if urgent:
            return _make("needs_human", "urgent_wake_budget_exhausted")
        return _make("defer", "wake_budget_exhausted")
    return _make("deliver", "wake_within_budget")


__all__ = [
    "ATTEMPT_ACTIONS",
    "AttemptBudget",
    "AttemptDecision",
    "BUDGET_AGGREGATE_STATES",
    "BUDGET_DIMENSIONS",
    "BUDGET_DIMENSION_STATES",
    "BudgetEvaluation",
    "BudgetLimits",
    "CEO_DEFERRED_STATES",
    "CEO_SENDABLE_STATES",
    "CLOUD_ACTIONS",
    "CloudApproval",
    "CloudDecision",
    "CloudEscalationRequest",
    "DEFAULT_URGENT_WAKE_PRIORITY",
    "EscalationPolicy",
    "FAILURE_CLASSIFICATIONS",
    "FailureFact",
    "TokenUsage",
    "WAKE_ACTIONS",
    "WAKE_PRIORITY_MAX",
    "WAKE_PRIORITY_MIN",
    "WakeBudget",
    "WakeDecision",
    "WakeRequest",
    "WORKER_OUTCOMES",
    "authorize_cloud_escalation",
    "decide_attempt",
    "decide_wake",
    "evaluate_budget",
]
