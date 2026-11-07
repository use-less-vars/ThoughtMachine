"""W1.5 chunk C: the addendum's FULL CHAIN, driven against a LIVE session.

What is proven here, in one process and without a network:

1. ``POST /api/session/{id}/addendum`` -- the REAL route coroutine
   ``web_ui.backend.addendum_routes.post_session_addendum`` -- accepts an entry
   for a session a bridge in this process is holding live, mutates that very
   object and persists it;
2. the NEXT REAL TURN (``Agent.process_query``) composes the entry into the
   single system-prompt string the model receives;
3. the entry survives the REAL end-of-turn save (``WebAgentBridge.save_session``
   -> ``SessionManager`` -> store) and is still there after a reload from disk.

REAL, i.e. NOT stubbed (production object or coroutine in every case):

* ``session.store.FileSystemSessionStore`` over a tmp vault (save / load /
  ``mutate_session``) and ``session.models.Session`` itself;
* ``web_ui.backend.bridge.WebAgentBridge`` -- a real bridge carrying its real
  ``_session_state_lock`` (bridge.py:510) and real ``SessionManager``
  (bridge.py:527), registered in the real live-bridge registry
  (``web_ui.backend.event_forwarder._active_tab_bridges``);
* the route coroutines ``post_session_addendum`` / ``delete_session_addendum``:
  origin policy, ``_resolve_addendum_session`` and the live write-through
  (``addendum_routes.py:163`` / ``:215``), driven with a real
  ``starlette.requests.Request``;
* ``session.addendum.write_addendum_entry`` (``session/addendum.py:156``) -- the
  ONE budget boundary, including its own refusal sentence and its own numbers;
* ``agent.core.agent.Agent`` + ``Agent.process_query`` -- the real turn loop:
  the real per-turn ``ensure_system_prompt`` (agent/core/agent.py:1217), the real
  composition (``LLMClient.ensure_system_prompt`` agent/core/llm_client.py:201,
  the addendum appended at llm_client.py:225-227 through
  ``render_addendum_block`` llm_client.py:128), the real context build
  (agent/core/agent.py:1334 + ``ContextBuilder._cleanup_orphaned_tool_messages``
  agent/core/agent.py:1344) and the real request object handed to the transport
  at agent/core/agent.py:1398;
* ``WebAgentBridge.save_session`` (bridge.py:2229) -- the real end-of-turn save.

FAKE -- exactly ONE seam, the transport:

``agent.llm_client.stream_completion`` (agent/core/llm_client.py:277).  That is
the call that would leave the process to a provider at agent/core/agent.py:1398.
The fake CAPTURES the ``messages`` list it was handed -- that captured list *is*
the system string "the model receives" in these tests -- and yields a scripted
``TextDelta`` / ``Done`` stream instead of contacting a provider.  No session, no
bridge, no route, no resolver, no composition function and no queue is replaced,
and the API key is the dummy ``'test-key'``: nothing here touches the network.

The ``409`` numbers asserted below are the boundary's own (``attempted_chars``,
``budget_chars``, ``attempted_entries``, ``total_chars``), quoted by the route --
this file never re-derives them.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import web_ui.backend.session_routes as session_routes
from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.llm_client import ADDENDUM_HEADING
from agent.core.stream_events import Done, TextDelta
from session.models import Session
from session.store import FileSystemSessionStore
from web_ui.backend.addendum_routes import (
    AddendumWriteBody,
    delete_session_addendum,
    post_session_addendum,
)
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.event_forwarder import _active_tab_bridges

SID = "sess-live-chain"
TEXT = "ALWAYS use the staging kubeconfig."
QUERY = "which kubeconfig should I use?"

FILL_A = "A" * 20
FILL_B = "B" * 20
NEW_TEXT = "fresh entry after the clear"


# ── the ONE fake: the LLM transport ────────────────────────────────────────


class _CapturingTransport:
    """Scripted async stream that CAPTURES the request instead of sending it.

    Replaces ``agent.llm_client.stream_completion`` only.  ``requests[n]`` is
    the exact ``messages`` list the real turn loop built for call *n* -- i.e.
    what the provider would have received -- so the system string can be
    asserted against the real composition instead of a re-implementation.
    """

    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.requests: List[List[Any]] = []
        self.calls = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        self.requests.append(list(messages))
        for event in self._turns[index]:
            yield event


def _make_agent(session):
    return Agent(
        config=AgentConfig(api_key="test-key", enable_logging=False),
        session=session,
    )


def _drive_turn(agent, query=QUERY):
    """One real turn; returns ``(events, request_messages)``."""
    transport = _CapturingTransport([[TextDelta(text="ok"), Done()]])
    agent.llm_client.stream_completion = transport.stream_completion
    events = list(agent.process_query(query))
    assert transport.calls == 1, f"expected exactly one transport call, got {transport.calls}"
    return events, transport.requests[0]


def _role(message) -> Optional[str]:
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _content(message) -> str:
    if isinstance(message, dict):
        return message.get("content") or ""
    return getattr(message, "content", "") or ""


def _system_text(messages) -> str:
    """The single system message of a built request, as the model would see it."""
    system = [message for message in messages if _role(message) == "system"]
    assert len(system) == 1, f"expected one system message, got {len(system)}"
    return _content(system[0])


def _user_texts(messages) -> List[str]:
    return [_content(message) for message in messages if _role(message) == "user"]


# ── routes / store helpers (real route coroutines, real store) ─────────────


def _request(method: str = "POST", path: Optional[str] = None) -> Request:
    """A bare ASGI request; an absent Origin header is permitted by policy."""
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path or f"/api/session/{SID}/addendum",
        "raw_path": (path or "/").encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 54321),
        "server": ("testserver", 80),
    })


def _unpack(response):
    """``(status_code, body)`` for both a plain dict and a ``JSONResponse``."""
    if isinstance(response, JSONResponse):
        return response.status_code, json.loads(response.body.decode("utf-8"))
    return 200, response


def _post(text: str, session_id: str = SID):
    return _unpack(asyncio.run(
        post_session_addendum(_request("POST"), session_id, AddendumWriteBody(text=text))
    ))


def _delete(session_id: str = SID):
    return _unpack(asyncio.run(
        delete_session_addendum(_request("DELETE"), session_id)
    ))


def _seed(store: FileSystemSessionStore, budget: int = 8000,
          max_entries: int = 64, session_id: str = SID) -> Session:
    """Persist an empty-budgeted session (workspace-less: it stays in tmp)."""
    session = Session(session_id=session_id)
    session.addendum = {"entries": [], "budget_chars": budget, "max_entries": max_entries}
    store.save_session(session)
    return session


def _texts(session: Session) -> List[str]:
    return [item["text"] for item in (session.addendum or {}).get("entries", [])]


def _disk_texts(store: FileSystemSessionStore, session_id: str = SID) -> List[str]:
    session = store.load_session(session_id)
    assert session is not None, f"session {session_id} is not in the store"
    return _texts(session)


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def store(tmp_path, vault):
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
        enable_session_history_pruning=False,
    )


@pytest.fixture
def routes_store(store, monkeypatch):
    """Point the route module's store accessor at the tmp store."""
    monkeypatch.setattr(session_routes, "_get_store", lambda: store)
    return store


