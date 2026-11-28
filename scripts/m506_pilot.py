"""M5-06 local pilot harness: single (strong) vs mixed (router-selected).

This is a deliberately small diagnostic harness, not the formal roadmap
experiment.  It talks only to a loopback oMLX endpoint, uses two same-family
quantized models, and performs N=1 per (arm, task).  The router is consumed as
a library (``teamcraft.router.route``) and is not wired into the main dispatch
path.

The harness never reads credentials.  It stores only a redacted, size-capped
copy of ``message.content`` under ``<output>/contents/`` for reproducibility
and ignores ``message.reasoning_content``: only ``message.content`` is a
candidate answer.

Verifier hardening (second pilot round):

* every task carries explicit boundary cases that the task prompt also states,
  so the verifier is not a hidden trap;
* a verifier only counts as passing when it exits 0 *and* its final stdout line
  is ``PASS:<nonce>``, where the nonce is a fresh per-invocation secret passed
  through ``M506_VERIFIER_NONCE``.  ``os._exit(0)``/``sys.exit(0)`` inside a
  candidate solution therefore fail instead of bypassing the assertions;
* verifier child processes run with a ``sitecustomize`` shim that disables
  outbound sockets and process creation, a CPU rlimit, and a minimal
  ``HOME``/``TMPDIR`` pointing at the throwaway working directory.

Third pilot round adds ``--replay``: it re-judges retained ``contents`` from a
previous results directory with the current (fixed) verifiers, verifies every
``content_sha256`` before running, and writes ``replay.json``.  Replay never
calls a model or touches the network and never rewrites the original results.

The verifier is a *correctness guard, not a security boundary*.  Candidate
solutions run as in-process Python, so code that reaches libc (for example via
``ctypes``) or forges the nonce/excepthook from inside the same process can
bypass it.  A passing verifier therefore means "this non-adversarial model
output computes the required answer without boundary violations", not "this
code is safe to run".  See :data:`LIMITATIONS`.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from teamcraft import providers, router  # noqa: E402

# Hard-coded loopback endpoint.  `validate_endpoint` rejects anything else.
ENDPOINT = "http://127.0.0.1:8000"
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"

CHEAP_MODEL = "Qwen3.8-Flash-Next-4bit-mtp"
STRONG_MODEL = "Qwen3.8-Flash-Next-oQ4e-mtp"
MODELS = (CHEAP_MODEL, STRONG_MODEL)

ARMS = ("single", "mixed")
ARM_DESCRIPTIONS = {
    "single": "every task uses the strong oQ4e model",
    "mixed": "teamcraft.router chooses per declared risk/tags (normal -> 4bit, high -> oQ4e)",
}

HARNESS_NAME = "opencode-local"
PROVIDER_NAME = "omlx"
POLICY = router.RouterPolicy(high_risk_min_assurance=2)

DEFAULT_TIMEOUT_SECONDS = 180.0
TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 8192
# Kept as an alias for callers that imported the first-round constant name.
MAX_TASK_TOKENS = DEFAULT_MAX_TOKENS
WARMUP_TOKENS = 8
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_VERIFIER_DETAIL = 800
MAX_ERROR_CHARS = 1200
MAX_CONTENT_BYTES = 64 * 1024
SENTINEL_ENV = "M506_VERIFIER_NONCE"

TASKS_PATH = ROOT / "scripts" / "m506_tasks.json"
_ID_RE = re.compile(r"[a-z0-9_-]{1,32}\Z")
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,199}\Z")
_FENCE_RE = re.compile(
    r"```[ \t]*([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)(?:```|\Z)", re.DOTALL
)
_PYTHON_FENCES = frozenset({"python", "python3", "py"})
_SENSITIVE_RE = re.compile(
    r"\bBearer\s+\S+"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|\btck_[a-z0-9]{8,}\b"
    r"|(?<![A-Za-z0-9])(?:api[_-]?key|apikey|password|passwd|secret|token|credential)"
    r"\s*[:=]\s*\S+",
    re.I,
)

LIMITATIONS = (
    "Router is consumed at library level (teamcraft.router.route); it is not "
    "wired into the main dispatch path.",
    "Single machine, loopback-only oMLX endpoint, two same-family quantized "
    "models.",
    "N=1 per (arm, task); no statistical significance and no percentage "
    "improvement claim.",
    "No cloud arm: cloud transport is absent and remains PENDING; this pilot "
    "is not a cloud comparison.",
    "Diagnostic boundary evidence only; it is not the formal roadmap "
    "cloud-vs-mixed experiment.",
    "The verifier is a correctness guard, not a security boundary: in-process "
    "Python (for example ctypes/libc calls) or same-process nonce/excepthook "
    "forgery can bypass it. A pass means the output computes the required "
    "answer without the checked boundary violations, not that the code is "
    "safe. Conclusions apply only to non-adversarial model output.",
)

# Only these fields may reach results.json.  Unknown keys (for example raw
# model content or reasoning text) are dropped rather than serialized.
RESULT_FIELDS = (
    "arm",
    "task_id",
    "risk",
    "tags",
    "executed",
    "success",
    "model",
    "model_seconds",
    "verifier_seconds",
    "total_seconds",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "finish_reason",
    "usage_timings",
    "reason_code",
    "effective_risk",
    "decision_digest",
    "prompt_sha256",
    "content_sha256",
    "content_bytes",
    "content_truncated",
    "content_empty",
    "sentinel_ok",
    "verifier_returncode",
    "error",
)

# Installed as ``sitecustomize.py`` on PYTHONPATH for every verifier process.
# ``site`` imports it at interpreter startup, before solution.py is imported.
SANDBOX_SITECUSTOMIZE = '''"""M5-06 verifier sandbox (installed by the pilot harness)."""

import os
import socket


class SandboxViolation(PermissionError):
    """Raised when sandboxed code attempts network or process creation."""


def _blocked(name):
    def _deny(*args, **kwargs):
        raise SandboxViolation("m506 verifier sandbox: %s is disabled" % name)

    return _deny


socket.socket.connect = _blocked("socket.connect")
socket.socket.connect_ex = _blocked("socket.connect_ex")
socket.socket.sendto = _blocked("socket.sendto")
socket.create_connection = _blocked("socket.create_connection")
socket.getaddrinfo = _blocked("socket.getaddrinfo")

try:
    import subprocess as _m506_subprocess
except Exception:  # pragma: no cover - subprocess is always importable
    _m506_subprocess = None
if _m506_subprocess is not None:
    _m506_subprocess.Popen = _blocked("subprocess.Popen")

os.system = _blocked("os.system")
for _name in (
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "posix_spawn",
    "posix_spawnp",
    "fork",
):
    if hasattr(os, _name):
        setattr(os, _name, _blocked("os." + _name))
'''

# Appended to every verifier after its own assertions.  It must remain the last
# source statement so that the sentinel is the last stdout line.
SENTINEL_TRAILER = (
    "\n\n# --- M5-06 harness verifier sentinel (must be the final stdout line) ---\n"
    "import os as _m506_os\n"
    "import sys as _m506_sys\n"
    "_m506_sys.stdout.flush()\n"
    "_m506_os.write(1, ('PASS:' + _m506_os.environ['M506_VERIFIER_NONCE'] + '\\n').encode('utf-8'))\n"
)


class PilotError(Exception):
    """Base class for expected harness failures."""


class TaskSetError(PilotError):
    """The fixed task set is missing or violates its schema."""


class CodeExtractionError(PilotError):
    """The model response did not contain usable Python code."""


class InfrastructureError(PilotError):
    """The endpoint or process environment cannot support a run."""


class ModelTimeout(PilotError):
    """One model request exceeded the configured timeout."""


class ModelResponseError(PilotError):
    """The model endpoint returned a malformed or unusable response."""


class ReplayError(PilotError):
    """Retained results cannot be replayed safely (schema, hash, or file)."""


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    prompt: str
    verifier_python: str
    risk: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class ArmSelection:
    model: str | None
    reason_code: str
    effective_risk: str
    decision_digest: str | None
    refused: bool


@dataclass(frozen=True)
class VerifierOutcome:
    success: bool
    error: str | None
    seconds: float
    returncode: int | None
    sentinel_ok: bool = False


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def file_sha256(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def compute_provenance(tasks: Sequence[Task]) -> dict[str, Any]:
    """Return harness/tasks prompt digests for the given task selection."""

    return {
        "harness_sha256": file_sha256(Path(__file__)),
        "tasks_sha256": file_sha256(TASKS_PATH),
        "prompts_sha256": {task.id: sha256_text(task.prompt) for task in tasks},
    }


def validate_endpoint(url: str) -> str:
    """Return the bare loopback origin or raise ``PilotError``.

    Named hosts such as ``localhost`` are rejected on purpose: the trust
    decision must not depend on mutable DNS/hosts state.
    """

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise PilotError("endpoint URL is invalid") from exc

    host = parsed.hostname
    try:
        loopback = host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PilotError(
            "endpoint must be an unauthenticated literal-loopback http URL"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def load_tasks(path: str | Path | None = None) -> tuple[Task, ...]:
    """Load and validate the fixed task set."""

    target = Path(path) if path is not None else TASKS_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskSetError(f"cannot read task set: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskSetError(f"task set is not valid JSON: {exc}") from exc
    if not isinstance(document, list):
        raise TaskSetError("task set must be a JSON array")
    if not document:
        raise TaskSetError("task set must not be empty")

    required = ("id", "title", "prompt", "verifier_python", "risk", "tags")
    tasks: list[Task] = []
    seen: set[str] = set()
    for index, item in enumerate(document):
        label = f"task[{index}]"
        if not isinstance(item, dict):
            raise TaskSetError(f"{label} must be an object")
        missing = [field for field in required if field not in item]
        if missing:
            raise TaskSetError(f"{label} is missing fields: {', '.join(missing)}")

        task_id = item["id"]
        if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id):
            raise TaskSetError(f"{label}.id must match [a-z0-9_-]{{1,32}}")
        if task_id in seen:
            raise TaskSetError(f"duplicate task id: {task_id}")
        seen.add(task_id)

        for field in ("title", "prompt", "verifier_python"):
            value = item[field]
            if not isinstance(value, str) or not value.strip():
                raise TaskSetError(f"{label}.{field} must be a non-empty string")

        risk = item["risk"]
        if risk not in {"normal", "high"}:
            raise TaskSetError(f"{label}.risk must be normal or high")

        raw_tags = item["tags"]
        if not isinstance(raw_tags, list):
            raise TaskSetError(f"{label}.tags must be a list")
        tags: list[str] = []
        for tag in raw_tags:
            if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
                raise TaskSetError(f"{label}.tags contains an invalid tag")
            tags.append(tag)
        if len(set(tags)) != len(tags):
            raise TaskSetError(f"{label}.tags must not contain duplicates")

        tasks.append(
            Task(
                id=task_id,
                title=item["title"].strip(),
                prompt=item["prompt"],
                verifier_python=item["verifier_python"],
                risk=risk,
                tags=tuple(tags),
            )
        )
    return tuple(tasks)


def extract_code(content: str | None) -> str:
    """Extract Python from a model message.

    Priority: the first python fenced block, then the first fenced block of any
    language, then the whole message.  A missing/blank message is an error.
    """

    if not isinstance(content, str) or not content.strip():
        raise CodeExtractionError("model returned empty content")

    blocks = [
        (match.group(1).strip().lower(), match.group(2))
        for match in _FENCE_RE.finditer(content)
    ]
    for language, body in blocks:
        if language in _PYTHON_FENCES or language.startswith("python"):
            code = body.strip()
            if not code:
                raise CodeExtractionError("python fenced block is empty")
            return code
    if blocks:
        code = blocks[0][1].strip()
        if not code:
            raise CodeExtractionError("fenced block is empty")
        return code
    return content.strip()


def redact_text(value: Any, *, limit: int | None = None) -> str:
    """Remove credential-shaped text before anything is written to disk."""

    if value is None:
        return ""
    text = _SENSITIVE_RE.sub("[redacted]", str(value))
    if limit is not None and len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


def clean_error(value: Any) -> str:
    """Redact and bound an error string before it reaches results.json."""

    return redact_text(value, limit=MAX_ERROR_CHARS)


def build_snapshots(
    model: str, now: datetime
) -> tuple[providers.CapabilitySnapshot, providers.CapabilitySnapshot]:
    """Build a healthy harness snapshot and a model-scoped provider snapshot."""

    observed = _iso(now)
    valid_until = _iso(now + timedelta(hours=1))
    harness = providers.CapabilitySnapshot(
        "harness",
        HARNESS_NAME,
        None,
        None,
        (),
        {},
        {},
        "healthy",
        "ok",
        "m506_pilot",
        observed,
        valid_until,
    )
    provider = providers.CapabilitySnapshot(
        "provider",
        PROVIDER_NAME,
        None,
        "openai_compatible",
        (model,),
        {},
        {},
        "healthy",
        "ok",
        "m506_pilot",
        observed,
        valid_until,
    )
    return harness, provider


def build_candidates(
    now: datetime, *, weak_assurance: int = 1, strong_assurance: int = 2
) -> tuple[router.RouteCandidate, ...]:
    """Construct the two local candidates used by both arms."""

    cheap_harness, cheap_provider = build_snapshots(CHEAP_MODEL, now)
    strong_harness, strong_provider = build_snapshots(STRONG_MODEL, now)
    cheap = router.RouteCandidate(
        cheap_harness, cheap_provider, CHEAP_MODEL, "local", weak_assurance, 0
    )
    strong = router.RouteCandidate(
        strong_harness, strong_provider, STRONG_MODEL, "local", strong_assurance, 1
    )
    return (cheap, strong)


def route_arm(
    arm: str,
    task: Task,
    *,
    candidates: Sequence[router.RouteCandidate],
    now: datetime,
    policy: router.RouterPolicy = POLICY,
) -> ArmSelection:
    """Apply the arm's selection rule without ever silently falling back.

    ``single`` routes over the strong candidate only, so it always selects
    oQ4e while still producing a real decision digest.  ``mixed`` routes over
    both candidates; a refusal is returned as ``refused`` with no model.
    """

    if arm not in ARMS:
        raise PilotError(f"unknown arm: {arm}")
    request = router.RouteRequest(risk=task.risk, task_tags=tuple(task.tags))
    if arm == "single":
        strong = next(
            (candidate for candidate in candidates if candidate.model == STRONG_MODEL),
            None,
        )
        if strong is None:
            raise PilotError("single arm requires the strong candidate")
        considered: Sequence[router.RouteCandidate] = (strong,)
    else:
        considered = tuple(candidates)

    decision = router.route(request, considered, policy=policy, now=now)
    if decision.selection is None:
        return ArmSelection(
            None,
            decision.reason_code,
            decision.effective_risk,
            decision.decision_digest,
            True,
        )
    return ArmSelection(
        decision.selection.model,
        decision.reason_code,
        decision.effective_risk,
        decision.decision_digest,
        False,
    )


def base_result(arm: str, task: Task) -> dict[str, Any]:
    return {
        "arm": arm,
        "task_id": task.id,
        "risk": task.risk,
        "tags": list(task.tags),
        "executed": False,
        "success": None,
        "model": None,
        "model_seconds": None,
        "verifier_seconds": None,
        "total_seconds": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "finish_reason": None,
        "usage_timings": None,
        "reason_code": None,
        "effective_risk": None,
        "decision_digest": None,
        "prompt_sha256": sha256_text(task.prompt),
        "content_sha256": None,
        "content_bytes": None,
        "content_truncated": None,
        "content_empty": None,
        "sentinel_ok": None,
        "verifier_returncode": None,
        "error": None,
    }


def selection_result(arm: str, task: Task, selection: ArmSelection) -> dict[str, Any]:
    record = base_result(arm, task)
    record["reason_code"] = selection.reason_code
    record["effective_risk"] = selection.effective_risk
    record["decision_digest"] = selection.decision_digest
    if selection.refused:
        record["error"] = "router_refused"
    else:
        record["executed"] = True
        record["model"] = selection.model
    return record


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Keep every request on the already-validated loopback origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url, code, "pilot refuses HTTP redirects", headers, fp
        )


def call_chat(
    model: str,
    messages: Sequence[dict[str, str]],
    *,
    max_tokens: int,
    timeout_seconds: float,
    base_url: str = ENDPOINT,
) -> tuple[dict[str, Any], float]:
    """POST one Chat Completions request and return (document, wall seconds)."""

    origin = validate_endpoint(base_url)
    payload = {
        "model": model,
        "messages": list(messages),
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        origin + CHAT_PATH,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RefuseRedirect()
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise InfrastructureError(
            f"model endpoint returned HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ModelTimeout(
                f"model request timed out after {timeout_seconds:g}s"
            ) from exc
        raise InfrastructureError(f"model endpoint unreachable: {reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ModelTimeout(
            f"model request timed out after {timeout_seconds:g}s"
        ) from exc
    except OSError as exc:
        raise InfrastructureError(f"model request failed: {exc}") from exc
    elapsed = time.monotonic() - started

    if status != 200:
        raise InfrastructureError(f"model endpoint returned HTTP {status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelResponseError("model response exceeded the size limit")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelResponseError("model response is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ModelResponseError("model response must be a JSON object")
    return document, elapsed


def response_message(document: dict[str, Any]) -> dict[str, Any]:
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelResponseError("model response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ModelResponseError("model response choice is malformed")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ModelResponseError("model response has no message")
    return message


def response_finish_reason(document: dict[str, Any]) -> str | None:
    choices = document.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str):
            return reason
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def usage_tokens(document: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = document.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return (
        _int_or_none(usage.get("prompt_tokens")),
        _int_or_none(usage.get("completion_tokens")),
    )


def response_usage_metadata(
    document: dict[str, Any],
) -> tuple[int | None, dict[str, float]]:
    """Return (reasoning_tokens, usage timings) from one response body."""

    usage = document.get("usage")
    if not isinstance(usage, dict):
        return None, {}

    reasoning = None
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = _int_or_none(details.get("reasoning_tokens"))
    if reasoning is None:
        reasoning = _int_or_none(usage.get("reasoning_tokens"))

    timings: dict[str, float] = {}
    for key in ("total_time", "model_load_duration", "generation_duration"):
        value = _number_or_none(usage.get(key))
        if value is not None:
            timings[key] = value
    return reasoning, timings


def store_content(
    output_dir: str | Path, arm: str, task_id: str, content: Any
) -> dict[str, Any]:
    """Persist redacted ``message.content`` (64 KiB cap) and its digest."""

    text = redact_text(content if isinstance(content, str) else "")
    encoded = text.encode("utf-8")
    truncated = len(encoded) > MAX_CONTENT_BYTES
    if truncated:
        omitted = len(encoded) - MAX_CONTENT_BYTES
        safe = encoded[:MAX_CONTENT_BYTES].decode("utf-8", "ignore")
        stored = (
            safe
            + f"\n...[m506: content truncated at {MAX_CONTENT_BYTES} bytes; "
            f"{omitted} bytes omitted]\n"
        )
    else:
        stored = text
    data = stored.encode("utf-8")
    path = Path(output_dir) / "contents" / arm / f"{task_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "content_sha256": sha256_bytes(data),
        "content_bytes": len(data),
        "content_truncated": truncated,
        "content_empty": not text.strip(),
    }


def verifier_source(task: Task) -> str:
    """Return the verifier source with the harness sentinel appended last."""

    return task.verifier_python.rstrip() + SENTINEL_TRAILER


def _rlimit_preexec(timeout_seconds: float):
    """Return a ``preexec_fn`` that caps child CPU time, when supported."""

    if os.name != "posix":
        return None
    cpu_seconds = max(1, int(math.ceil(timeout_seconds)))

    def _apply() -> None:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except Exception:  # pragma: no cover - best effort on exotic hosts
            pass

    return _apply


def run_verifier(
    task: Task,
    code: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
    nonce: str | None = None,
) -> VerifierOutcome:
    """Run the verifier in a sandboxed child and require the PASS sentinel.

    A zero exit status is not enough: the final non-empty stdout line must be
    ``PASS:<nonce>``.  The nonce is fresh per invocation and is only passed to
    the child through ``M506_VERIFIER_NONCE``.
    """

    interpreter = python or sys.executable or "python3"
    nonce = nonce or secrets.token_hex(16)
    marker = f"PASS:{nonce}"
    with tempfile.TemporaryDirectory(prefix="m506-pilot-") as tmp:
        # Resolve /var -> /private/var (macOS) so HOME/TMPDIR and getcwd agree.
        root = Path(tmp).resolve()
        tmp = str(root)
        (root / "solution.py").write_text(code, encoding="utf-8")
        (root / "verifier.py").write_text(verifier_source(task), encoding="utf-8")
        (root / "sitecustomize.py").write_text(
            SANDBOX_SITECUSTOMIZE, encoding="utf-8"
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": tmp,
            "TMPDIR": tmp,
            "PYTHONPATH": tmp,
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            SENTINEL_ENV: nonce,
        }
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [interpreter, "-B", "verifier.py"],
                cwd=tmp,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                preexec_fn=_rlimit_preexec(timeout_seconds),
            )
        except subprocess.TimeoutExpired:
            return VerifierOutcome(
                False, "verifier_timeout", time.monotonic() - started, None, False
            )
        except OSError as exc:
            return VerifierOutcome(
                False,
                clean_error(f"verifier_spawn_failed: {exc}"),
                time.monotonic() - started,
                None,
                False,
            )
        elapsed = time.monotonic() - started
        stdout = completed.stdout or ""
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        sentinel_ok = bool(lines) and lines[-1] == marker

        if completed.returncode == 0 and sentinel_ok:
            return VerifierOutcome(True, None, elapsed, 0, True)

        if completed.returncode == 0:
            last = redact_text(lines[-1]) if lines else "<no stdout>"
            return VerifierOutcome(
                False,
                clean_error(
                    "verifier_sentinel_missing (exit 0): final stdout line was "
                    f"{last!r}, expected the harness sentinel"
                ),
                elapsed,
                0,
                False,
            )

        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > MAX_VERIFIER_DETAIL:
            detail = detail[-MAX_VERIFIER_DETAIL:]
        message = f"verifier_failed (exit {completed.returncode}): {detail}"
        return VerifierOutcome(
            False, clean_error(message), elapsed, completed.returncode, sentinel_ok
        )


def _truncated(row: dict[str, Any]) -> bool:
    return row.get("finish_reason") == "length" or row.get("content_empty") is True


def aggregate(results: Iterable[dict[str, Any]], *, arms: Sequence[str] = ARMS) -> dict[str, Any]:
    """Aggregate per-(arm, task) records without treating missing as zero."""

    rows = list(results)
    summary: dict[str, Any] = {}
    for arm in arms:
        arm_rows = [row for row in rows if row.get("arm") == arm]
        executed = [row for row in arm_rows if row.get("executed") is True]
        succeeded = [row for row in executed if row.get("success") is True]
        failed = [row for row in executed if row.get("success") is False]
        refused = [row for row in arm_rows if row.get("error") == "router_refused"]

        def _sum(key: str) -> float | int | None:
            values = [
                row[key]
                for row in arm_rows
                if isinstance(row.get(key), (int, float))
                and not isinstance(row.get(key), bool)
            ]
            if not values:
                return None
            total = sum(values)
            return round(total, 3) if isinstance(total, float) else total

        def _timing_sum(key: str) -> float | None:
            values = []
            for row in arm_rows:
                timings = row.get("usage_timings")
                if isinstance(timings, dict):
                    value = timings.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        values.append(float(value))
            if not values:
                return None
            return round(sum(values), 3)

        unknown_usage = sum(
            1
            for row in executed
            if row.get("prompt_tokens") is None or row.get("completion_tokens") is None
        )
        models = {row.get("model") for row in executed if row.get("model")}
        summary[arm] = {
            "tasks": len(arm_rows),
            "executed": len(executed),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "refused": len(refused),
            "success_rate_executed": (
                round(len(succeeded) / len(executed), 4) if executed else None
            ),
            "success_rate_total": (
                round(len(succeeded) / len(arm_rows), 4) if arm_rows else None
            ),
            # wall model time includes model load and switching.
            "model_seconds": _sum("model_seconds"),
            "wall_model_seconds": _sum("model_seconds"),
            # generation time excludes load/switch.
            "generation_seconds": _timing_sum("generation_duration"),
            "model_load_seconds": _timing_sum("model_load_duration"),
            "server_total_seconds": _timing_sum("total_time"),
            "verifier_seconds": _sum("verifier_seconds"),
            "prompt_tokens": _sum("prompt_tokens"),
            "completion_tokens": _sum("completion_tokens"),
            "reasoning_tokens": _sum("reasoning_tokens"),
            "truncated": sum(1 for row in executed if _truncated(row)),
            "distinct_models": len(models),
            "model_switch_occurred": len(models) > 1,
            "tasks_with_unknown_usage": unknown_usage,
        }
    return summary


def decision_distribution(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[Any, Any, Any, Any]] = Counter()
    for row in results:
        counts[
            (
                row.get("arm"),
                row.get("reason_code"),
                row.get("effective_risk"),
                row.get("model"),
            )
        ] += 1
    return [
        {
            "arm": key[0],
            "reason_code": key[1],
            "effective_risk": key[2],
            "model": key[3],
            "count": count,
        }
        for key, count in sorted(
            counts.items(),
            key=lambda item: tuple(str(part) for part in item[0]),
        )
    ]


def sanitize_result(record: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key in RESULT_FIELDS:
        if key not in record:
            continue
        value = record[key]
        if isinstance(value, str):
            cleaned[key] = redact_text(value)
        elif isinstance(value, list):
            cleaned[key] = [
                redact_text(item) if isinstance(item, str) else item for item in value
            ]
        elif isinstance(value, dict):
            cleaned[key] = {
                str(sub_key): sub_value
                for sub_key, sub_value in value.items()
                if isinstance(sub_value, (str, int, float, bool)) or sub_value is None
            }
        else:
            cleaned[key] = value
    return cleaned


def _load_total_seconds(
    results: Sequence[dict[str, Any]], warmups: Sequence[dict[str, Any]]
) -> float | None:
    total = 0.0
    seen = False
    for item in list(results) + list(warmups):
        timings = item.get("usage_timings")
        if isinstance(timings, dict):
            value = timings.get("model_load_duration")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value)
                seen = True
    return round(total, 3) if seen else None


def build_limits(
    results: Sequence[dict[str, Any]],
    *,
    summary: dict[str, Any],
    warmups: Sequence[dict[str, Any]],
    max_tokens: int,
) -> list[str]:
    """Base limitations plus data-derived disclosure about the actual run."""

    lines = list(LIMITATIONS)

    truncations: Counter[str] = Counter()
    for row in results:
        if row.get("executed") is True and row.get("model"):
            if _truncated(row):
                truncations[row["model"]] += 1
    trunc_text = ", ".join(
        f"{model}={truncations.get(model, 0)}" for model in MODELS
    )

    load_total = _load_total_seconds(results, warmups)
    switch_parts = []
    timing_parts = []
    for arm in ARMS:
        item = summary.get(arm, {})
        switch_parts.append(
            f"{arm}: {'yes' if item.get('model_switch_occurred') else 'no'} "
            f"({item.get('distinct_models', 0)} model(s))"
        )
        timing_parts.append(
            f"{arm}: wall_model_seconds={_fmt(item.get('wall_model_seconds'))}, "
            f"generation_seconds={_fmt(item.get('generation_seconds'))}"
        )

    lines.extend(
        [
            f"max_tokens={max_tokens} for every task request; truncations "
            f"(finish_reason=length or empty content): {trunc_text}.",
            f"Model load total: {_fmt(load_total)}s across warmups and task "
            "calls (included in wall_model_seconds, excluded from "
            "generation_seconds).",
            f"Model switching: {'; '.join(switch_parts)}.",
            f"wall_model_seconds vs generation_seconds — {'; '.join(timing_parts)}"
            ". Wall time includes model load/switch; generation time excludes "
            "load.",
            "Warmup: both models are warmed once before each arm; warmup "
            "durations are recorded separately and excluded from per-task "
            "model_seconds.",
            "Verifier isolation: a fresh nonce sentinel, PYTHONPATH "
            "sitecustomize network/process denial, RLIMIT_CPU, and "
            "HOME/TMPDIR pointed at a throwaway directory.",
        ]
    )
    return lines


def build_results_document(
    results: Sequence[dict[str, Any]],
    *,
    generated_at: datetime,
    timeout_seconds: float,
    warmups: Sequence[dict[str, Any]] = (),
    endpoint: str = ENDPOINT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = aggregate(results)
    prov = provenance or {}
    harness_sha = prov.get("harness_sha256") or file_sha256(Path(__file__))
    tasks_sha = prov.get("tasks_sha256") or file_sha256(TASKS_PATH)
    prompts_sha = dict(prov.get("prompts_sha256") or {})
    return {
        "schema_version": 2,
        "mode": "run",
        "generated_at": _iso(generated_at),
        "endpoint": endpoint,
        "loopback_only": True,
        "models": {"cheap": CHEAP_MODEL, "strong": STRONG_MODEL},
        "arms": {
            arm: ARM_DESCRIPTIONS[arm] for arm in ARMS
        },
        "router": {
            "mode": "library-level",
            "module": "teamcraft.router",
            "high_risk_min_assurance": 2,
        },
        "harness_sha256": harness_sha,
        "tasks_sha256": tasks_sha,
        "prompts_sha256": prompts_sha,
        "timeout_seconds": timeout_seconds,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "model_switch_occurred": any(
            summary[arm]["model_switch_occurred"] for arm in ARMS
        ),
        "limits": build_limits(
            results, summary=summary, warmups=warmups, max_tokens=max_tokens
        ),
        "warmups": [dict(item) for item in warmups],
        "summary": summary,
        "decisions": decision_distribution(results),
        "results": [sanitize_result(row) for row in results],
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_summary(document: dict[str, Any]) -> str:
    prompts = document.get("prompts_sha256") or {}
    lines = [
        "# M5-06 local pilot — loopback oMLX single vs mixed",
        "",
        f"- generated_at: `{document['generated_at']}`",
        f"- endpoint: `{document['endpoint']}` (loopback only)",
        f"- models: cheap=`{document['models']['cheap']}`, "
        f"strong=`{document['models']['strong']}`",
        f"- arms: single={ARM_DESCRIPTIONS['single']}; "
        f"mixed={ARM_DESCRIPTIONS['mixed']}",
        f"- router: {document['router']['mode']} "
        f"(`{document['router']['module']}`, "
        f"high_risk_min_assurance={document['router']['high_risk_min_assurance']})",
        f"- temperature={document['temperature']}, "
        f"max_tokens={document['max_tokens']}, N=1 per (arm, task)",
        f"- harness_sha256: `{document.get('harness_sha256')}`",
        f"- tasks_sha256: `{document.get('tasks_sha256')}`",
        f"- prompts_sha256: "
        + ", ".join(f"`{task_id}`={digest}" for task_id, digest in sorted(prompts.items())),
        "",
        "## Arm summary",
        "",
        "| arm | tasks | executed | succeeded | failed | refused | "
        "success/executed | success/total | wall_model_s | generation_s | "
        "verifier_s | prompt_tokens | completion_tokens | reasoning_tokens | "
        "truncated | usage_unknown |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in ARMS:
        item = document["summary"][arm]
        lines.append(
            f"| {arm} | {item['tasks']} | {item['executed']} | "
            f"{item['succeeded']} | {item['failed']} | {item['refused']} | "
            f"{_fmt(item['success_rate_executed'])} | "
            f"{_fmt(item['success_rate_total'])} | "
            f"{_fmt(item.get('wall_model_seconds'))} | "
            f"{_fmt(item.get('generation_seconds'))} | "
            f"{_fmt(item['verifier_seconds'])} | "
            f"{_fmt(item['prompt_tokens'])} | "
            f"{_fmt(item['completion_tokens'])} | "
            f"{_fmt(item.get('reasoning_tokens'))} | "
            f"{_fmt(item.get('truncated'))} | "
            f"{item['tasks_with_unknown_usage']} |"
        )

    lines += [
        "",
        "`wall_model_seconds` includes model load and model switching; "
        "`generation_seconds` sums `usage.generation_duration` and excludes "
        "load time.",
        "",
        "## Warmups (both models before each arm)",
        "",
        "| arm | model | wall_s | load_s | generation_s | prompt_tokens | "
        "completion_tokens | reasoning_tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in document.get("warmups", []):
        timings = item.get("usage_timings") or {}
        lines.append(
            f"| {_fmt(item.get('arm'))} | {_fmt(item.get('model'))} | "
            f"{_fmt(item.get('seconds'))} | "
            f"{_fmt(timings.get('model_load_duration'))} | "
            f"{_fmt(timings.get('generation_duration'))} | "
            f"{_fmt(item.get('prompt_tokens'))} | "
            f"{_fmt(item.get('completion_tokens'))} | "
            f"{_fmt(item.get('reasoning_tokens'))} |"
        )

    lines += [
        "",
        "## Router decisions",
        "",
        "| arm | reason_code | effective_risk | model | count |",
        "|---|---|---|---|---|",
    ]
    for item in document["decisions"]:
        lines.append(
            f"| {item['arm']} | {item['reason_code']} | "
            f"{item['effective_risk']} | {_fmt(item['model'])} | {item['count']} |"
        )

    lines += [
        "",
        "## Per (arm, task)",
        "",
        "| arm | task | risk | tags | model | success | finish | wall_s | "
        "gen_s | verifier_s | tokens p/c/r | truncated | error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in document["results"]:
        tokens = (
            f"{_fmt(row.get('prompt_tokens'))}/"
            f"{_fmt(row.get('completion_tokens'))}/"
            f"{_fmt(row.get('reasoning_tokens'))}"
        )
        timings = row.get("usage_timings") or {}
        truncated = "yes" if (
            row.get("finish_reason") == "length"
            or row.get("content_empty") is True
        ) else "no"
        lines.append(
            f"| {row.get('arm')} | {row.get('task_id')} | {row.get('risk')} | "
            f"{', '.join(row.get('tags') or []) or '-'} | "
            f"{_fmt(row.get('model'))} | {_fmt(row.get('success'))} | "
            f"{_fmt(row.get('finish_reason'))} | "
            f"{_fmt(row.get('model_seconds'))} | "
            f"{_fmt(timings.get('generation_duration'))} | "
            f"{_fmt(row.get('verifier_seconds'))} | {tokens} | "
            f"{truncated} | {_fmt(row.get('error'))} |"
        )

    lines += [
        "",
        "## Provenance",
        "",
        f"- harness_sha256: `{document.get('harness_sha256')}`",
        f"- tasks_sha256: `{document.get('tasks_sha256')}`",
        f"- max_tokens: {document.get('max_tokens')}",
        f"- model_switch_occurred: {document.get('model_switch_occurred')}",
        "",
        "## Limitations and measured limits (must be quoted with these numbers)",
        "",
    ]
    lines.extend(f"- {item}" for item in document["limits"])
    lines += [
        "",
        "`n/a` means not measured/not applicable. Router refusal is recorded as "
        "`router_refused` and is **not** replaced by a silent fallback.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(output_dir: str | Path, document: dict[str, Any]) -> tuple[Path, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results_path = root / "results.json"
    summary_path = root / "summary.md"
    results_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_path.write_text(render_summary(document), encoding="utf-8")
    return results_path, summary_path


def _warmup(
    arm: str,
    model: str,
    *,
    origin: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        document, seconds = call_chat(
            model,
            [{"role": "user", "content": "Reply with OK."}],
            max_tokens=WARMUP_TOKENS,
            timeout_seconds=timeout_seconds,
            base_url=origin,
        )
    except ModelTimeout as exc:
        raise InfrastructureError(
            f"warmup timed out for {model} ({arm}): {exc}"
        ) from exc
    try:
        response_message(document)
    except ModelResponseError as exc:
        raise InfrastructureError(
            f"warmup returned an unusable response for {model} ({arm}): {exc}"
        ) from exc
    prompt_tokens, completion_tokens = usage_tokens(document)
    reasoning_tokens, timings = response_usage_metadata(document)
    return {
        "arm": arm,
        "model": model,
        "seconds": round(seconds, 3),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "usage_timings": timings,
    }


def run_pilot(
    tasks: Sequence[Task],
    *,
    output_dir: str | Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    base_url: str = ENDPOINT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Warm both models before each arm, run every (arm, task), write results."""

    origin = validate_endpoint(base_url)
    now = datetime.now(timezone.utc)
    candidates = build_candidates(now)
    provenance = compute_provenance(tasks)

    warmups: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for arm in ARMS:
        # Warm both models before the arm so no task pays the cold load and a
        # warmup never lands inside a task's model_seconds.
        for model in MODELS:
            warmups.append(
                _warmup(
                    arm,
                    model,
                    origin=origin,
                    timeout_seconds=timeout_seconds,
                )
            )

        for task in tasks:
            selection = route_arm(
                arm, task, candidates=candidates, now=now
            )
            record = selection_result(arm, task, selection)
            if not record["executed"]:
                results.append(record)
                continue

            model = record["model"]
            try:
                document, model_seconds = call_chat(
                    model,
                    [{"role": "user", "content": task.prompt}],
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                    base_url=origin,
                )
            except ModelTimeout as exc:
                record["model_seconds"] = round(timeout_seconds, 3)
                record["success"] = False
                record["error"] = clean_error(str(exc))
                results.append(record)
                continue
            except ModelResponseError as exc:
                record["success"] = False
                record["error"] = clean_error(str(exc))
                results.append(record)
                continue

            record["model_seconds"] = round(model_seconds, 3)
            record["finish_reason"] = response_finish_reason(document)
            reasoning_tokens, timings = response_usage_metadata(document)
            record["reasoning_tokens"] = reasoning_tokens
            record["usage_timings"] = timings
            try:
                message = response_message(document)
                prompt_tokens, completion_tokens = usage_tokens(document)
                record["prompt_tokens"] = prompt_tokens
                record["completion_tokens"] = completion_tokens
                record.update(
                    store_content(
                        output_dir, arm, task.id, message.get("content")
                    )
                )
                code = extract_code(message.get("content"))
            except ModelResponseError as exc:
                record["success"] = False
                record["error"] = clean_error(str(exc))
                results.append(record)
                continue
            except CodeExtractionError as exc:
                record["success"] = False
                record["error"] = clean_error(str(exc))
                results.append(record)
                continue

            outcome = run_verifier(
                task, code, timeout_seconds=timeout_seconds
            )
            record["verifier_seconds"] = round(outcome.seconds, 3)
            record["success"] = outcome.success
            record["error"] = outcome.error
            record["sentinel_ok"] = outcome.sentinel_ok
            record["verifier_returncode"] = outcome.returncode
            model_part = record["model_seconds"] or 0.0
            verifier_part = record["verifier_seconds"] or 0.0
            record["total_seconds"] = round(model_part + verifier_part, 3)
            results.append(record)

    document = build_results_document(
        results,
        generated_at=now,
        timeout_seconds=timeout_seconds,
        warmups=warmups,
        endpoint=origin,
        max_tokens=max_tokens,
        provenance=provenance,
    )
    write_outputs(output_dir, document)
    return document


