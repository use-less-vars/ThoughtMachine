"""Write-through persistence for the session addendum (W1.5, ruling Option A).

What is pinned here:

* a write ACCEPTED against a live session lands on the object the turn loop and
  the model read *and* in the store, in the same critical section -- so the
  entry an operator just added cannot be lost to the next end-of-turn save, nor
  be invisible to the next turn;
* a write against a COLD session goes through the store's single atomic
  read-modify-write;
* a REFUSAL is quoted, never re-derived: the route reports the numbers and the
  sentence the write boundary measured, and never measures the submitted text
  itself.  The route module contains no ``sum(`` and no ``len(`` at all, so a
  displayed total cannot drift away from the enforced budget;
* a bridge save from the "no active session" fallback (`_session is None`)
  carries the stored addendum instead of writing the default empty one over it.

The route coroutines are driven directly (``asyncio.run(route(...))``) rather
than through ``TestClient``: one test must inject a body that pydantic
validation would reject (a ``_PoisonText`` whose ``__len__`` raises), which
``model_construct`` allows and the HTTP surface does not.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import web_ui.backend.session_routes as session_routes
from session.addendum import write_addendum_entry
from session.models import Session
from session.store import FileSystemSessionStore
from web_ui.backend import addendum_routes
from web_ui.backend.addendum_routes import (
    AddendumWriteBody,
    _live_bridges,
    _resolve_addendum_session,
    delete_session_addendum,
    get_session_addendum,
    post_session_addendum,
)
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.event_forwarder import _active_tab_bridges

SID = "sess-write-through"
TEXT = "ALWAYS use the staging kubeconfig."
ORIGIN = "http://localhost:5173"


# ── helpers ─────────────────────────────────────────────────────────────────


class _Bridge(SimpleNamespace):
    """A tab bridge stand-in.

    ``types.SimpleNamespace`` is unhashable (it defines ``__eq__``), and the
    live-bridge registry is a ``set``, so identity hashing is restored here.
    """

    __eq__ = object.__eq__
    __hash__ = object.__hash__


def _bridge(agent=None, session=None, loaded=None, lock=None):
    """A bridge carrying whatever subset of live-session slots a test needs."""
    return _Bridge(
        _agent=agent,
        _session=session,
        _loaded_session=loaded,
        _session_state_lock=lock if lock is not None else threading.RLock(),
    )


def _request(method: str = "POST", path: Optional[str] = None,
             origin: Optional[str] = None) -> Request:
    """A bare ASGI request; an absent Origin header is permitted by policy."""
    headers: List[Any] = []
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
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
        "headers": headers,
        "client": ("127.0.0.1", 54321),
        "server": ("testserver", 80),
    })


def _unpack(response):
    """``(status_code, body)`` for both a plain dict and a ``JSONResponse``."""
    if isinstance(response, JSONResponse):
        return response.status_code, json.loads(response.body.decode("utf-8"))
    return 200, response


def _post(body, session_id: str = SID):
    return _unpack(asyncio.run(
        post_session_addendum(_request("POST"), session_id, body)
    ))


def _post_text(text: str, session_id: str = SID):
    return _post(AddendumWriteBody(text=text), session_id)


def _get(session_id: str = SID):
    return _unpack(asyncio.run(
        get_session_addendum(_request("GET"), session_id)
    ))


def _delete(session_id: str = SID):
    return _unpack(asyncio.run(
        delete_session_addendum(_request("DELETE"), session_id)
    ))


def _entry(text: str, entry_id: str = "e1") -> Dict[str, Any]:
    return {"id": entry_id, "ts": "", "source": "operator", "text": text,
            "pinned": False}


def _seed(store: FileSystemSessionStore, entries=(), budget: int = 8000,
          max_entries: int = 64, session_id: str = SID) -> Session:
    """Persist a session with the given addendum (workspace-less: stays in tmp)."""
    session = Session(session_id=session_id)
    session.addendum = {
        "entries": [copy.deepcopy(item) for item in entries],
        "budget_chars": budget,
        "max_entries": max_entries,
    }
    store.save_session(session)
    return session


def _texts(session: Session) -> List[str]:
    return [item["text"] for item in (session.addendum or {}).get("entries", [])]


def _disk_texts(store: FileSystemSessionStore, session_id: str = SID) -> List[str]:
    session = store.load_session(session_id)
    assert session is not None, f"session {session_id} is not in the store"
    return _texts(session)


def _session_file(store: FileSystemSessionStore, session_id: str = SID) -> Path:
    path = store._find_session_path(session_id)
    assert path is not None, f"session {session_id} has no file"
    return Path(path)


def _fingerprint(store: FileSystemSessionStore, session_id: str = SID):
    """Bytes + mtime of the stored record: any write changes at least one."""
    path = _session_file(store, session_id)
    return path.read_bytes(), path.stat().st_mtime_ns


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


# ── (a) a refusal quotes the boundary; the route measures nothing ───────────


CANNED_REFUSAL = {
    "status": "dropped",
    "entry": None,
    "reason": "budget_exceeded",
    "message": "addendum budget exceeded: 999999 > 12345 chars",
    "entries": [],
    "attempted_chars": 1000001,
    "budget_chars": 12345,
    "attempted_entries": 3,
    "max_entries": 424242,
    "total_chars": 999999,
}


def test_a1_refusal_reports_the_boundarys_numbers_verbatim(routes_store, monkeypatch):
    """Every number and the sentence come from the boundary, not from a recount."""
    _seed(routes_store)
    before = _fingerprint(routes_store)
    monkeypatch.setattr(
        addendum_routes, "write_addendum_entry",
        lambda *args, **kwargs: copy.deepcopy(CANNED_REFUSAL),
    )

    status, body = _post_text("well under the real budget, but the verdict is canned")

    assert status == 409
    assert body["status"] == "refused"
    assert body["reason"] == CANNED_REFUSAL["reason"]
    assert body["message"] == CANNED_REFUSAL["message"]
    assert body["total_chars"] == 999999
    assert body["budget_chars"] == 12345
    assert body["max_entries"] == 424242
    assert body["attempted_chars"] == 1000001
    assert body["attempted_entries"] == 3
    # a refusal changed nothing, so nothing was persisted
    assert _fingerprint(routes_store) == before


class _PoisonText(str):
    """A ``str`` whose length may not be measured outside the boundary."""

    measured = False

    def __len__(self):
        _PoisonText.measured = True
        raise RuntimeError("the addendum text was measured outside the boundary")


def test_a2_route_never_measures_the_submitted_text(routes_store):
    """A ``len()`` in the route cannot be answered, yet the write still 409s.

    The body is injected with ``model_construct`` so the poison survives
    validation; if the route measured the text before handing it to the
    boundary, this test would fail with a 500 (or the assertion below).
    """
    _PoisonText.measured = False
    _seed(routes_store)
    before = _fingerprint(routes_store)
    body = AddendumWriteBody.model_construct(text=_PoisonText("poison"))

    status, payload = _post(body)

    assert status == 409
    assert payload["status"] == "refused"
    assert payload["reason"] == "exception"
    assert payload["message"] == "addendum write refused: exception"
    assert payload["entries"] == []
    assert payload["total_chars"] == 0
    assert _PoisonText.measured is True, (
        "the poison never reached a measurement -- this test would prove nothing"
    )
    assert _fingerprint(routes_store) == before


def test_a3_route_module_never_sums_or_measures_text():
    """The reported total must come from the boundary's own measurement function."""
    source = Path(addendum_routes.__file__).read_text(encoding="utf-8")
    assert "sum(" not in source
    assert "len(" not in source


