"""M1-07 Runtime Command Service regressions (temp roots, synthetic data).

Covers CEO gates A (single SQLite authority), C (durable claim BEFORE the
effect, UNKNOWN on ambiguity, reconcile from facts, never blind resend) and D
(a stale leader can never commit success), plus lineage/atomicity behaviour of
:mod:`teamcraft.runtime_adapter`.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import commands as command_core
from teamcraft import leader, runtime_adapter as ra, storage
from teamcraft.errors import ValidationError
from teamcraft.storage import SchemaTooNewError

_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class _FixedClock:
    def __init__(self, offset_seconds: int = 0) -> None:
        self.moment = _NOW + timedelta(seconds=offset_seconds)

    def now(self) -> datetime:
        return self.moment


def _member(name: str, **overrides):
    member = {
        "name": name, "role": "engineer", "responsibility": f"own {name}",
        "kind": "opencode", "task": f"deliver {name} milestone",
        "ownership": [f"src/{name}/**"], "read_only": False,
        "cwd": "/synthetic/worktree", "from_pane_id": "w-1",
        "pane_id": None, "terminal_id": None, "agent_session": None,
        "agent_args": ["opencode"], "opencode_config_content": None,
        "direction": None, "start_timeout_ms": 30_000, "phase": "reserved",
        "prompt_may_have_been_delivered": False, "live_state": None,
        "error": None, "task_id": None, "attempt_id": None,
        "session_id": None, "created_at": int(_NOW.timestamp()),
        "updated_at": int(_NOW.timestamp()),
    }
    member.update(overrides)
    return member


def _state(**overrides):
    state = {
        "version": ra.STATE_VERSION,
        "team_id": "team-synthetic",
        "project_root": "/synthetic/root",
        "herdr_session": {"server_id": "srv-1",
                         "socket_path": "/synthetic/herdr.sock"},
        "created_at": int(_NOW.timestamp()),
        "members": {},
    }
    state.update(overrides)
    return state


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="m107-adapter-")
        self.root = Path(self.temp.name)
        self.conn, self.report = ra.open_runtime(self.root)

    def tearDown(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
        self.temp.cleanup()

    # ------------------------------------------------------------- opening

    def test_open_runtime_creates_verified_head_without_json(self) -> None:
        db = self.root / ra.RUNTIME_DIRNAME / ra.DATABASE_FILENAME
        self.assertTrue(db.is_file())
        raw = sqlite3.connect(db)
        try:
            self.assertEqual(
                raw.execute("PRAGMA user_version").fetchone()[0],
                storage.SCHEMA_VERSION,
            )
            tables = {
                str(row[0])
                for row in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({"leader_lease", "runtime_meta", "members"}
                            <= tables)
        finally:
            raw.close()
        self.assertFalse((self.root / ".teamcraft" / "state.json").exists())
        self.assertIsNone(ra.team_view(self.conn))

    def test_open_runtime_is_idempotent_across_reopen(self) -> None:
        ra.sync_state(self.conn, _state(members={"dev": _member("dev")}),
                     clock=_FixedClock())
        self.conn.close()
        self.conn = ra.connect_runtime(self.root)
        view = ra.team_view(self.conn)
        self.assertIsNotNone(view)
        self.assertEqual(view["team_id"], "team-synthetic")
        self.assertEqual(list(view["members"]), ["dev"])

    def test_open_runtime_refuses_future_schema(self) -> None:
        self.conn.close()
        self.conn = None
        db = self.root / ra.RUNTIME_DIRNAME / ra.DATABASE_FILENAME
        raw = sqlite3.connect(db)
        raw.execute(f"PRAGMA user_version = {storage.SCHEMA_VERSION + 5}")
        raw.commit()
        raw.close()
        with self.assertRaises(SchemaTooNewError):
            ra.open_runtime(self.root)

    def test_legacy_json_refused_and_bytes_preserved(self) -> None:
        # Gate A: fresh start keeps failing closed on non-empty legacy JSON.
        # Uses a NEVER-provisioned sub-root so "no db created" is provable.
        root = self.root / "legacy-root"
        (root / ".teamcraft").mkdir(parents=True)
        legacy = root / ".teamcraft" / "state.json"
        legacy.write_text('{"version": 1, "members": {"x": 1}}',
                         encoding="utf-8")
        before = legacy.read_bytes()
        with self.assertRaises(Exception) as ctx:
            ra.open_runtime(root)
        self.assertIn("legacy", str(ctx.exception).lower())
        self.assertEqual(legacy.read_bytes(), before)
        db = root / ".teamcraft" / ra.DATABASE_FILENAME
        self.assertFalse(db.exists())

    # ------------------------------------------------------ sync authority

    def test_sync_persists_team_members_and_lineage(self) -> None:
        state = _state(members={"dev": _member("dev")})
        ra.sync_state(self.conn, state, clock=_FixedClock())
        view = ra.team_view(self.conn)
        self.assertEqual(view["members"]["dev"]["phase"], "reserved")
        self.assertIsNotNone(view["members"]["dev"]["task_id"])
        task_id = view["members"]["dev"]["task_id"]
        task = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        self.assertEqual(str(task[0]), "ready")
        events = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE payload_json LIKE '%dev%'"
        ).fetchone()
        self.assertGreaterEqual(int(events[0]), 1)

    def test_lineage_progresses_with_member_lifecycle(self) -> None:
        state = _state(members={"dev": _member("dev")})
        ra.sync_state(self.conn, state, clock=_FixedClock())
        dev = state["members"]["dev"]
        dev.update({"phase": "split_requested"})
        ra.sync_state(self.conn, state, clock=_FixedClock(1))
        dev.update({"phase": "pane_created", "pane_id": "w-2"})
        ra.sync_state(self.conn, state, clock=_FixedClock(2))
        dev.update({"phase": "agent_started",
                   "agent_session": {"kind": "exact", "value": "s-9"},
                   "live_state": "working"})
        ra.sync_state(self.conn, state, clock=_FixedClock(3))
        dev.update({"phase": "prompt_submitted",
                   "prompt_may_have_been_delivered": True})
        ra.sync_state(self.conn, state, clock=_FixedClock(4))
        view = ra.team_view(self.conn)
        self.assertIsNotNone(view["members"]["dev"]["attempt_id"])
        self.assertIsNotNone(view["members"]["dev"]["session_id"])
        attempt = self.conn.execute(
            "SELECT status FROM attempts WHERE id = ?",
            (view["members"]["dev"]["attempt_id"],),
        ).fetchone()
        self.assertEqual(str(attempt[0]), "active")
        session = self.conn.execute(
            "SELECT state FROM sessions WHERE id = ?",
            (view["members"]["dev"]["session_id"],),
        ).fetchone()
        self.assertEqual(str(session[0]), "working")

        dev.update({"phase": "completed", "live_state": "done"})
        ra.sync_state(self.conn, state, clock=_FixedClock(5))
        attempt = self.conn.execute(
            "SELECT status FROM attempts WHERE id = ?",
            (dev["attempt_id"],),
        ).fetchone()
        self.assertEqual(str(attempt[0]), "finished")
        task = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (dev["task_id"],)
        ).fetchone()
        self.assertIn(str(task[0]), {"running", "verifying"})

    def test_team_identity_is_immutable(self) -> None:
        ra.sync_state(self.conn, _state(members={"dev": _member("dev")}),
                     clock=_FixedClock())
        with self.assertRaises(ra.RuntimeAdapterError) as ctx:
            ra.sync_state(self.conn,
                         _state(team_id="team-other",
                              members={"dev": _member("dev")}),
                         clock=_FixedClock(1))
        self.assertIn("immutable", str(ctx.exception))

    def test_session_mismatch_refused(self) -> None:
        ra.sync_state(self.conn, _state(members={"dev": _member("dev")}),
                     clock=_FixedClock())
        moved = _state(
            herdr_session={"server_id": "srv-2",
                          "socket_path": "/synthetic/other.sock"},
            members={"dev": _member("dev")},
        )
        with self.assertRaises(ra.TeamSessionMismatchError):
            ra.sync_state(self.conn, moved, clock=_FixedClock(1))

    def test_member_cannot_disappear_from_authority(self) -> None:
        state = _state(members={"dev": _member("dev"),
                               "qa": _member("qa")})
        ra.sync_state(self.conn, state, clock=_FixedClock())
        with self.assertRaises(ra.RuntimeAdapterError):
            ra.sync_state(self.conn,
                         _state(members={"dev": _member("dev")}),
                         clock=_FixedClock(1))
        view = ra.team_view(self.conn)
        self.assertIn("qa", view["members"])

    def test_failed_sync_transaction_persists_nothing(self) -> None:
        ra.sync_state(self.conn, _state(members={"dev": _member("dev")}),
                     clock=_FixedClock())
        before_events = self.conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()
        # A sync whose second member fails must roll the whole unit back.
        bad = _state(members={"dev": _member("dev"),
                              "qa": _member("qa", phase="bogus_phase")})
        with self.assertRaises(ValidationError):
            ra.sync_state(self.conn, bad, clock=_FixedClock(1))
        view = ra.team_view(self.conn)
        self.assertNotIn("qa", view["members"])
        after_events = self.conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()
        self.assertEqual(int(before_events[0]), int(after_events[0]))

    # ------------------------------------------------- stage commands (C/D)

    def _ready_member(self):
        state = _state(members={"dev": _member("dev")})
        ra.sync_state(self.conn, state, clock=_FixedClock())
        dev = state["members"]["dev"]
        dev.update({"phase": "split_requested"})
        ra.sync_state(self.conn, state, clock=_FixedClock(1))
        return state, dev

    def test_claim_is_durable_running_before_effect(self) -> None:
        _, dev = self._ready_member()
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=60, clock=_FixedClock())
        command = ra.stage_claim(
            self.conn, team_id="team-synthetic", member="dev",
            stage=ra.STAGE_SPLIT, lease=lease,
            task_id=dev["task_id"], attempt_id=dev["attempt_id"],
            clock=_FixedClock(2),
        )
        self.assertEqual(command.state, "running")
        # Durability check from a completely separate connection, BEFORE any
        # simulated effect: the claim is already committed.
        other = ra.connect_runtime(self.root)
        try:
            latest = ra.latest_stage_command(other, "dev", ra.STAGE_SPLIT)
            self.assertIsNotNone(latest)
            self.assertEqual(latest.id, command.id)
            self.assertEqual(latest.state, "running")
        finally:
            other.close()

    def test_stage_sequence_numbers_and_idempotent_submit(self) -> None:
        _, dev = self._ready_member()
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=60, clock=_FixedClock())
        first = ra.stage_claim(self.conn, team_id="team-synthetic",
                              member="dev", stage=ra.STAGE_SPLIT, lease=lease,
                              task_id=dev["task_id"],
                              attempt_id=dev["attempt_id"],
                              clock=_FixedClock(2))
        payload = json.dumps({"member": "dev"}, sort_keys=True)
        evidence = json.dumps(
            {"attempt_id": dev["attempt_id"], "attempt_no": 1,
             "task_id": dev["task_id"], "task_version": 2}, sort_keys=True)
        # Identical replay of the SAME key converges onto the same row.
        same = command_core.submit_command(
            self.conn, idempotency_key=first.idempotency_key,
            kind=ra.STAGE_SPLIT, payload_json=payload,
            evidence_json=evidence, clock=_FixedClock(3),
        )
        self.assertTrue(same.reused)
        self.assertEqual(same.command.id, first.id)
        # A new attempt gets a fresh sequence number under its own key.
        second = ra.stage_claim(self.conn, team_id="team-synthetic",
                               member="dev", stage=ra.STAGE_SPLIT, lease=lease,
                               task_id=dev["task_id"],
                               attempt_id=dev["attempt_id"],
                               clock=_FixedClock(4))
        self.assertNotEqual(second.id, first.id)
        self.assertTrue(second.idempotency_key.endswith(":2"))

    def test_stale_leader_can_never_commit_success(self) -> None:
        _, dev = self._ready_member()
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=_FixedClock())
        command = ra.stage_claim(self.conn, team_id="team-synthetic",
                                member="dev", stage=ra.STAGE_SPLIT, lease=lease,
                                task_id=dev["task_id"],
                                attempt_id=dev["attempt_id"],
                                clock=_FixedClock(1))
        # Another epoch takes over while our effect is in flight.
        leader.acquire_or_renew(self.conn, "member:dev", "thief",
                               ttl_seconds=30, clock=_FixedClock(31))
        with self.assertRaises(leader.StaleLeaderError):
            ra.stage_confirm(self.conn, command.id, lease=lease,
                           evidence={"pane_id": "w-2"},
                           result={"pane_id": "w-2"},
                           clock=_FixedClock(32))
        # Nothing committed as success; the row is still running and must be
        # settled by UNKNOWN + reconciliation, never a blind resend.
        latest = ra.latest_stage_command(self.conn, "dev", ra.STAGE_SPLIT)
        self.assertEqual(latest.state, "running")
        ra.stage_unknown(self.conn, command.id,
                        note="success write refused after lease loss",
                        clock=_FixedClock(33))
        self.assertEqual(
            ra.latest_stage_command(self.conn, "dev", ra.STAGE_SPLIT).state,
            "unknown",
        )
        ra.stage_reconcile(self.conn, command.id,
                          evidence={"pane_id": "w-2", "observed": "pane exists"},
                          resolved_ok=True,
                          note="resume inspection proved the split happened",
                          clock=_FixedClock(34))
        self.assertEqual(
            ra.latest_stage_command(self.conn, "dev", ra.STAGE_SPLIT).state,
            "confirmed",
        )

    def test_unknown_then_expired_reconcile(self) -> None:
        _, dev = self._ready_member()
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=_FixedClock())
        command = ra.stage_claim(self.conn, team_id="team-synthetic",
                                member="dev", stage=ra.STAGE_PROMPT, lease=lease,
                                task_id=dev["task_id"],
                                clock=_FixedClock(1))
        ra.stage_unknown(self.conn, command.id,
                        note="response lost in transport",
                        clock=_FixedClock(2))
        ra.stage_reconcile(self.conn, command.id,
                          evidence={"pane": "w-2", "prompt_visible": False},
                          resolved_ok=False,
                          note="facts prove the prompt never landed",
                          clock=_FixedClock(3))
        self.assertEqual(
            ra.latest_stage_command(self.conn, "dev", ra.STAGE_PROMPT).state,
            "expired",
        )

    def test_claim_requires_live_lease(self) -> None:
        _, dev = self._ready_member()
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=_FixedClock())
        leader.release(self.conn, "member:dev", "A", epoch=lease.epoch,
                      clock=_FixedClock(1))
        with self.assertRaises(leader.StaleLeaderError):
            ra.stage_claim(self.conn, team_id="team-synthetic", member="dev",
                         stage=ra.STAGE_SPLIT, lease=lease,
                         task_id=dev["task_id"], clock=_FixedClock(2))

    def test_launch_failure_rollback_after_claim_keeps_audit(self) -> None:
        # A crash AFTER the durable claim leaves the command running (never
        # silently deleted) so resume must reconcile from facts.
        _, dev = self._ready_member()
        lease = leader.acquire_or_renew(self.conn, "member:dev", "A",
                                       ttl_seconds=30, clock=_FixedClock())
        command = ra.stage_claim(self.conn, team_id="team-synthetic",
                                member="dev", stage=ra.STAGE_SPLIT, lease=lease,
                                task_id=dev["task_id"],
                                attempt_id=dev["attempt_id"],
                                clock=_FixedClock(1))
        with self.assertRaises(RuntimeError):
            with storage.transaction(self.conn):
                ra.sync_state(self.conn, _state(members={
                    "dev": _member("dev", phase="pane_created",
                                  pane_id="w-2",
                                  task_id=dev["task_id"],
                                  attempt_id=dev["attempt_id"])}),
                    clock=_FixedClock(2))
                raise RuntimeError("effect crashed before confirm")
        latest = ra.latest_stage_command(self.conn, "dev", ra.STAGE_SPLIT)
        self.assertEqual(latest.id, command.id)
        self.assertEqual(latest.state, "running")
        view = ra.team_view(self.conn)
        self.assertIsNone(view["members"]["dev"]["pane_id"])


if __name__ == "__main__":
    unittest.main()
