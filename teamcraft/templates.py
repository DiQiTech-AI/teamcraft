"""M6-04 Task Brief templates and personal preferences (M0 review 9.6 M6-04).

Scope, pinned by this module and ``tests/test_templates_m604.py``:

* **brief text and display only** — a template renders *plain text* for a Task
  Brief and preferences carry only how Verifier/routing information is
  *displayed*.  Nothing in this module creates tasks, dispatches attempts,
  selects routes, runs commands, or touches approvals; consumers feed rendered
  text into the existing M2-03 brief boundary (``briefs.create_attempt_brief``)
  and read display preferences on the client side only.
* **versioned documents** — every document pins ``schema_version`` (exactly
  :data:`DOCUMENT_SCHEMA_VERSION`); unknown fields are refused, never ignored
  (forward-config must not silently downgrade to "trusted later").
* **strict import validation** — parse first, normalize, then apply: every
  unknown key, bad identifier, out-of-range value, unbalanced placeholder, or
  duplicate id raises before anything is stored; a refused import provably
  leaves the store untouched.
* **privilege fields are refused, loudly** — keys whose normalized segments hit
  :data:`FORBIDDEN_KEY_TOKENS` (ownership/leader/lease, command/allow/exec/run/
  shell, approval, permission/privilege, policy/risk/assurance/capability/
  local_first, ...) raise :class:`ForbiddenFieldError` with reason code
  ``FORBIDDEN_PRIVILEGE_FIELD`` at ANY depth.  The final merged document is
  re-checked against the closed allowlist as defense-in-depth, so a template or
  preference can never carry ownership, execution-allowlist, approval, or
  routing-enforcement configuration.
* **project overrides, leaf-wise** — project documents override user/global
  preferences per leaf field (not per section), replace templates by ``id``,
  and may add new templates; global entries not overridden survive.  The
  effective config is pure: resolution writes nothing.
* **rollback / selection** — :class:`ConfigStore` keeps an append-only history
  of immutable versioned snapshots plus an atomically swapped ``ACTIVE``
  pointer.  ``select(v)`` re-points ACTIVE at any previously stored version
  after re-verifying its digest; ``rollback()`` picks the newest version
  below the active one.  The module never reads a wall clock (``imported_at``
  is only what the caller supplies) and every file write is tmp-file + rename.
* **operator CLI** — ``python -m teamcraft.templates`` exposes the whole
  workflow project-locally (default store
  ``<project root>/.teamcraft/templates``, honoring ``TEAMCRAFT_PROJECT_ROOT``
  via ``teamcraft.config``; ``--store`` overrides)::

      python -m teamcraft.templates preview briefs.json   # validate only, zero writes
      python -m teamcraft.templates import  briefs.json   # validate, append + activate vN
      python -m teamcraft.templates list                  # versions with ACTIVE marker
      python -m teamcraft.templates show [VERSION]        # print stored document
      python -m teamcraft.templates render ID --set k=v   # EXPLICIT rendering to stdout
      python -m teamcraft.templates rollback [--to N]     # revert to a stored version
      python -m teamcraft.templates select N              # activate any stored version

  Rendering is explicit and everything else stays inert: ``import`` never
  renders or executes anything, ``preview`` proves validity before committing,
  and display preferences remain plain data.  Exit codes: 0 ok, 1 document
  refused (validation/privilege), 2 usage/configuration, 3 store integrity,
  4 not found (``--help`` documents this too).

Integration boundary: this is a pure local-Python boundary like M6-03.  A
later API/Web surface (M3-05 lineage) may expose it unchanged; it owns no
database rows, no ownership or routing facts, and adds no transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import load_config
from .errors import (
    ConfigurationError,
    NotFoundError,
    StorageError,
    TeamCraftError,
    ValidationError,
)

__all__ = [
    "DOCUMENT_SCHEMA_VERSION",
    "STORE_FORMAT",
    "STORE_DIRNAME",
    "ACTIVE_FILENAME",
    "VERSIONS_DIRNAME",
    "FORBIDDEN_KEY_TOKENS",
    "PRIVILEGE_REASON_CODE",
    "MAX_TEMPLATES",
    "MAX_SECTIONS",
    "MAX_BODY_BYTES",
    "MAX_VALUE_BYTES",
    "MAX_BRIEF_BYTES",
    "VERIFIER_SORTS",
    "BriefSection",
    "TaskBriefTemplate",
    "VerifierDisplay",
    "RoutingDisplay",
    "BriefDisplay",
    "DisplayPreferences",
    "ConfigDocument",
    "EffectiveConfig",
    "StoredVersion",
    "ForbiddenFieldError",
    "main",
    "resolve_store_dir",
    "parse_template",
    "parse_preferences",
    "parse_document",
    "render_brief",
    "resolve",
    "ConfigStore",
]

#: Only this document version is accepted (bump deliberately, never coerce).
DOCUMENT_SCHEMA_VERSION = 1

#: Store file identity (distinct from the document schema on purpose).
STORE_FORMAT = "teamcraft-m604-configstore"
STORE_FORMAT_VERSION = 1
ACTIVE_FILENAME = "ACTIVE"
VERSIONS_DIRNAME = "versions"

#: Default store location below the project runtime directory
#: (``<project root>/.teamcraft/templates``; see :func:`resolve_store_dir`).
STORE_DIRNAME = "templates"

#: Hard ceilings (briefs.py caps raw content at 256 KiB; stay far below it).
MAX_TEMPLATES = 50
MAX_SECTIONS = 16
MAX_BODY_BYTES = 4 * 1024
MAX_VALUE_BYTES = 8 * 1024
MAX_BRIEF_BYTES = 64 * 1024
MAX_ID_CHARS = 64
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 512
MAX_HEADING_CHARS = 80
MAX_IMPORTED_AT_CHARS = 64

#: Closed display vocabularies / bounds.
VERIFIER_SORTS = ("seq", "occurred_at", "profile")
MAX_ROWS_BOUNDS = (1, 500)
MAX_PREVIEW_CHARS_BOUNDS = (16, 4096)

#: Key segments that mark a field as privilege/enforcement-bearing.  A key is
#: split on non-alphanumerics and CamelCase-free simple words; a segment that
#: STARTS with one of these tokens is refused.  The legitimate display
#: vocabulary below deliberately avoids every token.
FORBIDDEN_KEY_TOKENS: frozenset[str] = frozenset({
    "owner", "ownership", "leader", "lease", "epoch", "fenc",
    "allow", "allowlist", "command", "exec", "execute", "executable",
    "run", "runner", "shell", "binary",
    "approv", "approval", "auto_approve",
    "permission", "privileg", "role", "admin", "sudo", "bypass", "escalat",
    "policy", "risk", "assurance", "capabilit", "local_first",
    "enforce", "enforcement", "guard", "override",
})
PRIVILEGE_REASON_CODE = "FORBIDDEN_PRIVILEGE_FIELD"

_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,%d}\Z" % (MAX_ID_CHARS - 1))
_HEADING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:()/+-]{0,%d}\Z" % (MAX_HEADING_CHARS - 1))
_NAME_RE = re.compile(r"^[A-Za-z0-9][^\x00-\x1f\x7f]{0,%d}\Z" % (MAX_NAME_CHARS - 1))
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z][a-z0-9_]{0,31})\}\}")
_BRACE_RE = re.compile(r"\{\{|\}\}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ForbiddenFieldError(ValidationError):
    """An imported document tried to carry a privilege/enforcement field.

    Fail-closed and self-evidencing: the offending key path is reported, but
    the value is never quoted back.  Nothing from the document is applied.
    Subclasses :class:`ValidationError` so strict-import refusals form one
    catchable family (mirrors M6-03's ``SanitizationRefusal``).
    """

    def __init__(self, path: str, key: str) -> None:
        super().__init__(
            f"field {path!r} is enforcement-bearing (key {key!r}); templates "
            "and preferences may never carry ownership, command allowlist, "
            "approval, or routing-enforcement configuration; the whole import "
            "was refused"
        )
        self.path = path
        self.key = key
        self.reason_code = PRIVILEGE_REASON_CODE


# ---------------------------------------------------------------------------
# shared validation helpers
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{where} must be a JSON object")
    return value


def _allow_keys(raw: Mapping[str, Any], allowed: set[str] | frozenset[str],
                where: str) -> None:
    extra = {str(k) for k in raw} - set(allowed)
    if extra:
        raise ValidationError(
            f"{where} carries non-allowlisted keys {sorted(extra)}; "
            "unknown fields are refused, not ignored"
        )


def _text(value: Any, where: str, *, max_chars: int,
          allow_spaces: bool = True) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{where} must be a string")
    text = _CONTROL_RE.sub("", value)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValidationError(f"{where} must not be blank")
    if not allow_spaces and re.search(r"\s", text):
        raise ValidationError(f"{where} must not contain whitespace")
    if len(text) > max_chars:
        raise ValidationError(f"{where} exceeds {max_chars} characters")
    return text


def _boolean(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValidationError(f"{where} must be a boolean")
    return value


def _bounded_int(value: Any, where: str, bounds: tuple[int, int],
                 default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{where} must be an integer")
    if not bounds[0] <= value <= bounds[1]:
        raise ValidationError(
            f"{where} must be between {bounds[0]} and {bounds[1]}"
        )
    return value


def _scan_forbidden(node: Any, *, path: str) -> None:
    """Refuse enforcement-bearing keys at any depth, BEFORE parsing values."""

    if isinstance(node, Mapping):
        for raw_key, value in node.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]+", "_", key.lower())
            segments = [s for s in normalized.split("_") if s]
            matched = any(
                segment.startswith(token)
                for segment in segments
                for token in FORBIDDEN_KEY_TOKENS
                if "_" not in token
            ) or any(
                token in normalized
                for token in FORBIDDEN_KEY_TOKENS
                if "_" in token
            )
            if matched:
                raise ForbiddenFieldError(f"{path}.{key}" if path else key, key)
            _scan_forbidden(value, path=f"{path}.{key}" if path else key)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _scan_forbidden(item, path=f"{path}[{index}]")


def _validate_schema_version(raw: Mapping[str, Any], where: str) -> None:
    version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationError(f"{where}.schema_version must be an integer")
    if version != DOCUMENT_SCHEMA_VERSION:
        raise ValidationError(
            f"{where}.schema_version {version!r} unsupported; this build "
            f"accepts exactly {DOCUMENT_SCHEMA_VERSION}"
        )


# ---------------------------------------------------------------------------
# Task Brief templates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefSection:
    heading: str
    body: str


@dataclass(frozen=True)
class TaskBriefTemplate:
    id: str
    name: str
    description: str | None
    sections: tuple[BriefSection, ...]
    placeholders: frozenset[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "sections": [{"heading": s.heading, "body": s.body}
                        for s in self.sections],
        }


def parse_template(raw: Any, *, where: str = "template") -> TaskBriefTemplate:
    """Strictly parse one Task Brief template; text and structure only."""

    node = _as_mapping(raw, where)
    _scan_forbidden(node, path=where)
    _allow_keys(node, {"id", "name", "description", "sections"}, where)

    template_id = _text(node.get("id"), f"{where}.id", max_chars=MAX_ID_CHARS,
                        allow_spaces=False)
    if not _TEMPLATE_ID_RE.match(template_id):
        raise ValidationError(
            f"{where}.id must be lowercase letters, digits, '_' or '-'"
        )
    name = _text(node.get("name"), f"{where}.name", max_chars=MAX_NAME_CHARS)
    if not _NAME_RE.match(name):
        raise ValidationError(f"{where}.name is not a clean display name")
    description = node.get("description")
    if description is not None:
        description = _text(description, f"{where}.description",
                           max_chars=MAX_DESCRIPTION_CHARS)

    raw_sections = node.get("sections")
    if isinstance(raw_sections, (str, bytes)) or not isinstance(
            raw_sections, (list, tuple)):
        raise ValidationError(f"{where}.sections must be a list of section objects")
    if not raw_sections:
        raise ValidationError(f"{where}.sections must not be empty")
    if len(raw_sections) > MAX_SECTIONS:
        raise ValidationError(
            f"{where}.sections exceeds {MAX_SECTIONS} sections"
        )

    placeholders: set[str] = set()
    sections: list[BriefSection] = []
    for index, raw_section in enumerate(raw_sections):
        section_where = f"{where}.sections[{index}]"
        section = _as_mapping(raw_section, section_where)
        _allow_keys(section, {"heading", "body"}, section_where)
        heading = _text(section.get("heading"), f"{section_where}.heading",
                        max_chars=MAX_HEADING_CHARS)
        if not _HEADING_RE.match(heading):
            raise ValidationError(
                f"{section_where}.heading is not a clean display heading"
            )
        body = section.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValidationError(f"{section_where}.body must be non-empty text")
        if _CONTROL_RE.search(body):
            raise ValidationError(
                f"{section_where}.body contains control characters; brief text "
                "must be plain printable text (tabs/newlines only)"
            )
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValidationError(
                f"{section_where}.body exceeds {MAX_BODY_BYTES} bytes"
            )
        # Every brace pair must be a well-formed placeholder — no stray or
        # nested braces that could masquerade as a directive later.
        without_placeholders = _PLACEHOLDER_RE.sub("\x00", body)
        if _BRACE_RE.search(without_placeholders) or "{" in without_placeholders \
                or "}" in without_placeholders:
            raise ValidationError(
                f"{section_where}.body contains unbalanced or malformed "
                "braces; only double-curly placeholder syntax is allowed"
            )
        placeholders.update(_PLACEHOLDER_RE.findall(body))
        sections.append(BriefSection(heading=heading, body=body))

    return TaskBriefTemplate(
        id=template_id,
        name=name,
        description=description,
        sections=tuple(sections),
        placeholders=frozenset(placeholders),
    )


def render_brief(template: TaskBriefTemplate, values: Mapping[str, Any]) -> str:
    """Fill a template's placeholders and return plain Task Brief text.

    Values must cover exactly the declared placeholders (missing or extra keys
    are refused), be plain strings within :data:`MAX_VALUE_BYTES`, carry no
    control characters, and never contain brace characters — rendered text is
    inert data, so a value can neither invent a new placeholder nor smuggle a
    directive toward the worker prompt.
    """

    if not isinstance(template, TaskBriefTemplate):
        raise ValidationError("render_brief requires a TaskBriefTemplate")
    if not isinstance(values, Mapping):
        raise ValidationError("brief values must be a mapping of placeholder to text")
    keys = {str(k) for k in values}
    missing = sorted(template.placeholders - keys)
    if missing:
        raise ValidationError(f"brief values missing placeholders: {missing}")
    extra = sorted(keys - set(template.placeholders))
    if extra:
        raise ValidationError(
            f"brief values carry undeclared keys: {extra}; refusing silent drops"
        )
    filled: dict[str, str] = {}
    for key in sorted(template.placeholders):
        value = values[key]
        if not isinstance(value, str):
            raise ValidationError(f"brief value {key!r} must be a string")
        clean = value
        if _CONTROL_RE.search(clean):
            raise ValidationError(
                f"brief value {key!r} contains control characters; values are "
                "inert text (tabs/newlines only)"
            )
        if _BRACE_RE.search(clean):
            raise ValidationError(
                f"brief value {key!r} contains brace characters; values are "
                "inert text and may not carry placeholder syntax"
            )
        if len(clean.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ValidationError(
                f"brief value {key!r} exceeds {MAX_VALUE_BYTES} bytes"
            )
        filled[key] = clean

    blocks: list[str] = []
    for section in template.sections:
        body = _PLACEHOLDER_RE.sub(lambda m: filled[m.group(1)], section.body)
        blocks.append(f"## {section.heading}\n{body}")
    text = "\n\n".join(blocks) + "\n"
    if len(text.encode("utf-8")) > MAX_BRIEF_BYTES:
        raise ValidationError(
            f"rendered brief exceeds {MAX_BRIEF_BYTES} bytes; shorten the "
            "template or values"
        )
    return text


# ---------------------------------------------------------------------------
# display preferences (Verifier / routing / brief)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifierDisplay:
    show_profile: bool = True
    show_exit_code: bool = True
    sort: str = "seq"
    max_rows: int = 50

    def to_payload(self) -> dict[str, Any]:
        return {"show_profile": self.show_profile,
                "show_exit_code": self.show_exit_code,
                "sort": self.sort, "max_rows": self.max_rows}


@dataclass(frozen=True)
class RoutingDisplay:
    show_reason_code: bool = True
    show_selection: bool = True
    show_cost: bool = False
    show_locality: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {"show_reason_code": self.show_reason_code,
                "show_selection": self.show_selection,
                "show_cost": self.show_cost,
                "show_locality": self.show_locality}


@dataclass(frozen=True)
class BriefDisplay:
    default_template_id: str | None = None
    max_preview_chars: int = 2000

    def to_payload(self) -> dict[str, Any]:
        return {"default_template_id": self.default_template_id,
                "max_preview_chars": self.max_preview_chars}


@dataclass(frozen=True)
class DisplayPreferences:
    verifier: VerifierDisplay = VerifierDisplay()
    routing: RoutingDisplay = RoutingDisplay()
    brief: BriefDisplay = BriefDisplay()

    def to_payload(self) -> dict[str, Any]:
        return {"verifier": self.verifier.to_payload(),
                "routing": self.routing.to_payload(),
                "brief": self.brief.to_payload()}


def parse_preferences(raw: Any, *, where: str = "preferences"
                     ) -> DisplayPreferences:
    """Strictly parse display preferences: closed vocabulary, bounded values."""

    if raw is None:
        return DisplayPreferences()
    node = _as_mapping(raw, where)
    _scan_forbidden(node, path=where)
    _allow_keys(node, {"verifier", "routing", "brief"}, where)

    verifier_raw = node.get("verifier")
    verifier_raw = {} if verifier_raw is None else verifier_raw
    verifier_raw = _as_mapping(verifier_raw, f"{where}.verifier")
    _allow_keys(verifier_raw,
                {"show_profile", "show_exit_code", "sort", "max_rows"},
                f"{where}.verifier")
    sort = verifier_raw.get("sort")
    if sort is None:
        sort = VerifierDisplay().sort
    elif sort not in VERIFIER_SORTS:
        raise ValidationError(
            f"{where}.verifier.sort {sort!r} invalid; expected one of "
            f"{list(VERIFIER_SORTS)}"
        )
    verifier = VerifierDisplay(
        show_profile=_boolean(verifier_raw.get("show_profile"),
                             f"{where}.verifier.show_profile", True),
        show_exit_code=_boolean(verifier_raw.get("show_exit_code"),
                               f"{where}.verifier.show_exit_code", True),
        sort=sort,
        max_rows=_bounded_int(verifier_raw.get("max_rows"),
                             f"{where}.verifier.max_rows", MAX_ROWS_BOUNDS, 50),
    )

    routing_raw = node.get("routing")
    routing_raw = {} if routing_raw is None else routing_raw
    routing_raw = _as_mapping(routing_raw, f"{where}.routing")
    _allow_keys(routing_raw,
                {"show_reason_code", "show_selection", "show_cost",
                 "show_locality"}, f"{where}.routing")
    routing = RoutingDisplay(
        show_reason_code=_boolean(routing_raw.get("show_reason_code"),
                                 f"{where}.routing.show_reason_code", True),
        show_selection=_boolean(routing_raw.get("show_selection"),
                               f"{where}.routing.show_selection", True),
        show_cost=_boolean(routing_raw.get("show_cost"),
                          f"{where}.routing.show_cost", False),
        show_locality=_boolean(routing_raw.get("show_locality"),
                              f"{where}.routing.show_locality", True),
    )

    brief_raw = node.get("brief")
    brief_raw = {} if brief_raw is None else brief_raw
    brief_raw = _as_mapping(brief_raw, f"{where}.brief")
    _allow_keys(brief_raw, {"default_template_id", "max_preview_chars"},
                f"{where}.brief")
    default_id = brief_raw.get("default_template_id")
    if default_id is not None:
        default_id = _text(default_id, f"{where}.brief.default_template_id",
                           max_chars=MAX_ID_CHARS, allow_spaces=False)
        if not _TEMPLATE_ID_RE.match(default_id):
            raise ValidationError(
                f"{where}.brief.default_template_id is not a valid template id"
            )
    brief = BriefDisplay(
        default_template_id=default_id,
        max_preview_chars=_bounded_int(
            brief_raw.get("max_preview_chars"),
            f"{where}.brief.max_preview_chars",
            MAX_PREVIEW_CHARS_BOUNDS, 2000),
    )
    return DisplayPreferences(verifier=verifier, routing=routing, brief=brief)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]
               ) -> dict[str, Any]:
    """Leaf-wise deep merge: nested mappings merge recursively, every other
    value (scalar, list) is replaced whole by the override side."""

    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def merge_preferences(base: Any = None, override: Any = None
                     ) -> DisplayPreferences:
    """Merge raw preference layers leaf-wise, then strictly parse the result.

    Parsing each layer alone would materialize defaults and turn them into
    "explicit" overrides; merging the RAW mappings first is what makes
    "project wins per field, everything else keeps the base value" true.
    The merged document is still validated through the strict allowlist, so a
    layer cannot smuggle unknown or enforcement keys through merging.
    """

    merged: dict[str, Any] = {}
    for layer in (base, override):
        if layer is None:
            continue
        node = _as_mapping(layer, "preferences layer")
        _scan_forbidden(node, path="preferences")
        merged = _deep_merge(merged, node)
    return parse_preferences(merged)


# ---------------------------------------------------------------------------
# documents and resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigDocument:
    templates: tuple[TaskBriefTemplate, ...]
    preferences: DisplayPreferences

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "templates": [t.to_payload() for t in self.templates],
            "preferences": self.preferences.to_payload(),
        }

    @property
    def doc_digest(self) -> str:
        return _sha256(_canonical(self.to_payload()))


def parse_document(raw: Any, *, where: str = "config") -> ConfigDocument:
    """Strictly parse a whole template/preference document."""

    node = _as_mapping(raw, where)
    _scan_forbidden(node, path=where)
    _allow_keys(node, {"schema_version", "templates", "preferences"}, where)
    _validate_schema_version(node, where)

    raw_templates = node.get("templates")
    if raw_templates is None:
        raw_templates = []
    if isinstance(raw_templates, (str, bytes)) or not isinstance(
            raw_templates, (list, tuple)):
        raise ValidationError(f"{where}.templates must be a list of templates")
    if len(raw_templates) > MAX_TEMPLATES:
        raise ValidationError(
            f"{where}.templates exceeds {MAX_TEMPLATES} templates"
        )
    templates = tuple(
        parse_templates_item(item, where=f"{where}.templates[{index}]")
        for index, item in enumerate(raw_templates)
    )
    ids = [t.id for t in templates]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise ValidationError(
            f"{where}.templates carries duplicate ids: {duplicates}"
        )
    preferences = parse_preferences(node.get("preferences"),
                                   where=f"{where}.preferences")
    return ConfigDocument(templates=templates, preferences=preferences)


def parse_templates_item(raw: Any, *, where: str) -> TaskBriefTemplate:
    # Split out so error paths read ``config.templates[0]`` while
    # ``parse_template`` keeps a friendly default location for callers.
    return parse_template(raw, where=where)


@dataclass(frozen=True)
class EffectiveConfig:
    templates: tuple[TaskBriefTemplate, ...]
    preferences: DisplayPreferences

    def template_index(self) -> dict[str, TaskBriefTemplate]:
        return {t.id: t for t in self.templates}

    def default_template(self) -> TaskBriefTemplate | None:
        template_id = self.preferences.brief.default_template_id
        if template_id is None:
            return None
        return self.template_index().get(template_id)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "templates": [t.to_payload() for t in self.templates],
            "preferences": self.preferences.to_payload(),
        }

    @property
    def digest(self) -> str:
        return _sha256(_canonical(self.to_payload()))


def resolve(global_raw: Any = None, project_raw: Any = None
            ) -> EffectiveConfig:
    """Resolve user/global and project layers into the effective config.

    * preferences merge leaf-wise, project wins per field;
    * a project template with the same ``id`` replaces the global one in
      place; new project ids append (global order preserved);
    * ``brief.default_template_id`` must name a template in the EFFECTIVE set
      — a project override that orphans the default is a configuration error.

    Pure: writes nothing; any layer failure raises before a result exists.
    """

    if global_raw is not None and not isinstance(global_raw, Mapping):
        raise ValidationError("global config layer must be an object or absent")
    if project_raw is not None and not isinstance(project_raw, Mapping):
        raise ValidationError("project config layer must be an object or absent")
    if global_raw is not None:
        _scan_forbidden(global_raw, path="global")
    if project_raw is not None:
        _scan_forbidden(project_raw, path="project")

    global_doc = (parse_document(global_raw, where="global")
                  if global_raw is not None else None)
    project_doc = (parse_document(project_raw, where="project")
                   if project_raw is not None else None)

    templates: list[TaskBriefTemplate] = []
    if global_doc is not None:
        templates = list(global_doc.templates)
    if project_doc is not None:
        positions = {t.id: index for index, t in enumerate(templates)}
        for template in project_doc.templates:
            if template.id in positions:
                templates[positions[template.id]] = template
            else:
                positions[template.id] = len(templates)
                templates.append(template)

    global_prefs_raw = (global_raw or {}).get("preferences") if global_raw else None
    project_prefs_raw = (project_raw or {}).get("preferences") if project_raw else None
    preferences = merge_preferences(global_prefs_raw, project_prefs_raw)

    effective = EffectiveConfig(templates=tuple(templates),
                               preferences=preferences)
    default_id = preferences.brief.default_template_id
    if default_id is not None and default_id not in effective.template_index():
        raise ValidationError(
            f"preferences.brief.default_template_id {default_id!r} names no "
            "template in the effective config; fix or rollback the layer that "
            "introduced it"
        )
    # Defense-in-depth: the final merged document must still parse cleanly
    # through the strict allowlist (it can only contain display/brief fields).
    parse_document(effective.to_payload(), where="effective")
    return effective


# ---------------------------------------------------------------------------
# versioned store with rollback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredVersion:
    version: int
    doc_digest: str
    imported_at: str | None


class ConfigStore:
    """Append-only versioned config directory with an atomic ACTIVE pointer.

    Layout::

        <root>/versions/<v%06d>.json   immutable envelope per applied version
        <root>/ACTIVE                 the applied version number, atomic rename

    Every import fully validates BEFORE any file is written, so a refused
    import provably changes nothing.  Re-importing the active document
    byte-for-byte is idempotent (same version).  Rollback re-points ACTIVE at
    a previously stored version after re-verifying its digest; history files
    are never rewritten or deleted here.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        path = Path(os.fspath(root))
        if path.exists() and not path.is_dir():
            raise ConfigurationError(
                f"config store root {str(path)!r} exists and is not a directory"
            )
        self.root = path
        (self.root / VERSIONS_DIRNAME).mkdir(parents=True, exist_ok=True)

    # -- helpers ----------------------------------------------------------

    def _version_path(self, version: int) -> Path:
        return self.root / VERSIONS_DIRNAME / f"v{version:06d}.json"

    def _active_path(self) -> Path:
        return self.root / ACTIVE_FILENAME

    def _atomic_write(self, target: Path, data: bytes) -> None:
        tmp = target.with_name(target.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(target))

    # -- queries ----------------------------------------------------------

    def versions(self) -> tuple[int, ...]:
        found: list[int] = []
        for entry in (self.root / VERSIONS_DIRNAME).glob("v*.json"):
            match = re.fullmatch(r"v(\d{6})\.json", entry.name)
            if match:
                found.append(int(match.group(1)))
        return tuple(sorted(found))

    def active_version(self) -> int | None:
        path = self._active_path()
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        try:
            return int(text)
        except ValueError as exc:
            raise StorageError(
                f"ACTIVE pointer {str(path)!r} is corrupt: {text!r}"
            ) from exc

    def _load_envelope(self, version: int) -> dict[str, Any]:
        path = self._version_path(version)
        if not path.exists():
            raise NotFoundError(f"config version {version} is not stored")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(
                f"stored config version {version} is unreadable: {exc}"
            ) from exc
        envelope = _as_mapping(envelope, f"stored version {version}")
        if envelope.get("format") != STORE_FORMAT \
                or envelope.get("format_version") != STORE_FORMAT_VERSION:
            raise StorageError(
                f"stored config version {version} has unexpected store format"
            )
        if envelope.get("version") != version:
            raise StorageError(
                f"stored config version {version} envelope mismatch"
            )
        document = envelope.get("document")
        digest = envelope.get("doc_digest")
        if not isinstance(digest, str) or _sha256(_canonical(document)) != digest:
            raise StorageError(
                f"stored config version {version} failed digest verification; "
                "history is corrupt — nothing was applied"
            )
        # A stored snapshot must ALSO survive strict re-validation: a file
        # edited on disk can never re-enter the active configuration.
        parse_document(document, where=f"stored version {version}.document")
        return envelope

    def active_document(self) -> dict[str, Any] | None:
        version = self.active_version()
        if version is None:
            return None
        return dict(self._load_envelope(version)["document"])

    def active(self) -> StoredVersion | None:
        version = self.active_version()
        if version is None:
            return None
        envelope = self._load_envelope(version)
        return StoredVersion(version, str(envelope["doc_digest"]),
                            envelope.get("imported_at"))

    def document_at(self, version: int) -> dict[str, Any]:
        return dict(self._load_envelope(int(version))["document"])

    # -- mutations --------------------------------------------------------

    def import_document(self, raw: Any, *, imported_at: str | None = None
                       ) -> StoredVersion:
        """Validate a document, then append it as the new active version.

        All validation (strict parse, forbidden-field scan, default-template
        resolution against the *effective* merged view) happens before the
        first filesystem write; a refusal leaves the store byte-identical.
        """

        if isinstance(imported_at, str):
            if not imported_at.strip() or len(imported_at) > MAX_IMPORTED_AT_CHARS \
                    or _CONTROL_RE.search(imported_at) \
                    or "/" in imported_at or "\\" in imported_at:
                raise ValidationError(
                    "imported_at must be a short single-line path-free "
                    "timestamp string or None"
                )
            imported_at = imported_at.strip()
        elif imported_at is not None:
            raise ValidationError("imported_at must be a string or None")

        document = parse_document(raw)
        # Project-style guard at the store level too: the default template
        # must exist within this document's own template set once merged over
        # the current effective config (so an import cannot break the default
        # the UI is using).
        current = self.active_document()
        _effective_check = resolve(current, document.to_payload())

        payload = document.to_payload()
        digest = _sha256(_canonical(payload))
        active = self.active_version()
        if active is not None:
            active_envelope = self._load_envelope(active)
            if active_envelope["doc_digest"] == digest:
                return StoredVersion(active, digest,
                                   active_envelope.get("imported_at"))

        version = (max(self.versions()) + 1) if self.versions() else 1
        envelope = {
            "format": STORE_FORMAT,
            "format_version": STORE_FORMAT_VERSION,
            "version": version,
            "doc_digest": digest,
            "imported_at": imported_at,
            "document": payload,
        }
        self._atomic_write(self._version_path(version),
                          _canonical(envelope).encode("utf-8"))
        self._atomic_write(self._active_path(), str(version).encode("utf-8"))
        return StoredVersion(version, digest, imported_at)

    def select(self, version: int) -> StoredVersion:
        """Activate a previously stored VERSION (no renumbering, no rewrite).

        The target must exist, keep the strict store format, and verify its
        digest; enforcement-sensitive tampering therefore cannot be selected
        INTO.  Selecting the already-active version is an honest no-op that
        still returns the stored facts.
        """

        if isinstance(version, bool) or not isinstance(version, int) \
                or version < 1:
            raise ValidationError("selected version must be a positive integer")
        envelope = self._load_envelope(version)  # existence + integrity gate
        if version != self.active_version():
            self._atomic_write(self._active_path(), str(version).encode("utf-8"))
        return StoredVersion(version, str(envelope["doc_digest"]),
                            envelope.get("imported_at"))

    def rollback(self, version: int | None = None) -> StoredVersion:
        """Re-point ACTIVE at a previously stored version.

        Without an explicit target, the newest version strictly below the
        current one is selected; there must be one to roll back to.
        """

        current = self.active_version()
        if current is None:
            raise NotFoundError("config store has nothing to roll back from")
        if version is None:
            candidates = [v for v in self.versions() if v < current]
            if not candidates:
                raise NotFoundError(
                    f"config version {current} has no earlier version to roll "
                    "back to"
                )
            version = max(candidates)
        return self.select(version)


# ---------------------------------------------------------------------------
# operator CLI (python -m teamcraft.templates)
# ---------------------------------------------------------------------------

_CLI_EXAMPLES = """\
workflow (project-local, all reads/writes stay inside the store directory):

  python -m teamcraft.templates preview briefs.json   # strict-validate, show effective result, write nothing
  python -m teamcraft.templates import  briefs.json   # same validation, then append + activate vN
  python -m teamcraft.templates list                  # stored versions, ACTIVE marker
  python -m teamcraft.templates show                  # print the ACTIVE document
  python -m teamcraft.templates render std-brief --set objective="Fix login" \\
      --set scope=src/ --set reviewer=the-verifier     # explicit rendering to stdout
  python -m teamcraft.templates rollback              # re-activate the previous version
  python -m teamcraft.templates select 1              # re-activate any stored version

Store: defaults to <project root>/.teamcraft/templates (TEAMCRAFT_PROJECT_ROOT
and the cwd are honored via teamcraft.config); --store overrides it.

Rendering is explicit and everything else is inert: importing never renders or
executes anything, and display preferences are plain data the UI reads —
nothing here creates tasks, dispatches attempts, selects routes, runs
commands, or touches approvals.

exit codes: 0 ok · 1 document refused (validation/privilege) · 2 usage or
configuration error · 3 store integrity error · 4 not found
"""

_EXIT_OK = 0
_EXIT_REFUSED = 1
_EXIT_USAGE = 2
_EXIT_STORE = 3
_EXIT_MISSING = 4


def resolve_store_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Store directory for operator commands: explicit ``--store`` wins,
    otherwise ``<project root>/.teamcraft/templates`` via ``teamcraft.config``
    (so ``TEAMCRAFT_PROJECT_ROOT`` is honored)."""

    if explicit is not None and str(explicit).strip():
        return Path(os.fspath(explicit)).expanduser()
    return load_config().runtime_directory / STORE_DIRNAME


def _load_document_file(path: str) -> Any:
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotFoundError(f"document file not found: {str(file_path)!r}") from exc
    except OSError as exc:
        raise NotFoundError(f"document file unreadable: {str(file_path)!r} ({exc})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"document file {str(file_path)!r} is not valid JSON: {exc}"
        ) from exc


def _pretty(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _open_store(store_dir: Path) -> ConfigStore:
    if not store_dir.exists():
        raise NotFoundError(
            f"no template config store at {str(store_dir)!r}; run the "
            "'import' command first"
        )
    return ConfigStore(store_dir)


def _optional_active_document(store_dir: Path) -> dict[str, Any] | None:
    """Active document if a store already exists; creates nothing."""

    if not (store_dir / ACTIVE_FILENAME).exists():
        return None
    return ConfigStore(store_dir).active_document()


def _cmd_import(args: argparse.Namespace) -> int:
    store_dir = resolve_store_dir(args.store)
    candidate = parse_document(_load_document_file(args.file))
    store = ConfigStore(store_dir)
    previous = store.active()
    stored = store.import_document(candidate.to_payload(),
                                  imported_at=args.imported_at)
    if previous is not None and stored.version == previous.version \
            and stored.doc_digest == previous.doc_digest:
        print(f"unchanged: document matches active version {stored.version} "
              f"(digest {stored.doc_digest}); nothing new was written")
    else:
        print(f"imported and activated version {stored.version}")
        print(f"digest: {stored.doc_digest}")
        if stored.imported_at is not None:
            print(f"imported_at: {stored.imported_at}")
    return _EXIT_OK


def _cmd_preview(args: argparse.Namespace) -> int:
    store_dir = resolve_store_dir(args.store)
    candidate = parse_document(_load_document_file(args.file))
    payload = candidate.to_payload()
    digest = candidate.doc_digest
    active = _optional_active_document(store_dir)
    effective = resolve(active, payload)
    print(f"candidate digest: {digest}")
    if active is None:
        print(f"active config: none (store {str(store_dir)!r} is empty)")
    else:
        print(f"active config digest: {_sha256(_canonical(active))}")
    after = effective.digest
    # Honest comparison: the effective config AFTER the import versus the
    # effective config as it stands now (not just the raw candidate digest).
    current_effective = resolve(active, None)
    would_change = after != current_effective.digest
    print(f"effective digest after import: {after}")
    print(f"would change the effective config: {'yes' if would_change else 'no'}")
    print("effective document:")
    print(_pretty(effective.to_payload()))
    return _EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    store_dir = resolve_store_dir(args.store)
    if not store_dir.exists():
        print(f"no template config store at {str(store_dir)!r}; nothing stored")
        return _EXIT_OK
    store = ConfigStore(store_dir)
    active = store.active_version()
    versions = store.versions()
    if not versions:
        print(f"template config store {str(store_dir)!r} has no versions yet")
        return _EXIT_OK
    for version in versions:
        envelope = store._load_envelope(version)
        marker = "ACTIVE " if version == active else "       "
        imported_at = envelope.get("imported_at") or "-"
        print(f"v{version:<6} {marker} {envelope['doc_digest']}  "
              f"imported_at={imported_at}")
    return _EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    store = _open_store(resolve_store_dir(args.store))
    version = args.version
    if version is None:
        version = store.active_version()
        if version is None:
            raise NotFoundError("store has no active config version")
    document = store.document_at(version)
    print(f"version: {version}")
    print(_pretty(document))
    return _EXIT_OK


def _cmd_select(args: argparse.Namespace) -> int:
    store = _open_store(resolve_store_dir(args.store))
    stored = store.select(args.version)
    print(f"activated version {stored.version}")
    print(f"digest: {stored.doc_digest}")
    return _EXIT_OK


def _cmd_rollback(args: argparse.Namespace) -> int:
    store = _open_store(resolve_store_dir(args.store))
    stored = store.rollback(args.to_version)
    print(f"rolled back: activated version {stored.version}")
    print(f"digest: {stored.doc_digest}")
    return _EXIT_OK


def _collect_render_values(args: argparse.Namespace) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if args.values is not None:
        loaded = _load_document_file(args.values)
        if not isinstance(loaded, Mapping):
            raise ValidationError("--values file must contain a JSON object")
        values.update({str(k): v for k, v in loaded.items()})
    for assignment in args.set or []:
        if "=" not in assignment:
            raise ValidationError(
                f"--set expects KEY=VALUE, got {assignment!r}"
            )
        key, _, value = assignment.partition("=")
        values[key.strip()] = value
    return values


def _cmd_render(args: argparse.Namespace) -> int:
    store = _open_store(resolve_store_dir(args.store))
    document = store.active_document()
    if document is None:
        raise NotFoundError("store has no active config to render from")
    payloads = {str(t.get("id")): t for t in document.get("templates", [])}
    if args.template_id not in payloads:
        raise NotFoundError(
            f"template {args.template_id!r} is not in the active config; "
            f"available: {sorted(payloads)}"
        )
    template = parse_template(payloads[args.template_id])
    text = render_brief(template, _collect_render_values(args))
    sys.stdout.write(text)
    return _EXIT_OK


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m teamcraft.templates",
        description="Import, preview, select and roll back versioned Task "
                    "Brief templates and display preferences (M6-04). "
                    "Strict validation; brief text and display config only.",
        epilog=_CLI_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True,
                                      metavar="COMMAND")

    def _with_store(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--store", default=None,
                       help="config store directory (default: "
                            "<project root>/.teamcraft/templates)")
        return sub

    p_import = _with_store(subparsers.add_parser(
        "import", help="strict-validate FILE, then append + activate as vN"))
    p_import.add_argument("file", help="JSON document file")
    p_import.add_argument("--imported-at", dest="imported_at", default=None,
                         help="optional short audit stamp stored with the "
                              "version (never read from a clock)")

    p_preview = _with_store(subparsers.add_parser(
        "preview", help="strict-validate FILE and show the effective result; "
                       "writes nothing"))
    p_preview.add_argument("file", help="JSON document file")

    _with_store(subparsers.add_parser(
        "list", help="list stored versions and mark the ACTIVE one"))

    p_show = _with_store(subparsers.add_parser(
        "show", help="print a stored document (default: ACTIVE)"))
    p_show.add_argument("version", nargs="?", type=int, default=None,
                       help="stored version number (default: ACTIVE)")

    p_select = _with_store(subparsers.add_parser(
        "select", help="activate any stored VERSION after digest "
                      "re-verification"))
    p_select.add_argument("version", type=int, help="stored version number")

    p_rollback = _with_store(subparsers.add_parser(
        "rollback", help="activate the previous stored version "
                        "(or --to N)"))
    p_rollback.add_argument("--to", dest="to_version", type=int, default=None,
                           help="explicit target version (default: the newest "
                                "version below the active one)")

    p_render = _with_store(subparsers.add_parser(
        "render", help="explicitly render TEMPLATE_ID from the ACTIVE config "
                      "to stdout"))
    p_render.add_argument("template_id", help="template id from the active "
                                             "config")
    p_render.add_argument("--values", default=None,
                         help="JSON object file of placeholder values")
    p_render.add_argument("--set", action="append", metavar="KEY=VALUE",
                         default=None, help="inline placeholder value; may "
                                          "repeat, wins over --values")
    return parser


_CLI_HANDLERS = {
    "import": _cmd_import,
    "preview": _cmd_preview,
    "list": _cmd_list,
    "show": _cmd_show,
    "select": _cmd_select,
    "rollback": _cmd_rollback,
    "render": _cmd_render,
}


def _exit_code_for(exc: Exception) -> int:
    if isinstance(exc, ValidationError):  # includes ForbiddenFieldError
        return _EXIT_REFUSED
    if isinstance(exc, ConfigurationError):
        return _EXIT_USAGE
    if isinstance(exc, StorageError):
        return _EXIT_STORE
    if isinstance(exc, NotFoundError):
        return _EXIT_MISSING
    return _EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m teamcraft.templates``.

    All document validation happens before any store write (import is the
    only writing command); refusals print ``error:`` on stderr with the
    documented exit-code mapping and leave the store untouched.
    """

    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return _CLI_HANDLERS[args.command](args)
    except ForbiddenFieldError as exc:
        # Keep the machine-readable reason code on the operator's surface.
        print(f"error: [{exc.reason_code}] {exc}", file=sys.stderr)
        return _EXIT_REFUSED
    except (TeamCraftError, OSError) as exc:  # single operator-facing gate
        print(f"error: {exc}", file=sys.stderr)
        return _exit_code_for(exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
