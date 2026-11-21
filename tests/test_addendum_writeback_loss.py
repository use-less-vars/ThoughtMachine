"""Does an ACCEPTED addendum entry survive the next save of a stale Session?

The loss mode (measured, not assumed): ``addendum_routes._write_through``
persists an accepted entry onto the live ``Session`` and onto *the twins it can
reach* through ``_held_sessions``.  Any copy it cannot reach -- another
process's, a reconnecting tab's, the ``_loaded_session`` of a bridge that
resolved cold while the write went to disk -- keeps its pre-write entry set.
The next ``WebAgentBridge.save_session`` on that copy then writes its whole
snapshot over the record, so the entry the server minted an id for and echoed
to the client is simply gone from the store.

The invariant asserted here, in the operator's words: **after any accepted
write, an immediate read of the store must contain the entry that was
accepted -- same id, same text, same length.**  A failing test therefore IS the
reported bug; nothing below asserts that the entry is lost, and nothing below
asserts anything about *where* the file lives (the store is rooted entirely in
``tmp_path``; the single real session file is already workspace-scoped, so
path scoping plays no part in this failure).

Scenario, all against the REAL store and the REAL save path:

1. a session carrying one entry (``e1``) is written by the real
   :class:`FileSystemSessionStore`;
2. an in-memory ``Session`` is loaded from it -- the object a turn loop holds
   (``WebAgentBridge._session`` / ``._loaded_session``);
3. a LATE entry (``e2``) is appended straight to the store through the cold
   path (:meth:`FileSystemSessionStore.mutate_session`, the same atomic
   read-modify-write the addendum routes use when no live bridge owns the
   session).  This is a different writer: the in-memory object above knows
   nothing about it;
4. the real persist path runs: ``WebAgentBridge.save_session`` on the active
   path (``_session`` set) and on the fallback path (``_session`` is None, so
   the bridge rebuilds a ``Session`` from ``_loaded_session``);
5. the session is reloaded through a *fresh* store instance.
"""
from __future__ import annotations

import threading

from session.addendum import write_addendum_entry
from session.models import Session
from session.store import FileSystemSessionStore
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.session_manager import SessionManager

SESSION_ID = "sess-writeback-loss"
FIRST_TEXT = "operator entry that predates the turn"
LATE_TEXT = "operator entry accepted by the store mid-session"
NEW_TEXT = "operator entry appended in memory after the store write"


def _entry(entry_id: str, text: str) -> dict:
    return {
        "id": entry_id,
        "ts": "2024-01-01T00:00:00+00:00",
        "source": "operator",
        "text": text,
        "pinned": False,
    }


def _store(tmp_path) -> FileSystemSessionStore:
    """A real store rooted entirely in ``tmp_path`` (never the live vault)."""
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
        enable_session_history_pruning=False,
    )


class _BridgeHarness:
    """Minimal carrier for the REAL ``WebAgentBridge.save_session`` method.

    Only the attributes/methods ``save_session`` actually touches are
    provided; ``save_session`` and ``_config_dump_for_persistence`` are the
    production functions, not re-implementations.
    """

    save_session = WebAgentBridge.save_session
    _config_dump_for_persistence = WebAgentBridge._config_dump_for_persistence

    def __init__(self, store, session=None, loaded=None):
        self._session_state_lock = threading.RLock()
        self._session = session
        self._loaded_session = loaded
        self._session_manager = SessionManager(store, config_manager=None)
        # No workspace id: the store above is rooted in ``tmp_path``, so the
        # path a save resolves to is irrelevant to the loss under test.
        self._workspace_id = None
        self._session_config = None
        self._session_config_persist_dump = None
        self._persisted_workers = {}

    def _load_worker_contexts(self):  # not part of the hazard
        return None


def _seed_session_with_first_entry(tmp_path):
    """Write a session carrying ``e1`` to disk and return two loaded copies.

    Two independent ``load_session`` calls, not ``copy.deepcopy`` of one: a
    ``Session`` carries non-picklable members (locks), and the point is anyhow
    that these are the objects a turn loop holds BEFORE the late write.
    """
    store = _store(tmp_path)
    session = Session(
        session_id=SESSION_ID,
        user_history=[{"role": "user", "content": "start the turn"}],
        metadata={"name": "Writeback Loss"},
    )
    session.addendum["entries"].append(_entry("e1", FIRST_TEXT))
    store.save_session(session)
    first = store.load_session(SESSION_ID)
    second = store.load_session(SESSION_ID)
    assert first is not second
    return first, second


