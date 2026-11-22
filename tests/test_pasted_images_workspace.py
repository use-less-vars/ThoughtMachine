"""Pasted images must be written to the workspace the READ route will search.

Regression: the browser's WebSocket URL carries no ``?project=``
(``frontend/src/apiBase.js:wsUrl``), so ``_handshake_ws_id`` was ``None`` for
every browser connection and ``ingest_incoming_images`` was called with
``workspace_id=None``: the bytes landed in the process-wide cache only, nothing
was written to ``<workspace>/images/``, and after an F5
``GET /api/session/{sid}/image/{id}`` answered 404 ``image-unreadable`` -- the
transcript showed the "Image" placeholder for a paste the user had just made.

The fix is ``server._ingest_turn_images`` (one entry point for both
``start_session`` and ``continue_session``) which resolves the workspace from
the session binding first, exactly like ``session_routes._session_workspace_id``
does on the read side.

Contract pinned here
--------------------
1. No handshake project + a session that IS bound to a workspace => the bytes
   are written to ``<workspace>/images/`` under the metadata id.
2. That same id, re-read through the real route with the store as the only
   authority (a reload: no live bridge), is a 200 with its stored media type.
3. Resolution precedence: session binding, then the bridge's binding, then the
   handshake pin.
4. No workspace anywhere => behaviour is unchanged (cache-only, no file) and
   the cache-only path is WARNING-logged instead of silent.
"""

from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session.store as store_mod  # noqa: E402  (path setup must run first)
from agent.core import image_files, image_store  # noqa: E402
from session.store import FileSystemSessionStore  # noqa: E402
from web_ui.backend import session_routes  # noqa: E402
import web_ui.backend.server as server_mod  # noqa: E402

WS_ALPHA = "ws-alpha"
WS_BETA = "ws-beta"
SESSION_ID = "sess-ingest-workspace-1"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"png-payload"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-payload"

URL = "/api/session/{session_id}/image/{image_id}"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")


def _entry(data: bytes = PNG_BYTES, media_type: str = "image/png") -> dict:
    return {"data_url": _data_url(data, media_type), "media_type": media_type,
            "width": 800, "height": 600}


