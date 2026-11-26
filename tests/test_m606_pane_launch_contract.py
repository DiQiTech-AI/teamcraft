"""M6-06 pane-launch ↔ native-session identity contract.

WHAT THIS PINS
    The runner only accepts a pane whose live identity exposes a native
    ``ses_`` id.  That id must originate from the preregistered OpenCode
    session (``--session <id>``), never be inferred from the model, the
    provider catalog, the pane id, or the operator's expectations.

    This module checks the launch side *and* the two Herdr integrators that can
    carry the id to Herdr:

    * ``herdr-agent-state.js`` (server plugin, integration ``opencode``)
      reports ``agent_session_id`` taken **verbatim** from the OpenCode event's
      ``properties.sessionID``.  It does **not** own the lifecycle of a bare
      interactive launch (``opencode --standalone <dir>``): its
      ``ownsLocalLifecycle()`` requires ``run`` or ``--mini``.
    * ``herdr-tui-session.js`` (TUI plugin, integration ``opencode-tui``)
      reports the *selected* session via ``pane.report_agent_session`` with
      ``session_start_source: "select"``; the id comes from
      ``api.route.current.params.sessionID`` — the same id passed to
      ``--session`` — never from model state.

    The offline tests are hermetic (no OpenCode, no Herdr, no network).  The
    opt-in live probe (``M606_LIVE_BRIDGE=1``) imports the **installed** v13
    plugins against a disposable Unix socket and asserts the exact wire frames;
    it uses a synthetic pane id/session id and never reads or mutates a live
    pane.

WHAT THE PREPARATION TOOL NOW DOES
    ``scripts/m606_prepare_opencode_pane.py`` verifies the installed
    ``opencode-tui`` v13 bridge and its local registration, copies only that
    bridge into the isolated XDG, and writes a minimal isolated ``tui.jsonc``
    registration (directory wrapper) that OpenCode v2.4.0 recognizes.  The live
    probe confirms the copied/registered artifact emits the preregistered id to
    a disposable socket; this is still a prepared-pane result, not a claim that
    a real Herdr pane has been observed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import m606_prepare_opencode_pane as prep  # noqa: E402


def _load_fixtures():
    """Reuse the disposable-world helpers already pinned by the prep suite."""
    spec = importlib.util.spec_from_file_location(
        "m606_pane_launch_fixtures",
        TESTS_DIR / "test_m606_prepare_opencode_pane.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_fx = _load_fixtures()
TEMPLATE = _fx.TEMPLATE
BASE_ENV = dict(_fx.BASE_ENV)
DESIGNATED_PANE = _fx.DESIGNATED_PANE

SERVER_BRIDGE = "herdr-agent-state.js"
TUI_BRIDGE = "herdr-tui-session.js"
#: Synthetic, disposable id.  Never a live session; no inference is claimed.
PREREGISTERED = "ses_panecontract00000000000000001"
PROBE_PANE = "m606probe:p1"

FORBIDDEN_LAUNCH_TOKENS = (
    "--prompt", "opencode run", "opencode mini", "send-keys", "agent prompt",
    "session.prompt", "session.command", "herdr pane new", "worktree add",
    "--consent-dispatch",
)


def tui_selection_bridge_ready(xdg_config_home: Path) -> bool:
    """True only when the isolated XDG has the copied bridge + supported registration."""
    xdg = Path(xdg_config_home) / "opencode"
    bridge = xdg / TUI_BRIDGE
    tui_config = xdg / "tui.jsonc"
    if not (bridge.is_file() and tui_config.is_file()):
        return False
    if (xdg / "opencode.json").exists():
        return False
    try:
        cfg = json.loads(tui_config.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    plugins = cfg.get("plugin") if isinstance(cfg, dict) else None
    return isinstance(plugins, list) and f"./{TUI_BRIDGE}" in plugins


def server_bridge_owns_local_lifecycle(argv: list[str]) -> bool:
    """Mirror the installed v13 ``ownsLocalLifecycle()`` (cross-checked live).

    Kept as a pure predicate so the default suite documents the rule without a
    host dependency; :class:`LiveBridgeWireTests` proves it against the actual
    plugin source.
    """
    args = list(argv)
    if "--" in args:
        args = args[:args.index("--")]
    if any(a == "--attach" or a.startswith("--attach=") for a in args):
        return False
    while args and (args[0] == "--print-logs" or args[0] == "--log-level"
                    or args[0].startswith("--log-level=")):
        args = args[2:] if args[0] == "--log-level" else args[1:]
    if not args:
        return False
    return args[0] == "run" or (args[0] not in {"serve", "web", "attach"}
                                 and "--mini" in args)


# ---------------------------------------------------------------------------
# Offline: the launch command binds the preregistered id and nothing else
# ---------------------------------------------------------------------------

class PaneLaunchContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="m606-launch-"))
        self.root, self.worktree, self.project = _fx.make_world(self.tmp)
        self.src, self.status = _fx.fake_bridge(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prep(self, **kw):
        kw.setdefault("role", "worker")
        kw.setdefault("root", str(self.root))
        kw.setdefault("pane", DESIGNATED_PANE)
        kw.setdefault("base_env", dict(BASE_ENV))
        kw.setdefault("template", TEMPLATE)
        kw.setdefault("bridge_src", self.src)
        kw.setdefault("bridge_status_text", self.status)
        return prep.prepare_pane(**kw)

    def test_preregistered_session_is_bound_verbatim(self) -> None:
        rep = self._prep(session_id=PREREGISTERED)
        cmd = rep["launch_command"]
        self.assertEqual(cmd.count("--session"), 1, cmd)
        self.assertIn(f"--session {PREREGISTERED}", cmd)
        # The operator supplies the id; prep neither derives nor rewrites it.
        self.assertEqual(rep["launch_command"].count(PREREGISTERED), 1)

    def test_without_preregistration_no_id_is_invented(self) -> None:
        cmd = self._prep()["launch_command"]
        self.assertNotIn("--session", cmd)
        self.assertIsNone(re.search(r"ses_[A-Za-z0-9]+", cmd), cmd)

    def test_launch_is_interactive_standalone_not_a_run(self) -> None:
        cmd = self._prep(session_id=PREREGISTERED)["launch_command"]
        self.assertIn("opencode --standalone", cmd)
        self.assertNotIn("opencode run", cmd)
        self.assertNotIn("opencode mini", cmd)
        for token in FORBIDDEN_LAUNCH_TOKENS:
            self.assertNotIn(token, cmd, f"{token!r} must not appear in a preview launch")

    def test_launch_env_isolated_and_no_cloud_or_user_config(self) -> None:
        cmd = self._prep()["launch_command"]
        self.assertTrue(cmd.startswith("env -i"), cmd)
        for leaked in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
                       "SOME_GENERAL_CONFIG"):
            self.assertNotIn(leaked, cmd)
        # Only the three documented Herdr vars survive; the live socket is
        # expanded at launch time, never baked into the preview.
        self.assertIn('HERDR_ENV="${HERDR_ENV:?', cmd)
        self.assertIn('HERDR_SOCKET_PATH="${HERDR_SOCKET_PATH:?', cmd)
        self.assertNotIn(BASE_ENV["HERDR_SOCKET_PATH"], cmd)
        self.assertIn(f"HERDR_PANE_ID={DESIGNATED_PANE}", cmd)
        self.assertNotIn("w9:p9Z", cmd)  # invoking pane is not inherited

    def test_isolated_xdg_contains_exact_identity_bridges_and_registration(self) -> None:
        rep = self._prep(session_id=PREREGISTERED)
        xdg = Path(rep["sandbox"]) / "xdg-config" / "opencode"
        files = sorted(p.name for p in xdg.rglob("*") if p.is_file())
        self.assertEqual(files, sorted([SERVER_BRIDGE, TUI_BRIDGE, "tui.jsonc"]))
        for general in ("opencode.json", "tui.json", "cli.json"):
            self.assertFalse((xdg / general).exists(), general)
        cfg = json.loads((xdg / "tui.jsonc").read_text("utf-8"))
        self.assertEqual(cfg["plugin"], [f"./{TUI_BRIDGE}"])
        self.assertTrue(rep["server_bridge_ready"])
        self.assertTrue(rep["tui_bridge_ready"])
        self.assertTrue(rep["identity_bridge_ready"])
        self.assertTrue(rep["bridge_ready"])
        self.assertTrue(tui_selection_bridge_ready(Path(rep["sandbox"]) / "xdg-config"))

    def test_bare_tui_launch_is_outside_the_server_bridge_lifecycle(self) -> None:
        # The bare TUI is still not owned by the *server* plugin, so the TUI
        # selection bridge is the one that must carry the preregistered id.
        self.assertFalse(server_bridge_owns_local_lifecycle(
            ["--standalone", str(self.worktree)]))
        self.assertTrue(server_bridge_owns_local_lifecycle(["run", "hello"]))
        self.assertTrue(server_bridge_owns_local_lifecycle(["--mini"]))

    def test_preview_contains_no_dispatch_or_live_db_write(self) -> None:
        rep = self._prep(session_id=PREREGISTERED)
        combined = "\n".join(str(v) for v in rep.values() if isinstance(v, str))
        self.assertNotIn("--consent-dispatch", rep["runner_validate"])
        self.assertIn("--consent-dispatch", rep["dispatch_example"])
        self.assertNotIn("teamcraft.db", combined)


# ---------------------------------------------------------------------------
# Opt-in live probe: installed bridge plugins against a disposable socket
# ---------------------------------------------------------------------------

PROBE_JS = r"""
import net from "node:net";
import { pathToFileURL } from "node:url";

