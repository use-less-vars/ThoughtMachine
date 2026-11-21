"""The addendum route must READ the newest generation, not just write it.

PR A (``fix/addendum-writeback-loss``) made a *save* rev-aware: a stale
in-memory snapshot no longer overwrites a newer record.  That closed half of the
problem.  This file pins the other half -- the READ seam.

``_resolve_addendum_session`` prefers a LIVE ``Session`` object (the running
agent's copy) over the store.  Nothing refreshes that object from disk, so a
generation another writer committed -- the cold path's ``store.mutate_session``,
a second process, a warm bridge that was reloaded elsewhere -- is invisible to
every endpoint until the live copy happens to be replaced.  Two consequences,
both pinned here:

1. ``GET .../addendum/effective`` serves a stale entry set and a stale
   ``rendered_block``, so the operator reads a preview that disagrees with the
   record the next resumed turn will read.

2. Worse, ``_write_through`` on the live path calls ``store.save_session``
   DIRECTLY -- it does not go through ``SessionManager.save_session``, so the
   rev-aware reconcile PR A added never runs on an addendum route write.  The
   mutator therefore advances the rev of the STALE copy and writes that whole
   snapshot over the newer record: a route write destroys a disk-only entry.

The fix is one refresh at the seam both cases share: the resolver brings a live
copy up to the stored generation before anything reads or mutates it, so reads
and writes both start from the newest generation.  The three-case reconcile is
shared with ``SessionManager`` (``session/addendum.py``) rather than copied, so
the two seams cannot drift on what "newer" means.

Isolation follows ``test_addendum_warm_bridge_liveness.py``: the store behind
the routes is monkeypatched (``session_routes._get_store``), vault-backed paths
are redirected into ``tmp_path``, and both process-wide registries are emptied
for the duration and restored afterwards.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import List, Optional

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import web_ui.backend.session_routes as session_routes
from session.addendum import addendum_rev, write_addendum_entry
from session.models import Session
from session.store import FileSystemSessionStore
from web_ui.backend.addendum_routes import (
    AddendumWriteBody,
    get_session_addendum_effective,
    post_session_addendum,
)
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.event_forwarder import _active_tab_bridges
from web_ui.backend.session_manager import SessionManager

SID = "sess-read-seam"
LATE_TEXT = "PW 33553"      # written by the OTHER writer, straight to the store
NEW_TEXT = "PW 99001"       # written through the REST route under test


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


def _store(tmp_path) -> FileSystemSessionStore:
    """A SECOND store over the same directories -- another process's view.

    Distinct instance, same files: exactly the shape of the writer this module
    could not see, and the reason the route resolved a live copy in the first
    place.
    """
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
        enable_session_history_pruning=False,
    )


def _seed(store: FileSystemSessionStore, session_id: str = SID) -> Session:
    """Persist an empty-budgeted session (workspace-less: it stays in tmp)."""
    session = Session(session_id=session_id)
    session.addendum = {"entries": [], "budget_chars": 8000, "max_entries": 64}
    store.save_session(session)
    return session


def _warm_bridge(store: FileSystemSessionStore, session: Session) -> WebAgentBridge:
    """A REAL bridge holding *session*, left OUT of the tab-set registry."""
    bridge = WebAgentBridge(event_callback=lambda event: None, session_store=store)
    bridge._session = session
    bridge._loaded_session = session
    return bridge


def _append_from_another_writer(tmp_path, text: str) -> dict:
    """Commit an entry straight to the store, through the real write boundary.

    This is the cold path's own shape -- ``store.mutate_session`` with the
    boundary inside it -- so the entry's id, its ``ts`` and the addendum's new
    generation are the production ones, and no live object learns about it.
    """
    def _mutate(stored: Session):
        result = write_addendum_entry(stored, text, source="operator", notify=False)
        return result, result.get("status") == "ok"

    mutated, result = _store(tmp_path).mutate_session(SID, _mutate)
    assert mutated is not None, "test setup: the session file must exist to be mutated"
    assert result is not None and result.get("status") == "ok", (
        f"test setup: the boundary must accept the other writer's entry; "
        f"result={result!r}"
    )
    return dict(result["entry"])


def _texts(session: Session) -> List[str]:
    return [item["text"] for item in (session.addendum or {}).get("entries", [])]


def _server_module():
    """The LIVE ``web_ui.backend.server`` object, resolved at CALL time."""
    return importlib.import_module("web_ui.backend.server")


# ── fixtures ────────────────────────────────────────────────────────────────


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
    """Empty the server's warm bridge cache for the test; restore it after."""
    registry = _server_module()._session_bridges
    saved = dict(registry)
    registry.clear()
    try:
        yield registry
    finally:
        registry.clear()
        registry.update(saved)


# ── the tests ───────────────────────────────────────────────────────────────


