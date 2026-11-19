"""S3 -- end-to-end: envelope ``images`` in, provider wire content out.

The other pasted-image suites test each seam in isolation:

* ``tests/test_pasted_images_transport.py`` -- WebSocket envelope -> ingest
  -> controller/bridge queue -> ``Agent.process_query`` metadata;
* ``tests/providers/test_openai_image_blocks.py`` -- canonical message ->
  ``build_wire_messages`` content blocks.

This module joins them: it feeds the *exact* metadata dicts
``ingest_incoming_images`` returns into ``build_wire_messages`` and asserts the
base64 that reaches the provider decodes back to the bytes the browser sent.
That is the assertion the two seam tests cannot make -- a renamed metadata key
(``image_id`` -> ``id``) would pass both of them and still lose the image here.

It also pins the per-turn payload cap: the per-image cap bounds one screenshot,
not a turn, so ``MAX_TOTAL_IMAGE_BYTES`` bounds the decoded total and a rejected
turn must leave nothing behind in the process-wide store.

No provider is called and no network is touched.
"""

import base64

import pytest

from agent.core import image_store
from agent.core.image_store import IMAGE_UNAVAILABLE_PLACEHOLDER
from llm_providers.openai_compatible import build_wire_messages
from web_ui.backend import image_ingest
from web_ui.backend.image_ingest import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_TOTAL_IMAGE_BYTES,
    ingest_incoming_images,
)

# ── Fixtures / helpers ─────────────────────────────────────────────────────

MB = 1024 * 1024

PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload" * 4
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-payload" * 4


def _data_url(data: bytes, media_type: str) -> str:
    """Build the data URL the frontend sends for one image."""
    return f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")


def _entry(data: bytes, media_type: str = "image/png", width: int = 1280,
           height: int = 720) -> dict:
    return {"data_url": _data_url(data, media_type), "media_type": media_type,
            "width": width, "height": height}


def _wire(messages, **kwargs):
    """Build wire messages for a vision-capable gpt-4o turn."""
    return build_wire_messages(
        messages, model="gpt-4o", supports_images=True, **kwargs
    )


def _image_urls(wire_message) -> list:
    """The ``image_url`` URLs of one wire message, in order."""
    return [
        part["image_url"]["url"]
        for part in wire_message["content"]
        if part.get("type") == "image_url"
    ]


@pytest.fixture(autouse=True)
def _clean_image_store():
    """Every test starts and ends with an empty image store."""
    image_store.reset()
    yield
    image_store.reset()


# ── Envelope -> store: the metadata contract ───────────────────────────────


def test_ingest_returns_id_metadata_whose_bytes_are_resolvable():
    """Ingest hands back ``id`` -- and that id resolves to the exact bytes."""
    metadata, error = ingest_incoming_images([_entry(PNG)])

    assert error is None
    assert len(metadata) == 1
    assert set(metadata[0]) == {"id", "media_type", "width", "height"}
    assert metadata[0]["media_type"] == "image/png"
    assert metadata[0]["width"] == 1280
    assert metadata[0]["height"] == 720

    stored = image_store.get(metadata[0]["id"])
    assert stored is not None, "ingest stored no bytes under the id it returned"
    assert stored == (PNG, "image/png")


# ── The join: ingest metadata -> wire content, byte exact ─────────────────


def test_ingest_to_wire_round_trip_is_byte_exact():
    """The base64 the provider receives decodes back to the sent bytes."""
    metadata, error = ingest_incoming_images([_entry(PNG)])
    assert error is None

    wire = _wire([
        {"role": "user", "content": "what is wrong with this screenshot?",
         "images": metadata},
    ])
    assert len(wire) == 1

    parts = wire[0]["content"]
    assert parts[0] == {"type": "text",
                        "text": "what is wrong with this screenshot?"}
    assert len(parts) == 2, "expected one text part and one image part"

    url = _image_urls(wire[0])[0]
    prefix = "data:image/png;base64,"
    assert url.startswith(prefix)
    assert base64.b64decode(url[len(prefix):]) == PNG
    # The envelope's ``data_url`` key must not survive into the wire message:
    # only the resolved content block carries those bytes any further.
    assert "data_url" not in str(wire)


def test_two_images_both_survive_the_join_in_order():
    """Both ids resolve, in envelope order, each to its own bytes."""
    metadata, error = ingest_incoming_images([
        _entry(PNG, "image/png"),
        _entry(JPEG, "image/jpeg"),
    ])
    assert error is None
    assert len(metadata) == 2

    wire = _wire([{"role": "user", "content": "compare these",
                   "images": metadata}])
    urls = _image_urls(wire[0])

    assert len(urls) == 2
    assert base64.b64decode(urls[0].split(",", 1)[1]) == PNG
    assert base64.b64decode(urls[1].split(",", 1)[1]) == JPEG
    assert urls[0].startswith("data:image/png;base64,")
    assert urls[1].startswith("data:image/jpeg;base64,")


# ── Backwards compatibility of the resolved key ───────────────────────────


def test_legacy_image_id_key_still_resolves_to_bytes():
    """A dict entry carrying the pre-rename ``image_id`` key still resolves."""
    image_id = image_store.put(PNG, "image/png")

    wire = _wire([{
        "role": "user",
        "content": "legacy transcript",
        "images": [{"image_id": image_id, "media_type": "image/png",
                    "width": 1, "height": 2}],
    }])

    urls = _image_urls(wire[0])
    assert len(urls) == 1, "legacy image_id entry degraded to the placeholder"
    assert base64.b64decode(urls[0].split(",", 1)[1]) == PNG
    assert IMAGE_UNAVAILABLE_PLACEHOLDER not in [
        part.get("text") for part in wire[0]["content"]
    ]


