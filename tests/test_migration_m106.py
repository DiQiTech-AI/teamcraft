"""M1-06 Legacy import rehearsal + cutover fault-matrix contract tests.

Everything here runs against synthetic JSON fixtures and disposable SQLite
databases inside ``tempfile.TemporaryDirectory`` (run with
``TMPDIR="$PWD/.tmp-m106"`` so all data stays repository-local).  Nothing reads
or writes the real ``.teamcraft/state.json`` or ``.teamcraft/teamcraft.db``, no
production import or cutover is performed, no Herdr session is contacted, and no
agent receives a prompt.

Pinned contracts (M0 review §8.2/§8.3, §9.1 M1-06):

* explicit synthetic source only — no default, and any ``.teamcraft`` /
  ``.herdr-team`` runtime path or the production DB filename is refused before
  it is opened;
* the original JSON bytes are never overwritten (inode/mtime/size/SHA-256 plus a
  byte-identical backup artifact);
* repeated rehearsal is idempotent (same plan digest reuses rows, no new events);
* field mapping is auditable and never invents completion or liveness: legacy
  ``completed``/``released`` land in a Task ``needs_human``, ambiguous phases and
  unmapped live signals land in ``unknown``;
* cross-Session state imports as history: Sessions ``unknown``,
  ``adopt/resume/resend = 0``, no command rows, no adoption;
* guard contract: legacy mutation allowed only while holding ``state.lock`` with
  no freeze/DB-authoritative marker; corrupt marker fails closed;
* stale/interleaving writers abort the switch (or pause dispatch after
  cutover) and, when fencing cannot be proven, JSON stays the sole writer and the
  SQLite shadow is never promoted;
* crash matrix at every marker/hash/import/commit boundary plus restart picks a
  single authority, and the commit/marker split-brain window stops at
  NEEDS_HUMAN instead of guessing.
"""

from __future__ import annotations

import contextlib
import functools
import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from typing import Any

from teamcraft import migration as mig
from teamcraft.errors import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent

CURRENT_SESSION = {
    "socket_path": "/synthetic/herdr.sock",
    "socket_device": 77,
    "socket_inode": 1001,
}
OTHER_SESSION = {
    "socket_path": "/synthetic/herdr.sock",
    "socket_device": 77,
    "socket_inode": 2002,
}


class SimulatedCrash(RuntimeError):
    """Stand-in for a process dying at an injected boundary."""


def _open_fd_count() -> int | None:
    """Current process descriptor count (macOS/Linux via ``/dev/fd``)."""

    descriptors = Path("/dev/fd")
    if not descriptors.is_dir():  # pragma: no cover - non-POSIX fallback
        return None
    return len(list(descriptors.iterdir()))


@contextlib.contextmanager
def strict_resource_warnings(*, check_fds: bool = True) -> Any:
    """Escalate ``ResourceWarning`` and fail on leaked file descriptors.

    A lock/cursor/connection/pipe that is dropped without closing surfaces here
    either as an error at garbage-collection time or as a rising ``/dev/fd``
    count, which is exactly the review blocker for the lock boundaries.
    """

    before = _open_fd_count() if check_fds else None
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        try:
            yield
        finally:
            gc.collect()
            if before is not None:
                after = _open_fd_count()
                assert after <= before, f"file descriptors leaked: {before} -> {after}"


def legacy_member(name: str, **overrides: Any) -> dict[str, Any]:
    """A member record shaped exactly like the v1 writer produces."""

    member: dict[str, Any] = {
        "name": name,
        "role": "developer",
        "responsibility": "Own the assigned scope.",
        "kind": "codex",
        "task": f"deliver {name}",
        "ownership": [f"src/{name}.py"],
        "read_only": False,
        "cwd": "/synthetic/project",
        "from_pane_id": "pane-parent",
        "pane_id": "pane-1",
        "terminal_id": "term-1",
        "agent_session": "sess-1",
        "agent_args": [],
        "opencode_config_content": None,
        "direction": "right",
        "start_timeout_ms": 30000,
        "phase": "prompt_submitted",
        "prompt_may_have_been_delivered": False,
        "live_state": "working",
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_010,
        "error": None,
    }
    member.update(overrides)
    return member


def legacy_state(
    members: dict[str, dict[str, Any]] | None = None,
    *,
    team_id: str = "team-synthetic-1",
    session: dict[str, Any] | None = None,
    project_root: str = "/synthetic/project",
) -> dict[str, Any]:
    return {
        "version": 1,
        "team_id": team_id,
        "project_root": project_root,
        "herdr_session": dict(CURRENT_SESSION if session is None else session),
        "created_at": 1_700_000_000,
        "members": dict(members or {}),
    }


