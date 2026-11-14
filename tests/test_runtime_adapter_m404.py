"""M4-04 runtime-adapter tests for member-scoped local delivery.

These exercise the thin adapter over :mod:`teamcraft.delivery`: it must
(i) resolve the member's OWN Task/Attempt identity, (ii) refuse read-only
members before Core is touched, (iii) forward the fenced lease + idempotency
key verbatim, (iv) shape durable evidence into JSON-safe DTOs, (v) surface
currentness so stale/failed evidence is explicit (fail closed), and (vi) refuse
cross-attempt evidence access.  Core safety itself is covered by
``tests/test_delivery_m404.py``; parity here proves the surface reaches the
SAME Core entry points.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from teamcraft import delivery, leader, runtime_adapter as ra
from teamcraft.runtime_adapter import RuntimeAdapterError


def member(name: str, *, read_only: bool = False):
    return {
        "name": name, "role": "tester" if read_only else "developer",
        "responsibility": f"own {name}", "kind": "opencode",
        "task": f"deliver {name}", "ownership": [] if read_only else ["src/**"],
        "read_only": read_only, "cwd": None, "from_pane_id": "w1:p1",
        "pane_id": "w1:p2", "terminal_id": None, "agent_session": None,
        "agent_args": [], "opencode_config_content": None, "direction": None,
        "start_timeout_ms": 30_000, "phase": "pane_created",
        "prompt_may_have_been_delivered": False, "live_state": None,
        "error": None, "task_id": None, "attempt_id": None,
        "session_id": None, "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


def commit_evidence(attempt_id: str, task_id: str, project_id: str, **over):
    base = dict(
        seq=10, event_id="commit-ev-1", project_id=project_id, task_id=task_id,
        attempt_id=attempt_id, attempt_no=1, task_version=3,
        dispatch_task_version=1, brief_digest="a" * 64, brief_version=1,
        worker_report_id=7, worker_report_digest="b" * 64,
        allocation_id="alloc-1", worktree_path="/tmp/wt", branch="tc/t/a",
        base_sha="c" * 40, parent_sha="c" * 40, commit_sha="d" * 40,
        diff_digest="e" * 64, file_count=2, claims=("src/**",),
        claims_digest="f" * 64, source="created", command_id="cmd-1",
        evidence_digest="0" * 64, occurred_at="2026-01-01T00:00:00+00:00",
    )
    base.update(over)
    return delivery.CommitEvidence(**base)


def ship_evidence(attempt_id: str, task_id: str, project_id: str, **over):
    base = dict(
        seq=11, event_id="ship-ev-1", project_id=project_id, task_id=task_id,
        attempt_id=attempt_id, attempt_no=1, task_version=3,
        dispatch_task_version=1, brief_digest="a" * 64, brief_version=1,
        worker_report_id=7, worker_report_digest="b" * 64,
        delivery_mode="local_only",
        proposed_action=delivery.LOCAL_ONLY_ACCEPT_ACTION,
        allocation_id="alloc-1", worktree_path="/tmp/wt", branch="tc/t/a",
        base_sha="c" * 40, commit_sha="d" * 40,
        commit_evidence_id="commit-ev-1", diff_digest="e" * 64, diff_bytes=128,
        diff_relpath="deliveries/ship-ev-1.diff", file_count=2,
        verifiers=({"profile": "test", "profile_version": 1,
                   "config_digest": "1" * 64, "status": "pass"},),
        verifier_digest="2" * 64, policy_digest="3" * 64,
        evidence_digest="4" * 64, command_id="cmd-2",
        occurred_at="2026-01-01T00:00:00+00:00",
    )
    base.update(over)
    return delivery.ShipEvidence(**base)


def approval(evidence_id: str, attempt_id: str, task_id: str, project_id: str,
             **over):
    base = dict(
        seq=12, event_id="appr-1", command_id="cmd-3",
        ship_evidence_id=evidence_id, project_id=project_id, task_id=task_id,
        attempt_id=attempt_id, attempt_no=1, task_version=3,
        commit_sha="d" * 40, branch="tc/t/a", diff_digest="e" * 64,
        evidence_digest="4" * 64, verifier_digest="2" * 64,
        proposed_action=delivery.LOCAL_ONLY_ACCEPT_ACTION, decision="accept",
        actor="human", reason="looks good", task_status_after="done",
        occurred_at="2026-01-01T00:00:00+00:00",
    )
    base.update(over)
    return delivery.DeliveryApproval(**base)


class M404RuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m404-adapter-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "project"
        self.root.mkdir()
        self.conn, _ = ra.open_runtime(self.root)
        self.addCleanup(self.conn.close)
        self.state = {
            "version": ra.STATE_VERSION, "team_id": "team-m404",
            "project_root": str(self.root),
            "herdr_session": {"server_id": "srv", "socket_path": "/sock"},
            "created_at": int(time.time()),
            "members": {
                "dev": member("dev"),
                "reader": member("reader", read_only=True),
            },
        }
        for value in self.state["members"].values():
            value["cwd"] = str(self.root)
        ra.sync_state(self.conn, self.state)

    def identity(self, member_name: str = "dev"):
        row = self.conn.execute(
            "SELECT task_id, attempt_id FROM members WHERE name = ?",
            (member_name,)).fetchone()
        return str(row[0]), str(row[1])

    def project_id(self, member_name: str = "dev"):
        task_id, _ = self.identity(member_name)
        return str(self.conn.execute(
            "SELECT project_id FROM tasks WHERE id = ?",
            (task_id,)).fetchone()[0])

    def lease(self, member_name: str = "dev"):
        return leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id(member_name)),
            "adapter-test", ttl_seconds=60)

    # -- create local commit ------------------------------------------------

    def test_writable_member_commit_forwards_exact_attempt_and_fence(self):
        task_id, attempt_id = self.identity("dev")
        expected = commit_evidence(attempt_id, task_id, self.project_id("dev"))
        lease = self.lease()
        with mock.patch.object(delivery, "create_local_commit",
                              return_value=expected) as core:
            result = ra.create_member_local_commit(
                self.conn, "dev", idempotency_key="commit-1", lease=lease,
                message="feat: x", artifact_root="/tmp/artifacts")
        core.assert_called_once()
        self.assertEqual(core.call_args.args[1], attempt_id)
        self.assertIs(core.call_args.kwargs["lease"], lease)
        self.assertEqual(core.call_args.kwargs["idempotency_key"], "commit-1")
        self.assertEqual(core.call_args.kwargs["message"], "feat: x")
        self.assertEqual(result["member"], "dev")
        self.assertEqual(result["source"], "created")
        self.assertEqual(result["commit"]["commit_evidence_id"], expected.event_id)
        self.assertEqual(result["commit"]["commit_sha"], expected.commit_sha)
        self.assertEqual(result["commit"]["branch"], "tc/t/a")

    def test_read_only_member_commit_refused_before_core(self):
        with mock.patch.object(delivery, "create_local_commit") as core:
            with self.assertRaises(RuntimeAdapterError):
                ra.create_member_local_commit(
                    self.conn, "reader", idempotency_key="k",
                    lease=self.lease("reader"))
        core.assert_not_called()

    # -- reconcile commit receipt --------------------------------------------

    def test_reconcile_adopts_matching_attempt(self):
        task_id, attempt_id = self.identity("dev")
        project_id = self.project_id("dev")
        evidence = commit_evidence(attempt_id, task_id, project_id,
                                  source="reconciled")
        command = mock.Mock(id="cmd-1", kind=delivery.COMMIT_COMMAND_KIND,
                           state="confirmed", idempotency_key="commit-1",
                           payload_json=json.dumps({
                               "project_id": project_id, "task_id": task_id,
                               "attempt_id": attempt_id}))
        with mock.patch.object(ra.command_core, "get_command",
                               return_value=command), mock.patch.object(
                delivery, "reconcile_commit_receipt",
                return_value={"resolution": "adopted", "command": command,
                            "commit_evidence": evidence}) as core:
            result = ra.reconcile_member_commit_receipt(
                self.conn, "dev", "cmd-1", lease=self.lease(),
                actor="ops", reason="lost receipt")
        self.assertEqual(core.call_args.args[1], "cmd-1")
        self.assertEqual(result["resolution"], "adopted")
        self.assertEqual(result["command"]["command_id"], "cmd-1")
        self.assertEqual(result["commit"]["source"], "reconciled")

    def test_reconcile_of_foreign_attempt_refused(self):
        task_id, _attempt_id = self.identity("dev")
        project_id = self.project_id("dev")
        command = mock.Mock(id="cmd-x", kind=delivery.COMMIT_COMMAND_KIND,
                           state="unknown", idempotency_key="k",
                           payload_json=json.dumps({
                               "project_id": project_id, "task_id": task_id,
                               "attempt_id": "attempt-other"}))
        with mock.patch.object(ra.command_core, "get_command",
                               return_value=command), mock.patch.object(
                delivery, "reconcile_commit_receipt") as core:
            with self.assertRaises(RuntimeAdapterError):
                ra.reconcile_member_commit_receipt(
                    self.conn, "dev", "cmd-x", lease=self.lease(),
                    actor="ops", reason="foreign")
        core.assert_not_called()

    # -- record ship evidence ----------------------------------------------

    def test_ship_evidence_proposes_local_only_action(self):
        task_id, attempt_id = self.identity("dev")
        evidence = ship_evidence(attempt_id, task_id, self.project_id("dev"))
        lease = self.lease()
        with mock.patch.object(delivery, "record_ship_evidence",
                              return_value=evidence) as core:
            result = ra.record_member_ship_evidence(
                self.conn, "dev", idempotency_key="ship-1", lease=lease)
        self.assertEqual(core.call_args.args[1], attempt_id)
        self.assertIs(core.call_args.kwargs["lease"], lease)
        self.assertEqual(result["proposed_action"],
                         delivery.LOCAL_ONLY_ACCEPT_ACTION)
        self.assertEqual(result["ship"]["delivery_mode"], "local_only")
        self.assertEqual(result["ship"]["ship_evidence_id"], evidence.event_id)

    # -- status: currentness + fail closed -------------------------------

    def test_status_reports_current_and_stale_problems(self):
        task_id, attempt_id = self.identity("dev")
        project_id = self.project_id("dev")
        commit = commit_evidence(attempt_id, task_id, project_id)
        ship = ship_evidence(attempt_id, task_id, project_id)
        appr = approval(ship.id, attempt_id, task_id, project_id)
        with mock.patch.object(delivery, "load_commit_evidence",
                              return_value=commit), \
                mock.patch.object(delivery, "load_ship_evidence",
                                return_value=ship), \
                mock.patch.object(delivery, "load_delivery_approval",
                                return_value=appr), \
                mock.patch.object(delivery, "commit_evidence_is_current",
                                return_value=delivery.DeliveryFreshness(
                                    ok=True, problems=())), \
                mock.patch.object(delivery, "ship_evidence_is_current",
                                return_value=delivery.DeliveryFreshness(
                                    ok=False, problems=("commit: drift",))), \
                mock.patch.object(delivery, "delivery_approval_is_current",
                                return_value=delivery.DeliveryFreshness(
                                    ok=False, problems=("evidence: stale",))):
            result = ra.member_delivery_status(self.conn, "dev")
        self.assertTrue(result["bound"])
        self.assertTrue(result["commit"]["current"])
        self.assertFalse(result["ship"]["current"])
        self.assertEqual(result["ship"]["problems"], ["commit: drift"])
        self.assertFalse(result["approval"]["current"])
        self.assertEqual(result["proposed_action"],
                         delivery.LOCAL_ONLY_ACCEPT_ACTION)

    def test_read_only_member_status_is_empty_view(self):
        result = ra.member_delivery_status(self.conn, "reader")
        self.assertFalse(result["bound"])
        self.assertIsNone(result["commit"])
        self.assertIsNone(result["ship"])

    # -- diff read: integrity forwarded, cross-attempt refused ----------

    def test_ship_diff_reads_bound_artifact(self):
        task_id, attempt_id = self.identity("dev")
        ship = ship_evidence(attempt_id, task_id, self.project_id("dev"))
        payload = {"ship_evidence_id": ship.id, "diff": "+hello"}
        with mock.patch.object(delivery, "load_ship_evidence",
                              return_value=ship), \
                mock.patch.object(delivery, "get_ship_evidence",
                                return_value=ship), \
                mock.patch.object(delivery, "read_ship_diff",
                                return_value=payload) as core:
            result = ra.member_ship_diff(self.conn, "dev")
        self.assertIs(core.call_args.args[1], ship.id)
        self.assertEqual(result["diff"], "+hello")

    def test_ship_diff_foreign_evidence_refused(self):
        task_id, _attempt_id = self.identity("dev")
        stranger = ship_evidence("attempt-other", task_id, self.project_id("dev"))
        with mock.patch.object(delivery, "get_ship_evidence",
                              return_value=stranger):
            with self.assertRaises(RuntimeAdapterError):
                ra.member_ship_diff(
                    self.conn, "dev", ship_evidence_id=stranger.id)

    # -- accept / reject: echo + cross-attempt refusal -------------------

    def test_accept_forwards_exact_commit_and_branch(self):
        task_id, attempt_id = self.identity("dev")
        project_id = self.project_id("dev")
        ship = ship_evidence(attempt_id, task_id, project_id)
        result_appr = approval(ship.id, attempt_id, task_id, project_id,
                              decision="accept")
        lease = self.lease()
        with mock.patch.object(delivery, "get_ship_evidence",
                              return_value=ship), \
                mock.patch.object(delivery, "approve_local_delivery",
                                return_value=result_appr) as core:
            result = ra.decide_member_local_delivery(
                self.conn, "dev", decision="accept", ship_evidence_id=ship.id,
                commit_sha=ship.commit_sha, branch=ship.branch,
                actor="human", reason="ok", idempotency_key="acc-1", lease=lease)
        self.assertEqual(core.call_args.kwargs["commit_sha"], ship.commit_sha)
        self.assertEqual(core.call_args.kwargs["branch"], ship.branch)
        self.assertIs(core.call_args.kwargs["lease"], lease)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["task_status_after"], "done")
        self.assertEqual(result["approval"]["approval_id"], result_appr.id)

    def test_reject_forwards_and_keeps_verifying(self):
        task_id, attempt_id = self.identity("dev")
        ship = ship_evidence(attempt_id, task_id, self.project_id("dev"))
        rejected = approval(ship.id, attempt_id, task_id, self.project_id("dev"),
                           decision="reject", task_status_after="verifying")
        with mock.patch.object(delivery, "get_ship_evidence",
                              return_value=ship), \
                mock.patch.object(delivery, "approve_local_delivery",
                                return_value=rejected):
            result = ra.decide_member_local_delivery(
                self.conn, "dev", decision="reject", ship_evidence_id=ship.id,
                actor="human", reason="not yet", idempotency_key="rej-1",
                lease=self.lease())
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["task_status_after"], "verifying")

    def test_decide_foreign_evidence_refused_before_core(self):
        task_id, _attempt_id = self.identity("dev")
        stranger = ship_evidence("attempt-other", task_id, self.project_id("dev"))
        with mock.patch.object(delivery, "get_ship_evidence",
                              return_value=stranger), \
                mock.patch.object(delivery, "approve_local_delivery") as core:
            with self.assertRaises(RuntimeAdapterError):
                ra.decide_member_local_delivery(
                    self.conn, "dev", decision="accept",
                    ship_evidence_id=stranger.id, commit_sha=stranger.commit_sha,
                    branch=stranger.branch, actor="human", reason="r",
                    idempotency_key="k", lease=self.lease())
        core.assert_not_called()

    # -- report-only parity: refusal surfaces as a Core error -------------

    def test_report_only_commit_refusal_propagates(self):
        task_id, attempt_id = self.identity("dev")
        with mock.patch.object(
                delivery, "create_local_commit",
                side_effect=delivery.DeliveryRefusal(
                    "mode", "task policy is report_only")) as core:
            with self.assertRaises(delivery.DeliveryRefusal) as ctx:
                ra.create_member_local_commit(
                    self.conn, "dev", idempotency_key="k", lease=self.lease())
        self.assertEqual(ctx.exception.kind, "mode")
        core.assert_called_once()


if __name__ == "__main__":
    unittest.main()
