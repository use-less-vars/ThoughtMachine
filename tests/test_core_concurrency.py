"""Concurrency guarantees of the core runtime: per-thread stream loops and the
global event bus.

Backend-only tests (no UI, no HTTP server, no provider connection). They cover

1. two controller-style worker threads get two *distinct* event loops that are
   live at the same time (``_STREAM_LOCAL`` in ``agent/core/agent.py``),
2. thread-death cleanup (``_close_stream_resources``) closes the dying thread's
   provider and loop while another thread's loop stays open and usable -- and
   leaves the loop open when that cleanup is disabled,
3. ``global_event_bus`` survives 5 x 100 concurrent publishes plus a
   subscribe/unsubscribe churn thread, delivers every event exactly once, and
   never lets a raising subscriber kill a publisher thread.
"""

import threading
import time
import traceback

from agent.core.agent import (
    _STREAM_LOCAL,
    _close_stream_resources,
    _iter_stream_events,
)
from agent.events import BaseEvent, EventMetadata, EventType, global_event_bus
from tests.test_resource_cleanup import _FakeAsyncClient, _FakeProvider

_STREAM_CHUNK = {'type': 'text_delta', 'text': 'chunk'}


async def _one_chunk():
    yield dict(_STREAM_CHUNK)


async def _ping():
    return 'pong'


def _start(worker, errors, name):
    """Run ``worker`` on a daemon thread, recording any failure in ``errors``."""
    def wrapped():
        try:
            worker()
        except BaseException:
            errors.append((name, traceback.format_exc()))

    thread = threading.Thread(target=wrapped, name=name, daemon=True)
    thread.start()
    return thread


def test_two_threads_get_two_distinct_simultaneously_live_event_loops():
    """Each thread drives its own loop, and both are open at the same moment."""
    errors = []
    holders = ({}, {})
    ready = (threading.Event(), threading.Event())
    release = threading.Event()

    def worker(index):
        def body():
            iterator = _iter_stream_events(_one_chunk())
            first = next(iterator)
            loop = _STREAM_LOCAL.loop
            holders[index]['loop'] = loop
            holders[index]['first'] = first
            ready[index].set()
            # Hold the loop open until the main thread has observed both loops.
            assert release.wait(timeout=10), 'main thread never released the worker'
            holders[index]['events'] = [first, *iterator]

        try:
            body()
        except BaseException:
            errors.append(('worker-%d' % index, traceback.format_exc()))
        finally:
            holders[index]['cleanup'] = _close_stream_resources()
            holders[index]['closed_after'] = holders[index]['loop'].is_closed()
            ready[index].set()

    threads = [_start(lambda index=index: worker(index), errors, 'loop-%d' % index)
               for index in (0, 1)]
    try:
        assert ready[0].wait(timeout=10), 'worker 0 never created its loop'
        assert ready[1].wait(timeout=10), 'worker 1 never created its loop'
        loop_a = holders[0]['loop']
        loop_b = holders[1]['loop']
        assert loop_a is not loop_b, 'both threads shared one event loop object'
        # Measured, not assumed: both workers are still parked on `release`, so
        # neither has reached its `finally` cleanup yet. (A worker that had
        # already finished would have closed its own loop, which the is_closed()
        # checks below catch too; this makes the simultaneity explicit.)
        assert all(thread.is_alive() for thread in threads), (
            'a worker finished before both loops could be observed at once'
        )
        assert 'cleanup' not in holders[0] and 'cleanup' not in holders[1], (
            'a worker had already run its cleanup before the simultaneity check'
        )
        # Checked at runtime: both loops are open at the same time.
        assert loop_a.is_closed() is False
        assert loop_b.is_closed() is False
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)

    assert errors == []
    for thread in threads:
        assert not thread.is_alive()
    for index in (0, 1):
        assert holders[index]['first'] == _STREAM_CHUNK
        assert holders[index]['events'] == [_STREAM_CHUNK]
        assert holders[index]['cleanup'] is True, 'no live loop was found to close'
        assert holders[index]['closed_after'] is True


