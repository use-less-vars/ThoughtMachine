"""Concurrent-session isolation tests for the WebSocket server.

R3 (T1-T4): concurrent sessions must be fully isolated from each other.

These tests drive TWO simultaneous WebSocket connections against one hermetic
server instance and verify:

1. ``test_concurrent_sessions_respond_independently``
   - Each connection's ``new_session`` yields a distinct ``session_id``.
   - A query on session A produces events ONLY on A's socket; session B's
     socket stays silent while A is running (and vice versa).
   - No event, ``session_id``, or message content ever crosses the boundary.

2. ``test_worker_events_delivered_to_owning_session_only``
   - A worker event whose ``data`` carries a ``session_id`` is delivered only
     to the WebSocket owning that session.
   - A worker event WITHOUT a ``session_id`` in its ``data`` passes the bridge
     filter (``if data.get('session_id') and data['session_id'] != self._session_id``)
     and is therefore broadcast to EVERY connected bridge/socket.  This is
     documented behaviour of ``web_ui/backend/bridge.py``; the test asserts
     both directions of the filter.

3. ``test_closing_one_session_leaves_other_running``
   - Closing one client's WebSocket does not tear down the other session:
     the surviving connection still completes a follow-up query.

Hermetic harness: temp HOME + patched ``Path.home()`` + purged/re-imported
server modules with a registered MockProvider, exactly like
``tests/web_ui/backend/test_ws_mock_provider.py``.  No network, no LLM, no
Docker daemon involved.

Pool hazard note (see ``recv_n`` / ``poll_for_type``): a timed-out receive
leaves the single pool worker blocked inside ``ws.receive_text`` and that
worker consumes the NEXT message that arrives on the socket.  Test steps are
therefore ordered so that no receive is needed on a socket AFTER a timed-out
receive on it: silence checks / leftover drains are always the LAST reads on a
socket (or the socket receives >= 2 messages so the poll still succeeds).

Run (from repo root):
    python -m pytest tests/integration/test_concurrent_sessions.py -v
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import shutil
import sys as sys_mod
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from llm_providers.base import LLMProvider, ProviderConfig, LLMResponse
from llm_providers.factory import ProviderFactory

# Same singleton instance the bridges subscribe to (agent.events is NOT purged
# by the fixture, so the re-imported server shares this object).
from agent.events import global_event_bus, WorkerStatusEvent

pytestmark = pytest.mark.integration


# ══════════════════════════════════════════════════════════════════════════════
# Module-level single-threaded executor for WebSocket reads.
# NOT wrapped in a ``with`` block — the executor lives for the full process
# lifetime so that a timed-out ``ws.receive_text()`` thread doesn't block
# cleanup.
# ══════════════════════════════════════════════════════════════════════════════
_receive_pool = ThreadPoolExecutor(max_workers=1)


# ══════════════════════════════════════════════════════════════════════════════
# MockProvider — a fake LLM provider for testing (mirrored verbatim from
# tests/web_ui/backend/test_ws_mock_provider.py)
# ══════════════════════════════════════════════════════════════════════════════

class MockProvider(LLMProvider):
    """A mock LLM provider that returns canned responses."""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.call_count = 0
        self.last_messages: Optional[List[Dict[str, Any]]] = None
        self.last_tools: Optional[List[Dict[str, Any]]] = None
        self._response_text = "This is a mock response from the test provider."

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        self.last_tools = tools
        return LLMResponse(
            content=self._response_text,
            reasoning="mock reasoning",
            tool_calls=None,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            provider="mock",
            model="mock-model",
        )

    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        # Return a fixed token count for testing
        return 42


def _register_mock_provider():
    """Register MockProvider if not already registered."""
    if "mock" not in ProviderFactory._get_providers():
        ProviderFactory.register_provider("mock", MockProvider)


@pytest.fixture(scope="module")
def mock_server():
    """Temp HOME + patched Path.home() + MockProvider registration + fresh server import."""
    tmp_home = tempfile.mkdtemp(prefix="test_mock_home_")
    fake_home = Path(tmp_home)

    # Set HOME env var
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    # Clear real API keys to prevent any accidental use
    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    # Patch Path.home()
    patcher = patch.object(pathlib.Path, "home", return_value=fake_home)
    patcher.start()

    # Register MockProvider BEFORE importing server
    _register_mock_provider()

    # Clear cached modules so re-import picks up the mock
    mod_prefixes = (
        "web_ui.backend", "agent.config.provider_profile", "thoughtmachine.bootstrap"
    )
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield app, tmp_home

    # Cleanup
    patcher.stop()
    if old_home is not None:
        os.environ["HOME"] = old_home
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val
    shutil.rmtree(tmp_home, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_mock_provider():
    """Reset MockProvider call tracking between tests."""
    if "mock" in ProviderFactory._get_providers():
        provider_cls = ProviderFactory._get_providers()["mock"]
        if hasattr(provider_cls, "reset_all"):
            provider_cls.reset_all()


# Add a reset mechanism to MockProvider
MockProvider._instances = []

_orig_mock_init = MockProvider.__init__


def _tracking_init(self, config):
    _orig_mock_init(self, config)
    MockProvider._instances.append(self)


MockProvider.__init__ = _tracking_init


@classmethod
def reset_all(cls):
    for inst in cls._instances:
        inst.call_count = 0
        inst.last_messages = None
        inst.last_tools = None


MockProvider.reset_all = reset_all


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def recv_n(ws, n: int, timeout: float = 5.0) -> list:
    """Receive exactly *n* text messages from the WebSocket.
    Uses a thread pool to enforce a real wall-clock timeout."""
    messages = []
    deadline = time.monotonic() + timeout
    for _ in range(n):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        future = _receive_pool.submit(ws.receive_text)
        try:
            raw = future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            break
        messages.append(json.loads(raw))
    return messages


def poll_for_type(ws, expected_type: str, timeout: float = 5.0) -> list:
    """Receive messages until one of type ``expected_type`` is found.
    Uses a thread pool to enforce a real wall-clock timeout."""
    messages = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        future = _receive_pool.submit(ws.receive_text)
        try:
            raw = future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            break
        msg = json.loads(raw)
        messages.append(msg)
        if msg.get("type") == expected_type:
            break
    return messages


def new_session(ws):
    """Create a new session and drain the initial 5 lifecycle events."""
    ws.send_json({"command": "new_session"})
    return recv_n(ws, 5, timeout=5.0)


def _types(messages) -> List[str]:
    return [m.get("type") for m in messages]


def _session_id_from(messages) -> Optional[str]:
    """Extract the session_id carried by the session_loaded event."""
    for m in messages:
        if m.get("session_id"):
            return m["session_id"]
    return None


def _assert_new_session_events(messages, label: str):
    expected = [
        "session_loaded", "tokens_updated",
        "context_updated", "config_changed", "status_message",
    ]
    assert [m.get("type") for m in messages] == expected, (
        f"{label}: unexpected new_session event sequence: {_types(messages)}"
    )


def _assert_no_reference(messages, needle: str, label: str):
    """Assert *needle* (a session_id or query text) appears in NO message."""
    for m in messages:
        assert needle not in json.dumps(m), (
            f"{label}: {needle!r} leaked into a message: {m}"
        )


def _assert_worker_event(messages, session_id, worker: str, label: str):
    """Assert the polled messages contain a worker:worker_status event.

    ``session_id`` is the expected ``data.session_id``; pass ``None`` to
    assert the event carries NO session_id (tagless broadcast).
    """
    worker_msgs = [m for m in messages if m.get("type") == "worker:worker_status"]
    assert worker_msgs, f"{label}: no worker:worker_status received; got {_types(messages)}"
    event = worker_msgs[-1]
    assert event.get("worker_name") == worker, (
        f"{label}: unexpected worker_name {event.get('worker_name')!r}: {event}"
    )
    if session_id is None:
        assert not event.get("data", {}).get("session_id"), (
            f"{label}: tagless event unexpectedly carries a session_id: {event}"
        )
    else:
        assert event["data"].get("session_id") == session_id, (
            f"{label}: worker event tagged for the wrong session: {event}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestConcurrentSessions:

    def test_concurrent_sessions_respond_independently(self, mock_server):
        """Two concurrent sessions answer only on their own socket."""
        app, _tmp_home = mock_server
        query_a = "hello from session A"
        query_b = "hello from session B"

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws_a:
                with client.websocket_connect("/ws") as ws_b:
                    # Both sessions start independently with distinct ids.
                    msgs_a = new_session(ws_a)
                    sid_a = _session_id_from(msgs_a)
                    _assert_new_session_events(msgs_a, "session A")

                    msgs_b = new_session(ws_b)
                    sid_b = _session_id_from(msgs_b)
                    _assert_new_session_events(msgs_b, "session B")

                    assert sid_a and sid_b, "new_session must yield session_ids"
                    assert sid_a != sid_b, "Each connection must get its own session_id"

                    # A query on A produces events ONLY on A's socket.
                    ws_a.send_json({"command": "continue_session", "query": query_a})
                    a_query_msgs = poll_for_type(ws_a, "status_message", timeout=8.0)
                    assert any(
                        m.get("type") == "conversation_changed" for m in a_query_msgs
                    ), f"No conversation_changed on A's socket: {_types(a_query_msgs)}"
                    assert any(
                        m.get("type") == "status_message" for m in a_query_msgs
                    ), f"No status_message on A's socket: {_types(a_query_msgs)}"
                    _assert_no_reference(
                        a_query_msgs, sid_b, "A's query events must not reference session B"
                    )

                    # B's socket stays silent while A runs (isolation check).
                    b_silence = recv_n(ws_b, 1, timeout=1.0)
                    assert b_silence == [], (
                        f"Session B received events while session A was running: "
                        f"{_types(b_silence)}"
                    )

                    # B answers its own query independently.
                    ws_b.send_json({"command": "continue_session", "query": query_b})
                    b_query_msgs = poll_for_type(ws_b, "status_message", timeout=8.0)
                    assert any(
                        m.get("type") == "status_message" for m in b_query_msgs
                    ), f"No status_message on B's socket after its own query: {_types(b_query_msgs)}"
                    _assert_no_reference(
                        b_query_msgs, sid_a, "B's query events must not reference session A"
                    )

                    # Drain leftover lifecycle events — these are the LAST reads
                    # on each socket (a timeout here poisons the pool worker).
                    a_leftover = recv_n(ws_a, 3, timeout=1.2)
                    b_leftover = recv_n(ws_b, 2, timeout=1.0)

        # Final invariants over every message each socket received.
        all_a = msgs_a + a_query_msgs + a_leftover
        all_b = msgs_b + b_silence + b_query_msgs + b_leftover

        for msg in all_a:
            if msg.get("session_id"):
                assert msg["session_id"] == sid_a, (
                    f"Session A received an event for another session: {msg}"
                )
            _assert_no_reference(
                [msg], query_b, f"Session A received content from session B"
            )
        for msg in all_b:
            if msg.get("session_id"):
                assert msg["session_id"] == sid_b, (
                    f"Session B received an event for another session: {msg}"
                )
            _assert_no_reference(
                [msg], query_a, f"Session B received content from session A"
            )

    def test_worker_events_delivered_to_owning_session_only(self, mock_server):
        """
        Worker events tagged with a session_id reach only that session's socket;
        tagless worker events are broadcast to every bridge.

        Documented bridge filter (web_ui/backend/bridge.py): an event is dropped
        only when ``data['session_id']`` is present AND mismatched, so events
        without a session_id pass the filter on ALL bridges.
        """
        app, _tmp_home = mock_server

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws_a:
                with client.websocket_connect("/ws") as ws_b:
                    msgs_a = new_session(ws_a)
                    sid_a = _session_id_from(msgs_a)
                    msgs_b = new_session(ws_b)
                    sid_b = _session_id_from(msgs_b)
                    assert sid_a and sid_b and sid_a != sid_b

                    # Run one query per session so each bridge's _session_id is
                    # captured (create_session alone does NOT set it; the bridge
                    # learns its session_id from controller events during a query).
                    ws_a.send_json({"command": "continue_session", "query": "prime A"})
                    poll_for_type(ws_a, "status_message", timeout=8.0)
                    ws_b.send_json({"command": "continue_session", "query": "prime B"})
                    poll_for_type(ws_b, "status_message", timeout=8.0)

                    # Tagged for A → only A's socket receives it.
                    global_event_bus.publish(WorkerStatusEvent(data={
                        "worker_name": "w1", "status": "running", "session_id": sid_a,
                    }))
                    a_msgs = poll_for_type(ws_a, "worker:worker_status", timeout=3.0)
                    _assert_worker_event(a_msgs, session_id=sid_a, worker="w1",
                                         label="tagged-A on A")
                    _assert_no_reference(
                        a_msgs, sid_b, "A's worker messages must not reference session B"
                    )

                    # Tagged for B → only B's socket receives it.
                    global_event_bus.publish(WorkerStatusEvent(data={
                        "worker_name": "w1", "status": "running", "session_id": sid_b,
                    }))
                    b_msgs = poll_for_type(ws_b, "worker:worker_status", timeout=3.0)
                    _assert_worker_event(b_msgs, session_id=sid_b, worker="w1",
                                         label="tagged-B on B")
                    _assert_no_reference(
                        b_msgs, sid_a, "B's worker messages must not reference session A"
                    )

                    # Tagless → passes the filter on every bridge (documented).
                    global_event_bus.publish(WorkerStatusEvent(data={
                        "worker_name": "w1", "status": "running",
                    }))
                    a_tagless = poll_for_type(ws_a, "worker:worker_status", timeout=3.0)
                    _assert_worker_event(a_tagless, session_id=None, worker="w1",
                                         label="tagless on A")
                    b_tagless = poll_for_type(ws_b, "worker:worker_status", timeout=3.0)
                    _assert_worker_event(b_tagless, session_id=None, worker="w1",
                                         label="tagless on B")

                    # Cross-checks LAST (these may time out and poison the pool
                    # worker, so nothing may be read afterwards).
                    b_cross = recv_n(ws_b, 5, timeout=1.0)
                    a_cross = recv_n(ws_a, 5, timeout=1.0)

        assert not [m for m in b_cross if m.get("type") == "worker:worker_status"], (
            f"Session B received a worker event it should not have: {b_cross}"
        )
        assert not [m for m in a_cross if m.get("type") == "worker:worker_status"], (
            f"Session A received a worker event it should not have: {a_cross}"
        )
        for msg in b_cross:
            _assert_no_reference([msg], sid_a, "Session B got an event tagged for A")
        for msg in a_cross:
            _assert_no_reference([msg], sid_b, "Session A got an event tagged for B")

    def test_closing_one_session_leaves_other_running(self, mock_server):
        """Closing one session's WebSocket does not affect the other session."""
        app, _tmp_home = mock_server

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws_a:
                with client.websocket_connect("/ws") as ws_b:
                    new_session(ws_a)
                    new_session(ws_b)

                    # B runs a query successfully.
                    ws_b.send_json({
                        "command": "continue_session", "query": "first query on B",
                    })
                    b_first = poll_for_type(ws_b, "status_message", timeout=8.0)
                    assert any(
                        m.get("type") == "status_message" for m in b_first
                    ), f"B's first query produced no status_message: {_types(b_first)}"

                    # A closes its connection (client-side close).
                    ws_a.close()

                    # B must still respond to a follow-up query.
                    ws_b.send_json({
                        "command": "continue_session", "query": "second query on B",
                    })
                    b_second = poll_for_type(ws_b, "status_message", timeout=8.0)
                    assert any(
                        m.get("type") == "status_message" for m in b_second
                    ), (
                        "Session B stopped responding after session A was closed: "
                        f"got {_types(b_second)}"
                    )
