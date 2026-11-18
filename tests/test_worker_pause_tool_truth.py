"""``pause`` tool truth tests: the operator intent is latched on the record.

``_action_pause`` must record the manual-only marker on the WORKER RECORD even
when the worker's thread is not alive at that instant: the tool returns
``{"status": "paused", "manual_only": True, ...}``, so a success envelope with
no latched marker would be a tool that reports success while doing nothing.
Only the cooperative ``pause()`` call (and the ``workers_paused`` entry) stay
liveness-gated -- a dead loop cannot be signalled.

These tests use lightweight doubles registered in the production
``_worker_registry`` (the existing tray tests spawn real threads; a thread that
is genuinely not alive cannot be spawned deterministically), and they mirror
that module's registry hygiene: every key added here is popped again so nothing
leaks into another test's registry view.
"""

from tools.workspace import worker as worker_module
from tools.workspace.worker import Worker

#: The exact envelope keys the live-thread case returns; the dead-thread case
#: must return the SAME set (no key may be conditional on liveness).
_EXPECTED_RESULT_KEYS = [
    "main_agent_paused",
    "manual_only",
    "note",
    "scope",
    "session_id",
    "status",
    "workers_paused",
]


class _DeadWorkerDouble:
    """Worker double whose loop is NOT running."""

    def __init__(self, name):
        self.name = name
        self._manual_only_pause = False
        self.pause_calls = 0
        self.status = "ready"

    def is_alive(self):
        return False

    def pause(self, *args, **kwargs):
        self.pause_calls += 1


class _LiveWorkerDouble(_DeadWorkerDouble):
    """Worker double whose loop IS running."""

    def is_alive(self):
        return True


def _register(session_id, name, thread):
    """Register ``thread`` under the production registry key shape."""
    key = (session_id, name, 1)
    with worker_module._registry_lock:
        worker_module._worker_registry[key] = thread
    return key


def _unregister(key):
    with worker_module._registry_lock:
        worker_module._worker_registry.pop(key, None)


def test_manual_only_pause_records_marker_even_when_thread_not_alive():
    """The regression: a dead loop must not swallow the operator's latch."""
    session_id = "sess-pause-dead"
    name = "w-dead"
    thread = _DeadWorkerDouble(name)
    key = _register(session_id, name, thread)
    try:
        tool = Worker(
            action="pause",
            worker_name=name,
            worker_query="manual_only",
            session_id=session_id,
        )
        result = tool._action_pause([])

        # THE regression assertion: intent recorded despite is_alive() False.
        assert thread._manual_only_pause is True
        assert result["manual_only"] is True
        assert result["status"] == "paused"
        # A dead loop is never signalled and never reported as paused.
        assert thread.pause_calls == 0
        assert result["workers_paused"] == []
        # The envelope shape is identical to the live-thread case.
        assert sorted(result) == _EXPECTED_RESULT_KEYS
    finally:
        _unregister(key)


def test_manual_only_pause_still_pauses_live_thread():
    """The live path is unchanged: marker + one pause() + workers_paused."""
    session_id = "sess-pause-live"
    name = "w-live"
    thread = _LiveWorkerDouble(name)
    key = _register(session_id, name, thread)
    try:
        tool = Worker(
            action="pause",
            worker_name=name,
            worker_query="manual_only",
            session_id=session_id,
        )
        result = tool._action_pause([])

        assert thread._manual_only_pause is True
        assert thread.pause_calls == 1
        assert result["workers_paused"] == [
            {"worker_name": name, "instance_id": 1}
        ]
        assert result["manual_only"] is True
        assert sorted(result) == _EXPECTED_RESULT_KEYS
    finally:
        _unregister(key)


def test_plain_pause_does_not_latch_marker_on_dead_thread():
    """A plain ``all`` pause never latches the operator-owned marker."""
    session_id = "sess-plain-dead"
    name = "w-plain"
    thread = _DeadWorkerDouble(name)
    key = _register(session_id, name, thread)
    try:
        tool = Worker(
            action="pause",
            worker_name=name,
            worker_query="all",
            session_id=session_id,
        )
        result = tool._action_pause([])

        assert thread._manual_only_pause is False
        assert result["manual_only"] is False
        assert result["workers_paused"] == []
    finally:
        _unregister(key)
