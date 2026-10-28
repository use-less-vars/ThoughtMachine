"""Thread-safety regression tests for ``agent.core.agent._iter_stream_events``.

The original implementation cached ONE private event loop in the module global
``_STREAM_LOOP`` and drove it with ``run_until_complete()`` from whichever
per-session daemon thread happened to call in. Two concurrent sessions then
contended for the same loop object and the loser raised
``RuntimeError: This event loop is already running`` - surfacing in the UI as
``[SYSTEM NOTIFICATION] Error: UNEXPECTED_ERROR``.

The fix gives every thread its own private loop, so concurrent callers on
different threads cannot contend for a shared loop object, while sequential
calls on one thread keep reusing the SAME loop - each provider's async HTTP
client is bound to the loop it first ran on, so a fresh loop per call would
leave that client unusable on the next turn.
"""

import asyncio
import threading

import pytest

from agent.core.agent import _iter_stream_events


async def _yielding_stream(count=3, delay=0.05):
    """Async generator that yields ``count`` events with a real await between them.

    The ``await asyncio.sleep()`` is load-bearing: it keeps the driving loop
    busy long enough for the second thread to reach
    ``run_until_complete()`` while the first thread's loop is still running,
    which is exactly the contention the bug is about.
    """
    for index in range(count):
        await asyncio.sleep(delay)
        yield {'type': 'text_delta', 'text': f'chunk-{index}'}


def _drain(errors, barrier, results):
    try:
        barrier.wait(timeout=5)
        results.append(list(_iter_stream_events(_yielding_stream())))
    except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
        errors.append(exc)


def test_two_threads_stream_concurrently():
    """Two threads may drive two streams at the same time without contention."""
    # Warm-up: exhaust one stream in the main thread first, so that the
    # implementation's loop (cached or not) is already initialised when the
    # worker threads start. Without this, both threads could create their own
    # loop in the same check-then-set window and the shared-loop bug would go
    # unobserved. It is not load-bearing under the per-thread implementation
    # (each worker thread gets its own loop), but it costs nothing and keeps
    # the test honest for any future shared-loop design.
    assert list(_iter_stream_events(_yielding_stream(count=2))) == [
        {'type': 'text_delta', 'text': 'chunk-0'},
        {'type': 'text_delta', 'text': 'chunk-1'},
    ]

    barrier = threading.Barrier(2)
    errors = []
    results = []
    threads = [
        threading.Thread(target=_drain, args=(errors, barrier, results))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads), 'a streaming thread hung'
    assert errors == [], f'streaming raised in a worker thread: {errors!r}'
    assert len(results) == 2
    for events in results:
        assert [event['text'] for event in events] == ['chunk-0', 'chunk-1', 'chunk-2']


def test_loop_is_stable_across_sequential_calls_on_one_thread(monkeypatch):
    """Sequential calls on one thread reuse one loop instead of building a new one.

    A per-call ``asyncio.new_event_loop()`` would also stop the contention, but
    it would break the provider: ``openai_compatible._ensure_async_client``
    caches one ``AsyncOpenAI`` client, and an httpx async client is bound to the
    loop it first ran on, so the next turn on a fresh loop fails. Exactly one
    loop may therefore be created for a thread, no matter how many streams it
    drives.
    """
    real_new_event_loop = asyncio.new_event_loop
    created = []

    def _counting_new_event_loop(*args, **kwargs):
        loop = real_new_event_loop(*args, **kwargs)
        created.append(loop)
        return loop

    monkeypatch.setattr(asyncio, 'new_event_loop', _counting_new_event_loop)

    errors = []
    results = []

    def _drain_twice():
        # A dedicated thread, so the count starts from a clean slate: this
        # thread has never driven a stream before.
        try:
            for _ in range(2):
                results.append(
                    list(_iter_stream_events(_yielding_stream(count=2, delay=0)))
                )
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
            errors.append(exc)

    thread = threading.Thread(target=_drain_twice)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), 'streaming thread hung'
    assert errors == [], f'streaming raised: {errors!r}'
    assert len(results) == 2
    for events in results:
        assert [event['text'] for event in events] == ['chunk-0', 'chunk-1']
    assert len(created) == 1, (
        'expected the thread to create exactly one loop across two sequential '
        f'streams, but {len(created)} loops were created'
    )


def test_reentrancy_guard_rejects_calls_from_a_running_loop():
    """Calling from a thread that already runs a loop is still a clear error."""

    async def _call_from_running_loop():
        return list(_iter_stream_events(_yielding_stream(count=1, delay=0)))

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_call_from_running_loop())

    assert str(excinfo.value) == (
        '_iter_stream_events() must be called from synchronous code: this thread '
        'already has a running event loop, so the stream cannot be driven with '
        'run_until_complete().'
    )
