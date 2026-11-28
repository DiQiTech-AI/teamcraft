"""Contract tests for the M5-06 local pilot harness (no real model calls)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "m506_pilot.py")
SPEC = importlib.util.spec_from_file_location("m506_pilot", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)

REFERENCE_SOLUTIONS = {
    "merge_intervals": (
        "def merge_intervals(intervals):\n"
        "    out = []\n"
        "    for start, end in sorted(intervals):\n"
        "        if out and start <= out[-1][1]:\n"
        "            if end > out[-1][1]:\n"
        "                out[-1][1] = end\n"
        "        else:\n"
        "            out.append([start, end])\n"
        "    return out\n"
    ),
    "parse_csv_line": (
        "def parse_csv_line(line):\n"
        "    if line == '':\n"
        "        return []\n"
        "    fields = []\n"
        "    i = 0\n"
        "    n = len(line)\n"
        "    while True:\n"
        "        if i < n and line[i] == chr(34):\n"
        "            i += 1\n"
        "            chars = []\n"
        "            closed = False\n"
        "            while i < n:\n"
        "                ch = line[i]\n"
        "                if ch == chr(34):\n"
        "                    if i + 1 < n and line[i + 1] == chr(34):\n"
        "                        chars.append(chr(34))\n"
        "                        i += 2\n"
        "                        continue\n"
        "                    i += 1\n"
        "                    closed = True\n"
        "                    break\n"
        "                chars.append(ch)\n"
        "                i += 1\n"
        "            if not closed:\n"
        "                raise ValueError('unterminated quoted field')\n"
        "            if i < n and line[i] != ',':\n"
        "                raise ValueError('unexpected character after closing quote')\n"
        "            fields.append(''.join(chars))\n"
        "        else:\n"
        "            start = i\n"
        "            while i < n and line[i] != ',':\n"
        "                i += 1\n"
        "            fields.append(line[start:i])\n"
        "        if i >= n:\n"
        "            break\n"
        "        i += 1\n"
        "        if i == n:\n"
        "            fields.append('')\n"
        "            break\n"
        "    return fields\n"
    ),
    "slugify": (
        "import re\n"
        "def slugify(text):\n"
        "    return re.sub(r'[^a-z0-9]+', '-', text.casefold()).strip('-')\n"
    ),
    "retry_delays": (
        "def retry_delays(attempts, base, factor, max_delay):\n"
        "    if attempts <= 0:\n"
        "        return []\n"
        "    return [min(base * (factor ** i), max_delay) for i in range(attempts)]\n"
    ),
    "aggregate_windows": (
        "def aggregate_windows(events, window_seconds):\n"
        "    if window_seconds <= 0:\n"
        "        raise ValueError('window_seconds must be positive')\n"
        "    totals = {}\n"
        "    for timestamp, value in events:\n"
        "        start = timestamp - (timestamp % window_seconds)\n"
        "        totals[start] = totals.get(start, 0.0) + value\n"
        "    return totals\n"
    ),
    "top_k_frequent": (
        "def top_k_frequent(values, k):\n"
        "    if k <= 0 or not values:\n"
        "        return []\n"
        "    counts = {}\n"
        "    first = {}\n"
        "    for index, value in enumerate(values):\n"
        "        counts[value] = counts.get(value, 0) + 1\n"
        "        if value not in first:\n"
        "            first[value] = index\n"
        "    ordered = sorted(counts, key=lambda value: (-counts[value], first[value]))\n"
        "    return ordered[:k]\n"
    ),
    "validate_api_key_shape": (
        "def validate_api_key_shape(value):\n"
        "    if not isinstance(value, str):\n"
        "        return False\n"
        "    if len(value) != 36 or not value.startswith('tck_'):\n"
        "        return False\n"
        "    return all(ch.isascii() and (ch.islower() or ch.isdigit())\n"
        "               for ch in value[4:])\n"
    ),
    "mask_secret": (
        "def mask_secret(value, visible=4):\n"
        "    length = len(value)\n"
        "    if visible <= 0 or length <= visible:\n"
        "        return '*' * length\n"
        "    return '*' * (length - visible) + value[-visible:]\n"
    ),
}

# Wrong aggregate implementations kept separate so sorting-mutation and
# negative-window failures are never conflated in a single test case.
AGGREGATE_SORT_ONLY = (
    "def aggregate_windows(events, window_seconds):\n"
    "    if window_seconds <= 0:\n"
    "        raise ValueError('window_seconds must be positive')\n"
    "    events.sort()\n"
    "    totals = {}\n"
    "    for timestamp, value in events:\n"
    "        start = timestamp - (timestamp % window_seconds)\n"
    "        totals[start] = totals.get(start, 0.0) + value\n"
    "    return totals\n"
)
AGGREGATE_NEGATIVE_PATH_MUTATION = (
    "def aggregate_windows(events, window_seconds):\n"
    "    if window_seconds <= 0:\n"
    "        events.sort()\n"
    "        raise ValueError('window_seconds must be positive')\n"
    "    totals = {}\n"
    "    for timestamp, value in events:\n"
    "        start = timestamp - (timestamp % window_seconds)\n"
    "        totals[start] = totals.get(start, 0.0) + value\n"
    "    return totals\n"
)
AGGREGATE_NO_NEGATIVE_ERROR = REFERENCE_SOLUTIONS["aggregate_windows"].replace(
    "if window_seconds <= 0:", "if window_seconds == 0:"
)

# Each entry is a plausible implementation that passes the coarse cases but
# violates one specific boundary the task prompt now states.
WRONG_SOLUTIONS = {
    # Boundary: must return a list of two-element lists, not a tuple.
    "merge_intervals": REFERENCE_SOLUTIONS["merge_intervals"]
    + "\n_orig = merge_intervals\n"
    "merge_intervals = lambda intervals: tuple(_orig(intervals))\n",
    # Boundary: quoting applies to the second and later fields too.
    "parse_csv_line": REFERENCE_SOLUTIONS["parse_csv_line"].replace(
        "if i < n and line[i] == chr(34):",
        "if i == 0 and line[i] == chr(34):",
    ),
    # Boundary: str.casefold(), so sharp s expands to ss.
    "slugify": REFERENCE_SOLUTIONS["slugify"].replace(".casefold()", ".lower()"),
    # Boundary: do not round the delay values.
    "retry_delays": REFERENCE_SOLUTIONS["retry_delays"].replace(
        "return [min(base * (factor ** i), max_delay) for i in range(attempts)]",
        "return [round(min(base * (factor ** i), max_delay), 2) for i in range(attempts)]",
    ),
    # Boundary: an implementation that sorts the caller's unsorted list before
    # aggregating changes observable input even though its result is correct.
    "aggregate_windows": AGGREGATE_SORT_ONLY,
    # Boundary: equal counts keep first-appearance order (no re-sorting).
    "top_k_frequent": (
        "from collections import Counter\n"
        "def top_k_frequent(values, k):\n"
        "    if k <= 0 or not values:\n"
        "        return []\n"
        "    counts = Counter(values)\n"
        "    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))\n"
        "    return [value for value, _ in ordered[:k]]\n"
    ),
    # Boundary: non-ASCII characters are rejected even when islower() is True.
    "validate_api_key_shape": REFERENCE_SOLUTIONS["validate_api_key_shape"].replace(
        "ch.isascii() and (ch.islower() or ch.isdigit())",
        "(ch.islower() or ch.isdigit())",
    ),
    # Boundary: visible <= 0, including zero, masks every character.
    "mask_secret": REFERENCE_SOLUTIONS["mask_secret"].replace(
        "if visible <= 0", "if visible < 0"
    ),
}

# Additional isolated wrong solutions per task. aggregate_windows keeps these
# separate from the sort-on-success case so each failure mode is proven alone.
EXTRA_WRONG_SOLUTIONS = {
    "aggregate_windows": [AGGREGATE_NEGATIVE_PATH_MUTATION, AGGREGATE_NO_NEGATIVE_ERROR],
}


def _capture_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = MOD.main(argv)
    return code, out.getvalue(), err.getvalue()


def _record(
    arm,
    task_id,
    *,
    executed=True,
    success=True,
    model="model-a",
    model_seconds=1.0,
    verifier_seconds=0.1,
    prompt_tokens=10,
    completion_tokens=5,
    reasoning_tokens=None,
    finish_reason="stop",
    usage_timings=None,
    content_empty=False,
    reason_code="selected_local_first",
    effective_risk="normal",
    error=None,
):
    return {
        "arm": arm,
        "task_id": task_id,
        "risk": effective_risk,
        "tags": [],
        "executed": executed,
        "success": success,
        "model": model,
        "model_seconds": model_seconds,
        "verifier_seconds": verifier_seconds,
        "total_seconds": None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "finish_reason": finish_reason,
        "usage_timings": usage_timings,
        "reason_code": reason_code,
        "effective_risk": effective_risk,
        "decision_digest": "digest",
        "content_empty": content_empty,
        "error": error,
    }


def _probe_task(verifier, *, prompt="p", task_id="probe"):
    return MOD.Task(
        id=task_id,
        title=task_id,
        prompt=prompt,
        verifier_python=verifier,
        risk="normal",
        tags=(),
    )


def _fenced(code):
    return "```python\n" + code.rstrip() + "\n```\n"


def _write_replay_source(root, entries):
    """Write a synthetic results.json + contents/ tree.

    ``entries`` items are ``(arm, task_id, text, old_success, error)``.
    """

    root = Path(root)
    contents = root / "contents"
    rows = []
    for arm, task_id, text, old_success, error in entries:
        data = text.encode("utf-8")
        path = contents / arm / (task_id + ".txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append(
            {
                "arm": arm,
                "task_id": task_id,
                "risk": "normal",
                "tags": ["time"],
                "executed": True,
                "success": old_success,
                "model": "model-a",
                "model_seconds": 1.0,
                "verifier_seconds": 0.1,
                "total_seconds": 1.1,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "reasoning_tokens": 0,
                "finish_reason": "stop",
                "usage_timings": {"generation_duration": 0.5},
                "reason_code": "selected_local_first",
                "effective_risk": "normal",
                "prompt_sha256": "0" * 64,
                "content_sha256": MOD.sha256_bytes(data),
                "content_bytes": len(data),
                "content_truncated": False,
                "content_empty": not text.strip(),
                "sentinel_ok": None,
                "verifier_returncode": None,
                "error": error,
            }
        )
    document = {
        "schema_version": 2,
        "generated_at": "2026-11-28T11:59:46+00:00",
        "harness_sha256": "a" * 64,
        "tasks_sha256": "b" * 64,
        "max_tokens": 8192,
        "warmups": [],
        "results": rows,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "results.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return document


class TaskSetContractTests(unittest.TestCase):
    def setUp(self):
        self.tasks = MOD.load_tasks()

    def test_schema_ids_and_risk_mix(self):
        self.assertEqual(len(self.tasks), 8)
        ids = [task.id for task in self.tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for task in self.tasks:
            self.assertRegex(task.id, r"^[a-z0-9_-]{1,32}$")
            self.assertTrue(task.title)
            self.assertIn("Output only", task.prompt)
            self.assertIn("if __name__", task.verifier_python)
            self.assertIn(task.risk, {"normal", "high"})
            self.assertIsInstance(task.tags, tuple)

        high = [task for task in self.tasks if task.risk == "high"]
        self.assertGreaterEqual(len(high), 2)
        for task in high:
            self.assertTrue(
                {"credential", "key"} & set(task.tags),
                f"{task.id} lacks a credential/key tag",
            )

        tagged_normal = [
            task for task in self.tasks
            if task.risk == "normal" and task.tags
        ]
        self.assertGreaterEqual(len(tagged_normal), 2)

    def test_prompts_disclose_verifier_boundaries(self):
        required = {
            "merge_intervals": ["not a tuple", "[start, end]"],
            "parse_csv_line": ["every field", "unterminated"],
            "slugify": ["casefold"],
            "retry_delays": ["round"],
            "aggregate_windows": ["window_seconds <= 0", "mutate"],
            "top_k_frequent": ["first appeared"],
            "validate_api_key_shape": ["non-ASCII"],
            "mask_secret": ["visible <= 0", "empty string"],
        }
        prompts = {task.id: task.prompt for task in self.tasks}
        for task_id, fragments in required.items():
            for fragment in fragments:
                with self.subTest(task=task_id, fragment=fragment):
                    self.assertIn(fragment, prompts[task_id])

    def test_verifier_source_appends_the_sentinel_last(self):
        for task in self.tasks:
            with self.subTest(task=task.id):
                source = MOD.verifier_source(task)
                self.assertIn(MOD.SENTINEL_ENV, source)
                self.assertTrue(source.rstrip().endswith("encode('utf-8'))"))
                # The sentinel must come after the task's own main() call.
                self.assertGreater(
                    source.index(MOD.SENTINEL_ENV),
                    source.index("if __name__"),
                )

    def test_every_verifier_accepts_its_reference_solution(self):
        self.assertEqual(set(REFERENCE_SOLUTIONS), {task.id for task in self.tasks})
        for task in self.tasks:
            with self.subTest(task=task.id):
                outcome = MOD.run_verifier(
                    task, REFERENCE_SOLUTIONS[task.id], timeout_seconds=20
                )
                self.assertTrue(outcome.success, outcome.error)
                self.assertTrue(outcome.sentinel_ok)

    def test_every_verifier_rejects_a_failing_solution(self):
        for task in self.tasks:
            with self.subTest(task=task.id):
                outcome = MOD.run_verifier(
                    task, "raise RuntimeError('wrong answer')", timeout_seconds=20
                )
                self.assertFalse(outcome.success)
                self.assertIsNotNone(outcome.error)
                self.assertTrue(outcome.error.startswith("verifier_failed"))

    def test_every_boundary_wrong_solution_is_rejected(self):
        self.assertEqual(set(WRONG_SOLUTIONS), {task.id for task in self.tasks})
        for task in self.tasks:
            with self.subTest(task=task.id):
                outcome = MOD.run_verifier(
                    task, WRONG_SOLUTIONS[task.id], timeout_seconds=20
                )
                self.assertFalse(
                    outcome.success,
                    f"{task.id} accepted its boundary wrong solution",
                )
                self.assertFalse(outcome.sentinel_ok)

    def test_extra_boundary_wrong_solutions_are_rejected(self):
        by_id = {task.id: task for task in self.tasks}
        for task_id, variants in EXTRA_WRONG_SOLUTIONS.items():
            for index, code in enumerate(variants):
                with self.subTest(task=task_id, variant=index):
                    outcome = MOD.run_verifier(
                        by_id[task_id], code, timeout_seconds=20
                    )
                    self.assertFalse(
                        outcome.success,
                        f"{task_id} accepted extra wrong variant {index}",
                    )
                    self.assertFalse(outcome.sentinel_ok)


class AggregateWindowsVerifierTests(unittest.TestCase):
    def setUp(self):
        self.task = next(
            task for task in MOD.load_tasks() if task.id == "aggregate_windows"
        )

    def test_correct_unsorted_solution_passes(self):
        outcome = MOD.run_verifier(
            self.task,
            REFERENCE_SOLUTIONS["aggregate_windows"],
            timeout_seconds=20,
        )
        self.assertTrue(outcome.success, outcome.error)
        self.assertTrue(outcome.sentinel_ok)

    def test_sort_only_mutation_of_unsorted_input_is_rejected(self):
        outcome = MOD.run_verifier(
            self.task, AGGREGATE_SORT_ONLY, timeout_seconds=20
        )
        self.assertFalse(outcome.success)
        self.assertIn("reordered or changed input", outcome.error)

    def test_negative_window_mutation_is_rejected(self):
        outcome = MOD.run_verifier(
            self.task, AGGREGATE_NEGATIVE_PATH_MUTATION, timeout_seconds=20
        )
        self.assertFalse(outcome.success)
        self.assertIn("mutated its input on ValueError", outcome.error)

    def test_negative_window_without_value_error_is_rejected(self):
        outcome = MOD.run_verifier(
            self.task, AGGREGATE_NO_NEGATIVE_ERROR, timeout_seconds=20
        )
        self.assertFalse(outcome.success)
        self.assertIn("must raise ValueError", outcome.error)


class SentinelTests(unittest.TestCase):
    GOOD_VERIFIER = (
        "from solution import f\n"
        "assert f() == 1\n"
        "print('ok')\n"
    )

    def test_normal_verifier_passes_with_sentinel(self):
        outcome = MOD.run_verifier(
            _probe_task("print('one')\nprint('two')"), "x = 1", timeout_seconds=10
        )
        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(outcome.returncode, 0)
        self.assertTrue(outcome.sentinel_ok)

    def test_os_exit_zero_during_solution_import_is_rejected(self):
        outcome = MOD.run_verifier(
            _probe_task(self.GOOD_VERIFIER),
            "import os\nos._exit(0)\n",
            timeout_seconds=10,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.sentinel_ok)
        self.assertIn("verifier_sentinel_missing", outcome.error)

    def test_sys_exit_zero_during_solution_import_is_rejected(self):
        outcome = MOD.run_verifier(
            _probe_task(self.GOOD_VERIFIER),
            "import sys\nsys.exit(0)\n",
            timeout_seconds=10,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.sentinel_ok)

    def test_verifier_that_exits_zero_without_a_sentinel_is_rejected(self):
        outcome = MOD.run_verifier(
            _probe_task("import os\nos._exit(0)"), "x = 1", timeout_seconds=10
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.returncode, 0)
        self.assertIn("verifier_sentinel_missing", outcome.error)

    def test_os_exit_zero_inside_the_called_function_is_rejected(self):
        code = "def f():\n    import os\n    os._exit(0)\n"
        outcome = MOD.run_verifier(
            _probe_task(self.GOOD_VERIFIER), code, timeout_seconds=10
        )
        self.assertFalse(outcome.success)
        self.assertFalse(outcome.sentinel_ok)

    def test_output_after_the_sentinel_breaks_the_last_line_rule(self):
        verifier = (
            "import atexit\n"
            "atexit.register(lambda: print('after'))\n"
            "print('ok')\n"
        )
        outcome = MOD.run_verifier(_probe_task(verifier), "x = 1", timeout_seconds=10)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.sentinel_ok)

    def test_explicit_nonce_is_required_in_the_sentinel(self):
        outcome = MOD.run_verifier(
            _probe_task("print('ok')"), "x = 1", timeout_seconds=10, nonce="deadbeef"
        )
        self.assertTrue(outcome.success, outcome.error)
        self.assertTrue(outcome.sentinel_ok)


class SandboxTests(unittest.TestCase):
    SANDBOX_VERIFIER = """import os
