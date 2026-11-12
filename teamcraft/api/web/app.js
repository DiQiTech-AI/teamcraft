"use strict";

/* TeamCraft M3-05 minimal web console.
 *
 * Ground rules (pinned by tests/test_web_m305.py):
 *
 * - Real API only: every read goes through the M3-02 Query routes, the live
 *   tail through the M3-03 SSE stream, every write through an M3-04 Command
 *   route. No client-side state is ever presented as fact, and nothing here
 *   is seeded with placeholder rows.
 * - Safe rendering: all server-supplied values are attached via textContent
 *   (or input.value). The HTML-parsing and dynamic-code sinks banned by
 *   tests/test_web_m305.py appear nowhere in this file, so hostile
 *   HTML/Markdown/log text can never execute.
 * - Auth/CSRF: reads carry the bearer token; writes additionally carry the
 *   X-CSRF-Token header and a deterministic Idempotency-Key derived from
 *   method + path + body, so an accidental identical re-submit is a safe
 *   server-side replay instead of a second write.
 * - Honest states: loading / empty / offline / unavailable / auth / conflict
 *   / stale-version / truncation are distinct visible states. Null usage is
 *   rendered as "unknown", never zero.
 */

const API = "/api/v1";

const TASK_STATUSES = ["pending", "ready", "blocked", "running", "verifying",
  "needs_human", "ready_to_merge", "done", "failed", "cancelled"];
const TASK_KINDS = ["scout", "ship"];
/* Delivery is a closed set — mirrors teamcraft.domain.DELIVERY_MODES, the
 * same set the Command API validates against.  The policy form offers only
 * these values, so an invalid free-text configuration cannot even be typed
 * in the UI, let alone reach the server. */
const DELIVERY_MODES = ["report_only", "local_only", "direct_pr", "ship"];
const WAKE_STATES = ["pending", "sending", "submitted", "acknowledged",
  "processed", "unknown", "blocked"];
const VERIFICATION_STATUSES = ["pass", "fail", "error", "stale"];
const DEPENDENCY_EDITABLE = new Set(["pending", "ready", "blocked"]);
const ACTIVE_RUN = new Set(["running", "verifying", "ready_to_merge"]);

const state = {
  token: sessionStorage.getItem("tc.token") || "",
  projectId: sessionStorage.getItem("tc.project") || "",
  view: "overview",
  taskId: "",
  lastSeq: 0,
  streamAbort: null,
  streamTimer: null,
  streamNote: "",
  pageOffset: 0,
  taskFilter: { status: "", kind: "" },
  wakeFilter: "",
};

/* ------------------------------------------------------------------ *
 * DOM helpers — text-only construction (safe by construction).
 * ------------------------------------------------------------------ */

function el(tag, opts, children) {
  const node = document.createElement(tag);
  const o = opts || {};
  if (typeof o === "string") {
    node.textContent = o;
  } else {
    for (const key of Object.keys(o)) {
      const v = o[key];
      if (v === null || v === undefined || v === false) continue;
      if (key === "text") node.textContent = String(v);
      else if (key === "class") node.className = v;
      else if (key.startsWith("on") && typeof v === "function") {
        node.addEventListener(key.slice(2), v);
      } else node.setAttribute(key, String(v));
    }
  }
  /* children may be an array, a single Node, a string, or a scalar.  A
     DOM Node/number is not iterable, so normalise first — without this,
     badge/button/scalar cells threw "children is not iterable" and whole
     views (overview kv rows, task-list rows) rendered as nothing. */
  let kids = children;
  if (kids === null || kids === undefined || kids === false) {
    kids = [];
  } else if (typeof kids === "string" ||
             typeof kids.nodeType === "number" ||
             typeof kids[Symbol.iterator] !== "function") {
    kids = [kids];
  }
  for (const child of kids) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === "string") node.appendChild(document
      .createTextNode(child));
    else if (typeof child.nodeType === "number") node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
  return node;
}

function q(id) { return document.getElementById(id); }

const main = () => q("main");

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function banner(kind, text) {
  const node = clear(q("banner"));
  if (!text) { q("banner").hidden = true; return; }
  node.className = "banner b-" + kind;
  node.textContent = text;
  q("banner").hidden = false;
}

function badge(text, kind) {
  return el("span", { class: "badge b-" + (kind || "muted"), text: text });
}

function statusKind(value) {
  const v = String(value || "");
  if (["done", "processed", "confirmed", "acknowledged", "pass", "finished",
    "idle"].includes(v)) return "ok";
  if (["failed", "cancelled", "expired", "blocked", "unknown", "error",
    "stale", "missing"].includes(v)) return "bad";
  if (["running", "verifying", "sending", "submitted", "active", "starting",
    "working", "pending", "needs_human"].includes(v)) return "warn";
  return "muted";
}

function fmt(value) {
  if (value === null || value === undefined) return "unknown";
  if (value === "") return "(empty)";
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

/* Usage stays null-meaning-unknown (FR-018): never collapse to zero. */
function fmtUsage(u) {
  if (!u || typeof u !== "object") return "unknown";
  if (u.available === false) return "unknown (not recorded — not zero)";
  const parts = ["input_tokens", "output_tokens", "total_tokens",
    "cost_microunits"].map(function (k) {
    return k + "=" + fmt(u[k]);
  });
  return parts.join("  ");
}

/* Every *_truncated flag the API raised, plus raw-payload caps, becomes a
 * visible badge instead of silently clipped text. */
function truncationBadges(obj) {
  const out = [];
  if (!obj || typeof obj !== "object") return out;
  for (const key of Object.keys(obj)) {
    if (key === "payload_truncated" && obj[key] === true) {
      out.push(badge("payload truncated — fetch " +
        (obj.recover || "the event") + " for full bytes", "warn"));
    } else if (key.endsWith("_truncated") && key !== "payload_truncated"
      && obj[key] === true) {
      out.push(badge(key.replace("_truncated", "") + " truncated by API",
        "warn"));
    }
  }
  return out;
}

const PREVIEW_LIMIT = 8000;

function jsonBlock(value, label) {
  const box = el("details", {}, [
    el("summary", {}, [label || "details",
      badge("text only", "muted")]),
  ]);
  let text;
  try {
    text = JSON.stringify(value, null, 2);
  } catch (_err) {
    text = String(value);
  }
  if (text === undefined) text = "null";
  if (text.length > PREVIEW_LIMIT) {
    text = text.slice(0, PREVIEW_LIMIT) +
      "\n… (client preview cut at " + PREVIEW_LIMIT +
      " characters — the durable bytes stay server-side)";
  }
  box.appendChild(el("pre", { class: "json", text: text }));
  return box;
}

function kvTable(rows) {
  const table = el("table", { class: "kv" });
  for (const row of rows) {
    table.appendChild(el("tr", {}, [
      el("th", { text: row[0] }),
      el("td", {}, row[1]),
    ]));
  }
  return table;
}

function heading(text, extra) {
  const row = el("div", { class: "hrow" }, [el("h2", { text: text })]);
  for (const node of extra || []) row.appendChild(node);
  return row;
}

function section(title, children) {
  return el("section", { class: "card" },
    [el("h3", { text: title })].concat(children));
}

function para(text, cls) {
  return el("p", { class: cls || "", text: text });
}

function logLine(kind, text) {
  const log = q("result-log");
  log.insertBefore(el("p", { class: "line b-" + kind,
    text: new Date().toISOString() + "  " + text }), log.firstChild);
  while (log.children.length > 60) log.removeChild(log.lastChild);
}

function emptyNote(what) {
  return para("No " + what + " recorded — this is the real store, not a " +
    "placeholder.", "empty");
}

function spinner(text) {
  return el("p", { class: "loading", text: text || "Loading…" });
}

/* ------------------------------------------------------------------ *
 * API layer — guarded fetch with classified failures.
 * ------------------------------------------------------------------ */

function cyrb53(str, seed) {
  let h1 = 0xdeadbeef ^ seed;
  let h2 = 0x41c6ce57 ^ seed;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507);
  h1 ^= Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507);
  h2 ^= Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h2 >>> 0).toString(16).padStart(8, "0") +
    (h1 >>> 0).toString(16).padStart(8, "0");
}

/* Deterministic key: an accidental identical re-submission replays the
 * stored Command server-side instead of creating a second write. Any
 * intentional change (fields, reason, current version) yields a new key. */
function idemKey(method, path, payload) {
  const src = method + "\n" + path + "\n" + (payload || "");
  return "ui-" + cyrb53(src, 0x9e3779b9) + cyrb53(src, 0x85ebca6b) +
    "-" + src.length;
}

function classifyFailure(status, body) {
  const reason = body && body.details && body.details.reason_code;
  if (status === 401) return "auth";
  if (status === 403) return "csrf";
  if (status === 503) return "unavailable";
  if (status === 429) return "busy";
  if (status === 409 && reason === "VERSION_CONFLICT") return "stale";
  if (status === 409 && reason === "EVIDENCE_STALE") return "stale";
  if (status === 409) return "conflict";
  return "error";
}

