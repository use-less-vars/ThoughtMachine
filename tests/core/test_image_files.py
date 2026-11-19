"""Tests for the durable pasted-image store (``agent/core/image_files.py``).

The durable layer is what makes a screenshot survive a reload, a session switch
and a process restart: one directory per workspace under the vault,
``workspaces/<workspace_id>/images/<image_id>.<ext>``, with the id minted
randomly at ingest (never a content hash -- see the module docstring).

Everything here runs against a throw-away vault root (``THOUGHTMACHINE_VAULT_ROOT``
pointed at ``tmp_path``), so no test reads or writes the real user vault.
"""

import base64
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_files, image_store
from agent.core.image_files import (
    IMAGE_ID_RE,
    MAX_WORKSPACE_IMAGE_BYTES,
    delete_image,
    image_path,
    load_image,
    media_type_of,
    mint_image_id,
    referenced_ids_for_workspace,
    referenced_ids_from_history,
    save_image,
    sweep_all_workspaces,
    sweep_workspace,
    workspace_image_bytes,
    workspace_images_dir,
)
from agent.core.image_store import ImageStore

# ── Fixtures / helpers ───────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload" * 4
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-payload" * 4
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"webp-payload" * 4
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'/>"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point the vault at a throw-away directory for the duration of a test."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def _clean_image_store():
    """Every test starts and ends with an empty in-memory cache."""
    image_store.reset()
    yield
    image_store.reset()


def _images_dir(vault: Path, workspace_id: str) -> Path:
    return vault / "workspaces" / workspace_id / "images"


def _sessions_dir(vault: Path, workspace_id: str) -> Path:
    return vault / "workspaces" / workspace_id / "sessions"


def _write_session(vault: Path, workspace_id: str, session_id: str, history: list) -> Path:
    """Write one session record the way the store lays it out on disk."""
    sessions_dir = _sessions_dir(vault, workspace_id)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}.json"
    path.write_text(
        json.dumps({"session_id": session_id, "user_history": history}),
        encoding="utf-8",
    )
    return path


def _age(path: Path, seconds: int) -> None:
    """Backdate *path*'s mtime by *seconds*, so a TTL sweep can see it."""
    old = time.time() - seconds
    os.utime(path, (old, old))


# ── 1. Round trip and on-disk contract ───────────────────────────────────────


def test_save_then_load_round_trips_the_bytes(vault):
    image_id = save_image("ws-a", "image/png", PNG)

    assert IMAGE_ID_RE.match(image_id)
    assert load_image("ws-a", image_id) == PNG
    assert media_type_of("ws-a", image_id) == "image/png"
    assert image_path("ws-a", image_id) == _images_dir(vault, "ws-a") / f"{image_id}.png"


