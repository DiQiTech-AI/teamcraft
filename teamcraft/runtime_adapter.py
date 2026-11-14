"""M1-07 Core Runtime Command Service boundary for teamctl.

This is the single authoritative path for runtime state in TeamCraft 2.0
(CEO gate A/B): the CLI's team/member state lives ONLY in
``.teamcraft/teamcraft.db`` (schema v8).  There is deliberately NO JSON
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import commands as command_core
from . import briefs
from . import worker_reports
from . import recovery
from . import ceo_protocol
from . import ceo_initialization
from . import domain
from . import fresh_start
from . import leader
from . import storage
from . import verification
from . import worktrees
from . import delivery
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


def _compat_epoch(value: Any, field: str) -> int:
    """Read legacy epoch or Core ISO timestamps into the frozen CLI shape.

    ``members`` originated as a compatibility table whose timestamps were
    integer epochs.  Newer Core operations share the row and use the normal
    UTC ISO representation used by the domain schema.  SQLite accepts both
    despite the column's INTEGER affinity, so the compatibility reader must
    normalize either representation rather than failing after a Core update.
    """

    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise RuntimeAdapterError(
            f"member {field} is neither an epoch nor an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise RuntimeAdapterError(f"member {field} ISO timestamp lacks a timezone")
    return int(parsed.timestamp())


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
        "created_at": _compat_epoch(row[23], "created_at"),
        "updated_at": _compat_epoch(row[24], "updated_at"),
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
    ceo_names = sorted(name for name, member in members.items()
                       if str(member.get("role", "")) == "ceo")
    if len(ceo_names) > 1:
        raise RuntimeAdapterError(
            f"a project may have only one canonical CEO; found {ceo_names!r}"
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
    if member.get("role") == "ceo":
        other_ceos = conn.execute(
            "SELECT m.name FROM members m LEFT JOIN tasks t ON t.id=m.task_id "
            "WHERE m.role='ceo' AND m.name<>? AND (t.project_id=? OR m.task_id IS NULL) ORDER BY m.name",
            (str(member["name"]), project_id),
        ).fetchall()
        if other_ceos:
            raise RuntimeAdapterError(
                "project already has a canonical CEO: "
                + ", ".join(str(row[0]) for row in other_ceos)
            )
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


def prepare_ceo_bootstrap(conn: TeamCraftConnection, state: dict[str, Any],
                          member: dict[str, Any], *, clock: Any = None) -> dict[str, Any] | None:
    """Create the one-time project challenge for the canonical CEO prompt.

    A repeated/unknown bootstrap is refused. ``None`` is returned only after a
    initialized record accepted by the current receipt policy exists. With
    the temporary ``assumed_default`` mode, the bootstrap challenge is still
    required and acceptance is recorded only after successful prompt
    submission plus a live-session identity recheck.
    """
    if member.get("role") != "ceo":
        return None
    project_id = _project_id_for_member(conn, str(member["name"]), member.get("task_id"))
    try:
        record, nonce = ceo_initialization.discover_and_issue(
            conn, project_id, str(member["name"]), clock=clock,
        )
    except TeamCraftError as exc:
        detail = str(exc)
        lowered = detail.lower()
        failure_state = "incompatible" if any(
            marker in lowered for marker in ("protocol version", "protocol hash", "protocol pin", "incompatible", "pinned", "differs", "sha-256")
        ) else "failed"
        ceo_initialization.record_discovery_failure(
            conn, project_id, str(member["name"]), detail, state=failure_state, clock=clock,
        )
        raise RuntimeAdapterError(str(exc)) from exc
    if record.state == "initialized":
        return None
    if nonce is None:
        raise RuntimeAdapterError(
            f"CEO initialization is {record.state}; bootstrap will not be resent"
        )
    try:
        return ceo_initialization.bootstrap_prompt_fields(record, nonce)
    except TeamCraftError as exc:
        raise RuntimeAdapterError(str(exc)) from exc


def admit_ceo_bootstrap_effect(conn: TeamCraftConnection, project_id: str,
                               nonce: str, *, lease: leader.Lease,
                               clock: Any = None) -> None:
    """Fence and consume the one-use challenge immediately before Herdr prompt."""
    try:
        record = ceo_initialization.mark_bootstrap_unknown(
            conn, project_id, nonce, lease=lease, clock=clock,
        )
    except TeamCraftError as exc:
        raise RuntimeAdapterError(str(exc)) from exc
    if record.state != "unknown":
        raise RuntimeAdapterError(
            f"bootstrap challenge is {record.state}; refusing Herdr prompt"
        )


def record_ceo_bootstrap_submission_assumed(
    conn: TeamCraftConnection, project_id: str, nonce: str, *,
    native_session_id: str, agent_session: Any = None, session_state: str,
    lease: leader.Lease, clock: Any = None,
) -> ceo_initialization.CEOInitialization:
    """Pin post-prompt Session identity and persist the temporary assumption."""
    try:
        return ceo_initialization.record_bootstrap_submitted_assumed(
            conn, project_id, nonce, native_session_id=native_session_id,
            agent_session=agent_session, session_state=session_state,
            lease=lease, clock=clock,
        )
    except TeamCraftError as exc:
        raise RuntimeAdapterError(str(exc)) from exc


def record_ceo_bootstrap_live_process_assumed(
    conn: TeamCraftConnection, project_id: str, *,
    live_process_identity: dict[str, Any], session_state: str,
    actor: str, reason: str, lease: leader.Lease, clock: Any = None,
) -> ceo_initialization.CEOInitialization:
    """Explicitly bind a submitted CEO bootstrap to this process lifetime."""
    try:
        return ceo_initialization.record_bootstrap_submitted_live_process(
            conn, project_id, live_process_identity=live_process_identity,
            session_state=session_state, actor=actor, reason=reason,
            lease=lease, clock=clock,
        )
    except TeamCraftError as exc:
        raise RuntimeAdapterError(str(exc)) from exc


def reconcile_ceo_bootstrap_native_session(
    conn: TeamCraftConnection, project_id: str, *,
    native_session_id: str, agent_session: Any, session_state: str,
    pane_id: str, terminal_id: str, harness: str,
    herdr_session_identity_hash: str, actor: str, reason: str,
    lease: leader.Lease, clock: Any = None,
) -> ceo_initialization.CEOInitialization:
    """Bind a delayed native Session to a confirmed submitted bootstrap."""
    try:
        return ceo_initialization.reconcile_bootstrap_submitted_native_session(
            conn, project_id, native_session_id=native_session_id,
            agent_session=agent_session, session_state=session_state,
            pane_id=pane_id, terminal_id=terminal_id, harness=harness,
            herdr_session_identity_hash=herdr_session_identity_hash,
            actor=actor, reason=reason, lease=lease, clock=clock,
        )
    except TeamCraftError as exc:
        raise RuntimeAdapterError(str(exc)) from exc


def record_ceo_agent_claim(conn: TeamCraftConnection, project_id: str,
                           claim_json: str, *, lease: leader.Lease,
                           clock: Any = None) -> ceo_initialization.CEOInitialization:
    """Persist Agent JSON as unverified audit material; never as proof."""
    try:
        return ceo_initialization.record_agent_claim(
            conn, project_id, claim_json, lease=lease, clock=clock,
        )
    except TeamCraftError as exc:
        raise RuntimeAdapterError(str(exc)) from exc


def require_ceo_initialized(
    conn: TeamCraftConnection, project_id: str, *,
    allow_live_process: bool = False,
):
    try:
        return ceo_initialization.require_initialized(
            conn, project_id, allow_live_process=allow_live_process,
        )
    except TeamCraftError as exc:
        raise RuntimeAdapterError(str(exc)) from exc


# ---------------------------------------------------------------------------
# M4-02 workspace routing: writable developers into their own worktrees
# ---------------------------------------------------------------------------


def _workspace_member_row(conn: TeamCraftConnection, member: str) -> tuple[str, bool, str | None, str | None]:
    row = conn.execute(
        "SELECT role, read_only, task_id, attempt_id FROM members WHERE name = ?",
        (member,),
    ).fetchone()
    if row is None:
        raise RuntimeAdapterError(f"member {member!r} is not recorded")
    return (str(row[0]), bool(row[1]),
            None if row[2] is None else str(row[2]),
            None if row[3] is None else str(row[3]))


def _runtime_root(conn: TeamCraftConnection) -> str:
    meta = conn.execute(
        "SELECT project_root FROM runtime_meta WHERE id = 1").fetchone()
    if meta is None:
        raise RuntimeAdapterError("no team runtime recorded; cannot route a workspace")
    return str(meta[0])


def _worktree_dict(allocation: "worktrees.WorktreeAllocation", state: str | None,
                  created: bool) -> dict[str, Any]:
    return {
        "allocation_id": allocation.allocation_id,
        "attempt_id": allocation.attempt_id,
        "task_id": allocation.task_id,
        "worktree_path": allocation.worktree_path,
        "branch": allocation.branch,
        "base_sha": allocation.base_sha,
        "state": state,
        "created": created,
    }


def route_member_workspace(
    conn: TeamCraftConnection,
    member: str,
    *,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Deliver/adopt the execution workspace for one member (M4-02 routing).

    * **read-only members stay on the project root** and receive NO writable
      worktree — no allocation is recorded for them at all;
    * a writable member is routed into ITS OWN isolated worktree and branch
      (``<root>/.teamcraft/worktrees/<attempt-id>`` and
      ``tc/<task-id>/<attempt-id>``) delivered through
      :func:`teamcraft.worktrees.allocate_attempt_worktree`; two writable
      members therefore never share an uncommitted workspace.

    The routed ``cwd`` is RETURNED, not silently written into the member row:
    :func:`sync_state` remains the single authoritative writer of members, so
    the CLI carries ``cwd`` through the compatibility state dict on the next
    sync.  Restart/retry re-routes adopt only the exact recorded identity.
    """

    role, read_only, task_id, attempt_id = _workspace_member_row(conn, member)
    root = _runtime_root(conn)
    if read_only:
        return {"member": member, "role": role, "routed": "project_root",
                "cwd": root, "worktree": None,
                "note": "read-only members receive no writable worktree"}
    if task_id is None or attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Task/Attempt lineage; refusing to "
            "allocate a workspace for an unbound member")
    allocation, created = worktrees.allocate_attempt_worktree(
        conn, task_id=task_id, attempt_id=attempt_id, repo_root=root,
        run_git=run_git, clock=clock)
    return {"member": member, "role": role, "routed": "worktree",
            "cwd": allocation.worktree_path, "created": created,
            "worktree": _worktree_dict(allocation,
                                      worktrees.lifecycle_state(conn, attempt_id),
                                      created)}


