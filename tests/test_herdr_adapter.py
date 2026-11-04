import unittest
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from teamcraft.herdr_adapter import HerdrAdapter, HerdrError


class FakeHerdr:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, timeout, accepted):
        self.calls.append((list(argv), timeout, accepted))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class HerdrAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (Path(__file__).resolve().parents[1]
                  / ".agents/skills/teamcraft/scripts/teamctl.py")
        spec = importlib.util.spec_from_file_location("teamctl_m201_test", source)
        cls.teamctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.teamctl)

    def test_request_normalizes_shape_and_fake_runs_offline(self):
        fake = FakeHerdr([('{"result":{"agents":[{"name":"a"},null]}}', '', 0)])
        adapter = HerdrAdapter(fake)
        self.assertEqual(adapter.list_agents(), [{"name": "a"}])
        self.assertEqual(fake.calls[0], (["herdr", "agent", "list"], 15, (0,)))

    def test_missing_fields_have_stable_code(self):
        with self.assertRaises(HerdrError) as caught:
            HerdrAdapter().pane({"result": {}})
        self.assertEqual(caught.exception.code, "missing_field")

    def test_invalid_json_shape_and_utf8(self):
        for body, expected in [("[]", "invalid_shape"), ("not-json", "invalid_json")]:
            with self.subTest(body=body), self.assertRaises(HerdrError) as caught:
                HerdrAdapter(FakeHerdr([(body, "", 0)])).request(["agent", "get", "a"])
            self.assertEqual(caught.exception.code, expected)
        fake = FakeHerdr([None])
        def invalid_utf8(argv, timeout, accepted):
            raise HerdrError("invalid_utf8", "invalid bytes")
        with self.assertRaises(HerdrError) as caught:
            HerdrAdapter(invalid_utf8).request(["agent", "list"])
        self.assertEqual(caught.exception.code, "invalid_utf8")

    def test_timeout_and_command_failure_are_normalized(self):
        for error, code in [
            (HerdrError("timeout", "timed out"), "timeout"),
            (HerdrError("command_failed", "failed", returncode=7), "command_failed"),
        ]:
            with self.subTest(code=code), self.assertRaises(HerdrError) as caught:
                HerdrAdapter(FakeHerdr([error])).request(["agent", "get", "a"])
            self.assertEqual(caught.exception.code, code)

    def test_read_and_stop_contracts(self):
        fake = FakeHerdr([('{"ok":true}', '', 0), ('{"ok":true}', '', 0)])
        adapter = HerdrAdapter(fake)
        adapter.read_agent("a", "--tail", "3")
        adapter.stop_agent("a", "--force")
        self.assertEqual([call[0] for call in fake.calls], [
            ["herdr", "agent", "read", "a", "--tail", "3"],
            ["herdr", "agent", "stop", "a", "--force"],
        ])

    def test_effect_argv_and_timeouts(self):
        fake = FakeHerdr([('{"result":{"pane":{"pane_id":"p"}}}', '', 0), ('{}', '', 0), ('{}', '', 0)])
        adapter = HerdrAdapter(fake)
        adapter.split_pane("parent", "right", "/work", extra=["--env", "X=1"])
        adapter.start_agent("worker", "codex", "p", 30_000, ["--model", "m"])
        adapter.prompt_agent("worker", "hello", wait=True, timeout_ms=60_000)
        self.assertEqual([call[0] for call in fake.calls], [
            ["herdr", "pane", "split", "--pane", "parent", "--direction", "right", "--cwd", "/work", "--no-focus", "--env", "X=1"],
            ["herdr", "agent", "start", "worker", "--kind", "codex", "--pane", "p", "--timeout", "30000", "--", "--model", "m"],
            ["herdr", "agent", "prompt", "worker", "hello", "--wait", "--timeout", "60000"],
        ])
        self.assertEqual([call[1] for call in fake.calls], [15, 35, 65])

    def test_production_start_member_uses_typed_adapter_after_durable_claim(self):
        calls = []

        class FakeAdapter:
            def start_agent(self, *args, **kwargs):
                calls.append(("start", args, kwargs))
                return {}
            def get_agent(self, name):
                calls.append(("get", name))
                return {"name": name, "agent": "codex", "pane_id": "pane-1",
                        "terminal_id": "term-1", "agent_status": "working"}

        class FakeStore:
            def __init__(self, member):
                self.state = {"members": {"worker": member},
                              "herdr_session": {"server_id": "s"}}
            def locked(self):
                from contextlib import contextmanager
                @contextmanager
                def held():
                    yield self.state
                return held()
            def write_unlocked(self, state, **kwargs):
                calls.append(("write", state["members"]["worker"]["phase"], kwargs))
            def stage_claim(self, name, stage, **kwargs):
                calls.append(("claim", stage, kwargs))
                return SimpleNamespace(id="command-1")
            def stage_unknown(self, command_id, **kwargs):
                calls.append(("unknown", command_id, kwargs))

        member = {"name": "worker", "role": "developer", "kind": "codex",
                  "pane_id": "pane-1", "terminal_id": None, "agent_session": None,
                  "agent_args": ["--model", "m"], "start_timeout_ms": 30_000,
                  "phase": "pane_created", "task": "work", "responsibility": "code",
                  "ownership": ["src/**"], "read_only": False,
                  "prompt_may_have_been_delivered": False, "live_state": "missing",
                  "error": None, "cwd": "/work", "updated_at": 0}
        store = FakeStore(member)
        tc = self.teamctl
        original = tc._herdr_adapter
        tc._herdr_adapter = lambda: FakeAdapter()
        try:
            result = tc.start_member(member, store, {"server_id": "s"}, lease=object())
        finally:
            tc._herdr_adapter = original
        self.assertEqual(result["phase"], "agent_started")
        self.assertEqual([call[0] for call in calls], ["write", "claim", "start", "get", "write"])
        self.assertEqual(calls[1][1], "agent.start")
        self.assertEqual(calls[1][2]["payload"]["argv"], [
            "agent", "start", "worker", "--kind", "codex", "--pane", "pane-1",
            "--timeout", "30000", "--", "--model", "m",
        ])
        self.assertEqual(calls[2][1], ("worker", "codex", "pane-1", 30_000, ["--model", "m"]))

    def test_teamctl_adapter_errors_preserve_ambiguous_side_effect_classification(self):
        tc = self.teamctl
        adapter = tc._TeamctlHerdrAdapter(
            lambda argv, timeout, accepted: ("not-json", "", 0)
        )
        with self.assertRaises(tc.TeamctlError):
            adapter.start_agent("worker", "codex", "pane-1", 30_000)


if __name__ == "__main__":
    unittest.main()
