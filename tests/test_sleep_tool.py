# tests/test_sleep_tool.py
"""Tests for the interruptible ``sleep`` tool (P0 proof for per-session tools).

The property under test is interruptibility, not sleeping: a tool that blocks
for 300 s must still return within a slice of the cancellation signal.  Every
test keeps real wall time tiny (largest single sleep is 0.3 s).

Cancellation is thread-scoped (``bind_turn_cancel``) as of W7,
so the autouse fixture resets it around each test.
"""

import json
import queue
import threading
import time

import pytest
from pydantic import ValidationError

from tools.sleep_tool import (
    MAX_SECONDS,
    Sleep,
    bind_turn_cancel,
    interrupt,
    is_interrupted,
    reset,
    unbind,
)


@pytest.fixture(autouse=True)
def _clean_interrupt():
    reset()
    yield
    reset()


def _run(**kwargs):
    return json.loads(Sleep(**kwargs).execute())


def test_registered_in_tool_registry():
    from tools import get_tool_class

    assert get_tool_class("Sleep") is Sleep


def test_full_sleep_reports_not_interrupted():
    out = _run(seconds=0.3, reason="waiting for CI")
    assert out["ok"] is True
    assert out["interrupted"] is False
    assert out["requested_seconds"] == 0.3
    assert out["elapsed_seconds"] >= 0.3
    assert out["reason"] == "waiting for CI"
    assert "error" not in out


def test_interrupt_mid_sleep_returns_promptly():
    # Thread-scoped token: the SLEEPER binds its own event and the canceller
    # sets exactly that event -- which is what kill()/detach() now do.
    token = threading.Event()

    def _fire():
        time.sleep(0.2)
        token.set()

    thread = threading.Thread(target=_fire)
    thread.start()
    bind_turn_cancel(token)
    try:
        started = time.monotonic()
        out = _run(seconds=5.0, reason="long wait")
        wall = time.monotonic() - started
    finally:
        unbind()
    thread.join()

    assert out["ok"] is False
    assert out["interrupted"] is True
    assert out["reason"] == "turn_cancelled"
    assert out["requested_reason"] == "long wait"
    assert out["elapsed_seconds"] < 5.0
    assert wall < 2.0, f"interrupt took {wall:.2f}s -- not prompt"


def test_over_cap_is_refused_not_clamped():
    out = _run(seconds=MAX_SECONDS + 1)
    assert out["ok"] is False
    assert out["interrupted"] is False
    assert "300" in out["error"]
    assert out["requested_seconds"] == MAX_SECONDS + 1


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_non_positive_seconds_refused(bad):
    out = _run(seconds=bad)
    assert out["ok"] is False
    assert out["interrupted"] is False
    assert "> 0" in out["error"]


def test_non_finite_seconds_refused():
    out = _run(seconds=float("inf"))
    assert out["ok"] is False
    assert out["interrupted"] is False


def test_non_numeric_seconds_rejected_by_schema():
    with pytest.raises(ValidationError):
        Sleep(seconds="soon")


def test_reset_clears_pending_interrupt():
    interrupt("stale")
    assert is_interrupted() is True
    reset()
    assert is_interrupted() is False
    # A stale interrupt must not abort a later, unrelated call.
    out = _run(seconds=0.15)
    assert out["interrupted"] is False


# ── W7: per-worker, turn-scoped cancellation ────────────────────────────────


def _make_stub_worker(tmp_path):
    """A ``WorkerThread`` with no session: only the attributes detach() touches."""
    from tools.workspace.worker_thread import WorkerThread

    worker = WorkerThread.__new__(WorkerThread)
    worker._turn_cancel = threading.Event()
    worker._stop_event = threading.Event()
    worker._park_event = threading.Event()
    worker._resume_event = threading.Event()
    worker._input_queue = queue.Queue()
    worker._worker_dir = tmp_path
    worker._detach_requested = False
    worker._detached_at = None
    worker.status = "running"
    worker._write_status_file = lambda: None
    worker._save_context = lambda: None
    worker._set_detached_registration = lambda flag: None
    worker._publish_global_event = lambda *a, **k: None
    return worker


def _sleep_on_thread(token, seconds=1.0):
    """Run a real ``Sleep.execute()`` on its own thread, bound to *token*."""
    holder = {}

    def _body():
        bind_turn_cancel(token)
        try:
            holder["out"] = _run(seconds=seconds)
        finally:
            unbind()

    thread = threading.Thread(target=_body)
    thread.start()
    return thread, holder


def test_cancelling_one_worker_does_not_cancel_another_worker_sleep():
    """ANTI-BLEED: the old process-global event FAILED this exact test.

    Two "workers" sleep on their own threads with their own tokens; only A's
    is cancelled. A must return early, B must run its full duration.
    """
    token_a, token_b = threading.Event(), threading.Event()
    thread_a, out_a = _sleep_on_thread(token_a)
    thread_b, out_b = _sleep_on_thread(token_b)

    time.sleep(0.2)
    token_a.set()  # cancel A ONLY
    thread_a.join(5.0)
    thread_b.join(5.0)

    assert out_a["out"]["interrupted"] is True
    assert out_a["out"]["elapsed_seconds"] < 0.8
    assert out_b["out"]["interrupted"] is False
    assert out_b["out"]["elapsed_seconds"] >= 1.0


def test_detach_cancels_an_in_flight_sleep(tmp_path):
    """Stop (= detach) must return a sleeping tool promptly, not kill it."""
    worker = _make_stub_worker(tmp_path)
    thread, holder = _sleep_on_thread(worker._turn_cancel, seconds=5.0)

    time.sleep(0.2)
    started = time.monotonic()
    worker.detach()  # exact production code path (WorkerThread.detach)
    thread.join(5.0)
    wall = time.monotonic() - started

    assert holder["out"]["interrupted"] is True
    assert wall < 2.0, f"detach did not cancel the sleep promptly ({wall:.2f}s)"
    # detach() is NOT termination: the terminal kill latch stays clear.
    assert worker._stop_event.is_set() is False
    assert worker._park_event.is_set() is True


def test_new_query_clears_turn_cancel(tmp_path):
    """A cancel from the previous turn must not abort the next query."""
    from tools.workspace.worker_thread import WorkerThread

    worker = _make_stub_worker(tmp_path)
    worker._agent = None  # the per-query clear runs BEFORE this guard
    worker._turn_cancel.set()

    WorkerThread._run_tool_loop(worker, "next query")  # production clear site

    assert worker._turn_cancel.is_set() is False
    # ...and a sleep on the same token now runs to completion.
    thread, holder = _sleep_on_thread(worker._turn_cancel, seconds=1.0)
    thread.join(5.0)
    assert holder["out"]["interrupted"] is False
    assert holder["out"]["elapsed_seconds"] >= 1.0
