# M6-06 — Local Host-Gate Results (oMLX · Herdr · executables/protocol)

| Field | Value |
|---|---|
| Status | **Host gates probed read-only on the real host. G2 GREEN, G3 GREEN (identity), G1 NOT released.** No agent spawned, no model invoked, no credential read, no live-DB access, no topology change, no commit/push. |
| Roadmap item | M6-06 — host-gate slice between the scripted fixture (`9987713`) and the consented real-host run |
| Drafted from | `main` @ `9987713` ("Exercise M6-06 scripted local delivery in disposable fixtures"), 2026-11-22 |
| Inputs | `doc/M6_06_LOCAL_E2E_PREFLIGHT.md` PRE-1…PRE-8, gates G2/G3; `doc/M6_06_HARNESS_READINESS.md` PG-1/PG-7/PG-8/PG-9 |
| Owned surface | **This file only.** |
| Probes used | `teamcraft.providers.probe_omlx`, `teamcraft.ceo_protocol.verify_protocol`, `scripts/install.sh --dry-run`, `git`, `herdr status/api/tab list/session list` |

Scope honesty: this document closes the *environment* half of G2 and G3. It
deliberately does **not** close PG-7. Nothing here marks Codex/OpenCode
init/dispatch/Wake as REAL — `prompt_ack`, `event_stream` and host-trusted
receipt stay `unknown`/`false` (§3.4, §7-HG-F).

---

## 1. HG-1 — oMLX loopback health + model catalog (releases G2)

Executed through the sanctioned API only: `providers.probe_omlx()`. Its
`_loopback_models_url()` requires scheme `http`, a **literal** loopback IP, and
rejects userinfo/query/fragment; `_default_http_get` installs an empty
`ProxyHandler`, so the probe cannot be redirected through an ambient proxy and
carries no auth header.

### 1.1 Service presence

| Check | Result |
|---|---|
| Listener on loopback | `python3.1 127.0.0.1:8000` (LISTEN) — the oMLX server |
| `GET /v1/models` | **HTTP 200** |
| `GET /models` | HTTP 404 |
| `GET /health` | **no endpoint** (no response) |
| `probe_omlx("http://127.0.0.1:8000/v1")` | `health=healthy`, `reason_code=ok`, `source=http://127.0.0.1:8000/v1/models` |
| `probe_omlx("http://127.0.0.1:8000")` | identical — path is normalised to `/v1/models` |
| `snapshot.version` | `None` (server omits a version field; not a failure) |
| `discovery` | `{"model_catalog": "supported"}` |
| `capabilities` | `inference_request=supported`, `event_stream=unsupported`, `prompt_ack=unsupported` |

**Design confirmation (H-3):** oMLX exposes **no** `/health` route. `/v1/models`
*is* the health signal, which is exactly what `probe_omlx` assumes. A future
run must not add a `/health` dependency.

### 1.2 Local model catalog

```
Qwen3.8-Flash-Next-4bit-mtp
Qwen3.8-Flash-Next-oQ4e-mtp
```

2 models, ids deduped+sorted by the probe.

### 1.3 Route declaration vs catalog (G2 closure)

Performed **structurally** — counts of patterns inside user config; file contents
were never printed and no key value was read.

| Harness | Config | Loopback-route refs | Declares catalog model `…oQ4e-mtp` | Verdict |
|---|---|---|---|---|
| OpenCode | `~/.config/opencode/opencode.json` (exists) | **1** (`baseURL` → `127.0.0.1:8000`, `npm`, `options`) | **3 refs — yes** | **local route configured + model present in catalog ⇒ G2 CLOSED for the OpenCode leg** |
| Codex | `~/.codex/config.toml` (exists) | **0** (`model_provider` 0, `base_url` 0, `127.0.0.1` 0, `localhost` 0) | n/a | **no local route declared ⇒ Codex model leg is cloud-dependent ⇒ gated by G6, not G2** |

Catalog also carries `Qwen3.8-Flash-Next-4bit-mtp`, which OpenCode does **not**
declare (0 refs) — harmless, but it means "in catalog" ≠ "routed"; the run must
assert the *declared* id is present (HG-B below), not merely that the catalog is
non-empty.

### 1.4 Negative guards (no request issued — validation precedes I/O)

| URL | Result |
|---|---|
| `http://localhost:8000/v1` | `ValidationError` — hostname alias rejected; trust must not depend on mutable `/etc/hosts` |
| `https://127.0.0.1:8000/v1` | `ValidationError` — non-`http` scheme |
| `http://192.168.1.5:8000/v1` | `ValidationError` — non-loopback |
| `http://user@127.0.0.1:8000/v1` | `ValidationError` — userinfo (credential-bearing) rejected |

All four refused **before** any socket was opened. This is the property that
makes the probe safe to wire into an automated run.

