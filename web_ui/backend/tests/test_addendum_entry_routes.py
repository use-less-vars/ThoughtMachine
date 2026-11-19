"""TestClient coverage for the per-entry addendum endpoints and the preview route.

Covers the three endpoints added alongside the append/clear surface:

* ``PATCH  /api/session/{session_id}/addendum/entries/{entry_id}``
* ``DELETE /api/session/{session_id}/addendum/entries/{entry_id}``
* ``GET    /api/session/{session_id}/addendum/effective``

What is pinned here:

* an accepted PATCH replaces the TEXT in place and preserves the entry's
  identity (``id`` / ``ts`` / ``source`` / ``pinned``); the set keeps its order;
* an accepted DELETE removes exactly one entry and reports the id it removed;
* a refusal persists NOTHING -- the stored record's bytes and mtime are
  byte-identical afterwards, so a refused write can never bump ``updated_at``;
* an unknown entry id is a missing RESOURCE (404), not a refused write (409),
  because the id came from the caller;
* ``.../addendum/effective`` renders the block through
  ``agent.core.llm_client.render_addendum_block`` -- the very function the turn
  composes the system prompt with -- so the operator reads what the model reads;
* a clear repairs an out-of-range cap ON DISK, which is what makes an unusable
  record writable again.

Isolation follows ``test_addendum_routes.py`` / ``test_addendum_write_through.py``:
the store behind the routes is replaced by monkeypatching
``session_routes._get_store`` (the addendum router reads the store through that
same accessor) and every vault-backed path is redirected into ``tmp_path``.  The
store is a REAL ``FileSystemSessionStore``, so "persisted" means "in the file",
which is what lets a test assert on the record's literal bytes.

Every test carries ``@pytest.mark.trust_harness``: ``TestClient`` arrives from
the peer name ``testclient``, which the boundary honours only while the suite's
harness-trust flag is on for that test.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.session_routes as session_routes
from agent.core.llm_client import ADDENDUM_HEADING
from session.models import Session
from session.store import FileSystemSessionStore
from web_ui.backend.event_forwarder import _active_tab_bridges
from web_ui.backend.server import app

client = TestClient(app)

SID = "sess-addendum-entries-001"

#: The ``ts`` every seeded entry carries: it is identity, so an accepted PATCH
#: must hand it back unchanged rather than stamping a new one.
TS = "2025-01-01T00:00:00+00:00"

OLD = "Prefer terse answers."
NEW = "Prefer TERSE answers while the migration is running."
OTHER = "Never touch prod from a script."


# ── URLs ────────────────────────────────────────────────────────────────────


def url(session_id: str = SID) -> str:
    return f"/api/session/{session_id}/addendum"


def entries_url(entry_id: str, session_id: str = SID) -> str:
    return f"{url(session_id)}/entries/{entry_id}"


def effective_url(session_id: str = SID) -> str:
    return f"{url(session_id)}/effective"


# ── seeding and on-disk assertions ──────────────────────────────────────────


def _entry(text: str, entry_id: str = "e1", source: str = "operator",
           pinned: bool = False) -> dict:
    return {
        "id": entry_id,
        "ts": TS,
        "source": source,
        "text": text,
        "pinned": pinned,
    }


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


def _session_file(store: FileSystemSessionStore, session_id: str = SID) -> Path:
    path = store._find_session_path(session_id)
    assert path is not None, f"session {session_id} has no file"
    return Path(path)


def _disk_addendum(store: FileSystemSessionStore, session_id: str = SID) -> dict:
    """The stored addendum, read off the FILE: no normalisation in the way."""
    return json.loads(_session_file(store, session_id).read_text())["addendum"]


def _fingerprint(store: FileSystemSessionStore, session_id: str = SID):
    """Bytes + mtime of the stored record: any write changes at least one."""
    path = _session_file(store, session_id)
    return path.read_bytes(), path.stat().st_mtime_ns


def _texts(body: dict) -> list:
    return [item["text"] for item in body["entries"]]


def _ids(body: dict) -> list:
    return [item["id"] for item in body["entries"]]


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def store_dirs(tmp_path):
    """The two directory paths, so a SECOND cold store can share them."""
    return {
        "sessions_dir": str(tmp_path / "sessions"),
        "state_dir": str(tmp_path / "state"),
    }


@pytest.fixture
def store(store_dirs, vault):
    return FileSystemSessionStore(
        **store_dirs,
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


# ── PATCH: an accepted in-place rewrite ─────────────────────────────────────


@pytest.mark.trust_harness
def test_patch_replaces_text_and_preserves_entry_identity(store, routes_store):
    _seed(store, [_entry(OLD, "e1"), _entry(OTHER, "e2")])

    response = client.patch(entries_url("e1"), json={"text": NEW})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "updated"
    # Identity is preserved: only ``text`` moved.
    assert body["entry"] == {
        "id": "e1",
        "ts": TS,
        "source": "operator",
        "text": NEW,
        "pinned": False,
    }
    # The set keeps its order and its membership: an update is not a re-append.
    assert _ids(body) == ["e1", "e2"]
    assert _texts(body) == [NEW, OTHER]
    # The reported total is the resulting set's, i.e. the old text is not
    # double-counted.
    assert body["total_chars"] == len(NEW) + len(OTHER)
    assert body["budget_chars"] == 8000
    assert body["max_entries"] == 64

    # It is on disk, not only in the response.
    assert _texts(_disk_addendum(store)) == [NEW, OTHER]

    # And the read endpoint the UI polls agrees with it.
    plain = client.get(url())
    assert plain.status_code == 200, plain.text
    assert _texts(plain.json()) == [NEW, OTHER]


# ── PATCH: an unknown id is a missing resource, not a refused write ─────────


@pytest.mark.trust_harness
def test_patch_unknown_entry_is_404_and_persists_nothing(store, routes_store):
    _seed(store, [_entry(OLD, "e1")])
    before = _fingerprint(store)

    response = client.patch(entries_url("ghost"), json={"text": NEW})

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["status"] == "not_found"
    assert body["reason"] == "entry_not_found"
    assert "ghost" in body["message"]
    # The unchanged set is reported, and nothing was rewritten.
    assert _ids(body) == ["e1"]
    assert _texts(body) == [OLD]
    assert body["total_chars"] == len(OLD)
    assert _fingerprint(store) == before


# ── PATCH: a budget refusal quotes the boundary and writes nothing ──────────


@pytest.mark.trust_harness
def test_patch_over_budget_is_409_and_persists_nothing(store, routes_store):
    long_ok = "a" * 50
    _seed(store, [_entry(long_ok, "e1")], budget=100)
    before = _fingerprint(store)

    response = client.patch(entries_url("e1"), json={"text": "b" * 200})

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["reason"] == "budget_exceeded"
    # The boundary's own numbers: the resulting set was 200 chars against a cap
    # of 100, while ``total_chars`` is the unchanged set it left behind.
    assert body["attempted_chars"] == 200
    assert body["budget_chars"] == 100
    assert body["total_chars"] == 50
    # The stored text is byte-identical to what it was -- the update is
    # all-or-nothing.
    assert _texts(body) == [long_ok]
    assert _texts(_disk_addendum(store)) == [long_ok]
    assert _fingerprint(store) == before


# ── DELETE one entry ────────────────────────────────────────────────────────


@pytest.mark.trust_harness
def test_delete_entry_removes_exactly_that_entry(store, routes_store):
    _seed(store, [_entry(OLD, "e1"), _entry(OTHER, "e2"), _entry("third", "e3")])

    response = client.delete(entries_url("e2"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "removed"
    assert body["removed_id"] == "e2"
    assert _ids(body) == ["e1", "e3"]
    assert _texts(body) == [OLD, "third"]
    # A removal only ever shrinks the measured set.
    assert body["total_chars"] == len(OLD) + len("third")

    # Both readers agree: the HTTP GET and a fresh load off the store.
    assert _ids(client.get(url()).json()) == ["e1", "e3"]
    reloaded = store.load_session(SID)
    assert reloaded is not None
    assert [item["id"] for item in reloaded.addendum["entries"]] == ["e1", "e3"]


@pytest.mark.trust_harness
def test_delete_unknown_entry_is_404_and_persists_nothing(store, routes_store):
    _seed(store, [_entry(OLD, "e1")])
    before = _fingerprint(store)

    response = client.delete(entries_url("ghost"))

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["status"] == "not_found"
    assert body["reason"] == "entry_not_found"
    assert "ghost" in body["message"]
    assert _ids(body) == ["e1"]
    assert _texts(body) == [OLD]
    assert _fingerprint(store) == before


# ── GET .../effective: the block the next query will carry ──────────────────


@pytest.mark.trust_harness
def test_effective_renders_the_next_query_block_verbatim(store, routes_store):
    _seed(store, [_entry(OLD, "e1"), _entry(OTHER, "e2")])

    response = client.get(effective_url())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["session_id"] == SID
    # Nothing holds the session in this process, so the store was the authority.
    assert body["resolved_via"] == "store"
    assert body["applied"] == "next_query"

    block = body["rendered_block"]
    assert block.startswith(ADDENDUM_HEADING)
    assert OLD in block and OTHER in block
    # Stored order is preserved: nothing is sorted or reordered by ``pinned``.
    assert block.index(OLD) < block.index(OTHER)
    assert body["block_chars"] == len(block)

    assert _texts(body) == [OLD, OTHER]
    assert body["total_chars"] == len(OLD) + len(OTHER)
    assert body["budget_chars"] == 8000
    assert body["max_entries"] == 64

    diagnostics = body["diagnostics"]
    assert diagnostics["live_bridges"] == 0
    assert diagnostics["agent_bearing_bridges"] == 0
    # Cold path: no agent holds the session, so these cannot but be empty.
    assert diagnostics["agent_session_object_id"] is None
    assert diagnostics["same_object"] is False
    assert isinstance(diagnostics["session_object_id"], int)


@pytest.mark.trust_harness
def test_effective_is_the_state_plus_the_rendering(store, routes_store):
    _seed(store, [_entry(OLD, "e1")])

    plain = client.get(url())
    effective = client.get(effective_url())

    assert plain.status_code == 200, plain.text
    assert effective.status_code == 200, effective.text
    plain_body, effective_body = plain.json(), effective.json()

    # The preview is the state payload PLUS the rendering: the plain GET never
    # claims to have composed anything, so it must not carry these keys.
    assert "rendered_block" not in plain_body
    assert "applied" not in plain_body
    assert isinstance(effective_body["rendered_block"], str)
    assert effective_body["applied"] == "next_query"
    assert effective_body != plain_body

    # ... and the shared half really is shared.
    assert _texts(plain_body) == _texts(effective_body)
    assert plain_body["total_chars"] == effective_body["total_chars"]


# ── durability: the rewrite survives the process that made it ───────────────


@pytest.mark.trust_harness
def test_patch_is_visible_to_a_cold_store(store, routes_store, store_dirs):
    _seed(store, [_entry(OLD, "e1")])

    assert client.patch(entries_url("e1"), json={"text": NEW}).status_code == 200

    # A SECOND store on the same directories knows nothing but the files, so
    # what it reads was persisted rather than mutated in memory.
    cold = FileSystemSessionStore(
        **store_dirs,
        enable_session_history_pruning=False,
    )
    reloaded = cold.load_session(SID)
    assert reloaded is not None
    assert [item["text"] for item in reloaded.addendum["entries"]] == [NEW]


# ── clearing is the repair path for an unusable cap ─────────────────────────


@pytest.mark.trust_harness
def test_clear_repairs_an_out_of_range_cap_on_disk(store, routes_store):
    # ``budget_chars: 0`` is persisted verbatim (persistence does not normalise),
    # so the record on disk really is unusable until something repairs it.
    oversized = "z" * 9000
    _seed(store, [_entry(oversized, "e1")], budget=0)
    assert _disk_addendum(store)["budget_chars"] == 0

    # Reading repairs the cap to the default, so a write is now refused for
    # size rather than for a cap that could never be met.
    refused = client.post(url(), json={"text": "x"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["reason"] == "budget_exceeded"
    assert refused.json()["budget_chars"] == 8000

    cleared = client.delete(url())
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] == "cleared"
    assert cleared.json()["entries"] == []
    assert cleared.json()["budget_chars"] == 8000

    # The FILE no longer holds the unusable cap: clearing repaired it durably.
    assert _disk_addendum(store)["budget_chars"] == 8000

    accepted = client.post(url(), json={"text": "x"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["total_chars"] == 1
