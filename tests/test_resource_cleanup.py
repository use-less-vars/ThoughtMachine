"""Resource-cleanup tests for the controller thread's private stream loop.

``agent.core.agent._iter_stream_events`` drives streaming adapters on a
per-thread event loop kept in the thread-local ``_STREAM_LOCAL``. Until now
that loop was never closed and the provider's async HTTP client was never
closed either, so every finished session leaked one loop (and its self-pipe
fds) plus one ``httpx`` connection pool for the lifetime of the process - the
console/server is long-lived and cannot be restarted cheaply.

The cleanup has two hard constraints, and these tests pin both:

* The async client belongs to the loop it was first used on, so it must be
  closed *by* that loop (``run_until_complete``) before ``loop.close()``, and
  the thread-local must be dropped afterwards. Closing the loop while leaving
  ``_STREAM_LOCAL.loop`` pointing at the corpse is the trap: the next turn on
  the same thread would either drive a closed loop or, worse, reuse a client
  whose pool is attached to it.
* Cleanup runs from a ``finally`` at thread exit, so it must be
  exception-safe (never replace the exception that ended the thread) and
  idempotent (a second call must not blow up).
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent.core.agent import _STREAM_LOCAL, _close_stream_resources, _iter_stream_events


async def _one_chunk():
    """Minimal async-generator stream: enough to create the thread's loop."""
    yield {'type': 'text_delta', 'text': 'chunk'}


class _FakeAsyncClient:
    """Stand-in for ``AsyncOpenAI``: only ``close()`` matters here."""

    def __init__(self, raises=None):
        self.closed = 0
        self.raises = raises

    async def close(self):
        self.closed += 1
        if self.raises is not None:
            raise self.raises


class _FakeProvider:
    """Stand-in for ``OpenAICompatibleProvider``'s close surface."""

    def __init__(self, client):
        self._async_client = client
        self.aclose_calls = 0

    async def aclose(self):
        self.aclose_calls += 1
        client = getattr(self, '_async_client', None)
        self._async_client = None
        if client is not None:
            await client.close()


def _run_in_thread(worker, timeout=10):
    """Run ``worker`` on a fresh thread; return (result, errors).

    A dedicated thread is load-bearing everywhere in this file: the loop under
    test is thread-local, so the test body must not pollute (or be polluted by)
    the pytest thread's own stream loop.
    """
    results = []
    errors = []

    def _target():
        try:
            results.append(worker())
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
            errors.append(exc)

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), 'cleanup worker thread hung'
    return (results[0] if results else None), errors


def test_provider_async_client_is_closed_on_the_loop_that_owns_it():
    """The provider's async client is closed, driven by its own loop."""
    client = _FakeAsyncClient()
    provider = _FakeProvider(client)

    def _worker():
        list(_iter_stream_events(_one_chunk()))
        return _close_stream_resources(provider)

    closed, errors = _run_in_thread(_worker)

    assert errors == [], f'cleanup raised: {errors!r}'
    assert closed is True, 'cleanup reported nothing to do although a loop existed'
    assert provider.aclose_calls == 1, 'provider.aclose() was not invoked exactly once'
    assert client.closed == 1, 'the async client was not closed'
    assert provider._async_client is None, (
        'the async client reference must be dropped so a later turn rebuilds a '
        'client bound to the new loop'
    )


def test_stream_loop_is_closed_after_the_run_exits():
    """After the run exits the thread's stream loop is closed and forgotten."""
    captured = {}

    def _worker():
        list(_iter_stream_events(_one_chunk()))
        captured['loop'] = _STREAM_LOCAL.loop
        _close_stream_resources()
        captured['after'] = getattr(_STREAM_LOCAL, 'loop', None)
        return True

    _, errors = _run_in_thread(_worker)

    assert errors == [], f'cleanup raised: {errors!r}'
    assert captured['loop'].is_closed() is True, 'the stream loop was left open'
    assert captured['after'] is None, (
        'the thread-local still points at the closed loop; the next turn would '
        'find a corpse instead of building a fresh loop'
    )


