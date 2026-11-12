"""M3-06 Chromium browser E2E suite — the roadmap recovery matrix.

Roadmap line (doc/TEAMCRAFT_2_0_M0_REVIEW.md, M3-06): drive the real console
in a real Chromium browser against the real M3-02/M3-03/M3-04 stack, with an
isolated temporary DB/project state and a Fake-Herdr mock runtime, covering:
report-only/scout flows with full management + acceptance through the UI,
CEO/protocol facts, event batching, wake/manual request/failure, DAG cycle,
stale version, ordinary ship waiting for M4, SSE reconnect/backfill/dedup,
backend restart and server absence, explicit queue TTL, duplicate clicks,
long logs and hostile HTML, and browser-disconnect durability.

How this suite runs WITHOUT installing anything
----------------------------------------------
No Playwright/Selenium/puppeteer package and no downloaded browser is used
(the environment forbids network installs and none are needed).  The suite
locates the locally installed Chromium-family browser (Google Chrome on this
machine; env override ``TEAMCRAFT_E2E_CHROME``), launches it headless with
``--remote-debugging-port=0`` against a throwaway profile directory, and
drives it over the DevTools Protocol with a pure-standard-library WebSocket
client (socket + SHA-1 handshake + frame masking).  Clicks are real
``Input.dispatchMouseEvent`` events at the element's rendered coordinates;
DOM facts are read back from the rendered document.  If no Chromium-family
browser is found every test SKIPs with an explicit reason — a skip is NOT
evidence, and the run summary/README say so.

Isolation
---------
Every test class gets its own temporary project root: its own
``.teamcraft/teamcraft.db`` (fresh_start), its own session token file, and
its own ``python3 -m teamcraft.api`` SUBPROCESS on a probed free loopback
port.  Every browser test gets its own tab (fresh sessionStorage).  Nothing
here reads or writes the repository's own ``.teamcraft`` state.

Fake Herdr vs real Herdr — THE LINE THAT IS NEVER CROSSED
---------------------------------------------------------
The mock runtime (``FakeHerdrRuntime``) calls the same Core primitives the
real CLI/runtime uses after Herdr transport results (member sync, brief,
WorkerReport, wake ingest/admit/submit/ack/processed, CEO bootstrap under the
temporary ``assumed_default`` receipt) so the browser sees realistic rows.
It NEVER executes a ``herdr`` binary, never opens a Herdr socket, and never
claims a host-trusted receipt.  Every fact it produces is stored exactly as
Core stores it — with ``receipt_source: assumed_default`` where the real host
receipt is missing.  This suite therefore proves WEB/API/browser behaviour;
real-Herdr prompt delivery, restart recovery, identity drift, GitHub Actions,
and cross-browser behaviour remain **unverified** (recorded in the README and
the M0 review; see M2-08/M2-09 status).

Not covered here (recorded, not claimed): Safari/WebKit and Firefox engines
(only the installed Chromium family is driven), the fenced runtime scheduler
process itself (the fake runtime performs the same Core calls single-shot),
and any real host receipt from Herdr.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:  # the API server + fixture client need the web stack; core suites must
    # stay runnable without it, so this module SKIPs rather than ERRORs.
    import httpx

    from teamcraft.api.command import (
        IDEMPOTENCY_HEADER,
        KIND_APPROVAL_DECISION,
        KIND_CEO_WAKE_REQUEST,
        KIND_SCHEDULER_PAUSE,
        KIND_SCHEDULER_RESUME,
        KIND_TASK_CREATE,
        KIND_TASK_DEPENDENCY_ADD,
        KIND_TASK_POLICY_UPDATE,
    )
    from teamcraft.api.serve import WEB_ROOT

    _WEB_STACK_OK = True
    _WEB_STACK_REASON = ""
except ImportError as exc:  # pragma: no cover - depends on install state
    _WEB_STACK_OK = False
    _WEB_STACK_REASON = f"web dependencies unavailable: {exc}"

from teamcraft import (
    briefs,
    ceo_initialization,
    commands,
    domain,
    events,
    fresh_start,
    leader,
    runtime_adapter,
    storage,
    verification,
    wake,
    worker_reports,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_TOKEN_FILENAME = "session-token"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _find_chromium() -> str | None:
    """Locate an installed Chromium-family browser (never download one)."""
    candidates: list[str] = []
    env = os.environ.get("TEAMCRAFT_E2E_CHROME")
    if env:
        candidates.append(env)
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser(
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for name in ("google-chrome", "chromium", "chromium-browser",
                 "microsoft-edge", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(
                candidate, os.X_OK):
            return candidate
    return None


_CHROME_BINARY = _find_chromium()
_SKIP_REASON = ""
if not _WEB_STACK_OK:
    _SKIP_REASON = _WEB_STACK_REASON
elif not _CHROME_BINARY:
    _SKIP_REASON = ("no installed Chromium-family browser found "
                   "(checked Google Chrome / Chromium / Edge paths and PATH; "
                   "set TEAMCRAFT_E2E_CHROME to point at one; this suite "
                   "never downloads a browser)")

requires_e2e = unittest.skipUnless(_WEB_STACK_OK and _CHROME_BINARY,
                                  _SKIP_REASON or "ok")

#: Injected once per page after load.  Every helper returns JSON-safe values
#: (strings/numbers) so the Python side never has to reason about CDP object
#: wrappers.  Button lookups refuse disabled or invisible elements, so a click
#: helper succeeding means the REAL rendered UI offered that control.
_JS_HELPERS = r"""
window.__e2e = {
  /* Bring an element on-screen and return its CENTRE in viewport coords,
     clamped away from the window edges; returns "" if still invisible.
     Input.dispatchMouseEvent speaks viewport coordinates, so a target
     below the fold MUST be scrolled into view first — otherwise the
     press lands on nothing and every "click" silently does nothing. */
  centre(el) {
    if (!el) return "";
    el.scrollIntoView({block: "center", inline: "nearest"});
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return "";
    const x = Math.min(Math.max(r.x + r.width / 2, 4),
                       window.innerWidth - 4);
    const y = Math.min(Math.max(r.y + r.height / 2, 4),
                       window.innerHeight - 4);
    return JSON.stringify({x: x, y: y});
  },
  rectButton(label) {
    for (const b of document.querySelectorAll("button")) {
      if (!b.disabled && b.textContent.trim() === label) {
        const hit = this.centre(b);
        if (hit) return hit;
      }
    }
    return "";
  },
  rectButtonPrefix(prefix) {
    for (const b of document.querySelectorAll("button")) {
      if (!b.disabled && b.textContent.trim().indexOf(prefix) === 0) {
        const hit = this.centre(b);
        if (hit) return hit;
      }
    }
    return "";
  },
  rectRow(text) {
    for (const tr of document.querySelectorAll("#main table tr")) {
      if (tr.innerText.indexOf(text) !== -1) {
        const b = tr.querySelector("button");
        if (b) {
          const hit = this.centre(b);
          if (hit) return hit;
        }
      }
    }
    return "";
  },
  rectCss(sel) {
    return this.centre(document.querySelector(sel));
  },
  setInput(sel, value) {
    const el = document.querySelector(sel);
    if (!el) return "missing";
    el.value = value;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    return "ok";
  },
  setSelect(sel, value) {
    const el = document.querySelector(sel);
    if (!el) return "missing";
    for (const o of el.options) {
      if (o.value === value) {
        el.value = value;
        el.dispatchEvent(new Event("change", {bubbles: true}));
        return "ok";
      }
    }
    return "no-option:" + value;
  },
  section(prefix) {
    for (const s of document.querySelectorAll("#main section.card")) {
      const h = s.querySelector("h3");
      if (h && h.textContent.indexOf(prefix) === 0) return s;
    }
    return null;
  },
  fillCreateTask(f) {
    const s = this.section("Create task");
    if (!s) return "no-section";
    const ctl = s.querySelectorAll("input, textarea, select");
    /* DOM order in the console's create form:
       0=title 1=description 2=kind 3=priority 4=risk 5=max_attempts
       6=acceptance 7=ownership 8=dependencies — pinned here so markup drift
       fails the E2E loudly instead of silently filling the wrong field. */
    if (ctl.length < 9) return "unexpected-form:" + ctl.length;
    ctl[0].value = f.title;
    ctl[1].value = f.description === undefined ? "" : f.description;
    ctl[2].value = f.kind || "ship";
    ctl[3].value = String(f.priority === undefined ? 50 : f.priority);
    if (f.acceptance) ctl[6].value = f.acceptance;
    if (f.dependencies) ctl[8].value = f.dependencies;
    return "ok";
  },
  fillEdit(f) {
    const s = this.section("Edit mutable fields");
    if (!s) return "no-section";
    const ctl = s.querySelectorAll("input, textarea");
    if (ctl.length < 4) return "unexpected-form:" + ctl.length;
    if (f.title !== undefined && f.title !== null) ctl[0].value = f.title;
    if (f.description !== undefined && f.description !== null) {
      ctl[1].value = f.description;
    }
    if (f.priority !== undefined && f.priority !== null) {
      ctl[2].value = String(f.priority);
    }
    return "ok";
  },
  setPolicyDelivery(value) {
    const s = this.section("Execution policy");
    if (!s) return "no-section";
    const sel = s.querySelector("select");
    if (!sel) return "no-select";
    sel.value = value;
    return "ok";
  },
  setTaskCommandReason(value) {
    const s = this.section("Lifecycle commands");
    if (!s) return "no-section";
    const ctl = s.querySelectorAll("input");
    if (ctl.length < 2) return "unexpected-form:" + ctl.length;
    ctl[1].value = value;
    return "ok";
  },
  setCeoReason(value) {
    const s = this.section("Ask the CEO");
    if (!s) return "no-section";
    const ctl = s.querySelectorAll("input");
    if (ctl.length < 2) return "unexpected-form:" + ctl.length;
    ctl[1].value = value;
    return "ok";
  },
  setDecisionReason(value) {
    const d = document.querySelector("#main div.decision input");
    if (!d) return "no-decision";
    d.value = value;
    return "ok";
  },
  setDepInput(value) {
    const s = this.section("Dependencies");
    if (!s) return "no-section";
    const inputs = s.querySelectorAll("input");
    if (!inputs.length) return "no-input";
    inputs[inputs.length - 1].value = value;
    return "ok";
  },
  setSchedulerReason(value) {
    const s = this.section("Scheduler gate");
    if (!s) return "no-section";
    const el = s.querySelector("input.wide");
    if (!el) return "no-input";
    el.value = value;
    return "ok";
  },
  seqs() {
    const list = document.getElementById("event-list");
    if (!list) return "[]";
    /* Cards render newest-first by design; report seqs ascending so the
       test can compare against the durable SQLite order directly. */
    return JSON.stringify(
      [...list.querySelectorAll(".badge")]
        .map(b => b.textContent.trim())
        .filter(t => /^seq \d+$/.test(t))
        .map(t => Number(t.slice(4)))
        .sort((a, b) => a - b));
  },
  /* jsonBlock() renders previews inside COLLAPSED <details>; innerText
     excludes collapsed content, so previews must be opened before text
     assertions can see them. */
  openDetails() {
    for (const d of document.querySelectorAll("#main details")) d.open = true;
    return "ok";
  },
  banner() {
    const b = document.getElementById("banner");
    return (b && !b.hidden) ? b.textContent : "";
  },
  mainText() { return document.getElementById("main").innerText; },
  bodyText() { return document.body.innerText; },
  logText() { return document.getElementById("result-log").innerText; },
  chip() { return document.getElementById("conn-state").textContent; },
  optionValues() {
    const s = document.getElementById("project-select");
    return JSON.stringify([...s.options].map(o => o.value));
  },
  lastSeq() { return Number(state.lastSeq); },
  streamCount() {
    return JSON.stringify(window.__streamOpens || 0);
  },
};
/* Count openStream invocations: in-flight SSE fetches never appear in
   performance resource timing, so resource entries cannot measure streams. */