def test_directory_and_file_modes_are_private(vault):
    image_id = save_image("ws-a", "image/png", PNG)

    images_dir = _images_dir(vault, "ws-a")
    assert stat.S_IMODE(os.stat(images_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(images_dir / f"{image_id}.png").st_mode) == 0o600


def test_write_is_atomic_and_leaves_no_temp_file(vault):
    image_id = save_image("ws-a", "image/png", PNG)
    names = sorted(entry.name for entry in _images_dir(vault, "ws-a").iterdir())

    assert names == [f"{image_id}.png"]


def test_ids_are_random_so_identical_bytes_do_not_collide(vault):
    first = save_image("ws-a", "image/png", PNG)
    second = save_image("ws-a", "image/png", PNG)

    assert first != second
    assert load_image("ws-a", first) == load_image("ws-a", second) == PNG


def test_load_delete_and_totals(vault):
    image_id = save_image("ws-a", "image/png", PNG)

    assert workspace_image_bytes("ws-a") == len(PNG)
    assert delete_image("ws-a", image_id) is True
    assert delete_image("ws-a", image_id) is False
    assert load_image("ws-a", image_id) is None
    assert workspace_image_bytes("ws-a") == 0


def test_missing_and_malformed_ids_are_a_miss_not_an_error(vault):
    assert image_path("ws-a", "no-such-id") is None
    assert load_image("ws-a", "a" * 32) is None
    assert media_type_of("ws-a", "a" * 32) is None
    assert delete_image("ws-a", "a" * 32) is False
    assert image_path("never-stored", mint_image_id()) is None


def test_minted_id_is_32_hex_characters():
    image_id = mint_image_id()

    assert len(image_id) == 32
    assert IMAGE_ID_RE.match(image_id)


# ── 2. Workspace isolation ───────────────────────────────────────────────────


def test_a_file_is_invisible_to_another_workspace(vault):
    image_id = save_image("ws-a", "image/png", PNG)

    assert load_image("ws-a", image_id) == PNG
    assert load_image("ws-b", image_id) is None
    assert image_path("ws-b", image_id) is None
    assert media_type_of("ws-b", image_id) is None
    assert delete_image("ws-b", image_id) is False
    # The owning copy is untouched by the other workspace's miss.
    assert load_image("ws-a", image_id) == PNG


def test_each_workspace_gets_its_own_directory(vault):
    a_id = save_image("ws-a", "image/png", PNG)
    b_id = save_image("ws-b", "image/png", PNG)

    assert a_id != b_id
    assert {entry.name for entry in _images_dir(vault, "ws-a").iterdir()} == {f"{a_id}.png"}
    assert {entry.name for entry in _images_dir(vault, "ws-b").iterdir()} == {f"{b_id}.png"}


# ── 3. Traversal and symlink escapes ────────────────────────────────────────


@pytest.mark.parametrize("bad_workspace_id", ["..", ".", "../ws-a", "ws/../..", "global", ""])
def test_an_unusable_workspace_id_cannot_be_turned_into_a_path(vault, bad_workspace_id):
    with pytest.raises(ValueError):
        workspace_images_dir(bad_workspace_id)
    with pytest.raises(ValueError):
        save_image(bad_workspace_id, "image/png", PNG)


@pytest.mark.parametrize("bad_workspace_id", ["..", "../ws-a", "global", ""])
def test_the_read_path_treats_an_unusable_workspace_id_as_a_miss(vault, bad_workspace_id):
    assert image_path(bad_workspace_id, mint_image_id()) is None
    assert load_image(bad_workspace_id, mint_image_id()) is None
    assert workspace_image_bytes(bad_workspace_id) == 0


@pytest.mark.parametrize(
    "bad_image_id",
    ["../../../etc/passwd", "../" + "a" * 32, "a" * 31, "A" * 32, "a" * 32 + "/../x"],
)
def test_a_malformed_image_id_never_names_a_file(vault, bad_image_id):
    save_image("ws-a", "image/png", PNG)

    assert image_path("ws-a", bad_image_id) is None
    assert load_image("ws-a", bad_image_id) is None


def test_a_symlink_pointing_out_of_the_store_is_refused(vault, caplog):
    (vault / "workspaces" / "ws-a").mkdir(parents=True)
    outside = vault / "workspaces" / "ws-a" / "outside.png"
    outside.write_bytes(b"not an image this store wrote")
    images_dir = workspace_images_dir("ws-a")
    image_id = mint_image_id()
    os.symlink(str(outside), str(images_dir / f"{image_id}.png"))

    with caplog.at_level(logging.WARNING, logger="agent.core.image_files"):
        assert image_path("ws-a", image_id) is None
        assert load_image("ws-a", image_id) is None

    assert any("outside the workspace image directory" in record.message
               for record in caplog.records)


def test_a_symlink_staying_inside_the_store_is_served(vault):
    image_id = save_image("ws-a", "image/png", PNG)
    original = _images_dir(vault, "ws-a") / f"{image_id}.png"
    alias = _images_dir(vault, "ws-a") / f"{mint_image_id()}.png"
    os.symlink(str(original), str(alias))

    assert load_image("ws-a", alias.stem) == PNG


# ── 4. Media-type whitelist ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "media_type,ext",
    [("image/png", ".png"), ("image/jpeg", ".jpg"), ("image/webp", ".webp")],
)
def test_whitelisted_types_map_to_a_fixed_extension(vault, media_type, ext):
    image_id = save_image("ws-a", media_type, PNG)

    assert image_path("ws-a", image_id) == _images_dir(vault, "ws-a") / f"{image_id}{ext}"
    assert media_type_of("ws-a", image_id) == media_type


@pytest.mark.parametrize("media_type", ["image/svg+xml", "image/gif", "text/plain", "", None])
def test_types_outside_the_whitelist_are_refused(vault, media_type):
    with pytest.raises(ValueError):
        save_image("ws-a", media_type, SVG)

    assert not _images_dir(vault, "ws-a").exists()


