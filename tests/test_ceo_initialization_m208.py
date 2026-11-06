"""M2-08 project-scoped CEO bootstrap and schema acceptance tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from teamcraft import ceo_initialization as init, domain, events, leader, runtime_adapter, scheduler, storage, wake


class Clock:
    def __init__(self): self.value = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    def now(self): return self.value


class CEOInitializationM208Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1] / ".agents" / "skills"
        shutil.copytree(source / "teamcraft-ceo", self.root / ".agents/skills/teamcraft-ceo")
        skill = self.root / ".agents/skills/teamcraft"
        skill.mkdir(parents=True)
        shutil.copy2(source / "teamcraft/SKILL.md", skill / "SKILL.md")
        self.conn, _ = runtime_adapter.open_runtime(self.root)
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        self.state = {
            "version": runtime_adapter.STATE_VERSION,
            "team_id": "team-m208",
            "project_root": str(self.root),
            "herdr_session": {"server_id": "srv-m208", "socket_path": "/tmp/herdr-m208.sock"},
            "created_at": 1,
            "members": {
                "ceo": {
                    "name": "ceo", "role": "ceo", "responsibility": "lead",
                    "kind": "codex", "task": "initialize project", "ownership": [],
                    "read_only": True, "cwd": str(self.root), "from_pane_id": "w1:p1",
                    "pane_id": "w1:p1", "terminal_id": "terminal-m208",
                    "agent_session": {"kind": "native", "value": "native-m208"},
                    "agent_args": [], "opencode_config_content": None, "direction": None,
                    "start_timeout_ms": 30_000, "phase": "prompt_submitted",
                    "prompt_may_have_been_delivered": True, "live_state": "working",
                    "error": None, "task_id": None, "attempt_id": None, "session_id": None,
                    "created_at": 1, "updated_at": 1,
                }
            },
        }
        runtime_adapter.sync_state(self.conn, self.state, clock=self.clock)
        self.project_id = runtime_adapter.project_id_for_root(self.conn, str(self.root))
        self.lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id), "m208-test", ttl_seconds=60, clock=self.clock,
        )

    def issue(self, ttl=300):
        return init.discover_and_issue(
            self.conn, self.project_id, "ceo", nonce_ttl_seconds=ttl, clock=self.clock,
        )

    def claim_for(self, record, nonce):
        return init.bootstrap_prompt_fields(record, nonce) | {"nonce": nonce}

    def test_schema_v8_migrates_to_head_with_constraints_and_state_triggers(self):
        db = self.root / runtime_adapter.RUNTIME_DIRNAME / runtime_adapter.DATABASE_FILENAME
        raw = sqlite3.connect(db)
        try:
            for trigger in storage._V9_TRIGGERS: raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V9_INDEXES: raw.execute(f"DROP INDEX IF EXISTS {index}")
            for trigger in storage._V10_TRIGGERS: raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V10_INDEXES: raw.execute(f"DROP INDEX IF EXISTS {index}")
            raw.execute("DROP TABLE IF EXISTS ceo_initializations")
            raw.execute("DROP TABLE IF EXISTS wake_events")
            raw.execute("DROP TABLE IF EXISTS wakes")
            raw.execute("DROP TABLE IF EXISTS wake_cursors")
            raw.execute("DELETE FROM schema_migrations WHERE version > 8")
            raw.execute("PRAGMA user_version=8")
            raw.commit()
        finally:
            raw.close()
        result = storage.migrate_to_head(db, allow_unverified_location=True)
        self.assertEqual((result.from_version, result.to_version, result.applied), (8, 12, (9, 10, 11, 12)))
        audit = storage.verify_database(db)
        self.assertTrue(audit["at_head"])
        self.assertIn("ceo_initializations", audit["tables"])
        self.assertTrue({"wake_cursors", "wakes", "wake_events"} <= set(audit["tables"]))
        names = {row[0] for row in sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE type IN ('trigger','index')")}
        self.assertTrue(storage._V9_TRIGGERS <= names)
        self.assertTrue(storage._V9_INDEXES <= names)

        record, _ = self.issue()
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute("UPDATE ceo_initializations SET state='initialized' WHERE project_id=?",
                                  (self.project_id,))
        other = domain.create_project(self.conn, "initialized-probe", str(self.root / "other"), clock=self.clock)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "accepted receipt source"):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO ceo_initializations (project_id,canonical_ceo_name,state,team_id,"
                    "herdr_session_identity_json,created_at,updated_at) VALUES (?, 'ceo','initialized',"
                    "'team','{}','now','now')", (other.id,))
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE ceo_initializations SET state='failed' WHERE project_id=?",
                              (self.project_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "state transition"):
            with storage.transaction(self.conn):
                self.conn.execute("UPDATE ceo_initializations SET state='unknown' WHERE project_id=?",
                                  (self.project_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "bindings are immutable"):
            with storage.transaction(self.conn):
                self.conn.execute("UPDATE ceo_initializations SET terminal_id='changed' WHERE project_id=?",
                                  (self.project_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, r"length\(nonce_hash\)"):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO ceo_initializations (project_id,canonical_ceo_name,state,team_id,"
                    "herdr_session_identity_json,nonce_hash,created_at,updated_at) VALUES (?, 'ceo',"
                    "'discovered','team','{}','short','now','now')", (other.id,))
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "failed")

    def test_single_ceo_binding_and_discovery_pins_all_runtime_identities(self):
        attempt_id = self.state["members"]["ceo"]["attempt_id"]
        attempt = domain.get_attempt(self.conn, attempt_id)
        self.assertTrue(attempt.protocol_hash)
        record, nonce = self.issue()
        self.assertEqual(record.state, "discovered")
        self.assertEqual(record.teamcraft_session_id, self.state["members"]["ceo"]["session_id"])
        self.assertEqual(record.terminal_id, "terminal-m208")
        self.assertEqual(record.native_session_id, "native-m208")
        self.assertEqual(record.protocol_hash, attempt.protocol_hash)
        self.assertEqual(record.runtime_skill_hash,
                         hashlib.sha256((self.root / ".agents/skills/teamcraft/SKILL.md").read_bytes()).hexdigest())
        self.assertEqual(len(nonce), 43)  # 32 random bytes, URL-safe encoding.
        row = self.conn.execute("SELECT nonce_hash, nonce_expires_at FROM ceo_initializations WHERE project_id=?",
                                (self.project_id,)).fetchone()
        self.assertEqual(row[0], hashlib.sha256(nonce.encode("ascii")).hexdigest())
        self.assertNotIn(nonce, str(row[0]))
        self.assertEqual(datetime.fromisoformat(row[1]) - self.clock.now(), timedelta(seconds=300))
        with self.assertRaises(runtime_adapter.RuntimeAdapterError):
            runtime_adapter.sync_state(self.conn, self.state | {"members": {
                **self.state["members"],
                "ceo2": self.state["members"]["ceo"] | {"name": "ceo2"},
            }}, clock=self.clock)

    def test_nonce_is_one_use_expiry_replay_and_binding_drift_fail_closed(self):
        record, nonce = self.issue(ttl=10)
        result = init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        self.assertEqual(result.state, "unknown")
        with self.assertRaises(init.CEOInitializationReplayError):
            init.mark_bootstrap_unknown(self.conn, self.project_id, nonce,
                                        lease=self.lease, clock=self.clock)
        claim = self.claim_for(record, nonce)
        accepted = init.record_agent_claim(self.conn, self.project_id, json.dumps(claim),
                                           lease=self.lease, clock=self.clock)
        self.assertEqual(accepted.state, "read_unverified")
        self.assertEqual(accepted.receipt_source, "unverified")
        self.assertEqual(accepted.nonce_consumed_at, self.clock.now().isoformat())
        replay = init.record_agent_claim(self.conn, self.project_id, json.dumps(claim),
                                          lease=self.lease, clock=self.clock)
        self.assertEqual(replay.state, "read_unverified")
        with self.assertRaises(init.CEOInitializationGateClosed):
            init.require_initialized(self.conn, self.project_id)


    def test_expired_nonce_is_consumed_and_cannot_be_replayed(self):
        _, expired_nonce = self.issue(ttl=10)
        self.clock.value += timedelta(seconds=11)
        expired = init.mark_bootstrap_unknown(self.conn, self.project_id, expired_nonce,
                                               lease=self.lease, clock=self.clock)
        self.assertEqual(expired.state, "failed")
        self.assertIsNotNone(expired.nonce_consumed_at)
        with self.assertRaises(init.CEOInitializationReplayError):
            init.mark_bootstrap_unknown(self.conn, self.project_id, expired_nonce,
                                        lease=self.lease, clock=self.clock)

    def test_valid_agent_claim_stays_unverified_until_transport_assumption_is_recorded(self):
        record, nonce = self.issue()
        claim = self.claim_for(record, nonce)
        result = init.record_agent_claim(self.conn, self.project_id, json.dumps(claim),
                                         lease=self.lease, clock=self.clock)
        self.assertEqual(result.state, "read_unverified")
        self.assertEqual(result.receipt_source, "unverified")
        self.assertEqual(json.loads(result.audit_material_json), claim)
        with self.assertRaises(init.CEOInitializationGateClosed):
            init.require_initialized(self.conn, self.project_id)

    def test_successful_bootstrap_transport_uses_explicit_default_assumption(self):
        _, nonce = self.issue()
        pending = init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        self.assertEqual(pending.state, "unknown")
        assumed = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        self.assertEqual((assumed.state, assumed.receipt_source), ("initialized", "assumed_default"))
        self.assertIn("trusted host receipt assumed", assumed.detail)
        self.assertEqual(init.require_initialized(self.conn, self.project_id).receipt_source, "assumed_default")

    def test_wake_command_sending_submitted_ack_and_processing_are_durable(self):
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock)
        initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        domain.observe_session(self.conn, initialized.teamcraft_session_id, "idle", clock=self.clock)
        source = events.append_event(self.conn, "task.updated", project_id=self.project_id,
                                     payload_json='{"dedupe_key":"task:1"}', clock=self.clock)
        intake = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock)
        item = intake.wakes[0]
        admitted = wake.admit_pending_wake(self.conn, self.project_id, item.id, self.lease, clock=self.clock)
        self.assertEqual((admitted.wake.state, admitted.command.state), ("sending", "running"))
        self.assertEqual(admitted.wake.target_session_id, initialized.native_session_id)
        submitted = wake.record_submission_result(
            self.conn, self.project_id, item.id, self.lease,
            outcome="submitted", receipt_json='{"accepted":true}', clock=self.clock,
        )
        self.assertEqual(submitted.state, "submitted")
        self.assertEqual(wake.get_cursor(self.conn, self.project_id).acknowledged_seq, 0)
        acknowledged = wake.acknowledge_wake(
            self.conn, self.project_id, item.id, self.lease,
            target_session_id=initialized.native_session_id,
            acknowledged_through_seq=source.seq, clock=self.clock,
        )
        self.assertEqual(acknowledged.state, "acknowledged")
        processed = wake.mark_processed(self.conn, self.project_id, item.id, self.lease, clock=self.clock)
        self.assertEqual(processed.state, "processed")
        cursor = wake.get_cursor(self.conn, self.project_id)
        self.assertEqual((cursor.acknowledged_seq, cursor.processed_seq), (source.seq, source.seq))

    def test_unknown_wake_submission_is_not_automatically_retried(self):
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock)
        initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        domain.observe_session(self.conn, initialized.teamcraft_session_id, "idle", clock=self.clock)
        events.append_event(self.conn, "task.updated", project_id=self.project_id,
                            payload_json='{"dedupe_key":"task:1"}', clock=self.clock)
        item = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock).wakes[0]
        admitted = wake.admit_pending_wake(self.conn, self.project_id, item.id, self.lease, clock=self.clock)
        unknown = wake.record_submission_result(
            self.conn, self.project_id, item.id, self.lease,
            outcome="unknown", detail="Herdr response lost", clock=self.clock,
        )
        self.assertEqual(unknown.state, "unknown")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 1)
        with self.assertRaisesRegex(Exception, "only pending Wakes"):
            wake.admit_pending_wake(self.conn, self.project_id, item.id, self.lease, clock=self.clock)
        self.assertEqual(self.conn.execute("SELECT state FROM commands WHERE id=?", (admitted.command.id,)).fetchone()[0], "unknown")

    def test_unknown_wake_reconciliation_submitted_never_resends(self):
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock)
        initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        domain.observe_session(self.conn, initialized.teamcraft_session_id, "idle", clock=self.clock)
        source = events.append_event(self.conn, "task.updated", project_id=self.project_id,
                                     payload_json='{"dedupe_key":"task:reconcile"}', clock=self.clock)
        item = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock).wakes[0]
        admitted = wake.admit_pending_wake(self.conn, self.project_id, item.id, self.lease, clock=self.clock)
        wake.record_submission_result(self.conn, self.project_id, item.id, self.lease,
                                      outcome="unknown", detail="Lost Herdr response", clock=self.clock)
        settled = wake.reconcile_wake_delivery(
            self.conn, self.project_id, item.id, self.lease, delivered=True,
            actor="operator", note="Herdr history confirms submission",
            evidence_json='{"observed_prompt":true}', clock=self.clock,
        )
        self.assertEqual(settled.state, "submitted")
        self.assertEqual(self.conn.execute("SELECT state FROM commands WHERE id=?", (admitted.command.id,)).fetchone()[0], "confirmed")
        with self.assertRaisesRegex(Exception, "only pending Wakes"):
            wake.admit_pending_wake(self.conn, self.project_id, item.id, self.lease, clock=self.clock)
        ack = wake.acknowledge_wake(
            self.conn, self.project_id, item.id, self.lease,
            target_session_id=initialized.native_session_id,
            acknowledged_through_seq=source.seq, clock=self.clock,
        )
        self.assertEqual(ack.state, "acknowledged")

    def test_unknown_wake_reconciled_not_delivered_is_blocked_not_requeued(self):
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock)
        initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        domain.observe_session(self.conn, initialized.teamcraft_session_id, "idle", clock=self.clock)
        events.append_event(self.conn, "task.updated", project_id=self.project_id,
                            payload_json='{"dedupe_key":"task:not-delivered"}', clock=self.clock)
        item = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock).wakes[0]
        admitted = wake.admit_pending_wake(self.conn, self.project_id, item.id, self.lease, clock=self.clock)
        wake.record_submission_result(self.conn, self.project_id, item.id, self.lease,
                                      outcome="unknown", detail="Transport interrupted", clock=self.clock)
        settled = wake.reconcile_wake_delivery(
            self.conn, self.project_id, item.id, self.lease, delivered=False,
            actor="operator", note="Inspected session history; no prompt found",
            evidence_json='{"matching_prompt_found":false}', clock=self.clock,
        )
        self.assertEqual(settled.state, "blocked")
        self.assertEqual(self.conn.execute("SELECT state FROM commands WHERE id=?", (admitted.command.id,)).fetchone()[0], "expired")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 1)


    def test_runtime_skill_drift_before_admitted_bootstrap_fails_closed(self):
        record, nonce = self.issue()
        skill = self.root / ".agents/skills/teamcraft/SKILL.md"
        skill.write_bytes(skill.read_bytes() + b"\nmodified after challenge\n")
        drifted = init.mark_bootstrap_unknown(self.conn, self.project_id, nonce,
                                               lease=self.lease, clock=self.clock)
        self.assertEqual(drifted.state, "failed")
        self.assertIn("drifted", drifted.detail)

    def test_runtime_skill_symlink_before_admitted_bootstrap_fails_closed(self):
        _, nonce = self.issue()
        skill = self.root / ".agents/skills/teamcraft/SKILL.md"
        original = skill.read_bytes()
        target = self.root / "skill-target.md"
        target.write_bytes(original)
        skill.unlink()
        skill.symlink_to(target)
        result = init.mark_bootstrap_unknown(self.conn, self.project_id, nonce,
                                             lease=self.lease, clock=self.clock)
        self.assertEqual(result.state, "failed")
        self.assertIsNotNone(result.nonce_consumed_at)

    def test_task_snapshot_drift_before_admitted_bootstrap_fails_closed(self):
        _, nonce = self.issue()
        task = domain.create_task(self.conn, self.project_id, "new task after challenge", clock=self.clock)
        self.assertIsNotNone(task)
        result = init.mark_bootstrap_unknown(self.conn, self.project_id, nonce,
                                             lease=self.lease, clock=self.clock)
        self.assertEqual(result.state, "failed")
        self.assertIn("drifted", result.detail)

    def test_protocol_and_terminal_session_drift_fail_closed(self):
        _, protocol_nonce = self.issue()
        protocol_file = self.root / ".agents/skills/teamcraft-ceo/references/CEO_PROTOCOL.md"
        protocol_file.write_bytes(protocol_file.read_bytes() + b"\nchanged\n")
        protocol_result = init.mark_bootstrap_unknown(
            self.conn, self.project_id, protocol_nonce, lease=self.lease, clock=self.clock,
        )
        self.assertEqual(protocol_result.state, "failed")

    def test_terminal_binding_drift_before_admitted_bootstrap_fails_closed(self):
        _, nonce = self.issue()
        with storage.transaction(self.conn):
            self.conn.execute("UPDATE members SET terminal_id='terminal-moved' WHERE name='ceo'")
        result = init.mark_bootstrap_unknown(self.conn, self.project_id, nonce,
                                             lease=self.lease, clock=self.clock)
        self.assertEqual(result.state, "failed")
        self.assertIn("drifted", result.detail)

    def test_bootstrap_is_single_initial_path_and_unknown_is_not_resent_without_receipt(self):
        view = runtime_adapter.team_view(self.conn)
        member = view["members"]["ceo"]
        fields = runtime_adapter.prepare_ceo_bootstrap(self.conn, view, member, clock=self.clock)
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "discovered")
        runtime_adapter.admit_ceo_bootstrap_effect(
            self.conn, self.project_id, fields["nonce"], lease=self.lease, clock=self.clock,
        )
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "unknown")
        with self.assertRaisesRegex(runtime_adapter.RuntimeAdapterError, "resend bootstrap"):
            runtime_adapter.prepare_ceo_bootstrap(self.conn, view, member, clock=self.clock)

    def test_scheduler_requires_ceo_initialization_before_creating_attempt(self):
        task = domain.create_task(self.conn, self.project_id, "blocked task", ownership_json='[]', clock=self.clock)
        task = domain.apply_task_event(self.conn, task.id, "dependencies_satisfied",
                                       expected_version=task.version, clock=self.clock)
        lease = self.lease
        before = self.conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0]
        with self.assertRaises(init.CEOInitializationGateClosed):
            scheduler.dispatch_ready(self.conn, self.project_id, lease, concurrency_limit=1, clock=self.clock)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task.id,)).fetchone()[0], before)


if __name__ == "__main__": unittest.main()
