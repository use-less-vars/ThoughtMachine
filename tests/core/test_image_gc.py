"""Tests for the per-session pasted-image collector (``agent/core/image_gc.py``).

``agent/core/image_store.py`` is process-wide and content-addressed, so one
session cannot reconcile it: the same screenshot pasted in two tabs is ONE
entry, and ``image_store.reconcile`` would blank the other tab's image.

These tests pin the bookkeeping that makes collection safe: a session releases
only the images IT put into the store, only once its persisted (post-prune,
post-cap) history stops referencing them, and never an id another live session
still tracks.

Everything is in-memory: no provider, no network, no real session files beyond
``tmp_path``.
"""

import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_files, image_gc, image_store


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts and ends with an empty store and no tracked sessions."""
    image_store.reset()
    image_gc.reset()
    yield
    image_gc.reset()
    image_store.reset()


def _stored(data: bytes) -> str:
    """Put *data* into the shared store and return its content-addressed id."""
    return image_store.put(data, 'image/png')


def _user_message(*ids):
    """A user message referencing *ids* -- the shape the transcript persists."""
    return {'role': 'user', 'content': 'what is wrong here?', 'images': list(ids)}


PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload" * 4

WORKSPACE = 'ws-close'


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point the vault at a throw-away directory for the duration of a test.

    ``image_files`` resolves the vault root itself, so a test that lets
    ``drop_session`` reach the reference scan MUST take this fixture -- the
    default root is the real user vault.
    """
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


