"""M1-05 Event/Command atomicity contract tests.

All databases live inside ``tempfile.TemporaryDirectory``.  Nothing here
reads, writes, or migrates the repository's real runtime directories
(``.teamcraft`` included), and no Herdr session is contacted.

Pinned contracts (M0 review 9.1 M1-05 rows; no external orchestration —
Herdr/GitHub are NOT exactly-once and this milestone records/arbitrates
commands only):

* append-only events: the seq of COMMITTED events is persistent and strictly
  increasing (readers never see reordering or regression); rolled-back
  events are invisible; AUTOINCREMENT makes no gap-free promise — a client
  that sees a gap may safely refetch/snapshot, but must not assume the
  cause.  DB triggers abort UPDATE/DELETE on ``events`` and DELETE on
  ``commands``;
* payload hygiene: valid JSON, bounded size, lowercase kind identifiers —
  refusals write nothing;
* idempotency: same key + identical (kind, payload, evidence) REUSES the
  stored command (including a confirmed command's ``result_json``) and
  appends no duplicate event; same key with different content raises
  ``IdempotencyConflictError`` with zero side effects;
* command state machine pending/running/confirmed/unknown/expired as
  compare-and-swap: illegal edges refused with structured reason codes
  (``TERMINAL_FROZEN``, ``ILLEGAL_TRANSITION``), CAS losers get
  ``CommandStateConflictError``, and two racing claims leave exactly one
  winner; UNKNOWN closes ONLY via reconcile with a mandatory audit note and
  is never auto-retried;
* TTL: expired pendings refuse claims with ``TTL_EXPIRED`` and move to
  ``expired`` through the sweep (with ``TTL_EXPIRED`` / ``EVIDENCE_STALE``
  reasons); RUNNING commands are never swept — in-flight side effects must
  reconcile (8.2-11);
* evidence binding: a Task CAS bump atomically invalidates PENDING commands
  bound to an older version of that Task (domain wiring); a bump around the
  hook (raw storage-level write) leaves the command pending but CLAIM is
  refused with ``EVIDENCE_STALE``/``EVIDENCE_TARGET_GONE`` under the lock;
* atomicity: state change + event + command commit or roll back together —
  fault injection proves there is never state without its event/command nor
  a command/event without its state, even mid outer transaction;
* lagging v1/v2 schemas raise ``SchemaOutOfDateError`` for the event and
  command services while storage-level contracts keep working.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teamcraft import commands, domain, events, storage
from teamcraft.errors import (
    CommandStateConflictError,
    IdempotencyConflictError,
    IllegalTransitionError,
    NotFoundError,
    SchemaOutOfDateError,
    ValidationError,
)

class FixedClock:
    """Deterministic injectable UTC clock."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: int = 60) -> None:
        self.t = self.t + timedelta(seconds=seconds)


