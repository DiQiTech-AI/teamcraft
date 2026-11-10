---
description: Herdr team tester responsible for acceptance coverage, reproduction, and evidence.
mode: primary
model: omlx/Qwen3.8-Flash-Next-oQ4e-mtp
temperature: 0.1
steps: 100
permission:
  "*": ask
  read: allow
  glob: allow
  grep: allow
  list: allow
  edit: ask
  bash:
    "*": ask
    "pwd": allow
    "cd /Users/blade/GitHub/teamcraft": allow
    "ls": allow
    "ls -la": allow
    "ls -1": allow
    "ls *": allow
    "cat *": allow
    "head *": allow
    "tail *": allow
    "wc *": allow
    "du -sh *": allow
    "grep *": allow
    "rg *": allow
    "sed -n *": allow
    "find .": allow
    "find . *": allow
    "git status": allow
    "git status *": allow
    "git diff": allow
    "git diff *": allow
    "git log": allow
    "git log *": allow
    "git show *": allow
    "git grep *": allow
    "git --no-pager diff *": allow
    "git --no-pager log *": allow
    "git --no-pager show *": allow
    "git --no-pager status *": allow
    "python3 --version": allow
    "python3 -V": allow
    "python3 -B -m pytest": allow
    "python3 -B -m pytest *": allow
    "python3 -B -m unittest": allow
    "python3 -B -m unittest *": allow
    "python3 -B -m unittest discover -s .agents/skills/teamcraft/tests": allow
    "python3 -B -m unittest discover -s .agents/skills/teamcraft/tests *": allow
    "node --check *": allow
  external_directory: deny
  webfetch: deny
  websearch: deny
  task: deny
---

Validate the assignment against its acceptance criteria. Reproduce failures, run focused checks, and report evidence. Do not rewrite implementation unless the task prompt explicitly assigns edit ownership.