def _write_session(vault: Path, workspace_id: str, session_id: str, history: list) -> Path:
    """Write one session record the way the store lays it out on disk."""
    sessions_dir = vault / "workspaces" / workspace_id / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}.json"
    path.write_text(
        json.dumps({"session_id": session_id, "user_history": history}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# tracking
# ---------------------------------------------------------------------------


def test_tracked_ids_starts_empty():
    assert image_gc.tracked_ids('sess-a') == set()


def test_track_records_the_ids_the_session_put_in():
    first = _stored(b'first')
    second = _stored(b'second')

    image_gc.track_images('sess-a', [first, second])

    assert image_gc.tracked_ids('sess-a') == {first, second}


def test_track_accepts_metadata_dicts_and_legacy_keys():
    canonical = _stored(b'canonical')
    legacy = _stored(b'legacy')

    image_gc.track_images(
        'sess-a',
        [{'id': canonical, 'media_type': 'image/png', 'width': 4, 'height': 4},
         {'image_id': legacy}],
    )

    assert image_gc.tracked_ids('sess-a') == {canonical, legacy}


def test_track_ignores_entries_without_an_id():
    kept = _stored(b'kept')

    image_gc.track_images('sess-a', [None, 42, {}, {'id': 7}, kept])

    assert image_gc.tracked_ids('sess-a') == {kept}


def test_track_is_idempotent():
    image_id = _stored(b'once')

    image_gc.track_images('sess-a', [image_id])
    image_gc.track_images('sess-a', [image_id])

    assert image_gc.tracked_ids('sess-a') == {image_id}


def test_track_without_a_session_id_is_a_noop():
    image_id = _stored(b'orphan')

    image_gc.track_images(None, [image_id])
    image_gc.track_images('', [image_id])

    assert image_gc.tracked_ids(None) == set()
    assert image_gc.tracked_ids('') == set()


def test_track_without_images_is_a_noop():
    image_gc.track_images('sess-a', None)
    image_gc.track_images('sess-a', [])

    assert image_gc.tracked_ids('sess-a') == set()


# ---------------------------------------------------------------------------
# live ids in a transcript
# ---------------------------------------------------------------------------


def test_live_image_ids_walks_every_message():
    first = _stored(b'live-one')
    second = _stored(b'live-two')

    live = image_gc.live_image_ids([
        _user_message(first),
        {'role': 'assistant', 'content': 'no image here'},
        _user_message({'id': second}),
    ])

    assert live == {first, second}


def test_live_image_ids_tolerates_odd_history_entries():
    image_id = _stored(b'odd')

    live = image_gc.live_image_ids([
        'not a message',
        None,
        {'role': 'user', 'content': 'images is not a list', 'images': image_id},
        {'role': 'user', 'content': 'junk entries', 'images': [None, 5, image_id]},
    ])

    assert live == {image_id}


def test_live_image_ids_of_empty_history_is_empty():
    assert image_gc.live_image_ids([]) == set()
    assert image_gc.live_image_ids(None) == set()


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


def test_gc_releases_images_the_persisted_history_dropped():
    kept = _stored(b'kept')
    dropped = _stored(b'dropped')
    image_gc.track_images('sess-a', [kept, dropped])

    released = image_gc.gc_session('sess-a', [_user_message(kept)])

    assert released == 1
    assert image_store.get(dropped) is None
    assert image_store.get(kept) is not None
    assert image_gc.tracked_ids('sess-a') == {kept}


def test_gc_keeps_everything_the_history_still_references():
    image_id = _stored(b'still-there')
    image_gc.track_images('sess-a', [image_id])

    released = image_gc.gc_session('sess-a', [_user_message(image_id)])

    assert released == 0
    assert image_store.get(image_id) is not None
    assert image_gc.tracked_ids('sess-a') == {image_id}


def test_gc_of_an_untracked_session_never_touches_the_store():
    image_id = _stored(b'not-mine')

    released = image_gc.gc_session('sess-other', [])

    assert released == 0
    assert image_store.get(image_id) is not None
    assert image_gc.tracked_ids('sess-other') == set()


def test_gc_without_a_session_id_is_a_noop():
    image_id = _stored(b'anonymous')

    assert image_gc.gc_session(None, []) == 0
    assert image_store.get(image_id) is not None


def test_gc_cross_session_dedupe_protects_a_shared_image():
    """The same screenshot in two tabs is ONE entry: the first collector yields."""
    shared = _stored(b'same-screenshot')
    image_gc.track_images('sess-a', [shared])
    image_gc.track_images('sess-b', [shared])

    assert image_gc.gc_session('sess-a', []) == 0
    assert image_store.get(shared) is not None
    assert image_gc.tracked_ids('sess-a') == set()

    # Only once the last session that tracked it drops it does it go.
    assert image_gc.gc_session('sess-b', []) == 1
    assert image_store.get(shared) is None


def test_gc_counts_only_entries_actually_removed():
    """An id the store already evicted is collected from tracking, not counted."""
    image_gc.track_images('sess-a', ['0123456789abcdef'])

    assert image_gc.gc_session('sess-a', []) == 0
    assert image_gc.tracked_ids('sess-a') == set()


def test_gc_stops_tracking_a_session_that_references_nothing():
    image_id = _stored(b'gone')
    image_gc.track_images('sess-a', [image_id])

    image_gc.gc_session('sess-a', [])

    # Nothing left to collect on the next save.
    assert image_gc.gc_session('sess-a', []) == 0


def test_gc_is_a_fixpoint_for_a_history_that_keeps_its_images():
    image_id = _stored(b'stable')
    image_gc.track_images('sess-a', [image_id])
    history = [_user_message(image_id)]

    for _ in range(3):
        assert image_gc.gc_session('sess-a', history) == 0

    assert image_store.get(image_id) is not None


# ---------------------------------------------------------------------------
# session teardown
# ---------------------------------------------------------------------------


def test_drop_session_forgets_tracking_without_releasing_the_bytes():
    image_id = _stored(b'for-saved-transcript')
    image_gc.track_images('sess-a', [image_id])

    image_gc.drop_session('sess-a')

    assert image_gc.tracked_ids('sess-a') == set()
    # The saved transcript still names the id, so this process must still resolve it.
    assert image_store.get(image_id) is not None


def test_drop_session_tolerates_unknown_and_missing_ids():
    image_gc.drop_session('never-tracked')
    image_gc.drop_session(None)
    image_gc.drop_session('')

    assert image_gc.tracked_ids('never-tracked') == set()


def test_drop_session_does_not_unprotect_another_sessions_image():
    shared = _stored(b'shared')
    image_gc.track_images('sess-a', [shared])
    image_gc.track_images('sess-b', [shared])

    image_gc.drop_session('sess-a')

    assert image_gc.gc_session('sess-b', []) == 1
    assert image_store.get(shared) is None


def test_reset_forgets_every_session():
    image_gc.track_images('sess-a', [_stored(b'a')])
    image_gc.track_images('sess-b', [_stored(b'b')])

    image_gc.reset()

    assert image_gc.tracked_ids('sess-a') == set()
    assert image_gc.tracked_ids('sess-b') == set()


# ---------------------------------------------------------------------------
# session close: byte reclamation (``drop_session`` -> ``_unlink_over``)
#
# The in-memory tracking is gone once a session closes, so the only authority
# left is what the workspace's persisted transcripts say.  Deletion is
# therefore conservative in both directions: an unknown workspace deletes
# nothing, and a scan that could not read every transcript deletes nothing.
# ---------------------------------------------------------------------------


def test_close_reclaims_an_image_no_persisted_transcript_references(vault):
    """An unreferenced file of a closing session is unlinked; the count is honest."""
    orphan = image_files.save_image(WORKSPACE, "image/png", PNG)
    image_gc.track_images('sess-a', [orphan], workspace_id=WORKSPACE)

    image_gc.drop_session('sess-a', workspace_id=WORKSPACE)

    assert image_files.load_image(WORKSPACE, orphan) is None
    assert image_gc.tracked_ids('sess-a') == set()

    # Only files that actually existed are counted.
    second = image_files.save_image(WORKSPACE, "image/png", PNG)
    assert image_gc._unlink_over(WORKSPACE, [second]) == 1
    assert image_gc._unlink_over(WORKSPACE, [second]) == 0


def test_close_without_a_workspace_keeps_the_bytes(vault):
    """Backward compatibility: no workspace known, explicit or stored, no unlink."""
    implicit = image_files.save_image(WORKSPACE, "image/png", PNG)
    explicit_none = image_files.save_image(WORKSPACE, "image/png", PNG)
    image_gc.track_images('sess-omitted', [implicit])
    image_gc.track_images('sess-explicit', [explicit_none])

    image_gc.drop_session('sess-omitted')  # argument omitted entirely
    image_gc.drop_session('sess-explicit', workspace_id=None)

    assert image_gc.tracked_ids('sess-omitted') == set()
    assert image_gc.tracked_ids('sess-explicit') == set()
    assert image_files.load_image(WORKSPACE, implicit) == PNG
    assert image_files.load_image(WORKSPACE, explicit_none) == PNG


def test_close_keeps_an_image_a_persisted_transcript_still_references(vault):
    kept = image_files.save_image(WORKSPACE, "image/png", PNG)
    _write_session(vault, WORKSPACE, 'sess-other', [_user_message(kept)])
    image_gc.track_images('sess-a', [kept], workspace_id=WORKSPACE)

    image_gc.drop_session('sess-a', workspace_id=WORKSPACE)

    assert image_files.load_image(WORKSPACE, kept) == PNG
    assert image_gc.tracked_ids('sess-a') == set()


def test_close_keeps_everything_when_a_transcript_cannot_be_read(vault, caplog):
    """An incomplete scan may UNDER-count, so it keeps all candidates."""
    orphan = image_files.save_image(WORKSPACE, "image/png", PNG)
    broken = vault / "workspaces" / WORKSPACE / "sessions" / "broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")
    image_gc.track_images('sess-a', [orphan], workspace_id=WORKSPACE)

    with caplog.at_level(logging.WARNING, logger="agent.core.image_gc"):
        image_gc.drop_session('sess-a', workspace_id=WORKSPACE)

    assert image_files.load_image(WORKSPACE, orphan) == PNG
    assert any(
        record.levelno == logging.WARNING and 'could not be read' in record.getMessage()
        for record in caplog.records
    )


def test_gc_session_never_unlinks_a_durable_file(vault):
    """gc_session runs on EVERY turn: it collects the store, never disk."""
    dropped = image_files.save_image(WORKSPACE, "image/png", PNG)
    image_gc.track_images('sess-a', [dropped], workspace_id=WORKSPACE)

    released = image_gc.gc_session('sess-a', [])

    assert released == 0  # never in the in-memory store, so nothing to release
    assert image_gc.tracked_ids('sess-a') == set()
    assert image_files.load_image(WORKSPACE, dropped) == PNG


# ---------------------------------------------------------------------------
# hook: FileSystemSessionStore.save_session collects against the SAVED history
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """A session store rooted in tmp_path (no vault, no pruning)."""
    from session.store import FileSystemSessionStore

    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / 'sessions'),
        state_dir=str(tmp_path / 'state'),
        enable_session_history_pruning=False,
    )


