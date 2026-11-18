"""S2 — transport of pasted images from the WebSocket envelope to the agent.

The feature's contract, from the boundary INWARD:

* the browser sends its images as a TOP-LEVEL ``images`` key of the envelope
  (a sibling of ``command``/``query``/``config``) -- never nested in
  ``config``, which is validated by ``SessionConfig`` (``extra='forbid'``) and
  filtered by ``_session_config_dict`` before it gets there;
* ``web_ui.backend.image_ingest`` is the ONE place a ``data:...;base64``
  string is parsed.  It decodes once, stores the bytes in the process-wide
  ``agent.core.image_store`` and returns METADATA ONLY;
* metadata rides the controller/bridge query queue as a ``(query, images)``
  payload so a query can never be paired with another turn's images;
* ``Agent.process_query(query, images=...)`` puts the metadata on the user
  message.

Everything here is a transport assertion: no provider is called, no agent
thread is started (the bridge is faked at the seam the server uses).
"""

import base64
import inspect
import json

import pytest

from web_ui.backend import image_ingest
from web_ui.backend.image_ingest import (
    IMAGE_ONLY_QUERY_PLACEHOLDER,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    ingest_incoming_images,
)

# ── Fixtures / helpers ───────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload" * 4
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-payload" * 4
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"webp-payload" * 4
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'/>"


def _data_url(data: bytes, media_type: str) -> str:
    """Build the data URL the frontend sends for one image."""
    return f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")


@pytest.fixture(autouse=True)
def _clean_image_store():
    """Every test starts and ends with an empty image store."""
    from agent.core import image_store

    image_store.reset()
    yield
    image_store.reset()


# ── T5.2 ingest units ───────────────────────────────────────────────────────


def test_ingest_single_png_returns_metadata_only():
    """One PNG in, one metadata dict out — the data URL must not survive."""
    metadata, error = ingest_incoming_images(
        [{"data_url": _data_url(PNG, "image/png"), "media_type": "image/png",
          "width": 1280, "height": 720}]
    )

    assert error is None
    assert len(metadata) == 1
    entry = metadata[0]
    assert set(entry) == {"id", "media_type", "width", "height"}
    assert entry["media_type"] == "image/png"
    assert (entry["width"], entry["height"]) == (1280, 720)
    assert "data_url" not in entry

    # The bytes are in the store, keyed by the returned id.
    from agent.core import image_store
    stored = image_store.get(entry["id"])
    assert stored is not None
    assert stored[0] == PNG


def test_ingest_accepts_all_allowed_media_types_up_to_the_cap():
    """PNG, JPEG and WEBP are all accepted, and MAX_IMAGES of them at once."""
    raw = [
        {"data_url": _data_url(PNG, "image/png"), "media_type": "image/png"},
        {"data_url": _data_url(JPEG, "image/jpeg"), "media_type": "image/jpeg"},
        {"data_url": _data_url(WEBP, "image/webp"), "media_type": "image/webp"},
        {"data_url": _data_url(PNG + b"4", "image/png"), "media_type": "image/png"},
    ]
    assert len(raw) == MAX_IMAGES

    metadata, error = ingest_incoming_images(raw)

    assert error is None
    assert [m["media_type"] for m in metadata] == [
        "image/png", "image/jpeg", "image/webp", "image/png",
    ]
    assert len({m["id"] for m in metadata}) == MAX_IMAGES


def test_ingest_rejects_more_than_max_images():
    """A fifth image rejects the whole turn (no partial acceptance)."""
    raw = [{"data_url": _data_url(PNG + bytes([i]), "image/png")} for i in range(MAX_IMAGES + 1)]

    metadata, error = ingest_incoming_images(raw)

    assert metadata == []
    assert error is not None
    assert "Too many images" in error
    from agent.core import image_store
    assert image_store.stats()["entries"] == 0


def test_ingest_rejects_oversized_image():
    """The decoded-size cap is enforced (and MAX_IMAGE_BYTES is 6 MiB)."""
    assert MAX_IMAGE_BYTES == 6 * 1024 * 1024

    # Cap parameterised down so the test never materialises 6 MiB of base64.
    too_big = _data_url(b"\x00" * 4096, "image/png")
    metadata, error = ingest_incoming_images(
        [{"data_url": too_big, "media_type": "image/png"}], max_bytes=1024
    )

    assert metadata == []
    assert error is not None
    assert "too large" in error


def test_ingest_rejects_svg():
    """SVG is a script-bearing document, not a bitmap: rejected by media type."""
    metadata, error = ingest_incoming_images(
        [{"data_url": _data_url(SVG, "image/svg+xml"), "media_type": "image/svg+xml"}]
    )

    assert metadata == []
    assert error is not None
    assert "unsupported media type" in error