def legacy_bytes(payload: dict[str, Any]) -> bytes:
    """Exactly the v1 writer's on-disk form (indent 2, sorted keys, newline)."""

    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def no_resource_warnings(func: Any) -> Any:
    """Run a test under ``strict_resource_warnings`` (pipes/locks/fds)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with strict_resource_warnings():
            return func(*args, **kwargs)

    return wrapper


class Fixture:
    """One synthetic project directory: state.json, lock, marker, shadow DB."""

    def __init__(self, parent: str, name: str = "proj") -> None:
        self.root = Path(parent) / name
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = self.root / "state.json"
        self.lock = self.root / "state.lock"
        self.marker = self.root / "rehearsal-marker"
        self.work = self.root / "rehearsal"
        self.work.mkdir(parents=True, exist_ok=True)
        self.db = self.work / "shadow-rehearsal.db"
        self.artifacts = self.work / "artifacts"

    def write(self, payload: dict[str, Any]) -> bytes:
        raw = legacy_bytes(payload)
        self.state.write_bytes(raw)
        return raw

    def source_bytes(self) -> bytes:
        return self.state.read_bytes()

    def plan(
        self,
        *,
        session: dict[str, Any] | None = None,
        generation: str | None = None,
        with_artifact: bool = True,
    ) -> mig.ImportPlan:
        return mig.plan_rehearsal(
            json_path=str(self.state),
            current_session=dict(CURRENT_SESSION if session is None else session),
            generation=generation,
            artifact_dir=str(self.artifacts) if with_artifact else None,
        )

    def cutover(
        self,
        plan: mig.ImportPlan,
        *,
        fence: mig.FenceEvidence | None = None,
        fault: Any = None,
        lock_timeout_seconds: float = 2.0,
    ) -> mig.CutoverRehearsalReport:
        return mig.rehearse_cutover(
            plan=plan,
            db_path=str(self.db),
            lock_path=str(self.lock),
            marker_path=str(self.marker),
            fence=fence or mig.FenceEvidence(
                writer_modes=(mig.WRITER_GUARDED,), writers_enumerated=True, quiescent=True
            ),
            lock_timeout_seconds=lock_timeout_seconds,
            fault=fault,
        )

    def authority(self) -> mig.AuthorityDecision:
        return mig.choose_authority(db_path=str(self.db), marker_path=str(self.marker))

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if not self.db.exists():
            raise AssertionError(f"no rehearsal database was created at {self.db}")
        connection = sqlite3.connect(str(self.db))
        try:
            return [tuple(row) for row in connection.execute(sql, params).fetchall()]
        finally:
            connection.close()

    def count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(self.rows(sql, params)[0][0])

    def value(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self.rows(sql, params)[0][0]

    def authority_value(self) -> str:
        return str(self.value("SELECT authoritative FROM legacy_cutover_state"))

    def event_kinds(self) -> list[str]:
        return [str(row[0]) for row in self.rows("SELECT kind FROM events ORDER BY seq")]

    def guard_allows_legacy_write(self, generation: str | None = None) -> mig.GuardDecision:
        with mig.LegacyStateLock(str(self.lock), timeout_seconds=2.0) as lock:
            return mig.guard_legacy_write(
                marker_path=str(self.marker), lock=lock, expected_generation=generation
            )


class RepositoryLocalTempTests(unittest.TestCase):
    def test_temporary_data_stays_inside_repository(self) -> None:
        """Every fixture DB/file lands under the repo (TMPDIR="$PWD/.tmp-m106")."""

        temporary = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(
            str(temporary).startswith(str(REPO_ROOT)),
            f"tests must run with TMPDIR inside the repository, got {temporary}",
        )


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-bound-")
        self.addCleanup(self._tmp.cleanup)
        self.parent = self._tmp.name

    def test_no_default_source_and_relative_paths_refused(self) -> None:
        with self.assertRaises(TypeError):
            mig.plan_rehearsal()  # type: ignore[call-arg]
        with self.assertRaises(ValidationError) as ctx:
            mig.plan_rehearsal(json_path="state.json")
        self.assertIn("absolute", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            mig.hash_legacy_state("relative/state.json")
        self.assertIn("absolute", str(ctx.exception))

    def test_real_runtime_directory_is_refused_before_opening(self) -> None:
        for runtime in (".teamcraft", ".herdr-team"):
            with self.subTest(runtime=runtime):
                directory = Path(self.parent) / f"real-{runtime}" / runtime
                directory.mkdir(parents=True, exist_ok=True)
                state = directory / "state.json"
                original = legacy_bytes(legacy_state({"dev": legacy_member("dev")}))
                state.write_bytes(original)
                with self.assertRaises(mig.ProductionStateRefusal) as ctx:
                    mig.plan_rehearsal(json_path=str(state))
                self.assertIn("runtime directory", str(ctx.exception))
                self.assertEqual(state.read_bytes(), original)
                self.assertFalse((directory / "teamcraft.db").exists())
                self.assertEqual(
                    sorted(p.name for p in directory.iterdir()), ["state.json"]
                )

    def test_production_database_name_is_refused(self) -> None:
        fixture = Fixture(self.parent, "db-name")
        fixture.write(legacy_state({"dev": legacy_member("dev")}))
        with self.assertRaises(mig.ProductionStateRefusal):
            mig.run_rehearsal(
                plan=fixture.plan(), db_path=str(fixture.root / "teamcraft.db")
            )
        self.assertFalse((fixture.root / "teamcraft.db").exists())

    def test_symlinked_source_and_missing_file_refused(self) -> None:
        fixture = Fixture(self.parent, "symlink")
        fixture.write(legacy_state())
        link = fixture.root / "alias.json"
        link.symlink_to(fixture.state.name)
        with self.assertRaises(ValidationError):
            mig.capture_snapshot(str(link))
        with self.assertRaises(ValidationError):
            mig.capture_snapshot(str(fixture.root / "absent.json"))


class LegacyReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-read-")
        self.addCleanup(self._tmp.cleanup)
        self.parent = self._tmp.name

    def test_empty_state_is_a_valid_rehearsal_source(self) -> None:
        fixture = Fixture(self.parent, "empty")
        before = fixture.write(legacy_state({}))
        plan = fixture.plan()
        self.assertEqual(plan.records, ())
        self.assertFalse(plan.cross_session)
        report = mig.run_rehearsal(plan=plan, db_path=str(fixture.db))
        self.assertEqual(report.record_count, 0)
        self.assertEqual(report.authoritative, mig.MODE_JSON)
        self.assertTrue(report.db_is_shadow)
        self.assertEqual(fixture.count("SELECT COUNT(*) FROM projects"), 1)
        self.assertEqual(fixture.source_bytes(), before)

    def test_missing_herdr_session_is_cross_session(self) -> None:
        payload = legacy_state({"dev": legacy_member("dev")})
        del payload["herdr_session"]
        fixture = Fixture(self.parent, "nosession")
        fixture.write(payload)
        plan = fixture.plan()
        self.assertTrue(plan.cross_session)
        self.assertEqual(
            plan.prohibited_actions, mig.PROHIBITED_CROSS_SESSION_ACTIONS
        )

    def test_corrupt_and_unsupported_documents_are_refused_without_side_effects(self) -> None:
        cases: dict[str, Any] = {
            "not_json": b"{not json",
            "not_utf8": b'{"version": 1, "team_id": "\xff\xfe"}',
            "root_not_object": b'["version", 1]',
            "version_two": json.dumps({"version": 2, "team_id": "t", "project_root": "/x"}).encode(),
            "version_missing": json.dumps({"team_id": "t", "project_root": "/x"}).encode(),
            "blank_team_id": json.dumps({"version": 1, "team_id": " ", "project_root": "/x"}).encode(),
            "missing_project_root": json.dumps({"version": 1, "team_id": "t"}).encode(),
            "members_not_object": json.dumps(
                {"version": 1, "team_id": "t", "project_root": "/x", "members": []}
            ).encode(),
            "member_not_object": json.dumps(
                {"version": 1, "team_id": "t", "project_root": "/x", "members": {"a": 3}}
            ).encode(),
            "key_name_mismatch": json.dumps(
                {
                    "version": 1, "team_id": "t", "project_root": "/x",
                    "members": {"a": {"name": "b", "phase": "reserved"}},
                }
            ).encode(),
            "session_not_object": json.dumps(
                {
                    "version": 1, "team_id": "t", "project_root": "/x",
                    "members": {}, "herdr_session": "sock",
                }
            ).encode(),
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                path = Path(self.parent) / f"{name}.json"
                path.write_bytes(raw)
                before = path.read_bytes()
                with self.assertRaises(mig.LegacyStateInvalid):
                    mig.validate_legacy_state(raw, source=name)
                with self.assertRaises(mig.LegacyStateInvalid):
                    mig.capture_snapshot(str(path))
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(Path(self.parent, f"{name}.db").exists())


class BytePreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-bytes-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "bytes")
        self.payload = legacy_state(
            {"ceo": legacy_member("ceo", role="ceo", read_only=True, ownership=[]),
             "dev": legacy_member("dev")}
        )
        self.original = self.fixture.write(self.payload)

    def test_snapshot_and_backup_are_byte_identical(self) -> None:
        snapshot = mig.capture_snapshot(str(self.fixture.state), artifact_dir=str(self.fixture.artifacts))
        self.assertTrue(snapshot.backup_is_byte_identical())
        self.assertTrue(snapshot.source_unchanged())
        self.assertEqual(self.fixture.source_bytes(), self.original)
        fingerprint = snapshot.fingerprint
        current = mig.fingerprint_source(str(self.fixture.state))
        self.assertEqual((fingerprint.size, fingerprint.sha256), (current.size, current.sha256))
        self.assertTrue(fingerprint.same_stability_window(current))
        artifacts = sorted(p.name for p in self.fixture.artifacts.iterdir())
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0].startswith("legacy-snapshot-"))

    def test_full_rehearsal_and_cutover_never_touch_the_source_file(self) -> None:
        plan = self.fixture.plan()
        mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        self.fixture.cutover(plan)
        self.assertEqual(self.fixture.source_bytes(), self.original)
        self.assertEqual(
            mig.hash_legacy_state(str(self.fixture.state)), plan.source_sha256
        )
        extra = sorted(
            p.name for p in self.fixture.root.iterdir() if p.name not in {"state.json", "state.lock", "rehearsal-marker", "rehearsal"}
        )
        self.assertEqual(extra, [])


class FieldMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-map-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "map")
        self.payload = legacy_state(
            {
                "ceo": legacy_member(
                    "ceo", role="ceo", kind="codex", read_only=True, ownership=[],
                    phase="prompt_submitted", live_state="working",
                ),
                "dev": legacy_member(
                    "dev", role="developer", kind="opencode",
                    ownership=["src/dev.py", "docs/**"], phase="prompt_ambiguous",
                    live_state="unknown",
                ),
                "tester": legacy_member(
                    "tester", role="tester", phase="weird_phase", live_state="done",
                ),
                "cto": legacy_member("cto", role="cto", phase="completed", live_state="idle"),
                "gone": legacy_member("gone", role="designer", phase="released", live_state="missing"),
            }
        )
        self.fixture.write(self.payload)
        self.plan = self.fixture.plan()
        self.report = mig.run_rehearsal(plan=self.plan, db_path=str(self.fixture.db))

    def _record(self, name: str) -> mig.ImportRecord:
        return next(record for record in self.plan.records if record.legacy_name == name)

    def test_counts_and_projection(self) -> None:
        self.assertEqual(self.report.record_count, 5)
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM tasks"), 5)
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM attempts"), 5)
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM sessions"), 5)
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM legacy_migration_records"), 5)

    def test_read_only_member_is_scout_without_ownership(self) -> None:
        record = self._record("ceo")
        self.assertTrue(record.read_only)
        self.assertEqual(record.task_kind, "scout")
        self.assertEqual(record.ownership, ())
        row = self.fixture.rows(
            "SELECT kind, status, ownership_json FROM tasks WHERE id = ?", (record.task_id,)
        )[0]
        self.assertEqual(row, ("scout", "running", "[]"))

    def test_editing_member_keeps_ownership_and_ship_kind(self) -> None:
        record = self._record("dev")
        self.assertEqual(record.task_kind, "ship")
        self.assertEqual(record.ownership, ("src/dev.py", "docs/**"))
        self.assertIn("ambiguous_execution_never_auto_resend", record.flags)
        self.assertEqual(record.task_status, "running")
        self.assertEqual(record.attempt_status, "unknown")
        self.assertEqual(record.session_state, "unknown")

    def test_unknown_phase_and_live_state_are_quarantined(self) -> None:
        record = self._record("tester")
        self.assertEqual(record.task_status, "needs_human")
        self.assertEqual(record.attempt_status, "unknown")
        self.assertEqual(record.session_state, "unknown")
        self.assertIn("unknown_phase", record.flags)
        self.assertIn("unmapped_live_state:done", record.flags)
        self.assertIn("tester: unknown phase 'weird_phase'", self.plan.unknowns)

    def test_legacy_terminal_phases_neach_import_as_done(self) -> None:
        """8.2-4/8.4: v1 has no verification evidence, so nothing imports DONE."""

        self.assertEqual(self._record("cto").task_status, "needs_human")
        self.assertEqual(self._record("cto").attempt_status, "finished")
        self.assertEqual(self._record("gone").attempt_status, "cancelled")
        self.assertEqual(self._record("gone").session_state, "missing")
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM tasks WHERE status = 'done'"), 0
        )
        self.assertEqual(
            self.fixture.count(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN "
                "('pending','ready','running','needs_human')"
            ),
            0,
        )

    def test_unknown_attempt_keeps_the_occupying_slot(self) -> None:
        """Ambiguous legacy execution still occupies its task slot (no re-dispatch)."""

        dev = self._record("dev")
        with self.assertRaises(sqlite3.IntegrityError):
            connection = sqlite3.connect(str(self.fixture.db))
            try:
                connection.execute(
                    "INSERT INTO attempts (id, task_id, agent_id, attempt_no, status)"
                    " VALUES ('slot-probe', ?, NULL, 9, 'active')",
                    (dev.task_id,),
                )
            finally:
                connection.close()

    def test_legacy_payload_and_field_map_are_retained_for_audit(self) -> None:
        row = self.fixture.rows(
            "SELECT legacy_payload_json, mapping_json, flags_json, task_status,"
            " attempt_status, session_state FROM legacy_migration_records"
            " WHERE legacy_name = 'dev'"
        )[0]
        payload = json.loads(str(row[0]))
        mapping = json.loads(str(row[1]))
        self.assertEqual(payload["task"], "deliver dev")
        self.assertEqual(payload["pane_id"], "pane-1")
        self.assertEqual(mapping["legacy.kind"], "opencode")
        self.assertEqual(mapping["domain.task.status"], "running")
        self.assertEqual(mapping["domain.session.state"], "unknown")
        self.assertIn("ambiguous_execution_never_auto_resend", json.loads(str(row[2])))

    def test_ids_are_stable_across_replans_and_scoped_by_team(self) -> None:
        again = mig.plan_rehearsal(
            json_path=str(self.fixture.state), current_session=dict(CURRENT_SESSION)
        )
        self.assertEqual(
            [record.task_id for record in self.plan.records],
            [record.task_id for record in again.records],
        )
        self.assertEqual(self.plan.snapshot.team_id, again.snapshot.team_id)
        other_team = mig.plan_rehearsal(
            json_path=str(self.fixture.state), current_session=dict(CURRENT_SESSION),
            generation="cut_other",
        )
        self.assertNotEqual(
            mig.new_cutover_generation(), mig.new_cutover_generation()
        )
        self.assertEqual(
            self.plan.project_id,
            mig.legacy_entity_id("project", self.plan.snapshot.team_id, self.plan.repo_path),
        )
        del other_team


class CrossSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-cross-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "cross")
        self.fixture.write(
            legacy_state({"dev": legacy_member("dev"), "ceo": legacy_member(
                "ceo", role="ceo", read_only=True, ownership=[])})
        )

    def test_cross_session_imports_history_without_adoption(self) -> None:
        plan = self.fixture.plan(session=OTHER_SESSION)
        self.assertTrue(plan.cross_session)
        self.assertEqual(plan.prohibited_actions, mig.PROHIBITED_CROSS_SESSION_ACTIONS)
        mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM sessions WHERE state <> 'unknown'"), 0
        )
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM legacy_migration_records WHERE cross_session = 1"),
            2,
        )
        self.assertEqual(
            self.fixture.count(
                "SELECT COUNT(*) FROM legacy_migration_records WHERE adopt <> 0 OR resume <> 0"
                " OR resend_prompt <> 0"
            ),
            0,
        )
        # No resume/resend/adopt command was ever queued from imported history.
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM commands"), 0)
        self.assertFalse([kind for kind in self.fixture.event_kinds() if "resend" in kind])
        self.assertFalse([kind for kind in self.fixture.event_kinds() if "adopt" in kind])

    def test_task_ids_are_independent_of_session_identity(self) -> None:
        same = self.fixture.plan(session=CURRENT_SESSION)
        cross = self.fixture.plan(session=OTHER_SESSION)
        self.assertEqual(
            [record.task_id for record in same.records],
            [record.task_id for record in cross.records],
        )
        self.assertNotEqual(same.plan_sha256, cross.plan_sha256)

    def test_same_session_keeps_observed_live_signal_as_history(self) -> None:
        plan = self.fixture.plan(session=CURRENT_SESSION)
        self.assertFalse(plan.cross_session)
        record = next(r for r in plan.records if r.legacy_name == "dev")
        self.assertEqual(record.session_state, "working")
        self.assertEqual(record.flags, ())


class IdempotenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-idem-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "idem")
        self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    def test_repeat_rehearsal_reuses_rows_and_appends_no_events(self) -> None:
        plan = self.fixture.plan()
        first = mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        self.assertFalse(first.reused)
        events_after_first = self.fixture.event_kinds()
        rows_after_first = (
            self.fixture.count("SELECT COUNT(*) FROM tasks"),
            self.fixture.count("SELECT COUNT(*) FROM attempts"),
            self.fixture.count("SELECT COUNT(*) FROM legacy_migration_records"),
            self.fixture.count("SELECT COUNT(*) FROM legacy_migration_rehearsals"),
        )
        for _ in range(3):
            repeat = mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
            self.assertTrue(repeat.reused)
            self.assertEqual(repeat.plan_sha256, first.plan_sha256)
            self.assertEqual(repeat.record_count, first.record_count)
        self.assertEqual(self.fixture.event_kinds(), events_after_first)
        self.assertEqual(
            (
                self.fixture.count("SELECT COUNT(*) FROM tasks"),
                self.fixture.count("SELECT COUNT(*) FROM attempts"),
                self.fixture.count("SELECT COUNT(*) FROM legacy_migration_records"),
                self.fixture.count("SELECT COUNT(*) FROM legacy_migration_rehearsals"),
            ),
            rows_after_first,
        )

    def test_fresh_generation_same_content_still_converges_to_one_rehearsal(self) -> None:
        first = mig.run_rehearsal(plan=self.fixture.plan(), db_path=str(self.fixture.db))
        second_plan = self.fixture.plan(generation=mig.new_cutover_generation())
        second = mig.run_rehearsal(plan=second_plan, db_path=str(self.fixture.db))
        self.assertTrue(second.reused)
        self.assertNotEqual(first.generation, second.generation)
        # Only the first generation owns a rehearsal row; the second converged
        # onto the existing rows instead of duplicating them.
        self.assertEqual(
            [str(row[0]) for row in self.fixture.rows(
                "SELECT generation FROM legacy_migration_rehearsals")],
            [first.generation],
        )
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM legacy_migration_rehearsals"), 1
        )
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM tasks"), 1)

    def test_changed_content_is_a_new_rehearsal(self) -> None:
        mig.run_rehearsal(plan=self.fixture.plan(), db_path=str(self.fixture.db))
        self.fixture.write(legacy_state({"dev": legacy_member("dev"), "qa": legacy_member("qa")}))
        plan = self.fixture.plan()
        report = mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        self.assertFalse(report.reused)
        self.assertEqual(report.record_count, 2)
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM tasks"), 2)


class GuardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-guard-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "guard")
        self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    def test_no_marker_means_json_is_the_writer(self) -> None:
        decision = self.fixture.guard_allows_legacy_write()
        self.assertTrue(decision.allow)
        self.assertEqual(decision.reason_code, "ALLOW_NO_MARKER")

    def test_guard_requires_the_lock_before_it_trusts_anything(self) -> None:
        mig.deploy_marker(str(self.fixture.marker), mode=mig.MARKER_JSON_AUTHORITATIVE, generation="g1")
        unheld = mig.LegacyStateLock(str(self.fixture.lock))
        decision = mig.guard_legacy_write(marker_path=str(self.fixture.marker), lock=unheld)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason_code, "LOCK_NOT_HELD")

    def test_freeze_and_db_authoritative_markers_deny_legacy_writes(self) -> None:
        mig.deploy_marker(str(self.fixture.marker), mode=mig.MARKER_FREEZE, generation="gen-1")
        decision = self.fixture.guard_allows_legacy_write(generation="gen-1")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason_code, "FROZEN")
        mig.deploy_marker(
            str(self.fixture.marker), mode=mig.MARKER_DB_AUTHORITATIVE, generation="gen-1"
        )
        decision = self.fixture.guard_allows_legacy_write(generation="gen-1")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason_code, "DB_AUTHORITATIVE")

    def test_json_authoritative_marker_allows_and_generation_mismatch_denies(self) -> None:
        mig.deploy_marker(
            str(self.fixture.marker), mode=mig.MARKER_JSON_AUTHORITATIVE, generation="gen-9"
        )
        self.assertTrue(self.fixture.guard_allows_legacy_write().allow)
        mismatch = self.fixture.guard_allows_legacy_write(generation="gen-1")
        self.assertFalse(mismatch.allow)
        self.assertEqual(mismatch.reason_code, "GENERATION_MISMATCH")

    def test_corrupt_marker_fails_closed(self) -> None:
        self.fixture.marker.write_text("garbage{", encoding="utf-8")
        decision = self.fixture.guard_allows_legacy_write()
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason_code, "MARKER_CORRUPT")
        with self.assertRaises(mig.MarkerCorrupt):
            mig.read_marker(str(self.fixture.marker))

    def test_marker_protocol_fields_are_pinned(self) -> None:
        state = mig.deploy_marker(
            str(self.fixture.marker), mode=mig.MARKER_FREEZE, generation="gen-7",
            source_sha256="ab" * 32,
        )
        document = json.loads(self.fixture.marker.read_text(encoding="utf-8"))
        self.assertEqual(document["protocol"], mig.MARKER_PROTOCOL)
        self.assertEqual(document["mode"], mig.MARKER_FREEZE)
        self.assertEqual(document["generation"], "gen-7")
        self.assertEqual(state.generation, "gen-7")
        self.assertTrue(mig.remove_marker(str(self.fixture.marker)))
        self.assertFalse(mig.remove_marker(str(self.fixture.marker)))

    def test_lock_is_exclusive_against_a_concurrent_holder(self) -> None:
        held = mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=0.05)
        held.acquire()
        try:
            with self.assertRaises(mig.LockUnavailable):
                mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=0.05).acquire()
        finally:
            held.release()
        again = mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=0.5)
        again.acquire()
        again.release()
        self.assertFalse(again.owned)


class FenceAssessmentTests(unittest.TestCase):
    def test_only_enumerated_guarded_quiescent_writers_can_fence(self) -> None:
        good = mig.FenceEvidence(
            writer_modes=(mig.WRITER_GUARDED, mig.WRITER_GUARDED),
            writers_enumerated=True,
            quiescent=True,
        )
        self.assertTrue(good.can_fence)
        self.assertEqual(mig.evaluate_fence(good).allow_cutover, True)

    def test_unfenceable_evidence_keeps_json_the_sole_writer(self) -> None:
        cases = {
            mig.WRITER_LOADED_UNAWARE: "LOADED_OLD_WRITER_NOT_FENCEABLE",
            mig.WRITER_UNKNOWN_PROCESS: "UNKNOWN_LEGACY_PROCESS",
        }
        for mode, reason in cases.items():
            with self.subTest(mode=mode):
                evidence = mig.FenceEvidence(
                    writer_modes=(mig.WRITER_GUARDED, mode),
                    writers_enumerated=True,
                    quiescent=True,
                )
                assessment = mig.evaluate_fence(evidence.with_quiescence(True))
                self.assertFalse(assessment.allow_cutover)
                self.assertIn(reason, assessment.reasons)
                self.assertTrue(assessment.json_is_sole_writer)

    def test_missing_enumeration_or_quiescence_refuses(self) -> None:
        not_enumerated = mig.FenceEvidence(
            writer_modes=(mig.WRITER_GUARDED,), writers_enumerated=False, quiescent=True
        )
        self.assertIn("WRITERS_NOT_ENUMERATED", not_enumerated.reasons())
        no_evidence = mig.FenceEvidence()
        self.assertIn("NO_KNOWN_WRITER_EVIDENCE", no_evidence.reasons())
        noisy = mig.FenceEvidence(
            writer_modes=(mig.WRITER_GUARDED,), writers_enumerated=True, quiescent=True
        )
        assessment = mig.evaluate_fence(noisy.with_quiescence(False))
        self.assertFalse(assessment.allow_cutover)
        self.assertIn("QUIESCENCE_NOT_PROVEN", assessment.reasons)


class QuiescenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-quiet-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "quiet")
        self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    def test_stable_window_under_lock_proves_quiescence(self) -> None:
        baseline = mig.fingerprint_source(str(self.fixture.state))
        with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=2.0) as lock:
            proof = mig.prove_quiescence(
                json_path=str(self.fixture.state), baseline=baseline, lock=lock,
                extra_observations=2, observation_interval_seconds=0.01,
            )
        self.assertTrue(proof.stable)
        self.assertEqual(proof.reasons, ())
        self.assertEqual(len(proof.observations), 3)

    def test_without_the_lock_no_proof_is_possible(self) -> None:
        baseline = mig.fingerprint_source(str(self.fixture.state))
        proof = mig.prove_quiescence(
            json_path=str(self.fixture.state), baseline=baseline,
            lock=mig.LegacyStateLock(str(self.fixture.lock)),
        )
        self.assertFalse(proof.stable)
        self.assertEqual(proof.reasons, ("LOCK_NOT_HELD",))

    def test_interleaving_writer_before_the_lock_is_observed(self) -> None:
        baseline = mig.fingerprint_source(str(self.fixture.state))
        stop = threading.Event()

        def writer() -> None:
            deadline = time.monotonic() + 2.0
            while not stop.is_set() and time.monotonic() < deadline:
                payload = legacy_state({"dev": legacy_member("dev"), "late": legacy_member("late")})
                with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=1.0):
                    self.fixture.write(payload)
                    return

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        stop.set()
        thread.join(timeout=2.0)
        with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=2.0) as lock:
            proof = mig.prove_quiescence(
                json_path=str(self.fixture.state), baseline=baseline, lock=lock
            )
        self.assertFalse(proof.stable)
        self.assertIn("SOURCE_CHANGED_BEFORE_FREEZE", proof.reasons)


class LockReleaseGuaranteeTests(unittest.TestCase):
    """Review blocker: faults at EITHER lock boundary return the capability now.

    An injected exception between ``lock.acquire()`` and its protecting
    try/finally (``lock_acquire``) or inside the exit finally before
    ``lock.release()`` (``release_lock``) used to strand the fd and keep the
    flock held in the live process (with a ``ResourceWarning`` at GC).  Each
    test injects the fault and then proves, with zero wait, that an
    independent descriptor can take the lock, that no descriptor leaked and no
    ``ResourceWarning`` fired, and that the crash semantics at that boundary
    are exactly what they were before.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-lockfix-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "lockfix")
        self.original = self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    @staticmethod
    def _raiser(stage: str) -> Any:
        def hook(current: str) -> None:
            if current == stage:
                raise SimulatedCrash(f"crash at {current}")

        return hook

    def _acquire_immediately(self) -> None:
        """Zero-wait acquisition must succeed once the fault has unwound."""

        probe = mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=0.0)
        probe.acquire()
        probe.release()

    def test_fault_at_lock_acquire_returns_capability_and_fd(self) -> None:
        plan = self.fixture.plan()
        with strict_resource_warnings(), self.assertRaises(SimulatedCrash):
            self.fixture.cutover(plan, fault=self._raiser(mig.STAGE_LOCK_ACQUIRE))
        self._acquire_immediately()
        # Boundary semantics unchanged: freeze survived, JSON stays authoritative
        # and paused, nothing promoted, source untouched.
        marker = mig.read_marker(str(self.fixture.marker))
        self.assertEqual(marker.mode, mig.MARKER_FREEZE)
        authority = self.fixture.authority()
        self.assertEqual(authority.source, mig.MODE_JSON)
        self.assertTrue(authority.dispatch_paused)
        self.assertFalse(authority.db_side_effects_permitted)
        self.assertEqual(self.fixture.source_bytes(), self.original)

    def test_fault_at_release_lock_returns_capability_and_fd(self) -> None:
        plan = self.fixture.plan()
        with strict_resource_warnings(), self.assertRaises(SimulatedCrash):
            self.fixture.cutover(plan, fault=self._raiser(mig.STAGE_RELEASE_LOCK))
        self._acquire_immediately()
        # Boundary semantics unchanged: the completed cutover is consistent.
        authority = self.fixture.authority()
        self.assertEqual(authority.source, mig.MODE_DB)
        self.assertEqual(authority.reason_code, "DB_AND_MARKER_AGREE")
        self.assertEqual(self.fixture.source_bytes(), self.original)

    def test_capability_is_genuinely_exclusive_during_the_body(self) -> None:
        """The lock must still exclude others while the cutover body runs."""

        plan = self.fixture.plan()
        contention: list[str] = []

        def hook(stage: str) -> None:
            if stage == mig.STAGE_IMPORT_ROWS:
                probe = mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=0.0)
                try:
                    probe.acquire()
                except mig.LockUnavailable:
                    contention.append("blocked")
                else:
                    contention.append("acquired")
                finally:
                    probe.release()

        with strict_resource_warnings():
            report = self.fixture.cutover(plan, fault=hook)
        self.assertEqual(contention, ["blocked"])
        self.assertEqual(report.authoritative, mig.MODE_DB)
        self._acquire_immediately()
        self.assertEqual(self.fixture.source_bytes(), self.original)

    def test_repeated_boundary_faults_never_leak_descriptors(self) -> None:
        """Repeat each boundary fault on ISOLATED fixtures/generations.

        A fresh fixture per iteration keeps every cutover eligible for the
        injected fault (no accidental ALREADY_CUTOVER_OTHER_GENERATION
        short-circuit) while the fd accounting spans all cycles.
        """

        for stage in (mig.STAGE_LOCK_ACQUIRE, mig.STAGE_RELEASE_LOCK):
            with self.subTest(stage=stage):
                before = _open_fd_count()
                for iteration in range(5):
                    fixture = Fixture(self._tmp.name, f"leak-{stage}-{iteration}")
                    fixture.write(legacy_state({"dev": legacy_member("dev")}))
                    plan = fixture.plan()  # fresh generation each cycle
                    with self.assertRaises(SimulatedCrash):
                        fixture.cutover(plan, fault=self._raiser(stage))
                    probe = mig.LegacyStateLock(
                        str(fixture.lock), timeout_seconds=0.0
                    )
                    probe.acquire()  # must not raise LockUnavailable
                    probe.release()
                gc.collect()
                if before is not None:
                    self.assertLessEqual(_open_fd_count(), before)


