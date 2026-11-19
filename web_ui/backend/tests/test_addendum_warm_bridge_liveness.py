"""The addendum route must see the bridge the SERVER holds warm.

The tab-set registry (``web_ui.backend.event_forwarder._active_tab_bridges``)
is NOT the only place a session can be live.  A bridge is unregistered from it
when its tab's websocket disconnects, and the server's warm-reuse path
(``web_ui.backend.server``: ``bridge = existing``) re-attaches the cached bridge
WITHOUT registering it again.  The authoritative warm cache is
``web_ui.backend.server._session_bridges`` (``session_id -> bridge``).

Pinned here:

* a session held warm by the server -- absent from the tab set -- still
  resolves LIVE, so an accepted ``POST .../addendum`` lands on the object the
  agent holds rather than on disk alone;
* because it landed on that object, the agent's next real end-of-turn save
  (``WebAgentBridge.save_session``) PERSISTS the entry instead of overwriting
  the record with a snapshot taken before the write -- the lost-write bug this
  file exists to keep fixed;
* the entry is in the session the agent reads and in the block the next query
  renders;
* the COLD path (nobody holds the session) is unchanged;
* ``GET .../addendum/effective`` reports which registry supplied the resolution.

Isolation follows ``test_addendum_write_through.py`` / ``test_addendum_live_chain.py``:
the store behind the routes is replaced by monkeypatching
``session_routes._get_store``, every vault-backed path is redirected into
``tmp_path``, and BOTH process-wide registries are emptied for the duration and
restored afterwards (the tab set and the server's warm cache).
"""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import web_ui.backend.session_routes as session_routes
from agent.core.llm_client import render_addendum_block
from session.models import Session
from session.store import FileSystemSessionStore
from web_ui.backend.addendum_routes import (
    AddendumWriteBody,
    _live_bridges,
    _resolve_addendum_session,
    get_session_addendum_effective,
    post_session_addendum,
)
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.event_forwarder import _active_tab_bridges

SID = "sess-warm-bridge-liveness"
TEXT = "PW 33553"


# ── helpers ──────────────────────────────────────────────────────────────────


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


def _post_text(text: str, session_id: str = SID):
    return _unpack(asyncio.run(
        post_session_addendum(
            _request("POST"), session_id, AddendumWriteBody(text=text)
        )
    ))


def _get_effective(session_id: str = SID):
    return _unpack(asyncio.run(
        get_session_addendum_effective(
            _request("GET", path=f"/api/session/{session_id}/addendum/effective"),
            session_id,
        )
    ))


def _seed(store: FileSystemSessionStore, session_id: str = SID) -> Session:
    """Persist an empty-budgeted session (workspace-less: it stays in tmp)."""
    session = Session(session_id=session_id)
    session.addendum = {"entries": [], "budget_chars": 8000, "max_entries": 64}
    store.save_session(session)
    return session


def _texts(session: Session) -> List[str]:
    return [item["text"] for item in (session.addendum or {}).get("entries", [])]


def _server_module():
    """The LIVE ``web_ui.backend.server`` object, resolved at CALL time.

    A peer test in the top-level ``tests/`` tree purges and re-imports the
    server module, which replaces the ``sys.modules`` entry; resolving through
    ``sys.modules`` here keeps this file's fixture and the route module's own
    ``_warm_registry_bridges`` lookup on the same object.
    """
    return importlib.import_module("web_ui.backend.server")


def _warm_bridge(store: FileSystemSessionStore, session: Session) -> WebAgentBridge:
    """A REAL bridge holding *session*, left OUT of the tab-set registry.

    That is the post-F5 shape: the server keeps the bridge warm, the tab set
    does not know about it.
    """
    bridge = WebAgentBridge(event_callback=lambda event: None, session_store=store)
    bridge._session = session
    bridge._loaded_session = session
    return bridge


# ── fixtures ─────────────────────────────────────────────────────────────────


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


@pytest.fixture(autouse=True)
def warm_registry():
    """Empty the server's warm bridge cache for the test; restore it after.

    Yields the cache itself, so a test can put a bridge in it and the route
    module (which reads it live, off ``sys.modules``) sees the same object.
    """
    registry = _server_module()._session_bridges
    saved = dict(registry)
    registry.clear()
    try:
        yield registry
    finally:
        registry.clear()
        registry.update(saved)


# ── the tests ────────────────────────────────────────────────────────────────


def test_warm_bridge_is_found_when_absent_from_tab_set(
    store, routes_store, warm_registry
):
    """A warm-only bridge IS a live bridge; the tab set is not the whole story."""
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _warm_bridge(store, live)
    warm_registry[SID] = bridge

    # Precondition: the tab-set registry cannot see it (post-F5 shape).
    assert _active_tab_bridges == set()
    assert bridge not in _active_tab_bridges

    assert [pair[0] for pair in _live_bridges(SID)] == [bridge]

    _store, bridges, session = _resolve_addendum_session(SID)
    assert bridges, "a bridge the server holds warm must resolve as live"
    assert bridges[0][0] is bridge
    assert session is live


def test_accepted_entry_survives_the_agents_end_of_turn_save(
    store, routes_store, warm_registry
):
    """The write lands on the agent's object, so the next save persists it."""
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _warm_bridge(store, live)
    warm_registry[SID] = bridge
    assert _texts(live) == []

    status, body = _post_text(TEXT)

    assert status == 200, body
    assert body["status"] == "accepted"
    assert body["entry"]["text"] == TEXT
    # The accepted entry reached the object the end-of-turn save persists.
    assert _texts(live) == [TEXT]

    # The REAL end-of-turn save: it snapshots ``_session`` and writes it.
    saved = bridge.save_session()
    assert saved is not None, "the real end-of-turn save failed"

    reloaded = store.load_session(SID)
    assert reloaded is not None
    assert TEXT in _texts(reloaded), _texts(reloaded)


def test_agent_session_and_rendered_block_carry_the_entry(
    store, routes_store, warm_registry
):
    """The entry is in the session the agent reads AND in the rendered block."""
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _warm_bridge(store, live)
    bridge._agent = SimpleNamespace(session=live)
    warm_registry[SID] = bridge
    assert render_addendum_block(live) == ""

    status, body = _post_text(TEXT)

    assert status == 200, body
    assert body["status"] == "accepted"
    assert TEXT in [item["text"] for item in live.addendum["entries"]]
    assert bridge._agent.session is live
    assert TEXT in render_addendum_block(live)


def test_cold_path_still_works_without_any_registry(
    store, routes_store, warm_registry
):
    """Nobody holds the session: the store's atomic read-modify-write decides."""
    _seed(store)
    assert _live_bridges(SID) == []
    assert warm_registry == {}

    status, body = _post_text(TEXT)

    assert status == 200, body
    assert body["status"] == "accepted"
    assert body["entry"]["text"] == TEXT

    reloaded = store.load_session(SID)
    assert reloaded is not None
    assert TEXT in _texts(reloaded), _texts(reloaded)


def test_effective_diagnostics_report_warm_registry(
    store, routes_store, warm_registry
):
    """The diagnostic names the registry that supplied the resolution."""
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _warm_bridge(store, live)
    warm_registry[SID] = bridge
    assert _active_tab_bridges == set()

    status, body = _get_effective()

    assert status == 200, body
    assert body["resolved_via"] == "live"
    diagnostics = body["diagnostics"]
    assert diagnostics["warm_registry_bridges"] >= 1
    assert diagnostics["registry_source"] == "warm"
