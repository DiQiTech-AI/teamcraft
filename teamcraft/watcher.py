"""Deterministic, caller-driven observation and progress clocks.

This module has no daemon, timer, dispatch, or Herdr side effect.  A caller
performs any Adapter reads first, creates a :class:`SessionObservation`, then
asks :func:`record_observation` to commit the resulting facts.  Subscription
is intentionally only a capability contract: this build implements polling
semantics and does not claim an unverified subscription is equivalent.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from . import domain, events, storage
from .clock import Clock, SystemClock, require_utc
from .errors import ValidationError
from .ids import require_entity_id
from .storage import TeamCraftConnection


class BriefProvider(Protocol):
    def __call__(self, conn: TeamCraftConnection, attempt_id: str) -> Any: ...


class ProgressProvider(Protocol):
    def __call__(self, conn: TeamCraftConnection, attempt_id: str) -> Any | None: ...


class AdapterProbe(Protocol):
    """Reads an Adapter using only the immutable snapshot supplied by polling."""
    def __call__(self, adapter: Any, session: Mapping[str, Any]) -> SessionObservation | Mapping[str, Any]: ...


@dataclass(frozen=True)
class SessionObservation:
    """A sanitized result of one external probe.

    ``identity_verified`` must mean that the caller checked its terminal,
    pane, and native agent-session identity.  This layer additionally checks
    the pane/native IDs persisted for the Session.  Log bytes are deliberately
    absent: log churn is not an activity or progress signal.
    """

    session_id: str
    state: str = "unknown"
    pane_id: str | None = None
    native_session_id: str | None = None
    agent_id: str | None = None
    process_alive: bool | None = None
    activity_sequence: int | None = None
    identity_verified: bool = False


@dataclass(frozen=True)
class WatchResult:
    attempt_id: str
    session_id: str
    observed_state: str
    identity_verified: bool
    process_alive: bool | None
    agent_active: bool | None
    observed_at: str
    last_activity_at: str | None
    last_progress_at: str | None
    token_usage: int | None
    stalled: bool
    deadline_exceeded: bool
    events_inserted: tuple[str, ...]


def subscription_capability(adapter: Any) -> bool:
    """Return an explicit opt-in capability only; never infer it from methods."""
    return bool(getattr(adapter, "watcher_subscription_capability", False))


def _now(clock: Clock | None) -> datetime:
    active = clock if clock is not None else SystemClock()
    return require_utc(active.now())


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return require_utc(parsed)
    except (TypeError, ValueError, ValidationError):
        return None


def _stable_id(kind: str, payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "watch." + hashlib.sha256((kind + "\0" + canonical).encode()).hexdigest()


def _json(payload: Mapping[str, Any]) -> str:
    # The payload contains IDs, fixed status words, and digests only.  The
    # explicit bound keeps a malformed injected provider from smuggling logs.
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > 4096:
        raise ValidationError("watcher event payload exceeds 4096 bytes")
    return encoded


def _append(
    conn: TeamCraftConnection, kind: str, *, project_id: str, task_id: str,
    attempt_id: str, payload: Mapping[str, Any], clock: Clock | None,
) -> tuple[events.Event, bool]:
    encoded = _json(payload)
    return events.append_event_idempotent(
        conn, kind, project_id=project_id, task_id=task_id,
        attempt_id=attempt_id, payload_json=encoded,
        event_id=_stable_id(kind, payload), clock=clock,
    )


def _default_brief(conn: TeamCraftConnection, attempt_id: str) -> Any:
    # M2-03 owns this import.  Keeping it lazy allows independent delivery and
    # makes a missing/invalid policy fail closed rather than inventing defaults.
    from .briefs import get_attempt_brief
    return get_attempt_brief(conn, attempt_id)


def _default_progress(conn: TeamCraftConnection, attempt_id: str) -> Any | None:
    from .worker_reports import load_validated_progress
    return load_validated_progress(conn, attempt_id)


def _brief_fields(brief: Any, attempt_id: str) -> tuple[str, str, Any, int | None, int]:
    required = ("task_id", "attempt_id", "deadline_at", "no_progress_timeout_seconds", "poll_interval_seconds")
    if any(not hasattr(brief, field) for field in required):
        raise ValidationError("attempt brief is missing watcher policy fields")
    if str(brief.attempt_id) != attempt_id:
        raise ValidationError("attempt brief identity does not match observed attempt")
    timeout = brief.no_progress_timeout_seconds
    interval = brief.poll_interval_seconds
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0):
        raise ValidationError("brief no_progress_timeout_seconds must be a non-negative integer or null")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValidationError("brief poll_interval_seconds must be a positive integer")
    return str(brief.task_id), str(brief.attempt_id), brief.deadline_at, timeout, interval


def _progress_payload(progress: Any, *, task_id: str, attempt_id: str, brief: Any) -> Mapping[str, Any] | None:
    if progress is None:
        return None
    fields = ("task_id", "attempt_id", "task_version", "brief_digest", "milestone_id", "report_digest", "artifact_digest", "recorded_at")
    if any(not hasattr(progress, field) for field in fields):
        raise ValidationError("progress provider returned an invalid ValidatedProgress")
    if str(progress.task_id) != task_id or str(progress.attempt_id) != attempt_id:
        raise ValidationError("validated progress identity mismatch")
    if hasattr(brief, "brief_digest") and str(progress.brief_digest) != str(brief.brief_digest):
        raise ValidationError("validated progress brief digest mismatch")
    recorded = _parse_time(progress.recorded_at)
    if recorded is None:
        raise ValidationError("validated progress recorded_at must be an aware ISO timestamp")
    if isinstance(progress.task_version, bool) or not isinstance(progress.task_version, int) or progress.task_version < 0:
        raise ValidationError("validated progress task_version must be a non-negative integer")
    artifact = progress.artifact_digest
    if artifact is not None and not isinstance(artifact, str):
        raise ValidationError("validated progress artifact_digest must be string or null")
    result = {
        "artifact_digest": artifact, "brief_digest": str(progress.brief_digest),
        "milestone_id": str(progress.milestone_id), "recorded_at": _iso(recorded),
        "report_digest": str(progress.report_digest), "task_version": progress.task_version,
    }
    return result


def _latest_event_time(conn: TeamCraftConnection, attempt_id: str, kind: str) -> str | None:
    row = conn.execute(
        "SELECT occurred_at FROM events WHERE attempt_id = ? AND kind = ? ORDER BY seq DESC LIMIT 1",
        (attempt_id, kind),
    ).fetchone()
    return None if row is None else str(row[0])


def _latest_progress_recorded_at(conn: TeamCraftConnection, attempt_id: str) -> str | None:
    """Return the greatest validated report clock, not the observer clock."""
    latest: datetime | None = None
    for row in conn.execute(
        "SELECT payload_json FROM events WHERE attempt_id = ? AND kind = 'watch.task_progress'",
        (attempt_id,),
    ):
        try:
            candidate = _parse_time(json.loads(str(row[0])).get("recorded_at"))
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate = None
        if candidate is not None and (latest is None or candidate > latest):
            latest = candidate
    return None if latest is None else _iso(latest)


def _identity_ok(session: domain.SessionRecord, attempt: domain.Attempt, observation: SessionObservation) -> bool:
    if not observation.identity_verified:
        return False
    if session.pane_id is not None and observation.pane_id != session.pane_id:
        return False
    if session.native_session_id is not None and observation.native_session_id != session.native_session_id:
        return False
    if attempt.agent_id is not None and observation.agent_id != attempt.agent_id:
        return False
    return True


def _coerce_observation(value: SessionObservation | Mapping[str, Any]) -> SessionObservation:
    if isinstance(value, SessionObservation):
        return value
    if isinstance(value, Mapping):
        try:
            return SessionObservation(**dict(value))
        except TypeError as exc:
            raise ValidationError("invalid session observation") from exc
    raise ValidationError("observation must be SessionObservation or mapping")


def _native_value(value: Any) -> str | None:
    """Extract the only persisted native-session representation we support."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping) and isinstance(value.get("value"), str) and value["value"]:
        return str(value["value"])
    return None


