"""Unit tests for the Responses -> Chat Completions bridge helpers."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1]
          / "scripts" / "m606_responses_proxy.py")
SPEC = importlib.util.spec_from_file_location("m606_responses_proxy", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class ResponsesBridgeTests(unittest.TestCase):
    def test_string_input_becomes_user_message(self) -> None:
        self.assertEqual(MOD._messages({"input": "hi"}),
                         [{"role": "user", "content": "hi"}])

    def test_instructions_prepended_as_system(self) -> None:
        msgs = MOD._messages({"instructions": "be terse", "input": "hi"})
        self.assertEqual(msgs[0], {"role": "system", "content": "be terse"})

    def test_function_call_and_output_round_trip(self) -> None:
        msgs = MOD._messages({"input": [
            {"type": "function_call", "call_id": "c1", "name": "shell",
             "arguments": "{\"cmd\":\"ls\"}"},
            {"type": "function_call_output", "call_id": "c1", "output": "app.py"},
        ]})
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertEqual(msgs[0]["tool_calls"][0]["function"]["name"], "shell")
        self.assertEqual(msgs[1],
                         {"role": "tool", "tool_call_id": "c1", "content": "app.py"})

    def test_tools_are_translated(self) -> None:
        tools = MOD._tools([{"type": "function", "name": "shell",
                             "description": "run", "parameters": {"type": "object"}}])
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["name"], "shell")

    def test_chat_tool_calls_become_output_items(self) -> None:
        items = MOD._output_items({"content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "shell", "arguments": "{}"}}]})
        self.assertEqual(items[0]["type"], "function_call")
        self.assertEqual(items[0]["call_id"], "c1")


if __name__ == "__main__":
    unittest.main()
