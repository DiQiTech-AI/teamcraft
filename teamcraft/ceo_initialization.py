"""Project-scoped CEO protocol initialization state (M2-08).

This service persists discovery and one-use handshake challenges without
creating a Task or Attempt. A matching Agent-authored JSON claim is retained
only as ``read_unverified`` audit material. The current Herdr/Harness adapter
has no host-trusted Protocol/Skill receipt; the explicit temporary
``assumed_default`` path may initialize only after successful bootstrap
transport, post-submit native Session binding, and a second identity check.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ceo_protocol, commands, domain, events, leader, storage
from .clock import Clock, SystemClock
from .errors import NotFoundError, TeamCraftError, ValidationError
from .storage import TeamCraftConnection, transaction

CEO_STATES = frozenset({
    "discovered", "read_unverified", "initialized", "failed", "incompatible", "unknown",
})
MAX_AGENT_CLAIM_BYTES = 16 * 1024
DEFAULT_NONCE_TTL_SECONDS = 300


class CEOInitializationError(TeamCraftError):
    """CEO initialization could not be proven or safely resumed."""


class CEOInitializationGateClosed(CEOInitializationError):
    """Project dispatch is blocked until trusted initialization is recorded."""


class CEOInitializationReplayError(CEOInitializationError):
    """A nonce was expired, consumed, or mismatched and cannot be replayed."""


@dataclass(frozen=True)
class CEOInitialization:
    project_id: str
    canonical_ceo_name: str
    state: str
    team_id: str
    teamcraft_session_id: str | None
    herdr_session_identity_json: str
    native_session_id: str | None
    terminal_id: str | None
    protocol_version: str | None
    protocol_hash: str | None
    runtime_skill_hash: str | None
    snapshot_digest: str | None
    nonce_expires_at: str | None
    nonce_consumed_at: str | None
    audit_material_json: str | None
    detail: str | None
    receipt_source: str
    identity_mode: str
    live_process_identity_json: str | None
    created_at: str
    updated_at: str


_COLUMNS = (
    "project_id, canonical_ceo_name, state, team_id, teamcraft_session_id, "
    "herdr_session_identity_json, native_session_id, terminal_id, "
    "protocol_version, protocol_hash, runtime_skill_hash, snapshot_digest, "
    "nonce_expires_at, nonce_consumed_at, audit_material_json, detail, receipt_source, "
    "identity_mode, live_process_identity_json, created_at, updated_at"
)


def _now(clock: Clock | None) -> datetime:
    return (clock or SystemClock()).now().astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _record(row: Any) -> CEOInitialization:
    return CEOInitialization(
        str(row[0]), str(row[1]), str(row[2]), str(row[3]),
        None if row[4] is None else str(row[4]), str(row[5]),
        None if row[6] is None else str(row[6]),
        None if row[7] is None else str(row[7]),
        None if row[8] is None else str(row[8]),
        None if row[9] is None else str(row[9]),
        None if row[10] is None else str(row[10]),
        None if row[11] is None else str(row[11]),
        None if row[12] is None else str(row[12]),
        None if row[13] is None else str(row[13]),
        None if row[14] is None else str(row[14]),
        None if row[15] is None else str(row[15]), str(row[16]), str(row[17]),
        None if row[18] is None else str(row[18]), str(row[19]), str(row[20]),
    )


def get_initialization(conn: TeamCraftConnection, project_id: str) -> CEOInitialization | None:
    domain.require_head(conn)
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM ceo_initializations WHERE project_id=?", (project_id,),
    ).fetchone()
    return None if row is None else _record(row)


def record_discovery_failure(
    conn: TeamCraftConnection, project_id: str, ceo_name: str, detail: str, *,
    state: str = "failed", clock: Clock | None = None,
) -> CEOInitialization | None:
    """Audit a retryable pre-effect discovery failure without pinning a row.

    No bootstrap Command has been claimed and no external side effect can have
    happened at this point.  Keeping ``ceo_initializations`` empty lets the
    same canonical CEO retry after its local protocol/session precondition is
    repaired; the append-only event preserves the failed attempt.
    """
    if state not in {"failed", "incompatible"}:
        raise ValidationError("discovery failure state must be failed or incompatible")
    with storage.transaction(conn):
        existing = get_initialization(conn, project_id)
        if existing is not None:
            return existing
        domain.get_project(conn, project_id)
        events.append_event(conn, f"ceo.initialization.{state}", project_id=project_id,
            payload_json=_json({"ceo_name": ceo_name, "detail": detail[:2048],
                                "retryable": True, "effect_claimed": False}), clock=clock)
        return None


def project_task_snapshot_digest(conn: TeamCraftConnection, project_id: str) -> str:
    """Hash the canonical project and current Task facts for a challenge binding."""
    project = domain.get_project(conn, project_id)
    tasks = conn.execute(
        "SELECT id, title, description, acceptance_json, kind, priority, status, risk, "
        "ownership_json, version, created_at, updated_at FROM tasks "
        "WHERE project_id=? ORDER BY id", (project_id,),
    ).fetchall()
    task_items = []
    for row in tasks:
        dependencies = conn.execute(
            "SELECT d.depends_on_id FROM task_dependencies d "
            "JOIN tasks t ON t.id=d.task_id WHERE d.task_id=? AND t.project_id=? "
            "ORDER BY d.depends_on_id", (str(row[0]), project_id),
        ).fetchall()
        task_items.append({
            "id": str(row[0]), "title": str(row[1]), "description": str(row[2]),
            "acceptance_json": str(row[3]), "kind": str(row[4]), "priority": int(row[5]),
            "status": str(row[6]), "risk": str(row[7]), "ownership_json": str(row[8]),
            "version": int(row[9]), "created_at": str(row[10]), "updated_at": str(row[11]),
            "depends_on": [str(dep[0]) for dep in dependencies],
        })
    return _digest({"project": {"id": project.id, "name": project.name,
                                 "repo_path": project.repo_path, "config_json": project.config_json},
                    "tasks": task_items})


def _ceo_bindings(
    conn: TeamCraftConnection, project_id: str, ceo_name: str, *,
    require_native_session: bool = True,
    require_live_session: bool = True,
) -> dict[str, Any]:
    project = domain.get_project(conn, project_id)
    rows = conn.execute(
        "SELECT m.name, m.kind, m.task_id, m.attempt_id, m.session_id, m.terminal_id, "
        "m.agent_session_json, s.native_session_id, s.attempt_id, t.project_id "
        "FROM members m LEFT JOIN sessions s ON s.id=m.session_id "
        "LEFT JOIN tasks t ON t.id=m.task_id WHERE m.role='ceo' "
        "AND (t.project_id=? OR m.task_id IS NULL) ORDER BY m.name", (project_id,),
    ).fetchall()
    names = {str(row[0]) for row in rows}
    if len(names) != 1 or ceo_name not in names:
        raise CEOInitializationError(
            f"project must have exactly one canonical CEO member; found {sorted(names)!r}"
        )
    row = rows[0]
    if str(row[1]) not in {"codex", "opencode"}:
        raise CEOInitializationError("CEO Harness has no supported protocol discovery contract")
    if (row[4] is None or row[5] is None or not str(row[5]).strip()
            or row[3] is None or row[8] is None or str(row[3]) != str(row[8])):
        raise CEOInitializationError(
            "CEO TeamCraft Session, terminal, and Attempt binding are required"
        )
    native_session_id = None if row[7] is None else str(row[7]).strip() or None
    try:
        member_session = None if row[6] is None else json.loads(str(row[6]))
    except json.JSONDecodeError as exc:
        raise CEOInitializationError("CEO member native Session binding is malformed") from exc
    if isinstance(member_session, dict):
        member_native_session_id = member_session.get("value")
    else:
        member_native_session_id = member_session
    if member_native_session_id is not None:
        if not isinstance(member_native_session_id, str) or not member_native_session_id.strip():
            raise CEOInitializationError("CEO member native Session binding is malformed")
        member_native_session_id = member_native_session_id.strip()
    if member_native_session_id != native_session_id:
        raise CEOInitializationError(
            "CEO member and TeamCraft Session native identities do not match"
        )
    if require_native_session and native_session_id is None:
        raise CEOInitializationError("CEO Herdr native Session binding is required")
    attempt = domain.get_attempt(conn, str(row[3]))
    if attempt.task_id != str(row[2]) or not attempt.protocol_version or not attempt.protocol_hash:
        raise CEOInitializationError("CEO Attempt does not carry the immutable protocol pin")
    meta = conn.execute(
        "SELECT team_id, project_root, herdr_session_json FROM runtime_meta WHERE id=1",
    ).fetchone()
    if meta is None or str(meta[1]) != project.repo_path:
        raise CEOInitializationError("runtime project/session identity is unavailable or mismatched")
    identity = json.loads(str(meta[2]))
    if not isinstance(identity, dict):
        raise CEOInitializationError("stored Herdr team session identity is malformed")
    session = domain.get_session(conn, str(row[4]))
    if session.attempt_id != str(row[3]) or session.native_session_id != native_session_id:
        raise CEOInitializationError("CEO TeamCraft Session does not match its Herdr native Session")
    if require_live_session and session.state not in {"idle", "working"}:
        raise CEOInitializationError(
            f"CEO Session liveness is {session.state}; unknown, missing, or exited CEO cannot initialize"
        )
    return {
        "team_id": str(meta[0]), "teamcraft_session_id": str(row[4]),
        "herdr_session_identity_json": _json(identity), "native_session_id": native_session_id,
        "terminal_id": str(row[5]), "protocol_version": attempt.protocol_version,
        "protocol_hash": attempt.protocol_hash, "harness": str(row[1]),
        "project_root": project.repo_path, "attempt_id": str(row[3]),
        "pane_id": session.pane_id,
    }


def discover_and_issue(
    conn: TeamCraftConnection,
    project_id: str,
    ceo_name: str,
    *,
    nonce_ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS,
    clock: Clock | None = None,
) -> tuple[CEOInitialization, str | None]:
    """Persist local discovery and issue a nonce once; never recover its plaintext."""
    if isinstance(nonce_ttl_seconds, bool) or not isinstance(nonce_ttl_seconds, int) or nonce_ttl_seconds <= 0:
        raise ValidationError("nonce_ttl_seconds must be a positive integer")
    now = _now(clock)
    with storage.transaction(conn):
        bindings = _ceo_bindings(
            conn, project_id, ceo_name,
            require_native_session=False, require_live_session=False,
        )
        existing = get_initialization(conn, project_id)
        refresh_existing = False
        if existing is not None:
            if existing.canonical_ceo_name != ceo_name:
                raise CEOInitializationGateClosed(
                    "project CEO identity is already pinned; refusing to create or bootstrap a second CEO"
                )
            if existing.state == "initialized":
                return require_initialized(
                    conn, project_id, allow_live_process=True,
                ), None
            if existing.state != "discovered" or existing.nonce_consumed_at is not None:
                raise CEOInitializationGateClosed(
                    f"CEO initialization is {existing.state}; do not create a second CEO or resend bootstrap"
                )
            prompt_claim = conn.execute(
                "SELECT id, state FROM commands WHERE kind='agent.prompt' "
                "AND json_extract(payload_json, '$.member')=? "
                "AND json_extract(payload_json, '$.attempt_id')=? "
                "ORDER BY requested_at DESC, id DESC LIMIT 1",
                (ceo_name, bindings["attempt_id"]),
            ).fetchone()
            if prompt_claim is not None:
                raise CEOInitializationGateClosed(
                    "CEO bootstrap has a durable Prompt Command; reconcile it instead of rotating the nonce"
                )
            refresh_existing = True
        try:
            protocol = ceo_protocol.discover_protocol(
                bindings["project_root"], bindings["harness"],
                expected_version=bindings["protocol_version"],
                expected_sha256=bindings["protocol_hash"],
            )
            skill_path = Path(bindings["project_root"]) / ".agents" / "skills" / "teamcraft" / "SKILL.md"
            if skill_path.is_symlink():
                raise CEOInitializationError("TeamCraft runtime Skill must not be a symbolic link")
            skill_bytes = skill_path.read_bytes()
            skill_bytes.decode("utf-8")
        except (ceo_protocol.ProtocolError, OSError, UnicodeError) as exc:
            raise CEOInitializationGateClosed(f"CEO protocol/runtime Skill discovery failed: {exc}") from exc
        if hashlib.sha256(protocol.content).hexdigest() != bindings["protocol_hash"]:
            raise CEOInitializationGateClosed("CEO protocol changed after the Attempt pin")
        nonce = secrets.token_urlsafe(32)  # 256 bits before URL-safe encoding.
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        expires = _iso(now + timedelta(seconds=nonce_ttl_seconds))
        snapshot = project_task_snapshot_digest(conn, project_id)
        runtime_skill_hash = hashlib.sha256(skill_bytes).hexdigest()
        if refresh_existing:
            assert existing is not None
            if (existing.team_id != bindings["team_id"]
                    or existing.teamcraft_session_id != bindings["teamcraft_session_id"]
                    or existing.herdr_session_identity_json != bindings["herdr_session_identity_json"]
                    or existing.native_session_id != bindings["native_session_id"]
                    or existing.terminal_id != bindings["terminal_id"]
                    or existing.protocol_version != protocol.version
                    or existing.protocol_hash != protocol.sha256
                    or existing.runtime_skill_hash != runtime_skill_hash
                    or existing.snapshot_digest != snapshot):
                raise CEOInitializationGateClosed(
                    "CEO bootstrap bindings drifted before safe pre-effect retry"
                )
            updated = conn.execute(
                "UPDATE ceo_initializations SET nonce_hash=?, nonce_expires_at=?, "
                "detail=NULL, updated_at=? WHERE project_id=? AND state='discovered' "
                "AND nonce_consumed_at IS NULL",
                (nonce_hash, expires, _iso(now), project_id),
            )
            if updated.rowcount != 1:
                raise CEOInitializationGateClosed(
                    "CEO bootstrap changed before safe pre-effect retry"
                )
            event_kind = "ceo.initialization.challenge_refreshed"
        else:
            conn.execute(
                "INSERT INTO ceo_initializations (project_id, canonical_ceo_name, state, team_id, "
                "teamcraft_session_id, herdr_session_identity_json, native_session_id, terminal_id, "
                "protocol_version, protocol_hash, runtime_skill_hash, snapshot_digest, nonce_hash, "
                "nonce_expires_at, nonce_consumed_at, audit_material_json, detail, created_at, updated_at) "
                "VALUES (?, ?, 'discovered', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (project_id, ceo_name, bindings["team_id"], bindings["teamcraft_session_id"],
                 bindings["herdr_session_identity_json"], bindings["native_session_id"],
                 bindings["terminal_id"], protocol.version, protocol.sha256,
                 runtime_skill_hash, snapshot, nonce_hash, expires, _iso(now), _iso(now)),
            )
            event_kind = "ceo.initialization.discovered"
        events.append_event(conn, event_kind, project_id=project_id,
            payload_json=_json({"ceo_name": ceo_name, "state": "discovered",
                "protocol_version": protocol.version, "protocol_hash": protocol.sha256,
                "snapshot_digest": snapshot, "nonce_expires_at": expires,
                "retry": refresh_existing, "effect_claimed": False}), clock=clock)
        record = get_initialization(conn, project_id)
        assert record is not None
        return record, nonce


def bootstrap_prompt_fields(record: CEOInitialization, nonce: str) -> dict[str, Any]:
    """Fields the first CEO prompt may ask the Agent to echo as unverified material."""
    if record.state != "discovered" or not nonce:
        raise CEOInitializationGateClosed("no fresh CEO initialization challenge is available")
    return {
        "project_id": record.project_id,
        "team_id": record.team_id,
        "nonce": nonce,
        "herdr_session_identity_hash": hashlib.sha256(
            record.herdr_session_identity_json.encode("utf-8")
        ).hexdigest(),
        "teamcraft_session_id": str(record.teamcraft_session_id),
        # Some Harnesses expose this only after their first prompt.  ``None``
        # is an explicit pre-bootstrap fact, never the string "None".
        "herdr_session_id": record.native_session_id,
        "terminal_id": str(record.terminal_id),
        "protocol_version": str(record.protocol_version),
        "protocol_hash": str(record.protocol_hash),
        "runtime_skill_hash": str(record.runtime_skill_hash),
        "snapshot_digest": str(record.snapshot_digest),
    }


def mark_bootstrap_unknown(
    conn: TeamCraftConnection,
    project_id: str,
    nonce: str,
    *,
    lease: leader.Lease,
    clock: Clock | None = None,
) -> CEOInitialization:
    """Admit the one prompt only while its challenge bindings are still current.

    The transition to ``unknown`` is persisted before the external call so a
    crash or ambiguous response cannot trigger a resend. The nonce remains
    unconsumed until one response is audited by ``record_agent_claim``.
    """
    now = _now(clock)
    with storage.transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        record = get_initialization(conn, project_id)
        if record is None or record.state != "discovered" or record.nonce_consumed_at is not None:
            raise CEOInitializationReplayError("CEO initialization nonce is absent, consumed, or not pending")
        row = conn.execute(
            "SELECT nonce_hash, nonce_expires_at FROM ceo_initializations WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None or hashlib.sha256(nonce.encode("utf-8")).hexdigest() != str(row[0]):
            raise CEOInitializationReplayError("CEO initialization nonce does not match this project challenge")
        if datetime.fromisoformat(str(row[1])) <= now:
            conn.execute(
                "UPDATE ceo_initializations SET state='failed', nonce_consumed_at=?, detail=?, updated_at=? WHERE project_id=?",
                (_iso(now), "bootstrap challenge expired before prompt admission", _iso(now), project_id),
            )
            events.append_event(conn, "ceo.initialization.failed", project_id=project_id,
                payload_json=_json({"reason": "nonce_expired_before_prompt"}), clock=clock)
            result = get_initialization(conn, project_id)
            assert result is not None
            return result
        try:
            bindings = _ceo_bindings(
                conn, project_id, record.canonical_ceo_name,
                require_native_session=False, require_live_session=False,
            )
            # Re-read both protocol artifacts at the effect admission boundary.
            # The immutable Attempt pin alone cannot detect files changed after
            # discover_and_issue() returned its challenge.
            protocol = ceo_protocol.discover_protocol(
                bindings["project_root"], bindings["harness"],
                expected_version=record.protocol_version,
                expected_sha256=record.protocol_hash,
            )
            skill_path = Path(bindings["project_root"]) / ".agents" / "skills" / "teamcraft" / "SKILL.md"
            skill_chain = (
                Path(bindings["project_root"]) / ".agents",
                Path(bindings["project_root"]) / ".agents" / "skills",
                Path(bindings["project_root"]) / ".agents" / "skills" / "teamcraft",
                skill_path,
            )
            if any(path.is_symlink() for path in skill_chain):
                raise CEOInitializationError("TeamCraft runtime Skill path must not contain symbolic links")
            skill_bytes = skill_path.read_bytes()
            skill_bytes.decode("utf-8")
            current_skill_hash = hashlib.sha256(skill_bytes).hexdigest()
            current_snapshot = project_task_snapshot_digest(conn, project_id)
            bound = (bindings["team_id"] == record.team_id
                and bindings["teamcraft_session_id"] == record.teamcraft_session_id
                and bindings["herdr_session_identity_json"] == record.herdr_session_identity_json
                and (record.native_session_id is None
                     or bindings["native_session_id"] == record.native_session_id)
                and bindings["terminal_id"] == record.terminal_id
                and bindings["protocol_version"] == record.protocol_version
                and bindings["protocol_hash"] == record.protocol_hash
                and protocol.version == record.protocol_version
                and protocol.sha256 == record.protocol_hash
                and current_skill_hash == record.runtime_skill_hash
                and current_snapshot == record.snapshot_digest)
        except Exception:
            bound = False
        if not bound:
            conn.execute(
                "UPDATE ceo_initializations SET state='failed', nonce_consumed_at=?, detail=?, updated_at=? WHERE project_id=?",
                (_iso(now), "CEO identity, runtime Skill, protocol, or project/Task snapshot drifted before bootstrap", _iso(now), project_id),
            )
            events.append_event(conn, "ceo.initialization.failed", project_id=project_id,
                payload_json=_json({"reason": "bootstrap_binding_drift"}), clock=clock)
            result = get_initialization(conn, project_id)
            assert result is not None
            return result
        conn.execute(
            "UPDATE ceo_initializations SET state='unknown', detail=?, updated_at=? WHERE project_id=?",
            ("Herdr prompt submission is not a trusted protocol receipt", _iso(now), project_id),
        )
        events.append_event(conn, "ceo.initialization.unknown", project_id=project_id,
            payload_json=_json({"reason": "bootstrap_prompt_admitted; nonce_pending_agent_claim; trusted_receipt_unavailable"}), clock=clock)
        result = get_initialization(conn, project_id)
        assert result is not None
        return result


def record_bootstrap_submitted_assumed(
    conn: TeamCraftConnection,
    project_id: str,
    nonce: str,
    *,
    native_session_id: str,
    agent_session: Any = None,
    session_state: str,
    lease: leader.Lease,
    clock: Clock | None = None,
) -> CEOInitialization:
    """Accept successful bootstrap transport under the explicit default assumption.

    Call only after the existing Herdr adapter returned success and the caller
    rechecked the same live CEO occupant.  The post-submit native Session ID
    is pinned exactly once together with the TeamCraft Session and
    initialization record.  This records ``assumed_default``; it does not
    claim a host-trusted Protocol/Skill receipt was observed.
    """
    if not isinstance(native_session_id, str) or not native_session_id.strip():
        raise ValidationError("post-bootstrap native Session ID must be a non-empty string")
    native_session_id = native_session_id.strip()
    if agent_session is None:
        agent_session = {"value": native_session_id}
    if (not isinstance(agent_session, dict)
            or agent_session.get("value") != native_session_id):
        raise ValidationError(
            "post-bootstrap Agent Session identity must match its native Session ID"
        )
    if session_state not in {"idle", "working"}:
        raise ValidationError("post-bootstrap CEO Session must be idle or working")
    now = _now(clock)
    with storage.transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        record = get_initialization(conn, project_id)
        row = conn.execute(
            "SELECT nonce_hash, nonce_expires_at, nonce_consumed_at FROM ceo_initializations WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if (record is None or record.state != "unknown" or row is None
                or row[2] is not None
                or hashlib.sha256(nonce.encode("utf-8")).hexdigest() != str(row[0])):
            raise CEOInitializationReplayError("bootstrap submission does not match a pending challenge")
        if datetime.fromisoformat(str(row[1])) <= now:
            raise CEOInitializationReplayError("bootstrap challenge expired before submission was recorded")
        bindings = _ceo_bindings(
            conn, project_id, record.canonical_ceo_name,
            require_native_session=False, require_live_session=False,
        )
        if (bindings["team_id"] != record.team_id
                or bindings["teamcraft_session_id"] != record.teamcraft_session_id
                or bindings["herdr_session_identity_json"] != record.herdr_session_identity_json
                or (record.native_session_id is not None
                    and record.native_session_id != native_session_id)
                or (bindings["native_session_id"] is not None
                    and bindings["native_session_id"] != native_session_id)
                or bindings["terminal_id"] != record.terminal_id
                or bindings["protocol_version"] != record.protocol_version
                or bindings["protocol_hash"] != record.protocol_hash
                or project_task_snapshot_digest(conn, project_id) != record.snapshot_digest):
            raise CEOInitializationGateClosed("CEO bootstrap bindings drifted before assumed acceptance")
        protocol = ceo_protocol.discover_protocol(
            bindings["project_root"], bindings["harness"],
            expected_version=record.protocol_version, expected_sha256=record.protocol_hash,
        )
        skill_path = Path(bindings["project_root"]) / ".agents" / "skills" / "teamcraft" / "SKILL.md"
        if (protocol.sha256 != record.protocol_hash or skill_path.is_symlink()
                or hashlib.sha256(skill_path.read_bytes()).hexdigest() != record.runtime_skill_hash):
            raise CEOInitializationGateClosed("CEO Protocol or runtime Skill drifted before assumed acceptance")
        updated_member = conn.execute(
            "UPDATE members SET agent_session_json=CASE WHEN agent_session_json IS NULL "
            "THEN ? ELSE agent_session_json END, updated_at=? "
            "WHERE name=? AND session_id=? AND (agent_session_json IS NULL OR "
            "json_extract(agent_session_json, '$.value')=?)",
            (_json(agent_session), _iso(now), record.canonical_ceo_name,
             record.teamcraft_session_id, native_session_id),
        )
        if updated_member.rowcount != 1:
            raise CEOInitializationGateClosed(
                "CEO member changed before post-bootstrap native Session binding"
            )
        updated_session = conn.execute(
            "UPDATE sessions SET native_session_id=?, state=?, last_observed_at=? "
            "WHERE id=? AND attempt_id=? AND (native_session_id IS NULL OR native_session_id=?)",
            (native_session_id, session_state, _iso(now), record.teamcraft_session_id,
             bindings["attempt_id"], native_session_id),
        )
        if updated_session.rowcount != 1:
            raise CEOInitializationGateClosed(
                "CEO TeamCraft Session changed before post-bootstrap native Session binding"
            )
        detail = "bootstrap transport succeeded; trusted host receipt assumed by temporary default"
        updated_initialization = conn.execute(
            "UPDATE ceo_initializations SET state='initialized', receipt_source='assumed_default', "
            "native_session_id=?, nonce_consumed_at=?, detail=?, updated_at=? "
            "WHERE project_id=? AND state='unknown' AND "
            "(native_session_id IS NULL OR native_session_id=?)",
            (native_session_id, _iso(now), detail, _iso(now), project_id, native_session_id),
        )
        if updated_initialization.rowcount != 1:
            raise CEOInitializationGateClosed(
                "CEO initialization changed before native Session acceptance"
            )
        events.append_event(conn, "ceo.initialization.assumed", project_id=project_id,
            payload_json=_json({"state": "initialized", "receipt_source": "assumed_default",
                                "native_session_bound": True, "detail": detail}), clock=clock)
        result = get_initialization(conn, project_id)
        assert result is not None
        return result


def reconcile_bootstrap_submitted_native_session(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    native_session_id: str,
    agent_session: Any,
    session_state: str,
    pane_id: str,
    terminal_id: str,
    harness: str,
    herdr_session_identity_hash: str,
    actor: str,
    reason: str,
    lease: leader.Lease,
    clock: Clock | None = None,
) -> CEOInitialization:
    """Bind a native Session that appeared after bootstrap reconciliation timed out.

    Recovery is allowed only for the exact UNKNOWN initialization whose
    version-bound bootstrap Prompt is already durably confirmed as submitted.
    It performs no external effect and never resends the Prompt.
    """
    if not isinstance(native_session_id, str) or not native_session_id.strip():
        raise ValidationError("native-session reconciliation requires a native Session ID")
    native_session_id = native_session_id.strip()
    if (not isinstance(agent_session, dict)
            or agent_session.get("value") != native_session_id):
        raise ValidationError(
            "live Agent Session identity must match the native Session ID"
        )
    if session_state not in {"idle", "working"}:
        raise ValidationError("native-session reconciliation requires an idle or working CEO")
    observed = {
        "native_session_id": native_session_id,
        "agent_session": agent_session,
        "pane_id": pane_id,
        "terminal_id": terminal_id,
        "harness": harness,
        "herdr_session_identity_hash": herdr_session_identity_hash,
    }
    for key in ("pane_id", "terminal_id", "harness", "herdr_session_identity_hash"):
        value = observed[key]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"native-session reconciliation {key} must be non-empty")
        observed[key] = value.strip()
    if (len(observed["herdr_session_identity_hash"]) != 64
            or any(ch not in "0123456789abcdef"
                   for ch in observed["herdr_session_identity_hash"])):
        raise ValidationError("native-session Herdr identity hash must be SHA-256")
    if not isinstance(actor, str) or not actor.strip():
        raise ValidationError("native-session reconciliation requires an actor")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("native-session reconciliation requires a reason")

    now = _now(clock)
    observed_json = _json(observed)
    observed_digest = hashlib.sha256(observed_json.encode("utf-8")).hexdigest()
    with storage.transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        record = get_initialization(conn, project_id)
        if record is None:
            raise CEOInitializationGateClosed("CEO initialization challenge is missing")
        if (record.state != "unknown" or record.receipt_source != "unverified"
                or record.native_session_id is not None
                or record.nonce_consumed_at is not None):
            raise CEOInitializationGateClosed(
                "native-session reconciliation requires one unconsumed UNKNOWN initialization"
            )
        bindings = _ceo_bindings(
            conn, project_id, record.canonical_ceo_name,
            require_native_session=False, require_live_session=False,
        )
        expected_socket_hash = hashlib.sha256(
            record.herdr_session_identity_json.encode("utf-8")
        ).hexdigest()
        if (bindings["team_id"] != record.team_id
                or bindings["teamcraft_session_id"] != record.teamcraft_session_id
                or bindings["herdr_session_identity_json"] != record.herdr_session_identity_json
                or bindings["native_session_id"] is not None
                or bindings["terminal_id"] != record.terminal_id
                or bindings["protocol_version"] != record.protocol_version
                or bindings["protocol_hash"] != record.protocol_hash
                or observed["pane_id"] != bindings["pane_id"]
                or observed["terminal_id"] != record.terminal_id
                or observed["harness"] != bindings["harness"]
                or observed["herdr_session_identity_hash"] != expected_socket_hash):
            raise CEOInitializationGateClosed(
                "native-session reconciliation binding drifted"
            )

        prompts = conn.execute(
            "SELECT c.id,c.state,p.state,c.requested_at FROM commands c "
            "JOIN prompt_deliveries p ON p.command_id=c.id "
            "WHERE c.kind='agent.prompt' AND json_extract(c.payload_json,'$.member')=? "
            "AND json_extract(c.payload_json,'$.attempt_id')=? "
            "ORDER BY c.requested_at,c.id",
            (record.canonical_ceo_name, bindings["attempt_id"]),
        ).fetchall()
        if (len(prompts) != 1
                or (str(prompts[0][1]), str(prompts[0][2]))
                != ("confirmed", "submitted")):
            raise CEOInitializationGateClosed(
                "native-session reconciliation requires exactly one confirmed submitted "
                "bootstrap transport"
            )
        prompt = prompts[0]
        try:
            prompt_requested = datetime.fromisoformat(str(prompt[3]))
            challenge_expires = datetime.fromisoformat(str(record.nonce_expires_at))
            challenge_created = datetime.fromisoformat(record.created_at)
        except (TypeError, ValueError) as exc:
            raise CEOInitializationGateClosed(
                "bootstrap Prompt or challenge time binding is malformed"
            ) from exc
        if (prompt_requested.tzinfo is None or challenge_expires.tzinfo is None
                or challenge_created.tzinfo is None
                or prompt_requested < challenge_created
                or prompt_requested > challenge_expires):
            raise CEOInitializationGateClosed(
                "bootstrap Prompt was not submitted within its challenge lifetime"
            )

        protocol = ceo_protocol.discover_protocol(
            bindings["project_root"], bindings["harness"],
            expected_version=record.protocol_version,
            expected_sha256=record.protocol_hash,
        )
        skill_path = (
            Path(bindings["project_root"]) / ".agents" / "skills" / "teamcraft" / "SKILL.md"
        )
        if (protocol.sha256 != record.protocol_hash or skill_path.is_symlink()
                or hashlib.sha256(skill_path.read_bytes()).hexdigest()
                != record.runtime_skill_hash):
            raise CEOInitializationGateClosed("CEO Protocol or runtime Skill drifted")

        payload = _json({
            "project_id": project_id,
            "ceo_name": record.canonical_ceo_name,
            "prompt_command_id": str(prompt[0]),
            "observed_identity_digest": observed_digest,
        })
        submission = commands.submit_command(
            conn,
            idempotency_key=(
                f"{project_id}:ceo.identity.native_session:{observed_digest}"
            ),
            kind="ceo.identity.native_session",
            payload_json=payload,
            evidence_json="{}",
            clock=clock,
        )
        command = submission.command
        if command.state == "pending":
            command = commands.claim_command(conn, command.id, clock=clock)
        if command.state != "running":
            raise CEOInitializationGateClosed(
                f"native-session identity Command is {command.state}; refusing mutation"
            )

        updated_member = conn.execute(
            "UPDATE members SET agent_session_json=?, live_state=?, updated_at=? "
            "WHERE name=? AND session_id=? AND agent_session_json IS NULL",
            (_json(agent_session), session_state, _iso(now),
             record.canonical_ceo_name, record.teamcraft_session_id),
        )
        if updated_member.rowcount != 1:
            raise CEOInitializationGateClosed(
                "CEO member changed before native-session reconciliation"
            )
        updated_session = conn.execute(
            "UPDATE sessions SET native_session_id=?, state=?, last_observed_at=? "
            "WHERE id=? AND attempt_id=? AND native_session_id IS NULL",
            (native_session_id, session_state, _iso(now), record.teamcraft_session_id,
             bindings["attempt_id"]),
        )
        if updated_session.rowcount != 1:
            raise CEOInitializationGateClosed(
                "CEO Session changed before native-session reconciliation"
            )
        detail = (
            "confirmed bootstrap transport reconciled to the delayed native Session report"
        )
        updated = conn.execute(
            "UPDATE ceo_initializations SET state='initialized', "
            "receipt_source='assumed_default', identity_mode='native', "
            "native_session_id=?, live_process_identity_json=NULL, nonce_consumed_at=?, "
            "detail=?, updated_at=? WHERE project_id=? AND state='unknown' "
            "AND receipt_source='unverified' AND native_session_id IS NULL "
            "AND nonce_consumed_at IS NULL",
            (native_session_id, _iso(now), detail, _iso(now), project_id),
        )
        if updated.rowcount != 1:
            raise CEOInitializationGateClosed(
                "CEO initialization changed before native-session reconciliation"
            )
        commands.confirm_command(
            conn, command.id,
            result_json=_json({
                "identity_mode": "native",
                "native_session_id": native_session_id,
                "observed_identity_digest": observed_digest,
            }),
            note=f"approved by {actor.strip()}: {reason.strip()}",
            clock=clock,
        )
        events.append_event(
            conn, "ceo.initialization.native_session_assumed", project_id=project_id,
            payload_json=_json({
                "state": "initialized", "identity_mode": "native",
                "native_session_id": native_session_id,
                "observed_identity_digest": observed_digest,
                "prompt_command_id": str(prompt[0]),
            }), clock=clock,
        )
        result = get_initialization(conn, project_id)
        assert result is not None
        return result


_LIVE_PROCESS_KEYS = frozenset({
    "pid", "process_start", "process_name", "argv", "cwd", "pane_id",
    "terminal_id", "harness", "herdr_session_identity_hash",
})


def normalize_live_process_identity(value: Any) -> dict[str, Any]:
    """Validate the non-portable identity of one already-running Harness.

    PID alone is vulnerable to reuse.  The start fingerprint is obtained from
    the host OS and is bound together with the Herdr socket identity, pane,
    terminal, Harness kind, and observable process metadata.  This record is
    deliberately not a native Session reference and cannot be resumed.
    """
    if not isinstance(value, dict) or set(value) != _LIVE_PROCESS_KEYS:
        raise ValidationError("live process identity has an invalid field set")
    pid = value.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValidationError("live process identity PID must be a positive integer")
    for key in (
        "process_start", "process_name", "cwd", "pane_id", "terminal_id",
        "harness", "herdr_session_identity_hash",
    ):
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"live process identity {key} must be non-empty")
    if (len(value["herdr_session_identity_hash"]) != 64
            or any(ch not in "0123456789abcdef" for ch in value["herdr_session_identity_hash"])):
        raise ValidationError("live process Herdr identity hash must be SHA-256")
    argv = value.get("argv")
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(item, str) or not item for item in argv)):
        raise ValidationError("live process identity argv must be a non-empty string list")
    return {
        "pid": pid,
        "process_start": value["process_start"].strip(),
        "process_name": value["process_name"].strip(),
        "argv": list(argv),
        "cwd": value["cwd"].strip(),
        "pane_id": value["pane_id"].strip(),
        "terminal_id": value["terminal_id"].strip(),
        "harness": value["harness"].strip(),
        "herdr_session_identity_hash": value["herdr_session_identity_hash"],
    }


def record_bootstrap_submitted_live_process(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    live_process_identity: dict[str, Any],
    session_state: str,
    actor: str,
    reason: str,
    lease: leader.Lease,
    clock: Clock | None = None,
) -> CEOInitialization:
    """Explicitly admit an already-submitted bootstrap for this process only.

    The latest version-bound Prompt must already be durably submitted and its
    Command confirmed.  This function performs no external effect and never
    populates either native Session field.  The resulting mode is valid only
    while the caller re-observes the exact process binding.
    """
    identity = normalize_live_process_identity(live_process_identity)
    if session_state not in {"idle", "working"}:
        raise ValidationError("live-process CEO Session must be idle or working")
    if not isinstance(actor, str) or not actor.strip():
        raise ValidationError("live-process reconciliation requires an actor")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("live-process reconciliation requires a reason")
    now = _now(clock)
    identity_json = _json(identity)
    identity_digest = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
    with storage.transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        record = get_initialization(conn, project_id)
        if record is None:
            raise CEOInitializationGateClosed("CEO initialization challenge is missing")
        if record.state == "initialized":
            if (record.identity_mode == "live_process"
                    and record.live_process_identity_json == identity_json):
                return require_initialized(conn, project_id, allow_live_process=True)
            raise CEOInitializationGateClosed("CEO initialization is already bound to another identity")
        if record.state != "unknown" or record.native_session_id is not None:
            raise CEOInitializationGateClosed(
                "live-process reconciliation requires UNKNOWN initialization without a native Session"
            )
        bindings = _ceo_bindings(
            conn, project_id, record.canonical_ceo_name,
            require_native_session=False, require_live_session=False,
        )
        if (bindings["team_id"] != record.team_id
                or bindings["teamcraft_session_id"] != record.teamcraft_session_id
                or bindings["herdr_session_identity_json"] != record.herdr_session_identity_json
                or bindings["native_session_id"] is not None
                or bindings["terminal_id"] != record.terminal_id
                or bindings["protocol_version"] != record.protocol_version
                or bindings["protocol_hash"] != record.protocol_hash
                or identity["pane_id"] != bindings["pane_id"]
                or identity["terminal_id"] != record.terminal_id
                or identity["harness"] != bindings["harness"]
                or identity["herdr_session_identity_hash"] != hashlib.sha256(
                    record.herdr_session_identity_json.encode("utf-8")
                ).hexdigest()):
            raise CEOInitializationGateClosed("live-process reconciliation binding drifted")
        prompt = conn.execute(
            "SELECT c.id,c.state,p.state,c.requested_at FROM commands c "
            "JOIN prompt_deliveries p ON p.command_id=c.id "
            "WHERE c.kind='agent.prompt' AND json_extract(c.payload_json,'$.member')=? "
            "AND json_extract(c.payload_json,'$.attempt_id')=? "
            "ORDER BY c.requested_at DESC,c.id DESC LIMIT 1",
            (record.canonical_ceo_name, bindings["attempt_id"]),
        ).fetchone()
        if prompt is None or (str(prompt[1]), str(prompt[2])) != ("confirmed", "submitted"):
            raise CEOInitializationGateClosed(
                "live-process reconciliation requires confirmed submitted bootstrap transport"
            )
        try:
            process_started = datetime.fromisoformat(identity["process_start"])
            prompt_requested = datetime.fromisoformat(str(prompt[3]))
        except ValueError as exc:
            raise CEOInitializationGateClosed(
                "live-process or Prompt time binding is malformed"
            ) from exc
        if process_started.tzinfo is None or prompt_requested.tzinfo is None:
            raise CEOInitializationGateClosed(
                "live-process and Prompt times must include timezone offsets"
            )
        prompt_floor = prompt_requested.replace(microsecond=0)
        if process_started >= prompt_floor:
            raise CEOInitializationGateClosed(
                "live Harness process did not start strictly before the submitted bootstrap; "
                "refusing retroactive adoption"
            )
        protocol = ceo_protocol.discover_protocol(
            bindings["project_root"], bindings["harness"],
            expected_version=record.protocol_version,
            expected_sha256=record.protocol_hash,
        )
        skill_path = Path(bindings["project_root"]) / ".agents" / "skills" / "teamcraft" / "SKILL.md"
        if (protocol.sha256 != record.protocol_hash or skill_path.is_symlink()
                or hashlib.sha256(skill_path.read_bytes()).hexdigest() != record.runtime_skill_hash):
            raise CEOInitializationGateClosed("CEO Protocol or runtime Skill drifted")
        payload = _json({
            "project_id": project_id,
            "ceo_name": record.canonical_ceo_name,
            "prompt_command_id": str(prompt[0]),
            "identity_digest": identity_digest,
        })
        submission = commands.submit_command(
            conn,
            idempotency_key=f"{project_id}:ceo.identity.live_process:{identity_digest}",
            kind="ceo.identity.live_process",
            payload_json=payload,
            evidence_json="{}",
            clock=clock,
        )
        command = submission.command
        if command.state == "pending":
            command = commands.claim_command(conn, command.id, clock=clock)
        if command.state != "running":
            raise CEOInitializationGateClosed(
                f"live-process identity Command is {command.state}; refusing mutation"
            )
        updated_session = conn.execute(
            "UPDATE sessions SET state=?, last_observed_at=? WHERE id=? "
            "AND attempt_id=? AND native_session_id IS NULL",
            (session_state, _iso(now), record.teamcraft_session_id, bindings["attempt_id"]),
        )
        if updated_session.rowcount != 1:
            raise CEOInitializationGateClosed("CEO Session changed before live-process binding")
        detail = (
            "bootstrap transport submitted; explicitly bound to current live process only; "
            "no server/process restore or Wake is permitted"
        )
        updated = conn.execute(
            "UPDATE ceo_initializations SET state='initialized', "
            "receipt_source='assumed_default', identity_mode='live_process', "
            "live_process_identity_json=?, nonce_consumed_at=?, detail=?, updated_at=? "
            "WHERE project_id=? AND state='unknown' AND native_session_id IS NULL",
            (identity_json, _iso(now), detail, _iso(now), project_id),
        )
        if updated.rowcount != 1:
            raise CEOInitializationGateClosed("CEO initialization changed before live-process binding")
        commands.confirm_command(
            conn, command.id,
            result_json=_json({"identity_mode": "live_process", "identity_digest": identity_digest}),
            note=f"approved by {actor.strip()}: {reason.strip()}",
            clock=clock,
        )
        events.append_event(
            conn, "ceo.initialization.live_process_assumed", project_id=project_id,
            payload_json=_json({
                "state": "initialized", "identity_mode": "live_process",
                "identity_digest": identity_digest, "portable": False,
            }), clock=clock,
        )
        result = get_initialization(conn, project_id)
        assert result is not None
        return result


def record_agent_claim(
    conn: TeamCraftConnection,
    project_id: str,
    claim_json: str,
    *,
    lease: leader.Lease,
    clock: Clock | None = None,
) -> CEOInitialization:
    """Store an Agent self-claim as unverified evidence; it can never initialize the project."""
    if not isinstance(claim_json, str):
        raise ValidationError("Agent initialization claim must be a JSON string")
    try:
        claim_size = len(claim_json.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValidationError("Agent initialization claim is not valid Unicode") from exc
    if claim_size > MAX_AGENT_CLAIM_BYTES:
        raise ValidationError(f"Agent initialization claim must be at most {MAX_AGENT_CLAIM_BYTES} UTF-8 bytes")
    try:
        claim = json.loads(claim_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Agent initialization claim is not JSON: {exc}") from exc
    if not isinstance(claim, dict):
        raise ValidationError("Agent initialization claim must be a JSON object")
    now = _now(clock)
    with storage.transaction(conn):
        leader.require_project_lease(conn, lease, project_id, clock=clock)
        record = get_initialization(conn, project_id)
        if record is None:
            raise CEOInitializationReplayError("no CEO initialization challenge exists")
        row = conn.execute(
            "SELECT nonce_hash, nonce_expires_at, nonce_consumed_at FROM ceo_initializations WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise CEOInitializationReplayError("CEO initialization nonce is missing")
        if record.state not in {"discovered", "unknown"} or row[2] is not None:
            events.append_event(conn, "ceo.initialization.replay_rejected", project_id=project_id,
                payload_json=_json({"reason": "nonce_already_consumed_or_not_pending"}), clock=clock)
            return record
        nonce = claim.get("nonce")
        if not isinstance(nonce, str) or hashlib.sha256(nonce.encode("utf-8")).hexdigest() != str(row[0]):
            events.append_event(conn, "ceo.initialization.replay_rejected", project_id=project_id,
                payload_json=_json({"reason": "nonce_mismatch"}), clock=clock)
            return record
        expired = datetime.fromisoformat(str(row[1])) <= now
        expected = {
            "project_id": record.project_id,
            "team_id": record.team_id,
            "herdr_session_identity_hash": hashlib.sha256(
                record.herdr_session_identity_json.encode("utf-8")
            ).hexdigest(),
            "teamcraft_session_id": record.teamcraft_session_id,
            "herdr_session_id": record.native_session_id,
            "terminal_id": record.terminal_id,
            "protocol_version": record.protocol_version,
            "protocol_hash": record.protocol_hash,
            "runtime_skill_hash": record.runtime_skill_hash,
            "snapshot_digest": record.snapshot_digest,
        }
        mismatches = [key for key, value in expected.items() if claim.get(key) != value]
        try:
            bindings = _ceo_bindings(
                conn, project_id, record.canonical_ceo_name,
                require_native_session=False, require_live_session=False,
            )
            if (bindings["team_id"] != record.team_id
                    or bindings["teamcraft_session_id"] != record.teamcraft_session_id
                    or bindings["herdr_session_identity_json"] != record.herdr_session_identity_json
                    or (record.native_session_id is not None
                        and bindings["native_session_id"] != record.native_session_id)
                    or bindings["terminal_id"] != record.terminal_id
                    or bindings["protocol_version"] != record.protocol_version
                    or bindings["protocol_hash"] != record.protocol_hash):
                mismatches.append("current_session_identity")
        except Exception:
            mismatches.append("current_session_identity")
        incompatible = any(key in {"protocol_version", "protocol_hash"} for key in mismatches)
        state = "failed" if expired or (mismatches and not incompatible) else (
            "incompatible" if incompatible else "read_unverified")
        receipt_source = "unverified"
        detail = "nonce expired" if expired else ("claim binding mismatch: " + ",".join(mismatches) if mismatches else
            "Agent self-claim retained as audit material; initialization acceptance depends on the explicit default assumption")
        conn.execute(
            "UPDATE ceo_initializations SET state=?, nonce_consumed_at=?, audit_material_json=?, detail=?, receipt_source=?, updated_at=? WHERE project_id=?",
            (state, _iso(now), claim_json, detail, receipt_source, _iso(now), project_id),
        )
        event_kind = {
            "failed": "ceo.initialization.failed",
            "incompatible": "ceo.initialization.incompatible",
            "read_unverified": "ceo.initialization.read_unverified",
        }[state]
        events.append_event(conn, event_kind, project_id=project_id,
            payload_json=_json({"state": state, "detail": detail, "receipt_source": receipt_source}), clock=clock)
        result = get_initialization(conn, project_id)
        assert result is not None
        return result


def require_initialized(
    conn: TeamCraftConnection,
    project_id: str,
    *,
    allow_live_process: bool = False,
) -> CEOInitialization:
    """Require a current initialization.

    The default is deliberately portable/native only because Core Scheduler
    and Wake paths cannot revalidate a host process incarnation.  The in-pane
    CLI may opt into ``allow_live_process`` and must then perform an immediate
    OS/Herdr process recheck before every external effect.
    """
    record = get_initialization(conn, project_id)
    if record is None:
        raise CEOInitializationGateClosed("CEO initialization is missing; Scheduler dispatch is blocked")
    if record.state != "initialized" or record.receipt_source not in {"assumed_default", "host_verified"}:
        raise CEOInitializationGateClosed(
            f"CEO initialization state/source is {record.state}/{record.receipt_source}; dispatch is blocked"
        )
    if (record.nonce_expires_at is None or record.nonce_consumed_at is None
            or record.protocol_version is None or record.protocol_hash is None
            or record.runtime_skill_hash is None or record.snapshot_digest is None):
        raise CEOInitializationGateClosed("initialized CEO record is missing its consumed challenge bindings")
    if record.identity_mode == "live_process" and not allow_live_process:
        raise CEOInitializationGateClosed(
            "CEO uses non-portable live-process identity; Scheduler/Wake/restore dispatch is blocked"
        )
    if record.identity_mode not in {"native", "live_process"}:
        raise CEOInitializationGateClosed("CEO initialization identity mode is invalid")
    if record.identity_mode == "live_process":
        if record.native_session_id is not None or record.live_process_identity_json is None:
            raise CEOInitializationGateClosed("live-process CEO identity binding is incomplete")
        try:
            normalize_live_process_identity(json.loads(record.live_process_identity_json))
        except (ValueError, ValidationError) as exc:
            raise CEOInitializationGateClosed("live-process CEO identity binding is malformed") from exc
    elif record.native_session_id is None or record.live_process_identity_json is not None:
        raise CEOInitializationGateClosed("native CEO identity binding is incomplete")
    bindings = _ceo_bindings(
        conn, project_id, record.canonical_ceo_name,
        require_native_session=record.identity_mode == "native",
    )
    if (bindings["team_id"] != record.team_id
            or bindings["teamcraft_session_id"] != record.teamcraft_session_id
            or bindings["herdr_session_identity_json"] != record.herdr_session_identity_json
            or bindings["native_session_id"] != record.native_session_id
            or bindings["terminal_id"] != record.terminal_id
            or bindings["protocol_version"] != record.protocol_version
            or bindings["protocol_hash"] != record.protocol_hash):
        raise CEOInitializationGateClosed("CEO initialization identity drifted; dispatch is blocked")
    try:
        protocol = ceo_protocol.discover_protocol(
            bindings["project_root"], bindings["harness"],
            expected_version=record.protocol_version, expected_sha256=record.protocol_hash,
        )
        skill_path = Path(bindings["project_root"]) / ".agents" / "skills" / "teamcraft" / "SKILL.md"
        if skill_path.is_symlink() or hashlib.sha256(skill_path.read_bytes()).hexdigest() != record.runtime_skill_hash:
            raise CEOInitializationGateClosed("TeamCraft runtime Skill identity drifted; dispatch is blocked")
    except (ceo_protocol.ProtocolError, OSError) as exc:
        raise CEOInitializationGateClosed(f"initialized CEO protocol/runtime Skill is unavailable: {exc}") from exc
    if protocol.sha256 != record.protocol_hash:
        raise CEOInitializationGateClosed("CEO protocol identity drifted; dispatch is blocked")
    return record
