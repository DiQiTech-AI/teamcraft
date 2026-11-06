"""Project-scoped CEO protocol initialization state (M2-08).

This service persists discovery and one-use handshake challenges without
creating a Task or Attempt. A matching Agent-authored JSON claim is retained
only as ``read_unverified`` audit material. The current Herdr/Harness adapter
has no host-trusted Protocol/Skill receipt, so this module deliberately has
no operation that can promote a claim to ``initialized``.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ceo_protocol, domain, events, leader, storage
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
    created_at: str
    updated_at: str


_COLUMNS = (
    "project_id, canonical_ceo_name, state, team_id, teamcraft_session_id, "
    "herdr_session_identity_json, native_session_id, terminal_id, "
    "protocol_version, protocol_hash, runtime_skill_hash, snapshot_digest, "
    "nonce_expires_at, nonce_consumed_at, audit_material_json, detail, receipt_source, created_at, updated_at"
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
        None if row[15] is None else str(row[15]), str(row[16]), str(row[17]), str(row[18]),
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
    """Persist an early protocol/session discovery failure when no row exists."""
    if state not in {"failed", "incompatible"}:
        raise ValidationError("discovery failure state must be failed or incompatible")
    now = _iso(_now(clock))
    with storage.transaction(conn):
        existing = get_initialization(conn, project_id)
        if existing is not None:
            return existing
        project = domain.get_project(conn, project_id)
        meta = conn.execute(
            "SELECT team_id, herdr_session_json FROM runtime_meta WHERE id=1",
        ).fetchone()
        team_id = "" if meta is None else str(meta[0])
        identity_json = "{}" if meta is None else str(meta[1])
        conn.execute(
            "INSERT INTO ceo_initializations (project_id, canonical_ceo_name, state, team_id, "
            "teamcraft_session_id, herdr_session_identity_json, native_session_id, terminal_id, "
            "protocol_version, protocol_hash, runtime_skill_hash, snapshot_digest, nonce_hash, "
            "nonce_expires_at, nonce_consumed_at, audit_material_json, detail, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)",
            (project.id, ceo_name, state, team_id, identity_json, detail[:2048], now, now),
        )
        events.append_event(conn, f"ceo.initialization.{state}", project_id=project_id,
            payload_json=_json({"ceo_name": ceo_name, "detail": detail[:2048]}), clock=clock)
        return get_initialization(conn, project_id)


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
    conn: TeamCraftConnection, project_id: str, ceo_name: str,
) -> dict[str, str]:
    project = domain.get_project(conn, project_id)
    rows = conn.execute(
        "SELECT m.name, m.kind, m.task_id, m.attempt_id, m.session_id, m.terminal_id, "
        "s.native_session_id, s.attempt_id, t.project_id "
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
            or row[6] is None or not str(row[6]).strip()
            or row[3] is None or row[7] is None or str(row[3]) != str(row[7])):
        raise CEOInitializationError(
            "CEO TeamCraft Session, Herdr native Session, terminal, and Attempt binding are required"
        )
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
    if session.attempt_id != str(row[3]) or session.native_session_id != str(row[6]):
        raise CEOInitializationError("CEO TeamCraft Session does not match its Herdr native Session")
    if session.state not in {"idle", "working"}:
        raise CEOInitializationError(
            f"CEO Session liveness is {session.state}; unknown, missing, or exited CEO cannot initialize"
        )
    return {
        "team_id": str(meta[0]), "teamcraft_session_id": str(row[4]),
        "herdr_session_identity_json": _json(identity), "native_session_id": str(row[6]),
        "terminal_id": str(row[5]), "protocol_version": attempt.protocol_version,
        "protocol_hash": attempt.protocol_hash, "harness": str(row[1]),
        "project_root": project.repo_path,
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
        bindings = _ceo_bindings(conn, project_id, ceo_name)
        existing = get_initialization(conn, project_id)
        if existing is not None:
            if existing.canonical_ceo_name != ceo_name:
                raise CEOInitializationGateClosed(
                    "project CEO identity is already pinned; refusing to create or bootstrap a second CEO"
                )
            if existing.state == "initialized":
                return require_initialized(conn, project_id), None
            raise CEOInitializationGateClosed(
                f"CEO initialization is {existing.state}; do not create a second CEO or resend bootstrap"
            )
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
        conn.execute(
            "INSERT INTO ceo_initializations (project_id, canonical_ceo_name, state, team_id, "
            "teamcraft_session_id, herdr_session_identity_json, native_session_id, terminal_id, "
            "protocol_version, protocol_hash, runtime_skill_hash, snapshot_digest, nonce_hash, "
            "nonce_expires_at, nonce_consumed_at, audit_material_json, detail, created_at, updated_at) "
            "VALUES (?, ?, 'discovered', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
            (project_id, ceo_name, bindings["team_id"], bindings["teamcraft_session_id"],
             bindings["herdr_session_identity_json"], bindings["native_session_id"],
             bindings["terminal_id"], protocol.version, protocol.sha256,
             hashlib.sha256(skill_bytes).hexdigest(), snapshot, nonce_hash, expires,
             _iso(now), _iso(now)),
        )
        events.append_event(conn, "ceo.initialization.discovered", project_id=project_id,
            payload_json=_json({"ceo_name": ceo_name, "state": "discovered",
                "protocol_version": protocol.version, "protocol_hash": protocol.sha256,
                "snapshot_digest": snapshot, "nonce_expires_at": expires}), clock=clock)
        record = get_initialization(conn, project_id)
        assert record is not None
        return record, nonce


def bootstrap_prompt_fields(record: CEOInitialization, nonce: str) -> dict[str, str]:
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
        "herdr_session_id": str(record.native_session_id),
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
            bindings = _ceo_bindings(conn, project_id, record.canonical_ceo_name)
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
                and bindings["native_session_id"] == record.native_session_id
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
    lease: leader.Lease,
    clock: Clock | None = None,
) -> CEOInitialization:
    """Accept successful bootstrap transport under the explicit default assumption.

    Call only after the existing Herdr adapter returned success and the caller
    rechecked the same live CEO occupant.  This records ``assumed_default``;
    it does not claim a host-trusted Protocol/Skill receipt was observed.
    """
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
        bindings = _ceo_bindings(conn, project_id, record.canonical_ceo_name)
        if (bindings["team_id"] != record.team_id
                or bindings["teamcraft_session_id"] != record.teamcraft_session_id
                or bindings["herdr_session_identity_json"] != record.herdr_session_identity_json
                or bindings["native_session_id"] != record.native_session_id
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
        detail = "bootstrap transport succeeded; trusted host receipt assumed by temporary default"
        conn.execute(
            "UPDATE ceo_initializations SET state='initialized', receipt_source='assumed_default', "
            "nonce_consumed_at=?, detail=?, updated_at=? WHERE project_id=? AND state='unknown'",
            (_iso(now), detail, _iso(now), project_id),
        )
        events.append_event(conn, "ceo.initialization.assumed", project_id=project_id,
            payload_json=_json({"state": "initialized", "receipt_source": "assumed_default",
                                "detail": detail}), clock=clock)
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
            bindings = _ceo_bindings(conn, project_id, record.canonical_ceo_name)
            if (bindings["team_id"] != record.team_id
                    or bindings["teamcraft_session_id"] != record.teamcraft_session_id
                    or bindings["herdr_session_identity_json"] != record.herdr_session_identity_json
                    or bindings["native_session_id"] != record.native_session_id
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


def require_initialized(conn: TeamCraftConnection, project_id: str) -> CEOInitialization:
    """Require a current initialization accepted by host evidence or explicit assumption."""
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
    bindings = _ceo_bindings(conn, project_id, record.canonical_ceo_name)
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