@pytest.fixture
def session():
    from session.models import Session

    return Session()


def test_save_session_keeps_images_the_saved_history_references(store, session):
    image_id = _stored(b'pasted-into-turn')
    session.add_message('user', 'why does this fail?', images=[image_id])
    image_gc.track_images(session.session_id, [image_id])

    store.save_session(session)

    assert image_store.get(image_id) is not None
    assert image_gc.tracked_ids(session.session_id) == {image_id}


def test_save_session_releases_images_the_saved_history_no_longer_has(store, session):
    image_id = _stored(b'pruned-away')
    session.add_message('user', 'why does this fail?', images=[image_id])
    image_gc.track_images(session.session_id, [image_id])
    store.save_session(session)
    assert image_store.get(image_id) is not None

    # The pruner/cap rewrote the turn without its image; the next save collects it.
    session.user_history[0]['images'] = []
    store.save_session(session)

    assert image_store.get(image_id) is None
    assert image_gc.tracked_ids(session.session_id) == set()


def test_save_session_refuses_to_release_another_sessions_image(store):
    from session.models import Session

    shared = _stored(b'pasted-in-both-tabs')
    owner = Session()
    owner.add_message('user', 'mine', images=[shared])
    other = Session()
    other.add_message('user', 'mine too', images=[shared])
    image_gc.track_images(owner.session_id, [shared])
    image_gc.track_images(other.session_id, [shared])

    # The owner's copy loses the image first; the other tab still resolves it.
    owner.user_history[0]['images'] = []
    store.save_session(owner)

    assert image_store.get(shared) is not None

    # And when the other tab lets go too, the entry finally goes.
    other.user_history[0]['images'] = []
    store.save_session(other)

    assert image_store.get(shared) is None