const cfg = JSON.parse(process.env.M606_PROBE_CONFIG);
const frames = [];
const hooks = [];
const socketServer = net.createServer((socket) => {
  let buffer = "";
  socket.on("error", () => {});
  socket.on("data", (chunk) => {
    buffer += chunk.toString("utf8");
    let newline;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      try { frames.push(JSON.parse(line)); } catch { frames.push({ raw_line: line }); }
      try { socket.write("ok\n"); } catch {}
    }
  });
});
await new Promise((resolve, reject) => {
  socketServer.once("error", reject);
  socketServer.listen(cfg.socket, resolve);
});

const sid = cfg.sessionID;
try {
  if (cfg.mode === "tui") {
    const mod = await import(pathToFileURL(cfg.tui).href);
    const api = {
      route: { current: { name: "session", params: { sessionID: sid } } },
      state: { session: { get: (id) => (id === sid ? { id: sid } : undefined) } },
      client: { session: {
        get: async () => ({ data: { id: sid } }),
        status: async () => ({ data: {} }),
        message: async () => ({ data: { parts: [] } }),
      } },
      permission: { list: async () => ({ data: [] }) },
      question: { list: async () => ({ data: [] }) },
      event: { on: () => () => {} },
      lifecycle: { onDispose: () => {} },
    };
    await mod.default.tui(api);
  } else {
    const mod = await import(pathToFileURL(cfg.server).href);
    process.argv = ["node", "opencode", ...cfg.argv];
    const plugin = await mod.HerdrAgentStatePlugin();
    hooks.push(...Object.keys(plugin));
    if (plugin.event) {
      await plugin.event({ event: { type: "session.updated", properties: { sessionID: sid } } });
      await plugin.event({ event: { type: "session.status", properties: { sessionID: sid, status: { type: "idle" } } } });
      await plugin["chat.message"]({ sessionID: sid });
    }
  }
  await new Promise((resolve) => setTimeout(resolve, cfg.waitMs ?? 800));
} finally {
  socketServer.close();
}