---

## 2. HG-2 — Herdr tab identity + protocol (G3, topology unchanged)

Read-only subcommands only: `herdr status`, `herdr session list`, `herdr tab
list`, `herdr api snapshot`, `herdr api schema`. No `create` / `close` /
`focus` / `split` / `agent` command was run.

### 2.1 Client/server/protocol

| Field | Value |
|---|---|
| client version / channel | `0.9.3` / `stable` |
| client protocol | `22`, `endpoint_protocol_generation: 1` |
| server status / version | `running` / `0.9.3` |
| `endpoint_compatible` | **yes** |
| `private_protocol` / compatible | `22` / **yes** |
| socket | `~/.config/herdr/herdr.sock` |
| `restart_needed` / `server_binary_stale` | `no` / `no` |
| `herdr api schema` | `protocol: 22`, `schema_version: 1` |

⇒ The **0.9.3 session-contract precondition of G3 holds**: client and server are
the same version and mutually protocol-compatible, with no pending restart. This
removes the preflight caveat "session-contract behavior under this version
unverified on host" at the *transport* level.

### 2.2 Sessions, tabs, panes (current topology — observed, not modified)

```
session    default      running   dir ~/.config/herdr
workspace  w3           label "teamcraft"
tab        w3:t1        label "1"  number 1  pane_count 2  focused true
pane       w3:p4        focused false  cwd <project-root>
pane       w3:p2Q       focused true   cwd <project-root>
```

Exactly **1 workspace / 1 tab / 2 panes**.

### 2.3 Agent identity binding (the actual G3 evidence)

`herdr api snapshot` → `result.snapshot.agents[]`, 2 records:

| Pane | `agent` | `name` | `agent_status` | `agent_session.kind` | `agent_session.source` | `agent_session.value` | `terminal_id` |
|---|---|---|---|---|---|---|---|
| `w3:p4` | `codex` | — | `idle` | `id` | `herdr:codex` | `[redacted]` | `[redacted]` |
| `w3:p2Q` | `opencode` | `dev_e2e_m606` | `working` | `id` | `herdr:opencode` | `[redacted]` | `[redacted]` |

Both agents report `foreground_cwd = <project-root>`. Available
identity fields: `agent`, `agent_session{agent,kind,source,value}`,
`agent_status`, `cwd`, `foreground_cwd`, `focused`, `interactive_ready`, `name`,
`pane_id`, `tab_id`, `workspace_id`, `terminal_id`, `terminal_title`,
`terminal_title_stripped`, `revision`, `state_change_seq`, `completion_seq`,
`screen_detection_skipped`.

**What this proves:** a submit target is addressable as a *quadruple*
`(workspace_id, tab_id, pane_id, terminal_id)` and herdr already resolves a
native session id per agent (`kind=id`, `source=herdr:<agent>`). That is the
exact binding shape M2-08 wants for post-submit identity, so S2's
"session identity hash" artifact is obtainable **without** any new herdr
capability.

**What this does NOT prove:** a `session.value` is an *observation of the
harness's own session*, not an acknowledgement that a prompt was accepted.
`interactive_ready` is present only on the `opencode` record (absent on
`codex`), and `screen_detection_skipped=true` on it. Therefore `prompt_ack` and
`event_stream` stay `unknown`, and **no host-trusted receipt** is claimed.

### 2.4 Non-mutation proof

Re-listed after all inspection: tabs identical
(`[('w3:t1', 2, True, 'working')]`) and the 2 agent records compare equal across
inspections after excluding volatile counters (`revision`,
`state_change_seq`, `completion_seq`, `interactive_ready`,
`screen_detection_skipped`). **Topology and identity unchanged by this slice.**

---

## 3. HG-3 — Executables and protocol integrity

### 3.1 PRE-1 executable inventory (re-observed today)

| Tool | Path | Version | Note |
|---|---|---|---|
| `codex` | `/opt/homebrew/bin/codex` | **codex-cli 0.170.0** | drift vs §12.11's 0.166.0 → skill-load must be re-verified at run start (R4) |
| `opencode` | `/opt/homebrew/bin/opencode` | **v2.2.6** | drift vs the v2.0.29 `provider.no-route` failure; §1.3 shows a loopback route is now declared |
| `herdr` | `/opt/homebrew/bin/herdr` | **0.9.3** | protocol 22, compatible (§2.1) |
| `claude` | — | **absent** | PG-1 unchanged ⇒ every matrix keeps Claude `pending:PG-1` |
| Python | project venv | 3.13.x | floor ≥3.10 |

### 3.2 PRE-2 canonical protocol

```
protocol_version : 1.0.0
sha256           : c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742
verify_protocol(..., expected_version="1.0.0", expected_sha256=c5c358bf…742) → PASS
```