def _append_late_entry_directly_to_store(tmp_path) -> dict:
    """A writer the in-memory Session knows nothing about (the cold path).

    The append goes through the REAL write boundary
    (``session.addendum.write_addendum_entry``) inside the store's atomic
    read-modify-write -- exactly what the REST route does when nobody holds the
    session -- so the entry's id, its ``ts`` and the addendum's generation are
    the production ones, not fixtures that happen to look right.
    """
    def _mutate(stored: Session):
        result = write_addendum_entry(stored, LATE_TEXT, source="operator", notify=False)
        return result, result.get("status") == "ok"

    writer = _store(tmp_path)  # separate store instance, as another process would have
    mutated, result = writer.mutate_session(SESSION_ID, _mutate)
    assert mutated is not None, "test setup: the session file must exist to be mutated"
    assert result is not None and result.get("status") == "ok", (
        f"test setup: the boundary must accept the late write; result={result!r}"
    )
    accepted = dict(result["entry"])

    on_disk = _store(tmp_path).load_session(SESSION_ID)
    assert accepted["id"] in [e["id"] for e in on_disk.addendum["entries"]], (
        "test setup: the late entry must be on disk before the save under test"
    )
    return accepted


def _entries(tmp_path):
    reloaded = _store(tmp_path).load_session(SESSION_ID)
    assert reloaded is not None, "the session file must still exist after the save"
    return list(reloaded.addendum["entries"])


def _assert_survived(tmp_path, accepted: dict, context: str) -> None:
    """THE invariant: accepted means readable back, verbatim, right now."""
    entries = _entries(tmp_path)
    ids = [e.get("id") for e in entries]
    assert accepted["id"] in ids, (
        f"{context}: the entry the store accepted (id={accepted['id']!r}) is not "
        f"in the store after the save; stored ids={ids!r}, texts="
        f"{[e.get('text') for e in entries]!r}"
    )
    stored = next(e for e in entries if e.get("id") == accepted["id"])
    assert stored["text"] == accepted["text"], (
        f"{context}: the accepted entry's text changed; "
        f"stored={stored['text']!r} accepted={accepted['text']!r}"
    )
    assert len(stored["text"]) == len(accepted["text"]), (
        f"{context}: the accepted entry's length changed; "
        f"stored={len(stored['text'])} accepted={len(accepted['text'])}"
    )


def test_accepted_entry_survives_save_on_the_active_session_path(tmp_path):
    """``_session`` set (mid-turn): the end-of-turn save must not drop ``e2``."""
    stale_in_memory, stale_for_bridge = _seed_session_with_first_entry(tmp_path)
    accepted = _append_late_entry_directly_to_store(tmp_path)

    bridge = _BridgeHarness(_store(tmp_path),
                            session=stale_in_memory,
                            loaded=stale_for_bridge)
    saved = bridge.save_session()
    assert saved is not None, "save_session must not swallow the failure and return None"

    _assert_survived(tmp_path, accepted, "active session path")
    assert FIRST_TEXT in [e["text"] for e in _entries(tmp_path)], (
        f"the pre-existing entry must survive too; entries={_entries(tmp_path)!r}"
    )


def test_accepted_entry_survives_save_on_the_fallback_path(tmp_path):
    """``_session`` is None: the rebuilt Session must not drop ``e2`` either."""
    _stale_in_memory, stale_for_bridge = _seed_session_with_first_entry(tmp_path)
    accepted = _append_late_entry_directly_to_store(tmp_path)

    bridge = _BridgeHarness(_store(tmp_path), session=None, loaded=stale_for_bridge)
    saved = bridge.save_session()
    assert saved is not None, "save_session must not swallow the failure and return None"

    _assert_survived(tmp_path, accepted, "fallback path")


def test_in_memory_entry_is_not_the_price_of_keeping_the_accepted_one(tmp_path):
    """The mirror guard: an entry only the in-memory Session holds must persist.

    A fix that lets the store's record win wholesale would trade one loss for
    the other -- the turn loop's own append would vanish instead.  Both
    directions have to hold for "accepted means readable back" to mean anything.
    """
    stale_in_memory, stale_for_bridge = _seed_session_with_first_entry(tmp_path)
    accepted = _append_late_entry_directly_to_store(tmp_path)
    # Same boundary, this time on the object that knows nothing about the late
    # write: a legitimate in-memory append the save must not trade away.
    in_memory = write_addendum_entry(stale_in_memory, NEW_TEXT, source="operator",
                                     notify=False)
    assert in_memory.get("status") == "ok", f"test setup: {in_memory!r}"
    in_memory_entry = dict(in_memory["entry"])

    bridge = _BridgeHarness(_store(tmp_path),
                            session=stale_in_memory,
                            loaded=stale_for_bridge)
    assert bridge.save_session() is not None

    _assert_survived(tmp_path, accepted, "active session path with an in-memory append")
    _assert_survived(tmp_path, in_memory_entry, "in-memory append")