def test_thread_death_cleanup_closes_only_the_dead_threads_loop():
    """Tier 1: the dying thread's provider and loop are closed. Tier 2: another
    thread's loop is neither closed nor broken by that cleanup."""
    errors = []
    tier1 = {}
    tier2 = {}
    control = {}
    tier1_done = threading.Event()
    tier2_ready = threading.Event()

    client = _FakeAsyncClient()
    provider = _FakeProvider(client)

    def tier1_body():
        # Tier 1: dies and takes its loop with it, exactly like the
        # AgentController._run() finally block.
        try:
            iterator = _iter_stream_events(_one_chunk())
            first = next(iterator)
            tier1['loop'] = _STREAM_LOCAL.loop
            tier1['events'] = [first, *iterator]
            tier1['cleanup'] = _close_stream_resources(provider)
            tier1['provider_aclose_calls'] = provider.aclose_calls
            tier1['closed_after'] = tier1['loop'].is_closed()
        finally:
            tier1_done.set()

    def tier2_body():
        # Tier 2: alive before, during and after tier 1's death.
        iterator = _iter_stream_events(_one_chunk())
        first = next(iterator)
        loop = _STREAM_LOCAL.loop
        tier2['loop'] = loop
        tier2_ready.set()
        assert tier1_done.wait(timeout=10), 'tier 1 never finished'
        tier2['closed_while_tier1_died'] = loop.is_closed()
        tier2['still_owns_local_loop'] = _STREAM_LOCAL.loop is loop
        # Not merely un-closed: the loop must still drive work.
        tier2['ran_coroutine'] = loop.run_until_complete(_ping())
        tier2['events'] = [first, *iterator]
        tier2['cleanup'] = _close_stream_resources()
        tier2['closed_after'] = loop.is_closed()

    def control_body():
        # Same shape as tier 1 but with the cleanup call disabled: the loop must
        # be left open. This is what proves the tier-1 assertion discriminates.
        iterator = _iter_stream_events(_one_chunk())
        first = next(iterator)
        control['loop'] = _STREAM_LOCAL.loop
        control['events'] = [first, *iterator]
        control['closed_after'] = control['loop'].is_closed()

    tier2_thread = _start(tier2_body, errors, 'tier2')
    assert tier2_ready.wait(timeout=10), 'tier 2 never created its loop'
    assert tier2_thread.is_alive(), 'tier 2 must stay alive across the tier 1 death'

    tier1_thread = _start(tier1_body, errors, 'tier1')
    tier1_thread.join(timeout=10)
    assert not tier1_thread.is_alive(), 'tier 1 thread hung'

    tier2_thread.join(timeout=10)
    assert not tier2_thread.is_alive(), 'tier 2 thread hung'

    control_thread = _start(control_body, errors, 'control')
    control_thread.join(timeout=10)
    assert not control_thread.is_alive(), 'control thread hung'

    assert errors == []

    # Tier 1: cleanup happened, in the documented order (client, then loop).
    assert tier1['events'] == [_STREAM_CHUNK]
    assert tier1['cleanup'] is True
    assert tier1['provider_aclose_calls'] == 1
    # Asserted on the captured client object itself, not through `provider`: the
    # fake's aclose() drops `provider._async_client`, so the reference has to be
    # taken before teardown. `_FakeAsyncClient.closed` is a call COUNT starting at
    # 0, so `== 1` means "closed exactly once" (it is not the bool `True == 1`
    # tautology it looks like). On the real SDK, `AsyncOpenAI.is_closed` is a
    # zero-arg METHOD, so a bare `assert client.is_closed` would assert a truthy
    # bound method and be vacuous - it has to be called.
    assert client.closed == 1, 'the provider did not close its async client exactly once'
    assert provider._async_client is None, (
        'the provider still holds the closed client; a later turn on this thread '
        'would reuse a client bound to a closed loop'
    )
    assert tier1['closed_after'] is True

    # Tier 2: undisturbed and still functional.
    assert tier2['loop'] is not tier1['loop']
    assert tier2['closed_while_tier1_died'] is False
    assert tier2['still_owns_local_loop'] is True
    assert tier2['ran_coroutine'] == 'pong'
    assert tier2['events'] == [_STREAM_CHUNK]
    assert tier2['cleanup'] is True
    assert tier2['closed_after'] is True

    # Control: with cleanup disabled the loop is left open (RED evidence).
    assert control['events'] == [_STREAM_CHUNK]
    assert control['closed_after'] is False, (
        'cleanup-disabled control closed its loop anyway: the assertion above is '
        'not measuring _close_stream_resources'
    )
    control['loop'].close()


