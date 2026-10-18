#!/usr/bin/env python3
"""Create and recover role-scoped agents in a Herdr development team."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Sequence
import uuid


STATE_VERSION = 1
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
ROLES = ("ceo", "cto", "developer", "designer", "tester", "custom")
TERMINAL_PHASES = {"completed", "released"}
LIVE_STATES = {"blocked", "working", "idle", "done", "unknown"}
MIN_SPLIT_WIDTH = 50
MIN_SPLIT_HEIGHT = 16
PREFER_RIGHT_WIDTH = 120
SKILL_PATH = Path(__file__).resolve().parent.parent / "SKILL.md"

ROLE_RESPONSIBILITIES = {
    "ceo": "Own requirements, planning, staffing, assignments, review, integration, and the final delivery decision.",
    "cto": "Own architecture, technical tradeoffs, interfaces, and technical risk for the assigned scope.",
    "developer": "Implement the assigned code scope and provide verification evidence.",
    "designer": "Own product flow, interaction, visual specifications, and design assets in the assigned scope.",
    "tester": "Own acceptance coverage, reproduction, verification, and evidence for the assigned scope.",
}


class TeamctlError(RuntimeError):
    """An expected, actionable teamctl failure."""


class HerdrCommandError(TeamctlError):
    def __init__(self, argv: Sequence[str], returncode: int, detail: str) -> None:
        super().__init__(detail)
        self.argv = list(argv)
        self.returncode = returncode


class CommandTimeout(TeamctlError):
    def __init__(self, argv: Sequence[str], timeout_seconds: float) -> None:
        super().__init__(
            f"command timed out after {timeout_seconds:g}s: {' '.join(argv[:3])}; "
            "the remote phase may have completed"
        )


def _decode(data: bytes | None, stream: str) -> str:
    try:
        return (data or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TeamctlError(f"{stream} from Herdr was not valid UTF-8: {exc}") from exc


def run_process(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 15,
    accepted_codes: tuple[int, ...] = (0,),
) -> tuple[str, str, int]:
    try:
        completed = subprocess.run(
            list(argv), check=False, capture_output=True, timeout=timeout_seconds
        )
    except FileNotFoundError as exc:
        raise TeamctlError(f"executable was not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(argv, timeout_seconds) from exc
    stdout = _decode(completed.stdout, "stdout")
    stderr = _decode(completed.stderr, "stderr")
    if completed.returncode not in accepted_codes:
        detail = stderr.strip() or stdout.strip() or f"exit status {completed.returncode}"
        raise HerdrCommandError(argv, completed.returncode, detail)
    return stdout, stderr, completed.returncode


def run_herdr(arguments: Sequence[str], *, timeout_seconds: float = 15) -> dict[str, Any]:
    stdout, _, _ = run_process(["herdr", *arguments], timeout_seconds=timeout_seconds)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TeamctlError(
            f"Herdr returned invalid JSON for {' '.join(arguments[:2])}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise TeamctlError("Herdr returned a JSON value with an unexpected shape")
    return payload


def require_herdr() -> None:
    if os.environ.get("HERDR_ENV") != "1":
        raise TeamctlError("HERDR_ENV=1 is required; run teamctl inside Herdr")
    if not os.environ.get("HERDR_SOCKET_PATH"):
        raise TeamctlError("HERDR_SOCKET_PATH is required to scope team state")


def current_session_identity() -> dict[str, Any]:
    socket_path = Path(os.environ["HERDR_SOCKET_PATH"]).expanduser()
    identity: dict[str, Any] = {"socket_path": str(socket_path.resolve(strict=False))}
    try:
        stat = socket_path.stat()
    except OSError:
        identity.update(socket_device=None, socket_inode=None)
    else:
        identity.update(socket_device=stat.st_dev, socket_inode=stat.st_ino)
    return identity


def now_epoch() -> int:
    return int(time.time())


class StateStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.directory = project_root / ".herdr-team"
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any] | None]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield self.read_unlocked()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read_unlocked(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise TeamctlError(f"state file is not valid UTF-8: {self.path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TeamctlError(f"cannot read state file {self.path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
            raise TeamctlError(f"unsupported or malformed state file: {self.path}")
        return value

    def write_unlocked(self, state: dict[str, Any]) -> None:
        descriptor, temp_name = tempfile.mkstemp(
            prefix="state.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def member_operation(self, name: str) -> Iterator[None]:
        if not NAME_RE.fullmatch(name):
            raise TeamctlError("name must match [a-z][a-z0-9_-]{0,31}")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        operation_path = self.directory / f"member.{name}.lock"
        with operation_path.open("a+b") as operation_file:
            try:
                fcntl.flock(
                    operation_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except BlockingIOError as exc:
                raise TeamctlError(
                    f"another lifecycle operation is already running for member {name}"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(operation_file.fileno(), fcntl.LOCK_UN)


def new_state(project_root: Path, session: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "team_id": str(uuid.uuid4()),
        "project_root": str(project_root),
        "herdr_session": session,
        "created_at": now_epoch(),
        "members": {},
    }


def validate_state_session(state: dict[str, Any], session: dict[str, Any]) -> None:
    if state.get("herdr_session") != session:
        raise TeamctlError(
            "team state belongs to a different Herdr server session; do not adopt its pane or agent names automatically"
        )


def update_member(
    store: StateStore, session: dict[str, Any], name: str, **changes: Any
) -> dict[str, Any]:
    with store.locked() as state:
        if state is None:
            raise TeamctlError("team state disappeared during the operation")
        validate_state_session(state, session)
        try:
            member = state["members"][name]
        except (KeyError, TypeError) as exc:
            raise TeamctlError(f"member is not recorded: {name}") from exc
        member.update(changes)
        member["updated_at"] = now_epoch()
        store.write_unlocked(state)
        return dict(member)


def resolve_project_root(value: str | None) -> Path:
    root = Path(value).expanduser() if value else Path.cwd()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise TeamctlError(f"cannot resolve project directory {root}: {exc}") from exc
    if not root.is_dir():
        raise TeamctlError(f"project directory is not a directory: {root}")
    return root


def parse_task(args: argparse.Namespace) -> str:
    if args.task is not None:
        task = args.task.strip()
    else:
        task_path = Path(args.task_file).expanduser()
        try:
            task = task_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise TeamctlError(f"task file is not valid UTF-8: {task_path}") from exc
        except OSError as exc:
            raise TeamctlError(f"cannot read task file {task_path}: {exc}") from exc
    if not task:
        raise TeamctlError("task must not be empty")
    return task


def role_responsibility(role: str, supplied: str | None) -> str:
    if role == "custom":
        if supplied is None or not supplied.strip():
            raise TeamctlError("--responsibility is required for custom roles")
        return supplied.strip()
    if supplied is not None:
        raise TeamctlError("--responsibility is only valid with --role custom")
    return ROLE_RESPONSIBILITIES[role]


def normalize_ownership(raw_claims: Sequence[str], read_only: bool) -> list[str]:
    if read_only and raw_claims:
        raise TeamctlError("--read-only assignments cannot reserve --ownership")
    normalized: list[str] = []
    for raw in raw_claims:
        claim = raw.strip()
        recursive = claim.endswith("/**")
        value = claim[:-3] if recursive else claim
        if (
            not value
            or value in (".", "./")
            or value.startswith("./")
            or value.startswith("/")
            or "\\" in value
        ):
            raise TeamctlError(f"ownership must be a project-relative path: {raw!r}")
        if any(char in value for char in "*?[]{}"):
            raise TeamctlError(
                f"unsupported ownership glob {raw!r}; only a trailing /** is allowed"
            )
        parts = PurePosixPath(value).parts
        if not parts or any(
            part in ("", ".", "..")
            or not part.strip()
            or any(ord(char) < 32 for char in part)
            for part in parts
        ):
            raise TeamctlError(
                f"ownership must use normalized path segments, not prose or traversal: {raw!r}"
            )
        result = "/".join(parts) + ("/**" if recursive else "")
        if result not in normalized:
            normalized.append(result)
    return normalized


def ownership_parts(claim: str) -> tuple[str, ...]:
    return PurePosixPath(claim[:-3] if claim.endswith("/**") else claim).parts


def claims_overlap(left: str, right: str) -> bool:
    left_parts, right_parts = ownership_parts(left), ownership_parts(right)
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def reserve_member(
    store: StateStore, session: dict[str, Any], member: dict[str, Any]
) -> dict[str, Any]:
    with store.locked() as state:
        if state is None:
            state = new_state(store.project_root, session)
        validate_state_session(state, session)
        members = state.setdefault("members", {})
        if member["name"] in members:
            raise TeamctlError(f"member name is already recorded: {member['name']}")
        if not member["read_only"]:
            for other in members.values():
                if other.get("read_only") or other.get("phase") in TERMINAL_PHASES:
                    continue
                for claim in member["ownership"]:
                    for existing in other.get("ownership", []):
                        if claims_overlap(claim, existing):
                            raise TeamctlError(
                                f"ownership {claim!r} overlaps active member {other.get('name')!r} claim {existing!r}"
                            )
        members[member["name"]] = member
        store.write_unlocked(state)
        return dict(member)


def supported_kinds() -> set[str]:
    stdout, stderr, _ = run_process(
        ["herdr", "agent"], timeout_seconds=5, accepted_codes=(0, 2)
    )
    match = re.search(
        r"^\s*kinds:\s*([^\r\n]+)", stdout + "\n" + stderr, re.MULTILINE
    )
    if not match:
        raise TeamctlError("could not discover supported agent kinds from installed Herdr")
    return {item.strip() for item in match.group(1).split("|") if item.strip()}


def preflight_kind(kind: str) -> None:
    if kind not in supported_kinds():
        raise TeamctlError(f"unsupported Herdr agent kind: {kind}")
    if shutil.which(kind) is None:
        raise TeamctlError(f"agent executable is not available in PATH: {kind}")


def agents_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        agents = payload["result"]["agents"]
    except (KeyError, TypeError) as exc:
        raise TeamctlError("Herdr agent list response is missing agent data") from exc
    if not isinstance(agents, list):
        raise TeamctlError("Herdr agent list returned invalid agent data")
    return [agent for agent in agents if isinstance(agent, dict)]


def ensure_live_name_available(name: str) -> None:
    if any(
        agent.get("name") == name
        for agent in agents_from(run_herdr(["agent", "list"]))
    ):
        raise TeamctlError(f"a live Herdr agent is already named {name!r}")


def pane_from(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        pane = payload["result"]["pane"]
    except (KeyError, TypeError) as exc:
        raise TeamctlError("Herdr response is missing pane data") from exc
    if not isinstance(pane, dict) or not pane.get("pane_id"):
        raise TeamctlError("Herdr returned invalid pane data")
    return pane


def resolve_pane(target: str | None) -> dict[str, Any]:
    selector = ["--pane", target] if target else ["--current"]
    return pane_from(run_herdr(["pane", "current", *selector]))


def get_target_rect(payload: dict[str, Any], pane_id: str) -> tuple[int, int]:
    try:
        panes = payload["result"]["layout"]["panes"]
    except (KeyError, TypeError) as exc:
        raise TeamctlError("Herdr pane layout response is missing layout data") from exc
    for pane in panes:
        if isinstance(pane, dict) and pane.get("pane_id") == pane_id:
            try:
                return int(pane["rect"]["width"]), int(pane["rect"]["height"])
            except (KeyError, TypeError, ValueError) as exc:
                raise TeamctlError("target pane has invalid geometry data") from exc
    raise TeamctlError(f"canonical pane {pane_id!r} was not found in its layout")


def choose_direction(requested: str | None, width: int, height: int) -> str:
    can_right = width >= MIN_SPLIT_WIDTH * 2
    can_down = height >= MIN_SPLIT_HEIGHT * 2
    if requested == "right" and not can_right:
        raise TeamctlError(f"pane is too narrow to split right ({width} columns)")
    if requested == "down" and not can_down:
        raise TeamctlError(f"pane is too short to split down ({height} rows)")
    if requested:
        return requested
    if can_right and width >= PREFER_RIGHT_WIDTH:
        return "right"
    if can_down:
        return "down"
    if can_right:
        return "right"
    raise TeamctlError(f"pane is too small to split safely ({width}x{height})")


def agent_info_from(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        agent = payload["result"]["agent"]
    except (KeyError, TypeError) as exc:
        raise TeamctlError("Herdr response is missing agent data") from exc
    if not isinstance(agent, dict):
        raise TeamctlError("Herdr returned invalid agent data")
    return agent


def live_state(agent: dict[str, Any] | None) -> str:
    if not agent:
        return "missing"
    value = agent.get("agent_status")
    return value if value in LIVE_STATES else "unknown"


def build_prompt(member: dict[str, Any]) -> str:
    if member["read_only"]:
        ownership_text = "- Read-only assignment. Do not modify project files."
    elif member["ownership"]:
        ownership_text = "\n".join(f"- {item}" for item in member["ownership"])
    elif member["role"] == "ceo":
        ownership_text = "- Own coordination and final integration. Do not overlap active worker edits."
    else:
        ownership_text = "- No edit ownership. Coordinate scope before modifying files."
    lines = [
        f"You are the {member['role'].upper()} agent named {member['name']}.",
        f"Working directory: {member['cwd']}",
        f"Responsibility: {member['responsibility']}",
        "",
        "Assignment:",
        member["task"],
        "",
        "File or module ownership:",
        ownership_text,
        "",
        "Do not edit any file outside the ownership listed above. If the assignment "
        "requires another path, stop and ask the CEO to change the recorded ownership.",
        "Preserve user and peer changes. Report deliverables, changed files, verification evidence, and remaining risks or blockers.",
    ]
    if member["role"] == "ceo":
        lines.extend(
            [
                "",
                f"Read and follow the team skill at {SKILL_PATH}.",
                "Create only needed workers. Give each concrete deliverables, acceptance criteria, and non-overlapping ownership; review evidence before integration.",
            ]
        )
    return "\n".join(lines)


def opencode_profile(role: str) -> str:
    return f"herdr-{role}"


def opencode_config_content(project_root: Path, role: str) -> str:
    profile = opencode_profile(role)
    profile_path = project_root / ".opencode" / "agents" / f"{profile}.md"
    if not profile_path.is_file():
        raise TeamctlError(
            f"OpenCode role profile is missing for this project: {profile_path}"
        )
    raw = os.environ.get("OPENCODE_CONFIG_CONTENT")
    if raw:
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TeamctlError(
                f"OPENCODE_CONFIG_CONTENT is not valid JSON: {exc}"
            ) from exc
        if not isinstance(config, dict):
            raise TeamctlError("OPENCODE_CONFIG_CONTENT must contain a JSON object")
        config = dict(config)
    else:
        config = {}
    config["default_agent"] = profile
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


def split_for_member(
    member: dict[str, Any], store: StateStore, session: dict[str, Any]
) -> dict[str, Any]:
    parent = resolve_pane(member.get("from_pane_id"))
    parent_id = str(parent["pane_id"])
    layout = run_herdr(["pane", "layout", "--pane", parent_id])
    width, height = get_target_rect(layout, parent_id)
    direction = choose_direction(member.get("direction"), width, height)
    split_args = [
        "pane", "split", "--pane", parent_id, "--direction", direction,
        "--cwd", member["cwd"], "--no-focus",
    ]
    if member["kind"] == "opencode":
        override = member.get("opencode_config_content")
        if not override:
            override = opencode_config_content(Path(member["cwd"]), member["role"])
        split_args.extend(["--env", f"OPENCODE_CONFIG_CONTENT={override}"])
    update_member(
        store, session, member["name"], phase="split_requested",
        from_pane_id=parent_id, direction=direction,
    )
    try:
        pane = pane_from(run_herdr(split_args))
    except TeamctlError as exc:
        update_member(
            store, session, member["name"], phase="split_ambiguous", error=str(exc)
        )
        raise TeamctlError(
            "pane split outcome is ambiguous; inspect the layout before explicitly "
            f"retrying resume {member['name']} --split-action retry. Cause: {exc}"
        ) from exc
    return update_member(
        store, session, member["name"], phase="pane_created",
        pane_id=pane["pane_id"], terminal_id=pane.get("terminal_id"),
        from_pane_id=parent_id, direction=direction,
    )


def start_member(
    member: dict[str, Any], store: StateStore, session: dict[str, Any]
) -> dict[str, Any]:
    name, pane_id = member["name"], member.get("pane_id")
    if not pane_id:
        raise TeamctlError(f"member {name} has no recorded pane")
    update_member(store, session, name, phase="start_requested")
    args = [
        "agent", "start", name, "--kind", member["kind"], "--pane", pane_id,
        "--timeout", str(member["start_timeout_ms"]),
    ]
    if member.get("agent_args"):
        args.extend(["--", *member["agent_args"]])
    try:
        run_herdr(
            args, timeout_seconds=max(10, member["start_timeout_ms"] / 1000 + 5)
        )
    except TeamctlError as exc:
        update_member(store, session, name, phase="start_ambiguous", error=str(exc))
        raise TeamctlError(
            f"agent start is ambiguous in existing pane {pane_id}; resume {name} to inspect or reuse that pane. Cause: {exc}"
        ) from exc
    info = agent_info_from(run_herdr(["agent", "get", name]))
    return update_member(
        store, session, name, phase="agent_started",
        pane_id=info.get("pane_id", pane_id),
        terminal_id=info.get("terminal_id", member.get("terminal_id")),
        agent_session=info.get("agent_session"), live_state=live_state(info), error=None,
    )


def prompt_member(
    member: dict[str, Any],
    store: StateStore,
    session: dict[str, Any],
    *,
    wait: bool,
    timeout_ms: int,
) -> dict[str, Any]:
    name = member["name"]
    update_member(
        store, session, name, phase="prompt_submitting",
        prompt_may_have_been_delivered=True,
    )
    args = ["agent", "prompt", name, build_prompt(member)]
    if wait:
        args.extend(["--wait", "--timeout", str(timeout_ms)])
    try:
        run_herdr(
            args, timeout_seconds=max(15, timeout_ms / 1000 + 5) if wait else 15
        )
    except TeamctlError as exc:
        update_member(
            store, session, name, phase="prompt_ambiguous", error=str(exc),
            prompt_may_have_been_delivered=True,
        )
        raise TeamctlError(
            f"prompt outcome for {name} is ambiguous; do not resend automatically. Run status, then resume {name} --prompt-action send|skip after inspection. Cause: {exc}"
        ) from exc
    info = agent_info_from(run_herdr(["agent", "get", name]))
    return update_member(
        store, session, name, phase="prompt_submitted", live_state=live_state(info),
        agent_session=info.get("agent_session", member.get("agent_session")),
        terminal_id=info.get("terminal_id", member.get("terminal_id")), error=None,
    )


def member_output(
    state: dict[str, Any], member: dict[str, Any], agent: dict[str, Any] | None = None
) -> dict[str, Any]:
    actual = live_state(agent) if agent is not None else member.get("live_state", "unknown")
    return {
        "team_id": state["team_id"], "name": member["name"],
        "role": member["role"], "kind": member["kind"],
        "pane_id": member.get("pane_id"), "terminal_id": member.get("terminal_id"),
        "phase": member["phase"], "agent_state": actual,
        "blocked": actual == "blocked", "read_only": member["read_only"],
        "ownership": member["ownership"],
    }


def validate_common_launch(
    args: argparse.Namespace,
) -> tuple[Path, str, str, list[str]]:
    if not NAME_RE.fullmatch(args.name):
        raise TeamctlError("name must match [a-z][a-z0-9_-]{0,31}")
    if args.start_timeout_ms <= 3000 or args.start_timeout_ms > 300_000:
        raise TeamctlError("--start-timeout-ms must be greater than 3000 and at most 300000")
    if args.wait and args.timeout_ms <= 0:
        raise TeamctlError("--timeout-ms must be greater than zero")
    root = resolve_project_root(args.cwd)
    task = parse_task(args)
    responsibility = role_responsibility(args.role, args.responsibility)
    ownership = normalize_ownership(args.ownership, args.read_only)
    if not args.read_only and args.role != "ceo" and not ownership:
        raise TeamctlError(
            "editing workers require at least one --ownership; use --read-only otherwise"
        )
    return root, task, responsibility, ownership


def _launch_operation(args: argparse.Namespace) -> int:
    require_herdr()
    root, task, responsibility, ownership = validate_common_launch(args)
    preflight_kind(args.kind)
    ensure_live_name_available(args.name)
    parent = resolve_pane(args.from_pane)
    session, store = current_session_identity(), StateStore(root)
    opencode_content = (
        opencode_config_content(root, args.role) if args.kind == "opencode" else None
    )
    member = {
        "name": args.name, "role": args.role, "responsibility": responsibility,
        "kind": args.kind, "task": task, "ownership": ownership,
        "read_only": args.read_only, "cwd": str(root),
        "from_pane_id": parent["pane_id"], "pane_id": None, "terminal_id": None,
        "agent_session": None, "agent_args": list(args.agent_arg),
        "opencode_config_content": opencode_content,
        "direction": args.direction, "start_timeout_ms": args.start_timeout_ms,
        "phase": "reserved", "prompt_may_have_been_delivered": False,
        "live_state": "missing", "created_at": now_epoch(),
        "updated_at": now_epoch(), "error": None,
    }
    reserve_member(store, session, member)
    member = split_for_member(member, store, session)
    member = start_member(member, store, session)
    member = prompt_member(
        member, store, session, wait=args.wait, timeout_ms=args.timeout_ms
    )
    with store.locked() as state:
        assert state is not None
        output = member_output(state, member)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


def load_member_for_mutation(
    root: Path, name: str
) -> tuple[StateStore, dict[str, Any], dict[str, Any], dict[str, Any]]:
    session, store = current_session_identity(), StateStore(root)
    with store.locked() as state:
        if state is None:
            raise TeamctlError(f"no team state exists in {root}")
        validate_state_session(state, session)
        try:
            member = dict(state["members"][name])
        except (KeyError, TypeError) as exc:
            raise TeamctlError(f"member is not recorded: {name}") from exc
        return store, session, state, member


def verify_recorded_occupant(
    member: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pane_id = member.get("pane_id")
    if not pane_id:
        return {}, None
    try:
        pane = pane_from(run_herdr(["pane", "current", "--pane", pane_id]))
    except HerdrCommandError:
        # A moved pane gets a new public ID. Resolve by the still-unique live name,
        # then require persisted terminal/session identity before adopting it.
        candidate = agent_info_from(run_herdr(["agent", "get", member["name"]]))
        pane = candidate
    recorded_terminal = member.get("terminal_id")
    if recorded_terminal and pane.get("terminal_id") != recorded_terminal:
        raise TeamctlError(
            f"pane {pane_id} now has a different terminal; refusing to adopt its occupant"
        )
    if pane.get("agent"):
        agent = pane
        # `pane current` reports the harness kind and native session, but a live
        # response does not necessarily include Herdr's assigned agent name.
        # Resolve that name explicitly, then require it to identify this exact
        # terminal before adopting the occupant.
        if not pane.get("name"):
            try:
                candidate = agent_info_from(
                    run_herdr(["agent", "get", member["name"]])
                )
            except TeamctlError as exc:
                raise TeamctlError(
                    f"pane {pane['pane_id']} contains an unrelated agent {pane.get('agent')}"
                ) from exc
            if (
                candidate.get("pane_id") != pane.get("pane_id")
                or candidate.get("terminal_id") != pane.get("terminal_id")
                or candidate.get("agent") != pane.get("agent")
            ):
                raise TeamctlError(
                    f"pane {pane['pane_id']} contains an unrelated agent {pane.get('agent')}"
                )
            agent = candidate
        if agent.get("name") != member["name"] or agent.get("agent") != member["kind"]:
            raise TeamctlError(
                f"pane {pane['pane_id']} contains unrelated agent {agent.get('name') or agent.get('agent')}"
            )
        recorded_session = member.get("agent_session")
        current_session = agent.get("agent_session")
        if recorded_session:
            if not current_session:
                raise TeamctlError(
                    "live occupant did not report an agent session; identity cannot be verified"
                )
            if recorded_session != current_session:
                raise TeamctlError("recorded agent session does not match the live occupant")
        return pane, agent
    process = run_herdr(["pane", "process-info", "--pane", pane["pane_id"]])
    try:
        info = process["result"]["process_info"]
        shell_pid = info.get("shell_pid")
        foreground = info.get("foreground_processes", [])
    except (KeyError, TypeError) as exc:
        raise TeamctlError("Herdr process-info response is malformed") from exc
    if any(
        item.get("pid") != shell_pid for item in foreground if isinstance(item, dict)
    ):
        raise TeamctlError(f"pane {pane['pane_id']} contains an unrelated foreground process")
    return pane, None


def _resume_operation(args: argparse.Namespace) -> int:
    require_herdr()
    if args.wait and args.timeout_ms <= 0:
        raise TeamctlError("--timeout-ms must be greater than zero")
    root = resolve_project_root(args.cwd)
    store, session, state, member = load_member_for_mutation(root, args.name)
    if member["phase"] in TERMINAL_PHASES:
        raise TeamctlError(f"member {args.name} is already {member['phase']}")
    preflight_kind(member["kind"])
    pane, agent = verify_recorded_occupant(member)
    if pane and pane.get("pane_id") != member.get("pane_id"):
        member = update_member(
            store, session, args.name, pane_id=pane["pane_id"],
            terminal_id=pane.get("terminal_id", member.get("terminal_id")),
        )
    if (
        not pane
        and member["phase"] in {"split_requested", "split_ambiguous"}
        and args.split_action != "retry"
    ):
        output = member_output(state, member)
        output["prompt_action"] = args.prompt_action
        output["next_action_required"] = (
            "inspect the current layout, then use --split-action retry only if no pane was created"
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    if not pane:
        member = split_for_member(member, store, session)
        pane = pane_from(run_herdr(["pane", "get", member["pane_id"]]))
    if agent is None:
        member = start_member(member, store, session)
        agent = agent_info_from(run_herdr(["agent", "get", args.name]))
    else:
        member = update_member(
            store, session, args.name,
            terminal_id=agent.get("terminal_id", member.get("terminal_id")),
            agent_session=agent.get("agent_session", member.get("agent_session")),
            live_state=live_state(agent),
        )
    if args.prompt_action == "send":
        member = prompt_member(
            member, store, session, wait=args.wait, timeout_ms=args.timeout_ms
        )
        agent = agent_info_from(run_herdr(["agent", "get", args.name]))
    elif args.prompt_action == "skip":
        member = update_member(
            store, session, args.name, phase="prompt_skipped", error=None
        )
    with store.locked() as latest:
        assert latest is not None
        output = member_output(latest, member, agent)
    output["prompt_action"] = args.prompt_action
    if args.prompt_action == "inspect":
        if member.get("prompt_may_have_been_delivered"):
            output["next_action_required"] = (
                "choose --prompt-action send or skip after inspection"
            )
        elif member["phase"] not in {"prompt_submitted", "prompt_skipped"}:
            output["next_action_required"] = (
                "use --prompt-action send to submit the recorded assignment"
            )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


def status(args: argparse.Namespace) -> int:
    require_herdr()
    root, session = resolve_project_root(args.cwd), current_session_identity()
    store = StateStore(root)
    with store.locked() as state:
        if state is None:
            print(json.dumps({"project_root": str(root), "team": None, "members": []}))
            return 0
        session_match = state.get("herdr_session") == session
        snapshot = json.loads(json.dumps(state))
    results = []
    for member in snapshot.get("members", {}).values():
        agent, error = None, None
        effective_member = dict(member)
        if not session_match:
            effective_member["live_state"] = "unknown"
            error = "recorded state belongs to a different Herdr session"
        elif member.get("pane_id"):
            try:
                pane, agent = verify_recorded_occupant(member)
                if agent is None and pane:
                    effective_member["live_state"] = "missing"
                    error = "recorded pane contains an available shell, not a live agent"
            except TeamctlError as exc:
                effective_member["live_state"] = "unknown"
                error = str(exc)
        else:
            effective_member["live_state"] = "missing"
        item = member_output(snapshot, effective_member, agent)
        if error:
            item["inspection_error"] = error
        results.append(item)
    print(
        json.dumps(
            {"project_root": str(root), "team_id": snapshot["team_id"],
             "session_match": session_match, "members": results},
            ensure_ascii=False, sort_keys=True,
        )
    )
    return 0


def _finish_member_operation(args: argparse.Namespace) -> int:
    require_herdr()
    root = resolve_project_root(args.cwd)
    store, session, state, member = load_member_for_mutation(root, args.name)
    agent = None
    if member.get("pane_id"):
        _, agent = verify_recorded_occupant(member)
    current_state = live_state(agent)
    if current_state in {"working", "blocked", "unknown"} and not args.force:
        raise TeamctlError(
            f"member {args.name} is {current_state}; wait for a verified idle/done state "
            "or repeat with --force"
        )
    phase = "completed" if args.command == "complete" else "released"
    member = update_member(
        store, session, args.name, phase=phase, ownership=[], error=None
    )
    output = member_output(state, member, agent)
    output["pane_closed"] = False
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


def launch(args: argparse.Namespace) -> int:
    require_herdr()
    root = resolve_project_root(args.cwd)
    store = StateStore(root)
    with store.member_operation(args.name):
        return _launch_operation(args)


def resume(args: argparse.Namespace) -> int:
    require_herdr()
    root = resolve_project_root(args.cwd)
    store = StateStore(root)
    with store.member_operation(args.name):
        return _resume_operation(args)


def finish_member(args: argparse.Namespace) -> int:
    require_herdr()
    root = resolve_project_root(args.cwd)
    store = StateStore(root)
    with store.member_operation(args.name):
        return _finish_member_operation(args)


def add_cwd(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", help="project root containing .herdr-team")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, inspect, and recover a stateful Herdr development team."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    launch_parser = commands.add_parser("launch")
    launch_parser.add_argument("--role", required=True, choices=ROLES)
    launch_parser.add_argument("--responsibility")
    launch_parser.add_argument("--kind", required=True)
    launch_parser.add_argument("--name", required=True)
    tasks = launch_parser.add_mutually_exclusive_group(required=True)
    tasks.add_argument("--task")
    tasks.add_argument("--task-file")
    launch_parser.add_argument("--cwd")
    launch_parser.add_argument("--from-pane")
    launch_parser.add_argument("--direction", choices=("right", "down"))
    launch_parser.add_argument("--ownership", action="append", default=[])
    launch_parser.add_argument("--read-only", action="store_true")
    launch_parser.add_argument("--agent-arg", action="append", default=[])
    launch_parser.add_argument("--start-timeout-ms", type=int, default=30_000)
    launch_parser.add_argument("--wait", action="store_true")
    launch_parser.add_argument("--timeout-ms", type=int, default=120_000)
    launch_parser.set_defaults(handler=launch)

    resume_parser = commands.add_parser("resume")
    resume_parser.add_argument("name")
    add_cwd(resume_parser)
    resume_parser.add_argument(
        "--prompt-action", choices=("inspect", "send", "skip"), default="inspect"
    )
    resume_parser.add_argument(
        "--split-action", choices=("inspect", "retry"), default="inspect"
    )
    resume_parser.add_argument("--wait", action="store_true")
    resume_parser.add_argument("--timeout-ms", type=int, default=120_000)
    resume_parser.set_defaults(handler=resume)

    status_parser = commands.add_parser("status")
    add_cwd(status_parser)
    status_parser.set_defaults(handler=status)
    for name in ("release", "complete"):
        finish_parser = commands.add_parser(name)
        finish_parser.add_argument("name")
        add_cwd(finish_parser)
        finish_parser.add_argument(
            "--force", action="store_true",
            help="release ownership despite a verified working, blocked, or unknown state",
        )
        finish_parser.set_defaults(handler=finish_member)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except TeamctlError as exc:
        print(f"teamctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