def test_canonical_id_key_and_plain_string_entries_resolve():
    """``id`` dicts (the ingest shape) and bare id strings both resolve."""
    image_id = image_store.put(PNG, "image/png")

    from_dict = _wire([{"role": "user", "content": "a",
                        "images": [{"id": image_id}]}])
    from_str = _wire([{"role": "user", "content": "b", "images": [image_id]}])

    for wire in (from_dict, from_str):
        urls = _image_urls(wire[0])
        assert len(urls) == 1
        assert base64.b64decode(urls[0].split(",", 1)[1]) == PNG


def test_unknown_image_id_degrades_to_placeholder_text():
    """A dangling id warns and degrades instead of failing the turn."""
    wire = _wire([{"role": "user", "content": "gone",
                   "images": ["0123456789abcdef"]}])

    parts = wire[0]["content"]
    assert _image_urls(wire[0]) == []
    assert {"type": "text", "text": IMAGE_UNAVAILABLE_PLACEHOLDER} in parts


# ── The per-turn payload cap ──────────────────────────────────────────────


def test_turn_over_total_cap_is_rejected_and_rolled_back():
    """6 MiB + 5 MiB passes the per-image cap but not the per-turn cap."""
    assert MAX_IMAGE_BYTES == 6 * MB

    metadata, error = ingest_incoming_images([
        _entry(b"\x89PNG\r\n\x1a\n" + b"a" * (6 * MB - 8), "image/png"),
        _entry(b"\xff\xd8\xff\xe0" + b"b" * (5 * MB - 4), "image/jpeg"),
    ])

    assert metadata == []
    assert error == "Images too large: 11 MB total (maximum 10 MB per message)"

    # Total rejection: the first image was already stored and must be released.
    stats = image_store.stats()
    assert stats["entries"] == 0
    assert stats["bytes"] == 0


def test_turn_exactly_at_total_cap_is_accepted():
    """Two 5 MiB images are exactly the 10 MiB cap: accepted, both kept."""
    metadata, error = ingest_incoming_images([
        _entry(b"\x89PNG\r\n\x1a\n" + b"a" * (5 * MB - 8), "image/png"),
        _entry(b"\xff\xd8\xff\xe0" + b"b" * (5 * MB - 4), "image/jpeg"),
    ])

    assert error is None
    assert len(metadata) == 2
    assert image_store.stats()["bytes"] == 10 * MB


def test_total_cap_override_accepts_exactly_the_cap():
    """``max_total_bytes`` is honoured at the boundary (one byte under too)."""
    first = b"x" * MB
    second = b"y" * MB

    metadata, error = ingest_incoming_images(
        [_entry(first, "image/png"), _entry(second, "image/png")],
        max_total_bytes=2 * MB,
    )

    assert error is None
    assert len(metadata) == 2
    assert image_store.stats()["bytes"] == 2 * MB


def test_total_cap_override_rejects_one_byte_over_and_releases():
    """One byte past ``max_total_bytes`` rejects the turn and stores nothing."""
    payload = b"x" * MB

    metadata, error = ingest_incoming_images(
        [_entry(payload, "image/png"),
         _entry(b"y" * (MB + 1), "image/png")],
        max_total_bytes=2 * MB,
    )

    assert metadata == []
    assert error == "Images too large: 2 MB total (maximum 2 MB per message)"
    assert image_store.stats()["entries"] == 0


# ── Constants / per-image cap stays independent ───────────────────────────


def test_total_cap_constant_is_exported_and_bounds_a_full_turn():
    """The cap is public, and it actually bounds MAX_IMAGES x MAX_IMAGE_BYTES."""
    assert MAX_TOTAL_IMAGE_BYTES == 10 * MB
    assert "MAX_TOTAL_IMAGE_BYTES" in image_ingest.__all__
    assert MAX_TOTAL_IMAGE_BYTES < MAX_IMAGES * MAX_IMAGE_BYTES


def test_per_image_cap_still_applies_independently_of_the_total():
    """A single image past ``MAX_IMAGE_BYTES`` fails the per-image rule."""
    oversized = b"\x89PNG\r\n\x1a\n" + b"a" * (MAX_IMAGE_BYTES - 8 + 1)

    metadata, error = ingest_incoming_images([_entry(oversized, "image/png")])

    assert metadata == []
    assert error == (
        f"Image 1: too large ({MAX_IMAGE_BYTES + 1} bytes, "
        f"maximum {MAX_IMAGE_BYTES} bytes)"
    )
    assert image_store.stats()["entries"] == 0

    # ...and a single image exactly at the per-image cap is fine: the turn cap
    # (10 MiB) is what makes it a *per-turn* limit, not a per-image one.
    at_cap = b"\x89PNG\r\n\x1a\n" + b"a" * (MAX_IMAGE_BYTES - 8)
    metadata, error = ingest_incoming_images([_entry(at_cap, "image/png")])
    assert error is None
    assert len(metadata) == 1
    assert image_store.get(metadata[0]["id"]) == (at_cap, "image/png")
