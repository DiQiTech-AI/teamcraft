---
name: teamcraft-ceo
description: Lead a software delivery objective through the TeamCraft CEO protocol. Use when acting as the project CEO to discover context, plan work, coordinate specialists, review evidence, and decide delivery.
---

# TeamCraft CEO

This is a loader, not a second copy of the CEO rules. Before making project decisions:

1. Read `references/CEO_PROTOCOL.md` in full.
2. Verify its declared version and exact bytes against `references/protocol.manifest.json`, using `PYTHONPATH=.agents/skills/teamcraft/scripts/core python3 -m teamcraft.ceo_protocol .agents/skills/teamcraft-ceo/references`. The manifest SHA-256 covers the raw UTF-8 bytes of the Markdown file, including line endings.
3. If either file is missing, unreadable, mismatched, or incompatible with a version/hash already pinned to the active Attempt, stop dispatch and report `NEEDS_HUMAN`. Do not substitute remembered instructions or silently adopt a newer protocol.
4. Continue to use the `teamcraft` runtime Skill for Herdr operations and its durable command/recovery rules. This CEO Skill does not replace or duplicate that runtime.

The shared `.agents/skills/teamcraft-ceo/` path is used by Codex and OpenCode. Do not copy the protocol into `AGENTS.md`, `CLAUDE.md`, or Harness-specific agent prompts. Claude Code installation is not part of this release.
