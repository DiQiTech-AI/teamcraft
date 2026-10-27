"""M1-01 offline compatibility contract fixtures for the existing teamctl CLI.

These tests freeze the *current visible* behaviour that the original 20 tests
in ``test_teamctl.py`` did not pin: parser defaults, error exit codes, the
stdout JSON field set, representative Herdr argv sequences (conservative
recovery included), state.json v1 layout and the legacy write path's
blindness to an externally planted cutover marker, foreign-session
behaviour, OpenCode pane-local config, and the guarantee that
``complete``/``release`` never close a pane or stop an agent.

The M1-01 fixtures originally honoured these rules:
- offline only: every Herdr call is intercepted by a local double;
- no product-code change, no SQLite, no migration, no cutover;
- assertions target stable shapes (keys, argv, exit codes), not prose;
- ``test_json_write_path_ignores_external_cutover_marker`` documents a
  read-only threat (see M0 review 8.3 / 11.1) and does NOT implement any
  fence, guard, or claim that a stale pre-cutover process exists.

The 2026-10-27 runtime-directory follow-up intentionally updates path
assertions and adds fail-closed coverage for legacy state and symlinks. It
still performs no migration, cutover, network access, or real Herdr call.
"""

from __future__ import annotations

import hashlib
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "teamctl.py"
SPEC = importlib.util.spec_from_file_location("herdr_teamctl_compat", SCRIPT)
assert SPEC and SPEC.loader
teamctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(teamctl)

PARENT_PANE_ID = "w1:p1"
CHILD_PANE_ID = "w1:p2"
AGENT_SESSION = {"kind": "id", "value": "native-1"}
# Layout wide enough that choose_direction() deterministically prefers "right".
LAYOUT_WIDTH = 140
LAYOUT_HEIGHT = 40


def make_agent_payload(name, *, kind="codex", pane_id=CHILD_PANE_ID,
                      terminal_id="term-2", status="working", session=None):
    return {
        "name": name, "agent": kind, "pane_id": pane_id,
        "terminal_id": terminal_id,
        "agent_session": dict(AGENT_SESSION if session is None else session),
        "agent_status": status,
    }


class HerdrDouble:
    """Offline stand-in for run_herdr that records argv in call order."""

    def __init__(self, *, occupant_name="dev", kind="codex", status="idle",
                 layout_width=LAYOUT_WIDTH, layout_height=LAYOUT_HEIGHT):
        self.calls = []
        self.occupant_name = occupant_name
        self.kind = kind
        self.status = status
        self.layout_width = layout_width
        self.layout_height = layout_height

    def __call__(self, arguments, **_kwargs):
        args = [str(item) for item in arguments]
        self.calls.append(args)
        head = tuple(args[:2])
        if head == ("agent", "list"):
            return {"result": {"agents": []}}
        if head == ("pane", "current"):
            if "--current" in args:
                return {"result": {"pane": {"pane_id": PARENT_PANE_ID}}}
            pane_id = args[args.index("--pane") + 1]
            payload = make_agent_payload(
                self.occupant_name, kind=self.kind, pane_id=pane_id,
                status=self.status,
            )
            return {"result": {"pane": payload}}
        if head == ("pane", "layout"):
            return {"result": {"layout": {"panes": [
                {"pane_id": PARENT_PANE_ID,
                 "rect": {"width": self.layout_width, "height": self.layout_height}},
            ]}}}
        if head == ("pane", "split"):
            return {"result": {"pane": {
                "pane_id": CHILD_PANE_ID, "terminal_id": "term-2",
            }}}
        if head == ("pane", "get"):
            return {"result": {"pane": {"pane_id": args[2]}}}
        if head in {("agent", "start"), ("agent", "prompt")}:
            return {"result": {}}
        if head == ("agent", "get"):
            return {"result": {"agent": make_agent_payload(
                args[2], kind=self.kind, status=self.status,
            )}}
        raise AssertionError(f"unexpected Herdr call: {args}")


