#!/usr/bin/env python3
"""M6-06 / PG-9 — bounded re-verification that OpenCode loads the CEO Skill.

What this script does
---------------------
It produces a repeatable, honest answer to one narrow question:

    Does a real, model-driven ``opencode run`` observe the ``teamcraft-ceo``
    Skill and report its pinned protocol identity (``1.0.0`` and the canonical
    SHA-256)?

The default mode is **offline and inert**.  It prints the exact command that
*would* be run, the provider/model it would use, the protocol identity read from
the repository manifest, and the pass/pending/fail rules.  It performs no
network call, starts no ``opencode`` process, and reads no credential material.

``--live`` performs exactly one bounded, non-interactive ``opencode run`` in the
repository root.  A timeout, ``provider.no-route``, or any non-zero exit is
reported as **pending** with the original output preserved.  A zero exit whose
output lacks the pinned version or SHA-256 is reported as **fail** — never as a
pass.  A pass requires *both* tokens, and the probe prompt deliberately contains
neither value, so they can only be produced by reading the Skill/reference files.

The live command includes ``--standalone`` (a private server, not the shared
background service), ``--archived`` (keeps the run out of session lists), and
``--auto`` (so a permission question cannot block the one-shot probe).  ``--auto``
is why live mode is explicit: the hard timeout bounds the run, and the prompt
forbids writes or publication, but the operator still chooses to allow the model
to act for that single bounded call.

Protocol source of truth
------------------------
The expected values are read from the repository's actual manifest
(``.agents/skills/teamcraft-ceo/references/protocol.manifest.json``) and its
``CEO_PROTOCOL.md`` bytes are verified through the canonical
``teamcraft.ceo_protocol.verify_protocol`` helper.  The M6-06 pin constants below
are a **cross-check**, not the only source: a manifest that is internally
consistent but has drifted to another protocol identity is rejected, so a
mutated manifest cannot silently redefine success.

Output
------
``--json`` emits a machine-readable object with at least ``status`` (one of
``pending``, ``pass``, ``fail``), ``command``, ``exit_code`` (the opencode
process exit code, or null), and ``reason``.  The default text format prints the
same facts.  Process exit codes are ``0`` for pass/dry-run, ``1`` for fail, and
``2`` for pending.

This module never opens OpenCode's credential store, never reads API-key
environment variables, and never prints them.  It only inherits the environment
for the child process, exactly as a normal shell invocation would.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:  # pragma: no cover - import failure is exercised through run_verification
    from teamcraft.ceo_protocol import ProtocolError, verify_protocol
except Exception as exc:  # noqa: BLE001 - pragma: no cover - defensive only
    ProtocolError = ValueError  # type: ignore[assignment,misc]
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None

# ---------------------------------------------------------------------------
# M6-06 pins (canonical; the manifest remains the authoritative source read at
# run time, these constants only prevent a locally-drifted manifest from
# silently changing what "pass" means).
# ---------------------------------------------------------------------------

PINNED_PROTOCOL_VERSION = "1.0.0"
PINNED_PROTOCOL_SHA256 = (
    "c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742")

DEFAULT_PROTOCOL_DIR = (
    REPO_ROOT / ".agents" / "skills" / "teamcraft-ceo" / "references")
DEFAULT_MODEL = "deepseek/deepseek-flash"
DEFAULT_AGENT = "teamcraft-ceo"
DEFAULT_TIMEOUT_SECONDS = 180.0
CAPTURE_LIMIT_CHARS = 100_000

PROBE_PROMPT = (
    "Use the teamcraft-ceo Skill now. Open .agents/skills/teamcraft-ceo/SKILL.md "
    "and follow it to read references/CEO_PROTOCOL.md and "
    "references/protocol.manifest.json. Do not modify any file. Do not run "
    "commands that write or publish. Your final answer must contain exactly "
    "these two labeled lines, copied from the files you read:\n"
    "protocol_version: <declared MAJOR.MINOR.PATCH>\n"
    "protocol_sha256: <the full 64 lowercase hex characters>"
)

RULES = (
    ("pass: opencode run exits 0 AND the output contains the pinned protocol "
     "version and the full pinned SHA-256 (the prompt contains neither value)."),
    ("pending: no opencode executable, timeout, provider.no-route, or any "
     "non-zero opencode exit; the original output is preserved and never "
     "converted to pass."),
    ("fail: opencode exits 0 but the pinned version and/or SHA-256 is missing, "
     "or the local protocol manifest/Skill artifact is inconsistent with the "
     "M6-06 pin."),
)

EXIT_CODES = {"pass": 0, "fail": 1, "pending": 2}


@dataclass(frozen=True)
class ProbeOutcome:
    """Normalized result of one ``opencode run`` attempt."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


