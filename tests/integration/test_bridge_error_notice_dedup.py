"""Error-branch narrowing: ``WebAgentBridge._map_and_emit`` must NOT emit the
transient ``⚠ Error: …`` ``status_message`` bubble when the core already
persisted a **corresponding** ``[SYSTEM NOTIFICATION]`` notice in
``session.user_history`` (otherwise the operator sees the same error twice).

3 of the 8 raw ``error`` producers are SUPPRESSED when a matching notice is
already the last ``user_history`` message: P2 agent ``LLM_ERROR``, P3 agent
``PROVIDER_ERROR`` and P4 agent unexpected ``Exception``.  The other 5 always
keep the ⚠ bubble: P1 agent ``invalid_config`` and the controller
``AGENT_CREATION_ERROR`` producers (P5 agent-start failure, P7 restart failure)
own a first-class UI surface (``config_apply_failed`` / agent-start text) and are
explicitly excluded; P6 (context-full) and P8 (controller error) never persist a
matching notice.  The duplicate and bridge-only families can emit byte-identical
event dicts (P5 vs P7), so the only discriminator is session state: the notice is
the *last* history message and it embeds this error's ``message`` text.

See .thoughtmachine/reports/bug-core-messages-not-truthful-narrow-fix.md
"""
from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import sys as sys_mod
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


