"""Tests for the ephemeral pasted-image store (``agent/core/image_store.py``).

The store holds the only copy of a pasted screenshot: the transcript carries a
16-character id, never the bytes. Everything here is plain in-memory state --
no provider, no network, no fixtures beyond resetting the shared store.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_store
from agent.core.image_store import (
    IMAGE_UNAVAILABLE_PLACEHOLDER,
    IMAGE_UNSUPPORTED_PLACEHOLDER,
    MAX_BYTES,
    MAX_ENTRIES,
    ImageStore,
)


@pytest.fixture(autouse=True)
def _clean_store():
    """Every test starts and ends with an empty shared store."""
    image_store.reset()
    yield
    image_store.reset()


# ---------------------------------------------------------------------------
# Content addressing
# ---------------------------------------------------------------------------


def test_put_returns_a_short_content_addressed_id():
    image_id = image_store.put(b'\x89PNG-body', 'image/png')

    assert isinstance(image_id, str)
    assert len(image_id) == 16
    assert all(char in '0123456789abcdef' for char in image_id)


def test_same_bytes_pasted_twice_store_one_copy():
    first = image_store.put(b'identical', 'image/png')
    second = image_store.put(b'identical', 'image/png')

    assert first == second
    assert image_store.stats()['entries'] == 1


def test_different_bytes_get_different_ids():
    first = image_store.put(b'one', 'image/png')
    second = image_store.put(b'two', 'image/png')

    assert first != second
    assert image_store.stats()['entries'] == 2


def test_get_returns_the_bytes_and_media_type():
    image_id = image_store.put(b'\x89PNG-body', 'image/webp')

    stored = image_store.get(image_id)

    assert stored == (b'\x89PNG-body', 'image/webp')


def test_get_of_an_unknown_id_is_a_miss_not_an_exception():
    assert image_store.get('0000000000000000') is None


def test_put_rejects_non_bytes():
    with pytest.raises(TypeError):
        image_store.put('not bytes', 'image/png')


# ---------------------------------------------------------------------------
# Release / reconcile / stats / reset
# ---------------------------------------------------------------------------


def test_release_drops_the_entry():
    image_id = image_store.put(b'bytes', 'image/png')

    assert image_store.release(image_id) is True
    assert image_store.get(image_id) is None
    assert image_store.stats()['entries'] == 0


def test_release_of_an_unknown_id_reports_false():
    assert image_store.release('0000000000000000') is False


def test_reconcile_drops_images_the_transcript_no_longer_mentions():
    kept = image_store.put(b'kept', 'image/png')
    dropped = image_store.put(b'dropped', 'image/png')

    result = image_store.reconcile([kept])

    assert result['entries'] == 1
    assert result['ids'] == [kept]
    assert image_store.get(dropped) is None


def test_reconcile_counts_a_reference_twice_for_the_same_id():
    """A screenshot quoted in two messages must not be evicted between them."""
    image_id = image_store.put(b'quoted twice', 'image/png')

    image_store.reconcile([image_id, image_id])

    assert image_store.get(image_id) is not None


def test_stats_reports_capacity_and_payload_size():
    image_store.put(b'12345', 'image/png')

    reported = image_store.stats()

    assert reported['entries'] == 1
    assert reported['bytes'] == 5
    assert reported['max_entries'] == MAX_ENTRIES
    assert reported['max_bytes'] == MAX_BYTES


def test_reset_drops_everything():
    image_id = image_store.put(b'bytes', 'image/png')

    image_store.reset()

    assert image_store.get(image_id) is None
    assert image_store.stats() == {
        'entries': 0,
        'bytes': 0,
        'ids': [],
        'max_entries': MAX_ENTRIES,
        'max_bytes': MAX_BYTES,
    }


# ---------------------------------------------------------------------------
# Bounded capacity (the store must never grow without limit)
# ---------------------------------------------------------------------------


def test_entry_cap_evicts_the_least_recently_used_image():
    store = ImageStore(max_entries=2)
    old = store.put(b'old', 'image/png')
    new = store.put(b'new', 'image/png')

    latest = store.put(b'latest', 'image/png')

    assert store.get(old) is None
    assert store.get(new) is not None
    assert store.get(latest) is not None
    assert store.stats()['entries'] == 2


def test_a_use_refreshes_the_lru_position():
    store = ImageStore(max_entries=2)
    old = store.put(b'old', 'image/png')
    new = store.put(b'new', 'image/png')

    assert store.get(old) is not None  # touch: `new` is now the coldest
    store.put(b'latest', 'image/png')

    assert store.get(old) is not None
    assert store.get(new) is None


def test_byte_cap_evicts_until_the_new_image_fits():
    store = ImageStore(max_entries=10, max_bytes=10)
    first = store.put(b'123456', 'image/png')

    second = store.put(b'abcdef', 'image/png')

    assert store.stats()['bytes'] <= 10
    assert store.get(first) is None
    assert store.get(second) == (b'abcdef', 'image/png')


def test_eviction_prefers_images_the_transcript_no_longer_references():
    store = ImageStore(max_entries=2)
    referenced = store.put(b'referenced', 'image/png')
    store.reconcile([referenced])
    unreferenced = store.put(b'unreferenced', 'image/png')

    store.put(b'latest', 'image/png')

    assert store.get(referenced) is not None
    assert store.get(unreferenced) is None


# ---------------------------------------------------------------------------
# The shared singleton and the placeholder
# ---------------------------------------------------------------------------


def test_module_functions_share_one_store():
    image_id = image_store.put(b'bytes', 'image/png')

    assert image_store.get_image_store() is image_store.get_image_store()
    assert image_store.get_image_store().get(image_id) == (b'bytes', 'image/png')


def test_placeholder_is_the_documented_text():
    assert IMAGE_UNAVAILABLE_PLACEHOLDER == '[screenshot — no longer available]'


def test_the_unsupported_placeholder_is_the_documented_text():
    assert (
        IMAGE_UNSUPPORTED_PLACEHOLDER
        == '[screenshot — this model cannot view images]'
    )