def test_effective_read_shows_an_entry_another_writer_persisted(
    tmp_path, store, routes_store, warm_registry
):
    """The preview is the resolved session's, so it must BE the newest one.

    A live copy is the runtime authority for a WRITE (it is what the turn loop
    reads), but it is not automatically the newest GENERATION.  When the record
    is newer, the resolved copy must be refreshed first: serving the snapshot the
    bridge loaded before the other writer committed is how the panel shows a
    list -- and a rendered block -- that the next resumed turn will not read.
    """
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _warm_bridge(store, live)
    warm_registry[SID] = bridge
    assert _texts(live) == []

    accepted = _append_from_another_writer(tmp_path, LATE_TEXT)

    # Precondition: the record really is newer than the live copy, so "stale
    # live copy" is the state under test rather than a guess.
    on_disk = _store(tmp_path).load_session(SID)
    assert [item["id"] for item in on_disk.addendum["entries"]] == [accepted["id"]]
    assert addendum_rev(on_disk.addendum) > addendum_rev(live.addendum), (
        "test setup: the other writer must have committed a NEWER generation"
    )

    status, body = _get_effective()

    assert status == 200, body
    assert body["resolved_via"] == "live"
    ids = [item["id"] for item in body["entries"]]
    assert accepted["id"] in ids, (
        "the effective read resolved a live copy that is older than the record; "
        f"served ids={ids}, stored id={accepted['id']}"
    )
    assert LATE_TEXT in body["rendered_block"], (
        "the rendered block must be the block the NEXT query carries, and the "
        "record -- not the stale live snapshot -- is what a resumed turn reads"
    )


def test_route_write_keeps_the_generation_the_store_already_has(
    tmp_path, store, routes_store, warm_registry
):
    """A live write must extend the newest generation, never overwrite it.

    ``_write_through`` persists with ``store.save_session`` directly, so the
    rev-aware reconcile in ``SessionManager.save_session`` never runs on an
    addendum route write.  If the resolver hands it a stale object, the mutator
    advances the STALE rev and the snapshot write replaces a newer record: an
    entry someone else already committed disappears, for every reader.
    """
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _warm_bridge(store, live)
    warm_registry[SID] = bridge

    late = _append_from_another_writer(tmp_path, LATE_TEXT)

    status, body = _post_text(NEW_TEXT)

    assert status == 200, body
    assert body["status"] == "accepted"
    assert body["entry"]["text"] == NEW_TEXT

    on_disk = _store(tmp_path).load_session(SID)
    texts = _texts(on_disk)
    assert LATE_TEXT in texts, (
        "the route write overwrote a newer stored generation; "
        f"stored entries={texts}, lost id={late['id']}"
    )
    assert NEW_TEXT in texts

    # The response is the same authority, so it must already report both.
    assert {LATE_TEXT, NEW_TEXT} <= {item["text"] for item in body["entries"]}


def test_effective_read_agrees_with_the_record_after_a_stale_snapshot_save(
    tmp_path, store, routes_store, warm_registry
):
    """The read seam must not undo PR A's write seam.

    PR A's invariant is "after any accepted write, an immediate read of the
    store contains the entry that was accepted".  A stale whole-record save is
    what used to break it; this checks the OTHER half -- that after such a save
    the preview a live bridge serves still contains that entry, so the panel and
    the record agree instead of the panel lagging one generation behind.
    """
    _seed(store)
    live = store.load_session(SID)
    assert live is not None
    bridge = _warm_bridge(store, live)
    warm_registry[SID] = bridge

    # A second copy of the same session, taken BEFORE the write: the snapshot
    # whose save would overwrite the record with a pre-write addendum.
    stale = _store(tmp_path).load_session(SID)
    assert stale is not None

    status, body = _post_text(LATE_TEXT)
    assert status == 200 and body["status"] == "accepted", body
    accepted = body["entry"]

    manager = SessionManager(session_store=store, config_manager=ConfigManager())
    manager.save_session(stale)

    on_disk = _store(tmp_path).load_session(SID)
    assert accepted["id"] in [item["id"] for item in on_disk.addendum["entries"]], (
        "the accepted entry must survive the stale whole-record save"
    )

    status, effective = _get_effective()
    assert status == 200, effective
    assert accepted["id"] in [item["id"] for item in effective["entries"]], (
        "the preview must agree with the record it is previewing"
    )
    assert LATE_TEXT in effective["rendered_block"]


def test_cold_path_still_reads_the_record_directly(
    tmp_path, store, routes_store, warm_registry
):
    """Nobody holds the session: the store IS the authority, unchanged."""
    _seed(store)
    accepted = _append_from_another_writer(tmp_path, LATE_TEXT)

    status, body = _get_effective()

    assert status == 200, body
    assert body["resolved_via"] == "store"
    assert accepted["id"] in [item["id"] for item in body["entries"]]
    assert LATE_TEXT in body["rendered_block"]
