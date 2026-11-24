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

    bridge_target = (sandbox_path / "xdg-config" / "opencode" / "plugins"
                     / _sessreg.BRIDGE_PLUGIN_NAME)
    mutable_paths = [
        *(sandbox_path / name for name in
          ("home", "xdg-config", "xdg-data", "xdg-cache", "xdg-state")),
        bridge_target, role_cwd / "opencode.json",
    ]
    for target in mutable_paths:
        if _within_root(target, root_real, "preparation path") != target:
            raise PrepError(f"preparation path must not traverse a symlink: {target}")

    # ---- now safe to write ---------------------------------------------------
    for d in ("home", "xdg-config", "xdg-data", "xdg-cache", "xdg-state"):
        (sandbox_path / d).mkdir(parents=True, exist_ok=True)

    status = bridge_status_text if bridge_status_text is not None else _default_bridge_status()
    installed = _sessreg.installed_bridge_path(status)
    src = Path(bridge_src) if bridge_src else (Path(installed) if installed else None)
    if not (src and src.exists()):
        raise PrepError("verified installed OpenCode bridge not found; refusing an unbridged pane")
    _sessreg.provision_bridge_xdg(sandbox_path / "xdg-config", src)
    bridge_ready = _sessreg.bridge_available(sandbox_path / "xdg-config")

    pinned = install_pinned_config(role_cwd, template, model=model, omlx_url=omlx_url)
    cmd, info = launch_command(role, pane, role_cwd, sandbox_path, base_env, session_id)

    report = {
        "role": role, "pane_id": pane, "cwd": str(role_cwd), "sandbox": str(sandbox_path),
        "bridge_ready": bridge_ready, "bridge_only_in_xdg": bridge_ready,
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
                            f"scripts/m606_opencode_e2e.py --confirm-live-run --root {root_path} "
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
    pp.add_argument("--bridge-src", default=None, help="path to the verified installed bridge plugin")
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
