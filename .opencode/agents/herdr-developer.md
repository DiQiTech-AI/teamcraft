---
description: Herdr team developer responsible for bounded implementation and verification.
mode: primary
model: omlx/Qwen3.8-Flash-Next-oQ4e-mtp
temperature: 0.1
steps: 150
permission:
  "*": ask
  read: allow
  glob: allow
  grep: allow
  list: allow
  edit: allow
  bash:
    "*": ask
    "git": allow
    "git *": allow
    "pwd": allow
    "pwd *": allow
    "ls": allow
    "ls *": allow
    "rg *": allow
    "grep *": allow
    "cat *": allow
    "head *": allow
    "tail *": allow
    "wc *": allow
    "du -sh *": allow
    "find .": allow
    "git status": allow
    "git status *": allow
    "git diff": allow
    "git diff *": allow
    "git log": allow
    "git log *": allow
    "git show": allow
    "git show *": allow
    "git grep *": allow
    "git ls-files": allow
    "git ls-files *": allow
    "git rev-parse *": allow
    "git worktree list": allow
    "git worktree list *": allow
    "git --no-pager diff *": allow
    "git --no-pager log *": allow
    "git --no-pager show *": allow
    "git --no-pager status *": allow
    "python3 -B -m pytest": allow
    "python3 -B -m pytest *": allow
    "python3 -B -m unittest": allow
    "python3 -B -m unittest *": allow
    "python3 -B -m unittest discover -s .agents/skills/teamcraft/tests": allow
    "python3 -B -m unittest discover -s .agents/skills/teamcraft/tests *": allow
    "node --check *": allow
    "python3 --version": allow
    "python3 -V": allow
    "python3 -B -m py_compile *": allow
    "shasum -a 256 *": allow
    # Explicit denies (defense-in-depth on top of "*": ask) remain for
    # non-Git network, package-install, destructive, and elevated commands.
    # Git commands are authorized by the project default above.
    "pip": deny
    "pip *": deny
    "pip3": deny
    "pip3 *": deny
    "npm *": deny
    "npx *": deny
    "curl *": deny
    "wget *": deny
    "sudo": deny
    "sudo *": deny
    "rm": deny
    "rm *": deny
  external_directory: deny
  webfetch: deny
  websearch: deny
  task: deny
---

Implement and verify the bounded assignment in the Herdr task prompt. Edit only the recorded ownership paths. Preserve unrelated changes and return changed files, verification evidence, and blockers. Use built-in read/glob/grep/list tools and the allowlisted bash commands directly without requesting approval; non-Git commands outside the allowlist or with possible side effects remain gated. All Git commands are authorized by the project default and do not require another permission prompt.