def test_global_event_bus_concurrent_publish_and_subscriber_churn():
    """500 concurrent publishes, a raising subscriber and a subscribe/unsubscribe
    churn thread: every event reaches every subscriber exactly once and no
    publisher thread dies."""
    bus = global_event_bus
    wildcard_baseline = len(bus._wildcard_subscribers)
    typed_baseline = sum(len(callbacks) for callbacks in bus._subscribers.values())

    errors = []
    lock = threading.Lock()
    received = []
    subscriber_errors = []
    churn_callbacks = []
    stop_churn = threading.Event()

    publishers = 5
    per_publisher = 100
    total = publishers * per_publisher

    def counting_subscriber(event):
        with lock:
            received.append(event.data['i'])

    def raising_subscriber(event):
        if event.data['i'] % 10 == 0:
            with lock:
                subscriber_errors.append(event.data['i'])
            raise RuntimeError('deliberate subscriber failure')

    def publisher_body(index):
        for offset in range(per_publisher):
            bus.publish(BaseEvent(
                type=EventType.AGENT_START,
                metadata=EventMetadata(source='concurrency-test'),
                data={'i': index * per_publisher + offset},
            ))

    def churn_body():
        while not stop_churn.is_set():
            hits = [0]

            def temp(event, hits=hits):
                hits[0] += 1

            churn_callbacks.append(temp)
            bus.subscribe(None, temp)
            bus.unsubscribe(None, temp)
            time.sleep(0.001)

    bus.subscribe(None, counting_subscriber)
    bus.subscribe(EventType.AGENT_START, raising_subscriber)
    try:
        churn_thread = _start(churn_body, errors, 'churn')
        threads = [_start(lambda index=index: publisher_body(index), errors,
                          'publisher-%d' % index) for index in range(publishers)]
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), '%s hung' % thread.name
        stop_churn.set()
        churn_thread.join(timeout=10)
        assert not churn_thread.is_alive(), 'churn thread hung'

        # No publisher thread died: a raising subscriber is swallowed by
        # EventBus.publish, it never escapes into the publishing thread.
        assert errors == []
        assert sorted(received) == list(range(total))
        assert sorted(subscriber_errors) == list(range(0, total, 10))

        # The bus is still coherent after the churn: one publish, one delivery.
        with lock:
            received.clear()
        bus.publish(BaseEvent(
            type=EventType.AGENT_START,
            metadata=EventMetadata(source='concurrency-test'),
            data={'i': -1},
        ))
        assert received == [-1]
    finally:
        stop_churn.set()
        bus.unsubscribe(None, counting_subscriber)
        bus.unsubscribe(EventType.AGENT_START, raising_subscriber)
        for callback in churn_callbacks:
            bus.unsubscribe(None, callback)

    # Subscriber registries are back to their pre-test state (no leaks).
    assert len(bus._wildcard_subscribers) == wildcard_baseline
    assert sum(len(callbacks) for callbacks in bus._subscribers.values()) == typed_baseline
