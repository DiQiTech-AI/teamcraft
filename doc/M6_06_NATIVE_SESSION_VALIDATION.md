# M6-06 — Preregistered native session identity → Herdr (validation record)

| Field | Value |
|---|---|
| Status | **Source mechanism verified; the prepared isolated pane now installs and registers the TUI bridge. No real Herdr pane identity is claimed.** |
| Base (record-time snapshot) | `main` @ `753b096` + this slice's working-tree changes |
| Environment (record-time snapshot) | `opencode` v2.4.0; `herdr` 0.9.3; `node` v26.11.1; server bridge `opencode` v13; TUI bridge `opencode-tui` v13 |
| Provenance correction (2026-11-26) | The two rows above are the original snapshot: base **was** `753b096` and the then-installed CLI **was** `opencode` v2.4.0. Re-verified on 2026-11-26: current `HEAD` is `f189ba7` (6 commits later: `475fc88`, `2560297`, `42ea56b`, `1e909d0`, `6c13818`, `f189ba7`) and the installed CLI is `opencode` **v2.4.1**. No commit or push was performed by this slice; the body evidence below remains a record of the original snapshot. |
| Owned artifacts | `scripts/m606_prepare_opencode_pane.py`, `tests/test_m606_prepare_opencode_pane.py`, `tests/test_m606_pane_launch_contract.py`, this file |
| Constraints honored | No prompt, no model inference, no credential lookup, no live pane mutation, no live `teamcraft` DB access, no commit/push. The DeepSeek pane was **not** used as evidence. |

## 1. Question and answer

When a native OpenCode session is registered **before** the pane launches
(`session.create`, empty body, no message), how does the isolated bridge report
that exact `ses_…` identity to Herdr — and is it reported **without inference**
from the model, provider catalog, pane id, or operator expectation?

**Verified:** both Herdr OpenCode integrators carry the id verbatim from the
harness. The prepared isolated pane now includes the TUI selection bridge and a
minimal local registration that OpenCode v2.4.0 recognizes; the copied bridge
emits the preregistered id to a disposable socket.

**Not claimed:** that a real interactive Herdr pane has loaded this configuration
and exposed the id. That requires an operator-owned live pane; none was touched in
this slice.

## 2. Two independent Herdr integrations

`herdr integration status` reports only the **server** integration
(`opencode: current (v13)`). The **TUI** integration (`opencode-tui` v13) is a
separate file registered through the user's OpenCode CLI/TUI config. Both copy
the session id verbatim; neither consults a model.

| Bridge | Trigger | Wire method | Identity source |
|---|---|---|---|
| `<config>/opencode/plugins/herdr-agent-state.js` (server, v13) | OpenCode events, only when `ownsLocalLifecycle()` is true | `pane.report_agent_session`, `pane.report_agent` | `event.properties.sessionID` (copied, never transformed) |
| `<config>/opencode/herdr-tui-session.js` (TUI, v13) | selected TUI route | `pane.report_agent_session` (`session_start_source: "select"`) | `api.route.current.params.sessionID` (the `--session` id) |

`ownsLocalLifecycle()` in the server plugin v13 returns true only for
`opencode run …` or a launch containing `--mini`; it returns false for `serve`,
`web`, `attach`, and for a bare interactive
`opencode --standalone <directory>`. The bare-TUI launch therefore depends on the
**TUI** selection bridge, not the server plugin.

## 3. How the installed TUI bridge is located and registered

The preparation tool performs this discovery **without printing or copying user
config**:

1. Read `herdr integration status`; the server plugin path anchors the OpenCode
   config root (`…/plugins/herdr-agent-state.js` → `…/opencode`).
2. Locate the sibling `herdr-tui-session.js`; require a regular (non-symlink)
   file with exactly `// HERDR_INTEGRATION_ID=opencode-tui` and
   `// HERDR_INTEGRATION_VERSION=13` (or newer), plus the required report tokens.
3. Parse only the TUI config (`tui.jsonc` / `tui.json`) in memory, keep local
   `./`/`../` `plugin` specifiers, and accept either a bare-file spec equal to
   `./herdr-tui-session.js` or a directory plugin (`tui.js`/`index.js`/`index.mjs`)
   whose entrypoint references the verified bridge by exact filename plus an
   `export`/`require` token marker. Reject escapes, absolute paths and symlinks.
   The directory case is a textual trust-on-first-use check, not a byte-identity
   or cryptographic verification: any module containing the bridge filename and
   either token passes. Residual risk: a malicious or corrupted user-local
   entrypoint could satisfy the directory check; the tool parses registration
   config in memory only, does not copy general user config, and never executes
   it. This is the user's own config tree (outside this slice's threat model).
4. `cli.json` belongs to the server bridge and is **never** read as a TUI
   registration. OpenCode v2.4.1 loads TUI plugins from `tui.jsonc`/`tui.json`
   under the singular `plugin` key; the supported isolated form is a bare-file
   spec (`./herdr-tui-session.js`).
5. Anchor `--bridge-src` to the exact server plugin path reported current by
   `herdr integration status`. A different path, a symlinked override, or a
   symlinked reported path is rejected before any write.

Missing, unregistered, symlinked, escaped, malformed, version-mismatched, or
mis-anchored bridges abort **before any filesystem write**.

## 4. Isolated provisioning (exact result)

The prepared XDG contains only:

```text
<run>/pane-prep/<role>/xdg-config/opencode/
├── herdr-tui-session.js
├── tui.jsonc
└── plugins/
    └── herdr-agent-state.js
```

`tui.jsonc` is the minimal isolated TUI registration:

