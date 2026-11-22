"""M6-04 Task Brief template & preference contract tests.

Pinned contracts (M0 review 9.6 M6-04 row: "Task Brief 模板、Verifier/路由
显示偏好、项目覆盖；配置版本和导入校验 / 模板不能绕过 Ownership、命令白名单
或审批；配置错误可回退"):

* strict versioned parsing — only ``schema_version == 1``; unknown fields,
  bad ids, malformed braces, duplicate template ids, out-of-range values, and
  closed-vocabulary violations are refused before anything is applied;
* privilege fields refused — any key whose normalized segment hits a
  forbidden token (owner/allow/command/exec/approv/policy/risk/assurance/
  local_first/...) raises ``ForbiddenFieldError`` with reason code
  ``FORBIDDEN_PRIVILEGE_FIELD`` at any depth; the rendered brief is plain
  text and values are inert (no braces, no control chars);
* project overrides — leaf-wise preference merge, template replace-by-id plus
  append; a default template id that names nothing after resolution is a
  configuration error;
* rollback — append-only versioned store with atomic ACTIVE pointer: refused
  imports provably leave the directory unchanged, re-importing the active
  bytes is idempotent, rollback re-points after digest re-verification, and
  tampered history fails closed;
* determinism — identical documents give identical canonical payload digests
  and identical stored file bytes across stores.

All stores live under ``tempfile.TemporaryDirectory``; nothing here touches
the repository runtime directories or the network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from teamcraft import templates
from teamcraft.errors import NotFoundError, StorageError, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]


def minimal_template(template_id="std-brief", **overrides):
    doc = {
        "id": template_id,
        "name": "Standard Task Brief",
        "sections": [
            {"heading": "Objective", "body": "{{objective}}"},
            {"heading": "Constraints",
             "body": "Stay inside {{scope}}; report to {{reviewer}}."},
        ],
    }
    doc.update(overrides)
    return doc


def minimal_document(**overrides):
    doc = {
        "schema_version": 1,
        "templates": [minimal_template()],
        "preferences": {"verifier": {"sort": "occurred_at"}},
    }
    doc.update(overrides)
    return doc


class ParseTemplateTests(unittest.TestCase):
    def test_valid_template_collects_placeholders(self):
        template = templates.parse_template(minimal_template())
        self.assertEqual(template.id, "std-brief")
        self.assertEqual(len(template.sections), 2)
        self.assertEqual(template.placeholders,
                         frozenset({"objective", "scope", "reviewer"}))

    def test_unknown_field_refused(self):
        with self.assertRaisesRegex(ValidationError, "non-allowlisted"):
            templates.parse_template(minimal_template(extra="x"))

    def test_bad_id_refused(self):
        for bad in ("", "  ", "Upper", "with space", "x" * 65, "_lead"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    templates.parse_template(minimal_template(id=bad))

    def test_empty_sections_refused(self):
        with self.assertRaises(ValidationError):
            templates.parse_template(minimal_template(sections=[]))

    def test_malformed_braces_refused(self):
        for body in ("fix {{ thing", "fix {thing}", "run {{a{{b}}}}",
                    " lone } brace"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValidationError, "brace"):
                    templates.parse_template(
                        {"id": "t", "name": "T",
                         "sections": [{"heading": "H", "body": body}]})

    def test_body_size_bound(self):
        with self.assertRaisesRegex(ValidationError, "exceeds"):
            templates.parse_template(
                {"id": "t", "name": "T",
                 "sections": [{"heading": "H", "body": "x" * 8192}]})


class RenderBriefTests(unittest.TestCase):
    def test_render_fills_and_keeps_section_order(self):
        template = templates.parse_template(minimal_template())
        text = templates.render_brief(template, {
            "objective": "Fix the login bug",
            "scope": "teamcraft/templates.py",
            "reviewer": "the verifier",
        })
        self.assertIn("## Objective", text)
        self.assertIn("## Constraints", text)
        self.assertLess(text.index("## Objective"), text.index("## Constraints"))
        self.assertIn("Fix the login bug", text)
        self.assertNotIn("{{", text)
        self.assertIsInstance(text, str)

    def test_missing_or_extra_values_refused(self):
        template = templates.parse_template(minimal_template())
        with self.assertRaisesRegex(ValidationError, "missing"):
            templates.render_brief(template, {"objective": "x", "scope": "y"})
        with self.assertRaisesRegex(ValidationError, "undeclared"):
            templates.render_brief(template, {
                "objective": "x", "scope": "y", "reviewer": "z", "extra": "e"})

    def test_values_are_inert(self):
        template = templates.parse_template(minimal_template())
        base = {"objective": "x", "scope": "y", "reviewer": "z"}
        for bad_value in ("{{objective}}", "}}", "line\x00break",
                          "tab\tok\nbut \x07 no"):
            with self.subTest(bad_value=repr(bad_value)):
                with self.assertRaises(ValidationError):
                    templates.render_brief(template, {**base,
                                                     "objective": bad_value})

    def test_render_size_bound(self):
        template = templates.parse_template({
            "id": "big", "name": "Big",
            "sections": [{"heading": f"S{i}", "body": f"{{{{v{i}}}}}"}
                        for i in range(9)],
        })
        values = {f"v{i}": "x" * 8192 for i in range(9)}
        with self.assertRaisesRegex(ValidationError, "exceeds"):
            templates.render_brief(template, values)


class ForbiddenFieldTests(unittest.TestCase):
    """Templates/preferences may never carry enforcement configuration."""

    FORBIDDEN_PLACEMENTS = [
        ("templates[0].owner",
         {"schema_version": 1,
          "templates": [minimal_template(owner="someone")]}),
        ("templates[0].allowed_commands nested in section",
         {"schema_version": 1,
          "templates": [{"id": "t", "name": "T", "sections": [
              {"heading": "H", "body": "b",
               "allowed_commands": ["rm -rf /"]}]}]}),
        ("preferences.routing.local_first",
         {"schema_version": 1,
          "preferences": {"routing": {"local_first": False}}}),
        ("preferences.verifier.approvals",
         {"schema_version": 1,
          "preferences": {"verifier": {"auto_approve": True}}}),
        ("preferences.required_capabilities",
         {"schema_version": 1,
          "preferences": {"required_capabilities": ["herdr"]}}),
        ("top-level policy",
         {"schema_version": 1, "policy": {"high_risk_min_assurance": 0}}),
        ("preferences.brief.command_allowlist",
         {"schema_version": 1,
          "preferences": {"brief": {"command_allowlist": ["pytest"]}}}),
    ]

    def test_forbidden_fields_refused_with_reason_code(self):
        for where, doc in self.FORBIDDEN_PLACEMENTS:
            with self.subTest(where=where):
                with self.assertRaises(templates.ForbiddenFieldError) as ctx:
                    templates.parse_document(doc)
                self.assertEqual(ctx.exception.reason_code,
                                 "FORBIDDEN_PRIVILEGE_FIELD")
                # The offending value must never be echoed back.
                self.assertNotIn("rm -rf", str(ctx.exception))

    def test_legitimate_display_vocabulary_is_not_over_redacted(self):
        doc = minimal_document(preferences={
            "verifier": {"show_profile": False, "show_exit_code": False,
                        "sort": "profile", "max_rows": 100},
            "routing": {"show_reason_code": False, "show_selection": True,
                       "show_cost": True, "show_locality": False},
            "brief": {"default_template_id": "std-brief",
                     "max_preview_chars": 500},
        })
        parsed = templates.parse_document(doc)
        self.assertTrue(parsed.preferences.routing.show_cost)
        self.assertEqual(parsed.preferences.verifier.max_rows, 100)
        self.assertEqual(parsed.preferences.brief.default_template_id,
                         "std-brief")

    def test_brief_text_may_mention_commands_as_data(self):
        # The word "run" or "pytest" inside VALUES (brief prose) is data, not
        # configuration: only KEY segments are privilege-scanned.
        template = templates.parse_template({
            "id": "t", "name": "T",
            "sections": [{"heading": "How",
                         "body": "Run pytest and report results."}],
        })
        text = templates.render_brief(template, {})
        self.assertIn("Run pytest", text)


class PreferencesTests(unittest.TestCase):
    def test_defaults(self):
        prefs = templates.parse_preferences(None)
        self.assertEqual(prefs.verifier.sort, "seq")
        self.assertEqual(prefs.verifier.max_rows, 50)
        self.assertTrue(prefs.routing.show_reason_code)
        self.assertFalse(prefs.routing.show_cost)
        self.assertIsNone(prefs.brief.default_template_id)

    def test_closed_vocabularies_and_bounds(self):
        with self.assertRaisesRegex(ValidationError, "sort"):
            templates.parse_preferences({"verifier": {"sort": "random"}})
        with self.assertRaisesRegex(ValidationError, "max_rows"):
            templates.parse_preferences({"verifier": {"max_rows": 0}})
        with self.assertRaisesRegex(ValidationError, "max_rows"):
            templates.parse_preferences({"verifier": {"max_rows": 501}})
        with self.assertRaisesRegex(ValidationError, "max_preview_chars"):
            templates.parse_preferences({"brief": {"max_preview_chars": 4097}})
        with self.assertRaisesRegex(ValidationError, "non-allowlisted"):
            templates.parse_preferences({"verifier": {"theme_hack": True}})
        with self.assertRaisesRegex(ValidationError, "non-allowlisted"):
            templates.parse_preferences({"editor": {}})

    def test_bad_types_refused(self):
        for raw in ({"verifier": 0}, {"verifier": "seq"}, {"routing": []}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    templates.parse_preferences(raw)


class DocumentTests(unittest.TestCase):
    def test_schema_version_pinned(self):
        for bad in (None, 0, 2, "1", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    templates.parse_document(
                        minimal_document(schema_version=bad))

    def test_duplicate_template_ids_refused(self):
        with self.assertRaisesRegex(ValidationError, "duplicate ids"):
            templates.parse_document(minimal_document(
                templates=[minimal_template(), minimal_template()]))

    def test_digest_is_deterministic_across_equal_documents(self):
        a = templates.parse_document(minimal_document())
        b = templates.parse_document(minimal_document())
        self.assertEqual(a.doc_digest, b.doc_digest)
        other = templates.parse_document(minimal_document(
            preferences={"verifier": {"sort": "seq"}}))
        self.assertNotEqual(a.doc_digest, other.doc_digest)


class ResolveTests(unittest.TestCase):
    GLOBAL_DOC = {
        "schema_version": 1,
        "templates": [
            minimal_template("std-brief"),
            minimal_template("report", name="Report Brief",
                           sections=[{"heading": "Findings",
                                    "body": "{{findings}}"}]),
        ],
        "preferences": {
            "verifier": {"sort": "seq", "max_rows": 40},
            "routing": {"show_cost": False},
            "brief": {"default_template_id": "std-brief"},
        },
    }

    def test_project_override_is_leaf_wise_and_template_replace_by_id(self):
        project = {
            "schema_version": 1,
            "templates": [
                minimal_template("std-brief", name="Project Brief"),
                minimal_template("extra", name="Extra Brief",
                               sections=[{"heading": "X", "body": "{{v}}"}]),
            ],
            "preferences": {"routing": {"show_cost": True}},
        }
        effective = templates.resolve(self.GLOBAL_DOC, project)
        # preferences: project leaf wins, other global leaves survive
        self.assertTrue(effective.preferences.routing.show_cost)
        self.assertEqual(effective.preferences.verifier.max_rows, 40)
        self.assertEqual(effective.preferences.verifier.sort, "seq")
        # templates: same id replaced in place; new id appended; untouched
        # global template survives
        ids = [t.id for t in effective.templates]
        self.assertEqual(ids, ["std-brief", "report", "extra"])
        self.assertEqual(effective.template_index()["std-brief"].name,
                         "Project Brief")
        self.assertEqual(effective.template_index()["report"].name,
                         "Report Brief")

    def test_orphan_default_template_id_refused(self):
        project = {
            "schema_version": 1,
            "preferences": {"brief": {"default_template_id": "ghost"}},
        }
        # The global layer's default "std-brief" survives via merge (project
        # did not set that leaf), but naming a ghost explicitly fails.
        with self.assertRaisesRegex(ValidationError, "ghost"):
            templates.resolve(self.GLOBAL_DOC, project)

    def test_invalid_project_layer_raises_without_partial_result(self):
        broken = {"schema_version": 1,
                  "preferences": {"verifier": {"sort": "bogus"}}}
        with self.assertRaises(ValidationError):
            templates.resolve(self.GLOBAL_DOC, broken)

    def test_resolve_empty_layers_defaults(self):
        effective = templates.resolve()
        self.assertEqual(effective.templates, ())
        self.assertIsNone(effective.default_template())
        self.assertEqual(effective.preferences.verifier.max_rows, 50)


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "config-store"
        self.store = templates.ConfigStore(self.root)

    def snapshot_dir(self):
        return sorted(
            (str(p.relative_to(self.root)), p.read_bytes())
            for p in self.root.rglob("*") if p.is_file()
        )

    def test_import_creates_versions_and_active_pointer(self):
        v1 = self.store.import_document(minimal_document(),
                                       imported_at="2026-11-21T00:00:00Z")
        self.assertEqual(v1.version, 1)
        self.assertEqual(self.store.active_version(), 1)
        doc = self.store.active_document()
        self.assertEqual(doc["templates"][0]["id"], "std-brief")
        self.assertEqual(self.store.active().imported_at,
                         "2026-11-21T00:00:00Z")

    def test_idempotent_reimport_same_digest(self):
        v1 = self.store.import_document(minimal_document())
        v1b = self.store.import_document(minimal_document())
        self.assertEqual(v1.version, v1b.version)
        self.assertEqual(v1.doc_digest, v1b.doc_digest)
        self.assertEqual(self.store.versions(), (1,))

    def test_refused_import_leaves_store_unchanged(self):
        good = minimal_document()
        self.store.import_document(good)
        before = self.snapshot_dir()
        bad_documents = [
            minimal_document(unknown_top="x"),                      # unknown key
            minimal_document(schema_version=2),                     # bad version
            {"schema_version": 1,
             "preferences": {"verifier": {"approval_mode": "auto"}}},  # privilege
            minimal_document(templates=[minimal_template(),
                                      minimal_template()]),          # dup ids
        ]
        for bad in bad_documents:
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.store.import_document(bad)
                self.assertEqual(self.snapshot_dir(), before)
                self.assertEqual(self.store.active_version(), 1)

    def test_rollback_restores_previous_document(self):
        v1 = self.store.import_document(minimal_document())
        second = minimal_document(
            preferences={"verifier": {"sort": "profile"}})
        v2 = self.store.import_document(second)
        self.assertNotEqual(v1.doc_digest, v2.doc_digest)
        rolled = self.store.rollback()  # default: previous
        self.assertEqual(rolled.version, 1)
        self.assertEqual(rolled.doc_digest, v1.doc_digest)
        self.assertEqual(self.store.active_version(), 1)
        # history is append-only: v2 file still present and readable
        self.assertEqual(self.store.versions(), (1, 2))
        self.assertEqual(self.store.document_at(2)["preferences"]["verifier"]
                         ["sort"], "profile")

    def test_rollback_to_specific_and_no_history_errors(self):
        v1 = self.store.import_document(minimal_document())
        self.store.import_document(minimal_document(
            preferences={"verifier": {"sort": "profile"}}))
        self.store.import_document(minimal_document(
            preferences={"verifier": {"sort": "seq"},
                        "routing": {"show_cost": True}}))
        rolled = self.store.rollback(1)
        self.assertEqual(rolled.version, 1)
        self.assertEqual(self.store.active_document()["preferences"]
                         ["routing"]["show_cost"], False)
        with self.assertRaises(ValidationError):
            self.store.rollback(0)
        with self.assertRaises(NotFoundError):
            self.store.rollback(99)

    def test_fresh_store_rollback_and_missing_active(self):
        self.assertIsNone(self.store.active_version())
        self.assertIsNone(self.store.active_document())
        self.assertIsNone(self.store.active())
        with self.assertRaises(NotFoundError):
            self.store.rollback()

    def test_tampered_history_fails_closed(self):
        self.store.import_document(minimal_document())
        self.store.import_document(minimal_document(
            preferences={"verifier": {"sort": "profile"}}))
        victim = self.store._version_path(1)
        payload = json.loads(victim.read_text(encoding="utf-8"))
        # hostile edit: smuggle an approval switch AND break the digest
        payload["document"]["preferences"]["approvals"] = {"auto": True}
        victim.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(StorageError):
            self.store.document_at(1)
        with self.assertRaises(StorageError):
            self.store.rollback()  # target v1 fails digest verification
        # and the tampered document cannot even be re-imported
        with self.assertRaises(templates.ForbiddenFieldError):
            self.store.import_document(payload["document"])

    def test_import_of_document_with_unknown_default_refused(self):
        # default template id must exist in the merged effective view
        with self.assertRaisesRegex(ValidationError, "ghost"):
            self.store.import_document(minimal_document(
                preferences={"brief": {"default_template_id": "ghost"}}))
        self.assertIsNone(self.store.active_version())

    def test_imported_at_validation(self):
        with self.assertRaisesRegex(ValidationError, "imported_at"):
            self.store.import_document(minimal_document(),
                                     imported_at="/etc/passwd")
        with self.assertRaisesRegex(ValidationError, "imported_at"):
            self.store.import_document(minimal_document(), imported_at=123)
        stored = self.store.import_document(minimal_document(),
                                           imported_at="2026-11-21T09:30:00Z")
        self.assertEqual(stored.imported_at, "2026-11-21T09:30:00Z")

    def test_deterministic_stored_bytes_across_stores(self):
        with tempfile.TemporaryDirectory() as other_root:
            store_b = templates.ConfigStore(Path(other_root) / "store")
            doc = minimal_document()
            v_a = self.store.import_document(doc)
            v_b = store_b.import_document(doc)
            self.assertEqual(v_a.doc_digest, v_b.doc_digest)
            self.assertEqual(self.store._version_path(1).read_bytes(),
                           store_b._version_path(1).read_bytes())

    def test_store_root_must_not_be_a_file(self):
        clash = self.root.parent / "clash"
        clash.write_text("x", encoding="utf-8")
        with self.assertRaises(Exception):
            templates.ConfigStore(clash)


class RenderIntoBriefBoundaryTests(unittest.TestCase):
    """The rendered output is exactly what M2-03 accepts as brief content."""

    def test_rendered_text_is_valid_brief_content_shape(self):
        template = templates.parse_template(minimal_template())
        text = templates.render_brief(template, {
            "objective": "Ship M6-04", "scope": "templates.py",
            "reviewer": "CEO"})
        # briefs._content contract (without a DB): non-empty text under
        # 256 KiB — the ceilings in this module stay strictly inside it.
        self.assertTrue(text.strip())
        self.assertLessEqual(len(text.encode("utf-8")), 256 * 1024)
        self.assertLessEqual(templates.MAX_BRIEF_BYTES, 256 * 1024)


def snapshot_files(root: Path):
    """(relative path, bytes) for every file under ``root``, sorted."""

    return sorted(
        (str(p.relative_to(root)), p.read_bytes())
        for p in root.rglob("*") if p.is_file()
    )


class OperatorCliTests(unittest.TestCase):
    """In-process tests of ``main(argv)`` — the real CLI dispatch path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store_dir = self.root / "store"
        self.store = str(self.store_dir)
        self.doc_path = self.root / "doc.json"
        self.doc_path.write_text(json.dumps(minimal_document()),
                                encoding="utf-8")
        self.doc2_path = self.root / "doc2.json"
        self.doc2_path.write_text(json.dumps(minimal_document(
            preferences={"verifier": {"sort": "profile"}})), encoding="utf-8")
        self.values_path = self.root / "values.json"
        self.values_path.write_text(json.dumps({
            "objective": "Fix login", "scope": "src/", "reviewer": "CEO"}),
            encoding="utf-8")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = templates.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # -- import / list / show ---------------------------------------------

    def test_import_then_list_and_show(self):
        code, out, _ = self.run_main("import", str(self.doc_path),
                                    "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("version 1", out)
        self.assertRegex(out, r"[0-9a-f]{64}")
        code, out, _ = self.run_main("import", str(self.doc2_path),
                                    "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("version 2", out)

        code, out, _ = self.run_main("list", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("v1", out)
        self.assertIn("v2", out)
        active_lines = [line for line in out.splitlines() if "ACTIVE" in line]
        self.assertEqual(len(active_lines), 1)
        self.assertIn("v2", active_lines[0])

        code, out, _ = self.run_main("show", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("version: 2", out)
        document = json.loads(out[out.index("{"):])
        self.assertEqual(document["templates"][0]["id"], "std-brief")
        self.assertEqual(document["preferences"]["verifier"]["sort"],
                         "profile")
        code, out, _ = self.run_main("show", "1", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("version: 1", out)

    def test_import_refused_leaves_store_unchanged(self):
        self.run_main("import", str(self.doc_path), "--store", self.store)
        before = snapshot_files(self.store_dir)
        bad_doc = self.root / "bad.json"
        bad_doc.write_text(json.dumps(
            minimal_document(unknown_top="x")), encoding="utf-8")
        code, _, err = self.run_main("import", str(bad_doc),
                                    "--store", self.store)
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertEqual(snapshot_files(self.store_dir), before)

        hostile = self.root / "hostile.json"
        hostile.write_text(json.dumps({"schema_version": 1, "templates": [
            minimal_template(owner="someone")]}), encoding="utf-8")
        code, _, err = self.run_main("import", str(hostile),
                                    "--store", self.store)
        self.assertEqual(code, 1)
        self.assertIn("FORBIDDEN_PRIVILEGE_FIELD", err)
        self.assertEqual(snapshot_files(self.store_dir), before)

    def test_import_idempotent_when_identical_to_active(self):
        self.run_main("import", str(self.doc_path), "--store", self.store)
        code, out, _ = self.run_main("import", str(self.doc_path),
                                    "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(templates.ConfigStore(self.store_dir).versions(),
                         (1,))

    def test_import_records_imported_at_and_rejects_bad_stamp(self):
        code, out, _ = self.run_main("import", str(self.doc_path),
                                    "--store", self.store,
                                    "--imported-at", "2026-11-22T10:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("2026-11-22T10:00:00Z", out)
        code, out, _ = self.run_main("list", "--store", self.store)
        self.assertIn("2026-11-22T10:00:00Z", out)
        code, _, err = self.run_main("import", str(self.doc_path),
                                    "--store", self.store,
                                    "--imported-at", "/etc/passwd")
        self.assertEqual(code, 1)
        self.assertIn("imported_at", err)
        # the refusal did not add a version
        self.assertEqual(templates.ConfigStore(self.store_dir).versions(),
                         (1,))

    # -- preview ----------------------------------------------------------

    def test_preview_reports_effective_result_and_writes_nothing(self):
        self.run_main("import", str(self.doc_path), "--store", self.store)
        before = snapshot_files(self.store_dir)
        code, out, _ = self.run_main("preview", str(self.doc2_path),
                                    "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("would change the effective config: yes", out)
        self.assertIn("effective digest after import", out)
        self.assertEqual(snapshot_files(self.store_dir), before)

        code, out, _ = self.run_main("preview", str(self.doc_path),
                                    "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("would change the effective config: no", out)
        self.assertEqual(snapshot_files(self.store_dir), before)

    def test_preview_refusals_and_missing_input(self):
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, _, err = self.run_main("preview", str(bad), "--store",
                                    self.store)
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)

        hostile = self.root / "hostile.json"
        hostile.write_text(json.dumps({"schema_version": 1, "preferences": {
            "routing": {"local_first": False}}}), encoding="utf-8")
        code, _, err = self.run_main("preview", str(hostile), "--store",
                                    self.store)
        self.assertEqual(code, 1)
        self.assertIn("FORBIDDEN_PRIVILEGE_FIELD", err)

        code, _, err = self.run_main("preview", str(self.root / "ghost.json"),
                                    "--store", self.store)
        self.assertEqual(code, 4)
        self.assertIn("not found", err)
        self.assertFalse(self.store_dir.exists())

    # -- select / rollback --------------------------------------------------

    def test_select_and_roll_back_across_versions(self):
        self.run_main("import", str(self.doc_path), "--store", self.store)
        self.run_main("import", str(self.doc2_path), "--store", self.store)
        store = templates.ConfigStore(self.store_dir)

        code, out, _ = self.run_main("select", "1", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("activated version 1", out)
        self.assertEqual(store.active_version(), 1)

        code, out, _ = self.run_main("rollback", "--store", self.store)
        self.assertEqual(code, 4)  # v1 is the floor: nothing earlier

        code, _, _ = self.run_main("select", "2", "--store", self.store)
        self.assertEqual(code, 0)
        code, out, _ = self.run_main("rollback", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("rolled back: activated version 1", out)
        self.assertEqual(store.active_version(), 1)

        code, out, _ = self.run_main("rollback", "--to", "2", "--store",
                                    self.store)
        self.assertEqual(code, 0)
        self.assertIn("activated version 2", out)
        self.assertEqual(store.active_version(), 2)

        code, _, _ = self.run_main("select", "77", "--store", self.store)
        self.assertEqual(code, 4)
        code, _, err = self.run_main("select", "0", "--store", self.store)
        self.assertEqual(code, 1)
        self.assertIn("positive integer", err)

    def test_readonly_commands_on_absent_store(self):
        ghost_store = str(self.root / "never-created")
        code, out, _ = self.run_main("list", "--store", ghost_store)
        self.assertEqual(code, 0)
        self.assertIn("nothing stored", out)
        self.assertFalse((self.root / "never-created").exists())
        for argv in (("show",), ("select", "1"), ("rollback",),
                     ("render", "std-brief")):
            with self.subTest(argv=argv):
                code, _, err = self.run_main(*argv, "--store", ghost_store)
                self.assertEqual(code, 4)
                self.assertIn("error:", err)

    # -- render ---------------------------------------------------------

    def test_render_is_explicit_and_inert(self):
        self.run_main("import", str(self.doc_path), "--store", self.store)
        code, out, _ = self.run_main("render", "std-brief", "--values",
                                    str(self.values_path), "--store",
                                    self.store)
        self.assertEqual(code, 0)
        self.assertIn("## Objective", out)
        self.assertIn("Fix login", out)
        self.assertNotIn("{{", out)

        code, out, _ = self.run_main(
            "render", "std-brief", "--values", str(self.values_path),
            "--set", "objective=Overridden objective", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("Overridden objective", out)
        self.assertNotIn("Fix login", out)

        code, _, err = self.run_main("render", "std-brief", "--store",
                                    self.store)
        self.assertEqual(code, 1)
        self.assertIn("missing placeholders", err)

        code, _, err = self.run_main("render", "ghost-id", "--values",
                                    str(self.values_path), "--store", self.store)
        self.assertEqual(code, 4)
        self.assertIn("std-brief", err)  # lists what IS available

        code, _, err = self.run_main("render", "std-brief", "--set",
                                    "novalue", "--values",
                                    str(self.values_path), "--store", self.store)
        self.assertEqual(code, 1)
        self.assertIn("KEY=VALUE", err)

    def test_render_does_not_reach_into_project_store_by_default(self):
        # With TEAMCRAFT_PROJECT_ROOT pointing at an empty tmp dir the default
        # store has nothing; render must fail closed instead of touching cwd.
        self.run_main("import", str(self.doc_path), "--store", self.store)
        out, err = io.StringIO(), io.StringIO()
        environ = {k: v for k, v in os.environ.items()
                   if k != "TEAMCRAFT_PROJECT_ROOT"}
        environ["TEAMCRAFT_PROJECT_ROOT"] = str(self.root / "empty-project")
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                unittest.mock.patch.dict(os.environ, environ, clear=True):
            code = templates.main(["render", "std-brief"])
        self.assertEqual(code, 4)

class ModuleCliEntryTests(unittest.TestCase):
    """``python -m teamcraft.templates`` over a real subprocess boundary."""

    def run_dash_m(self, *argv, cwd=None, env_extra=None):
        env = {k: v for k, v in os.environ.items()
               if k != "TEAMCRAFT_PROJECT_ROOT"}
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "teamcraft.templates", *argv],
            cwd=str(cwd or REPO_ROOT), env=env, capture_output=True,
            text=True, timeout=120)

    def test_dash_m_help_documents_workflow_and_exit_codes(self):
        completed = self.run_dash_m("--help")
        self.assertEqual(completed.returncode, 0)
        for marker in ("preview", "import", "list", "show", "render",
                      "rollback", "select", "inert", "exit codes"):
            self.assertIn(marker, completed.stdout)

    def test_dash_m_default_store_lands_in_project_runtime_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            doc = project / "briefs.json"
            doc.write_text(json.dumps(minimal_document()), encoding="utf-8")
            completed = self.run_dash_m(
                "import", str(doc),
                env_extra={"TEAMCRAFT_PROJECT_ROOT": str(project)})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            store = project / ".teamcraft" / "templates"
            self.assertEqual((store / "ACTIVE").read_text(encoding="utf-8"),
                             "1")
            self.assertTrue((store / "versions" / "v000001.json").exists())

    def test_dash_m_usage_error_exit_two(self):
        completed = self.run_dash_m("frobnicate")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid choice", completed.stderr)

    def test_dash_m_refusal_exit_one_zero_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            doc = Path(tmp) / "hostile.json"
            doc.write_text(json.dumps({"schema_version": 1, "templates": [
                minimal_template(owner="someone")]}), encoding="utf-8")
            completed = self.run_dash_m("import", str(doc), "--store",
                                       str(store))
            self.assertEqual(completed.returncode, 1)
            self.assertIn("FORBIDDEN_PRIVILEGE_FIELD", completed.stderr)
            self.assertFalse((store / "ACTIVE").exists())
            self.assertEqual(
                [p.name for p in (store / "versions").iterdir()]
                if (store / "versions").exists() else [], [])

    def test_dash_m_full_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "store")
            doc = Path(tmp) / "doc.json"
            doc.write_text(json.dumps(minimal_document()), encoding="utf-8")
            doc2 = Path(tmp) / "doc2.json"
            doc2.write_text(json.dumps(minimal_document(
                preferences={"verifier": {"sort": "profile"}})),
                encoding="utf-8")
            values = Path(tmp) / "values.json"
            values.write_text(json.dumps({
                "objective": "Ship M6-04 CLI", "scope": "templates.py",
                "reviewer": "the-verifier"}), encoding="utf-8")
            expected_steps = [
                (["preview", str(doc), "--store", store], 0, "would change"),
                (["import", str(doc), "--store", store], 0, "version 1"),
                (["import", str(doc2), "--store", store], 0, "version 2"),
                (["list", "--store", store], 0, "ACTIVE"),
                (["show", "--store", store], 0, "version: 2"),
                (["rollback", "--store", store], 0, "activated version 1"),
                (["select", "2", "--store", store], 0, "activated version 2"),
                (["render", "std-brief", "--values", str(values),
                  "--store", store], 0, "Ship M6-04 CLI"),
            ]
            for argv, code, marker in expected_steps:
                with self.subTest(argv=argv[0]):
                    completed = self.run_dash_m(*argv)
                    self.assertEqual(completed.returncode, code,
                                   completed.stderr)
                    self.assertIn(marker, completed.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