class EventStoreM105Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m105-ev-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"
        report = storage.migrate_to_head(self.db)
        self.assertTrue(report.fresh)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc))

    # -- seq monotonicity -------------------------------------------------------

    def test_seq_is_monotonic_and_persisted_across_reconnect(self) -> None:
        e1 = events.append_event(self.conn, "watch.process_started", clock=self.clock)
        self.clock.advance()
        e2 = events.append_event(self.conn, "watch.agent_active", clock=self.clock)
        self.assertEqual((e1.seq, e2.seq), (1, 2))
        self.assertEqual(events.current_seq(self.conn), 2)
        self.conn.close()
        self.conn = storage.connect(self.db)  # durability across reopen
        self.assertEqual(events.current_seq(self.conn), 2)
        e3 = events.append_event(self.conn, "watch.process_exited", clock=self.clock)
        self.assertEqual(e3.seq, 3)
        streamed = events.events_after(self.conn, 0, limit=10)
        self.assertEqual([e.seq for e in streamed], [1, 2, 3])
        self.assertEqual([e.kind for e in streamed],
                         ["watch.process_started", "watch.agent_active",
                          "watch.process_exited"])
        # ``after`` pagination (M3-03 backfill shape)
        self.assertEqual([e.seq for e in events.events_after(self.conn, 2, limit=10)],
                         [3])

    def test_rolled_back_events_are_invisible_and_committed_seq_keeps_increasing(
        self,
    ) -> None:
        """Committed-stream contract (verified against SQLite): a rolled-back
        event never becomes visible, and the next committed seq is strictly
        greater than the last committed seq.  (SQLite MAY reclaim an
        uncommitted number — no gap permanence is claimed.)"""
        e1 = events.append_event(self.conn, "a.first", clock=self.clock)
        with self.assertRaises(RuntimeError):
            with storage.transaction(self.conn):
                events.append_event(self.conn, "a.victim", clock=self.clock)
                raise RuntimeError("crash before commit")
        self.assertEqual(events.current_seq(self.conn), 1)  # victim invisible
        e3 = events.append_event(self.conn, "a.after-crash", clock=self.clock)
        self.assertGreater(e3.seq, e1.seq)  # strictly increasing among committed
        self.assertEqual(events.current_seq(self.conn), e3.seq)
        kinds = [e.kind for e in events.events_after(self.conn, 0, limit=10)]
        self.assertEqual(kinds, ["a.first", "a.after-crash"])
        self.conn.close()
        self.conn = storage.connect(self.db)  # and it stays durable across reopen
        self.assertEqual(events.current_seq(self.conn), e3.seq)

    # -- append-only enforcement --------------------------------------------------

    def test_update_and_delete_are_db_rejected(self) -> None:
        events.append_event(self.conn, "audit.keepme", clock=self.clock)
        self.conn.commit() if self.conn.in_transaction else None
        raw = sqlite3.connect(str(self.db))
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            raw.execute("UPDATE events SET kind = 'tampered'")
        self.assertIn("append-only", str(ctx.exception))
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM events")
        self.assertEqual(raw.execute("SELECT kind FROM events").fetchone()[0],
                         "audit.keepme")

    # -- hygiene ------------------------------------------------------------------

    def test_payload_and_kind_validation_writes_nothing(self) -> None:
        with self.assertRaises(ValidationError):  # invalid JSON
            events.append_event(self.conn, "x.y", payload_json="{oops",
                               clock=self.clock)
        with self.assertRaises(ValidationError):  # oversized payload
            events.append_event(
                self.conn, "x.big",
                payload_json='{"d": "' + "x" * events.MAX_EVENT_PAYLOAD_BYTES + '"}',
                clock=self.clock,
            )
        with self.assertRaises(ValidationError):  # uppercase / illegal kind
            events.append_event(self.conn, "Task.Done", clock=self.clock)
        with self.assertRaises(ValidationError):  # blank kind
            events.append_event(self.conn, "   ", clock=self.clock)
        with self.assertRaises(ValidationError):  # over-long kind
            events.append_event(self.conn, "k." + "x" * events.MAX_EVENT_KIND_LENGTH,
                              clock=self.clock)
        self.assertEqual(events.current_seq(self.conn), 0)

    def test_association_and_lookup(self) -> None:
        pid = domain.create_project(self.conn, "p", "/repo/p", clock=self.clock).id
        task = domain.create_task(self.conn, pid, "t", clock=self.clock)
        e = events.append_event(
            self.conn, "task.created", project_id=pid, task_id=task.id,
            payload_json='{"title": "t"}', clock=self.clock,
        )
        self.assertEqual((e.project_id, e.task_id), (pid, task.id))
        self.assertEqual(events.get_event(self.conn, e.event_id), e)
        with self.assertRaises(NotFoundError):
            events.get_event(self.conn, "nope-not-here")
        with self.assertRaises(ValidationError):
            events.events_after(self.conn, -1)
        with self.assertRaises(ValidationError):
            events.events_after(self.conn, 0, limit=0)
        with self.assertRaises(ValidationError):
            events.events_after(self.conn, 0, limit=events.MAX_EVENT_PAGE_SIZE + 1)

    # -- atomicity with state -------------------------------------------------------

    def test_event_and_state_change_commit_together(self) -> None:
        pid = domain.create_project(self.conn, "p", "/repo/p", clock=self.clock).id
        task = domain.create_task(self.conn, pid, "t", clock=self.clock)
        with storage.transaction(self.conn):
            domain.recompute_ready(self.conn, clock=self.clock)
            after = domain.get_task(self.conn, task.id)
            events.append_event(
                self.conn, "task.ready", task_id=task.id,
                payload_json=json.dumps({"version": after.version}),
                clock=self.clock,
            )
        self.assertEqual(domain.get_task(self.conn, task.id).status, "ready")
        self.assertEqual(len(events.events_after(self.conn, 0, limit=10)), 1)

    def test_fault_injection_never_leaves_state_without_event(self) -> None:
        """Core M1-05 acceptance: crash anywhere in a compound write leaves
        NEITHER the state change NOR the event (no half commit)."""
        pid = domain.create_project(self.conn, "p", "/repo/p", clock=self.clock).id
        task = domain.create_task(self.conn, pid, "t", clock=self.clock)
        with self.assertRaises(RuntimeError):
            with storage.transaction(self.conn):
                domain.recompute_ready(self.conn, clock=self.clock)
                events.append_event(self.conn, "task.ready", task_id=task.id,
                                  clock=self.clock)
                raise RuntimeError("crash between state and event")
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.status, after.version), ("pending", 0))
        self.assertEqual(events.current_seq(self.conn), 0)

    def test_event_store_refuses_lagging_schema(self) -> None:
        raw = sqlite3.connect(str(self.db))
        raw.execute("DROP TABLE events")
        raw.execute("DROP TABLE commands")
        raw.execute("DELETE FROM schema_migrations WHERE version > 1")
        raw.execute("PRAGMA user_version=1")
        raw.commit()
        raw.close()
        lagging = storage.connect(self.db)
        self.addCleanup(lagging.close)
        with self.assertRaises(SchemaOutOfDateError):
            events.append_event(lagging, "task.created", clock=self.clock)
        with self.assertRaises(SchemaOutOfDateError):
            events.current_seq(lagging)
        # storage-level contracts still hold at v1 (forward compatibility):
        with storage.transaction(lagging):
            lagging.execute(
                "INSERT INTO projects (id, name, repo_path, created_at)"
                " VALUES ('p-v1', 'v1', '/v1', '2026-11-01T00:00:00+00:00')"
            )


