"""``GET /api/session/{session_id}/image/{image_id}`` serves pasted-image bytes.

The transcript carries only a short opaque id per pasted image; the pixels live
in the workspace's durable image store
(``agent.core.image_files``).  This route is the bridge between the two, so it is
the one place a browser can ask for the bytes behind an id -- and therefore the
one place that has to refuse everything it should not serve.

Contract pinned here
--------------------
1. A 200 carries the exact stored bytes and the media type the store derived
   from the file's own extension -- never a client-supplied string.  Pinned for
   every whitelisted type (png, jpeg, webp).
2. Every miss is the same 404.  An id the session's history never referenced, an
   id whose bytes belong to a *different* workspace, an id whose bytes are gone,
   a malformed id and an unknown session are indistinguishable to the caller:
   the route is double-bound (session AND workspace), and a refusal must not
   become an oracle for what exists elsewhere.
3. The session's history is read LIVE-FIRST.  A freshly pasted id is appended to
   the running ``Session`` object and only reaches disk at the end-of-turn save,
   so the store alone would 404 a thumbnail the user is already looking at.
4. The route is behind the single origin/credential gate
   (``AuthBoundaryMiddleware``), like every other non-exempt path: no credential,
   no bytes.

Everything is hermetic: the vault root is a temporary directory (patched on both
modules that bind it by name), the session store is a real
``FileSystemSessionStore`` over that directory, and live sessions are injected
as plain namespaces.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session.store as store_mod  # noqa: E402  (path setup must run first)
from agent.core import image_files  # noqa: E402
from session.store import FileSystemSessionStore  # noqa: E402
from web_ui.backend import session_routes  # noqa: E402
from web_ui.backend import auth_boundary  # noqa: E402
from web_ui.backend.auth_boundary import AuthBoundaryMiddleware  # noqa: E402

WS_ALPHA = "ws-alpha"
WS_BETA = "ws-beta"
SESSION_ID = "sess-image-1"

#: Secret injected into the boundary the ``gated`` fixture builds.
TEST_TOKEN = "session-image-route-test-token"

# A minimal byte string per whitelisted type.  Never decoded -- the route is a
# byte pipe -- so these only have to be distinguishable from one another.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"png-payload"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-payload"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"webp-payload"

URL = "/api/session/{session_id}/image/{image_id}"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _url(session_id: str = SESSION_ID, image_id: str = "") -> str:
    return URL.format(session_id=session_id, image_id=image_id)


def _write_session(
    vault: Path,
    workspace_id: str,
    session_id: str = SESSION_ID,
    images: list | None = None,
) -> None:
    """Persist a session whose last user message references *images*."""
    path = vault / "workspaces" / workspace_id / "sessions" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "metadata": {"name": "Image route test"},
        "updated_at": "2024-01-01T00:00:00",
        "user_history": [
            {"role": "user", "content": "what is this?", "images": list(images or [])},
            {"role": "assistant", "content": "a screenshot"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _live_bridge(workspace_id: str, images: list, session_id: str = SESSION_ID):
    """A bridge holding one in-memory session that references *images*.

    Shape follows ``WebAgentBridge``: the running agent's own ``Session`` is the
    most authoritative holder, the workspace binding lives on the bridge.
    """
    session = types.SimpleNamespace(
        session_id=session_id,
        user_history=[
            {"role": "user", "content": "look", "images": list(images)},
        ],
    )
    return types.SimpleNamespace(
        _agent=types.SimpleNamespace(session=session),
        _session=None,
        _loaded_session=None,
        _workspace_id=workspace_id,
    )


# ── Harness ───────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point both ``vault_root`` bindings at ``tmp_path``.

    ``agent.core.image_files`` and ``session.store`` each import the helper by
    name, so the module attribute is what actually redirects their paths.
    """
    monkeypatch.setattr(image_files, "vault_root", lambda: tmp_path)
    monkeypatch.setattr(store_mod, "vault_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def store(vault):
    return FileSystemSessionStore(
        sessions_dir=str(vault / "sessions"),
        state_dir=str(vault / "state"),
    )


@pytest.fixture
def no_live_sessions(monkeypatch):
    """No bridge anywhere holds the session: the store is the only answer."""
    monkeypatch.setattr(session_routes, "_active_tab_bridges", [])
    monkeypatch.setitem(
        sys.modules,
        "web_ui.backend.server",
        types.SimpleNamespace(_session_bridges={}),
    )


@pytest.fixture
def app(store, no_live_sessions, monkeypatch):
    """Bare app over this router, reading the temp store.

    No boundary middleware: these tests pin the route's own decisions, and the
    gate gets a dedicated test of its own (``test_credential_is_required``).
    """
    monkeypatch.setattr(session_routes, "_get_store", lambda: store)
    application = FastAPI()
    application.include_router(session_routes.router)
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


# ── 200: the bytes, and the type the store derived ───────────────────────────


@pytest.mark.parametrize(
    "media_type,payload",
    [
        ("image/png", PNG_BYTES),
        ("image/jpeg", JPEG_BYTES),
        ("image/webp", WEBP_BYTES),
    ],
)
def test_stored_image_is_served_with_its_own_media_type(
    client, vault, media_type, payload
):
    """Round-trip: exact bytes back, content type from the stored extension."""
    image_id = image_files.save_image(WS_ALPHA, media_type, payload)
    _write_session(vault, WS_ALPHA, images=[image_id])

    response = client.get(_url(image_id=image_id))

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == media_type


def test_image_descriptor_dicts_in_history_are_recognised(client, vault):
    """A history entry may be the richer ``{"id": ...}`` descriptor, not a bare id."""
    image_id = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    _write_session(vault, WS_ALPHA, images=[{"id": image_id, "media_type": "image/png"}])

    response = client.get(_url(image_id=image_id))

    assert response.status_code == 200
    assert response.content == PNG_BYTES


# ── 404: every miss looks the same ───────────────────────────────────────────


def test_bytes_of_another_workspace_are_not_served(client, vault):
    """The route is workspace-bound: a foreign workspace's bytes are a miss."""
    image_id = image_files.save_image(WS_BETA, "image/png", PNG_BYTES)
    _write_session(vault, WS_ALPHA, images=[image_id])

    response = client.get(_url(image_id=image_id))

    assert response.status_code == 404
    # Nothing about the foreign copy leaks into the refusal.
    assert WS_BETA not in response.text
    assert PNG_BYTES not in response.content


def test_id_the_session_never_referenced_is_not_served(client, vault):
    """A well-formed id that exists, but is not in THIS session's history."""
    referenced = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    unrelated = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    _write_session(vault, WS_ALPHA, images=[referenced])

    assert client.get(_url(image_id=referenced)).status_code == 200
    assert client.get(_url(image_id=unrelated)).status_code == 404


def test_unknown_session_is_not_served(client, vault):
    """An id nothing persisted, with no live session, is a miss (not an error)."""
    image_id = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)

    assert client.get(_url(session_id="no-such-session", image_id=image_id)).status_code == 404