def _change_reason(old_success: Any, new_success: bool) -> str:
    if not isinstance(old_success, bool):
        return "old_verdict_unknown"
    if old_success == new_success:
        return "verifier_still_passes" if new_success else "verifier_still_fails"
    return "fixed_verifier_accepts" if new_success else "fixed_verifier_rejects"


def _replay_one(
    task: Task,
    row: dict[str, Any],
    *,
    contents_root: Path,
    timeout_seconds: float,
    python: str | None,
) -> dict[str, Any]:
    """Re-judge one retained ``message.content`` with the current verifier."""

    arm = row.get("arm")
    task_id = task.id
    executed = row.get("executed") is True
    old_success = row.get("success")
    record: dict[str, Any] = {
        "arm": arm,
        "task_id": task_id,
        "executed": executed,
        "old_success": old_success if isinstance(old_success, bool) else None,
        "new_success": None,
        "changed": False,
        "reason": None,
        "old_error": clean_error(row.get("error")) if row.get("error") else None,
        "new_error": None,
        "verifier_returncode": None,
        "sentinel_ok": None,
        "content_sha256": None,
    }
    if not executed:
        record["reason"] = "not_executed"
        return record

    expected = row.get("content_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ReplayError(
            f"{arm}/{task_id}: results.json lacks a valid content_sha256"
        )
    path = contents_root / str(arm) / f"{task_id}.txt"
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReplayError(
            f"{arm}/{task_id}: cannot read retained content {path}: {exc}"
        ) from exc
    actual = sha256_bytes(data)
    if actual != expected:
        raise ReplayError(
            f"{arm}/{task_id}: retained content hash mismatch "
            f"(results.json={expected}, file={actual})"
        )
    record["content_sha256"] = actual

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReplayError(
            f"{arm}/{task_id}: retained content is not valid UTF-8"
        ) from exc
    try:
        code = extract_code(text)
    except CodeExtractionError as exc:
        record["new_success"] = False
        record["new_error"] = clean_error(f"code_extraction_error: {exc}")
        record["reason"] = _change_reason(record["old_success"], False)
        record["changed"] = (
            isinstance(record["old_success"], bool)
            and record["old_success"] is True
        )
        return record

    outcome = run_verifier(
        task, code, timeout_seconds=timeout_seconds, python=python
    )
    record["new_success"] = outcome.success
    record["new_error"] = outcome.error
    record["verifier_returncode"] = outcome.returncode
    record["sentinel_ok"] = outcome.sentinel_ok
    record["reason"] = _change_reason(record["old_success"], outcome.success)
    record["changed"] = (
        isinstance(record["old_success"], bool)
        and record["old_success"] != outcome.success
    )
    return record