if (typeof openStream === "function" && !window.__openStreamWrapped) {
  const origOpenStream = openStream;
  window.openStream = function () {
    window.__streamOpens = (window.__streamOpens || 0) + 1;
    return origOpenStream.apply(this, arguments);
  };
  window.__openStreamWrapped = true;
}
"ok"
"""


class _WebSocket:
    """Minimal RFC 6455 client (text frames, masking, fragmentation)."""

    def __init__(self, ws_url: str, timeout: float = 15.0) -> None:
        assert ws_url.startswith("ws://"), ws_url
        hostport, _, path = ws_url[len("ws://"):].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)),
                                            timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake closed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"websocket handshake refused: {head[:80]!r}")
        expect = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if expect.encode().lower() not in head.lower():
            raise ConnectionError("websocket handshake: bad Sec-WebSocket-Accept")
        self._buf = bytearray(rest)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed mid-frame")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = bytes(random.getrandbits(8) for _ in range(4))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_message(self) -> str | None:
        """Return the next text payload (reassembling fragments); None for a
        control-only exchange (ping already answered)."""
        opcode = None
        payload = bytearray()
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            op = b0 & 0x0F
            if opcode is None:
                opcode = op
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            payload += self._recv_exact(length)
            if fin:
                break
        if opcode == 0x9:  # ping -> pong
            pong = bytearray([0x8A, 0x80 | len(payload)])
            mask = bytes(random.getrandbits(8) for _ in range(4))
            self.sock.sendall(bytes(pong)
                             + mask
                             + bytes(b ^ mask[i % 4]
                                   for i, b in enumerate(payload)))
            return None
        if opcode == 0x8:
            raise ConnectionError("websocket closed by peer")
        return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _Page:
    """One browser tab driven over CDP, with error/dialog telemetry."""

    def __init__(self, controller: "_Chrome", info: dict[str, Any]) -> None:
        self._controller = controller
        self.target_id = str(info["id"])
        self._ws = _WebSocket(str(info["webSocketDebuggerUrl"]))
        self._ws.sock.settimeout(20.0)
        self._next_id = 0
        self.exceptions: list[str] = []
        self.dialogs: list[str] = []
        self._closed = False

    # -- low level ---------------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None,
             timeout: float = 20.0) -> dict[str, Any]:
        self._next_id += 1
        mine = self._next_id
        self._ws.sock.settimeout(timeout)
        self._ws.send_text(json.dumps(
            {"id": mine, "method": method, "params": params or {}}))
        while True:
            raw = self._ws.recv_message()
            if raw is None:
                continue
            message = json.loads(raw)
            if message.get("id") == mine:
                if "error" in message:
                    raise AssertionError(
                        f"CDP {method} failed: {message['error']}")
                return message.get("result", {})
            self._note_event(message)

    def _note_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            exc = details.get("exception") or {}
            text = str(details.get("text") or exc.get("description")
                      or exc or "exception")
            # Our own readiness probe can fire between a page-initiated
            # reload and helper re-injection; that ReferenceError is a test
            # artifact, not console behaviour, so it must not poison the
            # zero-JS-error assertion.  Genuine app errors still land.
            if "__e2e is not defined" in text:
                return
            frames = ((exc.get("stackTrace") or details.get("stackTrace")
                      or {}).get("callFrames") or [])
            if frames:
                top = frames[0]
                text += (f" @ {top.get('url','')}"
                       f":{top.get('lineNumber')}:{top.get('columnNumber')}")
            self.exceptions.append(text[:400])
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                self.exceptions.append(
                    f"console: {entry.get('text', '')}"[:400])
        elif method == "Page.javascriptDialogOpening":
            self.dialogs.append(str(params.get("message", "")))
            # Never let a dialog wedge the headless renderer.
            self._safe_call("Page.handleJavaScriptDialog",
                           {"accept": True, "promptText": ""})

    def _safe_call(self, method: str,
                   params: dict[str, Any] | None = None) -> None:
        try:
            self._next_id += 1
            mine = self._next_id
            self._ws.sock.settimeout(5.0)
            self._ws.send_text(json.dumps(
                {"id": mine, "method": method, "params": params or {}}))
        except OSError:
            pass

    def drain(self, timeout: float = 0.05) -> None:
        """Read whatever events already arrived (best effort)."""
        try:
            self._ws.sock.settimeout(timeout)
            while True:
                raw = self._ws.recv_message()
                if raw is None:
                    continue
                message = json.loads(raw)
                if "method" in message:
                    self._note_event(message)
        except (socket.timeout, TimeoutError):
            return
        except ConnectionError:
            return

    # -- scripting ----------------------------------------------------------

    def js(self, expression: str, timeout: float = 20.0) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
        }, timeout=timeout)
        details = result.get("exceptionDetails")
        if (details and "__e2e is not defined" in str(details)
                and "__e2e." in expression):
            # A page-initiated reload (Forget button, location.reload) drops
            # injected helpers; re-inject against the live document once and
            # retry, so telemetry survives navigations we did not start.
            self.js(_JS_HELPERS, timeout=timeout)
            return self.js(expression, timeout=timeout)
        if details:
            raise AssertionError(
                f"page JS raised: {result['exceptionDetails']}")
        return (result.get("result") or {}).get("value")

    def rpc(self, name: str, *args: Any) -> Any:
        rendered = ", ".join(json.dumps(a) for a in args)
        return self.js(f"__e2e.{name}({rendered})")

    def wait(self, expression: str, *, timeout: float = 15.0,
             what: str = "", interval: float = 0.1) -> Any:
        """Poll an expression until it yields a truthy value."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            last = self.js(expression)
            if last not in (None, False, "", 0, "0"):
                return last
            time.sleep(interval)
        self._diag()
        raise AssertionError(
            f"timed out after {timeout}s waiting for {what or expression}"
            f" (last value: {str(last)[:200]!r})")

    def _diag(self) -> None:  # debug aid: dump page state on failure paths
        try:
            print("DIAG mainText:", str(self.rpc("mainText"))[:300]
                  .replace("\n", " | "))
            print("DIAG exceptions:", self.exceptions[:3])
            print("DIAG dialogs:", self.dialogs[:2])
            print("DIAG url:", str(self.js("location.href")))
            print("DIAG pending-ish:", str(self.js(
                "[...document.querySelectorAll('#main .loading, #main .state"
                "box, #banner')].map(e=>e.id+':'+e.textContent.slice(0,60))"
                ".join(' ;; ')")))
        except Exception as exc:  # pragma: no cover
            print("DIAG unavailable:", exc)

    # -- actions -------------------------------------------------------------

    def open(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})
        self.wait("document.readyState === 'complete'",
                 what=f"page load of {url}", timeout=20)
        self.js(_JS_HELPERS)

    def _click_rect(self, rect_json: str) -> None:
        rect = json.loads(rect_json)
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": rect["x"], "y": rect["y"]})
        self.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": rect["x"], "y": rect["y"],
            "button": "left", "clickCount": 1, "buttons": 1})
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": rect["x"], "y": rect["y"],
            "button": "left", "clickCount": 1, "buttons": 0})

    def _press_at(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y})
        self.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1, "buttons": 1})
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1, "buttons": 0})

    def _click_targeted(self, probe: str, hit=None, *,
                        timeout: float = 15.0, what: str = "") -> None:
        """Measure a rect, re-measure right before pressing, hit-test the
        point, and only press when everything agrees.  The console's header
        grows when badges populate, and in headless Chrome an input event
        dispatched at stale coordinates lands on whatever element moved
        into that pixel — silently doing nothing.  Still exactly ONE press
        per attempt (no duplicate side effects).  ``hit(x, y)`` is an
        optional JS-expression callable that must return truthy at the press
        point for the press to fire."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            raw = self.wait(probe, timeout=1.0, what=what or probe,
                           interval=0.05)
            r1 = json.loads(str(raw))
            raw2 = self.js(probe)
            if raw2 in ("", None):
                continue
            r2 = json.loads(str(raw2))
            if abs(r2["x"] - r1["x"]) > 1.5 or abs(r2["y"] - r1["y"]) > 1.5:
                last = ("shifted", r1, r2)
                time.sleep(0.05)
                continue
            if hit is not None:
                ok = self.js(hit(r2["x"], r2["y"]))
                if not ok:
                    at = self.js(
                        "(function(){const e=document.elementFromPoint("
                        f"{r2['x']}, {r2['y']}); return e ? e.tagName + "
                        "'[' + (e.textContent||'').slice(0,40) + ']' "
                        ": 'nothing';})()")
                    last = ("hit-miss", at)
                    time.sleep(0.05)
                    continue
            self._press_at(r2["x"], r2["y"])
            return
        raise AssertionError(f"click target never stabilized/hit: "
                           f"{what or probe} (last: {str(last)[:160]!r})")

    def click_button(self, label: str, *, timeout: float = 15.0) -> None:
        lit = json.dumps(label)
        self._click_targeted(
            f"__e2e.rectButton({lit})",
            hit=lambda x, y: (
                "(function(){const e=document.elementFromPoint("
                + str(x) + ", " + str(y) + ");const b=e&&e.closest('button');"
                + "return !!(b && b.textContent.trim()===" + lit + ");})()"),
            timeout=timeout, what=f"enabled button {label!r}")

    def click_row_button(self, row_text: str, *,
                        timeout: float = 15.0) -> None:
        lit = json.dumps(row_text)
        self._click_targeted(
            f"__e2e.rectRow({lit})",
            hit=lambda x, y: (
                "(function(){const e=document.elementFromPoint("
                + str(x) + ", " + str(y) + ");const tr=e&&e.closest('tr');"
                + "return !!(tr && tr.innerText.indexOf(" + lit
                + ") !== -1);})()"),
            timeout=timeout, what=f"table row {row_text!r}")

    def click_css(self, selector: str, *, timeout: float = 15.0) -> None:
        lit = json.dumps(selector)
        self._click_targeted(
            f"__e2e.rectCss({lit})",
            hit=lambda x, y: (
                "(function(){const e=document.elementFromPoint("
                + str(x) + ", " + str(y) + ");return !!(e && e.closest("
                + lit + "));})()"),
            timeout=timeout, what=f"element {selector!r}")

    def double_click_button(self, label: str, *, timeout: float = 15.0) -> bool:
        """Real duplicate click: two mouse presses as fast as the transport
        allows.  Returns True when the second press was even possible (the
        console disables the button while a write is in flight)."""
        rect = self.wait(f"__e2e.rectButton({json.dumps(label)})",
                        timeout=timeout, what=f"enabled button {label!r}")
        r = json.loads(str(rect))
        for _ in range(2):
            self.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": r["x"], "y": r["y"]})
            self.call("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": r["x"], "y": r["y"],
                "button": "left", "clickCount": 1, "buttons": 1})
            self.call("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": r["x"], "y": r["y"],
                "button": "left", "clickCount": 1, "buttons": 0})
        # Was the control still enabled at the moment of the second press?
        still_enabled = self.rpc(
            "rectButton", label) not in ("", None)
        return bool(still_enabled)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._controller.close_target(self.target_id)
        self._ws.close()


class _Chrome:
    """Process-wide headless Chromium manager (one launch, many tabs)."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._profile: str | None = None
        self._version: str = ""

    # -- lifecycle -----------------------------------------------------------

    def _launch(self) -> None:
        assert _CHROME_BINARY is not None
        self._profile = tempfile.mkdtemp(prefix="tc-m306-chrome-")
        flags = [
            _CHROME_BINARY,
            f"--user-data-dir={self._profile}",
            "--headless=new",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-sync",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--password-store=basic",
            "--use-mock-keychain",
            "--mute-audio",
            "--window-size=1280,900",
            "--hide-scrollbars",
            "about:blank",
        ]
        self._proc = subprocess.Popen(flags, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        port_file = Path(self._profile) / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if port_file.exists():
                try:
                    self._port = int(
                        port_file.read_text(encoding="utf-8")
                        .splitlines()[0].strip())
                    if self._port > 0:
                        break
                except (ValueError, IndexError):
                    pass
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"Chromium exited early (code {self._proc.returncode})")
            time.sleep(0.05)
        else:
            raise RuntimeError("Chromium did not expose DevToolsActivePort")
        version = self._http("/json/version")
        self._version = str(version.get("Browser", "unknown"))

    def ensure(self) -> int:
        if self._proc is None or self._proc.poll() is not None:
            self.shutdown()
            self._launch()
        assert self._port is not None
        return self._port

    def shutdown(self) -> None:
        proc, self._proc = self._proc, None
        self._port = None
        profile, self._profile = self._profile, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if profile:
            shutil.rmtree(profile, ignore_errors=True)

    # -- targets --------------------------------------------------------------

    def _http(self, path: str, method: str = "GET") -> dict[str, Any]:
        assert self._port is not None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}", method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def new_page(self) -> _Page:
        self.ensure()
        info = self._http("/json/new?url=about%3Ablank", method="PUT")
        try:
            # Make the new tab the active one so input events route to it
            # reliably in headless "new" mode.
            self._http(f"/json/activate/{info['id']}")
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            pass
        page = _Page(self, info)
        page.call("Runtime.enable")
        page.call("Page.enable")
        return page

    def close_target(self, target_id: str) -> None:
        try:
            # Chrome replies "Target is closing" (plain text, not JSON) —
            # any reply or transport error means the tab is going away,
            # which is all we wanted.
            self._http(f"/json/close/{target_id}")
        except (urllib.error.URLError, OSError, AssertionError,
                json.JSONDecodeError, ValueError):
            pass

    @property
    def version(self) -> str:
        return self._version


