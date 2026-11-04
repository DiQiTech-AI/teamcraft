"""Small, offline-testable boundary for the Herdr CLI contract.

The adapter owns transport normalization and response shapes. Business policy,
identity checks, and durable side-effect claims remain in the caller.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Sequence


class HerdrError(RuntimeError):
    """Normalized Herdr transport or response error."""

    def __init__(self, code: str, message: str, *, argv: Sequence[str] = (), returncode: int | None = None):
        super().__init__(message)
        self.code = code
        self.argv = list(argv)
        self.returncode = returncode


Runner = Callable[[Sequence[str], float, tuple[int, ...]], tuple[str, str, int]]


def subprocess_runner(argv: Sequence[str], timeout: float, accepted: tuple[int, ...]) -> tuple[str, str, int]:
    try:
        result = subprocess.run(list(argv), check=False, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise HerdrError("executable_missing", f"executable was not found: {argv[0]}", argv=argv) from exc
    except subprocess.TimeoutExpired as exc:
        raise HerdrError("timeout", f"command timed out after {timeout:g}s: {' '.join(argv[:3])}", argv=argv) from exc
    try:
        out = (result.stdout or b"").decode("utf-8")
        err = (result.stderr or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HerdrError("invalid_utf8", f"Herdr output was not valid UTF-8: {exc}", argv=argv) from exc
    if result.returncode not in accepted:
        raise HerdrError("command_failed", err.strip() or out.strip() or f"exit status {result.returncode}", argv=argv, returncode=result.returncode)
    return out, err, result.returncode


class HerdrAdapter:
    def __init__(self, runner: Runner = subprocess_runner):
        self.runner = runner

    def process(self, arguments: Sequence[str], *, timeout: float = 15, accepted: tuple[int, ...] = (0,)) -> tuple[str, str, int]:
        return self.runner(["herdr", *arguments], timeout, accepted)

    def request(self, arguments: Sequence[str], *, timeout: float = 15) -> dict[str, Any]:
        stdout, _, _ = self.process(arguments, timeout=timeout)
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise HerdrError("invalid_json", f"Herdr returned invalid JSON for {' '.join(arguments[:2])}: {exc}", argv=["herdr", *arguments]) from exc
        if not isinstance(data, dict):
            raise HerdrError("invalid_shape", "Herdr returned a JSON value with an unexpected shape", argv=["herdr", *arguments])
        return data

    def supported_kinds(self) -> set[str]:
        out, err, _ = self.process(["agent"], timeout=5, accepted=(0, 2))
        match = re.search(r"^\s*kinds:\s*([^\r\n]+)", out + "\n" + err, re.MULTILINE)
        if not match:
            raise HerdrError("invalid_shape", "could not discover supported agent kinds from installed Herdr")
        return {s.strip() for s in match.group(1).split("|") if s.strip()}

    def list_agents(self) -> list[dict[str, Any]]:
        data = self.request(["agent", "list"])
        try: value = data["result"]["agents"]
        except (KeyError, TypeError) as exc: raise HerdrError("missing_field", "Herdr agent list response is missing agent data") from exc
        if not isinstance(value, list): raise HerdrError("invalid_shape", "Herdr agent list returned invalid agent data")
        return [item for item in value if isinstance(item, dict)]

    def pane(self, data: dict[str, Any]) -> dict[str, Any]:
        try: value = data["result"]["pane"]
        except (KeyError, TypeError) as exc: raise HerdrError("missing_field", "Herdr response is missing pane data") from exc
        if not isinstance(value, dict) or not value.get("pane_id"): raise HerdrError("invalid_shape", "Herdr returned invalid pane data")
        return value

    def agent(self, data: dict[str, Any]) -> dict[str, Any]:
        try: value = data["result"]["agent"]
        except (KeyError, TypeError) as exc: raise HerdrError("missing_field", "Herdr response is missing agent data") from exc
        if not isinstance(value, dict): raise HerdrError("invalid_shape", "Herdr returned invalid agent data")
        return value

    # Capability surface includes operations not yet used by teamctl. Their
    # argv is deliberately centralized so a future lifecycle can opt in.
    def split_pane(self, pane_id: str, direction: str, cwd: str, *, extra: Sequence[str] = ()) -> dict[str, Any]:
        return self.pane(self.request(["pane", "split", "--pane", pane_id, "--direction", direction, "--cwd", cwd, "--no-focus", *extra]))

    def start_agent(self, name: str, kind: str, pane_id: str, timeout_ms: int, args: Sequence[str] = ()) -> dict[str, Any]:
        argv = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", str(timeout_ms)]
        if args: argv.extend(["--", *args])
        return self.request(argv, timeout=max(10, timeout_ms / 1000 + 5))

    def get_agent(self, name: str) -> dict[str, Any]: return self.agent(self.request(["agent", "get", name]))
    def prompt_agent(self, name: str, text: str, *, wait: bool = False, timeout_ms: int = 120_000) -> dict[str, Any]:
        argv = ["agent", "prompt", name, text]
        if wait: argv.extend(["--wait", "--timeout", str(timeout_ms)])
        return self.request(argv, timeout=max(15, timeout_ms / 1000 + 5) if wait else 15)
    def read_agent(self, name: str, *args: str) -> dict[str, Any]: return self.request(["agent", "read", name, *args])
    def stop_agent(self, name: str, *args: str) -> dict[str, Any]: return self.request(["agent", "stop", name, *args])
    def current_pane(self, target: str | None = None) -> dict[str, Any]:
        selector = ["--pane", target] if target else ["--current"]
        return self.pane(self.request(["pane", "current", *selector]))
    def get_pane(self, pane_id: str) -> dict[str, Any]: return self.pane(self.request(["pane", "get", pane_id]))
    def layout(self, pane_id: str) -> dict[str, Any]: return self.request(["pane", "layout", "--pane", pane_id])
    def process_info(self, pane_id: str) -> dict[str, Any]: return self.request(["pane", "process-info", "--pane", pane_id])