### 3.3 PRE-3 discovered-copy census — **21/21 MATCH, 0 drift**

`sha256` of every `CEO_PROTOCOL.md` found under the repo (incl. all
`.teamcraft/worktrees/**`), plus the external OpenCode worktree:

| Location group | Copies | Result |
|---|---|---|
| canonical `.agents/skills/teamcraft-ceo/references/` | 1 | MATCH |
| `.teamcraft/m503-verify-09a8936/…` | 1 | MATCH |
| `.teamcraft/worktrees/<uuid>/…` (top-level) | 16 | MATCH |
| nested `…/e055930b…/.teamcraft/worktrees/<uuid>/…` | 2 | MATCH |
| `~/.local/share/opencode/worktree/cab59e/m306-race-repro/…` | 1 | MATCH |
| **total** | **21** | **21 MATCH / 0 DRIFT** |

**H-6 (new):** **zero** copies exist under `~/.codex/` or `~/.config/opencode/`.
The CEO skill has never been installed at user scope on this host, so a real run
**must** install into a disposable target (S1) and cannot rely on user-scope
skill discovery.

### 3.4 PRE-4 installer dry-run into a disposable target

`bash scripts/install.sh --target <mktemp -d>/m606-hostgate-XXXXXX --dry-run`
(with `.venv/bin` prepended to `PATH`):

| Observation | Value |
|---|---|
| exit code | `0` |
| files created in target | **0** (before 0 → after 0) |
| planned actions | **52 × "would create"** (`.agents/skills/teamcraft/**`, `teamcraft-ceo/**` incl. `CEO_PROTOCOL.md` + `protocol.manifest.json`) |
| conflict/claude guard lines | none (fresh target) |

Zero-write contract holds; `--target`/`--dry-run` confirmed functional
(PREFLIGHT PRE-4 → **executed green**).

---

## 4. PRE-5 — offline contract green baseline (executed today)

| Suite | Result |
|---|---|
| `test_providers_m501` `test_router_m502` `test_ceo_protocol_m108` `test_ceo_initialization_m208` `test_wake_m209` | **84 passed, 12 subtests passed**, 1 unrelated Starlette/httpx deprecation warning |
| `test_local_e2e_m606` (the S0/S1/S4/S5 scripted fixture @ `9987713`) | **23 passed** |

PRE-5 → **executed green**. The scripted path that S2–S5 will reuse is proven
end-to-end offline: path guard, fresh DB at head, installer+protocol pin,
verdict-from-repo-facts, Diff Gate, local-only delivery, CLEANABLE+cleanup,
UNKNOWN refusal.

---

## 5. PRE-8 — blocker census snapshot (this host, this moment)

| # | Observation | Value | Consequence |
|---|---|---|---|
| B1 | Uncommitted managed files | **15** porcelain lines — 11 modified tracked (incl. `.agents/skills/teamcraft/scripts/teamctl.py`, `.opencode/agents/herdr-tester.md`, `teamcraft/{delivery,domain,recovery,runtime_adapter,worktrees}.py`, 5 tests) + 4 untracked (`.codex/`, 3 new tests) | installer against the live repo would hit managed-conflict discipline ⇒ **install only into a disposable target** |
| B2 | Existing git worktrees | **20** | untouched; scenario uses its own tmp root |
| B3 | Live runtime DB | `.teamcraft/teamcraft.db` present, **913 408 bytes**; `.teamcraft/` has **117** entries | hard rule: run DB is created in the run root by `storage.migrate_to_head`; path guard aborts if the root resolves inside this repo |
| B4 | `opencode.json` in repo root | present | never replaced; target gets a template only |
| B5 | OpenCode model route | **healthy local route** (§1.3) | preflight's `provider.no-route` caveat is **superseded** on this host |
| **H-1 (new)** | **Stale disposable roots** | **232** `m606-e2e-*` dirs under `$TMPDIR`, **621 MB**; re-counted **255** after exactly one `test_local_e2e_m606.py` run ⇒ **+23 = one leaked root per test** | the scripted fixture closes its DB (`addCleanup(self.run.close)`) but never `rmtree`s its run root ⇒ **23 roots per suite run, unbounded**. Reclaim before the consented run (HG-A); the fixture needs a root-removal cleanup |
| B3-proof | Live DB untouched by this slice | size **913 408 bytes identical** at census and at close-out, **no `-wal` file**; only `ls`/`stat` touched it | the "never open the live DB" rule is verifiable, not just asserted |

`HEAD = 9987713`, `main` is **17 ahead / 0 behind** `origin/main`.
NOTE: `tests/test_local_e2e_m606.py` is now **committed** as `9987713`; this
slice did not commit anything.

---

## 6. Gate verdicts

