"""M4-04 storage contract: zero schema step + immutable delivery facts.

M4-04 deliberately adds NO migration (SCHEMA_VERSION stays 17): the
Commit/Diff evidence, ship Verification evidence and delivery Approval ride
the append-only ``events`` stream (M4-02 precedent).  These tests pin THAT
contract:

* the schema version and table set are unchanged — a later phase may harden
  the same kinds with dedicated tables/triggers (v18) but M4-04 shipped none;
* deterministic event ids converge under replay and fail closed on divergence;
* the v3 no-update/no-delete triggers make the three delivery kinds immutable;
* the strict loaders fail closed on ANY tampered payload shape/digest.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from teamcraft import delivery, domain, events, storage
from teamcraft.errors import StorageError


class M404StorageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m404-storage-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "demo", "/nonexistent/demo")

    def _binding(self, tag: str) -> tuple[str, str]:
        """Real task+attempt rows so forged events satisfy FKs."""
        task = domain.create_task(self.conn, self.project.id, f"forge {tag}")
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version)
        attempt = domain.create_attempt(
            self.conn, task.id, expected_version=task.version)
        return task.id, attempt.id

    # -- zero-schema-step contract -------------------------------------------

    def test_schema_version_stays_17_and_no_delivery_tables_exist(self):
        self.assertEqual(storage.SCHEMA_VERSION, 17)
        verified = storage.verify_database(self.db)
        self.assertTrue(verified["at_head"])
        self.assertEqual(verified["user_version"], 17)
        tables = set(verified["tables"])
        # M4-04 introduced no delivery/commit/approval evidence tables; the
        # facts live in ``events``.  (If a future v18 adds them, THIS test is
        # the place the ownership conversation happens.)
        self.assertFalse([t for t in tables
                         if t.startswith("delivery") or t.startswith("commit")])
        self.assertEqual(self.conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()[0], 17)
        # The Attempt resource column M4-04 mirrors the delivered SHA onto
        # already exists at v17 — no bump was required for the mirror.
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(attempts)").fetchall()}
        self.assertIn("commit_sha", columns)

    def test_migrate_from_head_is_a_no_op(self):
        report = storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.assertEqual((report.from_version, report.to_version), (17, 17))
        self.assertEqual(report.applied, ())

    # -- deterministic event ids ---------------------------------------------

    def test_deterministic_event_ids_converge_and_divergence_fails_closed(self):
        task_id, attempt_id = self._binding("det")
        payload = json.dumps({"n": 1}, sort_keys=True)
        event_id = delivery._commit_event_id(attempt_id)
        self.assertTrue(event_id.startswith("evt-dce-"))
        self.assertEqual(len(event_id), len("evt-dce-") + 40)
        first, inserted = events.append_event_idempotent(
            self.conn, delivery.COMMIT_EVIDENCE_KIND,
            project_id=self.project.id, task_id=task_id,
            attempt_id=attempt_id, payload_json=payload, event_id=event_id)
        self.assertTrue(inserted)
        second, reinserted = events.append_event_idempotent(
            self.conn, delivery.COMMIT_EVIDENCE_KIND,
            project_id=self.project.id, task_id=task_id,
            attempt_id=attempt_id, payload_json=payload, event_id=event_id)
        self.assertFalse(reinserted)
        self.assertEqual(second.event_id, first.event_id)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_id=?", (event_id,)).fetchone()[0],
            1)
        # Same key, different content: the immutable-stream contract refuses.
        with self.assertRaises(StorageError):
            events.append_event_idempotent(
                self.conn, delivery.COMMIT_EVIDENCE_KIND,
                project_id=self.project.id, task_id=task_id,
                attempt_id=attempt_id, payload_json=json.dumps({"n": 2}),
                event_id=event_id)

    def test_one_binding_per_attempt_is_pinned_by_the_reader_invariant(self):
        # The UNIQUE event_id caps one record per binding because the ids are
        # DETERMINISTIC per attempt/evidence; two non-deterministic rows for
        # the same (kind, attempt) are storage corruption, and the reader
        # must fail closed rather than pick a winner.
        task_id, attempt_id = self._binding("dup")
        for suffix in ("evt-dce-dup-a", "evt-dce-dup-b"):
            events.append_event(
                self.conn, delivery.COMMIT_EVIDENCE_KIND,
                project_id=self.project.id, task_id=task_id,
                attempt_id=attempt_id, payload_json="{}", event_id=suffix)
        with self.assertRaises(StorageError):
            delivery.load_commit_evidence(self.conn, attempt_id)

    # -- append-only immutability ---------------------------------------------

    def test_delivery_evidence_rows_are_immutable(self):
        task_id, attempt_id = self._binding("immut")
        events.append_event(
            self.conn, delivery.SHIP_EVIDENCE_KIND,
            project_id=self.project.id, task_id=task_id,
            attempt_id=attempt_id, payload_json=json.dumps({"stub": True}),
            event_id="evt-dse-immutable")
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "UPDATE events SET payload_json='{}'"
                    " WHERE event_id='evt-dse-immutable'")
        with self.assertRaises(sqlite3.IntegrityError):
            with storage.transaction(self.conn):
                self.conn.execute(
                    "DELETE FROM events WHERE event_id='evt-dse-immutable'")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM events WHERE event_id='evt-dse-immutable'").fetchone())

    # -- strict loaders fail closed -------------------------------------------

    def _commit_payload(self, task_id: str, attempt_id: str, **overrides) -> str:
        fields = {
            "allocation_id": "alloc-1", "attempt_id": attempt_id,
            "attempt_no": 1, "base_sha": "a" * 40,
            "branch": f"tc/{task_id}/{attempt_id}",
            "brief_digest": "b" * 64, "brief_version": 1,
            "claims": ["src/**"],
            "claims_digest": delivery._sha256_hex(
                delivery._canonical_json(["src/**"])),
            "commit_sha": "c" * 40, "command_id": "cmd-1",
            "dispatch_task_version": 2, "file_count": 1,
            "files": [{"path": "src/a.py", "added": True, "deleted": False,
                      "renamed": False, "rename_source": None,
                      "rename_target": None}],
            "files_truncated": False, "parent_sha": "a" * 40,
            "project_id": self.project.id, "source": "created",
            "task_id": task_id, "task_version": 3,
            "worker_report_digest": "d" * 64, "worker_report_id": 1,
            "worktree_path": "/tmp/none",
        }
        fields.update(overrides)
        return delivery._commit_payload(**fields)

    def test_commit_evidence_loader_rejects_tampered_payloads(self):
        task_id, attempt_id = self._binding("tamper")
        # 1) tampered field without digest recompute
        forged = json.loads(self._commit_payload(task_id, attempt_id))
        forged["commit_sha"] = "9" * 40
        events.append_event(
            self.conn, delivery.COMMIT_EVIDENCE_KIND,
            project_id=self.project.id, task_id=task_id, attempt_id=attempt_id,
            payload_json=delivery._canonical_json(forged),
            event_id="evt-dce-tamper-field")
        with self.assertRaises(StorageError):
            delivery.load_commit_evidence(self.conn, attempt_id)

        # 2) structurally wrong shape (missing required keys)
        task_id2, attempt_id2 = self._binding("shape")
        events.append_event(
            self.conn, delivery.COMMIT_EVIDENCE_KIND,
            project_id=self.project.id, task_id=task_id2, attempt_id=attempt_id2,
            payload_json="{}",
            event_id="evt-dce-tamper-shape")
        with self.assertRaises(StorageError):
            delivery.load_commit_evidence(self.conn, attempt_id2)

    def test_ship_and_approval_loaders_reject_bad_evidence(self):
        task_id, attempt_id = self._binding("shipbad")
        # A ship payload whose pinned verifier did not pass fails closed even
        # with a perfectly recomputing evidence_digest.
        verifiers = [{
            "profile": "test", "profile_version": 1, "config_digest": "e" * 64,
            "verification_id": "ver-1", "report_digest": "f" * 64,
            "log_digest": None, "exit_code": 1, "status": "fail",
        }]
        ship = {
            "allocation_id": "alloc-1", "attempt_id": attempt_id,
            "attempt_no": 1, "base_sha": "a" * 40,
            "branch": f"tc/{task_id}/{attempt_id}",
            "brief_digest": "b" * 64, "brief_version": 1,
            "commit_evidence_id": "evt-dce-x", "commit_sha": "c" * 40,
            "command_id": "cmd-1", "delivery_mode": "local_only",
            "diff_bytes": 10, "diff_digest": "1" * 64,
            "diff_relpath": f"ship/{attempt_id}/c-diff.diff",
            "dispatch_task_version": 2, "file_count": 1,
            "policy_digest": "2" * 64, "project_id": self.project.id,
            "proposed_action": delivery.LOCAL_ONLY_ACCEPT_ACTION,
            "schema_version": delivery.DELIVERY_CONTRACT_VERSION,
            "task_id": task_id, "task_version": 3,
            "verifier_digest": delivery._verifier_digest(verifiers),
            "verifiers_json": delivery._canonical_json(verifiers),
            "worker_report_digest": "d" * 64, "worker_report_id": 1,
            "worktree_path": "/tmp/none",
        }
        ship["evidence_digest"] = delivery._payload_digest(ship)
        events.append_event(
            self.conn, delivery.SHIP_EVIDENCE_KIND,
            project_id=self.project.id, task_id=task_id, attempt_id=attempt_id,
            payload_json=delivery._canonical_json(ship),
            event_id="evt-dse-fail-verifier")
        with self.assertRaises(StorageError):
            delivery.get_ship_evidence(self.conn, "evt-dse-fail-verifier")

        # An approval claiming accept must record task_status_after=done.
        approval = {
            "actor": "human", "attempt_id": attempt_id, "attempt_no": 1,
            "branch": f"tc/{task_id}/{attempt_id}",
            "brief_digest": "b" * 64, "brief_version": 1,
            "command_id": "cmd-2", "commit_sha": "c" * 40, "decision": "accept",
            "diff_digest": "1" * 64, "evidence_digest": "3" * 64,
            "project_id": self.project.id,
            "proposed_action": delivery.LOCAL_ONLY_ACCEPT_ACTION,
            "reason": "ok", "schema_version": delivery.DELIVERY_CONTRACT_VERSION,
            "ship_evidence_id": "evt-dse-fail-verifier", "task_id": task_id,
            "task_status_after": "verifying", "task_version": 3,
            "verifier_digest": delivery._verifier_digest(verifiers),
            "worker_report_digest": "d" * 64, "worker_report_id": 1,
        }
        approval["evidence_digest"] = delivery._payload_digest(approval)
        events.append_event(
            self.conn, delivery.APPROVAL_EVENT_KIND,
            project_id=self.project.id, task_id=task_id, attempt_id=attempt_id,
            payload_json=delivery._canonical_json(approval),
            event_id="evt-dap-bad-status")
        with self.assertRaises(StorageError):
            delivery.get_delivery_approval(self.conn, "evt-dap-bad-status")

    def test_evidence_digest_rule_matches_the_domain_gate(self):
        # delivery and domain independently recompute the SAME digest over the
        # canonical payload minus evidence_digest (the acceptance gate's rule).
        task_id, attempt_id = self._binding("digest")
        payload = json.loads(self._commit_payload(task_id, attempt_id))
        self.assertEqual(
            delivery._payload_digest(payload),
            domain._delivery_payload_digest(payload))


if __name__ == "__main__":
    unittest.main()
