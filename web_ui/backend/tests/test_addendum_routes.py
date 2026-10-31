"""Integration tests (TestClient) for the W1.5 operator addendum REST endpoints.

Covers:

* ``GET    /api/session/{session_id}/addendum``
* ``POST   /api/session/{session_id}/addendum``
* ``DELETE /api/session/{session_id}/addendum``

Isolation follows ``test_permission_routes.py``: the store behind the routes is
replaced by monkeypatching ``session_routes._get_store`` (the addendum router
reads the store through that same accessor), and all vault-backed state is
redirected to a per-test temporary vault via ``THOUGHTMACHINE_VAULT_ROOT``.

The stub store round-trips REAL ``Session`` objects through
``to_persistable_dict`` / ``from_persistable_dict`` on every load, so a request
observes what the previous request actually persisted -- "persisted" here means
"what the next request reads back", not a shared in-memory object.

Case letters below match the W1.5 backend acceptance list (a)-(h).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.session_routes as session_routes
from agent.core.llm_client import ADDENDUM_HEADING, LLMClient
from llm_providers.factory import ProviderFactory
from session.models import Session
from web_ui.backend.server import app

client = TestClient(app)

SID = "sess-addendum-001"
WS = "ws-addendum-a"

#: A text long enough to be interesting and short enough to fit the default
#: budget (8000 chars) with plenty of room to spare.
TEXT = "Prefer terse answers while the migration is running."

BASE_PROMPT = "You are ThoughtMachine.\nBe precise; never invent file contents."

FOREIGN_ORIGIN = {"Origin": "https://evil.example"}


def url(session_id: str = SID) -> str:
    return f"/api/session/{session_id}/addendum"


def _entry(entry_id: str, text: str, source: str = "operator", pinned: bool = False) -> dict:
    return {
        "id": entry_id,
        "ts": "2025-01-01T00:00:00+00:00",
        "source": source,
        "text": text,
        "pinned": pinned,
    }


def _addendum(entries=(), budget_chars: int = 8000, max_entries: int = 64) -> dict:
    return {
        "entries": list(entries),
        "budget_chars": budget_chars,
        "max_entries": max_entries,
    }


def _seed_entries(count: int) -> list:
    return [_entry(f"e-{i:03d}", "x") for i in range(count)]


# ---------------------------------------------------------------------------
# Stub store: in-memory, but round-tripping real Session payloads
# ---------------------------------------------------------------------------

class _StubSessionStore:
    """Deterministic stand-in for ``FileSystemSessionStore``.

    ``load_session`` builds a FRESH ``Session`` from the stored payload on every
    call, and ``save_session`` writes the session's payload back, so a write is
    only visible to the next request if the route really persisted it.
    """

    def __init__(self):
        self._records = {}
        self.save_calls = []

    def register(self, session_id: str = SID, addendum=None, workspace_id: str = WS) -> None:
        session = Session(session_id=session_id, workspace_id=workspace_id)
        if addendum is not None:
            session.addendum = addendum
        self._records[session_id] = copy.deepcopy(session.to_persistable_dict())

    def load_session(self, session_id: str, workspace_id=None):
        record = self._records.get(session_id)
        if record is None:
            return None
        return Session.from_persistable_dict(copy.deepcopy(record))

    def save_session(self, session, workspace_id=None):
        self.save_calls.append(session.session_id)
        self._records[session.session_id] = copy.deepcopy(session.to_persistable_dict())

    # -- assertions helpers ------------------------------------------------

    def stored_addendum(self, session_id: str = SID) -> dict:
        return copy.deepcopy(self._records[session_id]["addendum"])


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point the vault-backed chain at an empty temp vault."""
    root = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def stub_store(monkeypatch):
    """Route ``session_routes._get_store()`` (and thus the addendum routes) to a stub."""
    store = _StubSessionStore()
    monkeypatch.setattr(session_routes, "_get_store", lambda: store)
    return store


