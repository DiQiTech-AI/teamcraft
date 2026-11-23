"""M6-06 pre-dispatch native-session registration for OpenCode v2.4.0 + Herdr.

Verified facts this test locks in (from read-only CLI help, repo Herdr adapter
tests, and disposable-loopback probes):

* Herdr observes an OpenCode native session through the *installed bridge plugin*
  ``~/.config/opencode/plugins/herdr-agent-state.js`` (``herdr integration status``
  reports ``opencode: current (vN)``).  v13 of that plugin reads exactly three
  environment variables (lines 62-63 / 138-140 of the plugin):

      HERDR_ENV, HERDR_SOCKET_PATH, HERDR_PANE_ID

  An ``env -i`` launch strips them, and redirecting ``XDG_CONFIG_HOME`` hides the
  plugin — so a naive isolated probe silently has NO bridge.  The FIX is to retain
  ONLY those three Herdr variables and to make ONLY the verified installed bridge
  available inside the isolated XDG, WITHOUT importing general user config
  (``~/.config/opencode/opencode.json`` and its sibling plugins).

* Registration is a SEPARATE concern from runtime model selection.  The v2 API
  operation ``session.create`` (``opencode api session.create -d {}``) registers a
  native ``ses_`` session with **no message and no provider configured**, so any
  inference is structurally impossible.  Which model the pane uses *at runtime*
  (project ``opencode.json`` loopback pin + ``--model`` + operator attestation) is
  not part of registration and is never asserted here.

These tests are offline and CI-safe (no OpenCode/Herdr process, no network).  The
opt-in live test reproduces the verified route against a disposable sandbox while
synthesising the socket/pane so the live Herdr pane is never mutated.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Verified constants
# ---------------------------------------------------------------------------

#: The ONLY Herdr variables the installed v13 bridge plugin consumes.
HERDR_BRIDGE_VARS: tuple[str, ...] = ("HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID")

#: Non-Herdr environment a probe legitimately needs to run a CLI at all.
STRUCTURAL_ENV: tuple[str, ...] = ("PATH", "TERM", "LANG", "LC_ALL", "CI", "NO_COLOR")

#: Prefixes whose presence would let a pane reach a cloud provider (inference).
CLOUD_ENV_PREFIXES: tuple[str, ...] = (
    "OPENAI_", "ANTHROPIC_", "GOOGLE_", "AZURE_", "AWS_", "XAI_", "MISTRAL_", "GEMINI_",
)

#: v2 API operation IDs. ``session.create`` registers without a message; nothing
#: here may touch ``session.prompt`` / ``session.command`` (those are inference).
ROUTE_LIST = "session.list"
ROUTE_CREATE = "session.create"

BRIDGE_PLUGIN_NAME = "herdr-agent-state.js"


# ---------------------------------------------------------------------------
# Helpers (importable by the runner once coordinated)
# ---------------------------------------------------------------------------

def herdr_bridge_env(source: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Retain exactly the three Herdr bridge variables from ``source``.

    Returns ``(retained, complete)``.  Anything that is not a Herdr bridge var is
    dropped here, so general config / cloud credentials cannot leak in.
    """
    retained = {k: source[k] for k in HERDR_BRIDGE_VARS if k in source}
    return retained, set(HERDR_BRIDGE_VARS).issubset(retained)


def build_probe_env(base: dict[str, str], isolated: Path) -> dict[str, str]:
    """Minimal, isolated env: structural vars + the three Herdr vars only.

    Redirects HOME and every XDG dir into ``isolated`` so no general user config is
    read.  Raises if any cloud-credential variable survives (defense in depth).
    """
    retained, _complete = herdr_bridge_env(base)
    env = {k: v for k, v in base.items() if k in STRUCTURAL_ENV}
    env.update(retained)
    env.update({
        "HOME": str(isolated / "home"),
        "XDG_CONFIG_HOME": str(isolated / "xdg-config"),
        "XDG_DATA_HOME": str(isolated / "xdg-data"),
        "XDG_STATE_HOME": str(isolated / "xdg-state"),
        "XDG_CACHE_HOME": str(isolated / "xdg-cache"),
    })
    leaked = [k for k in env if k.upper().startswith(CLOUD_ENV_PREFIXES)]
    if leaked:
        raise AssertionError(f"cloud-credential env leaked into probe env: {leaked}")
    return env


def installed_bridge_path(status_text: str) -> str | None:
    """Parse ``herdr integration status`` for the verified installed OpenCode bridge.

    Only a ``current`` (installed, versioned) line qualifies; ``not installed``
    yields ``None`` so a probe never silently runs without the bridge.
    """
    m = re.search(r"^opencode:\s*current\s*\(v\d+\)\s*\((?P<p>[^)]+)\)", status_text, re.M)
    return m.group("p") if m else None