def _default_adapter_probe(adapter: Any, snapshot: Mapping[str, Any]) -> SessionObservation:
    """Conservative typed-Herdr probe for the runtime member/session binding.

    Any missing profile field, Adapter error, or identity disagreement returns
    UNKNOWN.  It never treats a readable log, a pane's existence, or an
    agent name alone as activity.
    """
    sid = str(snapshot["session_id"])
    name, pane_id, terminal_id, native_id = (snapshot.get("member_name"), snapshot.get("pane_id"),
                                               snapshot.get("terminal_id"), snapshot.get("native_session_id"))
    if not all(isinstance(item, str) and item for item in (name, pane_id, terminal_id, native_id)):
        return SessionObservation(session_id=sid)
    try:
        pane = adapter.get_pane(pane_id)
        agent = adapter.get_agent(name)
        process = adapter.process_info(pane_id)
    except Exception:  # Adapter normalizes operational failures; probing must fail closed.
        return SessionObservation(session_id=sid)
    result = process.get("result") if isinstance(process, Mapping) else None
    if not isinstance(pane, Mapping) or not isinstance(agent, Mapping) or not isinstance(result, Mapping):
        return SessionObservation(session_id=sid)
    alive = result.get("alive")
    pane_ok = pane.get("pane_id") == pane_id and pane.get("terminal_id") == terminal_id
    process_ok = result.get("terminal_id") == terminal_id and isinstance(alive, bool)
    agent_ok = agent.get("pane_id") == pane_id and _native_value(agent.get("agent_session")) == native_id
    if not (pane_ok and process_ok and agent_ok):
        return SessionObservation(session_id=sid)
    # Herdr's typed get_agent shape calls this ``agent_status``.  Retain the
    # older aliases for compatible adapters, but never manufacture a state
    # when all three are absent/unknown.
    raw_state = agent.get("agent_status", agent.get("live_state", agent.get("state")))
    state = raw_state if isinstance(raw_state, str) and raw_state in domain.SESSION_STATES else "unknown"
    # A dead process is a liveness fact, not a completion claim.
    if not alive:
        state = "exited"
    return SessionObservation(session_id=sid, state=state, pane_id=pane_id,
        native_session_id=native_id, agent_id=snapshot.get("agent_id"),
        process_alive=alive, identity_verified=True)


