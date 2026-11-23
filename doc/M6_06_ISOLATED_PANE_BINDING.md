# M6-06 — Isolated OpenCode Pane ↔ Project‑Local Loopback Binding

Question: can an isolated OpenCode pane for the retained real‑probe phase‑1 world
be bound to the **project‑local loopback** model (`omlx` @ `127.0.0.1:8000/v1`,
`Qwen3.8-Flash-Next-oQ4e-mtp`) **without inheriting live‑repo or cloud defaults**?

Determination: **Yes — mechanically bindable, but the guarantee is
operator‑attested, not host‑trusted.** Isolation is achieved by cwd placement +
project config + an explicit model pin + environment hygiene; OpenCode v2.2.6 has
**no single "disable global config" switch**, so the pane's *effective* model
cannot be machine‑proven by the runner — it is proven by operator attestation with
provenance, backed by the loopback catalog and capability binding.

Status: read‑only analysis only. No pane opened, no prompt/dispatch, no model call,
no credential read, no attestation inferred from catalog, live DB untouched, no
commit. Existing handoffs referenced only by `run_id`.

## 0. Evidence basis (read‑only)

| Source | Fact relied on |
| --- | --- |
| Committed runner `check_omlx` / `capture_and_bind_capabilities` | provider must be `omlx` @ `127.0.0.1`/`::1`, healthy, model in catalog; **`cloud_fallback: not-permitted`** |
| Committed runner `pane_model_proof` | attestation `model` **must equal** the pinned model; `pane_id`/`terminal_id`/`session_id`/`agent` must match the live identity; non‑empty `provenance` required; result `operator-attested`, **`host_trusted: false`**; catalog/terminal screen are **supplements, never a substitute** |
| Committed runner `gate_dispatch_target` / `gate_ceo_target` | worker pane cwd **== allocated worktree**; CEO pane cwd **== project root**; idle + stable native session id |
| Committed `opencode.example.json` | project‑local provider `omlx` = `@ai-sdk/openai-compatible`, `baseURL http://127.0.0.1:8000/v1`, top‑level `model: "omlx/Qwen3.8-Flash-Next-oQ4e-mtp"` |
| Installed `opencode --help` (v2.2.6) | positional `<directory>` ("Directory to start OpenCode in"); `--standalone` (private server, not the shared background service); `run --model <provider/model#variant>` explicit pin; no `--config`/global‑disable flag exposed |
| Committed host‑gate record | user config `~/.config/opencode/opencode.json` exists and already declares the `omlx` loopback provider (`baseURL 127.0.0.1:8000`); catalog healthy |

## 1. What could leak, and the lever that neutralizes each

| Inherited default | Risk | Neutralizing lever |
| --- | --- | --- |
| Live **repo** config (`opencode.json`, `AGENTS.md`, `.opencode/` of the TeamCraft repo) | pane picks the real repo's providers/rules | **cwd placement**: start OpenCode with the disposable world directory as its positional `<directory>` argument; its parent chain is the temp tree, not the TeamCraft repo, so repo‑local config is out of scope |
| **Global** user config `~/.config/opencode/opencode.json` | a cloud default `model`/provider, or extra providers | project `opencode.json` (loopback) **overrides** the default `model`; use a fresh isolated HOME and XDG configuration directory to hide global defaults |
| Cloud **credentials** in the OpenCode auth store (`auth export` prints secrets — never run it) | a cloud route is reachable | never select a cloud provider; the pinned `omlx` model needs **no** key; unset cloud env (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`, `GOOGLE_API_KEY`) so no cloud provider is configured |
| Shared **background service** | stale session/provider state from other projects | `--standalone` (private server) + a fresh `--session <new>` id |
| Catalog ≠ pane selection | trusting that "the server lists the model ⇒ the pane uses it" | forbidden by design: prove the pane's own selected model via operator attestation + provenance (in‑pane `/models`), never catalog alone |

## 2. Project‑local config (place in BOTH targets)

Write this exact file as `opencode.json` at the **project root** and the
**allocated worktree** of the phase‑1 world (never overwrite the user's global
config; the template is copied into the scenario target only):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "omlx/Qwen3.8-Flash-Next-oQ4e-mtp",
  "provider": {
    "omlx": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local oMLX",
      "options": { "baseURL": "http://127.0.0.1:8000/v1" },
      "models": {
        "Qwen3.8-Flash-Next-oQ4e-mtp": { "name": "Qwen3.8-Flash-Next-oQ4e-mtp" }
      }
    }
  }
}
```

## 3. Launch / cwd / attestation procedure

Set each directory variable to the corresponding handoff path. Run from
**inside** the pane dedicated to the role. The interactive command does not
accept `--model`; the project config selects the model. Do **not** reuse
the shared background service; do **not** send any prompt during binding (the pane
must stay **idle** — dispatch happens later, only under `--consent-dispatch`).

**Worker pane** — cwd = allocated worktree:
```
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_BASE_URL -u GOOGLE_API_KEY \
    opencode --standalone "$ALLOCATED_WORKTREE_PATH"
```
**CEO pane** — cwd = project root, in a separate pane:
```
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_BASE_URL -u GOOGLE_API_KEY \
    opencode --standalone "$PROJECT_ROOT_PATH"
```
Inside the pane, confirm the selected model is **`omlx/Qwen3.8-Flash-Next-oQ4e-mtp`**
and the provider baseURL is **`http://127.0.0.1:8000/v1`**. Leave it idle; do not
message it.

Note the pane's live identity from Herdr (operator‑local, never pasted into a
shared doc): `pane_id`, `terminal_id`, native `session_id`, `agent`.

