"""Offline contract tests for scripts/m606_verify_opencode_skill.py (PG-9).

Every test here is hermetic: no OpenCode process, no network, no model call, no
credential read.  The live probe path is exercised through the injected
``runner``/``which`` seams, so timeout/no-route/non-zero/success classification is
proven without contacting any provider.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import m606_verify_opencode_skill as probe

SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "teamcraft-ceo"
SUCCESS_STDOUT = (
    f"protocol_version: {probe.PINNED_PROTOCOL_VERSION}\n"
    f"protocol_sha256: {probe.PINNED_PROTOCOL_SHA256}\n"
)
WRONG_HASH = "a" * 64


def copied_protocol(tmp: Path) -> Path:
    """Copy the real Skill + references so the manifest is genuine."""
    dest = tmp / "teamcraft-ceo"
    shutil.copytree(SKILL_DIR, dest)
    return dest / "references"


def write_drifted_protocol(tmp: Path, *, version: str = "2.0.0",
                           with_skill: bool = True) -> Path:
    """Write a self-consistent protocol artifact that is not the M6-06 pin."""
    root = tmp / "teamcraft-ceo"
    refs = root / "references"
    refs.mkdir(parents=True)
    body = f"---\nprotocol_version: {version}\n---\n\n# drifted protocol\n".encode()
    (refs / "CEO_PROTOCOL.md").write_bytes(body)
    manifest = {"protocol_version": version,
                "sha256": hashlib.sha256(body).hexdigest()}
    (refs / "protocol.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    if with_skill:
        root.mkdir(exist_ok=True)
        (root / "SKILL.md").write_text(
            "---\nname: teamcraft-ceo\n---\n", encoding="utf-8")
    return refs


class Recorder:
    """Runner seam that records calls and returns one fixed outcome."""

    def __init__(self, outcome: probe.ProbeOutcome):
        self.outcome = outcome
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": list(command), "kwargs": dict(kwargs)})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class Exploding:
    """Any accidental external call must fail the test loudly."""

    def __init__(self, name: str):
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.name} must not be called")


def which_ok(name: str) -> str | None:
    return "/usr/bin/opencode" if name == "opencode" else None


class CommandAndDryRunTests(unittest.TestCase):
    def test_command_is_one_bounded_noninteractive_run(self):
        command = probe.build_command("deepseek/deepseek-flash", "teamcraft-ceo")
        self.assertEqual(command[:2], ["opencode", "run"])
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1],
                         "deepseek/deepseek-flash")
        self.assertEqual(command[command.index("--agent") + 1],
                         "teamcraft-ceo")
        for flag in ("--format", "--archived", "--standalone", "--auto"):
            self.assertIn(flag, command)
        self.assertEqual(command[-1], probe.PROBE_PROMPT)
        # The prompt must not contain the answer; otherwise a pass proves nothing.
        self.assertNotIn(probe.PINNED_PROTOCOL_VERSION, probe.PROBE_PROMPT)
        self.assertNotIn(probe.PINNED_PROTOCOL_SHA256, probe.PROBE_PROMPT)

    def test_default_dry_run_makes_no_external_call(self):
        result = probe.run_verification(
            live=False,
            runner=Exploding("runner"),
            which=Exploding("which"),
        )
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["mode"], "dry-run")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["script_exit_code"], 0)
        self.assertIn("dry-run", result["reason"])
        self.assertEqual(result["command"][0:2], ["opencode", "run"])
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["model"], "deepseek/deepseek-flash")
        self.assertTrue(result["rules"])
        self.assertTrue(result["protocol"]["pinned_match"])

    def test_default_protocol_matches_actual_repository_manifest(self):
        manifest = json.loads(
            (SKILL_DIR / "references" / "protocol.manifest.json").read_text(
                encoding="utf-8"))
        loaded = probe.load_protocol()
        self.assertEqual(loaded["version"], manifest["protocol_version"])
        self.assertEqual(loaded["sha256"], manifest["sha256"])
        self.assertEqual(loaded["version"], probe.PINNED_PROTOCOL_VERSION)
        self.assertEqual(loaded["sha256"], probe.PINNED_PROTOCOL_SHA256)
        self.assertTrue(Path(loaded["manifest"]).is_file())
        self.assertTrue(Path(loaded["protocol"]).is_file())
        self.assertTrue(Path(loaded["skill"]).is_file())

    def test_cli_dry_run_json_has_contract_fields(self):
        out = io.StringIO()
        code = probe.main(
            ["--json"], runner=Exploding("runner"),
            which=Exploding("which"), stdout=out)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        for key in ("status", "command", "exit_code", "reason"):
            self.assertIn(key, payload)
        self.assertEqual(payload["status"], "pending")
        self.assertTrue(payload["dry_run"])

    def test_cli_text_dry_run_prints_command_and_rules(self):
        out = io.StringIO()
        code = probe.main(
            [], runner=Exploding("runner"),
            which=Exploding("which"), stdout=out)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("status: pending", text)
        self.assertIn("command: opencode run", text)
        self.assertIn("rules:", text)
        self.assertIn("deepseek/deepseek-flash", text)


class LiveProbeTests(unittest.TestCase):
    def run_live(self, outcome, *, which=which_ok):
        recorder = Recorder(outcome)
        result = probe.run_verification(
            live=True, runner=recorder, which=which)
        return result, recorder

    def test_missing_opencode_executable_is_pending(self):
        result, recorder = self.run_live(
            probe.ProbeOutcome(0, SUCCESS_STDOUT), which=lambda name: None)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["script_exit_code"], 2)
        self.assertIsNone(result["exit_code"])
        self.assertIn("not found", result["reason"])
        self.assertEqual(recorder.calls, [])

    def test_provider_no_route_is_pending_and_preserves_error(self):
        stderr = "Error: provider.no-route: Model unavailable: omlx/Qwen3.8"
        result, recorder = self.run_live(probe.ProbeOutcome(1, "", stderr))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["script_exit_code"], 2)
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("provider.no-route", result["reason"])
        self.assertEqual(result["stderr"], stderr)
        self.assertIn("provider.no-route", result["reason"])
        self.assertEqual(len(recorder.calls), 1)
        self.assertNotIn("env", recorder.calls[0]["kwargs"])

    def test_provider_no_route_with_exit_zero_is_still_pending(self):
        result, _ = self.run_live(
            probe.ProbeOutcome(0, "", "provider.no-route: Model unavailable"))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("provider.no-route", result["reason"])

    def test_nonzero_exit_is_pending_and_not_pass(self):
        result, _ = self.run_live(
            probe.ProbeOutcome(3, SUCCESS_STDOUT, "route down"))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["script_exit_code"], 2)
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("code 3", result["reason"])
        self.assertEqual(result["stderr"], "route down")

    def test_timeout_is_pending(self):
        result, _ = self.run_live(
            probe.ProbeOutcome(None, "", "", timed_out=True))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["script_exit_code"], 2)
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["timed_out"])
        self.assertIn("timed out", result["reason"])

    def test_success_output_parses_version_and_hash_as_pass(self):
        result, recorder = self.run_live(
            probe.ProbeOutcome(0, SUCCESS_STDOUT, ""))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["script_exit_code"], 0)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["observed"]["version"],
                         probe.PINNED_PROTOCOL_VERSION)
        self.assertEqual(result["observed"]["sha256"],
                         probe.PINNED_PROTOCOL_SHA256)
        self.assertIn(probe.PINNED_PROTOCOL_SHA256, result["reason"])
        self.assertEqual(len(recorder.calls), 1)

    def test_json_shaped_success_output_is_pass(self):
        stdout = json.dumps({
            "protocol_version": probe.PINNED_PROTOCOL_VERSION,
            "sha256": probe.PINNED_PROTOCOL_SHA256,
        })
        result, _ = self.run_live(probe.ProbeOutcome(0, stdout, ""))
        self.assertEqual(result["status"], "pass")

    def test_exit_zero_without_hash_is_fail(self):
        result, _ = self.run_live(
            probe.ProbeOutcome(0, "protocol_version: 1.0.0\n", ""))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["script_exit_code"], 1)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("SHA-256", result["reason"])

    def test_exit_zero_without_version_is_fail(self):
        stdout = f"protocol_sha256: {probe.PINNED_PROTOCOL_SHA256}\n"
        result, _ = self.run_live(probe.ProbeOutcome(0, stdout, ""))
        self.assertEqual(result["status"], "fail")
        self.assertIn("version", result["reason"])

    def test_exit_zero_with_wrong_hash_is_fail(self):
        stdout = (f"protocol_version: {probe.PINNED_PROTOCOL_VERSION}\n"
                  f"protocol_sha256: {WRONG_HASH}\n")
        result, _ = self.run_live(probe.ProbeOutcome(0, stdout, ""))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["observed"]["sha256"], WRONG_HASH)
        self.assertIn("SHA-256", result["reason"])

    def test_runner_exception_is_pending(self):
        result, _ = self.run_live(RuntimeError("boom"))
        self.assertEqual(result["status"], "pending")
        self.assertIn("runner failed", result["reason"])

    def test_cli_live_pending_returns_two(self):
        out = io.StringIO()
        code = probe.main(
            ["--live", "--json"], runner=Recorder(probe.ProbeOutcome(1, "", "x")),
            which=which_ok, stdout=out)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "pending")


class ProtocolArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inconsistent_manifest_is_fail_without_running_probe(self):
        refs = copied_protocol(self.tmp)
        manifest_path = refs / "protocol.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["sha256"] = WRONG_HASH
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        recorder = Recorder(probe.ProbeOutcome(0, SUCCESS_STDOUT))
        result = probe.run_verification(
            live=True, protocol_dir=refs, runner=recorder, which=which_ok)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["script_exit_code"], 1)
        self.assertIn("protocol artifact invalid", result["reason"])
        self.assertEqual(recorder.calls, [])

    def test_pin_drift_is_fail_even_when_manifest_is_self_consistent(self):
        refs = write_drifted_protocol(self.tmp, version="2.0.0")
        recorder = Recorder(probe.ProbeOutcome(0, SUCCESS_STDOUT))
        result = probe.run_verification(
            live=True, protocol_dir=refs, runner=recorder, which=which_ok)
        self.assertEqual(result["status"], "fail")
        self.assertIn("does not match M6-06 pin", result["reason"])
        self.assertEqual(recorder.calls, [])

    def test_missing_skill_file_is_fail(self):
        refs = write_drifted_protocol(self.tmp, with_skill=False)
        recorder = Recorder(probe.ProbeOutcome(0, SUCCESS_STDOUT))
        result = probe.run_verification(
            live=True, protocol_dir=refs, runner=recorder, which=which_ok)
        self.assertEqual(result["status"], "fail")
        self.assertIn("CEO Skill file missing", result["reason"])
        self.assertEqual(recorder.calls, [])

    def test_invalid_model_is_fail(self):
        result = probe.run_verification(
            live=True, model="no-slash-model",
            runner=Exploding("runner"), which=Exploding("which"))
        self.assertEqual(result["status"], "fail")
        self.assertIn("provider/model", result["reason"])


class CredentialSafetyTests(unittest.TestCase):
    def test_source_has_no_credential_store_or_key_variable_access(self):
        source = Path(probe.__file__).read_text(encoding="utf-8")
        forbidden = (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "OPENAI_BASE_URL",
            "auth.json",
            "auth export",
            "api_key",
            "sk-",
        )
        for token in forbidden:
            self.assertNotIn(token, source, f"source references {token!r}")

    def test_secret_environment_is_not_read_or_emitted(self):
        sentinel = "sk-offline-sentinel-do-not-leak"
        recorder = Recorder(probe.ProbeOutcome(0, SUCCESS_STDOUT))
        with mock.patch.dict(os.environ, {"OPENCODE_TEST_SECRET": sentinel}):
            result = probe.run_verification(
                live=True, runner=recorder, which=which_ok)
        payload = json.dumps(result, sort_keys=True)
        self.assertNotIn(sentinel, payload)
        self.assertNotIn(sentinel, " ".join(result["command"]))
        self.assertEqual(result["status"], "pass")

    def test_runner_receives_only_timeout_and_cwd(self):
        recorder = Recorder(probe.ProbeOutcome(0, SUCCESS_STDOUT))
        probe.run_verification(live=True, runner=recorder, which=which_ok)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(set(recorder.calls[0]["kwargs"]), {"timeout", "cwd"})
        self.assertNotIn("env", recorder.calls[0]["kwargs"])


if __name__ == "__main__":
    unittest.main()
