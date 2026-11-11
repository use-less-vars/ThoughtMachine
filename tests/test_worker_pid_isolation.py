# tests/test_worker_pid_isolation.py
"""scripts/prove_worker_pid.py must FAIL when it cannot get the worker's PID.

Why this file exists
--------------------
The proof script answers one question -- *which OS process runs the worker's
code?* -- by asking the worker to stamp its own ``os.getpid()`` on the reply it
sends back.  That design has one failure mode worth guarding, and it is the
quiet one: a proof that cannot obtain the PID must never pass, and must never
be skipped.  "I could not see where the worker ran" and "the worker ran in its
own process" are different statements, and a test suite that cannot tell them
apart is worse than no test at all.

What is covered
---------------
1. :func:`parse_pid_from_reply` against realistic worker output -- the
   three-way envelope the script uses and the minimal ``SENTINEL:pid=<n>:<q>``
   shape tests/test_worker_process_e2e.py already relies on -- including every
   way a reply can fail to carry a PID.
2. :func:`verdict_for` both ways: equal PIDs are *not* isolation, distinct PIDs
   are.  The equality case is a real answer (thread mode), not an error.
3. The real-spawn path is genuinely driven (``WORKER_RUNTIME_MODE=process``,
   a real ``multiprocessing`` child), and an unobtainable PID is asserted to
   raise -- never to return, never to skip.  No ``skipif``: a platform that
   cannot spawn a child cannot satisfy the contract, and that is a red test.

Hermetic: the spawn uses the script's own stand-in worker factory (resolved by
``module:attr`` inside the child), so no LLM, vault, docker or network is
touched.  The only real work is one OS process per spawn test.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.prove_worker_pid as prove  # noqa: E402 - path fixed above


class TestPidParsing(unittest.TestCase):
    """The PID has to come off the wire; nothing else may be inferred."""

    def test_parses_pid_from_the_scripts_own_envelope(self):
        reply = (
            "prove-worker-pid:pid=4711:tid=126716000466624:ppid=4702"
            ":ns=4026534238:exec_pid=4713:prove-worker-pid"
        )
        self.assertEqual(prove.parse_pid_from_reply(reply), 4711)

    def test_parses_the_minimal_envelope_the_e2e_suite_uses(self):
        """``SENTINEL:pid=<pid>:<query>`` -- the shape C40 already asserts on."""
        self.assertEqual(prove.parse_pid_from_reply("prove-worker-pid:pid=99:q"), 99)

    def test_reads_the_informational_fields_without_letting_them_reach_the_verdict(self):
        reply = (
            "prove-worker-pid:pid=4711:tid=139:ppid=4702"
            ":ns=4026534238:exec_pid=4713:prove-worker-pid"
        )
        fields = prove.reply_fields(reply)
        self.assertEqual(fields["pid"], "4711")
        self.assertEqual(fields["tid"], "139")
        self.assertEqual(fields["ppid"], "4702")
        self.assertEqual(fields["ns"], "4026534238")
        self.assertEqual(fields["exec_pid"], "4713")

    def test_reply_without_a_pid_raises(self):
        with self.assertRaises(prove.WorkerPidUnavailable):
            prove.parse_pid_from_reply("the worker answered something else")

    def test_non_numeric_pid_raises(self):
        with self.assertRaises(prove.WorkerPidUnavailable):
            prove.parse_pid_from_reply("prove-worker-pid:pid=not-a-pid:q")

    def test_zero_pid_raises(self):
        with self.assertRaises(prove.WorkerPidUnavailable):
            prove.parse_pid_from_reply("prove-worker-pid:pid=0:q")

    def test_empty_and_missing_replies_raise_rather_than_default(self):
        for reply in ("", "prove-worker-pid", None, 1234):
            with self.subTest(reply=reply):
                with self.assertRaises(prove.WorkerPidUnavailable):
                    prove.parse_pid_from_reply(reply)


class TestVerdictLogic(unittest.TestCase):
    """Equality and inequality, both asserted -- the equality case is an answer."""

    def test_equal_pids_are_not_isolation(self):
        proven, line = prove.verdict_for(parent_pid=1234, worker_pid=1234)
        self.assertFalse(proven)
        self.assertIn("PID 1234 == parent PID 1234", line)
        self.assertIn("no process isolation", line)

    def test_distinct_pids_are_isolation(self):
        proven, line = prove.verdict_for(parent_pid=1234, worker_pid=1235)
        self.assertTrue(proven)
        self.assertIn("PID 1235 != parent PID 1234", line)
        self.assertIn("separate OS process", line)


class TestTheProofIsDrivenAndCannotPassVacuously(unittest.TestCase):
    """The spawn is real; a missing PID is a failure."""

    def test_thread_mode_runs_the_worker_body_in_this_interpreter(self):
        """The shipped default, asserted as measured fact rather than assumed."""
        observation = prove.observe_thread_mode(str(_REPO_ROOT))
        self.assertEqual(observation.mode, prove.MODE_THREAD)
        self.assertEqual(observation.worker_pid, os.getpid())
        self.assertEqual(
            observation.detail["thread_ident"], str(threading.get_ident())
        )

    def test_process_mode_reports_a_pid_that_is_not_this_interpreter(self):
        """A real OS child answers, and its PID corroborates three ways.

        If the runtime silently fell back to threads this goes red on the first
        assertion -- which is the point.
        """
        observation = prove.observe_process_mode(str(_REPO_ROOT))
        self.assertEqual(observation.mode, prove.MODE_PROCESS)
        self.assertNotEqual(observation.worker_pid, os.getpid())
        self.assertEqual(observation.detail["runtime_pid"], observation.worker_pid)
        self.assertEqual(observation.detail["hello_pid"], observation.worker_pid)
        self.assertTrue(observation.detail["lazy_spawn"])

    def test_a_reply_carrying_no_pid_is_a_hard_failure_not_a_skip(self):
        """The quiet failure mode: the spawn happened, the PID did not come back."""
        seen = {}

        def _reply_without_a_pid(handle, query, timeout):
            seen["mode"] = handle.mode
            return "the worker said something, but not its PID"

        with mock.patch.object(prove, "_dispatch_query", _reply_without_a_pid):
            with self.assertRaises(prove.WorkerPidUnavailable):
                prove.observe_process_mode(str(_REPO_ROOT))

        self.assertEqual(
            seen.get("mode"),
            prove.MODE_PROCESS,
            "the process-mode spawn must be attempted before the PID is parsed",
        )

    def test_a_reply_pid_that_contradicts_the_spawned_process_is_a_hard_failure(self):
        """A reply stamped with *this* PID is a lie about the child; never a pass."""

        def _reply_claiming_this_process(handle, query, timeout):
            return f"{prove.SENTINEL}:pid={os.getpid()}:{prove.SENTINEL}"

        with mock.patch.object(prove, "_dispatch_query", _reply_claiming_this_process):
            with self.assertRaises(prove.WorkerPidUnavailable):
                prove.observe_process_mode(str(_REPO_ROOT))


if __name__ == "__main__":
    unittest.main()
