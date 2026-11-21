"""M6-03 sanitized diagnostics bundle contract tests.

All databases and exports live inside ``tempfile.TemporaryDirectory``; nothing
here touches the repository runtime directories, the network, or any Herdr
session.  Every export is scanned byte-for-byte for canary values.

Pinned contracts (M0 review 9.1 M6-03 row: "默认不含凭证和原始敏感日志；
用户预览导出清单；包可用于复现已覆盖故障" plus the task's hard edges):

* strict allowlist selection — unknown/duplicate/empty/non-sequence section
  selections are refused before any read; the default scope is the
  allowlist-minus-failures set and every ``policy.raw`` flag is False;
* no raw env / logs / credentials / source paths: an environment variable,
  event payloads, probe detail, and absolute paths seeded with canaries
  never appear in preview bytes or exported files; markers
  ``[REDACTED]``/``<path>`` appear instead where sanitized text was kept;
* evidenced failures only — a failure record without a 64-hex
  ``evidence_digest`` or with a non-allowlisted field is refused; detail is
  sanitized and hard-clipped with an honest ``detail_clipped`` flag;
* size bounds — per-section byte ceilings with tail-trim + ``truncated``,
  record/kind count caps, and an over-budget bundle refusal;
* deterministic manifest — identical inputs give identical canonical manifest
  bytes across builds and across two export runs, and ``manifest_digest``
  verifies against the canonical manifest-minus-digest;
* preview/export scope parity — the files on disk are exactly the preview's
  manifest plus one file per selected section, byte-identical, with digests
  matching the manifest entries; export fails-before-write when a target
  already exists;
* head-schema gate — a lagging database is refused with
  :class:`SchemaOutOfDateError` before any section is built.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import diagnostics, domain, events, storage
from teamcraft.errors import SchemaOutOfDateError, ValidationError

# --- canaries ---------------------------------------------------------------

GH_CANARY = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2          # token-shape secret
AWS_CANARY = "AKIA" + "ABCDEFGHIJKLMN12"                   # AWS access key id
KV_CANARY = "password=hunter2xyz"                         # key=value secret
POSIX_CANARY = "/Users/tester/private/topsecret/main.py"  # source path
HOME_CANARY = "~/workspace/secret_plan.txt"               # home path
WIN_CANARY = "C:\\Users\\tester\\secrets.env"             # Windows path
ENV_VAR_NAME = "TEAMCRAFT_M603_ENV_CANARY"
ENV_CANARY = "env-secret-value-9f8e7d6c"
PAYLOAD_CANARY = "PAYLOAD-ONLY-SENTINEL-42"
EVIDENCE = "ab" * 32


class FixedClock:
    """Deterministic injectable UTC clock (manifest parity needs one)."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: int = 60) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def all_bytes_of(preview: diagnostics.DiagnosticsPreview) -> bytes:
    blobs = [preview.manifest_text] + [text for _, text in preview.sections]
    return "".join(blobs).encode("utf-8")


def read_export(out: Path) -> dict[str, bytes]:
    return {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }


class DiagnosticsPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m603-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"
        report = storage.migrate_to_head(self.db)
        self.assertTrue(report.fresh)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc))
        # Set a real env var: the module must never read the environment, so
        # this value can only surface if it leaks through some raw-env path.
        os.environ[ENV_VAR_NAME] = ENV_CANARY
        self.addCleanup(os.environ.pop, ENV_VAR_NAME, None)

    # -- selection -----------------------------------------------------------

    def test_default_scope_is_allowlist_bounded_and_raw_free(self) -> None:
        events.append_event(
            self.conn, "task.failed",
            payload_json=json.dumps({"why": PAYLOAD_CANARY, "path": POSIX_CANARY}),
            clock=self.clock,
        )
        preview = diagnostics.build_preview(self.conn)
        self.assertEqual(preview.selected, diagnostics.DEFAULT_SECTIONS)
        self.assertEqual(preview.selected, ("version", "schema", "health", "events"))
        self.assertEqual(set(preview.selected) - set(diagnostics.SECTION_ALLOWLIST), set())
        policy = preview.manifest["policy"]
        self.assertEqual(policy["allowlist"], list(diagnostics.SECTION_ALLOWLIST))
        self.assertEqual(policy["default_sections"], list(diagnostics.DEFAULT_SECTIONS))
        self.assertTrue(policy["raw"])
        self.assertEqual(set(policy["raw"].values()), {False})
        blob = all_bytes_of(preview)
        for canary in (ENV_CANARY, PAYLOAD_CANARY, POSIX_CANARY, GH_CANARY,
                      KV_CANARY, WIN_CANARY, HOME_CANARY):
            self.assertNotIn(canary.encode(), blob)

    def test_selection_must_be_explicit_allowlist_and_unique(self) -> None:
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(self.conn, sections=("version", "nope"))
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(self.conn, sections=("version", "version"))
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(self.conn, sections=())
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(self.conn, sections="version")
        # Every allowlisted section is individually selectable.
        for name in diagnostics.SECTION_ALLOWLIST:
            kwargs: dict = {"sections": (name,)}
            if name == "failures":
                kwargs["failures"] = [{"kind": "verifier.test", "status": "fail",
                                     "evidence_digest": EVIDENCE}]
            preview = diagnostics.build_preview(self.conn, **kwargs)
            self.assertEqual(preview.selected, (name,))
            self.assertEqual([n for n, _ in preview.sections], [name])

    # -- version / schema ------------------------------------------------------

    def test_version_section_reports_versions(self) -> None:
        preview = diagnostics.build_preview(self.conn, sections=("version",))
        section = preview.section_payloads()["version"]
        self.assertEqual(section["app_version"], storage.APP_VERSION)
        self.assertEqual(section["build_schema_version"], storage.SCHEMA_VERSION)
        self.assertEqual(section["database_schema_version"], storage.SCHEMA_VERSION)
        self.assertIn("sqlite_version", section)

    def test_schema_section_counts_tables_without_internals(self) -> None:
        events.append_event(self.conn, "watch.process_started", clock=self.clock)
        preview = diagnostics.build_preview(self.conn, sections=("schema",))
        section = preview.section_payloads()["schema"]
        names = [t["name"] for t in section["tables"]]
        self.assertIn("events", names)
        self.assertIn("projects", names)
        self.assertIn("worktree_verifications", names)
        self.assertFalse([n for n in names if n.startswith("sqlite_")])
        counts = {t["name"]: t["row_count"] for t in section["tables"]}
        self.assertEqual(counts["events"], 1)
        self.assertFalse(section["truncated"])

    # -- events ----------------------------------------------------------------

    def test_events_summary_counts_and_excludes_payloads(self) -> None:
        events.append_event(
            self.conn, "task.failed",
            payload_json=json.dumps({"detail": GH_CANARY, "root": POSIX_CANARY}),
            clock=self.clock,
        )
        self.clock.advance()
        events.append_event(self.conn, "task.failed", clock=self.clock)
        self.clock.advance()
        events.append_event(self.conn, "watch.agent_active", clock=self.clock)
        preview = diagnostics.build_preview(self.conn, sections=("events",))
        section = preview.section_payloads()["events"]
        self.assertEqual(section["total_events"], 3)
        self.assertFalse(section["payloads_included"])
        by_kind = {k["kind"]: k for k in section["kinds"]}
        self.assertEqual(by_kind["task.failed"]["count"], 2)
        self.assertEqual(by_kind["task.failed"]["first_seq"], 1)
        self.assertEqual(by_kind["task.failed"]["last_seq"], 2)
        self.assertEqual(by_kind["watch.agent_active"]["count"], 1)
        self.assertNotIn(PAYLOAD_CANARY.encode(), all_bytes_of(preview))
        self.assertNotIn(GH_CANARY.encode(), all_bytes_of(preview))

    def test_events_project_filter(self) -> None:
        a = domain.create_project(self.conn, "a", "/repo/a", clock=self.clock).id
        b = domain.create_project(self.conn, "b", "/repo/b", clock=self.clock).id
        events.append_event(self.conn, "task.failed", project_id=a, clock=self.clock)
        events.append_event(self.conn, "task.failed", project_id=b, clock=self.clock)
        events.append_event(self.conn, "task.done", project_id=b, clock=self.clock)
        preview = diagnostics.build_preview(
            self.conn, sections=("events",), project_id=b
        )
        section = preview.section_payloads()["events"]
        self.assertEqual(section["project_id"], b)
        self.assertEqual(section["total_events"], 2)
        self.assertEqual({k["kind"] for k in section["kinds"]},
                         {"task.failed", "task.done"})

    def test_events_truncation_and_size_bound(self) -> None:
        for i in range(150):
            events.append_event(self.conn, f"bench.kind{i:03d}", clock=self.clock)
        preview = diagnostics.build_preview(self.conn, sections=("events",))
        section = preview.section_payloads()["events"]
        self.assertTrue(section["truncated"])
        self.assertEqual(len(section["kinds"]), diagnostics.MAX_EVENT_KINDS)
        self.assertEqual(
            [k["kind"] for k in section["kinds"]],
            sorted(k["kind"] for k in section["kinds"]),
        )
        text = preview.section_map()["events"]
        self.assertLessEqual(len(text.encode("utf-8")), diagnostics.MAX_SECTION_BYTES)
        self.assertTrue(preview.manifest["truncated"])
        self.assertTrue(preview.manifest["truncations"]["events"])

    # -- health ------------------------------------------------------------------

    def test_health_section_sanitizes_and_aggregates(self) -> None:
        preview = diagnostics.build_preview(
            self.conn,
            sections=("health",),
            health={"database": "up", "herdr": {"status": "down"}},
        )
        section = preview.section_payloads()["health"]
        self.assertEqual(section["dependencies"], {"database": "up", "herdr": "down"})
        self.assertEqual(section["aggregate"], "degraded")
        self.assertFalse(section["probe_details_included"])
        self.assertTrue(section["snapshot_provided"])
        # no snapshot => honest unknown, not fabricated ok
        preview2 = diagnostics.build_preview(self.conn, sections=("health",))
        self.assertEqual(
            preview2.section_payloads()["health"]["aggregate"], "unknown"
        )
        self.assertFalse(preview2.section_payloads()["health"]["snapshot_provided"])

    def test_health_input_rejects_detail_and_bad_values(self) -> None:
        with self.assertRaises(ValidationError):
            # probe detail (where paths/credentials hide) is refused, not dropped
            diagnostics.build_preview(
                self.conn, sections=("health",),
                health={"db": {"status": "up", "detail": POSIX_CANARY}},
            )
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(
                self.conn, sections=("health",), health={"db": "healthy"}
            )
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(
                self.conn, sections=("health",), health={"BAD NAME!": "up"}
            )

    # -- failures ---------------------------------------------------------------

    def test_failures_require_evidence_and_reject_extra_fields(self) -> None:
        base = {"kind": "verifier.test", "status": "fail"}
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(
                self.conn, sections=("failures",),
                failures=[dict(base)],  # no evidence_digest => not evidenced
            )
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(
                self.conn, sections=("failures",),
                failures=[dict(base, evidence_digest="xyz")],  # not 64-hex
            )
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(
                self.conn, sections=("failures",),
                failures=[dict(base, evidence_digest=EVIDENCE, log_text="raw log")],
            )
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(
                self.conn, sections=("failures",),
                failures=[dict(base, evidence_digest=EVIDENCE, status="exploded")],
            )

    def test_failures_sanitized_bounded_entries(self) -> None:
        long_detail = "detail head " + POSIX_CANARY + " " + GH_CANARY + " " + "x" * 400
        preview = diagnostics.build_preview(
            self.conn,
            sections=("failures",),
            failures=[
                {
                    "kind": "verifier.test",
                    "status": "fail",
                    "evidence_digest": EVIDENCE,
                    "exit_code": 1,
                    "profile": "test",
                    "occurred_at": "2026-11-01T12:00:00+00:00",
                    "detail": long_detail,
                },
                {
                    "kind": "watch.stalled",
                    "status": "blocked",
                    "evidence_digest": "cd" * 32,
                    "detail": KV_CANARY + " " + HOME_CANARY + " " + WIN_CANARY,
                },
            ],
            secrets=(AWS_CANARY,),
        )
        section = preview.section_payloads()["failures"]
        self.assertEqual(section["count"], 2)
        self.assertFalse(section["raw_logs_included"])
        first, second = section["failures"]
        self.assertEqual(first["exit_code"], 1)
        self.assertTrue(first["detail_clipped"])
        self.assertLessEqual(len(first["detail"]), diagnostics.MAX_DETAIL_CHARS)
        self.assertFalse(second.get("detail_clipped", False))
        blob = all_bytes_of(preview)
        for canary in (POSIX_CANARY, GH_CANARY, KV_CANARY.split("=")[1],
                      HOME_CANARY, WIN_CANARY, AWS_CANARY, ENV_CANARY):
            self.assertNotIn(canary.encode(), blob)
        self.assertIn(diagnostics.SECRET_MARKER.encode(), blob)
        self.assertIn(diagnostics.PATH_MARKER.encode(), blob)
        # The kv key stays visible; only its value is scrubbed.
        self.assertIn(b"password=", blob)
        # bool exit codes and out-of-range ints are refused.
        with self.assertRaises(ValidationError):
            diagnostics.build_preview(
                self.conn, sections=("failures",),
                failures=[{"kind": "k", "status": "error",
                         "evidence_digest": EVIDENCE, "exit_code": True}],
            )

    def test_failure_record_bound_and_bundle_budget(self) -> None:
        records = [
            {"kind": f"task.step{i:03d}", "status": "fail",
             "evidence_digest": EVIDENCE, "detail": "y" * diagnostics.MAX_DETAIL_CHARS}
            for i in range(diagnostics.MAX_FAILURE_RECORDS + 10)
        ]
        preview = diagnostics.build_preview(
            self.conn, sections=("failures",), failures=records
        )
        section = preview.section_payloads()["failures"]
        self.assertEqual(section["count"], diagnostics.MAX_FAILURE_RECORDS)
        self.assertTrue(section["truncated"])
        self.assertLessEqual(
            len(preview.section_map()["failures"].encode("utf-8")),
            diagnostics.MAX_SECTION_BYTES,
        )
        self.assertLessEqual(
            preview.manifest["sections_bytes"], diagnostics.MAX_BUNDLE_BYTES
        )
        # Trimming keeps the section valid, bounded, and leak-free.
        self.assertIsNone(preview.scan())

    # -- secrets / paths ----------------------------------------------------------

    def test_secret_and_path_canaries_never_reach_preview(self) -> None:
        events.append_event(
            self.conn, "task.failed",
            payload_json=json.dumps(
                {"token": GH_CANARY, "root": POSIX_CANARY, "aws": AWS_CANARY}
            ),
            clock=self.clock,
        )
        preview = diagnostics.build_preview(
            self.conn,
            sections=diagnostics.SECTION_ALLOWLIST,
            health={"db": "up"},
            failures=[{"kind": "attempt.unknown", "status": "unknown",
                      "evidence_digest": EVIDENCE,
                      "detail": f"key={AWS_CANARY} at {POSIX_CANARY}"}],
            secrets=(AWS_CANARY, GH_CANARY),
        )
        blob = all_bytes_of(preview)
        for canary in (GH_CANARY, AWS_CANARY, POSIX_CANARY, ENV_CANARY,
                      PAYLOAD_CANARY):
            self.assertNotIn(canary.encode(), blob)
        self.assertNotIn(b"/Users/", blob)
        self.assertIsNone(preview.scan())

    def test_scan_of_scrubbed_output_is_clean_and_clip_boundary_safe(self) -> None:
        # After sanitization, "[REDACTED]"/"<path>" artifacts must NOT
        # re-trigger the residual scan (which would refuse the bundle).
        preview = diagnostics.build_preview(
            self.conn, sections=("failures",),
            failures=[{"kind": "attempt.crash", "status": "error",
                      "evidence_digest": EVIDENCE,
                      "detail": f"token={'z' * 40} leaked at {POSIX_CANARY}"}],
        )
        blob = all_bytes_of(preview)
        self.assertNotIn(POSIX_CANARY.encode(), blob)
        self.assertNotIn(("token=" + "z" * 40).encode(), blob)
        self.assertIsNone(preview.scan())
        # A secret truncated exactly at the clip limit leaves neither the
        # value nor a dangling half marker: the entry is kept (bounded),
        # the value is gone (safe), and the scan stays clean (no false
        # refusal).
        preview2 = diagnostics.build_preview(
            self.conn, sections=("failures",),
            failures=[{"kind": "attempt.crash", "status": "error",
                      "evidence_digest": EVIDENCE,
                      "detail": "p" * 185 + KV_CANARY}],
        )
        entry = preview2.section_payloads()["failures"]["failures"][0]
        self.assertTrue(entry["detail_clipped"])
        self.assertNotIn(b"hunter2xyz", all_bytes_of(preview2))
        self.assertFalse(entry["detail"].endswith("[REDA"))
        self.assertTrue(entry["detail"].endswith("password="))
        self.assertIsNone(preview2.scan())


class DiagnosticsExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m603-x-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"
        storage.migrate_to_head(self.db)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc))
        events.append_event(
            self.conn, "task.failed",
            payload_json=json.dumps({"leak": GH_CANARY, "root": POSIX_CANARY}),
            clock=self.clock,
        )
        self.kwargs = {
            "sections": diagnostics.SECTION_ALLOWLIST,
            "health": {"database": "up", "herdr": "unknown"},
            "failures": [{"kind": "verifier.test", "status": "fail",
                         "evidence_digest": EVIDENCE, "exit_code": 2,
                         "detail": f"at {POSIX_CANARY} token={GH_CANARY}"}],
            "secrets": (GH_CANARY,),
            "generated_at": "2026-11-21T00:00:00+00:00",
        }

    def test_manifest_digest_is_deterministic_and_verifiable(self) -> None:
        first = diagnostics.build_preview(self.conn, **self.kwargs)
        second = diagnostics.build_preview(self.conn, **self.kwargs)
        self.assertEqual(first.manifest_text, second.manifest_text)
        manifest = json.loads(first.manifest_text)
        digest = manifest.pop("manifest_digest")
        body = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
        self.assertEqual(digest, hashlib.sha256(body.encode("utf-8")).hexdigest())
        # An explicit stamp is the only time input and it changes the digest.
        unstamped = diagnostics.build_preview(
            self.conn, **{**self.kwargs, "generated_at": None}
        )
        self.assertIsNone(unstamped.manifest["generated_at"])
        self.assertNotEqual(unstamped.manifest_digest, first.manifest_digest)

    def test_export_scope_parity_with_preview(self) -> None:
        preview = diagnostics.build_preview(self.conn, **self.kwargs)
        out = Path(self._tmp.name) / "out1"
        result = diagnostics.export_bundle(preview, out)
        on_disk = read_export(out)
        expected_names = {"manifest.json"} | {
            f"sections/{name}.json" for name in preview.selected
        }
        self.assertEqual(set(on_disk), expected_names)
        self.assertEqual(on_disk["manifest.json"], preview.manifest_text.encode())
        manifest = json.loads(on_disk["manifest.json"].decode())
        self.assertEqual(
            [f["path"] for f in manifest["files"]],
            [f"sections/{name}.json" for name in preview.selected],
        )
        for entry in manifest["files"]:
            data = on_disk[entry["path"]]
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(entry["bytes"], len(data))
            self.assertEqual(data, preview.section_map()[
                Path(entry["path"]).stem].encode())
        self.assertEqual(result.manifest_digest, preview.manifest_digest)
        self.assertEqual(
            result.total_bytes,
            manifest["sections_bytes"] + len(on_disk["manifest.json"]),
        )

    def test_two_export_runs_are_byte_identical(self) -> None:
        preview_a = diagnostics.build_preview(self.conn, **self.kwargs)
        preview_b = diagnostics.build_preview(self.conn, **self.kwargs)
        out_a = Path(self._tmp.name) / "run-a"
        out_b = Path(self._tmp.name) / "run-b"
        diagnostics.export_bundle(preview_a, out_a)
        diagnostics.export_bundle(preview_b, out_b)
        self.assertEqual(read_export(out_a), read_export(out_b))

    def test_export_refuses_existing_files_and_writes_nothing(self) -> None:
        preview = diagnostics.build_preview(self.conn, **self.kwargs)
        out = Path(self._tmp.name) / "busy"
        (out / "sections").mkdir(parents=True)
        (out / "sections" / "version.json").write_text("keep me")
        with self.assertRaises(ValidationError):
            diagnostics.export_bundle(preview, out)
        self.assertFalse((out / "manifest.json").exists())
        self.assertEqual(
            (out / "sections" / "version.json").read_text(), "keep me"
        )
        self.assertEqual(len(read_export(out)), 1)

    def test_exported_bundle_free_of_raw_canaries(self) -> None:
        os.environ[ENV_VAR_NAME] = ENV_CANARY
        self.addCleanup(os.environ.pop, ENV_VAR_NAME, None)
        preview, result = diagnostics.build_and_export(
            self.conn, Path(self._tmp.name) / "clean", **self.kwargs
        )
        blob = b"".join(read_export(Path(result.output_dir)).values())
        for canary in (GH_CANARY, ENV_CANARY, PAYLOAD_CANARY):
            self.assertNotIn(canary.encode(), blob)
        self.assertNotIn(b"/Users/", blob)
        self.assertNotIn(b"C:\\", blob)
        self.assertNotIn(b"~/", blob)
        self.assertIn(diagnostics.SECRET_MARKER.encode(), blob)
        self.assertIn(diagnostics.PATH_MARKER.encode(), blob)
        self.assertTrue(
            all(name.startswith("sections/") or name == "manifest.json"
                for name in read_export(Path(result.output_dir)))
        )
        self.assertIsNone(preview.scan())

    def test_exported_files_are_private_and_listed(self) -> None:
        preview = diagnostics.build_preview(self.conn, **self.kwargs)
        out = Path(self._tmp.name) / "priv"
        result = diagnostics.export_bundle(preview, out)
        self.assertEqual(
            {f.name for f in result.files},
            {"manifest.json"} | {f"sections/{n}.json" for n in preview.selected},
        )
        for exported in result.files:
            mode = (out / exported.name).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_lagging_schema_refused_before_any_output(self) -> None:
        stale = Path(self._tmp.name) / "stale.db"
        conn = storage.connect(stale)  # fresh file: user_version 0, head is 17
        self.addCleanup(conn.close)
        with self.assertRaises(SchemaOutOfDateError):
            diagnostics.build_preview(conn)


if __name__ == "__main__":
    unittest.main()
