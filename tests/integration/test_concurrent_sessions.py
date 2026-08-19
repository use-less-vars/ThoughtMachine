"""Concurrent-session isolation tests for the WebSocket server.

R3 (T1-T4): concurrent sessions must be fully isolated from each other.

These tests drive TWO simultaneous WebSocket connections against one hermetic
server instance and verify:

1. ``test_two_sessions_same_workspace_run_concurrently``
   - Two concurrent ``new_session`` handshakes yield distinct ``session_id``s
     that resolve to the SAME ``workspace_id``, both sessions appear in the
     open-session registry, and both sockets answer queries.

2. ``test_two_sessions_respond_independently``
   - A query on session A produces events ONLY on A's socket; session B's
     socket stays silent while A is running (and vice versa).
   - No event, ``session_id``, or message content ever crosses the boundary.

3. ``test_worker_in_session_a_not_visible_to_session_b``
   - The WorkerRegistry (``tools/workspace/worker_registry.py``) keys workers
     by ``(session_id, worker_name, instance_id)``: a worker registered for
     session A is invisible to session B's lookups (``get_all_workers`` /
     ``get_worker`` / ``get_event_buses_for_session`` / ``get_event_bus``).

4. ``test_worker_in_session_b_not_visible_to_session_a``
   - Mirror of test 3: a worker registered for session B is invisible to
     session A's lookups.

5. ``test_closing_session_a_leaves_session_b_running``
   - Closing session A's WebSocket does not tear down session B: the
     surviving connection still completes a follow-up query.

6. ``test_worker_events_delivered_only_to_owning_session``
   - A WORKER_SPAWNED event whose ``data`` carries ``session_id`` reaches only
     the owning session's socket, and events published on the worker's
     per-worker EventBus (e.g. ``worker:worker_message``) are forwarded only
     to the owning session's socket — the other socket never sees
     ``worker:*`` events about that worker.

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

# Same singleton instances the bridges use (agent.events and
# tools.workspace.worker_registry are NOT purged by the fixture, so the
# re-imported server shares these objects).
from agent.events import (
    EventBus,
    WorkerMessageEvent,
    WorkerSpawnedEvent,
    WorkerStatusEvent,
    global_event_bus,
)
from tools.workspace.worker_registry import WorkerRegistry

pytestmark = pytest.mark.integration


# ════════════════════════════════════════════════════════════════════════════
# Module-level single-threaded executor for WebSocket reads.
# NOT wrapped in a ``with`` block — the executor lives for the full process
# lifetime so that a timed-out ``ws.receive_text()`` thread doesn't block
# cleanup.
# ════════════════════════════════════════════════════════════════════════════
_receive_pool = ThreadPoolExecutor(max_workers=1)


# ════════════════════════════════════════════════════════════════════════════
# MockProvider — a fake LLM provider for testing (mirrored verbatim from
# tests/web_ui/backend/test_ws_mock_provider.py)
# ════════════════════════════════════════════════════════════════════════════

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

    # Register MockProvider BEFORE importing server. Remember the registry
    # state so teardown can restore it: registering our MockProvider under the
    # global "mock" key would otherwise shadow the class that
    # tests/web_ui/backend/test_ws_mock_provider.py registers later.
    prev_mock_cls = ProviderFactory._get_providers().get("mock")
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
    # Leave the global ProviderFactory registry as we found it (run after
    # yield, so it also restores when a test in this module fails).
    providers = ProviderFactory._get_providers()
    if prev_mock_cls is None:
        providers.pop("mock", None)
    else:
        providers["mock"] = prev_mock_cls


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


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

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


def _assert_worker_typed_event(messages, event_type: str, session_id, worker: str,
                               label: str):
    """Assert the polled messages contain a ``worker:*`` event of *event_type*.

    ``session_id`` is the expected ``data.session_id``; pass ``None`` to
    assert the event carries NO session_id (tagless broadcast).
    """
    worker_msgs = [m for m in messages if m.get("type") == event_type]
    assert worker_msgs, f"{label}: no {event_type} received; got {_types(messages)}"
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


def _assert_worker_event(messages, session_id, worker: str, label: str):
    """Assert the polled messages contain a worker:worker_status event.

    ``session_id`` is the expected ``data.session_id``; pass ``None`` to
    assert the event carries NO session_id (tagless broadcast).
    """
    _assert_worker_typed_event(
        messages, "worker:worker_status", session_id, worker, label
    )


def _assert_worker_spawned_event(messages, session_id, worker: str, label: str):
    """Assert the polled messages contain a worker:worker_spawned event."""
    _assert_worker_typed_event(
        messages, "worker:worker_spawned", session_id, worker, label
    )


def _assert_worker_message_event(messages, session_id, worker: str, label: str):
    """Assert the polled messages contain a worker:worker_message event."""
    _assert_worker_typed_event(
        messages, "worker:worker_message", session_id, worker, label
    )



# ════════════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════════════

def test_two_sessions_respond_independently(mock_server):
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


def test_closing_session_a_leaves_session_b_running(mock_server):
    """Closing session A's WebSocket leaves session B running."""
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