class CommandServiceM105Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamcraft-m105-cmd-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teamcraft.db"
        report = storage.migrate_to_head(self.db)
        self.assertTrue(report.fresh)
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc))
        self.pid = domain.create_project(
            self.conn, "proj", "/repo/proj", clock=self.clock
        ).id

    # -- helpers ---------------------------------------------------------------

    def ready_task(self, title: str, **kwargs) -> domain.Task:
        self.clock.advance()
        task = domain.create_task(self.conn, self.pid, title, clock=self.clock,
                                 **kwargs)
        domain.recompute_ready(self.conn, clock=self.clock)
        return domain.get_task(self.conn, task.id)

    def evidence(self, task: domain.Task) -> str:
        return json.dumps({"task_id": task.id, "task_version": task.version},
                         sort_keys=True)

    def submit(self, key: str = "k1", task: domain.Task | None = None,
               payload: str = '{"a": 1}', ttl: int | None = None,
               **kwargs) -> commands.Submission:
        task = task if task is not None else self.ready_task(f"cmd-{key}")
        return commands.submit_command(
            self.conn, idempotency_key=key, kind=kwargs.pop("kind", "task.pause"),
            payload_json=payload, evidence_json=self.evidence(task),
            ttl_seconds=ttl, clock=self.clock, **kwargs,
        )

    def _kind_count(self, kind: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)
        ).fetchone()[0]

    # -- submit / idempotency matrix ----------------------------------------------

    def test_submit_persists_pending_command_and_event_atomically(self) -> None:
        sub = self.submit(ttl=300)
        cmd = sub.command
        self.assertTrue(sub.created)
        self.assertEqual(cmd.state, "pending")
        self.assertEqual(cmd.idempotency_key, "k1")
        self.assertIsNotNone(cmd.expires_at)
        self.assertEqual(self._kind_count("command.submitted"), 1)

    def test_same_key_same_payload_reuses_stored_result(self) -> None:
        task = self.ready_task("reuse")
        sub = self.submit(task=task)
        commands.claim_command(self.conn, sub.command.id, clock=self.clock)
        done = commands.confirm_command(
            self.conn, sub.command.id, result_json='{"ok": true, "n": 7}',
            clock=self.clock,
        )
        self.assertEqual(done.state, "confirmed")
        self.clock.advance(3600)
        replay = self.submit(task=task)  # byte-identical request, long after
        self.assertFalse(replay.created)
        self.assertTrue(replay.reused)
        self.assertEqual(replay.command.id, sub.command.id)
        self.assertEqual(replay.command.state, "confirmed")
        self.assertEqual(replay.command.result_json, '{"ok": true, "n": 7}')
        self.assertEqual(self._kind_count("command.submitted"), 1)  # no dupes
        self.assertEqual(self._kind_count("command.claimed"), 1)
        self.assertEqual(self._kind_count("command.confirmed"), 1)

    def test_same_key_different_payload_conflicts_without_side_effects(self) -> None:
        task = self.ready_task("idem")
        sub = self.submit(payload='{"a": 1}', task=task)
        variants = (
            dict(payload_json='{"a": 2}'),                      # payload drift
            dict(kind="task.cancel"),                           # kind drift
            dict(evidence_json=json.dumps(                      # evidence drift
                {"task_id": task.id, "task_version": 0}, sort_keys=True)),
        )
        for variant in variants:
            request = dict(
                idempotency_key="k1", kind="task.pause",
                payload_json='{"a": 1}', evidence_json=self.evidence(task),
                clock=self.clock,
            )
            request.update(variant)
            with self.assertRaises(IdempotencyConflictError) as ctx:
                commands.submit_command(self.conn, **request)
            self.assertEqual(ctx.exception.existing_command_id, sub.command.id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 1
        )
        self.assertEqual(self._kind_count("command.submitted"), 1)

    def test_submit_input_validation(self) -> None:
        task = self.ready_task("valid")
        evidence = self.evidence(task)
        clock = self.clock
        with self.assertRaises(ValidationError):  # blank key
            commands.submit_command(
                self.conn, idempotency_key="  ", kind="task.pause",
                payload_json="{}", evidence_json=evidence, clock=clock)
        with self.assertRaises(ValidationError):  # over-long key
            commands.submit_command(
                self.conn, idempotency_key="k" * 257, kind="task.pause",
                payload_json="{}", evidence_json=evidence, clock=clock)
        with self.assertRaises(ValidationError):  # invalid JSON payload
            commands.submit_command(
                self.conn, idempotency_key="k", kind="task.pause",
                payload_json="[", evidence_json=evidence, clock=clock)
        with self.assertRaises(ValidationError):  # ttl zero
            commands.submit_command(
                self.conn, idempotency_key="k", kind="task.pause",
                payload_json="{}", evidence_json=evidence, ttl_seconds=0,
                clock=clock)
        with self.assertRaises(ValidationError):  # ttl beyond one month
            commands.submit_command(
                self.conn, idempotency_key="k", kind="task.pause",
                payload_json="{}", evidence_json=evidence,
                ttl_seconds=commands.MAX_TTL_SECONDS + 1, clock=clock)
        with self.assertRaises(ValidationError):  # task_id without task_version
            commands.submit_command(
                self.conn, idempotency_key="k", kind="task.pause",
                payload_json="{}", evidence_json=json.dumps({"task_id": task.id}),
                clock=clock)
        with self.assertRaises(ValidationError):  # bool masquerading as version
            commands.submit_command(
                self.conn, idempotency_key="k", kind="task.pause",
                payload_json="{}",
                evidence_json=json.dumps(
                    {"task_id": task.id, "task_version": True}),
                clock=clock)
        with self.assertRaises(ValidationError):  # unknown evidence key
            commands.submit_command(
                self.conn, idempotency_key="k", kind="task.pause",
                payload_json="{}", evidence_json='{"vibe": "checked"}',
                clock=clock)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0
        )

    # -- state machine CAS ----------------------------------------------------------

    def test_legal_lifecycle_writes_event_per_edge(self) -> None:
        sub = self.submit()
        cid = sub.command.id
        c = commands.claim_command(self.conn, cid, clock=self.clock)
        self.assertEqual(c.state, "running")
        c = commands.confirm_command(self.conn, cid, result_json='{"x": 1}',
                                    clock=self.clock)
        self.assertEqual((c.state, c.result_json), ("confirmed", '{"x": 1}'))
        for kind in ("command.submitted", "command.claimed", "command.confirmed"):
            self.assertEqual(self._kind_count(kind), 1)
        # every event references the bound task
        rows = self.conn.execute(
            "SELECT DISTINCT task_id FROM events"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_double_claim_exactly_one_wins_across_connections(self) -> None:
        sub = self.submit()
        conn2 = storage.connect(self.db)
        self.addCleanup(conn2.close)
        winner = commands.claim_command(self.conn, sub.command.id, clock=self.clock)
        with self.assertRaises(CommandStateConflictError) as ctx:
            commands.claim_command(conn2, sub.command.id, clock=self.clock)
        self.assertEqual((ctx.exception.expected_state, ctx.exception.found_state),
                         ("pending", "running"))
        self.assertEqual(winner.state, "running")

    def test_illegal_edges_and_terminal_freeze(self) -> None:
        sub = self.submit()
        cid = sub.command.id
        with self.assertRaises(IllegalTransitionError) as ctx:
            commands.confirm_command(self.conn, cid, clock=self.clock)  # pending
        self.assertEqual(ctx.exception.reason_code, "ILLEGAL_TRANSITION")
        self.assertEqual(ctx.exception.entity, "command")
        commands.claim_command(self.conn, cid, clock=self.clock)
        with self.assertRaises(CommandStateConflictError):
            commands.claim_command(self.conn, cid, clock=self.clock)  # CAS loser
        commands.confirm_command(self.conn, cid, clock=self.clock)
        for call in (
            lambda: commands.claim_command(self.conn, cid, clock=self.clock),
            lambda: commands.confirm_command(self.conn, cid, clock=self.clock),
            lambda: commands.mark_command_unknown(self.conn, cid, note="?",
                                                clock=self.clock),
            lambda: commands.expire_command(self.conn, cid, clock=self.clock),
            lambda: commands.reconcile_command(self.conn, cid, outcome="confirmed",
                                              note="?", clock=self.clock),
        ):
            with self.assertRaises(IllegalTransitionError) as ctx:
                call()
            self.assertEqual(ctx.exception.reason_code, "TERMINAL_FROZEN")

    def test_unknown_requires_note_and_closes_only_via_reconcile(self) -> None:
        sub = self.submit()
        cid = sub.command.id
        with self.assertRaises(ValidationError):  # audit note mandatory
            commands.mark_command_unknown(self.conn, cid, note="  ",
                                        clock=self.clock)
        commands.claim_command(self.conn, cid, clock=self.clock)
        c = commands.mark_command_unknown(self.conn, cid, note="pane vanished",
                                         clock=self.clock)
        self.assertEqual(c.state, "unknown")
        self.assertEqual(c.note, "pane vanished")
        for call in (
            lambda: commands.claim_command(self.conn, cid, clock=self.clock),
            lambda: commands.confirm_command(self.conn, cid, clock=self.clock),
            lambda: commands.expire_command(self.conn, cid, clock=self.clock),
        ):
            with self.assertRaises(IllegalTransitionError):
                call()  # unknown is NOT the ordinary path (8.2-11)
        settled = commands.reconcile_command(
            self.conn, cid, outcome="confirmed",
            result_json='{"observed": "commit exists"}',
            note="git log shows the push", clock=self.clock,
        )
        self.assertEqual(settled.state, "confirmed")
        self.assertEqual(settled.result_json, '{"observed": "commit exists"}')
        self.assertEqual(self._kind_count("command.reconciled"), 1)

    def test_reconcile_expired_and_argument_rules(self) -> None:
        sub = self.submit()
        cid = sub.command.id
        commands.claim_command(self.conn, cid, clock=self.clock)
        commands.mark_command_unknown(self.conn, cid, note="sigkill suspected",
                                    clock=self.clock)
        with self.assertRaises(ValidationError):  # bad outcome
            commands.reconcile_command(self.conn, cid, outcome="running",
                                     note="n", clock=self.clock)
        with self.assertRaises(ValidationError):  # expired carries no result
            commands.reconcile_command(self.conn, cid, outcome="expired",
                                     note="n", result_json='{"nope": 1}',
                                     clock=self.clock)
        with self.assertRaises(ValidationError):  # missing note
            commands.reconcile_command(self.conn, cid, outcome="expired",
                                     note="", clock=self.clock)
        settled = commands.reconcile_command(
            self.conn, cid, outcome="expired",
            note="remote has no new commit; side effect did not happen",
            clock=self.clock,
        )
        self.assertEqual(settled.state, "expired")
        # reconcile from pending is illegal (nothing was ever executed)
        other = self.submit(key="k-reconcile-pending")
        with self.assertRaises(IllegalTransitionError):
            commands.reconcile_command(self.conn, other.command.id,
                                     outcome="confirmed", note="n",
                                     clock=self.clock)

    def test_note_byte_cap_rejects_without_side_effects(self) -> None:
        """Every persisted note/reason honours MAX_COMMAND_NOTE_BYTES; the
        refusal happens before the transaction opens, so neither command
        state nor the event stream is touched."""
        cap = commands.MAX_COMMAND_NOTE_BYTES
        huge = "x" * (cap + 1)
        wide = "✓" * cap  # cap CHARACTERS but 2*cap UTF-8 bytes: bytes rule
        sub = self.submit()
        cid = sub.command.id
        commands.claim_command(self.conn, cid, clock=self.clock)
        # confirm's OPTIONAL note: oversized refusals change nothing
        for bad in (huge, wide):
            with self.assertRaises(ValidationError):
                commands.confirm_command(self.conn, cid, result_json="{}",
                                       note=bad, clock=self.clock)
            self.assertEqual(commands.get_command(self.conn, cid).state,
                             "running")
            self.assertEqual(self._kind_count("command.confirmed"), 0)
        # mark_unknown over cap: still running, no unknown event
        with self.assertRaises(ValidationError):
            commands.mark_command_unknown(self.conn, cid, note=huge,
                                        clock=self.clock)
        self.assertEqual(commands.get_command(self.conn, cid).state, "running")
        self.assertEqual(self._kind_count("command.unknown"), 0)
        # settle to unknown; reconcile refusal keeps it unknown, no event
        commands.mark_command_unknown(self.conn, cid, note="transient",
                                    clock=self.clock)
        with self.assertRaises(ValidationError):
            commands.reconcile_command(self.conn, cid, outcome="expired",
                                     note=huge, clock=self.clock)
        self.assertEqual(commands.get_command(self.conn, cid).state, "unknown")
        self.assertEqual(self._kind_count("command.reconciled"), 0)
        # expire's reason over cap: still pending, no expired event
        other = self.submit(key="k-note-cap")
        with self.assertRaises(ValidationError):
            commands.expire_command(self.conn, other.command.id, reason=huge,
                                  clock=self.clock)
        self.assertEqual(
            commands.get_command(self.conn, other.command.id).state, "pending"
        )
        self.assertEqual(self._kind_count("command.expired"), 0)
        # boundary: exactly cap bytes is accepted and stored verbatim
        settled = commands.reconcile_command(
            self.conn, cid, outcome="expired", note="x" * cap, clock=self.clock
        )
        self.assertEqual((settled.state, settled.note), ("expired", "x" * cap))

    def test_get_and_list(self) -> None:
        a = self.submit(key="ka")
        b = self.submit(key="kb")
        commands.claim_command(self.conn, b.command.id, clock=self.clock)
        pend = commands.list_commands(self.conn, state="pending")
        run = commands.list_commands(self.conn, state="running")
        self.assertEqual([c.idempotency_key for c in pend], ["ka"])
        self.assertEqual([c.idempotency_key for c in run], ["kb"])
        self.assertEqual(
            commands.get_command(self.conn, a.command.id).idempotency_key, "ka"
        )
        with self.assertRaises(NotFoundError):
            commands.get_command(self.conn, "ghost")
        with self.assertRaises(ValidationError):
            commands.list_commands(self.conn, state="bogus")
        with self.assertRaises(ValidationError):
            commands.list_commands(self.conn, limit=1001)

    # -- TTL -----------------------------------------------------------------------

    def test_ttl_expiry_blocks_claim_then_sweep_transitions(self) -> None:
        sub = self.submit(ttl=60)
        self.clock.advance(61)
        with self.assertRaises(IllegalTransitionError) as ctx:
            commands.claim_command(self.conn, sub.command.id, clock=self.clock)
        self.assertEqual(ctx.exception.reason_code, "TTL_EXPIRED")
        # refusal wrote nothing: still pending until the sweep says so
        self.assertEqual(commands.get_command(self.conn, sub.command.id).state,
                         "pending")
        swept = commands.sweep_stale_commands(self.conn, clock=self.clock)
        self.assertEqual(swept, [(sub.command.id, "TTL_EXPIRED")])
        cmd = commands.get_command(self.conn, sub.command.id)
        self.assertEqual((cmd.state, cmd.note), ("expired", "TTL_EXPIRED"))
        self.assertEqual(self._kind_count("command.expired"), 1)
        # no TTL -> never swept for time reasons
        forever = self.submit(key="k-eternal")
        self.clock.advance(10 * 365 * 24 * 3600)
        self.assertEqual(commands.sweep_stale_commands(self.conn, clock=self.clock),
                         [])
        self.assertEqual(
            commands.get_command(self.conn, forever.command.id).state, "pending"
        )

    def test_sweep_never_touches_running_or_confirmed(self) -> None:
        sub = self.submit(ttl=60)
        commands.claim_command(self.conn, sub.command.id, clock=self.clock)
        self.clock.advance(3600)
        self.assertEqual(commands.sweep_stale_commands(self.conn, clock=self.clock),
                         [])  # in flight: reconcile, never silent expiry
        self.assertEqual(commands.get_command(self.conn, sub.command.id).state,
                         "running")

    # -- evidence version invalidation ----------------------------------------------

    def test_task_cas_bump_invalidates_pending_commands_atomically(self) -> None:
        task = self.ready_task("bump")
        sub = self.submit(key="kbump", task=task)
        self.assertEqual(commands.get_command(self.conn, sub.command.id).state,
                         "pending")
        domain.update_task(self.conn, task.id, expected_version=task.version,
                          priority=90, clock=self.clock)
        cmd = commands.get_command(self.conn, sub.command.id)
        self.assertEqual((cmd.state, cmd.note), ("expired", "EVIDENCE_STALE"))
        self.assertEqual(self._kind_count("command.expired"), 1)

    def test_bump_and_invalidation_roll_back_together(self) -> None:
        task = self.ready_task("bump-rb")
        sub = self.submit(key="krb", task=task)
        with self.assertRaises(RuntimeError):
            with storage.transaction(self.conn):
                domain.update_task(self.conn, task.id,
                                 expected_version=task.version, priority=42,
                                 clock=self.clock)
                # invalidation already happened inside the tx…
                self.assertEqual(
                    commands.get_command(self.conn, sub.command.id).state, "expired"
                )
                raise RuntimeError("crash after invalidation")
        # …and vanished with it: version and command state revert as one unit
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.priority, after.version),
                         (task.priority, task.version))
        self.assertEqual(
            commands.get_command(self.conn, sub.command.id).state, "pending"
        )
        self.assertEqual(self._kind_count("command.expired"), 0)

    def test_claim_refused_when_evidence_drifted_around_the_hook(self) -> None:
        """A bump done at the RAW storage level (no domain hook) leaves the
        command pending — but the claim still refuses under the lock."""
        task = self.ready_task("raw-bump")
        sub = self.submit(key="kraw", task=task)
        with storage.transaction(self.conn):  # storage-level, hook-free bump
            self.conn.execute(
                "UPDATE tasks SET version = version + 1 WHERE id = ?", (task.id,)
            )
        self.assertEqual(commands.get_command(self.conn, sub.command.id).state,
                         "pending")  # auto-invalidated only via domain CAS
        with self.assertRaises(IllegalTransitionError) as ctx:
            commands.claim_command(self.conn, sub.command.id, clock=self.clock)
        self.assertEqual(ctx.exception.reason_code, "EVIDENCE_STALE")
        # sweep now performs the recorded transition
        swept = commands.sweep_stale_commands(self.conn, clock=self.clock)
        self.assertEqual(swept, [(sub.command.id, "EVIDENCE_STALE")])
        # vanished target
        gone = commands.submit_command(
            self.conn, idempotency_key="kgone", kind="task.pause",
            payload_json="{}",
            evidence_json='{"task_id": "t-ghost", "task_version": 0}',
            clock=self.clock,
        )
        with self.assertRaises(IllegalTransitionError) as ctx:
            commands.claim_command(self.conn, gone.command.id, clock=self.clock)
        self.assertEqual(ctx.exception.reason_code, "EVIDENCE_TARGET_GONE")

    def test_commands_bound_to_fresh_version_survive_invalidation(self) -> None:
        task = self.ready_task("survive")
        stale = self.submit(key="k-stale", task=task)
        domain.update_task(self.conn, task.id, expected_version=task.version,
                          priority=90, clock=self.clock)
        task = domain.get_task(self.conn, task.id)  # version advanced
        fresh = self.submit(key="k-fresh", task=task)
        self.assertEqual(
            commands.get_command(self.conn, stale.command.id).state, "expired"
        )
        self.assertEqual(
            commands.get_command(self.conn, fresh.command.id).state, "pending"
        )
        commands.claim_command(self.conn, fresh.command.id, clock=self.clock)

    # -- compound atomicity (state + event + command) -------------------------------

    def test_fault_injection_no_command_or_event_without_state(self) -> None:
        task = self.ready_task("compound")
        with self.assertRaises(RuntimeError):
            with storage.transaction(self.conn):
                sub = commands.submit_command(
                    self.conn, idempotency_key="k-compound", kind="task.cancel",
                    payload_json='{"reason": "user"}',
                    evidence_json=self.evidence(task), clock=self.clock
                )
                commands.claim_command(self.conn, sub.command.id, clock=self.clock)
                domain.update_task(self.conn, task.id,
                                 expected_version=task.version, priority=1,
                                 clock=self.clock)
                raise RuntimeError("crash mid compound write")
        # absolutely nothing persisted from the aborted unit
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind LIKE 'command.%'"
            ).fetchone()[0],
            0,
        )
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.priority, after.version), (task.priority,
                                                           task.version))

    def test_compound_commit_persists_all_three_layers(self) -> None:
        task = self.ready_task("compound-ok")
        with storage.transaction(self.conn):
            # real domain CAS first: Task version/priority move…
            domain.update_task(self.conn, task.id,
                              expected_version=task.version, priority=55,
                              clock=self.clock)
            updated = domain.get_task(self.conn, task.id)
            self.assertEqual((updated.priority, updated.version),
                            (55, task.version + 1))
            # …then the command binds the UPDATED evidence version, so the
            # in-transaction bump must not invalidate it
            sub = commands.submit_command(
                self.conn, idempotency_key="k-all", kind="task.cancel",
                payload_json='{"reason": "user"}',
                evidence_json=self.evidence(updated), clock=self.clock,
            )
            commands.claim_command(self.conn, sub.command.id, clock=self.clock)
            commands.confirm_command(self.conn, sub.command.id,
                                   result_json='{"cancelled_via": "ui"}',
                                   clock=self.clock)
        # all three layers durable after the single COMMIT
        after = domain.get_task(self.conn, task.id)
        self.assertEqual((after.priority, after.version), (55, task.version + 1))
        cmd = commands.get_command(self.conn, sub.command.id)
        self.assertEqual((cmd.state, cmd.result_json),
                         ("confirmed", '{"cancelled_via": "ui"}'))
        self.assertEqual(self._kind_count("command.expired"), 0)  # fresh binding
        kinds = [e.kind for e in events.events_after(self.conn, 0, limit=50)]
        self.assertEqual(
            [k for k in kinds if k.startswith("command.")],
            ["command.submitted", "command.claimed", "command.confirmed"],
        )
        # commands rows are permanent audit records: DELETE is refused
        raw = sqlite3.connect(str(self.db))
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM commands")

    def test_command_service_refuses_lagging_schema(self) -> None:
        raw = sqlite3.connect(str(self.db))
        raw.execute("DROP TABLE events")
        raw.execute("DROP TABLE commands")
        raw.execute("DELETE FROM schema_migrations WHERE version > 1")
        raw.execute("PRAGMA user_version=1")
        raw.commit()
        raw.close()
        lagging = storage.connect(self.db)
        self.addCleanup(lagging.close)
        with self.assertRaises(SchemaOutOfDateError):
            commands.submit_command(lagging, idempotency_key="k", kind="task.pause",
                                  clock=self.clock)
        with self.assertRaises(SchemaOutOfDateError):
            commands.list_commands(lagging)


if __name__ == "__main__":
    unittest.main()