# ---------------------------------------------------------------------------
# Composition helpers (mirror tests/test_addendum_composition.py)
# ---------------------------------------------------------------------------

class _RecordingProvider:
    """Provider stub: records the messages it is asked to complete."""

    def __init__(self):
        self.calls = []

    def chat_completion(self, messages=None, tools=None, **kwargs):
        self.calls.append(list(messages or []))
        return SimpleNamespace(
            content="ok", reasoning=None, tool_calls=None, usage={}, model="stub"
        )


def _make_config(system_prompt: str = BASE_PROMPT):
    return SimpleNamespace(
        provider_type="openai_compatible",
        api_key="test-key",
        base_url=None,
        model="test-model",
        temperature=0.2,
        provider_config={},
        system_prompt=system_prompt,
    )


def _compose_system_prompt(monkeypatch, session) -> str:
    """Run the REAL composition point (``LLMClient.ensure_system_prompt``)."""
    monkeypatch.setattr(
        ProviderFactory,
        "create_provider",
        staticmethod(lambda **kwargs: _RecordingProvider()),
    )
    llm = LLMClient(_make_config(), session=session)
    conversation = llm.ensure_system_prompt([{"role": "user", "content": "hello"}])
    system_messages = [m for m in conversation if m.get("role") == "system"]
    assert len(system_messages) == 1, "exactly one system message expected"
    return system_messages[0]["content"]


# ---------------------------------------------------------------------------
# (a) Fresh session: canonical defaults, no entries
# ---------------------------------------------------------------------------

def test_a_get_fresh_addendum_defaults(vault, stub_store):
    stub_store.register()

    resp = client.get(url())

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "entries": [],
        "budget_chars": 8000,
        "max_entries": 64,
        "total_chars": 0,
        "session_id": SID,
    }


# ---------------------------------------------------------------------------
# (b) Accepted write: 200, entry shape, persisted, total_chars
# ---------------------------------------------------------------------------

def test_b_post_accepts_and_persists_entry(vault, stub_store):
    stub_store.register()

    resp = client.post(url(), json={"text": TEXT})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["total_chars"] == len(TEXT)
    assert body["budget_chars"] == 8000
    assert body["max_entries"] == 64
    entry = body["entry"]
    assert entry["text"] == TEXT
    assert entry["source"] == "operator"
    assert entry["pinned"] is False
    assert isinstance(entry["id"], str) and entry["id"]
    assert isinstance(entry["ts"], str) and entry["ts"]
    assert [e["id"] for e in body["entries"]] == [entry["id"]]

    # Persisted: the store was asked to save, and a fresh load shows the entry.
    assert stub_store.save_calls == [SID]
    persisted = stub_store.stored_addendum()
    assert [e["text"] for e in persisted["entries"]] == [TEXT]

    # ... and the GET endpoint (a separate request, i.e. a fresh load) agrees.
    again = client.get(url()).json()
    assert [e["text"] for e in again["entries"]] == [TEXT]
    assert again["total_chars"] == len(TEXT)


# ---------------------------------------------------------------------------
# (c) Full chain: REST write -> persisted session -> composed system prompt
# ---------------------------------------------------------------------------

def test_c_written_entry_reaches_composed_system_prompt(vault, stub_store, monkeypatch):
    stub_store.register()

    resp = client.post(url(), json={"text": TEXT})
    assert resp.status_code == 200

    # Compose from the session as a LATER reader sees it (a fresh store load).
    session = stub_store.load_session(SID)
    system_prompt = _compose_system_prompt(monkeypatch, session)

    assert TEXT in system_prompt
    assert ADDENDUM_HEADING in system_prompt
    # Appended after the base prompt, never replacing it.
    assert system_prompt.startswith(BASE_PROMPT)
    assert system_prompt.index(BASE_PROMPT) < system_prompt.index(TEXT)


# ---------------------------------------------------------------------------
# (d) Refused: over budget (cap 100, write 101) -> 409, nothing persisted
# ---------------------------------------------------------------------------

