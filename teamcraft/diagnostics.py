"""M6-03 sanitized diagnostics bundle: local preview and export (M0 review 9.1 M6-03).

Contract pinned by this module and ``tests/test_diagnostics_m603.py``:

* **strict allowlist** — a bundle may contain ONLY the sections in
  :data:`SECTION_ALLOWLIST` (``version``, ``schema``, ``health``, ``events``,
  ``failures``).  Selection is explicit (unknown or duplicate section names are
  refused before anything is read); the default scope is
  :data:`DEFAULT_SECTIONS` and never includes ``failures`` free-text beyond
  the sanitized bounds;
* **nothing raw by default** — the module never reads ``os.environ``, never
  opens log files, and never includes event payloads, credentials, probe
  detail, or any source/absolute path.  Event evidence rides as per-kind
  count/seq summaries; log presence is reported as a digest, never a path or
  content;
* **sanitization before emission** — every string that can carry caller data
  passes exact-value secret scrubbing (caller-registered secrets, longest
  first — the M3-01 pattern, duplicated locally because the core package
  stays dependency- and ``teamcraft.api``-import-free), known secret-shape
  patterns, POSIX/home/Windows absolute-path replacement, control-character
  stripping, and a final residual scan that FAILS CLOSED
  (:class:`SanitizationRefusal`) rather than emit a possibly-leaking bundle;
* **evidenced failures only** — a failure record is accepted only with a
  64-hex evidence digest and only through a strict per-field allowlist;
  free-text ``detail`` is sanitized and hard-clipped
  (``detail_clipped: true`` marks the cut);
* **bounded size** — per-section lists are trimmed (with an honest
  ``truncated: true``) until each section fits :data:`MAX_SECTION_BYTES`,
  and a bundle over :data:`MAX_BUNDLE_BYTES` is refused outright;
* **deterministic manifest** — canonical JSON (sorted keys, fixed
  separators), fixed field set, no wall-clock read by this module: byte-for-byte
  identical inputs produce identical section bytes, manifest bytes, and
  :data:`DiagnosticsPreview.manifest_digest`.  ``generated_at`` is only ever
  what the caller explicitly supplies (otherwise ``null``);
* **preview/export scope parity** — :func:`export_bundle` writes EXACTLY the
  bytes the preview already produced (manifest plus one canonical JSON file per
  selected section) — export has no private data path of its own — and refuses
  to overwrite any existing target file before writing anything
  (fail-before-write, then O_EXCL per file), verifying every written file's
  SHA-256 against the manifest after the write.

Local-only by design: this module performs no network I/O and adds no
transport; a later route/CLI integration (see module history of M3-04/M6-02)
would call :func:`build_preview`/:func:`export_bundle` unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import SchemaOutOfDateError, StorageError, ValidationError
from .ids import require_entity_id
from .storage import APP_VERSION, SCHEMA_VERSION, TeamCraftConnection

__all__ = [
    "BUNDLE_NAME",
    "BUNDLE_FORMAT_VERSION",
    "DIAGNOSTICS_SCHEMA",
    "SECTION_ALLOWLIST",
    "DEFAULT_SECTIONS",
    "MANIFEST_FILENAME",
    "SECTIONS_DIRNAME",
    "MAX_SECTION_BYTES",
    "MAX_BUNDLE_BYTES",
    "SECRET_MARKER",
    "PATH_MARKER",
    "SanitizationRefusal",
    "BundleTooLargeError",
    "DiagnosticsPreview",
    "ExportedFile",
    "ExportResult",
    "build_preview",
    "export_bundle",
    "build_and_export",
]

#: Manifest identity.
BUNDLE_NAME = "teamcraft-diagnostics"
BUNDLE_FORMAT_VERSION = 1
DIAGNOSTICS_SCHEMA = "m603.1"

#: The only sections a bundle may ever contain (strict allowlist).
SECTION_ALLOWLIST: tuple[str, ...] = (
    "version",
    "schema",
    "health",
    "events",
    "failures",
)
#: Default scope — no failures section, and no raw input in any section.
DEFAULT_SECTIONS: tuple[str, ...] = ("version", "schema", "health", "events")

MANIFEST_FILENAME = "manifest.json"
SECTIONS_DIRNAME = "sections"

#: Hard size ceilings: five maximal sections plus the manifest must fit.
MAX_SECTION_BYTES = 24 * 1024
MAX_BUNDLE_BYTES = 128 * 1024
MAX_EVENT_KINDS = 100
MAX_SCHEMA_TABLES = 100
MAX_HEALTH_DEPENDENCIES = 100
MAX_FAILURE_RECORDS = 50
MAX_DETAIL_CHARS = 200

#: Markers substituted for scrubbed content (safe direction: over-redaction).
SECRET_MARKER = "[REDACTED]"
PATH_MARKER = "<path>"

#: Coarse health vocabulary mirroring ``teamcraft.api.health`` (that module
#: needs FastAPI, so the vocabulary — not the import — is shared).
HEALTH_STATUSES = ("up", "down", "unknown")
HEALTH_AGGREGATE_OK = "ok"
HEALTH_AGGREGATE_DEGRADED = "degraded"
HEALTH_AGGREGATE_UNKNOWN = "unknown"

#: Attempt/verification outcomes a failure record may report.
FAILURE_STATUSES = ("fail", "error", "stale", "blocked", "unknown")

#: Verifier profile vocabulary (M4-03 profiles; closed set).
VERIFIER_PROFILES = ("lint", "test", "typecheck", "secret_scan")

#: Allowed fields per failure record — anything else is refused outright.
_FAILURE_FIELDS = frozenset(
    {
        "kind",
        "status",
        "evidence_digest",
        "id",
        "task_id",
        "attempt_id",
        "exit_code",
        "profile",
        "occurred_at",
        "detail",
    }
)

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
_DIGEST64_RE = re.compile(r"^[0-9a-f]{64}$")
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Absolute-path shapes: POSIX absolute and home paths require at least two
#: segments ("/seg/seg", "~/seg/seg"); Windows drive paths take the rest of
#: the token.  Single-segment slashes ("/api") and bare "or/else" words are
#: left alone; "and/or/nor"-style prose CAN over-redact — the safe direction.
_POSIX_PATH_RE = re.compile(r"~?/(?:[\w.~@+\-]+/)+[\w.~@+\-]*")
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]*")

#: Known secret shapes used for BOTH substitution and residual scanning,
#: except the key=value rule (:data:`_KV_SECRET_RE`), which is scanned
#: structurally — a value that is nothing but markers is a scrub artifact,
#: not a leak.
_SECRET_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
        r"(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    ),
)

#: key=value secret rule: the key name survives, the value is replaced.  It
#: runs inside :func:`_shape_scrub` (so a joined ``key=<secret>junk`` collapses
#: to ``key=[REDACTED]``) and inside the residual scan, where a value made up
#: entirely of markers counts as a scrub artifact, not a leak.
_KV_SECRET_RE = re.compile(
    r"(?i)([A-Za-z0-9_\-]*(?:password|passwd|secret|token|api[-_]?key"
    r"|credential)[A-Za-z0-9_\-]*)\s*[:=]\s*([^\s,;]+)"
)


class SanitizationRefusal(ValidationError):
    """A value still looks secret/path-bearing after sanitization.

    Fail-closed: the offending content is never quoted back (that would be
    the leak), only the field name and the detection category.
    """

    def __init__(self, where: str, category: str) -> None:
        super().__init__(
            f"diagnostics input refused at {where}: residual {category} "
            "detected after sanitization; nothing was emitted for it"
        )
        self.where = where
        self.category = category


class BundleTooLargeError(ValidationError):
    """The selected scope cannot be serialized within the bundle budget."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_head(conn: TeamCraftConnection) -> int:
    """Read and enforce the head schema version (same gate as ``events``)."""

    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise StorageError(f"cannot read user_version: {exc}") from exc
    if version > SCHEMA_VERSION:
        raise SchemaOutOfDateError(
            f"database schema v{version} is newer than this build "
            f"supports ({SCHEMA_VERSION}); refusing to run"
        )
    if version < SCHEMA_VERSION:
        raise SchemaOutOfDateError(
            f"database schema v{version} lags this build ({SCHEMA_VERSION}); "
            "run teamcraft.migrate_to_head() first"
        )
    return version


