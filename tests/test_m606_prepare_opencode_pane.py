"""Offline contract tests for scripts/m606_prepare_opencode_pane.py (option B).

No OpenCode/Herdr process, no network, no live ``teamcraft`` DB, no real session
ids.  Runs against a disposable fake world under ``mkdtemp`` with the tested
primitives injected (bridge src/status, proof runner, herdr client).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
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


TUI_SOURCE = """// installed by herdr
// managed by herdr; reinstalling or updating the integration overwrites this file.
// HERDR_INTEGRATION_ID=opencode-tui
// HERDR_INTEGRATION_VERSION=13
const SOURCE = "herdr:opencode";
function requestOnce(sessionID, state) {
  return request(state === undefined ? "pane.report_agent_session" : "pane.report_agent", {
    agent_session_id: sessionID,
    ...(state === undefined ? { session_start_source: "select" } : { state }),
  });
}
function routeID() { const route = api.route.current; return route?.params?.sessionID; }
"""


def fake_bridge(tmp: Path, *, tui: bool = True, register: bool = True,
                tui_source: str | None = None, config_name: str = "tui.jsonc",
                config_plugin: list | None = None,
                server_cli: bool = False):
    """Create a disposable installed layout (server + optional TUI bridge).

    Mirrors the verified OpenCode v2.4.1 layout: the server plugin lives under
    ``opencode/plugins`` and the TUI bridge is registered in ``tui.jsonc`` under
    the singular ``plugin`` key as ``./herdr-tui-session.js``.  ``cli.json`` is a
    *server* config (``plugins``) and must never count as a TUI registration.
    """
    root = tmp / "installed"
    shutil.rmtree(root, ignore_errors=True)   # each fixture call is a fresh layout
    src = root / "plugins" / prep._sessreg.BRIDGE_PLUGIN_NAME
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("// verified v13 bridge\n", encoding="utf-8")
    if tui:
        bridge = root / prep.TUI_BRIDGE_PLUGIN_NAME
        bridge.write_text(tui_source if tui_source is not None else TUI_SOURCE,
                          encoding="utf-8")
    if register:
        plugin = config_plugin if config_plugin is not None else [prep.TUI_BRIDGE_SPEC]
        (root / config_name).write_text(json.dumps({"plugin": plugin}), encoding="utf-8")
    if server_cli:
        wrapper = root / prep.TUI_REGISTRATION_DIR / prep.TUI_REGISTRATION_ENTRY
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text('export { default } from "../herdr-tui-session.js";\n',
                           encoding="utf-8")
        (root / "cli.json").write_text(
            json.dumps({"plugins": [f"./{prep.TUI_REGISTRATION_DIR}"]}), encoding="utf-8")
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

    def test_bridge_src_must_equal_reported_server_path(self):
        other = self.tmp / "elsewhere" / prep._sessreg.BRIDGE_PLUGIN_NAME
        other.parent.mkdir(parents=True)
        other.write_text(Path(self.src).read_text("utf-8"), encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(bridge_src=str(other)))
        self.assertFalse((self.root / "pane-prep" / "worker").exists())

    def test_bridge_src_symlink_refused(self):
        link = self.tmp / "linked-server.js"
        os.symlink(self.src, link)
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(bridge_src=str(link)))
        self.assertFalse((self.root / "pane-prep" / "worker").exists())

    def test_reported_server_bridge_symlink_refused(self):
        real = self.tmp / "real-server.js"
        real.write_text(Path(self.src).read_text("utf-8"), encoding="utf-8")
        link = self.tmp / "status-server.js"
        os.symlink(str(real), str(link))
        self.src = str(link)
        self.status = f"opencode: current (v13) ({link})\n"
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base(bridge_src=None))
        self.assertFalse((self.root / "pane-prep" / "worker").exists())

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

    def test_missing_tui_bridge_refused_before_write(self):
        self.src, self.status = fake_bridge(self.tmp, tui=False)
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())
        self.assertFalse((self.root / "pane-prep" / "worker").exists())

    def test_unregistered_tui_bridge_refused_before_write(self):
        self.src, self.status = fake_bridge(self.tmp, register=False)
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())
        self.assertFalse((self.root / "pane-prep" / "worker").exists())

    def test_unverified_tui_bridge_refused(self):
        for src in (
            "// HERDR_INTEGRATION_ID=opencode\n// HERDR_INTEGRATION_VERSION=13\n"
            "pane.report_agent_session agent_session_id session_start_source route.current\n",
            "// HERDR_INTEGRATION_ID=opencode-tui\n// HERDR_INTEGRATION_VERSION=12\n"
            "pane.report_agent_session agent_session_id session_start_source route.current\n",
        ):
            with self.subTest(src=src):
                self.src, self.status = fake_bridge(self.tmp, tui_source=src)
                with self.assertRaises(prep.PrepError):
                    prep.prepare_pane(**self._base())
                self.assertFalse((self.root / "pane-prep" / "worker").exists())

    def test_cli_json_server_registration_is_not_tui(self):
        # A server-only cli.json `plugins` entry must never satisfy TUI discovery.
        self.src, self.status = fake_bridge(self.tmp, register=False, server_cli=True)
        with self.assertRaises(prep.PrepError):
            prep.find_installed_tui_bridge(self.status)
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())
        self.assertFalse((self.root / "pane-prep" / "worker").exists())

    def test_cli_json_key_in_tui_config_is_not_a_tui_plugin(self):
        # Even a tui.jsonc that only carries the server key `plugins` is rejected.
        self.src, self.status = fake_bridge(self.tmp, register=False)
        (self.tmp / "installed" / "tui.jsonc").write_text(
            json.dumps({"plugins": [prep.TUI_BRIDGE_SPEC]}), encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())

    def test_tui_registration_escape_refused(self):
        self.src, self.status = fake_bridge(self.tmp, register=False)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "tui.js").write_text('export { default } from "../herdr-tui-session.js";\n',
                                        encoding="utf-8")
        (self.tmp / "installed" / "tui.jsonc").write_text(
            json.dumps({"plugin": ["../outside"]}), encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["tui.js"])

    def test_symlinked_tui_registration_refused(self):
        self.src, self.status = fake_bridge(self.tmp, register=False)
        root = self.tmp / "installed"
        real = root / "real-tui.js"
        real.write_text(TUI_SOURCE, encoding="utf-8")
        os.symlink(str(real), str(root / "linked-tui.js"))
        (root / "tui.jsonc").write_text(
            json.dumps({"plugin": ["./linked-tui.js"]}), encoding="utf-8")
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())

    def test_symlinked_installed_tui_bridge_refused(self):
        self.src, self.status = fake_bridge(self.tmp, register=True)
        bridge = self.tmp / "installed" / prep.TUI_BRIDGE_PLUGIN_NAME
        real = self.tmp / "real-tui.js"
        real.write_text(TUI_SOURCE, encoding="utf-8")
        bridge.unlink()
        os.symlink(str(real), str(bridge))
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())

    def test_isolated_general_config_refused_before_any_write(self):
        sbx = self.root / "pane-prep" / "worker"
        general = sbx / "xdg-config" / "opencode" / "opencode.json"
        general.parent.mkdir(parents=True)
        general.write_text('{"model":"cloud/x"}\n', encoding="utf-8")
        before = sorted(p.relative_to(sbx).as_posix()
                        for p in sbx.rglob("*"))
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())
        self.assertEqual(general.read_text("utf-8"), '{"model":"cloud/x"}\n')
        after = sorted(p.relative_to(sbx).as_posix()
                       for p in sbx.rglob("*"))
        self.assertEqual(after, before)   # no dirs, copies, or prep.json created
        self.assertFalse((self.worktree / "opencode.json").exists())
        self.assertFalse((sbx / "xdg-config" / "opencode" / "plugins").exists())
        self.assertFalse((sbx / "xdg-config" / "opencode" / prep.TUI_ISOLATED_CONFIG).exists())
        self.assertFalse((sbx / "prep.json").exists())

    def test_general_config_cli_failure_is_clean_and_partial_free(self):
        sbx = self.root / "pane-prep" / "worker"
        general = sbx / "xdg-config" / "opencode" / "opencode.json"
        general.parent.mkdir(parents=True)
        general.write_text('{"model":"cloud/x"}\n', encoding="utf-8")
        before = sorted(p.relative_to(sbx).as_posix()
                        for p in sbx.rglob("*"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = prep.main(["prep", "--role", "worker", "--root", str(self.root),
                            "--herdr-pane", DESIGNATED_PANE, "--sandbox", str(sbx)])
        self.assertEqual(rc, 2)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("general config", payload["error"])
        after = sorted(p.relative_to(sbx).as_posix()
                       for p in sbx.rglob("*"))
        self.assertEqual(after, before)
        self.assertFalse((self.worktree / "opencode.json").exists())

    def test_isolated_tui_config_symlink_escape_refused(self):
        sbx = self.root / "pane-prep" / "worker"
        xdg = sbx / "xdg-config" / "opencode"
        xdg.mkdir(parents=True)
        outside = self.other / "tui.jsonc"
        outside.write_text("{}\n", encoding="utf-8")
        os.symlink(str(outside), str(xdg / prep.TUI_ISOLATED_CONFIG))
        with self.assertRaises(prep.PrepError):
            prep.prepare_pane(**self._base())
        self.assertEqual(outside.read_text("utf-8"), "{}\n")


class FindInstalledTuiBridgeTests(unittest.TestCase):
    """TUI discovery reads only tui.json/tui.jsonc `plugin`, never cli.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src, self.status = fake_bridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tui_jsonc_bare_file_registration_accepted(self):
        found = prep.find_installed_tui_bridge(self.status)
        self.assertEqual(found["integration_id"], prep.TUI_INTEGRATION_ID)
        self.assertEqual(found["version"], 13)
        self.assertEqual(found["source_config"], "tui.jsonc")
        self.assertEqual(found["source_spec"], "./herdr-tui-session.js")
        self.assertEqual(Path(found["source_module"]).resolve(),
                         (self.tmp / "installed" / prep.TUI_BRIDGE_PLUGIN_NAME).resolve())

    def test_tui_json_variant_accepted(self):
        self.src, self.status = fake_bridge(self.tmp, config_name="tui.json")
        found = prep.find_installed_tui_bridge(self.status)
        self.assertEqual(found["source_config"], "tui.json")

    def test_cli_json_only_is_not_tui_registration(self):
        self.src, self.status = fake_bridge(self.tmp, register=False, server_cli=True)
        with self.assertRaises(prep.PrepError):
            prep.find_installed_tui_bridge(self.status)

    def test_injected_cli_config_text_is_not_tui_registration(self):
        self.src, self.status = fake_bridge(self.tmp, register=False)
        with self.assertRaises(prep.PrepError):
            prep.find_installed_tui_bridge(
                self.status,
                config_texts={"cli.json": json.dumps(
                    {"plugins": [f"./{prep.TUI_REGISTRATION_DIR}"]})})

    def test_directory_wrapper_in_tui_config_still_supported(self):
        self.src, self.status = fake_bridge(self.tmp, register=False)
        wrapper = (self.tmp / "installed" / prep.TUI_REGISTRATION_DIR
                   / prep.TUI_REGISTRATION_ENTRY)
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('export { default } from "../herdr-tui-session.js";\n',
                           encoding="utf-8")
        (self.tmp / "installed" / "tui.jsonc").write_text(
            json.dumps({"plugin": [f"./{prep.TUI_REGISTRATION_DIR}"]}), encoding="utf-8")
        found = prep.find_installed_tui_bridge(self.status)
        self.assertEqual(Path(found["source_module"]).resolve(), wrapper.resolve())


