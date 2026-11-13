# tests/test_worker_is_a_real_process.py
"""Is a worker's mode a fact about the substrate, or just a label?

The law
-------
A runtime mode is a *claim about where the code runs*. This file states that
claim once, as one helper (:func:`assert_the_mode_is_answered_by_its_substrate`),
and then feeds the helper two kinds of handle: the one the production spawn path
really builds, and one that lies about its mode. A law that only ever sees the
honest case proves nothing, so the lying handle is not a hypothetical -- it is
the red test for the helper itself.

What the three tests are
------------------------
1. ``test_the_default_spawn_path_is_answered_by_the_substrate_it_declares`` --
   with ``WORKER_RUNTIME_MODE`` deleted from the environment, drive the REAL
   ``Worker._action_spawn`` (``tools/workspace/worker_thread.py``, the
   ``attach_thread_frame_sink(thread)`` line), dispatch a query through the
   handle it built, and read the PID the stand-in worker stamped with
   ``os.getpid()``. The measurement is not assumed to be thread mode or process
   mode: it branches on ``handle.mode`` and then holds the handle to the law for
   *that* mode, which is what makes the test survive the default flipping. It
   also asserts the law rejects the mirror mode for the same measurement -- if
   that assertion could not fail, the branch above could always pick the
   vacuous side and pass.
2. ``test_the_law_bites_a_handle_that_lies_about_its_mode`` -- a synthetic
   handle that declares ``process`` and answers in this interpreter must be
   rejected by the shared helper.
3. ``test_the_source_of_truth_constant_is_what_the_default_path_resolves`` --
   with the flag unset, the mode the spawn path resolves must be
   ``DEFAULT_RUNTIME_MODE`` (and ``resolve_runtime_mode()`` must agree), so the
   constant is the source of truth only because the spawn path reaches it.

No ``skipif`` anywhere: a platform that cannot create a process cannot satisfy
the law, and that is a red test with a message worth reading -- the same rule
tests/test_worker_pid_isolation.py applies to the proof script. Hermetic: no
LLM, vault, docker or network, and at most one OS process at a time. The
production spawn site, the stand-in worker and the child-side factory are
imported from tests/test_worker_process_e2e.py rather than copied, so there is
exactly one fake of the spawn site and it cannot drift between the two files.
"""

from __future__ import annotations

import multiprocessing
import os
from typing import Any, List

import pytest

import tools.workspace.worker as worker_module
from tools.workspace.worker_runtime import (
    DEFAULT_RUNTIME_MODE,
    MODE_PROCESS,
    MODE_THREAD,
    WORKER_RUNTIME_MODE_ENV,
    resolve_runtime_mode,
)
from tests.test_worker_process_e2e import (
    SENTINEL,
    WORKER,
    _drive_the_production_spawn_site,
    _pid_from_reply,
)


# ─────────────────────────────────────────────────────────────────────────────
# The law, in one place, used by every test below
# ─────────────────────────────────────────────────────────────────────────────


def _owns_a_real_child_process(handle: Any) -> bool:
    """Whether *handle* actually holds a live ``multiprocessing`` child.

    A ``process``-mode runtime that answered a query owns one; a label over an
    in-process answer owns nothing. This is a fact about the object, not about
    its mode string.
    """

    runtime = getattr(handle, "_process_runtime", None)
    if runtime is None:
        return False
    return any(
        isinstance(value, multiprocessing.process.BaseProcess)
        for value in vars(runtime).values()
    )