def test_malformed_image_id_is_not_served(client, vault):
    """An id that is not 32 lowercase hex names no file, referenced or not."""
    _write_session(vault, WS_ALPHA, images=["NOT-AN-IMAGE-ID"])

    assert client.get(_url(image_id="NOT-AN-IMAGE-ID")).status_code == 404


def test_missing_bytes_are_not_served(client, vault):
    """The history still references the id, but the bytes are gone."""
    image_id = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    _write_session(vault, WS_ALPHA, images=[image_id])
    assert image_files.delete_image(WS_ALPHA, image_id) is True

    assert client.get(_url(image_id=image_id)).status_code == 404


# ── Live history is read before the save ─────────────────────────────────────


@pytest.mark.parametrize("registry", ["warm_cache", "tab_set"])
def test_live_history_serves_a_paste_that_is_not_saved_yet(
    client, vault, monkeypatch, registry
):
    """The id is only on the running Session -- the store has never seen it.

    Both live registries must be consulted: a bridge can sit in the server's
    warm cache without being in the tab set (the warm-reuse path re-attaches a
    cached bridge without re-registering it).
    """
    image_id = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    # Persisted history predates the paste: the store cannot resolve this id.
    _write_session(vault, WS_ALPHA, images=[])
    bridge = _live_bridge(WS_ALPHA, [image_id])

    if registry == "warm_cache":
        monkeypatch.setitem(
            sys.modules,
            "web_ui.backend.server",
            types.SimpleNamespace(_session_bridges={SESSION_ID: bridge}),
        )
    else:
        monkeypatch.setattr(session_routes, "_active_tab_bridges", [bridge])

    response = client.get(_url(image_id=image_id))

    assert response.status_code == 200
    assert response.content == PNG_BYTES


