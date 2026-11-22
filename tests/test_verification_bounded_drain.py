"""Pinpoint regression for ``verification._run_bounded`` pipe-drain race.

CI flake at 83a3ab8 (tests/test_verification_policy_rebind.py subTest
``case=different_policy`` and siblings): the verifier ran and PASSED
(``ran=true, exit_code=0, stdout='ok'``, identity + diff gates clean) yet the
verdict recorded ``status=error`` with
``cleanup_error='could not kill verifier process: [Errno 1] Operation not
permitted'``.

Root cause: once ``proc.poll()`` first sees the exited child, the loop arms a
1-second *pipe-retention* deadline.  If the test process is descheduled for
more than a second before the next ``selector.select()`` drains the pipes
(parallel CI load), the expiry branch fires while the readiness events are
already pending.  The old code then broke out, closed the pipes WITHOUT a
final drain, SIGKILLed the exited child's (possibly recycled) process group —
ESRCH or EPERM — and labelled a healthy run as having retained an output
pipe.

The fix drains pending readiness non-blockingly at expiry and only declares
retention for pipes whose write end is STILL open (reads would block).  These
tests pin both directions:

* the stall scenario must produce NO cleanup error (fails on pre-fix code);
* genuine inherited-pipe retention, output bounds, and genuine timeouts keep
  their pre-existing error semantics (pass before and after the fix).

The scheduler stall is made deterministic by a selector shim that swallows the
readiness events of the two first ``select()`` calls while the real clock
advances past the retention deadline; no sleeps-as-synchronization, the
assertions are on the runner's returned tuple only.
"""

from __future__ import annotations

import selectors as real_selectors
import sys
import tempfile
import time
import unittest

from teamcraft import verification


class _StallingSelector:
    """Pass-through selector whose first ``swallow`` select() calls fake a
    scheduler stall: sleep (real clock advances) and report no readiness."""

    def __init__(self, swallow: int, stall_seconds: float) -> None:
        self._inner = real_selectors.DefaultSelector()
        self._swallow = swallow
        self._stall_seconds = stall_seconds
        self.stalled_calls = 0

    def register(self, *args, **kwargs):
        return self._inner.register(*args, **kwargs)

    def unregister(self, *args, **kwargs):
        return self._inner.unregister(*args, **kwargs)

    def get_map(self):
        return self._inner.get_map()

    def close(self):
        return self._inner.close()

    def select(self, timeout=None):
        if self._swallow > 0:
            self._swallow -= 1
            self.stalled_calls += 1
            time.sleep(self._stall_seconds)
            return []
        return self._inner.select(timeout)


class _SelectorNamespace:
    """Minimal stand-in for the ``selectors`` module attribute of
    ``teamcraft verification`` so ``_run_bounded`` builds our shim."""

    EVENT_READ = real_selectors.EVENT_READ

    def __init__(self, swallow: int, stall_seconds: float) -> None:
        self.swallow = swallow
        self.stall_seconds = stall_seconds
        self.last_selector: _StallingSelector | None = None

    def DefaultSelector(self) -> _StallingSelector:
        self.last_selector = _StallingSelector(self.swallow, self.stall_seconds)
        return self.last_selector


class BoundedDrainRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tc-bounded-drain-")
        self.addCleanup(self._tmp.cleanup)
        self.cwd = self._tmp.name
        self._original_selectors = verification.selectors
        self.addCleanup(setattr, verification, "selectors",
                       self._original_selectors)

    def _install_shim(self, *, swallow: int, stall_seconds: float) -> _SelectorNamespace:
        shim = _SelectorNamespace(swallow=swallow, stall_seconds=stall_seconds)
        verification.selectors = shim
        return shim

    # -- the proven CI bug ------------------------------------------------------------

    def test_scheduler_stall_after_exit_does_not_fabricate_pipe_retention(self) -> None:
        # Healthy verifier: prints 'ok', exits promptly; both pipes hold
        # pending EOF the moment it dies.  Two swallowed readiness windows
        # (> 1.0 s total, past the retention deadline) emulate the CI stall.
        shim = self._install_shim(swallow=2, stall_seconds=1.2)
        started = time.monotonic()
        returncode, stdout, stderr, timed_out, truncated, cleanup_error = (
            verification._run_bounded(
                [sys.executable, "-c", "print('ok')"],
                cwd=self.cwd, timeout_seconds=30))
        elapsed = time.monotonic() - started
        self.assertEqual(shim.last_selector.stalled_calls, 2)
        self.assertGreater(elapsed, 1.0)  # retention deadline genuinely passed
        self.assertEqual(returncode, 0)
        # Pre-fix behaviour (the CI flake): a fabricated cleanup error —
        # "... retained an output pipe after the parent exited", or the EPERM
        # from SIGKILLing a recycled process group — on a fully passing run.
        self.assertIsNone(cleanup_error)
        self.assertEqual(stdout, b"ok\n")
        self.assertEqual(stderr, b"")
        self.assertFalse(timed_out)
        self.assertFalse(truncated)

    # -- guardrails that must survive the fix ------------------------------------------

    def test_eof_with_exit_status_pending_is_not_killed(self) -> None:
        # Second proven CI race (same 83a3ab8 report): the kernel closes the
        # pipe write ends just before the exit status becomes collectable, so
        # the loop exits on EOF with proc.poll() STILL None for a child that
        # already completed.  Pre-fix, `if proc.poll() is None: terminate()`
        # then SIGKILLed the dead-or-recycled process group — EPERM from a
        # recycled pg polluted cleanup_error over a passed run (and a child
        # that finishes within the grace must likewise never be killed).
        code = (
            "import os, sys, time\n"
            "print('ok', flush=True)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "os.close(1); os.close(2)\n"
            "time.sleep(0.05)\n"
            "os._exit(0)\n"
        )
        returncode, stdout, stderr, timed_out, truncated, cleanup_error = (
            verification._run_bounded(
                [sys.executable, "-c", code],
                cwd=self.cwd, timeout_seconds=30))
        # Pre-fix: SIGKILL lands mid-sleep -> returncode -9 (the caller maps
        # a negative exit to a verifier-crash ERROR over a passing run).
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout, b"ok\n")
        self.assertFalse(timed_out)
        self.assertFalse(truncated)
        self.assertIsNone(cleanup_error)

    def test_pipe_closing_runaway_is_still_terminated_quickly(self) -> None:
        # A verifier that closes both pipes but keeps running is the genuine
        # runaway the post-loop terminate exists for: it must still be killed
        # long before its own sleep ends (and without tripping the 30s
        # command deadline itself).
        code = (
            "import os, sys, time\n"
            "print('ok', flush=True)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "os.close(1); os.close(2)\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        returncode, stdout, stderr, timed_out, truncated, cleanup_error = (
            verification._run_bounded(
                [sys.executable, "-c", code],
                cwd=self.cwd, timeout_seconds=30))
        elapsed = time.monotonic() - started
        self.assertNotEqual(returncode, 0)
        self.assertFalse(timed_out)
        self.assertFalse(truncated)
        self.assertLess(elapsed, 5.0)

    def test_descendant_pipe_retention_still_errors(self) -> None:
        # A detached grandchild inherits stdout/stderr and keeps sleeping 30s
        # after the verifier itself exits: the retention guard must still fire,
        # report the inherited-pipe problem, and kill the process group so the
        # grandchild does not linger.
        code = (
            "import subprocess, sys\n"
            "subprocess.Popen(['/bin/sleep', '30'], stdout=sys.stdout,"
            " stderr=sys.stderr, stdin=subprocess.DEVNULL)\n"
            "print('ok', flush=True)\n"
        )
        started = time.monotonic()
        returncode, stdout, stderr, timed_out, truncated, cleanup_error = (
            verification._run_bounded(
                [sys.executable, "-c", code],
                cwd=self.cwd, timeout_seconds=30))
        elapsed = time.monotonic() - started
        self.assertEqual(returncode, 0)
        self.assertTrue(stdout.startswith(b"ok"))
        self.assertFalse(timed_out)
        self.assertFalse(truncated)
        self.assertIsNotNone(cleanup_error)
        self.assertTrue(
            "retained an output pipe" in cleanup_error
            or "could not kill verifier process" in cleanup_error,
            cleanup_error)
        # The SIGKILL of the verifier's process group must have reaped the
        # grandchild long before its own 30-second sleep would end.
        self.assertLess(elapsed, 10.0)

    def test_output_bound_still_truncates_and_kills(self) -> None:
        payload = verification.MAX_CAPTURE_BYTES + 4096
        code = f"import sys; sys.stdout.write('x' * {payload}); sys.stdout.flush()"
        returncode, stdout, stderr, timed_out, truncated, cleanup_error = (
            verification._run_bounded(
                [sys.executable, "-c", code],
                cwd=self.cwd, timeout_seconds=30))
        self.assertTrue(truncated)
        self.assertEqual(len(stdout), verification.MAX_CAPTURE_BYTES)
        self.assertFalse(timed_out)
        self.assertNotEqual(returncode, 0)

    def test_genuine_timeout_still_times_out(self) -> None:
        returncode, stdout, stderr, timed_out, truncated, cleanup_error = (
            verification._run_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=self.cwd, timeout_seconds=1))
        self.assertTrue(timed_out)
        self.assertFalse(truncated)
        # SIGKILL of the process group reaps the child with signal 9; a
        # reaping timeout leaves returncode None.  Both are the established
        # genuine-timeout semantics (the caller maps timed_out to ERROR).
        self.assertIn(returncode, (None, -9))


if __name__ == "__main__":
    unittest.main()