### Attestation file (operator writes, feeds to `--pane-model-evidence`)

`model` **must** equal the pinned model exactly; `pane_id`/`terminal_id`/
`session_id`/`agent` must equal the live pane identity; `provenance` must state how
the operator personally confirmed the pane's selection (not copied from a catalog):

```json
{
  "model": "Qwen3.8-Flash-Next-oQ4e-mtp",
  "pane_id": "<live-pane-id>",
  "terminal_id": "<live-terminal-id>",
  "session_id": "<live-native-session-id>",
  "agent": "opencode",
  "provenance": "read from the pane's own /models selection; provider baseURL 127.0.0.1:8000/v1"
}
```
The runner records this as `operator-attested` / `host_trusted:false`. `--post-dispatch`
is **not** used for binding; it is only for continuing an already‑dispatched
attempt.

## 4. CEO checklist

- [ ] Phase‑1 world retained: `run_id c93283fd68dd19de` (exact path held separately).
- [ ] Loopback route healthy and pinned model in catalog (already bound:
      `provider_digest 306f85c1…`, `cloud_fallback not-permitted`).
- [ ] `opencode.json` (loopback) present in **project root** and **allocated
      worktree**; global user config **not** modified.
- [ ] A dedicated, **idle** OpenCode pane designated for CEO (cwd = project root)
      **distinct** from the worker pane.
- [ ] CEO attestation file authored (model == pinned, identity match, provenance),
      fed to `--ceo-pane` leg's `--pane-model-evidence`; never host‑trusted.
- [ ] Cloud env unset; no cloud provider selected; `auth export` never run.
- [ ] Acknowledgement understood as **unasserted** (`prompt_ack`/`event_stream`
      unsupported on herdr 0.9.3 → `assumed_default`).

## 5. Worker checklist

- [ ] `opencode.json` (loopback) present in the allocated worktree cwd.
- [ ] A dedicated, **idle** OpenCode pane whose cwd == the allocated worktree
      exactly, launched `--standalone` with a fresh session id.
- [ ] In‑pane model verified as `omlx/Qwen3.8-Flash-Next-oQ4e-mtp` (not cloud).
- [ ] Worker attestation file authored (model == pinned; pane/terminal/session/agent
      match live identity; non‑empty provenance); never inferred from catalog.
- [ ] Cloud env unset in the pane process; no cloud credentials reachable.
- [ ] Pane left idle until dispatch; no self‑prompt, no push, work only in the
      allocated worktree.

## 6. What the runner re‑proves at dispatch (and what it cannot)

Re‑proves: loopback oMLX healthy + pinned model + `cloud_fallback not-permitted`;
provider/harness snapshots durably bound before bind; pane idle + native session id
stable + cwd == required dir; attestation model == pinned and identity‑matched.
Cannot prove (by design): that the pane's runtime didn't inherit some cloud route —
`host_trusted:false`. Hence steps §1/§3 (project pin + isolated configuration + env
hygiene + attestation) are **mandatory**, not optional convenience.

## 7. Blockers

1. **No global‑config disable switch** in opencode v2.2.6 → isolation from
   `~/.config/opencode/opencode.json` depends on project‑config precedence +
   isolated configuration and env/HOME hygiene; confirm the effective model in‑pane at
   run start (no catalog shortcut).
2. **Only one idle OpenCode pane** observed (one idle pane and one working pane) → worker
   binding is feasible now; **CEO + worker simultaneously** need a second idle
   OpenCode pane.
3. **Attestation is operator‑attested, host‑untrusted** — a mismatched or catalog‑
   inferred attestation will (correctly) fail closed; the operator must read the
   pane's own selection.
4. **Acknowledgement unassertable** on herdr 0.9.3 (`prompt_ack`/`event_stream`
   unsupported) → receipts stay `assumed_default`; never upgrade to host‑verified.
5. Credential hygiene is manual: cloud keys must be unset in the pane env; `opencode
   auth export` prints secrets and must never be run.

## 8. Constraints honored

Read‑only inspection only — no panes opened, no dispatch, no model call, no
attestation inferred from catalog; credentials not read or printed; existing handoffs
referenced by `run_id` only (exact path kept separately); live `teamcraft` DB
untouched; no commit or push.
