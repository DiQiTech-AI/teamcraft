"""M4-05 domain gate tests: direct-PR transitions + durable fact gates.

These tests pin the Task state-machine edges M4-05 adds —
``verifying --direct_pr_ready--> ready_to_merge`` and
``ready_to_merge --approved_and_delivered--> done`` — and prove the
durable-fact gates re-derive everything (mode, digests, head/base/checks,
approval binding, Attempt/Brief/WorkerReport pins) from append-only rows.

Rows are FABRICATED directly into the events stream here (never by calling the
executor) so the gates themselves are what is under test.  The payload digest
rule is re-implemented in this file — that duplication is itself pinned: any
payload these gates accept is byte-stable with :mod:`teamcraft.delivery`'s
producer, which the M4-05 delivery suite proves end-to-end.  No GitHub, no
network, no gh.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from teamcraft import briefs, domain, storage, worker_reports
from teamcraft.errors import IllegalTransitionError, ValidationError
from teamcraft.github_adapter import GitHubError

POLICY = {"delivery": "direct_pr",
          "direct_pr": {"repo": "acme/widgets", "base": "main"}}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_digest(payload: dict) -> str:
    stripped = {k: v for k, v in payload.items() if k != "evidence_digest"}
    return sha(canonical(stripped))


def with_digest_at(payload: dict, schema_version: int) -> str:
    payload = dict(payload)
    payload["schema_version"] = schema_version
    payload["evidence_digest"] = payload_digest(payload)
    return canonical(payload)


def with_digest(payload: dict) -> str:
    return with_digest_at(payload, domain.PR_DELIVERY_CONTRACT_VERSION)


class DirectPrGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m405-dom-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.repo = base / "repo"
        self.repo.mkdir()
        self.db = base / "teamcraft.db"
        storage.migrate_to_head(self.db, allow_unverified_location=True)
        self.conn = storage.connect(self.db, allow_unverified_location=True)
        self.addCleanup(self.conn.close)
        self.project = domain.create_project(self.conn, "demo", str(self.repo))

    # -- fixtures --------------------------------------------------------------

    def ship(self, *, policy: dict | None = None, kind: str = "ship"):
        task = domain.create_task(
            self.conn, self.project.id, "deliver it", kind=kind,
            ownership_json=json.dumps(["src/**"]),
        )
        task = domain.apply_task_event(
            self.conn, task.id, "dependencies_satisfied",
            expected_version=task.version)
        if policy is not None:
            task = domain.set_execution_policy(
                self.conn, task.id, expected_version=task.version,
                policy=policy, actor="ceo", reason="m405 gate test")
        attempt = domain.create_attempt(self.conn, task.id,
                                       expected_version=task.version)
        domain.register_session(self.conn, attempt.id, "opencode",
                               native_session_id=f"native-{attempt.id}",
                               pane_id="pane-m405", state="working")
        domain.transition_attempt(self.conn, attempt.id, "start")
        domain.transition_attempt(self.conn, attempt.id, "activated")
        brief = briefs.create_attempt_brief(self.conn, attempt.id,
                                           content="M4-05 gate brief")
        report = {
            "schema_version": 1, "task_id": task.id, "attempt_id": attempt.id,
            "task_version": brief.task_version,
            "brief_digest": brief.brief_digest, "report_kind": "final",
            "outcome": "completed", "milestone_id": "final",
            "summary": "done", "artifact_digest": None,
            "usage": {"total_tokens": None},
        }
        worker_reports.record_worker_report(self.conn, report)
        row = self.conn.execute(
            "SELECT id, report_digest FROM worker_reports WHERE attempt_id = ?"
            " AND report_kind = 'final' ORDER BY id DESC LIMIT 1",
            (attempt.id,)).fetchone()
        task = domain.apply_task_event(
            self.conn, task.id, "worker_reported_done",
            expected_version=domain.get_task(self.conn, task.id).version)
        return (domain.get_task(self.conn, task.id), attempt, brief,
                int(row[0]), brief.task_version, str(row[1]))

    def policy_digest_of(self, task) -> str:
        return sha(canonical(json.loads(task.execution_policy_json or "{}")))

    def pr_evidence_payload(self, task, attempt, brief, report_id, dispatch_ver,
                           report_digest, **over):
        checks = over.pop("checks", [{"name": "ci", "status": "COMPLETED",
                                     "conclusion": "SUCCESS"}])
        payload = {
            "project_id": self.project.id, "task_id": task.id,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "task_version": task.version,
            "dispatch_task_version": dispatch_ver,
            "brief_digest": brief.brief_digest, "brief_version": brief.brief_version,
            "worker_report_id": report_id, "worker_report_digest": report_digest,
            "delivery_mode": "direct_pr",
            "proposed_action": domain.DIRECT_PR_MERGE_ACTION,
            "policy_digest": self.policy_digest_of(task),
            "head_branch": over.pop("head_branch", f"tc/{task.id}/{attempt.id}"),
            "head_sha": over.pop("head_sha", "a" * 40),
            "base_ref": over.pop("base_ref", "main"),
            "mergeable": over.pop("mergeable", "MERGEABLE"),
            "checks_json": canonical(checks),
            "checks_digest": sha(canonical(checks)),
            "verifier_digest": over.pop("verifier_digest",
                                        sha("verifier-pin")),
        }
        payload.update(over)
        return payload

    def append(self, kind: str, task, attempt, payload_json: str,
               event_id: str | None = None):
        from teamcraft import events
        return events.append_event(
            self.conn, kind, project_id=self.project.id, task_id=task.id,
            attempt_id=attempt.id, payload_json=payload_json,
            event_id=event_id)

    def ready_task(self, **over):
        """Ship Task advanced to READY_TO_MERGE through fabricated evidence."""
        task, attempt, brief, report_id, dispatch_ver, report_digest = self.ship(
            policy=over.pop("policy", POLICY))
        payload = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest, **over)
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest(payload))
        task = domain.apply_task_event(
            self.conn, task.id, domain.DIRECT_PR_READY_EVENT,
            expected_version=task.version)
        return task, attempt, brief, report_id, dispatch_ver, report_digest

    def approval_row(self, task, attempt, pr_event_id, pr_digest, *, head_sha,
                    base="main", version=None, decision="approve", **over):
        payload = {
            "project_id": self.project.id, "task_id": task.id,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "task_version": version if version is not None else task.version,
            "pr_evidence_id": pr_event_id, "pr_evidence_digest": pr_digest,
            "repo": POLICY["direct_pr"]["repo"], "pr_number": 11,
            "head_sha": head_sha, "base_ref": base, "intended_base": base,
            "checks_digest": sha(canonical([])),
            "proposed_action": domain.DIRECT_PR_MERGE_ACTION,
            "decision": decision, "actor": "human", "reason": "ok",
            "task_status_after": "ready_to_merge",
        }
        payload.update(over)
        return self.append(domain.PR_MERGE_APPROVAL_EVENT_KIND, task, attempt,
                          with_digest(payload))

    def merge_row(self, task, attempt, pr_event_id, approval_event_id, *,
                  head_sha, base="main", intended="main", merge_sha=None,
                  merged=True, **over):
        payload = {
            "project_id": self.project.id, "task_id": task.id,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "task_version": task.version,
            "dispatch_task_version": self.dispatch_ver,
            "brief_digest": self.brief_digest, "brief_version": self.brief_version,
            "worker_report_id": self.report_id,
            "worker_report_digest": self.report_digest,
            "policy_digest": self.policy_digest_of(task),
            "delivery_mode": "direct_pr",
            "action": domain.DIRECT_PR_MERGE_ACTION,
            "pr_evidence_id": pr_event_id,
            "approval_event_id": approval_event_id,
            "repo": POLICY["direct_pr"]["repo"], "pr_number": 11, "pr_url": "",
            "head_sha": head_sha, "base_ref": base, "intended_base": intended,
            "merge_commit_sha": merge_sha or ("d" * 40),
            "merged": merged, "merged_at": "2026-11-14T00:00:00Z",
            "method": "merge", "source": "executed",
        }
        payload.update(over)
        return self.append(domain.PR_MERGE_EVIDENCE_EVENT_KIND, task, attempt,
                          with_digest(payload))

    # -- transition table ------------------------------------------------------

    def test_targets_matrix(self):
        self.assertEqual(
            domain.task_transition_targets("verifying",
                                          domain.DIRECT_PR_READY_EVENT, "ship"),
            ("ready_to_merge",))
        self.assertEqual(
            domain.task_transition_targets("verifying",
                                          domain.DIRECT_PR_READY_EVENT, "scout"),
            ())
        self.assertEqual(
            domain.task_transition_targets("ready_to_merge",
                                          "approved_and_delivered", "ship"),
            ("done",))
        # done is terminal: nothing re-delivers.
        self.assertEqual(
            domain.task_transition_targets("done", "approved_and_delivered",
                                          "ship"), ())
        self.assertEqual(
            domain.task_transition_targets("verifying",
                                          "approved_and_delivered", "ship"), ())

    # -- direct_pr_ready refusals ---------------------------------------------

    def test_ready_requires_ship_kind(self):
        task, _attempt, _brief, _rid, _dv, _rd = self.ship(
            policy=POLICY, kind="scout")
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "M4_EVIDENCE_REQUIRED")

    def test_ready_requires_persisted_direct_pr_mode(self):
        task, _a, _b, _r, _d, _x = self.ship()  # no policy persisted at all
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code,
                         "DIRECT_PR_MODE_REQUIRED")

    def test_ready_requires_durable_evidence(self):
        task, _a, _b, _r, _d, _x = self.ship(policy=POLICY)
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code,
                         "PR_READY_FACTS_MISSING")

    def test_ready_happy_path_fabricated(self):
        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ship(policy=POLICY)
        payload = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest)
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest(payload))
        updated = domain.apply_task_event(
            self.conn, task.id, domain.DIRECT_PR_READY_EVENT,
            expected_version=task.version)
        self.assertEqual(updated.status, "ready_to_merge")
        self.assertEqual(updated.version, task.version + 1)

    def test_ready_refuses_legacy_v1_evidence_without_verifier_digest(self):
        """v1 rows are readable for audit but can never authorize readiness."""

        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ship(policy=POLICY)
        payload = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest)
        payload.pop("verifier_digest")  # exactly what a v1 durable row carries
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest_at(payload, 1))
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(
                self.conn, task.id, domain.DIRECT_PR_READY_EVENT,
                expected_version=task.version)
        self.assertEqual(caught.exception.reason_code,
                         "PR_EVIDENCE_LEGACY_SCHEMA")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "verifying")

    def test_ready_ignores_legacy_when_current_v2_evidence_exists(self):
        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ship(policy=POLICY)
        legacy = self.pr_evidence_payload(task, attempt, brief, report_id,
                                         dispatch_ver, report_digest)
        legacy.pop("verifier_digest")
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest_at(legacy, 1))
        current = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest)
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest(current))
        updated = domain.apply_task_event(
            self.conn, task.id, domain.DIRECT_PR_READY_EVENT,
            expected_version=task.version)
        self.assertEqual(updated.status, "ready_to_merge")

    def test_ready_refuses_tampered_digest(self):
        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ship(policy=POLICY)
        payload = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest)
        json_str = with_digest(payload).replace(
            '"evidence_digest":"', '"evidence_digest":"0')
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt, json_str)
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "PR_READY_FACTS_STALE")

    def test_ready_refuses_policy_drift(self):
        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ship(policy=POLICY)
        payload = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest)
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest(payload))
        domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"direct_pr": {"repo": POLICY["direct_pr"]["repo"],
                                 "base": "release"}},
            actor="ceo", reason="base drift")
        drifted = domain.get_task(self.conn, task.id)
        self.assertEqual(drifted.version, task.version + 1)
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=drifted.version)
        # The policy edit bumped the Task version, so the current-version
        # evidence query finds nothing at all (strongest form of invalidation);
        # the digest-mismatch branch below covers same-version tampering.
        self.assertIn(caught.exception.reason_code,
                    ("PR_READY_FACTS_MISSING", "PR_READY_FACTS_STALE"))

    def test_ready_refuses_non_isolated_head(self):
        for head in ("main", "master", "feature/x"):
            with self.subTest(head=head):
                task, attempt, brief, report_id, dispatch_ver, report_digest = \
                    self.ship(policy=POLICY)
                payload = self.pr_evidence_payload(
                    task, attempt, brief, report_id, dispatch_ver, report_digest,
                    head_branch=head)
                self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                          with_digest(payload))
                with self.assertRaises(IllegalTransitionError):
                    domain.apply_task_event(
                        self.conn, task.id, domain.DIRECT_PR_READY_EVENT,
                        expected_version=task.version)

    def test_ready_refuses_non_mergeable_and_failing_checks(self):
        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ship(policy=POLICY)
        payload = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest,
                                          mergeable="CONFLICTING")
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest(payload))
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "PR_READY_FACTS_STALE")

        task2, a2, b2, r2, d2, x2 = self.ship(policy=POLICY)
        payload2 = self.pr_evidence_payload(
            task2, a2, b2, r2, d2, x2,
            checks=[{"name": "ci", "status": "COMPLETED",
                    "conclusion": "FAILURE"}])
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task2, a2,
                    with_digest(payload2))
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task2.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=task2.version)
        self.assertEqual(caught.exception.reason_code, "PR_READY_FACTS_STALE")

    def test_ready_refuses_stale_attempt_pin(self):
        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ship(policy=POLICY)
        payload = self.pr_evidence_payload(task, attempt, brief, report_id,
                                          dispatch_ver, report_digest)
        payload["attempt_id"] = "0" * 32  # not the latest finished Attempt
        self.append(domain.PR_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest(payload))
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id,
                                  domain.DIRECT_PR_READY_EVENT,
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "PR_FACTS_STALE")

    # -- completion gate (ready_to_merge -> done) -----------------------------

    def _prepare_ready(self):
        task, attempt, brief, report_id, dispatch_ver, report_digest = \
            self.ready_task()
        self.dispatch_ver = dispatch_ver
        self.brief_digest = brief.brief_digest
        self.brief_version = brief.brief_version
        self.report_id = report_id
        self.report_digest = report_digest
        head_sha = "a" * 40
        evidence_row = self.conn.execute(
            "SELECT event_id, payload_json FROM events WHERE kind = ?"
            " AND task_id = ?", (domain.PR_EVIDENCE_EVENT_KIND, task.id),
        ).fetchone()
        self.evidence_digest = json.loads(str(evidence_row[1]))["evidence_digest"]
        self.pr_event_id = str(evidence_row[0])
        self.head_sha = head_sha
        return task, attempt

    def test_done_requires_merge_facts(self):
        task, _attempt = self._prepare_ready()
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "MERGE_FACTS_MISSING")

    def test_done_refuses_legacy_v1_merge_evidence(self):
        task, attempt = self._prepare_ready()
        approval = self.approval_row(task, attempt, self.pr_event_id,
                                    self.evidence_digest, head_sha=self.head_sha)
        payload = {
            "project_id": self.project.id, "task_id": task.id,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "task_version": task.version,
            "dispatch_task_version": self.dispatch_ver,
            "brief_digest": self.brief_digest, "brief_version": self.brief_version,
            "worker_report_id": self.report_id,
            "worker_report_digest": self.report_digest,
            "policy_digest": self.policy_digest_of(task),
            "delivery_mode": "direct_pr",
            "action": domain.DIRECT_PR_MERGE_ACTION,
            "pr_evidence_id": self.pr_event_id,
            "approval_event_id": approval.event_id,
            "repo": POLICY["direct_pr"]["repo"], "pr_number": 11, "pr_url": "",
            "head_sha": self.head_sha, "base_ref": "main", "intended_base": "main",
            "merge_commit_sha": "d" * 40, "merged": True,
            "merged_at": "2026-11-14T00:00:00Z", "method": "merge",
            "source": "executed",
        }
        self.append(domain.PR_MERGE_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest_at(payload, 1))
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code,
                         "MERGE_FACTS_LEGACY_SCHEMA")
        self.assertEqual(domain.get_task(self.conn, task.id).status,
                         "ready_to_merge")

    def test_done_refuses_wrong_base(self):
        task, attempt = self._prepare_ready()
        approval = self.approval_row(task, attempt, self.pr_event_id,
                                    self.evidence_digest, head_sha=self.head_sha)
        self.merge_row(task, attempt, self.pr_event_id, approval.event_id,
                      head_sha=self.head_sha, base="release")
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "MERGE_FACTS_STALE")

    def test_done_requires_approval_row(self):
        task, attempt = self._prepare_ready()
        self.merge_row(task, attempt, self.pr_event_id, "evt-nonexistent",
                      head_sha=self.head_sha)
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "APPROVAL_REQUIRED")

    def test_done_refuses_approval_binding_mismatch(self):
        task, attempt = self._prepare_ready()
        approval = self.approval_row(task, attempt, self.pr_event_id,
                                    self.evidence_digest,
                                    head_sha="f" * 40)  # different head
        self.merge_row(task, attempt, self.pr_event_id, approval.event_id,
                      head_sha=self.head_sha)
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "APPROVAL_STALE")

    def test_done_refuses_stale_version_approval(self):
        task, attempt = self._prepare_ready()
        approval = self.approval_row(task, attempt, self.pr_event_id,
                                    self.evidence_digest, head_sha=self.head_sha,
                                    version=task.version - 1)
        self.merge_row(task, attempt, self.pr_event_id, approval.event_id,
                      head_sha=self.head_sha)
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "APPROVAL_REQUIRED")

    def test_done_happy_path_fabricated_and_reopen(self):
        task, attempt = self._prepare_ready()
        approval = self.approval_row(task, attempt, self.pr_event_id,
                                    self.evidence_digest, head_sha=self.head_sha)
        self.merge_row(task, attempt, self.pr_event_id, approval.event_id,
                      head_sha=self.head_sha)
        done = domain.apply_task_event(
            self.conn, task.id, "approved_and_delivered",
            expected_version=task.version)
        self.assertEqual(done.status, "done")
        # Any later Task mutation invalidates the merge-approved evidence.
        reopened = domain.update_task(self.conn, task.id,
                                     expected_version=done.version,
                                     title="mutated after done")
        self.assertEqual(reopened.status, "needs_human")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM events WHERE kind = ? AND task_id = ?",
            ("delivery.pr_merge_approval_invalidated", task.id)).fetchone())

    def test_done_refuses_missing_merge_commit_sha(self):
        task, attempt = self._prepare_ready()
        approval = self.approval_row(task, attempt, self.pr_event_id,
                                    self.evidence_digest, head_sha=self.head_sha)
        payload = {
            "project_id": self.project.id, "task_id": task.id,
            "attempt_id": attempt.id, "attempt_no": attempt.attempt_no,
            "task_version": task.version,
            "dispatch_task_version": self.dispatch_ver,
            "brief_digest": self.brief_digest, "brief_version": self.brief_version,
            "worker_report_id": self.report_id,
            "worker_report_digest": self.report_digest,
            "policy_digest": self.policy_digest_of(task),
            "delivery_mode": "direct_pr",
            "action": domain.DIRECT_PR_MERGE_ACTION,
            "pr_evidence_id": self.pr_event_id,
            "approval_event_id": approval.event_id,
            "repo": POLICY["direct_pr"]["repo"], "pr_number": 11, "pr_url": "",
            "head_sha": self.head_sha, "base_ref": "main", "intended_base": "main",
            "merge_commit_sha": "", "merged": True,
            "merged_at": "2026-11-14T00:00:00Z", "method": "merge",
            "source": "executed",
        }
        self.merge_row_payload = None
        self.append(domain.PR_MERGE_EVIDENCE_EVENT_KIND, task, attempt,
                    with_digest(payload))
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.apply_task_event(self.conn, task.id, "approved_and_delivered",
                                  expected_version=task.version)
        self.assertEqual(caught.exception.reason_code, "MERGE_FACTS_STALE")

    # -- policy validation -----------------------------------------------------

    def test_direct_pr_subpolicy_validation(self):
        task = domain.create_task(self.conn, self.project.id, "t", kind="ship",
                                 ownership_json="[]")
        task = domain.apply_task_event(self.conn, task.id,
                                     "dependencies_satisfied",
                                     expected_version=task.version)
        cases = [
            {"delivery": "direct_pr", "direct_pr": {"repo": "bad repo"}},
            {"delivery": "direct_pr", "direct_pr": {"base": "main"}},
            {"delivery": "direct_pr",
             "direct_pr": {"repo": "o/r", "base": "ma in", "merge_method": "merge"}},
            {"delivery": "direct_pr",
             "direct_pr": {"repo": "o/r", "base": "main",
                          "merge_method": "obliterate"}},
            {"delivery": "direct_pr",
             "direct_pr": {"repo": "o/r", "base": "main", "extra": 1}},
            {"delivery": "direct_pr", "direct_pr": "nope"},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValidationError):
                    domain.set_execution_policy(
                        self.conn, task.id, expected_version=task.version,
                        policy=case, actor="ceo", reason="bad")
        # delivery=direct_pr ALONE persists (honest configuration) but is not
        # executable — direct_pr_config stays None until repo/base are explicit.
        persisted = domain.set_execution_policy(
            self.conn, task.id, expected_version=task.version,
            policy={"delivery": "direct_pr"}, actor="ceo", reason="mode only")
        self.assertIsNone(domain.direct_pr_config(
            json.loads(persisted.execution_policy_json)))
        ok = domain.set_execution_policy(
            self.conn, task.id, expected_version=persisted.version,
            policy=POLICY, actor="ceo", reason="full")
        cfg = domain.direct_pr_config(json.loads(ok.execution_policy_json))
        self.assertEqual(cfg, {"repo": "acme/widgets", "base": "main",
                               "merge_method": "merge"})
        self.assertIsNone(domain.direct_pr_config({"delivery": "local_only"}))
        self.assertIsNone(domain.direct_pr_config("nonsense"))

    def test_adapter_rejects_traversal_atoms(self):
        # Defense-in-depth parity between adapter atoms and policy values.
        with self.assertRaises(GitHubError):
            from teamcraft.github_adapter import normalize_repo
            normalize_repo("../repo")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
