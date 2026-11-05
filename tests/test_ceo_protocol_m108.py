from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest

from teamcraft.ceo_protocol import ProtocolError, discover_protocol, verify_protocol
from teamcraft import domain, leader, runtime_adapter, storage
from teamcraft.errors import ValidationError


ARTIFACTS = Path(__file__).resolve().parents[1] / ".agents/skills/teamcraft-ceo/references"


class CEOProtocolTests(unittest.TestCase):
    def _copy_skill(self, project: Path) -> Path:
        skill = project / ".agents/skills/teamcraft-ceo"
        shutil.copytree(ARTIFACTS.parent, skill)
        return skill

    def test_committed_protocol_matches_detached_manifest(self):
        snapshot = verify_protocol(ARTIFACTS)
        self.assertEqual(snapshot.version, "1.0.0")
        self.assertEqual(len(snapshot.sha256), 64)
        self.assertEqual(verify_protocol(ARTIFACTS, expected_version=snapshot.version,
                                         expected_sha256=snapshot.sha256), snapshot)

    def test_missing_or_changed_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("CEO_PROTOCOL.md", "protocol.manifest.json"):
                (root / name).write_bytes((ARTIFACTS / name).read_bytes())
            (root / "CEO_PROTOCOL.md").write_bytes((root / "CEO_PROTOCOL.md").read_bytes() + b"\nchange\n")
            with self.assertRaisesRegex(ProtocolError, "SHA-256"):
                verify_protocol(root)

    def test_manifest_version_and_attempt_pin_must_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("CEO_PROTOCOL.md", "protocol.manifest.json"):
                (root / name).write_bytes((ARTIFACTS / name).read_bytes())
            with self.assertRaisesRegex(ProtocolError, "pinned protocol version"):
                verify_protocol(root, expected_version="1.1.0")
            manifest = json.loads((root / "protocol.manifest.json").read_text())
            manifest["protocol_version"] = "2.0.0"
            (root / "protocol.manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ProtocolError, "differs from manifest"):
                verify_protocol(root)

    def test_missing_protocol_is_not_reconstructed(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProtocolError, "missing"):
                verify_protocol(temp)

    def test_codex_and_opencode_mock_discovery_share_exact_artifact(self):
        project = Path(__file__).resolve().parents[1]
        codex = discover_protocol(project, "codex")
        opencode = discover_protocol(project, "opencode")
        self.assertEqual((codex.version, codex.sha256), (opencode.version, opencode.sha256))

    def test_missing_or_unreadable_discovery_fails_without_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProtocolError, "missing or unreadable"):
                discover_protocol(temp, "codex")
            skill = Path(temp) / ".agents/skills/teamcraft-ceo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("not a skill\n")
            with self.assertRaisesRegex(ProtocolError, "discovery contract invalid"):
                discover_protocol(temp, "opencode")
        with self.assertRaisesRegex(ProtocolError, "unsupported"):
            discover_protocol(".", "claude")

    def test_attempt_persists_and_cannot_change_protocol_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "db.sqlite"
            storage.migrate_to_head(db)
            conn = storage.connect(db)
            self.addCleanup(conn.close)
            project = domain.create_project(conn, "p", str(Path(temp) / "repo"))
            task = domain.create_task(conn, project.id, "CEO work")
            domain.recompute_ready(conn)
            task = domain.get_task(conn, task.id)
            snapshot = verify_protocol(ARTIFACTS)
            attempt = domain.create_attempt(
                conn, task.id, expected_version=task.version,
                protocol_version=snapshot.version, protocol_hash=snapshot.sha256,
            )
            self.assertEqual(attempt.protocol_version, snapshot.version)
            self.assertEqual(attempt.protocol_hash, snapshot.sha256)
            # An upgraded protocol copy must not retarget an already-created
            # Attempt; its exact old bytes are still required to resume it.
            upgraded = snapshot.content.replace(b"protocol_version: 1.0.0",
                                                b"protocol_version: 1.1.0", 1)
            refs = Path(temp) / "upgraded-refs"
            refs.mkdir()
            (refs / "CEO_PROTOCOL.md").write_bytes(upgraded)
            (refs / "protocol.manifest.json").write_text(json.dumps({
                "protocol_version": "1.1.0",
                "sha256": hashlib.sha256(upgraded).hexdigest(),
            }))
            with self.assertRaisesRegex(ProtocolError, "pinned protocol version"):
                verify_protocol(refs, expected_version=attempt.protocol_version,
                                expected_sha256=attempt.protocol_hash)
            self.assertEqual(domain.get_attempt(conn, attempt.id), attempt)
            with self.assertRaises(sqlite3.IntegrityError):
                with storage.transaction(conn):
                    conn.execute("UPDATE attempts SET protocol_version = ? WHERE id = ?",
                                 ("1.1.0", attempt.id))
            self.assertEqual(domain.occupying_attempt(conn, task.id), attempt)

    def test_v4_database_upgrades_additively_and_keeps_old_attempts_unpinned(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "v4.sqlite"
            self.assertTrue(storage.migrate_to_head(db).fresh)
            raw = sqlite3.connect(db)
            # Downgrade only this synthetic fixture to the exact v4 shape.
            for trigger in storage._V5_TRIGGERS:
                raw.execute(f"DROP TRIGGER {trigger}")
            raw.execute("ALTER TABLE attempts DROP COLUMN protocol_hash")
            raw.execute("ALTER TABLE attempts DROP COLUMN protocol_version")
            for trigger in storage._V6_TRIGGERS:
                raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in storage._V6_INDEXES:
                raw.execute(f"DROP INDEX IF EXISTS {index}")
            for table in storage._V6_TABLES:
                raw.execute(f"DROP TABLE IF EXISTS {table}")
            task_columns = {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}
            if "max_attempts" in task_columns:
                raw.execute("ALTER TABLE tasks DROP COLUMN max_attempts")
            raw.execute("DELETE FROM schema_migrations WHERE version >= 5")
            raw.execute("PRAGMA user_version = 4")
            raw.execute("INSERT INTO projects (id,name,repo_path,config_json,created_at) VALUES ('p','p','/p','{}','now')")
            raw.execute("INSERT INTO tasks (id,project_id,title,status,created_at,updated_at) VALUES ('t','p','t','running','now','now')")
            raw.execute("INSERT INTO attempts (id,task_id,attempt_no,status) VALUES ('a','t',1,'active')")
            raw.commit()
            raw.close()
            report = storage.migrate_to_head(db)
            self.assertEqual((report.from_version, report.to_version), (4, storage.SCHEMA_VERSION))
            conn = storage.connect(db)
            self.addCleanup(conn.close)
            attempt = domain.get_attempt(conn, "a")
            self.assertEqual(attempt.status, "active")
            self.assertIsNone(attempt.protocol_version)
            self.assertIsNone(attempt.protocol_hash)
            with self.assertRaises(sqlite3.IntegrityError):
                with storage.transaction(conn):
                    conn.execute("UPDATE attempts SET protocol_version=?, protocol_hash=? WHERE id=?",
                                 ("1.0.0", "0" * 64, "a"))

    def test_runtime_pins_ceo_attempt_and_blocks_changed_protocol_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            self._copy_skill(project)
            conn, _ = runtime_adapter.open_runtime(project)
            self.addCleanup(conn.close)
            member = {
                "name": "ceo", "role": "ceo", "responsibility": "coordinate",
                "kind": "codex", "task": "deliver", "ownership": [],
                "read_only": True, "cwd": str(project), "from_pane_id": "p0",
                "pane_id": None, "terminal_id": None, "agent_session": None,
                "agent_args": [], "opencode_config_content": None, "direction": None,
                "start_timeout_ms": 30000, "phase": "reserved",
                "prompt_may_have_been_delivered": False, "live_state": None,
                "error": None, "task_id": None, "attempt_id": None,
                "session_id": None, "created_at": 1, "updated_at": 1,
            }
            state = {
                "version": runtime_adapter.STATE_VERSION, "team_id": "team-ceo",
                "project_root": str(project),
                "herdr_session": {"server_id": "srv", "socket_path": "/sock"},
                "created_at": 1, "members": {"ceo": member},
            }
            runtime_adapter.sync_state(conn, state)
            member["phase"] = "split_requested"
            runtime_adapter.sync_state(conn, state)
            attempt = domain.get_attempt(conn, member["attempt_id"])
            snapshot = verify_protocol(ARTIFACTS)
            self.assertEqual((attempt.protocol_version, attempt.protocol_hash),
                             (snapshot.version, snapshot.sha256))

            changed = snapshot.content.replace(b"protocol_version: 1.0.0",
                                               b"protocol_version: 1.1.0", 1)
            refs = project / ".agents/skills/teamcraft-ceo/references"
            (refs / "CEO_PROTOCOL.md").write_bytes(changed)
            (refs / "protocol.manifest.json").write_text(json.dumps({
                "protocol_version": "1.1.0",
                "sha256": hashlib.sha256(changed).hexdigest(),
            }))
            with self.assertRaisesRegex(runtime_adapter.RuntimeAdapterError,
                                        "NEEDS_HUMAN.*pinned CEO protocol"):
                runtime_adapter.sync_state(conn, state)

            lease = leader.acquire_or_renew(conn, "member:ceo", "test", ttl_seconds=30)
            with self.assertRaisesRegex(runtime_adapter.RuntimeAdapterError,
                                        "NEEDS_HUMAN.*pinned CEO protocol"):
                runtime_adapter.stage_claim(
                    conn, team_id="team-ceo", member="ceo",
                    stage=runtime_adapter.STAGE_PROMPT, lease=lease,
                    task_id=member["task_id"], attempt_id=member["attempt_id"],
                    payload={"argv_head": ["agent", "prompt", "ceo"]},
                )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
