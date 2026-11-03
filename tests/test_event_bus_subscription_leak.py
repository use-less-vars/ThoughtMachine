"""Behavioural regression test for the global-bus subscription leak.

Defect (recorded, never fixed): ``EventBus.subscribe`` (``agent/events.py``)
registers the callback and then falls off the end of its registration branch
**without returning anything**, so ``WebAgentBridge`` stores ``None`` in each of
its ``*_sub`` attributes::

    self._worker_spawned_sub = global_event_bus.subscribe(
        EventType.WORKER_SPAWNED, self._on_worker_spawned
    )

``_unsubscribe_worker_events`` guards every detach with ``if sub is not None:``,
so the guard is always false and **no global subscriber is ever removed**. Every
bridge the process ever builds stays subscribed to the process-global
``global_event_bus`` forever.

That is invisible while the leaked handler is healthy — ``publish`` swallows
per-subscriber exceptions. What makes it observable is that
``_on_worker_spawned`` reads ``self._forwarder._callbacks`` at its very first
line, *before* the session-mismatch guard, so a closed bridge whose forwarder
was replaced by a test double keeps executing production code on a later
``WORKER_SPAWNED`` and raises::

    Error in event subscriber for worker_spawned:
        '_ForwarderWithoutCallbacks' object has no attribute '_callbacks'

These three tests pin (A) the behaviour — a closed bridge must be detached, so
no later publish may reach it; (B) that what the bridge stores is actually usable
to detach a subscriber (behaviour only — the shape of the stored handle is not
pinned, since ``agent/events.py`` is deliberately left alone and
``subscribe()`` still returns ``None``; the bridge now stores its own
``(event_type, callback)`` pair); (C) the bridge-side bookkeeping — every global
subscription must be stored rather than dropped.

All three are expected to be RED before the fix. They must be GREEN after it,
with no other test changed.

Determinism: no threads, no sleeps, no timeouts. ``agent/events.py`` imports
``log`` from ``agent.logging`` (``from agent.logging import log``), and that
facility emits no stdlib logging records, so ``caplog`` cannot see the publish
loop's errors — the module-level ``log`` binding in ``agent.events`` is
monkeypatched instead (same seam as ``tests/test_bridge_stop_semantics.py``).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import agent.events as events_mod
from agent.events import EventType, create_event, global_event_bus
from session.store import FileSystemSessionStore
from web_ui.backend.bridge import WebAgentBridge

# Every attribute the bridge uses to hold a handle on the global bus
# (``bridge.py:517-524``): seven worker lifecycle subscriptions plus the
# security one.
GLOBAL_SUB_ATTRS = (
    '_worker_spawned_sub',
    '_worker_status_sub',
    '_worker_completed_sub',
    '_worker_error_sub',
    '_worker_partial_result_sub',
    '_worker_token_warning_sub',
    '_worker_message_sub',
    '_security_subscription',
)


class _ForwarderWithoutCallbacks:
    """A forwarder double that satisfies ``broadcast`` but has NO ``_callbacks``.

    This is what a closed bridge keeps a reference to after a test swapped
    ``bridge._forwarder`` for a recorder: the leaked subscriber still runs
    ``_on_worker_spawned``, whose first act is
    ``len(self._forwarder._callbacks)`` — the attribute this double deliberately
    does not define.
    """

    def __init__(self):
        self.broadcasts = []

    def broadcast(self, session_id, event_type, payload=None):
        self.broadcasts.append((session_id, event_type, payload))


@pytest.fixture
def clean_global_bus():
    """Run against an EMPTY global subscriber registry, restored afterwards.

    A failing assertion must never poison the rest of the session with this
    test's bridges, and leftovers from earlier tests must not be able to satisfy
    or break the assertions here.
    """
    with global_event_bus._lock:
        saved_subs = {evt: list(cbs) for evt, cbs in global_event_bus._subscribers.items()}
        saved_wildcard = list(global_event_bus._wildcard_subscribers)
        global_event_bus._subscribers.clear()
        global_event_bus._wildcard_subscribers.clear()
    try:
        yield global_event_bus
    finally:
        with global_event_bus._lock:
            global_event_bus._subscribers.clear()
            global_event_bus._subscribers.update(
                {evt: list(cbs) for evt, cbs in saved_subs.items()}
            )
            global_event_bus._wildcard_subscribers.clear()
            global_event_bus._wildcard_subscribers.extend(saved_wildcard)


@pytest.fixture
def recorded_log(monkeypatch):
    """Capture ``agent.events.log(level, category, message, ...)`` calls."""
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, str(message)))

    monkeypatch.setattr(events_mod, 'log', recorder)
    return events


def _make_bridge(tmp_path, name, session_id):
    """A real ``WebAgentBridge`` on a throwaway on-disk store."""
    store = FileSystemSessionStore(
        sessions_dir=str(tmp_path / name / "sessions"),
        state_dir=str(tmp_path / name / "state"),
    )
    bridge = WebAgentBridge(event_callback=lambda e: None, session_store=store)
    # start() is not driven here, so bind the session id by hand (the idiom
    # used by tests/test_per_worker_eventbus_bridge.py).
    bridge._session_id = session_id
    return bridge


def _worker_spawned(session_id, worker_name="w-leak"):
    return create_event(
        EventType.WORKER_SPAWNED,
        data={'session_id': session_id, 'worker_name': worker_name},
        source=f'worker:{worker_name}',
        session_id=session_id,
    )


def _subscriber_errors(recorded):
    return [msg for (level, category, msg) in recorded
            if 'Error in event subscriber' in msg]


def test_leaked_bridge_is_detached_on_close(clean_global_bus, recorded_log, tmp_path):
    """(A) A bridge closed via ``stop()`` must no longer receive bus events.

    The whole point: after teardown the bridge's ``_on_worker_spawned`` must not
    run for a later publish. Today it does, and because its forwarder was
    swapped for a recorder without ``_callbacks`` it raises inside
    ``EventBus.publish`` — which logs a ``core.event_bus`` error and carries on,
    silently.
    """
    bridge_1 = _make_bridge(tmp_path, "leak", "session-that-must-be-detached")
    try:
        assert bridge_1._on_worker_spawned in global_event_bus._subscribers.get(
            EventType.WORKER_SPAWNED, []
        ), (
            "precondition: constructing a bridge must subscribe _on_worker_spawned "
            f"to the global bus; subscribers={global_event_bus._subscribers!r}"
        )

        # A test double that has no ``_callbacks`` — the attribute
        # ``_on_worker_spawned`` reads before anything else.
        bridge_1._forwarder = _ForwarderWithoutCallbacks()

        # The real public teardown (server shutdown / close_session path).
        bridge_1.stop()
    finally:
        recorded_log.clear()

    # A bystander bridge, healthy forwarder, different session.
    bridge_2 = _make_bridge(tmp_path, "bystander", "session-of-the-bystander")
    try:
        global_event_bus.publish(_worker_spawned("session-that-must-be-detached"))

        errors = _subscriber_errors(recorded_log)
        assert not errors, (
            "a bridge torn down by stop() is still subscribed to the global "
            "WORKER_SPAWNED bus and ran _on_worker_spawned on a later publish: "
            f"{errors!r}. EventBus.subscribe() returns None, so "
            "_unsubscribe_worker_events()'s `if sub is not None:` guard never "
            "detaches anything."
        )
    finally:
        bridge_2.stop()


def test_stored_subscription_is_usable_to_detach(clean_global_bus, tmp_path):
    """(B) What the bridge stores must actually detach the subscriber.

    Behaviour only: the shape of the stored value is not pinned, only that it is
    enough to remove the listener again. This is the property that was missing —
    the bridge used to store ``None`` and hand it to a one-argument
    ``unsubscribe()``, which is a silent no-op.
    """
    bridge = _make_bridge(tmp_path, "detach", "session-to-detach")
    try:
        stored = bridge._worker_spawned_sub
        assert isinstance(stored, tuple) and len(stored) == 2, (
            "the bridge must store enough to detach with a two-argument "
            f"unsubscribe(event_type, callback); it stored {stored!r}"
        )
        event_type, callback = stored
        assert event_type is EventType.WORKER_SPAWNED, (
            f"stored subscription must name its event type, got {event_type!r}"
        )

        global_event_bus.unsubscribe(event_type, callback)

        remaining = global_event_bus._subscribers.get(EventType.WORKER_SPAWNED, [])
        assert bridge._on_worker_spawned not in remaining, (
            "the stored subscription could not detach the subscriber: "
            f"_on_worker_spawned is still registered ({remaining!r})"
        )
    finally:
        bridge.stop()


def test_bridge_stores_a_non_none_subscription_for_every_global_sub(
    clean_global_bus, tmp_path
):
    """(C) Every global subscription the bridge makes must be stored, not lost."""
    bridge = _make_bridge(tmp_path, "bookkeeping", "session-for-bookkeeping")
    try:
        dropped = [name for name in GLOBAL_SUB_ATTRS
                   if getattr(bridge, name, None) is None]
        assert not dropped, (
            "these global-bus handles were never captured, so teardown can "
            f"never detach them: {dropped}"
        )
    finally:
        bridge.stop()