def test_d_post_over_budget_refused(vault, stub_store):
    stub_store.register(addendum=_addendum([], budget_chars=100))

    resp = client.post(url(), json={"text": "x" * 101})

    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "refused"
    assert body["reason"] == "budget_exceeded"
    assert "101" in body["message"] and "100" in body["message"]
    assert "budget" in body["message"]
    # The refused set is reported unchanged, and nothing was written/persisted.
    assert body["entries"] == []
    assert body["total_chars"] == 0
    assert body["budget_chars"] == 100
    assert body["max_entries"] == 64
    assert stub_store.save_calls == []
    assert stub_store.stored_addendum()["entries"] == []
    assert client.get(url()).json()["entries"] == []


# ---------------------------------------------------------------------------
# (e) Refused: entry limit (64 stored, the 65th) -> 409
# ---------------------------------------------------------------------------

def test_e_post_at_max_entries_refused(vault, stub_store):
    stub_store.register(addendum=_addendum(_seed_entries(64)))

    resp = client.post(url(), json={"text": "y"})

    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "refused"
    assert body["reason"] == "max_entries_exceeded"
    assert "65" in body["message"] and "64" in body["message"]
    assert len(body["entries"]) == 64
    assert body["max_entries"] == 64
    assert stub_store.save_calls == []
    assert len(stub_store.stored_addendum()["entries"]) == 64


# ---------------------------------------------------------------------------
# (f) Recovery: over-budget refusal -> clear -> the next write is accepted
# ---------------------------------------------------------------------------

def test_f_recovery_after_clear(vault, stub_store):
    stub_store.register(addendum=_addendum([_entry("e-000", "z" * 90)], budget_chars=100))

    refused = client.post(url(), json={"text": "q" * 20})
    assert refused.status_code == 409
    assert refused.json()["reason"] == "budget_exceeded"

    cleared = client.delete(url())
    assert cleared.status_code == 200
    cleared_body = cleared.json()
    assert cleared_body == {
        "status": "cleared",
        "entries": [],
        "total_chars": 0,
        "budget_chars": 100,
        "max_entries": 64,
        "session_id": SID,
    }
    # The clear itself is persisted.
    assert stub_store.stored_addendum()["entries"] == []

    accepted = client.post(url(), json={"text": "q" * 20})
    assert accepted.status_code == 200
    accepted_body = accepted.json()
    assert accepted_body["status"] == "accepted"
    assert [e["text"] for e in accepted_body["entries"]] == ["q" * 20]
    assert accepted_body["total_chars"] == 20


# ---------------------------------------------------------------------------
# (g) Unknown session -> 404 on all three methods; foreign origin -> 403
# ---------------------------------------------------------------------------

def test_g_unknown_session_404_and_foreign_origin_403(vault, stub_store):
    missing = "sess-does-not-exist"

    for resp in (
        client.get(url(missing)),
        client.post(url(missing), json={"text": TEXT}),
        client.delete(url(missing)),
    ):
        assert resp.status_code == 404
        assert f"Session not found: {missing}" in str(resp.json().get("detail"))

    # The origin check runs FIRST: an unknown session behind a foreign Origin is
    # refused as an origin problem, not reported as a missing session.
    for resp in (
        client.get(url(missing), headers=FOREIGN_ORIGIN),
        client.post(url(missing), json={"text": TEXT}, headers=FOREIGN_ORIGIN),
        client.delete(url(missing), headers=FOREIGN_ORIGIN),
    ):
        assert resp.status_code == 403
        assert resp.json()["detail"] == "origin not permitted"


# ---------------------------------------------------------------------------
# (h) The client cannot choose the entry source
# ---------------------------------------------------------------------------

def test_h_client_cannot_smuggle_source(vault, stub_store):
    stub_store.register()

    resp = client.post(url(), json={"text": TEXT, "source": "mailbox"})

    assert resp.status_code == 200
    assert resp.json()["entry"]["source"] == "operator"
    assert stub_store.stored_addendum()["entries"][0]["source"] == "operator"
