"""Tests for bug/bridge-config-unknown-error-swallow (consumer-side fix).

``WebAgentBridge.continue_session`` (``web_ui/backend/bridge.py``) applied a
caller-supplied ``config_dict`` via ``apply_config`` and then gated its
post-apply logging on ``result.get("success")``::

    result = self.apply_config(config_dict)
    if result.get("success"):
        log('INFO', 'server.bridge', "Config updated during continue_session: ...")
    else:
        log('WARNING', 'server.bridge',
            f"Config update skipped during continue_session: "
            f"{result.get('error', 'unknown error')}")

``WebAgentBridge.apply_config`` never puts a ``"success"`` key in its result
dict -- its real contract is ``{"config", "settings", "permissions",
"merged_config"}`` (the same ``"config" in result`` shape every other consumer
keys off: the ``server.py`` ``config_changed`` path and
``bridge._on_controller_event``'s deferred-apply path).  So
``result.get("success")`` was always ``None``, the ``if`` branch was dead, and
the ``else`` branch fired unconditionally -- emitting a bogus

    [server.bridge] Config update skipped during continue_session: unknown error

on *every* successful config update.

The fix is consumer-side (the producer's result contract is left untouched):
the bogus success gate is removed, so a successful apply no longer produces
the skip warning.

``agent.logging.log`` (bound as ``web_ui.backend.bridge.log``) prints to stderr
and forwards to a JSONL ``AgentLogger``; it emits no stdlib ``logging`` records,
so ``caplog`` cannot observe it.  These tests monkeypatch the module-level
``log`` binding instead -- the same seam used by
``tests/test_periodic_sweep_registry_recheck.py``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import web_ui.backend.bridge as bridge_mod
from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore


SKIP_WARNING = "Config update skipped during continue_session"


class _FakeController:
    """Minimal live controller -- enough for continue_session to return cleanly."""

    def __init__(self):
        self.is_running = True
        self.continued = []
        self.pushed = []

    def continue_session(self, query):
        self.continued.append(query)

    def request_config_update(self, agent_config):
        self.pushed.append(agent_config)


def _record_log(monkeypatch):
    """Capture ``web_ui.backend.bridge.log(level, category, message, ...)``."""
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, message))

    monkeypatch.setattr(bridge_mod, "log", recorder)
    return events


def _successful_apply_result():
    """The dict ``WebAgentBridge.apply_config`` actually returns on success.

    Deliberately carries NO ``"success"`` / ``"error"`` key -- that absence is
    the whole bug.
    """
    return {
        "config": {"model": "m2"},
        "settings": {},
        "permissions": {},
        "merged_config": {"model": "m2"},
    }


@pytest.fixture
def bridge(tmp_path):
    store = FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
    )
    return WebAgentBridge(event_callback=lambda e: None, session_store=store)


def test_successful_config_apply_does_not_log_skip_warning(bridge, monkeypatch):
    """A successful ``apply_config`` must not emit the 'skipped ... unknown error' warning."""
    events = _record_log(monkeypatch)
    bridge.apply_config = lambda *a, **k: _successful_apply_result()
    bridge._controller = _FakeController()

    bridge.continue_session("hello", {"model": "m2"})

    skip_warnings = [
        (lvl, cat, msg)
        for (lvl, cat, msg) in events
        if lvl == "WARNING" and SKIP_WARNING in str(msg)
    ]
    assert skip_warnings == [], (
        "continue_session emitted a bogus config-skip warning for a successful "
        f"apply_config: {skip_warnings!r}"
    )


def test_continue_session_still_applies_config(bridge, monkeypatch):
    """The consumer fix must not drop the config apply itself."""
    _record_log(monkeypatch)
    seen = {}

    def _apply(cfg, *a, **k):
        seen["cfg"] = cfg
        return _successful_apply_result()

    bridge.apply_config = _apply
    bridge._controller = _FakeController()

    bridge.continue_session("hi", {"model": "m2"})

    assert seen.get("cfg") == {"model": "m2"}, "config_dict was not applied"
