"""Persistence invariants for the presenter event pipeline.

A terminal event (``agent_responded`` / ``stopped`` / ``max_turns`` /
``thread_finished``) is the only place where a completed assistant turn is
written to disk (``SessionLifecycle.auto_save_current_session``).  Before the
fix that save was the last *unguarded* statement of ``_process_terminal_event``,
so any exception raised earlier in ``process_event`` skipped it and the turn
never reached disk — a silent, unrecoverable data loss.

These tests pin the two halves of the fix:

* the save is a ``finally`` invariant of the terminal dispatch, and
* a raising plain event callback is reported through the log facade instead of
  being dumped to stderr with ``traceback.print_exc()``.
"""

import queue
from types import SimpleNamespace

import pytest

from agent.presenter.event_processor import EventProcessor


class _FakeLifecycle:
    """Minimal stand-in for SessionLifecycle."""

    def __init__(self):
        self.state = None
        self._restarting = False
        self.save_count = 0

    def auto_save_current_session(self) -> bool:
        self.save_count += 1
        return True


class _FakeGui:
    """GUI integration stub; can be told to explode on status emission."""

    def __init__(self, fail_on_status: bool = False):
        self.fail_on_status = fail_on_status
        self.status_messages = []
        self.conversation_changed_count = 0

    def emit_status_message(self, message):
        if self.fail_on_status:
            raise RuntimeError('status emission exploded')
        self.status_messages.append(message)

    def emit_conversation_changed(self):
        self.conversation_changed_count += 1


class _FakeStateBridge:
    """StateBridge stub holding one current session."""

    def __init__(self, session_id: str = 'sess-1'):
        self.current_session_id = session_id
        self.current_session = SimpleNamespace(
            final_content=None,
            final_reasoning=None,
            final_timestamp=None,
        )


def _make_processor(lifecycle=None, gui=None, state_bridge=None):
    lifecycle = lifecycle or _FakeLifecycle()
    gui = gui or _FakeGui()
    state_bridge = state_bridge or _FakeStateBridge()
    return EventProcessor(
        state_bridge=state_bridge,
        session_lifecycle=lifecycle,
        gui_integration=gui,
    ), lifecycle, gui, state_bridge


def test_terminal_handler_saves_on_success():
    """A terminal event still saves the session exactly once (unchanged)."""
    processor, lifecycle, _gui, state_bridge = _make_processor()

    processor.process_event({
        'type': 'agent_responded',
        'session_id': 'sess-1',
        'content': 'the answer',
        'reasoning': 'because',
        'created_at': '2026-10-18T10:00:00',
    })

    assert lifecycle.save_count == 1
    assert state_bridge.current_session.final_content == 'the answer'
    assert state_bridge.current_session.final_reasoning == 'because'


def test_non_terminal_event_does_not_save():
    """Granularity is unchanged: a non-terminal event never writes a session."""
    processor, lifecycle, _gui, _state_bridge = _make_processor()

    processor.process_event({'type': 'turn', 'session_id': 'sess-1', 'turn': 3})

    assert lifecycle.save_count == 0


def test_terminal_handler_saves_when_pre_save_step_raises():
    """A step that raises before the save must not skip the save.

    This is the regression the fix exists for: the exception still propagates
    to the caller (handler isolation is out of scope), but the turn is on disk.
    """
    processor, lifecycle, gui, _state_bridge = _make_processor(
        gui=_FakeGui(fail_on_status=True),
    )

    with pytest.raises(RuntimeError):
        processor.process_event({
            'type': 'agent_responded',
            'session_id': 'sess-1',
            'content': 'the answer',
        })

    assert gui.fail_on_status is True
    assert lifecycle.save_count == 1


def test_callback_swallow_is_logged(monkeypatch, capsys):
    """A raising plain event callback is logged, not just traceback-printed.

    The unified ``agent.logging.log`` facade writes to stderr and forwards to
    the JSONL AgentLogger; it emits no stdlib ``logging`` records, so ``caplog``
    cannot observe it (see tests/test_boot_sweep_registry_fail_closed.py).  The
    module-level ``log`` seam is the equivalent assertion point.
    """
    import agent.controller as controller_module
    import agent.logging.lifecycle as lifecycle_module

    # _emit_event best-effort writes to the session log; keep it off disk.
    monkeypatch.setattr(lifecycle_module, 'log_session_event', lambda *a, **k: None)

    warnings = []

    def _record(level, tag, message, *args, **kwargs):
        warnings.append((str(level).upper(), tag, message))

    monkeypatch.setattr(controller_module, 'log', _record)

    calls = []

    def _boom(event):
        calls.append('boom')
        raise RuntimeError('callback exploded')

    def _ok(event):
        calls.append('ok')

    controller = object.__new__(controller_module.AgentController)
    controller.current_session_id = 'sess-1'
    controller.event_queue = queue.Queue()
    controller._event_callbacks = [_boom, _ok]

    controller._emit_event({'type': 'turn', 'session_id': 'sess-1'})

    # The failure was recorded through the log facade, at WARNING.
    recorded = [w for w in warnings if w[0] == 'WARNING']
    assert recorded, f'raising event callback was not logged: {warnings!r}'
    _level, tag, message = recorded[0]
    assert tag == 'core.controller'
    assert 'turn' in message and 'callback exploded' in message

    # ... and not merely dumped to stderr as a traceback.
    assert 'Traceback' not in capsys.readouterr().err

    # One bad callback does not stop the others.
    assert calls == ['boom', 'ok']
