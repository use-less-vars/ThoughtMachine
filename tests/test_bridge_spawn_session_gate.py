"""Regression tests for the session gate that swallows ``WORKER_SPAWNED``.

Defect under test
─────────────────
``WebAgentBridge._on_worker_spawned`` (``web_ui/backend/bridge.py:1026``)
resolves a session id of its own::

    session_id = data.get('session_id', self._session_id or '')   # :1041

…and then gates on a *different* value, re-deriving it from the raw payload::

    if not self._event_belongs_to_this_session(data):             # :1057
        return                                                    # :1058

``_event_belongs_to_this_session`` (``:1481``) is deny-by-default: the payload
must carry a non-empty ``session_id`` **equal to** ``self._session_id``, which
is ``None`` until a session is bound (``:1806`` / ``:2129`` / ``:3274``, reset
at ``:3077``) — and it rejects an unbound bridge outright.  On the real startup
path the ``WORKER_SPAWNED`` publish therefore lands *before* the bridge knows
its session, the handler returns at ``:1058``, and everything below that line
is skipped:

* the per-worker ``EventBus`` subscription (``:1079``-``:1111``) — so no
  ``tool_call`` / ``tool_result`` / ``assistant_message`` rows ever reach the
  panel;
* ``_buffer_worker_event`` (``:1118``) — so not even the spawn lifecycle frame
  is replayed on reconnect.

Both observed symptoms ("worker panel never fills in" and "spawn row missing
after F5") are this one ``return``.

Required fix contract (asserted below)
──────────────────────────────────────
1. A spawn whose ``session_id`` is known but which arrives while the bridge has
   no session bound yet must be **parked**, not dropped: no callback frame and
   no per-worker subscription yet (parking must not adopt the event's session
   id as its own), but the frame is retained.
2. The bridge must expose a session-bind entry point that (a) records the
   session id and (b) replays the parked spawns belonging to that session to
   the registered callbacks exactly once.  ``_adopt()`` below probes the
   documented names; ``_bind_session_id`` is the name the fix is expected to
   provide, the others are accepted aliases.
3. Parking is bounded: at most 32 frames are retained, oldest dropped first,
   in ``bridge._parked_worker_spawns``.
4. A spawn belonging to a *foreign* session is still dropped (deny-by-default
   must not regress) and is never parked.
5. The foreign-session drop must be *diagnosable at the default log level*: the
   logging facility used by ``bridge.py`` (``agent.logging.log``) writes to the
   console only, and with no ``TM_LOG_TAGS`` set ``_should_log_to_console``
   shows ``WARNING`` and above — ``DEBUG``/``INFO`` are invisible (that is why
   ``/workspace/logs/backend_startup.log`` has zero ``DEBUG`` lines).  A silent
   drop is what made this bug take a forensics session to find, so the drop
   must emit a ``WARNING`` (or higher) console line naming *both* session ids.
   ``caplog`` is consulted as a second channel in case the fix routes the
   message through ``logging`` instead.

Expected state before the fix: (a), (d), (e) RED; (b), (c) green.

Determinism: ``EventBus.publish`` is synchronous, so no threads/sleeps are
needed.  Session ids are per-test uuids because ``EventBus.subscribe`` returns
``None`` and the bridge therefore cannot detach its global subscription —
bridges leaked by earlier tests stay subscribed and must stay inert here.
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent.events import EventBus, EventType, create_event, global_event_bus
from tools.workspace.worker import (
    register_worker_event_bus,
    unregister_worker_event_bus,
)
from web_ui.backend.bridge import WebAgentBridge

#: Frames parked while no session is bound (bounded ring, oldest dropped).
PARKED_ATTR = '_parked_worker_spawns'
#: Documented bound on the parking ring.
PARKED_MAX = 32
#: Accepted names for the session-bind entry point, preferred first.
ADOPT_METHODS = ('_bind_session_id', '_adopt_session_id', '_adopt_session')


# ── helpers ──────────────────────────────────────────────────────────────────

def _sid(tag: str) -> str:
    return f'sess-{tag}-{uuid.uuid4().hex[:8]}'


def _make_bridge(session_id=None):
    """Return ``(bridge, received)`` with the session id set as ``start()`` would.

    No agent thread is started; ``event_callback`` is registered the way a
    live WebSocket connection registers one, so a parked frame has somewhere to
    be replayed *to*.
    """
    received = []
    bridge = WebAgentBridge(event_callback=lambda ev: received.append(ev))
    bridge._session_id = session_id
    return bridge, received


def _publish_spawn(session_id, worker_name, instance_id=1):
    assert global_event_bus is not None, 'event system unavailable in this environment'
    global_event_bus.publish(create_event(
        EventType.WORKER_SPAWNED,
        data={
            'session_id': session_id,
            'worker_name': worker_name,
            'instance_id': instance_id,
        },
        source=f'worker:{worker_name}',
        session_id=session_id,
    ))


def _adopt(bridge, session_id):
    """Bind *bridge* to *session_id* and return the method name used (or None).

    The fix is expected to funnel every ``_session_id`` assignment through one
    entry point that also replays parked spawns.  On an unfixed bridge no such
    method exists, so fall back to the plain attribute assignment plus the
    existing reconnect replay path — that keeps the failure of test (a) an
    *assertion* about the missing replay rather than an ``AttributeError``.
    """
    for name in ADOPT_METHODS:
        fn = getattr(bridge, name, None)
        if callable(fn):
            fn(session_id)
            return name
    bridge._session_id = session_id
    bridge._flush_worker_event_buffer()
    return None


def _parked(bridge):
    """Parked frames, or ``None`` when the bridge has no parking ring at all."""
    return getattr(bridge, PARKED_ATTR, None)


def _frame_worker_name(frame):
    """Worker label of a forwarded/parked frame, wherever the fix puts it."""
    if not isinstance(frame, dict):
        return None
    if frame.get('worker_name') is not None:
        return frame['worker_name']
    data = frame.get('data')
    if isinstance(data, dict):
        return data.get('worker_name')
    return None


def _release(bridge, session_id, worker_name):
    bridge._unsubscribe_worker_events()
    unregister_worker_event_bus(session_id, worker_name)


# ── (a) park before the session is bound, replay on bind ─────────────────────

def test_spawn_before_session_bound_is_parked_then_replayed():
    """A spawn that arrives before ``_session_id`` is set must be parked and
    replayed once the session is adopted — not dropped at the deny-by-default
    gate (``bridge.py:1057``) before the per-worker subscription (:1079) and the
    reconnect buffer (:1118)."""
    session_id = _sid('park')
    worker_name = 'worker-park'

    bridge, received = _make_bridge(None)
    # Register the worker's bus so a *fixed* bridge has something real to bind
    # to, and so "no subscription yet" is a meaningful assertion below.
    register_worker_event_bus(session_id, worker_name, EventBus(), instance_id=1)
    try:
        _publish_spawn(session_id, worker_name)

        assert received == [], (
            'an unbound bridge forwarded a WORKER_SPAWNED frame before any '
            f'session was adopted: {received}'
        )
        assert bridge._worker_bus_subs == {}, (
            'an unbound bridge subscribed to a per-worker EventBus before any '
            f'session was adopted: {bridge._worker_bus_subs}'
        )

        _adopt(bridge, session_id)

        assert len(received) == 1, (
            'expected exactly one replayed spawn frame after adopting '
            f'{session_id!r}, got {len(received)}: '
            f'{[f.get("type") if isinstance(f, dict) else f for f in received]} '
            '— the spawn was dropped at the session gate instead of parked '
            f'(parked={_parked(bridge)!r})'
        )
        frame = received[0]
        assert frame.get('type') == 'worker:worker_spawned', (
            f'unexpected replayed frame type: {frame.get("type")!r} in {frame}'
        )
        assert _frame_worker_name(frame) == worker_name, (
            f'replayed frame lost the worker label: {frame}'
        )
    finally:
        _release(bridge, session_id, worker_name)


# ── (b) foreign-session spawn is dropped, and not parked ─────────────────────

def test_foreign_session_spawn_is_dropped_and_not_parked():
    """Deny-by-default must survive the fix: a spawn for another session is not
    forwarded, is not parked, and cannot be replayed by adopting that session."""
    own_session = _sid('own')
    foreign_session = _sid('foreign')
    worker_name = 'worker-foreign'

    bridge, received = _make_bridge(own_session)
    try:
        _publish_spawn(foreign_session, worker_name)

        assert received == [], (
            'a bridge bound to '
            f'{own_session!r} forwarded a spawn from {foreign_session!r}: {received}'
        )
        parked = _parked(bridge)
        if parked is not None:
            assert list(parked) == [], (
                f'a foreign-session spawn was parked instead of dropped: {parked}'
            )

        _adopt(bridge, foreign_session)
        assert received == [], (
            'adopting the foreign session replayed a spawn that must have been '
            f'dropped: {received}'
        )
        parked = _parked(bridge)
        if parked is not None:
            assert list(parked) == [], (
                f'parking ring not drained for a dropped spawn: {parked}'
            )
    finally:
        _release(bridge, own_session, worker_name)


# ── (c) adopting with an empty parking ring must not raise ───────────────────

def test_adopt_with_empty_parking_does_not_raise():
    """The session-bind hook runs on every session open/switch, almost always
    with nothing parked; it must be a no-op then (and idempotent)."""
    session_id = _sid('empty')

    bridge, received = _make_bridge(None)
    try:
        _adopt(bridge, session_id)
        _adopt(bridge, session_id)   # idempotent re-adopt
        assert received == [], (
            f'adopting an empty parking ring emitted frames: {received}'
        )
        assert bridge._session_id == session_id, (
            f'adoption did not record the session id: {bridge._session_id!r}'
        )
    finally:
        _release(bridge, session_id, 'worker-empty')


# ── (d) parking is bounded ───────────────────────────────────────────────────

def test_parked_spawns_are_bounded():
    """A bridge that never gets a session bound must not leak memory: the
    parking ring keeps at most ``PARKED_MAX`` (32) frames, dropping the oldest.

    Asserted on ``bridge._parked_worker_spawns`` (the ring itself), not on a
    counter, because the drop-oldest behaviour is what matters.
    """
    session_id = _sid('bound')

    bridge, received = _make_bridge(None)
    try:
        total = PARKED_MAX + 1
        names = [f'parked-{i}' for i in range(total)]
        for name in names:
            _publish_spawn(session_id, name)

        parked = _parked(bridge)
        assert parked is not None, (
            f'no parking ring on the bridge: expected a bounded list at '
            f'{PARKED_ATTR!r} holding spawns that arrive before the session is '
            f'bound (received={received})'
        )
        assert len(parked) == PARKED_MAX, (
            f'parking ring is not bounded at {PARKED_MAX}: published {total} '
            f'spawns, ring holds {len(parked)} '
            f'({[_frame_worker_name(f) for f in parked]})'
        )
        kept = [_frame_worker_name(f) for f in parked]
        assert names[0] not in kept, (
            f'parking ring did not drop the oldest frame: kept {kept}'
        )
        assert names[-1] in kept, 'parking ring dropped the newest frame'
        assert kept == names[1:], (
            f'parking ring did not keep the newest {PARKED_MAX} frames in '
            f'order: {kept}'
        )
    finally:
        _release(bridge, session_id, 'parked-0')


# ── (e) the drop is diagnosable at the default log level ─────────────────────

def test_drop_is_visible_at_default_log_level(monkeypatch, capsys, caplog):
    """A foreign-session spawn must leave a ``WARNING``-or-higher trace naming
    *both* session ids.

    ``bridge.py`` logs through ``agent.logging.log``, which prints to the
    console; with no ``TM_LOG_TAGS`` configured ``_should_log_to_console``
    admits ``WARNING`` and above only.  The two gate diagnostics today are
    ``DEBUG`` (``bridge.py:1050`` and ``:1059``), so the drop is completely
    silent in a default deployment — reading both ids therefore requires
    ``WARNING`` (``INFO`` would be invisible).  ``caplog`` is checked as a
    second channel in case the fix emits a stdlib record instead.
    """
    import agent.logging.unified as unified

    # Hermetic default-logging state: no component debug override, no tags.
    for var in ('THOUGHTMACHINE_DEBUG', 'DEBUG_BRIDGE',
                'DEBUG_PIPELINE_BRIDGE', 'TM_LOG_TAGS'):
        monkeypatch.delenv(var, raising=False)
    previous_tags = unified.set_log_tags([])
    caplog.set_level(logging.INFO)

    own_session = _sid('own')
    foreign_session = _sid('foreign')
    worker_name = 'worker-dropped'

    bridge, received = _make_bridge(own_session)
    try:
        capsys.readouterr()          # drain construction noise
        caplog.clear()

        _publish_spawn(foreign_session, worker_name)

        captured = capsys.readouterr()
        console = (captured.err or '') + (captured.out or '')
        warning_lines = [
            line for line in console.splitlines()
            if any(lvl in line for lvl in ('WARNING', 'ERROR', 'CRITICAL'))
        ]
        records = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        visible = warning_lines + records

        assert received == [], (
            f'foreign-session spawn was forwarded: {received}'
        )
        assert visible, (
            'the foreign-session drop left no WARNING-or-higher trace at the '
            'default log level, so an operator cannot tell a dropped spawn '
            f'from an idle worker (own={own_session!r}, '
            f'foreign={foreign_session!r}); captured console={console!r}, '
            f'records={records!r}'
        )
        assert any(
            own_session in msg and foreign_session in msg for msg in visible
        ), (
            'no WARNING-or-higher message names BOTH session ids, so the drop '
            f'cannot be attributed (own={own_session!r}, '
            f'foreign={foreign_session!r}); visible={visible!r}'
        )
    finally:
        unified.set_log_tags(previous_tags)
        _release(bridge, own_session, worker_name)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q']))