def replay_results(
    results_dir: str | Path,
    *,
    output_dir: str | Path,
    tasks: Sequence[Task] | None = None,
    selected_ids: set[str] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
) -> dict[str, Any]:
    """Re-judge retained outputs read-only and write ``replay.json``.

    The source ``results.json`` and ``contents/`` are never modified.  Every
    retained file must match its recorded ``content_sha256``; a mismatch is a
    hard error.  No model request is made and no network is used.
    """

    source_root = Path(results_dir)
    results_path = source_root / "results.json"
    try:
        source = json.loads(results_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReplayError(f"cannot read {results_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{results_path} is not valid JSON: {exc}") from exc
    if not isinstance(source, dict):
        raise ReplayError(f"{results_path} must contain a JSON object")
    source_rows = source.get("results")
    if not isinstance(source_rows, list) or not source_rows:
        raise ReplayError(f"{results_path} has no results to replay")

    task_list = tuple(tasks) if tasks is not None else load_tasks()
    by_id = {task.id: task for task in task_list}
    contents_root = source_root / "contents"
    if not contents_root.is_dir():
        raise ReplayError(f"missing retained contents directory: {contents_root}")

    replay_rows: list[dict[str, Any]] = []
    old_rows: list[dict[str, Any]] = []
    new_rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for index, row in enumerate(source_rows):
        if not isinstance(row, dict):
            raise ReplayError(f"results[{index}] must be an object")
        arm = row.get("arm")
        task_id = row.get("task_id")
        if arm not in ARMS:
            raise ReplayError(f"results[{index}] has unknown arm: {arm!r}")
        if not isinstance(task_id, str) or task_id not in by_id:
            raise ReplayError(f"results[{index}] has unknown task_id: {task_id!r}")
        key = (arm, task_id)
        if key in seen:
            raise ReplayError(f"duplicate result for {arm}/{task_id}")
        seen.add(key)
        if selected_ids is not None and task_id not in selected_ids:
            continue

        record = _replay_one(
            by_id[task_id],
            row,
            contents_root=contents_root,
            timeout_seconds=timeout_seconds,
            python=python,
        )
        replay_rows.append(record)
        old_rows.append(row)
        new_row = dict(row)
        if row.get("executed") is True:
            new_row["success"] = record["new_success"]
            new_row["error"] = record["new_error"]
        new_rows.append(new_row)

    old_summary = aggregate(old_rows)
    new_summary = aggregate(new_rows)
    changed = [row for row in replay_rows if row["changed"]]

    arms_summary: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [row for row in replay_rows if row["arm"] == arm]
        arms_summary[arm] = {
            "tasks": old_summary[arm]["tasks"],
            "executed": old_summary[arm]["executed"],
            "old_succeeded": old_summary[arm]["succeeded"],
            "new_succeeded": new_summary[arm]["succeeded"],
            "old_success_rate_executed": old_summary[arm][
                "success_rate_executed"
            ],
            "fixed_success_rate_executed": new_summary[arm][
                "success_rate_executed"
            ],
            "changed_tasks": [row["task_id"] for row in arm_rows if row["changed"]],
        }

    source_max_tokens = source.get("max_tokens")
    if not isinstance(source_max_tokens, int) or isinstance(source_max_tokens, bool):
        source_max_tokens = DEFAULT_MAX_TOKENS
    source_warmups = source.get("warmups")
    if not isinstance(source_warmups, list):
        source_warmups = []

    generated_at = datetime.now(timezone.utc)
    document = {
        "schema_version": 1,
        "mode": "replay",
        "generated_at": _iso(generated_at),
        "source_results": str(results_path),
        "source_generated_at": source.get("generated_at"),
        "source_harness_sha256": source.get("harness_sha256"),
        "source_tasks_sha256": source.get("tasks_sha256"),
        "read_only_source": True,
        "network_access": False,
        "verifier_fixed": True,
        "harness_sha256": file_sha256(Path(__file__)),
        "tasks_sha256": file_sha256(TASKS_PATH),
        "prompts_sha256": {
            task.id: sha256_text(task.prompt) for task in task_list
        },
        "timeout_seconds": timeout_seconds,
        "max_tokens": source_max_tokens,
        "limits": build_limits(
            new_rows,
            summary=new_summary,
            warmups=source_warmups,
            max_tokens=source_max_tokens,
        ),
        "summary": {
            "old": old_summary,
            "new": new_summary,
            "with_fixed_verifier": arms_summary,
            "changed_count": len(changed),
            "changed": [
                {
                    "arm": row["arm"],
                    "task_id": row["task_id"],
                    "old_success": row["old_success"],
                    "new_success": row["new_success"],
                    "reason": row["reason"],
                }
                for row in changed
            ],
        },
        "results": replay_rows,
    }

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "replay.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def build_dry_run(
    tasks: Sequence[Task], *, max_tokens: int = DEFAULT_MAX_TOKENS
) -> dict[str, Any]:
    """Build a deterministic plan without touching the network or clock."""

    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidates = build_candidates(fixed_now)
    provenance = compute_provenance(tasks)
    matrix: dict[str, list[dict[str, Any]]] = {}
    for arm in ARMS:
        matrix[arm] = []
        for task in tasks:
            selection = route_arm(
                arm, task, candidates=candidates, now=fixed_now
            )
            matrix[arm].append(
                {
                    "task_id": task.id,
                    "risk": task.risk,
                    "tags": list(task.tags),
                    "model": selection.model,
                    "reason_code": selection.reason_code,
                    "effective_risk": selection.effective_risk,
                }
            )
    return {
        "schema_version": 2,
        "mode": "dry-run",
        "endpoint": ENDPOINT,
        "loopback_only": True,
        "router": {"mode": "library-level", "module": "teamcraft.router"},
        "models": {"cheap": CHEAP_MODEL, "strong": STRONG_MODEL},
        "arms": {arm: ARM_DESCRIPTIONS[arm] for arm in ARMS},
        "harness_sha256": provenance["harness_sha256"],
        "tasks_sha256": provenance["tasks_sha256"],
        "prompts_sha256": provenance["prompts_sha256"],
        "max_tokens": max_tokens,
        "tasks": [
            {"id": task.id, "title": task.title, "risk": task.risk, "tags": list(task.tags)}
            for task in tasks
        ],
        "matrix": matrix,
        "note": "Dry run: no model calls were made; the fixed synthetic clock "
        "makes this output deterministic.",
    }