class CutoverRefusalTests(unittest.TestCase):
    """The critical outcome: no reliable fence means no cutover."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-refuse-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "refuse")
        self.original = self.fixture.write(
            legacy_state({"dev": legacy_member("dev"), "ceo": legacy_member(
                "ceo", role="ceo", read_only=True, ownership=[])})
        )

    def _assert_json_writes_and_shadow(self) -> None:
        self.assertEqual(self.fixture.source_bytes(), self.original)
        authority = self.fixture.authority()
        self.assertEqual(authority.source, mig.MODE_JSON)
        self.assertFalse(authority.db_side_effects_permitted)
        self.assertEqual(self.fixture.authority_value(), "json")

    def test_loaded_unaware_writer_refuses_cutover(self) -> None:
        plan = self.fixture.plan()
        fence = mig.FenceEvidence(
            writer_modes=(mig.WRITER_GUARDED, mig.WRITER_LOADED_UNAWARE),
            writers_enumerated=True,
            quiescent=True,
        )
        with self.assertRaises(mig.CutoverRefused) as ctx:
            self.fixture.cutover(plan, fence=fence)
        self.assertIn("LOADED_OLD_WRITER_NOT_FENCEABLE", ctx.exception.reasons)
        self.assertEqual(ctx.exception.authoritative, mig.MODE_JSON)
        self.assertTrue(ctx.exception.marker_lifted)
        self.assertFalse(self.fixture.marker.exists())
        self._assert_json_writes_and_shadow()
        self.assertIn("migration.cutover.refused", self.fixture.event_kinds())
        self.assertEqual(
            self.fixture.value(
                "SELECT status FROM legacy_migration_rehearsals WHERE generation = ?",
                (plan.generation,),
            ),
            "refused",
        )
        # Shadow rows may exist but the DB is never promoted and never exported
        # as authoritative state.
        self.assertNotEqual(self.fixture.authority().source, mig.MODE_DB)

    def test_unknown_process_refuses_cutover(self) -> None:
        plan = self.fixture.plan()
        with self.assertRaises(mig.CutoverRefused) as ctx:
            self.fixture.cutover(
                plan,
                fence=mig.FenceEvidence(
                    writer_modes=(mig.WRITER_UNKNOWN_PROCESS,),
                    writers_enumerated=True,
                    quiescent=True,
                ),
            )
        self.assertIn("UNKNOWN_LEGACY_PROCESS", ctx.exception.reasons)
        self._assert_json_writes_and_shadow()

    def test_writers_not_enumerated_refuses_cutover(self) -> None:
        plan = self.fixture.plan()
        with self.assertRaises(mig.CutoverRefused) as ctx:
            self.fixture.cutover(
                plan, fence=mig.FenceEvidence(writer_modes=(), writers_enumerated=False, quiescent=True)
            )
        self.assertIn("WRITERS_NOT_ENUMERATED", ctx.exception.reasons)
        self.assertFalse(self.fixture.marker.exists())

    def test_busy_legacy_lock_refuses_cutover_and_lifts_freeze(self) -> None:
        plan = self.fixture.plan()
        held = mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=2.0)
        held.acquire()
        try:
            with self.assertRaises(mig.CutoverRefused) as ctx:
                self.fixture.cutover(plan, lock_timeout_seconds=0.05)
        finally:
            held.release()
        self.assertIn("LEGACY_LOCK_UNAVAILABLE", ctx.exception.reasons)
        self.assertTrue(ctx.exception.marker_lifted)
        self.assertFalse(self.fixture.marker.exists())
        self._assert_json_writes_and_shadow()

    def test_stale_plan_refuses_cutover(self) -> None:
        plan = self.fixture.plan()
        self.fixture.write(legacy_state({"dev": legacy_member("dev"), "extra": legacy_member("extra")}))
        with self.assertRaises(mig.CutoverRefused) as ctx:
            self.fixture.cutover(plan)
        self.assertIn("SOURCE_STALE_AT_CUTOVER", ctx.exception.reasons)
        self.assertFalse(self.fixture.marker.exists())
        authority = self.fixture.authority()
        self.assertEqual(authority.source, mig.MODE_JSON)

    def test_run_rehearsal_refuses_stale_source(self) -> None:
        plan = self.fixture.plan()
        self.fixture.write(legacy_state({"other": legacy_member("other")}))
        with self.assertRaises(mig.CutoverRefused) as ctx:
            mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        self.assertIn("SOURCE_STALE_AT_REHEARSAL", ctx.exception.reasons)
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM legacy_migration_rehearsals WHERE status = 'imported'"), 0)


class InterleavingWriterTests(unittest.TestCase):
    """Deployed-guard writers must be blocked inside the cutover window."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-interleave-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "interleave")
        self.original = self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    def test_guarded_writer_is_blocked_and_cutover_proceeds(self) -> None:
        plan = self.fixture.plan()
        mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        attempts: list[mig.GuardDecision] = []
        ready = threading.Event()
        done = threading.Event()

        def guarded_writer() -> None:
            with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=5.0) as lock:
                ready.set()
                attempts.append(
                    mig.guard_legacy_write(
                        marker_path=str(self.fixture.marker), lock=lock,
                        expected_generation=plan.generation,
                    )
                )
                done.set()

        # Freeze first (the cutover does this), then try to write: refused.
        mig.deploy_marker(
            str(self.fixture.marker), mode=mig.MARKER_FREEZE, generation=plan.generation
        )
        thread = threading.Thread(target=guarded_writer, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5.0))
        self.assertTrue(done.wait(5.0))
        thread.join(timeout=5.0)
        self.assertEqual(attempts[0].reason_code, "FROZEN")
        self.assertFalse(attempts[0].allow)
        mig.remove_marker(str(self.fixture.marker))
        report = self.fixture.cutover(plan)
        self.assertEqual(report.authoritative, mig.MODE_DB)
        self.assertEqual(self.fixture.source_bytes(), self.original)

    def test_writer_holding_the_lock_forces_a_refusal_then_retry_after_quiet(self) -> None:
        plan = self.fixture.plan()
        release = threading.Event()
        acquired = threading.Event()
        payload = legacy_state({"dev": legacy_member("dev"), "late": legacy_member("late")})

        def loaded_writer() -> None:
            with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=5.0):
                acquired.set()
                release.wait(5.0)
                self.fixture.write(payload)

        thread = threading.Thread(target=loaded_writer, daemon=True)
        thread.start()
        self.assertTrue(acquired.wait(5.0))
        with self.assertRaises(mig.CutoverRefused) as ctx:
            self.fixture.cutover(plan, lock_timeout_seconds=0.05)
        self.assertIn("LEGACY_LOCK_UNAVAILABLE", ctx.exception.reasons)
        self.assertEqual(self.fixture.authority().source, mig.MODE_JSON)
        release.set()
        thread.join(timeout=5.0)
        # The old writer did write, so the frozen plan is stale: refuse again,
        # then re-plan against the new bytes and only then switch.
        with self.assertRaises(mig.CutoverRefused) as stale:
            self.fixture.cutover(plan)
        self.assertIn("SOURCE_STALE_AT_CUTOVER", stale.exception.reasons)
        fresh = self.fixture.plan()
        report = self.fixture.cutover(fresh)
        self.assertEqual(report.authoritative, mig.MODE_DB)
        self.assertEqual(report.record_count, 2)


class CrashMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-crash-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "crash")
        self.original = self.fixture.write(
            legacy_state({"dev": legacy_member("dev"), "ceo": legacy_member(
                "ceo", role="ceo", read_only=True, ownership=[])})
        )

    def _raiser(self, stage: str, reached: list[str]) -> Any:
        def hook(current: str) -> None:
            reached.append(current)
            if current == stage:
                raise SimulatedCrash(f"crash at {current}")

        return hook

    def test_crashes_up_to_authority_commit_keep_json_authority(self) -> None:
        """Any crash before the durable authority commit leaves JSON in charge."""

        stages_before_authority = [
            mig.STAGE_HASH_CHECK,
            mig.STAGE_FREEZE_MARKER,
            mig.STAGE_LOCK_ACQUIRE,
            mig.STAGE_QUIESCENCE,
            mig.STAGE_IMPORT_BEGIN,
            mig.STAGE_IMPORT_ROWS,
            mig.STAGE_BEFORE_COMMIT,
            mig.STAGE_AFTER_IMPORT_COMMIT,
        ]
        for stage in stages_before_authority:
            with self.subTest(stage=stage):
                fixture = Fixture(self._tmp.name, f"pre-{stage}")
                before = fixture.write(legacy_state({"dev": legacy_member("dev")}))
                plan = fixture.plan()
                reached: list[str] = []
                with self.assertRaises(SimulatedCrash):
                    fixture.cutover(plan, fault=self._raiser(stage, reached))
                self.assertIn(stage, reached)
                authority = fixture.authority()
                self.assertEqual(authority.source, mig.MODE_JSON)
                self.assertFalse(authority.db_side_effects_permitted)
                # Whatever marker survived is only freeze (never a DB claim).
                marker = mig.read_marker(str(fixture.marker)) if fixture.marker.exists() else None
                if marker is not None:
                    self.assertEqual(marker.mode, mig.MARKER_FREEZE)
                    self.assertTrue(authority.dispatch_paused)
                self.assertEqual(fixture.source_bytes(), before)
                if fixture.db.exists():
                    self.assertEqual(fixture.authority_value(), "json")
                    # The cutover never became durable at any of these stages.
                    self.assertEqual(
                        fixture.count(
                            "SELECT COUNT(*) FROM events WHERE kind = 'migration.cutover.committed'"
                        ),
                        0,
                    )
                    if stage != mig.STAGE_AFTER_IMPORT_COMMIT:
                        self.assertEqual(
                            fixture.count(
                                "SELECT COUNT(*) FROM legacy_migration_rehearsals"
                                " WHERE status = 'imported'"
                            ),
                            0,
                        )
                    else:
                        # A committed shadow import is allowed (disposable read
                        # model) as long as nothing was promoted and JSON is
                        # still the sole writer.
                        self.assertFalse(self._promoted(fixture))
                else:
                    self.assertEqual(authority.reason_code, "NO_DATABASE")

    @staticmethod
    def _promoted(fixture: Fixture) -> bool:
        return (
            fixture.db.exists()
            and fixture.authority_value() == mig.MODE_DB
        )

    def test_crash_between_commit_and_marker_is_split_brain_needs_human(self) -> None:
        for stage in (mig.STAGE_AFTER_COMMIT, mig.STAGE_BEFORE_MARKER_PUBLISH):
            with self.subTest(stage=stage):
                fixture = Fixture(self._tmp.name, f"split-{stage}")
                before = fixture.write(legacy_state({"dev": legacy_member("dev")}))
                plan = fixture.plan()
                reached: list[str] = []
                with self.assertRaises(SimulatedCrash):
                    fixture.cutover(plan, fault=self._raiser(stage, reached))
                authority = fixture.authority()
                self.assertEqual(authority.source, "none")
                self.assertEqual(authority.reason_code, "AUTHORITY_DISAGREEMENT")
                self.assertTrue(authority.needs_human)
                self.assertTrue(authority.dispatch_paused)
                self.assertFalse(authority.json_writes_permitted)
                self.assertFalse(authority.db_side_effects_permitted)
                self.assertEqual(fixture.authority_value(), "sqlite")
                marker = mig.read_marker(str(fixture.marker))
                self.assertEqual(marker.mode, mig.MARKER_FREEZE)
                self.assertEqual(fixture.source_bytes(), before)
                # Recovery: finish the interrupted marker publication under the lock.
                completion = mig.complete_interrupted_cutover(
                    db_path=str(fixture.db),
                    marker_path=str(fixture.marker),
                    lock_path=str(fixture.lock),
                    json_path=str(fixture.state),
                )
                self.assertTrue(completion.completed)
                self.assertEqual(completion.authority.source, mig.MODE_DB)

    def test_crash_after_marker_publish_leaves_consistent_db_authority(self) -> None:
        for stage in (mig.STAGE_AFTER_MARKER_PUBLISH, mig.STAGE_RELEASE_LOCK):
            with self.subTest(stage=stage):
                fixture = Fixture(self._tmp.name, f"post-{stage}")
                before = fixture.write(legacy_state({"dev": legacy_member("dev")}))
                plan = fixture.plan()
                reached: list[str] = []
                with self.assertRaises(SimulatedCrash):
                    fixture.cutover(plan, fault=self._raiser(stage, reached))
                authority = fixture.authority()
                self.assertEqual(authority.source, mig.MODE_DB)
                self.assertEqual(authority.reason_code, "DB_AND_MARKER_AGREE")
                self.assertFalse(authority.needs_human)
                self.assertFalse(authority.json_writes_permitted)
                marker = mig.read_marker(str(fixture.marker))
                self.assertEqual(marker.mode, mig.MARKER_DB_AUTHORITATIVE)
                self.assertEqual(fixture.source_bytes(), before)

    def test_every_cutover_stage_is_instrumented(self) -> None:
        fixture = Fixture(self._tmp.name, "stages")
        fixture.write(legacy_state({"dev": legacy_member("dev")}))
        plan = fixture.plan()
        reached: list[str] = []
        fixture.cutover(plan, fault=lambda stage: reached.append(stage))
        for stage in (
            mig.STAGE_HASH_CHECK,
            mig.STAGE_FREEZE_MARKER,
            mig.STAGE_LOCK_ACQUIRE,
            mig.STAGE_QUIESCENCE,
            mig.STAGE_IMPORT_BEGIN,
            mig.STAGE_IMPORT_ROWS,
            mig.STAGE_BEFORE_COMMIT,
            mig.STAGE_AFTER_IMPORT_COMMIT,
            mig.STAGE_AFTER_COMMIT,
            mig.STAGE_BEFORE_MARKER_PUBLISH,
            mig.STAGE_AFTER_MARKER_PUBLISH,
            mig.STAGE_RELEASE_LOCK,
        ):
            self.assertIn(stage, reached)

    def test_import_crash_leaves_no_partial_rows(self) -> None:
        fixture = Fixture(self._tmp.name, "partial")
        fixture.write(legacy_state({"dev": legacy_member("dev"), "ceo": legacy_member("ceo")}))
        plan = fixture.plan()
        with self.assertRaises(SimulatedCrash):
            mig.run_rehearsal(plan=plan, db_path=str(fixture.db), fault=self._raiser(mig.STAGE_BEFORE_COMMIT, []))
        self.assertEqual(fixture.count("SELECT COUNT(*) FROM tasks"), 0)
        self.assertEqual(fixture.count("SELECT COUNT(*) FROM attempts"), 0)
        self.assertEqual(fixture.count("SELECT COUNT(*) FROM legacy_migration_records"), 0)
        self.assertEqual(fixture.count("SELECT COUNT(*) FROM events"), 0)
        # A later clean rehearsal still works and imports both members.
        report = mig.run_rehearsal(plan=plan, db_path=str(fixture.db))
        self.assertEqual(report.record_count, 2)

    def test_fault_at_planning_stage_is_propagated(self) -> None:
        fixture = Fixture(self._tmp.name, "planning")
        fixture.write(legacy_state())
        with self.assertRaises(SimulatedCrash):
            mig.plan_rehearsal(
                json_path=str(fixture.state),
                fault=lambda stage: (_ for _ in ()).throw(SimulatedCrash(f"crash at {stage}")),
            )

    CHILD_HANG_AT_COMMIT = r"""
import sys, time
from teamcraft import migration as mig
from teamcraft.errors import ValidationError
state_path, lock_path, marker_path, db_path, ready_path = sys.argv[1:6]
plan = mig.plan_rehearsal(
    json_path=state_path,
    current_session={"socket_path": "/synthetic/herdr.sock", "socket_device": 77,
                     "socket_inode": 1001},
)
def hook(stage):
    if stage == mig.STAGE_BEFORE_COMMIT:
        open(ready_path, "w").write(stage)
        time.sleep(30)
print(mig.rehearse_cutover(
    plan=plan, db_path=db_path, lock_path=lock_path, marker_path=marker_path,
    fence=mig.FenceEvidence(writer_modes=(mig.WRITER_GUARDED,),
                           writers_enumerated=True, quiescent=True),
    fault=hook,
).authoritative)
"""

    @no_resource_warnings
    def test_sigkill_during_commit_rolls_back_and_json_stays_authoritative(self) -> None:
        """A process that dies holding the transaction leaves no trace of it."""

        fixture = Fixture(self._tmp.name, "sigkill")
        before = fixture.write(legacy_state({"dev": legacy_member("dev")}))
        ready = fixture.root / "child-in-commit"
        # ``with`` guarantees the child's pipes are closed even on assertion
        # failure, so no unclosed-transport ResourceWarning is emitted.
        with subprocess.Popen(  # fixed interpreter and args, no shell
            [sys.executable, "-c", self.CHILD_HANG_AT_COMMIT, str(fixture.state),
             str(fixture.lock), str(fixture.marker), str(fixture.db), str(ready)],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        ) as child:
            try:
                deadline = time.monotonic() + 30
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "child never reached the commit boundary")
            finally:
                child.kill()
                child.wait(timeout=20)
        self.assertEqual(fixture.count("SELECT COUNT(*) FROM legacy_migration_rehearsals WHERE status = 'imported'"), 0)
        self.assertEqual(fixture.count("SELECT COUNT(*) FROM tasks"), 0)
        authority = fixture.authority()
        self.assertEqual(authority.source, mig.MODE_JSON)
        self.assertFalse(authority.db_side_effects_permitted)
        self.assertTrue(authority.dispatch_paused)  # freeze marker survived the crash
        self.assertEqual(fixture.source_bytes(), before)
        report = mig.storage.verify_database(str(fixture.db))
        self.assertEqual(report["integrity"], "ok")
        self.assertTrue(report["at_head"])


class AuthorityChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-authority-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "authority")
        self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    def _set_db(self, authoritative: str, generation: str | None = "gen-1") -> None:
        connection = mig.open_rehearsal_database(str(self.fixture.db))
        try:
            with mig.storage.transaction(connection):
                connection.execute(
                    "UPDATE legacy_cutover_state SET authoritative = ?, generation = ?,"
                    " source_sha256 = ? WHERE id = 1",
                    (authoritative, generation, "ab" * 32),
                )
        finally:
            connection.close()

    def test_matrix(self) -> None:
        cases: list[tuple[str, str | None, str, str, bool, bool, bool]] = [
            # (db authority, marker mode, expected source, expected reason,
            #  needs_human, json writes permitted, db side effects permitted)
            (mig.MODE_JSON, None, mig.MODE_JSON, "JSON_AUTHORITATIVE", False, True, False),
            (mig.MODE_JSON, mig.MARKER_FREEZE, mig.MODE_JSON, "REFUSED_OR_IN_FLIGHT_FREEZE", False, False, False),
            (mig.MODE_JSON, mig.MARKER_JSON_AUTHORITATIVE, mig.MODE_JSON, "JSON_AUTHORITATIVE", False, True, False),
            (mig.MODE_JSON, mig.MARKER_DB_AUTHORITATIVE, "none", "AUTHORITY_DISAGREEMENT", True, False, False),
            (mig.MODE_DB, mig.MARKER_DB_AUTHORITATIVE, mig.MODE_DB, "DB_AND_MARKER_AGREE", False, False, True),
            (mig.MODE_DB, mig.MARKER_FREEZE, "none", "AUTHORITY_DISAGREEMENT", True, False, False),
            (mig.MODE_DB, None, "none", "AUTHORITY_DISAGREEMENT", True, False, False),
            (mig.MODE_DB, mig.MARKER_JSON_AUTHORITATIVE, "none", "AUTHORITY_DISAGREEMENT", True, False, False),
        ]
        for db_mode, marker_mode, expected_source, expected_reason, needs_human, json_ok, db_ok in cases:
            with self.subTest(db=db_mode, marker=marker_mode):
                self._set_db(db_mode)
                if marker_mode is None:
                    mig.remove_marker(str(self.fixture.marker))
                else:
                    mig.deploy_marker(str(self.fixture.marker), mode=marker_mode, generation="gen-1")
                decision = self.fixture.authority()
                self.assertEqual(decision.source, expected_source)
                self.assertEqual(decision.reason_code, expected_reason)
                self.assertEqual(decision.needs_human, needs_human)
                self.assertEqual(decision.json_writes_permitted, json_ok)
                self.assertEqual(decision.db_side_effects_permitted, db_ok)
                self.assertEqual(decision.dispatch_paused, not (json_ok or db_ok))

    def test_corrupt_marker_stops_with_human_and_no_side_effects(self) -> None:
        self._set_db(mig.MODE_JSON)
        self.fixture.marker.write_bytes(b"\x00\xff not json")
        decision = self.fixture.authority()
        self.assertEqual(decision.source, mig.MODE_JSON)
        self.assertEqual(decision.reason_code, "MARKER_CORRUPT")
        self.assertTrue(decision.needs_human)
        self.assertFalse(decision.json_writes_permitted)
        self.assertFalse(decision.db_side_effects_permitted)

    def test_missing_database_defaults_to_json(self) -> None:
        mig.remove_marker(str(self.fixture.marker))
        decision = self.fixture.authority()
        self.assertEqual(decision.source, mig.MODE_JSON)
        self.assertEqual(decision.reason_code, "NO_DATABASE")
        self.assertFalse(decision.db_side_effects_permitted)
        self.assertFalse(decision.dispatch_paused)

    def test_generation_mismatch_between_db_and_marker_is_not_authoritative(self) -> None:
        self._set_db(mig.MODE_DB, generation="gen-1")
        mig.deploy_marker(str(self.fixture.marker), mode=mig.MARKER_DB_AUTHORITATIVE, generation="gen-2")
        decision = self.fixture.authority()
        self.assertEqual(decision.source, "none")
        self.assertEqual(decision.reason_code, "AUTHORITY_GENERATION_MISMATCH")
        self.assertTrue(decision.needs_human)
        self.assertFalse(decision.json_writes_permitted)
        self.assertFalse(decision.db_side_effects_permitted)


class RollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-rollback-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "rollback")
        self.original = self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    def test_rollback_returns_authority_to_json_without_touching_json(self) -> None:
        plan = self.fixture.plan()
        report = self.fixture.cutover(plan)
        self.assertEqual(report.authoritative, mig.MODE_DB)
        result = mig.rollback_to_json(
            db_path=str(self.fixture.db),
            marker_path=str(self.fixture.marker),
            generation=plan.generation,
        )
        self.assertEqual(result.authoritative, mig.MODE_JSON)
        self.assertEqual(result.marker_mode, mig.MARKER_JSON_AUTHORITATIVE)
        self.assertEqual(self.fixture.source_bytes(), self.original)
        decision = self.fixture.authority()
        self.assertEqual(decision.source, mig.MODE_JSON)
        self.assertTrue(decision.json_writes_permitted)
        self.assertFalse(decision.db_side_effects_permitted)
        # The legacy entry point resumes writing (guard allows under the lock).
        self.assertTrue(self.fixture.guard_allows_legacy_write(plan.generation).allow)
        # Shadow DB retained as evidence, nothing flushed into the JSON.
        self.assertGreaterEqual(self.fixture.count("SELECT COUNT(*) FROM legacy_migration_records"), 1)
        self.assertIn("migration.rollback.to_json", self.fixture.event_kinds())
        self.assertEqual(self.fixture.source_bytes(), self.original)

    def test_rollback_requires_a_confirmed_db_authority_and_matching_generation(self) -> None:
        plan = self.fixture.plan()
        with self.assertRaises(mig.RollbackBlocked):
            mig.rollback_to_json(
                db_path=str(self.fixture.db), marker_path=str(self.fixture.marker),
                generation=plan.generation,
            )
        self.fixture.cutover(plan)
        with self.assertRaises(mig.RollbackBlocked):
            mig.rollback_to_json(
                db_path=str(self.fixture.db), marker_path=str(self.fixture.marker),
                generation="gen-not-mine",
            )

    def test_complete_interrupted_cutover_refuses_when_source_moved(self) -> None:
        plan = self.fixture.plan()
        reached: list[str] = []
        with self.assertRaises(SimulatedCrash):
            self.fixture.cutover(
                plan,
                fault=lambda stage: (
                    reached.append(stage)
                    or (stage == mig.STAGE_AFTER_COMMIT and (_ for _ in ()).throw(SimulatedCrash("boom")))
                ),
            )
        # An old writer changed the JSON after the commit window opened.
        self.fixture.write(legacy_state({"dev": legacy_member("dev"), "late": legacy_member("late")}))
        completion = mig.complete_interrupted_cutover(
            db_path=str(self.fixture.db),
            marker_path=str(self.fixture.marker),
            lock_path=str(self.fixture.lock),
            json_path=str(self.fixture.state),
        )
        self.assertFalse(completion.completed)
        self.assertEqual(completion.reason_code, "SOURCE_CHANGED_SINCE_COMMIT")
        self.assertEqual(completion.authority.source, "none")
        self.assertTrue(completion.authority.needs_human)


class PostCutoverWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-watch-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "watch")
        self.original = self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    def test_clean_watch_reports_no_change(self) -> None:
        plan = self.fixture.plan()
        self.fixture.cutover(plan)
        with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=2.0) as lock:
            observation = mig.observe_post_cutover(
                db_path=str(self.fixture.db), json_path=str(self.fixture.state),
                marker_path=str(self.fixture.marker), lock=lock,
            )
        self.assertEqual(observation.verdict, "clean")
        self.assertFalse(observation.dispatch_paused)
        self.assertNotIn("migration.stale_writer_detected", self.fixture.event_kinds())

    def test_stale_json_change_pauses_dispatch_and_is_never_merged(self) -> None:
        plan = self.fixture.plan()
        self.fixture.cutover(plan)
        tasks_before = self.fixture.count("SELECT COUNT(*) FROM tasks")
        records_before = self.fixture.count("SELECT COUNT(*) FROM legacy_migration_records")
        # A loaded old writer that ignores markers mutates the JSON after cutover.
        self.fixture.write(legacy_state({"dev": legacy_member("dev"), "ghost": legacy_member("ghost")}))
        with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=2.0) as lock:
            observation = mig.observe_post_cutover(
                db_path=str(self.fixture.db), json_path=str(self.fixture.state),
                marker_path=str(self.fixture.marker), lock=lock,
            )
        self.assertEqual(observation.verdict, "stale_writer")
        self.assertTrue(observation.dispatch_paused)
        self.assertTrue(observation.requires_human_reconciliation)
        self.assertEqual(observation.recorded_sha256, self.original and plan.source_sha256)
        self.assertNotEqual(observation.current_sha256, plan.source_sha256)
        self.assertIn("migration.stale_writer_detected", self.fixture.event_kinds())
        self.assertEqual(
            self.fixture.count("SELECT dispatch_paused FROM legacy_migration_rehearsals WHERE generation = ?", (plan.generation,)),
            1,
        )
        # Nothing was merged from JSON into the DB, and nothing was written back.
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM tasks"), tasks_before)
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM legacy_migration_records"), records_before
        )
        self.assertEqual(self.fixture.count("SELECT COUNT(*) FROM commands"), 0)
        self.assertIn("ghost", self.fixture.source_bytes().decode("utf-8"))

    def test_source_instability_during_rehearsal_marks_generation_refused(self) -> None:
        """An old writer landing a change mid-import aborts the rehearsal."""

        plan = self.fixture.plan()
        migrated = mig.open_rehearsal_database(str(self.fixture.db))
        migrated.close()

        def hook(stage: str) -> None:
            if stage == mig.STAGE_AFTER_IMPORT_COMMIT:
                # Simulated loaded old writer: changes JSON after our commit.
                self.fixture.write(
                    legacy_state({"dev": legacy_member("dev"), "ghost": legacy_member("ghost")})
                )

        with self.assertRaises(mig.CutoverRefused) as ctx:
            mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db), fault=hook)
        self.assertIn("SOURCE_UNSTABLE", ctx.exception.reasons)
        self.assertEqual(ctx.exception.authoritative, mig.MODE_JSON)
        self.assertEqual(
            self.fixture.value(
                "SELECT status FROM legacy_migration_rehearsals WHERE generation = ?",
                (plan.generation,),
            ),
            "refused",
        )
        self.assertIn("migration.rehearsal.aborted", self.fixture.event_kinds())
        self.assertEqual(self.fixture.authority_value(), "json")
        self.assertFalse(self.fixture.authority().db_side_effects_permitted)

    def test_watch_without_any_database_is_not_a_stale_writer(self) -> None:
        with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=2.0) as lock:
            observation = mig.observe_post_cutover(
                db_path=str(self.fixture.work / "never-created.db"),
                json_path=str(self.fixture.state),
                marker_path=str(self.fixture.marker),
                lock=lock,
            )
        self.assertEqual(observation.verdict, "not_cutover")
        self.assertEqual(observation.action, "no_rehearsal_database")
        self.assertFalse(observation.requires_human_reconciliation)

    def test_watch_before_cutover_is_not_a_stale_writer(self) -> None:
        plan = self.fixture.plan()
        mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        with mig.LegacyStateLock(str(self.fixture.lock), timeout_seconds=2.0) as lock:
            observation = mig.observe_post_cutover(
                db_path=str(self.fixture.db), json_path=str(self.fixture.state),
                marker_path=str(self.fixture.marker), lock=lock,
            )
        self.assertEqual(observation.verdict, "not_cutover")
        self.assertFalse(observation.requires_human_reconciliation)