Runner = Callable[..., ProbeOutcome]
Which = Callable[[str], str | None]


class ProtocolProbeError(RuntimeError):
    """The repository's protocol/Skill artifact cannot establish an expectation."""


# ---------------------------------------------------------------------------
# Command construction and protocol loading
# ---------------------------------------------------------------------------


def build_command(model: str, agent: str, prompt: str = PROBE_PROMPT) -> list[str]:
    """Return the exact bounded, non-interactive opencode invocation."""
    return [
        "opencode", "run",
        "--model", model,
        "--agent", agent,
        "--format", "json",
        "--archived",
        "--standalone",
        "--auto",
        prompt,
    ]


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def _validate_model(model: str) -> str | None:
    if model.count("/") != 1:
        return f"model must be provider/model, got {model!r}"
    provider, name = model.split("/", 1)
    if not provider or not name:
        return f"model must be provider/model, got {model!r}"
    return None


def load_protocol(protocol_dir: str | Path = DEFAULT_PROTOCOL_DIR) -> dict[str, Any]:
    """Read and verify the actual manifest, then cross-check the M6-06 pin.

    The manifest is authoritative for the bytes on disk; the pin constants make
    a drifted manifest an explicit failure instead of a moving target.
    """
    if _IMPORT_ERROR is not None:  # pragma: no cover - defensive only
        raise ProtocolProbeError(
            f"cannot import teamcraft.ceo_protocol: {_IMPORT_ERROR}")

    root = Path(protocol_dir)
    skill_md = root.parent / "SKILL.md"
    if not skill_md.is_file():
        raise ProtocolProbeError(f"CEO Skill file missing: {skill_md}")
    try:
        snapshot = verify_protocol(root)
    except ProtocolError as exc:
        raise ProtocolProbeError(f"protocol artifact invalid: {exc}") from exc
    if snapshot.version != PINNED_PROTOCOL_VERSION:
        raise ProtocolProbeError(
            "protocol manifest version "
            f"{snapshot.version!r} does not match M6-06 pin "
            f"{PINNED_PROTOCOL_VERSION!r} (reinitialization required)")
    if snapshot.sha256 != PINNED_PROTOCOL_SHA256:
        raise ProtocolProbeError(
            "protocol manifest SHA-256 "
            f"{snapshot.sha256!r} does not match M6-06 pin "
            f"{PINNED_PROTOCOL_SHA256!r} (reinitialization required)")
    return {
        "version": snapshot.version,
        "sha256": snapshot.sha256,
        "manifest": str(root / "protocol.manifest.json"),
        "protocol": str(root / "CEO_PROTOCOL.md"),
        "skill": str(skill_md),
        "pinned_match": True,
        "source_note": (
            "values read from the repository manifest and verified against its "
            "CEO_PROTOCOL.md bytes; the M6-06 pin is used as a drift cross-check"
        ),
    }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_LABELED_VERSION = re.compile(
    r"protocol_version\s*[\"']?\s*[:=]\s*[\"']?([0-9]+\.[0-9]+\.[0-9]+)",
    re.IGNORECASE)
_LABELED_HASH = re.compile(
    r"(?:protocol_sha256|sha-?256)\s*[\"']?\s*[:=]\s*[\"']?([0-9a-f]{64})",
    re.IGNORECASE)
_VERSION_TOKEN = re.compile(r"(?<![\d.])([0-9]+\.[0-9]+\.[0-9]+)(?![\d.])")
_HASH_TOKEN = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")


def parse_identity(text: str) -> dict[str, str | None]:
    """Best-effort extraction of a version/hash for evidence reporting."""
    version_match = _LABELED_VERSION.search(text) or _VERSION_TOKEN.search(text)
    hash_match = _LABELED_HASH.search(text) or _HASH_TOKEN.search(text)
    return {
        "version": version_match.group(1) if version_match else None,
        "sha256": hash_match.group(1).lower() if hash_match else None,
    }