def _prime_session(ws, query: str, label: str):
    """Run one query so the bridge captures its session_id (sets _session_id)."""
    ws.send_json({"command": "continue_session", "query": query})
    msgs = poll_for_type(ws, "status_message", timeout=8.0)
    assert any(m.get("type") == "status_message" for m in msgs), (
        f"{label}: priming query produced no status_message: {_types(msgs)}"
    )


def _register_worker_for_session(registry, session_id: str, worker_name: str) -> EventBus:
    """Register a fake worker + a real per-worker EventBus; return the bus."""
    registry.register_worker(session_id, worker_name, object())
    worker_bus = EventBus()
    registry.register_event_bus(session_id, worker_name, worker_bus)
    return worker_bus


def test_worker_in_session_a_not_visible_to_session_b(mock_server):
    """
    The WorkerRegistry keys workers by (session_id, worker_name, instance_id):
    a worker registered under session A is invisible to session B — absent
    from get_all_workers() under (sid_b, name), get_worker(sid_b, name) is
    None, and the per-worker event bus never appears in B's session buses.
    """
    app, _tmp_home = mock_server
    registry = WorkerRegistry.get_instance()

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws_a:
            with client.websocket_connect("/ws") as ws_b:
                msgs_a = new_session(ws_a)
                sid_a = _session_id_from(msgs_a)
                msgs_b = new_session(ws_b)
                sid_b = _session_id_from(msgs_b)
                assert sid_a and sid_b and sid_a != sid_b

                _prime_session(ws_a, "prime A", "session A")
                _prime_session(ws_b, "prime B", "session B")

                try:
                    worker_bus = _register_worker_for_session(registry, sid_a, "worker_a")

                    # Registry visibility: present under A, absent under B.
                    all_workers = registry.get_all_workers()
                    assert (sid_a, "worker_a", 1) in all_workers, (
                        f"worker_a missing for {sid_a}: {list(all_workers.keys())}"
                    )
                    assert (sid_b, "worker_a", 1) not in all_workers, (
                        f"worker_a visible under {sid_b}: {list(all_workers.keys())}"
                    )
                    assert registry.get_worker(sid_a, "worker_a") is not None
                    assert registry.get_worker(sid_b, "worker_a") is None

                    # Per-worker EventBus visibility is likewise scoped.
                    buses_a = registry.get_event_buses_for_session(sid_a)
                    assert "worker_a" in buses_a and buses_a["worker_a"] is worker_bus
                    buses_b = registry.get_event_buses_for_session(sid_b)
                    assert "worker_a" not in buses_b, (
                        f"Session B can see A's worker bus: {list(buses_b.keys())}"
                    )
                    assert registry.get_event_bus(sid_b, "worker_a") is None

                    # A spawned event for A's worker must not surface on B.
                    global_event_bus.publish(WorkerSpawnedEvent(data={
                        "worker_name": "worker_a", "session_id": sid_a,
                    }))
                    a_msgs = poll_for_type(ws_a, "worker:worker_spawned", timeout=3.0)
                    _assert_worker_spawned_event(
                        a_msgs, session_id=sid_a, worker="worker_a",
                        label="spawned on A",
                    )

                    # B never sees worker:* about worker_a (LAST read on B).
                    b_drain = recv_n(ws_b, 5, timeout=1.0)
                finally:
                    registry.unregister_worker(sid_a, "worker_a")
                    registry.unregister_event_bus(sid_a, "worker_a")

    assert not [m for m in b_drain if m.get("type", "").startswith("worker:")], (
        f"Session B received worker events about A's worker: {b_drain}"
    )



def test_two_sessions_same_workspace_run_concurrently(mock_server):
    """
    Two concurrent ``new_session`` handshakes from the same project root
    resolve to the SAME ``workspace_id``, both sessions are tracked in the
    open-session registry, and both sockets answer queries while the other
    session is alive.
    """
    app, _tmp_home = mock_server
    # The fixture purged and re-imported web_ui.backend.server; importing it
    # here (inside the test) yields that same module instance, so its
    # _session_store singleton is the one shared by the live bridges.
    import web_ui.backend.server as server_mod

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws_a:
            with client.websocket_connect("/ws") as ws_b:
                msgs_a = new_session(ws_a)
                sid_a = _session_id_from(msgs_a)
                _assert_new_session_events(msgs_a, "session A")
                # msgs_a[0] is session_loaded (exact 5-event sequence above).
                ws_a_id = msgs_a[0].get("workspace_id")

                msgs_b = new_session(ws_b)
                sid_b = _session_id_from(msgs_b)
                _assert_new_session_events(msgs_b, "session B")
                ws_b_id = msgs_b[0].get("workspace_id")

                assert sid_a and sid_b and sid_a != sid_b
                assert ws_a_id, (
                    f"session A's session_loaded carried no workspace_id: {msgs_a[0]}"
                )
                assert ws_a_id == ws_b_id, (
                    "Sessions must share the same workspace_id: A=%r B=%r"
                    % (ws_a_id, ws_b_id)
                )

                # Both sessions are listed in the shared open-session store.
                store = server_mod._get_session_store()
                open_ids = store.get_open_sessions()
                assert sid_a in open_ids, (
                    f"session A missing from the open-session registry: {open_ids}"
                )
                assert sid_b in open_ids, (
                    f"session B missing from the open-session registry: {open_ids}"
                )

                # Both sockets answer a query while the other session is alive.
                _prime_session(ws_a, "query on A", "session A")
                _prime_session(ws_b, "query on B", "session B")


