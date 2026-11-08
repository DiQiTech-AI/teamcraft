"""M3-03 SSE and reconnect-recovery tests.

Covers the acceptance line from doc/TEAMCRAFT_2_0_M0_REVIEW.md (M3-03)
and the cross-task index row "Web 断线事件补拉/客户端去重":

* authenticated read-only SSE: no token -> uniform 401; stream bodies
  carry the right media type and anti-cache/anti-buffer headers;
* ``after`` / ``Last-Event-ID`` cursor semantics with ``after`` winning,
  malformed cursors -> 422, cursors ahead of the durable stream -> 409
  with an explicit ``snapshot_required`` action (no live stream opens);
* reconnecting along the chain (cursor := last id seen) neither loses
  nor duplicates events: the union of delivered ids across connections
  equals ``SELECT seq FROM events ORDER BY seq`` exactly;
* SSE ids are the durable SQLite ``events.seq``: strictly increasing,
  identical to the REST Query surface items for the same window, and
  comments/heartbeats/control frames never carry (never consume) ids;
* sequence gaps (durable seq jumping past cursor+1) end the stream with
  a ``snapshot_required`` fallback; the REST event list still serves the
  surviving rows and a re-baselined cursor resumes cleanly;
* bounded behavior: batched backfill, per-connection backfill budget
  (409 + REST-backfill action before the stream), connection cap (429
  while full) with slot cleanup on client disconnect, frame-size cap
  (oversized payloads degrade to flagged compact references);
* read-only invariants: a full reconnect cycle changes nothing in the
  database (row digest), and the stream's own SQLite channel physically
  rejects DML (``mode=ro``);
* response-lifecycle guard: scripted ASGI transports (no sockets) prove
  the limiter slot and the ro read connection are released exactly once
  even when the transport fails before the body generator ever starts,
  fails at the first body chunk, or the client disconnects right after
  delivery — and that the release is idempotent across both hooks;
* payload text is secret-redacted, and the M3-01/M3-02 surface
  (ping, REST events, path precedence) keeps working alongside.

Streams are exercised against a real uvicorn server on an ephemeral
loopback port with a streaming httpx client (the ASGI TestClient cannot
represent live streaming responses); every server binds 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import http.client
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from urllib.parse import urlencode

try:  # the SSE surface needs the web stack; core tests stay runnable without
    import httpx
    import uvicorn
    from starlette.requests import Request

    from teamcraft.api import (
        EVENT_SNAPSHOT_REQUIRED,
        QueryService,
        SseConfig,
        StreamConnectionLimiter,
        create_app,
    )
    from teamcraft.api import stream as stream_module
    from teamcraft.api.errors import (
        CONFLICT,
        NOT_FOUND,
        SERVICE_UNAVAILABLE,
        TOO_MANY_REQUESTS,
        UNAUTHENTICATED,
        VALIDATION_FAILED,
    )
    from teamcraft.api.redaction import REDACTION_MARKER

    _WEB_STACK_OK = True
    _WEB_STACK_REASON = ""
except ImportError as exc:  # pragma: no cover - depends on CI install order
    _WEB_STACK_OK = False
    _WEB_STACK_REASON = f"web dependencies unavailable: {exc}"

from teamcraft import domain, events, fresh_start, storage
from teamcraft.errors import ValidationError

requires_web = unittest.skipUnless(_WEB_STACK_OK, _WEB_STACK_REASON)

ERROR_KEYS = {"code", "message", "details", "request_id"}

#: Durable tables whose rows must not shift under any stream operation.
_INTEGRITY_TABLES = (
    "projects",
    "agents",
    "tasks",
    "attempts",
    "sessions",
    "events",
    "commands",
    "wake_cursors",
    "wakes",
    "wake_events",
)


def _iso() -> str:
    return datetime(2026, 11, 8, tzinfo=timezone.utc).isoformat()


def fast_config(**overrides) -> SseConfig:
    """SseConfig with loopback-test-friendly timings; overrides applied."""

    values = {
        "poll_interval_seconds": 0.02,
        "heartbeat_interval_seconds": 0.08,
        "backfill_batch_size": 100,
        "max_backfill_events": 1000,
        "max_connections": 8,
        "max_duration_seconds": 0.4,
        "retry_ms": 250,
        "max_frame_bytes": 192 * 1024,
    }
    values.update(overrides)
    if values["max_backfill_events"] < values["backfill_batch_size"]:
        values["backfill_batch_size"] = values["max_backfill_events"]
    return SseConfig(**values)


def parse_frames(lines: list[str]) -> list[dict]:
    """Parse SSE wire lines into frame dicts split on blank lines."""

    frames: list[dict] = []
    current: dict = {}
    for line in lines:
        if not line:
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith(":"):
            current["comment"] = line
        elif line.startswith("retry:"):
            current["retry"] = int(line.split(":", 1)[1].strip())
        elif line.startswith("id:"):
            current["id"] = int(line.split(":", 1)[1].strip())
        elif line.startswith("event:"):
            current["event"] = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            raw = line.split(":", 1)[1].strip()
            try:
                current["data"] = json.loads(raw)
            except ValueError:  # pragma: no cover - defensive
                current["data_raw"] = raw
    if current:
        frames.append(current)
    return frames


def ids_in(lines: list[str]) -> list[int]:
    return [f["id"] for f in parse_frames(lines) if "id" in f]


def snapshot_frames(lines: list[str]) -> list[dict]:
    return [
        f
        for f in parse_frames(lines)
        if f.get("event") == EVENT_SNAPSHOT_REQUIRED
    ]


def _start_server(app) -> tuple[object, threading.Thread, str]:
    """Run ``app`` on an ephemeral 127.0.0.1 port; (server, thread, base)."""

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test uvicorn server failed to start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, f"http://127.0.0.1:{port}"


def _stop_server(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------


@requires_web
class StreamScaffold(unittest.TestCase):
    """Temp project DB + guarded app served by real loopback uvicorn."""

    stream_overrides: dict = {}
    seed_count = 0

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="tc-m303-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(self.root, ".teamcraft", "teamcraft.db")
        self.service = QueryService(self.db_path)
        self.writer = storage.connect(self.db_path)
        self.addCleanup(self.writer.close)
        self.project_id = domain.create_project(self.writer, "alpha", self.root).id

        self.app = create_app(
            query_service=self.service,
            event_stream_config=fast_config(**self.stream_overrides),
        )
        self.token = self.app.state.token_store.ensure()
        self.limiter: StreamConnectionLimiter = self.app.state.sse_connection_limiter
        self.server, self.thread, self.base = _start_server(self.app)
        self.addCleanup(_stop_server, self.server, self.thread)
        self.client = httpx.Client(
            base_url=self.base,
            timeout=10.0,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.addCleanup(self.client.close)

        self.secret_sentinel = f"seeded-secret-{os.urandom(6).hex()}"
        for index in range(self.seed_count):
            self.seed_event(f"watch.seed_{index}")

    # -- seeding / authority reads (CLI-side writes; the stream reads) ------

    def seed_event(self, kind: str, *, payload: dict | None = None, project_id: str | None = None):
        return events.append_event(
            self.writer,
            kind,
            project_id=project_id,
            payload_json=json.dumps(payload if payload is not None else {"k": kind}),
        )

    def db_seqs(self, project_id: str | None = None) -> list[int]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            if project_id is None:
                rows = conn.execute("SELECT seq FROM events ORDER BY seq").fetchall()
            else:
                rows = conn.execute(
                    "SELECT seq FROM events WHERE project_id = ? ORDER BY seq",
                    (project_id,),
                ).fetchall()
        finally:
            conn.close()
        return [int(row[0]) for row in rows]

    def insert_event_at_seq(
        self, seq: int, *, kind: str = "watch.gapfill", delay: float = 0.0
    ) -> str:
        """Simulate a retention/pruning gap: a row whose seq skips values.

        The v3 store blocks UPDATE/DELETE on ``events`` and this build has
        no public pruning API, so an out-of-band INSERT at a far-ahead seq
        is the faithful way for a test to create the gap condition the
        consumer contract (M1-05) says streams must survive.

        With ``delay`` > 0 the insert runs from a background thread once
        the delay elapses, so a test can plant the gap *after* a stream's
        backfill window has already closed (live-path detection).
        """

        event_id = f"event_gap_{seq}"

        def _insert() -> None:
            if delay > 0:
                time.sleep(delay)
            conn = storage.connect(self.db_path)
            try:
                with storage.transaction(conn):
                    conn.execute(
                        "INSERT INTO events (seq, event_id, kind, payload_json,"
                        " occurred_at) VALUES (?, ?, ?, ?, ?)",
                        (seq, event_id, kind, json.dumps({"gap": True}), _iso()),
                    )
            finally:
                conn.close()

        if delay > 0:
            thread = threading.Thread(target=_insert, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
        else:
            _insert()
        return event_id

    def integrity_digest(self) -> str:
        conn = storage.connect(self.db_path)
        try:
            digest = hashlib.sha256()
            for table in _INTEGRITY_TABLES:
                digest.update(table.encode("utf-8"))
                digest.update(b"\x00")
                for row in conn.execute(
                    f"SELECT rowid, * FROM {table} ORDER BY rowid"
                ):
                    digest.update(repr(row).encode("utf-8"))
                    digest.update(b"\n")
            return digest.hexdigest()
        finally:
            conn.close()

    # -- stream helpers --------------------------------------------------------

    def collect(
        self,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        stop=None,
        timeout: float = 8.0,
    ):
        """Open the SSE endpoint; return (response, lines-so-far).

        Non-200 responses have their (JSON) body read; the stream context
        is always closed on cleanup so slots are returned to the server.
        """

        url = "/api/v1/events/stream"
        if params:
            url += "?" + urlencode(params)
        manager = self.client.stream("GET", url, headers=headers or {})
        response = manager.__enter__()
        self.addCleanup(_exit_quietly, manager)
        if response.status_code != 200:
            response.read()
            return response, []
        lines: list[str] = []
        started = time.monotonic()
        for line in response.iter_lines():
            lines.append(line)
            if stop is not None and stop(line, lines):
                break
            if time.monotonic() - started > timeout:
                break
        return response, lines

    def append_later(self, delay: float, **kwargs) -> None:
        """Append an event from a background thread (simulated live write)."""

        def _append() -> None:
            time.sleep(delay)
            conn = storage.connect(self.db_path)
            try:
                events.append_event(
                    conn,
                    kwargs.get("kind", "watch.live"),
                    project_id=kwargs.get("project_id"),
                    payload_json=json.dumps(kwargs.get("payload", {"live": True})),
                )
            finally:
                conn.close()

        thread = threading.Thread(target=_append, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

    def restart_with(self, config: SseConfig) -> None:
        """Serve a freshly built app (new config/token) on a new port."""

        _stop_server(self.server, self.thread)
        self.app = create_app(
            query_service=self.service, event_stream_config=config
        )
        self.token = self.app.state.token_store.ensure()
        self.limiter = self.app.state.sse_connection_limiter
        self.client.headers["Authorization"] = f"Bearer {self.token}"
        self.server, self.thread, self.base = _start_server(self.app)
        self.addCleanup(_stop_server, self.server, self.thread)
        self.client.base_url = httpx.URL(self.base)

    def open_live_stream(self):
        """Open an SSE stream and wait until its slot and first frame land.

        The line iterator reference is returned alongside so the caller
        keeps the partially-read stream alive: dropping an unconsumed
        ``iter_lines`` generator makes httpx close the connection, which
        correctly ends the server-side stream and frees its slot.
        """

        manager = self.client.stream("GET", "/api/v1/events/stream")
        response = manager.__enter__()
        self.addCleanup(_exit_quietly, manager)
        if response.status_code != 200:
            response.read()
            return response, manager, [], iter(())
        lines: list[str] = []
        iterator = response.iter_lines()
        for line in iterator:
            lines.append(line)
            if line.startswith("data:") or line.startswith("event:"):
                break
        deadline = time.monotonic() + 5
        while self.limiter.active == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        return response, manager, lines, iterator

    def raw_get(
        self,
        path: str,
        extra_headers: dict,
        *,
        omit_auth: bool = False,
        base: str | None = None,
        token: str | None = None,
    ) -> tuple[int, dict, str]:
        """HTTP/1.1 request via stdlib (for header values httpx refuses)."""

        host, port = (base or self.base).split("//", 1)[1].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        try:
            headers: dict = {}
            if not omit_auth:
                headers["Authorization"] = f"Bearer {token or self.token}"
            headers.update(extra_headers)
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = response.read().decode("utf-8", "replace")
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()

    # -- assertion helpers -----------------------------------------------------

    def assert_uniform_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text[:400])
        body = response.json()
        self.assertEqual(set(body.keys()), ERROR_KEYS)
        self.assertEqual(body["code"], code)
        self.assertTrue(body["request_id"])
        self.assertNotIn(self.secret_sentinel, response.text)
        self.assertNotIn(self.token, response.text)
        return body

    def assert_ids_strict(self, ids: list[int]) -> None:
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue(all(seq > 0 for seq in ids))


def _exit_quietly(manager) -> None:
    try:
        manager.__exit__(None, None, None)
    except Exception:  # pragma: no cover - already-closed streams
        pass


# ---------------------------------------------------------------------------
# Authentication and wire shape
# ---------------------------------------------------------------------------


class M303AuthAndShape(StreamScaffold):
    seed_count = 3

    def test_stream_requires_bearer_token(self) -> None:
        with httpx.Client(base_url=self.base, timeout=5.0) as anon:
            response = anon.get("/api/v1/events/stream")
        self.assert_uniform_error(response, 401, UNAUTHENTICATED)
        self.assertTrue(response.headers.get("x-request-id"))

    def test_stream_rejects_wrong_token(self) -> None:
        with httpx.Client(
            base_url=self.base,
            timeout=5.0,
            headers={"Authorization": "Bearer " + "x" * 43},
        ) as wrong:
            response = wrong.get("/api/v1/events/stream")
        self.assert_uniform_error(response, 401, UNAUTHENTICATED)

    def test_stream_media_and_security_headers(self) -> None:
        response, _lines = self.collect()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertTrue(response.headers.get("x-request-id"))


# ---------------------------------------------------------------------------
# Backfill, ordering, cursor semantics
# ---------------------------------------------------------------------------


class M303BackfillOrdering(StreamScaffold):
    seed_count = 6

    def test_ids_match_durable_db_order(self) -> None:
        response, lines = self.collect()
        self.assertEqual(response.status_code, 200)
        ids = ids_in(lines)
        self.assertEqual(ids, self.db_seqs())
        self.assert_ids_strict(ids)
        # Same-source proof: the REST Query surface lists the same window.
        rest = self.client.get("/api/v1/events", params={"after_seq": 0, "limit": 50})
        self.assertEqual(rest.status_code, 200)
        self.assertEqual([i["seq"] for i in rest.json()["items"]], ids)
        # Event names mirror the durable kind; payloads ride through.
        frames = [f for f in parse_frames(lines) if "id" in f]
        self.assertEqual(frames[0]["event"], "watch.seed_0")
        self.assertEqual(frames[0]["data"]["seq"], ids[0])
        self.assertEqual(frames[0]["data"]["kind"], "watch.seed_0")

    def test_after_query_resumes_without_duplicates(self) -> None:
        _response, lines = self.collect(params={"after": 3})
        self.assertEqual(ids_in(lines), [4, 5, 6])

    def test_last_event_id_header_resumes(self) -> None:
        _response, lines = self.collect(headers={"Last-Event-ID": "3"})
        self.assertEqual(ids_in(lines), [4, 5, 6])

    def test_after_param_wins_over_header(self) -> None:
        _response, lines = self.collect(
            params={"after": 4}, headers={"Last-Event-ID": "2"}
        )
        self.assertEqual(ids_in(lines), [5, 6])

    def test_batched_backfill_delivers_everything(self) -> None:
        # Rebuild the server with tiny batches: all seven rows must still
        # arrive in order across backfill batches.
        self.restart_with(fast_config(backfill_batch_size=2))
        self.seed_event("watch.seed_extra")
        _response, lines = self.collect()
        self.assertEqual(ids_in(lines), self.db_seqs())

    def test_project_scoped_stream_filters_by_durable_seq(self) -> None:
        beta_root = os.path.join(self.root, "beta-repo")
        os.makedirs(beta_root)
        other = domain.create_project(self.writer, "beta", beta_root)
        own = self.seed_event("watch.mine_a", project_id=self.project_id)
        foreign = self.seed_event("watch.theirs", project_id=other.id)
        more = self.seed_event("watch.mine_b", project_id=self.project_id)
        _response, lines = self.collect(params={"project_id": self.project_id})
        ids = ids_in(lines)
        # Ordering is the durable seq, scoped exactly to the project.
        self.assertEqual(ids, self.db_seqs(self.project_id))
        self.assertEqual(ids, [own.seq, more.seq])
        self.assertNotIn(foreign.seq, ids)

    def test_unknown_project_is_404_before_stream(self) -> None:
        response, lines = self.collect(params={"project_id": "p_missing"})
        self.assertEqual(lines, [])
        self.assert_uniform_error(response, 404, NOT_FOUND)

    def test_cursor_at_durable_tail_streams_clean(self) -> None:
        response, lines = self.collect(params={"after": 6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ids_in(lines), [])
        closes = snapshot_frames(lines)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["data"]["reason"], "max_duration")
        self.assertEqual(closes[0]["data"]["resume_after"], 6)


# ---------------------------------------------------------------------------
# Cursor validation
# ---------------------------------------------------------------------------


class M303CursorValidation(StreamScaffold):
    seed_count = 3

    def test_after_must_be_a_non_negative_int(self) -> None:
        for raw in ("abc", "-1", "1.5", "7x"):
            response, lines = self.collect(params={"after": raw})
            self.assertEqual(lines, [], raw)
            self.assert_uniform_error(response, 422, VALIDATION_FAILED)

    def test_malformed_last_event_id_header_is_422(self) -> None:
        # Values httpx can put on the wire:
        for raw in ("abc", "-3", "12.5", "0x7", "1" * 21):
            response, lines = self.collect(headers={"Last-Event-ID": raw})
            self.assertEqual(lines, [], repr(raw))
            body = self.assert_uniform_error(response, 422, VALIDATION_FAILED)
            self.assertEqual(body["details"], {"field": "last_event_id"})
        # Header values a strict client refuses to send (blank/whitespace)
        # still reach the server as bytes via stdlib http.client and must
        # be rejected the same way — the client must not be able to nudge
        # the stream into guessing a resume position.
        for raw in ("", " "):
            status, headers, text = self.raw_get(
                "/api/v1/events/stream", {"Last-Event-ID": raw}
            )
            self.assertEqual(status, 422, repr(raw))
            self.assertEqual(
                headers["content-type"].split(";")[0], "application/json"
            )
            body = json.loads(text)
            self.assertEqual(set(body.keys()), ERROR_KEYS, repr(raw))
            self.assertEqual(body["code"], VALIDATION_FAILED)
            self.assertEqual(body["details"], {"field": "last_event_id"})

    def test_ahead_cursor_is_409_snapshot_required(self) -> None:
        response, lines = self.collect(params={"after": 99})
        self.assertEqual(lines, [])
        body = self.assert_uniform_error(response, 409, CONFLICT)
        self.assertEqual(body["details"]["reason"], "cursor_ahead")
        self.assertEqual(body["details"]["action"], "snapshot_required")
        self.assertEqual(body["details"]["cursor"], 99)
        self.assertEqual(body["details"]["durable_seq"], 3)
        self.assertEqual(
            response.headers["content-type"].split(";")[0], "application/json"
        )

    def test_ahead_last_event_id_is_409(self) -> None:
        response, _lines = self.collect(headers={"Last-Event-ID": "4"})
        body = self.assert_uniform_error(response, 409, CONFLICT)
        self.assertEqual(body["details"]["durable_seq"], 3)
        self.assertEqual(
            body["details"]["snapshot_endpoint"], "/api/v1/events"
        )

    def test_project_scoped_ahead_points_at_project_snapshot(self) -> None:
        response, _lines = self.collect(params={"after": 50, "project_id": self.project_id})
        body = self.assert_uniform_error(response, 409, CONFLICT)
        self.assertEqual(
            body["details"]["snapshot_endpoint"],
            f"/api/v1/projects/{self.project_id}/snapshot",
        )


# ---------------------------------------------------------------------------
# Sequence-gap fallback
# ---------------------------------------------------------------------------


class M303GapFallback(StreamScaffold):
    seed_count = 3

    def test_backfill_gap_emits_snapshot_required_and_closes(self) -> None:
        self.insert_event_at_seq(10)
        _response, lines = self.collect(params={"after": 2})
        self.assertEqual(ids_in(lines), [3])
        closes = snapshot_frames(lines)
        self.assertEqual(len(closes), 1)
        data = closes[0]["data"]
        self.assertEqual(data["reason"], "sequence_gap")
        self.assertEqual(data["action"], "snapshot_required")
        self.assertEqual(data["last_seq"], 3)
        self.assertEqual(data["expected_seq"], 4)
        self.assertEqual(data["gap_size"], 6)
        self.assertEqual(data["durable_seq"], 10)
        self.assertEqual(data["resume_after"], 3)
        # Control frames never carry an id (no id consumption).
        self.assertNotIn("id", closes[0])
        # The stream ended on the gap instead of guessing past it.
        self.assertEqual(parse_frames(lines)[-1]["event"], EVENT_SNAPSHOT_REQUIRED)

    def test_live_gap_detected_after_backfill(self) -> None:
        # Backfill closes over seq 1..3 at connect time. Seq 4 lands live,
        # then seq 10 lands: the live poll must detect the jump (the gap
        # did not exist when the stream opened).
        self.append_later(0.15, kind="watch.after_gap")  # seq 4
        self.insert_event_at_seq(10, delay=0.35)  # gap appears mid-stream

        def _stop(line: str, collected: list[str]) -> bool:
            # Wait for the snapshot frame's data line (the "event:" line
            # alone arrives before its payload).
            return line.startswith("data:") and '"reason"' in line

        _response, lines = self.collect(stop=_stop, timeout=6.0)
        self.assertEqual(ids_in(lines), [1, 2, 3, 4])
        closes = snapshot_frames(lines)
        self.assertEqual(len(closes), 1)
        data = closes[0]["data"]
        self.assertEqual(data["reason"], "sequence_gap")
        self.assertEqual(data["last_seq"], 4)
        self.assertEqual(data["expected_seq"], 5)
        self.assertEqual(data["gap_size"], 5)
        self.assertEqual(data["durable_seq"], 10)
        self.assertEqual(data["resume_after"], 4)
        self.assertNotIn("id", closes[0])

    def test_rest_serves_surviving_rows_and_rebaseline_resumes_clean(self) -> None:
        self.insert_event_at_seq(10)
        # Reconnect with the lost cursor: the gap is reported again (the
        # stream refuses to silently skip), which is the documented loop
        # until the client re-baselines its cursor...
        _response, lines = self.collect(params={"after": 3})
        self.assertEqual(ids_in(lines), [])
        closes = snapshot_frames(lines)
        self.assertEqual(closes[0]["data"]["reason"], "sequence_gap")
        self.assertEqual(closes[0]["data"]["resume_after"], 3)
        # ...and REST is the durable source for the surviving rows:
        rest = self.client.get("/api/v1/events", params={"after_seq": 3})
        self.assertEqual(rest.status_code, 200)
        surviving = [item["seq"] for item in rest.json()["items"]]
        self.assertEqual(surviving, [10])
        # With a re-baselined cursor (from the durable data) the stream
        # resumes cleanly with no gap and no duplicates.
        _response2, lines2 = self.collect(params={"after": surviving[-1]})
        self.assertEqual(ids_in(lines2), [])
        self.assertEqual(snapshot_frames(lines2)[0]["data"]["reason"], "max_duration")


# ---------------------------------------------------------------------------
# Heartbeats and control frames
# ---------------------------------------------------------------------------


class M303HeartbeatsAndControl(StreamScaffold):
    seed_count = 2

    def test_retry_and_open_comments_precede_events_without_ids(self) -> None:
        self.insert_event_at_seq(3)
        _response, lines = self.collect()
        frames = parse_frames(lines)
        self.assertEqual(frames[0]["retry"], 250)
        self.assertNotIn("id", frames[0])
        self.assertIn("stream-open", frames[1]["comment"])
        self.assertIn("durable_seq=3", frames[1]["comment"])
        self.assertIn("cursor=0", frames[1]["comment"])
        self.assertNotIn("id", frames[1])
        self.assertEqual(ids_in(lines), [1, 2, 3])

    def test_idle_heartbeats_are_comments_and_consume_no_ids(self) -> None:
        _response, lines = self.collect(params={"after": 2})
        comment_lines = [line for line in lines if line.startswith(": heartbeat")]
        self.assertGreaterEqual(len(comment_lines), 2)
        # Nothing on this idle stream carries an id...
        self.assertEqual(ids_in(lines), [])
        # ...including the closing control frame.
        closes = snapshot_frames(lines)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["data"]["reason"], "max_duration")
        self.assertNotIn("id", closes[0])
        for frame in parse_frames(lines):
            self.assertNotIn("id", frame)

    def test_heartbeats_do_not_shift_live_event_ids(self) -> None:
        self.append_later(0.15)

        def _stop(line: str, collected: list[str]) -> bool:
            return line.startswith("id: 3")

        _response, lines = self.collect(stop=_stop, timeout=6.0)
        ids = ids_in(lines)
        self.assertEqual(ids, [1, 2, 3])
        heartbeat_indexes = [
            index for index, line in enumerate(lines) if line.startswith(": heartbeat")
        ]
        id3_index = next(i for i, line in enumerate(lines) if line.startswith("id: 3"))
        self.assertTrue(heartbeat_indexes)
        self.assertTrue(any(index < id3_index for index in heartbeat_indexes))
        # The live event's id is exactly its durable seq (heartbeats did
        # not shift the sequence).
        self.assertEqual(self.db_seqs(), [1, 2, 3])


# ---------------------------------------------------------------------------
# Bounded backfill budget
# ---------------------------------------------------------------------------


class M303BackfillBudget(StreamScaffold):
    seed_count = 8

    def test_pending_over_budget_is_409_rest_backfill_before_stream(self) -> None:
        self.restart_with(fast_config(max_backfill_events=5))
        response, lines = self.collect()
        self.assertEqual(lines, [])
        body = self.assert_uniform_error(response, 409, CONFLICT)
        details = body["details"]
        self.assertEqual(details["reason"], "backfill_too_large")
        self.assertEqual(details["action"], "rest_backfill_required")
        self.assertEqual(details["pending"], 8)
        self.assertEqual(details["budget"], 5)
        self.assertEqual(details["resume_after"], 0)
        self.assertEqual(details["events_endpoint"], "/api/v1/events")
        # With a cursor that fits the budget the same stream opens fine.
        response2, lines2 = self.collect(params={"after": 5})
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(ids_in(lines2), [6, 7, 8])


# ---------------------------------------------------------------------------
# Redaction and frame bounds
# ---------------------------------------------------------------------------


class M303RedactionAndFrameBounds(StreamScaffold):
    def test_payload_text_is_secret_redacted_in_stream(self) -> None:
        # The redactor knows active session-token material; a payload that
        # embeds the live API token must never leak it over the wire.
        self.seed_event(
            "watch.seeded_note",
            payload={"note": f"token seen {self.token}", "keep": "visible"},
        )
        _response, lines = self.collect()
        text = "\n".join(lines)
        self.assertIn(REDACTION_MARKER, text)
        self.assertNotIn(self.token, text)
        frame = next(
            f for f in parse_frames(lines) if f.get("event") == "watch.seeded_note"
        )
        self.assertNotIn(self.token, json.dumps(frame["data"]))
        self.assertIn(REDACTION_MARKER, json.dumps(frame["data"]["payload"]))
        self.assertIn("visible", text)  # non-secret text still rides through

    def test_frame_size_cap_degrades_to_compact_reference(self) -> None:
        self.restart_with(fast_config(max_frame_bytes=400))
        small = self.seed_event("watch.small", payload={"ok": True})
        big = self.seed_event("watch.big", payload={"blob": "y" * 400})
        _response, lines = self.collect()
        frames = [f for f in parse_frames(lines) if "id" in f]
        self.assertEqual([f["id"] for f in frames], [small.seq, big.seq])
        small_data, big_data = frames[0]["data"], frames[1]["data"]
        self.assertEqual(small_data["payload"], {"ok": True})
        self.assertNotIn("payload_truncated", small_data)
        self.assertIsNone(big_data["payload"])
        self.assertTrue(big_data["payload_truncated"])
        self.assertGreaterEqual(big_data["payload_size_bytes"], 400)
        self.assertEqual(big_data["recover"], f"/api/v1/events/{big.event_id}")
        # Ordering survives truncation: ids are still the durable seqs.
        self.assertEqual(ids_in(lines), self.db_seqs())
        # The recover pointer serves the full durable payload via REST.
        rest = self.client.get(f"/api/v1/events/{big.event_id}")
        self.assertEqual(rest.status_code, 200)
        self.assertEqual(len(rest.json()["payload"]["blob"]), 400)

    def test_max_size_payload_fits_default_frame_cap(self) -> None:
        # The store caps payloads at 64 KiB and the default transport cap is
        # 192 KiB, so the biggest legal payload streams untouched — the
        # degradation path (previous test) only triggers for tighter caps.
        blob = "z" * (events.MAX_EVENT_PAYLOAD_BYTES - len('{"blob": ""}'))
        payload_json = json.dumps({"blob": blob})
        self.assertLessEqual(
            len(payload_json.encode("utf-8")), events.MAX_EVENT_PAYLOAD_BYTES
        )
        events.append_event(
            self.writer, "watch.bigblob", payload_json=payload_json
        )
        _response, lines = self.collect()
        frames = [f for f in parse_frames(lines) if "id" in f]
        self.assertEqual(len(frames), 1)
        data = frames[0]["data"]
        self.assertEqual(data["payload"], {"blob": blob})
        self.assertNotIn("payload_truncated", data)
        self.assertEqual(self.db_seqs(), [data["seq"]])
        self.assertLess(
            len("\n".join(lines).encode("utf-8")),
            self.app.state.sse_config.max_frame_bytes,
        )


# ---------------------------------------------------------------------------
# Bounded connections and disconnect cleanup
# ---------------------------------------------------------------------------


class M303ConnectionLimits(StreamScaffold):
    seed_count = 2
    stream_overrides = {"max_connections": 2, "max_duration_seconds": 5.0}

    def test_cap_rejects_third_stream_and_all_slots_cleanup(self) -> None:
        response_a, manager_a, lines_a, keep_a = self.open_live_stream()
        self.assertEqual(response_a.status_code, 200, lines_a[:6])
        response_b, manager_b, lines_b, keep_b = self.open_live_stream()
        self.assertEqual(response_b.status_code, 200, lines_b[:6])
        deadline = time.monotonic() + 5
        while self.limiter.active < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(
            self.limiter.active,
            2,
            f"a={lines_a[:6]} b={lines_b[:6]}",
        )
        try:
            third = self.client.get("/api/v1/events/stream", timeout=5.0)
            self.assert_uniform_error(third, 429, TOO_MANY_REQUESTS)
            self.assertEqual(third.json()["details"]["max_connections"], 2)
        finally:
            manager_a.__exit__(None, None, None)
            manager_b.__exit__(None, None, None)
        deadline = time.monotonic() + 5
        while self.limiter.active != 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.limiter.active, 0)
        # Slots are reusable after streams end.
        reopened = self.client.get("/api/v1/events/stream", timeout=5.0)
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(ids_in(reopened.text.splitlines()), self.db_seqs())


class M303DisconnectCleanup(StreamScaffold):
    seed_count = 2
    stream_overrides = {"max_connections": 1, "max_duration_seconds": 2.0}

    def test_client_disconnect_releases_the_slot(self) -> None:
        manager = self.client.stream("GET", "/api/v1/events/stream")
        response = manager.__enter__()
        seen: list[str] = []
        for line in response.iter_lines():
            seen.append(line)
            if "stream-open" in line:
                break
        self.assertEqual(seen[0], "retry: 250")
        self.assertEqual(self.limiter.active, 1)
        manager.__exit__(None, None, None)  # hard client disconnect
        deadline = time.monotonic() + 5
        while self.limiter.active != 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.limiter.active, 0)
        # A reconnect after the disconnect is accepted immediately.
        next_response, lines = self.collect()
        self.assertEqual(next_response.status_code, 200)
        self.assertEqual(ids_in(lines), self.db_seqs())


# ---------------------------------------------------------------------------
# Read-only invariants
# ---------------------------------------------------------------------------


class M303ReadOnlyInvariant(StreamScaffold):
    seed_count = 5

    def test_streaming_cycle_changes_nothing_in_the_store(self) -> None:
        before = self.integrity_digest()
        # Exercise: full backfill, resumed cursor, malformed cursor, ahead
        # cursor, project-scoped connect, idle heartbeat stream.
        self.collect()
        self.collect(params={"after": 2})
        self.collect(params={"after": "oops"})
        self.collect(params={"after": 999})
        self.collect(params={"project_id": self.project_id})
        self.collect(params={"after": 5})
        after = self.integrity_digest()
        self.assertEqual(before, after)

    def test_stream_sqlite_channel_physically_rejects_writes(self) -> None:
        conn = stream_module._open_read_connection(self.service)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO events (event_id, kind, payload_json, occurred_at)"
                    " VALUES ('x', 'x', '{}', 'x')"
                )
            # Reads on the same channel keep working (proof it's the ro
            # gate, not a broken connection).
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]), 5
            )
        finally:
            conn.close()

    def test_no_stream_route_wakes_or_dispatches(self) -> None:
        before = self.integrity_digest()
        self.collect(params={"after": 1})
        conn = storage.connect(self.db_path)
        try:
            wake_rows = conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0]
            command_rows = conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((wake_rows, command_rows), (0, 0))
        self.assertEqual(self.integrity_digest(), before)


# ---------------------------------------------------------------------------
# App-surface preservation (M3-01 / M3-02 keep working)
# ---------------------------------------------------------------------------


class M303AppPreservation(StreamScaffold):
    seed_count = 3

    def test_ping_and_rest_events_coexist_with_stream(self) -> None:
        pong = self.client.get("/api/v1/ping")
        self.assertEqual(pong.status_code, 200)
        self.assertTrue(pong.json()["pong"])
        _response, lines = self.collect()
        rest = self.client.get("/api/v1/events", params={"after_seq": 0})
        self.assertEqual(
            [item["seq"] for item in rest.json()["items"]], ids_in(lines)
        )

    def test_events_path_precedence_keeps_rest_get_by_id(self) -> None:
        seeded = self.db_seqs()
        self.assertEqual(len(seeded), 3)
        event_id = self.seed_event("watch.rest").event_id
        rest = self.client.get(f"/api/v1/events/{event_id}")
        self.assertEqual(rest.status_code, 200)
        self.assertEqual(rest.json()["seq"], 4)
        # The stream path is its own route, not swallowed by /{event_id}.
        response, _lines = self.collect()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )

    def test_stream_absent_without_query_service(self) -> None:
        bare_app = create_app()
        bare_token = bare_app.state.token_store.ensure()
        server, thread, base = _start_server(bare_app)
        self.addCleanup(_stop_server, server, thread)
        with httpx.Client(
            base_url=base,
            timeout=5.0,
            headers={"Authorization": f"Bearer {bare_token}"},
        ) as client:
            # Auth is a path-agnostic gate (everything but the public
            # allowlist needs the token), so an anonymous hit is still 401
            # even with no Query Service mounted...
            anon_status, _h, anon_text = self.raw_get(
                "/api/v1/events/stream", {}, omit_auth=True, base=base
            )
            self.assertEqual(anon_status, 401)
            self.assertEqual(json.loads(anon_text)["code"], UNAUTHENTICATED)
            # ...and with a valid token the route simply does not exist:
            # without a Query Service there is no durable stream to expose.
            response = client.get("/api/v1/events/stream")
            self.assert_uniform_error(response, 404, NOT_FOUND)

    def test_health_stays_public(self) -> None:
        with httpx.Client(base_url=self.base, timeout=5.0) as anon:
            live = anon.get("/api/v1/health/live")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["status"], "alive")

    def test_uniform_service_unavailable_when_store_missing(self) -> None:
        missing_db = os.path.join(self.root, "nope", "teamcraft.db")
        self.restart_with(fast_config())
        self.service = QueryService(missing_db)
        # Point the live app's stream at the missing store: rebuild with a
        # service bound to the absent path.
        _stop_server(self.server, self.thread)
        self.app = create_app(
            query_service=QueryService(missing_db),
            event_stream_config=fast_config(),
        )
        self.token = self.app.state.token_store.ensure()
        self.client.headers["Authorization"] = f"Bearer {self.token}"
        self.server, self.thread, self.base = _start_server(self.app)
        self.addCleanup(_stop_server, self.server, self.thread)
        self.client.base_url = httpx.URL(self.base)
        response, lines = self.collect()
        self.assertEqual(lines, [])
        self.assert_uniform_error(response, 503, SERVICE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Unit-level config and limiter checks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Response-lifecycle guard (deterministic scripted ASGI transport, no sockets)
# ---------------------------------------------------------------------------


class _SendFailed(RuntimeError):
    """Simulated transport failure from the ASGI ``send`` callable."""


@requires_web
class M303ResponseLifecycle(unittest.TestCase):
    """Guarded-response checks for the slot/connection release edge cases.

    Drives the real endpoint callable with a scripted ASGI transport: with
    spec version 2.4 Starlette sends sequentially, so failures can be
    injected exactly at ``http.response.start`` (the body generator never
    starts, so *its* ``finally`` cannot run — only the guard response's
    ``__call__`` finally can release) and at the first body chunk (the
    generator starts but is abandoned mid-suspension).  Every path must
    return the limiter slot and close the ``mode=ro`` read connection
    exactly once, and normal streaming must be byte-for-byte preserved.
    """

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="tc-m303-lc-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(self.root, ".teamcraft", "teamcraft.db")
        self.service = QueryService(self.db_path)
        self.writer = storage.connect(self.db_path)
        self.addCleanup(self.writer.close)
        domain.create_project(self.writer, "alpha", self.root)
        for index in range(2):
            events.append_event(
                self.writer,
                f"watch.lc_{index}",
                payload_json=json.dumps({"i": index}),
            )
        self.app = create_app(
            query_service=self.service,
            event_stream_config=fast_config(max_duration_seconds=0.05),
        )
        self.token = self.app.state.token_store.ensure()
        self.limiter = self.app.state.sse_connection_limiter
        # Build the router directly against the app's shared limiter (this
        # FastAPI version hides included-router paths behind an opaque
        # _IncludedRouter, so the endpoint is taken from the source router
        # rather than by walking app.routes).
        events_router = stream_module.build_events_router(
            self.service,
            secret_provider=lambda: (self.token,),
            config=self.app.state.sse_config,
            limiter=self.limiter,
        )
        self.endpoint = next(
            route.endpoint
            for route in events_router.routes
            if getattr(route, "path", None) == "/api/v1/events/stream"
        )
        # Capture the ro connections the endpoint opens; installed *after*
        # all test-side writes so only stream connections are recorded.
        self.stream_conns: list[sqlite3.Connection] = []
        real_connect = stream_module.sqlite3.connect

        def spy_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            self.stream_conns.append(conn)
            return conn

        stream_module.sqlite3.connect = spy_connect
        self.addCleanup(setattr, stream_module.sqlite3, "connect", real_connect)

    # -- scripted transport helpers ---------------------------------------------

    def make_scope(self) -> dict:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/events/stream",
            "raw_path": b"/api/v1/events/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"authorization", f"Bearer {self.token}".encode("ascii")),
            ],
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 8000),
        }

    @staticmethod
    def make_receive(script: list[dict]):
        async def _receive() -> dict:
            if script:
                return script.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        return _receive

    def open_endpoint_response(
        self, receive_script: list[dict] | None = None
    ) -> tuple[dict, object]:
        """Call the real handler directly; assert it took exactly one slot.

        The receive script feeds both the Request (which the generator
        polls via ``is_disconnected``) and the ASGI call, so scripted
        disconnects are visible on whichever channel the code consults.
        """

        script = list(receive_script or [])
        scope = self.make_scope()
        self.receive = self.make_receive(script)
        request = Request(scope, receive=self.receive)
        response = self.endpoint(request, None, None)
        self.assertIsInstance(response, stream_module._GuardedStreamingResponse)
        self.assertEqual(self.limiter.active, 1)
        self.assertEqual(len(self.stream_conns), 1)
        return scope, response

    def assert_released_once(self) -> None:
        self.assertEqual(self.limiter.active, 0)
        self.assertEqual(len(self.stream_conns), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.stream_conns[0].execute("SELECT 1")  # must be closed

    @staticmethod
    def collect_send(messages: list[str]):
        async def _send(message: dict) -> None:
            if message["type"] == "http.response.body" and message.get("more_body"):
                messages.append(message["body"].decode("utf-8"))

        return _send

    # -- the edge cases themselves ----------------------------------------------

    def test_send_failure_before_first_chunk_releases_slot_and_conn(self) -> None:
        # The CEO-found leak: transport dies at http.response.start, so the
        # body generator never starts and its finally cannot run.  The
        # guard response's __call__ finally must release everything.
        scope, response = self.open_endpoint_response()
        sent: list[str] = []

        async def failing_start_send(message: dict) -> None:
            sent.append(message["type"])
            raise _SendFailed("transport died at http.response.start")

        with self.assertRaises(_SendFailed):
            asyncio.run(response(scope, self.receive, failing_start_send))
        self.assertEqual(sent, ["http.response.start"])
        self.assert_released_once()
        # Proof the generator itself never ran: afterwards it is still at
        # its very first frame (a started-then-abandoned generator would
        # instead surface its finally during loop shutdown).
        async def first_frame() -> str:
            return await response.body_iterator.__anext__()

        self.assertTrue(asyncio.run(first_frame()).startswith("retry:"))

    def test_send_failure_at_first_body_chunk_releases_slot_and_conn(self) -> None:
        # Generator started (backfill computed) but is abandoned while
        # suspended at its first yield: its finally has not run yet, and
        # would only run at eventual GC — the guard releases immediately.
        scope, response = self.open_endpoint_response()
        sent_types: list[str] = []

        async def failing_body_send(message: dict) -> None:
            if message["type"] == "http.response.body":
                raise _SendFailed("socket closed before the first SSE frame")
            sent_types.append(message["type"])

        with self.assertRaises(_SendFailed):
            asyncio.run(response(scope, self.receive, failing_body_send))
        self.assertEqual(sent_types, ["http.response.start"])
        self.assert_released_once()

    def test_disconnect_after_delivery_releases_immediately(self) -> None:
        # Client vanishes right after the backfilled frames: the live loop
        # notices via is_disconnected polling and the shared cleanup runs.
        scope, response = self.open_endpoint_response(
            [{"type": "http.disconnect"}]
        )
        chunks: list[str] = []
        asyncio.run(response(scope, self.receive, self.collect_send(chunks)))
        lines = "".join(chunks).splitlines()
        self.assertEqual(ids_in(lines), [1, 2])
        self.assertEqual(snapshot_frames(lines), [])  # left via disconnect
        self.assert_released_once()

    def test_normal_completion_streams_frames_and_releases(self) -> None:
        # Normal behavior is byte-visible through the guard: backfilled
        # events, then the explicit max_duration close, then release.
        scope, response = self.open_endpoint_response()
        chunks: list[str] = []
        asyncio.run(response(scope, self.receive, self.collect_send(chunks)))
        lines = "".join(chunks).splitlines()
        frames = parse_frames(lines)
        self.assertEqual(frames[0]["retry"], 250)
        self.assertIn("stream-open", frames[1]["comment"])
        self.assertEqual(ids_in(lines), [1, 2])
        closes = snapshot_frames(lines)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["data"]["reason"], "max_duration")
        self.assert_released_once()

    def test_cleanup_is_idempotent_across_hooks(self) -> None:
        # Both hooks (generator finally + guard finally) fire in normal
        # operation; extra or repeated releases must be silent no-ops and
        # must never underflow the limiter.
        scope, response = self.open_endpoint_response()
        response._cleanup()  # type: ignore[attr-defined]
        response._cleanup()  # type: ignore[attr-defined]
        self.assertEqual(self.limiter.active, 0)
        self.assertFalse(self.limiter.release())  # never negative
        with self.assertRaises(sqlite3.ProgrammingError):
            self.stream_conns[0].execute("SELECT 1")


class M303ConfigAndLimiterChecks(unittest.TestCase):
    def test_config_defaults_are_valid_and_conservative(self) -> None:
        cfg = SseConfig().validate()
        self.assertGreaterEqual(cfg.poll_interval_seconds, 0.01)
        self.assertGreaterEqual(cfg.heartbeat_interval_seconds, 0.05)
        self.assertGreaterEqual(cfg.max_duration_seconds, 1.0)
        self.assertGreaterEqual(cfg.max_backfill_events, cfg.backfill_batch_size)
        self.assertGreaterEqual(cfg.max_frame_bytes, events.MAX_EVENT_PAYLOAD_BYTES)

    def test_config_rejects_broken_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            SseConfig(poll_interval_seconds=0).validate()
        with self.assertRaises(ValidationError):
            SseConfig(backfill_batch_size=0).validate()
        with self.assertRaises(ValidationError):
            SseConfig(backfill_batch_size=1001).validate()
        with self.assertRaises(ValidationError):
            SseConfig(max_backfill_events=1, backfill_batch_size=100).validate()
        with self.assertRaises(ValidationError):
            SseConfig(max_connections=0).validate()
        with self.assertRaises(ValidationError):
            SseConfig(max_frame_bytes=64).validate()

    def test_limiter_bounds_acquire_release_underflow(self) -> None:
        limiter = StreamConnectionLimiter(2)
        self.assertTrue(limiter.try_acquire())
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        self.assertEqual(limiter.active, 2)
        self.assertTrue(limiter.release())
        self.assertEqual(limiter.active, 1)
        self.assertTrue(limiter.release())
        self.assertFalse(limiter.release())  # never goes negative
        with self.assertRaises(ValidationError):
            StreamConnectionLimiter(0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