def test_second_run_on_the_same_thread_gets_a_fresh_usable_loop():
    """A second run after cleanup must work: no ``Event loop is closed``.

    This is the regression the cleanup order exists for. Closing the loop
    without invalidating ``_STREAM_LOCAL`` (or closing the client on a loop
    that is already closed) breaks the *next* turn on the same thread, which is
    exactly what a keep-alive controller thread does.
    """
    captured = {}

    def _worker():
        list(_iter_stream_events(_one_chunk()))
        captured['first'] = _STREAM_LOCAL.loop
        _close_stream_resources()

        events = list(_iter_stream_events(_one_chunk()))
        captured['second'] = _STREAM_LOCAL.loop
        return events

    events, errors = _run_in_thread(_worker)

    assert errors == [], f'a second run on the same thread failed: {errors!r}'
    assert events == [{'type': 'text_delta', 'text': 'chunk'}]
    assert captured['first'].is_closed() is True
    assert captured['second'] is not captured['first'], 'the loop was not rebuilt'
    assert captured['second'].is_closed() is False, 'the fresh loop is unusable'


def test_cleanup_is_idempotent():
    """Calling cleanup twice does not raise and does not re-close anything."""
    client = _FakeAsyncClient()
    provider = _FakeProvider(client)

    def _worker():
        list(_iter_stream_events(_one_chunk()))
        return _close_stream_resources(provider), _close_stream_resources(provider)

    outcome, errors = _run_in_thread(_worker)

    assert errors == [], f'second cleanup raised: {errors!r}'
    assert outcome == (True, False), 'the second call should be a no-op'
    assert provider.aclose_calls == 1
    assert client.closed == 1


def test_cleanup_failure_does_not_mask_the_original_exception():
    """A failing close is swallowed; the exception leaving the run survives."""
    client = _FakeAsyncClient(raises=RuntimeError('close exploded'))
    provider = _FakeProvider(client)

    class _TurnFailed(Exception):
        pass

    def _worker():
        list(_iter_stream_events(_one_chunk()))
        try:
            raise _TurnFailed('turn failed')
        finally:
            _close_stream_resources(provider)

    _, errors = _run_in_thread(_worker)

    assert len(errors) == 1, f'expected exactly one exception, got {errors!r}'
    assert isinstance(errors[0], _TurnFailed), (
        f'the cleanup error replaced the real exception: {errors[0]!r}'
    )
    assert str(errors[0]) == 'turn failed'
    assert client.closed == 1, 'the failing close was not attempted'


def test_controller_run_closes_the_thread_stream_loop():
    """``AgentController._run``'s ``finally`` is wired to the cleanup.

    The thread lifecycle is what makes the cleanup safe: ``_run`` owns the
    thread that owns the loop, so the ``finally`` runs on the right thread with
    the right thread-local. ``_keep_alive = False`` shortcuts the main loop so
    the finally is reached immediately.
    """
    from agent.controller import AgentController

    client = _FakeAsyncClient()
    provider = _FakeProvider(client)
    captured = {}

    def _worker():
        list(_iter_stream_events(_one_chunk()))
        captured['loop'] = _STREAM_LOCAL.loop

        controller = AgentController()
        fake_agent = SimpleNamespace(
            config=SimpleNamespace(stop_check=None),
            halt_check=None,
            session=None,
            conversation=[],
            session_id='cleanup-test-session',
        )
        fake_agent.llm_client = SimpleNamespace(provider=provider)
        controller._agent_override = fake_agent
        controller._keep_alive = False  # exit the main loop immediately
        controller._run()
        captured['after'] = getattr(_STREAM_LOCAL, 'loop', None)

    _, errors = _run_in_thread(_worker)

    assert errors == [], f'_run raised: {errors!r}'
    assert provider.aclose_calls == 1, (
        '_run did not close the async client; the finally is not wired to the cleanup'
    )
    assert captured['loop'].is_closed() is True
    assert captured['after'] is None
