"""M6-01 read-only dependency DAG query/API (roadmap 9.6 M6-01).

Pinned acceptance for the graph surface feeding the console DAG view:

* UI matches backend dependencies — every edge of
  ``GET /projects/{id}/dependency-graph`` is a durable ``task_dependencies``
  row, identical to what ``GET /tasks/{id}/dependencies`` reports, and the
  node set equals the Task rows in core order;
* cycles cannot be manufactured by the UI — the surface is GET-only (write
  methods refused), and the ``acyclic``/``cycle_nodes`` fields VERIFY the
  invariant against the data: a tamper-inserted cycle (which the Core's
  ``add_dependency`` refuses) is reported honestly instead of laid out as
  legitimate;
* blockers are explainable — per-node ``unmet_dependencies`` mirrors the
  domain's own "dependency not DONE" predicate and ``blocking_path`` walks
  that same predicate upstream with an explicit truncation flag;
* status/kind filters and pagination act on nodes; edges pointing outside
  the returned page are surfaced in ``edges_outside_page`` rather than
  silently dropped;
* uniform auth/validation shapes (401/404/422) like every other query
  route, and reading never mutates the store.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

try:
    from fastapi.testclient import TestClient

    from teamcraft.api import create_app
    from teamcraft.api.errors import NOT_FOUND, UNAUTHENTICATED, VALIDATION_FAILED
    from teamcraft.query import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, QueryService

    _API_OK = True
    _API_REASON = ""
except ImportError as exc:  # pragma: no cover
    _API_OK = False
    _API_REASON = f"web dependencies unavailable: {exc}"

from teamcraft import domain, fresh_start, storage

ERROR_KEYS = {"code", "message", "details", "request_id"}


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


@unittest.skipUnless(_API_OK, _API_REASON)
class DependencyGraphM601Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="tc-m601-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(self.root, ".teamcraft", "teamcraft.db")
        self.service = QueryService(self.db_path)
        self.app = create_app(query_service=self.service)
        self.client = TestClient(self.app)
        self.token = self.app.state.token_store.ensure()
        self.clock = FixedClock(datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc))
        self.fx = self._seed()

    # -- seeding through the authentic core paths ---------------------------

    def _seed(self) -> dict:
        conn = storage.connect(self.db_path)
        fx: dict = {}
        project = domain.create_project(conn, "m601", self.root, clock=self.clock)
        fx["project_id"] = project.id

        root = domain.create_task(conn, project.id, "root-scout", kind="scout",
                                 clock=self.clock)
        mid = domain.create_task(conn, project.id, "middle-task",
                                clock=self.clock)
        leaf = domain.create_task(conn, project.id, "leaf-ship",
                                 clock=self.clock)
        alone = domain.create_task(conn, project.id, "standalone",
                                  clock=self.clock)
        fx.update(root=root.id, mid=mid.id, leaf=leaf.id, alone=alone.id)

        # real edges through the domain (version bumps included)
        domain.add_dependency(conn, mid.id, root.id,
                             expected_version=mid.version, clock=self.clock)
        domain.add_dependency(conn, leaf.id, mid.id,
                             expected_version=leaf.version, clock=self.clock)

        # a durable member assignment on the leaf task
        now_iso = self.clock.now().isoformat()
        with storage.transaction(conn):
            conn.execute(
                "INSERT INTO members (name, role, responsibility, kind, task,"
                " cwd, phase, task_id, created_at, updated_at)"
                " VALUES ('worker-a','worker','seed','opencode','t','/nowhere',"
                "'activated',?,?,?)",
                (leaf.id, now_iso, now_iso))
        conn.close()
        return fx

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

    def graph(self, **params) -> dict:
        return self.ok(self.get(
            f"/api/v1/projects/{self.fx['project_id']}/dependency-graph",
            **params))

    def db_digest(self) -> str:
        with open(self.db_path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    # -- UI matches backend dependencies --------------------------------------

    def test_nodes_and_edges_are_the_core_rows(self) -> None:
        body = self.graph()
        core_order = [t["id"] for t in
                      self.service.list_tasks(self.fx["project_id"])["items"]]
        self.assertEqual([n["id"] for n in body["nodes"]], core_order)
        edges = {(e["source"], e["target"]) for e in body["edges"]}
        self.assertEqual(edges, {(self.fx["root"], self.fx["mid"]),
                                (self.fx["mid"], self.fx["leaf"])})
        self.assertTrue(all(not e["satisfied"] for e in body["edges"]),
                        "no dependency is DONE yet — nothing may look satisfied")
        self.assertEqual(body["stats"]["tasks_total"], 4)
        self.assertEqual(body["stats"]["edges_total"], 2)

    def test_edges_identical_to_per_task_dependency_route(self) -> None:
        body = self.graph()
        by_id = {n["id"]: n for n in body["nodes"]}
        for tid in (self.fx["root"], self.fx["mid"], self.fx["leaf"],
                    self.fx["alone"]):
            deps = self.ok(self.get(f"/api/v1/tasks/{tid}/dependencies"))
            self.assertEqual(by_id[tid]["depends_on"], deps["depends_on"], tid)
            self.assertEqual(by_id[tid]["required_by"], deps["required_by"], tid)

    def test_depth_is_longest_chain_over_full_graph(self) -> None:
        body = self.graph()
        depths = {n["id"]: n["depth"] for n in body["nodes"]}
        self.assertEqual(depths, {self.fx["root"]: 0, self.fx["mid"]: 1,
                                 self.fx["leaf"]: 2, self.fx["alone"]: 0})
        self.assertEqual(body["stats"]["roots"], 2)
        self.assertEqual(body["stats"]["max_depth"], 2)

    def test_role_and_assignee_facts(self) -> None:
        body = self.graph()
        by_id = {n["id"]: n for n in body["nodes"]}
        self.assertEqual(by_id[self.fx["root"]]["kind"], "scout")
        self.assertEqual(by_id[self.fx["root"]]["role"], "scout")
        self.assertEqual(by_id[self.fx["leaf"]]["assignees"],
                         [{"name": "worker-a", "role": "worker",
                           "phase": "activated"}])
        self.assertEqual(by_id[self.fx["alone"]]["assignees"], [])

    # -- blocker explanations grounded in the core predicate -----------------

    def test_unmet_dependencies_and_blocking_paths(self) -> None:
        body = self.graph()
        by_id = {n["id"]: n for n in body["nodes"]}
        self.assertEqual([d["id"] for d in
                         by_id[self.fx["mid"]]["unmet_dependencies"]],
                         [self.fx["root"]])
        leaf = by_id[self.fx["leaf"]]
        self.assertEqual([d["id"] for d in leaf["unmet_dependencies"]],
                         [self.fx["mid"]])
        # upstream chain: leaf waits on mid which waits on root.
        self.assertEqual([p["id"] for p in leaf["blocking_path"]],
                         [self.fx["mid"], self.fx["root"]])
        self.assertFalse(leaf["blocking_path_truncated"])
        self.assertTrue(leaf["blocked_by_dependencies"])
        self.assertFalse(leaf["status_blocked"])
        self.assertEqual(by_id[self.fx["alone"]]["unmet_dependencies"], [])
        self.assertFalse(by_id[self.fx["alone"]]["blocked_by_dependencies"])
        self.assertFalse(by_id[self.fx["root"]]["blocked_by_dependencies"])

    def test_satisfied_edge_after_dependency_done(self) -> None:
        conn = storage.connect(self.db_path)
        with storage.transaction(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?",
                        (self.fx["root"],))
        conn.close()
        body = self.graph()
        edge = {(e["source"], e["target"]): e for e in body["edges"]}
        self.assertTrue(edge[(self.fx["root"], self.fx["mid"])]["satisfied"])
        self.assertFalse(edge[(self.fx["mid"], self.fx["leaf"])]["satisfied"])
        by_id = {n["id"]: n for n in body["nodes"]}
        self.assertEqual(by_id[self.fx["mid"]]["unmet_dependencies"], [])
        self.assertTrue(by_id[self.fx["mid"]]["status"] != "done")

    # -- cycles: verified, never manufactured ----------------------------------

    def test_graph_reports_acyclic_when_core_built(self) -> None:
        body = self.graph()
        self.assertTrue(body["acyclic"])
        self.assertEqual(body["cycle_nodes"], [])

    def test_tampered_cycle_is_reported_not_laid_out(self) -> None:
        # Bypassing the domain on purpose: the CORE refuses such an edge
        # (add_dependency raises DependencyCycleError); the read surface must
        # expose the inconsistency honestly instead of drawing a fake layer.
        conn = storage.connect(self.db_path)
        with storage.transaction(conn):
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_id,"
                " created_at) VALUES (?,?,?)",
                (self.fx["root"], self.fx["mid"],
                 self.clock.now().isoformat()))
        conn.close()
        body = self.graph()
        self.assertFalse(body["acyclic"])
        self.assertIn(self.fx["root"], body["cycle_nodes"])
        self.assertIn(self.fx["mid"], body["cycle_nodes"])
        depths = {n["id"]: n["depth"] for n in body["nodes"]}
        self.assertIsNone(depths[self.fx["root"]])
        self.assertIsNone(depths[self.fx["mid"]])

    def test_core_still_refuses_the_cycle_through_the_domain(self) -> None:
        conn = storage.connect(self.db_path)
        root = domain.get_task(conn, self.fx["root"])
        with self.assertRaises(domain.DependencyCycleError):
            domain.add_dependency(conn, self.fx["root"], self.fx["mid"],
                                 expected_version=root.version, clock=self.clock)
        conn.close()

    # -- filters, pagination, honesty about the page edge --------------------

    def test_filters_are_closed_sets(self) -> None:
        kinds = {n["kind"] for n in self.graph(kind="scout")["nodes"]}
        self.assertEqual(kinds, {"scout"})
        all_nodes = self.graph()["nodes"]
        pending_like = {n["status"] for n in all_nodes}
        one_status = min(pending_like)
        only = self.graph(status=one_status)["nodes"]
        self.assertTrue(only)
        self.assertTrue(all(n["status"] == one_status for n in only))
        self.assertEqual(
            self.uniform_error(self.get(
                f"/api/v1/projects/{self.fx['project_id']}/dependency-graph",
                status="not-a-status"),
                422, VALIDATION_FAILED)["code"], VALIDATION_FAILED)
        self.assertEqual(
            self.uniform_error(self.get(
                f"/api/v1/projects/{self.fx['project_id']}/dependency-graph",
                kind="boss"), 422, VALIDATION_FAILED)["code"],
            VALIDATION_FAILED)

    def test_pagination_and_outside_page_edges(self) -> None:
        core_order = [t["id"] for t in
                     self.service.list_tasks(self.fx["project_id"])["items"]]
        page1_ids = core_order[:2]
        body = self.graph(limit=2, offset=0)
        self.assertEqual([n["id"] for n in body["nodes"]], page1_ids)
        self.assertEqual(body["page"], {"limit": 2, "offset": 0, "total": 4,
                                       "has_more": True})
        # in-view + outside == total; nothing silently dropped
        page1 = set(page1_ids)
        all_edges = {(self.fx["root"], self.fx["mid"]),
                     (self.fx["mid"], self.fx["leaf"])}
        expect_outside = [{"source": s, "target": t} for s, t in all_edges
                         if s not in page1 or t not in page1]
        expect_in = len(all_edges) - len(expect_outside)
        self.assertEqual(body["stats"]["edges_in_view"], expect_in)
        self.assertEqual(body["stats"]["edges_outside_page"],
                         len(expect_outside))
        self.assertEqual({(e["source"], e["target"])
                         for e in body["edges_outside_page"]},
                         {(e["source"], e["target"]) for e in expect_outside})
        # depth still spans the FULL graph even though only 2 nodes are shown
        self.assertEqual(body["stats"]["max_depth"], 2)
        page2 = self.graph(limit=2, offset=2)
        self.assertEqual([n["id"] for n in page2["nodes"]], core_order[2:])
        self.assertFalse(page2["page"]["has_more"])

    def test_page_size_bounded_like_every_list_route(self) -> None:
        body = self.graph()
        self.assertEqual(body["page"]["limit"], DEFAULT_PAGE_SIZE)
        bad = self.get(
            f"/api/v1/projects/{self.fx['project_id']}/dependency-graph",
            limit=MAX_PAGE_SIZE + 1)
        self.uniform_error(bad, 422, VALIDATION_FAILED)

    # -- auth / method / read-only invariants -----------------------------------

    def test_requires_authentication(self) -> None:
        denial = self.client.get(
            f"/api/v1/projects/{self.fx['project_id']}/dependency-graph")
        self.uniform_error(denial, 401, UNAUTHENTICATED)

    def test_unknown_project_is_uniform_404(self) -> None:
        body = self.uniform_error(
            self.client.get("/api/v1/projects/proj_missing/dependency-graph",
                           headers=self.auth()),
            404, NOT_FOUND)
        self.assertEqual(body["code"], NOT_FOUND)

    def test_write_methods_refused_and_store_untouched(self) -> None:
        before = self.db_digest()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.client.request(
                method,
                f"/api/v1/projects/{self.fx['project_id']}/dependency-graph",
                headers=self.auth(), json={})
            self.assertIn(response.status_code, (403, 405), method)
        self.assertEqual(self.db_digest(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