def render_dry_run(
    tasks: Sequence[Task], *, max_tokens: int = DEFAULT_MAX_TOKENS
) -> str:
    return json.dumps(
        build_dry_run(tasks, max_tokens=max_tokens),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _select_tasks(tasks: Sequence[Task], wanted: Sequence[str]) -> tuple[Task, ...]:
    by_id = {task.id: task for task in tasks}
    unknown = [task_id for task_id in wanted if task_id not in by_id]
    if unknown:
        raise TaskSetError(f"unknown task id(s): {', '.join(unknown)}")
    wanted_set = set(wanted)
    return tuple(task for task in tasks if task.id in wanted_set)


def _task_failures(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in document.get("results", [])
        if not (row.get("executed") is True and row.get("success") is True)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="m506_pilot.py",
        description="M5-06 loopback oMLX single-vs-mixed diagnostic pilot.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the deterministic arm/task/model matrix (default)",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="run the pilot and write results.json + summary.md",
    )
    mode.add_argument(
        "--replay",
        action="store_true",
        help="re-judge retained contents with the current verifiers "
        "(read-only, no model calls) and write replay.json",
    )
    parser.add_argument(
        "--results",
        metavar="DIR",
        help="previous results directory to replay (with --replay)",
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        help="output directory for --run or --replay",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="ID",
        help="run only this task id (repeatable)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="per-request and per-verifier timeout (default: 180)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help="max_tokens for every task request (default: 8192)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 3 when any (arm, task) fails; default is 0 for task-level "
        "failures (infrastructure failures are always non-zero)",
    )
    args = parser.parse_args(argv)

    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")

    try:
        tasks = load_tasks()
        if args.task:
            tasks = _select_tasks(tasks, args.task)
        origin = validate_endpoint(ENDPOINT)
    except PilotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.run and not args.replay:
        print(render_dry_run(tasks, max_tokens=args.max_tokens))
        return 0

    if args.replay:
        if not args.results:
            parser.error("--replay requires --results DIR")
        if not args.output:
            parser.error("--replay requires --output DIR")
        selected = {task.id for task in tasks} if args.task else None
        try:
            document = replay_results(
                args.results,
                output_dir=args.output,
                tasks=tasks,
                selected_ids=selected,
                timeout_seconds=args.timeout_seconds,
            )
        except ReplayError as exc:
            print(f"replay error: {exc}", file=sys.stderr)
            return 3
        print(f"wrote {Path(args.output) / 'replay.json'}")
        for arm in ARMS:
            item = document["summary"]["with_fixed_verifier"][arm]
            print(
                f"{arm}: old {item['old_succeeded']}/{item['executed']} -> "
                f"fixed {item['new_succeeded']}/{item['executed']} "
                f"(old_rate={_fmt(item['old_success_rate_executed'])}, "
                f"fixed_rate={_fmt(item['fixed_success_rate_executed'])})"
            )
        changed = document["summary"]["changed"]
        if changed:
            print(f"changed verdicts: {len(changed)}")
            for item in changed:
                print(
                    f"  {item['arm']}/{item['task_id']}: "
                    f"{item['old_success']} -> {item['new_success']} "
                    f"({item['reason']})"
                )
        else:
            print("changed verdicts: 0")
        return 0

    if not args.output:
        parser.error("--run requires --output DIR")

    try:
        document = run_pilot(
            tasks,
            output_dir=args.output,
            timeout_seconds=args.timeout_seconds,
            base_url=origin,
            max_tokens=args.max_tokens,
        )
    except ModelTimeout as exc:
        print(f"infrastructure error: {exc}", file=sys.stderr)
        return 3
    except PilotError as exc:
        print(f"infrastructure error: {exc}", file=sys.stderr)
        return 3

    print(
        f"wrote {Path(args.output) / 'results.json'} and "
        f"{Path(args.output) / 'summary.md'}"
    )
    for arm in ARMS:
        item = document["summary"][arm]
        print(
            f"{arm}: {item['succeeded']}/{item['executed']} succeeded; "
            f"wall_model_seconds={_fmt(item.get('wall_model_seconds'))}, "
            f"generation_seconds={_fmt(item.get('generation_seconds'))}, "
            f"truncated={_fmt(item.get('truncated'))}"
        )
    failures = _task_failures(document)
    if failures and args.strict:
        print(
            f"strict: {len(failures)} (arm, task) failure(s)", file=sys.stderr
        )
        return 3
    return 0


__all__ = [
    "ARMS",
    "ArmSelection",
    "CHEAP_MODEL",
    "CodeExtractionError",
    "DEFAULT_MAX_TOKENS",
    "ENDPOINT",
    "InfrastructureError",
    "MODELS",
    "ModelResponseError",
    "ModelTimeout",
    "PilotError",
    "ReplayError",
    "SENTINEL_ENV",
    "STRONG_MODEL",
    "Task",
    "TaskSetError",
    "VerifierOutcome",
    "aggregate",
    "base_result",
    "build_candidates",
    "build_dry_run",
    "build_limits",
    "build_results_document",
    "build_snapshots",
    "call_chat",
    "clean_error",
    "compute_provenance",
    "decision_distribution",
    "extract_code",
    "file_sha256",
    "load_tasks",
    "main",
    "redact_text",
    "render_dry_run",
    "render_summary",
    "replay_results",
    "response_finish_reason",
    "response_message",
    "response_usage_metadata",
    "route_arm",
    "run_pilot",
    "run_verifier",
    "sanitize_result",
    "selection_result",
    "sha256_bytes",
    "sha256_text",
    "store_content",
    "usage_tokens",
    "validate_endpoint",
    "verifier_source",
    "write_outputs",
]

if __name__ == "__main__":
    raise SystemExit(main())