def provision_bridge_xdg(isolated_config_home: Path, plugin_src: Path) -> Path:
    """Make ONLY the verified installed bridge available inside isolated XDG.

    Copies just ``herdr-agent-state.js`` and refuses to run if general user config
    (``opencode.json``) would be present — isolation must exclude it.
    """
    general = isolated_config_home / "opencode" / "opencode.json"
    if general.exists():
        raise AssertionError(f"general user config must not be imported: {general}")
    dest = isolated_config_home / "opencode" / "plugins" / BRIDGE_PLUGIN_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(plugin_src), str(dest))
    return dest


def bridge_available(isolated_config_home: Path) -> bool:
    xdg = Path(isolated_config_home) / "opencode"
    plugin = (xdg / "plugins" / BRIDGE_PLUGIN_NAME).exists()
    general = (xdg / "opencode.json").exists()
    return plugin and not general


def _first_session_id(obj: object) -> str | None:
    stack: list[object] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str) and cur.startswith("ses_"):
            return cur
    return None


def _list_ids(json_obj: object) -> list[str]:
    arr = json_obj if isinstance(json_obj, list) else (
        json_obj.get("data") if isinstance(json_obj, dict) else None)
    if not isinstance(arr, list):
        return []
    return [x["id"] for x in arr if isinstance(x, dict) and isinstance(x.get("id"), str)]


def ensure_native_session(runner, project_dir: str) -> str:
    """Observe a native session id for ``project_dir`` with zero prompt.

    ``runner(op_id, *argv) -> (exit_code, json_obj)`` is injected (real CLI or fake)
    so this logic is testable offline.  It lists existing sessions for the cwd and,
    only if none exist, registers one via ``session.create`` with an EMPTY body —
    never a message.  It never inspects providers/models (registration != model).
    """
    code, listed = runner(ROUTE_LIST)
    if code == 0:
        ids = _list_ids(listed)
        if ids:
            return ids[0]
    code, created = runner(ROUTE_CREATE, "-d", "{}")
    if code != 0:
        raise RuntimeError(f"{ROUTE_CREATE} failed (exit {code}); cannot register a native session")
    new_id = _first_session_id(created)
    if not new_id:
        raise RuntimeError(f"{ROUTE_CREATE} returned no native session id")
    return new_id


# ---------------------------------------------------------------------------
# Offline contract tests (no OpenCode/Herdr process)
# ---------------------------------------------------------------------------

class BridgeEnvTests(unittest.TestCase):
    BASE = {
        "PATH": "/usr/bin:/bin", "TERM": "xterm", "CI": "1",
        "HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/run/herdr/herdr.sock",
        "HERDR_PANE_ID": "w3:p2Q",
        # noise that must NOT survive:
        "OPENAI_API_KEY": "sk-secret", "ANTHROPIC_API_KEY": "sk-secret",
        "SOME_GENERAL_CONFIG": "x", "GOOGLE_API_KEY": "g",
    }

    def test_retains_only_the_three_herder_vars(self) -> None:
        retained, complete = herdr_bridge_env(self.BASE)
        self.assertEqual(retained, {"HERDR_ENV": "1",
                                   "HERDR_SOCKET_PATH": "/run/herdr/herdr.sock",
                                   "HERDR_PANE_ID": "w3:p2Q"})
        self.assertTrue(complete)

    def test_probe_env_carries_no_cloud_or_general_config(self) -> None:
        env = build_probe_env(self.BASE, Path("/tmp/m606-iso"))
        self.assertTrue(all(v in env for v in HERDR_BRIDGE_VARS))
        self.assertFalse([k for k in env if k.upper().startswith(CLOUD_ENV_PREFIXES)])
        self.assertNotIn("SOME_GENERAL_CONFIG", env)

    def test_missing_bridge_var_is_reported_incomplete(self) -> None:
        partial = {k: v for k, v in self.BASE.items() if k != "HERDR_PANE_ID"}
        _retained, complete = herdr_bridge_env(partial)
        self.assertFalse(complete)

    def test_cloud_keys_are_stripped_not_inherited(self) -> None:
        # Whitelisting means a cloud key present in the base NEVER survives into the
        # probe env (stronger than rejecting at the end): isolation is by inclusion.
        env = build_probe_env({**self.BASE, "OPENAI_API_KEY": "sk-x"}, Path("/tmp/x"))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertFalse([k for k in env if k.upper().startswith(CLOUD_ENV_PREFIXES)])


class BridgeProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.isolated = self.tmp / "xdg-config"
        self.plugin_src = self.tmp / "installed" / BRIDGE_PLUGIN_NAME
        self.plugin_src.parent.mkdir(parents=True)
        self.plugin_src.write_text("// verified v13 bridge\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_provision_places_only_the_verified_bridge(self) -> None:
        dest = provision_bridge_xdg(self.isolated, self.plugin_src)
        self.assertTrue(dest.exists())
        files = sorted(p.name for p in (self.isolated / "opencode").rglob("*") if p.is_file())
        self.assertEqual(files, [BRIDGE_PLUGIN_NAME])
        self.assertTrue(bridge_available(self.isolated))

    def test_refuses_when_general_config_present(self) -> None:
        (self.isolated / "opencode").mkdir(parents=True)
        (self.isolated / "opencode" / "opencode.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(AssertionError):
            provision_bridge_xdg(self.isolated, self.plugin_src)


class InstalledBridgePathTests(unittest.TestCase):
    def test_parses_current_versioned_bridge(self) -> None:
        txt = ("codex: current (v8) (/Users/x/.codex/herdr-agent-state.sh)\n"
               "opencode: current (v13) (/Users/x/.config/opencode/plugins/herdr-agent-state.js)\n")
        self.assertEqual(installed_bridge_path(txt),
                         "/Users/x/.config/opencode/plugins/herdr-agent-state.js")

    def test_none_when_not_installed(self) -> None:
        txt = "opencode: not installed (/Users/x/.config/opencode/plugins/herdr-agent-state.js)\n"
        self.assertIsNone(installed_bridge_path(txt))


class RegistrationContractTests(unittest.TestCase):
    """Registration is observable with zero prompt and independent of model."""

    def test_registers_via_create_with_empty_body_no_prompt(self) -> None:
        calls: list[tuple] = []

        def fake_runner(op, *argv):
            calls.append((op, *argv))
            if op == ROUTE_LIST:
                return 0, {"data": []}          # no session yet for this cwd
            if op == ROUTE_CREATE:
                return 0, {"data": {"id": "ses_probe00000000000000000000a1"}}
            raise AssertionError(f"unexpected op {op!r}")

        sid = ensure_native_session(fake_runner, "/tmp/m606-proj")
        self.assertTrue(sid.startswith("ses_"))
        self.assertEqual(calls, [(ROUTE_LIST,), (ROUTE_CREATE, "-d", "{}")])
        # No inference path, and the create body carries no message/model:
        self.assertNotIn("session.prompt", [c[0] for c in calls])
        self.assertNotIn("session.command", [c[0] for c in calls])

    def test_observes_existing_session_without_creating(self) -> None:
        calls: list[tuple] = []

        def fake_runner(op, *argv):
            calls.append((op, *argv))
            return 0, {"data": [{"id": "ses_existing000000000000000000000"}]}

        sid = ensure_native_session(fake_runner, "/tmp/m606-proj")
        self.assertEqual(sid, "ses_existing000000000000000000000")
        self.assertEqual(calls, [(ROUTE_LIST,)])  # no create needed

    def test_registration_never_requires_a_model(self) -> None:
        # Provider-less config is normal at registration; ensure_native_session must
        # not read providers/models and must still yield a native id.
        def fake_runner(op, *argv):
            if op == ROUTE_LIST:
                return 0, {"data": []}
            return 0, {"id": "ses_noprovider0000000000000000000"}
        sid = ensure_native_session(fake_runner, "/tmp/m606-proj")
        self.assertTrue(sid.startswith("ses_"))


# ---------------------------------------------------------------------------
# Opt-in live probe (disposable sandbox; live Herdr pane never mutated)
# ---------------------------------------------------------------------------

_LIVE_READY = (shutil.which("opencode") is not None
              and all(os.environ.get(k) for k in HERDR_BRIDGE_VARS)
              and os.environ.get("M606_LIVE_BRIDGE") == "1")


@unittest.skipUnless(_LIVE_READY,
                    "set M606_LIVE_BRIDGE=1 with opencode + HERDR_* present to run")
class LiveRegistrationTest(unittest.TestCase):
    def test_registers_and_observes_native_session_no_prompt(self) -> None:
        status = subprocess.run(["herdr", "integration", "status"],
                               capture_output=True, text=True, timeout=30).stdout
        src = installed_bridge_path(status)
        self.assertIsNotNone(src, "verified installed OpenCode bridge not found")

        tmp = Path(tempfile.mkdtemp())
        try:
            isolated = tmp / "xdg-config"
            provision_bridge_xdg(isolated, Path(str(src)))
            self.assertTrue(bridge_available(isolated))
            env = build_probe_env(os.environ, tmp)
            # Safety: synthesise socket/pane so the live Herdr pane is not mutated.
            env["HERDR_SOCKET_PATH"] = str(tmp / "dead.sock")
            env["HERDR_PANE_ID"] = "m606-probe:p1"
            proj = tmp / "proj"; proj.mkdir()

            def runner(op, *argv, timeout=30):
                r = subprocess.run(["opencode", "--standalone", "api", op, *argv],
                                 cwd=str(proj), env=env, capture_output=True,
                                 text=True, timeout=timeout, stdin=subprocess.DEVNULL)
                obj = None
                m = re.search(r"(\{.*\}|\[.*\])", (r.stdout or "") + (r.stderr or ""), re.S)
                if m:
                    try:
                        obj = json.loads(m.group(1))
                    except Exception:
                        obj = None
                return r.returncode, obj

            sid = ensure_native_session(runner, str(proj))
            self.assertTrue(sid.startswith("ses_"))
            # Provider config absent => registration used no model route.
            self.assertFalse((isolated / "opencode" / "opencode.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
