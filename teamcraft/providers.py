"""M5-01 Harness/Provider capability discovery and Attempt snapshots.

The registry is an append-only projection over the existing event store.  Probe
code accepts no credentials, reads no environment variables, and the oMLX
transport is restricted to an unauthenticated loopback HTTP endpoint.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from . import events, storage
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, StorageError, ValidationError

CAPABILITY_PROBE_EVENT = "provider.capability_probe"
ATTEMPT_CAPABILITY_EVENT = "attempt.capability_snapshot"
HEALTH_STATES = frozenset({"healthy", "unhealthy", "unknown", "unsupported"})
CAPABILITY_STATES = frozenset({"supported", "unsupported", "unknown"})
DEFAULT_TTL_SECONDS = 300
MAX_HARNESS_VERSION_OUTPUT_BYTES = 4096
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,199}\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+:/()-]{0,199}\Z")
_VERSION_DIAGNOSTIC = re.compile(r"(?:warning|warn|error|platformerror)\b", re.I)
_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|bearer|password|secret|token)", re.I)

ProcessRunner = Callable[[Sequence[str], str, float], tuple[int, str]]
HttpGetter = Callable[[str, float], tuple[int, bytes]]


def _now(clock: Clock | None) -> datetime:
    source = clock if clock is not None else SystemClock()
    return source.now().astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("capability snapshot has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("capability snapshot timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_name(value: str | None, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValidationError(f"{field} must be a bounded safe identifier")
    return value


def _safe_version(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not _SAFE_VERSION.fullmatch(text):
        raise ValidationError("version must be bounded printable metadata")
    return text


def _reject_secrets(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ValidationError(f"{path} contains a forbidden credential-shaped field")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and re.search(r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}", value, re.I):
        raise ValidationError(f"{path} contains credential-shaped text")


@dataclass(frozen=True)
class CapabilitySnapshot:
    entity_type: str
    name: str
    version: str | None
    provider: str | None
    models: tuple[str, ...]
    discovery: Mapping[str, str]
    capabilities: Mapping[str, str]
    health: str
    reason_code: str
    source: str
    observed_at: str
    valid_until: str

    def __post_init__(self) -> None:
        if self.entity_type not in {"harness", "provider"}:
            raise ValidationError("entity_type must be harness or provider")
        _safe_name(self.name, "name")
        _safe_version(self.version)
        _safe_name(self.provider, "provider", optional=True)
        if self.health not in HEALTH_STATES:
            raise ValidationError("unknown health state")
        _safe_name(self.reason_code, "reason_code")
        if not isinstance(self.source, str) or not self.source:
            raise ValidationError("source must not be blank")
        observed, valid = _parse_time(self.observed_at), _parse_time(self.valid_until)
        if valid < observed:
            raise ValidationError("valid_until cannot precede observed_at")
        for model in self.models:
            _safe_name(model, "model")
        for mapping_name, mapping in (("discovery", self.discovery), ("capabilities", self.capabilities)):
            for key, state in mapping.items():
                _safe_name(str(key), f"{mapping_name} key")
                if state not in CAPABILITY_STATES:
                    raise ValidationError(f"{mapping_name} state must be supported, unsupported, or unknown")
        _reject_secrets(self.payload())

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["models"] = list(self.models)
        value["discovery"] = dict(self.discovery)
        value["capabilities"] = dict(self.capabilities)
        return value

    @property
    def snapshot_digest(self) -> str:
        return _digest(self.payload())

    def effective_health(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stale = current > _parse_time(self.valid_until)
        if stale:
            return {"status": "stale", "healthy": False, "reason_code": "probe_stale"}
        return {
            "status": self.health,
            "healthy": self.health == "healthy",
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "CapabilitySnapshot":
        try:
            return cls(
                entity_type=str(value["entity_type"]), name=str(value["name"]),
                version=None if value.get("version") is None else str(value["version"]),
                provider=None if value.get("provider") is None else str(value["provider"]),
                models=tuple(str(item) for item in value.get("models", [])),
                discovery={str(k): str(v) for k, v in dict(value["discovery"]).items()},
                capabilities={str(k): str(v) for k, v in dict(value["capabilities"]).items()},
                health=str(value["health"]), reason_code=str(value["reason_code"]),
                source=str(value["source"]), observed_at=str(value["observed_at"]),
                valid_until=str(value["valid_until"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed capability snapshot payload") from exc


def _default_process_runner(argv: Sequence[str], cwd: str, timeout: float) -> tuple[int, str]:
    # Harness version probes must not consult the user's config, logs, cache,
    # credentials, or proxy settings.  A fresh HOME/XDG scope also gives tools
    # such as OpenCode a writable log location without touching user state.
    with tempfile.TemporaryDirectory(prefix="teamcraft-harness-probe-") as home:
        root = Path(home)
        config, data, cache = root / "config", root / "data", root / "cache"
        for directory in (config, data, cache):
            directory.mkdir(mode=0o700)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
            "XDG_CACHE_HOME": str(cache),
        }
        try:
            result = subprocess.run(
                list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=timeout, check=False, env=env,
            )
        except FileNotFoundError:
            return 127, ""
        except subprocess.TimeoutExpired:
            return 124, ""
        except OSError:
            return 126, ""
    return int(result.returncode), (result.stdout or "")[:MAX_HARNESS_VERSION_OUTPUT_BYTES]


def _bounded_version(output: str) -> str | None:
    for raw_line in output[:MAX_HARNESS_VERSION_OUTPUT_BYTES].splitlines():
        line = raw_line.strip()
        if (not line or len(line) > 200 or _VERSION_DIAGNOSTIC.match(line)
                or not any(char.isdigit() for char in line)):
            continue
        if _SAFE_VERSION.fullmatch(line):
            return line
    return None


def probe_harness(name: str, project_root: str | Path, *, runner: ProcessRunner | None = None,
                  clock: Clock | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> CapabilitySnapshot:
    name = str(_safe_name(name, "harness"))
    if ttl_seconds <= 0:
        raise ValidationError("ttl_seconds must be positive")
    observed = _now(clock)
    if name == "claude":
        return CapabilitySnapshot("harness", name, None, None, (),
            {"skill_rules": "unsupported", "project_rules": "unsupported"},
            {"event_stream": "unknown", "prompt_ack": "unknown"},
            "unsupported", "adapter_pending", "static_policy", _iso(observed),
            _iso(observed + timedelta(seconds=ttl_seconds)))
    executable = {"codex": "codex", "opencode": "opencode"}.get(name)
    if executable is None:
        raise ValidationError("unsupported harness name")
    root = Path(project_root).resolve()
    run = runner or _default_process_runner
    code, output = run((executable, "--version"), str(root), 5.0)
    skill = (root / ".agents/skills/teamcraft-ceo/SKILL.md").is_file()
    rules = ((root / "AGENTS.md").is_file() if name == "codex"
             else (root / ".opencode/agents").is_dir())
    return CapabilitySnapshot("harness", name, _bounded_version(output), None, (),
        {"skill_rules": "supported" if skill else "unsupported",
         "project_rules": "supported" if rules else "unsupported"},
        {"event_stream": "unknown", "prompt_ack": "unknown"},
        "healthy" if code == 0 else "unhealthy",
        "ok" if code == 0 else ("executable_missing" if code == 127 else "version_probe_failed"),
        "local_process", _iso(observed), _iso(observed + timedelta(seconds=ttl_seconds)))


def _loopback_models_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        try:
            # Literal addresses only: accepting ``localhost`` would make the
            # trust decision depend on mutable hosts/DNS resolution.
            loopback = host is not None and ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    except ValueError as exc:
        raise ValidationError("oMLX base URL is invalid") from exc
    if parsed.scheme != "http" or not loopback or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError("oMLX probe requires an unauthenticated loopback http URL")
    path = parsed.path.rstrip("/")
    if path.endswith("/models"):
        model_path = path
    elif path.endswith("/v1"):
        model_path = path + "/models"
    else:
        model_path = path + "/v1/models"
    return urlunsplit((parsed.scheme, parsed.netloc, model_path, "", ""))


def _default_http_get(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    # A provider health probe must not inherit HTTP(S)_PROXY/NO_PROXY or any
    # authentication behavior from the host environment.  The endpoint is
    # already constrained to loopback; this explicit empty ProxyHandler keeps
    # urllib from consulting ambient proxy configuration on the request path.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(262145)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


def probe_omlx(base_url: str = "http://127.0.0.1:8000/v1", *, getter: HttpGetter | None = None,
               clock: Clock | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> CapabilitySnapshot:
    if ttl_seconds <= 0:
        raise ValidationError("ttl_seconds must be positive")
    url = _loopback_models_url(base_url)
    observed = _now(clock)
    status, body = (getter or _default_http_get)(url, 5.0)
    models: tuple[str, ...] = ()
    version: str | None = None
    reason = "service_disconnected" if status == 0 else "http_error"
    health = "unhealthy"
    if status == 200 and len(body) <= 262144:
        try:
            document = json.loads(body.decode("utf-8"))
            raw_models = document.get("data", [])
            models = tuple(sorted({str(item["id"]) for item in raw_models
                                   if isinstance(item, dict) and _SAFE_NAME.fullmatch(str(item.get("id", ""))) }))
            version = _safe_version(document.get("version"))
            health, reason = "healthy", "ok"
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValidationError):
            reason = "invalid_response"
    return CapabilitySnapshot("provider", "omlx", version, "openai_compatible", models,
        {"model_catalog": "supported" if health == "healthy" else "unknown"},
        {"inference_request": "supported", "event_stream": "unsupported", "prompt_ack": "unsupported"},
        health, reason, url, _iso(observed), _iso(observed + timedelta(seconds=ttl_seconds)))


def record_capability_probe(conn: storage.TeamCraftConnection, project_id: str,
                            snapshot: CapabilitySnapshot) -> events.Event:
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise NotFoundError(f"project not found: {project_id}")
    payload = {"schema_version": 1, "snapshot": snapshot.payload(),
               "snapshot_digest": snapshot.snapshot_digest}
    _reject_secrets(payload)
    event_id = "evt-cap-" + snapshot.snapshot_digest[:40]
    event, _ = events.append_event_idempotent(conn, CAPABILITY_PROBE_EVENT,
        project_id=project_id, payload_json=_canonical(payload), event_id=event_id)
    return event


def latest_capability_probes(conn: storage.TeamCraftConnection, project_id: str) -> tuple[CapabilitySnapshot, ...]:
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE project_id = ? AND kind = ? ORDER BY seq DESC",
        (project_id, CAPABILITY_PROBE_EVENT)).fetchall()
    latest: dict[tuple[str, str], CapabilitySnapshot] = {}
    for row in rows:
        try:
            snapshot = CapabilitySnapshot.from_payload(json.loads(str(row[0]))["snapshot"])
        except (json.JSONDecodeError, KeyError, ValidationError) as exc:
            raise ConflictError("stored capability probe is malformed") from exc
        latest.setdefault((snapshot.entity_type, snapshot.name), snapshot)
    return tuple(latest[key] for key in sorted(latest))


def bind_attempt_capabilities(conn: storage.TeamCraftConnection, attempt_id: str, *,
                              harness: CapabilitySnapshot, provider: CapabilitySnapshot,
                              model: str, clock: Clock | None = None) -> events.Event:
    model = str(_safe_name(model, "model"))
    if harness.entity_type != "harness" or provider.entity_type != "provider":
        raise ValidationError("Attempt selection requires one harness and one provider snapshot")
    row = conn.execute("SELECT a.task_id, t.project_id FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
                       (attempt_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"attempt not found: {attempt_id}")
    for label, snapshot in (("harness", harness), ("provider", provider)):
        durable = conn.execute(
            "SELECT 1 FROM events WHERE project_id=? AND kind=?"
            " AND json_extract(payload_json, '$.snapshot_digest')=? LIMIT 1",
            (str(row[1]), CAPABILITY_PROBE_EVENT, snapshot.snapshot_digest),
        ).fetchone()
        if durable is None:
            raise ValidationError(
                f"selected {label} capability snapshot is not durably registered"
            )
        health = snapshot.effective_health(now=_now(clock))
        if not health["healthy"]:
            raise ValidationError(
                f"selected {label} capability snapshot is not currently healthy"
            )
    if model not in provider.models:
        raise ValidationError("selected model is absent from the provider capability snapshot")
    selection = {"harness": harness.name, "provider": provider.name, "model": model}
    payload = {"schema_version": 1, "attempt_id": attempt_id, "selection": selection,
               "harness_snapshot": harness.payload(), "provider_snapshot": provider.payload(),
               "harness_digest": harness.snapshot_digest, "provider_digest": provider.snapshot_digest}
    _reject_secrets(payload)
    try:
        event, _ = events.append_event_idempotent(conn, ATTEMPT_CAPABILITY_EVENT,
            project_id=str(row[1]), task_id=str(row[0]), attempt_id=attempt_id,
            payload_json=_canonical(payload), event_id="evt-atcap-" + hashlib.sha256(attempt_id.encode()).hexdigest()[:40])
    except StorageError as exc:
        raise ConflictError("Attempt capability selection is immutable") from exc
    return event


def get_attempt_capabilities(conn: storage.TeamCraftConnection, attempt_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload_json, occurred_at FROM events WHERE attempt_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
                       (attempt_id, ATTEMPT_CAPABILITY_EVENT)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
        harness = CapabilitySnapshot.from_payload(payload["harness_snapshot"])
        provider = CapabilitySnapshot.from_payload(payload["provider_snapshot"])
    except (json.JSONDecodeError, KeyError, ValidationError) as exc:
        raise ConflictError("stored Attempt capability snapshot is malformed") from exc
    if payload.get("harness_digest") != harness.snapshot_digest or payload.get("provider_digest") != provider.snapshot_digest:
        raise ConflictError("stored Attempt capability snapshot digest mismatch")
    return {
        **payload,
        "harness_effective_health": harness.effective_health(),
        "provider_effective_health": provider.effective_health(),
        "occurred_at": str(row[1]),
    }


__all__ = ["ATTEMPT_CAPABILITY_EVENT", "CAPABILITY_PROBE_EVENT", "CapabilitySnapshot",
           "bind_attempt_capabilities", "get_attempt_capabilities", "latest_capability_probes",
           "probe_harness", "probe_omlx", "record_capability_probe"]
