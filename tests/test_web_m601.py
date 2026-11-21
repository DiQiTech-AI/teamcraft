"""M6-01 DAG console contract tests (static + served shell).

Follows the M3-05/M5-05 discipline — the console is checked by
construction, with contracts specific to the read-only DAG view:

* a DAG tab exists in the shell and routes to ``viewDag`` without
  disturbing the existing tabs;
* the view calls ONLY the authorized M6-01 read route
  (``/projects/{id}/dependency-graph``) through the guarded ``api()``
  helper — no direct ``fetch``, no external URLs anywhere;
* zero-dependency rendering: plain inline SVG via ``createElementNS``;
  React Flow is absent in this environment (no package.json / node_modules
  / build step) and the page never references it; the SVG namespace is
  assembled at runtime so the static "no http(s) literal" invariant stays
  mechanically checkable;
* read-only by construction: the DAG block contains no write method, no
  drag/resize/pointer-edit handlers and no ``contenteditable`` — the UI
  cannot manufacture an edge, so cycle prevention stays with the Core;
* status, role and blockers render from backend fields only
  (``statusKind`` colours, ``kind``/``assignees`` roles, and the
  ``unmet_dependencies``/``blocking_path`` explanation table);
* big-graph affordances exist: closed-set status/kind filters, id
  locate/highlight, and pagination.

Not covered here (recorded, not claimed): real-browser rendering and
click-through (that matrix belongs to the M3-06 Chromium suite).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient

    from teamcraft.api import create_app

    WEB_AVAILABLE = True
except ImportError:  # pragma: no cover
    WEB_AVAILABLE = False

WEB_ROOT = Path(__file__).resolve().parent.parent / "teamcraft" / "api" / "web"

DANGEROUS_SINKS = ("innerHTML", "outerHTML", "document.write", "eval(",
                   "new Function", "localStorage", "insertAdjacentHTML")


def _js() -> str:
    return (WEB_ROOT / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (WEB_ROOT / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (WEB_ROOT / "style.css").read_text(encoding="utf-8")


def dag_block(js: str) -> str:
    start = js.index("async function viewDag(m)")
    end = js.index("async function viewTaskDetail(m)", start)
    return js[start:end]


class DagConsoleM601Tests(unittest.TestCase):
    # -- shell wiring ----------------------------------------------------------

    def test_dag_tab_exists_without_disturbing_existing_tabs(self) -> None:
        html = _html()
        for view in ("overview", "tasks", "agents", "ceo", "events",
                    "wakes", "usage", "approvals", "verifications"):
            self.assertIn(f'data-view="{view}"', html)
        self.assertEqual(html.count('data-view="dag"'), 1)

    def test_view_dag_registered_and_defined_once(self) -> None:
        js = _js()
        self.assertIn("dag: viewDag,", js)
        self.assertEqual(js.count("async function viewDag(m)"), 1)

    # -- route surface -----------------------------------------------------------

    def test_dag_view_calls_only_the_guarded_graph_route(self) -> None:
        block = dag_block(_js())
        self.assertIn('"/dependency-graph?"', block)
        # every read still goes through api() against relative /api/v1
        self.assertNotIn("fetch(", block)
        self.assertIn("state.dagFilter.status", block)
        self.assertIn("state.dagFilter.kind", block)

    def test_no_external_resources_anywhere(self) -> None:
        for name, text in (("app.js", _js()), ("index.html", _html()),
                          ("style.css", _css())):
            self.assertIsNone(re.search(r"https?://", text),
                            f"{name} carries an external URL literal")
        # the SVG namespace is an XML identifier, not a fetched resource;
        # it must stay assembled so the invariant above remains provable
        js = _js()
        self.assertIn('"http:" + "//www.w3.org/2000/svg"', js)
        self.assertIn("createElementNS", js)
        self.assertNotIn('createElement("svg")', js)

    def test_no_react_flow_dependency_exists(self) -> None:
        repo = WEB_ROOT.parents[2]
        self.assertFalse((repo / "package.json").exists(),
                        "package.json appeared: revisit the React Flow gap")
        self.assertFalse((repo / "node_modules").exists())
        js = _js()
        self.assertIsNone(re.search(r"[Rr]eact[- ][Ff]low", js))

    # -- read-only by construction -----------------------------------------------

    def test_dag_view_is_read_only(self) -> None:
        block = dag_block(_js())
        for write_marker in ('method: "POST"', 'method: "PUT"',
                            'method: "PATCH"', 'method: "DELETE"',
                            "Idempotency-Key", "idemKey("):
            self.assertNotIn(write_marker, block,
                            "the DAG view must never write")
        # no drag/edit handlers: node positions are presentation-only and
        # are never sent anywhere
        for edit_marker in ("ondrag", "onpointerdown", "pointermove",
                           "contenteditable", "draggable"):
            self.assertNotIn(edit_marker, block)

    def test_readonly_and_acyclic_semantics_are_visible(self) -> None:
        block = dag_block(_js())
        self.assertIn("no edge edits", block)
        self.assertIn("acyclic", block)
        self.assertIn("cycle_nodes", block)
        self.assertIn("edge_semantics", block)

    # -- facts drive the picture -------------------------------------------------

    def test_status_role_and_blockers_render_from_backend_fields(self) -> None:
        block = dag_block(_js())
        for field in ("n.status", "n.kind", "n.assignees",
                     "unmet_dependencies", "blocking_path",
                     "blocked_by_dependencies", "status_blocked",
                     "blocking_path_truncated"):
            self.assertIn(field, block,
                        f"DAG rendering ignores backend field {field!r}")
        # node colours reuse the shared honest status taxonomy
        self.assertIn('statusKind(n.status)', block)
        self.assertIn('"k-" + statusKind(n.status)', _js())
        self.assertIn("unmet dependencies", block)
        self.assertIn("blocking path", block.lower())

    def test_big_graph_affordances(self) -> None:
        block = dag_block(_js())
        self.assertIn("selectOf(TASK_STATUSES", block)
        self.assertIn("selectOf(TASK_KINDS", block)
        self.assertIn("Locate", block)
        self.assertIn("state.dagOffset", block)
        self.assertIn("has_more", block)
        self.assertIn("edges_outside_page", block)
        # layering uses the server-computed depth, never a client-side guess
        self.assertIn("n.depth", _js())

    def test_css_styles_dag_nodes_per_status(self) -> None:
        css = _css()
        for selector in (".dagwrap", ".dag-node rect.k-ok",
                        ".dag-node rect.k-warn", ".dag-node rect.k-bad",
                        ".dag-edge.e-unmet", ".dag-focus rect"):
            self.assertIn(selector, css)

    # -- served shell ------------------------------------------------------------

    def test_served_shell_contains_dag_tab(self) -> None:
        if not WEB_AVAILABLE:  # pragma: no cover
            self.skipTest("web stack unavailable")
        client = TestClient(create_app(static_root=WEB_ROOT))
        response = client.get("/static/index.html")  # public shell
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-view="dag"', response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
