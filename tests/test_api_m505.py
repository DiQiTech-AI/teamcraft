"""M5-05 usage & cost query/API acceptance (roadmap 9.5 M5-05).

All figures must come from the durable M5-04 ledger (usage.recorded events +
the CEO/Wake projection over the wakes table) and be served through the
existing authorized read surfaces with:

* role / locality / precision / time-window filters (closed sets, 422 on
  anything else);
* source, precision and currency labels on every row and total;
* failed attempts included (the ledger records every final WorkerReport);
* missing measurements labelled ``unavailable`` — never zero;
* totals clearly split into complete vs partial (NULLs visible, never
  silently summed);
* uniform auth/validation/redaction like every other query route.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from fastapi.testclient import TestClient

    from teamcraft.api import create_app
    from teamcraft.api.errors import NOT_FOUND, UNAUTHENTICATED, VALIDATION_FAILED
    from teamcraft.query import QueryService

    WEB_AVAILABLE = True
except ImportError:  # pragma: no cover
    WEB_AVAILABLE = False

from teamcraft import (briefs, domain, events, fresh_start, storage, usage,
                      worker_reports)

ERROR_KEYS = {"code", "message", "details", "request_id"}
WEB_DIR = Path(__file__).resolve().parents[1] / "teamcraft" / "api" / "web"


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


@unittest.skipUnless(WEB_AVAILABLE, "web test dependencies unavailable")
class UsageCostM505Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="tc-m505-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(self.root, ".teamcraft", "teamcraft.db")
        self.service = QueryService(self.db_path)
        self.app = create_app(query_service=self.service)
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()
        self.clock = FixedClock(datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc))
        self.fixture = self._seed()

    # -- seeding through the authentic core paths --------------------------

    def _seed(self) -> dict:
        conn = storage.connect(self.db_path)
        fx: dict = {}
        project = domain.create_project(conn, "m505", self.root, clock=self.clock)
        fx["project_id"] = project.id

        # task 1 — completed worker final WITH usage (authentic hook).
        done = self._attempt_with_final(conn, project.id, "worker-task",
                                       outcome="completed",
                                       usage_obj={"input_tokens": 1000,
                                            "output_tokens": 250,
                                            "total_tokens": 1250,
                                            "cost_microunits": 900})
        fx["task_id"] = done["task_id"]
        fx["attempt_id"] = done["attempt_id"]
        # task 2 — FAILED final: must still appear in usage.
        failed = self._attempt_with_final(conn, project.id, "failed-task",
                                         outcome="failed", usage_obj=None)
        fx["failed_task_id"] = failed["task_id"]
        # task 3 — no usage at all: everything unavailable, never zero.
        task3 = domain.create_task(conn, project.id, "quiet-task",
                                  clock=self.clock)
        fx["quiet_task_id"] = task3.id

        # explicit ledger rows (same events the hooks would write).
        rows = [
            usage.UsageRecord(
                role="verifier", source="verification_run", precision="exact",
                locality="local", input_tokens=400, output_tokens=100,
                elapsed_ms=5000,
                recorded_at="2026-11-21T09:30:00+00:00"),
            usage.UsageRecord(
                role="ceo", source="wake_dispatcher", precision="unavailable",
                wake_count=3,
                recorded_at="2026-11-21T09:45:00+00:00"),
            usage.UsageRecord(
                role="worker", source="worker_report", precision="exact",
                locality="local", input_tokens=700,
                recorded_at="2026-11-19T09:00:00+00:00"),  # outside window
            usage.UsageRecord(
                role="worker", source="usage_api", precision="estimated",
                model="cloud-m", locality="cloud", output_tokens=300,
                cost_microunits=450, currency="EUR", price_version="2026-11",
                recorded_at="2026-11-21T09:50:00+00:00"),
            usage.UsageRecord(
                role="ceo", source="worktree_verification_run",
                precision="estimated", locality="local", total_tokens=1500,
                cost_microunits=250, currency="USD", price_version="2026-10",
                elapsed_ms=2000,
                recorded_at="2026-11-21T09:55:00+00:00"),
        ]
        fx["usage_ids"] = [r.usage_id for r in rows]
        for row in rows:
            usage.record_usage(conn, project.id, row)

        # wakes for the CEO projection: submitted+sending count (2),
        # pending and blocked never count.
        for wake_id, state in (("wk-1", "submitted"), ("wk-2", "pending"),
                              ("wk-3", "blocked"), ("wk-4", "sending")):
            self._wake(conn, project.id, wake_id, state=state)
        conn.close()
        return fx

    def _attempt_with_final(self, conn, project_id, title, *, outcome,
                           usage_obj):
        task = domain.create_task(conn, project_id, title, clock=self.clock)
        task = domain.apply_task_event(
            conn, task.id, "dependencies_satisfied",
            expected_version=task.version, clock=self.clock)
        attempt = domain.create_attempt(
            conn, task.id, expected_version=task.version, clock=self.clock)
        domain.transition_attempt(conn, attempt.id, "start", clock=self.clock)
        domain.transition_attempt(conn, attempt.id, "activated", clock=self.clock)
        brief = briefs.create_attempt_brief(
            conn, attempt.id, content={"milestones": [{"id": "final"}]},
            clock=self.clock)
        report = {"schema_version": 1, "task_id": task.id,
                  "attempt_id": attempt.id, "task_version": brief.task_version,
                  "brief_digest": brief.brief_digest, "report_kind": "final",
                  "outcome": outcome, "milestone_id": "final",
                  "summary": "seeded", "artifact_digest": None,
                  "usage": usage_obj}
        worker_reports.record_worker_report(conn, report, clock=self.clock)
        return {"task_id": task.id, "attempt_id": attempt.id}

    def _wake(self, conn, project_id, wake_id, *, state) -> None:
        source = events.append_event(conn, "test.signal",
                                    project_id=project_id, payload_json="{}",
                                    clock=self.clock)
        now = self.clock.now().isoformat()
        with storage.transaction(conn):
            conn.execute(
                "INSERT INTO wakes (id, project_id, target_ceo_name, dedupe_key,"
                " window_start, priority, state, first_seq, last_seq,"
                " event_count, event_digest, created_at, updated_at)"
                " VALUES (?,?,'ceo',?,?,50,'pending',?,?,1,?,?,?)",
                (wake_id, project_id, f"dk-{wake_id}", now,
                 source.seq, source.seq, "0" * 64, now, now))
            if state == "blocked":
                conn.execute(
                    "UPDATE wakes SET state='blocked', updated_at=? WHERE id=?",
                    (now, wake_id))
            elif state != "pending":
                conn.execute(
                    "UPDATE wakes SET state='sending', updated_at=? WHERE id=?",
                    (now, wake_id))
                if state == "submitted":
                    conn.execute(
                        "UPDATE wakes SET state='submitted', updated_at=?,"
                        " submitted_at=? WHERE id=?", (now, now, wake_id))

    # -- helpers ------------------------------------------------------------

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **params) -> object:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.client.get(path + ("?" + query if query else ""),
                               headers=self.auth())

    def ok(self, response) -> dict:
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def uniform_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body.keys()), ERROR_KEYS)
        self.assertEqual(body["code"], code)
        return body

    # -- totals, labels and gaps ----------------------------------------------

    def test_project_usage_totals_are_labelled_and_never_zero_the_gaps(
            self) -> None:
        body = self.ok(self.get(f"/api/v1/projects/{self.fixture['project_id']}"
                               "/usage"))
        dims = body["totals"]["dimensions"]
        # unfiltered: 7 rows (2 hook finals — one completed with usage, one
        # failed without — plus 5 explicit ledger rows).  input known on
        # completed-hook, verifier and early rows = 2100.
        self.assertEqual(body["record_count"], 7)
        self.assertEqual(dims["input_tokens"]["known_sum"], 2100)
        self.assertEqual(dims["input_tokens"]["completeness"], "partial")
        self.assertEqual(dims["input_tokens"]["unknown_count"], 4)
        # cache: nobody reported any -> unavailable, and the strict total is
        # NULL (never 0)
        self.assertEqual(dims["cache_tokens"]["completeness"], "unavailable")
        self.assertIsNone(dims["cache_tokens"]["strict_total"])
        self.assertEqual(dims["wake_count"]["known_sum"], 3)
        costs = body["totals"]["cost_by_currency"]
        self.assertEqual(costs["EUR"]["known_sum"], 450)
        self.assertEqual(costs["USD"]["known_sum"], 250)
        self.assertEqual(costs["UNATTRIBUTED"]["known_sum"], 900)
        self.assertTrue(body["totals"]["mixed_currencies"])
        self.assertEqual(body["totals"]["unattributed_cost_count"], 1)
        self.assertEqual(body["totals"]["missing_price_version_cost_count"], 1)
        self.assertEqual(body["totals"]["price_versions"], ["2026-10", "2026-11"])
        self.assertTrue(body["totals"]["has_traceability_gap"])
        self.assertTrue(any("never summed as zero" in n for n in body["notes"]))
        # the overall label must reflect ACTUAL dimension verdicts, never a
        # blanket "partial" (M5-05 QA WARN): this set has partial dimensions
        self.assertTrue(body["totals"]["total_label"].startswith("partial"))
        # trend buckets exist per UTC day and carry the SHARED totals shape
        days = [entry["date"] for entry in body["trend"]]
        self.assertEqual(days, ["2026-11-19", "2026-11-21"])
        early_totals = body["trend"][0]["totals"]
        early_input = early_totals["dimensions"]["input_tokens"]
        self.assertEqual(early_input["known_sum"], 700)
        # the only row that day reported input -> that dimension is complete
        self.assertEqual(early_input["completeness"], "complete")
        self.assertEqual(early_input["unknown_count"], 0)
        # ...but unreported dimensions keep that bucket out of a plain
        # "complete": honest middle wording, never blanket partial
        self.assertTrue(early_totals["total_label"].startswith(
            "complete for reported dimensions"))

    def test_trend_entries_follow_the_shared_totals_dto_contract(self) -> None:
        """M5-05 QA release-blocker regression: trend buckets and totals.

        The console renders every totals-bearing object with ONE pair of
        helpers (``usageAmountText`` / ``usageCostText``) that read
        ``known_sum`` / ``unknown_count`` / ``completeness``.  Every
        bucket in ``trend`` must therefore carry the exact same DTO shape
        as the grand totals — and ``total_label`` must stay derivable
        from the per-dimension verdicts (never a blanket "partial").
        """

        body = self.ok(self.get(f"/api/v1/projects/{self.fixture['project_id']}"
                               "/usage"))
        required = {"known_sum", "known_count", "unknown_count", "available",
                    "complete", "strict_total", "completeness"}
        self.assertTrue(body["trend"])
        for entry in body["trend"]:
            self.assertEqual(set(entry) - {"date", "records", "totals"}, set())
            totals = entry["totals"]
            self.assertEqual(set(totals["dimensions"]),
                            set(usage.USAGE_DIMENSIONS))
            for dim in totals["dimensions"].values():
                self.assertTrue(required <= set(dim),
                              f"renderer field missing from {dim}")
            for currency, cost in totals["cost_by_currency"].items():
                self.assertTrue(required <= set(cost))
                # the exact cell the console prints: never undefined/null
                self.assertIsInstance(cost["known_sum"], int)
            kinds = {d["completeness"]
                     for d in totals["dimensions"].values()}
            label = totals["total_label"]
            if kinds == {"unavailable"}:
                self.assertTrue(label.startswith("unavailable"))
            elif "partial" in kinds:
                self.assertTrue(label.startswith("partial"))
            elif "unavailable" in kinds:
                self.assertTrue(label.startswith(
                    "complete for reported dimensions"))
            else:
                self.assertEqual(label, "complete")

    def test_console_helpers_render_every_live_payload_without_undefined(
            self) -> None:
        """Execute the REAL console helpers over REAL API payloads (node).

        This pins the API/UI contract behaviourally, not just by shape:
        if any served totals object can make the shipped renderer print
        ``undefined``/``null``/``NaN`` — the exact M5-05 day-trend
        blocker — this test fails.  Skipped only when node is absent.
        """

        node = shutil.which("node")
        if node is None:
            self.skipTest("node unavailable")
        js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        helpers = []
        for sig in ("function usageAmountText(totals, name) {",
                    "function usageCostText(totals) {"):
            start = js.index(sig)
            helpers.append(js[start:js.index("\n}", start) + 2])
        payloads: list[dict] = []

        def collect_totals(value) -> None:
            if isinstance(value, dict):
                if "dimensions" in value and "cost_by_currency" in value:
                    payloads.append(value)
                for nested in value.values():
                    collect_totals(nested)

        pid = self.fixture["project_id"]
        collect_totals(self.ok(self.get(f"/api/v1/projects/{pid}/usage")))
        for task_key in ("task_id", "failed_task_id", "quiet_task_id"):
            collect_totals(self.ok(
                self.get(f"/api/v1/tasks/{self.fixture[task_key]}/usage")))
        collect_totals(self.ok(self.get(f"/api/v1/projects/{pid}/ceo/usage")))
        self.assertTrue(payloads)
        script = "\n".join(helpers) + """