| Gate | Position after this slice | Basis |
|---|---|---|
| **G2** — OpenCode model-driven legs | **CLOSED (host-local)** | loopback `healthy`/`ok`; catalog non-empty; declared model `Qwen3.8-Flash-Next-oQ4e-mtp` present in catalog; probe refuses non-loopable/auth'd URLs. Re-run HG-B at run start (TTL-bound). |
| **G3** — herdr dispatch evidence quality | **CLOSED at identity/transport level** | 0.9.3 client==server, protocol 22 compatible both ways, no restart pending; 1 tab / 2 panes enumerated; per-agent native session ids + pane/terminal ids available; identity stable across inspection. Does **not** cover submit/ack (G1). |
| **G1** — any harness-agent spawn | **NOT RELEASED** | No consent obtained in this slice; nothing spawned. Also review G4 before running. |
| G4 — disk hygiene | **CONDITION FAILS TODAY** | H-1: 232 leaked roots / 621 MB. Reclaim (HG-A) before the run. |
| G5 — Claude | **still pending** | PG-1: binary absent. |
| G6 — cloud | **out of scope / binding on Codex** | Codex declares no local route (§1.3) ⇒ a Codex *model-driven* leg needs budget approval; a Codex leg without a local route cannot count as local E2E evidence. |

---

## 7. Reproducible disposable real-host run procedure

Copy-pasteable. Everything lands under `$RUNROOT`; **$RUNROOT must be outside
this repo** (HG-0 enforces it structurally, not by promise). Steps HG-A…HG-E are
safe **without** consent; HG-F is the consent checkpoint; HG-G/H are close-out.

```sh
REPO=$(git rev-parse --show-toplevel)
TMP=${TMPDIR:-/tmp}
```

**HG-A — hygiene + isolation precondition (abort-first).**
```sh
# reclaim leaked roots from earlier fixture runs (H-1); nothing outside this glob
du -shc "$TMP"/m606-e2e-* 2>/dev/null | tail -1
# Review and remove only individually verified fixture directories, if needed.
RUNROOT=$(mktemp -d "$TMP/m606-run-XXXXXXXX"); echo "$RUNROOT"
case "$(cd "$RUNROOT" && pwd -P)" in "$REPO"|"$REPO"/*) echo GUARD; exit 1;; esac
cd "$REPO" && git rev-parse HEAD && test -z "$(git status --porcelain)" || \
  echo "NOTE: dirty tree (B1) — install must not target $REPO"
```
*Evidence:* `runroot.txt` with the resolved path + `outside_protected=true`.
*Abort:* guard branch taken.

**HG-B — G2 start check (loopback only, no model call, no key).**
```sh
cd "$REPO" && .venv/bin/python - "$RUNROOT" <<'PY'
import json,sys,pathlib
from teamcraft import providers
s=providers.probe_omlx("http://127.0.0.1:8000/v1")
want="Qwen3.8-Flash-Next-oQ4e-mtp"
out={"health":s.health,"reason_code":s.reason_code,"source":s.source,
     "models":list(s.models),"declared_model_present":want in s.models}
pathlib.Path(sys.argv[1],"g2-omlx.json").write_text(json.dumps(out,indent=2)+"\n")
print(out); assert s.health=="healthy" and out["declared_model_present"], "G2 not met"
PY
```
*Evidence:* `g2-omlx.json`. *Abort:* `health != healthy` or declared id absent ⇒
do **not** start the model-driven leg; record `G2-fail`, never fall back to
cloud.

**HG-C — G3 start check (read-only herdr; forbidden to mutate).**
```sh
herdr status > "$RUNROOT/g3-status.txt"
herdr session list > "$RUNROOT/g3-sessions.json"
herdr api snapshot > "$RUNROOT/g3-snapshot-before.json"
```
Assert `private_protocol_compatible: yes`, `restart_needed: no`, and record the
chosen pane's `(workspace_id,tab_id,pane_id,terminal_id,agent,agent_session.value)`.
*Evidence:* the three files + `g3-target-pane.json`.
*Abort:* version skew, `endpoint_compatible: no`, or no `agent_session.value`.
**Allowed verbs:** `status`, `session list`, `tab list`, `api snapshot|schema`.
**Forbidden here:** `tab create|close|focus`, `pane`, `agent`, `session
attach|stop|delete` — topology is set by the operator, never by the run.