function describeFailure(err) {
  if (err.kind === "offline") {
    return "API unreachable — the local server is down or restarting. " +
      "Server-side work continues regardless; this console observes, it " +
      "does not run anything.";
  }
  if (err.kind === "auth") {
    return "Authentication failed (401): paste the current session token " +
      "(teamcraft-api --print-token) and Save. Tokens rotated with " +
      "--rotate-token invalidate the old value immediately.";
  }
  if (err.kind === "csrf") {
    return "Write refused by Origin/CSRF guards (403): the request did not " +
      "come from this page's own origin with the matching token header.";
  }
  if (err.kind === "unavailable") {
    return err.message || "Core store unavailable — the runtime database is " +
      "missing or its schema is newer than this build understands.";
  }
  if (err.kind === "busy") {
    return "Too many concurrent streams (429): close another console tab " +
      "or retry shortly.";
  }
  if (err.kind === "stale") {
    const d = err.details || {};
    return "Version conflict (409): this view bound evidence v" +
      fmt(d.expected) + " but the current version is v" + fmt(d.found) +
      ". Nothing was written — reload the current state before retrying.";
  }
  if (err.kind === "conflict") {
    const d = err.details || {};
    if (d.reason_code === "LEADER_BUSY") {
      return "Leader lease is held by the runtime (" + fmt(d.holder_id) +
        "): the console refuses to write unfenced. Try again shortly.";
    }
    if (d.reason_code === "DEPENDENCY_CYCLE") {
      return "Dependency cycle refused: " + (d.cycle || []).join(" -> ");
    }
    if (d.reason_code === "TASK_PAUSED") {
      return "Task is paused: dispatch stays withheld until resume.";
    }
    return "Conflict (409): " + (err.message || "the command conflicts " +
      "with the current state") +
      (d.reason_code ? " [" + d.reason_code + "]" : "") +
      (d.command_id ? " — command " + d.command_id : "");
  }
  return "Request failed (" + fmt(err.status) + "): " +
    (err.message || "see the uniform error fields");
}

async function api(path, opts) {
  const o = opts || {};
  const method = o.method || "GET";
  const headers = {};
  if (state.token) headers.Authorization = "Bearer " + state.token;
  let payload;
  if (o.body !== undefined) {
    payload = JSON.stringify(o.body);
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = state.token;
    headers["Idempotency-Key"] = o.key || idemKey(method, path, payload);
  }
  let res;
  try {
    res = await fetch(API + path, { method: method, headers: headers,
      body: payload });
  } catch (_networkErr) {
    throw { kind: "offline", status: 0, message: "network fetch failed" };
  }
  let data = null;
  try { data = await res.json(); } catch (_parseErr) { data = null; }
  if (!res.ok) {
    throw {
      kind: classifyFailure(res.status, data),
      status: res.status,
      code: data && data.code,
      message: data && data.message,
      details: data && data.details,
      requestId: data && data.request_id,
    };
  }
  return {
    data: data,
    replay: res.headers.get("X-Idempotent-Replay") === "true",
    requestId: res.headers.get("X-Request-Id"),
  };
}

function reportWrite(name, path, result) {
  const cmd = result.data && result.data.command;
  const bits = [name, path,
    cmd ? cmd.kind + " " + cmd.command_id : "ok",
    cmd ? "state=" + cmd.state : "",
    result.replay ? "REPLAY of existing command (no new write)" : "applied",
    result.requestId ? "req " + result.requestId : ""];
  logLine(result.replay ? "warn" : "ok", bits.filter(Boolean).join("  "));
}

function reportFailure(name, err) {
  logLine("bad", name + " — " + describeFailure(err));
}

/* ------------------------------------------------------------------ *
 * Auth / connection bootstrap.
 * ------------------------------------------------------------------ */

async function checkConnection() {
  const chip = q("conn-state");
  try {
    const res = await fetch(API + "/health/live");
    if (res.ok) {
      chip.textContent = "server: up";
      chip.className = "badge b-ok";
      return true;
    }
    chip.textContent = "server: unhealthy";
    chip.className = "badge b-warn";
    return false;
  } catch (_err) {
    chip.textContent = "server: unreachable";
    chip.className = "badge b-bad";
    return false;
  }
}

function saveToken() {
  const value = q("token-input").value.trim();
  if (!value) { banner("bad", "Enter a token first."); return; }
  state.token = value;
  sessionStorage.setItem("tc.token", value);
  banner("ok", "Token kept in this tab only (sessionStorage).");
  boot();
}

function forgetToken() {
  state.token = "";
  sessionStorage.removeItem("tc.token");
  q("token-input").value = "";
  stopStream();
  banner("muted", "Token forgotten. The server and its background work " +
    "are untouched — this only clears the value held in this tab.");
  location.reload();
}

async function loadProjects() {
  const seq = renderSeq;
  const select = clear(q("project-select"));
  let items;
  try {
    const res = await api("/projects?limit=200");
    items = res.data.items || [];
  } catch (err) {
    if (err.kind === "auth") banner("bad", describeFailure(err));
    else if (err.kind === "unavailable" || err.kind === "offline") {
      banner("bad", describeFailure(err));
    } else banner("bad", describeFailure(err));
    return;
  }
  banner("", "");
  if (!items.length) {
    select.appendChild(el("option", { value: "",
      text: "(no projects in the store)" }));
    state.projectId = "";
    sessionStorage.removeItem("tc.project");
    if (seq === renderSeq) renderView();
    return;
  }
  for (const p of items) {
    select.appendChild(el("option", { value: p.id,
      text: p.name + "  ·  " + p.id }));
  }
  if (!items.some(function (p) { return p.id === state.projectId; })) {
    state.projectId = items[0].id;
  }
  select.value = state.projectId;
  sessionStorage.setItem("tc.project", state.projectId);
  /* If the operator navigated while this fetch was in flight, their
     navigation already rendered the target view; a late re-render here
     would rebuild it under the live stream and blank the event list. */
  if (seq === renderSeq) renderView();
}

function boot() {
  checkConnection().then(function (up) {
    if (!up) {
      banner("bad", "API unreachable. Start it with `teamcraft-api` " +
        "(loopback by default); work already queued keeps running " +
        "server-side meanwhile.");
    }
    if (state.token) loadProjects();
  });
}

/* ------------------------------------------------------------------ *
 * Tabs + dispatch.
 * ------------------------------------------------------------------ */

function setView(view) {
  if (view !== "events") stopStream();
  state.view = view;
  state.pageOffset = 0;
  renderView();
}

/* Render generation counter.  Async views commit after their fetches;
   without a freshness check a slower stale render (e.g. the Overview
   kicked off by project-selection) can overwrite the view the operator
   navigated to seconds later.  Views commit only while their generation
   is still the newest. */
let renderSeq = 0;
function renderView() {
  renderSeq += 1;
  const buttons = q("nav").querySelectorAll(".tab");
  for (const b of buttons) {
    b.className = "tab" + (b.getAttribute("data-view") === state.view
      ? " active" : "");
  }
  const m = clear(main());
  if (!state.token) {
    m.appendChild(el("section", { id: "gate" }, [
      el("h2", { text: "Connect" }),
      para("Paste the local session token above and press Save. Nothing " +
        "on this page loads or pretends to hold data before that."),
    ]));
    return;
  }
  const handlers = {
    overview: viewOverview,
    tasks: viewTasks,
    task: viewTaskDetail,
    agents: viewAgents,
    ceo: viewCeo,
    events: viewEvents,
    wakes: viewWakes,
    approvals: viewApprovals,
    verifications: viewVerifications,
  };
  (handlers[state.view] || viewOverview)(m);
}

function needProject(m) {
  if (state.projectId) return true;
  m.appendChild(para("No project exists in the store yet. Create one " +
    "through the CLI/Core first — this console shows only what is real.",
  "empty"));
  return false;
}

function failInto(node, err, retry) {
  const box = el("div", { class: "statebox s-" + err.kind });
  box.appendChild(el("strong", { text: describeFailure(err) }));
  const bits = [];
  if (err.status) bits.push("HTTP " + err.status);
  if (err.code) bits.push(err.code);
  if (err.requestId) bits.push("request " + err.requestId);
  if (bits.length) box.appendChild(para(bits.join("  ·  "), "muted"));
  if (err.details && Object.keys(err.details).length) {
    box.appendChild(jsonBlock(err.details, "error details (verbatim, " +
      "text-only)"));
  }
  if (retry) {
    box.appendChild(el("button", { type: "button", text: "Retry",
      onclick: retry }));
  }
  node.appendChild(box);
}

/* ------------------------------------------------------------------ *
 * Overview: snapshot + scheduler gate + CEO init state.
 * ------------------------------------------------------------------ */