def make_member(name, ownership, **changes):
    member = {
        "name": name, "role": "developer",
        "responsibility": "Implement the assigned scope.",
        "kind": "codex", "task": "Implement and verify.",
        "ownership": ownership, "read_only": False, "cwd": "/project",
        "from_pane_id": PARENT_PANE_ID, "pane_id": None, "terminal_id": None,
        "agent_session": None, "agent_args": [],
        "opencode_config_content": None, "direction": None,
        "start_timeout_ms": 30_000, "phase": "reserved",
        "prompt_may_have_been_delivered": False, "live_state": "missing",
        "created_at": 1, "updated_at": 1, "error": None,
    }
    member.update(changes)
    return member


class CompatContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_resolved = self.root.resolve()
        self.socket = self.root / "herdr.sock"
        self.socket.touch()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HERDR_ENV": "1",
                "HERDR_SOCKET_PATH": str(self.socket),
                "HERDR_PANE_ID": PARENT_PANE_ID,
            },
        )
        self.env.start()
        self.session = teamctl.current_session_identity()
        self.store = teamctl.StateStore(self.root_resolved)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    # --------------------------------------------------------------- helpers

    def run_main(self, argv):
        """Run teamctl.main capturing stdout/stderr; returns (code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = teamctl.main(argv)
        return code, out.getvalue(), err.getvalue()

    def launch(self, extra=None, double=None, *, role="developer", name="dev",
               kind="codex", ownership=("--ownership", "src/api/**")):
        double = double or HerdrDouble()
        argv = ["launch", "--role", role, "--kind", kind, "--name", name,
                "--task", "Implement the assigned scope.",
                "--cwd", str(self.root_resolved), *ownership]
        argv.extend(extra or [])
        with mock.patch.object(teamctl, "preflight_kind"), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(argv)
        return code, out, err, double

    # ------------------------------------------- parser / defaults / exit codes

    def test_missing_subcommand_exits_2(self):
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
            teamctl.main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_launch_parse_errors_exit_2(self):
        cases = {
            "missing_required": ["launch"],
            "bad_role_choice": ["launch", "--role", "boss", "--kind", "codex",
                               "--name", "ceo", "--task", "t"],
            "task_mutex": ["launch", "--role", "ceo", "--kind", "codex",
                          "--name", "ceo", "--task", "t", "--task-file", "f.md"],
        }
        for label, argv in cases.items():
            with self.subTest(label):
                with self.assertRaises(SystemExit) as ctx, \
                        redirect_stderr(io.StringIO()):
                    teamctl.main(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_cli_defaults_are_frozen_per_command(self):
        parser = teamctl.build_parser()
        launch = parser.parse_args(
            ["launch", "--role", "ceo", "--kind", "codex", "--name", "ceo",
             "--task", "t"]
        )
        self.assertEqual(launch.start_timeout_ms, 30_000)
        self.assertEqual(launch.timeout_ms, 120_000)
        self.assertFalse(launch.wait)
        self.assertEqual(launch.ownership, [])
        self.assertEqual(launch.agent_arg, [])
        self.assertFalse(launch.read_only)
        self.assertIsNone(launch.direction)
        self.assertIsNone(launch.from_pane)
        self.assertIsNone(launch.cwd)
        resume = parser.parse_args(["resume", "dev"])
        self.assertEqual(resume.prompt_action, "inspect")
        self.assertEqual(resume.split_action, "inspect")
        self.assertFalse(resume.wait)
        self.assertEqual(resume.timeout_ms, 120_000)
        status = parser.parse_args(["status"])
        self.assertIsNone(status.cwd)
        for command in ("release", "complete"):
            finish = parser.parse_args([command, "dev"])
            self.assertFalse(finish.force)

    def test_status_outside_herdr_exits_1_with_prefix(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, err = self.run_main(["status", "--cwd", str(self.root)])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("teamctl: "), err)
        self.assertIn("HERDR_ENV=1 is required", err)

    def test_launch_validation_errors_exit_1(self):
        base = ["launch", "--kind", "codex", "--cwd", str(self.root_resolved)]
        cases = {
            "bad_name": (
                [*base, "--role", "developer", "--name", "Dev", "--task", "t",
                 "--ownership", "src/**"],
                "name must match",
            ),
            "timeout_low": (
                [*base, "--role", "developer", "--name", "dev", "--task", "t",
                 "--ownership", "src/**", "--start-timeout-ms", "3000"],
                "start-timeout-ms must be greater than 3000",
            ),
            "timeout_high": (
                [*base, "--role", "developer", "--name", "dev", "--task", "t",
                 "--ownership", "src/**", "--start-timeout-ms", "300001"],
                "at most 300000",
            ),
            "wait_needs_timeout": (
                [*base, "--role", "developer", "--name", "dev", "--task", "t",
                 "--ownership", "src/**", "--wait", "--timeout-ms", "0"],
                "--timeout-ms must be greater than zero",
            ),
            "missing_ownership": (
                [*base, "--role", "developer", "--name", "dev", "--task", "t"],
                "editing workers require at least one --ownership",
            ),
            "custom_needs_responsibility": (
                [*base, "--role", "custom", "--name", "rel", "--task", "t",
                 "--ownership", "deploy/**"],
                "--responsibility is required for custom roles",
            ),
            "responsibility_needs_custom": (
                [*base, "--role", "developer", "--name", "dev", "--task", "t",
                 "--ownership", "src/**", "--responsibility", "extra"],
                "--responsibility is only valid with --role custom",
            ),
        }
        for label, (argv, fragment) in cases.items():
            with self.subTest(label):
                code, _, err = self.run_main(argv)
                self.assertEqual(code, 1)
                self.assertIn(fragment, err)

    # -------------------------------------------------- representative argv

    def test_launch_herdr_argv_sequence_for_codex(self):
        code, out, err, double = self.launch(extra=["--agent-arg=--standalone"])
        self.assertEqual(code, 0, err)
        root = str(self.root_resolved)
        expected_prefix = [
            ["agent", "list"],
            ["pane", "current", "--current"],
            # split re-resolves the parent by its already-known canonical ID
            ["pane", "current", "--pane", PARENT_PANE_ID],
            ["pane", "layout", "--pane", PARENT_PANE_ID],
            ["pane", "split", "--pane", PARENT_PANE_ID, "--direction", "right",
             "--cwd", root, "--no-focus"],
            ["agent", "start", "dev", "--kind", "codex", "--pane", CHILD_PANE_ID,
             "--timeout", "30000", "--", "--standalone"],
            ["agent", "get", "dev"],
        ]
        self.assertEqual(double.calls[:7], expected_prefix)
        self.assertEqual(double.calls[7][:3], ["agent", "prompt", "dev"])
        self.assertEqual(double.calls[8], ["agent", "get", "dev"])
        # Codex launches must not carry OpenCode env injection.
        self.assertNotIn("--env", double.calls[4])

    def test_launch_stdout_json_field_set_and_phase(self):
        code, out, err, _double = self.launch(double=HerdrDouble(status="working"))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(
            set(payload),
            {"team_id", "name", "role", "kind", "pane_id", "terminal_id",
             "phase", "agent_state", "blocked", "read_only", "ownership"},
        )
        self.assertEqual(payload["phase"], "prompt_submitted")
        self.assertEqual(payload["agent_state"], "working")
        self.assertFalse(payload["blocked"])
        self.assertFalse(payload["read_only"])
        self.assertEqual(payload["ownership"], ["src/api/**"])
        self.assertEqual(payload["pane_id"], CHILD_PANE_ID)
        self.assertNotIn("pane_closed", payload)
        recorded = json.loads((self.root_resolved / ".teamcraft" / "state.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(recorded["members"]["dev"]["phase"], "prompt_submitted")
        # Current implementation flags possible delivery before submitting and
        # never clears it on success; freeze that exact behaviour.
        self.assertTrue(recorded["members"]["dev"]["prompt_may_have_been_delivered"])

    def test_kind_discovery_and_executable_preflight_contract(self):
        with mock.patch.object(teamctl, "run_process",
                              return_value=("usage\n  kinds: codex | opencode \n", "", 0)):
            self.assertEqual(teamctl.supported_kinds(), {"codex", "opencode"})
        with mock.patch.object(teamctl, "run_process",
                              return_value=("usage only", "", 0)):
            with self.assertRaisesRegex(teamctl.TeamctlError, "could not discover"):
                teamctl.supported_kinds()
        with mock.patch.object(teamctl, "supported_kinds", return_value={"codex"}), \
                mock.patch.object(teamctl.shutil, "which", return_value=None):
            with self.assertRaisesRegex(teamctl.TeamctlError,
                                       "executable is not available in PATH"):
                teamctl.preflight_kind("codex")

    # ------------------------------------------ ownership / read-only deltas

    def test_read_only_member_coexists_with_editor_and_prompt_text(self):
        teamctl.reserve_member(
            self.store, self.session,
            make_member("audit", [], read_only=True, role="tester"),
        )
        teamctl.reserve_member(  # read-only claims never block an editor
            self.store, self.session, make_member("dev", ["src/**"]),
        )
        with self.assertRaisesRegex(teamctl.TeamctlError, "overlaps"):
            teamctl.reserve_member(
                self.store, self.session, make_member("dev2", ["src/app.py"]),
            )
        prompt = teamctl.build_prompt(
            make_member("audit", [], read_only=True, role="tester")
        )
        self.assertIn("Read-only assignment. Do not modify project files.", prompt)

    def test_ceo_prompt_carries_skill_and_no_overlap_text(self):
        prompt = teamctl.build_prompt(
            make_member("ceo", [], role="ceo",
                      responsibility=teamctl.ROLE_RESPONSIBILITIES["ceo"])
        )
        self.assertIn("You are the CEO agent named ceo.", prompt)
        self.assertIn("Own coordination and final integration", prompt)
        self.assertIn(f"Read and follow the team skill at {teamctl.SKILL_PATH}",
                     prompt)
        self.assertIn("non-overlapping ownership", prompt)

    # ------------------------------------ state.json v1, locks, atomicity

    def test_state_v1_artifacts_and_version_gate(self):
        teamctl.reserve_member(self.store, self.session,
                              make_member("dev", ["src/**"]))
        runtime = self.root_resolved / ".teamcraft"
        state_path = runtime / "state.json"
        mode = runtime.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)
        self.assertTrue((runtime / "state.lock").exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(state),
            {"version", "team_id", "project_root", "herdr_session",
             "created_at", "members"},
        )
        self.assertEqual(state["version"], 1)
        self.assertEqual(
            [p.name for p in runtime.iterdir() if p.name.endswith(".tmp")],
            [],  # atomic replace must not leave temp files behind
        )
        foreign = dict(state, version=2)
        state_path.write_text(json.dumps(foreign), encoding="utf-8")
        snapshot = state_path.read_bytes()
        with self.assertRaisesRegex(teamctl.TeamctlError,
                                   "unsupported or malformed state file"):
            teamctl.reserve_member(self.store, self.session,
                                 make_member("late", ["docs/**"]))
        self.assertEqual(state_path.read_bytes(), snapshot)  # refusal is read-only

    def test_json_write_path_ignores_external_cutover_marker(self):
        """Read-only threat fixture (M0 review 8.3 / 11.1; M1-07 guard gap).

        A post-cutover environment is *simulated externally* inside this
        temp project only: a ``cutover.json`` marker declaring DB authority
        with a generation, plus the imported snapshot it references by
        SHA-256.  The current JSON write path (``update_member`` /
        ``reserve_member``, each taking the normal ``state.lock``) is then
        exercised after that marker exists.

        Frozen current behaviour: the JSON writer never reads the marker or
        generation, so JSON keeps being rewritten post-cutover.  This pins
        the detection gap M1-07 must close with an explicit guard.  It does
        NOT prove a stale pre-cutover process was ever running (the
        2026-10-23 user confirmation says none is), and it implements no
        fence itself.
        """
        teamctl.reserve_member(self.store, self.session,
                             make_member("alpha", ["src/a/**"]))
        runtime = self.root_resolved / ".teamcraft"
        state_path = runtime / "state.json"
        snapshot = state_path.read_bytes()
        snapshot_path = runtime / "imported-snapshot.json"
        marker_path = runtime / "cutover.json"
        snapshot_path.write_bytes(snapshot)
        marker_path.write_text(json.dumps({
            "state": "DB_AUTHORITATIVE",
            "cutover_generation": "gen-0001",
            "json_sha256": hashlib.sha256(snapshot).hexdigest(),
        }, sort_keys=True), encoding="utf-8")
        marker_before = marker_path.read_bytes()

        # Real JSON write path, normal locking, after the simulated cutover:
        teamctl.update_member(self.store, self.session, "alpha",
                             phase="released", ownership=[])
        teamctl.reserve_member(self.store, self.session,
                             make_member("post_cutover", ["src/z/**"]))

        after = json.loads(state_path.read_bytes().decode("utf-8"))
        self.assertNotEqual(state_path.read_bytes(), snapshot)  # still writable
        self.assertEqual(after["members"]["alpha"]["phase"], "released")
        self.assertIn("post_cutover", after["members"])
        # Nothing consulted the marker: marker/snapshot untouched, and no
        # guard metadata (generation, authority flag) entered v1 state.
        self.assertEqual(marker_path.read_bytes(), marker_before)
        self.assertEqual(snapshot_path.read_bytes(), snapshot)
        self.assertEqual(
            set(after),
            {"version", "team_id", "project_root", "herdr_session",
             "created_at", "members"},
        )
        self.assertNotIn("cutover_generation", after)

    def test_status_without_state_shape(self):
        code, out, err = self.run_main(["status", "--cwd", str(self.root_resolved)])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"project_root", "team", "members"})
        self.assertIsNone(payload["team"])
        self.assertEqual(payload["members"], [])

    def test_legacy_state_fails_closed_before_any_herdr_call_or_new_write(self):
        legacy_runtime = self.root_resolved / ".herdr-team"
        legacy_runtime.mkdir()
        legacy_state = legacy_runtime / "state.json"
        legacy_bytes = b'{"legacy": true}\n'
        legacy_state.write_bytes(legacy_bytes)
        commands = (
            ["status", "--cwd", str(self.root_resolved)],
            ["launch", "--role", "developer", "--kind", "codex", "--name",
             "dev", "--task", "t", "--ownership", "src/**", "--cwd",
             str(self.root_resolved)],
            ["resume", "dev", "--cwd", str(self.root_resolved)],
            ["complete", "dev", "--cwd", str(self.root_resolved)],
            ["release", "dev", "--cwd", str(self.root_resolved)],
        )
        with mock.patch.object(teamctl, "run_herdr") as herdr, \
                mock.patch.object(teamctl, "preflight_kind"):
            for argv in commands:
                with self.subTest(command=argv[0]):
                    code, out, err = self.run_main(argv)
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("legacy team state detected", err)
                    self.assertIn("no files were changed", err)
        herdr.assert_not_called()
        self.assertEqual(legacy_state.read_bytes(), legacy_bytes)
        self.assertFalse((self.root_resolved / ".teamcraft").exists())

    def test_runtime_symlinks_fail_closed(self):
        outside = self.root_resolved / "outside"
        outside.mkdir()
        for name, message in (
            (".herdr-team", "legacy team state detected"),
            (".teamcraft", "runtime directory must not be a symbolic link"),
        ):
            with self.subTest(name=name):
                link = self.root_resolved / name
                link.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(teamctl.TeamctlError, message):
                    teamctl.StateStore(self.root_resolved)
                link.unlink()
        self.assertEqual(list(outside.iterdir()), [])

    # --------------------------------------------------- session identity

    def test_foreign_session_status_is_unknown_and_mutation_refused(self):
        teamctl.reserve_member(self.store, self.session,
                             make_member("dev", ["src/**"]))
        snapshot = (self.root_resolved / ".teamcraft" / "state.json").read_bytes()
        other_socket = self.root / "other-herdr.sock"
        other_socket.touch()
        double = HerdrDouble()
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": str(other_socket)}), \
                mock.patch.object(teamctl, "run_herdr", side_effect=double):
            code, out, err = self.run_main(
                ["status", "--cwd", str(self.root_resolved)])
            other_session = teamctl.current_session_identity()
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["session_match"])
        member = payload["members"][0]
        self.assertEqual(member["agent_state"], "unknown")
        self.assertIn("different Herdr session", member["inspection_error"])
        self.assertEqual(double.calls, [])  # foreign state must not probe Herdr
        with self.assertRaisesRegex(teamctl.TeamctlError,
                                   "different Herdr server session"):
            teamctl.reserve_member(self.store, other_session,
                                 make_member("invader", ["docs/**"]))
        self.assertEqual(
            (self.root_resolved / ".teamcraft" / "state.json").read_bytes(),
            snapshot,
        )

    def test_session_identity_missing_socket_has_null_ids(self):
        with mock.patch.dict(
            os.environ,
            {"HERDR_ENV": "1",
             "HERDR_SOCKET_PATH": str(self.root / "absent.sock")},
        ):
            identity = teamctl.current_session_identity()
        self.assertEqual(
            set(identity), {"socket_path", "socket_device", "socket_inode"},
        )
        self.assertIsNone(identity["socket_device"])
        self.assertIsNone(identity["socket_inode"])

    # ------------------------------------------------ OpenCode pane config

    def test_invalid_opencode_config_content_exit_1(self):
        profile_dir = self.root_resolved / ".opencode" / "agents"
        profile_dir.mkdir(parents=True)
        (profile_dir / "herdr-developer.md").write_text("profile", encoding="utf-8")
        cases = {
            "not_json": ("{oops", "OPENCODE_CONFIG_CONTENT is not valid JSON"),
            "not_object": ('"just a string"',
                          "OPENCODE_CONFIG_CONTENT must contain a JSON object"),
        }
        for label, (content, fragment) in cases.items():
            with self.subTest(label), \
                    mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": content}):
                code, _, err, double = self.launch(kind="opencode")
            self.assertEqual(code, 1)
            self.assertIn(fragment, err)
            self.assertEqual([c[:2] for c in double.calls],
                             [["agent", "list"], ["pane", "current"]])
            # fail closed before any pane split or agent lifecycle mutation
            self.assertFalse(any(c[:2] == ["pane", "split"] for c in double.calls))

    def test_opencode_override_keeps_cjk_and_unknown_keys(self):
        profile_dir = self.root_resolved / ".opencode" / "agents"
        profile_dir.mkdir(parents=True)
        (profile_dir / "herdr-developer.md").write_text("profile", encoding="utf-8")
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT":
                                         json.dumps({"model": "x/y",
                                                   "instructions": "保留 中文"},
                                                  ensure_ascii=False)}):
            code, _out, err, double = self.launch(kind="opencode")
        self.assertEqual(code, 0, err)
        split = next(call for call in double.calls if call[:2] == ["pane", "split"])
        env_value = split[split.index("--env") + 1]
        self.assertTrue(env_value.startswith("OPENCODE_CONFIG_CONTENT="))
        self.assertIn('"default_agent":"herdr-developer"', env_value)
        self.assertIn('"model":"x/y"', env_value)
        self.assertIn("保留 中文", env_value)  # ensure_ascii=False stays readable

    # ------------------------------------------- conservative resume/finish

    def test_resume_prompt_send_and_skip_contract(self):
        for action, expected_phase, prompts in (
            ("send", "prompt_submitted", True),
            ("skip", "prompt_skipped", False),
        ):
            with self.subTest(action):
                self.temp_reset()
                teamctl.reserve_member(
                    self.store, self.session,
                    make_member("dev", ["src/**"], pane_id=CHILD_PANE_ID,
                              terminal_id="term-2", agent_session=AGENT_SESSION,
                              phase="prompt_ambiguous",
                              prompt_may_have_been_delivered=True),
                )
                double = HerdrDouble()
                with mock.patch.object(teamctl, "preflight_kind"), \
                        mock.patch.object(teamctl, "run_herdr", side_effect=double):
                    code, out, err = self.run_main(
                        ["resume", "dev", "--cwd", str(self.root_resolved),
                         f"--prompt-action={action}"])
                self.assertEqual(code, 0, err)
                payload = json.loads(out)
                self.assertEqual(payload["prompt_action"], action)
                self.assertEqual(payload["phase"], expected_phase)
                called = [call[:2] for call in double.calls]
                self.assertEqual(["agent", "prompt"] in called, prompts)
                self.assertFalse(any(call[:2] == ["pane", "split"] for call in
                                   double.calls))
                recorded = json.loads(
                    (self.root_resolved / ".teamcraft" / "state.json")
                    .read_text(encoding="utf-8"))
                self.assertEqual(recorded["members"]["dev"]["phase"],
                               expected_phase)

    def test_resume_terminal_member_refused_exit_1(self):
        teamctl.reserve_member(self.store, self.session,
                             make_member("dev", ["src/**"], phase="completed"))
        code, _, err = self.run_main(
            ["resume", "dev", "--cwd", str(self.root_resolved)])
        self.assertEqual(code, 1)
        self.assertIn("already completed", err)

    def test_complete_and_release_never_close_pane_or_stop_agent(self):
        forbidden = {("pane", "close"), ("pane", "kill"), ("agent", "stop"),
                     ("agent", "cancel")}
        for command, expected_phase in (("complete", "completed"),
                                      ("release", "released")):
            with self.subTest(command):
                self.temp_reset()
                teamctl.reserve_member(
                    self.store, self.session,
                    make_member("dev", ["src/**"], pane_id=CHILD_PANE_ID,
                              terminal_id="term-2", agent_session=AGENT_SESSION,
                              phase="agent_started", live_state="idle"),
                )
                double = HerdrDouble()
                with mock.patch.object(teamctl, "run_herdr", side_effect=double):
                    code, out, err = self.run_main(
                        [command, "dev", "--cwd", str(self.root_resolved)])
                self.assertEqual(code, 0, err)
                payload = json.loads(out)
                self.assertIs(payload["pane_closed"], False)
                self.assertFalse(forbidden & {tuple(call[:2]) for call in
                                            double.calls})
                recorded = json.loads(
                    (self.root_resolved / ".teamcraft" / "state.json")
                    .read_text(encoding="utf-8"))
                member = recorded["members"]["dev"]
                self.assertEqual(member["phase"], expected_phase)
                self.assertEqual(member["ownership"], [])

    def temp_reset(self):
        """Remove state between subtests without recreating the temp dir."""
        state_path = self.root_resolved / ".teamcraft" / "state.json"
        if state_path.exists():
            state_path.unlink()


if __name__ == "__main__":
    unittest.main()