def test_ingest_rejects_garbage_base64():
    """Invalid base64 is rejected, not silently decoded to nothing."""
    metadata, error = ingest_incoming_images(
        [{"data_url": "data:image/png;base64,!!!not-base64!!!", "media_type": "image/png"}]
    )

    assert metadata == []
    assert error is not None
    assert "not valid base64" in error


def test_ingest_accepts_bare_data_url_string():
    """A bare data URL (no wrapper object) is a legal entry."""
    metadata, error = ingest_incoming_images([_data_url(PNG, "image/png")])

    assert error is None
    assert len(metadata) == 1
    assert metadata[0]["media_type"] == "image/png"
    assert (metadata[0]["width"], metadata[0]["height"]) == (0, 0)


def test_ingest_rejects_media_type_mismatch():
    """A declared media_type that contradicts the data URL is a rejection."""
    metadata, error = ingest_incoming_images(
        [{"data_url": _data_url(PNG, "image/png"), "media_type": "image/jpeg"}]
    )

    assert metadata == []
    assert error is not None
    assert "does not match" in error


def test_ingest_coerces_dimensions():
    """Dimensions are advisory ints: junk/booleans become 0, numbers survive."""
    metadata, error = ingest_incoming_images([
        {"data_url": _data_url(PNG, "image/png"), "width": "1280", "height": True},
        {"data_url": _data_url(JPEG, "image/jpeg"), "width": 640, "height": 480},
    ])

    assert error is None
    assert (metadata[0]["width"], metadata[0]["height"]) == (0, 0)
    assert (metadata[1]["width"], metadata[1]["height"]) == (640, 480)


def test_ingest_none_and_empty_are_the_no_image_case():
    """No images is not an error."""
    assert ingest_incoming_images(None) == ([], None)
    assert ingest_incoming_images([]) == ([], None)


def test_ingest_releases_already_stored_images_on_reject():
    """A reject anywhere in the list must not leak earlier entries into the store."""
    from agent.core import image_store

    metadata, error = ingest_incoming_images([
        {"data_url": _data_url(PNG, "image/png"), "media_type": "image/png"},
        {"data_url": _data_url(SVG, "image/svg+xml"), "media_type": "image/svg+xml"},
    ])

    assert metadata == []
    assert error is not None
    assert image_store.stats()["entries"] == 0


# ── T5.1 / T5.3 transport: the real websocket_endpoint ──────────────────────


def _run_endpoint(monkeypatch, tmp_path, frames):
    """Drive the real ``server.websocket_endpoint`` with ``frames``.

    The bridge/controller the handler imports are replaced so nothing below
    the socket is exercised; every emitted ``send_json`` payload is returned.
    """
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    import web_ui.backend.server as server_mod
    from fastapi import WebSocketDisconnect

    inbox = [json.dumps(frame) for frame in frames]
    sent = []
    bridges = []
    controllers = []

    class FakeController:
        def __init__(self):
            controllers.append(self)

    class FakeBridge:
        # Set by a test to model ``bridge.start()`` leaving an agent running.
        auto_running = False

        def __init__(self, session_store=None):
            self.session_store = session_store
            self._loaded_session = None
            self._session_config = None
            self._controller = None
            self._session_id = None
            self._cleanly_closed = False
            self.agent_is_running = False
            self.start_calls = []
            self.continue_calls = []
            self.event_callback = None
            bridges.append(self)

        # ── lifecycle no-ops the handler calls ──
        def set_event_callback(self, callback, key=None):
            self.event_callback = callback

        def remove_event_callback(self, key=None):
            return None

        def register(self):
            return None

        def unregister(self):
            return None

        def set_controller(self, controller):
            self._controller = controller

        def stop(self):
            return None

        def save_open_session(self):
            return None

        # ── the two seams under test ──
        def start(self, query, session_config=None, images=None):
            self.start_calls.append(
                {"query": query, "session_config": session_config, "images": images}
            )
            self.agent_is_running = FakeBridge.auto_running

        def continue_session(self, query, config_dict=None, images=None):
            self.continue_calls.append(
                {"query": query, "config_dict": config_dict, "images": images}
            )

    monkeypatch.setattr("web_ui.backend.bridge.WebAgentBridge", FakeBridge)
    monkeypatch.setattr("agent.controller.AgentController", FakeController)

    class FakeWS:
        _closed = False
        client = "fake-client"

        async def accept(self):
            return None

        async def receive_text(self):
            if inbox:
                return inbox.pop(0)
            raise WebSocketDisconnect(1000)

        async def send_json(self, data):
            sent.append(data)

    asyncio.run(server_mod.websocket_endpoint(FakeWS()))
    return sent, bridges