async function viewOverview(m) {
  const seq = renderSeq;
  m.appendChild(spinner("Loading project snapshot…"));
  if (!needProject(clear(m))) return;
  const pid = state.projectId;
  let snap, sched;
  const errs = [];
  try { snap = (await api("/projects/" + encodeURIComponent(pid) +
    "/snapshot")).data; }
  catch (err) { errs.push(err); }
  try { sched = (await api("/scheduler/state?project_id=" +
    encodeURIComponent(pid))).data.scheduler; }
  catch (err) { errs.push(err); }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  if (!snap) {
    for (const err of errs) failInto(m, err, function () {
      renderView();
    });
    return;
  }
  const counts = snap.counts || {};
  m.appendChild(heading("Project overview",
    [badge(snap.project.name, "ok"), badge(snap.project.id, "muted")]));
  const pausedBadge = sched
    ? (sched.paused ? badge("scheduler PAUSED", "bad")
      : badge("scheduler running", "ok"))
    : badge("scheduler state unknown", "warn");
  const ceoBadge = snap.ceo_initialization_state
    ? badge("CEO init: " + snap.ceo_initialization_state,
      statusKind(snap.ceo_initialization_state))
    : badge("CEO: not initialized", "warn");
  m.appendChild(kvTable([
    ["repo path", el("code", { text: fmt(snap.project.repo_path) })],
    ["created", fmt(snap.project.created_at)],
    ["durable event seq", fmt(snap.event_seq)],
    ["wake cursor (ceo)", snap.wake_cursor
      ? ("scanned " + snap.wake_cursor.scanned_seq +
        " · acked " + snap.wake_cursor.acknowledged_seq +
        " · processed " + snap.wake_cursor.processed_seq)
      : "none yet"],
    ["CEO initialization", ceoBadge],
    ["dispatch gate", pausedBadge],
  ]));
  m.appendChild(section("Tasks by status", countGrid(
    counts.tasks_by_status, TASK_STATUSES)));
  m.appendChild(section("Attempts by status", countGrid(
    counts.attempts_by_status)));
  m.appendChild(section("Sessions by state", countGrid(
    counts.sessions_by_state)));
  m.appendChild(section("Wakes by state", countGrid(
    counts.wakes_by_state, WAKE_STATES)));
  m.appendChild(schedulerControls(pid, sched));
  for (const err of errs) failInto(m, err, null);
}

function countGrid(map, canonical) {
  const box = el("div", { class: "grid" });
  const keys = canonical || Object.keys(map || {});
  let any = false;
  for (const k of keys) {
    const v = (map && typeof map[k] === "number") ? map[k] : 0;
    if (v > 0) any = true;
    box.appendChild(el("div", { class: "cell" }, [
      el("span", { class: "n", text: String(v) }),
      badge(k, v > 0 ? statusKind(k) : "muted"),
    ]));
  }
  if (!any) box.appendChild(para("Nothing recorded in this dimension.",
    "empty"));
  return box;
}

function schedulerControls(pid, sched) {
  const actor = el("input", { type: "text", value: "web-console",
    class: "in" });
  const reason = el("input", { type: "text", placeholder: "reason (required)",
    class: "in wide" });
  function control(action) {
    return el("button", {
      type: "button",
      disabled: action === "pause" ? (sched && sched.paused === true)
        : !(sched && sched.paused === true),
      text: action === "pause" ? "Pause scheduler" : "Resume scheduler",
      onclick: async function () {
        this.disabled = true;
        try {
          const res = await api("/scheduler/" + action, {
            method: "POST",
            body: { project_id: pid, actor: actor.value.trim() ||
              "web-console", reason: reason.value.trim() ||
              "operator action from web console" },
          });
          reportWrite("scheduler/" + action, "/api/v1/scheduler/" + action,
            res);
          renderView();
        } catch (err) {
          reportFailure("scheduler/" + action, err);
          this.disabled = false;
        }
      },
    });
  }
  const rows = [];
  if (sched) {
    rows.push(kvTable([
      ["paused", sched.paused ? badge("paused", "bad")
        : badge("active", "ok")],
      ["actor", fmt(sched.actor)],
      ["reason", fmt(sched.reason)],
      ["control version", fmt(sched.version)],
      ["updated", fmt(sched.updated_at)],
    ]));
  }
  rows.push(el("div", { class: "formrow" }, [actor, reason,
    control("pause"), control("resume")]));
  rows.push(para("Pause/resume writes a durable control row through the " +
    "Command API — it withholds NEW dispatch decisions only and never " +
    "touches a live Leader lease.", "muted"));
  return section("Scheduler gate", rows);
}

/* ------------------------------------------------------------------ *
 * Tasks: list, filters, pagination, create form.
 * ------------------------------------------------------------------ */

function selectOf(values, current, onchange) {
  const sel = el("select", { class: "in" });
  sel.appendChild(el("option", { value: "", text: "(any)" }));
  for (const v of values) {
    sel.appendChild(el("option", { value: v, text: v,
      selected: v === current }));
  }
  sel.addEventListener("change", onchange);
  return sel;
}

async function viewTasks(m) {
  const seq = renderSeq;
  if (!needProject(clear(m))) return;
  m.appendChild(spinner("Loading tasks…"));
  const pid = state.projectId;
  const qs = [];
  if (state.taskFilter.status) qs.push("status=" + state.taskFilter.status);
  if (state.taskFilter.kind) qs.push("kind=" + state.taskFilter.kind);
  qs.push("limit=50");
  qs.push("offset=" + state.pageOffset);
  let page, err = null;
  try {
    page = (await api("/projects/" + encodeURIComponent(pid) + "/tasks?" +
      qs.join("&"))).data;
  } catch (e) { err = e; }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  m.appendChild(heading("Tasks", [badge(pid, "muted")]));
  if (err) { failInto(m, err, function () { renderView(); }); return; }
  const p = page.page || {};
  const filters = el("div", { class: "formrow" }, [
    selectOf(TASK_STATUSES, state.taskFilter.status, function () {
      state.taskFilter.status = this.value; state.pageOffset = 0;
      renderView();
    }),
    selectOf(TASK_KINDS, state.taskFilter.kind, function () {
      state.taskFilter.kind = this.value; state.pageOffset = 0;
      renderView();
    }),
    badge("total " + fmt(p.total) + " · page offset " + fmt(p.offset),
      "muted"),
  ]);
  if (state.pageOffset > 0) {
    filters.appendChild(el("button", { type: "button", text: "Previous",
      onclick: function () {
        state.pageOffset = Math.max(0, state.pageOffset - 50); renderView();
      } }));
  }
  if (p.has_more) {
    filters.appendChild(el("button", { type: "button", text: "Next",
      onclick: function () {
        state.pageOffset += 50; renderView();
      } }));
  }
  m.appendChild(filters);
  if (!page.items.length) {
    m.appendChild(emptyNote("tasks match this filter"));
  } else {
    const table = el("table", { class: "list" }, [
      el("tr", {}, ["id", "title", "kind", "prio", "status", "paused",
        "risk", "version"].map(function (h) {
        return el("th", { text: h });
      })),
    ]);
    for (const t of page.items) {
      const tr = el("tr", {}, [
        el("td", {}, el("button", { type: "button", class: "linkish",
          text: t.id, onclick: function () {
            state.taskId = t.id; state.view = "task"; renderView();
          } })),
        el("td", { text: fmt(t.title) }),
        el("td", { text: fmt(t.kind) }),
        el("td", { text: fmt(t.priority) }),
        el("td", {}, badge(fmt(t.status), statusKind(t.status))),
        el("td", {}, badge(t.paused ? "paused" : "—",
          t.paused ? "bad" : "muted")),
        el("td", { text: fmt(t.risk) }),
        el("td", { text: "v" + fmt(t.version) }),
      ]);
      table.appendChild(tr);
      for (const b of truncationBadges(t)) {
        tr.appendChild(el("td", {}, b));
      }
    }
    m.appendChild(section("Task list", [table]));
  }
  m.appendChild(createTaskForm(pid));
}

function linesOf(textarea) {
  return textarea.value.split("\n").map(function (s) {
    return s.trim();
  }).filter(Boolean);
}

function createTaskForm(pid) {
  const title = el("input", { type: "text", class: "in wide",
    placeholder: "title (required)" });
  const desc = el("textarea", { class: "in", rows: "3",
    placeholder: "description" });
  const kind = el("select", { class: "in" }, [
    el("option", { value: "ship", text: "ship" }),
    el("option", { value: "scout", text: "scout" }),
  ]);
  const priority = el("input", { type: "number", value: "50",
    min: "0", max: "1000", class: "in" });
  const risk = el("input", { type: "text", value: "normal", class: "in" });
  const maxAttempts = el("input", { type: "number", value: "3",
    min: "1", max: "1000", class: "in" });
  const acceptance = el("textarea", { class: "in", rows: "2",
    placeholder: "acceptance — one item per line" });
  const ownership = el("textarea", { class: "in", rows: "2",
    placeholder: "ownership — one path per line (project-relative)" });
  const deps = el("input", { type: "text", class: "in wide",
    placeholder: "dependencies — task ids, comma separated" });
  const submit = el("button", { type: "button", text: "Create task",
    onclick: async function () {
      submit.disabled = true;
      const body = {
        project_id: pid,
        title: title.value.trim(),
        description: desc.value,
        kind: kind.value || "ship",
        priority: parseInt(priority.value, 10),
        risk: risk.value.trim() || "normal",
        max_attempts: parseInt(maxAttempts.value, 10),
        acceptance: linesOf(acceptance),
        ownership: linesOf(ownership),
        dependencies: deps.value.split(",").map(function (s) {
          return s.trim();
        }).filter(Boolean),
      };
      try {
        const res = await api("/tasks", { method: "POST", body: body });
        reportWrite("create task", "/api/v1/tasks", res);
        renderView();
      } catch (err) {
        reportFailure("create task", err);
        failInto(main(), err, null);
        submit.disabled = false;
      }
    } });
  return section("Create task (POST /tasks)", [
    el("div", { class: "formrow" }, [title]),
    el("div", { class: "formrow" }, [desc]),
    el("div", { class: "formrow" }, [kind, priority, risk, maxAttempts]),
    el("div", { class: "formrow" }, [acceptance, ownership]),
    el("div", { class: "formrow" }, [deps, submit]),
    para("The write carries Idempotency-Key + CSRF; unknown fields are " +
      "refused 422 before any core call (ownership/max_attempts cannot be " +
      "silently dropped).", "muted"),
  ]);
}