def assert_the_mode_is_answered_by_its_substrate(
    declared_mode: str,
    *,
    reply_pid: int,
    parent_pid: int,
    child_process_exists: bool,
) -> None:
    """THE LAW: a declared mode must be answered by the substrate it names.

    ``process`` promises the worker body ran outside this interpreter, so the
    PID the body stamped must not be ``os.getpid()`` *and* the handle must own a
    real child process. ``thread`` promises the opposite, so both halves invert.
    Anything else is not a mode this build implements.
    """

    if declared_mode == MODE_PROCESS:
        assert reply_pid != parent_pid, (
            f"the handle declares {MODE_PROCESS!r}, but the worker body answered "
            f"from PID {reply_pid}, which is this interpreter (parent PID "
            f"{parent_pid}): the 'child process' is a thread wearing a "
            "process-mode label"
        )
        assert child_process_exists, (
            f"the handle declares {MODE_PROCESS!r} and answered from another PID, "
            "but it owns no multiprocessing child: nothing was spawned, so "
            "'process' names nothing"
        )
        return

    if declared_mode == MODE_THREAD:
        assert reply_pid == parent_pid, (
            f"the handle declares {MODE_THREAD!r}, but the worker body answered "
            f"from PID {reply_pid} instead of this interpreter's {parent_pid}: "
            "the mode understates the isolation it claims"
        )
        assert not child_process_exists, (
            f"the handle declares {MODE_THREAD!r}, but it owns a live "
            "multiprocessing child: the mode understates the isolation it claims"
        )
        return

    raise AssertionError(
        f"{declared_mode!r} is not one of ({MODE_THREAD!r}, {MODE_PROCESS!r}): "
        "there is no substrate to hold a mode this build does not implement to"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. The default spawn path, measured against the law for its own mode
# ─────────────────────────────────────────────────────────────────────────────


def test_the_default_spawn_path_is_answered_by_the_substrate_it_declares(
    tmp_path, monkeypatch
):
    """Flag unset: whatever mode the spawn site resolves must be true of it.

    This is the test that makes the default a measured fact instead of a
    printed constant. It does not care *which* mode the default is -- it cares
    that the mode the production path resolved is the mode the answer came
    from.
    """

    monkeypatch.delenv(WORKER_RUNTIME_MODE_ENV, raising=False)
    assert WORKER_RUNTIME_MODE_ENV not in os.environ, (
        "precondition: the flag must genuinely be absent, or this measures the "
        "operator's environment instead of the shipped default"
    )

    parent_pid = os.getpid()
    created: List[Any] = []
    session_id, result, fake = _drive_the_production_spawn_site(
        tmp_path, monkeypatch, created
    )
    handle = fake._runtime_handle
    try:
        assert result.get("spawned") is True, f"the spawn itself failed: {result!r}"
        assert handle.mode in (MODE_PROCESS, MODE_THREAD), (
            f"the production spawn site resolved {handle.mode!r}, which is not a "
            "mode this build implements"
        )

        reply = handle.dispatch_query(SENTINEL, timeout=30)
        reply_pid = _pid_from_reply(reply)
        child_process_exists = _owns_a_real_child_process(handle)

        # Branch on the mode the SPAWN PATH resolved -- not on one this test
        # picked -- so the same file keeps testing the truth after C62 flips
        # the default.
        if handle.mode == MODE_PROCESS:
            assert reply_pid != parent_pid, (
                "the default resolved 'process', but the worker body answered "
                f"from PID {reply_pid} == this interpreter: the default mode is "
                "a label, not a substrate"
            )
            assert child_process_exists, (
                "the default resolved 'process' and answered from another PID, "
                "but no multiprocessing child is held: nothing was spawned"
            )
        else:
            assert reply_pid == parent_pid, (
                "the default resolved 'thread', but the worker body answered from "
                f"PID {reply_pid} instead of this interpreter's {parent_pid}: the "
                "default understates the isolation it claims"
            )
            assert not child_process_exists, (
                "the default resolved 'thread', but a child process is held: the "
                "default understates the isolation it claims"
            )

        # The law itself, not just the hand-written branch above.
        assert_the_mode_is_answered_by_its_substrate(
            handle.mode,
            reply_pid=reply_pid,
            parent_pid=parent_pid,
            child_process_exists=child_process_exists,
        )

        # ...and the law has teeth: the mirror mode must reject this very
        # measurement. Without this, the branch above could always fall on a
        # vacuous side and the test would pass over any handle at all.
        with pytest.raises(AssertionError):
            assert_the_mode_is_answered_by_its_substrate(
                MODE_THREAD if handle.mode == MODE_PROCESS else MODE_PROCESS,
                reply_pid=reply_pid,
                parent_pid=parent_pid,
                child_process_exists=child_process_exists,
            )
    finally:
        handle.close(grace_ms=2000)
        worker_module._worker_registry.pop((session_id, WORKER, 1), None)


# ─────────────────────────────────────────────────────────────────────────────
# 2. The law bites a handle that lies about its mode
# ─────────────────────────────────────────────────────────────────────────────


class _LyingHandle:
    """Declares ``process`` and answers from this interpreter.

    The shape a silent regression would take: the operator is told "process",
    the code runs on a thread, and the reply looks completely normal.
    """

    def __init__(self, reply_pid: int) -> None:
        self.mode = MODE_PROCESS
        self._process_runtime = None
        self._reply_pid = reply_pid

    def dispatch_query(self, query: str, timeout: float = 30.0) -> str:
        return f"{SENTINEL}:pid={self._reply_pid}:{query}"


def test_the_law_bites_a_handle_that_lies_about_its_mode():
    """A ``process`` label over an in-process answer must raise, not pass."""

    parent_pid = os.getpid()
    handle = _LyingHandle(reply_pid=parent_pid)
    reply_pid = _pid_from_reply(handle.dispatch_query(SENTINEL, timeout=30))

    assert handle.mode == MODE_PROCESS, "the lie is the whole point of this test"
    assert reply_pid == parent_pid, (
        "the stand-in must answer in this process for the lie to be a lie"
    )
    assert not _owns_a_real_child_process(handle), (
        "a handle that answered in-process must own no child process"
    )

    with pytest.raises(AssertionError) as excinfo:
        assert_the_mode_is_answered_by_its_substrate(
            handle.mode,
            reply_pid=reply_pid,
            parent_pid=parent_pid,
            child_process_exists=_owns_a_real_child_process(handle),
        )
    assert "process" in str(excinfo.value), (
        "the rejection must name the mode that lied: "
        f"{str(excinfo.value)!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. The constant is only the source of truth because the spawn path reaches it
# ─────────────────────────────────────────────────────────────────────────────


def test_the_source_of_truth_constant_is_what_the_default_path_resolves(
    tmp_path, monkeypatch
):
    """Flag unset: ``DEFAULT_RUNTIME_MODE`` == what the spawn site resolves."""

    monkeypatch.delenv(WORKER_RUNTIME_MODE_ENV, raising=False)
    assert WORKER_RUNTIME_MODE_ENV not in os.environ, (
        "precondition: the flag must genuinely be absent"
    )

    created: List[Any] = []
    session_id, result, fake = _drive_the_production_spawn_site(
        tmp_path, monkeypatch, created
    )
    handle = fake._runtime_handle
    try:
        assert result.get("spawned") is True, f"the spawn itself failed: {result!r}"
        assert handle.mode == DEFAULT_RUNTIME_MODE, (
            f"tools/workspace/worker_runtime.py:DEFAULT_RUNTIME_MODE is "
            f"{DEFAULT_RUNTIME_MODE!r}, but the production spawn site "
            "('tools/workspace/worker_thread.py', attach_thread_frame_sink(thread)) "
            f"resolved {handle.mode!r} with {WORKER_RUNTIME_MODE_ENV} unset: the "
            "constant is not the source of truth unless the spawn path reaches it"
        )
        assert resolve_runtime_mode({}) == DEFAULT_RUNTIME_MODE, (
            "resolve_runtime_mode({}) -- the rule the spawn path applies -- must "
            f"return DEFAULT_RUNTIME_MODE ({DEFAULT_RUNTIME_MODE!r}) when the flag "
            "is absent"
        )
    finally:
        handle.close(grace_ms=2000)
        worker_module._worker_registry.pop((session_id, WORKER, 1), None)