def _run_endpoint_with_running_agent(monkeypatch, tmp_path, frames):
    """Same driver, but ``bridge.start()`` leaves the agent RUNNING.

    That is what makes the *next* ``continue_session`` frame take CASE 2
    (``bridge.continue_session``) instead of CASE 1/CASE 3.
    """
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    import web_ui.backend.server as server_mod
    from fastapi import WebSocketDisconnect

    inbox = [json.dumps(frame) for frame in frames]
    sent = []
    bridges = []

    class FakeBridge:
        def __init__(self, session_store=None):
            self.session_store = session_store
            self._loaded_session = None
            self._session_config = None
            self._controller = None
            self._session_id = None
            self._cleanly_closed = False
            self.agent_is_running = False
            self.start_calls = []
            self.continue_calls = []
            bridges.append(self)

        def set_event_callback(self, callback, key=None):
            return None

        def remove_event_callback(self, key=None):
            return None

        def register(self):
            return None

        def unregister(self):
            return None

        def set_controller(self, controller):
            self._controller = controller

        def stop(self):
            return None

        def save_open_session(self):
            return None

        def start(self, query, session_config=None, images=None):
            self.start_calls.append(
                {"query": query, "session_config": session_config, "images": images}
            )
            self.agent_is_running = True

        def continue_session(self, query, config_dict=None, images=None):
            self.continue_calls.append(
                {"query": query, "config_dict": config_dict, "images": images}
            )

    class FakeController:
        is_running = True

        def __init__(self):
            pass

    monkeypatch.setattr("web_ui.backend.bridge.WebAgentBridge", FakeBridge)
    monkeypatch.setattr("agent.controller.AgentController", FakeController)

    class FakeWS:
        _closed = False
        client = "fake-client"

        async def accept(self):
            return None

        async def receive_text(self):
            if inbox:
                return inbox.pop(0)
            raise WebSocketDisconnect(1000)

        async def send_json(self, data):
            sent.append(data)

    asyncio.run(server_mod.websocket_endpoint(FakeWS()))
    return sent, bridges


def _status_texts(sent):
    return [m.get("text", "") for m in sent if m.get("type") == "status_message"]


def test_start_session_forwards_top_level_images_to_bridge(monkeypatch, tmp_path):
    """The envelope's top-level ``images`` reaches bridge.start as metadata."""
    sent, bridges = _run_endpoint(monkeypatch, tmp_path, [{
        "command": "start_session",
        "query": "why does this fail?",
        "config": {},
        "images": [{"data_url": _data_url(PNG, "image/png"),
                    "media_type": "image/png", "width": 800, "height": 600}],
    }])

    assert len(bridges) == 1
    assert len(bridges[0].start_calls) == 1
    call = bridges[0].start_calls[0]
    assert call["query"] == "why does this fail?"
    assert isinstance(call["images"], list) and len(call["images"]) == 1
    entry = call["images"][0]
    assert set(entry) == {"id", "media_type", "width", "height"}
    assert entry["media_type"] == "image/png"
    assert (entry["width"], entry["height"]) == (800, 600)
    # The data URL stopped at the handler: nothing downstream ever sees base64.
    assert "base64" not in json.dumps(call["images"])
    assert any("Session started" in t for t in _status_texts(sent))


def test_image_only_start_session_uses_placeholder_query(monkeypatch, tmp_path):
    """A blank query WITH images is a legal image-only turn."""
    sent, bridges = _run_endpoint(monkeypatch, tmp_path, [{
        "command": "start_session",
        "query": "   ",
        "config": {},
        "images": [{"data_url": _data_url(JPEG, "image/jpeg"), "media_type": "image/jpeg"}],
    }])

    assert len(bridges) == 1
    call = bridges[0].start_calls[0]
    assert call["query"] == IMAGE_ONLY_QUERY_PLACEHOLDER
    assert len(call["images"]) == 1
    assert not any("cannot be empty" in t for t in _status_texts(sent))


def test_blank_query_without_images_is_still_rejected(monkeypatch, tmp_path):
    """Back-compat: the old empty-query rejection is unchanged."""
    sent, bridges = _run_endpoint(monkeypatch, tmp_path, [{
        "command": "start_session", "query": "", "config": {},
    }])

    assert bridges == []
    assert any("Query cannot be empty" in t for t in _status_texts(sent))