_CHROME_CTRL = _Chrome()
atexit.register(_CHROME_CTRL.shutdown)


class _Backend:
    """The API server as a subprocess against one isolated project root."""

    def __init__(self, project_root: str) -> None:
        self.root = project_root
        self.port: int = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr: list[str] = []
        self._reader: threading.Thread | None = None

    # -- process --------------------------------------------------------------

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def start(self) -> None:
        last_error = "unknown"
        # Reuse the previous port when restarting: an already-open page has
        # the old base URL baked in and must find the server again.
        preferred = [self.port] if self.port else []
        ports = preferred + [self._free_port() for _ in range(4)]
        for port in ports:
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-B", "-m", "teamcraft.api",
                     "--project-root", self.root, "--port", str(port)],
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except OSError as exc:
                last_error = f"spawn failed on {port}: {exc}"
                continue

            def _drain() -> None:
                assert proc.stderr is not None
                for line in proc.stderr:
                    self._stderr.append(line.rstrip())

            reader = threading.Thread(target=_drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self._alive(port):
                    self.port = port
                    self._proc = proc
                    self._reader = reader
                    return
                if proc.poll() is not None:
                    last_error = (
                        f"server exited early rc={proc.returncode}: "
                        + " | ".join(self._stderr[-5:]))
                    break
                time.sleep(0.05)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise RuntimeError(f"test API server failed to start: {last_error}")

    @staticmethod
    def _alive(port: int) -> bool:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/health/live",
                    timeout=1.5) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def stop(self, *, kill: bool = False) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if kill or proc.poll() is None:
            if kill:
                proc.kill()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def wait_up(self, *, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._alive(self.port):
                return
            time.sleep(0.05)
        raise AssertionError(
            "backend did not come back up in time; stderr tail: "
            + " | ".join(self._stderr[-8:]))

    def wait_down(self, *, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._alive(self.port):
                return
            time.sleep(0.05)
        raise AssertionError("backend is still answering; restart not proven")

    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # -- facts ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def console_url(self) -> str:
        return f"{self.base_url}/static/index.html"

    def token(self) -> str:
        path = Path(self.root) / ".teamcraft" / "api" / _TOKEN_FILENAME
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError(f"session token file not found at {path}")


class FakeHerdrRuntime:
    """Mock host runtime for the E2E fixtures — never touches real Herdr.

    Every method mirrors exactly one Core primitive the real runtime/CLI uses
    AFTER Herdr returns, and records the pretend transport line in
    ``self.transcript``.  No ``herdr`` binary is spawned and no Herdr socket
    is opened anywhere in this file; results are labelled with whatever the
    Core stores (e.g. ``receipt_source=assumed_default``) — never as a
    host-trusted receipt.
    """

    NATIVE_SESSION = "native-e2e-1"

    def __init__(self, conn: storage.TeamCraftConnection, project_id: str,
                 root: str, team_id: str) -> None:
        self.conn = conn
        self.project_id = project_id
        self.root = root
        self.team_id = team_id
        self.transcript: list[str] = []

    # -- lease ------------------------------------------------------------------

    def lease(self, ttl_seconds: int = 600) -> leader.Lease:
        lease = leader.acquire_or_renew(
            self.conn, leader.project_scope(self.project_id),
            "fake-herdr-e2e", ttl_seconds=ttl_seconds)
        self.transcript.append(f"lease acquired (epoch {lease.epoch})")
        return lease

    def release(self, lease: leader.Lease) -> None:
        leader.release(self.conn, lease.scope, "fake-herdr-e2e",
                      epoch=lease.epoch)
        self.transcript.append("lease released")

    # -- team/session identity ----------------------------------------------------

    def sync_ceo(self, live: str) -> None:
        self._team_state = {
            "version": runtime_adapter.STATE_VERSION,
            "team_id": self.team_id,
            "project_root": str(self.root),
            "herdr_session": {"server_id": "srv-m306",
                             "socket_path": "/tmp/herdr-m306.sock"},
            "created_at": 1,
            "members": {
                "ceo": {
                    "name": "ceo", "role": "ceo",
                    "responsibility": "lead", "kind": "codex",
                    "task": "bootstrap e2e", "ownership": [],
                    "read_only": True, "cwd": str(self.root),
                    "from_pane_id": "w1:p1", "pane_id": "w1:p1",
                    "terminal_id": "terminal-m306",
                    "agent_session": {"kind": "native",
                                    "value": self.NATIVE_SESSION},
                    "agent_args": [], "opencode_config_content": None,
                    "direction": None, "start_timeout_ms": 30_000,
                    "phase": "prompt_submitted",
                    "prompt_may_have_been_delivered": True,
                    "live_state": live, "error": None,
                    "task_id": None, "attempt_id": None, "session_id": None,
                    "created_at": 1, "updated_at": 1,
                },
            },
        }
        runtime_adapter.sync_state(self.conn, self._team_state)
        self.transcript.append(
            f"fake herdr: agent session observed live_state={live}")

    def set_live(self, live: str) -> None:
        member = self._team_state["members"]["ceo"]
        member["live_state"] = live
        member["updated_at"] = int(time.time())
        runtime_adapter.sync_state(self.conn, self._team_state)
        self.transcript.append(
            f"fake herdr: agent session observed live_state={live}")

    # -- CEO bootstrap (temporary assumed_default receipt) -----------------------

    def init_ceo(self) -> tuple[ceo_initialization.CEOInitialization, str]:
        record, nonce = ceo_initialization.discover_and_issue(
            self.conn, self.project_id, "ceo")
        self.transcript.append(
            f"fake herdr: protocol discovered {record.protocol_version}")
        lease = self.lease()
        try:
            ceo_initialization.mark_bootstrap_unknown(
                self.conn, self.project_id, nonce, lease=lease)
            self.transcript.append(
                "fake herdr: bootstrap prompt submitted (unverified)")
            initialized = ceo_initialization.record_bootstrap_submitted_assumed(
                self.conn, self.project_id, nonce,
                native_session_id=self.NATIVE_SESSION,
                agent_session={"value": self.NATIVE_SESSION},
                session_state="idle", lease=lease)
        finally:
            self.release(lease)
        self.transcript.append(
            "fake herdr: NO host receipt — recorded assumed_default")
        return initialized, nonce

    # -- dispatch / worker / verification ------------------------------------------

    def dispatch(self, task_id: str) -> domain.Attempt:
        task = domain.get_task(self.conn, task_id)
        if task.status == "pending":
            task = domain.apply_task_event(
                self.conn, task_id, "dependencies_satisfied",
                expected_version=task.version)
        attempt = domain.create_attempt(
            self.conn, task_id, expected_version=task.version,
            worktree_path=str(self.root))
        domain.transition_attempt(self.conn, attempt.id, "start")
        attempt = domain.transition_attempt(self.conn, attempt.id, "activated")
        self.transcript.append(
            f"fake herdr: pane split + agent started for {task_id}")
        return attempt

    def brief(self, attempt_id: str, content: str) -> briefs.AttemptBrief:
        made = briefs.create_attempt_brief(self.conn, attempt_id,
                                          content=content)
        self.transcript.append("fake herdr: versioned prompt delivered")
        return made

    def worker_done(self, task_id: str, attempt_id: str,
                   brief: briefs.AttemptBrief,
                   summary: str = "e2e work finished") -> Any:
        report = {
            "schema_version": 1, "task_id": task_id, "attempt_id": attempt_id,
            "task_version": brief.task_version,
            "brief_digest": brief.brief_digest, "report_kind": "final",
            "outcome": "completed", "milestone_id": "final",
            "summary": summary, "artifact_digest": "a" * 64,
            "usage": {"total_tokens": None},
        }
        stored = worker_reports.record_worker_report(self.conn, report)
        task = domain.get_task(self.conn, task_id)
        domain.apply_task_event(self.conn, task_id, "worker_reported_done",
                               expected_version=task.version)
        self.transcript.append("fake herdr: final WorkerReport received")
        return stored

    def configure_verifier(self, code: str, key: str) -> dict[str, Any]:
        argv = [sys.executable, "-B", "-c", code]
        lease = self.lease()
        try:
            made = verification.configure_verifier(
                self.conn, self.project_id, argv=argv,
                rules_version="rules-e2e", timeout_seconds=30,
                actor="e2e-operator", idempotency_key=key, lease=lease)
        finally:
            self.release(lease)
        self.transcript.append("verifier configured (local python, no network)")
        return made

    def run_verification(self, attempt_id: str, key: str) -> Any:
        lease = self.lease()
        try:
            result = verification.run_verification(
                self.conn, attempt_id, idempotency_key=key, lease=lease)
        finally:
            self.release(lease)
        self.transcript.append(f"verifier exited {result.status}")
        return result

    # -- wakes ----------------------------------------------------------------------

    def progress_events(self, count: int, dedupe: str) -> None:
        for i in range(count):
            events.append_event(
                self.conn, "watch.task_progress", project_id=self.project_id,
                payload_json=json.dumps({
                    "dedupe_key": dedupe, "priority": 25,
                    "note": f"e2e progress {dedupe} #{i}"}))
        self.transcript.append(
            f"fake herdr: {count} progress observations folded in")

    def tick_ingest(self) -> wake.IngestResult:
        lease = self.lease()
        try:
            result = wake.ingest_events(self.conn, self.project_id, lease)
        finally:
            self.release(lease)
        self.transcript.append(
            f"fake tick: ingested {result.scanned_events} events "
            f"({result.created_wakes} wake(s) created, "
            f"{result.merged_events} merged)")
        return result

    def wake_state(self, wake_id: str) -> str:
        return str(self.conn.execute(
            "SELECT state FROM wakes WHERE id=?", (wake_id,)).fetchone()[0])

    def pending_wake_ids(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT id FROM wakes WHERE project_id=? AND state='pending'"
            " ORDER BY first_seq", (self.project_id,)).fetchall()
        return [str(row[0]) for row in rows]

    def admit(self, wake_id: str) -> wake.AdmissionResult:
        lease = self.lease()
        try:
            result = wake.admit_pending_wake(
                self.conn, self.project_id, wake_id, lease)
        finally:
            self.release(lease)
        self.transcript.append(
            f"fake herdr: wake {wake_id[:8]} admission -> "
            f"{result.deferred_reason or 'sending'}")
        return result

    def submitted(self, wake_id: str) -> wake.Wake:
        lease = self.lease()
        try:
            result = wake.record_submission_result(
                self.conn, self.project_id, wake_id, lease,
                outcome="submitted",
                receipt_json=json.dumps({"transport": "fake-herdr"}))
        finally:
            self.release(lease)
        self.transcript.append(
            f"fake herdr: prompt submission confirmed (transport only,"
            f" not a host receipt)")
        return result

    def unknown(self, wake_id: str, detail: str) -> wake.Wake:
        lease = self.lease()
        try:
            result = wake.record_submission_result(
                self.conn, self.project_id, wake_id, lease,
                outcome="unknown", detail=detail)
        finally:
            self.release(lease)
        self.transcript.append(f"fake herdr: transport outcome unknown: {detail}")
        return result

    def acknowledge(self, wake_obj: wake.Wake) -> wake.Wake:
        lease = self.lease()
        try:
            result = wake.acknowledge_wake(
                self.conn, self.project_id, wake_obj.id, lease,
                target_session_id=self.NATIVE_SESSION,
                acknowledged_through_seq=wake_obj.last_seq)
        finally:
            self.release(lease)
        self.transcript.append("fake herdr: CEO acknowledged the merged wake")
        return result

    def processed(self, wake_id: str) -> wake.Wake:
        lease = self.lease()
        try:
            result = wake.mark_processed(
                self.conn, self.project_id, wake_id, lease)
        finally:
            self.release(lease)
        self.transcript.append("fake herdr: wake processed")
        return result

    def queue_manual_request(self, key: str, ttl_seconds: int) -> commands.Command:
        command, _reused = wake.request_ceo_attention(
            self.conn, self.project_id, actor="fake-runtime",
            reason=f"ttl probe {key}", idempotency_key=key,
            ttl_seconds=ttl_seconds)
        return command

    def sweep(self) -> list[tuple[str, str]]:
        return commands.sweep_stale_commands(self.conn)

    def assert_never_touched_real_herdr(self) -> None:
        """The transcript is fake by construction: pretend transport lines
        only — no binary spawn, no socket connect, no host-trusted claim."""
        for line in self.transcript:
            assert "spawned" not in line, line
            assert "connect" not in line, line
            assert "host-trusted" not in line, line


class _E2EBase(unittest.TestCase):
    """Temporary project root + subprocess backend + Chrome tab per test."""

    make_project = True
    project_name = "m306"
    team_id = "team-m306"
    init_ceo = False  # subclass flips on for the fake-herdr wake scenario

    # -- fixture ------------------------------------------------------------

    def _prepare_root(self, root: str) -> None:
        """Hook: seed files needed before the backend starts."""

    def _after_project(self) -> None:
        """Hook: seed durable facts after the project row exists."""

    def setUp(self) -> None:
        _CHROME_CTRL.ensure()
        self.root = tempfile.mkdtemp(prefix="tc-m306-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._prepare_root(self.root)
        fresh_start.initialize_project_root(self.root)
        self.db_path = os.path.join(self.root, ".teamcraft", "teamcraft.db")
        self.backend = _Backend(self.root)
        self.addCleanup(self.backend.stop)
        self.backend.start()
        self.token = self.backend.token()
        self.conn = storage.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.project_id = ""
        if self.make_project:
            self.project_id = domain.create_project(
                self.conn, self.project_name, self.root).id
        self.fake = FakeHerdrRuntime(self.conn, self.project_id, self.root,
                                    self.team_id) if self.project_id else None
        if self.project_id and self.init_ceo and self.fake is not None:
            self.fake.sync_ceo("idle")
            self.fake.init_ceo()
        self._after_project()
        self.page = _CHROME_CTRL.new_page()
        self.addCleanup(self.page.close)

    # -- HTTP helpers (the console's exact request shape) ----------------------

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.backend.base_url, timeout=15)

    def api_get(self, path: str) -> dict[str, Any]:
        with self.client() as client:
            response = client.get(path, headers={
                "Authorization": f"Bearer {self.token}"})
        assert response.status_code == 200, f"{path}: {response.text[:300]}"
        return response.json()

    def api_write(self, method: str, path: str, body: dict[str, Any],
                 key: str) -> tuple[int, dict[str, Any]]:
        with self.client() as client:
            response = client.request(
                method, path, json=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-CSRF-Token": self.token,
                    IDEMPOTENCY_HEADER: key,
                    "Origin": self.backend.base_url,
                })
        return response.status_code, response.json()

    # -- browser helpers ----------------------------------------------------------

    def open_console(self) -> None:
        self.page.open(self.backend.console_url)
        self.page.wait("__e2e.chip().indexOf('up') !== -1",
                      what="connection chip resolves to server: up")
        self.assertIn("TeamCraft console", self.page.js("document.title"))

    def login(self) -> None:
        self.assertEqual("ok", self.page.rpc("setInput", "#token-input",
                                            self.token))
        self.page.click_button("Save")
        self.page.wait(
            "__e2e.mainText().indexOf('Connect') === -1",
            what="gate replaced after token save")

    def goto_view(self, tab: str) -> None:
        self.page.click_button(tab)
        self.page.wait("document.querySelector('#main').innerText.length > 0",
                      what=f"{tab} view rendered")

    def select_project(self) -> None:
        values = json.loads(self.page.rpc("optionValues"))
        self.assertIn(self.project_id, values)
        self.assertEqual("ok", self.page.rpc("setSelect", "#project-select",
                                            self.project_id))

    def create_task_via_ui(self, *, title: str, description: str = "",
                          kind: str = "ship", priority: int = 50,
                          acceptance: str = "",
                          dependencies: str = "") -> None:
        payload = {"title": title, "description": description, "kind": kind,
                   "priority": priority}
        if acceptance:
            payload["acceptance"] = acceptance
        if dependencies:
            payload["dependencies"] = dependencies
        # Self-healing fill: a transient re-render (post-write renderView)
        # can remove the form between navigation and fill; retry until the
        # SAME evaluation both finds the form and fills it.
        status = ""
        for _ in range(50):
            status = str(self.page.rpc("fillCreateTask", payload))
            if status == "ok":
                break
            time.sleep(0.1)
        self.assertEqual("ok", status)
        self.page.click_button("Create task")

    def _fill(self, name: str, *args: Any, timeout: float = 10.0) -> None:
        """Run a form-fill RPC until the target section exists; a transient
        post-write re-render can briefly remove forms."""
        status = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = str(self.page.rpc(name, *args))
            if status == "ok":
                return
            time.sleep(0.1)
        self.fail(f"{name} never became fillable (last: {status!r})")

    def wait_log(self, fragment: str, *, timeout: float = 15.0) -> str:
        text = self.page.wait(
            f"__e2e.logText().indexOf({json.dumps(fragment)}) !== -1"
            " ? __e2e.logText() : ''",
            timeout=timeout, what=f"log line containing {fragment!r}")
        return str(text)

    # -- durable facts ------------------------------------------------------------

    def table_count(self, table: str, where: str = "",
                    args: tuple[Any, ...] = ()) -> int:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.conn.execute(sql, args).fetchone()[0])

    def scalar(self, sql: str, args: tuple[Any, ...] = ()) -> Any:
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row is not None else None

    def rest_event_seqs(self) -> list[int]:
        payload = self.api_get(
            f"/api/v1/events?project_id={self.project_id}&limit=200")
        return [int(item["seq"]) for item in payload["items"]]

    def assert_no_page_errors(self) -> None:
        self.page.drain()
        self.assertEqual([], self.page.exceptions,
                         "uncaught JS errors in the real browser")
        self.assertEqual([], self.page.dialogs,
                         "a JS dialog opened — hostile markup executed?!")

    def assert_no_herdr_subprocess(self) -> None:
        """This module spawns only the API server and Chromium."""
        if self.fake is not None:
            self.fake.assert_never_touched_real_herdr()


