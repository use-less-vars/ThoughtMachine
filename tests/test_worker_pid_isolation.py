# tests/test_worker_pid_isolation.py
"""scripts/prove_worker_pid.py must FAIL when it cannot obtain the worker's PID.

Why this file exists
--------------------
The proof script answers one question -- *which OS process runs the worker's
code?* -- and it answers it by cross-checking several independent witnesses:
the ``hello`` frame's pid (computed by the child), the pid ``multiprocessing``
gave the child ``Process``, ``/proc/<pid>``, and the shipped ``Worker`` tool's
own ``inspect`` row.  That design has one failure mode worth guarding, and it
is the quiet one: a proof that cannot obtain the PID must never pass, and must
never be skipped.  "I could not see where the worker ran" and "the worker ran
in its own process" are different statements, and a test suite that cannot
tell them apart is worse than no test at all.

What is covered
---------------
1. The witness readers (``hello_pid``, ``multiprocessing_pid``,
   ``proc_evidence``): each of them, when it cannot read its witness, returns
   ``None`` plus a line containing ``NOT MEASURED`` -- never a plausible
   default that would flow into a verdict.
2. The verdicts themselves, as the script now reports them: distinct exit
   codes.  The thread layer is *measured* and says "no child exists"; the
   process layer is *proven* by a real spawned child, and the pid the child
   computed is asserted to equal the pid the machinery reports and to differ
   from ``os.getpid()``.
3. The honesty contract, driven against a REAL child: if the machinery cannot
   name the pid (or names the wrong one), the run must come back
   ``EXIT_UNMEASURED`` -- never ``EXIT_PROVEN``.  No skip: a platform that
   cannot spawn a child cannot satisfy the contract, and that is a red test.

No stand-in worker is used anywhere
-----------------------------------
The script used to install a sentinel ``worker_factory`` that resolved, inside
the child, to a canned-reply stub, so ``WorkerThread.__init__`` never ran there
and the printed numbers were real OS facts about a puppet body.  That factory
is gone, and this file must not bring it back: every test that spawns drives
``build_real_worker()``'s real ``WorkerThread`` through the real
``attach_thread_frame_sink``.  Where a test needs an *unreadable* witness it
degrades the parent's view only -- ``_BlindedHandle`` proxies a real handle and
rewrites one entry of ``substrate()``; the child stays a real interpreter
running a real ``WorkerThread``.  ``TestNoStandInWorkerIsEverInstalled`` pins
that as an assertion.

Hermetic: no agent, no LLM call, no tool loop, no vault, no docker, no network.
The only real work is one ``multiprocessing`` spawn per spawn test.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.prove_worker_pid as prove  # noqa: E402 - path fixed above

#: Above ``/proc/sys/kernel/pid_max`` on Linux, so no process can ever have it.
_IMPOSSIBLE_PID = 2**31 - 1


def _fresh_workspace(test: unittest.TestCase) -> Path:
    """A throwaway workspace the script's workers may write into.

    Never deleted inside the test body: a parent-side ``WorkerThread`` and a
    spawned child both keep writing ``workers/<label>/`` after ``run_layer``
    returns, so an eager rmtree races them (and the race lands on the test, not
    on the code under test).  ``ignore_errors`` on the way out is enough.
    """
    path = Path(tempfile.mkdtemp(prefix="prove-pid-test-"))
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def _isolated_identity(test: unittest.TestCase) -> dict:
    """A per-test worker identity.

    ``WorkerThread`` allocates its ``_generation`` per worker directory and the
    in-process registry is keyed by ``(session_id, worker_name, instance_id)``,
    so two measurements sharing a workspace or an identity would fence each
    other's commands out.  Each test therefore gets its own.
    """
    tag = test.id().rsplit(".", 1)[-1].replace("_", "-")
    return {
        "WORKER_NAME": f"{prove.WORKER_NAME}-{tag}",
        "SESSION_ID": f"{prove.SESSION_ID}-{tag}",
    }


class _BlindedHandle:
    """A parent-side PROXY around a real runtime handle.

    Only ``substrate()`` is rewritten; everything else is delegated, so the
    spawn, the Ping/Pause round trip and the child-side ``WorkerThread`` are
    the real ones.  This is how an unreadable or lying PID witness is simulated
    without introducing a stand-in worker anywhere.
    """

    def __init__(self, handle, blind):
        object.__setattr__(self, "_real_handle", handle)
        object.__setattr__(self, "_blind", blind)

    def substrate(self):
        return self._blind(dict(self._real_handle.substrate()))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real_handle"), name)


def _blind_child_pid(substrate):
    """The machinery reports a child but refuses to name its pid."""
    substrate["child_pid"] = None
    return substrate


def _claim_this_process(substrate):
    """The machinery names *this* interpreter's pid as the child's."""
    substrate["child_pid"] = os.getpid()
    substrate["child_exists"] = True
    substrate["child_alive"] = True
    return substrate


class TestWitnessReadersNeverInventAPid(unittest.TestCase):
    """A witness that cannot be read says so; it never returns a number."""

    def test_hello_pid_reads_both_frame_shapes_the_handle_carries(self):
        self.assertEqual(
            prove.hello_pid(SimpleNamespace(hello=SimpleNamespace(pid=4711))),
            (4711, "handle.hello"),
        )
        self.assertEqual(
            prove.hello_pid(
                SimpleNamespace(
                    _process_runtime=SimpleNamespace(hello=SimpleNamespace(pid=24))
                )
            ),
            (24, "handle._process_runtime.hello"),
        )

    def test_a_non_int_hello_pid_is_not_measured(self):
        pid, why = prove.hello_pid(SimpleNamespace(hello=SimpleNamespace(pid="4711")))
        self.assertIsNone(pid)
        self.assertIn(prove.NOT_MEASURED, why)

    def test_a_handle_with_no_hello_frame_is_not_measured(self):
        pid, why = prove.hello_pid(SimpleNamespace())
        self.assertIsNone(pid)
        self.assertIn(prove.NOT_MEASURED, why)

    def test_multiprocessing_pid_reads_both_process_shapes(self):
        self.assertEqual(
            prove.multiprocessing_pid(
                SimpleNamespace(_process_runtime=SimpleNamespace(pid=24))
            ),
            (24, "handle._process_runtime.pid"),
        )
        self.assertEqual(
            prove.multiprocessing_pid(
                SimpleNamespace(
                    _process_runtime=SimpleNamespace(
                        _process=SimpleNamespace(pid=25)
                    )
                )
            ),
            (25, "handle._process_runtime._process.pid"),
        )

    def test_a_proc_object_without_an_int_pid_is_not_measured(self):
        pid, why = prove.multiprocessing_pid(
            SimpleNamespace(_process_runtime=SimpleNamespace(pid="24"))
        )
        self.assertIsNone(pid)
        self.assertIn(prove.NOT_MEASURED, why)

    def test_proc_evidence_speaks_for_a_live_and_an_absent_pid(self):
        self.assertIn(f"/proc/{os.getpid()} exists", prove.proc_evidence(os.getpid()))
        self.assertIn("NOT live", prove.proc_evidence(_IMPOSSIBLE_PID))
        self.assertIn(prove.NOT_MEASURED, prove.proc_evidence(None))


class TestNoStandInWorkerIsEverInstalled(unittest.TestCase):
    """The child must build the real WorkerThread, never a canned-reply puppet."""

    def test_the_object_the_child_would_build_is_a_real_worker_thread(self):
        with tempfile.TemporaryDirectory() as td:
            worker = prove.build_real_worker(Path(td))
            self.assertEqual(type(worker).__name__, "WorkerThread")
            self.assertIsNone(
                prove.spec_from_worker(worker).get("worker_factory"),
                "a worker factory is installed again; the child would build a stand-in",
            )
            self.assertFalse(hasattr(worker, "_runtime_worker_factory"))
            lines = prove.assert_no_factory(worker)  # raises if either is back
            self.assertTrue(
                any("worker_factory'] = None" in line for line in lines), lines
            )
            self.assertIsInstance(worker.workspace_dir, Path)

    def test_the_real_constructor_coerces_a_str_workspace_dir(self):
        with tempfile.TemporaryDirectory() as td:
            lines = prove.assert_str_workspace_dir_coercion(Path(td))
        self.assertTrue(any("pathlib.Path: True" in line for line in lines), lines)


class TestTheProofIsDrivenAndCannotPassVacuously(unittest.TestCase):
    """Real threads and real children; "not measured" is its own verdict."""

    def test_exit_codes_are_distinct_and_labelled(self):
        codes = {prove.EXIT_PROVEN, prove.EXIT_SAME_PROCESS, prove.EXIT_UNMEASURED}
        self.assertEqual(len(codes), 3)
        self.assertEqual(len({prove._exit_label(code) for code in codes}), 3)

    def test_thread_layer_reports_no_child_rather_than_a_pid(self):
        """The shipped thread mode is measured, and it is *not* a proof."""
        with mock.patch.multiple(prove, **_isolated_identity(self)):
            result = prove.run_layer("thread", prove.MODE_THREAD, _fresh_workspace(self))
        text = "\n".join(result.lines)
        self.assertEqual(result.exit_code, prove.EXIT_SAME_PROCESS, text)
        self.assertIn("child_exists = False", text)
        self.assertIn("NO CHILD PROCESS EXISTS", text)
        self.assertNotIn("PROVEN", text)

    def test_process_layer_proves_a_different_live_child(self):
        """A real OS child answers, and the child-computed pid corroborates it."""
        with mock.patch.multiple(prove, **_isolated_identity(self)):
            result = prove.run_layer("process", prove.MODE_PROCESS, _fresh_workspace(self))
        text = "\n".join(result.lines)
        self.assertEqual(result.exit_code, prove.EXIT_PROVEN, text)
        self.assertIn("finding: PROVEN", text)

        verdict = re.search(r"child_pid=(\d+) parent_pid=(\d+) same_process=(\S+)", text)
        self.assertIsNotNone(verdict, text)
        child_pid, parent_pid = int(verdict.group(1)), int(verdict.group(2))
        self.assertNotEqual(child_pid, parent_pid)
        self.assertEqual(parent_pid, os.getpid())
        self.assertEqual(verdict.group(3), "False")

        hello = re.search(r"witness hello\.pid\s+= (\S+)", text)
        self.assertIsNotNone(hello, text)
        self.assertEqual(hello.group(1), str(child_pid))
        self.assertIn("exists, comm=", text)

    def test_main_reports_the_thread_layer_as_no_fork(self):
        """The top-level aggregation cannot turn "no child" into a proof."""
        with mock.patch.multiple(prove, **_isolated_identity(self)):
            self.assertEqual(
                prove.main(["--layer", "thread"]), prove.EXIT_SAME_PROCESS
            )

    def test_a_proof_that_cannot_read_a_pid_never_reports_proven(self):
        """The quiet failure mode: the child spawns, no witness names its pid."""
        real_attach = prove.attach_thread_frame_sink
        seen = {}

        def _attach(worker, mode=None):
            handle = real_attach(worker) if mode is None else real_attach(worker, mode=mode)
            seen["mode"] = getattr(handle, "mode", None)
            return _BlindedHandle(handle, _blind_child_pid)

        unmeasured = (None, f"{prove.NOT_MEASURED} (blinded for this test)")
        with mock.patch.multiple(prove, **_isolated_identity(self)), \
                mock.patch.object(prove, "attach_thread_frame_sink", _attach), \
                mock.patch.object(prove, "hello_pid", lambda handle: unmeasured), \
                mock.patch.object(prove, "multiprocessing_pid", lambda handle: unmeasured), \
                mock.patch.object(prove, "inspect_reading", lambda w, h: unmeasured):
            result = prove.run_layer("process", prove.MODE_PROCESS, _fresh_workspace(self))

        text = "\n".join(result.lines)
        self.assertEqual(
            seen.get("mode"),
            prove.MODE_PROCESS,
            "the real process-mode spawn must be attempted before any pid is read",
        )
        self.assertIn(prove.NOT_MEASURED, text)
        # the child really was there and really answered: the spawn happened and
        # only the *pid witness* was lost -- that is the failure being modelled
        self.assertIn("child_exists = True", text, text)
        self.assertIn("handle.ping() = True", text, text)
        self.assertEqual(result.exit_code, prove.EXIT_UNMEASURED, text)
        self.assertNotIn("PROVEN", text)

    def test_a_child_pid_that_names_this_process_is_a_disagreement(self):
        """A pid contradicting the real child is a finding, never a pass."""
        real_attach = prove.attach_thread_frame_sink

        def _attach(worker, mode=None):
            handle = real_attach(worker) if mode is None else real_attach(worker, mode=mode)
            return _BlindedHandle(handle, _claim_this_process)

        with mock.patch.multiple(prove, **_isolated_identity(self)), \
                mock.patch.object(prove, "attach_thread_frame_sink", _attach):
            result = prove.run_layer("process", prove.MODE_PROCESS, _fresh_workspace(self))

        text = "\n".join(result.lines)
        self.assertIn("child_exists = True", text, text)
        self.assertIn("handle.ping() = True", text, text)
        self.assertIn("DISAGREEMENT", text, text)
        self.assertEqual(result.exit_code, prove.EXIT_UNMEASURED, text)
        self.assertNotIn("PROVEN", text)


class TestNothingHereCanBeSkipped(unittest.TestCase):
    """A platform that cannot spawn a child is a red test, not a green skip."""

    def test_the_module_carries_no_skip_marker(self):
        source = Path(__file__).read_text(encoding="utf-8")
        for marker in ("skip" + "if", "unittest" + ".skip", "pytest" + ".mark.skip"):
            self.assertNotIn(marker, source, f"{marker} would weaken the contract")


if __name__ == "__main__":
    unittest.main()
