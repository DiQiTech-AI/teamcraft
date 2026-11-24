"""Offline contract tests for scripts/m606_prepare_opencode_pane.py (option B).

No OpenCode/Herdr process, no network, no live ``teamcraft`` DB, no real session
ids.  Runs against a disposable fake world under ``mkdtemp`` with the tested
primitives injected (bridge src/status, proof runner, herdr client).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import m606_prepare_opencode_pane as prep  # noqa: E402
import m606_opencode_e2e as runner  # noqa: E402

TEMPLATE = REPO_ROOT / "opencode.example.json"
ATTEMPT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DESIGNATED_PANE = "w7:p9"

# Designated pane differs from the invoking shell's HERDR_PANE_ID (w9:p9Z) to prove
# the launch command BINDS the designated pane rather than inheriting a stale one.
BASE_ENV = {
    "PATH": "/usr/bin:/bin", "TERM": "xterm",
    "HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/run/herdr/h.sock", "HERDR_PANE_ID": "w9:p9Z",
    "OPENAI_API_KEY": "sk-leak", "ANTHROPIC_API_KEY": "sk-leak", "SOME_GENERAL_CONFIG": "x",
}


def make_world(tmp: Path):
    root = tmp / "run"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "journal.jsonl").write_text('{"kind":"created"}\n', encoding="utf-8")
    worktree = root / "project" / ".teamcraft" / "worktrees" / ATTEMPT
    worktree.mkdir(parents=True)
    (root / "evidence" / "handoff.json").write_text(
        json.dumps({"run_id": "r" * 16, "attempt_id": ATTEMPT,
                   "allocated_cwd": str(worktree), "base_sha": "0" * 40}), encoding="utf-8")
    return root, worktree, root / "project"


def fake_bridge(tmp: Path):
    src = tmp / "installed" / prep._sessreg.BRIDGE_PLUGIN_NAME
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("// verified v13 bridge\n", encoding="utf-8")
    return str(src), f"opencode: current (v13) ({src})\n"


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root, self.worktree, self.project = make_world(self.tmp)
        self.src, self.status = fake_bridge(self.tmp)
        self.other = Path(tempfile.mkdtemp())   # a different, external dir

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.other, ignore_errors=True)

    def _base(self, **kw):
        kw.setdefault("role", "worker"); kw.setdefault("root", str(self.root))
        kw.setdefault("pane", DESIGNATED_PANE); kw.setdefault("base_env", dict(BASE_ENV))
        kw.setdefault("template", TEMPLATE); kw.setdefault("bridge_src", self.src)
        kw.setdefault("bridge_status_text", self.status)
        return kw

    def test_bad_role(self):
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(role="ghost"))

    def test_bad_pane_format(self):
        for bad in ("", "auto", "w1", "pane", "wX:pY"):
            with self.assertRaises(prep.PrepError):
                prep.prepare_pane(**self._base(pane=bad))

    def test_invalid_session_id_rejected(self):
        for bad in ("bad-id", "ses_", "ses_hase spaces", "sess_x", "X" * 20):
            with self.assertRaises(prep.PrepError):
                prep.prepare_pane(**self._base(session_id=bad))

    def test_live_repo_root_refused_by_reused_guard(self):
        with self.assertRaises(runner.GuardError):
            prep.prepare_pane(**self._base(root=str(REPO_ROOT)))

    def test_cwd_override_outside_root_rejected(self):
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(cwd=str(self.other)))

    def test_cwd_override_wrong_role_rejected(self):
        # worker expects the allocated worktree; passing project root must fail.
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(role="worker", cwd=str(self.project)))

    def test_sandbox_override_outside_root_rejected(self):
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(sandbox=str(self.other)))
        # nothing written under the external dir
        self.assertEqual(list(self.other.iterdir()), [])

    def test_sandbox_symlink_escape_rejected(self):
        esc = self.root / "esc"
        os.symlink(str(self.other), str(esc))
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(sandbox=str(esc)))
        self.assertEqual(list(self.other.iterdir()), [])   # no writes escaped into it

    def test_handoff_alloc_cwd_outside_root_rejected(self):
        (self.root / "evidence" / "handoff.json").write_text(
            json.dumps({"attempt_id": ATTEMPT, "allocated_cwd": str(self.other)}), encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(role="worker"))


class PrepareHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root, self.worktree, self.project = make_world(self.tmp)
        self.src, self.status = fake_bridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prep(self, **kw):
        kw.setdefault("role", "worker"); kw.setdefault("root", str(self.root))
        kw.setdefault("pane", DESIGNATED_PANE); kw.setdefault("base_env", dict(BASE_ENV))
        kw.setdefault("template", TEMPLATE); kw.setdefault("bridge_src", self.src)
        kw.setdefault("bridge_status_text", self.status)
        return prep.prepare_pane(**kw)

    def test_worker_targets_alloc_worktree_and_only_bridge(self):
        rep = self._prep()
        self.assertEqual(Path(rep["cwd"]).resolve(), self.worktree.resolve())
        self.assertTrue(rep["bridge_ready"])
        files = sorted(p.name for p in (Path(rep["sandbox"]) / "xdg-config" / "opencode").rglob("*") if p.is_file())
        self.assertEqual(files, [prep._sessreg.BRIDGE_PLUGIN_NAME])
        self.assertEqual(rep["pinned_config"]["status"], "written")

    def test_ceo_targets_project_root(self):
        rep = self._prep(role="ceo")
        self.assertEqual(Path(rep["cwd"]).resolve(), self.project.resolve())

    def test_custom_sandbox_inside_root_accepted(self):
        sbx = self.root / "mysbx"
        rep = self._prep(sandbox=str(sbx))
        self.assertTrue((sbx / "xdg-config").exists())
        self.assertEqual(Path(rep["sandbox"]).resolve(), sbx.resolve())

    def test_existing_valid_loopback_reused_not_overwritten(self):
        cfg = json.loads(TEMPLATE.read_text("utf-8"))
        dest = self.worktree / "opencode.json"
        dest.write_text(json.dumps(cfg) + "\n", encoding="utf-8")
        before = dest.read_text()
        rep = self._prep()
        self.assertEqual(rep["pinned_config"]["status"], "reused-validated")
        self.assertFalse(rep["pinned_config"]["written"])
        self.assertEqual(dest.read_text(), before)   # untouched

    def test_existing_invalid_config_rejected_and_untouched(self):
        dest = self.worktree / "opencode.json"
        bad = json.dumps({"model": "openai/gpt-4",
                         "provider": {"openai": {"options": {"baseURL": "https://api.openai.com/v1", "apiKey": "sk-x"}}}})
        dest.write_text(bad + "\n", encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            self._prep()
        self.assertEqual(dest.read_text(), bad + "\n")   # never overwritten

    def test_existing_malformed_config_rejected(self):
        dest = self.worktree / "opencode.json"
        dest.write_text("{not json", encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            self._prep()

    def test_launch_env_minus_i_binds_designated_pane(self):
        rep = self._prep(session_id="ses_worker0000000000000000000001")
        cmd = rep["launch_command"]
        self.assertTrue(cmd.startswith("env -i"), cmd)
        self.assertIn('PATH="$PATH"', cmd)
        self.assertIn('HERDR_ENV="${HERDR_ENV:?', cmd)
        self.assertIn('HERDR_SOCKET_PATH="${HERDR_SOCKET_PATH:?', cmd)
        self.assertIn(f"HERDR_PANE_ID={DESIGNATED_PANE}", cmd)          # bound to designated
        self.assertNotIn("w9:p9Z", cmd)                                  # stale base pane NOT baked
        self.assertNotIn(BASE_ENV["HERDR_SOCKET_PATH"], cmd)             # live socket preserved, not baked
        self.assertNotIn("-u ", cmd)                                     # env -i clears, no -u needed
        self.assertIn("--standalone", cmd)
        self.assertIn("--session ses_worker0000000000000000000001", cmd)
        self.assertEqual(rep["retained_herder_vars"], ["HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID"])

    def test_launch_command_shell_quotes_paths_with_spaces(self):
        cmd, _ = prep.launch_command("worker", "w1:p2", Path("/tmp/space dir/work"),
                                    Path("/tmp/space dir/sbx"), BASE_ENV, "ses_ok00000000000000000000000")
        self.assertIn("'/tmp/space dir/work'", cmd)
        self.assertIn("'/tmp/space dir/sbx/home'", cmd)

    def test_preview_lacks_consent_dispatch_and_dispatch_is_separate(self):
        rep = self._prep()
        self.assertNotIn("--consent-dispatch", rep["runner_validate"])
        self.assertIn("--resume-handoff", rep["runner_validate"])
        self.assertIn("--consent-dispatch", rep["dispatch_example"])
        self.assertIn("ACTUAL DISPATCH", rep["dispatch_example"])

    def test_no_prompt_or_lifecycle_or_live_db(self):
        rep = self._prep()
        combined = rep["launch_command"] + "\n" + rep["runner_validate"] + "\n" + rep["dispatch_example"] + "\n" + rep["observe_command"]
        for forbidden in ("send-keys", "agent prompt", "session.prompt", "session.command",
                         "herdr pane new", "worktree add", "--worktree", "opencode run"):
            self.assertNotIn(forbidden, combined)
        for val in rep.values():
            if isinstance(val, str):
                self.assertNotIn("teamcraft.db", val)


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root, self.worktree, self.project = make_world(self.tmp)
        self.src, self.status = fake_bridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disposable_proof_no_prompt(self):
        calls = []

        def proof(op, *argv, timeout=30):
            calls.append((op, *argv))
            return (0, {"data": []}) if op == "session.list" else (0, {"data": {"id": "ses_disposable00000000000000000000"}})

        rep = prep.prepare_pane(role="worker", root=str(self.root), pane=DESIGNATED_PANE,
                                base_env=dict(BASE_ENV), template=TEMPLATE, bridge_src=self.src,
                                bridge_status_text=self.status, with_proof=True, proof_runner=proof)
        self.assertTrue(rep["proof"]["native_id"].startswith("ses_"))
        self.assertTrue(rep["proof"]["native_ref"].startswith("sha256:"))
        self.assertTrue(rep["proof"]["provider_absent"])
        self.assertIn(("session.create", "-d", "{}"), calls)
        self.assertNotIn("session.prompt", [c[0] for c in calls])

    def test_no_proof_by_default(self):
        rep = prep.prepare_pane(role="worker", root=str(self.root), pane=DESIGNATED_PANE,
                                base_env=dict(BASE_ENV), template=TEMPLATE,
                                bridge_src=self.src, bridge_status_text=self.status)
        self.assertNotIn("proof", rep)


class ObserveTests(unittest.TestCase):
    class FakeHerdr:
        def identity(self, pane):
            return {"pane_id": pane, "agent": "opencode", "session_id": "ses_live0000000000000000000099"}

    class NoSessionHerdr:
        def identity(self, pane):
            raise runner.PostconditionError("no native session id")

    def test_mask_and_reveal(self):
        m = prep.observe_native_id(DESIGNATED_PANE, herdr=self.FakeHerdr())
        self.assertNotIn("ses_live0000000000000000000099", m["session_id"])
        self.assertTrue(m["session_ref"].startswith("sha256:"))
        raw = prep.observe_native_id(DESIGNATED_PANE, reveal=True, herdr=self.FakeHerdr())
        self.assertEqual(raw["session_id"], "ses_live0000000000000000000099")

    def test_refuses_when_no_native_session(self):
        with self.assertRaises(runner.PostconditionError):
            prep.observe_native_id(DESIGNATED_PANE, herdr=self.NoSessionHerdr())


class CloudTemplateRejected(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cloud_template_refused(self):
        bad = self.tmp / "cloud.json"
        bad.write_text(json.dumps({"model": "openai/gpt-4",
                                  "provider": {"openai": {"options": {"baseURL": "https://api.openai.com/v1", "apiKey": "sk-x"}}}}), encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            prep.pinned_loopback_config(bad)


class CliTests(unittest.TestCase):
    def test_parser(self):
        import contextlib
        import io
        p = prep.build_parser()
        args = p.parse_args(["prep", "--role", "worker", "--root", "/tmp/run", "--herdr-pane", "w1:p2"])
        self.assertEqual((args.command, args.role), ("prep", "worker"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.parse_args(["prep"])


if __name__ == "__main__":
    unittest.main()