def record_observation(
    conn: TeamCraftConnection, attempt_id: str, observation: SessionObservation | Mapping[str, Any], *,
    clock: Clock | None = None, brief_provider: BriefProvider | None = None,
    progress_provider: ProgressProvider | None = None,
) -> WatchResult:
    """Persist one observation and derived clocks, without any external call.

    The injected providers are read before the write transaction.  In a normal
    caller-driven poll, all Herdr Adapter reads likewise happen before this
    function.  Only Session state and append-only event facts are committed.
    """
    attempt_id = str(require_entity_id(attempt_id))
    observed = _coerce_observation(observation)
    if str(require_entity_id(observed.session_id)) != observed.session_id:
        raise ValidationError("invalid session ID")
    if observed.state not in domain.SESSION_STATES:
        raise ValidationError("unknown observed session state")
    if observed.activity_sequence is not None and (isinstance(observed.activity_sequence, bool) or not isinstance(observed.activity_sequence, int) or observed.activity_sequence < 0):
        raise ValidationError("activity_sequence must be a non-negative integer or null")
    now = _now(clock)
    brief = (brief_provider or _default_brief)(conn, attempt_id)
    task_id, _, deadline_raw, no_progress_timeout, _ = _brief_fields(brief, attempt_id)
    supplied_progress = (progress_provider or _default_progress)(conn, attempt_id)
    # M2-03 returns all validated records.  A legacy/provider single record is
    # accepted only for test injection; each fresh record remains its own
    # durable fact and stable digest-keyed event.
    progress_items = () if supplied_progress is None else (supplied_progress if isinstance(supplied_progress, (list, tuple)) else (supplied_progress,))
    progress_payloads = tuple(_progress_payload(item, task_id=task_id, attempt_id=attempt_id, brief=brief) for item in progress_items)
    deadline = _parse_time(deadline_raw) if deadline_raw is not None else None
    if deadline_raw is not None and deadline is None:
        raise ValidationError("brief deadline_at must be an aware ISO timestamp or null")

    inserted: list[str] = []
    with storage.transaction(conn):
        attempt = domain.get_attempt(conn, attempt_id)
        if attempt.task_id != task_id:
            raise ValidationError("attempt brief task does not match persisted attempt")
        session = domain.get_session(conn, observed.session_id)
        if session.attempt_id != attempt_id:
            raise ValidationError("session does not belong to observed attempt")
        identity_ok = _identity_ok(session, attempt, observed)
        state = observed.state if identity_ok else "unknown"
        previous_state = session.state
        # ``observe_session`` owns legal state transitions.  Its state machine
        # deliberately rejects a self-transition, so a duplicate poll updates
        # only the same canonical observation clock in-place; it cannot become
        # activity/progress or a new event.
        if previous_state == state:
            conn.execute("UPDATE sessions SET last_observed_at = ? WHERE id = ?", (_iso(now), session.id))
        else:
            domain.observe_session(conn, session.id, state, clock=clock)

        predecessor = conn.execute(
            "SELECT event_id FROM events WHERE attempt_id = ? AND kind = 'watch.session_observed' "
            "ORDER BY seq DESC LIMIT 1", (attempt_id,)
        ).fetchone()
        # A predecessor makes state transitions a durable chain: A→B can
        # later occur again after B→A without inserting a poll timestamp in
        # the dedupe key.  A concurrent duplicate sees the committed state
        # and therefore does not create a second link.
        base = {"session_id": session.id, "from": previous_state, "to": state,
                "predecessor": None if predecessor is None else str(predecessor[0])}
        if previous_state != state:
            _, did_insert = _append(conn, "watch.session_observed", project_id=domain.get_task(conn, task_id).project_id,
                task_id=task_id, attempt_id=attempt_id, payload=base, clock=clock)
            if did_insert: inserted.append("watch.session_observed")

        sequence_advanced = False
        if identity_ok and observed.activity_sequence is not None:
            row = conn.execute(
                "SELECT payload_json FROM events WHERE attempt_id = ? AND kind = 'watch.agent_activity' ORDER BY seq DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            old_sequence = -1
            if row is not None:
                try: old_sequence = int(json.loads(str(row[0])).get("sequence", -1))
                except (TypeError, ValueError, json.JSONDecodeError): old_sequence = -1
            sequence_advanced = observed.activity_sequence > old_sequence
        state_activity = identity_ok and previous_state != state
        if state_activity or sequence_advanced:
            activity = {"session_id": session.id, "from": previous_state, "to": state,
                        "predecessor": base["predecessor"],
                        "sequence": observed.activity_sequence if sequence_advanced else None}
            _, did_insert = _append(conn, "watch.agent_activity", project_id=domain.get_task(conn, task_id).project_id,
                task_id=task_id, attempt_id=attempt_id, payload=activity, clock=clock)
            if did_insert: inserted.append("watch.agent_activity")

        # This is intentionally not M2-03's final report fact.  It says only
        # that a verified Agent/session is demonstrably executing; no prompt,
        # log byte, pane liveness, or report submission can create it.
        execution_finished = conn.execute(
            "SELECT 1 FROM events WHERE attempt_id = ? AND kind = 'attempt.execution_finished' LIMIT 1",
            (attempt_id,),
        ).fetchone() is not None
        execution_started = conn.execute(
            "SELECT 1 FROM events WHERE attempt_id = ? AND kind = 'attempt.executing' LIMIT 1",
            (attempt_id,),
        ).fetchone() is not None
        # A stale external snapshot may still say working after M2-03 has
        # atomically recorded its final report and finished the Attempt.  Do
        # not let that late observation reverse durable execution ordering.
        if (identity_ok and state == "working" and attempt.status == "active"
                and not execution_finished and not execution_started):
            executing = {"brief_digest": str(brief.brief_digest)}
            _, did_insert = _append(conn, "attempt.executing", project_id=domain.get_task(conn, task_id).project_id,
                task_id=task_id, attempt_id=attempt_id, payload=executing, clock=clock)
            if did_insert: inserted.append("attempt.executing")

        for progress_payload in progress_payloads:
            assert progress_payload is not None
            _, did_insert = _append(conn, "watch.task_progress", project_id=domain.get_task(conn, task_id).project_id,
                task_id=task_id, attempt_id=attempt_id, payload=progress_payload, clock=clock)
            if did_insert: inserted.append("watch.task_progress")

        last_activity = _latest_event_time(conn, attempt_id, "watch.agent_activity")
        last_progress = _latest_progress_recorded_at(conn, attempt_id)
        # No inferred progress: no-progress starts at the known Session birth
        # (or Attempt start when available), while last_progress remains NULL.
        baseline = _parse_time(last_progress) or _parse_time(attempt.started_at) or _parse_time(session.created_at) or now
        stalled = no_progress_timeout is not None and now >= baseline + timedelta(seconds=no_progress_timeout)
        deadline_exceeded = deadline is not None and now >= deadline
        project_id = domain.get_task(conn, task_id).project_id
        if stalled:
            payload = {"baseline_at": _iso(baseline), "timeout_seconds": no_progress_timeout}
            _, did_insert = _append(conn, "watch.no_progress_threshold", project_id=project_id, task_id=task_id,
                attempt_id=attempt_id, payload=payload, clock=clock)
            if did_insert: inserted.append("watch.no_progress_threshold")
        if deadline_exceeded:
            payload = {"deadline_at": _iso(deadline)}
            _, did_insert = _append(conn, "watch.deadline_exceeded", project_id=project_id, task_id=task_id,
                attempt_id=attempt_id, payload=payload, clock=clock)
            if did_insert: inserted.append("watch.deadline_exceeded")

    return WatchResult(
        attempt_id=attempt_id, session_id=observed.session_id, observed_state=state,
        identity_verified=identity_ok, process_alive=observed.process_alive if identity_ok else None,
        agent_active=(state == "working") if identity_ok else None, observed_at=_iso(now),
        last_activity_at=last_activity, last_progress_at=last_progress, token_usage=None,
        stalled=stalled, deadline_exceeded=deadline_exceeded, events_inserted=tuple(inserted),
    )


def poll_once(
    conn: TeamCraftConnection, attempt_id: str,
    observations: Iterable[SessionObservation | Mapping[str, Any]] | None = None, *,
    adapter: Any | None = None, probe: AdapterProbe | None = None, **kwargs: Any,
) -> tuple[WatchResult, ...]:
    """Perform one caller-driven poll, without sleeping or holding DB locks for I/O.

    Passing ``observations`` is the deterministic fake/test path.  The adapter
    path first snapshots Session identity and ``last_observed_at`` in a short
    read, releases SQLite, calls the supplied typed-Adapter probe, then calls
    :func:`record_observation` to re-check identity and commit.  The probe is
    intentionally explicit because the current HerdrAdapter has no verified
    universal session-status response or subscription cursor.
    """
    if observations is not None:
        if adapter is not None or probe is not None:
            raise ValidationError("pass observations or adapter/probe, not both")
        return tuple(record_observation(conn, attempt_id, item, **kwargs) for item in observations)
    if adapter is None:
        raise ValidationError("adapter polling requires an adapter")
    probe = probe or _default_adapter_probe
    attempt_id = str(require_entity_id(attempt_id))
    brief = (kwargs.get("brief_provider") or _default_brief)(conn, attempt_id)
    _, _, _, _, interval = _brief_fields(brief, attempt_id)
    now = _now(kwargs.get("clock"))
    # Read-only snapshot ends before any adapter method runs.
    rows = conn.execute(
        "SELECT s.id, s.attempt_id, s.pane_id, s.native_session_id, s.state, s.last_observed_at, "
        "m.name, m.terminal_id, a.agent_id "
        "FROM sessions AS s LEFT JOIN members AS m ON m.session_id = s.id "
        "JOIN attempts AS a ON a.id = s.attempt_id WHERE s.attempt_id = ? ORDER BY s.id", (attempt_id,)
    ).fetchall()
    results: list[WatchResult] = []
    for row in rows:
        snapshot = dict(zip(("session_id", "attempt_id", "pane_id", "native_session_id", "state", "last_observed_at", "member_name", "terminal_id", "agent_id"), tuple(row)))
        last = _parse_time(snapshot["last_observed_at"])
        if last is not None and now < last + timedelta(seconds=interval):
            continue
        # External I/O belongs exactly here, after the DB snapshot.
        result = probe(adapter, snapshot)
        results.append(record_observation(conn, attempt_id, result, **kwargs))
    return tuple(results)


__all__ = ["SessionObservation", "WatchResult", "poll_once", "record_observation", "subscription_capability"]