# ---------------------------------------------------------------------------
# 1. Preflight: real Chromium renders the real console
# ---------------------------------------------------------------------------


@requires_e2e
class M306ChromiumPreflightTests(_E2EBase):
    make_project = False

    def test_installed_chromium_launches_and_is_recorded(self) -> None:
        version = _CHROME_CTRL.ensure() and _CHROME_CTRL.version
        self.assertTrue(
            version.startswith("Chrome/") or version.startswith("Chromium/"),
            f"unexpected browser family: {version!r}")
        major = int(version.split("/")[1].split(".")[0])
        self.assertGreaterEqual(major, 100,
                               f"Chromium family too old: {version}")

    def test_console_shell_renders_in_real_browser(self) -> None:
        self.open_console()
        # The public shell renders before any auth, honestly shows the gate.
        body = self.page.rpc("bodyText")
        self.assertIn("TeamCraft console", body)
        self.assertIn("Connect", body)
        self.assertIn("up", self.page.rpc("chip"),
                      "public liveness probe must resolve in the browser")
        # app.js really executed (globals exist) — not a static snapshot.
        self.assertTrue(self.page.js("typeof boot === 'function'"))
        self.assertTrue(self.page.js("typeof openStream === 'function'"))
        # Assets came from the confined route inside the browser:
        src = self.page.js(
            "document.querySelectorAll('script[src=\"app.js\"]').length")
        self.assertEqual(1, src)
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 2. Auth states + server absence (real offline behaviour)
# ---------------------------------------------------------------------------