/* ------------------------------------------------------------------ *
 * Task detail: fields, edit, dependencies, policy, commands,
 * attempts/sessions/reports, verifications, approvals, recovery.
 * ------------------------------------------------------------------ */

async function viewTaskDetail(m) {
  const seq = renderSeq;
  const id = state.taskId;
  m.appendChild(spinner("Loading task " + id + "…"));
  const enc = encodeURIComponent(id);
  let task, attempts, deps, verifs, approvals, recovery, err = null;
  try {
    const results = await Promise.all([
      api("/tasks/" + enc),
      api("/tasks/" + enc + "/attempts?limit=200"),
      api("/tasks/" + enc + "/dependencies"),
      api("/tasks/" + enc + "/verifications?limit=50"),
      api("/tasks/" + enc + "/approvals?limit=50"),
      api("/tasks/" + enc + "/recovery-actions?limit=50"),
    ]);
    task = results[0].data; attempts = results[1].data;
    deps = results[2].data; verifs = results[3].data;
    approvals = results[4].data; recovery = results[5].data;
  } catch (e) { err = e; }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  if (err) { failInto(m, err, function () { renderView(); }); return; }

  m.appendChild(heading("Task " + task.id, [
    badge(task.status, statusKind(task.status)),
    badge("v" + task.version, "muted"),
    task.paused ? badge("paused", "bad") : badge("not paused", "ok"),
    el("button", { type: "button", text: "Back to list",
      onclick: function () { state.view = "tasks"; renderView(); } }),
  ]));
  m.appendChild(para("Query route GET /api/v1/tasks/{id} — every field " +
    "below is the durable row, incl. truncated flags.", "muted"));
  for (const b of truncationBadges(task)) m.appendChild(b);

  if (ACTIVE_RUN.has(task.status)) {
    m.appendChild(el("div", { class: "statebox s-conflict" }, [
      el("strong", { text: "Active run (" + task.status + "): identity " +
        "fields are immutable." }),
      para("id, project, kind, acceptance, ownership and max_attempts " +
        "have no API mutation path after creation. title/description/" +
        "priority/risk edits are critical: they bump the Task version and " +
        "expire PENDING commands bound to older evidence. Form fields " +
        "below that the API cannot change are disabled and labelled " +
        "immutable.", "muted"),
    ]));
  }

  m.appendChild(section("Identity (immutable through the M3-04 API)", [
    kvTable([
      ["id", el("code", { text: task.id })],
      ["project", el("code", { text: task.project_id })],
      ["kind", badge(task.kind, "muted")],
      ["acceptance", ul(task.acceptance)],
      ["ownership", ul(task.ownership)],
      ["max_attempts", fmt(task.max_attempts) + " (immutable after " +
        "creation; policy.max_attempts is the mutable mirror)"],
      ["created / updated", fmt(task.created_at) + " / " +
        fmt(task.updated_at)],
    ]),
  ]));

  m.appendChild(editTaskForm(task));
  m.appendChild(policyForm(task));
  m.appendChild(taskCommands(task));

  const depRows = [];
  if (!DEPENDENCY_EDITABLE.has(task.status)) {
    depRows.push(para("Dependencies are editable only while the task is " +
      "pending/ready/blocked — status is " + task.status + " (core rule " +
      "DEPENDENCY_EDIT_NOT_ALLOWED).", "muted"));
  }
  depRows.push(depList("depends_on", deps.depends_on, task, true));
  depRows.push(depList("required_by", deps.required_by, task, false));
  const depAdd = el("input", { type: "text", class: "in wide",
    placeholder: "depends_on task id" });
  depRows.push(el("div", { class: "formrow" }, [depAdd,
    el("button", { type: "button", text: "Add dependency",
      disabled: !DEPENDENCY_EDITABLE.has(task.status),
      onclick: async function () {
        this.disabled = true;
        try {
          const res = await api("/tasks/" + enc + "/dependencies", {
            method: "POST",
            body: { depends_on_id: depAdd.value.trim(),
              expected_version: task.version },
          });
          reportWrite("add dependency", "/api/v1/tasks/" + task.id +
            "/dependencies", res);
          renderView();
        } catch (e) {
          reportFailure("add dependency", e);
          failInto(main(), e, null);
        }
      } })]));
  m.appendChild(section("Dependencies (DAG)", depRows));

  m.appendChild(section("Attempts (separate layer from Task status)",
    attemptsPage(attempts, function (a) { return a.id; })));
  m.appendChild(section("Verifications for this task",
    verifTable(verifs.items || [])));
  m.appendChild(section("Approvals for this task",
    approvalTable(approvals.items || [])));
  m.appendChild(section("Recovery actions",
    recoveryTable(recovery.items || [])));
}

function ul(items) {
  if (!items || !items.length) return para("(none)", "empty");
  return el("ul", {}, items.map(function (i) {
    return el("li", { text: fmt(i) });
  }));
}

function editTaskForm(task) {
  const enc = encodeURIComponent(task.id);
  const title = el("input", { type: "text", class: "in wide",
    value: task.title });
  const desc = el("textarea", { class: "in", rows: "3",
    text: task.description });
  const priority = el("input", { type: "number", class: "in",
    value: String(task.priority), min: "0", max: "1000" });
  const risk = el("input", { type: "text", class: "in",
    value: task.risk });
  const versionNote = badge("expected_version = v" + task.version,
    "muted");
  const submit = el("button", { type: "button", text: "Save edit (PATCH)" });
  submit.addEventListener("click", async function () {
    submit.disabled = true;
    const body = { expected_version: task.version };
    if (title.value !== task.title) body.title = title.value;
    if (desc.value !== task.description) body.description = desc.value;
    if (parseInt(priority.value, 10) !== task.priority) {
      body.priority = parseInt(priority.value, 10);
    }
    if (risk.value !== task.risk) body.risk = risk.value;
    if (!Object.keys(body).slice(1).length) {
      banner("muted", "Nothing changed — no write sent.");
      submit.disabled = false;
      return;
    }
    try {
      const res = await api("/tasks/" + enc, { method: "PATCH",
        body: body });
      reportWrite("edit task", "/api/v1/tasks/" + task.id, res);
      renderView();
    } catch (e) {
      reportFailure("edit task", e);
      failInto(main(), e, function () { renderView(); });
      submit.disabled = false;
    }
  });
  return section("Edit mutable fields (title/description/priority/risk)", [
    el("div", { class: "formrow" }, [title]),
    el("div", { class: "formrow" }, [desc]),
    el("div", { class: "formrow" }, [priority, risk, versionNote, submit]),
    para("Optimistic concurrency: this edit binds the version shown above. " +
      "If the task moved on, the API answers 409 VERSION_CONFLICT and " +
      "nothing is written — reload, review, retry.", "muted"),
  ]);
}

function policyForm(task) {
  const enc = encodeURIComponent(task.id);
  const cur = task.execution_policy || {};
  /* Closed set, not free text: the select exposes exactly the four
   * DELIVERY_MODES (an empty choice keeps the stored value untouched).
   * A stored value outside the set — impossible through this API, kept
   * here only for honesty about pre-existing rows — is never preselected
   * and is re-sent only if the operator explicitly picks a valid mode. */
  const curDelivery = DELIVERY_MODES.includes(cur.delivery)
    ? cur.delivery : "";
  const delivery = el("select", { class: "in" },
    [el("option", { value: "", text: "delivery (unchanged)" })].concat(
      DELIVERY_MODES.map(function (mode) {
        return el("option", { value: mode, text: mode,
          selected: mode === curDelivery });
      })));
  const router = el("input", { type: "text", class: "in",
    placeholder: "router", value: fmt(cur.router) === "unknown"
      ? "" : fmt(cur.router) });
  const verifier = el("input", { type: "text", class: "in",
    placeholder: "verifier", value: fmt(cur.verifier) === "unknown"
      ? "" : fmt(cur.verifier) });
  const timeout = el("input", { type: "number", class: "in", min: "1",
    max: "3600",
    placeholder: "timeout_seconds",
    value: cur.timeout_seconds ? String(cur.timeout_seconds) : "" });
  const maxAttempts = el("input", { type: "number", class: "in", min: "1",
    max: "1000", placeholder: "max_attempts",
    value: cur.max_attempts ? String(cur.max_attempts) : "" });
  return section("Execution policy (persisted config — execution belongs " +
    "to M4)", [
    kvTable([["current policy", jsonBlock(cur, "stored policy")]]),
    el("div", { class: "formrow" }, [delivery, router, verifier, timeout,
      maxAttempts,
      el("button", { type: "button", text: "Update policy",
        onclick: async function () {
          const body = { expected_version: task.version };
          if (delivery.value && DELIVERY_MODES.includes(delivery.value)) {
            body.delivery = delivery.value;
          }
          if (router.value.trim()) body.router = router.value.trim();
          if (verifier.value.trim()) body.verifier = verifier.value.trim();
          if (timeout.value) body.timeout_seconds =
            parseInt(timeout.value, 10);
          if (maxAttempts.value) body.max_attempts =
            parseInt(maxAttempts.value, 10);
          this.disabled = true;
          try {
            const res = await api("/tasks/" + enc + "/policy",
              { method: "POST", body: body });
            reportWrite("update policy", "/api/v1/tasks/" + task.id +
              "/policy", res);
            renderView();
          } catch (e) {
            reportFailure("update policy", e);
            failInto(main(), e, function () { renderView(); });
            this.disabled = false;
          }
        } })]),
    para("Delivery is offered only as the closed set report_only / " +
      "local_only / direct_pr / ship (the same DELIVERY_MODES the Command " +
      "API enforces) — invalid free text cannot be submitted from this " +
      "form. Values are validated and persisted under CAS by the Command " +
      "API (closed key set; max_attempts mirrors the enforced column). " +
      "Nothing here routes or ships — policy execution is M4.", "muted"),
  ]);
}

