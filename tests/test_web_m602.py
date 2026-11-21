"""M6-02 Diff / verification / execution-timeline console contract tests.

Follows the M3-05/M5-05/M6-01 discipline — the console is checked by
construction, with contracts specific to the two new read-only evidence
pages:

* Diff and Timeline tabs exist exactly once without disturbing existing
  tabs and route to ``viewDiff`` / ``viewTimeline``;
* the pages call ONLY the authorized authenticated read routes
  (``/tasks/{id}/delivery``, ``/ship-evidence/{id}/diff``,
  ``/tasks/{id}/verifications``, ``/tasks/{id}/attempts``,
  ``/events?task_id=``, ``/tasks/{id}/approvals``, ``/attempts/{id}``)
  through the guarded ``api()`` helper — no direct ``fetch``, no write
  method, no Idempotency machinery, no external URLs anywhere;
* commit and evidence links are preserved: commit/parent/base SHAs,
  diff/evidence/verifier digests, event/command ids all render, and the
  diff is fetched through the exact ship-evidence id from the delivery
  payload; server-supplied PR URLs stay TEXT (no href is ever created
  from server data);
* display is bounded with clear indicators: character, per-line and
  row caps with visible truncation markers, and binary content is
  announced via the server's flags plus in-text binary self-reporting
  (never rendered as silent glyph soup);
* download capability is honestly stated: the server's only diff
  surface is the COMPLETE-JSON route (Core refuses oversized diffs
  fail-closed) and there is NO raw-byte/Content-Disposition endpoint —
  so the console never fakes a download (no blob URLs, no window.open).

Server-side ground truth pinned here (verified against the build):
teamcraft/api/query.py exposes the diff as a JSON field and nothing
else; grep of teamcraft/api for Content-Disposition/FileResponse shows
only console static assets.

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
                   "new Function", "localStorage", "insertAdjacentHTML",
                   # fake-download / navigation sinks: the server offers no
                   # raw-byte endpoint, so the console must not invent one
                   "createObjectURL", "window.open", "blob:", "srcdoc")

WRITE_MARKERS = ('method: "POST"', 'method: "PUT"', 'method: "PATCH"',
                'method: "DELETE"', "Idempotency-Key", "idemKey(")


def _js() -> str:
    return (WEB_ROOT / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (WEB_ROOT / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (WEB_ROOT / "style.css").read_text(encoding="utf-8")


def diff_block(js: str) -> str:
    # covers diffEvidenceNode / freshnessBadges / loadDiffInto /
    # deliveryAttemptCard / verificationCard / viewDiff
    start = js.index("function diffEvidenceNode(p)")
    end = js.index("function tlEntry(", start)
    return js[start:end]


def timeline_block(js: str) -> str:
    # covers tlEntry / timelineEntries / timelineTable / lazySessionsFor /
    # viewTimeline
    start = js.index("function tlEntry(")
    end = js.index('q("token-save")', start)
    return js[start:end]


class DiffConsoleM602Tests(unittest.TestCase):
    # -- shell wiring -----------------------------------------------------------

    def test_tabs_exist_once_without_disturbing_existing_tabs(self) -> None:
        html = _html()
        for view in ("overview", "tasks", "dag", "agents", "ceo", "events",
                    "wakes", "usage", "approvals", "verifications"):
            self.assertIn(f'data-view="{view}"', html)
        self.assertEqual(html.count('data-view="diff"'), 1)
        self.assertEqual(html.count('data-view="timeline"'), 1)

    def test_views_registered_and_defined_once(self) -> None:
        js = _js()
        self.assertIn("diff: viewDiff,", js)
        self.assertIn("timeline: viewTimeline,", js)
        self.assertEqual(js.count("async function viewDiff(m)"), 1)
        self.assertEqual(js.count("async function viewTimeline(m)"), 1)

    # -- route surface ----------------------------------------------------------

    def test_diff_view_calls_only_guarded_read_routes(self) -> None:
        block = diff_block(_js())
        for path in ('"/delivery?limit=100"', '"/verifications?limit=100"',
                    '"/ship-evidence/"', '"/diff"'):
            self.assertIn(path, block)
        self.assertNotIn("fetch(", block)
        for marker in WRITE_MARKERS:
            self.assertNotIn(marker, block,
                            "the diff evidence view must never write")

    def test_timeline_view_calls_only_guarded_read_routes(self) -> None:
        block = timeline_block(_js())
        for path in ('"/attempts?limit=100"', '"/events?task_id="',
                    '"/verifications?limit=100"', '"/approvals?limit=100"',
                    '"/attempts/"'):
            self.assertIn(path, block)
        self.assertNotIn("fetch(", block)
        for marker in WRITE_MARKERS:
            self.assertNotIn(marker, block,
                            "the timeline view must never write")

    def test_no_external_resources_anywhere(self) -> None:
        for name, text in (("app.js", _js()), ("index.html", _html()),
                          ("style.css", _css())):
            self.assertIsNone(re.search(r"https?://", text),
                            f"{name} carries an external URL literal")

    # -- safe rendering -----------------------------------------------------------

    def test_no_dangerous_sinks_and_no_href_from_server_data(self) -> None:
        js = _js()
        for sink in DANGEROUS_SINKS:
            self.assertNotIn(sink, js, f"sink {sink!r} appeared")
        # no anchor elements at all: server URLs (e.g. pr_url) render as
        # text, never as clickable navigation (no href attribute is ever
        # set, in el() opts or via setAttribute)
        self.assertNotIn('el("a"', js)
        self.assertNotIn('"href":', js)
        self.assertNotIn("'href':", js)
        self.assertNotIn('setAttribute("href"', js)
        self.assertNotIn("onclick=", _html())

    def test_all_evidence_values_attach_via_text_nodes(self) -> None:
        js = _js()
        # the shared text-only helpers are the only render path
        self.assertIn('el("code", { text: fmt(p.commit_sha) })', js)
        self.assertIn("textContent", js)
        # diff bodies render into a plain <pre> of text lines
        self.assertIn('el("pre", { class: "diff" }', js)

    # -- commit and evidence links -----------------------------------------------

    def test_commit_and_evidence_links_are_preserved(self) -> None:
        block = diff_block(_js())
        for field in ("p.commit_sha", "p.base_sha", "p.diff_digest",
                     "commit.commit_sha", "commit.parent_sha",
                     "commit.evidence_digest", "commit.event_id",
                     "commit.command_id", "ship.evidence_digest",
                     "ship.verifier_digest", "diff_relpath"):
            self.assertIn(field, block,
                         f"evidence link field {field!r} dropped")
        # the diff is fetched through the exact ship-evidence id
        self.assertIn("loadDiffInto(diffBox, ship.id)", block)
        self.assertIn('"/ship-evidence/" + encodeURIComponent(shipEvidenceId)'
                     ' + "/diff"', _js())
        # cross-page navigation (timeline <-> diff) is by id, no writes
        self.assertIn('setView("diff")', _js())

    def test_server_urls_stay_text_not_hrefs(self) -> None:
        block = diff_block(_js())
        self.assertIn("pr_url", block)
        self.assertIn("text only", block)

    # -- bounded display with clear indicators ------------------------------------

    def test_display_bounds_are_declared_and_applied(self) -> None:
        js = _js()
        self.assertRegex(js, r"const DIFF_RENDER_LIMIT = \d+;")
        self.assertRegex(js, r"const DIFF_LINE_LIMIT = \d+;")
        self.assertRegex(js, r"const TIMELINE_RENDER_LIMIT = \d+;")
        self.assertIn("display truncated at ", js)
        self.assertIn("[line clipped]", js)
        self.assertIn("display bound", js)
        # characters beyond the bound never silently disappear
        self.assertIn("COMPLETE", js)

    def test_binary_content_is_announced_never_silent(self) -> None:
        js = _js()
        self.assertIn("function looksBinaryDiff(text)", js)
        self.assertIn('"\\uFFFD"', js)
        self.assertIn("GIT binary patch", js)
        self.assertIn("Binary files ", js)
        # the artifact flags from the query layer are honoured verbatim
        self.assertIn("art.binary", js)
        self.assertIn("art.preview_truncated", js)
        self.assertIn("binary artifact", js)
        self.assertIn("preview truncated by the API", js)

    # -- verified download honesty -------------------------------------------------

    def test_download_capability_is_stated_as_json_only(self) -> None:
        js = _js()
        # the server truth (query.py ships the diff as a JSON field; no
        # Content-Disposition surface exists in teamcraft/api)
        self.assertGreaterEqual(js.count("raw-byte download"), 2)
        self.assertIn("Content-Disposition", js)
        self.assertIn("no raw-byte download endpoint", js)

    def test_server_really_has_no_raw_download_route(self) -> None:
        api_root = WEB_ROOT.parent
        texts = []
        for name in ("query.py", "static.py", "stream.py", "app.py",
                    "serve.py"):
            texts.append((api_root / name).read_text(encoding="utf-8"))
        joined = "\n".join(texts)
        self.assertNotIn("Content-Disposition", joined,
                        "a raw download surface appeared: update the console "
                        "statements AND this contract")
        self.assertIn("def get_ship_evidence_diff", joined)

    # -- timeline order honesty -----------------------------------------------------

    def test_timeline_keeps_layers_and_order_authority_visible(self) -> None:
        js = _js()
        block = timeline_block(js)
        for layer in ('"attempt"', '"verification"', '"approval"', '"event"'):
            self.assertIn(layer, block)
        self.assertIn("ordering authority: durable event seq", block)
        self.assertIn("timestamp unknown", block)
        # sessions are a separate live-signal layer, loaded lazily
        self.assertIn("never implies Attempt finish", block)
        self.assertIn("lazySessionsFor", block)

    def test_shared_honest_status_taxonomy_is_reused(self) -> None:
        combined = diff_block(_js()) + timeline_block(_js())
        self.assertIn("statusKind(", combined)
        self.assertIn("emptyNote(", combined)
        self.assertIn("failInto(", combined)

    # -- css ------------------------------------------------------------------------

    def test_css_styles_diff_and_timeline(self) -> None:
        css = _css()
        for selector in ("pre.diff .d-add", "pre.diff .d-del",
                        "pre.diff .d-meta", "pre.diff .d-ctx", ".tlrow",
                        ".warn"):
            self.assertIn(selector, css)

    # -- served shell ---------------------------------------------------------------

    @unittest.skipUnless(WEB_AVAILABLE, "web test dependencies unavailable")
    def test_served_shell_contains_new_tabs(self) -> None:
        client = TestClient(create_app(static_root=str(WEB_ROOT)))
        shell = client.get("/static/index.html")
        self.assertEqual(shell.status_code, 200)
        self.assertIn('data-view="diff"', shell.text)
        self.assertIn('data-view="timeline"', shell.text)
        script = client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("async function viewDiff(m)", script.text)
        self.assertIn("async function viewTimeline(m)", script.text)


if __name__ == "__main__":
    unittest.main()