import socket
import subprocess


def blocked(call):
    try:
        call()
    except BaseException:
        return True
    return False


assert blocked(lambda: socket.socket().connect(('127.0.0.1', 9)))
assert blocked(lambda: socket.socket().connect_ex(('127.0.0.1', 9)))
assert blocked(lambda: socket.create_connection(('127.0.0.1', 9), timeout=0.2))
assert blocked(lambda: socket.getaddrinfo('example.com', 80))
assert blocked(lambda: subprocess.Popen(['/bin/echo', 'x']))
assert blocked(lambda: subprocess.run(['/bin/echo', 'x']))
assert blocked(lambda: os.system('echo x'))
assert blocked(lambda: os.execv('/bin/echo', ['echo', 'x']))
assert blocked(lambda: os.fork())
assert os.environ.get('HOME') == os.getcwd(), (os.environ.get('HOME'), os.getcwd())
assert os.environ.get('TMPDIR') == os.getcwd(), (
    os.environ.get('TMPDIR'), os.getcwd()
)
assert 'M506_HOST_SECRET' not in os.environ
print('sandbox ok')
"""

    def test_network_and_process_calls_are_blocked_without_side_effects(self):
        marker_dir = tempfile.mkdtemp(prefix="m506-sandbox-marker-")
        marker = Path(marker_dir) / "side-effect"
        verifier = (
            self.SANDBOX_VERIFIER
            + "\nassert blocked(lambda: os.system('touch " + str(marker) + "'))\n"
            + "assert not os.path.exists('" + str(marker) + "')\n"
        )
        with mock.patch.dict(os.environ, {"M506_HOST_SECRET": "must-not-leak"}):
            outcome = MOD.run_verifier(
                _probe_task(verifier), "x = 1", timeout_seconds=10
            )
        self.assertTrue(outcome.success, outcome.error)
        self.assertTrue(outcome.sentinel_ok)
        self.assertFalse(marker.exists(), "sandboxed os.system created a side effect")

    def test_uncaught_blocked_network_call_fails_the_verifier(self):
        verifier = (
            "import socket\n"
            "socket.create_connection(('127.0.0.1', 9), timeout=0.2)\n"
            "print('should not reach here')\n"
        )
        outcome = MOD.run_verifier(_probe_task(verifier), "x = 1", timeout_seconds=10)
        self.assertFalse(outcome.success)
        self.assertNotEqual(outcome.returncode, 0)

    def test_cpu_rlimit_helper_is_available_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX only")
        preexec = MOD._rlimit_preexec(1.2)
        # Never call it here: preexec_fn sets RLIMIT_CPU on the *current*
        # process and would cap the test runner itself.
        self.assertTrue(callable(preexec))


class ExtractionTests(unittest.TestCase):
    def test_python_fence_wins(self):
        content = "```python\nprint(1)\n```"
        self.assertEqual(MOD.extract_code(content), "print(1)")

    def test_plain_text_without_fence_is_used(self):
        self.assertEqual(MOD.extract_code("print(2)"), "print(2)")

    def test_python_fence_beats_an_earlier_text_fence(self):
        content = "```text\nnope\n```\n```python\nprint(3)\n```"
        self.assertEqual(MOD.extract_code(content), "print(3)")

    def test_multiple_python_fences_take_the_first(self):
        content = "```python\nA = 1\n```\n```python\nB = 2\n```"
        self.assertEqual(MOD.extract_code(content), "A = 1")

    def test_first_any_fence_is_used_when_no_python_fence(self):
        content = "```\nA = 1\n```\n```text\nB = 2\n```"
        self.assertEqual(MOD.extract_code(content), "A = 1")

    def test_empty_content_raises(self):
        for value in ("", "   \n", None):
            with self.subTest(value=value):
                with self.assertRaises(MOD.CodeExtractionError):
                    MOD.extract_code(value)

    def test_empty_fence_raises(self):
        with self.assertRaises(MOD.CodeExtractionError):
            MOD.extract_code("```python\n\n```")


class FailureClassificationTests(unittest.TestCase):
    def test_verifier_timeout_is_classified(self):
        task = _probe_task("import time\ntime.sleep(5)\nprint('late')")
        outcome = MOD.run_verifier(task, "x = 1", timeout_seconds=0.2)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.error, "verifier_timeout")
        self.assertGreater(outcome.seconds, 0)

    def test_verifier_nonzero_exit_is_classified(self):
        task = _probe_task("raise SystemExit(3)")
        outcome = MOD.run_verifier(task, "x = 1", timeout_seconds=5)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.returncode, 3)
        self.assertTrue(outcome.error.startswith("verifier_failed (exit 3)"))

    def test_dry_run_is_deterministic_and_never_calls_a_model(self):
        with mock.patch.object(
            MOD, "call_chat", side_effect=AssertionError("network call in dry-run")
        ):
            code1, out1, err1 = _capture_main(["--dry-run"])
            code2, out2, err2 = _capture_main(["--dry-run"])
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertEqual(out1, out2)
        self.assertEqual(err1, "")
        self.assertEqual(err2, "")
        self.assertIn('"mode": "dry-run"', out1)
        self.assertIn("no model calls were made", out1)
        payload = json.loads(out1)
        self.assertEqual(payload["max_tokens"], MOD.DEFAULT_MAX_TOKENS)
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(len(payload["harness_sha256"]), 64)
        self.assertEqual(len(payload["tasks_sha256"]), 64)
        self.assertEqual(
            set(payload["prompts_sha256"]),
            {task.id for task in MOD.load_tasks()},
        )


class ArmSelectionTests(unittest.TestCase):
    def setUp(self):
        self.candidates = MOD.build_candidates(NOW)

    def _task(self, task_id, risk, tags=()):
        return MOD.Task(
            id=task_id,
            title=task_id,
            prompt="p",
            verifier_python="raise SystemExit(0)",
            risk=risk,
            tags=tuple(tags),
        )

    def test_mixed_normal_selects_cheap_model(self):
        selection = MOD.route_arm(
            "mixed",
            self._task("normal", "normal", ("algorithms",)),
            candidates=self.candidates,
            now=NOW,
        )
        self.assertFalse(selection.refused)
        self.assertEqual(selection.model, MOD.CHEAP_MODEL)
        self.assertEqual(selection.effective_risk, "normal")
        self.assertEqual(selection.reason_code, "selected_local_first")
        self.assertTrue(selection.decision_digest)

    def test_mixed_high_credential_selects_strong_model(self):
        selection = MOD.route_arm(
            "mixed",
            self._task("high", "high", ("credential", "key")),
            candidates=self.candidates,
            now=NOW,
        )
        self.assertFalse(selection.refused)
        self.assertEqual(selection.model, MOD.STRONG_MODEL)
        self.assertEqual(selection.effective_risk, "high")
        self.assertEqual(selection.reason_code, "selected_high_risk_assurance")
        self.assertTrue(selection.decision_digest)

    def test_credential_tag_alone_escalates_effective_risk(self):
        selection = MOD.route_arm(
            "mixed",
            self._task("tagged", "normal", ("credential",)),
            candidates=self.candidates,
            now=NOW,
        )
        self.assertEqual(selection.effective_risk, "high")
        self.assertEqual(selection.model, MOD.STRONG_MODEL)

    def test_router_refusal_is_not_silently_replaced(self):
        weak = MOD.build_candidates(NOW, weak_assurance=0, strong_assurance=1)
        task = self._task("high", "high", ("credential",))
        selection = MOD.route_arm("mixed", task, candidates=weak, now=NOW)
        self.assertTrue(selection.refused)
        self.assertIsNone(selection.model)
        self.assertEqual(selection.reason_code, "high_risk_assurance_unsatisfied")

        record = MOD.selection_result("mixed", task, selection)
        self.assertFalse(record["executed"])
        self.assertIsNone(record["success"])
        self.assertIsNone(record["model"])
        self.assertEqual(record["error"], "router_refused")
        self.assertEqual(record["reason_code"], selection.reason_code)
        self.assertTrue(record["decision_digest"])

    def test_single_always_uses_strong_model(self):
        for risk in ("normal", "high"):
            with self.subTest(risk=risk):
                selection = MOD.route_arm(
                    "single",
                    self._task("single", risk, ("algorithms",)),
                    candidates=self.candidates,
                    now=NOW,
                )
                self.assertFalse(selection.refused)
                self.assertEqual(selection.model, MOD.STRONG_MODEL)


class EndpointAndRedactionTests(unittest.TestCase):
    def test_loopback_endpoint_is_accepted(self):
        self.assertEqual(MOD.validate_endpoint(MOD.ENDPOINT), MOD.ENDPOINT)

    def test_non_loopback_and_non_http_endpoints_are_rejected(self):
        for url in (
            "http://10.0.0.5:8000",
            "http://example.com:8000",
            "https://127.0.0.1:8000",
            "http://localhost:8000",
        ):
            with self.subTest(url=url):
                with self.assertRaises(MOD.PilotError):
                    MOD.validate_endpoint(url)

    def test_redaction_removes_credential_shapes(self):
        dirty = (
            "failed: api_key=sk-abcdefghijklmnop password=hunter2 "
            "token: abc123 Bearer zzzz"
        )
        clean = MOD.redact_text(dirty)
        for secret in ("sk-abcdefghijklmnop", "hunter2", "abc123", "Bearer zzzz"):
            self.assertNotIn(secret, clean)
        self.assertIn("[redacted]", clean)

        key = "tck_" + "a" * 32
        self.assertNotIn(key, MOD.redact_text("bad " + key))

    def test_error_redaction_is_bounded(self):
        dirty = "api_key=sk-abcdefghijklmnop " + ("x" * (MOD.MAX_ERROR_CHARS + 100))
        clean = MOD.clean_error(dirty)
        self.assertNotIn("sk-abcdefghijklmnop", clean)
        self.assertIn("[redacted]", clean)
        self.assertLessEqual(
            len(clean), MOD.MAX_ERROR_CHARS + len("...[truncated]")
        )

    def test_results_document_never_writes_secrets_or_raw_model_text(self):
        dirty = "boom api_key=sk-abcdefghijklmnop password=hunter2"
        record = _record("single", "merge_intervals", error=dirty)
        record["content"] = "raw model content"
        record["reasoning_content"] = "raw reasoning"
        document = MOD.build_results_document(
            [record], generated_at=NOW, timeout_seconds=180
        )
        text = json.dumps(document)
        self.assertNotIn("sk-abcdefghijklmnop", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("raw model content", text)
        self.assertNotIn("raw reasoning", text)
        self.assertNotIn("content", document["results"][0])
        self.assertNotIn("reasoning_content", document["results"][0])
        self.assertIn("library-level", text)
        self.assertIn("No cloud arm", text)


class ProvenanceTests(unittest.TestCase):
    def test_compute_provenance_hashes_harness_tasks_and_prompts(self):
        tasks = MOD.load_tasks()
        prov = MOD.compute_provenance(tasks)
        self.assertEqual(len(prov["harness_sha256"]), 64)
        self.assertEqual(len(prov["tasks_sha256"]), 64)
        self.assertEqual(set(prov["prompts_sha256"]), {task.id for task in tasks})
        for task in tasks:
            self.assertEqual(
                prov["prompts_sha256"][task.id], MOD.sha256_text(task.prompt)
            )

    def test_run_pilot_records_provenance_content_and_usage(self):
        secret = "api_key=sk-abcdefghijklmnop"
        task = MOD.Task(
            id="probe",
            title="probe",
            prompt="Solve the probe task. " + secret,
            verifier_python="print('ok')",
            risk="normal",
            tags=(),
        )

        def fake_call_chat(model, messages, *, max_tokens, timeout_seconds,
                           base_url=MOD.ENDPOINT):
            if messages[0]["content"] == "Reply with OK.":
                document = {
                    "choices": [
                        {"message": {"content": "OK"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "completion_tokens_details": {"reasoning_tokens": 0},
                        "total_time": 0.2,
                        "model_load_duration": 0.1,
                        "generation_duration": 0.05,
                    },
                }
                return document, 0.3
            content = "```python\n# " + secret + "\nX = 1\n```"
            document = {
                "choices": [
                    {"message": {"content": content}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "completion_tokens_details": {"reasoning_tokens": 7},
                    "total_time": 2.4,
                    "model_load_duration": 0.0,
                    "generation_duration": 2.0,
                },
            }
            return document, 2.5

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MOD, "call_chat", side_effect=fake_call_chat
        ), mock.patch.object(
            MOD,
            "run_verifier",
            return_value=MOD.VerifierOutcome(True, None, 0.01, 0, True),
        ):
            document = MOD.run_pilot([task], output_dir=tmp, max_tokens=8192)

            self.assertEqual(len(document["warmups"]), 4)
            for warmup in document["warmups"]:
                self.assertEqual(warmup["usage_timings"]["generation_duration"], 0.05)
            self.assertEqual(document["max_tokens"], 8192)
            self.assertEqual(document["harness_sha256"], MOD.file_sha256(SCRIPT))
            self.assertEqual(
                document["tasks_sha256"], MOD.file_sha256(MOD.TASKS_PATH)
            )
            self.assertEqual(
                document["prompts_sha256"][task.id],
                MOD.sha256_text(task.prompt),
            )
            self.assertEqual(len(document["results"]), 2)
            for row in document["results"]:
                self.assertEqual(row["finish_reason"], "stop")
                self.assertEqual(row["reasoning_tokens"], 7)
                self.assertEqual(
                    row["usage_timings"],
                    {
                        "total_time": 2.4,
                        "model_load_duration": 0.0,
                        "generation_duration": 2.0,
                    },
                )
                self.assertEqual(row["prompt_sha256"], MOD.sha256_text(task.prompt))
                self.assertTrue(row["sentinel_ok"])
                content_path = (
                    Path(tmp) / "contents" / row["arm"] / (task.id + ".txt")
                )
                data = content_path.read_bytes()
                self.assertNotIn(b"sk-abcdefghijklmnop", data)
                self.assertIn(b"[redacted]", data)
                self.assertEqual(row["content_sha256"], MOD.sha256_bytes(data))
                self.assertEqual(row["content_bytes"], len(data))
                self.assertFalse(row["content_truncated"])
                self.assertFalse(row["content_empty"])
            self.assertEqual(
                document["summary"]["single"]["wall_model_seconds"], 2.5
            )
            self.assertEqual(
                document["summary"]["single"]["generation_seconds"], 2.0
            )
            self.assertEqual(
                document["summary"]["single"]["model_load_seconds"], 0.0
            )
            joined = "\n".join(document["limits"])
            self.assertIn("max_tokens=8192", joined)
            self.assertIn("wall_model_seconds", joined)
            self.assertIn("generation_seconds", joined)
            self.assertIn("not a security boundary", joined)
            self.assertIn("ctypes", joined)
            summary = MOD.render_summary(document)
            self.assertIn("wall_model_seconds", summary)
            self.assertIn("generation_seconds", summary)

    def test_run_pilot_truncates_content_at_64_kib_with_marker(self):
        task = MOD.Task(
            id="probe",
            title="probe",
            prompt="p",
            verifier_python="print('ok')",
            risk="normal",
            tags=(),
        )
        big = "a" * (MOD.MAX_CONTENT_BYTES + 5000)

        def fake_call_chat(model, messages, *, max_tokens, timeout_seconds,
                           base_url=MOD.ENDPOINT):
            if messages[0]["content"] == "Reply with OK.":
                return {
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }, 0.1
            return {
                "choices": [
                    {"message": {"content": big}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }, 0.2

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MOD, "call_chat", side_effect=fake_call_chat
        ), mock.patch.object(
            MOD,
            "run_verifier",
            return_value=MOD.VerifierOutcome(True, None, 0.01, 0, True),
        ):
            document = MOD.run_pilot([task], output_dir=tmp)
            row = next(
                item for item in document["results"] if item["arm"] == "single"
            )
            self.assertTrue(row["content_truncated"])
            data = (
                Path(tmp) / "contents" / "single" / (task.id + ".txt")
            ).read_bytes()
            self.assertGreater(row["content_bytes"], MOD.MAX_CONTENT_BYTES)
            self.assertLessEqual(
                row["content_bytes"], MOD.MAX_CONTENT_BYTES + 256
            )
            self.assertIn(b"content truncated at", data)
            self.assertEqual(row["content_sha256"], MOD.sha256_bytes(data))

    def test_missing_message_content_is_stored_as_empty(self):
        task = MOD.Task(
            id="probe",
            title="probe",
            prompt="p",
            verifier_python="print('ok')",
            risk="normal",
            tags=(),
        )

        def fake_call_chat(model, messages, *, max_tokens, timeout_seconds,
                           base_url=MOD.ENDPOINT):
            if messages[0]["content"] == "Reply with OK.":
                return {
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }, 0.1
            return {
                "choices": [
                    {"message": {"content": None}, "finish_reason": "length"}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 8},
                },
            }, 0.2

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MOD, "call_chat", side_effect=fake_call_chat
        ):
            document = MOD.run_pilot([task], output_dir=tmp)
            row = next(
                item for item in document["results"] if item["arm"] == "single"
            )
            self.assertTrue(row["content_empty"])
            self.assertFalse(row["success"])
            self.assertEqual(row["finish_reason"], "length")
            self.assertEqual(document["summary"]["single"]["truncated"], 1)


class TimingAndAggregationTests(unittest.TestCase):
    def test_success_rates_usage_and_refusal_counts(self):
        results = [
            _record("single", "a", success=True, model_seconds=1.0,
                    verifier_seconds=0.1, prompt_tokens=10, completion_tokens=5),
            _record("single", "b", success=False, model_seconds=2.0,
                    verifier_seconds=0.2, prompt_tokens=20, completion_tokens=7),
            _record("single", "c", executed=False, success=None, model=None,
                    model_seconds=None, verifier_seconds=None,
                    prompt_tokens=None, completion_tokens=None,
                    error="router_refused"),
            _record("mixed", "a", success=True, model_seconds=0.5,
                    verifier_seconds=0.05, prompt_tokens=None,
                    completion_tokens=None),
        ]
        summary = MOD.aggregate(results)

        single = summary["single"]
        self.assertEqual(single["tasks"], 3)
        self.assertEqual(single["executed"], 2)
        self.assertEqual(single["succeeded"], 1)
        self.assertEqual(single["failed"], 1)
        self.assertEqual(single["refused"], 1)
        self.assertAlmostEqual(single["success_rate_executed"], 0.5)
        self.assertAlmostEqual(single["success_rate_total"], 1 / 3, places=4)
        self.assertEqual(single["model_seconds"], 3.0)
        self.assertEqual(single["verifier_seconds"], 0.3)
        self.assertEqual(single["prompt_tokens"], 30)
        self.assertEqual(single["completion_tokens"], 12)
        self.assertEqual(single["tasks_with_unknown_usage"], 0)

        mixed = summary["mixed"]
        self.assertEqual(mixed["executed"], 1)
        self.assertEqual(mixed["succeeded"], 1)
        self.assertIsNone(mixed["prompt_tokens"])
        self.assertIsNone(mixed["completion_tokens"])
        self.assertEqual(mixed["tasks_with_unknown_usage"], 1)

    def test_wall_and_generation_seconds_are_summed_separately(self):
        results = [
            _record(
                "single",
                "a",
                model_seconds=1.5,
                reasoning_tokens=3,
                usage_timings={
                    "total_time": 1.2,
                    "model_load_duration": 0.2,
                    "generation_duration": 0.9,
                },
            ),
            _record(
                "single",
                "b",
                model_seconds=2.5,
                reasoning_tokens=4,
                usage_timings={
                    "total_time": 2.0,
                    "model_load_duration": 0.5,
                    "generation_duration": 1.4,
                },
            ),
        ]
        summary = MOD.aggregate(results)
        single = summary["single"]
        self.assertEqual(single["wall_model_seconds"], 4.0)
        self.assertEqual(single["model_seconds"], 4.0)
        self.assertEqual(single["generation_seconds"], 2.3)
        self.assertEqual(single["model_load_seconds"], 0.7)
        self.assertEqual(single["reasoning_tokens"], 7)

    def test_truncation_and_model_switch_are_counted(self):
        results = [
            _record("mixed", "a", model=MOD.CHEAP_MODEL,
                    finish_reason="length", content_empty=True),
            _record("mixed", "b", model=MOD.CHEAP_MODEL,
                    finish_reason="stop", content_empty=False),
            _record("mixed", "c", model=MOD.STRONG_MODEL,
                    finish_reason="stop", content_empty=False),
            _record("single", "a", model=MOD.STRONG_MODEL,
                    finish_reason="stop", content_empty=False),
        ]
        summary = MOD.aggregate(results)
        self.assertEqual(summary["mixed"]["truncated"], 1)
        self.assertEqual(summary["mixed"]["distinct_models"], 2)
        self.assertTrue(summary["mixed"]["model_switch_occurred"])
        self.assertEqual(summary["single"]["distinct_models"], 1)
        self.assertFalse(summary["single"]["model_switch_occurred"])

    def test_decision_distribution_counts_each_arm(self):
        results = [
            _record("single", "a", reason_code="selected_high_risk_assurance",
                    effective_risk="high", model=MOD.STRONG_MODEL),
            _record("single", "b", reason_code="selected_high_risk_assurance",
                    effective_risk="high", model=MOD.STRONG_MODEL),
            _record("mixed", "a", reason_code="selected_local_first",
                    effective_risk="normal", model=MOD.CHEAP_MODEL),
            _record("mixed", "c", executed=False, success=None, model=None,
                    model_seconds=None, verifier_seconds=None,
                    prompt_tokens=None, completion_tokens=None,
                    reason_code="high_risk_assurance_unsatisfied",
                    effective_risk="high", error="router_refused"),
        ]
        distribution = MOD.decision_distribution(results)
        lookup = {
            (item["arm"], item["reason_code"], item["model"]): item["count"]
            for item in distribution
        }
        self.assertEqual(
            lookup[("single", "selected_high_risk_assurance", MOD.STRONG_MODEL)], 2
        )
        self.assertEqual(
            lookup[("mixed", "selected_local_first", MOD.CHEAP_MODEL)], 1
        )
        self.assertEqual(
            lookup[("mixed", "high_risk_assurance_unsatisfied", None)], 1
        )

    def test_summary_renders_timing_and_limitations(self):
        results = [_record("single", "a"), _record("mixed", "a")]
        document = MOD.build_results_document(
            results, generated_at=NOW, timeout_seconds=180, max_tokens=8192
        )
        summary = MOD.render_summary(document)
        self.assertIn("N=1", summary)
        self.assertIn("library-level", summary)
        self.assertIn("No cloud arm", summary)
        self.assertIn("no statistical significance", summary)
        self.assertIn("wall_model_seconds", summary)
        self.assertIn("generation_seconds", summary)
        self.assertIn("max_tokens=8192", summary)
        self.assertIn("harness_sha256", summary)
        self.assertIn("tasks_sha256", summary)
        self.assertIn("not a security boundary", summary)
        self.assertIn("non-adversarial model output", summary)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.task = next(
            task for task in MOD.load_tasks() if task.id == "aggregate_windows"
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name) / "source"
        self.output = Path(self._tmp.name) / "replay"

    def _changed_fixture(self):
        return _write_replay_source(
            self.source,
            [
                (
                    "single",
                    "aggregate_windows",
                    _fenced(AGGREGATE_SORT_ONLY),
                    True,
                    None,
                ),
                (
                    "mixed",
                    "aggregate_windows",
                    _fenced(REFERENCE_SOLUTIONS["aggregate_windows"]),
                    False,
                    "verifier_failed",
                ),
            ],
        )

    def test_replay_reports_old_vs_new_and_fixed_rates(self):
        self._changed_fixture()
        with mock.patch.object(
            MOD, "call_chat", side_effect=AssertionError("network call in replay")
        ):
            document = MOD.replay_results(
                self.source,
                output_dir=self.output,
                tasks=[self.task],
                timeout_seconds=20,
            )
        by_arm = {row["arm"]: row for row in document["results"]}
        self.assertTrue(by_arm["single"]["old_success"])
        self.assertFalse(by_arm["single"]["new_success"])
        self.assertTrue(by_arm["single"]["changed"])
        self.assertEqual(
            by_arm["single"]["reason"], "fixed_verifier_rejects"
        )
        self.assertFalse(by_arm["mixed"]["old_success"])
        self.assertTrue(by_arm["mixed"]["new_success"])
        self.assertEqual(
            by_arm["mixed"]["reason"], "fixed_verifier_accepts"
        )
        self.assertEqual(document["summary"]["changed_count"], 2)

        fixed = document["summary"]["with_fixed_verifier"]
        self.assertEqual(fixed["single"]["old_succeeded"], 1)
        self.assertEqual(fixed["single"]["new_succeeded"], 0)
        self.assertEqual(fixed["single"]["fixed_success_rate_executed"], 0.0)
        self.assertEqual(fixed["single"]["changed_tasks"], ["aggregate_windows"])
        self.assertEqual(fixed["mixed"]["old_succeeded"], 0)
        self.assertEqual(fixed["mixed"]["new_succeeded"], 1)
        self.assertEqual(fixed["mixed"]["fixed_success_rate_executed"], 1.0)
        self.assertEqual(fixed["mixed"]["changed_tasks"], ["aggregate_windows"])

        self.assertFalse(document["network_access"])
        self.assertTrue(document["read_only_source"])
        self.assertTrue(
            any("not a security boundary" in line for line in document["limits"])
        )
        written = json.loads((self.output / "replay.json").read_text())
        self.assertEqual(written["mode"], "replay")
        self.assertEqual(written["summary"]["changed_count"], 2)

    def test_replay_reports_unchanged_verdicts(self):
        _write_replay_source(
            self.source,
            [
                (
                    "single",
                    "aggregate_windows",
                    _fenced(REFERENCE_SOLUTIONS["aggregate_windows"]),
                    True,
                    None,
                ),
                (
                    "mixed",
                    "aggregate_windows",
                    _fenced(AGGREGATE_SORT_ONLY),
                    False,
                    "verifier_failed",
                ),
            ],
        )
        document = MOD.replay_results(
            self.source,
            output_dir=self.output,
            tasks=[self.task],
            timeout_seconds=20,
        )
        self.assertEqual(document["summary"]["changed_count"], 0)
        self.assertEqual(document["summary"]["changed"], [])
        for row in document["results"]:
            self.assertFalse(row["changed"])
        self.assertEqual(
            document["summary"]["with_fixed_verifier"]["single"][
                "fixed_success_rate_executed"
            ],
            1.0,
        )
        self.assertEqual(
            document["summary"]["with_fixed_verifier"]["mixed"][
                "fixed_success_rate_executed"
            ],
            0.0,
        )

    def test_replay_rejects_content_hash_mismatch(self):
        self._changed_fixture()
        path = self.source / "contents" / "single" / "aggregate_windows.txt"
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaises(MOD.ReplayError) as caught:
            MOD.replay_results(
                self.source,
                output_dir=self.output,
                tasks=[self.task],
                timeout_seconds=20,
            )
        self.assertIn("hash mismatch", str(caught.exception))
        self.assertFalse((self.output / "replay.json").exists())

    def test_replay_rejects_missing_content_file(self):
        self._changed_fixture()
        (self.source / "contents" / "single" / "aggregate_windows.txt").unlink()
        with self.assertRaises(MOD.ReplayError) as caught:
            MOD.replay_results(
                self.source,
                output_dir=self.output,
                tasks=[self.task],
                timeout_seconds=20,
            )
        self.assertIn("cannot read retained content", str(caught.exception))

    def test_replay_does_not_modify_the_source_tree(self):
        self._changed_fixture()
        before = {
            path: path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }
        MOD.replay_results(
            self.source,
            output_dir=self.output,
            tasks=[self.task],
            timeout_seconds=20,
        )
        after = {
            path: path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_cli_replay_writes_replay_json_without_network(self):
        self._changed_fixture()
        with mock.patch.object(
            MOD, "call_chat", side_effect=AssertionError("network call in replay")
        ):
            code, out, err = _capture_main(
                [
                    "--replay",
                    "--results",
                    str(self.source),
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 0, err)
        self.assertIn("wrote", out)
        self.assertIn("changed verdicts: 2", out)
        self.assertTrue((self.output / "replay.json").is_file())

    def test_cli_replay_requires_results(self):
        with self.assertRaises(SystemExit) as caught:
            _capture_main(["--replay"])
        self.assertNotEqual(caught.exception.code, 0)


class CliTests(unittest.TestCase):
    def _document(self, success):
        return {
            "summary": {
                "single": {
                    "succeeded": 0 if not success else 1,
                    "executed": 1,
                    "wall_model_seconds": 1.0,
                    "generation_seconds": 0.5,
                    "truncated": 0,
                },
                "mixed": {
                    "succeeded": 1,
                    "executed": 1,
                    "wall_model_seconds": 1.0,
                    "generation_seconds": 0.5,
                    "truncated": 0,
                },
            },
            "results": [{"executed": True, "success": success}],
        }

    def test_task_level_failure_exits_zero_unless_strict(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MOD, "run_pilot", return_value=self._document(False)
        ):
            code, _, _ = _capture_main(["--run", "--output", tmp])
            strict_code, _, strict_err = _capture_main(
                ["--run", "--output", tmp, "--strict"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(strict_code, 3)
        self.assertIn("strict: 1", strict_err)

    def test_strict_is_zero_when_everything_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MOD, "run_pilot", return_value=self._document(True)
        ):
            code, _, _ = _capture_main(["--run", "--output", tmp, "--strict"])
        self.assertEqual(code, 0)

    def test_infrastructure_failure_is_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MOD, "run_pilot", side_effect=MOD.InfrastructureError("boom")
        ):
            code, _, err = _capture_main(["--run", "--output", tmp])
        self.assertEqual(code, 3)
        self.assertIn("infrastructure error", err)

    def test_max_tokens_is_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MOD, "run_pilot", return_value=self._document(True)
        ) as run:
            code, _, _ = _capture_main(
                ["--run", "--output", tmp, "--max-tokens", "1234"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["max_tokens"], 1234)

    def test_dry_run_hash_changes_when_the_task_set_changes(self):
        tasks = MOD.load_tasks()
        first = MOD.build_dry_run(tasks)
        mutated = (
            MOD.Task(
                id=tasks[0].id,
                title=tasks[0].title,
                prompt=tasks[0].prompt + " (changed)",
                verifier_python=tasks[0].verifier_python,
                risk=tasks[0].risk,
                tags=tasks[0].tags,
            ),
        ) + tuple(tasks[1:])
        second = MOD.build_dry_run(mutated)
        self.assertNotEqual(
            first["prompts_sha256"], second["prompts_sha256"]
        )
        self.assertEqual(first["tasks_sha256"], second["tasks_sha256"])


if __name__ == "__main__":
    unittest.main()