**HG-D — S0/S1 scaffolding (disposable).**
```sh
git init -q --bare "$RUNROOT/origin.git"
git clone -q "$RUNROOT/origin.git" "$RUNROOT/project"
PATH="$REPO/.venv/bin:$PATH" bash "$REPO/scripts/install.sh" \
     --target "$RUNROOT/project" --dry-run | tee "$RUNROOT/install-dry.txt"
PATH="$REPO/.venv/bin:$PATH" bash "$REPO/scripts/install.sh" \
     --target "$RUNROOT/project"          | tee "$RUNROOT/install.txt"
```
Then in the run-local DB: `storage.migrate_to_head(db, allow_unverified_location=True)`,
create project + CEO Attempt pinning `protocol_version=1.0.0` +
`protocol_hash=c5c358bf…742`, and re-hash every copy under `$RUNROOT`
against the canonical hash.
*Pass:* dry-run creates 0 files; real run ≥52 changes; every copy MATCH;
`verify_protocol` in target returns the pinned pair; DB `fresh=True, 0→17`.
*Abort:* managed conflict, or any hash mismatch ⇒ `protocol-drift`.

**HG-E — PRE-5 green baseline at run start.**
```sh
cd "$REPO" && .venv/bin/python -m pytest tests/test_providers_m501.py \
  tests/test_router_m502.py tests/test_ceo_protocol_m108.py \
  tests/test_ceo_initialization_m208.py tests/test_wake_m209.py \
  tests/test_local_e2e_m606.py -q
```
*Abort:* any failure.