# ── (b) live sessions ───────────────────────────────────────────────────────


def test_b1_live_post_lands_on_the_object_and_on_disk(routes_store):
    """The accepted entry reaches the live object AND the store."""
    _seed(routes_store, entries=[_entry("seed", "e-seed")])
    live = routes_store.load_session(SID)
    # Drift the live copy away from disk: this is what the next turn reads.
    live.addendum["entries"].append(_entry("out-of-band", "e-oob"))
    _active_tab_bridges.add(_bridge(agent=SimpleNamespace(session=live), session=live))

    status, body = _post_text(TEXT)

    assert status == 200
    assert body["status"] == "accepted"
    assert _texts(live) == ["seed", "out-of-band", TEXT]
    assert body["entries"] == live.addendum["entries"]
    assert body["total_chars"] == sum(len(item) for item in _texts(live))
    assert _disk_texts(routes_store) == ["seed", "out-of-band", TEXT]


def test_b2_get_reads_the_live_object_not_the_cold_copy(routes_store):
    """A read resolves live-first, so a client never sees a set the turn won't."""
    _seed(routes_store, entries=[_entry("on-disk", "e-disk")])
    live = routes_store.load_session(SID)
    live.addendum = {
        "entries": [_entry("live-only", "e-live")],
        "budget_chars": 8000,
        "max_entries": 64,
    }
    _active_tab_bridges.add(_bridge(agent=SimpleNamespace(session=live), session=live))

    assert _disk_texts(routes_store) == ["on-disk"]

    status, body = _get()

    assert status == 200
    assert [item["text"] for item in body["entries"]] == ["live-only"]
    assert body["total_chars"] == len("live-only")
    assert body["session_id"] == SID
    # a read is a read: nothing was persisted
    assert _disk_texts(routes_store) == ["on-disk"]


