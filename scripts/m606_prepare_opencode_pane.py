#!/usr/bin/env python3
"""M6-06 option-B — OpenCode pane *preparation* (prompt-free, isolated, preview-only).

The runner (`scripts/m606_opencode_e2e.py`) stays the sole validator; its guard
semantics are **unchanged**.  This tool only *prepares* disposable inputs a human
needs to run one role's OpenCode pane.  It is a PREVIEW: it never dispatches.

Hard guarantees (validated **before any write**):

* ``--root`` validated by ``m606_opencode_e2e.validate_root`` (outside repo/live
  runtime, journal-required when resuming);
* ``--cwd`` and the handoff ``allocated_cwd`` must resolve **inside** the run root
  and equal the exact role directory (worker = allocated worktree, ceo = project
  root); symlink/``..`` escape is rejected;
* ``--sandbox`` must resolve inside the run root (symlink-aware) before any write;
* any existing ``opencode.json`` must be the **actual** loopback-only pin
  (only provider ``omlx``, loopback baseURL, pinned model, no credential) — never
  silently trusted, **never overwritten**; otherwise the run aborts;
* session ids must match ``^ses_[A-Za-z0-9]+$`` or are rejected;
* both identity bridges must be the verified **installed** integrations: the
  server plugin path from ``herdr integration status`` and the ``opencode-tui``
  v13+ bridge registered as a local directory plugin.  Missing, unregistered,
  symlinked, or version-mismatched TUI bridges abort **before any write**;
* the isolated XDG receives only the two bridge files, the local TUI wrapper, and
  a minimal ``cli.json`` registration — never the user's general config;
* the emitted launch command uses ``env -i`` (no inherited config), explicit
  necessary vars only, shell-quoted values, and **binds ``HERDR_PANE_ID`` to the
  designated pane** (socket/env preserved from the invoking pane via ``${VAR:?}``).

It never launches/closes a pane, sends a prompt, infers, inspects credentials, or
touches a live ``teamcraft`` DB.  Session ids are reported masked + as a ref; the
authoritative id is read read-only via ``observe``.  ``runner_validate`` is a
dispatch-refusing preview; ``dispatch_example`` is a clearly separated, operator-
conscious action that prep itself never runs.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import m606_opencode_e2e as runner  # noqa: E402  (guard semantics reused, never edited)


def _load_session_registration():
    spec = importlib.util.spec_from_file_location(
        "m606_session_reg", REPO_ROOT / "tests" / "test_m606_session_registration.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # the *tested* registration primitives
    return mod


_sessreg = _load_session_registration()

PANE_RE = re.compile(r"^w\d+:p\d+[0-9A-Za-z]*$")
SESSION_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
ROLES = ("worker", "ceo")
DEFAULT_MODEL = runner.DEFAULT_MODEL
OMLX_URL = runner.LOOPBACK_OMLX_URL
TEMPLATE = REPO_ROOT / "opencode.example.json"

# --- isolated TUI identity bridge (verified installed integration only) --------
TUI_BRIDGE_PLUGIN_NAME = "herdr-tui-session.js"
TUI_REGISTRATION_DIR = "herdr-opencode"
TUI_REGISTRATION_ENTRY = "tui.js"
TUI_INTEGRATION_ID = "opencode-tui"
TUI_MIN_VERSION = 13
TUI_CONFIG_NAMES = ("cli.json", "tui.json", "tui.jsonc")
MAX_BRIDGE_BYTES = 1 << 20

_INTEGRATION_ID_RE = re.compile(r"^//\s*HERDR_INTEGRATION_ID=([A-Za-z0-9_.-]+)\s*$", re.M)
_INTEGRATION_VERSION_RE = re.compile(r"^//\s*HERDR_INTEGRATION_VERSION=(\d+)\s*$", re.M)


class PrepError(RuntimeError):
    pass


def _mask(sid: str) -> str:
    return f"{sid[:5]}…{sid[-4:]}" if sid and len(sid) > 9 else str(sid)


def _ref(sid: str) -> str:
    return "sha256:" + runner.sha256_bytes(sid.encode("utf-8"))[:12]


def _within_root(target: Path, root_real: Path, label: str) -> Path:
    """Resolve ``target`` (symlinks included) and require it inside ``root_real``."""
    rp = Path(target).resolve()
    if rp != root_real and not str(rp).startswith(str(root_real) + os.sep):
        raise PrepError(f"{label} escapes the disposable run root: {str(target)!r}")
    return rp


def _assert_loopback_pin(cfg: dict, model: str, omlx_url: str, source: str) -> None:
    runner.validate_loopback(omlx_url)
    if cfg.get("model") != f"omlx/{model}":
        raise PrepError(f"{source}: model {cfg.get('model')!r} != omlx/{model}")
    prov = cfg.get("provider", {})
    if set(prov) != {"omlx"}:
        raise PrepError(f"{source}: must declare only loopback omlx, got {sorted(prov)}")
    base = prov["omlx"].get("options", {}).get("baseURL")
    if base != omlx_url:
        raise PrepError(f"{source}: baseURL {base!r} != loopback {omlx_url!r}")
    if re.search(r"apiKey|api_key|access[_-]?token|sk-[A-Za-z0-9]{6,}", json.dumps(cfg)):
        raise PrepError(f"{source}: must contain no credential")


def pinned_loopback_config(template: Path = TEMPLATE, model: str = DEFAULT_MODEL,
                          omlx_url: str = OMLX_URL) -> dict:
    cfg = json.loads(Path(template).read_text("utf-8"))
    _assert_loopback_pin(cfg, model, omlx_url, source=str(template))
    cfg["provider"]["omlx"]["options"]["baseURL"] = omlx_url
    cfg["model"] = f"omlx/{model}"
    return cfg


def install_pinned_config(cwd: Path, template: Path = TEMPLATE, model: str = DEFAULT_MODEL,
                         omlx_url: str = OMLX_URL) -> dict:
    """Validate-or-write loopback pin. Never overwrite; existing must be valid."""
    dest = Path(cwd) / "opencode.json"
    if dest.exists():
        raw = dest.read_text("utf-8")
        try:
            cur = json.loads(raw)
        except Exception as exc:  # never silently trust a malformed file
            raise PrepError(f"existing {dest} is not valid JSON; refusing to trust/overwrite") from exc
        _assert_loopback_pin(cur, model, omlx_url, source=f"existing {dest}")
        return {"status": "reused-validated", "written": False, "path": str(dest)}
    cfg = pinned_loopback_config(template, model=model, omlx_url=omlx_url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return {"status": "written", "written": True, "path": str(dest)}


def _default_bridge_status(timeout: float = 20.0) -> str:
    r = subprocess.run(["herdr", "integration", "status"],
                       capture_output=True, text=True, timeout=timeout, check=False)
    return (r.stdout or "") + (r.stderr or "")


def _bounded_text(path: Path, limit: int = MAX_BRIDGE_BYTES) -> str:
    """Read a verified module/config as UTF-8 without following a symlink."""
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise PrepError(f"not a regular file: {p}")
    if p.stat().st_size > limit:
        raise PrepError(f"file too large to verify ({p.stat().st_size} > {limit}): {p}")
    try:
        return p.read_text("utf-8")
    except UnicodeDecodeError as exc:
        raise PrepError(f"not UTF-8 text: {p}") from exc


def verify_tui_bridge(path: Path) -> dict:
    """Accept only the installed ``opencode-tui`` v13+ selection bridge.

    Provenance is the managed header plus the operations the bridge must emit;
    no user config or credential value is read, printed, or copied.
    """
    p = Path(path)
    text = _bounded_text(p)
    ids = _INTEGRATION_ID_RE.findall(text)
    versions = _INTEGRATION_VERSION_RE.findall(text)
    if ids != [TUI_INTEGRATION_ID]:
        raise PrepError(f"{p}: not the verified {TUI_INTEGRATION_ID} bridge")
    if not versions:
        raise PrepError(f"{p}: missing HERDR_INTEGRATION_VERSION marker")
    version = max(int(v) for v in versions)
    if version < TUI_MIN_VERSION:
        raise PrepError(f"{p}: {TUI_INTEGRATION_ID} v{version} < v{TUI_MIN_VERSION}")
    required = ("pane.report_agent_session", "agent_session_id",
                "session_start_source", "route.current")
    missing = [token for token in required if token not in text]
    if missing:
        raise PrepError(f"{p}: bridge is missing required operations {missing}")
    return {"path": str(p.resolve()), "integration_id": TUI_INTEGRATION_ID,
            "version": version}


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments plus trailing commas outside strings."""
    out: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            end = text.find("\n", i)
            if end == -1:
                break
            i = end
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", cleaned)