**HG-F — ⛔ CONSENT CHECKPOINT (G1).** Stop and obtain explicit operator consent
to spawn exactly one bounded worker leg in an existing pane. Until that answer,
S2/S3 stay `pending:G1`. Then, per leg:
1. submit **one** read-mostly prompt (e.g. "summarize README structure, no
   edits") to the HG-C-identified pane;
2. bind identity from `herdr api snapshot` **after** submit (the quadruple +
   `agent_session.value`) and store it with the attempt row;
3. enqueue exactly one `ceo.wake`, expect one `wake-dispatch`, and record
   `pending→sending→submitted|unknown`;
4. consume **the same** protocol hash on both legs (R4).
*Abort/UNKNOWN:* lost submit, duplicate send, or `sending` past timeout ⇒
classify **UNKNOWN**, quarantine, halt that leg, **never auto-retry**; keep the
worktree for inspection (the UNKNOWN refusal path is already proven green by the
fixture).

**HG-G — S4/S5 on the real leg.** Verifier profiles compute the verdict from
repo facts in the run worktree; Diff Gate over the branch diff; local-only
delivery against the local bare origin; `mark_worktree_cleanable` →
`evaluate_cleanup_eligibility` → `cleanup_attempt_worktree` (never force).
*Abort:* Diff-Gate unowned violation stands as the negative-probe pass; origin
must gain **no** ref other than the seeded `refs/heads/main`.

**HG-H — close-out + hygiene (mandatory, fixes H-1 for future runs).**
```sh
cd "$REPO" && .venv/bin/python -m pytest tests/test_local_e2e_m606.py -q   # still green
du -shc "$TMP"/m606-run-* 2>/dev/null | tail -1
# after evidence is copied out of $RUNROOT and digest-verified:
rm -rf "${RUNROOT:?}"
git -C "$REPO" status --porcelain   # must show only the intended doc/test changes
```
*Pass criteria:* every claim in `M6_06_E2E_RUN.md` maps to an artifact with a
digest; `g2-omlx.json` shows `declared_model_present=true`; `g3-*` shows the
bound pane; zero residual `m606-*` roots afterwards.

---

## 8. What still blocks PG-7 after this slice

1. **G1 operator consent** — the real-agent checkpoint (§7 HG-F). Finish the other applicable prechecks before running.
2. **Prompt-ack / event-stream evidence** — structurally unavailable on 0.9.3
   (§2.3): `prompt_ack`/`event_stream` stay `unknown`; `assumed_default` must be
   recorded as such, never as a host-trusted receipt.
3. **Codex local route (H-2)** — `~/.codex/config.toml` declares no loopback
   provider, so a Codex leg is cloud-dependent (G6). Either accept a
   *OpenCode-only* local E2E, or gate the Codex leg behind G6 budget approval.
4. **Claude (PG-1)** — binary absent; `probe_harness("claude")` also stays
   `unsupported/adapter_pending` unless an injected runner + pinned compatible
   version exist.
5. **Disk hygiene (H-1)** — reclaim 232 roots / 621 MB via HG-A, and give the
   fixture a run-root `rmtree` cleanup so it stops leaking (+23 roots per suite
   run, measured) (owner: the `tests/test_local_e2e_m606.py` surface, **not**
   this doc).

### 7.1 Self-check of this slice's own footprint

| Claim | Evidence |
|---|---|
| Owned exactly one file | `git status --porcelain` went **15 → 16** lines; the single addition is `doc/M6_06_HOST_GATE_RESULTS.md` |
| No live-DB access | `.teamcraft/teamcraft.db` byte size identical across the slice, no `-wal`; accessed via `ls`/`stat` only |
| No credentials read | route checks used `grep -c` pattern counts; no config body, token or key value was ever printed |
| No topology change | §2.4 before/after tab+agent comparison equal |
| No model call | only `GET /v1/models` (catalog) and `--version`; no inference request was issued |
| No agent spawn / no commit | HG-F was reached and **stopped**; `git log` top is still `9987713` |

---

*Evidence pins (all executed read-only on this host, 2026-11-22, from
`main`@`9987713`): §1 `providers.probe_omlx` output + `lsof` loopback listener
`python3.1 127.0.0.1:8000` + `curl` status codes (`/v1/models` 200, `/models`
404, `/health` none) + 4 pre-I/O `ValidationError` guards; §2 `herdr status`,
`herdr session list`, `herdr tab list`, `herdr api snapshot`, `herdr api schema`
(protocol 22, schema_version 1), before/after identity comparison excluding
volatile counters; §3 `--version` outputs, 21-copy `shasum -a 256` census vs
`c5c358bf…742`, `install.sh --target <tmp> --dry-run` (exit 0, 0 files, 52
"would create"); §4 pytest totals 84 + 12 subtests and 23 passed; §5 `git status
--porcelain`, `git worktree list`, `.teamcraft` census, `du -shc
$TMP/m606-e2e-*`. Route-declaration checks (§1.3) counted pattern occurrences
only — no config file contents, tokens or keys were read or printed. No live DB
connection was opened; no agent was spawned; no model was invoked; no commit or
push was made.*

---

## 9. HG-F 可执行同意清单（本轮只整理，不执行）

> **状态声明（必读）：** 本节把 §7 的 HG-A…HG-H 整理成一条**可执行、可审计**的顺序
> 清单。本轮**未执行**任何标注「⛔ 需要操作者显式同意（G1）」的步骤；未向任何真实
> Agent pane 发送 Prompt；未 spawn、未调用模型、未读凭据、未 commit/push。因此本节
> **不代表 PG-7 已关闭**，也**不代表 G1 已释放**。HG-F 的同意必须在真实运行开始时
> 由操作者针对**具体 pane、具体 leg、具体范围**逐次给出，不能由本文档、本 Agent、
> 计划默认值或任何“上一轮已同意”推断代劳。

### 9.1 同意分类总表（顺序即执行顺序）

| # | 步骤 | 具体动作（摘要） | 分类 | 真实效应 |
|---|---|---|---|---|
| 1 | **HG-A** | 盘点/回收 `$TMPDIR/m606-e2e-*` 泄漏根；`mktemp -d` 建 `$RUNROOT`；repo 越界 guard；只读 `git rev-parse`/`status` | **安全、无需同意** | 仅本机 `$TMPDIR` 一次性目录；不触碰 repo、不碰 Agent |
| 2 | **HG-B** | `providers.probe_omlx()` 回环健康 + 断言声明模型在目录中 | **安全、无需同意** | 仅一次 `GET /v1/models`；无推理、无 key |
| 3 | **HG-C** | `herdr status` / `session list` / `api snapshot` 只读；绑定目标 pane 四元组 | **安全、无需同意** | 只读拓扑；不改 tab/pane/agent |
| 4 | **HG-D** | 一次性 bare origin + clone + `install.sh --dry-run` 后真装 + 迁移 run 内 DB + 全量 hash 校验 | **安全、无需同意** | 全部写入 `$RUNROOT`；repo 只读 |
| 5 | **HG-E** | 离线 pytest 基线（providers/router/protocol/init/wake/local_e2e） | **安全、无需同意** | 纯本地测试，不触网 |
| 6 | **HG-F** | **真实派工**：向已绑定 pane 提交 1 条 prompt、post-submit 绑定身份、1 次 `ceo.wake` | **⛔ 需要操作者显式同意（G1）** | **产生真实 Agent 行为**；提交不可撤销，只能 UNKNOWN 隔离 |
| 7 | **HG-G** | 在真实 leg 的 `$RUNROOT` worktree 上跑验证 / Diff Gate / 本地交付 / cleanup | **⛔ 需要操作者显式同意（G1）** | 承接 HG-F 的真实产物；commit 只进 `$RUNROOT` 的一次性 origin |
| 8 | **HG-H** | 收尾：fixture 复跑、证据摘要、`rm -rf "${RUNROOT:?}"`、repo 状态核对 | **安全、无需同意**（仅限本次 `$RUNROOT`） | 仅删除本次运行自建目录 |

**一句话结论：** `HG-A → HG-E` 与 `HG-H` 属「安全、无需同意」；
**`HG-F → HG-G` 属「⛔ 需要操作者显式同意（G1）」，本轮一律停在 HG-F 之前。**

### 9.2 无需同意的步骤（HG-A → HG-E）：命令、中止、回滚

> 下列命令取自 §7，本轮**未执行**。所有真实写入只允许落在 `$RUNROOT`；repo 始终只读。

**HG-A — hygiene + 隔离前置（先建隔离，再谈其余）。**
```sh
REPO=$(git rev-parse --show-toplevel)
REPO=$(cd "$REPO" && pwd -P)          # 对称解析，避免 /var 与 /private/var 绕过 guard
TMP=${TMPDIR:-/tmp}

# A1 只读盘点（不删除）
find "$TMP" -maxdepth 1 -type d -name 'm606-e2e-*' | wc -l
du -shc "$TMP"/m606-e2e-* 2>/dev/null | tail -1

# A2 可选回收：先看清单，逐项核对后再删单个目录（禁止裸通配 rm）
find "$TMP" -maxdepth 1 -type d -name 'm606-e2e-*' -print
# 仅对已确认属旧 fixture 的目录逐个执行： rm -rf -- "<单个目录>"

# A3 建隔离根 + 越界 guard
RUNROOT=$(mktemp -d "$TMP/m606-run-XXXXXXXX"); RUNROOT=$(cd "$RUNROOT" && pwd -P)
echo "$RUNROOT"
case "$RUNROOT" in "$REPO"|"$REPO"/*) echo "GUARD: RUNROOT inside repo"; exit 1;; esac
git -C "$REPO" rev-parse HEAD
git -C "$REPO" status --porcelain | wc -l   # 记录基线；dirty 时禁止以 repo 作 install target
```
*证据：* `runroot.txt`（解析后路径 + `outside_protected=true`）。
*中止：* guard 命中、`RUNROOT` 为空、或 `mktemp` 失败。
*回滚：* `rm -rf "${RUNROOT:?}"`；除此之外无状态变更。

**HG-B — G2 启动检查（仅回环，无模型调用、无 key）。**
```sh
cd "$REPO" && .venv/bin/python - "$RUNROOT" <<'PY'
import json, sys, pathlib
from teamcraft import providers
s = providers.probe_omlx("http://127.0.0.1:8000/v1")
want = "Qwen3.8-Flash-Next-oQ4e-mtp"
out = {"health": s.health, "reason_code": s.reason_code, "source": s.source,
       "models": list(s.models), "declared_model_present": want in s.models}
pathlib.Path(sys.argv[1], "g2-omlx.json").write_text(json.dumps(out, indent=2) + "\n")
print(out)
assert s.health == "healthy" and out["declared_model_present"], "G2 not met"
PY
```
*证据：* `g2-omlx.json`。
*中止：* `health != healthy` 或声明模型不在目录 ⇒ 记录 `G2-fail`，**不得**回退到云端。
*回滚：* 删除 `g2-omlx.json` / `$RUNROOT`。

**HG-C — G3 启动检查（只读 herdr，禁止变更拓扑）。**
```sh
herdr status                > "$RUNROOT/g3-status.txt"
herdr session list          > "$RUNROOT/g3-sessions.json"
herdr api snapshot          > "$RUNROOT/g3-snapshot-before.json"
```
断言 `private_protocol_compatible: yes`、`restart_needed: no`，并记录目标 pane 的
`(workspace_id, tab_id, pane_id, terminal_id, agent, agent_session.value)` 到
`g3-target-pane.json`。
*允许动词：* `status`、`session list`、`tab list`、`api snapshot|schema`。
*本步禁止：* `tab create|close|focus`、`pane`、`agent`、`session attach|stop|delete`。
*中止：* 版本 skew、`endpoint_compatible: no`、或无 `agent_session.value`。
*回滚：* 删除本次写入的 `$RUNROOT/g3-*`；拓扑本就不该变化。

**HG-D — S0/S1 一次性脚手架。**
```sh
git init -q --bare "$RUNROOT/origin.git"
git clone -q "$RUNROOT/origin.git" "$RUNROOT/project"
PATH="$REPO/.venv/bin:$PATH" bash "$REPO/scripts/install.sh" --target "$RUNROOT/project" --dry-run | tee "$RUNROOT/install-dry.txt"
PATH="$REPO/.venv/bin:$PATH" bash "$REPO/scripts/install.sh" --target "$RUNROOT/project"           | tee "$RUNROOT/install.txt"
```
随后在 run 内 DB：`storage.migrate_to_head(db, allow_unverified_location=True)`，
建 project + CEO Attempt 并钉死 `protocol_version=1.0.0` /
`protocol_hash=c5c358bf…742`，再对 `$RUNROOT` 下每个副本重算 hash。
*通过：* dry-run 0 文件；真装 ≥52 变更；每个副本 MATCH；目标内 `verify_protocol`
返回钉死对；DB `fresh=True, 0→17`。
*中止：* 托管冲突或任一 hash 漂移 ⇒ `protocol-drift`，停止。
*回滚：* `rm -rf "${RUNROOT:?}"`。

**HG-E — PRE-5 离线基线（运行起点复跑）。**
```sh
cd "$REPO" && .venv/bin/python -m pytest tests/test_providers_m501.py tests/test_router_m502.py \
  tests/test_ceo_protocol_m108.py tests/test_ceo_initialization_m208.py tests/test_wake_m209.py \
  tests/test_local_e2e_m606.py -q
```
*中止：* 任一失败。
*回滚：* 无持久状态；pytest cache 可忽略。

### 9.3 ⛔ 需要同意的步骤（HG-F → HG-G）

**HG-F — ⛔ CONSENT CHECKPOINT (G1)，本轮到此为止。**
前置：HG-A…HG-E 全绿，且操作者已书面/口头明确指定**一个 pane**、**一条 leg**、
**一次 prompt 的范围**（如“只读摘要、禁止编辑”）。未获该同意时 S2/S3 保持
`pending:G1`，不得以任何形式代批。获同意后按序执行：
1. 向 HG-C 绑定的 pane 提交**一条** prompt（read-mostly，例如“总结 README 结构，不要编辑”）；
2. 提交**之后**再从 `herdr api snapshot` 绑定身份（四元组 + `agent_session.value`）并写入 attempt 行；
3. 入队**恰好一次** `ceo.wake`，预期**恰好一次** `wake-dispatch`，记录
   `pending→sending→submitted|unknown`；
4. 两条 leg 消费**同一个** protocol hash（R4）。
*中止 / UNKNOWN：* 提交丢失、重复发送、或 `sending` 超时 ⇒ 判 **UNKNOWN**、隔离、
   停该 leg、**绝不自动重试**；保留 worktree 供检查（UNKNOWN 拒绝路径已由 fixture 证明为绿）。
*回滚：* 提交本身不可撤回；可清理的只有 `$RUNROOT` 与 worktree 的 cleanable 状态。

**HG-G — ⛔ 承接 HG-F 的真实 leg 验证/交付。**
仅在 HG-F 已获同意且产生真实产物后执行：验证 profile 从 run worktree 的 repo facts
计算结论；对分支 diff 跑 Diff Gate；对本地 bare origin 做 local-only 交付；
`mark_worktree_cleanable` → `evaluate_cleanup_eligibility` → `cleanup_attempt_worktree`
（**永不 force**）。
*中止：* Diff-Gate 的未声明改动按“负向探针通过”记录；origin 除种子 `refs/heads/main`
外**不得**新增任何 ref。
*回滚：* 只删除 `$RUNROOT` 内 worktree/branch；repo 与真实 origin 不受影响。

### 9.4 回滚 / 清理纪律（复用 HG-A 与 HG-H）

- **隔离优先：** 先建 `$RUNROOT` 并过 guard，再做任何写入；repo 始终只读。
- **rmtree 纪律：** 只用 `rm -rf "${RUNROOT:?}"`（带 `:?`，变量为空即失败），
  绝不用裸 `$RUNROOT` / 未加引号通配。
- **回收纪律：** 只处理 `$TMPDIR` 下 `-maxdepth 1` 的 `m606-e2e-*` / `m606-run-*`，
  删除前 `-print` 逐项核对；不触碰 `.teamcraft/`、git worktree、真实 DB。
- **失败即停：** 任一 `*Abort` 命中后先保全证据、再删 `$RUNROOT`，不继续后续步骤。
- **收尾核对：** HG-H 复跑 `test_local_e2e_m606` 仍绿；`du -shc "$TMP"/m606-run-*`；
  证据拷出并 digest 校验后再删 `$RUNROOT`；`git -C "$REPO" status --porcelain`
  只应出现预期的 doc/test 变更。

### 9.5 本轮边界与本轮发现的脚本风险（需后续 owner 决定）

**明确声明：** 本节**未执行**任何「⛔ 需要操作者显式同意（G1）」的步骤，
**不代表 PG-7 已关闭**，**不代表 G1 已释放**，也未声称真实 Herdr / Claude /
GitHub Actions 已运行。

本轮仅阅读 §7 文本，发现三处需后续 owner 处理的事项（**未修改**，因不在本文件
ownership 内，仅记录给后续执行者）：
1. §7 HG-A 的 guard 只对 `RUNROOT` 做 `pwd -P`，未对 `$REPO` 对称解析；建议如 9.2
   所示先 `REPO=$(cd "$REPO" && pwd -P)`，避免符号链接/`/var`↔`/private/var` 绕过。
2. §7 第 277 行的 `test -z "$(git status --porcelain)" || echo NOTE` 只**警告**不中止；
   真实运行应把“repo dirty”作为**禁止以 repo 为 install target**的硬条件。
3. H-1（fixture 不 `rmtree`，每轮 +23 根）根因在 `tests/test_local_e2e_m606.py`，
   **不在本文件**；修复需该测试 owner 处理，本文档只记录。
