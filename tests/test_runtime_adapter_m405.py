"""M4-05 runtime adapter scope and read-only gates."""

from types import SimpleNamespace
import unittest
from unittest import mock

from teamcraft import runtime_adapter as ra
from teamcraft.runtime_adapter import RuntimeAdapterError


class DirectPrRuntimeAdapterTests(unittest.TestCase):
    def test_read_only_member_cannot_prepare_sync_or_reconcile(self):
        row = ("tester", True, "task-1", "attempt-1")
        with mock.patch.object(ra, "_workspace_member_row", return_value=row), \
             mock.patch.object(ra.delivery, "prepare_direct_pr") as prepare, \
             mock.patch.object(ra.delivery, "sync_direct_pr") as sync, \
             mock.patch.object(ra.command_core, "get_command") as get_command:
            with self.assertRaises(RuntimeAdapterError):
                ra.prepare_member_direct_pr(
                    object(), "tester", idempotency_key="prepare-1",
                    lease=object())
            with self.assertRaises(RuntimeAdapterError):
                ra.sync_member_direct_pr(
                    object(), "tester", idempotency_key="sync-1",
                    lease=object())
            with self.assertRaises(RuntimeAdapterError):
                ra.reconcile_member_pr_effect(
                    object(), "tester", "cmd-1", lease=object(),
                    actor="human", reason="inspect")
        prepare.assert_not_called()
        sync.assert_not_called()
        get_command.assert_not_called()

    def test_read_only_member_cannot_approve_or_merge(self):
        row = ("tester", True, "task-1", "attempt-1")
        with mock.patch.object(ra, "_workspace_member_row", return_value=row), \
             mock.patch.object(ra.delivery, "get_pr_evidence") as get_evidence:
            with self.assertRaises(RuntimeAdapterError):
                ra.decide_member_pr_merge(
                    object(), "tester", pr_evidence_id="pr-1",
                    decision="approve", pr_number=1, head_sha="a" * 40,
                    base="main", actor="human", reason="reviewed",
                    idempotency_key="approve-1", lease=object())
            with self.assertRaises(RuntimeAdapterError):
                ra.merge_member_direct_pr(
                    object(), "tester", pr_evidence_id="pr-1",
                    idempotency_key="merge-1", lease=object())
        get_evidence.assert_not_called()

    def test_foreign_pr_evidence_is_refused_before_effect(self):
        row = ("developer", False, "task-1", "attempt-1")
        foreign = SimpleNamespace(id="pr-1", attempt_id="attempt-2")
        with mock.patch.object(ra, "_workspace_member_row", return_value=row), \
             mock.patch.object(ra.delivery, "get_pr_evidence",
                               return_value=foreign), \
             mock.patch.object(ra.delivery, "approve_direct_pr_merge") as approve, \
             mock.patch.object(ra.delivery, "merge_direct_pr") as merge:
            with self.assertRaises(RuntimeAdapterError):
                ra.decide_member_pr_merge(
                    object(), "dev", pr_evidence_id="pr-1",
                    decision="approve", pr_number=1, head_sha="a" * 40,
                    base="main", actor="human", reason="reviewed",
                    idempotency_key="approve-1", lease=object())
            with self.assertRaises(RuntimeAdapterError):
                ra.merge_member_direct_pr(
                    object(), "dev", pr_evidence_id="pr-1",
                    idempotency_key="merge-1", lease=object())
        approve.assert_not_called()
        merge.assert_not_called()

    def test_reconcile_rejects_foreign_command_scope(self):
        row = ("developer", False, "task-1", "attempt-1")
        command = SimpleNamespace(
            id="cmd-1", kind=ra.delivery.PR_PREPARE_COMMAND_KIND,
            payload_json='{"task_id":"task-2","attempt_id":"attempt-2"}')
        with mock.patch.object(ra, "_workspace_member_row", return_value=row), \
             mock.patch.object(ra.command_core, "get_command",
                               return_value=command), \
             mock.patch.object(ra.delivery, "reconcile_pr_receipt") as reconcile:
            with self.assertRaises(RuntimeAdapterError):
                ra.reconcile_member_pr_effect(
                    object(), "dev", "cmd-1", lease=object(), actor="human",
                    reason="inspect")
        reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
