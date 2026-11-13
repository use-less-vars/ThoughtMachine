"""C12 regression pin: ``cancellation.unbind()`` must clear the per-thread
fallback cancellation token.

Background
----------
``tools/cancellation.py`` keeps a per-thread ``threading.Event`` ("the
fallback") for calls made while no turn event is bound.  ``interrupt()``
sets whichever token is active, so on an unbound thread it arms the
fallback.  Before the C12 fix ``unbind()`` only dropped ``_tls.event`` and
``_tls.reason``; the fallback event stayed SET for the rest of the
thread's life, so every later unbound call on that thread (the next turn,
a direct tool call, a poll of ``is_interrupted()``) saw a stale
"cancelled" state -- a cancel from turn N poisoning turn N+1.

These tests pin the fix and, just as importantly, pin its *limits*:

* a BOUND turn token must never be cleared by ``unbind()`` -- the owning
  worker owns the lifetime of that event, and clearing it could resurrect
  a token another component still holds;
* one thread's interrupt must never reach another thread (the whole point
  of the thread-local design).
"""

from __future__ import annotations

import threading

import pytest

from tools import cancellation


@pytest.fixture(autouse=True)
def _clean_cancel_token():
    """Start and end every test from a pristine per-thread cancel state."""
    cancellation.unbind()
    cancellation.reset()
    try:
        yield
    finally:
        cancellation.unbind()
        cancellation.reset()


def test_unbound_interrupt_arms_the_fallback():
    """With no turn token bound, ``interrupt()`` arms the fallback token.

    This is the documented by-design behaviour and must keep working after
    the C12 fix: an unbound interrupt still has to be observable.
    """
    cancellation.interrupt("x")
    assert cancellation.is_interrupted() is True  # catches: C12 - unbound interrupt must still arm the fallback


def test_unbind_clears_the_sticky_fallback():
    """THE PIN: an unbound interrupt must not survive ``unbind()``.

    ``unbind()`` is the turn-teardown hook.  If the fallback armed by
    ``interrupt()`` outlives it, the very next turn on this thread starts
    already-cancelled.
    """
    cancellation.interrupt("stopped_by_operator")
    cancellation.unbind()
    assert cancellation.is_interrupted() is False  # catches: C12 - sticky fallback event survives unbind()


def test_unbind_clears_cancel_reason():
    """``unbind()`` must also drop the reason recorded by the interrupt."""
    cancellation.interrupt("stopped_by_operator")
    cancellation.unbind()
    assert cancellation.cancel_reason() is None  # catches: C12 - interrupt reason survives unbind()


def test_bound_token_is_not_cleared_by_unbind():
    """A BOUND turn token belongs to the caller; ``unbind()`` must not clear it.

    Guards against the tempting wrong fix of calling ``_active_event().clear()``
    inside ``unbind()``: that would silently un-cancel an event another
    component (the worker's ``_turn_cancel``) still owns and may still read.
    """
    token = threading.Event()
    cancellation.bind_turn_cancel(token)
    cancellation.interrupt("stop")
    cancellation.unbind()
    assert token.is_set() is True  # catches: C12 - unbind() cleared a caller-owned turn token


def test_fallback_poison_does_not_survive_unbind_across_turns():
    """End-to-end turn simulation: turn 1 cancelled, turn 2 must run clean."""
    cancellation.interrupt("stopped_by_operator")  # turn 1 interrupted with no token bound
    cancellation.unbind()  # turn 1 teardown

    turn_2_token = threading.Event()
    cancellation.bind_turn_cancel(turn_2_token)
    assert cancellation.is_interrupted() is False  # catches: C12 - turn 1 cancel leaks into turn 2

    cancellation.unbind()  # turn 2 teardown
    assert cancellation.is_interrupted() is False  # catches: C12 - turn 1 cancel resurfaces after turn 2


def test_interrupt_with_no_token_does_not_leak_to_other_threads():
    """A fallback armed on one thread must never be visible on another."""
    results: dict[str, bool] = {}

    def worker() -> None:
        cancellation.interrupt("worker_only")
        results["worker_saw_cancel"] = cancellation.is_interrupted()
        cancellation.unbind()
        cancellation.reset()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=10)

    assert thread.is_alive() is False  # catches: C12 - worker thread hung
    assert results.get("worker_saw_cancel") is True  # catches: C12 - unbound interrupt missed its own thread
    assert cancellation.is_interrupted() is False  # catches: C12 - worker cancel leaked into the main thread