@requires_e2e
class M306AuthAndServerAbsenceTests(_E2EBase):
    make_project = False

    def test_honest_empty_store_and_token_lifecycle(self) -> None:
        self.open_console()
        self.login()
        # Honest emptiness comes from the store, not a placeholder row:
        # the select holds exactly the no-project option and the pane
        # states the truth instead of rendering demo data.
        self.page.wait(
            "__e2e.mainText().indexOf('No project exists') !== -1",
            timeout=25, what="honest no-project state")
        values = json.loads(self.page.rpc("optionValues"))
        self.assertEqual([""], values,
                         "empty store must present the honest no-project option")
        self.assertIn("(no projects in the store)", self.page.js(
            "document.querySelector('#project-select option').textContent"))
        # A wrong token gets the classified 401 state, never echoed back.
        bogus = "totally-wrong-token-000000"
        self.page.rpc("setInput", "#token-input", bogus)
        self.page.click_button("Save")
        self.page.wait(
            "__e2e.banner().indexOf('Authentication failed') !== -1",
            timeout=25, what="401 auth banner")
        self.assertNotIn(bogus, self.page.rpc("bodyText"))
        # Forget clears the tab-local copy; the gate comes back after reload.
        self.page.click_button("Forget")
        self.page.wait("document.readyState === 'complete'",
                      what="reload after Forget")
        self.page.wait("__e2e.chip().indexOf('up') !== -1",
                      what="chip up after reload")
        self.page.wait("__e2e.mainText().indexOf('Connect') !== -1",
                      what="Connect gate re-appears after Forget")
        self.assertNotIn(self.token, self.page.rpc("bodyText"))

    def test_server_absence_is_a_distinct_state_and_recovers(self) -> None:
        self.open_console()
        self.login()
        # Kill the backend WHILE the tab lives: this is the real absence.
        self.backend.stop()
        self.backend.wait_down()
        self.page.click_css("#refresh")
        self.page.wait("__e2e.chip() === 'server: unreachable'",
                      what="unreachable chip")
        banner = self.page.rpc("banner")
        self.assertIn("API unreachable", banner)
        # The app stays interactive and honest while offline (no crash, no
        # fabricated data, no lost page state): the shell, its nav, and the
        # write-activity log remain, and the project select honestly says
        # the store could not be read into options.
        body = self.page.rpc("bodyText").lower().replace("\n", " ")
        self.assertIn("overview", body)
        self.assertIn("write activity", body)
        self.assertIn("no project exists in the store yet", body)
        self.assertNotIn("Project overview", self.page.rpc("mainText"),
                        "offline must NOT fabricate an overview")
        self.assert_no_page_errors()
        # Restart: Refresh re-proves liveness and projects load again.
        self.backend.start()
        self.page.click_css("#refresh")
        self.page.wait("__e2e.chip().indexOf('up') !== -1",
                      what="chip up after backend restart")
        self.page.wait(
            f"__e2e.optionValues().indexOf({json.dumps(self.project_id)})"
            " !== -1 ? JSON.stringify(true) : ''",
            timeout=25, what="projects load again after recovery")
        self.assertIn(self.project_id,
                     json.loads(self.page.rpc("optionValues")))
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 3. report-only/scout: full management + acceptance through the browser
# ---------------------------------------------------------------------------