def mark_member_workspace_cleanable(
    conn: TeamCraftConnection,
    member: str,
    *,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Safely transition the member's allocated worktree toward CLEANABLE.

    All safety lives in :func:`teamcraft.worktrees.mark_worktree_cleanable`
    (terminal Attempt, exact identity, completely clean workspace); read-only
    members have no writable worktree and are refused here.  NOTHING is
    deleted — CLEANABLE is a durable marker consumed by later milestones.
    """

    role, read_only, _task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only:
        raise RuntimeAdapterError(
            f"member {member!r} is read-only and has no writable worktree "
            "to mark cleanable")
    if attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Attempt; nothing is allocated to mark")
    allocation = worktrees.mark_worktree_cleanable(
        conn, attempt_id, run_git=run_git, clock=clock)
    return {"member": member, "role": role,
            "worktree": _worktree_dict(allocation,
                                      worktrees.lifecycle_state(conn, attempt_id),
                                      False)}


def member_workspace_status(
    conn: TeamCraftConnection,
    member: str,
    *,
    run_git: Any = None,
) -> dict[str, Any]:
    """Durable + live workspace view for one member (restart discovery)."""

    role, read_only, task_id, attempt_id = _workspace_member_row(conn, member)
    root = _runtime_root(conn)
    if read_only:
        return {"member": member, "role": role, "routed": "project_root",
                "worktree": None}
    if attempt_id is None:
        return {"member": member, "role": role, "routed": "unallocated",
                "worktree": None}
    state = worktrees.lifecycle_state(conn, attempt_id)
    if state is None:
        return {"member": member, "role": role, "routed": "unallocated",
                "worktree": None}
    status = worktrees.inspect_worktree(conn, attempt_id, run_git=run_git)
    allocation = worktrees.require_allocation(conn, attempt_id)
    view = _worktree_dict(allocation, status.state, False)
    view.update({"exists": status.exists, "problems": list(status.problems),
                 "dirty": status.dirty, "change_count": status.change_count})
    return {"member": member, "role": role, "routed": "worktree",
            "worktree": view}


# ---------------------------------------------------------------------------
# M4-03 worktree verifier: pinned profile execution inside the Attempt worktree
# ---------------------------------------------------------------------------


def member_attempt(conn: TeamCraftConnection, member: str) -> tuple[str, str]:
    """(task_id, attempt_id) for a member, refusing unbound members."""

    _role, _read_only, task_id, attempt_id = _workspace_member_row(conn, member)
    if task_id is None or attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Task/Attempt lineage; nothing to verify")
    return str(task_id), str(attempt_id)


def _worktree_verification_dto(
    v: "verification.WorktreeVerification", *, current: bool | None = None,
) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "verification_id": v.id, "command_id": v.command_id,
        "task_id": v.task_id, "attempt_id": v.attempt_id,
        "attempt_no": v.attempt_no, "task_version": v.task_version,
        "dispatch_task_version": v.dispatch_task_version,
        "brief_digest": v.brief_digest,
        "worker_report_digest": v.worker_report_digest,
        "allocation_id": v.allocation_id, "worktree_path": v.worktree_path,
        "branch": v.branch, "base_sha": v.base_sha,
        "profile": v.profile, "profile_version": v.profile_version,
        "config_digest": v.config_digest, "argv": list(v.argv) if v.argv else None,
        "status": v.status, "exit_code": v.exit_code,
        "report_digest": v.report_digest, "log_relpath": v.log_relpath,
        "log_digest": v.log_digest, "created_at": v.created_at,
    }
    if current is not None:
        dto["current"] = current
    return dto


def verify_member_workspace(
    conn: TeamCraftConnection,
    member: str,
    *,
    profile: str,
    idempotency_key: str,
    lease: leader.Lease,
    artifact_root: str | None = None,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Run ONE approved verifier profile in the member's OWN worktree (M4-03).

    Read-only members have no writable worktree and are refused; every safety
    rule (finished Attempt + completed WorkerReport + VERIFYING task, exact
    registered identity, Diff Gate before/after, pinned cwd/env/timeout/argv,
    bounded artifact under the configured artifact root, one settled verdict
    per Attempt+profile) lives in
    :func:`teamcraft.verification.run_worktree_verifier`.
    """

    role, read_only, _task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only:
        raise RuntimeAdapterError(
            f"member {member!r} is read-only and has no writable worktree to"
            " verify inside")
    if attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Attempt; nothing allocated to verify")
    result = verification.run_worktree_verifier(
        conn, attempt_id, profile=profile, idempotency_key=idempotency_key,
        lease=lease, artifact_root=artifact_root, run_git=run_git, clock=clock)
    return {"member": member, "role": role,
            "verification": _worktree_verification_dto(result)}


def run_member_workspace_verifier_suite(
    conn: TeamCraftConnection,
    member: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    profiles: Sequence[str] | None = None,
    artifact_root: str | None = None,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Run the approved P0 profile suite in the member's worktree (M4-03)."""

    role, read_only, _task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only:
        raise RuntimeAdapterError(
            f"member {member!r} is read-only and has no writable worktree to"
            " verify inside")
    if attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Attempt; nothing allocated to verify")
    suite = verification.run_worktree_suite(
        conn, attempt_id, idempotency_key=idempotency_key, lease=lease,
        profiles=profiles, artifact_root=artifact_root, run_git=run_git,
        clock=clock)
    return {"member": member, "role": role, "suite": suite}


def member_workspace_verifications(
    conn: TeamCraftConnection,
    member: str,
    *,
    artifact_root: str | None = None,
) -> dict[str, Any]:
    """Durable worktree-verification evidence view for one member (audit)."""

    role, read_only, task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only or attempt_id is None or task_id is None:
        return {"member": member, "role": role, "verifications": [],
                "commands": [], "artifact_root": None}
    rows = verification.list_worktree_verifications(conn, attempt_id)
    project = domain.get_task(conn, task_id).project_id
    root = artifact_root or verification.default_artifact_root(
        domain.get_project(conn, project).repo_path)
    return {"member": member, "role": role,
            "artifact_root": root,
            "verifications": [
                _worktree_verification_dto(
                    row, current=verification.worktree_verification_is_current(conn, row))
                for row in rows],
            "commands": verification.list_worktree_verification_commands(conn, task_id)}


def configure_project_verifier_profile(
    conn: TeamCraftConnection,
    member: str,
    *,
    profile: str,
    argv: Sequence[str],
    timeout_seconds: int,
    actor: str,
    idempotency_key: str,
    lease: leader.Lease,
    enabled: bool = True,
    clock: Any = None,
) -> dict[str, Any]:
    """Approve one verifier profile argv for the member's project (M4-03)."""

    _role, _read_only, task_id, _attempt_id = _workspace_member_row(conn, member)
    project_id = _project_id_for_member(conn, member, task_id)
    return verification.configure_verifier_profile(
        conn, project_id, profile=profile, argv=argv,
        timeout_seconds=timeout_seconds, actor=actor,
        idempotency_key=idempotency_key, lease=lease, enabled=enabled,
        clock=clock)


# ---------------------------------------------------------------------------
# M4-04 local delivery: Commit/Diff evidence, ship evidence, local-only approval
# ---------------------------------------------------------------------------
#
# Thin, member-scoped surface over the :mod:`teamcraft.delivery` Core API.  All
# safety (idempotency keys, fenced project leader leases, exact member/attempt
# identity, base/verifier/policy drift, protected-branch refusal, report-only
# refusal, fail-closed staleness) lives in Core; this layer only resolves the
# member's own Task/Attempt, refuses read-only members where a WRITABLE
# worktree is required, forwards the fence + key verbatim, and shapes durable
# evidence into JSON-safe DTOs the CLI prints.  It NEVER pushes, merges, commits
# on a protected branch, or builds arbitrary git argv.


def _commit_evidence_dto(e: "delivery.CommitEvidence") -> dict[str, Any]:
    return {
        "commit_evidence_id": e.event_id, "project_id": e.project_id,
        "task_id": e.task_id, "attempt_id": e.attempt_id,
        "attempt_no": e.attempt_no, "task_version": e.task_version,
        "dispatch_task_version": e.dispatch_task_version,
        "brief_digest": e.brief_digest, "brief_version": e.brief_version,
        "worker_report_id": e.worker_report_id,
        "worker_report_digest": e.worker_report_digest,
        "allocation_id": e.allocation_id, "worktree_path": e.worktree_path,
        "branch": e.branch, "base_sha": e.base_sha, "parent_sha": e.parent_sha,
        "commit_sha": e.commit_sha, "diff_digest": e.diff_digest,
        "file_count": e.file_count, "claims": list(e.claims),
        "claims_digest": e.claims_digest, "source": e.source,
        "command_id": e.command_id, "evidence_digest": e.evidence_digest,
        "occurred_at": e.occurred_at,
    }


def _ship_evidence_dto(e: "delivery.ShipEvidence") -> dict[str, Any]:
    return {
        "ship_evidence_id": e.id, "project_id": e.project_id,
        "task_id": e.task_id, "attempt_id": e.attempt_id,
        "attempt_no": e.attempt_no, "task_version": e.task_version,
        "dispatch_task_version": e.dispatch_task_version,
        "brief_digest": e.brief_digest, "brief_version": e.brief_version,
        "worker_report_id": e.worker_report_id,
        "worker_report_digest": e.worker_report_digest,
        "delivery_mode": e.delivery_mode, "proposed_action": e.proposed_action,
        "allocation_id": e.allocation_id, "worktree_path": e.worktree_path,
        "branch": e.branch, "base_sha": e.base_sha, "commit_sha": e.commit_sha,
        "commit_evidence_id": e.commit_evidence_id,
        "diff_digest": e.diff_digest, "diff_bytes": e.diff_bytes,
        "diff_relpath": e.diff_relpath, "file_count": e.file_count,
        "verifiers": [dict(item) for item in e.verifiers],
        "verifier_digest": e.verifier_digest, "policy_digest": e.policy_digest,
        "evidence_digest": e.evidence_digest, "command_id": e.command_id,
        "occurred_at": e.occurred_at,
    }


def _delivery_approval_dto(a: "delivery.DeliveryApproval") -> dict[str, Any]:
    return {
        "approval_id": a.id, "command_id": a.command_id,
        "ship_evidence_id": a.ship_evidence_id, "project_id": a.project_id,
        "task_id": a.task_id, "attempt_id": a.attempt_id,
        "attempt_no": a.attempt_no, "task_version": a.task_version,
        "commit_sha": a.commit_sha, "branch": a.branch,
        "diff_digest": a.diff_digest, "evidence_digest": a.evidence_digest,
        "verifier_digest": a.verifier_digest,
        "proposed_action": a.proposed_action, "decision": a.decision,
        "actor": a.actor, "reason": a.reason,
        "task_status_after": a.task_status_after, "occurred_at": a.occurred_at,
    }


def _freshness_dto(fresh: "delivery.DeliveryFreshness") -> dict[str, Any]:
    return {"current": bool(fresh.ok), "problems": list(fresh.problems)}


def _command_summary(command: Any) -> dict[str, Any]:
    return {
        "command_id": command.id, "kind": command.kind, "state": command.state,
        "idempotency_key": command.idempotency_key,
    }


def create_member_local_commit(
    conn: TeamCraftConnection,
    member: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    message: str | None = None,
    artifact_root: str | None = None,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Create/adopt the local Commit on the member's OWN Attempt worktree (M4-04).

    Read-only members have no writable worktree and are refused before Core is
    touched.  Everything else — finished Attempt + WorkerReport, allocation and
    base identity, protected ``tc/<task>/<attempt>`` branch, unowned/empty/drift
    change sets, report-only policy, idempotency and the fenced project lease —
    is enforced by :func:`teamcraft.delivery.create_local_commit`.
    """

    role, read_only, _task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only:
        raise RuntimeAdapterError(
            f"member {member!r} is read-only and has no writable worktree to"
            " commit on")
    if attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Attempt; nothing allocated to commit")
    evidence = delivery.create_local_commit(
        conn, attempt_id, idempotency_key=idempotency_key, lease=lease,
        message=message, artifact_root=artifact_root, run_git=run_git,
        clock=clock)
    return {"member": member, "role": role, "source": evidence.source,
            "commit": _commit_evidence_dto(evidence)}


def reconcile_member_commit_receipt(
    conn: TeamCraftConnection,
    member: str,
    command_id: str,
    *,
    lease: leader.Lease,
    actor: str,
    reason: str,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Reconcile a lost/interrupted Commit receipt for one member (M4-04).

    This layer binds the durable Command's kind and exact
    project/Task/Attempt identity to the requesting member *before* entering
    Core, so a stray ``command_id`` can never be reconciled under the wrong
    member fence.  See :func:`teamcraft.delivery.reconcile_commit_receipt`.
    """

    role, read_only, task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only:
        raise RuntimeAdapterError(
            f"member {member!r} is read-only and has no commit receipt to"
            " reconcile")
    if attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Attempt; nothing to reconcile")

    # Scope the durable Command to the member BEFORE Core is allowed to
    # inspect Git or settle the Command.  Reconciliation can adopt a commit
    # and confirm an UNKNOWN Command, so a post-effect identity check would
    # report an error only after mutating another member's delivery record.
    command = command_core.get_command(conn, command_id)
    if command.kind != delivery.COMMIT_COMMAND_KIND:
        raise RuntimeAdapterError(
            f"command {command_id!r} is not a local commit Command")
    try:
        payload = json.loads(command.payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeAdapterError(
            f"commit command {command_id!r} has malformed payload") from exc
    if not isinstance(payload, dict):
        raise RuntimeAdapterError(
            f"commit command {command_id!r} has malformed payload")
    project_id = _project_id_for_member(conn, member, task_id)
    if (payload.get("attempt_id") != attempt_id
            or payload.get("task_id") != task_id
            or payload.get("project_id") != project_id):
        raise RuntimeAdapterError(
            f"commit command {command_id!r} belongs to another member scope;"
            f" member {member!r} may not reconcile it")

    result = delivery.reconcile_commit_receipt(
        conn, command_id, lease=lease, actor=actor, reason=reason,
        run_git=run_git, clock=clock)
    evidence = result.get("commit_evidence")
    # Core also binds its returned evidence.  Keep this defensive assertion,
    # but it is no longer the first scope check and therefore cannot be the
    # source of a post-effect refusal.
    if (evidence is not None
            and (evidence.attempt_id != attempt_id
                 or evidence.task_id != task_id
                 or evidence.project_id != project_id)):
        raise RuntimeAdapterError(
            f"commit command {command_id!r} returned foreign evidence;"
            f" member {member!r} may not reconcile it")
    return {"member": member, "role": role,
            "resolution": result["resolution"],
            "command": _command_summary(result["command"]),
            "commit": (_commit_evidence_dto(evidence)
                      if evidence is not None else None)}


def record_member_ship_evidence(
    conn: TeamCraftConnection,
    member: str,
    *,
    idempotency_key: str,
    lease: leader.Lease,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Bind immutable ship evidence + PROPOSE local-only acceptance (M4-04).

    Refusals — report-only mode, missing/failed/stale verifier, drifted commit
    or policy, no durable commit evidence — all fail closed inside
    :func:`teamcraft.delivery.record_ship_evidence`; nothing is written.
    """

    role, read_only, _task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only:
        raise RuntimeAdapterError(
            f"member {member!r} is read-only and has nothing to ship")
    if attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Attempt; nothing to ship")
    evidence = delivery.record_ship_evidence(
        conn, attempt_id, idempotency_key=idempotency_key, lease=lease,
        run_git=run_git, clock=clock)
    return {"member": member, "role": role,
            "proposed_action": evidence.proposed_action,
            "ship": _ship_evidence_dto(evidence)}


def member_delivery_status(
    conn: TeamCraftConnection,
    member: str,
    *,
    live_git: bool = True,
    run_git: Any = None,
) -> dict[str, Any]:
    """Currentness view of a member's commit/ship/approval chain (M4-04 audit).

    Read-only/unbound members yield an empty view.  Every durable record is
    re-checked against LIVE facts (``live_git`` honoured); ``current=false``
    plus ``problems`` makes staleness explicit so callers fail closed rather
    than trusting a settled-but-stale PASS.
    """

    role, read_only, task_id, attempt_id = _workspace_member_row(conn, member)
    empty = {"member": member, "role": role, "bound": False, "task_id": None,
             "attempt_id": None, "commit": None, "ship": None, "approval": None,
             "proposed_action": None}
    if read_only or task_id is None or attempt_id is None:
        return empty
    view = {"member": member, "role": role, "bound": True, "task_id": task_id,
            "attempt_id": attempt_id, "commit": None, "ship": None,
            "approval": None, "proposed_action": None}
    commit = delivery.load_commit_evidence(conn, attempt_id)
    if commit is not None:
        view["commit"] = dict(
            _commit_evidence_dto(commit),
            **_freshness_dto(delivery.commit_evidence_is_current(
                conn, commit, live_git=live_git, run_git=run_git)))
    ship = delivery.load_ship_evidence(conn, attempt_id)
    if ship is not None:
        view["ship"] = dict(
            _ship_evidence_dto(ship),
            **_freshness_dto(delivery.ship_evidence_is_current(
                conn, ship, live_git=live_git, run_git=run_git)))
        view["proposed_action"] = ship.proposed_action
        approval = delivery.load_delivery_approval(conn, ship.id)
        if approval is not None:
            view["approval"] = dict(
                _delivery_approval_dto(approval),
                **_freshness_dto(delivery.delivery_approval_is_current(
                    conn, approval, live_git=live_git, run_git=run_git)))
    return view


def member_ship_diff(
    conn: TeamCraftConnection,
    member: str,
    *,
    ship_evidence_id: str | None = None,
    artifact_root: str | None = None,
) -> dict[str, Any]:
    """Read + integrity-verify the COMPLETE diff artifact for a member (M4-04).

    Without ``ship_evidence_id`` the member's own bound ship evidence is used;
    with one, it MUST belong to the member's Attempt.  Integrity (size + digest
    recheck) is enforced by :func:`teamcraft.delivery.read_ship_diff`.
    """

    _role, read_only, task_id, attempt_id = _workspace_member_row(conn, member)
    if read_only or attempt_id is None or task_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no local-only delivery to diff")
    if ship_evidence_id is None:
        bound = delivery.load_ship_evidence(conn, attempt_id)
        if bound is None:
            raise RuntimeAdapterError(
                f"member {member!r} has no ship evidence to diff")
        ship_evidence_id = bound.id
    evidence = delivery.get_ship_evidence(conn, ship_evidence_id)
    if evidence.attempt_id != attempt_id:
        raise RuntimeAdapterError(
            f"ship evidence {ship_evidence_id!r} belongs to another Attempt;"
            f" member {member!r} may not read its diff")
    return delivery.read_ship_diff(conn, ship_evidence_id,
                                  artifact_root=artifact_root)


def decide_member_local_delivery(
    conn: TeamCraftConnection,
    member: str,
    *,
    decision: str,
    ship_evidence_id: str,
    commit_sha: str | None = None,
    branch: str | None = None,
    actor: str,
    reason: str,
    idempotency_key: str,
    lease: leader.Lease,
    run_git: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Explicitly accept or reject local-only delivery for one member (M4-04).

    The decision is bound to exactly one ship evidence that must belong to the
    member's Attempt and project; an ``accept`` must echo the exact commit and
    branch (enforced by Core) and is the ONLY path to DONE for a local-only
    Task.  A ``reject`` records the decision and leaves the Task VERIFYING so a
    fix arrives as a fresh bounded Attempt.  Stale/drifted evidence fails closed
    inside :func:`teamcraft.delivery.approve_local_delivery`.
    """

    _role, _read_only, _task_id, attempt_id = _workspace_member_row(conn, member)
    if attempt_id is None:
        raise RuntimeAdapterError(
            f"member {member!r} has no Attempt; nothing to accept or reject")
    evidence = delivery.get_ship_evidence(conn, ship_evidence_id)
    if evidence.attempt_id != attempt_id:
        raise RuntimeAdapterError(
            f"ship evidence {ship_evidence_id!r} belongs to another Attempt;"
            f" member {member!r} may not decide on it")
    approval = delivery.approve_local_delivery(
        conn, ship_evidence_id, decision=decision, commit_sha=commit_sha,
        branch=branch, actor=actor, reason=reason,
        idempotency_key=idempotency_key, lease=lease, run_git=run_git,
        clock=clock)
    return {"member": member, "decision": approval.decision,
            "task_status_after": approval.task_status_after,
            "approval": _delivery_approval_dto(approval)}


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


def reconcile_submitted_prompt(
    conn: TeamCraftConnection,
    command_id: str,
    *,
    member: str,
    lease: leader.Lease,
    actor: str,
    reason: str,
    clock: Any = None,
) -> command_core.Command:
    """Confirm an unresolved Prompt Command from its durable delivery fact.

    This reconciliation is deliberately narrower than generic stage
    reconciliation: the exact Command must be the member's latest
    ``agent.prompt`` Command and its version-bound delivery row must already
    be terminal ``submitted``.  It settles transport only.  CEO initialization
    and native Session binding are not modified, so the Scheduler gate remains
    closed while M2-08 identity is UNKNOWN.
    """
    if not isinstance(member, str) or not member:
        raise ValidationError("prompt reconciliation requires a member")
    if not isinstance(actor, str) or not actor.strip():
        raise ValidationError("prompt reconciliation requires an actor")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("prompt reconciliation requires a reason")
    evidence = {
        "actor": actor.strip(),
        "reason": reason.strip(),
        "durable_delivery_state": "submitted",
        "initialization_changed": False,
    }
    with _tx(conn):
        project_id = _project_id_for_command(conn, command_id)
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        command = command_core.get_command(conn, command_id)
        latest = latest_stage_command(conn, member, STAGE_PROMPT)
        try:
            payload = json.loads(command.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeAdapterError("prompt Command payload is corrupt") from exc
        delivery = conn.execute(
            "SELECT state, note FROM prompt_deliveries WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if (command.kind != STAGE_PROMPT or payload.get("member") != member
                or latest is None or latest.id != command_id):
            raise RuntimeAdapterError(
                "prompt reconciliation requires the member's latest agent.prompt Command"
            )
        if delivery is None or str(delivery[0]) != "submitted":
            raise RuntimeAdapterError(
                "prompt reconciliation requires durable submitted delivery evidence"
            )
        if command.state == "confirmed":
            return command
        if command.state not in {"running", "unknown"}:
            raise RuntimeAdapterError(
                f"prompt Command in state {command.state!r} cannot be reconciled as submitted"
            )
        audit = (
            "durable prompt delivery proves transport submission; CEO native "
            f"Session initialization remains independent | evidence={json.dumps(evidence, sort_keys=True)}"
        )
        return command_core.reconcile_command(
            conn, command_id, outcome="confirmed", note=audit,
            result_json=json.dumps(
                {"reconciled": True, "transport": "submitted", "evidence": evidence},
                sort_keys=True,
            ),
            clock=clock,
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
