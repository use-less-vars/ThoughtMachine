"""A ``WorkerThread.send_query`` WAIT is interruptible; the QUERY is not.

``send_query`` blocks the turn-running thread on the worker's reply, so a pause
(or a turn cancel) raised while it waits used to sit invisible for the whole
deadline -- 300 s + 60 s grace -- and the turn was finally reported as
``timeout`` (audit D16).  The wait is now sliced by
``QUERY_WAIT_SLICE_SECONDS`` and each slice re-reads the caller's signals, so it
returns a structured ``status='interrupted'`` envelope within one slice.  Only
the WAIT ends: the worker may still be running the query, and with no signal
bound (tests / direct use) the full-deadline wait is unchanged.

Hermetic: no real worker thread, no LLM, no Docker, no network.  The input
queue's ``put`` is stubbed, so no reply is ever produced.

Run:  python3 -m pytest tests/test_worker_send_query_interrupt.py -q
"""

import json
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.sleep_tool import (  # noqa: E402
    bind_pause_event,
    bind_turn_cancel,
    unbind,
)
from tools.workspace.worker_thread import WorkerThread  # noqa: E402

WORKER = "w-send"


def _thread(tmp_path: Path) -> WorkerThread:
    """A non-running WorkerThread: nothing ever answers its input queue."""
    return WorkerThread(
        name=WORKER,
        definition={},
        agent_config={},
        workspace_dir=tmp_path,
    )


def _silent_put(monkeypatch, thread: WorkerThread) -> list:
    """Stub the input queue so queued queries are captured but never answered."""
    sent = []

    def fake_put(item):
        sent.append(item)

    monkeypatch.setattr(thread._input_queue, "put", fake_put)
    return sent


def _run(fn, bindings=()):
    """Run *fn* on a fresh thread (binding the given signals there first).

    Returns ``(runner, out)`` where ``out`` holds ``(kind, value)`` -- ``kind``
    is ``"ok"`` or ``"raised"``.  The thread's bindings are dropped in a
    ``finally`` so nothing leaks into another test.
    """
    out = []

    def _target():
        for bind in bindings:
            bind()
        try:
            try:
                out.append(("ok", fn()))
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                out.append(("raised", exc))
        finally:
            unbind()

    runner = threading.Thread(target=_target, daemon=True)
    runner.start()
    return runner, out


def test_pause_interrupts_a_blocked_send_query(tmp_path, monkeypatch):
    """A pause lands within one slice instead of at the deadline (D16)."""
    thread = _thread(tmp_path)
    sent = _silent_put(monkeypatch, thread)
    pause_event = threading.Event()  # cleared = running, set = pause requested

    started = time.monotonic()
    runner, out = _run(
        lambda: thread.send_query("hi", timeout=30),
        bindings=[lambda: bind_pause_event(pause_event)],
    )
    time.sleep(0.3)  # let the wait enter a slice
    pause_event.set()
    runner.join(timeout=10)
    elapsed = time.monotonic() - started

    assert out and out[0][0] == "ok", out
    payload = json.loads(out[0][1])
    assert payload["status"] == "interrupted"
    assert payload["interrupted"] is True
    assert payload["reason"] == "paused"
    assert payload["worker_name"] == WORKER
    # The envelope names the query it abandoned, so the caller can correlate.
    assert sent and payload["query_id"] == sent[0][0]
    assert elapsed < 5.0, f"pause took {elapsed:.2f}s to unblock a 30s wait"


def test_turn_cancel_interrupts_a_blocked_send_query(tmp_path, monkeypatch):
    """detach()/kill() stop the WAIT; the reason says which signal fired."""
    thread = _thread(tmp_path)
    sent = _silent_put(monkeypatch, thread)
    token = threading.Event()

    started = time.monotonic()
    runner, out = _run(
        lambda: thread.send_query("hi", timeout=30),
        bindings=[lambda: bind_turn_cancel(token)],
    )
    time.sleep(0.3)
    token.set()
    runner.join(timeout=10)
    elapsed = time.monotonic() - started

    assert out and out[0][0] == "ok", out
    payload = json.loads(out[0][1])
    assert payload["status"] == "interrupted"
    assert payload["interrupted"] is True
    assert payload["reason"] == "turn_cancelled"
    assert payload["query_id"] == sent[0][0]
    assert elapsed < 5.0, f"cancel took {elapsed:.2f}s to unblock a 30s wait"


def test_bound_but_unset_signals_do_not_interrupt(tmp_path, monkeypatch):
    """Polarity guard: cleared events mean "running", so the wait times out."""
    thread = _thread(tmp_path)
    _silent_put(monkeypatch, thread)
    pause_event = threading.Event()
    token = threading.Event()

    runner, out = _run(
        lambda: thread.send_query("hi", timeout=0.3),
        bindings=[
            lambda: bind_pause_event(pause_event),
            lambda: bind_turn_cancel(token),
        ],
    )
    runner.join(timeout=10)

    assert out and out[0][0] == "raised", out
    assert isinstance(out[0][1], TimeoutError)


def test_unbound_wait_still_times_out(tmp_path, monkeypatch):
    """No binding (direct use): the full-deadline wait is unchanged."""
    thread = _thread(tmp_path)
    _silent_put(monkeypatch, thread)

    started = time.monotonic()
    runner, out = _run(lambda: thread.send_query("hi", timeout=0.3))
    runner.join(timeout=10)
    elapsed = time.monotonic() - started

    assert out and out[0][0] == "raised", out
    assert isinstance(out[0][1], TimeoutError)
    assert elapsed >= 0.25, f"timed out after {elapsed:.2f}s of a 0.3s wait"