process.stdout.write(JSON.stringify({ frames, hooks }), () => process.exit(0));
"""


def _bridge_host() -> dict | None:
    if os.environ.get("M606_LIVE_BRIDGE") != "1":
        return None
    node = shutil.which("node")
    opencode = shutil.which("opencode")
    if not node or not opencode:
        return None
    try:
        status = subprocess.run(["herdr", "integration", "status"],
                                capture_output=True, text=True, timeout=20,
                                check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (status.stdout or "") + (status.stderr or "")
    m = re.search(r"^opencode:\s*current\s*\(v\d+\)\s*\((?P<p>[^)]+)\)", text, re.M)
    if not m:
        return None
    server = Path(m.group("p"))
    candidates = (server.parent / TUI_BRIDGE, server.parent.parent / TUI_BRIDGE)
    tui = next((p for p in candidates if p.is_file()), None)
    if not (server.is_file() and tui):
        return None
    return {"node": node, "opencode": opencode, "server": server, "tui": tui,
            "status_text": text}


_LIVE = _bridge_host()


@unittest.skipUnless(_LIVE, "set M606_LIVE_BRIDGE=1 with node + installed v13 bridges")
class LiveBridgeWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = _LIVE
        self.tmp = Path(tempfile.mkdtemp(prefix="m606-bridge-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _probe(self, *, mode: str, server: Path, tui: Path | None = None,
               argv: tuple[str, ...] = (), wait_ms: int = 800) -> dict:
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        script = work / "probe.mjs"
        script.write_text(PROBE_JS, encoding="utf-8")
        sock = work / "herdr.sock"
        cfg = {"mode": mode, "socket": str(sock), "server": str(server),
               "tui": str(tui or ""), "argv": list(argv),
               "sessionID": PREREGISTERED, "waitMs": wait_ms}
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(work / "home"),
               "HERDR_ENV": "1", "HERDR_SOCKET_PATH": str(sock),
               "HERDR_PANE_ID": PROBE_PANE,
               "M606_PROBE_CONFIG": json.dumps(cfg)}
        proc = subprocess.run([self.host["node"], str(script)], cwd=str(work),
                              env=env, capture_output=True, text=True, timeout=40,
                              stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return json.loads(proc.stdout)

    def _assert_exact_id_only(self, frames: list[dict]) -> None:
        self.assertTrue(frames, "bridge emitted no frames")
        for frame in frames:
            params = frame.get("params", {})
            self.assertEqual(params.get("pane_id"), PROBE_PANE)
            self.assertEqual(params.get("source"), "herdr:opencode")
            self.assertEqual(params.get("agent"), "opencode")
            if "agent_session_id" in params:
                self.assertEqual(params["agent_session_id"], PREREGISTERED)
        blob = json.dumps(frames).lower()
        for inferred in ("omlx", "qwen", "provider", "model"):
            self.assertNotIn(inferred, blob)

    def test_tui_selection_reports_preregistered_id_verbatim(self) -> None:
        out = self._probe(mode="tui", server=self.host["server"], tui=self.host["tui"])
        self._assert_exact_id_only(out["frames"])
        selected = [f for f in out["frames"]
                    if f.get("method") == "pane.report_agent_session"]
        self.assertTrue(selected, out["frames"])
        for frame in selected:
            self.assertEqual(frame["params"].get("session_start_source"), "select")

    def test_server_bridge_declines_bare_tui_but_reports_run_events(self) -> None:
        bare = self._probe(mode="server", server=self.host["server"],
                           argv=("--standalone", str(self.tmp / "project")))
        self.assertEqual(bare["hooks"], [])
        self.assertEqual(bare["frames"], [])

        ran = self._probe(mode="server", server=self.host["server"],
                          argv=("run", "hello"))
        self.assertIn("event", ran["hooks"])
        self._assert_exact_id_only(ran["frames"])
        self.assertTrue(any(f.get("method") == "pane.report_agent_session"
                            for f in ran["frames"]), ran["frames"])

    def test_prepared_isolated_xdg_loads_registered_tui_bridge(self) -> None:
        root, _worktree, _project = _fx.make_world(self.tmp)
        rep = prep.prepare_pane(role="worker", root=str(root),
                                pane=DESIGNATED_PANE, base_env=dict(BASE_ENV),
                                template=TEMPLATE, bridge_src=str(self.host["server"]),
                                bridge_status_text=self.host["status_text"],
                                session_id=PREREGISTERED)
        xdg = Path(rep["sandbox"]) / "xdg-config" / "opencode"
        files = sorted(p.name for p in xdg.rglob("*") if p.is_file())
        self.assertEqual(files, sorted([SERVER_BRIDGE, TUI_BRIDGE, "tui.jsonc"]))
        self.assertTrue(rep["identity_bridge_ready"])
        self.assertTrue(tui_selection_bridge_ready(Path(rep["sandbox"]) / "xdg-config"))

        # OpenCode v2.4.0 must recognize the isolated registration as a TUI
        # plugin.  This is a bounded, prompt-free, model-free CLI read.
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(Path(rep["sandbox"]) / "home"),
               "XDG_CONFIG_HOME": str(Path(rep["sandbox"]) / "xdg-config"),
               "XDG_DATA_HOME": str(Path(rep["sandbox"]) / "xdg-data"),
               "XDG_STATE_HOME": str(Path(rep["sandbox"]) / "xdg-state"),
               "XDG_CACHE_HOME": str(Path(rep["sandbox"]) / "xdg-cache")}
        listed = subprocess.run([self.host["opencode"], "plugin", "list", "--standalone"],
                                cwd=str(work), env=env, capture_output=True, text=True,
                                timeout=40, stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(listed.returncode, 0, listed.stderr[-2000:])
        self.assertIn(TUI_BRIDGE, listed.stdout)
        self.assertNotIn("No plugins found", listed.stdout)

        # The copied, registered wrapper must emit the preregistered id verbatim
        # to a disposable socket (no pane, no model, no prompt).
        out = self._probe(mode="tui", server=xdg / "plugins" / SERVER_BRIDGE,
                          tui=xdg / TUI_BRIDGE)
        self._assert_exact_id_only(out["frames"])
        self.assertTrue(any(f.get("method") == "pane.report_agent_session"
                            for f in out["frames"]))


if __name__ == "__main__":
    unittest.main()