def _write_session(
    vault: Path,
    workspace_id: str,
    session_id: str = SESSION_ID,
    images: list | None = None,
) -> None:
    """Persist a session under ``workspaces/<ws>/sessions/`` (workspace by dir)."""
    path = vault / "workspaces" / workspace_id / "sessions" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "metadata": {"name": "Ingest workspace test"},
        "updated_at": "2024-01-01T00:00:00",
        "user_history": [
            {"role": "user", "content": "what is this?", "images": list(images or [])},
            {"role": "assistant", "content": "a screenshot"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _continue_envelope(session_id: str = SESSION_ID, data: bytes = PNG_BYTES,
                       media_type: str = "image/png") -> dict:
    """The exact envelope ``QueryBar`` sends: images at the TOP LEVEL."""
    return {
        "command": "continue_session",
        "query": "what is this?",
        "session_id": session_id,
        "images": [_entry(data, media_type)],
    }


def _live_bridge(workspace_id: str, session_id: str = SESSION_ID):
    """A bridge that knows its workspace and its session, per ``WebAgentBridge``."""
    return types.SimpleNamespace(
        _session_id=session_id,
        _loaded_session=types.SimpleNamespace(session_id=session_id),
        _workspace_id=workspace_id,
    )


def _stored_files(workspace_id: str) -> list:
    directory = image_files.workspace_images_dir(workspace_id)
    return sorted(p.name for p in directory.glob("*")) if directory.exists() else []


# ── Harness ───────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point both ``vault_root`` bindings at a temporary directory."""
    monkeypatch.setattr(image_files, "vault_root", lambda: tmp_path)
    monkeypatch.setattr(store_mod, "vault_root", lambda: tmp_path)
    image_store.reset()
    yield tmp_path
    image_store.reset()


@pytest.fixture
def store(vault):
    return FileSystemSessionStore(
        sessions_dir=str(vault / "sessions"),
        state_dir=str(vault / "state"),
    )


@pytest.fixture
def no_live_sessions(monkeypatch):
    """No bridge holds any session: after a reload the store is the only answer."""
    monkeypatch.setattr(session_routes, "_active_tab_bridges", [])
    monkeypatch.setattr(server_mod, "_session_bridges", {})


@pytest.fixture
def client(store, no_live_sessions, monkeypatch):
    monkeypatch.setattr(session_routes, "_get_store", lambda: store)
    application = FastAPI()
    application.include_router(session_routes.router)
    with TestClient(application) as test_client:
        yield test_client


# ── 1. No handshake project, but the session is bound: a real file is written ─


def test_ingest_without_handshake_project_persists_to_the_session_workspace(
    vault, store
):
    """The browser case: no ``?project=``, but the session names a workspace."""
    _write_session(vault, WS_ALPHA)

    metadata, error = server_mod._ingest_turn_images(
        _continue_envelope(), None, store, None,
    )

    assert error is None
    assert len(metadata) == 1
    image_id = metadata[0]["id"]
    path = image_files.image_path(WS_ALPHA, image_id)
    assert path is not None, "no durable file: ingest fell back to cache-only"
    assert path.parent == image_files.workspace_images_dir(WS_ALPHA)
    assert path.read_bytes() == PNG_BYTES
    assert _stored_files(WS_ALPHA) == [path.name]


# ── 2. That id survives a reload and is served by the real route ──────────────


def test_ingested_image_is_served_by_the_route_after_a_reload(vault, store, client):
    """Simulate F5: the turn's save persisted the id, no live bridge remains."""
    _write_session(vault, WS_ALPHA)

    metadata, error = server_mod._ingest_turn_images(
        _continue_envelope(data=JPEG_BYTES, media_type="image/jpeg"),
        None,
        store,
        None,
    )
    assert error is None
    image_id = metadata[0]["id"]

    # The end-of-turn save persists the transcript that references the id.
    _write_session(vault, WS_ALPHA, images=[image_id])

    response = client.get(URL.format(session_id=SESSION_ID, image_id=image_id))

    assert response.status_code == 200
    assert response.content == JPEG_BYTES
    assert response.headers["content-type"] == "image/jpeg"


# ── 3. Precedence ────────────────────────────────────────────────────────────


def test_session_binding_wins_over_a_handshake_pin_of_another_workspace(vault, store):
    """The read route resolves the workspace from the store first, so must we."""
    _write_session(vault, WS_ALPHA)

    metadata, error = server_mod._ingest_turn_images(
        _continue_envelope(), None, store, WS_BETA,
    )

    assert error is None
    image_id = metadata[0]["id"]
    assert image_files.image_path(WS_ALPHA, image_id) is not None
    assert _stored_files(WS_BETA) == []


def test_live_bridge_supplies_the_workspace_when_the_store_has_no_record(vault, store):
    """A brand-new session is not persisted yet; its bridge still knows."""
    assert store.load_session_workspace_id(SESSION_ID) is None

    metadata, error = server_mod._ingest_turn_images(
        _continue_envelope(), _live_bridge(WS_ALPHA), store, None,
    )

    assert error is None
    assert image_files.image_path(WS_ALPHA, metadata[0]["id"]) is not None


def test_handshake_pin_is_the_last_resort_when_nothing_binds_a_session(vault, store):
    """No session id and no bridge: the ``?project=`` pin is the only authority."""
    envelope = {"command": "start_session", "query": "look", "images": [_entry()]}

    metadata, error = server_mod._ingest_turn_images(envelope, None, store, WS_BETA)

    assert error is None
    assert image_files.image_path(WS_BETA, metadata[0]["id"]) is not None


# ── 3b. The client's own workspace id (a fresh session has nothing else) ────


def test_envelope_workspace_id_places_the_first_turn_of_a_new_session(
    vault, store, monkeypatch
):
    """A brand-new session: no session id, no bridge, no handshake pin.

    The browser still knows which workspace it is looking at, so ``QueryBar``
    puts it on the ``start_session`` envelope (``SessionTab`` -> ``workspaceId``).
    That id is validated against the registry and must then be the workspace the
    bytes are written to -- otherwise the first pasted image of every session
    is cache-only and dies on the next reload.
    """
    monkeypatch.setattr(
        server_mod, "_is_registered_workspace", lambda wid: wid in {WS_ALPHA, WS_BETA}
    )
    envelope = {
        "command": "start_session",
        "query": "look",
        "workspace_id": WS_ALPHA,
        "images": [_entry()],
    }

    metadata, error = server_mod._ingest_turn_images(envelope, None, store, None)

    assert error is None
    image_id = metadata[0]["id"]
    path = image_files.image_path(WS_ALPHA, image_id)
    assert path is not None, "envelope workspace_id was ignored: ingest stayed cache-only"
    assert path.parent == image_files.workspace_images_dir(WS_ALPHA)
    assert path.read_bytes() == PNG_BYTES
    assert _stored_files(WS_BETA) == []


def test_envelope_workspace_id_that_is_not_registered_stays_cache_only(
    vault, store, monkeypatch, capsys
):
    """Request input is never trusted: an unregistered id buys no directory."""
    monkeypatch.setattr(
        server_mod, "_is_registered_workspace", lambda wid: wid in {WS_ALPHA, WS_BETA}
    )
    envelope = {
        "command": "start_session",
        "query": "look",
        "workspace_id": "ws-ghost",
        "images": [_entry()],
    }

    metadata, error = server_mod._ingest_turn_images(envelope, None, store, None)

    assert error is None
    image_id = metadata[0]["id"]
    assert image_store.get(image_id) is not None
    assert image_files.image_path(WS_ALPHA, image_id) is None
    assert _stored_files(WS_ALPHA) == []
    assert _stored_files(WS_BETA) == []
    # No directory tree was materialised for the rejected id.  (Checked on the
    # filesystem, not via workspace_images_dir: that helper CREATES the dir.)
    assert not (vault / "workspaces" / "ws-ghost").exists()

    err = capsys.readouterr().err
    assert "not a registered workspace" in err
    assert "CACHE-ONLY" in err


# ── 4. Nothing resolves: unchanged cache-only path, now WARNING-logged ───────


def test_no_workspace_anywhere_stays_cache_only_and_warns(vault, store, capsys):
    """Unchanged behaviour (cache-only), but no longer a silent one."""
    envelope = {
        "command": "start_session",
        "query": "look",
        "session_id": "session-with-no-record",
        "images": [_entry()],
    }

    metadata, error = server_mod._ingest_turn_images(envelope, None, store, None)

    assert error is None
    image_id = metadata[0]["id"]
    # Cache-only: the bytes are served by this process, no file exists anywhere.
    assert image_store.get(image_id) is not None
    assert image_files.image_path(WS_ALPHA, image_id) is None
    assert _stored_files(WS_ALPHA) == []
    assert _stored_files(WS_BETA) == []

    err = capsys.readouterr().err
    assert "CACHE-ONLY" in err
    assert "session-with-no-record" in err
    assert "1 pasted image(s)" in err