class DualProcessTests(unittest.TestCase):
    """Real second-process writers: cross-process flock + refusal, no cutover."""

    CHILD = r"""
import fcntl, json, os, sys, time
lock_path, state_path, locked_path, written_path, go_path = sys.argv[1:6]
handle = open(lock_path, "a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
open(locked_path, "w").write("locked")
deadline = time.monotonic() + 20
while time.monotonic() < deadline and not os.path.exists(go_path):
    time.sleep(0.01)
open(state_path, "w").write(json.dumps({
    "version": 1, "team_id": "team-synthetic-1", "project_root": "/synthetic/project",
    "herdr_session": {"socket_path": %r, "socket_device": %d, "socket_inode": %d},
    "created_at": 1700000000,
    "members": {"dev": {"name": "dev", "role": "developer", "kind": "codex",
                         "task": "late write", "ownership": ["src/dev.py"], "read_only": False,
                         "phase": "prompt_submitted", "live_state": "working"}},
}, indent=2, sort_keys=True) + "\n")
open(written_path, "w").write("written")
fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m106-proc-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name, "dual")
        self.original = self.fixture.write(legacy_state({"dev": legacy_member("dev")}))

    @staticmethod
    def _wait_for(path: Path, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return False

    @no_resource_warnings
    def test_cross_process_writer_blocks_lock_and_forces_refusal(self) -> None:
        locked = self.fixture.root / "child-locked"
        written = self.fixture.root / "child-written"
        go = self.fixture.root / "child-go"
        plan = self.fixture.plan()  # captured before the other process touches anything
        # ``with`` guarantees the child's pipes are closed even on assertion
        # failure, so no unclosed-transport ResourceWarning is emitted.
        with subprocess.Popen(  # fixed interpreter and args, no shell
            [sys.executable, "-c", self.CHILD % (
                CURRENT_SESSION["socket_path"], CURRENT_SESSION["socket_device"],
                CURRENT_SESSION["socket_inode"],
            ), str(self.fixture.lock), str(self.fixture.state), str(locked), str(written),
             str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as child:
            try:
                self.assertTrue(self._wait_for(locked), "child writer never took the lock")
                # Another process holds the legacy lock: we cannot prove it is idle,
                # so the switch must be refused and the freeze lifted.
                with self.assertRaises(mig.CutoverRefused) as ctx:
                    self.fixture.cutover(plan, lock_timeout_seconds=0.1)
                self.assertIn("LEGACY_LOCK_UNAVAILABLE", ctx.exception.reasons)
                authority = self.fixture.authority()
                self.assertEqual(authority.source, mig.MODE_JSON)
                self.assertFalse(authority.db_side_effects_permitted)
                self.assertFalse(self.fixture.marker.exists())
                # We could not fence it, and we never wrote the JSON ourselves.
                self.assertEqual(self.fixture.source_bytes(), self.original)
                go.touch()  # let the unfenceable writer do its late write and exit
                self.assertTrue(self._wait_for(written), "child writer never wrote the JSON")
            finally:
                go.touch()
                child.wait(timeout=25)
        # The other process wrote while we could not fence it, so the recorded
        # plan is stale forever: only a re-plan against the new bytes may switch.
        self.assertNotEqual(self.fixture.source_bytes(), self.original)
        with self.assertRaises(mig.CutoverRefused) as stale:
            self.fixture.cutover(plan)
        self.assertIn("SOURCE_STALE_AT_CUTOVER", stale.exception.reasons)
        self.assertEqual(self.fixture.authority().source, mig.MODE_JSON)
        self.assertEqual(self.fixture.authority_value(), "json")
        # JSON remains the sole writer; the re-planned rehearsal stays shadow-only.
        fresh = self.fixture.plan()
        report = mig.run_rehearsal(plan=fresh, db_path=str(self.fixture.db))
        self.assertEqual(report.authoritative, mig.MODE_JSON)
        self.assertTrue(report.db_is_shadow)

    def test_two_threads_racing_the_cutover_produce_exactly_one_promotion(self) -> None:
        plan = self.fixture.plan()
        mig.run_rehearsal(plan=plan, db_path=str(self.fixture.db))
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            try:
                barrier.wait(timeout=10)
                report = self.fixture.cutover(plan, lock_timeout_seconds=10.0)
                outcomes.append(f"{name}:{report.outcome}")
            except mig.CutoverRefused as exc:
                outcomes.append(f"{name}:refused:{','.join(exc.reasons)}")
            except Exception as exc:  # noqa: BLE001 - surfaced in assertions below
                outcomes.append(f"{name}:error:{exc}")

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual([item for item in outcomes if "error" in item], [])
        promotions = [item for item in outcomes if item.endswith("rehearsed_db_authoritative")]
        self.assertEqual(len(promotions), 1, outcomes)
        # Exactly one durable authority event, exactly one imported rehearsal row.
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM events WHERE kind = 'migration.cutover.committed'"),
            1,
        )
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM legacy_migration_rehearsals WHERE status = 'imported'"),
            1,
        )
        self.assertEqual(
            self.fixture.count("SELECT COUNT(*) FROM legacy_migration_rehearsals"), 1
        )
        authority = self.fixture.authority()
        self.assertEqual(authority.source, mig.MODE_DB)
        self.assertEqual(authority.generation, plan.generation)
        self.assertEqual(self.fixture.source_bytes(), self.original)


class NoExternalSideEffectTests(unittest.TestCase):
    def test_module_never_reaches_outside_the_local_filesystem(self) -> None:
        """Rehearsal must not shell out, contact Herdr/GitHub, or send prompts."""

        source = Path(mig.__file__).read_text(encoding="utf-8")
        banned = (
            "import subprocess", "import socket", "import http", "import urllib",
            "import smtplib", "import multiprocessing", "import signal", "import pty",
            "os.system", "os.popen", "socket.", "urlopen", "run_herdr", "requests.",
            "--pane", "agent prompt",
        )
        found = [token for token in banned if token in source]
        self.assertEqual(found, [])

    def test_reports_declare_no_external_side_effects_and_shadow_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="teamcraft-m106-effect-") as temporary:
            fixture = Fixture(temporary, "effects")
            fixture.write(legacy_state({"dev": legacy_member("dev")}))
            plan = fixture.plan()
            report = mig.run_rehearsal(plan=plan, db_path=str(fixture.db))
            self.assertEqual(report.external_side_effects, ())
            self.assertTrue(report.db_is_shadow)
            payload = json.loads(str(fixture.rows(
                "SELECT payload_json FROM events WHERE kind = 'migration.rehearsal.imported'"
            )[0][0]))
            self.assertTrue(payload["shadow_only"])
            self.assertEqual(payload["authoritative"], mig.MODE_JSON)


if __name__ == "__main__":
    unittest.main()