function taskCommands(task) {
  const enc = encodeURIComponent(task.id);
  const actor = el("input", { type: "text", class: "in",
    value: "web-console" });
  const reason = el("input", { type: "text", class: "in wide",
    placeholder: "reason (required by the API)" });
  const attemptId = el("input", { type: "text", class: "in",
    placeholder: "attempt_id (optional)" });
  const actions = ["pause", "resume", "cancel", "retry", "wait",
    "reconcile"];
  const buttons = actions.map(function (action) {
    return el("button", {
      type: "button",
      text: action,
      disabled: (action === "pause" && task.paused) ||
        (action === "resume" && !task.paused) ||
        (["done", "failed", "cancelled"].includes(task.status)),
      onclick: async function () {
        this.disabled = true;
        const body = { action: action, expected_version: task.version,
          actor: actor.value.trim() || "web-console",
          reason: reason.value.trim() ||
            "operator action from web console" };
        if (attemptId.value.trim()) body.attempt_id = attemptId.value.trim();
        try {
          const res = await api("/tasks/" + enc + "/commands",
            { method: "POST", body: body });
          reportWrite("task/" + action, "/api/v1/tasks/" + task.id +
            "/commands", res);
          if (res.data.execution === "recorded") {
            banner("warn", "Recorded as a durable intent (202): a fenced " +
              "runtime executor must carry it out — the console never " +
              "stops a process or sends a prompt itself.");
          }
          renderView();
        } catch (e) {
          reportFailure("task/" + action, e);
          failInto(main(), e, function () { renderView(); });
          this.disabled = false;
        }
      },
    });
  });
  return section("Lifecycle commands (Idempotency-Key + expected_version)",
    [el("div", { class: "formrow" }, [actor, reason, attemptId]),
      el("div", { class: "formrow" }, buttons),
      para("pause/resume/cancel-attempt-free/retry apply synchronously; " +
        "cancel-with-occupied-attempt, wait and reconcile are recorded " +
        "durable intents (202) for the fenced executor.", "muted")]);
}

function depList(label, ids, task, removable) {
  const box = el("div", { class: "dep" });
  box.appendChild(el("h4", { text: label }));
  if (!ids || !ids.length) {
    box.appendChild(para("(none)", "empty"));
    return box;
  }
  for (const other of ids) {
    const row = el("div", { class: "formrow" }, [
      el("code", { text: other }),
      el("button", { type: "button", text: "view", onclick: function () {
        state.taskId = other; state.view = "task"; renderView();
      } }),
    ]);
    if (removable && DEPENDENCY_EDITABLE.has(task.status)) {
      row.appendChild(el("button", { type: "button", text: "remove",
        onclick: async function () {
          this.disabled = true;
          try {
            const res = await api("/tasks/" + encodeURIComponent(task.id) +
              "/dependencies/" + encodeURIComponent(other) +
              "?expected_version=" + task.version, { method: "DELETE" });
            reportWrite("remove dependency", "/api/v1/tasks/" + task.id +
              "/dependencies/" + other, res);
            renderView();
          } catch (e) {
            reportFailure("remove dependency", e);
            failInto(main(), e, function () { renderView(); });
            this.disabled = false;
          }
        } }));
    }
    box.appendChild(row);
  }
  return box;
}

function attemptsPage(page, _keyOf) {
  const items = (page && page.items) || [];
  if (!items.length) return emptyNote("attempts recorded yet");
  const out = [];
  for (const a of items) {
    out.push(attemptCard(a));
  }
  return out;
}

function attemptCard(a) {
  const card = el("div", { class: "attempt" });
  card.appendChild(el("div", { class: "hrow" }, [
    badge("attempt #" + fmt(a.attempt_no), statusKind(a.status)),
    badge(a.status, statusKind(a.status)),
    el("code", { text: fmt(a.id) }),
    a.protocol_version
      ? badge("protocol " + a.protocol_version, "muted") : null,
    a.protocol_hash ? badge("hash " + String(a.protocol_hash).slice(0, 12),
      "muted") : null,
  ]));
  card.appendChild(kvTable([
    ["agent", el("code", { text: fmt(a.agent_id) })],
    ["branch", el("code", { text: fmt(a.branch) })],
    ["commit", el("code", { text: fmt(a.commit_sha) })],
    ["started / finished", fmt(a.started_at) + " / " + fmt(a.finished_at)],
    ["worktree", el("code", { text: fmt(a.worktree_path) })],
  ]));
  for (const b of truncationBadges(a)) card.appendChild(b);
  card.appendChild(lazyAttempt(a.id));
  return card;
}

function lazyAttempt(attemptId) {
  const box = el("div", {});
  const btn = el("button", { type: "button", text: "Load attempt detail " +
    "(brief, delivery, sessions, reports)" });
  btn.addEventListener("click", async function () {
    btn.disabled = true;
    clear(box);
    box.appendChild(spinner("Loading attempt…"));
    try {
      const full = (await api("/attempts/" +
        encodeURIComponent(attemptId))).data;
      renderAttemptDetail(box, full);
    } catch (e) {
      clear(box);
      failInto(box, e, function () { btn.click(); });
      btn.disabled = false;
    }
  });
  box.appendChild(btn);
  return box;
}

function renderAttemptDetail(box, a) {
  clear(box);
  if (a.brief) {
    box.appendChild(jsonBlock({
      task_version: a.brief.task_version,
      brief_version: a.brief.brief_version,
      brief_digest: a.brief.brief_digest,
      deadline_at: a.brief.deadline_at,
      content_preview: a.brief.content_preview,
      content_truncated: a.brief.content_truncated,
    }, "brief (preview — truncated flag honoured)"));
  } else {
    box.appendChild(para("No brief stored for this attempt.", "empty"));
  }
  if (a.prompt_delivery) {
    box.appendChild(kvTable([
      ["prompt delivery", badge(a.prompt_delivery.state,
        statusKind(a.prompt_delivery.state))],
      ["delivery note", fmt(a.prompt_delivery.note &&
        a.prompt_delivery.note.note)],
    ]));
  }
  if (a.sessions && a.sessions.length) {
    const table = el("table", { class: "list" }, [
      el("tr", {}, ["session", "backend", "state", "native id", "pane",
        "last observed"].map(function (h) {
        return el("th", { text: h });
      })),
    ]);
    for (const s of a.sessions) {
      table.appendChild(el("tr", {}, [
        el("td", { text: fmt(s.id) }),
        el("td", { text: fmt(s.backend) }),
        el("td", {}, badge(fmt(s.state), statusKind(s.state))),
        el("td", { text: fmt(s.native_session_id) }),
        el("td", { text: fmt(s.pane_id) }),
        el("td", { text: fmt(s.last_observed_at) }),
      ]));
    }
    box.appendChild(para("Session state is the live signal only — it " +
      "never implies task completion.", "muted"));
    box.appendChild(table);
  } else {
    box.appendChild(para("No sessions for this attempt.", "empty"));
  }
  if (a.worker_reports && a.worker_reports.length) {
    for (const r of a.worker_reports) {
      box.appendChild(el("div", { class: "report" }, [
        el("div", { class: "hrow" }, [
          badge(r.report_kind, "muted"),
          r.outcome ? badge("outcome: " + r.outcome,
            statusKind(r.outcome)) : badge("outcome: not concluded",
            "warn"),
          badge("milestone " + fmt(r.milestone_id), "muted"),
        ]),
        para("summary: " + fmt(r.summary), ""),
        para("usage: " + fmtUsage(r.usage) + "  ·  task v" +
          fmt(r.task_version) + "  ·  report " + fmt(r.report_digest),
        "muted"),
      ].concat(truncationBadges(r))));
    }
  } else {
    box.appendChild(para("No WorkerReports recorded for this attempt.",
      "empty"));
  }
}

/* ------------------------------------------------------------------ *
 * Agents + members.
 * ------------------------------------------------------------------ */

