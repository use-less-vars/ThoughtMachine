"""Stop semantics: ``stop()`` is TEARDOWN, ``request_stop()`` truncates a TURN.

Two distinct operations live on ``WebAgentBridge`` and must not be confused:

* ``stop()`` — teardown. Used by ``close_session`` / server shutdown / config
  swap. It drops queued config, unregisters, unsubscribes and calls
  ``controller.stop()`` (or, on the legacy no-controller path, sets
  ``_stop_event``), ending the session.
* ``request_stop()`` — turn-scoped. The Stop button. It asks the controller for
  a graceful halt so the agent breaks out of the stream, commits the partial
  assistant text and emits its ``interrupted`` terminal. The bridge thread and
  the session survive: no ``_stop_event``, no ``controller.stop()``, no
  unregister/unsubscribe, no queued-config drop.

``request_stop()`` returns the *pre-halt* ``controller.is_busy`` so a caller CAN
tell "a turn was halted" from "nothing was running", but ``server.py``
``stop_session`` deliberately does not use it: the idle-stop acknowledgement
'⏹ Stopped.' is a pinned contract
(``tests/integration/test_state_transitions.py::test_ws_stop_session_status_only``).

The final hop — the bridge mapping an agent ``interrupted`` event onto the
frontend ``interrupted`` broadcast — is covered here too: if that hop drops the
event the frontend never learns the halt happened, however correct the rest is.

Determinism: a hand-written fake controller records calls; no threads, sleeps or
timeouts. ``agent.logging.log`` emits no stdlib logging records, so the module
level ``log`` binding is monkeypatched (the seam used by
``tests/test_bridge_continue_session_config_log.py``).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import web_ui.backend.bridge as bridge_mod
from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore


class _FakeController:
    """Records exactly which stop lever the bridge pulled."""

    def __init__(self, busy=False, with_halt=True):
        self.is_busy = busy
        self.halt_calls = 0
        self.stop_calls = 0
        if not with_halt:
            # Simulate an older controller that predates request_halt().
            self.request_halt = None

    def request_halt(self):  # noqa: F811 - replaced by None above when absent
        self.halt_calls += 1

    def stop(self):
        self.stop_calls += 1


class _RecordingForwarder:
    """Minimal EventForwarder stand-in: records broadcasts, returns nothing.

    ``EventForwarder.broadcast(session_id, event_type, payload)`` IGNORES the
    ``session_id`` argument — the session id that reaches the browser is the one
    the payload carries — so the fake takes it and records it verbatim.
    """

    def __init__(self):
        self.broadcasts = []

    def broadcast(self, session_id, event_type, payload=None):
        self.broadcasts.append((session_id, event_type, payload))


def _record_log(monkeypatch):
    """Capture ``web_ui.backend.bridge.log(level, category, message, ...)``."""
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, message))

    monkeypatch.setattr(bridge_mod, "log", recorder)
    return events


@pytest.fixture
def bridge(tmp_path):
    store = FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
    )
    return WebAgentBridge(event_callback=lambda e: None, session_store=store)


def test_request_stop_while_busy_halts_turn_only(bridge, monkeypatch):
    """(a) Busy: halt the turn, never tear the bridge down."""
    events = _record_log(monkeypatch)
    controller = _FakeController(busy=True)
    bridge._controller = controller

    assert bridge.request_stop() is True, "busy session must report a turn was halted"

    assert controller.halt_calls == 1, "request_halt() must be called exactly once"
    assert controller.stop_calls == 0, "request_stop() must never call controller.stop()"
    assert not bridge._stop_event.is_set(), (
        "request_stop() must not set _stop_event — that is teardown, and the "
        "bridge thread has to survive a Stop button press"
    )

    info = [m for (lvl, _cat, m) in events if lvl == "INFO"]
    assert any("request_stop" in str(m) for m in info), (
        f"no INFO log announcing the turn-level halt: {events!r}"
    )


def test_request_stop_while_idle_still_calls_halt_and_reports_false(bridge, monkeypatch):
    """(b) Idle: the halt is turn-scoped and cleared per query, so it is still sent."""
    _record_log(monkeypatch)
    controller = _FakeController(busy=False)
    bridge._controller = controller

    assert bridge.request_stop() is False, (
        "an idle session must report 'nothing to stop' so the ack is honest"
    )

    assert controller.halt_calls == 1, (
        "the halt flag is cleared per query, so it is set unconditionally; the "
        "busy sample only decides what we TELL the user"
    )
    assert controller.stop_calls == 0
    assert not bridge._stop_event.is_set()


def test_stop_is_still_teardown(bridge, monkeypatch):
    """(c) stop() keeps its teardown contract: controller.stop(), exactly once."""
    _record_log(monkeypatch)
    controller = _FakeController(busy=True)
    bridge._controller = controller

    bridge.stop()

    assert controller.stop_calls == 1, "stop() must tear the controller down"
    assert controller.halt_calls == 0, "stop() must not degrade into a turn-level halt"


def test_interrupted_event_crosses_the_bridge_hop(bridge):
    """The halt terminal must reach the socket, carrying the partial text.

    This is the delivery hop (``_map_and_emit``'s ``interrupted`` branch): the
    agent committing a partial message is worthless to the user if the bridge
    drops the event, and nothing else in the suite exercised this branch.
    """
    forwarder = _RecordingForwarder()
    bridge._forwarder = forwarder

    bridge._map_and_emit({
        "type": "interrupted",
        "content": "Partial",
        "stop_reason": "interrupted",
        "turn": 3,
    })

    interrupted = [b for b in forwarder.broadcasts if b[1] == "interrupted"]
    assert len(interrupted) == 1, (
        f"expected exactly one 'interrupted' broadcast, got {forwarder.broadcasts!r}"
    )

    _session_id_arg, _event_type, payload = interrupted[0]
    assert payload["text"] == "Partial", "the committed partial must survive the hop"
    assert payload["stop_reason"] == "interrupted"
    assert payload["turn"] == 3
    assert payload["session_id"] == bridge._session_id, (
        "broadcast() ignores its session_id argument, so the payload must carry it"
    )


def test_interrupted_event_with_empty_content_still_broadcasts(bridge):
    """A halt before any delta broadcasts text '' and must not raise.

    ``TurnTransaction.has_assistant_message`` stops the empty message being
    committed upstream; the bridge still has to deliver the terminal.
    """
    forwarder = _RecordingForwarder()
    bridge._forwarder = forwarder

    bridge._map_and_emit({
        "type": "interrupted",
        "content": "",
        "stop_reason": "interrupted",
        "turn": 1,
    })

    interrupted = [b for b in forwarder.broadcasts if b[1] == "interrupted"]
    assert len(interrupted) == 1, (
        f"expected exactly one 'interrupted' broadcast, got {forwarder.broadcasts!r}"
    )

    payload = interrupted[0][2]
    assert payload["text"] == ""
    assert payload["session_id"] == bridge._session_id


def test_request_stop_without_request_halt_returns_false(bridge, monkeypatch):
    """(d) A controller predating request_halt() cannot halt a turn — say so."""
    events = _record_log(monkeypatch)
    controller = _FakeController(busy=True, with_halt=False)
    bridge._controller = controller

    assert bridge.request_stop() is False

    assert controller.stop_calls == 0
    assert not bridge._stop_event.is_set(), (
        "a missing request_halt() must degrade to 'nothing halted', not to "
        "silently tearing the bridge down"
    )

    warnings = [m for (lvl, _cat, m) in events if lvl == "WARNING"]
    assert any("request_halt" in str(m) for m in warnings), (
        f"no WARNING about the missing turn-level stop: {events!r}"
    )