def test_live_binding_supplies_the_workspace_before_the_first_save(
    client, vault, monkeypatch
):
    """A brand-new session has no persisted record naming a workspace yet.

    The store answers ``None`` for the workspace, so the live binding must
    supply it -- otherwise the first paste of a brand-new session 404s.
    """
    image_id = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    bridge = _live_bridge(WS_ALPHA, [image_id])
    monkeypatch.setattr(session_routes, "_active_tab_bridges", [bridge])
    # No session file at all: nothing on disk names a workspace for it.
    assert not (vault / "workspaces" / WS_ALPHA / "sessions").exists()

    response = client.get(_url(image_id=image_id))

    assert response.status_code == 200
    assert response.content == PNG_BYTES


def test_live_history_of_another_session_is_not_consulted(client, vault, monkeypatch):
    """A bridge for a different session cannot vouch for this one."""
    image_id = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    _write_session(vault, WS_ALPHA, images=[])
    monkeypatch.setattr(
        session_routes,
        "_active_tab_bridges",
        [_live_bridge(WS_ALPHA, [image_id], session_id="some-other-session")],
    )

    assert client.get(_url(image_id=image_id)).status_code == 404


# ── The gate ─────────────────────────────────────────────────────────────────


def test_credential_is_required(app, vault):
    """The bytes are behind the one origin/credential gate, like every non-exempt path."""
    image_id = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    _write_session(vault, WS_ALPHA, images=[image_id])
    app.add_middleware(AuthBoundaryMiddleware, token=TEST_TOKEN)

    with TestClient(app) as gated:
        refused = gated.get(_url(image_id=image_id))
        gated.cookies.set(
            auth_boundary.COOKIE_NAME, auth_boundary.session_credential(TEST_TOKEN)
        )
        served = gated.get(_url(image_id=image_id))

    assert refused.status_code == 401
    assert refused.json() == {"error": "unauthorized"}
    assert "www-authenticate" in refused.headers
    assert served.status_code == 200
    assert served.content == PNG_BYTES


# ── Response headers: stated, not inherited ──────────────────────────────────


def test_success_carries_binary_safe_response_headers(client, vault):
    """A 200 says outright how the bytes may be interpreted, cached and opened.

    The first binary response in this codebase: the browser must not sniff a
    type of its own choosing, must not treat the bytes as navigable, and may
    cache them hard because an id's bytes never change.
    """
    payload = PNG_BYTES
    image_id = image_files.save_image(WS_ALPHA, "image/png", payload)
    _write_session(vault, WS_ALPHA, images=[image_id])

    response = client.get(_url(image_id=image_id))

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, max-age=3600, immutable"
    assert response.headers["content-disposition"] == "inline"
    assert int(response.headers["content-length"]) == len(payload)


# ── Every miss is THE SAME 404 ───────────────────────────────────────────────


def test_all_six_miss_kinds_return_the_same_body(client, vault):
    """Six different reasons to refuse, one indistinguishable answer.

    A refusal must not become an oracle: if an unknown session, an unreferenced
    id, a malformed id, a foreign workspace's id and a deleted id differed in
    status or body, the route would report what exists where.
    """
    referenced = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    foreign = image_files.save_image(WS_BETA, "image/png", PNG_BYTES)
    gone = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    unrelated = image_files.save_image(WS_ALPHA, "image/png", PNG_BYTES)
    _write_session(
        vault, WS_ALPHA, images=[referenced, foreign, gone, "NOT-AN-IMAGE-ID"]
    )
    assert image_files.delete_image(WS_ALPHA, gone) is True

    responses = [
        client.get(_url(session_id="no-such-session", image_id=referenced)),
        client.get(_url(image_id=unrelated)),
        client.get(_url(image_id="NOT-AN-IMAGE-ID")),
        client.get(_url(image_id=foreign)),
        client.get(_url(image_id=gone)),
        client.get(_url(image_id=image_files.mint_image_id())),
    ]

    assert [response.status_code for response in responses] == [404] * 6
    bodies = [response.json() for response in responses]
    assert bodies[0] == {"detail": "not found"}
    assert all(body == bodies[0] for body in bodies)


def test_miss_logs_reason_without_a_path(client, vault, caplog):
    """The reason survives server-side while the client learns nothing.

    Operators need to tell the miss kinds apart; the caller must not.  The log
    line names the reason and the two identifiers, and no filesystem path.
    """
    unreferenced = image_files.mint_image_id()

    with caplog.at_level(logging.INFO):
        response = client.get(
            _url(session_id="no-such-session", image_id=unreferenced)
        )

    assert response.status_code == 404
    records = [
        record
        for record in caplog.records
        if record.name == "web_ui.backend.session_routes"
    ]
    assert records, "a miss is recorded server-side"
    text = "\n".join(record.getMessage() for record in records)
    assert "unknown-session" in text
    assert "/" not in text
    assert str(vault) not in text
    assert "unknown-session" not in response.text