def test_b3_delete_clears_the_live_object_and_the_store(routes_store):
    _seed(routes_store, entries=[_entry("seed", "e-seed")])
    live = routes_store.load_session(SID)
    _active_tab_bridges.add(_bridge(session=live))

    status, body = _delete()

    assert status == 200
    assert body["status"] == "cleared"
    assert body["entries"] == []
    assert body["total_chars"] == 0
    assert body["budget_chars"] == 8000
    assert body["max_entries"] == 64
    assert body["session_id"] == SID
    assert _texts(live) == []
    assert _disk_texts(routes_store) == []


def test_b4_a_loaded_session_only_bridge_is_still_live(routes_store):
    """``_loaded_session`` alone is enough to be the runtime authority."""
    _seed(routes_store)
    live = routes_store.load_session(SID)
    live.addendum = {
        "entries": [_entry("live-only", "e-live")],
        "budget_chars": 8000,
        "max_entries": 64,
    }
    bridge = _bridge(agent=None, session=None, loaded=live)
    _active_tab_bridges.add(bridge)

    assert [pair[0] for pair in _live_bridges(SID)] == [bridge]
    assert _resolve_addendum_session(SID)[2] is live

    status, _body = _post_text(TEXT)

    assert status == 200
    # the live object was the authority (it kept its out-of-band entry) and the
    # store now agrees with it
    assert _texts(live) == ["live-only", TEXT]
    assert _disk_texts(routes_store) == ["live-only", TEXT]


def test_b5_another_tabs_copy_is_brought_to_the_accepted_set(routes_store):
    """No reader may see the write applied for one tab and absent for another."""
    _seed(routes_store, entries=[_entry("seed", "e-seed")])
    first = routes_store.load_session(SID)
    second = routes_store.load_session(SID)
    assert first is not second
    _active_tab_bridges.add(_bridge(session=first))
    _active_tab_bridges.add(_bridge(session=second))

    status, _body = _post_text(TEXT)

    assert status == 200
    assert _texts(first) == ["seed", TEXT]
    assert _texts(second) == ["seed", TEXT]
    assert _disk_texts(routes_store) == ["seed", TEXT]
    # the copies never alias each other's dicts
    assert first.addendum["entries"] is not second.addendum["entries"]
    assert first.addendum["entries"][1] is not second.addendum["entries"][1]


def test_b6_resolver_prefers_the_agent_bearing_bridge(routes_store):
    """The running agent's session outranks a merely loaded copy."""
    _seed(routes_store)
    agent_session = routes_store.load_session(SID)
    loaded_session = routes_store.load_session(SID)
    loaded_bridge = _bridge(loaded=loaded_session)
    agent_bridge = _bridge(agent=SimpleNamespace(session=agent_session),
                           session=agent_session)
    _active_tab_bridges.add(loaded_bridge)
    _active_tab_bridges.add(agent_bridge)

    assert _live_bridges(SID)[0][0] is agent_bridge
    assert _resolve_addendum_session(SID)[2] is agent_session