class TuiBridgeAvailableTests(unittest.TestCase):
    """`tui_bridge_available` validates the TUI config, not the server cli.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._src, status = fake_bridge(self.tmp)
        self.bridge_src = self.tmp / "installed" / prep.TUI_BRIDGE_PLUGIN_NAME
        self.config_home = self.tmp / "xdg-config"
        self.provisioned = prep.provision_tui_bridge_xdg(self.config_home, self.bridge_src)
        self.xdg = self.config_home / "opencode"
        self.tui_config = self.xdg / prep.TUI_ISOLATED_CONFIG

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_provision_writes_tui_jsonc_plugin_and_validates(self):
        self.assertTrue(self.tui_config.is_file())
        self.assertEqual(json.loads(self.tui_config.read_text("utf-8")),
                         {"plugin": ["./herdr-tui-session.js"]})
        self.assertFalse((self.xdg / "cli.json").exists())
        self.assertTrue(prep.tui_bridge_available(self.config_home))
        self.assertEqual(Path(self.provisioned["config"]), self.tui_config)
        self.assertEqual(self.provisioned["plugin"], "./herdr-tui-session.js")

    def test_missing_tui_config_is_unavailable(self):
        self.tui_config.unlink()
        self.assertFalse(prep.tui_bridge_available(self.config_home))

    def test_cli_json_only_is_unavailable(self):
        self.tui_config.unlink()
        (self.xdg / "cli.json").write_text(
            json.dumps({"plugins": ["./herdr-tui-session.js"]}), encoding="utf-8")
        self.assertFalse(prep.tui_bridge_available(self.config_home))

    def test_wrong_plugin_entry_is_unavailable(self):
        self.tui_config.write_text(json.dumps({"plugin": ["./other.js"]}), encoding="utf-8")
        self.assertFalse(prep.tui_bridge_available(self.config_home))

    def test_server_key_is_unavailable(self):
        self.tui_config.write_text(
            json.dumps({"plugins": ["./herdr-tui-session.js"]}), encoding="utf-8")
        self.assertFalse(prep.tui_bridge_available(self.config_home))

    def test_general_config_present_is_unavailable(self):
        (self.xdg / "opencode.json").write_text("{}\n", encoding="utf-8")
        self.assertFalse(prep.tui_bridge_available(self.config_home))

    def test_symlinked_tui_config_is_unavailable(self):
        outside = self.tmp / "outside-tui.jsonc"
        outside.write_text(json.dumps({"plugin": ["./herdr-tui-session.js"]}),
                           encoding="utf-8")
        self.tui_config.unlink()
        os.symlink(str(outside), str(self.tui_config))
        self.assertFalse(prep.tui_bridge_available(self.config_home))

    def test_missing_bridge_is_unavailable(self):
        (self.xdg / prep.TUI_BRIDGE_PLUGIN_NAME).unlink()
        self.assertFalse(prep.tui_bridge_available(self.config_home))


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

    def test_worker_targets_alloc_worktree_and_exact_identity_bridges(self):
        rep = self._prep()
        self.assertEqual(Path(rep["cwd"]).resolve(), self.worktree.resolve())
        self.assertTrue(rep["server_bridge_ready"])
        self.assertTrue(rep["tui_bridge_ready"])
        self.assertTrue(rep["identity_bridge_ready"])
        self.assertTrue(rep["bridge_ready"])
        xdg = Path(rep["sandbox"]) / "xdg-config" / "opencode"
        files = sorted(p.name for p in xdg.rglob("*") if p.is_file())
        self.assertEqual(files, [prep._sessreg.BRIDGE_PLUGIN_NAME,
                                 prep.TUI_BRIDGE_PLUGIN_NAME,
                                 prep.TUI_ISOLATED_CONFIG])
        self.assertFalse((xdg / "opencode.json").exists())
        self.assertFalse((xdg / "tui.json").exists())
        self.assertFalse((xdg / "cli.json").exists())
        cfg = json.loads((xdg / prep.TUI_ISOLATED_CONFIG).read_text("utf-8"))
        self.assertEqual(cfg["plugin"], ["./herdr-tui-session.js"])
        self.assertEqual((xdg / prep.TUI_BRIDGE_PLUGIN_NAME).read_text("utf-8"), TUI_SOURCE)
        self.assertEqual(rep["tui_bridge"]["integration_id"], prep.TUI_INTEGRATION_ID)
        self.assertEqual(rep["tui_bridge"]["version"], 13)
        self.assertEqual(rep["tui_bridge"]["source_config"], "tui.jsonc")
        self.assertEqual(rep["tui_bridge"]["isolated_config"], prep.TUI_ISOLATED_CONFIG)
        self.assertEqual(rep["tui_bridge"]["isolated_plugin"], "./herdr-tui-session.js")
        self.assertEqual(rep["pinned_config"]["status"], "written")

    def test_ceo_targets_project_root(self):
        rep = self._prep(role="ceo")
        self.assertEqual(Path(rep["cwd"]).resolve(), self.project.resolve())

    def test_dispatch_example_quotes_root_with_spaces(self):
        spaced = Path(tempfile.mkdtemp(prefix="m606 space "))
        try:
            root, _worktree, _project = make_world(spaced)
            src, status = fake_bridge(spaced)
            rep = prep.prepare_pane(role="worker", root=str(root), pane=DESIGNATED_PANE,
                                    base_env=dict(BASE_ENV), template=TEMPLATE,
                                    bridge_src=src, bridge_status_text=status)
            quoted = shlex.quote(str(root.resolve()))
            self.assertIn(f"--root {quoted}", rep["dispatch_example"])
            self.assertIn(f"--root {quoted}", rep["runner_validate"])
        finally:
            shutil.rmtree(spaced, ignore_errors=True)

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