def _local_plugin_specs(name: str, text: str) -> list[str]:
    """Extract local plugin specifiers from a CLI/TUI config without echoing it."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        try:
            obj = json.loads(_strip_jsonc(text))
        except json.JSONDecodeError as exc:
            raise PrepError(f"{name}: is not valid JSON/JSONC") from exc
    if not isinstance(obj, dict):
        raise PrepError(f"{name}: plugin config must be an object")
    raw = obj.get("plugins", obj.get("plugin", []))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise PrepError(f"{name}: 'plugins'/'plugin' must be a list")
    specs: list[str] = []
    for item in raw:
        spec = item if isinstance(item, str) else (
            item[0] if isinstance(item, list) and item and isinstance(item[0], str) else None)
        if spec and (spec.startswith("./") or spec.startswith("../")):
            specs.append(spec)
    return specs


def _registered_tui_module(spec: str, config_root: Path) -> Path | None:
    """Resolve a local plugin spec to its TUI entrypoint, refusing escapes.

    Only a directory entrypoint is accepted: ``opencode plugin list`` ignores a
    bare ``./herdr-tui-session.js`` file even though Node can import it.
    """
    root = Path(config_root).resolve()
    current = root
    for part in Path(spec).parts:
        if part == ".":
            continue
        current = (current.parent if part == ".." else current / part)
        if current.is_symlink():
            raise PrepError(f"plugin registration must not traverse a symlink: {spec}")
    target = current.resolve()
    if target != root and not str(target).startswith(str(root) + os.sep):
        raise PrepError(f"plugin registration escapes the OpenCode config dir: {spec}")
    if not target.is_dir():
        return None
    for entry in (TUI_REGISTRATION_ENTRY, "index.js", "index.mjs"):
        candidate = target / entry
        if candidate.exists():
            if candidate.is_symlink() or not candidate.is_file():
                raise PrepError(f"plugin entry must be a regular file: {spec}/{entry}")
            return candidate
    return None


def _module_references_bridge(module: Path, bridge: Path) -> bool:
    if module.resolve() == bridge.resolve():
        return False  # a bare file is not a supported TUI registration
    text = _bounded_text(module)
    name = Path(bridge).name
    return name in text and ("export" in text or "require" in text)


def find_installed_tui_bridge(status_text: str | None = None, *,
                              config_root: Path | str | None = None,
                              config_texts: dict[str, str] | None = None) -> dict:
    """Locate and verify the installed TUI bridge *and* its registration.

    The server bridge's ``herdr integration status`` path anchors discovery.  The
    TUI bridge is a sibling of the OpenCode config root; registration must be a
    local directory plugin whose entrypoint re-exports that exact bridge.  User
    config is parsed in memory only and never returned or copied.
    """
    text = status_text if status_text is not None else _default_bridge_status()
    server = _sessreg.installed_bridge_path(text)
    if not server:
        raise PrepError("verified installed OpenCode server bridge not found")
    server_path = Path(server)
    root = Path(config_root).resolve() if config_root else (
        server_path.parent.parent if server_path.parent.name == "plugins"
        else server_path.parent).resolve()
    candidates = [root / TUI_BRIDGE_PLUGIN_NAME,
                  server_path.parent / TUI_BRIDGE_PLUGIN_NAME]
    bridge = next((p for p in candidates if p.exists()), None)
    if bridge is None:
        raise PrepError(f"verified installed TUI bridge not found under {root}")
    verified = verify_tui_bridge(bridge)

    if config_texts is None:
        config_texts = {}
        for name in TUI_CONFIG_NAMES:
            candidate = root / name
            if candidate.exists():
                config_texts[name] = _bounded_text(candidate)
    if not config_texts:
        raise PrepError("installed TUI bridge has no local plugin registration")

    for name, cfg_text in config_texts.items():
        for spec in _local_plugin_specs(name, cfg_text):
            module = _registered_tui_module(spec, root)
            if module and _module_references_bridge(module, bridge):
                return {**verified, "config_root": str(root),
                        "source_config": name, "source_spec": spec,
                        "source_module": str(module)}
    raise PrepError("installed TUI bridge is not registered as a supported local TUI plugin")


def provision_tui_bridge_xdg(isolated_config_home: Path, bridge_src: Path) -> dict:
    """Copy only the verified TUI bridge and write its isolated registration."""
    verify_tui_bridge(bridge_src)
    xdg = Path(isolated_config_home) / "opencode"
    general_config = xdg / "opencode.json"
    if general_config.exists() or general_config.is_symlink():
        raise PrepError(f"general user config must not be imported: {general_config}")
    dest_bridge = xdg / TUI_BRIDGE_PLUGIN_NAME
    wrapper = xdg / TUI_REGISTRATION_DIR / TUI_REGISTRATION_ENTRY
    cli_config = xdg / "cli.json"
    for target in (dest_bridge, wrapper, cli_config):
        if target.is_symlink():
            raise PrepError(f"isolated bridge path must not be a symlink: {target}")
    dest_bridge.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(bridge_src), str(dest_bridge))
    verify_tui_bridge(dest_bridge)   # byte-copy integrity, not just existence
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        f'export {{ default }} from "../{TUI_BRIDGE_PLUGIN_NAME}";\n',
        encoding="utf-8")
    cli_config.write_text(json.dumps({
        "$schema": "https://opencode.ai/v2/cli.json",
        "plugins": [f"./{TUI_REGISTRATION_DIR}"],
    }, indent=2) + "\n", encoding="utf-8")
    return {"bridge": str(dest_bridge), "registration": str(wrapper),
            "config": str(cli_config), "plugin": f"./{TUI_REGISTRATION_DIR}"}


def tui_bridge_available(isolated_config_home: Path) -> bool:
    """True only when the isolated XDG has the bridge, wrapper, and registration."""
    xdg = Path(isolated_config_home) / "opencode"
    bridge = xdg / TUI_BRIDGE_PLUGIN_NAME
    wrapper = xdg / TUI_REGISTRATION_DIR / TUI_REGISTRATION_ENTRY
    cli_config = xdg / "cli.json"
    if not (bridge.is_file() and wrapper.is_file() and cli_config.is_file()):
        return False
    if (xdg / "opencode.json").exists():
        return False
    try:
        verify_tui_bridge(bridge)
        cfg = json.loads(cli_config.read_text("utf-8"))
        wrapper_text = wrapper.read_text("utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PrepError):
        return False
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    return (isinstance(plugins, list)
            and f"./{TUI_REGISTRATION_DIR}" in plugins
            and TUI_BRIDGE_PLUGIN_NAME in wrapper_text)


def launch_command(role: str, pane: str, cwd: Path, sandbox: Path, base_env,
                  session_id: str | None) -> tuple[str, dict]:
    """Compose the operator's prompt-free launch command, shell-quoted.

    ``env -i`` clears the environment (no inherited cloud/general config).  PATH is
    inherited so the opencode binary resolves; HERDR_ENV/SOCKET_PATH are preserved
    from the invoking pane via ``${VAR:?}`` (fail outside a pane); HERDR_PANE_ID is
    **bound to the designated pane**; HOME/XDG are isolated to the run sandbox.
    """
    _sessreg.build_probe_env(base_env, sandbox)  # reuse tested env classification
    parts = ["env", "-i", 'PATH="$PATH"',
             'HERDR_ENV="${HERDR_ENV:?not-in-designated-pane}"',
             'HERDR_SOCKET_PATH="${HERDR_SOCKET_PATH:?missing-herdr-socket}"',
             f"HERDR_PANE_ID={shlex.quote(pane)}",
             f"HOME={shlex.quote(str(sandbox / 'home'))}",
             f"XDG_CONFIG_HOME={shlex.quote(str(sandbox / 'xdg-config'))}",
             f"XDG_DATA_HOME={shlex.quote(str(sandbox / 'xdg-data'))}",
             f"XDG_STATE_HOME={shlex.quote(str(sandbox / 'xdg-state'))}",
             f"XDG_CACHE_HOME={shlex.quote(str(sandbox / 'xdg-cache'))}",
             "opencode", "--standalone"]
    if session_id:
        parts += ["--session", shlex.quote(session_id)]
    parts.append(shlex.quote(str(cwd)))
    return " ".join(parts), {"retained_herder_vars": list(_sessreg.HERDR_BRIDGE_VARS)}


def observe_native_id(pane: str, *, reveal: bool = False, herdr=None) -> dict:
    client = herdr or runner.Herdr(allow_dispatch=False)  # guards intact; cannot dispatch
    ident = client.identity(pane)                          # raises if no native session/agent
    sid = ident["session_id"]
    out = {"pane_id": pane, "agent": ident.get("agent"), "session_ref": _ref(sid)}
    out["session_id"] = sid if reveal else _mask(sid)
    return out


def prepare_pane(*, role: str, root: str, pane: str, cwd: str | None = None,
                 sandbox: str | None = None, model: str = DEFAULT_MODEL,
                 omlx_url: str = OMLX_URL, base_env=None, session_id: str | None = None,
                 template: Path = TEMPLATE, bridge_status_text: str | None = None,
                 bridge_src: str | None = None, allow_non_temp_root: bool = False,
                 with_proof: bool = False, proof_runner=None) -> dict:
    base_env = os.environ if base_env is None else base_env

    # ---- validate BEFORE any write --------------------------------------------
    if role not in ROLES:
        raise PrepError(f"--role must be one of {ROLES}, got {role!r}")
    if not pane or not PANE_RE.match(pane):
        raise PrepError(f"--herdr-pane must be a dedicated pane id like w1:p2, got {pane!r}")
    if session_id is not None and not SESSION_RE.match(session_id):
        raise PrepError(f"--session-id must match {SESSION_RE.pattern}, got {session_id!r}")

    root_path = runner.validate_root(str(root), allow_non_temp=allow_non_temp_root, for_resume=True)
    root_real = root_path.resolve()
    handoff_path = root_path / "evidence" / "handoff.json"
    if not handoff_path.is_file():
        raise PrepError(f"{handoff_path} not found; prep targets a phase-1 handoff world")
    handoff = json.loads(handoff_path.read_text("utf-8"))

    attempt = handoff.get("attempt_id")
    alloc = handoff.get("allocated_cwd")
    if not attempt or not alloc:
        raise PrepError("handoff.json missing attempt_id/allocated_cwd")
    alloc_real = _within_root(Path(alloc), root_real, "handoff allocated_cwd")
    project_real = _within_root(root_path / "project", root_real, "project root")
    expected = alloc_real if role == "worker" else project_real

    if cwd is not None:
        cwd_real = _within_root(Path(cwd), root_real, "--cwd")
        if cwd_real != expected:
            raise PrepError(f"--cwd must be the exact {role} directory {expected}, got {cwd_real}")
        role_cwd = cwd_real
    else:
        role_cwd = expected
    if not role_cwd.exists():
        raise PrepError(f"role cwd does not exist: {role_cwd}")

    if sandbox is not None:
        sandbox_path = _within_root(Path(sandbox), root_real, "--sandbox")
    else:
        sandbox_path = _within_root(root_path / "pane-prep" / role, root_real, "sandbox")

    # Refuse an existing general config before even creating sandbox dirs.
    general_config = sandbox_path / "xdg-config" / "opencode" / "opencode.json"
    if general_config.exists() or general_config.is_symlink():
        raise PrepError(
            f"isolated XDG already contains general config; refusing before write: {general_config}")

    # Resolve and verify BOTH identity bridges before any filesystem write.
    status = bridge_status_text if bridge_status_text is not None else _default_bridge_status()
    installed = _sessreg.installed_bridge_path(status)
    if not installed:
        raise PrepError("verified installed OpenCode bridge not found; refusing an unbridged pane")
    installed_path = Path(installed)
    if installed_path.is_symlink() or not installed_path.is_file():
        raise PrepError(
            f"reported server bridge is not a regular non-symlink file: {installed_path}")
    if bridge_src is not None:
        requested = Path(bridge_src)
        if requested.is_symlink():
            raise PrepError(f"--bridge-src must not be a symlink: {requested}")
        if requested.resolve() != installed_path.resolve():
            raise PrepError(
                f"--bridge-src {requested} is not the current installed bridge "
                f"reported by herdr: {installed_path}")
        src = requested
    else:
        src = installed_path
    tui = find_installed_tui_bridge(status)

    server_target = (sandbox_path / "xdg-config" / "opencode" / "plugins"
                     / _sessreg.BRIDGE_PLUGIN_NAME)
    tui_target = sandbox_path / "xdg-config" / "opencode" / TUI_BRIDGE_PLUGIN_NAME
    tui_wrapper = (sandbox_path / "xdg-config" / "opencode" / TUI_REGISTRATION_DIR
                   / TUI_REGISTRATION_ENTRY)
    tui_cli = sandbox_path / "xdg-config" / "opencode" / "cli.json"
    mutable_paths = [
        *(sandbox_path / name for name in
          ("home", "xdg-config", "xdg-data", "xdg-cache", "xdg-state")),
        server_target, tui_target, tui_wrapper, tui_cli, role_cwd / "opencode.json",
    ]
    for target in mutable_paths:
        if _within_root(target, root_real, "preparation path") != target:
            raise PrepError(f"preparation path must not traverse a symlink: {target}")

    # ---- now safe to write ---------------------------------------------------
    for d in ("home", "xdg-config", "xdg-data", "xdg-cache", "xdg-state"):
        (sandbox_path / d).mkdir(parents=True, exist_ok=True)

    try:
        _sessreg.provision_bridge_xdg(sandbox_path / "xdg-config", src)
    except AssertionError as exc:   # preflighted above; convert a race/defense error
        raise PrepError(str(exc)) from exc
    server_ready = _sessreg.bridge_available(sandbox_path / "xdg-config")
    tui_provisioned = provision_tui_bridge_xdg(sandbox_path / "xdg-config", Path(tui["path"]))
    tui_ready = tui_bridge_available(sandbox_path / "xdg-config")
    identity_ready = server_ready and tui_ready
    if not identity_ready:
        raise PrepError("isolated identity bridges were provisioned but did not validate")

    pinned = install_pinned_config(role_cwd, template, model=model, omlx_url=omlx_url)
    cmd, info = launch_command(role, pane, role_cwd, sandbox_path, base_env, session_id)

    report = {
        "role": role, "pane_id": pane, "cwd": str(role_cwd), "sandbox": str(sandbox_path),
        "server_bridge_ready": server_ready, "tui_bridge_ready": tui_ready,
        "identity_bridge_ready": identity_ready,
        "bridge_ready": identity_ready, "bridge_only_in_xdg": identity_ready,
        "tui_bridge": {
            "integration_id": tui["integration_id"], "version": tui["version"],
            "source_config": tui["source_config"], "source_spec": tui["source_spec"],
            "isolated_config": Path(tui_provisioned["config"]).name,
            "isolated_plugin": tui_provisioned["plugin"],
        },
        "retained_herder_vars": info["retained_herder_vars"],
        "model": f"omlx/{model}", "omlx_url": omlx_url, "pinned_config": pinned,
        "launch_command": cmd, "observe_command": f"herdr agent get {pane}",
        # PREVIEW only — no --consent-dispatch, so the runner refuses to dispatch:
        "runner_validate": (f"scripts/m606_opencode_e2e.py --confirm-live-run --root {shlex.quote(str(root_path))} "
                           f"--resume-handoff --herdr-pane {pane} "
                           '--pane-model-evidence "$ATTESTATION" --expect-session-id "$OBSERVED_ID" '
                           "--stop-after dispatch --keep-root"
                           + (f"  # expect {session_id}" if session_id else "")
                           + "  # PREVIEW: refuses to dispatch (dispatch-consent flag omitted)"),
        "dispatch_example": ("[ACTUAL DISPATCH — operator action; prep never runs this] "
                            f"scripts/m606_opencode_e2e.py --confirm-live-run "
                            f"--root {shlex.quote(str(root_path))} "
                            f"--resume-handoff --consent-dispatch --herdr-pane {pane} "
                            f"--pane-model-evidence <attestation.json> --expect-session-id <OBSERVED_ID>"),
    }

    if with_proof:
        runner_fn = proof_runner or _default_proof_runner(sandbox_path, role_cwd)
        sid = _sessreg.ensure_native_session(runner_fn, str(role_cwd))
        report["proof"] = {"native_id": _mask(sid), "native_ref": _ref(sid),
                          "provider_absent": not (sandbox_path / "xdg-config" / "opencode" / "opencode.json").exists()}

    (sandbox_path / "prep.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _default_proof_runner(sandbox: Path, cwd: Path):
    base = {k: v for k, v in os.environ.items()
            if k in _sessreg.HERDR_BRIDGE_VARS or k in _sessreg.STRUCTURAL_ENV}
    env = _sessreg.build_probe_env(base, sandbox)
    env["HERDR_SOCKET_PATH"] = str(sandbox / "dead.sock")   # synthetic → no live mutation
    env["HERDR_PANE_ID"] = "m606-probe:p1"                   # synthetic

    def runner_fn(op, *argv, timeout: float = 30):
        r = subprocess.run(["opencode", "--standalone", "api", op, *argv], cwd=str(cwd),
                           env=env, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, check=False)
        obj = None
        m = re.search(r"(\{.*\}|\[.*\])", (r.stdout or "") + (r.stderr or ""), re.S)
        if m:
            try:
                obj = json.loads(m.group(1))
            except Exception:
                obj = None
        return r.returncode, obj
    return runner_fn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    pp = sub.add_parser("prep", help="prepare one role's isolated disposable pane inputs (preview only)")
    pp.add_argument("--role", required=True, choices=ROLES)
    pp.add_argument("--root", required=True, help="retained phase-1 disposable run root")
    pp.add_argument("--herdr-pane", required=True, help="dedicated existing pane id (e.g. w1:p2)")
    pp.add_argument("--cwd", default=None, help="must equal the exact role directory; else rejected")
    pp.add_argument("--sandbox", default=None, help="must resolve inside the run root")
    pp.add_argument("--model", default=DEFAULT_MODEL)
    pp.add_argument("--omlx-url", default=OMLX_URL)
    pp.add_argument("--session-id", default=None, help=f"deterministic session id matching {SESSION_RE.pattern}")
    pp.add_argument("--bridge-src", default=None,
                    help="path to the verified installed server bridge (TUI bridge is auto-verified from its registration)")
    pp.add_argument("--allow-non-temp-root", action="store_true")
    pp.add_argument("--with-proof", action="store_true", help="run disposable prompt-free registration proof")
    ob = sub.add_parser("observe", help="read a live pane's native session id (read-only)")
    ob.add_argument("--herdr-pane", required=True)
    ob.add_argument("--reveal-id", action="store_true", help="print the raw id (local console only)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prep":
            rep = prepare_pane(role=args.role, root=args.root, pane=args.herdr_pane, cwd=args.cwd,
                               sandbox=args.sandbox, model=args.model, omlx_url=args.omlx_url,
                               session_id=args.session_id, bridge_src=args.bridge_src,
                               allow_non_temp_root=args.allow_non_temp_root, with_proof=args.with_proof)
        else:
            rep = observe_native_id(args.herdr_pane, reveal=args.reveal_id)
    except (PrepError, runner.GuardError, runner.PostconditionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(rep, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