def test_the_media_type_comes_from_the_extension_not_from_a_stored_string(vault):
    image_id = save_image("ws-a", "image/jpeg", JPEG)
    path = image_path("ws-a", image_id)

    # A client cannot make a stored file claim a type its name does not have.
    assert path.suffix == ".jpg"
    assert media_type_of("ws-a", image_id) == "image/jpeg"


def test_non_bytes_payload_is_a_type_error(vault):
    with pytest.raises(TypeError):
        save_image("ws-a", "image/png", "not bytes")


# ── 5. Orphan sweep: TTL ────────────────────────────────────────────────────


def test_sweep_removes_expired_orphans_and_keeps_referenced_files(vault, caplog):
    referenced_id = save_image("ws-a", "image/png", PNG)
    orphan_id = save_image("ws-a", "image/png", PNG)
    _age(_images_dir(vault, "ws-a") / f"{orphan_id}.png", image_files.ORPHAN_TTL_SECONDS + 60)

    with caplog.at_level(logging.INFO, logger="agent.core.image_files"):
        result = sweep_workspace("ws-a", {referenced_id})

    assert result == {
        "scanned_files": 2,
        "scanned_bytes": 2 * len(PNG),
        "removed": 1,
        "freed_bytes": len(PNG),
    }
    assert load_image("ws-a", referenced_id) == PNG
    assert load_image("ws-a", orphan_id) is None


def test_sweep_keeps_a_young_orphan(vault):
    orphan_id = save_image("ws-a", "image/png", PNG)

    result = sweep_workspace("ws-a", set())

    assert result["removed"] == 0
    assert load_image("ws-a", orphan_id) == PNG


def test_sweep_collects_a_stale_temp_file(vault):
    images_dir = workspace_images_dir("ws-a")
    stale = images_dir / f"{mint_image_id()}.png.tmp"
    stale.write_bytes(b"partial")
    _age(stale, image_files.ORPHAN_TTL_SECONDS + 60)

    result = sweep_workspace("ws-a", set())

    assert result["removed"] == 1
    assert not stale.exists()


def test_sweep_logs_exactly_one_info_line_even_when_it_removes_nothing(vault, caplog):
    save_image("ws-a", "image/png", PNG)

    with caplog.at_level(logging.INFO, logger="agent.core.image_files"):
        sweep_workspace("ws-a", set())

    lines = [record for record in caplog.records
             if record.name == "agent.core.image_files"
             and record.getMessage().startswith("Image sweep")]
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO


def test_sweep_leaves_foreign_files_alone(vault):
    images_dir = workspace_images_dir("ws-a")
    foreign = images_dir / "notes.txt"
    foreign.write_text("not this store's file")
    _age(foreign, image_files.ORPHAN_TTL_SECONDS + 60)

    result = sweep_workspace("ws-a", set())

    assert result["scanned_files"] == 0
    assert foreign.exists()


# ── 6. Orphan sweep: byte budget ────────────────────────────────────────────


def test_sweep_trims_the_oldest_unreferenced_files_to_the_budget(vault, monkeypatch):
    monkeypatch.setattr(image_files, "MAX_WORKSPACE_IMAGE_BYTES", 4 * len(PNG))
    ids = []
    for step in range(6):
        image_id = save_image("ws-a", "image/png", PNG)
        _age(_images_dir(vault, "ws-a") / f"{image_id}.png", 3600 - step * 60)
        ids.append(image_id)

    # ttl_seconds=0 would collect everything, so keep the TTL out of it: the
    # budget pass is what must trim here.
    result = sweep_workspace("ws-a", set(), ttl_seconds=10 ** 9)

    assert result["removed"] == 2
    assert workspace_image_bytes("ws-a") <= 4 * len(PNG)
    assert load_image("ws-a", ids[0]) is None
    assert load_image("ws-a", ids[-1]) == PNG


def test_the_budget_never_deletes_a_referenced_file(vault, monkeypatch):
    monkeypatch.setattr(image_files, "MAX_WORKSPACE_IMAGE_BYTES", 1)
    referenced_id = save_image("ws-a", "image/png", PNG)
    orphan_id = save_image("ws-a", "image/png", PNG)
    _age(_images_dir(vault, "ws-a") / f"{orphan_id}.png", 1)

    result = sweep_workspace("ws-a", {referenced_id}, ttl_seconds=10 ** 9)

    assert result["removed"] == 1
    assert load_image("ws-a", referenced_id) == PNG
    assert load_image("ws-a", orphan_id) is None
    assert workspace_image_bytes("ws-a") > 1  # over budget, and that is allowed