def test_worker_in_session_b_not_visible_to_session_a(mock_server):
    """
    Mirror of the session-A test: a worker registered for session B is
    invisible to session A — absent from ``get_all_workers()`` under
    (sid_a, name, instance_id), ``get_worker(sid_a, name)`` is None, and the
    per-worker event bus never appears in A's session buses.
    """
    app, _tmp_home = mock_server
    registry = WorkerRegistry.get_instance()

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws_a:
            with client.websocket_connect("/ws") as ws_b:
                msgs_a = new_session(ws_a)
                sid_a = _session_id_from(msgs_a)
                msgs_b = new_session(ws_b)
                sid_b = _session_id_from(msgs_b)
                assert sid_a and sid_b and sid_a != sid_b

                try:
                    worker_bus = _register_worker_for_session(registry, sid_b, "worker_b")

                    # Registry visibility: present under B, absent under A.
                    all_workers = registry.get_all_workers()
                    assert (sid_b, "worker_b", 1) in all_workers, (
                        f"worker_b missing for {sid_b}: {list(all_workers.keys())}"
                    )
                    assert (sid_a, "worker_b", 1) not in all_workers, (
                        f"worker_b visible under {sid_a}: {list(all_workers.keys())}"
                    )
                    assert registry.get_worker(sid_b, "worker_b") is not None
                    assert registry.get_worker(sid_a, "worker_b") is None

                    # Per-worker EventBus visibility is likewise scoped.
                    buses_b = registry.get_event_buses_for_session(sid_b)
                    assert "worker_b" in buses_b and buses_b["worker_b"] is worker_bus
                    buses_a = registry.get_event_buses_for_session(sid_a)
                    assert "worker_b" not in buses_a, (
                        f"Session A can see B's worker bus: {list(buses_a.keys())}"
                    )
                    assert registry.get_event_bus(sid_a, "worker_b") is None
                finally:
                    registry.unregister_worker(sid_b, "worker_b")
                    registry.unregister_event_bus(sid_b, "worker_b")


def test_worker_events_delivered_only_to_owning_session(mock_server):
    """
    A WORKER_SPAWNED event tagged with a session_id is forwarded only to that
    session's socket, and events published on the worker's per-worker EventBus
    (``worker:worker_message``) are likewise delivered only to the owning
    session's socket.  The other socket never sees ``worker:*`` events about
    the worker.
    """
    app, _tmp_home = mock_server
    registry = WorkerRegistry.get_instance()

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws_a:
            with client.websocket_connect("/ws") as ws_b:
                msgs_a = new_session(ws_a)
                sid_a = _session_id_from(msgs_a)
                msgs_b = new_session(ws_b)
                sid_b = _session_id_from(msgs_b)
                assert sid_a and sid_b and sid_a != sid_b

                # Prime both bridges so _session_id is captured.
                _prime_session(ws_a, "prime A", "session A")
                _prime_session(ws_b, "prime B", "session B")

                try:
                    worker_bus = _register_worker_for_session(registry, sid_a, "worker_a")

                    # Spawned event tagged for A → only A's socket sees it.
                    global_event_bus.publish(WorkerSpawnedEvent(data={
                        "worker_name": "worker_a", "session_id": sid_a,
                    }))
                    spawned = poll_for_type(ws_a, "worker:worker_spawned", timeout=3.0)
                    _assert_worker_spawned_event(
                        spawned, session_id=sid_a, worker="worker_a",
                        label="spawned on A",
                    )
                    _assert_no_reference(
                        spawned, sid_b, "A's spawned event must not reference session B"
                    )

                    # Events on A's per-worker bus reach only A's socket.
                    worker_bus.publish(WorkerMessageEvent(data={
                        "worker_name": "worker_a", "session_id": sid_a,
                        "message": "hello from worker_a",
                    }))
                    a_msgs = poll_for_type(ws_a, "worker:worker_message", timeout=3.0)
                    _assert_worker_message_event(
                        a_msgs, session_id=sid_a, worker="worker_a",
                        label="bus message on A",
                    )
                    assert "hello from worker_a" in json.dumps(a_msgs), (
                        f"A's bus message payload missing: {a_msgs}"
                    )
                    _assert_no_reference(
                        a_msgs, sid_b, "A's bus messages must not reference session B"
                    )

                    # B never sees worker:* about worker_a (LAST read on B).
                    b_drain = recv_n(ws_b, 5, timeout=1.0)
                finally:
                    registry.unregister_worker(sid_a, "worker_a")
                    registry.unregister_event_bus(sid_a, "worker_a")

    assert not [m for m in b_drain if m.get("type", "").startswith("worker:")], (
        f"Session B received worker events about A's worker: {b_drain}"
    )