def test_b7_write_through_holds_the_bridge_lock_across_the_save(routes_store,
                                                               monkeypatch):
    """A turn-loop save can never interleave its snapshot with this write."""
    _seed(routes_store)
    live = routes_store.load_session(SID)
    lock = threading.RLock()
    _active_tab_bridges.add(_bridge(session=live, lock=lock))

    real_save = routes_store.save_session
    contended = threading.Event()
    observed: Dict[str, Any] = {}

    def spy_save(session, *args, **kwargs):
        def contend():
            with lock:
                contended.set()

        thread = threading.Thread(target=contend, daemon=True)
        observed["thread"] = thread
        thread.start()
        observed["entered"] = contended.wait(0.5)
        return real_save(session, *args, **kwargs)

    monkeypatch.setattr(routes_store, "save_session", spy_save)

    status, body = _post_text(TEXT)

    assert status == 200
    assert body["status"] == "accepted"
    assert _texts(live) == [TEXT]
    assert observed["entered"] is False, (
        "a contending thread entered the bridge's session-state lock while the "
        "accepted write was being persisted"
    )
    observed["thread"].join(5.0)
    assert contended.is_set(), "the bridge lock was never released"
    assert _disk_texts(routes_store) == [TEXT]


# ── (c) cold sessions ───────────────────────────────────────────────────────


def test_c1_cold_post_get_delete_go_through_the_store(routes_store):
    _seed(routes_store)
    assert _live_bridges(SID) == []

    status, body = _post_text(TEXT)
    assert status == 200
    assert body["status"] == "accepted"
    assert _disk_texts(routes_store) == [TEXT]

    status, body = _get()
    assert status == 200
    assert [item["text"] for item in body["entries"]] == [TEXT]

    status, body = _delete()
    assert status == 200
    assert body["status"] == "cleared"
    assert _disk_texts(routes_store) == []


def test_c2_cold_refusal_persists_nothing(routes_store):
    _seed(routes_store, entries=[_entry("seed", "e-seed")], budget=10)
    before = _fingerprint(routes_store)

    status, body = _post_text("far longer than the ten character budget")

    assert status == 409
    assert body["status"] == "refused"
    assert body["reason"] == "budget_exceeded"
    assert body["message"].startswith("addendum budget exceeded: ")
    assert [item["text"] for item in body["entries"]] == ["seed"]
    assert body["total_chars"] == len("seed")
    assert _fingerprint(routes_store) == before


def test_c3_cold_paths_leave_no_temp_or_lock_files(routes_store, tmp_path):
    """Atomicity is temp-file + replace under a lock: neither may be left behind."""
    _seed(routes_store)

    _post_text(TEXT)
    _get()
    _post_text("z" * 100000)  # over budget: refused
    _delete()

    leftovers = sorted(
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix in (".tmp", ".lock")
    )
    assert leftovers == []


# ── (d) the store's atomic read-modify-write ────────────────────────────────


def test_d1_mutate_session_persists_an_accepted_write(store):
    _seed(store)

    def mutator(session):
        result = write_addendum_entry(session, TEXT, source="operator", notify=False)
        return result, result.get("status") == "ok"

    session, result = store.mutate_session(SID, mutator)

    assert result["status"] == "ok"
    assert _texts(session) == [TEXT]
    assert _disk_texts(store) == [TEXT]


def test_d2_mutate_session_writes_nothing_when_the_mutator_says_so(store):
    _seed(store, entries=[_entry("seed", "e-seed")])
    before = _fingerprint(store)

    session, result = store.mutate_session(
        SID, lambda _session: ({"status": "dropped"}, False)
    )

    assert result == {"status": "dropped"}
    assert session is not None
    assert _texts(session) == ["seed"]
    assert _fingerprint(store) == before


def test_d3_mutate_session_reports_a_missing_session(store):
    assert store.mutate_session("no-such-session", lambda _s: ({}, True)) == (None, None)


# ── (e) the end-of-turn save must not wipe the addendum ─────────────────────


def test_e_bridge_fallback_save_keeps_the_stored_addendum(store):
    """With no active session the save rebuilds from ``_loaded_session``."""
    _seed(store, entries=[_entry("CARRY-ME", "e-carried")])
    loaded = store.load_session(SID)
    bridge = WebAgentBridge(event_callback=lambda _event: None, session_store=store)
    bridge._session = None
    bridge._loaded_session = loaded

    saved = bridge.save_session()

    assert saved is not None, "the fallback save failed"
    assert _texts(saved) == ["CARRY-ME"]
    reloaded = store.load_session(saved.session_id)
    assert reloaded is not None
    assert _texts(reloaded) == ["CARRY-ME"]