def test_sweep_all_workspaces_only_touches_workspaces_that_stored_images(vault):
    a_id = save_image("ws-a", "image/png", PNG)
    (vault / "workspaces" / "ws-b").mkdir(parents=True)
    _age(_images_dir(vault, "ws-a") / f"{a_id}.png", image_files.ORPHAN_TTL_SECONDS + 60)

    result = sweep_all_workspaces({"ws-a": {a_id}, "ws-b": set()})

    assert set(result["workspaces"]) == {"ws-a"}
    assert result["removed"] == 0
    assert result["scanned_files"] == 1
    assert result["scanned_bytes"] == len(PNG)
    assert load_image("ws-a", a_id) == PNG
    assert not _images_dir(vault, "ws-b").exists()


def test_referenced_ids_from_history_reads_ids_and_metadata_dicts():
    history = [
        {"role": "user", "content": "hi", "images": [{"id": "a" * 32}]},
        {"role": "user", "content": "again", "images": ["b" * 32]},
        {"role": "assistant", "content": "no images"},
    ]

    assert referenced_ids_from_history(history) == {"a" * 32, "b" * 32}
    assert referenced_ids_from_history([]) == set()


# ── 7. Cache and durable layer stay independent ─────────────────────────────


def test_put_accepts_the_id_the_durable_layer_minted(vault):
    image_id = save_image("ws-a", "image/png", PNG)

    assert image_store.put(PNG, "image/png", image_id=image_id) == image_id
    assert image_store.get(image_id) == (PNG, "image/png")


def test_cache_eviction_never_unlinks_the_durable_file(vault):
    store = ImageStore(max_entries=1)
    first = save_image("ws-a", "image/png", PNG)
    second = save_image("ws-a", "image/jpeg", JPEG)
    store.put(PNG, "image/png", image_id=first)
    store.put(JPEG, "image/jpeg", image_id=second)

    assert store.get(first) is None  # the cache dropped it ...
    assert load_image("ws-a", first) == PNG  # ... the file survived


def test_put_rejects_an_empty_id(vault):
    with pytest.raises(ValueError):
        image_store.put(PNG, "image/png", image_id="")


# ── 8. Ingest writes exactly one durable file, named by the returned id ─────


def _data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")


def test_ingest_persists_one_file_per_image_named_by_the_returned_id(vault):
    from web_ui.backend.image_ingest import ingest_incoming_images

    metadata, error = ingest_incoming_images(
        [{"data_url": _data_url(PNG, "image/png"), "media_type": "image/png",
          "width": 1280, "height": 720}],
        workspace_id="ws-a",
    )

    assert error is None
    assert len(metadata) == 1
    image_id = metadata[0]["id"]
    files = sorted(entry.name for entry in _images_dir(vault, "ws-a").iterdir())
    assert files == [f"{image_id}.png"]
    assert Path(files[0]).stem == image_id
    assert load_image("ws-a", image_id) == PNG
    assert image_store.get(image_id) == (PNG, "image/png")


def test_ingest_without_a_workspace_caches_only(vault):
    from web_ui.backend.image_ingest import ingest_incoming_images

    metadata, error = ingest_incoming_images([_data_url(PNG, "image/png")])

    assert error is None
    assert image_store.get(metadata[0]["id"]) == (PNG, "image/png")
    assert not (vault / "workspaces" / "ws-a").exists()


def test_a_rejected_ingest_leaves_neither_a_file_nor_a_cache_entry(vault):
    from web_ui.backend.image_ingest import ingest_incoming_images

    metadata, error = ingest_incoming_images(
        [_data_url(PNG, "image/png"), _data_url(SVG, "image/svg+xml")],
        workspace_id="ws-a",
    )

    assert metadata == []
    assert error is not None
    assert list(_images_dir(vault, "ws-a").iterdir()) == []
    assert image_store.stats()["entries"] == 0


def test_max_workspace_image_bytes_is_a_sane_default():
    assert MAX_WORKSPACE_IMAGE_BYTES == 256 * 1024 * 1024
    assert image_files.ORPHAN_TTL_SECONDS == 24 * 3600


# ── 9. Persisted reference scan: one answer per workspace ──────────────────────────


