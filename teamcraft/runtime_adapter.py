"""M1-07 Core Runtime Command Service boundary for teamctl.

This is the single authoritative path for runtime state in TeamCraft 2.0
(CEO gate A/B): the CLI's team/member state lives ONLY in
``.teamcraft/teamcraft.db`` (schema v4).  There is deliberately NO JSON
authority, no dual write, no import, no cutover marker, and no rollback:
``fresh_start`` keeps failing closed (bytes preserved) on any non-empty
legacy ``state.json`` it finds.

Responsibilities:

* ``open_runtime`` — the only entry point: runs ``fresh_start`` readiness
  (migrate + verify + reopen) and returns a live connection;
* ``team_view``/``sync_state`` — the frozen compatibility dict view
  (``version/team_id/project_root/herdr_session/created_at/members``) and a
  transactional synchroniser that persists member facts AND maintains the
  domain Task/Attempt/Session lineage AND appends events in ONE
  ``BEGIN IMMEDIATE`` transaction per operation;
* stage commands — every persistent Herdr side effect (pane split / agent
  start / agent prompt) is modelled as a durable ``commands`` row claimed
  BEFORE the effect runs (CEO gate C).  Confirmation requires a still-valid
  leader lease (CEO gate D): a stale leader can never commit success; its
  command is recorded UNKNOWN and must be reconciled from external facts.

This module never invokes Herdr: effect I/O stays in the CLI layer, which
calls back into these primitives.  Tests drive it with temp directories and
synthetic data only.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any, Iterator

from . import commands as command_core
from . import domain
from . import fresh_start
from . import leader
from . import storage
from .errors import IllegalTransitionError, TeamCraftError, ValidationError
from .events import append_event
from .fresh_start import FreshStartReport
from .storage import TeamCraftConnection

RUNTIME_DIRNAME = fresh_start.RUNTIME_DIRNAME
DATABASE_FILENAME = storage.DEFAULT_DATABASE_FILENAME
STATE_VERSION = 1
TERMINAL_PHASES = frozenset({"completed", "released"})
#: The frozen member-phase vocabulary of the CLI contract.  The authoritative
#: store refuses vocabulary drift instead of silently persisting a phase no
#: reader understands.
MEMBER_PHASES = frozenset({
    "reserved", "split_requested", "split_ambiguous", "pane_created",
    "start_requested", "start_ambiguous", "agent_started",
    "prompt_submitting", "prompt_ambiguous", "prompt_submitted",
    "prompt_skipped", "completed", "released",
})
#: Phases that mean "an external effect may already have happened and was
#: not confirmed" — the CLI maps these to their *_ambiguous spellings.
AMBIGUOUS_SUFFIX = "_ambiguous"

#: Member lifecycle phases whose arrival means a dispatch attempt exists.
_ATTEMPT_PHASES = frozenset({
    "split_requested", "split_ambiguous", "pane_created", "start_requested",
    "start_ambiguous", "agent_started", "prompt_submitting",
    "prompt_ambiguous", "prompt_submitted", "prompt_skipped",
})

#: Member live_state vocabulary -> sessions.state vocabulary (M1-04 8.2-4).
_LIVE_TO_SESSION = {
    "working": "working",
    "blocked": "working",   # alive and engaged, not idle
    "idle": "idle",
    "done": "idle",
    "unknown": "unknown",
    "missing": "missing",
}


class RuntimeAdapterError(TeamCraftError):
    """Runtime state violation the CLI surfaces as an actionable error."""


class TeamSessionMismatchError(RuntimeAdapterError):
    """Stored team state belongs to a different Herdr session."""


def open_runtime(
    project_root: str | Path, *, busy_timeout_ms: int = 10000
) -> tuple[TeamCraftConnection, FreshStartReport]:
    """Bring the root to verified head and open it (single readiness gate)."""

    report = fresh_start.initialize_project_root(
        project_root, busy_timeout_ms=busy_timeout_ms
    )
    return connect_runtime(project_root, busy_timeout_ms=busy_timeout_ms), report


def connect_runtime(
    project_root: str | Path, *, busy_timeout_ms: int = 5000
) -> TeamCraftConnection:
    """Open an already-ready runtime database (no migration/verify pass)."""

    return storage.connect(
        Path(project_root) / RUNTIME_DIRNAME / DATABASE_FILENAME,
        busy_timeout_ms=busy_timeout_ms,
    )


@contextlib.contextmanager
def _tx(conn: TeamCraftConnection) -> Iterator[TeamCraftConnection]:
    """Durability gate composed with any outer open transaction (CEO fix).

    Mirrors :func:`teamcraft.domain._tx`: callers (including teamctl's own
    bundling and tests) may hold an outer ``storage.transaction``; the body
    then joins it instead of nesting a BEGIN SQLite forbids.
    """

    if conn.in_transaction:
        yield conn
    else:
        with storage.transaction(conn) as gated:
            yield gated


# ---------------------------------------------------------------------------
# compatibility dict view (frozen shape of the former JSON state)
# ---------------------------------------------------------------------------


def _member_view(row: Any) -> dict[str, Any]:
    return {
        "name": str(row[0]), "role": str(row[1]),
        "responsibility": str(row[2]), "kind": str(row[3]),
        "task": str(row[4]), "ownership": json.loads(str(row[5])),
        "read_only": bool(row[6]), "cwd": str(row[7]),
        "from_pane_id": _none_or(str, row[8]), "pane_id": _none_or(str, row[9]),
        "terminal_id": _none_or(str, row[10]),
        "agent_session": (
            json.loads(str(row[11])) if row[11] is not None else None
        ),
        "agent_args": json.loads(str(row[12])),
        "opencode_config_content": _none_or(str, row[13]),
        "direction": _none_or(str, row[14]),
        "start_timeout_ms": int(row[15]), "phase": str(row[16]),
        "prompt_may_have_been_delivered": bool(row[17]),
        "live_state": _none_or(str, row[18]), "error": _none_or(str, row[19]),
        "task_id": _none_or(str, row[20]), "attempt_id": _none_or(str, row[21]),
        "session_id": _none_or(str, row[22]),
        "created_at": int(row[23]), "updated_at": int(row[24]),
    }


def _none_or(cast: Any, value: Any) -> Any:
    return None if value is None else cast(value)


_MEMBER_COLUMNS = (
    "name, role, responsibility, kind, task, ownership_json, read_only, cwd,"
    " from_pane_id, pane_id, terminal_id, agent_session_json, agent_args_json,"
    " opencode_config_content, direction, start_timeout_ms, phase,"
    " prompt_may_have_been_delivered, live_state, error, task_id, attempt_id,"
    " session_id, created_at, updated_at"
)


def team_view(conn: TeamCraftConnection) -> dict[str, Any] | None:
    """The frozen state dict, or ``None`` when no team exists yet."""

    meta = conn.execute(
        "SELECT team_id, project_root, herdr_session_json, created_at"
        " FROM runtime_meta WHERE id = 1"
    ).fetchone()
    if meta is None:
        return None
    members = {}
    for row in conn.execute(
        f"SELECT {_MEMBER_COLUMNS} FROM members ORDER BY created_at, name"
    ):
        view = _member_view(row)
        members[view["name"]] = view
    return {
        "version": STATE_VERSION,
        "team_id": str(meta[0]),
        "project_root": str(meta[1]),
        "herdr_session": json.loads(str(meta[2])),
        "created_at": int(meta[3]),
        "members": members,
    }


def project_id_for_root(conn: TeamCraftConnection, root: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM projects WHERE repo_path = ?", (root,)
    ).fetchone()
    return None if row is None else str(row[0])


# ---------------------------------------------------------------------------
# transactional sync: members + lineage + events in ONE write transaction
# ---------------------------------------------------------------------------


def sync_state(conn: TeamCraftConnection, state: dict[str, Any],
               *, clock: Any = None) -> None:
    """Persist the compatibility dict authoritatively in one transaction.

    The dict is the CLI's working copy; this function is the ONLY writer.
    It upserts team meta (team identity is immutable once set), upserts
    every member row, and maintains Task/Attempt/Session lineage plus
    ``member.*`` events atomically — a crash anywhere leaves either the old
    or the new full picture, never a partial one.
    """

    session = state.get("herdr_session")
    if not isinstance(session, dict):
        raise ValidationError("state herdr_session must be an object")
    for name, member in state.get("members", {}).items():
        phase = str(member.get("phase", ""))
        if phase not in MEMBER_PHASES:
            raise ValidationError(
                f"member {name!r} has unknown phase {phase!r}; allowed:"
                f" {', '.join(sorted(MEMBER_PHASES))}"
            )
    with _tx(conn):
        meta = conn.execute(
            "SELECT team_id, herdr_session_json FROM runtime_meta WHERE id = 1"
        ).fetchone()
        if meta is None:
            conn.execute(
                "INSERT INTO runtime_meta (id, team_id, project_root,"
                " herdr_session_json, created_at) VALUES (1, ?, ?, ?, ?)",
                (str(state["team_id"]), str(state["project_root"]),
                 json.dumps(session, sort_keys=True),
                 int(state.get("created_at") or time.time())),
            )
            project = domain.create_project(
                conn,
                name=Path(str(state["project_root"])).name or "team",
                repo_path=str(state["project_root"]),
                clock=clock,
            )
        else:
            if str(meta[0]) != str(state["team_id"]):
                raise RuntimeAdapterError(
                    "team identity is immutable once created (stored"
                    f" {meta[0]!r}, attempted {state['team_id']!r})"
                )
            if json.loads(str(meta[1])) != session:
                raise TeamSessionMismatchError(
                    "team state belongs to a different Herdr server session;"
                    " do not adopt its pane or agent names automatically"
                )
        existing = {
            str(row[0]): row
            for row in conn.execute(
                f"SELECT {_MEMBER_COLUMNS} FROM members"
            )
        }
        for name, member in state.get("members", {}).items():
            before = _member_view(existing[name]) if name in existing else None
            _sync_member(conn, state, member, before, clock=clock)
        for vanished in sorted(set(existing) - set(state.get("members", {}))):
            # Deletion is not part of the frozen lifecycle; refuse silently
            # dropping durable history from the authoritative store.
            raise RuntimeAdapterError(
                f"member {vanished!r} cannot disappear from authoritative state"
            )


def _sync_member(conn: TeamCraftConnection, state: dict[str, Any],
                 member: dict[str, Any], before: dict[str, Any] | None,
                 *, clock: Any) -> None:
    name = str(member["name"])
    now = int(time.time())
    row_values = {
        "name": name,
        "role": str(member["role"]),
        "responsibility": str(member["responsibility"]),
        "kind": str(member["kind"]),
        "task": str(member["task"]),
        "ownership_json": json.dumps(list(member["ownership"]),
                                    ensure_ascii=False),
        "read_only": 1 if member["read_only"] else 0,
        "cwd": str(member["cwd"]),
        "from_pane_id": member.get("from_pane_id"),
        "pane_id": member.get("pane_id"),
        "terminal_id": member.get("terminal_id"),
        "agent_session_json": (
            json.dumps(member["agent_session"], sort_keys=True)
            if member.get("agent_session") is not None else None
        ),
        "agent_args_json": json.dumps(list(member.get("agent_args", []))),
        "opencode_config_content": member.get("opencode_config_content"),
        "direction": member.get("direction"),
        "start_timeout_ms": int(member.get("start_timeout_ms", 30_000)),
        "phase": str(member["phase"]),
        "prompt_may_have_been_delivered": (
            1 if member.get("prompt_may_have_been_delivered") else 0
        ),
        "live_state": member.get("live_state"),
        "error": member.get("error"),
        "task_id": member.get("task_id"),
        "attempt_id": member.get("attempt_id"),
        "session_id": member.get("session_id"),
        "created_at": int(member.get("created_at") or now),
        "updated_at": now,
    }
    if before is None:
        conn.execute(
            "INSERT INTO members (name, role, responsibility, kind, task,"
            " ownership_json, read_only, cwd, from_pane_id, pane_id,"
            " terminal_id, agent_session_json, agent_args_json,"
            " opencode_config_content, direction, start_timeout_ms, phase,"
            " prompt_may_have_been_delivered, live_state, error, task_id,"
            " attempt_id, session_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?)",
            tuple(row_values[k] for k in (
                "name", "role", "responsibility", "kind", "task",
                "ownership_json", "read_only", "cwd", "from_pane_id",
                "pane_id", "terminal_id", "agent_session_json",
                "agent_args_json", "opencode_config_content", "direction",
                "start_timeout_ms", "phase", "prompt_may_have_been_delivered",
                "live_state", "error", "task_id", "attempt_id", "session_id",
                "created_at", "updated_at",
            )),
        )
    else:
        conn.execute(
            "UPDATE members SET role = ?, responsibility = ?, kind = ?,"
            " task = ?, ownership_json = ?, read_only = ?, cwd = ?,"
            " from_pane_id = ?, pane_id = ?, terminal_id = ?,"
            " agent_session_json = ?, agent_args_json = ?,"
            " opencode_config_content = ?, direction = ?,"
            " start_timeout_ms = ?, phase = ?,"
            " prompt_may_have_been_delivered = ?, live_state = ?, error = ?,"
            " task_id = ?, attempt_id = ?, session_id = ?, updated_at = ?"
            " WHERE name = ?",
            tuple(row_values[k] for k in (
                "role", "responsibility", "kind", "task", "ownership_json",
                "read_only", "cwd", "from_pane_id", "pane_id", "terminal_id",
                "agent_session_json", "agent_args_json",
                "opencode_config_content", "direction", "start_timeout_ms",
                "phase", "prompt_may_have_been_delivered", "live_state",
                "error", "task_id", "attempt_id", "session_id", "updated_at",
            )) + (name,),
        )
    _maintain_lineage(conn, state, member, before, clock=clock)
    changed: dict[str, Any] = {"phase": row_values["phase"]}
    if before is not None:
        for key in ("pane_id", "terminal_id", "live_state", "error",
                    "prompt_may_have_been_delivered"):
            if before.get(key) != member.get(key):
                changed[key] = member.get(key)
    else:
        changed["reserved"] = True
    append_event(
        conn, f"member.{row_values['phase']}",
        project_id=project_id_for_root(conn, str(state["project_root"])),
        task_id=member.get("task_id"),
        attempt_id=member.get("attempt_id"),
        payload_json=json.dumps({"member": name, **changed},
                               ensure_ascii=False, sort_keys=True, default=str),
        clock=clock,
    )


def _maintain_lineage(conn: TeamCraftConnection, state: dict[str, Any],
                     member: dict[str, Any], before: dict[str, Any] | None,
                     *, clock: Any) -> None:
    """Task/Attempt/Session relations, best-effort-conservative.

    Every step is conditional on the CURRENT domain status: nothing here
    forces an illegal transition; an unavailable lineage step is skipped
    rather than fabricating history (the member rows and events remain the
    compatibility source, the lineage is the M1-04 audit overlay).
    """

    name = str(member["name"])
    project_id = project_id_for_root(conn, str(state["project_root"]))
    if project_id is None:
        return
    task_id = member.get("task_id")
    if task_id is None:
        task = domain.create_task(
            conn, project_id,
            title=f"{name}: {str(member['task'])[:120]}",
            description=str(member["task"]),
            ownership_json=json.dumps(list(member["ownership"]),
                                     ensure_ascii=False),
            clock=clock,
        )
        task_id = task.id
        member["task_id"] = task_id
        conn.execute("UPDATE members SET task_id = ? WHERE name = ?",
                     (task_id, name))
        if task.status == "pending":
            domain.apply_task_event(conn, task_id, "dependencies_satisfied",
                                   expected_version=task.version, clock=clock)
    else:
        task = domain.get_task(conn, task_id)
        desired = json.dumps(list(member["ownership"]), ensure_ascii=False)
        if task.ownership_json != desired and member["phase"] == "reserved":
            domain.update_task(conn, task_id, ownership_json=desired,
                              expected_version=task.version, clock=clock)

    attempt_id = member.get("attempt_id")
    phase = str(member["phase"])
    if attempt_id is None and phase in _ATTEMPT_PHASES:
        task = domain.get_task(conn, task_id)
        if task.status == "ready":
            attempt = domain.create_attempt(conn, task_id,
                                           expected_version=task.version,
                                           worktree_path=str(member["cwd"]),
                                           clock=clock)
            attempt = domain.transition_attempt(conn, attempt.id, "start",
                                               clock=clock)
            member["attempt_id"] = attempt.id
            conn.execute("UPDATE members SET attempt_id = ? WHERE name = ?",
                         (attempt.id, name))
            attempt_id = attempt.id
    if attempt_id is not None:
        attempt = domain.get_attempt(conn, attempt_id)
        if member.get("pane_id") and member.get("session_id") is None:
            native = member.get("agent_session")
            native_id = (
                str(native.get("value"))
                if isinstance(native, dict) and native.get("value") else None
            )
            session = domain.register_session(
                conn, attempt_id, str(member["kind"]),
                native_session_id=native_id, pane_id=str(member["pane_id"]),
                state=_LIVE_TO_SESSION.get(str(member.get("live_state")),
                                          "unknown"),
                clock=clock,
            )
            member["session_id"] = session.id
            conn.execute("UPDATE members SET session_id = ? WHERE name = ?",
                         (session.id, name))
        if member.get("session_id") is not None:
            live = _LIVE_TO_SESSION.get(str(member.get("live_state")))
            if live is not None:
                session = domain.get_session(conn, str(member["session_id"]))
                if session.state != live:
                    try:
                        domain.observe_session(conn, str(member["session_id"]),
                                             live, clock=clock)
                    except IllegalTransitionError:
                        pass  # liveness never resumes by assumption
        attempt = domain.get_attempt(conn, attempt_id)
        if attempt.status == "starting" and phase == "agent_started":
            domain.transition_attempt(conn, attempt_id, "activated",
                                     clock=clock)
        if attempt.status == "active" and phase.endswith(AMBIGUOUS_SUFFIX):
            domain.transition_attempt(conn, attempt_id, "runtime_lost",
                                     clock=clock)
        if phase == "completed" or phase == "released":
            attempt = domain.get_attempt(conn, attempt_id)
            if attempt.status == "active":
                domain.transition_attempt(conn, attempt_id, "finish",
                                        clock=clock)
            elif attempt.status == "starting":
                domain.transition_attempt(conn, attempt_id, "abandon",
                                        clock=clock)
            elif attempt.status == "unknown":
                domain.transition_attempt(conn, attempt_id,
                                        "reconciled_finished", clock=clock)
            if phase == "completed":
                task = domain.get_task(conn, task_id)
                if task.status == "running":
                    domain.apply_task_event(conn, task_id,
                                           "worker_reported_done",
                                           expected_version=task.version,
                                           clock=clock)


# ---------------------------------------------------------------------------
# persistent stage commands (CEO gate C: claim BEFORE the external effect)
# ---------------------------------------------------------------------------

STAGE_SPLIT = "pane.split"
STAGE_START = "agent.start"
STAGE_PROMPT = "agent.prompt"


def latest_stage_command(conn: TeamCraftConnection, member: str,
                        stage: str) -> command_core.Command | None:
    row = conn.execute(
        "SELECT id FROM commands WHERE kind = ? AND"
        " json_extract(payload_json, '$.member') = ?"
        " ORDER BY requested_at DESC, id DESC LIMIT 1",
        (stage, member),
    ).fetchone()
    if row is None:
        return None
    return command_core.get_command(conn, str(row[0]))


def stage_claim(
    conn: TeamCraftConnection,
    *,
    team_id: str,
    member: str,
    stage: str,
    lease: leader.Lease,
    task_id: str | None = None,
    attempt_id: str | None = None,
    payload: dict[str, Any] | None = None,
    clock: Any = None,
) -> command_core.Command:
    """Submit + claim a stage command durably, BEFORE the Herdr effect.

    The idempotency key is deterministic per member/stage/sequence, so a
    crashed caller that replays the identical request converges onto the
    existing row (reused), while any differing replay is a loud conflict.
    A command already in ``running`` (claim survived the crash) is adopted
    as-is; the caller must then reconcile rather than re-execute blindly.
    Evidence binds the review 8.2-6 pairs (task_id+task_version,
    attempt_id+attempt_no) as required by :mod:`teamcraft.commands`.
    """

    payload = dict(payload or {})
    payload.setdefault("member", member)
    evidence: dict[str, Any] = {}
    if task_id is not None:
        evidence["task_id"] = task_id
        evidence["task_version"] = domain.get_task(conn, task_id).version
    if attempt_id is not None:
        attempt = domain.get_attempt(conn, attempt_id)
        evidence["attempt_id"] = attempt_id
        evidence["attempt_no"] = attempt.attempt_no
    with storage.transaction(conn):
        leader.require_lease(conn, lease, clock=clock)
        prior = conn.execute(
            "SELECT COUNT(*) FROM commands WHERE kind = ? AND"
            " json_extract(payload_json, '$.member') = ?",
            (stage, member),
        ).fetchone()
        seq = int(prior[0]) + 1
        key = f"{team_id}:{member}:{stage}:{seq}"
        submission = command_core.submit_command(
            conn,
            idempotency_key=key, kind=stage,
            payload_json=json.dumps(payload, ensure_ascii=False,
                                   sort_keys=True),
            evidence_json=json.dumps(evidence, sort_keys=True),
            clock=clock,
        )
        command = submission.command
        if command.state == "pending":
            command = command_core.claim_command(conn, command.id, clock=clock)
        return command


def stage_confirm(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    lease: leader.Lease,
    evidence: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    clock: Any = None,
) -> command_core.Command:
    """Commit success for a claimed stage command — requires a live lease.

    A stale leader raises :class:`leader.StaleLeaderError` and NOTHING is
    committed as success; the caller must record UNKNOWN instead (the
    external effect's real outcome is then decided by reconciliation).
    ``evidence`` (what was observed to prove success) rides inside the
    durable ``result_json`` because command evidence is bound at submit time.
    """

    payload = {"result": result or {}, "evidence": evidence or {}}
    with storage.transaction(conn):
        leader.require_lease(conn, lease, clock=clock)
        return command_core.confirm_command(
            conn, command_id,
            result_json=json.dumps(payload, ensure_ascii=False,
                                  sort_keys=True, default=str),
            clock=clock,
        )


def stage_unknown(
    conn: TeamCraftConnection, command_id: str, *, note: str,
    evidence: dict[str, Any] | None = None, clock: Any = None,
) -> command_core.Command:
    """Record a fail-safe UNKNOWN outcome (always permitted, lease or not)."""

    audit = note if not evidence else f"{note} | evidence={json.dumps(evidence, sort_keys=True, default=str)}"
    with storage.transaction(conn):
        return command_core.mark_command_unknown(
            conn, command_id, note=audit, clock=clock,
        )


def stage_reconcile(
    conn: TeamCraftConnection, command_id: str, *,
    evidence: dict[str, Any], resolved_ok: bool, note: str,
    clock: Any = None,
) -> command_core.Command:
    """Close a ``running``/``unknown`` command from inspected external facts.

    ``confirmed`` means the facts prove the effect DID happen; ``expired``
    means the facts prove it did not (abandoned/never occurred). UNKNOWN
    never auto-retries: reconciliation records, it does not re-dispatch.
    """

    audit = f"{note} | evidence={json.dumps(evidence, sort_keys=True, default=str)}"
    with storage.transaction(conn):
        if resolved_ok:
            return command_core.reconcile_command(
                conn, command_id, outcome="confirmed", note=audit,
                result_json=json.dumps({"reconciled": True}, sort_keys=True),
                clock=clock,
            )
        return command_core.reconcile_command(
            conn, command_id, outcome="expired", note=audit, clock=clock,
        )
