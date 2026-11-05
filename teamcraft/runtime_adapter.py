"""M1-07 Core Runtime Command Service boundary for teamctl.

This is the single authoritative path for runtime state in TeamCraft 2.0
(CEO gate A/B): the CLI's team/member state lives ONLY in
``.teamcraft/teamcraft.db`` (schema v7).  There is deliberately NO JSON
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
import hashlib
import json
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Iterator

from . import commands as command_core
from . import briefs
from . import worker_reports
from . import recovery
from . import ceo_protocol
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


def _utc_iso(clock: Any = None) -> str:
    if clock is not None:
        return clock.now().astimezone(timezone.utc).isoformat()
    return __import__("datetime").datetime.now(timezone.utc).isoformat()


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


def ensure_project(conn: TeamCraftConnection, root: str, *, clock: Any = None) -> str:
    """Idempotently ensure the canonical project row for a runtime root."""
    if not isinstance(root, str) or not root.startswith("/"):
        raise ValidationError("project root must be an absolute canonical path")
    with _tx(conn):
        existing = project_id_for_root(conn, root)
        if existing is not None:
            return existing
        project = domain.create_project(
            conn, name=Path(root).name or "team", repo_path=root, clock=clock,
        )
        return project.id


def _project_id_for_member(conn: TeamCraftConnection, member: str,
                           task_id: str | None = None) -> str:
    row = conn.execute(
        "SELECT task_id FROM members WHERE name=?", (member,)
    ).fetchone()
    if row is None:
        raise RuntimeAdapterError(
            f"member {member!r} is not recorded; refusing project lease scope"
        )
    linked_task = str(row[0]) if row[0] is not None else None
    root = conn.execute("SELECT project_root FROM runtime_meta WHERE id=1").fetchone()
    runtime_project = project_id_for_root(conn, str(root[0])) if root is not None else None
    if runtime_project is None:
        raise RuntimeAdapterError(
            f"cannot determine runtime project for member {member!r}; refusing unfenced command"
        )
    if task_id is not None:
        task = domain.get_task(conn, task_id)
        if linked_task is not None and linked_task != task_id:
            raise RuntimeAdapterError(
                f"member {member!r} is linked to a different Task; refusing lease scope mismatch"
            )
        if task.project_id != runtime_project:
            raise RuntimeAdapterError(
                f"Task {task_id!r} belongs to another project; refusing lease scope mismatch"
            )
        return runtime_project
    if linked_task is not None:
        task_project = domain.get_task(conn, linked_task).project_id
        if task_project != runtime_project:
            raise RuntimeAdapterError(
                f"member {member!r} is linked to a Task in another project"
            )
    return runtime_project


def _project_id_for_command(conn: TeamCraftConnection, command_id: str) -> str:
    row = conn.execute(
        "SELECT evidence_json, payload_json FROM commands WHERE id=?", (command_id,)
    ).fetchone()
    if row is None:
        raise RuntimeAdapterError(f"command not found: {command_id}")
    try:
        evidence, payload = json.loads(str(row[0])), json.loads(str(row[1]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeAdapterError(
            f"command {command_id} has invalid project evidence; refusing lease scope"
        ) from exc
    task_id = evidence.get("task_id") if isinstance(evidence, dict) else None
    member = payload.get("member") if isinstance(payload, dict) else None
    if not isinstance(member, str) or not member:
        raise RuntimeAdapterError(
            f"command {command_id} has no member or Task project binding"
        )
    return _project_id_for_member(
        conn, member, str(task_id) if task_id is not None else None,
    )


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
    members = state.get("members", {})
    if not isinstance(members, dict):
        raise ValidationError("state members must be an object keyed by member name")
    for name, member in members.items():
        if not isinstance(name, str) or not isinstance(member, dict):
            raise ValidationError(
                "state members must map string names to member objects"
            )
        if member.get("name") != name:
            raise RuntimeAdapterError(
                f"member map key {name!r} does not match the member record"
                f" identity {member.get('name')!r}; refusing mismatched state"
            )
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
            ensure_project(conn, str(state["project_root"]), clock=clock)
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
        if task.project_id != project_id:
            raise RuntimeAdapterError(
                f"member {member.get('name')!r} Task belongs to another project"
            )
        desired = json.dumps(list(member["ownership"]), ensure_ascii=False)
        if task.ownership_json != desired and member["phase"] == "reserved":
            domain.update_task(conn, task_id, ownership_json=desired,
                              expected_version=task.version, clock=clock)

    attempt_id = member.get("attempt_id")
    phase = str(member["phase"])
    if attempt_id is None and phase in _ATTEMPT_PHASES:
        task = domain.get_task(conn, task_id)
        if task.status == "ready":
            protocol = _discover_member_protocol(state, member)
            attempt = domain.create_attempt(conn, task_id,
                                           expected_version=task.version,
                                           worktree_path=str(member["cwd"]),
                                           protocol_version=(protocol.version if protocol else None),
                                           protocol_hash=(protocol.sha256 if protocol else None),
                                           clock=clock)
            attempt = domain.transition_attempt(conn, attempt.id, "start",
                                               clock=clock)
            member["attempt_id"] = attempt.id
            conn.execute("UPDATE members SET attempt_id = ? WHERE name = ?",
                         (attempt.id, name))
            attempt_id = attempt.id
    if attempt_id is not None:
        attempt = domain.get_attempt(conn, attempt_id)
        if attempt.task_id != task_id:
            raise RuntimeAdapterError(
                f"member {name!r} Attempt {attempt_id!r} is not bound to Task {task_id!r}"
            )
        if before is None and phase == "reserved" and (
            task.status != "running" or attempt.status != "created"
        ):
            raise RuntimeAdapterError(
                "a pre-reserved member binding requires the Task to be running"
                " with its reserved Attempt still created"
            )
        _verify_member_protocol(state, member, attempt)
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
        if attempt.status == "created" and phase == "start_requested":
            domain.transition_attempt(conn, attempt_id, "start", clock=clock)
            attempt = domain.get_attempt(conn, attempt_id)
        if attempt.status == "starting" and phase == "agent_started":
            # Spawned is enough for the historical slot transition, but not
            # evidence of work. M2-04 alone emits attempt.executing after a
            # verified working observation.
            domain.transition_attempt(conn, attempt_id, "activated", clock=clock)
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
                # A member phase is not external proof. UNKNOWN may only be
                # settled by the fenced recovery API after reconciliation.
                pass
            if phase == "completed":
                task = domain.get_task(conn, task_id)
                if task.status == "running":
                    domain.apply_task_event(conn, task_id,
                                           "worker_reported_done",
                                           expected_version=task.version,
                                           clock=clock)


def _discover_member_protocol(state: dict[str, Any], member: dict[str, Any]):
    """Return the current immutable CEO protocol snapshot for new Attempts."""
    if member.get("role") != "ceo":
        return None
    try:
        return ceo_protocol.discover_protocol(
            str(state["project_root"]), str(member["kind"]),
        )
    except ceo_protocol.ProtocolError as exc:
        raise RuntimeAdapterError(f"NEEDS_HUMAN: CEO protocol cannot be pinned: {exc}") from exc


def _verify_member_protocol(state: dict[str, Any], member: dict[str, Any], attempt: Any) -> None:
    """Fail closed before further lifecycle progress if a CEO pin is absent or changed."""
    if member.get("role") != "ceo":
        return
    if not attempt.protocol_version or not attempt.protocol_hash:
        raise RuntimeAdapterError(
            "NEEDS_HUMAN: active CEO Attempt has no protocol pin; inspect and reinitialize explicitly"
        )
    try:
        ceo_protocol.discover_protocol(
            str(state["project_root"]), str(member["kind"]),
            expected_version=attempt.protocol_version,
            expected_sha256=attempt.protocol_hash,
        )
    except ceo_protocol.ProtocolError as exc:
        raise RuntimeAdapterError(f"NEEDS_HUMAN: pinned CEO protocol unavailable: {exc}") from exc


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


def prepare_attempt_brief(conn: TeamCraftConnection, member: str, *, clock: Any = None) -> briefs.AttemptBrief:
    """Create/read the full dispatch brief before a prompt Command is claimed."""
    row = conn.execute("SELECT task_id, attempt_id, session_id, role, responsibility, ownership_json, read_only, task FROM members WHERE name=?", (member,)).fetchone()
    if row is None or row[0] is None or row[1] is None:
        raise RuntimeAdapterError("versioned prompt requires member Task and Attempt")
    task = domain.get_task(conn, str(row[0]))
    acceptance = json.loads(task.acceptance_json)
    milestones = ([{"id": f"acceptance-{i + 1}"} for i, _ in enumerate(acceptance)]
                  if isinstance(acceptance, list) and acceptance else [{"id": "final"}])
    content = json.dumps({"task": {"id": task.id, "title": task.title, "description": task.description,
        "acceptance": acceptance, "version": task.version}, "milestones": milestones,
        "attempt_id": str(row[1]), "session_id": row[2], "role": row[3],
        "responsibility": row[4], "ownership": json.loads(str(row[5])),
        "read_only": bool(row[6]), "assignment": row[7],
        "allowed_commands": "only project-approved commands; do not broaden ownership",
        "budget": "usage may be unknown (NULL), never report it as zero"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        brief = briefs.get_attempt_brief(conn, str(row[1]))
        if domain.get_task(conn, brief.task_id).version != brief.task_version:
            raise RuntimeAdapterError("Task evidence version changed; version-bound prompt requires a new Attempt")
        return brief
    except Exception as exc:
        from .errors import NotFoundError
        if not isinstance(exc, NotFoundError): raise
    return briefs.create_attempt_brief(conn, str(row[1]), content=content, clock=clock)


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
    member_row = conn.execute(
        "SELECT role, kind FROM members WHERE name = ?", (member,)
    ).fetchone()
    if member_row is not None and str(member_row[0]) == "ceo":
        row = conn.execute(
            "SELECT project_root FROM runtime_meta WHERE id = 1"
        ).fetchone()
        if row is None or attempt_id is None:
            raise RuntimeAdapterError(
                "NEEDS_HUMAN: CEO dispatch has no protocol-pinned Attempt"
            )
        attempt = domain.get_attempt(conn, attempt_id)
        _verify_member_protocol(
            {"project_root": str(row[0])},
            {"role": str(member_row[0]), "kind": str(member_row[1])}, attempt,
        )
    prompt_brief = None
    if stage == STAGE_PROMPT:
        # Legacy callers may use a generic prompt command before they have
        # domain lineage.  Preserve that compatibility, but never pretend it
        # is a version-bound M2-03 delivery.
        if task_id is not None and attempt_id is not None:
        # The caller may pass the fully rendered legacy text; it is captured
        # once and thereafter cannot be silently replaced by a later task edit.
            try:
                prompt_brief = briefs.get_attempt_brief(conn, attempt_id)
            except Exception as exc:
                from .errors import NotFoundError
                if not isinstance(exc, NotFoundError): raise
                content = payload.pop("brief_content", None) or str(payload.get("prompt_text", ""))
                if not content: content = f"Task {task_id}; member {member}; assignment is recorded in runtime state."
                prompt_brief = briefs.create_attempt_brief(conn, attempt_id, content=content, clock=clock)
            payload["attempt_id"] = attempt_id
            payload["task_version"] = prompt_brief.task_version
            payload["brief_version"] = prompt_brief.brief_version
            payload["brief_digest"] = prompt_brief.brief_digest
            payload["evidence_version"] = prompt_brief.task_version
    if task_id is not None:
        evidence["task_id"] = task_id
        evidence["task_version"] = domain.get_task(conn, task_id).version
    if attempt_id is not None:
        attempt = domain.get_attempt(conn, attempt_id)
        evidence["attempt_id"] = attempt_id
        evidence["attempt_no"] = attempt.attempt_no
    if prompt_brief is not None:
        evidence["digest"] = prompt_brief.brief_digest
        if evidence["task_version"] != prompt_brief.task_version:
            raise RuntimeAdapterError("Task evidence version changed; refusing stale Brief prompt")
    with _tx(conn):
        project_id = _project_id_for_member(conn, member, task_id)
        leader.require_project_lease(conn, lease, project_id, clock=clock)
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
        if prompt_brief is not None:
            session = conn.execute("SELECT session_id FROM members WHERE name=?", (member,)).fetchone()
            conn.execute("INSERT INTO prompt_deliveries (command_id, attempt_id, session_id, task_version, brief_version, brief_digest, evidence_version, state, updated_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', ?, NULL)",
                (command.id, attempt_id, None if session is None else session[0], prompt_brief.task_version,
                 prompt_brief.brief_version, prompt_brief.brief_digest, prompt_brief.task_version,
                 _utc_iso(clock)))
        return command


def record_prompt_delivery(conn: TeamCraftConnection, command_id: str, state: str, *,
                           note: str | None = None, clock: Any = None) -> bool:
    """Record delivery facts separately from generic Command execution state.

    Herdr currently exposes no authenticated target-session receipt.  Callers
    may therefore record ``submitted`` or ``unknown`` but cannot manufacture
    ``acknowledged`` from an idle/done observation or a prompt return.
    """
    if state not in {"pending", "sending", "submitted", "acknowledged", "unknown"}:
        raise ValidationError("unknown prompt delivery state")
    if state == "acknowledged":
        raise ValidationError("Herdr has no verified prompt acknowledgement capability")
    with _tx(conn):
        row = conn.execute("SELECT state FROM prompt_deliveries WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            return False  # legacy/generic prompt has no versioned delivery row
        old = str(row[0])
        if old in {"submitted", "unknown", "acknowledged"} and old != state:
            raise RuntimeAdapterError("terminal/ambiguous prompt delivery requires reconciliation")
        conn.execute("UPDATE prompt_deliveries SET state=?, updated_at=?, note=? WHERE command_id=?", (state, _utc_iso(clock), note, command_id))
    return True


def recheck_prompt_binding(conn: TeamCraftConnection, command_id: str) -> None:
    """Fail closed immediately before Herdr prompt I/O on stale evidence."""
    row = conn.execute("SELECT c.payload_json, c.evidence_json, p.attempt_id, p.task_version, p.brief_digest, p.state FROM commands c JOIN prompt_deliveries p ON p.command_id=c.id WHERE c.id=?", (command_id,)).fetchone()
    if row is None:
        return  # legacy prompt command has no M2-03 binding
    try:
        payload, evidence = json.loads(str(row[0])), json.loads(str(row[1]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeAdapterError("prompt command binding is corrupt") from exc
    brief = briefs.get_attempt_brief(conn, str(row[2]))
    task = domain.get_task(conn, brief.task_id)
    rendered = payload.get("rendered_prompt_sha256")
    prompt_hash = payload.get("prompt_sha256")
    content = payload.get("brief_content")
    content_hash = (hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if isinstance(content, str) else None)
    valid = (str(row[5]) == "sending" and task.version == brief.task_version == int(row[3])
             and brief.brief_digest == str(row[4]) == evidence.get("digest")
             and payload.get("task_version") == brief.task_version
             and payload.get("brief_digest") == brief.brief_digest
             and content_hash == prompt_hash == rendered)
    if not valid:
        raise RuntimeAdapterError("prompt Brief/Task binding is stale; reconcile instead of sending")


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
    with _tx(conn):
        project_id = _project_id_for_command(conn, command_id)
        leader.require_project_lease(conn, lease, project_id, clock=clock)
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
    with _tx(conn):
        return command_core.mark_command_unknown(
            conn, command_id, note=audit, clock=clock,
        )


def stage_reconcile(
    conn: TeamCraftConnection, command_id: str, *, lease: leader.Lease,
    evidence: dict[str, Any], resolved_ok: bool, note: str,
    clock: Any = None,
) -> command_core.Command:
    """Close a ``running``/``unknown`` command from inspected external facts.

    Reconciliation commits an AUTHORITATIVE outcome (``confirmed`` = the facts
    prove the effect happened; ``expired`` = they prove it did not), so it
    requires the live leader lease exactly like ``stage_confirm`` (CEO gate D,
    M1-07 review follow-up): a stale or expired leader can never commit
    reconciliation either.  Recording ambiguity via :func:`stage_unknown`
    remains always permitted.  UNKNOWN never auto-retries: reconciliation
    records, it does not re-dispatch.
    """

    audit = f"{note} | evidence={json.dumps(evidence, sort_keys=True, default=str)}"
    with _tx(conn):
        project_id = _project_id_for_command(conn, command_id)
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        if resolved_ok:
            return command_core.reconcile_command(
                conn, command_id, outcome="confirmed", note=audit,
                result_json=json.dumps({"reconciled": True}, sort_keys=True),
                clock=clock,
            )
        return command_core.reconcile_command(
            conn, command_id, outcome="expired", note=audit, clock=clock,
        )


def claim_recovery_action(conn: TeamCraftConnection, command_id: str, *, lease: leader.Lease, clock: Any = None) -> command_core.Command:
    """Fenced claim for a recovery Command; CLI must not claim it directly."""
    with _tx(conn):
        leader.require_project_lease(conn, lease, _project_id_for_command(conn, command_id), clock=clock)
        return command_core.claim_command(conn, command_id, clock=clock)


def confirm_recovery_action(conn: TeamCraftConnection, command_id: str, *, lease: leader.Lease, result: dict[str, Any], clock: Any = None) -> command_core.Command:
    with _tx(conn):
        leader.require_project_lease(conn, lease, _project_id_for_command(conn, command_id), clock=clock)
        return command_core.confirm_command(conn, command_id, result_json=json.dumps(result, sort_keys=True), clock=clock)


def unknown_recovery_action(conn: TeamCraftConnection, command_id: str, *, note: str, clock: Any = None) -> command_core.Command:
    return stage_unknown(conn, command_id, note=note, clock=clock)
