"""M4-02 runtime-adapter routing tests: writable worktrees, root read-only.

Temp Git repositories only — the real project repository is never touched.

Acceptance covered here:
* two concurrent writable developer members route into DISTINCT worktrees and
  do not share uncommitted edits;
* read-only members stay on the project root and receive NO writable
  worktree (no allocation is recorded for them at all);
* a restart (reconnect) re-routes to the EXACT recorded identity;
* a failed worker's dirty workspace and durable state stay discoverable and
  are never marked cleanable;
* a clean terminal Attempt's workspace transitions ALLOCATED -> CLEANABLE
  (marker only — nothing is deleted at M4-02).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from teamcraft import domain, runtime_adapter as ra, worktrees
from teamcraft.errors import IllegalTransitionError
from teamcraft.runtime_adapter import RuntimeAdapterError

GIT = [
    "git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false", "-c", "user.name=T",
    "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run([*GIT, *args], cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def init_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text("/.teamcraft/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo


def _member(name: str, **overrides):
    member = {
        "name": name, "role": "developer", "responsibility": f"own {name}",
        "kind": "opencode", "task": f"deliver {name} milestone",
        "ownership": [f"src/{name}/**"], "read_only": False,
        "cwd": None, "from_pane_id": "w-1", "pane_id": "p-1",
        "terminal_id": None, "agent_session": None,
        "agent_args": ["opencode"], "opencode_config_content": None,
        "direction": None, "start_timeout_ms": 30_000,
        "phase": "pane_created",
        "prompt_may_have_been_delivered": False, "live_state": None,
        "error": None, "task_id": None, "attempt_id": None,
        "session_id": None, "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    member.update(overrides)
    return member


class AdapterRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-m402-adapter-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo = init_repo(self.base)
        self.conn, _report = ra.open_runtime(self.repo)
        self.addCleanup(self.conn.close)
        self.state = {
            "version": ra.STATE_VERSION, "team_id": "team-m402",
            "project_root": str(self.repo),
            "herdr_session": {"server_id": "srv", "socket_path": "/s"},
            "created_at": int(time.time()),
            "members": {
                "dev1": _member("dev1"),
                "dev2": _member("dev2"),
                "scout": _member("scout", role="designer", read_only=True,
                                ownership=["docs/**"]),
            },
        }
        # member rows carry provisional cwd (project root) until routed.
        for member in self.state["members"].values():
            member["cwd"] = str(self.repo)
        ra.sync_state(self.conn, self.state)

    # -- routing -----------------------------------------------------------

    def test_two_writable_members_get_distinct_isolated_worktrees(self):
        r1 = ra.route_member_workspace(self.conn, "dev1")
        r2 = ra.route_member_workspace(self.conn, "dev2")
        self.assertEqual(r1["routed"], r2["routed"], "worktree")
        self.assertTrue(r1["created"] and r2["created"])
        self.assertNotEqual(r1["cwd"], r2["cwd"])
        self.assertEqual(r1["worktree"]["branch"],
                         f"tc/{self.state['members']['dev1']['task_id']}/"
                         f"{self.state['members']['dev1']['attempt_id']}")
        # Edits never cross: dev1's file is absent in dev2 and the root.
        (Path(r1["cwd"]) / "src" / "dev1" ).mkdir(parents=True, exist_ok=True)
        (Path(r1["cwd"]) / "src" / "dev1" / "only.py").write_text(
            "1\n", encoding="utf-8")
        self.assertFalse((Path(r2["cwd"]) / "src" / "dev1" / "only.py").exists())
        self.assertFalse((self.repo / "src" / "dev1" / "only.py").exists())

    def test_read_only_member_stays_on_project_root_without_worktree(self):
        rs = ra.route_member_workspace(self.conn, "scout")
        self.assertEqual(rs["routed"], "project_root")
        self.assertEqual(Path(rs["cwd"]), self.repo)
        self.assertIsNone(rs["worktree"])
        attempt_id = self.state["members"]["scout"]["attempt_id"]
        self.assertIsNotNone(attempt_id)  # lineage exists…
        self.assertIsNone(worktrees.lifecycle_state(self.conn, attempt_id))
        # …but NO writable workspace was allocated for the read-only member.
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? AND attempt_id = ?",
            (worktrees.ALLOCATED_EVENT_KIND, attempt_id)).fetchone()
        self.assertEqual(int(rows[0]), 0)
        with self.assertRaises(RuntimeAdapterError):
            ra.mark_member_workspace_cleanable(self.conn, "scout")

    def test_writable_member_without_lineage_is_refused(self):
        extra = _member("late", phase="reserved")
        self.state["members"]["late"] = extra
        ra.sync_state(self.conn, self.state)
        with self.assertRaises(RuntimeAdapterError):
            ra.route_member_workspace(self.conn, "late")

    # -- restart adoption ----------------------------------------------------

    def test_restart_reroutes_to_exact_recorded_identity(self):
        r1 = ra.route_member_workspace(self.conn, "dev1")
        (Path(r1["cwd"]) / "draft.txt").write_text("keep me\n",
                                                  encoding="utf-8")
        self.conn.close()
        self.conn = ra.connect_runtime(self.repo)  # process restart
        r2 = ra.route_member_workspace(self.conn, "dev1")
        self.assertFalse(r2["created"])
        self.assertEqual(r2["cwd"], r1["cwd"])
        status = ra.member_workspace_status(self.conn, "dev1")
        self.assertEqual(status["routed"], "worktree")
        self.assertTrue(status["worktree"]["exists"])
        self.assertEqual(status["worktree"]["worktree_path"], r1["cwd"])
        self.assertEqual(status["worktree"]["branch"], r1["worktree"]["branch"])
        self.assertTrue((Path(r1["cwd"]) / "draft.txt").exists())

    # -- failure / cleanable --------------------------------------------------

    def _fail(self, member: str) -> str:
        attempt_id = self.state["members"][member]["attempt_id"]
        domain.transition_attempt(self.conn, attempt_id, "start_failed")
        return attempt_id

    def test_failed_worker_dirty_changes_preserved_and_discoverable(self):
        r1 = ra.route_member_workspace(self.conn, "dev1")
        dirty = Path(r1["cwd"]) / "src" / "dev1" / "half.py"
        dirty.parent.mkdir(parents=True, exist_ok=True)
        dirty.write_text("half done\n", encoding="utf-8")
        attempt_id = self._fail("dev1")
        with self.assertRaises(IllegalTransitionError) as ctx:
            ra.mark_member_workspace_cleanable(self.conn, "dev1")
        self.assertEqual(ctx.exception.reason_code, "WORKTREE_DIRTY")
        self.assertEqual(dirty.read_text(), "half done\n")
        status = ra.member_workspace_status(self.conn, "dev1")
        self.assertEqual(status["worktree"]["state"], "allocated")
        self.assertTrue(status["worktree"]["dirty"])
        self.assertEqual(status["worktree"]["change_count"], 1)
        # durable discovery after a further restart:
        self.conn.close()
        self.conn = ra.connect_runtime(self.repo)
        self.assertEqual(worktrees.lifecycle_state(self.conn, attempt_id),
                         "allocated")
        self.assertTrue(dirty.read_text() == "half done\n")

    def test_clean_terminal_attempt_marks_cleanable_without_deleting(self):
        r2 = ra.route_member_workspace(self.conn, "dev2")
        attempt_id = self.state["members"]["dev2"]["attempt_id"]
        domain.transition_attempt(self.conn, attempt_id, "activated")
        domain.transition_attempt(self.conn, attempt_id, "finish")
        marked = ra.mark_member_workspace_cleanable(self.conn, "dev2")
        self.assertEqual(marked["worktree"]["state"], "cleanable")
        self.assertTrue(Path(r2["cwd"]).is_dir())  # M4-06 deletes, not here
        listed = git("worktree", "list", cwd=self.repo)
        self.assertIn(r2["cwd"], listed)

    def test_status_before_allocation_is_unallocated(self):
        status = ra.member_workspace_status(self.conn, "dev1")
        self.assertEqual(status["routed"], "unallocated")
        self.assertIsNone(status["worktree"])


if __name__ == "__main__":
    unittest.main()
