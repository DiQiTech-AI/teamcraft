"""M5-05 usage/cost console contract tests (static + served shell).

Follows the M3-05 discipline: the console is checked by construction
(text-only rendering, no external resources, no new sinks) and by
behavioural string contracts specific to the Usage view:

* a Usage tab exists in the shell and routes to ``viewUsage``;
* the view only calls the authorized M5-05 query routes
  (``/projects/{id}/usage``, ``/projects/{id}/ceo/usage``,
  ``/tasks/{id}/usage``) through the guarded ``api()`` helper;
* filters are closed sets (ceo/worker/verifier, local/cloud) matching the
  core enumerations — free-text filters cannot be forged from this page;
* missing quantities render as ``unavailable`` and partial totals carry a
  visible partial badge — never a bare zero;
* failed attempts are explicitly labelled as included;
* the task detail view embeds a task-level usage section that degrades to
  "no usage recorded — unavailable, not zero".

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
                   "new Function", "localStorage")


class UsageConsoleM505Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        cls.js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        cls.css = (WEB_ROOT / "style.css").read_text(encoding="utf-8")

    # -- shell wiring ---------------------------------------------------------

    def test_usage_tab_exists_without_disturbing_existing_tabs(self) -> None:
        for view in ("overview", "tasks", "agents", "ceo", "events",
                     "wakes", "approvals", "verifications"):
            self.assertIn(f'data-view="{view}"', self.html)
        self.assertIn('data-view="usage"', self.html)
        self.assertEqual(self.html.count('data-view="usage"'), 1)

    def test_view_usage_registered_and_defined_once(self) -> None:
        self.assertIn("usage: viewUsage,", self.js)
        self.assertEqual(self.js.count("async function viewUsage(m)"), 1)

    # -- route surface ----------------------------------------------------------

    def test_usage_view_calls_only_guarded_local_api_routes(self) -> None:
        usage_block = self.js[self.js.index("async function viewUsage(m)"):]
        usage_block = usage_block[:usage_block.index("async function "
                                                    "viewApprovals")]
        for path in ('"/usage?limit=50"', '"/ceo/usage"'):
            self.assertIn(path, usage_block)
        self.assertIn('api("/tasks/" + enc + "/usage")', self.js)
        # every fetch still goes through API (relative /api/v1) only
        self.assertNotIn("fetch(", usage_block)
        self.assertIsNone(re.search(r"https?://", self.js))
        self.assertIsNone(re.search(r"https?://", self.html))
        self.assertIsNone(re.search(r"url\(\s*['\"]?https?://", self.css))

    # -- closed-set filters ------------------------------------------------------

    def test_filters_are_closed_sets_matching_core(self) -> None:
        from teamcraft import usage as usage_core

        match = re.search(r"const USAGE_ROLES = \[([^\]]+)\]", self.js)
        self.assertIsNotNone(match)
        roles = sorted(x.strip().strip('"') for x in match.group(1).split(","))
        self.assertEqual(roles, sorted(usage_core.USAGE_ROLES))
        match = re.search(r"const USAGE_LOCALITIES = \[([^\]]+)\]", self.js)
        self.assertIsNotNone(match)
        localities = sorted(x.strip().strip('"')
                           for x in match.group(1).split(","))
        self.assertEqual(localities, sorted(usage_core.USAGE_LOCALITIES))
        # filters flow through the guarded api() helper as query params
        self.assertIn('"&role=" + state.usageRole', self.js)
        self.assertIn('"&locality=" + state.usageLocality', self.js)

    # -- honesty labels -----------------------------------------------------------

    def test_missing_is_unavailable_never_zero_and_partial_is_visible(
            self) -> None:
        self.assertIn('"unavailable — not recorded (not zero)"', self.js)
        self.assertIn('"partial — some records unknown"', self.js)
        self.assertIn("unavailable, not zero", self.js)
        # amount text never prints null/undefined as a number
        self.assertIn('if (!d || d.completeness === "unavailable")', self.js)
        self.assertIn("never merge", self.js)  # mixed-currency refusal badge

    def test_failed_attempts_labelled_included(self) -> None:
        self.assertIn('"failed attempts included"', self.js)
        self.assertIn('badge(outcome + " — still counted", "bad")', self.js)
        # task detail: usage across attempts, failures included
        self.assertIn("failures included", self.js)

    def test_currency_and_precision_labels_rendered(self) -> None:
        self.assertIn("currency_label", self.js)
        self.assertIn('[" + r.currency_label + "]', self.js)
        self.assertIn("price versions", self.js)
        self.assertIn('price_version ? " @ " + r.price_version', self.js)
        # the three precision states are distinguishable badges
        self.assertIn('r.precision === "exact"', self.js)
        self.assertIn('r.precision === "estimated"', self.js)
        self.assertIn('"bad")', self.js)

    def test_trend_renderer_uses_the_shared_totals_shape(self) -> None:
        """M5-05 QA blocker regression (static side of the contract).

        The day-trend rows must be rendered through the same helpers as
        the grand totals, fed with each bucket's ``totals`` object — the
        old parallel cell renderer read fields the API never sent and
        printed ``undefined`` costs.
        """

        block = self.js[self.js.index("async function viewUsage(m)"):]
        block = block[:block.index("async function viewApprovals")]
        self.assertIn("const dt = day.totals || {};", block)
        self.assertIn('usageAmountText(dt, "input_tokens")', block)
        self.assertIn('usageAmountText(dt, "output_tokens")', block)
        self.assertIn("usageCostText(dt)", block)
        # the divergent shape/reader is gone from the view entirely
        self.assertNotIn("usageCostText(day)", block)
        self.assertNotIn("d.value", block)
        self.assertNotIn('completeness === "partial" ? " (partial)"', block)

    def test_overall_total_label_surfaced_with_matching_tone(self) -> None:
        """Label-inconsistency fix: the derived overall verdict is shown."""

        self.assertIn("total_label", self.js)
        self.assertIn("overall: ", self.js)
        self.assertIn("function totalLabelKind(label)", self.js)
        # the ok/warn/bad tones mirror the API's complete/partial/unavailable
        self.assertIn('indexOf("partial") === 0', self.js)
        self.assertIn('indexOf("unavailable") === 0', self.js)

    # -- safety posture preserved (M3-05 invariants) ----------------------------

    def test_no_new_dangerous_sinks_anywhere_in_console(self) -> None:
        for sink in DANGEROUS_SINKS:
            self.assertNotIn(sink, self.js, f"sink {sink!r} appeared")
        self.assertNotIn("onclick=", self.html)  # handlers via addEventListener
        self.assertNotIn("<script src=\"http", self.html)

    def test_ceo_wake_card_honest_labels(self) -> None:
        self.assertIn("CEO / Wake attention", self.js)
        self.assertIn("never double-counted", self.js)
        self.assertIn("no wakes recorded", self.js)

    # -- served shell -----------------------------------------------------------

    @unittest.skipUnless(WEB_AVAILABLE, "web test dependencies unavailable")
    def test_served_shell_exposes_usage_tab(self) -> None:
        client = TestClient(create_app(static_root=str(WEB_ROOT)))
        shell = client.get("/static/index.html")
        self.assertEqual(shell.status_code, 200)
        self.assertIn('data-view="usage"', shell.text)
        script = client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("function viewUsage", script.text)


if __name__ == "__main__":
    unittest.main()