```json
{
  "plugin": ["./herdr-tui-session.js"]
}
```

`cli.json` is intentionally absent from the isolated tree; it belongs to the
server bridge and is never treated as a TUI registration. No `opencode.json`,
`tui.json`, general user config, credential, or key value is copied into the
isolated tree. Existing symlinked targets and
symlink escapes are rejected before the copy/write.

A pre-existing isolated `opencode.json` is preflighted **before creating any
sandbox directory or copying either bridge**. The refusal is a `PrepError`; the
CLI prints `{"ok": false, "error": ...}` and exits `2`, leaving the pre-existing
tree unchanged and no partial bridge files behind. The `dispatch_example` shell
preview quotes the run root, so paths containing spaces stay a single argument.

## 5. Wire evidence — disposable, no prompt

Method (`tests/test_m606_pane_launch_contract.py::LiveBridgeWireTests`):

1. prepare a disposable world with the **real installed** server/TUI bridge and
   real registration discovery;
2. run `opencode plugin list --standalone` with only the isolated `HOME`/XDG
   environment; it lists the isolated `herdr-tui-session.js` registration
   (proving OpenCode recognizes it);
3. import the **copied** `herdr-tui-session.js` with Node, inject a fake TUI API
   with a synthetic `ses_` id, and capture every JSON line written to a disposable
   Unix socket.

No OpenCode prompt, model call, real pane, or live socket is involved.

### TUI selection bridge — preregistered id verbatim

```json
{
  "id": "herdr:opencode:tui:<clock>:<rand>",
  "method": "pane.report_agent_session",
  "params": {
    "pane_id": "m606probe:p1",
    "source": "herdr:opencode",
    "agent": "opencode",
    "agent_session_id": "ses_panecontract00000000000000001",
    "session_start_source": "select"
  }
}
```

Repeated reports while the route settled all carried the same id. No frame
contained `model`, `provider`, `omlx`, or `Qwen`.

### Server bridge — same id from event payloads, but not for a bare TUI

With argv `opencode run hello` and synthetic `session.updated` /
`session.status` / `chat.message` events:

```json
{"method":"pane.report_agent_session","params":{"pane_id":"m606probe:p1","source":"herdr:opencode","agent":"opencode","seq":<n>,"agent_session_id":"ses_panecontract00000000000000001"}}
{"method":"pane.report_agent","params":{...,"state":"idle","agent_session_id":"ses_panecontract00000000000000001"}}
```

With `opencode --standalone <dir>` the server plugin returned no hooks and emitted
zero frames. The TUI selection bridge is what carries the prepared bare-TUI id.

## 6. What is proven — and what is not

**Proven in this slice**

* the installed TUI bridge is located and verified from managed markers and its
  local registration, without copying or printing user config;
* `--bridge-src` is accepted only when it is the exact non-symlink server plugin
  path currently reported by `herdr integration status`;
* missing/unregistered/unverified/symlinked/escaping/mis-anchored bridges fail
  closed before any write;
* an existing isolated `opencode.json` fails closed before any directory, copy, or
  `prep.json` write; the CLI returns a clean JSON error (exit `2`);
* the isolated tree has exactly the four expected files and no general config;
* OpenCode v2.4.1 recognizes the isolated `./herdr-tui-session.js` (`tui.jsonc`)
  registration; the earlier v2.4.0 `./herdr-opencode` directory form is
  superseded (2026-11-26 correction).
* the **copied** bridge emits the preregistered synthetic id verbatim to a
  disposable socket, with no model/provider data.

**Not proven here**

* that a real interactive pane loaded this isolated config and Herdr exposed the
  id (requires an operator-owned live pane);
* acknowledgement, event-stream, or dispatch success;
* any end-to-end model or delivery result.

The runner must continue to reject any pane without a live native `ses_` id.

## 7. Test evidence (executed locally)

```text
$ .venv/bin/python -m pytest tests/test_m606_prepare_opencode_pane.py -q
39 passed, 2 subtests passed

$ .venv/bin/python -m pytest tests/test_m606_pane_launch_contract.py -q
7 passed, 3 skipped

$ .venv/bin/python -m pytest \
    tests/test_m606_pane_launch_contract.py \
    tests/test_m606_prepare_opencode_pane.py \
    tests/test_m606_session_registration.py -q
57 passed, 4 skipped, 2 subtests passed

$ M606_LIVE_BRIDGE=1 .venv/bin/python -m pytest \
    tests/test_m606_pane_launch_contract.py -q
10 passed
```

The offline suite covers exact isolated files, minimal registration parsing,
missing/unregistered/unverified bridge refusal, symlink/escape guards,
general-config refusal, rerun safety, and the launch command. The opt-in suite
adds installed-bridge discovery, `opencode plugin list` recognition, and the
disposable-socket wire frames.

## 8. Non-claims and constraints

* No OpenCode pane was started, read, or mutated; no prompt was sent.
* No model/inference endpoint was called; no credential store or credential value
  is read, printed, or copied by this slice.
* The synthetic id `ses_panecontract00000000000000001` is not a real session.
* The live `teamcraft` DB, `.teamcraft/` state, and pre-existing dirty files were
  not touched.
* This DeepSeek pane was not used as smoke evidence.
* Nothing was committed or pushed; `HEAD` **remained** `753b096` at record time.
  **Provenance correction (2026-11-26):** current re-verified `HEAD` is `f189ba7`
  (6 commits later) and the installed `opencode` is **v2.4.1**, not v2.4.0. This
  slice still performed no commit or push; the correction only records the
  provenance drift since the snapshot.