# ── hermetic import (temp HOME + purged modules) — mirrors repo harness ──────
@pytest.fixture(scope="module")
def bridge_mods():
    tmp_home = tempfile.mkdtemp(prefix="test_bridge_notice_dedup_")
    fake_home = Path(tmp_home)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home
    patcher = patch.object(pathlib.Path, "home", return_value=fake_home)
    patcher.start()

    mod_prefixes = ("web_ui.backend", "session", "agent.core.message")
    for name in list(sys_mod.modules.keys()):
        if any(name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[name]

    bridge_mod = importlib.import_module("web_ui.backend.bridge")
    models = importlib.import_module("session.models")
    store = importlib.import_module("session.store")
    message = importlib.import_module("agent.core.message")

    yield {
        "WebAgentBridge": bridge_mod.WebAgentBridge,
        "Session": models.Session,
        "FileSystemSessionStore": store.FileSystemSessionStore,
        "Message": message.Message,
    }

    patcher.stop()
    if old_home is not None:
        os.environ["HOME"] = old_home
    else:
        os.environ.pop("HOME", None)
    shutil.rmtree(tmp_home, ignore_errors=True)


SESSION_ID = "sess-narrow-fix"


def _notice(bridge_mods, text):
    return bridge_mods["Message"](role="user", content=text, is_system_notification=True)


def _plain(text, role="user"):
    from agent.core.message import Message
    return Message(role=role, content=text)


def _bridge_with_history(bridge_mods, history=None):
    """Fresh bridge (no threads) whose session carries *history* as its tail."""
    bridge = bridge_mods["WebAgentBridge"](session_store=bridge_mods["FileSystemSessionStore"]())
    events: list = []
    bridge.set_event_callback(events.append)
    session = bridge_mods["Session"](session_id=SESSION_ID)
    for msg in (history or []):
        session.user_history.append(msg)
    bridge._session = session
    bridge._session_id = SESSION_ID
    bridge._history_version = session.conversation_version
    return bridge, events


def _status_texts(events):
    return [e.get("text") for e in events if e.get("type") == "status_message"]


# ══════════════════════════════════════════════════════════════════════════
# D. DUPLICATE producers — core persisted a matching notice → NO ⚠ bubble
# ══════════════════════════════════════════════════════════════════════════

_DUP_CASES = [
    pytest.param(
        "[SYSTEM NOTIFICATION] Error: LLM_ERROR: connection reset by peer",
        {"type": "error", "error_type": "LLM_ERROR", "stop_reason": "error",
         "message": "connection reset by peer", "traceback": "...", "turn": 3,
         "context_length": 0, "usage": {}},
        id="P2-agent-LLMError",
    ),
    pytest.param(
        "[SYSTEM NOTIFICATION] Error: PROVIDER_ERROR: openai API error 500",
        {"type": "error", "error_type": "PROVIDER_ERROR", "stop_reason": "error",
         "message": "openai API error 500", "traceback": "...", "turn": 3,
         "context_length": 0, "usage": {}},
        id="P3-agent-ProviderError",
    ),
    pytest.param(
        "[SYSTEM NOTIFICATION] Error: UNEXPECTED_ERROR: division by zero",
        {"type": "error", "error_type": "UNEXPECTED_ERROR", "stop_reason": "error",
         "message": "division by zero", "traceback": "...", "turn": 3},
        id="P4-agent-unexpected",
    ),
]


@pytest.mark.parametrize("notice_text,event", _DUP_CASES)
def test_duplicate_error_suppresses_bubble(bridge_mods, notice_text, event):
    bridge, events = _bridge_with_history(bridge_mods, [_notice(bridge_mods, notice_text)])
    bridge._map_and_emit(event)

    assert _status_texts(events) == [], (
        "core already persisted a matching [SYSTEM NOTIFICATION] — the bridge "
        f"must NOT duplicate it as ⚠ status_message; got {_status_texts(events)!r}"
    )
    # …but the durable conversation sync must still fire.
    assert any(e.get("type") == "conversation_changed" for e in events), (
        "conversation_changed re-sync must be preserved"
    )


# ══════════════════════════════════════════════════════════════════════════
# B. BRIDGE-ONLY producers — no matching notice → ⚠ bubble IS emitted
# ══════════════════════════════════════════════════════════════════════════

_ONLY_CASES = [
    pytest.param(
        [_plain("Please summarise the repo.")],
        {"type": "error",
         "error": "Context too large. Please summarise manually and retry. (12345 tokens)",
         "stop_reason": "context_full"},
        "unknown",  # P6 sends 'error', NOT 'message' → renders ⚠ Error: unknown
        id="P6-agent-context_full",
    ),
    pytest.param(
        [_plain("start me")],
        {"type": "error", "error_type": "AGENT_CREATION_ERROR",
         "message": "provider 'anthropic' not configured", "traceback": "..."},
        "provider 'anthropic' not configured",
        id="P7-controller-restart_failure",
    ),
]


# P8 needs a Message instance, so build its case inline.
def _p8_case(bridge_mods):
    return (
        [_plain("hi"),
         _notice(bridge_mods, "[SYSTEM NOTIFICATION] Context at 95% — consider summarising.")],
        {"type": "error", "error_type": "CONTROLLER_ERROR",
         "message": "unexpected NoneType", "traceback": "..."},
        "unexpected NoneType",
    )


@pytest.mark.parametrize("history,event,expected_msg", _ONLY_CASES)
def test_bridge_only_error_emits_bubble(bridge_mods, history, event, expected_msg):
    bridge, events = _bridge_with_history(bridge_mods, history)
    bridge._map_and_emit(event)

    texts = _status_texts(events)
    assert len(texts) == 1, (
        f"bridge-only error path must still surface the ⚠ bubble; got {texts!r}"
    )
    assert texts[0] == f"⚠ Error: {expected_msg}"


def test_bridge_only_controller_error_with_unrelated_trailing_notice(bridge_mods):
    """P8: a trailing system notification that does NOT embed the message must
    NOT suppress the bubble (correspondence rule)."""
    history, event, expected_msg = _p8_case(bridge_mods)
    bridge, events = _bridge_with_history(bridge_mods, history)
    bridge._map_and_emit(event)

    texts = _status_texts(events)
    assert texts == [f"⚠ Error: {expected_msg}"], (
        f"unrelated trailing notice must not suppress the bubble; got {texts!r}"
    )


def test_matching_notice_not_last_does_not_suppress(bridge_mods):
    """The notice must be the LAST message; an earlier matching notice (already
    followed by a later turn) must not suppress a fresh bridge-only error."""
    history = [
        _notice(bridge_mods, "[SYSTEM NOTIFICATION] Error: PROVIDER_ERROR: earlier boom"),
        _plain("agent reply", role="assistant"),
    ]
    bridge, events = _bridge_with_history(bridge_mods, history)
    bridge._map_and_emit({"type": "error", "error_type": "PROVIDER_ERROR",
                          "message": "earlier boom"})

    assert len(_status_texts(events)) == 1


def test_no_session_still_emits_bubble(bridge_mods):
    """With no session the bridge is the sole surface → always emit."""
    bridge = bridge_mods["WebAgentBridge"](session_store=bridge_mods["FileSystemSessionStore"]())
    events: list = []
    bridge.set_event_callback(events.append)
    bridge._session = None
    bridge._session_id = SESSION_ID

    bridge._map_and_emit({"type": "error", "error_type": "PROVIDER_ERROR", "message": "boom"})
    assert _status_texts(events) == ["⚠ Error: boom"]


# ══════════════════════════════════════════════════════════════════════════
# C. status_message channel intact — deferred-config path (untouched)
# ══════════════════════════════════════════════════════════════════════════

def test_deferred_config_status_message_still_emitted(bridge_mods):
    """The deferred-config failure path in ``_on_controller_event`` is OUT of
    scope and must keep emitting its (non-error) status_message even when the
    history tail is a matching [SYSTEM NOTIFICATION] notice."""
    from types import SimpleNamespace

    bridge, events = _bridge_with_history(
        bridge_mods, [_notice(bridge_mods, "[SYSTEM NOTIFICATION] Error: PROVIDER_ERROR: boom")]
    )
    bridge._controller = SimpleNamespace(is_busy=False)
    bridge._pending_config = {"model": "x"}
    bridge.apply_config = lambda pending: {"error": "boom"}  # type: ignore[assignment]

    bridge._on_controller_event({"type": "tick_state"})

    statuses = _status_texts(events)
    assert "⚠ Failed to apply queued config: boom" in statuses, (
        f"deferred-config status_message must be untouched; got {statuses!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# A. EXCLUDED producers — own a first-class UI surface → ⚠ bubble ALWAYS emits
# ══════════════════════════════════════════════════════════════════════════════

_EXCLUDED_CASES = [
    pytest.param(
        "[SYSTEM NOTIFICATION] Configuration change failed: model 'gpt-99' not "
        "found. The previous configuration remains active. Please fix the "
        "settings and retry.",
        {"type": "error", "error_type": "invalid_config", "stop_reason": "error",
         "message": "model 'gpt-99' not found", "turn": 3},
        "model 'gpt-99' not found",
        id="P1-agent-invalid_config",
    ),
    pytest.param(
        "[SYSTEM NOTIFICATION] Agent failed to start: ANTHROPIC_API_KEY not set",
        {"type": "error", "error_type": "AGENT_CREATION_ERROR",
         "message": "ANTHROPIC_API_KEY not set", "traceback": "..."},
        "ANTHROPIC_API_KEY not set",
        id="P5-controller-agent_start_failure",
    ),
]


@pytest.mark.parametrize("notice_text,event,expected_msg", _EXCLUDED_CASES)
def test_excluded_producer_always_emits_bubble(bridge_mods, notice_text, event, expected_msg):
    """Even when a matching notice is the last history message, the excluded
    producers (P1 ``invalid_config`` / controller ``AGENT_CREATION_ERROR``) MUST
    keep the ⚠ bubble — they own a first-class UI surface."""
    bridge, events = _bridge_with_history(bridge_mods, [_notice(bridge_mods, notice_text)])
    bridge._map_and_emit(event)

    texts = _status_texts(events)
    assert texts == [f"⚠ Error: {expected_msg}"], (
        f"excluded producer must still surface the ⚠ bubble; got {texts!r}"
    )


def test_config_no_key_path_emits_bubble(bridge_mods):
    """The real first-run no-key path: the controller persists an
    ``AGENT_CREATION_ERROR`` "Agent failed to start" notice, then the bridge
    forwards the same ``error`` event → the ⚠ bubble must survive."""
    detail = (
        "No API key found for provider 'openai_compatible'. Provide api_key "
        "parameter or set OPENAI_COMPATIBLE_API_KEY environment variable."
    )
    bridge, events = _bridge_with_history(
        bridge_mods,
        [_notice(bridge_mods, f"[SYSTEM NOTIFICATION] Agent failed to start: {detail}")],
    )
    bridge._map_and_emit({
        "type": "error", "error_type": "AGENT_CREATION_ERROR",
        "message": detail, "traceback": "...",
    })

    texts = _status_texts(events)
    assert texts == [f"⚠ Error: {detail}"], (
        f"no-key AGENT_CREATION_ERROR must still surface the ⚠ bubble; got {texts!r}"
    )


def test_non_error_event_emits_no_error_bubble(bridge_mods):
    """A NON-error event (e.g. ``tool_call``) with a matching notice tail must
    not be turned into a spurious ⚠ error bubble."""
    bridge, events = _bridge_with_history(
        bridge_mods, [_notice(bridge_mods, "[SYSTEM NOTIFICATION] Error: PROVIDER_ERROR: boom")]
    )
    bridge._map_and_emit({"type": "tool_call", "message": "boom"})

    assert _status_texts(events) == [], (
        f"non-error events must not emit an error bubble; got {_status_texts(events)!r}"
    )