def _version_present(text: str, expected: str) -> bool:
    return re.search(
        rf"(?<![\d.]){re.escape(expected)}(?![\d.])", text) is not None


def _hash_present(text: str, expected: str) -> bool:
    return re.search(
        rf"(?<![0-9a-fA-F]){re.escape(expected)}(?![0-9a-fA-F])",
        text, re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# Default runner
# ---------------------------------------------------------------------------


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def subprocess_runner(command: list[str], *, timeout: float,
                      cwd: str | Path) -> ProbeOutcome:
    """Run exactly one opencode process with stdin closed and a hard timeout."""
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return ProbeOutcome(
            completed.returncode,
            _as_text(completed.stdout),
            _as_text(completed.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return ProbeOutcome(
            None,
            _as_text(exc.stdout),
            _as_text(exc.stderr),
            timed_out=True,
        )
    except OSError as exc:
        return ProbeOutcome(None, "", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _bounded(text: str) -> tuple[str, bool]:
    if len(text) <= CAPTURE_LIMIT_CHARS:
        return text, False
    return text[:CAPTURE_LIMIT_CHARS], True


def _first_error_line(outcome: ProbeOutcome) -> str:
    for text in (outcome.stderr, outcome.stdout):
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    return ""


def _base_result(*, live: bool, model: str, agent: str, timeout: float,
                 cwd: Path, command: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "pending",
        "mode": "live" if live else "dry-run",
        "dry_run": not live,
        "command": command,
        "command_display": " ".join(command),
        "cwd": str(cwd),
        "exit_code": None,
        "reason": "",
        "model": model,
        "provider": _provider_of(model),
        "agent": agent,
        "timeout_seconds": timeout,
        "protocol": None,
        "observed": {},
        "stdout": "",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "timed_out": False,
        "elapsed_seconds": None,
        "rules": list(RULES),
    }


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    result["script_exit_code"] = (
        0 if result["dry_run"] else EXIT_CODES[result["status"]])
    return result


def run_verification(*, live: bool = False, model: str = DEFAULT_MODEL,
                     agent: str = DEFAULT_AGENT,
                     timeout: float = DEFAULT_TIMEOUT_SECONDS,
                     protocol_dir: str | Path = DEFAULT_PROTOCOL_DIR,
                     cwd: str | Path = REPO_ROOT,
                     runner: Runner | None = None,
                     which: Which | None = None) -> dict[str, Any]:
    """Build, optionally execute, and classify the PG-9 probe.

    ``runner`` and ``which`` are injection seams for offline tests.  Unless
    ``live`` is true this function performs no process launch and no PATH probe.
    """
    cwd_path = Path(cwd)
    command = build_command(model, agent)
    result = _base_result(
        live=live, model=model, agent=agent, timeout=timeout,
        cwd=cwd_path, command=command)

    model_error = _validate_model(model)
    if model_error is not None:
        result["status"] = "fail"
        result["reason"] = model_error
        return _finish(result)

    try:
        result["protocol"] = load_protocol(protocol_dir)
    except ProtocolProbeError as exc:
        result["status"] = "fail"
        result["reason"] = f"protocol precondition failed: {exc}"
        return _finish(result)

    if not live:
        result["status"] = "pending"
        result["reason"] = (
            "dry-run: no opencode process was started; run with "
            f"--live --model {model} to perform the bounded probe")
        return _finish(result)

    which_fn: Which = shutil.which if which is None else which
    if which_fn("opencode") is None:
        result["status"] = "pending"
        result["reason"] = (
            "opencode executable not found on PATH; live probe not attempted")
        return _finish(result)

    runner_fn: Runner = subprocess_runner if runner is None else runner
    started = time.monotonic()
    try:
        outcome = runner_fn(command, timeout=timeout, cwd=str(cwd_path))
    except Exception as exc:  # noqa: BLE001 - any runner failure is unverified
        result["status"] = "pending"
        result["reason"] = (
            f"probe runner failed before verification: {type(exc).__name__}: {exc}")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return _finish(result)

    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    stdout, stdout_cut = _bounded(_as_text(getattr(outcome, "stdout", "")))
    stderr, stderr_cut = _bounded(_as_text(getattr(outcome, "stderr", "")))
    result.update({
        "exit_code": getattr(outcome, "returncode", None),
        "timed_out": bool(getattr(outcome, "timed_out", False)),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_cut,
        "stderr_truncated": stderr_cut,
    })
    combined = f"{stdout}\n{stderr}"
    result["observed"] = parse_identity(combined)

    if result["timed_out"]:
        result["status"] = "pending"
        result["reason"] = (
            f"opencode run timed out after {timeout:g}s; output preserved, "
            "verification not established")
        return _finish(result)

    no_route = "provider.no-route" in combined.casefold()
    if no_route:
        result["status"] = "pending"
        line = _first_error_line(outcome)
        result["reason"] = (
            "provider.no-route observed: "
            f"{line or 'model route unavailable'} (exit {result['exit_code']})")
        return _finish(result)

    if result["exit_code"] != 0:
        result["status"] = "pending"
        line = _first_error_line(outcome)
        result["reason"] = (
            f"opencode run exited with code {result['exit_code']}: "
            f"{line or 'no error text captured'}")
        return _finish(result)

    expected = result["protocol"]
    assert expected is not None  # load_protocol succeeded above
    version_ok = _version_present(combined, expected["version"])
    hash_ok = _hash_present(combined, expected["sha256"])
    if version_ok and hash_ok:
        result["status"] = "pass"
        result["reason"] = (
            "opencode exit 0 and output contains the pinned protocol version "
            f"{expected['version']} and SHA-256 {expected['sha256']}")
        return _finish(result)

    missing = []
    if not version_ok:
        missing.append(f"protocol version {expected['version']}")
    if not hash_ok:
        missing.append(f"SHA-256 {expected['sha256']}")
    result["status"] = "fail"
    result["reason"] = (
        "opencode exit 0 but the probe output is missing: " + ", ".join(missing) +
        "; the Skill load is not demonstrated")
    return _finish(result)


# ---------------------------------------------------------------------------
# Rendering / CLI
# ---------------------------------------------------------------------------


def render_text(result: dict[str, Any]) -> str:
    lines = [
        f"status: {result['status']}",
        f"mode: {result['mode']}",
        f"model: {result['model']} (provider: {result['provider']})",
        f"agent: {result['agent']}",
        f"cwd: {result['cwd']}",
        f"command: {result['command_display']}",
        f"exit_code: {result['exit_code']}",
        f"reason: {result['reason']}",
    ]
    protocol = result.get("protocol")
    if protocol:
        lines.extend([
            f"protocol: {protocol['version']} / {protocol['sha256']}",
            f"protocol_manifest: {protocol['manifest']}",
            f"pinned_match: {protocol['pinned_match']}",
        ])
    observed = result.get("observed") or {}
    if result["mode"] == "live":
        lines.extend([
            f"observed_version: {observed.get('version')}",
            f"observed_sha256: {observed.get('sha256')}",
            f"timed_out: {result['timed_out']}",
        ])
    lines.append("rules:")
    lines.extend(f"  - {rule}" for rule in result["rules"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m606_verify_opencode_skill.py",
        description=(
            "Verify that a model-driven OpenCode run loads the teamcraft-ceo "
            "Skill and reports the pinned protocol identity. Offline by default."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/m606_verify_opencode_skill.py --json\n"
            "  python3 scripts/m606_verify_opencode_skill.py --live "
            "--model deepseek/deepseek-flash --json\n"))
    parser.add_argument(
        "--live", action="store_true",
        help="perform exactly one bounded opencode run (otherwise dry-run only)")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"provider/model for the probe (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--agent", default=DEFAULT_AGENT,
        help=f"OpenCode agent used for the probe (default: {DEFAULT_AGENT})")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"hard timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})")
    parser.add_argument(
        "--protocol-dir", type=Path, default=DEFAULT_PROTOCOL_DIR,
        help="directory containing CEO_PROTOCOL.md and protocol.manifest.json")
    parser.add_argument(
        "--json", action="store_true",
        help="emit one machine-readable JSON object")
    return parser


def main(argv: list[str] | None = None, *,
         runner: Runner | None = None,
         which: Which | None = None,
         stdout: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = sys.stdout if stdout is None else stdout
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    result = run_verification(
        live=args.live,
        model=args.model,
        agent=args.agent,
        timeout=args.timeout,
        protocol_dir=args.protocol_dir,
        runner=runner,
        which=which,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True), file=out)
    else:
        print(render_text(result), file=out)
    return int(result["script_exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