@pytest.fixture(autouse=True)
def clean_bridges():
    """Keep the process-wide live-bridge registry out of these tests."""
    saved = set(_active_tab_bridges)
    _active_tab_bridges.clear()
    try:
        yield
    finally:
        _active_tab_bridges.clear()
        _active_tab_bridges.update(saved)


def _live_bridge(store: FileSystemSessionStore, session: Session) -> WebAgentBridge:
    """A REAL bridge holding *session* live, registered as the live tab."""
    bridge = WebAgentBridge(event_callback=lambda event: None, session_store=store)
    bridge._session = session
    _active_tab_bridges.add(bridge)
    return bridge


# ── the chain ──────────────────────────────────────────────────────────────


def test_live_chain_post_turn_and_end_of_turn_save(store, routes_store):
    """POST -> real turn (addendum in the system string) -> real save -> reload.

    The agent is built BEFORE the write, so the entry can only reach the system
    prompt through the per-turn composition (agent/core/agent.py:1217 ->
    agent/core/llm_client.py:225-227) and not through some earlier snapshot.
    """
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _live_bridge(store, live)

    agent = _make_agent(live)
    bridge._agent = agent                      # the running agent's own session
    assert agent.llm_client.session is live
    # Precondition: nothing written yet, so nothing composed yet.
    assert ADDENDUM_HEADING not in agent.conversation[0]["content"]
    assert _texts(live) == []

    # ── 1. the REAL route coroutine, against the LIVE object ──────────────
    status, body = _post(TEXT)
    assert status == 200, body
    assert body["status"] == "accepted"
    assert body["entry"]["text"] == TEXT
    assert body["entry"]["source"] == "operator"
    assert body["total_chars"] == len(TEXT)
    # The route mutated the object the agent (and the bridge) hold, not a copy.
    assert _texts(live) == [TEXT]
    assert bridge._session is live
    # ... and persisted it in the same critical section.
    assert _disk_texts(store) == [TEXT]

    # ── 2. ONE REAL TURN: the model's system string carries the entry ─────
    events, messages = _drive_turn(agent)
    assert [event.get("type") for event in events], "the turn produced no events"
    assert _role(messages[0]) == "system", "the built request must open with the system prompt"
    system = _system_text(messages)
    assert TEXT in system, system
    assert ADDENDUM_HEADING in system
    assert system.index(ADDENDUM_HEADING) < system.index(TEXT), "the entry must sit under the heading"
    assert QUERY in _user_texts(messages), "the turn's own request must be the one captured"

    # ── 3. the chain CLOSES: real end-of-turn save, then reload from disk ─
    saved = bridge.save_session()
    assert saved is not None, "the real end-of-turn save failed"
    assert saved is live, "the end-of-turn save must persist the live object itself"

    reloaded = store.load_session(SID)
    assert reloaded is not None
    assert TEXT in _texts(reloaded), _texts(reloaded)
    # The composed prompt itself is what the live session now holds, so the
    # entry is durable both as data and as the string the next turn will send.
    assert reloaded.user_history[0]["role"] == "system"
    assert TEXT in reloaded.user_history[0]["content"]