def test_referenced_ids_for_workspace_unions_every_session_file(vault, caplog):
    first_id, second_id = "a" * 32, "b" * 32
    _write_session(vault, "ws-a", "s1", [
        {"role": "user", "content": "hi", "images": [{"id": first_id}]},
    ])
    _write_session(vault, "ws-a", "s2", [
        {"role": "user", "content": "again", "images": [second_id]},
        {"role": "assistant", "content": "no images"},
    ])
    # A metadata sidecar and an unreadable file: neither may contribute an id.
    (_sessions_dir(vault, "ws-a") / "_meta_s1.json").write_text(
        json.dumps({"user_history": [{"images": ["c" * 32]}]}), encoding="utf-8"
    )
    (_sessions_dir(vault, "ws-a") / "broken.json").write_text(
        "{ this is not json", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="agent.core.image_files"):
        assert referenced_ids_for_workspace("ws-a") == {first_id, second_id}

    assert any("Skipping unreadable session file" in record.message
               for record in caplog.records)


def test_referenced_ids_for_workspace_is_empty_when_nothing_is_stored(vault):
    assert referenced_ids_for_workspace("never-used") == set()
    # The scan is a read: it must not materialise a directory.
    assert not _sessions_dir(vault, "never-used").exists()


def test_referenced_ids_for_workspace_ignores_another_workspace(vault):
    own_id, foreign_id = "a" * 32, "b" * 32
    _write_session(vault, "ws-a", "s1", [{"role": "user", "images": [own_id]}])
    _write_session(vault, "ws-b", "s2", [{"role": "user", "images": [foreign_id]}])

    assert referenced_ids_for_workspace("ws-a") == {own_id}
    assert referenced_ids_for_workspace("ws-b") == {foreign_id}


@pytest.mark.parametrize("bad_workspace_id", ["..", ".", "../ws-a", "global", ""])
def test_the_reference_scan_treats_an_unusable_workspace_id_as_a_miss(vault, bad_workspace_id):
    with pytest.raises(ValueError):
        image_files.workspace_images_dir(bad_workspace_id)

    assert referenced_ids_for_workspace(bad_workspace_id) == set()


def test_a_session_without_a_history_contributes_nothing(vault):
    _write_session(vault, "ws-a", "s1", [])
    _write_session(vault, "ws-a", "s2", [{"role": "user", "content": "no images"}])

    assert referenced_ids_for_workspace("ws-a") == set()


def test_referenced_ids_for_workspace_is_exported():
    assert "referenced_ids_for_workspace" in image_files.__all__
    assert callable(getattr(image_files, "referenced_ids_for_workspace"))


# ── 10. Drift guard: the scan against the REAL session store ──────────────────────
#
# Section 9 writes the session files by hand, so it would keep passing if the
# store renamed its payload files or moved the history under a different key --
# while the scan silently stopped seeing real sessions.  These tests drive the
# real ``FileSystemSessionStore`` and the real ``Session`` model into the same
# throw-away vault the scan reads, so a layout drift fails here instead of in
# production, where the cost is image bytes collected out from under a live
# session.


def _real_store(vault: Path):
    """A real session store writing into the throw-away vault."""
    from session.store import FileSystemSessionStore

    return FileSystemSessionStore(
        sessions_dir=str(vault / "legacy-sessions"),
        state_dir=str(vault / "state"),
        enable_session_history_pruning=False,
    )


def _real_session(session_id: str, history: list, name: str = "Drift Guard"):
    """A real ``Session``, built the way the app builds one."""
    from session.models import Session

    return Session(session_id=session_id, user_history=history,
                   metadata={"name": name})


def _save_real_session(store, session_id: str, history: list) -> None:
    """Persist *history* as a real session of workspace ``ws-a``."""
    store.save_session(_real_session(session_id, history), workspace_id="ws-a")


def _write_raw_payload(vault: Path, workspace_id: str, filename: str, payload) -> Path:
    """Write an arbitrary JSON payload where a session file would live."""
    sessions_dir = _sessions_dir(vault, workspace_id)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_real_session_save_is_visible_to_the_scan(vault):
    image_id = "a" * 32
    store = _real_store(vault)
    _save_real_session(store, "s1", [
        {"role": "user", "content": "look", "images": [{"id": image_id}]},
    ])

    referenced, complete = image_files.scan_workspace_references("ws-a")

    assert referenced == {image_id}
    assert complete is True