async function viewAgents(m) {
  const seq = renderSeq;
  m.appendChild(spinner("Loading agents and members…"));
  let agents, members, err = null;
  try {
    const results = await Promise.all([
      api("/agents?limit=200"), api("/members?limit=200"),
    ]);
    agents = results[0].data; members = results[1].data;
  } catch (e) { err = e; }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  if (err) { failInto(m, err, function () { renderView(); }); return; }
  m.appendChild(heading("Agents and runtime members", []));
  const agentRows = (agents.items || []).map(function (a) {
    return el("tr", {}, [
      el("td", { text: fmt(a.id) }),
      el("td", { text: fmt(a.role) }),
      el("td", { text: fmt(a.harness) }),
      el("td", { text: fmt(a.model) }),
      el("td", { text: fmt(a.provider) }),
      el("td", {}, jsonBlock(a.config, "config")),
    ]);
  });
  m.appendChild(section("Agents (GET /agents)",
    agentRows.length ? [el("table", { class: "list" },
      [el("tr", {}, ["id", "role", "harness", "model", "provider",
        "config"].map(function (h) {
        return el("th", { text: h });
      }))].concat(agentRows))]
      : [emptyNote("agents registered")]));
  const memberRows = (members.items || []).map(function (mm) {
    return el("tr", {}, [
      el("td", { text: fmt(mm.name) }),
      el("td", { text: fmt(mm.role) }),
      el("td", { text: fmt(mm.kind) }),
      el("td", {}, badge(fmt(mm.phase), statusKind(mm.phase))),
      el("td", {}, badge(fmt(mm.live_state), statusKind(mm.live_state))),
      el("td", { text: fmt(mm.task_id) }),
      el("td", { text: fmt(mm.attempt_id) }),
      el("td", { text: fmt(mm.session_id) }),
      el("td", {}, badge(mm.read_only ? "read-only" : "writable",
        mm.read_only ? "ok" : "warn")),
      el("td", {}, badge(mm.prompt_may_have_been_delivered
        ? "prompt may be delivered" : "prompt not delivered",
      mm.prompt_may_have_been_delivered ? "warn" : "muted")),
      el("td", {}, jsonBlock({ responsibility: mm.responsibility,
        task: mm.task, error: mm.error, ownership: mm.ownership },
      "text fields")),
    ].concat(truncationBadges(mm)));
  });
  m.appendChild(section("Members (GET /members — same facts as teamctl " +
    "status)",
  memberRows.length ? [el("table", { class: "list" },
    [el("tr", {}, ["name", "role", "kind", "phase", "live", "task",
      "attempt", "session", "scope", "prompt",
      "text"].map(function (h) {
      return el("th", { text: h });
    }))].concat(memberRows))]
    : [emptyNote("runtime members")]));
}

/* ------------------------------------------------------------------ *
 * CEO: protocol facts, initialization, manual wake (queue only).
 * ------------------------------------------------------------------ */

async function viewCeo(m) {
  const seq = renderSeq;
  if (!needProject(clear(m))) return;
  m.appendChild(spinner("Loading CEO view…"));
  const pid = state.projectId;
  let ceo, err = null;
  try {
    ceo = (await api("/projects/" + encodeURIComponent(pid) + "/ceo")).data;
  } catch (e) { err = e; }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  if (err) { failInto(m, err, function () { renderView(); }); return; }
  const init = ceo.initialization;
  const proto = ceo.protocol || {};
  m.appendChild(heading("CEO", [badge(pid, "muted")]));
  if (!init) {
    m.appendChild(section("Initialization",
      [emptyNote("CEO initialization for this project"),
        para("The runtime records initialization through the Core flow; " +
          "this console shows the stored state and never infers it from " +
          "a prompt.", "muted")]));
  } else {
    m.appendChild(section("Initialization (durable record)", [
      kvTable([
        ["state", badge(init.state, statusKind(init.state))],
        ["canonical name", fmt(init.canonical_ceo_name)],
        ["team id", el("code", { text: fmt(init.team_id) })],
        ["session (teamcraft/native/terminal)",
          fmt(init.teamcraft_session_id) + " / " +
            fmt(init.native_session_id) + " / " + fmt(init.terminal_id)],
        ["protocol version", fmt(init.protocol_version)],
        ["protocol hash", el("code", { text: fmt(init.protocol_hash) })],
        ["runtime skill hash",
          el("code", { text: fmt(init.runtime_skill_hash) })],
        ["snapshot digest", el("code", { text: fmt(init.snapshot_digest) })],
        ["receipt source", badge(fmt(init.receipt_source),
          init.receipt_source === "assumed_default" ? "warn" : "ok")],
        ["identity mode", fmt(init.identity_mode)],
        ["herdr session identity present",
          fmt(init.has_herdr_session_identity)],
        ["live process identity present",
          fmt(init.has_live_process_identity)],
        ["nonce consumed", fmt(init.nonce_consumed)],
        ["nonce expires", fmt(init.nonce_expires_at)],
        ["updated", fmt(init.updated_at)],
      ]),
      jsonBlock(init.detail || {}, "detail (text-only)"),
    ].concat(truncationBadges(init && init.detail ? init.detail : {}))));
  }
  m.appendChild(section("Protocol discovery (CEO harness files)", [
    kvTable([
      ["available", proto.available ? badge("available", "ok")
        : badge("not available", "bad")],
      ["protocol version", fmt(proto.protocol_version)],
      ["sha256", el("code", { text: fmt(proto.sha256) })],
      ["codex", fmt((proto.harnesses || {}).codex)],
      ["opencode", fmt((proto.harnesses || {}).opencode)],
    ]),
  ]));

  const actor = el("input", { type: "text", class: "in",
    value: "web-console" });
  const reason = el("input", { type: "text", class: "in wide",
    placeholder: "reason (required)" });
  const bindTask = el("input", { type: "text", class: "in",
    placeholder: "task_id (optional)" });
  const bindVersion = el("input", { type: "number", class: "in", min: "0",
    placeholder: "expected_task_version" });
  m.appendChild(section("Ask the CEO to take a look (queue only)", [
    el("div", { class: "formrow" }, [actor, reason]),
    el("div", { class: "formrow" }, [bindTask, bindVersion,
      el("button", { type: "button", text: "Request CEO processing",
        onclick: async function () {
          this.disabled = true;
          const body = { action: "request_processing", project_id: pid,
            actor: actor.value.trim() || "web-console",
            reason: reason.value.trim() ||
              "operator request from web console" };
          if (bindTask.value.trim()) {
            body.task_id = bindTask.value.trim();
            body.expected_task_version = parseInt(bindVersion.value, 10);
          }
          try {
            const res = await api("/ceo/commands", { method: "POST",
              body: body });
            reportWrite("ceo wake request", "/api/v1/ceo/commands", res);
            banner("ok", "Queued (202): a durable ceo.wake_request " +
              "command + event were recorded. The next fenced scheduler " +
              "tick may fold it into a merged Wake candidate — this page " +
              "cannot and did not send a prompt.");
            this.disabled = false;
          } catch (e) {
            reportFailure("ceo wake request", e);
            failInto(main(), e, null);
            this.disabled = false;
          }
        } })]),
    para("Manual wake is a durable intent with a bounded TTL — unconsumed " +
      "requests expire through the command sweep instead of lingering " +
      "forever.", "muted"),
  ]));
}

/* ------------------------------------------------------------------ *
 * Events: SSE live tail with seq dedup + snapshot-required resync.
 * ------------------------------------------------------------------ */

function stopStream() {
  state.streamActive = false;
  if (state.streamTimer) {
    clearTimeout(state.streamTimer);
    state.streamTimer = null;
  }
  if (state.streamAbort) {
    state.streamAbort.abort();
    state.streamAbort = null;
  }
}

function parseSse(buffer) {
  const frames = [];
  let idx;
  while ((idx = buffer.indexOf("\n\n")) !== -1) {
    const raw = buffer.slice(0, idx);
    buffer = buffer.slice(idx + 2);
    const frame = { event: "message", id: null, data: "", comment: false };
    const dataLines = [];
    for (let line of raw.split("\n")) {
      if (line.startsWith(":")) { frame.comment = true; continue; }
      if (line.startsWith("retry:")) continue;
      const colon = line.indexOf(":");
      let field = line, value = "";
      if (colon !== -1) {
        field = line.slice(0, colon);
        value = line.slice(colon + 1);
        /* SSE spec: a single leading space after the colon is part of the
           framing and must be removed. */
        if (value.startsWith(" ")) value = value.slice(1);
      }
      if (field === "event") frame.event = value.trim();
      else if (field === "id") frame.id = value.trim();
      else if (field === "data") dataLines.push(value);
    }
    frame.data = dataLines.join("\n");
    frames.push(frame);
  }
  return [frames, buffer];
}

async function viewEvents(m) {
  if (!needProject(clear(m))) return;
  const pid = state.projectId;
  m.appendChild(heading("Event stream", [
    badge("last seq " + state.lastSeq, "muted"),
    badge("durable SQLite seq is the order authority", "muted"),
  ]));
  const controls = el("div", { class: "formrow" });
  const connectBtn = el("button", { type: "button", text: "Connect" });
  const disconnectBtn = el("button", { type: "button",
    text: "Disconnect", disabled: true });
  controls.appendChild(connectBtn);
  controls.appendChild(disconnectBtn);
  controls.appendChild(el("button", { type: "button", text: "REST " +
    "backfill (events after last seq)", onclick: async function () {
    this.disabled = true;
    try {
      const page = (await api("/events?project_id=" +
        encodeURIComponent(pid) + "&after_seq=" + state.lastSeq +
        "&limit=100")).data;
      const items = page.items || [];
      for (const e of items) {
        /* Advance the cursor as REST-delivered events render; otherwise a
           later SSE (re)connect resumes behind these and every backfilled
           event lands in the DOM a second time. */
        if (typeof e.seq === "number" && e.seq > state.lastSeq) {
          state.lastSeq = e.seq;
        }
        renderEventCard(e);
      }
      banner("muted", "Backfilled " + items.length +
        " events via REST (has_more=" + fmt(page.page.has_more) + ").");
    } catch (e) {
      reportFailure("events backfill", e);
    }
    this.disabled = false;
  } }));
  m.appendChild(controls);
  m.appendChild(para("Closing this tab or disconnecting affects nothing " +
    "server-side — events keep being written by the runtime; on reopen " +
    "the console resumes from the durable seq.", "muted"));
  const list = el("div", { class: "events", id: "event-list" });
  m.appendChild(list);

  connectBtn.addEventListener("click", function () {
    connectBtn.disabled = true;
    disconnectBtn.disabled = false;
    state.streamActive = true;
    openStream(pid, list, function () {
      connectBtn.disabled = false;
      disconnectBtn.disabled = true;
    });
  });
  disconnectBtn.addEventListener("click", function () {
    stopStream();
    connectBtn.disabled = false;
    disconnectBtn.disabled = true;
    banner("muted", "Disconnected from the stream; the store and runtime " +
      "continue untouched.");
  });
  if (!state.streamActive) {
    /* Auto-connect on view entry.  streamActive MUST be set before the
       read loop starts: openStream tails while (state.streamActive), so
       without this the loop exited instantly and no event ever rendered
       unless Connect was clicked by hand. */
    state.streamActive = true;
    connectBtn.disabled = true;
    disconnectBtn.disabled = false;
    openStream(pid, list, function () {
      connectBtn.disabled = false;
      disconnectBtn.disabled = true;
    });
  }
}