def test_live_chain_recovery_409_then_clear_then_next_turn(store, routes_store):
    """Fill the budget to the edge, be refused, clear, re-write, next turn.

    The agent exists before every write, so each POST is applied to the live
    object the running turn loop holds; the turn after the clear must show the
    NEW text and none of the old ones in its system string (the stale system
    prompt from the previous turn is stripped, not appended to).
    """
    _seed(store, budget=40)
    live = store.load_session(SID)
    assert live is not None
    bridge = _live_bridge(store, live)

    agent = _make_agent(live)
    bridge._agent = agent
    assert ADDENDUM_HEADING not in agent.conversation[0]["content"]

    # ── fill to the edge: 20 + 20 == the 40-char budget, exactly ──────────
    status, body = _post(FILL_A)
    assert status == 200, body
    assert body["status"] == "accepted" and body["total_chars"] == 20

    status, body = _post(FILL_B)
    assert status == 200, body
    assert body["status"] == "accepted" and body["total_chars"] == 40

    _events, messages = _drive_turn(agent)
    at_edge = _system_text(messages)
    assert FILL_A in at_edge and FILL_B in at_edge, at_edge

    # ── one character more is refused, with the boundary's own numbers ────
    status, refused = _post("C")
    assert status == 409, refused
    assert refused["status"] == "refused"
    assert refused["reason"] == "budget_exceeded"
    assert refused["message"] == "addendum budget exceeded: 41 > 40 chars"
    assert refused["attempted_chars"] == 41
    assert refused["budget_chars"] == 40
    assert refused["attempted_entries"] == 3
    assert refused["total_chars"] == 40          # the unchanged set, as measured
    assert [entry["text"] for entry in refused["entries"]] == [FILL_A, FILL_B]
    # The refusal changed nothing: neither the live object nor the record.
    assert _texts(live) == [FILL_A, FILL_B]
    assert _disk_texts(store) == [FILL_A, FILL_B]

    # ── DELETE clears; the budget survives the clear ──────────────────────
    status, cleared = _delete()
    assert status == 200, cleared
    assert cleared["status"] == "cleared"
    assert cleared["entries"] == []
    assert cleared["total_chars"] == 0
    assert cleared["budget_chars"] == 40
    assert _texts(live) == []
    assert _disk_texts(store) == []

    # ── the following POST succeeds ───────────────────────────────────────
    status, body = _post(NEW_TEXT)
    assert status == 200, body
    assert body["status"] == "accepted"
    assert body["total_chars"] == len(NEW_TEXT)
    assert _texts(live) == [NEW_TEXT]
    assert _disk_texts(store) == [NEW_TEXT]

    # ── the NEXT REAL TURN shows the new text and none of the old ones ────
    _events, messages = _drive_turn(agent)
    after_clear = _system_text(messages)
    assert NEW_TEXT in after_clear, after_clear
    assert ADDENDUM_HEADING in after_clear
    assert FILL_A not in after_clear, "the cleared entries must not survive in the system prompt"
    assert FILL_B not in after_clear, "the cleared entries must not survive in the system prompt"
    assert after_clear.count(ADDENDUM_HEADING) == 1

    # ── and it closes through the real end-of-turn save ───────────────────
    saved = bridge.save_session()
    assert saved is not None
    assert _texts(store.load_session(SID)) == [NEW_TEXT]
