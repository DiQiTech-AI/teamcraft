import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from teamcraft import briefs, domain, runtime_adapter, storage, worker_reports
from teamcraft.errors import ValidationError


class WorkerReportsM203Tests(unittest.TestCase):
    def test_report_replay_is_idempotent_and_never_completes_task(self):
        path = Path(tempfile.mkdtemp()) / "report.db"; storage.migrate_to_head(path, allow_unverified_location=True)
        conn = storage.connect(path, allow_unverified_location=True)
        try:
            p = domain.create_project(conn, "p", "/tmp/m203-report")
            t = domain.create_task(conn, p.id, title="t")
            t = domain.apply_task_event(conn, t.id, "dependencies_satisfied", expected_version=t.version)
            a = domain.create_attempt(conn, t.id, expected_version=t.version)
            domain.transition_attempt(conn, a.id, "start")
            domain.transition_attempt(conn, a.id, "activated")
            b = briefs.create_attempt_brief(conn, a.id, content={"milestones": [{"id": "m1"}]})
            report = {"schema_version": 1, "task_id": t.id, "attempt_id": a.id, "task_version": b.task_version, "brief_digest": b.brief_digest, "report_kind": "progress", "milestone_id": "m1", "summary": "done", "artifact_digest": None, "usage": {"total_tokens": None}}
            report["report_digest"] = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            first = worker_reports.record_worker_report(conn, report)
            self.assertEqual(first, worker_reports.record_worker_report(conn, report))
            self.assertEqual(1, len(worker_reports.load_validated_progress(conn, a.id)))
            self.assertEqual("running", domain.get_task(conn, t.id).status)
        finally: conn.close()

    def test_malformed_objects_only_raise_validation_error(self):
        for report in ({"schema_version": True}, {"schema_version": 1, "report_kind": []}, {1: "bad"}):
            with self.assertRaises(ValidationError):
                worker_reports.parse_worker_report(report)

    def test_empty_plan_rejects_arbitrary_progress(self):
        path = Path(tempfile.mkdtemp()) / "empty.db"; storage.migrate_to_head(path, allow_unverified_location=True)
        conn = storage.connect(path, allow_unverified_location=True)
        try:
            p = domain.create_project(conn, "p", "/tmp/m203-empty")
            t = domain.create_task(conn, p.id, title="t")
            t = domain.apply_task_event(conn, t.id, "dependencies_satisfied", expected_version=t.version)
            a = domain.create_attempt(conn, t.id, expected_version=t.version)
            domain.transition_attempt(conn, a.id, "start"); domain.transition_attempt(conn, a.id, "activated")
            b = briefs.create_attempt_brief(conn, a.id, content="legacy")
            report = {"schema_version":1,"task_id":t.id,"attempt_id":a.id,"task_version":b.task_version,"brief_digest":b.brief_digest,"report_kind":"progress","milestone_id":"invented","summary":"x"}
            with self.assertRaises(ValidationError): worker_reports.record_worker_report(conn, report)
        finally: conn.close()

    def test_final_milestone_requires_final_kind_and_final_succeeds(self):
        path = Path(tempfile.mkdtemp()) / "final.db"; storage.migrate_to_head(path, allow_unverified_location=True)
        conn = storage.connect(path, allow_unverified_location=True)
        try:
            p = domain.create_project(conn, "p", "/tmp/m203-final")
            t = domain.create_task(conn, p.id, title="t")
            t = domain.apply_task_event(conn, t.id, "dependencies_satisfied", expected_version=t.version)
            a = domain.create_attempt(conn, t.id, expected_version=t.version)
            domain.transition_attempt(conn, a.id, "start"); domain.transition_attempt(conn, a.id, "activated")
            b = briefs.create_attempt_brief(conn, a.id, content={"milestones": [{"id": "final"}]})
            report = {"schema_version":1,"task_id":t.id,"attempt_id":a.id,"task_version":b.task_version,"brief_digest":b.brief_digest,"report_kind":"progress","milestone_id":"final","summary":"x"}
            with self.assertRaises(ValidationError): worker_reports.record_worker_report(conn, report)
            report.update({"report_kind":"final", "outcome":"completed"})
            worker_reports.record_worker_report(conn, report)
            self.assertEqual("finished", domain.get_attempt(conn, a.id).status)
        finally: conn.close()

    def test_runtime_empty_acceptance_brief_allows_only_final_final(self):
        path = Path(tempfile.mkdtemp()) / "runtime-empty.db"; storage.migrate_to_head(path, allow_unverified_location=True)
        conn = storage.connect(path, allow_unverified_location=True)
        try:
            p = domain.create_project(conn, "p", "/tmp/m203-runtime-empty")
            t = domain.create_task(conn, p.id, title="t", acceptance_json="[]")
            t = domain.apply_task_event(conn, t.id, "dependencies_satisfied", expected_version=t.version)
            a = domain.create_attempt(conn, t.id, expected_version=t.version)
            domain.transition_attempt(conn, a.id, "start"); domain.transition_attempt(conn, a.id, "activated")
            with storage.transaction(conn):
                conn.execute("INSERT INTO members (name,role,responsibility,kind,task,ownership_json,read_only,cwd,agent_args_json,start_timeout_ms,phase,prompt_may_have_been_delivered,task_id,attempt_id,created_at,updated_at) VALUES ('dev','developer','impl','codex','impl','[]',0,'/tmp','[]',30000,'agent_started',0,?,?,0,0)", (t.id, a.id))
            b = runtime_adapter.prepare_attempt_brief(conn, "dev")
            self.assertEqual([], __import__('json').loads(b.content)["task"]["acceptance"])
            progress = {"schema_version":1,"task_id":t.id,"attempt_id":a.id,"task_version":b.task_version,"brief_digest":b.brief_digest,"report_kind":"progress","milestone_id":"final","summary":"x"}
            with self.assertRaises(ValidationError): worker_reports.record_worker_report(conn, progress)
            progress.update({"report_kind":"final", "outcome":"completed"})
            worker_reports.record_worker_report(conn, progress)
            self.assertEqual("finished", domain.get_attempt(conn, a.id).status)
        finally: conn.close()