function renderEventCard(e) {
  const list = q("event-list");
  if (!list) return;
  const card = el("div", { class: "event" }, [
    el("div", { class: "hrow" }, [
      badge("seq " + fmt(e.seq), "ok"),
      badge(e.kind, statusKind(e.kind.split(".")[0])),
      e.progress_class ? badge(e.progress_class, "muted") : null,
      e.payload_truncated ? badge("payload truncated", "warn") : null,
      el("span", { class: "muted", text: fmt(e.occurred_at) }),
    ]),
    el("div", { class: "muted", text:
      "task " + fmt(e.task_id) + "  ·  attempt " + fmt(e.attempt_id) +
      "  ·  event " + fmt(e.event_id) }),
  ]);
  if (e.payload !== null && e.payload !== undefined) {
    card.appendChild(jsonBlock(e.payload, "payload"));
  }
  list.insertBefore(card, list.firstChild);
  while (list.children.length > 300) list.removeChild(list.lastChild);
}

async function openStream(pid, list, onClosed) {
  const enc = encodeURIComponent(pid);
  const url = API + "/events/stream?project_id=" + enc +
    "&after=" + state.lastSeq;
  const controller = new AbortController();
  state.streamAbort = controller;
  if (list) {
    clear(list).appendChild(spinner("Stream connecting… seq " +
      state.lastSeq));
  }
  let res;
  try {
    res = await fetch(url, {
      headers: {
        Authorization: "Bearer " + state.token,
        "Last-Event-ID": String(state.lastSeq),
        Accept: "text/event-stream",
      },
      signal: controller.signal,
    });
  } catch (_e) {
    if (!controller.signal.aborted) {
      banner("bad", "Stream offline — the console reconnects shortly; " +
        "server-side work continues meanwhile.");
      scheduleReconnect(pid, list, onClosed);
    }
    return;
  }
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch (_e) { data = null; }
    const err = { kind: classifyFailure(res.status, data),
      status: res.status, code: data && data.code,
      message: data && data.message, details: data && data.details,
      requestId: data && data.request_id };
    if (err.kind === "stale" || (err.status === 409 && data &&
      data.details && data.details.action === "snapshot_required")) {
      banner("warn", "Cursor ahead of the durable store (server restarted " +
        "or seq reset) — resyncing from a snapshot.");
      await resyncSnapshot(pid);
      scheduleReconnect(pid, list, onClosed);
      return;
    }
    banner("bad", describeFailure(err));
    if (onClosed) onClosed();
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (state.streamActive) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const parsed = parseSse(buffer);
      buffer = parsed[1];
      for (const frame of parsed[0]) {
        if (frame.comment) continue;
        if (frame.event === "snapshot_required") {
          let info = {};
          try { info = JSON.parse(frame.data); } catch (_e) { info = {}; }
          banner("warn", "snapshot_required (" + fmt(info.reason) +
            "): refetching snapshot and resuming at seq " +
            fmt(info.resume_after) + ".");
          await resyncSnapshot(pid);
          throw { stopAndReconnect: true };
        }
        if (!frame.data) continue;
        let dto;
        try { dto = JSON.parse(frame.data); } catch (_e) { continue; }
        if (typeof dto.seq !== "number") continue;
        if (dto.seq <= state.lastSeq) continue; /* client-side dedupe */
        state.lastSeq = dto.seq;
        renderEventCard(dto);
      }
    }
    /* server closed cleanly (max_duration expiry): reconnect resuming
     * from the durable cursor. */
    if (state.streamActive) {
      banner("muted", "Stream expired at its bound — reconnecting from " +
        "seq " + state.lastSeq + ".");
      scheduleReconnect(pid, list, onClosed);
    } else if (onClosed) onClosed();
  } catch (e) {
    if (e && e.stopAndReconnect && state.streamActive) {
      scheduleReconnect(pid, list, onClosed);
    } else if (!controller.signal.aborted) {
      banner("bad", "Stream interrupted — resuming from seq " +
        state.lastSeq + ".");
      scheduleReconnect(pid, list, onClosed);
    } else if (onClosed) onClosed();
  }
}

function scheduleReconnect(pid, list, onClosed) {
  if (!state.streamActive) { if (onClosed) onClosed(); return; }
  if (state.streamTimer) clearTimeout(state.streamTimer);
  state.streamTimer = setTimeout(function () {
    state.streamTimer = null;
    if (state.streamActive) openStream(pid, list, onClosed);
  }, 1500);
}

async function resyncSnapshot(pid) {
  try {
    const snap = (await api("/projects/" + encodeURIComponent(pid) +
      "/snapshot")).data;
    if (typeof snap.event_seq === "number") {
      state.lastSeq = Math.min(state.lastSeq, snap.event_seq);
    }
  } catch (_e) {
    /* snapshot unavailable — keep cursor; the next stream attempt will
     * re-prove it or demand another snapshot instead of guessing. */
  }
}

/* ------------------------------------------------------------------ *
 * Wakes: lifecycle incl. failures and recovery records.
 * ------------------------------------------------------------------ */

async function viewWakes(m) {
  const seq = renderSeq;
  if (!needProject(clear(m))) return;
  const pid = state.projectId;
  m.appendChild(spinner("Loading wakes…"));
  let wakes, cursor, recovery, err = null;
  try {
    const qs = state.wakeFilter ? "&state=" + state.wakeFilter : "";
    const results = await Promise.all([
      api("/wakes?project_id=" + encodeURIComponent(pid) + qs +
        "&limit=100"),
      api("/wake-cursors/" + encodeURIComponent(pid)).catch(function () {
        return { data: null };
      }),
      api("/recovery-actions?project_id=" + encodeURIComponent(pid) +
        "&limit=100"),
    ]);
    wakes = results[0].data;
    cursor = results[1].data;
    recovery = results[2].data;
  } catch (e) { err = e; }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  if (err) { failInto(m, err, function () { renderView(); }); return; }
  m.appendChild(heading("Wakes", [badge(pid, "muted"),
    badge("merged · deduped · durable", "muted")]));
  m.appendChild(el("div", { class: "formrow" }, [
    selectOf(WAKE_STATES, state.wakeFilter, function () {
      state.wakeFilter = this.value; renderView();
    }),
    cursor ? badge("cursor scanned " + cursor.scanned_seq + " · acked " +
      cursor.acknowledged_seq + " · processed " + cursor.processed_seq,
    "muted") : badge("no wake cursor yet", "warn"),
  ]));
  const items = wakes.items || [];
  if (!items.length) {
    m.appendChild(emptyNote("wakes for this filter"));
  } else {
    const rows = [];
    for (const w of items) {
      const failed = w.state === "unknown" || w.state === "blocked";
      rows.push(el("tr", {}, [
        el("td", { text: fmt(w.id) }),
        el("td", {}, badge(w.state, failed ? "bad" :
          statusKind(w.state))),
        el("td", { text: fmt(w.target_ceo_name) }),
        el("td", { text: "seq " + fmt(w.first_seq) + "–" + fmt(w.last_seq) +
          " (" + fmt(w.event_count) + " events)" }),
        el("td", { class: "muted", text: fmt(w.dedupe_key) }),
        el("td", { class: "muted",
          text: "created " + fmt(w.created_at) + " · submitted " +
            fmt(w.submitted_at) + " · acked " + fmt(w.acknowledged_at) +
            " · processed " + fmt(w.processed_at) }),
        el("td", {}, jsonBlock(w.detail || {}, "detail")),
        el("td", { class: "muted", text: fmt(w.command_id) }),
      ]));
      if (failed) {
        rows.push(el("tr", {}, [el("td", { colspan: "8" }, [
          para("Failure state " + w.state + ": an ambiguous or blocked " +
            "delivery is never auto-retried — reconcile it through the " +
            "recovery records below.", "muted"),
        ])]));
      }
    }
    m.appendChild(section("Wake queue (state kept separate at every " +
      "stage)", [el("table", { class: "list" },
      [el("tr", {}, ["id", "state", "target", "merged events", "dedupe",
        "lifecycle timestamps", "detail", "command"].map(function (h) {
        return el("th", { text: h });
      }))].concat(rows))]));
  }
  m.appendChild(section("Recovery records (durable intents + outcomes)",
    recoveryTable(recovery.items || [])));
}

/* ------------------------------------------------------------------ *
 * Approvals + Verifications.
 * ------------------------------------------------------------------ */

