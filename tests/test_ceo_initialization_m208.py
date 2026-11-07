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

    def clear_prebootstrap_native_session(self):
        """Model Harnesses that allocate a native Session on first prompt."""
        member = self.state["members"]["ceo"]
        member["agent_session"] = None
        with storage.transaction(self.conn):
            self.conn.execute(
                "UPDATE members SET agent_session_json=NULL, live_state='unknown' WHERE name='ceo'"
            )
            self.conn.execute(
                "UPDATE sessions SET native_session_id=NULL, state='unknown' WHERE id=?",
                (member["session_id"],),
            )

    def confirmed_submitted_bootstrap(self):
        self.clear_prebootstrap_native_session()
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        member = runtime_adapter.team_view(self.conn)["members"]["ceo"]
        command = runtime_adapter.stage_claim(
            self.conn, team_id=self.state["team_id"], member="ceo",
            stage=runtime_adapter.STAGE_PROMPT, lease=self.lease,
            task_id=member["task_id"], attempt_id=member["attempt_id"],
            payload={"brief_content": "bootstrap prompt"}, clock=self.clock,
        )
        runtime_adapter.record_prompt_delivery(
            self.conn, command.id, "submitted", clock=self.clock,
        )
        runtime_adapter.stage_confirm(
            self.conn, command.id, lease=self.lease,
            evidence={"transport": "submitted"}, clock=self.clock,
        )
        return member, command

    def live_identity(self, **changes):
        identity = {
            "pid": 1234,
            "process_start": "2026-11-06T11:59:58+00:00",
            "process_name": "codex",
            "argv": ["codex", "--model", "gpt-test"],
            "cwd": str(self.root),
            "pane_id": "w1:p1",
            "terminal_id": "terminal-m208",
            "harness": "codex",
            "herdr_session_identity_hash": hashlib.sha256(
                json.dumps(
                    self.state["herdr_session"], sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        identity.update(changes)
        return identity

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
        self.assertEqual((result.from_version, result.to_version, result.applied),
                         (8, 14, (9, 10, 11, 12, 13, 14)))
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

    def test_live_process_identity_is_nonportable_and_keeps_native_fields_empty(self):
        member, _ = self.confirmed_submitted_bootstrap()
        identity = self.live_identity()
        record = runtime_adapter.record_ceo_bootstrap_live_process_assumed(
            self.conn, self.project_id, live_process_identity=identity,
            session_state="idle", actor="owner", reason="native hook unavailable",
            lease=self.lease, clock=self.clock,
        )
        self.assertEqual((record.state, record.identity_mode),
                         ("initialized", "live_process"))
        self.assertIsNone(record.native_session_id)
        self.assertEqual(json.loads(record.live_process_identity_json), identity)
        row = self.conn.execute(
            "SELECT agent_session_json FROM members WHERE name='ceo'"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(domain.get_session(self.conn, member["session_id"]).native_session_id)
        with self.assertRaisesRegex(init.CEOInitializationGateClosed, "non-portable"):
            init.require_initialized(self.conn, self.project_id)
        self.assertEqual(
            init.require_initialized(
                self.conn, self.project_id, allow_live_process=True,
            ).identity_mode,
            "live_process",
        )
        with self.assertRaises(init.CEOInitializationGateClosed):
            scheduler.dispatch_ready(
                self.conn, self.project_id, self.lease,
                concurrency_limit=1, clock=self.clock,
            )

    def test_live_process_identity_blocks_wake_admission_without_side_effects(self):
        self.confirmed_submitted_bootstrap()
        record = runtime_adapter.record_ceo_bootstrap_live_process_assumed(
            self.conn, self.project_id, live_process_identity=self.live_identity(),
            session_state="idle", actor="owner", reason="native hook unavailable",
            lease=self.lease, clock=self.clock,
        )
        self.assertEqual(record.identity_mode, "live_process")
        events.append_event(
            self.conn, "watch.task_progress", project_id=self.project_id,
            payload_json=json.dumps({"dedupe_key": "task:live-only"}), clock=self.clock,
        )
        ingested = wake.ingest_events(self.conn, self.project_id, self.lease, clock=self.clock)
        self.assertEqual(ingested.created_wakes, 1)
        wake_id = ingested.wakes[-1].id
        with self.assertRaisesRegex(init.CEOInitializationGateClosed, "non-portable"):
            wake.admit_pending_wake(self.conn, self.project_id, wake_id, self.lease, clock=self.clock)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM commands WHERE kind='ceo.wake'").fetchone()[0], 0,
        )
        self.assertEqual(
            str(self.conn.execute("SELECT state FROM wakes WHERE id=?", (wake_id,)).fetchone()[0]),
            "pending",
        )

    def test_live_process_reconciliation_rejects_retroactive_replacement(self):
        self.confirmed_submitted_bootstrap()
        before = self.conn.execute(
            "SELECT COUNT(*) FROM commands WHERE kind='ceo.identity.live_process'"
        ).fetchone()[0]
        with self.assertRaisesRegex(runtime_adapter.RuntimeAdapterError, "strictly before"):
            runtime_adapter.record_ceo_bootstrap_live_process_assumed(
                self.conn, self.project_id,
                live_process_identity=self.live_identity(
                    process_start="2026-11-06T12:00:00+00:00",
                ),
                session_state="idle", actor="owner", reason="test refusal",
                lease=self.lease, clock=self.clock,
            )
        after = self.conn.execute(
            "SELECT COUNT(*) FROM commands WHERE kind='ceo.identity.live_process'"
        ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "unknown")

    def test_native_session_initialization_remains_portable(self):
        self.clear_prebootstrap_native_session()
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        record = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce,
            native_session_id="native-after-prompt",
            agent_session={"kind": "id", "value": "native-after-prompt"},
            session_state="idle", lease=self.lease, clock=self.clock,
        )
        self.assertEqual(record.identity_mode, "native")
        self.assertIsNone(record.live_process_identity_json)
        self.assertEqual(init.require_initialized(self.conn, self.project_id), record)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "state transition"):
            with storage.transaction(self.conn):
                self.conn.execute("UPDATE ceo_initializations SET state='unknown' WHERE project_id=?",
                                  (self.project_id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "bindings are immutable"):
            with storage.transaction(self.conn):
                self.conn.execute("UPDATE ceo_initializations SET terminal_id='changed' WHERE project_id=?",
                                  (self.project_id,))
        other = domain.create_project(self.conn, "portable-probe", str(self.root / "other"),
                                     clock=self.clock)
        with self.assertRaisesRegex(sqlite3.IntegrityError, r"length\(nonce_hash\)"):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO ceo_initializations (project_id,canonical_ceo_name,state,team_id,"
                    "herdr_session_identity_json,nonce_hash,created_at,updated_at) VALUES (?, 'ceo',"
                    "'discovered','team','{}','short','now','now')", (other.id,))
        # Every rejected probe rolled back; the portable native record is intact
        # and remains accepted by the default (Scheduler/Wake) gate.
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "initialized")
        self.assertEqual(
            init.require_initialized(self.conn, self.project_id).identity_mode, "native",
        )

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

    def test_first_prompt_may_bind_native_session_after_discovery(self):
        self.clear_prebootstrap_native_session()
        record, nonce = self.issue()
        self.assertIsNone(record.native_session_id)
        self.assertIsNone(init.bootstrap_prompt_fields(record, nonce)["herdr_session_id"])
        pending = init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        self.assertEqual(pending.state, "unknown")
        initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce, native_session_id="native-after-prompt",
            session_state="idle", lease=self.lease, clock=self.clock,
        )
        self.assertEqual((initialized.state, initialized.native_session_id),
                         ("initialized", "native-after-prompt"))
        session = domain.get_session(self.conn, initialized.teamcraft_session_id)
        self.assertEqual((session.native_session_id, session.state),
                         ("native-after-prompt", "idle"))
        member_session = json.loads(self.conn.execute(
            "SELECT agent_session_json FROM members WHERE name='ceo'"
        ).fetchone()[0])
        self.assertEqual(member_session["value"], "native-after-prompt")
        self.assertEqual(
            init.require_initialized(self.conn, self.project_id).receipt_source,
            "assumed_default",
        )

    def test_confirmed_bootstrap_can_reconcile_a_late_native_session(self):
        member, prompt = self.confirmed_submitted_bootstrap()
        agent_session = {
            "agent": "codex", "kind": "id", "source": "herdr:codex",
            "value": "native-late",
        }
        reconciled = init.reconcile_bootstrap_submitted_native_session(
            self.conn, self.project_id,
            native_session_id="native-late", agent_session=agent_session,
            session_state="idle", pane_id="w1:p1",
            terminal_id="terminal-m208", harness="codex",
            herdr_session_identity_hash=hashlib.sha256(
                json.dumps(
                    self.state["herdr_session"], sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            actor="owner", reason="Herdr reported the delayed native Session",
            lease=self.lease, clock=self.clock,
        )

        self.assertEqual(
            (reconciled.state, reconciled.receipt_source,
             reconciled.identity_mode, reconciled.native_session_id),
            ("initialized", "assumed_default", "native", "native-late"),
        )
        session = domain.get_session(self.conn, member["session_id"])
        self.assertEqual((session.native_session_id, session.state),
                         ("native-late", "idle"))
        stored_agent_session = json.loads(self.conn.execute(
            "SELECT agent_session_json FROM members WHERE name='ceo'"
        ).fetchone()[0])
        self.assertEqual(stored_agent_session, agent_session)
        identity_command = self.conn.execute(
            "SELECT state,payload_json FROM commands "
            "WHERE kind='ceo.identity.native_session'"
        ).fetchone()
        self.assertEqual(str(identity_command[0]), "confirmed")
        self.assertEqual(json.loads(identity_command[1])["prompt_command_id"], prompt.id)
        event = self.conn.execute(
            "SELECT payload_json FROM events "
            "WHERE kind='ceo.initialization.native_session_assumed'"
        ).fetchone()
        self.assertEqual(json.loads(event[0])["native_session_id"], "native-late")
        self.assertEqual(
            init.require_initialized(self.conn, self.project_id).native_session_id,
            "native-late",
        )
        with self.assertRaisesRegex(
            init.CEOInitializationGateClosed, "unconsumed UNKNOWN",
        ):
            init.reconcile_bootstrap_submitted_native_session(
                self.conn, self.project_id,
                native_session_id="native-late", agent_session=agent_session,
                session_state="idle", pane_id="w1:p1",
                terminal_id="terminal-m208", harness="codex",
                herdr_session_identity_hash=hashlib.sha256(
                    json.dumps(
                        self.state["herdr_session"], sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                actor="owner", reason="replay",
                lease=self.lease, clock=self.clock,
            )
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM commands WHERE kind='ceo.identity.native_session'"
        ).fetchone()[0], 1)

    def test_late_native_session_reconciliation_rejects_identity_drift(self):
        self.confirmed_submitted_bootstrap()
        agent_session = {"kind": "id", "value": "native-late"}
        socket_hash = hashlib.sha256(
            json.dumps(
                self.state["herdr_session"], sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        common = {
            "native_session_id": "native-late",
            "agent_session": agent_session,
            "session_state": "idle",
            "pane_id": "w1:p1",
            "terminal_id": "terminal-m208",
            "harness": "codex",
            "herdr_session_identity_hash": socket_hash,
            "actor": "owner",
            "reason": "inspect delayed native Session",
            "lease": self.lease,
            "clock": self.clock,
        }
        for changed in (
            {"terminal_id": "different-terminal"},
            {"pane_id": "w1:p9"},
            {"herdr_session_identity_hash": "f" * 64},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                init.CEOInitializationGateClosed, "binding drifted",
            ):
                init.reconcile_bootstrap_submitted_native_session(
                    self.conn, self.project_id, **(common | changed),
                )
        pending = init.get_initialization(self.conn, self.project_id)
        self.assertEqual((pending.state, pending.native_session_id), ("unknown", None))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM commands WHERE kind='ceo.identity.native_session'"
        ).fetchone()[0], 0)

    def test_late_native_session_reconciliation_requires_confirmed_submission(self):
        self.clear_prebootstrap_native_session()
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        with self.assertRaisesRegex(
            init.CEOInitializationGateClosed, "confirmed submitted",
        ):
            init.reconcile_bootstrap_submitted_native_session(
                self.conn, self.project_id,
                native_session_id="native-late",
                agent_session={"kind": "id", "value": "native-late"},
                session_state="idle", pane_id="w1:p1",
                terminal_id="terminal-m208", harness="codex",
                herdr_session_identity_hash=hashlib.sha256(
                    json.dumps(
                        self.state["herdr_session"], sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                actor="owner", reason="inspect delayed native Session",
                lease=self.lease, clock=self.clock,
            )

    def test_submitted_prompt_reconciliation_confirms_transport_not_initialization(self):
        self.clear_prebootstrap_native_session()
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        member = runtime_adapter.team_view(self.conn)["members"]["ceo"]
        command = runtime_adapter.stage_claim(
            self.conn, team_id=self.state["team_id"], member="ceo",
            stage=runtime_adapter.STAGE_PROMPT, lease=self.lease,
            task_id=member["task_id"], attempt_id=member["attempt_id"],
            payload={"brief_content": "bootstrap prompt"}, clock=self.clock,
        )
        runtime_adapter.record_prompt_delivery(
            self.conn, command.id, "submitted", clock=self.clock,
        )
        runtime_adapter.stage_unknown(
            self.conn, command.id,
            note="legacy identity-binding ambiguity after confirmed transport",
            clock=self.clock,
        )

        reconciled = runtime_adapter.reconcile_submitted_prompt(
            self.conn, command.id, member="ceo", lease=self.lease,
            actor="owner", reason="durable delivery row and pane transcript prove submission",
            clock=self.clock,
        )

        self.assertEqual(reconciled.state, "confirmed")
        delivery = self.conn.execute(
            "SELECT state FROM prompt_deliveries WHERE command_id=?", (command.id,),
        ).fetchone()
        self.assertEqual(str(delivery[0]), "submitted")
        initialization = init.get_initialization(self.conn, self.project_id)
        self.assertEqual((initialization.state, initialization.native_session_id),
                         ("unknown", None))
        session = domain.get_session(self.conn, member["session_id"])
        self.assertIsNone(session.native_session_id)
        with self.assertRaises(init.CEOInitializationGateClosed):
            init.require_initialized(self.conn, self.project_id)
        # Repeating the exact reconciliation is read-only/idempotent.
        repeated = runtime_adapter.reconcile_submitted_prompt(
            self.conn, command.id, member="ceo", lease=self.lease,
            actor="owner", reason="durable delivery row and pane transcript prove submission",
            clock=self.clock,
        )
        self.assertEqual(repeated.state, "confirmed")

    def test_crash_before_prompt_confirmation_remains_unknown(self):
        self.clear_prebootstrap_native_session()
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        member = runtime_adapter.team_view(self.conn)["members"]["ceo"]
        command = runtime_adapter.stage_claim(
            self.conn, team_id=self.state["team_id"], member="ceo",
            stage=runtime_adapter.STAGE_PROMPT, lease=self.lease,
            task_id=member["task_id"], attempt_id=member["attempt_id"],
            payload={"brief_content": "bootstrap prompt"}, clock=self.clock,
        )
        runtime_adapter.record_prompt_delivery(
            self.conn, command.id, "submitted", clock=self.clock,
        )
        unresolved = runtime_adapter.stage_unknown(
            self.conn, command.id,
            note="executor crashed before fenced Command confirmation",
            clock=self.clock,
        )
        self.assertEqual(unresolved.state, "unknown")
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "unknown")
        with self.assertRaises(init.CEOInitializationGateClosed):
            init.require_initialized(self.conn, self.project_id)

    def test_post_prompt_missing_or_changed_native_session_keeps_gate_closed(self):
        self.clear_prebootstrap_native_session()
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(
            self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock,
        )
        with self.assertRaisesRegex(Exception, "native Session ID"):
            init.record_bootstrap_submitted_assumed(
                self.conn, self.project_id, nonce, native_session_id="",
                session_state="idle", lease=self.lease, clock=self.clock,
            )
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "unknown")
        with storage.transaction(self.conn):
            self.conn.execute(
                "UPDATE sessions SET native_session_id='different-session' WHERE id=?",
                (self.state["members"]["ceo"]["session_id"],),
            )
        with self.assertRaisesRegex(init.CEOInitializationError, "do not match"):
            init.record_bootstrap_submitted_assumed(
                self.conn, self.project_id, nonce, native_session_id="post-prompt-session",
                session_state="idle", lease=self.lease, clock=self.clock,
            )
        self.assertEqual(init.get_initialization(self.conn, self.project_id).state, "unknown")
        with self.assertRaises(init.CEOInitializationError):
            init.require_initialized(self.conn, self.project_id)

    def test_pre_effect_discovery_failure_is_audited_and_retryable(self):
        self.clear_prebootstrap_native_session()
        protocol_file = self.root / ".agents/skills/teamcraft-ceo/references/CEO_PROTOCOL.md"
        original = protocol_file.read_bytes()
        protocol_file.write_bytes(original + b"\npreflight drift\n")
        view = runtime_adapter.team_view(self.conn)
        member = view["members"]["ceo"]
        with self.assertRaises(runtime_adapter.RuntimeAdapterError):
            runtime_adapter.prepare_ceo_bootstrap(self.conn, view, member, clock=self.clock)
        self.assertIsNone(init.get_initialization(self.conn, self.project_id))
        event = self.conn.execute(
            "SELECT payload_json FROM events WHERE kind='ceo.initialization.incompatible' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertTrue(json.loads(event[0])["retryable"])
        protocol_file.write_bytes(original)
        fields = runtime_adapter.prepare_ceo_bootstrap(
            self.conn, runtime_adapter.team_view(self.conn), member, clock=self.clock,
        )
        self.assertTrue(fields["nonce"])
        self.assertIsNone(fields["herdr_session_id"])

    def test_discovered_challenge_can_refresh_only_before_prompt_command_claim(self):
        self.clear_prebootstrap_native_session()
        first, old_nonce = self.issue()
        second, new_nonce = self.issue()
        self.assertEqual(first.project_id, second.project_id)
        self.assertNotEqual(old_nonce, new_nonce)
        with self.assertRaises(init.CEOInitializationReplayError):
            init.mark_bootstrap_unknown(
                self.conn, self.project_id, old_nonce, lease=self.lease, clock=self.clock,
            )
        member = self.state["members"]["ceo"]
        runtime_adapter.stage_claim(
            self.conn, team_id=self.state["team_id"], member="ceo",
            stage=runtime_adapter.STAGE_PROMPT, lease=self.lease,
            task_id=member["task_id"], attempt_id=member["attempt_id"],
            payload={"brief_content": "bootstrap"}, clock=self.clock,
        )
        with self.assertRaisesRegex(init.CEOInitializationGateClosed, "reconcile"):
            self.issue()
        admitted = init.mark_bootstrap_unknown(
            self.conn, self.project_id, new_nonce, lease=self.lease, clock=self.clock,
        )
        self.assertEqual(admitted.state, "unknown")
        event = self.conn.execute(
            "SELECT payload_json FROM events "
            "WHERE kind='ceo.initialization.challenge_refreshed' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(json.loads(event[0])["effect_claimed"], False)

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
            self.conn, self.project_id, nonce, native_session_id="native-m208",
            session_state="idle", lease=self.lease, clock=self.clock,
        )
        self.assertEqual((assumed.state, assumed.receipt_source), ("initialized", "assumed_default"))
        self.assertIn("trusted host receipt assumed", assumed.detail)
        self.assertEqual(init.require_initialized(self.conn, self.project_id).receipt_source, "assumed_default")

    def test_wake_command_sending_submitted_ack_and_processing_are_durable(self):
        _, nonce = self.issue()
        init.mark_bootstrap_unknown(self.conn, self.project_id, nonce, lease=self.lease, clock=self.clock)
        initialized = init.record_bootstrap_submitted_assumed(
            self.conn, self.project_id, nonce, native_session_id="native-m208",
            session_state="working", lease=self.lease, clock=self.clock,
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
            self.conn, self.project_id, nonce, native_session_id="native-m208",
            session_state="working", lease=self.lease, clock=self.clock,
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
            self.conn, self.project_id, nonce, native_session_id="native-m208",
            session_state="working", lease=self.lease, clock=self.clock,
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
            self.conn, self.project_id, nonce, native_session_id="native-m208",
            session_state="working", lease=self.lease, clock=self.clock,
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