@requires_e2e
class M306ScoutFlowTests(_E2EBase):
    project_name = "scout-e2e"
    team_id = "team-scout-e2e"

    def test_full_report_only_management_and_acceptance_in_browser(self) -> None:
        assert self.fake is not None and self.project_id
        self.open_console()
        self.login()
        self.select_project()

        # -- create via the real form ----------------------------------------
        self.goto_view("Tasks")
        self.page.wait("__e2e.mainText().indexOf('Create task') !== -1",
                      what="create form visible")
        self.create_task_via_ui(
            title="e2e scout: inspect the store",
            description="read-only inspection; report findings as text",
            kind="scout", priority=60,
            acceptance="produces a verification artifact")
        self.wait_log("create task")
        self.page.wait(
            f"__e2e.mainText().indexOf({json.dumps('e2e scout')}) !== -1",
            what="task row appears")
        task_id = str(self.scalar(
            "SELECT id FROM tasks WHERE project_id=? ORDER BY created_at"
            " LIMIT 1", (self.project_id,)))

        # -- mock host dispatch, worker report, verifier, verification ------
        attempt = self.fake.dispatch(task_id)
        brief = self.fake.brief(attempt.id, "M3-06 scout brief: inspect only")
        self.fake.worker_done(task_id, attempt.id, brief,
                             summary="scout finished; artifact attached")
        hostile = "<img src=x onerror=alert(777)>"
        self.fake.configure_verifier(
            "import sys; print('PASS e2e verifier')\n"
            f"print({hostile!r})\n"
            "print('L'*20000)\n",
            key="verifier-e2e-1")
        verdict = self.fake.run_verification(attempt.id, key="verify-e2e-1")
        self.assertEqual("pass", verdict.status)

        # -- the UI surfaces the verification and accepts it ----------------
        self.goto_view("Approvals")
        self.page.wait("__e2e.mainText().indexOf('awaiting a decision') !== -1"
                      " && __e2e.rectButton('Accept') !== ''",
                      what="decision form with Accept button")
        self._fill("setDecisionReason", "artifact reviewed by the e2e run")
        self.page.click_button("Accept")
        self.wait_log("approval accept")
        self.page.wait("__e2e.mainText().indexOf('accept') !== -1",
                      what="recorded decision visible")

        # -- task reaches done via the task detail view ----------------------
        self.goto_view("Tasks")
        self.page.click_row_button(task_id)
        # Gate on detail-only copy: the loading spinner also contains the
        # task id, so waiting on the id alone races the spinner.
        self.page.wait("__e2e.mainText().indexOf('Back to list') !== -1",
                      what="task detail")
        detail = self.page.rpc("mainText")
        self.assertIn("done", detail)

        # attempt detail loads and renders the verifier artifact preview as
        # TEXT — hostile markup stays inert, long output is truncated-flagged.
        self.page.wait("__e2e.rectButtonPrefix('Load attempt detail') !== ''",
                      what="attempt detail button")
        # The button label is long; click it via prefix probe with the
        # same settle + hit-test discipline as every other click.
        self.page._click_targeted(  # noqa: SLF001 - internal helper
            "__e2e.rectButtonPrefix(" + json.dumps("Load attempt detail") + ")",
            hit=lambda x, y: (
                "(function(){const e=document.elementFromPoint("
                + str(x) + ", " + str(y) + ");const b=e&&e.closest('button');"
                + "return !!(b&&b.textContent.trim().indexOf("
                "'Load attempt detail')===0);})()"),
            what="button starting with 'Load attempt detail'")
        # The verifier artifact preview lives in a collapsed <details>
        # (jsonBlock); open every preview on each poll so innerText-based
        # assertions can see it once the detail fetch commits.
        self.page.wait(
            "(__e2e.openDetails(),"
            " __e2e.mainText().indexOf('PASS e2e verifier') !== -1)"
            " ? JSON.stringify(true) : ''",
            what="verifier artifact preview text in the DOM")
        self.page.js("__e2e.openDetails()")
        body = self.page.rpc("bodyText")
        self.assertIn("PASS e2e verifier", body)
        self.assertIn("onerror=alert(777)", body,
                      "artifact text must render verbatim as text")
        # The hostile markup must appear as escaped TEXT, never as live
        # markup: no <img element, and the raw innerHTML carries only the
        # escaped form (indexOf('<img src=x') stays -1).
        self.assertEqual(
            -1, self.page.js("document.querySelector('#main').innerHTML"
                             ".indexOf('<img src=x')"),
            "raw markup must not contain a live <img> tag")
        # no <img> element may exist anywhere in main
        self.assertEqual(0, self.page.js(
            "document.querySelectorAll('#main img').length"))
        self.assertIn("preview_truncated", body,
                     "long verifier output must arrive flagged, not silently"
                     " clipped")
        self.assert_no_page_errors()

        # -- durable truth behind what the browser showed ----------------------
        task = self.api_get(f"/api/v1/tasks/{task_id}")
        self.assertEqual("done", task["status"])
        # (the report_only policy invariant is checked in the next test)
        verif = self.api_get(
            f"/api/v1/verifications?project_id={self.project_id}")["items"]
        self.assertEqual(1, len(verif))
        self.assertEqual("pass", verif[0]["status"])
        self.assertEqual(1, self.table_count("approvals"))
        self.assertEqual("accept", self.scalar(
            "SELECT decision FROM approvals LIMIT 1"))
        # every browser write went through the Command service:
        kinds = {str(row[0]) for row in self.conn.execute(
            "SELECT DISTINCT kind FROM commands").fetchall()}
        self.assertIn(KIND_TASK_CREATE, kinds)
        self.assertIn(KIND_APPROVAL_DECISION, kinds)
        self.assertEqual(0, self.table_count("wakes"),
                       "acceptance must not synthesize wake rows")
        self.assert_no_herdr_subprocess()

    def test_policy_form_sets_report_only_and_ship_texts_m4(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Tasks")
        self.create_task_via_ui(title="e2e policy probe", kind="scout")
        self.wait_log("create task")
        task_id = str(self.scalar(
            "SELECT id FROM tasks WHERE project_id=? AND title=?"
            " ORDER BY created_at LIMIT 1", (self.project_id,
                                            "e2e policy probe")))
        self.page.click_row_button(task_id)
        self.page.wait("__e2e.mainText().indexOf('Execution policy') !== -1",
                      what="policy section")
        # policy copy keeps the M4 boundary visible in the UI
        self.assertIn("policy execution is M4", self.page.rpc("mainText"))
        self._fill("setPolicyDelivery", "report_only")
        self.page.click_button("Update policy")
        self.wait_log("update policy")
        self.page.wait(
            "__e2e.mainText().indexOf('report_only') !== -1",
            what="stored policy visible after re-render")
        task = self.api_get(f"/api/v1/tasks/{task_id}")
        self.assertEqual("report_only", task["execution_policy"]["delivery"])
        # the applied write is a confirmed Command (single audit row)
        self.assertEqual(1, self.table_count(
            "commands", "kind=?", (KIND_TASK_POLICY_UPDATE,)))
        # and delivery stayed inside the closed set: an option outside the
        # set physically does not exist in the select.
        options = self.page.js(
            "(function(){const s=document.querySelector("
            "'#main section.card select');return JSON.stringify("
            "[...s.options].map(o=>o.value));})()")
        self.assertEqual(["", "report_only", "local_only", "direct_pr", "ship"],
                         json.loads(options))

    def test_duplicate_clicks_and_identical_resubmit_never_double_write(
            self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Tasks")
        self._fill("fillCreateTask", {"title": "e2e duplicate probe", "description": "same bytes", "kind": "ship", "priority": 50})
        second_press_possible = self.page.double_click_button("Create task")
        # The console disables the submit button while the write is in
        # flight, so normally the duplicate press cannot even fire; if it
        # can, the deterministic Idempotency-Key must collapse it.
        self.wait_log("create task")
        time.sleep(0.5)
        self.assertEqual(
            1, self.table_count("tasks", "project_id=? AND title=?",
                               (self.project_id, "e2e duplicate probe")),
            f"duplicate clicks double-wrote (second press possible:"
            f" {second_press_possible})")

        # Identical re-submission of the same form content replays: the
        # server returns the stored Command instead of a second write.
        self._fill("fillCreateTask", {"title": "e2e duplicate probe", "description": "same bytes", "kind": "ship", "priority": 50})
        self.page.click_button("Create task")
        self.wait_log("REPLAY")
        self.assertEqual(1, self.table_count(
            "tasks", "project_id=? AND title=?",
            (self.project_id, "e2e duplicate probe")))
        self.assertIn("REPLAY of existing command (no new write)",
                     self.page.rpc("logText"))
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 4. Task management: DAG cycle, stale version, pause gates in the browser
# ---------------------------------------------------------------------------


@requires_e2e
class M306TaskManagementTests(_E2EBase):
    project_name = "mgmt-e2e"
    team_id = "team-mgmt-e2e"

    def _create(self, title: str, kind: str = "ship") -> str:
        self.goto_view("Tasks")
        self.page.wait("__e2e.mainText().indexOf('Create task') !== -1",
                      what="create form")
        self.create_task_via_ui(title=title, kind=kind)
        # Read the id from durable SQLite, not from the shared write log:
        # an earlier "create task" line would satisfy a log-text wait and
        # make this return None for the CURRENT create.
        deadline = time.monotonic() + 15
        tid = None
        while time.monotonic() < deadline:
            tid = self.scalar(
                "SELECT id FROM tasks WHERE project_id=? AND title=?"
                " ORDER BY created_at LIMIT 1", (self.project_id, title))
            if tid:
                break
            time.sleep(0.1)
        self.assertIsNotNone(tid, f"task {title!r} never became durable")
        return str(tid)

    def _open_detail(self, task_id: str) -> None:
        self.goto_view("Tasks")
        self.page.click_row_button(task_id)
        # The detail spinner ALSO contains the task id, so gate on
        # detail-only copy instead.
        self.page.wait("__e2e.mainText().indexOf('Back to list') !== -1",
                      what=f"detail view for {task_id}")

    def test_dependency_cycle_surfaces_the_named_path_in_browser(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        one = self._create("cycle-one")
        two = self._create("cycle-two")

        self._open_detail(one)
        self.page.wait("__e2e.mainText().indexOf('Add dependency') !== -1",
                      what="dependency form")
        self._fill("setDepInput", two)
        self.page.click_button("Add dependency")
        self.wait_log("add dependency")
        self.page.wait(
            f"__e2e.mainText().indexOf({json.dumps(two)}) !== -1",
            what="depends_on row visible")

        # Closing the loop must name the concrete cycle.  The API answers
        # 409 DEPENDENCY_CYCLE with the closed path in details, and the
        # console renders command failures as a state box INSIDE #main
        # (failInto), not in the top #banner.
        self._open_detail(two)
        self._fill("setDepInput", one)
        self.page.click_button("Add dependency")
        self.page.wait(
            "__e2e.mainText().indexOf('Dependency cycle refused') !== -1",
            timeout=20, what="cycle refusal state box")
        self.page.js("__e2e.openDetails()")
        box = self.page.rpc("mainText")
        self.assertIn(one, box)
        self.assertIn(two, box)
        self.assertIn(" -> ", box, "the concrete closed path must be named")
        # nothing was persisted for the refused write
        self.assertEqual(1, self.table_count("task_dependencies"))
        self.assert_no_page_errors()

    def test_stale_version_edit_is_a_named_conflict_and_writes_nothing(
            self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        task_id = self._create("stale-victim")
        self._open_detail(task_id)
        self.assertIn("expected_version = v", self.page.rpc("mainText"))

        # Another surface (CLI/other tab) bumps the version underneath us.
        status, payload = self.api_write(
            "PATCH", f"/api/v1/tasks/{task_id}",
            {"expected_version": 0, "priority": 77}, "k-cli-bump")
        self.assertEqual(200, status, str(payload)[:300])

        # The stale view now saves with a stale expectation: a named 409
        # conflict that shows expected/found and writes nothing.
        self._fill("fillEdit", {"priority": 95})
        self.page.click_button("Save edit (PATCH)")
        self.page.wait(
            "__e2e.mainText().indexOf('Version conflict (409)') !== -1",
            timeout=20, what="stale-version conflict box")
        box = self.page.rpc("mainText")
        self.assertIn("v0", box)
        self.assertIn("v1", box)
        current = self.api_get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(77, current["priority"],
                         "stale edit must not overwrite the newer value")
        self.assert_no_page_errors()

    def test_scheduler_and_task_pause_resume_badges(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()

        # Scheduler gate via the Overview controls.
        self.page.wait("__e2e.mainText().indexOf('Scheduler gate') !== -1",
                      what="scheduler section")
        self._fill("setSchedulerReason", "hold dispatch")
        self.page.click_button("Pause scheduler")
        self.wait_log("scheduler/pause")
        self.page.wait("__e2e.mainText().indexOf('scheduler PAUSED') !== -1",
                      what="PAUSED badge on overview")
        state = self.api_get(
            f"/api/v1/scheduler/state?project_id={self.project_id}"
        )["scheduler"]
        self.assertTrue(state["paused"])
        self.assertEqual(1, self.table_count(
            "commands", "kind=?", (KIND_SCHEDULER_PAUSE,)))
        self.assertEqual(0, self.table_count("leader_lease"),
                       "pause must never touch the lease table")

        self._fill("setSchedulerReason", "dispatch again")
        self.page.click_button("Resume scheduler")
        self.wait_log("scheduler/resume")
        self.page.wait("__e2e.mainText().indexOf('scheduler running') !== -1",
                      what="running badge returns")
        self.assertEqual(1, self.table_count(
            "commands", "kind=?", (KIND_SCHEDULER_RESUME,)))

        # Task-level pause/resume via the detail view.
        task_id = self._create("pause-me")
        self._open_detail(task_id)
        self._fill("setTaskCommandReason", "operator hold from e2e")
        self.page.click_button("pause")
        self.wait_log("task/pause")
        self.page.wait("__e2e.mainText().indexOf('paused') !== -1",
                      what="paused badge on detail")
        task = self.api_get(f"/api/v1/tasks/{task_id}")
        self.assertTrue(task["paused"])
        self._fill("setTaskCommandReason", "operator release from e2e")
        self.page.click_button("resume")
        self.wait_log("task/resume")
        self.assertFalse(self.api_get(f"/api/v1/tasks/{task_id}")["paused"])
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 5. Ordinary ship waits for M4 — never falsely reported complete
# ---------------------------------------------------------------------------


@requires_e2e
class M306ShipWaitsForM4Tests(_E2EBase):
    project_name = "ship-e2e"
    team_id = "team-ship-e2e"

    def test_ship_policy_applies_but_completion_waits_for_m4(self) -> None:
        assert self.fake is not None and self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Tasks")
        self.create_task_via_ui(title="e2e ship task", kind="ship")
        self.wait_log("create task")
        task_id = str(self.scalar(
            "SELECT id FROM tasks WHERE project_id=? AND title=?"
            " ORDER BY created_at LIMIT 1", (self.project_id,
                                            "e2e ship task")))
        self.page.click_row_button(task_id)
        self.page.wait("__e2e.mainText().indexOf('Execution policy') !== -1",
                      what="policy section")
        self._fill("setPolicyDelivery", "ship")
        self.page.click_button("Update policy")
        self.wait_log("update policy")
        task = self.api_get(f"/api/v1/tasks/{task_id}")
        self.assertEqual("ship", task["execution_policy"]["delivery"])
        self.assertIn("policy execution is M4", self.page.rpc("mainText"))

        # Mock runtime drives it as far as Core allows: dispatch, brief,
        # final WorkerReport — a scout-shaped evidence path.
        attempt = self.fake.dispatch(task_id)
        brief = self.fake.brief(attempt.id, "ship attempt — no worktree evidence")
        self.fake.worker_done(task_id, attempt.id, brief,
                             summary="claims done; no M4 evidence exists")
        self.assertEqual("verifying",
                        self.api_get(f"/api/v1/tasks/{task_id}")["status"])

        # A verification run for a ship task is refused by Core with the M4
        # marker and persists NOTHING (no verifications row, no ship).
        self.fake.configure_verifier("print('PASS')", key="verifier-ship")
        with self.assertRaises(Exception) as caught:
            self.fake.run_verification(attempt.id, key="verify-ship")
        self.assertEqual("M4_EVIDENCE_REQUIRED",
                         getattr(caught.exception, "reason_code", None))
        self.assertEqual(0, self.table_count("verifications"))
        self.assertEqual(0, self.table_count("approvals"))

        # And the browser never reports completion: no done badge anywhere
        # for this task; verifications stay honestly empty.
        self._refresh_tasks_and_check(task_id)
        self.assert_no_page_errors()
        self.assert_no_herdr_subprocess()

    def _refresh_tasks_and_check(self, task_id: str) -> None:
        self.goto_view("Tasks")
        # Inspect THIS TASK'S ROW only: the whole-view text contains the
        # status filter dropdown, which always lists "done" as an option.
        row = self.page.wait(
            "[...document.querySelectorAll('#main table tr')].map("
            f"tr => tr.innerText).find(t => t.indexOf({json.dumps(task_id)})"
            " !== -1) || ''",
            timeout=25, what="task row after refresh")
        self.assertNotIn("done", str(row),
                        "ship must never be reported done before M4")
        self.assertIn("verifying", str(row),
                     "the honest pre-M4 state must be visible")
        self.goto_view("Verifications")
        self.page.wait("__e2e.mainText().indexOf('Verifications') !== -1",
                      what="verifications view")
        self.assertIn("No verification runs recorded",
                     self.page.rpc("mainText"))


# ---------------------------------------------------------------------------
# 6. SSE: live tail, REST backfill, client dedup, snapshot-required resync
# ---------------------------------------------------------------------------


@requires_e2e
class M306EventStreamTests(_E2EBase):
    project_name = "sse-e2e"
    team_id = "team-sse-e2e"

    def _make_events(self, count: int, prefix: str) -> None:
        for i in range(count):
            status, payload = self.api_write(
                "POST", "/api/v1/tasks",
                {"project_id": self.project_id,
                 "title": f"{prefix}-{i}", "kind": "ship", "priority": 50},
                f"k-{prefix}-{i}")
            self.assertEqual(201, status, str(payload)[:200])

    def test_live_tail_then_backfill_then_client_dedup(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Events")

        # live tail: events written server-side stream into the DOM.
        self._make_events(3, "live")
        self.page.wait(
            "JSON.parse(__e2e.seqs()).length >= 3",
            timeout=25, what="three live SSE cards")
        seqs = json.loads(self.page.rpc("seqs"))
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(self.page.rpc("lastSeq"), max(seqs))

        # disconnect: the store keeps growing, the page just stops tailing.
        self.page.click_button("Disconnect")
        self.assertIn("Disconnected from the stream", self.page.rpc("banner"))
        self._make_events(2, "missed")
        rest_total = len(self.rest_event_seqs())
        missed_count = rest_total - len(seqs)
        self.assertGreaterEqual(missed_count, 2,
                               "each task writes multiple durable events; the"
                               " disconnected window must have missed them")

        # REST backfill button folds the missed events in without SSE.
        self.page.click_button("REST backfill (events after last seq)")
        self.page.wait(
            "__e2e.banner().indexOf("
            f"'Backfilled {missed_count} events') !== -1",
            what="backfill banner")
        seqs_after = json.loads(self.page.rpc("seqs"))
        self.assertEqual(len(seqs_after), rest_total)

        # client dedup: open two EXTRA concurrent streams from the same
        # cursor while live events arrive — every stream delivers each new
        # event, the client must render each seq exactly once.  (Opening a
        # stream re-bases the visible list at the cursor by design, so the
        # expectation is "every durable event after the cursor, exactly
        # once" — a duplicate across two live streams would violate it.)
        cursor = seqs_after[-1]
        open_before = json.loads(self.page.rpc("streamCount"))
        self.page.js(
            "state.streamActive = true;"
            " openStream(state.projectId, document.getElementById("
            "'event-list'), null);"
            " openStream(state.projectId, document.getElementById("
            "'event-list'), null);")
        self._make_events(3, "overlap")
        final_rest = self.rest_event_seqs()
        expected_dom = [s for s in final_rest if s > cursor]
        self.page.wait(
            f"JSON.parse(__e2e.seqs()).length === {len(expected_dom)}"
            " ? JSON.stringify(true) : ''",
            timeout=25, what="all overlapping deliveries rendered once")
        final = json.loads(self.page.rpc("seqs"))
        streams = json.loads(self.page.rpc("streamCount"))
        self.assertGreaterEqual(streams, open_before + 2,
                               "the overlapping streams must have been made")
        self.assertEqual(len(final), len(set(final)),
                       "duplicate seq rendered — client dedup failed")
        self.assertEqual(final, expected_dom,
                       "each event after the cursor rendered exactly once "
                       "across three concurrent deliveries")
        self.assert_no_page_errors()

    def test_snapshot_required_resync_when_cursor_is_ahead(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Events")
        self._make_events(1, "seq-anchor")
        self.page.wait("JSON.parse(__e2e.seqs()).length >= 1",
                      timeout=25, what="anchor event card")
        durable = self.api_get(
            f"/api/v1/projects/{self.project_id}/snapshot")["event_seq"]

        # A cursor ahead of the durable store (restart/seq-reset signal)
        # must demand a snapshot, resync, and reconnect — not error out.
        self.page.click_button("Disconnect")
        self.page.js(f"state.lastSeq = {durable + 5000}")
        self.page.click_button("Connect")
        self.page.wait(
            "__e2e.banner().indexOf('Cursor ahead of the durable store') !== -1"
            " || __e2e.banner().indexOf('snapshot_required') !== -1",
            timeout=25, what="snapshot-required banner")
        self.page.wait(
            f"__e2e.lastSeq() <= {durable} && __e2e.lastSeq() >= 0"
            " ? JSON.stringify(true) : ''",
            timeout=25, what="cursor resynced to the durable seq")
        # the stream is live again: after resync the list re-bases at the
        # durable cursor and new durable events (a task write is several
        # events) arrive on top of it, exactly once.
        durable_seqs = self.rest_event_seqs()
        expected_new = len(durable_seqs) - durable
        self._make_events(1, "after-resync")
        final_rest = self.rest_event_seqs()
        expected_dom = [s for s in final_rest if s > durable]
        self.assertGreater(len(expected_dom), expected_new)
        self.page.wait(
            f"JSON.parse(__e2e.seqs()).length === {len(expected_dom)}"
            " ? JSON.stringify(true) : ''",
            timeout=25, what="post-resync live events delivered")
        self.assertEqual(json.loads(self.page.rpc("seqs")), expected_dom,
                         "resynced DOM must mirror the durable sequence "
                         "after the cursor — once each")
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 7. Backend restart: snapshots/SSE converge by SQLite seq; no dup effects
# ---------------------------------------------------------------------------


@requires_e2e
class M306BackendRestartTests(_E2EBase):
    project_name = "restart-e2e"
    team_id = "team-restart-e2e"

    def test_restart_converges_without_duplicate_commands_or_wakes(
            self) -> None:
        assert self.fake is not None and self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Events")

        # one UI write + its stream card BEFORE the crash
        status, payload = self.api_write(
            "POST", "/api/v1/tasks",
            {"project_id": self.project_id, "title": "pre-crash",
             "kind": "ship", "priority": 50}, "k-pre-crash")
        self.assertEqual(201, status, str(payload)[:200])
        self.page.wait("JSON.parse(__e2e.seqs()).length >= 1",
                      timeout=25, what="pre-crash event card")

        commands_before = self.table_count("commands")
        wakes_before = self.table_count("wakes")
        deps_before = self.table_count("task_dependencies")

        # kill the backend WHILE the SSE stream is open.
        self.backend.stop()
        self.backend.wait_down()
        self.page.wait(
            "__e2e.banner().indexOf('Stream') !== -1"
            " || __e2e.chip() === 'server: unreachable'",
            timeout=25, what="offline/interrupted state in the browser")

        # the durable plane keeps working while the API process is gone
        # (this is exactly what the fenced runtime does without the web).
        self.fake.progress_events(2, "e2e-while-down")
        events.append_event(self.conn, "watch.session_observed",
                           project_id=self.project_id,
                           payload_json=json.dumps(
                               {"note": "observed while API down"}))
        events_written_while_down = 3

        self.backend.start()  # fresh process, same DB (new ephemeral port)
        # The tab's auto-reconnect resumes from the durable cursor; point it
        # at the restarted process (the console probes the new port via
        # Refresh after we hand it the new URL).
        self.page.open(self.backend.console_url)
        self.login()
        self.select_project()
        self.goto_view("Events")

        # convergence: DOM seq == REST seq == durable SQLite order,
        # strictly increasing, zero duplicates, all offline writes present.
        expected = self.rest_event_seqs()
        self.page.wait(
            f"JSON.parse(__e2e.seqs()).length === {len(expected)}"
            " ? JSON.stringify(true) : ''",
            timeout=30, what="full event convergence after restart")
        seqs = json.loads(self.page.rpc("seqs"))
        self.assertEqual(seqs, expected)
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertGreaterEqual(
            len(seqs), events_written_while_down + 1)

        # no duplicated side effects across the restart
        self.assertEqual(commands_before, self.table_count("commands"))
        self.assertEqual(wakes_before, self.table_count("wakes"))
        self.assertEqual(deps_before, self.table_count("task_dependencies"))
        self.assertEqual(1, self.table_count(
            "tasks", "project_id=? AND title=?",
            (self.project_id, "pre-crash")))
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 8. Queue/TTL: an expired manual CEO request never executes
# ---------------------------------------------------------------------------


@requires_e2e
class M306QueueTtlTests(_E2EBase):
    project_name = "ttl-e2e"
    team_id = "team-ttl-e2e"

    def test_expired_queue_never_executes_and_the_expiry_is_visible(
            self) -> None:
        assert self.fake is not None and self.project_id
        self.open_console()
        self.login()
        self.select_project()

        # UI manual request: queued only (202), never a wake.
        self.goto_view("CEO")
        self.page.wait("__e2e.mainText().indexOf('Ask the CEO') !== -1",
                      what="CEO queue form")
        self._fill("setCeoReason", "browser asks the CEO")
        self.page.click_button("Request CEO processing")
        self.wait_log("ceo wake request")
        self.page.wait("__e2e.banner().indexOf('Queued (202)') !== -1",
                      what="queued banner")
        self.assertEqual(0, self.table_count("wakes"))
        self.assertEqual(0, self.table_count("prompt_deliveries"))
        queued_id = str(self.scalar(
            "SELECT id FROM commands WHERE kind=? ORDER BY requested_at DESC"
            " LIMIT 1", (KIND_CEO_WAKE_REQUEST,)))
        self.assertEqual("pending",
                        self.api_get(f"/api/v1/commands/{queued_id}"
                                   )["command"]["state"])

        # A queued intent with a 1s TTL: the runtime sweep moves it to
        # EXPIRED; nothing ran and nothing can run afterwards.
        short_lived = self.fake.queue_manual_request("k-ttl-1s",
                                                    ttl_seconds=1)
        time.sleep(1.5)
        swept = self.fake.sweep()
        self.assertIn((short_lived.id, "TTL_EXPIRED"), swept)
        expired = self.api_get(f"/api/v1/commands/{short_lived.id}"
                              )["command"]
        self.assertEqual("expired", expired["state"])
        self.assertEqual(0, self.table_count("wakes"),
                       "an expired queue entry must never spawn a wake")
        self.assertEqual(0, self.table_count("prompt_deliveries"))
        # the still-fresh UI request survived its own sweep: TTL decides
        # per-entry, not per-plane.
        self.assertEqual("pending",
                        self.api_get(f"/api/v1/commands/{queued_id}"
                                   )["command"]["state"])

        # The expiry is an auditable fact in the browser's Events view too.
        self.goto_view("Events")
        self.page.wait(
            "__e2e.mainText().indexOf('command.expired') !== -1",
            timeout=25, what="command.expired event card visible in the tab")
        self.assert_no_page_errors()
        self.assert_no_herdr_subprocess()


# ---------------------------------------------------------------------------
# 9. Fake-Herdr CEO + wake lifecycle, rendered live in the browser
# ---------------------------------------------------------------------------


@requires_e2e
class M306FakeHerdrCeoWakeTests(_E2EBase):
    project_name = "wake-e2e"
    team_id = "team-wake-e2e"
    init_ceo = True  # setUpClass-equivalent flow runs inside setUp

    def _prepare_root(self, root: str) -> None:
        # CEO protocol/skill discovery reads these project-local paths —
        # copied verbatim from the repository (never downloaded).
        source = REPO_ROOT / ".agents" / "skills"
        target = Path(root) / ".agents" / "skills"
        shutil.copytree(source / "teamcraft-ceo",
                       target / "teamcraft-ceo")
        (target / "teamcraft").mkdir(parents=True)
        shutil.copy2(source / "teamcraft" / "SKILL.md",
                    target / "teamcraft" / "SKILL.md")

    def test_protocol_init_and_wake_lifecycle_render_live(self) -> None:
        assert self.fake is not None and self.project_id
        # setUp already ran: sync_ceo(idle) + init_ceo() under
        # assumed_default. The browser must show exactly that honesty.
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("CEO")
        self.page.wait("__e2e.mainText().indexOf('Initialization') !== -1",
                      timeout=25, what="CEO initialization section")
        body = self.page.rpc("mainText")
        self.assertIn("initialized", body)
        self.assertIn("assumed_default", body,
                     "the temporary assumed receipt must be visible, never"
                     " presented as host-trusted")
        self.assertIn("1.0.0", body, "protocol version from the real files")
        # protocol facts (version + sha256 + both harnesses available)
        self.page.wait("__e2e.mainText().indexOf('Protocol discovery') !== -1",
                      what="protocol section")
        body = self.page.rpc("mainText")
        self.assertIn("available", body)
        self.assertIn("codex", body)
        self.assertIn("opencode", body)
        sha = str(self.scalar(
            "SELECT protocol_hash FROM ceo_initializations WHERE project_id=?",
            (self.project_id,)))
        self.assertTrue(sha and len(sha) == 64)
        self.assertIn(sha[:12], body, "protocol hash chip rendered")

        # --- multi-event merged wake (idle merge window) --------------------
        self.fake.progress_events(4, "e2e-merged")
        result = self.fake.tick_ingest()
        self.assertEqual(1, result.created_wakes)
        self.assertEqual(3, result.merged_events)
        # result.wakes holds the re-read snapshot after EACH folded event;
        # the final snapshot carries the fully merged row.
        self.assertEqual(4, result.wakes[-1].event_count)
        merged = result.wakes[-1]
        merged_row = (f"seq {merged.first_seq}\u2013{merged.last_seq}"
                      f" (4 events)")
        self.goto_view("Wakes")
        self.page.wait(
            f"__e2e.mainText().indexOf({json.dumps(merged_row)}) !== -1",
            timeout=25, what="merged multi-event wake row")
        self.assertIn("pending", self.page.rpc("mainText"))

        # --- happy-path transport lifecycle -----------------------------------
        admitted = self.fake.admit(merged.id)
        self.assertIsNotNone(admitted.command)
        self.assertEqual("sending", self.fake.wake_state(merged.id))
        self.goto_view("Wakes")
        self.page.wait("__e2e.mainText().indexOf('sending') !== -1",
                      timeout=25, what="sending state badge")
        submitted = self.fake.submitted(merged.id)
        self.goto_view("Wakes")
        self.page.wait("__e2e.mainText().indexOf('submitted') !== -1",
                      timeout=25, what="submitted state badge")
        acknowledged = self.fake.acknowledge(submitted)
        processed = self.fake.processed(acknowledged.id)
        self.assertEqual("processed", processed.state)
        self.goto_view("Wakes")
        self.page.wait("__e2e.mainText().indexOf('processed') !== -1",
                      timeout=25, what="processed state badge")
        # cursor badge advanced on the same view
        cursor = self.api_get(f"/api/v1/wake-cursors/{self.project_id}")
        self.assertGreaterEqual(cursor["scanned_seq"], merged.last_seq)
        self.assertGreaterEqual(cursor["acknowledged_seq"], merged.last_seq)
        self.assertGreaterEqual(cursor["processed_seq"], merged.last_seq)
        self.assertIn("cursor scanned", self.page.rpc("mainText"))

        # --- failure path: an ambiguous delivery is a flagged failure ------
        self.fake.progress_events(1, "e2e-failure")
        self.fake.tick_ingest()
        failing_id = [wid for wid in self.fake.pending_wake_ids()
                      if wid != merged.id][0]
        self.fake.admit(failing_id)
        self.fake.unknown(failing_id,
                         "fake herdr transport timed out (mock scenario)")
        self.goto_view("Wakes")
        self.page.wait("__e2e.mainText().indexOf('unknown') !== -1",
                      timeout=25, what="unknown wake failure row")
        body = self.page.rpc("mainText")
        self.assertIn("Failure state unknown", body,
                     "the console must flag ambiguous delivery explicitly")
        self.assertIn("never auto-retried", body)
        self.assertEqual(0, self.table_count("prompt_deliveries"),
                         "no prompt was EVER delivered — transport was faked"
                         " at the Core-fact layer only")

        # --- CEO working gate (mock live observation blocks admission) ------
        self.fake.set_live("working")
        self.fake.progress_events(1, "e2e-while-working")
        self.fake.tick_ingest()
        working_id = [wid for wid in self.fake.pending_wake_ids()
                      if wid not in (merged.id, failing_id)][0]
        blocked = self.fake.admit(working_id)
        self.assertEqual("ceo_working", blocked.deferred_reason)
        self.goto_view("Agents")
        self.page.wait("__e2e.mainText().indexOf('ceo') !== -1",
                      timeout=25, what="members table shows the ceo row")
        self.assertIn("working", self.page.rpc("mainText"))
        # still pending — the gate withheld it, no command was created
        self.assertEqual("pending", self.scalar(
            "SELECT state FROM wakes WHERE id=?", (working_id,)))

        # Facts, not authority: transcript proves no real host contact.
        self.assert_no_herdr_subprocess()
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 10. Browser-disconnect durability: the server's truth outlives the client
# ---------------------------------------------------------------------------


@requires_e2e
class M306BrowserDisconnectTests(_E2EBase):
    """A queued write survives the browser vanishing mid-response.

    The tab is closed while the create POST is still in flight, then the
    ENTIRE Chromium process is killed and relaunched (profile wiped — no
    localStorage, no sessionStorage, no open sockets).  The backend process
    is never touched.  Durability means: the accepted write is already
    durable server-side, it appears exactly once, and a fresh browser sees
    the same single fact after re-authenticating.
    """

    project_name = "disconnect-e2e"
    team_id = "team-disconnect-e2e"

    def _wait_durable_task(self, title: str,
                          timeout: float = 15.0) -> str:
        """Poll SQLite directly: did the server persist the write?"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.conn.execute(
                "SELECT id FROM tasks WHERE project_id=? AND title=?",
                (self.project_id, title)).fetchone()
            if row is not None:
                return str(row[0])
            time.sleep(0.05)
        raise AssertionError(
            f"task {title!r} never reached durable SQLite — the write was "
            "lost client-side, which is exactly what this test forbids")

    def test_write_survives_tab_close_and_full_browser_restart(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Tasks")
        self.page.wait("__e2e.mainText().indexOf('Create task') !== -1",
                      what="create form")

        # Real click; no waiting for the response — the browser is about to
        # stop existing mid-round-trip.
        self.create_task_via_ui(title="survives-the-browser")
        task_id = self._wait_durable_task("survives-the-browser")

        # The tab vanishes while the round trip may still be in flight.
        self.page.close()
        # ...and then the whole browser process dies with its profile.
        _CHROME_CTRL.shutdown()
        _CHROME_CTRL.ensure()
        self.page = _CHROME_CTRL.new_page()
        self.addCleanup(self.page.close)

        # The backend never restarted — same process, same DB file, and a
        # totally fresh browser (no client storage) re-authenticates to it.
        self.assertTrue(self.backend.running(),
                       "only the browser died; the server must not have")
        self.open_console()
        self.login()
        self.select_project()

        # The fresh browser sees the task exactly once — rendered from the
        # durable store, not from any surviving client state.
        self.goto_view("Tasks")
        once = self.page.wait(
            "[...document.querySelectorAll('#main table tr')].filter(tr => "
            f"tr.innerText.indexOf({json.dumps('survives-the-browser')}) !== "
            "-1).length === 1 ? JSON.stringify(true) : ''",
            timeout=25, what="exactly one row for the write that outran the "
                            "browser")
        self.assertEqual("true", str(once))

        # Durable facts: one command, confirmed, one task, no duplicates.
        rows = self.conn.execute(
            "SELECT state, idempotency_key FROM commands WHERE kind=?",
            (KIND_TASK_CREATE,)).fetchall()
        self.assertEqual(1, len(rows),
                       "exactly one create command row for one user click")
        self.assertEqual("confirmed", str(rows[0][0]))
        self.assertEqual(1, self.table_count(
            "tasks", "title=?", ("survives-the-browser",)))
        task = self.api_get(f"/api/v1/tasks/{task_id}")
        self.assertEqual("survives-the-browser", task["title"])
        self.assertEqual(task_id, self.scalar(
            "SELECT json_extract(result_json, '$.task_id') FROM commands "
            "WHERE kind=?", (KIND_TASK_CREATE,)),
            "the confirmed command's stored result names the same task the "
            "browser now renders")
        self.assert_no_page_errors()


# ---------------------------------------------------------------------------
# 11. Long logs and hostile HTML through the ordinary UI paths
# ---------------------------------------------------------------------------


@requires_e2e
class M306LongAndHostileHtmlTests(_E2EBase):
    """Hostile markup stays inert text; oversized text is a named refusal.

    The console renders every server value via ``textContent`` (no
    ``innerHTML``), so ``<img onerror>``/``<script>`` must appear verbatim
    as text with zero dialogs and zero injected elements.  The write path
    caps text fields at their API limits — a 5000-byte description is a
    visible 422 refusal that writes NOTHING (not a silent clip), while the
    read path clips at 4096 bytes and raises a
    ``description truncated by API`` badge so nothing is silently hidden.
    """

    project_name = "hostile-e2e"
    team_id = "team-hostile-e2e"

    HOSTILE_TITLE = '<img src=x onerror="alert(911)">'
    HOSTILE_DESC = ('</script><svg onload="alert(912)"> "quotes" '
                   "& <b>markup</b> stays text")

    def _after_project(self) -> None:
        # Seed a core-side task whose description EXCEEDS the read cap
        # directly through the domain (older/core writers are not bound by
        # the API's write cap) so the UI must show the truncation badge.
        assert self.project_id
        domain.create_task(self.conn, self.project_id, "long-desc-task",
                          description="D" * 6000, kind="scout")

    def _open_task_detail(self, title_or_id: str) -> None:
        self.goto_view("Tasks")
        self.page.click_row_button(title_or_id)
        self.page.wait(
            "__e2e.mainText().indexOf('Back to list') !== -1",
            what="task detail view")

    def test_hostile_markup_is_inert_text_in_the_browser(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Tasks")
        self.page.wait("__e2e.mainText().indexOf('Create task') !== -1",
                      what="create form")
        self.create_task_via_ui(title=self.HOSTILE_TITLE,
                                description=self.HOSTILE_DESC, kind="ship")
        self.wait_log("create task")
        self.page.wait("__e2e.logText().indexOf('state=confirmed') !== -1",
                      what="create confirmed in the write log")

        detail = self.page.rpc("mainText")  # list re-rendered after create
        self.assertIn(self.HOSTILE_TITLE, detail,
                    "hostile title must render verbatim as TEXT")

        self._open_task_detail("onerror")
        # innerText excludes <textarea> content by spec, so compare the
        # description via its field value — byte-for-byte, no re-parse.
        desc_val = self.page.js(
            "(document.querySelector('#main textarea') || {}).value")
        self.assertEqual(self.HOSTILE_DESC, str(desc_val),
                        "hostile description must round-trip as literal data")
        # The title renders as the edit field's VALUE (attribute path, not
        # markup) — read it back and compare byte-for-byte.
        title_val = self.page.js(
            "(document.querySelector('#main input[type=text]') || {})"
            ".value")
        self.assertEqual(self.HOSTILE_TITLE, str(title_val),
                        "hostile title round-trips as literal data, not DOM")
        # nothing was parsed as markup anywhere in main:
        self.assertEqual(0, self.page.js(
            "document.querySelectorAll('#main img').length"))
        self.assertEqual(0, self.page.js(
            "document.querySelectorAll('#main svg').length"))
        self.assertEqual(0, self.page.js(
            "document.querySelectorAll('#main script').length"))
        # no dialog ever opened (alert(911)/alert(912) never executed):
        self.assertEqual([], self.page.dialogs)
        self.assert_no_page_errors()

    def test_overlong_description_is_a_named_refusal_and_writes_nothing(
            self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self.goto_view("Tasks")
        self.page.wait("__e2e.mainText().indexOf('Create task') !== -1",
                      what="create form")
        self.create_task_via_ui(title="overlong-desc",
                                description="L" * 5000, kind="ship")
        log = self.wait_log("create task")
        self.page.wait("__e2e.logText().indexOf('422') !== -1",
                      timeout=20, what="named 422 refusal in the log")
        log = self.page.rpc("logText")
        self.assertIn("422", log)
        self.assertIn("invalid", log.lower())
        # nothing was written: no task, no command payload for this attempt.
        self.assertEqual(0, self.table_count("tasks", "title=?",
                                            ("overlong-desc",)))
        self.assertNotIn(
            "overlong-desc",
            str(self.conn.execute(
                "SELECT payload_json FROM commands").fetchall()))
        # the console stays alive and usable after the refusal: a valid
        # create right after must apply normally.
        self._fill("fillCreateTask", {"title": "after-refusal", "kind": "ship"})
        self.page.click_button("Create task")
        self.wait_log("state=confirmed")
        self.page.wait(
            "[...document.querySelectorAll('#main table tr')].filter(tr => "
            "tr.innerText.indexOf('after-refusal') !== -1).length === 1"
            " ? JSON.stringify(true) : ''",
            timeout=25, what="valid write renders after the refusal")
        self.assertEqual(1, self.table_count("tasks", "title=?",
                                            ("after-refusal",)),
                         "a refused write must not poison later writes")
        self.assert_no_page_errors()

    def test_read_side_clipping_is_flagged_never_silent(self) -> None:
        assert self.project_id
        self.open_console()
        self.login()
        self.select_project()
        self._open_task_detail("long-desc-task")
        body = self.page.rpc("mainText")
        self.assertIn("description truncated by API", body,
                     "a clipped description must announce itself")
        seeded_id = str(self.scalar(
            "SELECT id FROM tasks WHERE project_id=? AND title=?",
            (self.project_id, "long-desc-task")))
        dto = self.api_get(f"/api/v1/tasks/{seeded_id}")
        self.assertTrue(dto["description_truncated"])
        self.assertEqual(4096, len(dto["description"].encode("utf-8")))
        self.assertEqual(("D" * 6000)[:4096], dto["description"],
                        "clipping is a clean byte prefix — no split "
                        "codepoints, no silent middle cut")
        self.assert_no_page_errors()