# ---------------------------------------------------------------------------
# restart survival: the store is in-process, the bytes are on disk
# ---------------------------------------------------------------------------

def test_warm_images_restores_bytes_a_restart_lost(vault):
    """After a restart the transcript's image must still reach the model."""
    image_id = image_files.save_image(WORKSPACE, 'image/png', PNG)
    image_store.reset()  # the restart: nothing in memory, bytes still on disk

    warmed = image_gc.warm_images(WORKSPACE, [_user_message(image_id)])

    assert warmed == 1
    stored = image_store.get(image_id)
    assert stored is not None
    assert stored[0] == PNG
    assert stored[1] == 'image/png'
    # Already cached now, so a second turn warms nothing.
    assert image_gc.warm_images(WORKSPACE, [_user_message(image_id)]) == 0


def test_warm_images_leaves_an_absent_image_a_miss(vault):
    """Bytes that are gone stay gone: warming never invents a hit."""
    missing = 'a' * 32
    image_store.reset()

    assert image_gc.warm_images(WORKSPACE, [_user_message(missing)]) == 0
    assert image_store.get(missing) is None


def test_warm_images_warms_the_newest_images_within_budget(vault):
    """The store is bounded, so the newest turns get the budget."""
    ids = [
        image_files.save_image(WORKSPACE, 'image/png', PNG + bytes([index]))
        for index in range(3)
    ]
    image_store.reset()

    warmed = image_gc.warm_images(WORKSPACE, [_user_message(*ids)], budget=2)

    assert warmed == 2
    assert image_store.get(ids[2]) is not None
    assert image_store.get(ids[1]) is not None
    assert image_store.get(ids[0]) is None


def test_warm_images_is_a_noop_without_a_workspace_or_images(vault):
    """An unknown workspace is never guessed at, and nothing is warmed for it."""
    image_id = image_files.save_image(WORKSPACE, 'image/png', PNG)
    image_store.reset()

    assert image_gc.warm_images(None, [_user_message(image_id)]) == 0
    assert image_gc.warm_images('', [_user_message(image_id)]) == 0
    assert image_gc.warm_images(WORKSPACE, []) == 0
    assert image_gc.warm_images(WORKSPACE, [{'role': 'user', 'content': 'no images'}]) == 0
    assert image_store.get(image_id) is None


def test_llm_client_warming_is_a_noop_without_a_session(monkeypatch):
    """A client that cannot name a workspace must not reach for one."""
    from agent.core.llm_client import LLMClient

    calls = []
    monkeypatch.setattr(image_gc, 'warm_images', lambda *args, **kwargs: calls.append(args))

    client = LLMClient.__new__(LLMClient)
    client.session = None
    client._warm_pasted_images([_user_message('a' * 32)])

    assert calls == []


def test_warm_images_leaves_an_image_of_unknown_type_a_miss(vault, monkeypatch, caplog):
    """A file that cannot name its type is not warmed under a guessed one."""
    image_id = image_files.save_image(WORKSPACE, 'image/png', PNG)
    image_store.reset()  # the restart: only the durable copy is left
    # The durable file exists and is readable; what is missing is the answer to
    # "what does it hold?".  ``media_type_of`` recovers that from the file name
    # alone, so ``None`` IS the unknown-extension case -- simulated here because
    # ``image_path`` only ever resolves whitelisted extensions, which is also
    # why warming must never invent one.
    path = image_files.image_path(WORKSPACE, image_id)
    assert path is not None and path.is_file()
    monkeypatch.setattr(image_files, 'media_type_of', lambda *_: None)

    with caplog.at_level(logging.WARNING, logger='agent.core.image_gc'):
        assert image_gc.warm_images(WORKSPACE, [_user_message(image_id)]) == 0

    assert image_store.get(image_id) is None
    assert any(
        record.levelno == logging.WARNING
        and image_id in record.getMessage()
        and 'media type' in record.getMessage()
        and record.exc_info is None
        for record in caplog.records
    )

