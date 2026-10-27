---
name: teamcraft
description: Build and coordinate a stateful software delivery team in the current Herdr tab, with a CEO orchestrator and on-demand built-in or custom specialist agents. Use when the user asks to assemble, recover, inspect, or manage a development team through Herdr; do not use for ordinary single-agent delegation.
---

# TeamCraft

Create a task-specific team in sibling panes while preserving the current tab, project directory, and user focus.

## Preconditions

Before Herdr control, require `HERDR_ENV=1`, then run `herdr --skill` and follow the installed binary's semantics. Do not inspect or control a focused Herdr session from outside Herdr. Do not create a workspace, tab, or worktree unless the user requests that topology.

Use `scripts/teamctl.py` for managed team members. It reserves ownership and intent in `<project>/.teamcraft/state.json` before layout mutation, records every later phase, and scopes the state to the current Herdr socket session. Do not edit this state by hand while a team operation is running.

The `.teamcraft/` runtime directory contains tasks, state, and locks and is ignored by version control. If the CLI detects legacy `.herdr-team/state.json`, it fails closed without calling Herdr or creating new state; resolve that state explicitly before continuing.

## Team model

Bootstrap only the CEO unless the user asks for a fixed roster. If the user appoints the current agent as CEO, adopt the responsibilities in the current pane instead of spawning a duplicate CEO. Otherwise launch one CEO. The CEO decomposes the objective and creates workers only when their work is separable.

Roles describe responsibility; `--kind` independently selects any harness supported by the installed Herdr CLI.

- CEO owns requirements, staffing, assignments, review, integration, and delivery.
- CTO owns architecture, interfaces, tradeoffs, and technical risk.
- Developer owns bounded implementation and verification.
- Designer owns assigned flows, interaction, visual specifications, and assets.
- Tester owns acceptance coverage, reproduction, and evidence.
- Custom supplies another specialty through a concrete `--responsibility`.

Every editing worker needs concrete deliverables, acceptance criteria, and one or more non-overlapping ownership paths. Ownership is coordination metadata, not filesystem access enforcement. Use normalized project-relative exact paths or directory prefixes ending in `/**`; complex globs and prose are rejected. Use `--read-only` for review, investigation, or testing that must not edit files.

The CEO must collect changed-file lists and verification evidence, review results, release or complete finished ownership, and resolve integration gaps. Workers may inspect shared context but must not edit outside recorded ownership.

## Launch members

Launch a CEO from the project root:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role ceo --kind codex --name ceo \
  --task "Deliver the requested feature and create only needed workers."
```

Launch an OpenCode developer with two separate claims:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role developer --kind opencode --name api_dev \
  --ownership 'src/api/**' \
  --ownership 'tests/api/**' \
  --task-file /absolute/path/to/assignment.md \
  --agent-arg=--standalone
```

Launch a read-only tester:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role tester --kind codex --name acceptance \
  --read-only --task "Run the acceptance checks and report evidence; do not edit."
```

For another specialty:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role custom --responsibility "Own deployment automation and release evidence." \
  --kind codex --name release_eng --ownership 'deploy/**' \
  --task "Prepare staging; acceptance: the dry run succeeds."
```

The helper resolves the caller or `--from-pane` through Herdr before matching layout geometry, uses an explicit canonical pane ID, preserves native arguments, and keeps focus with `--no-focus`. It validates the installed Herdr kind list and harness executable before splitting.

For OpenCode, project profiles live in `.opencode/agents/herdr-*.md`. The helper selects the role through a pane-local `OPENCODE_CONFIG_CONTENT` default-agent override and retains the full OpenCode TUI for Herdr detection. This does not modify an existing default profile or widen its permissions. A live run has verified the CEO, developer, and tester profiles with `omlx/Qwen3.8-Flash-Next-oQ4e-mtp`; confirm the displayed role and model when first using an unverified profile or another project.

## Inspect and recover

Inspect recorded phase and actual live state:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py status
```

`agent_state` reports `blocked`, `working`, `idle`, `done`, `unknown`, or `missing`. When blocked, inspect `herdr agent get` and `herdr agent read`; do not blindly approve, resend, or retry.

Treat both `idle` and `done` as settled completion states. Do not wait only for `done`: Herdr may report a successfully completed worker as `idle`, and a done-only polling loop will not converge.

Recover a partial launch without creating another pane:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev
```

Resume verifies the recorded terminal ID and native agent session before adopting a live occupant. It reuses an available recorded shell pane after a failed start. Its default `--prompt-action inspect` never resends a possibly delivered task. After inspection, choose explicitly:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev --prompt-action send
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev --prompt-action skip
```

If a split timed out, inspect the layout first; use `--split-action retry` only when no pane was created. A timeout does not prove failure.

Release ownership without closing the pane, or mark the assignment complete:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py release api_dev
python3 .agents/skills/teamcraft/scripts/teamctl.py complete api_dev
```

Neither operation closes a pane or terminates an agent. Do not pre-create idle specialists, overlap active edit claims, or infer completion from `unknown`.

Release and completion refuse an agent in `working`, `blocked`, or `unknown` state. Use `--force` only after explicitly deciding that its ownership may be reassigned despite that state. Identity verification still applies.

Member lifecycle operations use an exclusive lock. State and locks in `.teamcraft/` are local runtime files excluded from version control. OpenCode launches require the selected role profile in the target project and preserve existing JSON `OPENCODE_CONFIG_CONTENT` settings while overriding only `default_agent`.