const payloads = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = [];
for (const p of payloads) {
  for (const name of Object.keys(p.dimensions || {})) {
    out.push(usageAmountText(p, name));
  }
  out.push(usageCostText(p));
}
const text = out.join("\\n");
if (/undefined|null|NaN/.test(text)) { console.log(text); process.exit(1); }
console.log("render-clean:" + out.length);
"""
        proc = subprocess.run([node, "-e", script], input=json.dumps(payloads),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0,
                         f"console helpers printed non-honest text:\n"
                         f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("render-clean", proc.stdout)

    def test_filters_are_closed_sets_and_time_window_works(self) -> None:
        base = f"/api/v1/projects/{self.fixture['project_id']}/usage"
        workers = self.ok(self.get(base, role="worker"))
        self.assertEqual({i["role"] for i in workers["items"]}, {"worker"})
        # A(completed) + failed final + early + cloud estimated
        self.assertEqual(workers["record_count"], 4)
        locals_only = self.ok(self.get(base, locality="local"))
        self.assertEqual({i["locality"] for i in locals_only["items"]},
                         {"local"})
        windowed = self.ok(self.get(base, since="2026-11-21T00:00:00%2B00:00"))
        self.assertNotIn("2026-11-19", [t["date"] for t in windowed["trend"]])
        self.assertEqual(windowed["totals"]["dimensions"]["input_tokens"
                                             ]["known_sum"], 1400)
        # closed-set + timestamp validation -> uniform 422, no data leak
        self.uniform_error(self.get(base, role="observer"), 422,
                          VALIDATION_FAILED)
        self.uniform_error(self.get(base, locality="onprem"), 422,
                          VALIDATION_FAILED)
        self.uniform_error(self.get(base, since="2026-11-21T00:00:00"), 422,
                          VALIDATION_FAILED)  # naive timestamp refused

    def test_pagination_has_more_and_traceability_ids(self) -> None:
        base = f"/api/v1/projects/{self.fixture['project_id']}/usage"
        page = self.ok(self.get(base, limit=2))
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["total"], 7)
        # totals cover the WHOLE scope even though items are one page
        self.assertEqual(page["totals"]["dimensions"]["input_tokens"
                                             ]["known_sum"], 2100)
        for item in page["items"]:
            self.assertTrue(item["event_id"].startswith("evt-usage-"))
            self.assertTrue(item["usage_id"].startswith("usage-"))
        roles = page["breakdown"]["by_role"]
        self.assertEqual(set(roles), {"ceo", "verifier", "worker"})

    # -- task level -----------------------------------------------------------

    def test_task_usage_includes_failed_and_quiet_task_is_unavailable(self) -> None:
        fx = self.fixture
        done = self.ok(self.get(f"/api/v1/tasks/{fx['task_id']}/usage"))
        self.assertEqual(done["record_count"], 1)
        item = done["items"][0]
        self.assertEqual(item["role"], "worker")
        self.assertEqual(item["precision"], "exact")
        self.assertEqual(item["attempt_outcome"], "completed")
        self.assertFalse(item["failure_included"])
        self.assertEqual(item["quantities"]["input_tokens"]["value"], 1000)
        # the hook carries no currency -> labeled, never invented
        self.assertIn("unattributed", item["currency_label"])
        self.assertEqual(item["quantities"]["cache_tokens"]["label"],
                         "unavailable")

        failed = self.ok(self.get(f"/api/v1/tasks/{fx['failed_task_id']}"
                                 "/usage"))
        self.assertEqual(failed["record_count"], 1,
                         "failed attempts must still be counted")
        self.assertTrue(failed["items"][0]["failure_included"])
        self.assertEqual(failed["items"][0]["attempt_outcome"], "failed")
        self.assertEqual(
            failed["totals"]["dimensions"]["input_tokens"]["completeness"],
            "unavailable")

        quiet = self.ok(self.get(f"/api/v1/tasks/{fx['quiet_task_id']}/usage"))
        self.assertEqual(quiet["record_count"], 0)
        self.assertEqual(quiet["items"], [])
        for dim in quiet["totals"]["dimensions"].values():
            self.assertEqual(dim["completeness"], "unavailable")
            self.assertIsNone(dim["strict_total"])
        self.assertEqual(quiet["totals"]["total_label"],
                         "unavailable — nothing recorded (not zero)")
        self.assertTrue(any("not zero" in n for n in quiet["notes"]))

        self.uniform_error(self.get("/api/v1/tasks/nope-nope/usage"), 404,
                          NOT_FOUND)

    # -- CEO / Wake -----------------------------------------------------------

    def test_ceo_usage_wake_projection_and_window(self) -> None:
        base = f"/api/v1/projects/{self.fixture['project_id']}/ceo/usage"
        body = self.ok(self.get(base))
        self.assertEqual(body["wake_usage"]["wake_count"], 2,
                         "submitted+sending count; pending/blocked never do")
        self.assertIn("unavailable", body["wake_usage"]["tokens_label"])
        self.assertEqual(body["wake_states"],
                         {"blocked": 1, "pending": 1, "sending": 1,
                          "submitted": 1})
        # ledger side: both CEO rows are visible with labels
        self.assertEqual(len(body["ledger_items"]), 2)
        by_source = {i["source"]: i for i in body["ledger_items"]}
        self.assertEqual(by_source["wake_dispatcher"]["quantities"
                                                 ]["wake_count"]["value"], 3)
        self.assertEqual(by_source["worktree_verification_run"
                                 ]["currency_label"], "USD")
        self.assertEqual(body["ledger_totals"]["dimensions"
                                 ]["wake_count"]["completeness"], "partial")
        later = self.ok(self.get(base, since="2026-11-22T00:00:00%2B00:00"))
        self.assertEqual(later["wake_usage"]["wake_count"], 0)
        self.assertEqual(later["ledger_items"], [])
        self.uniform_error(self.get(base, target="Bad Target"), 422,
                          VALIDATION_FAILED)

    # -- auth & redaction parity ----------------------------------------------

    def test_auth_required_and_body_never_echoes_token(self) -> None:
        base = f"/api/v1/projects/{self.fixture['project_id']}/usage"
        denial = self.client.get(base)
        self.uniform_error(denial, 401, UNAUTHENTICATED)
        body = self.ok(self.get(base))
        self.assertNotIn(self.token, str(body))
        ceo = self.ok(self.get(
            f"/api/v1/projects/{self.fixture['project_id']}/ceo/usage"))
        self.assertNotIn(self.token, str(ceo))


if __name__ == "__main__":
    unittest.main()