async function viewApprovals(m) {
  const seq = renderSeq;
  if (!needProject(clear(m))) return;
  const pid = state.projectId;
  m.appendChild(spinner("Loading approvals…"));
  let approvals, verifs, err = null;
  try {
    const results = await Promise.all([
      api("/approvals?project_id=" + encodeURIComponent(pid) +
        "&limit=100"),
      api("/verifications?project_id=" + encodeURIComponent(pid) +
        "&limit=100"),
    ]);
    approvals = results[0].data; verifs = results[1].data;
  } catch (e) { err = e; }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  if (err) { failInto(m, err, function () { renderView(); }); return; }
  m.appendChild(heading("Approvals", [badge(pid, "muted")]));
  m.appendChild(section("Recorded decisions",
    approvalTable(approvals.items || [])));
  const decided = new Set((approvals.items || []).map(function (a) {
    return a.verification_id;
  }));
  const pending = (verifs.items || []).filter(function (v) {
    return !decided.has(v.id);
  });
  m.appendChild(section("Verifications awaiting a decision",
    pending.length ? decisionForms(pending) : [
      emptyNote("verifications awaiting a decision")]));
  m.appendChild(para("Decisions run through the Command API under the " +
    "API's own short Leader lease: while the runtime holds the lease the " +
    "write is refused 409 LEADER_BUSY and nothing is recorded. Accept is " +
    "only ever valid for the latest passing verification of a task " +
    "(core rule), and binds the CURRENT task version.", "muted"));
}

function decisionForms(verifs) {
  const out = [];
  for (const v of verifs) {
    const reason = el("input", { type: "text", class: "in wide",
      placeholder: "reason (required)" });
    function decide(decision) {
      return el("button", { type: "button",
        text: decision === "accept" ? "Accept" : "Reject",
        onclick: async function () {
          this.disabled = true;
          try {
            const current = (await api("/tasks/" +
              encodeURIComponent(v.task_id))).data;
            const res = await api("/verifications/" +
              encodeURIComponent(v.id) + "/approvals", {
              method: "POST",
              body: { decision: decision,
                actor: "web-console",
                reason: reason.value.trim() ||
                  "operator decision from web console",
                expected_task_version: current.version },
            });
            reportWrite("approval " + decision, "/api/v1/verifications/" +
              v.id + "/approvals", res);
            renderView();
          } catch (e) {
            reportFailure("approval " + decision, e);
            failInto(main(), e, function () { renderView(); });
            this.disabled = false;
          }
        } });
    }
    out.push(el("div", { class: "decision" }, [
      el("div", { class: "hrow" }, [
        badge(v.status, statusKind(v.status)),
        el("code", { text: v.id }),
        el("span", { class: "muted", text: "task " + fmt(v.task_id) +
          " · attempt " + fmt(v.attempt_id) + " · task v" +
          fmt(v.task_version) + " · dispatch v" +
          fmt(v.dispatch_task_version) }),
      ]),
      el("div", { class: "formrow" }, [reason, decide("accept"),
        decide("reject")]),
    ]));
  }
  return out;
}

function approvalTable(items) {
  if (!items.length) return emptyNote("approval decisions");
  const rows = items.map(function (a) {
    return el("tr", {}, [
      el("td", { text: fmt(a.id) }),
      el("td", {}, badge(a.decision,
        a.decision === "accept" ? "ok" : "bad")),
      el("td", { text: fmt(a.proposed_action) }),
      el("td", { text: fmt(a.actor) }),
      el("td", { text: "task " + fmt(a.task_id) + " v" +
        fmt(a.task_version) }),
      el("td", { class: "muted", text: fmt(a.verification_id) }),
      el("td", {}, jsonBlock({ reason: a.reason,
        brief_digest: a.brief_digest,
        artifact_digest: a.artifact_digest,
        config_version: a.config_version,
        rules_version: a.rules_version }, "evidence binding")),
      el("td", { class: "muted", text: fmt(a.created_at) }),
    ].concat(truncationBadges(a)));
  });
  return [el("table", { class: "list" },
    [el("tr", {}, ["id", "decision", "proposed action", "actor", "task",
      "verification", "evidence", "created"].map(function (h) {
      return el("th", { text: h });
    }))].concat(rows))];
}

async function viewVerifications(m) {
  const seq = renderSeq;
  if (!needProject(clear(m))) return;
  m.appendChild(spinner("Loading verifications…"));
  const pid = state.projectId;
  let verifs, configs, err = null;
  try {
    const results = await Promise.all([
      api("/verifications?project_id=" + encodeURIComponent(pid) +
        "&limit=100"),
      api("/verification-configs?project_id=" + encodeURIComponent(pid) +
        "&limit=20"),
    ]);
    verifs = results[0].data; configs = results[1].data;
  } catch (e) { err = e; }
  if (seq !== renderSeq) return;  /* superseded by a newer navigation */
  clear(m);
  if (err) { failInto(m, err, function () { renderView(); }); return; }
  m.appendChild(heading("Verifications", [badge(pid, "muted")]));
  m.appendChild(section("Runs (report-only / scout until M4 evidence)",
    verifTable(verifs.items || [])));
  const cfgRows = (configs.items || []).map(function (c) {
    return el("tr", {}, [
      el("td", { text: "v" + fmt(c.config_version) }),
      el("td", { text: fmt(c.rules_version) }),
      el("td", {}, jsonBlock(c.argv, "argv")),
      el("td", { text: fmt(c.timeout_seconds) + "s" }),
      el("td", { class: "muted", text: fmt(c.config_digest) }),
      el("td", { text: fmt(c.approved_by) }),
      el("td", { class: "muted", text: fmt(c.created_at) }),
    ]);
  });
  m.appendChild(section("Verifier configs (versions)",
    cfgRows.length ? [el("table", { class: "list" },
      [el("tr", {}, ["config", "rules", "argv", "timeout", "digest",
        "approved by", "created"].map(function (h) {
        return el("th", { text: h });
      }))].concat(cfgRows))]
      : [emptyNote("verification configs")]));
}

function verifTable(items) {
  if (!items.length) return emptyNote("verification runs");
  const rows = items.map(function (v) {
    const art = v.artifact || {};
    const cells = [
      el("td", { text: fmt(v.id) }),
      el("td", {}, badge(v.status, statusKind(v.status))),
      el("td", { text: fmt(v.exit_code) }),
      el("td", { text: "task " + fmt(v.task_id) + " v" +
        fmt(v.task_version) }),
      el("td", { text: fmt(v.attempt_id) + " #" + fmt(v.attempt_no) }),
      el("td", {}, jsonBlock(v.argv, "argv")),
      el("td", {}, jsonBlock({ size_bytes: art.size_bytes,
        digest: art.digest, binary: art.binary,
        preview_truncated: art.preview_truncated,
        preview: art.preview }, "artifact preview")),
      el("td", { class: "muted", text: fmt(v.created_at) }),
      el("td", { class: "muted",
        text: "brief " + fmt(v.brief_digest) + " · report " +
          fmt(v.worker_report_digest) + " · cfg v" +
          fmt(v.config_version) }),
    ];
    return el("tr", {}, cells);
  });
  return [el("table", { class: "list" },
    [el("tr", {}, ["id", "status", "exit", "task", "attempt", "argv",
      "artifact", "created", "evidence"].map(function (h) {
      return el("th", { text: h });
    }))].concat(rows))];
}

/* Recovery actions come from the commands table (kind recovery.*); the
 * row shows the durable request, its command state, and the honest TTL —
 * never a claim that a retry ran. */
function recoveryTable(items) {
  if (!items.length) return emptyNote("recovery actions");
  const rows = items.map(function (a) {
    const cells = [
      el("td", { text: fmt(a.command_id) }),
      el("td", { text: fmt(a.action) }),
      el("td", {}, badge(fmt(a.state), statusKind(a.state))),
      el("td", { text: fmt(a.task_id) }),
      el("td", { text: "#" + fmt(a.attempt_no) }),
      el("td", { text: fmt(a.actor) }),
      el("td", { text: fmt(a.reason) +
        (a.reason_truncated ? " (truncated)" : "") }),
      el("td", { class: "muted", text: fmt(a.requested_at) }),
      el("td", { class: "muted",
        text: a.expires_at ? "expires " + fmt(a.expires_at)
                           : "no TTL" }),
    ];
    return el("tr", {}, cells);
  });
  return [el("table", { class: "list" },
    [el("tr", {}, ["command", "action", "state", "task", "attempt",
      "actor", "reason", "requested", "ttl"].map(function (h) {
      return el("th", { text: h });
    }))].concat(rows))];
}

/* ------------------------------------------------------------------ *
 * Wiring.
 * ------------------------------------------------------------------ */

q("token-save").addEventListener("click", saveToken);
q("token-forget").addEventListener("click", forgetToken);
q("refresh").addEventListener("click", function () {
  state.token = sessionStorage.getItem("tc.token") || state.token;
  boot();
});
q("project-select").addEventListener("change", function () {
  state.projectId = this.value;
  sessionStorage.setItem("tc.project", this.value);
  state.lastSeq = 0;
  renderView();
});
q("token-input").addEventListener("keydown", function (ev) {
  if (ev.key === "Enter") saveToken();
});
for (const b of q("nav").querySelectorAll(".tab")) {
  b.addEventListener("click", function () {
    setView(this.getAttribute("data-view"));
  });
}

boot();