def test_rejected_image_aborts_the_turn_before_start(monkeypatch, tmp_path):
    """A bad image surfaces a status_message and starts nothing."""
    sent, bridges = _run_endpoint(monkeypatch, tmp_path, [{
        "command": "start_session",
        "query": "look at this",
        "config": {},
        "images": [{"data_url": _data_url(SVG, "image/svg+xml"),
                    "media_type": "image/svg+xml"}],
    }])

    assert bridges == []
    texts = _status_texts(sent)
    assert any("unsupported media type" in t for t in texts), texts


def test_images_nested_in_config_are_not_the_transport(monkeypatch, tmp_path):
    """``config`` is SessionConfig's surface (extra='forbid'): images there are dropped.

    The envelope's top level is the only image channel, so a client that nests
    them in ``config`` loses them silently rather than crashing the session.
    """
    sent, bridges = _run_endpoint(monkeypatch, tmp_path, [{
        "command": "start_session",
        "query": "hello",
        "config": {"images": [{"data_url": _data_url(PNG, "image/png")}]},
    }])

    assert any("Session started" in t for t in _status_texts(sent)), _status_texts(sent)
    assert len(bridges) == 1
    call = bridges[0].start_calls[0]
    assert call["images"] == []
    assert not hasattr(call["session_config"], "images")


def test_continue_session_forwards_images_to_running_bridge(monkeypatch, tmp_path):
    """An already-running bridge receives the metadata on continue_session."""
    sent, bridges = _run_endpoint_with_running_agent(monkeypatch, tmp_path, [
        {"command": "start_session", "query": "first", "config": {}},
        {"command": "continue_session", "query": "and this one",
         "images": [{"data_url": _data_url(WEBP, "image/webp"), "media_type": "image/webp"}]},
    ])

    assert len(bridges) == 1
    bridge = bridges[0]
    assert bridge.start_calls[0]["images"] == []
    assert len(bridge.continue_calls) == 1
    call = bridge.continue_calls[0]
    assert call["query"] == "and this one"
    assert len(call["images"]) == 1
    assert call["images"][0]["media_type"] == "image/webp"
    assert "base64" not in json.dumps(call["images"])


# ── T5.4 the widened signatures stay backwards compatible ───────────────────


def _assert_images_param(func, *, before_var_keyword=False):
    params = inspect.signature(func).parameters
    assert "images" in params, f"{func!r} has no images parameter"
    assert params["images"].default is None, "images must default to None"
    if before_var_keyword:
        names = list(params)
        assert names.index("images") < names.index(next(
            n for n in names if params[n].kind is inspect.Parameter.VAR_KEYWORD
        )), "images must precede **overrides or it can never be passed"


def test_widened_signatures_are_backwards_compatible():
    """Every widened entry point accepts its old call shape unchanged."""
    from agent.controller import AgentController
    from agent.core.agent import Agent
    from web_ui.backend.bridge import WebAgentBridge

    _assert_images_param(AgentController.process_query)
    _assert_images_param(AgentController.start, before_var_keyword=True)
    _assert_images_param(AgentController.continue_session)
    _assert_images_param(WebAgentBridge.start)
    _assert_images_param(WebAgentBridge.continue_session)
    _assert_images_param(Agent.process_query)

    # Old positional call shape still resolves (no TypeError).
    inspect.signature(Agent.process_query).bind(object(), "q")
    inspect.signature(AgentController.start).bind(
        object(), "q", None, None, "preset", some_override=1
    )


def test_queue_payload_round_trip_keeps_images_with_their_query(monkeypatch, tmp_path):
    """``(query, images)`` travels as one queue item; sentinels stay bare."""
    from agent.controller import _queue_payload, _split_queue_payload

    images = [{"id": "abc123", "media_type": "image/png", "width": 1, "height": 2}]
    assert _split_queue_payload(_queue_payload("hi", images)) == ("hi", images)
    assert _split_queue_payload("[STOP]") == ("[STOP]", None)
    assert _split_queue_payload(_queue_payload("no images")) == ("no images", None)


# ── T5.5 the frame budget is large enough to be stated explicitly ───────────


def test_uvicorn_websocket_frame_budget_is_16_mib(monkeypatch, tmp_path):
    """main() must pass ws_max_size explicitly rather than inherit a default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    import web_ui.backend.server as server_mod

    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: captured.update(kw))
    monkeypatch.setattr("sys.argv", ["server"])

    server_mod.main()

    assert "ws_max_size" in captured, captured
    assert captured["ws_max_size"] == 16 * 1024 * 1024
    assert captured["ws_max_size"] == server_mod.WS_MAX_SIZE_BYTES