def test_a_real_meta_sidecar_does_not_cost_the_scan_its_completeness(vault):
    image_id = "a" * 32
    store = _real_store(vault)
    _save_real_session(store, "s1", [{"role": "user", "images": [image_id]}])

    names = sorted(entry.name for entry in _sessions_dir(vault, "ws-a").iterdir())
    assert any(name.startswith("_meta_") for name in names), names

    assert image_files.scan_workspace_references("ws-a") == ({image_id}, True)


def test_deleting_a_real_session_removes_its_id_from_the_scan(vault):
    image_id = "a" * 32
    store = _real_store(vault)
    _save_real_session(store, "s1", [{"role": "user", "images": [{"id": image_id}]}])
    assert image_files.scan_workspace_references("ws-a") == ({image_id}, True)

    assert store.delete_session("s1", workspace_id="ws-a") is True

    assert image_files.scan_workspace_references("ws-a") == (set(), True)


def test_re_saving_an_edited_history_drops_only_the_dropped_id(vault):
    dropped_id, kept_id = "a" * 32, "b" * 32
    store = _real_store(vault)
    _save_real_session(store, "s1", [{"role": "user", "images": [{"id": dropped_id}]}])
    _save_real_session(store, "s2", [{"role": "user", "images": [{"id": kept_id}]}])
    assert image_files.scan_workspace_references("ws-a") == ({dropped_id, kept_id}, True)

    # The user removes the image from session 1, and the session is saved again.
    _save_real_session(store, "s1", [{"role": "user", "content": "gone"}])

    assert image_files.scan_workspace_references("ws-a") == ({kept_id}, True)


def test_one_unreadable_file_makes_a_real_scan_partial(vault, caplog):
    image_id = "a" * 32
    store = _real_store(vault)
    _save_real_session(store, "s1", [{"role": "user", "images": [{"id": image_id}]}])
    (_sessions_dir(vault, "ws-a") / "truncated.json").write_text(
        '{"session_id": "s2", "user_history": [', encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="agent.core.image_files"):
        referenced, complete = image_files.scan_workspace_references("ws-a")

    assert referenced == {image_id}
    assert complete is False
    assert any("Skipping unreadable session file" in record.message
               for record in caplog.records)


def test_a_missing_directory_is_a_complete_empty_answer(vault):
    referenced, complete = image_files.scan_workspace_references("never-used")

    assert referenced == set()
    assert complete is True
    # Reading is not writing: the scan must not materialise the directory.
    assert not _sessions_dir(vault, "never-used").exists()


@pytest.mark.parametrize("bad_workspace_id", ["..", ".", "../ws-a", "global", ""])
def test_an_unusable_workspace_id_is_not_complete(vault, bad_workspace_id):
    assert image_files.scan_workspace_references(bad_workspace_id) == (set(), False)


@pytest.mark.parametrize("payload", [
    [],                                        # a JSON array is not a session record
    {"session_id": "s1", "user_history": "not a list"},
    {"session_id": "s1", "user_history": 7},
])
def test_a_payload_the_scan_cannot_read_is_partial(vault, payload):
    _write_raw_payload(vault, "ws-a", "odd.json", payload)

    assert image_files.scan_workspace_references("ws-a") == (set(), False)


def test_a_payload_without_a_history_key_stays_complete(vault):
    image_id = "a" * 32
    _write_raw_payload(vault, "ws-a", "no-history.json", {"session_id": "s0"})
    _write_session(vault, "ws-a", "s2", [{"role": "user", "images": [image_id]}])

    assert image_files.scan_workspace_references("ws-a") == ({image_id}, True)


def test_the_wrapper_never_disagrees_with_the_tuple_form(vault):
    image_id = "a" * 32
    store = _real_store(vault)
    _save_real_session(store, "s1", [{"role": "user", "images": [{"id": image_id}]}])

    referenced, complete = image_files.scan_workspace_references("ws-a")
    assert complete is True
    assert referenced_ids_for_workspace("ws-a") == referenced == {image_id}

    # The wrapper drops the flag but must never invent or lose an id, on a
    # partial answer as much as on a complete one.
    _write_raw_payload(vault, "ws-a", "odd.json", {"session_id": "s2", "user_history": 3})
    partial, complete = image_files.scan_workspace_references("ws-a")

    assert complete is False
    assert partial == {image_id}
    assert referenced_ids_for_workspace("ws-a") == partial


def test_the_tuple_form_is_exported():
    assert "scan_workspace_references" in image_files.__all__
    assert callable(getattr(image_files, "scan_workspace_references"))