# ---------------------------------------------------------------------------
# sanitization core
# ---------------------------------------------------------------------------


def _exact_scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Replace caller-registered secret values, longest first (M3-01 rule)."""

    result = text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret in result:
            result = result.replace(secret, SECRET_MARKER)
    return result


def _trim_partial_markers(text: str) -> str:
    """Drop a truncated marker left at the tail by a clip.

    A half marker (``password=[REDA``) would still read to the residual scan
    as a key=value with a value, i.e. a false refusal; trimming the tail
    keeps clipping honest without ever re-exposing content (markers only ever
    shrink text).
    """

    changed = True
    while changed:
        changed = False
        for marker in (SECRET_MARKER, PATH_MARKER):
            for cut in range(len(marker) - 1, 0, -1):
                if text.endswith(marker[:cut]):
                    text = text[:-cut]
                    changed = True
                    break
    return text


def _shape_scrub(text: str) -> str:
    for pattern in _SECRET_SHAPE_PATTERNS:
        text = pattern.sub(SECRET_MARKER, text)
    text = _KV_SECRET_RE.sub(rf"\1={SECRET_MARKER}", text)
    text = _WINDOWS_PATH_RE.sub(PATH_MARKER, text)
    text = _POSIX_PATH_RE.sub(PATH_MARKER, text)
    return text


def _markers_only(value: str) -> bool:
    return value.replace(SECRET_MARKER, "").replace(PATH_MARKER, "") == ""


def _residual_category(text: str, secrets: tuple[str, ...]) -> str | None:
    """Last-line scan: ``secret``, ``path`` or ``None`` for clean text.

    Structured rather than marker-stripping: a ``key=value`` match counts
    only when its value carries something beyond scrub markers, so the
    scrubber's own artifacts (``token=[REDACTED]``) never masquerade as a
    fresh leak — while any value that still holds content fails closed.
    """

    if any(secret in text for secret in secrets):
        return "secret"
    if any(pattern.search(text) for pattern in _SECRET_SHAPE_PATTERNS):
        return "secret"
    for match in _KV_SECRET_RE.finditer(text):
        if not _markers_only(match.group(2)):
            return "secret"
    if _WINDOWS_PATH_RE.search(text) or _POSIX_PATH_RE.search(text):
        return "path"
    return None


def _clean_text(
    value: Any,
    *,
    where: str,
    max_chars: int | None = None,
    secrets: tuple[str, ...] = (),
) -> tuple[str, bool]:
    """Sanitize one string; returns ``(text, clipped)``.

    Order is deliberate: control characters out, exact caller secrets first
    (they may contain path or pattern fragments), then shape patterns, then
    path replacement; the residual scan guards the whole pipeline.
    """

    if not isinstance(value, str):
        raise ValidationError(f"{where} must be a string")
    text = _CONTROL_RE.sub("?", value)
    text = _exact_scrub(text, secrets)
    text = _shape_scrub(text)
    clipped = False
    if max_chars is not None and len(text) > max_chars:
        text = _trim_partial_markers(text[:max_chars])
        clipped = True
    category = _residual_category(text, secrets)
    if category is not None:
        raise SanitizationRefusal(where, category)
    return text, clipped


def _require_clean_tree(
    node: Any, *, where: str, secrets: tuple[str, ...]
) -> None:
    """Scan an assembled payload STRUCTURALLY (keys and string leaves).

    The scan walks parsed values, never serialized JSON: JSON quoting would
    otherwise let a closed marker like ``token=[REDACTED]"`` masquerade as a
    fresh ``key=value`` (false refusal) or hide real punctuation-bearing
    content.  Bundle-relative paths (``sections/name.json``) are single-slash
    and stay below the two-segment path shape by construction.
    """

    if isinstance(node, str):
        category = _residual_category(node, secrets)
        if category is not None:
            raise SanitizationRefusal(where, category)
    elif isinstance(node, Mapping):
        for key, item in node.items():
            _require_clean_tree(key, where=where, secrets=secrets)
            _require_clean_tree(item, where=where, secrets=secrets)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _require_clean_tree(item, where=where, secrets=secrets)


def _validate_secrets(secrets: Iterable[str] | None) -> tuple[str, ...]:
    if secrets is None:
        return ()
    if isinstance(secrets, (str, bytes)) or not isinstance(secrets, Iterable):
        raise ValidationError("secrets must be an iterable of non-empty strings")
    result = []
    for secret in secrets:
        if not isinstance(secret, str) or not secret:
            raise ValidationError("each registered secret must be a non-empty string")
        if secret in (SECRET_MARKER, PATH_MARKER):
            raise ValidationError(
                "a registered secret must not be a redaction marker itself"
            )
        result.append(secret)
    return tuple(sorted(set(result)))


# ---------------------------------------------------------------------------
# section builders
# ---------------------------------------------------------------------------


def _fit_section(
    name: str, payload: dict[str, Any], list_key: str | None
) -> tuple[dict[str, Any], str]:
    """Trim the payload's item list (tail-first) until the canonical bytes
    fit :data:`MAX_SECTION_BYTES`; honest ``truncated`` flag when trimmed."""

    text = _canonical_json(payload)
    truncated = bool(payload.get("truncated", False))
    items = payload.get(list_key) if list_key else None
    while (
        len(text.encode("utf-8")) > MAX_SECTION_BYTES
        and isinstance(items, list)
        and items
    ):
        items.pop()
        truncated = True
        payload["truncated"] = True
        if isinstance(payload.get("count"), int):
            payload["count"] = len(items)
        text = _canonical_json(payload)
    if len(text.encode("utf-8")) > MAX_SECTION_BYTES:
        raise BundleTooLargeError(
            f"diagnostics section {name!r} cannot be bounded under "
            f"{MAX_SECTION_BYTES} bytes even when empty; narrow the selection"
        )
    payload["truncated"] = truncated
    text = _canonical_json(payload)
    return payload, text


def _version_section(database_schema_version: int) -> dict[str, Any]:
    return {
        "app_version": APP_VERSION,
        "build_schema_version": SCHEMA_VERSION,
        "database_schema_version": database_schema_version,
        "sqlite_version": sqlite3.sqlite_version,
    }


def _schema_section(conn: TeamCraftConnection) -> tuple[dict[str, Any], str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name ASC"
    ).fetchall()
    names = [str(row[0]) for row in rows]
    truncated = len(names) > MAX_SCHEMA_TABLES
    tables = []
    for name in names[:MAX_SCHEMA_TABLES]:
        if not _TABLE_NAME_RE.match(name):
            # Never interpolate an unexpected name; skip it honestly.
            truncated = True
            continue
        count = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        tables.append({"name": name, "row_count": count})
    payload = {"tables": tables, "count": len(tables), "truncated": truncated}
    return _fit_section("schema", payload, "tables")


def _events_section(
    conn: TeamCraftConnection,
    project_id: str | None,
    secrets: tuple[str, ...],
) -> tuple[dict[str, Any], str]:
    where = "events filter"
    if project_id is not None:
        project_id = str(require_entity_id(project_id))
        project_id, _ = _clean_text(project_id, where=where, secrets=secrets)
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT kind, COUNT(*), MIN(seq), MAX(seq), MIN(occurred_at),"
            " MAX(occurred_at) FROM events WHERE project_id = ?"
            " GROUP BY kind ORDER BY kind ASC",
            (project_id,),
        ).fetchall()
    else:
        total = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        rows = conn.execute(
            "SELECT kind, COUNT(*), MIN(seq), MAX(seq), MIN(occurred_at),"
            " MAX(occurred_at) FROM events GROUP BY kind ORDER BY kind ASC"
        ).fetchall()
    truncated = len(rows) > MAX_EVENT_KINDS
    kinds = []
    for row in rows[:MAX_EVENT_KINDS]:
        kind, _ = _clean_text(str(row[0]), where="events.kind", secrets=secrets)
        kinds.append(
            {
                "kind": kind,
                "count": int(row[1]),
                "first_seq": int(row[2]),
                "last_seq": int(row[3]),
                "first_occurred_at": _clean_text(
                    str(row[4]), where="events.first_occurred_at", secrets=secrets
                )[0],
                "last_occurred_at": _clean_text(
                    str(row[5]), where="events.last_occurred_at", secrets=secrets
                )[0],
            }
        )
    payload = {
        "project_id": project_id,
        "total_events": total,
        "kinds": kinds,
        "truncated": truncated,
        # Honest scope declaration: raw payloads are never part of a bundle.
        "payloads_included": False,
    }
    return _fit_section("events", payload, "kinds")


def _health_section(
    health: Mapping[str, Any] | None, secrets: tuple[str, ...]
) -> tuple[dict[str, Any], str]:
    dependencies: dict[str, str] = {}
    if health is not None:
        if not isinstance(health, Mapping):
            raise ValidationError("health must be a mapping of name to status")
        if len(health) > MAX_HEALTH_DEPENDENCIES:
            raise ValidationError(
                f"health snapshot exceeds {MAX_HEALTH_DEPENDENCIES} dependencies"
            )
        for raw_name, raw_status in sorted(health.items()):
            name, _ = _clean_text(
                raw_name, where="health.name", max_chars=64, secrets=secrets
            )
            if not _LABEL_RE.match(name):
                raise ValidationError(
                    f"health dependency name {raw_name!r} is not a clean label"
                )
            if isinstance(raw_status, Mapping):
                # Strict allowlist: probe detail (paths/credentials live there)
                # is refused, not silently dropped.
                unknown = set(raw_status) - {"status"}
                if unknown:
                    raise ValidationError(
                        "health entry for "
                        f"{raw_name!r} carries non-allowlisted keys "
                        f"{sorted(unknown)}; pass the coarse status only"
                    )
                status = raw_status.get("status")
            else:
                status = raw_status
            if not isinstance(status, str) or status not in HEALTH_STATUSES:
                raise ValidationError(
                    f"health dependency {raw_name!r} has invalid status "
                    f"{status!r}; expected one of {list(HEALTH_STATUSES)}"
                )
            dependencies[name] = status
    values = list(dependencies.values())
    if health is None:
        aggregate = HEALTH_AGGREGATE_UNKNOWN
    elif HEALTH_STATUSES[1] in values:  # "down"
        aggregate = HEALTH_AGGREGATE_DEGRADED
    elif HEALTH_STATUSES[2] in values:  # "unknown"
        aggregate = HEALTH_AGGREGATE_UNKNOWN
    else:
        aggregate = HEALTH_AGGREGATE_OK
    payload = {
        "aggregate": aggregate,
        "dependencies": dependencies,
        "count": len(dependencies),
        "snapshot_provided": health is not None,
        "probe_details_included": False,
    }
    return _fit_section("health", payload, None)


def _failures_section(
    failures: Iterable[Mapping[str, Any]] | None, secrets: tuple[str, ...]
) -> tuple[dict[str, Any], str]:
    records: list[dict[str, Any]] = []
    truncated = False
    if failures is not None:
        if isinstance(failures, (str, bytes, Mapping)) or not isinstance(
            failures, Iterable
        ):
            raise ValidationError("failures must be an iterable of mapping records")
        failures = list(failures)
        truncated = len(failures) > MAX_FAILURE_RECORDS
        for index, raw in enumerate(failures[:MAX_FAILURE_RECORDS]):
            where = f"failures[{index}]"
            if not isinstance(raw, Mapping):
                raise ValidationError(f"{where} must be a mapping record")
            extra = set(raw) - _FAILURE_FIELDS
            if extra:
                raise ValidationError(
                    f"{where} carries non-allowlisted keys {sorted(extra)}"
                )
            record: dict[str, Any] = {}
            kind = raw.get("kind")
            if kind is None:
                raise ValidationError(f"{where}.kind is required")
            record["kind"], _ = _clean_text(
                kind, where=f"{where}.kind", max_chars=128, secrets=secrets
            )
            if not _LABEL_RE.match(record["kind"]):
                raise ValidationError(
                    f"{where}.kind {kind!r} is not a clean identifier"
                )
            status = raw.get("status")
            if status not in FAILURE_STATUSES:
                raise ValidationError(
                    f"{where}.status {status!r} invalid; expected one of "
                    f"{list(FAILURE_STATUSES)}"
                )
            record["status"] = status
            digest = raw.get("evidence_digest")
            if not isinstance(digest, str) or not _DIGEST64_RE.match(digest):
                # "Only include sanitized bounded metadata for EVIDENCED
                # failures": no evidence reference, no entry.
                raise ValidationError(
                    f"{where}.evidence_digest must be a lowercase 64-hex digest "
                    "identifying the stored failure evidence"
                )
            record["evidence_digest"] = digest
            for optional in ("id", "task_id", "attempt_id"):
                value = raw.get(optional)
                if value is not None:
                    value = str(require_entity_id(value))
                    record[optional], _ = _clean_text(
                        value, where=f"{where}.{optional}", secrets=secrets
                    )
            exit_code = raw.get("exit_code")
            if exit_code is not None:
                if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                    raise ValidationError(f"{where}.exit_code must be an integer")
                if not -(2**31) <= exit_code < 2**31:
                    raise ValidationError(f"{where}.exit_code out of int32 range")
                record["exit_code"] = exit_code
            profile = raw.get("profile")
            if profile is not None:
                if profile not in VERIFIER_PROFILES:
                    raise ValidationError(
                        f"{where}.profile {profile!r} invalid; expected one of "
                        f"{list(VERIFIER_PROFILES)}"
                    )
                record["profile"] = profile
            occurred_at = raw.get("occurred_at")
            if occurred_at is not None:
                record["occurred_at"], _ = _clean_text(
                    occurred_at, where=f"{where}.occurred_at", max_chars=40,
                    secrets=secrets,
                )
            detail = raw.get("detail")
            if detail is not None:
                clean, clipped = _clean_text(
                    detail, where=f"{where}.detail", max_chars=MAX_DETAIL_CHARS,
                    secrets=secrets,
                )
                record["detail"] = clean
                record["detail_clipped"] = clipped
            records.append(record)
    payload = {
        "failures": records,
        "count": len(records),
        "truncated": truncated,
        "raw_logs_included": False,
    }
    return _fit_section("failures", payload, "failures")


# ---------------------------------------------------------------------------
# selection, preview, manifest
# ---------------------------------------------------------------------------


def _validate_selection(sections: Any) -> tuple[str, ...]:
    if sections is None:
        return DEFAULT_SECTIONS
    if isinstance(sections, (str, bytes)) or not isinstance(sections, Iterable):
        raise ValidationError(
            "sections must be an explicit sequence of section names; allowed: "
            f"{list(SECTION_ALLOWLIST)}"
        )
    selected = tuple(sections)
    if not selected:
        raise ValidationError("sections must not be empty; pick at least one of "
                             f"{list(SECTION_ALLOWLIST)}")
    seen: set[str] = set()
    for name in selected:
        if not isinstance(name, str) or name not in SECTION_ALLOWLIST:
            raise ValidationError(
                f"section {name!r} is not on the allowlist; allowed: "
                f"{list(SECTION_ALLOWLIST)}"
            )
        if name in seen:
            raise ValidationError(f"section {name!r} selected more than once")
        seen.add(name)
    return selected


def _generated_at_or_none(
    generated_at: Any, secrets: tuple[str, ...]
) -> str | None:
    if generated_at is None:
        return None
    text, clipped = _clean_text(
        generated_at, where="generated_at", max_chars=64, secrets=secrets
    )
    if clipped or not text:
        raise ValidationError("generated_at must be a short single-line timestamp")
    return text


@dataclass(frozen=True)
class DiagnosticsPreview:
    """Exact bytes an export would write — export has no other data path."""

    selected: tuple[str, ...]
    sections: tuple[tuple[str, str], ...]  # (name, canonical JSON text), ordered
    manifest_text: str

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_text)

    @property
    def manifest_digest(self) -> str:
        return str(self.manifest["manifest_digest"])

    def section_map(self) -> dict[str, str]:
        return dict(self.sections)

    def section_payloads(self) -> dict[str, Any]:
        return {name: json.loads(text) for name, text in self.sections}

    def scan(self) -> str | None:
        """Residual leak category across EVERY emitted value, else ``None``."""

        try:
            for name, text in self.sections:
                _require_clean_tree(
                    json.loads(text), where=f"sections/{name}.json", secrets=()
                )
            _require_clean_tree(
                json.loads(self.manifest_text), where=MANIFEST_FILENAME, secrets=()
            )
        except SanitizationRefusal as refusal:
            return f"{refusal.where}:{refusal.category}"
        return None


def build_preview(
    conn: TeamCraftConnection,
    *,
    sections: Iterable[str] | None = None,
    health: Mapping[str, Any] | None = None,
    failures: Iterable[Mapping[str, Any]] | None = None,
    project_id: str | None = None,
    generated_at: str | None = None,
    secrets: Iterable[str] | None = None,
) -> DiagnosticsPreview:
    """Build the sanitized, bounded, deterministic bundle preview.

    ``health`` is a coarse snapshot (``{name: "up"|"down"|"unknown"}`` or
    ``{name: {"status": ...}}`` — anything richer is refused); ``failures``
    is an explicit list of evidenced records (see :data:`_FAILURE_FIELDS`);
    ``secrets`` registers caller-known secret values for exact scrubbing.
    This function reads nothing but the opened database and these arguments —
    never the environment, never log files — and raises before producing any
    output on validation, sanitization, or size failure.
    """

    selected = _validate_selection(sections)
    secret_values = _validate_secrets(secrets)
    stamp = _generated_at_or_none(generated_at, secret_values)
    database_schema_version = _require_head(conn)

    built: list[tuple[str, str]] = []
    truncations: dict[str, bool] = {}
    for name in selected:
        if name == "version":
            payload = _version_section(database_schema_version)
            text = _canonical_json(payload)
        elif name == "schema":
            payload, text = _schema_section(conn)
        elif name == "events":
            payload, text = _events_section(conn, project_id, secret_values)
        elif name == "health":
            payload, text = _health_section(health, secret_values)
        elif name == "failures":
            payload, text = _failures_section(failures, secret_values)
        else:  # pragma: no cover - selection already restricts the set
            raise ValidationError(f"unreachable section {name!r}")
        truncations[name] = bool(payload.get("truncated", False))
        # Keys are ours; values were sanitized per field — walk the whole
        # structure anyway as the last line before acceptance.
        _require_clean_tree(payload, where=f"section {name!r}",
                           secrets=secret_values)
        built.append((name, text))

    sections_bytes = sum(len(text.encode("utf-8")) for _, text in built)
    if sections_bytes > MAX_BUNDLE_BYTES:
        raise BundleTooLargeError(
            f"selected sections total {sections_bytes} bytes, over the "
            f"{MAX_BUNDLE_BYTES} byte bundle budget"
        )

    manifest: dict[str, Any] = {
        "bundle": BUNDLE_NAME,
        "format": BUNDLE_FORMAT_VERSION,
        "diagnostics_schema": DIAGNOSTICS_SCHEMA,
        "app_version": APP_VERSION,
        "database_schema_version": database_schema_version,
        # Null unless the caller explicitly supplied a stamp; this module
        # never reads a wall clock, so identical inputs are byte-identical.
        "generated_at": stamp,
        "sections": list(selected),
        "files": [
            {
                "name": name,
                "path": f"{SECTIONS_DIRNAME}/{name}.json",
                "sha256": _sha256_hex(text),
                "bytes": len(text.encode("utf-8")),
            }
            for name, text in built
        ],
        "sections_bytes": sections_bytes,
        "truncated": any(truncations.values()),
        "truncations": truncations,
        "policy": {
            "allowlist": list(SECTION_ALLOWLIST),
            "default_sections": list(DEFAULT_SECTIONS),
            "secret_marker": SECRET_MARKER,
            "path_marker": PATH_MARKER,
            "secrets_registered": len(secret_values),
            "raw": {
                "environment": False,
                "log_contents": False,
                "credentials": False,
                "source_paths": False,
                "event_payloads": False,
                "health_probe_details": False,
            },
        },
    }
    body = _canonical_json(manifest)
    manifest["manifest_digest"] = _sha256_hex(body)
    manifest_text = _canonical_json(manifest)
    _require_clean_tree(manifest, where="manifest", secrets=secret_values)
    return DiagnosticsPreview(
        selected=selected, sections=tuple(built), manifest_text=manifest_text
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportedFile:
    """One file durably written by :func:`export_bundle` (relative name)."""

    name: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ExportResult:
    output_dir: str
    files: tuple[ExportedFile, ...]
    manifest_digest: str
    total_bytes: int


def export_bundle(preview: DiagnosticsPreview, output_dir: str | os.PathLike[str]) -> ExportResult:
    """Write exactly the preview's bytes under ``output_dir``.

    Fail-before-write: if ANY target file already exists, nothing is written;
    each file is then created exclusively (O_EXCL, 0600) and verified against
    its manifest digest by reading it back.
    """

    if not isinstance(preview, DiagnosticsPreview):
        raise ValidationError("export_bundle requires a DiagnosticsPreview")
    if isinstance(output_dir, (str, os.PathLike)):
        out = Path(os.fspath(output_dir))
    else:
        raise ValidationError("output_dir must be a path")
    if not str(out).strip():
        raise ValidationError("output_dir must not be empty")

    targets: list[tuple[str, str]] = [
        (f"{SECTIONS_DIRNAME}/{name}.json", text) for name, text in preview.sections
    ]
    targets.append((MANIFEST_FILENAME, preview.manifest_text))

    for rel, _ in targets:
        if (out / rel).exists():
            raise ValidationError(
                f"refusing to overwrite existing diagnostics file "
                f"{rel!r}; nothing was written"
            )
    total = sum(len(text.encode("utf-8")) for _, text in targets)
    if total > MAX_BUNDLE_BYTES:
        raise BundleTooLargeError(
            f"bundle would be {total} bytes, over the {MAX_BUNDLE_BYTES} byte "
            "budget"
        )

    (out / SECTIONS_DIRNAME).mkdir(parents=True, exist_ok=True)
    written: list[ExportedFile] = []
    for rel, text in targets:
        data = text.encode("utf-8")
        fd = os.open(str(out / rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        written_path = out / rel
        readback = written_path.read_bytes()
        if _sha256_hex(readback.decode("utf-8")) != _sha256_hex(text):
            raise StorageError(
                f"diagnostics export verification failed for {rel!r}; "
                "the written file does not match the previewed bytes"
            )
        written.append(
            ExportedFile(name=rel, bytes=len(readback), sha256=_sha256_hex(
                readback.decode("utf-8")))
        )
    # Manifest-embedded digests must match what actually landed on disk.
    manifest = preview.manifest
    for entry in manifest["files"]:
        on_disk = next(f for f in written if f.name == entry["path"])
        if on_disk.sha256 != entry["sha256"] or on_disk.bytes != entry["bytes"]:
            raise StorageError(
                f"diagnostics export manifest mismatch for {entry['path']!r}"
            )
    return ExportResult(
        output_dir=str(out),
        files=tuple(written),
        manifest_digest=preview.manifest_digest,
        total_bytes=sum(f.bytes for f in written),
    )


def build_and_export(
    conn: TeamCraftConnection,
    output_dir: str | os.PathLike[str],
    **kwargs: Any,
) -> tuple[DiagnosticsPreview, ExportResult]:
    """Convenience wrapper: :func:`build_preview` then :func:`export_bundle`."""

    preview = build_preview(conn, **kwargs)
    return preview, export_bundle(preview, output_dir)
