"""Durable, workspace-scoped storage for pasted-image bytes.

Layered with :mod:`agent.core.image_store`, this is the *durable* half:

* ``agent.core.image_store`` -- an in-process LRU **cache** of decoded bytes,
  bounded by ``MAX_ENTRIES`` / ``MAX_BYTES``, so serving a turn never touches
  the disk.
* ``agent.core.image_files`` (this module) -- the **durable** copy on disk,
  one directory per workspace::

      <vault root>/workspaces/<workspace_id>/images/<image_id>.<ext>

A screenshot therefore survives a reload, a session switch and a process
restart, while the transcript keeps carrying only a short id.

Why the id is RANDOM, never a content hash
------------------------------------------
The previous generation of this store minted ``sha256(data)[:16]`` as the id.
That made the id a *global* name for a byte string: the same screenshot pasted
in two different workspaces produced one id and one stored copy, so any holder
of that id could resolve bytes pasted in another workspace.  Cross-workspace
content dedupe was a leak, by construction -- it is deliberately forfeited
here.  Ids are minted with :func:`mint_image_id` (``secrets.token_hex(16)``,
32 lowercase hex characters) once per ingest, so identical bytes in two
workspaces yield two files and two unguessable ids.  Never derive the id from
the payload again, and never add a content hash to the public id.

Why isolation is STRUCTURAL
---------------------------
Every public function takes ``workspace_id`` as its first argument and resolves
the image directory *from it*; there is no id -> workspace index anywhere, and
none may be added.  A file is only reachable by a caller that already knows the
owning workspace, so a wrong workspace is simply a miss (``None``) rather than
a permission decision that has to be remembered.  Nothing here can serve a
foreign workspace's bytes because nothing here can *find* them.

On-disk contract
----------------
* the directory is created ``0o700`` (with an explicit ``chmod``: the vault
  only chmods its top compartments, so deeper directories would otherwise
  inherit the umask and be world-readable);
* files are ``0o600`` and written atomically -- ``<id>.<ext>.tmp`` in the same
  directory, ``chmod``, ``fsync``, then ``os.replace`` onto the final name, so a
  partial file is never visible under a real id;
* the media type is NEVER taken from the client: the extension comes from
  :data:`EXT_BY_MEDIA_TYPE` (png/jpeg/webp only -- no gif, no svg) and the media
  type is recovered from the extension on the way back out, so a stored file
  cannot claim a type its name does not have.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple

from agent.core.image_gc import live_image_ids
from thoughtmachine.vault import vault_root
from thoughtmachine.workspace_capabilities import _validate_workspace_id

__all__ = [
    "EXT_BY_MEDIA_TYPE",
    "IMAGE_ID_RE",
    "MAX_WORKSPACE_IMAGE_BYTES",
    "MEDIA_TYPE_BY_EXT",
    "ORPHAN_TTL_SECONDS",
    "delete_image",
    "image_path",
    "load_image",
    "media_type_of",
    "mint_image_id",
    "referenced_ids_for_workspace",
    "referenced_ids_from_history",
    "save_image",
    "scan_workspace_references",
    "sweep_all_workspaces",
    "sweep_workspace",
    "workspace_image_bytes",
    "workspace_images_dir",
]

logger = logging.getLogger(__name__)

#: Per-workspace byte budget for stored image files (256 MiB).  A sweep trims
#: the workspace back under this, oldest-unreferenced-first.
MAX_WORKSPACE_IMAGE_BYTES = 256 * 1024 * 1024

#: Age at which a file nothing references becomes an orphan a sweep may drop
#: (24 hours).  The window covers a reload/restart that has not re-saved yet.
ORPHAN_TTL_SECONDS = 24 * 3600

#: Extension -> media type.  This is the server-side whitelist: png, jpeg and
#: webp only.  ``image/gif`` and ``image/svg+xml`` are deliberately absent --
#: no supported provider consumes them inline.
MEDIA_TYPE_BY_EXT: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
}

#: Inverse map: an accepted input media type -> the extension it is stored as.
EXT_BY_MEDIA_TYPE: Dict[str, str] = {v: k for k, v in MEDIA_TYPE_BY_EXT.items()}

#: A durable image id: 32 lowercase hex characters (``secrets.token_hex(16)``).
#: The 16-character content hash of the previous generation does NOT match, so
#: an old transcript id can never name a file.
IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Suffix of an in-flight atomic write.  Never a final name.
_TMP_SUFFIX = ".tmp"

#: A final file name: ``<32 hex id><one of the whitelisted extensions>``.
_FILENAME_RE = re.compile(
    r"^(?P<id>[0-9a-f]{32})(?P<ext>" + "|".join(
        re.escape(ext) for ext in sorted(MEDIA_TYPE_BY_EXT)
    ) + r")$"
)


def _path_is_within(path: str, prefix: str) -> bool:
    """True when *path* equals *prefix* or lies beneath it (component-aware).

    Semantics copied verbatim from the two canonical copies of this test --
    ``web_ui/backend/server.py:4074-4078`` and
    ``web_ui/backend/workspace_routes.py:83-87`` -- because ``agent.core`` may
    not import ``web_ui.backend`` (agent code is imported by headless processes
    and by ``web_ui`` itself; pulling FastAPI in the other direction would
    invert the layering).  A bare ``startswith`` would accept ``/a/images-evil``
    for a prefix of ``/a/images``; the ``os.sep``-suffixed test does not.
    """
    path = os.path.normpath(path)
    prefix = os.path.normpath(prefix)
    return path == prefix or path.startswith(prefix + os.sep)


def _images_dir_path(workspace_id: str) -> Path:
    """Return the image directory path for *workspace_id* WITHOUT creating it.

    Read paths and the sweeps use this: a read must not materialise a
    directory, and a sweep must not create image directories inside workspaces
    that never stored an image.

    Raises:
        ValueError: If *workspace_id* is not a single, non-reserved segment.
    """
    return vault_root() / "workspaces" / _validate_workspace_id(workspace_id) / "images"


def _sessions_dir_path(workspace_id: str) -> Path:
    """Return the session directory of *workspace_id* WITHOUT creating it.

    Only :func:`scan_workspace_references` reads this: a reference scan must
    not materialise a directory inside a workspace that was never used.

    Raises:
        ValueError: If *workspace_id* is not a single, non-reserved segment.
    """
    return vault_root() / "workspaces" / _validate_workspace_id(workspace_id) / "sessions"


def workspace_images_dir(workspace_id: str) -> Path:
    """Return the image directory for *workspace_id*, creating it ``0o700``.

    Args:
        workspace_id: The owning workspace (one path segment; validated by the
            same authority every other workspace path uses).

    Returns:
        ``<vault root>/workspaces/<workspace_id>/images``.

    Raises:
        ValueError: If *workspace_id* is not a single, non-reserved segment.
    """
    images_dir = _images_dir_path(workspace_id)
    if not images_dir.exists():
        images_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        # mkdir's mode is masked by the process umask, so tighten explicitly.
        # Only on creation: an existing directory is left exactly as it is.
        os.chmod(images_dir, 0o700)
    return images_dir


def mint_image_id() -> str:
    """Return a fresh opaque image id (32 lowercase hex characters).

    Random, not content-derived -- see the module docstring for why.
    """
    return secrets.token_hex(16)


def _valid_image_id(image_id: Any) -> Optional[str]:
    """Return *image_id* when it is a well-formed durable id, else ``None``.

    Never raises: a bad id is a miss, not an error.
    """
    if not isinstance(image_id, str):
        return None
    if not IMAGE_ID_RE.match(image_id):
        return None
    return image_id


def _candidate_paths(images_dir: Path, image_id: str) -> Iterable[Path]:
    """Yield ``<images_dir>/<image_id><ext>`` for every whitelisted extension."""
    for ext in MEDIA_TYPE_BY_EXT:
        yield images_dir / f"{image_id}{ext}"


def _resolved_inside(images_dir: Path, candidate: Path) -> Optional[Path]:
    """Return *candidate*'s real path when it stays inside *images_dir*.

    ``os.path.realpath`` follows every symlink, so a link pointing out of the
    directory resolves outside and is refused (``None``).  The containment test
    is component-aware, matching the project's existing guards.
    """
    try:
        real = os.path.realpath(str(candidate))
        root = os.path.realpath(str(images_dir))
    except OSError:  # pragma: no cover - realpath does not raise on POSIX
        return None
    if not _path_is_within(real, root):
        logger.warning(
            "Refusing image path that resolves outside the workspace image "
            "directory: %s -> %s", candidate, real,
        )
        return None
    return Path(real)


def save_image(workspace_id: str, media_type: str, data: bytes) -> str:
    """Write image bytes to the workspace's durable store; return the new id.

    The file is written atomically: ``<id>.<ext>.tmp`` in the same directory,
    ``chmod 0o600``, ``fsync``, then ``os.replace`` onto ``<id>.<ext>``.  A
    partial file is therefore never visible under a real id, and a crash can
    leave at most a stale ``.tmp`` (which :func:`sweep_workspace` collects).

    Args:
        workspace_id: The owning workspace; also the isolation boundary.
        media_type: One of ``image/png``, ``image/jpeg``, ``image/webp``.  The
            extension is derived from it -- a client-supplied extension is
            never trusted, and anything outside the whitelist is refused.
        data: Raw image bytes.

    Returns:
        The minted image id (32 hex characters).

    Raises:
        ValueError: If *media_type* is not whitelisted, or *workspace_id* is
            not a usable path segment.
        TypeError: If *data* is not bytes-like.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("image data must be bytes")
    payload = bytes(data)
    ext = EXT_BY_MEDIA_TYPE.get(media_type)
    if ext is None:
        raise ValueError(
            f"unsupported image media type {media_type!r} "
            f"(allowed: {', '.join(sorted(EXT_BY_MEDIA_TYPE))})"
        )

    image_id = mint_image_id()
    images_dir = workspace_images_dir(workspace_id)
    final_path = images_dir / f"{image_id}{ext}"
    tmp_path = images_dir / f"{image_id}{ext}{_TMP_SUFFIX}"

    try:
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, final_path)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return image_id


def image_path(workspace_id: str, image_id: str) -> Optional[Path]:
    """Return the file holding *image_id* in *workspace_id*, or ``None``.

    ``None`` covers every miss: a malformed id, a well-formed id that names no
    file, an id belonging to another workspace, and a path -- symlink included
    -- that resolves outside the workspace's image directory.

    Args:
        workspace_id: The owning workspace.  An unusable workspace id is also a
            miss (``None``), never an exception: this is the read path.
        image_id: The durable image id.
    """
    valid = _valid_image_id(image_id)
    if valid is None:
        return None
    try:
        images_dir = _images_dir_path(workspace_id)
    except ValueError:
        return None
    if not images_dir.is_dir():
        return None
    for candidate in _candidate_paths(images_dir, valid):
        if not candidate.is_file():
            continue
        resolved = _resolved_inside(images_dir, candidate)
        if resolved is not None:
            return resolved
    return None


def load_image(workspace_id: str, image_id: str) -> Optional[bytes]:
    """Return the bytes stored under *image_id* in *workspace_id*, or ``None``."""
    path = image_path(workspace_id, image_id)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read stored image %s in workspace %r: %s",
                       image_id, workspace_id, exc)
        return None


def delete_image(workspace_id: str, image_id: str) -> bool:
    """Delete the stored bytes for *image_id*; return whether a file was removed.

    Used when the write that produced the image is rolled back.  It never
    raises: an absent file, a bad id or an unreadable path all report ``False``.
    """
    path = image_path(workspace_id, image_id)
    if path is None:
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def media_type_of(workspace_id: str, image_id: str) -> Optional[str]:
    """Return the media type of a stored image, derived from its extension.

    Recovered from the file name against :data:`MEDIA_TYPE_BY_EXT` -- never
    from a stored client string, which is why the existing file cannot lie
    about its type.
    """
    path = image_path(workspace_id, image_id)
    if path is None:
        return None
    return MEDIA_TYPE_BY_EXT.get(path.suffix.lower())


def scan_workspace_references(workspace_id: str) -> Tuple[Set[str], bool]:
    """Return the ids the persisted histories mention, plus whether that is all.

    The union over the workspace's session files, so a sweep can be handed one
    plainly correct answer per workspace instead of joining histories itself.
    Files are read straight from the store's on-disk layout
    (``<vault root>/workspaces/<workspace_id>/sessions/*.json``) and the
    persisted history is taken from the ``user_history`` key that
    ``Session.to_persistable_dict`` writes.

    Deliberately NOT read through ``FileSystemSessionStore.load_session``: that
    path pydantic-parses a whole session and can *write* (legacy-workspace
    migration), which a reference scan must never do.  ``_meta_*`` sidecars
    carry no history and are skipped.

    Reading the layout directly is what makes the second element necessary.
    This scan can be handed a file it cannot parse -- a payload caught
    mid-write, a truncated/legacy file, a ``user_history`` that is not a list
    -- and the honest answer for such a workspace is not "these ids and
    nothing else", it is "these ids, and I could not see everything".  A
    caller that DELETES must therefore branch on the flag:

    * ``complete=True`` -- every session file was read, so the set is the whole
      truth and an id outside it is unreferenced.
    * ``complete=False`` -- at least one file was unreadable or carried an
      unusable ``user_history``, so the set may be an UNDER-count.  It still
      names every id the scan did see, but an unlisted id must NOT be treated
      as dead: skip the pruning (or defer it) rather than act on a partial
      view.

    A workspace with no sessions directory at all is ``complete=True``: nothing
    is stored, so the empty set is complete.  An empty set with
    ``complete=False`` only ever means "cannot vouch for this workspace".

    Never raises: an unusable workspace id, a missing directory and an
    unreadable session file are all a miss, and the flag carries the doubt.
    Nothing here may invent an id it did not read.

    Args:
        workspace_id: The workspace whose persisted histories to scan.

    Returns:
        ``(referenced_ids, complete)``.
    """
    try:
        sessions_dir = _sessions_dir_path(workspace_id)
    except ValueError:
        return set(), False
    try:
        paths = sorted(sessions_dir.glob("*.json"))
    except OSError:
        return set(), False
    referenced: Set[str] = set()
    complete = True
    for path in paths:
        if path.name.startswith("_meta_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable session file %s: %s", path, exc)
            complete = False
            continue
        if not isinstance(data, dict):
            complete = False
            continue
        history = data.get("user_history")
        if history is None:
            continue
        if isinstance(history, list):
            referenced |= referenced_ids_from_history(history)
        else:
            # A payload whose history is not a list cannot be honest about the
            # ids it holds, so the answer is partial even though it is a miss.
            complete = False
    return referenced, complete


def referenced_ids_for_workspace(workspace_id: str) -> Set[str]:
    """Return every image id the persisted histories of *workspace_id* mention.

    Thin wrapper over :func:`scan_workspace_references` that drops the
    completeness flag -- see that function for the scan itself.

    Kept for callers whose use is purely additive (reporting, accounting,
    "does this workspace reference anything at all?").  Anything that DELETES
    image files -- notably a sweep -- must call
    :func:`scan_workspace_references` instead: a bare ``Set`` cannot express
    "one of the session files could not be read", and acting on that partial
    set would drop bytes a live session still references.

    Never raises (same guarantees as the tuple form).

    Args:
        workspace_id: The workspace whose persisted histories to scan.
    """
    referenced, _complete = scan_workspace_references(workspace_id)
    return referenced


def referenced_ids_from_history(history: list) -> Set[str]:
    """Return every image id still referenced by a persisted history.

    The walk itself lives in :func:`agent.core.image_gc.live_image_ids` (the
    same authority the in-memory collector uses), so a persisted ``images``
    list is read one way in this codebase, not two.

    Args:
        history: The session's message list as persisted (dicts, each possibly
            carrying ``images`` as ids or ``{"id": ...}`` metadata dicts).
    """
    if not history:
        return set()
    return live_image_ids(history)


def workspace_image_bytes(workspace_id: str) -> int:
    """Return the total size in bytes of *workspace_id*'s stored image files.

    Counts only files whose names match the store's own shape
    (``<32 hex id><whitelisted ext>``), so a stray file cannot inflate the
    budget accounting.  A missing directory is ``0``.
    """
    try:
        images_dir = _images_dir_path(workspace_id)
    except ValueError:
        return 0
    total = 0
    try:
        entries = list(os.scandir(images_dir))
    except OSError:
        return 0
    for entry in entries:
        if not _FILENAME_RE.match(entry.name):
            continue
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:  # pragma: no cover - raced with a delete
            continue
    return total


def _scan_files(images_dir: Path) -> list:
    """Return ``[(path, id_or_None, size, mtime)]`` for the store's own files.

    Recognised names (final ``<id>.<ext>`` and in-flight ``<id>.<ext>.tmp``)
    are returned; anything else in the directory is left completely alone --
    this store does not own foreign files and must not delete them.
    """
    scanned: list = []
    try:
        entries = list(os.scandir(images_dir))
    except OSError:
        return scanned
    for entry in entries:
        name = entry.name
        tmp = name.endswith(_TMP_SUFFIX)
        stem = name[: -len(_TMP_SUFFIX)] if tmp else name
        match = _FILENAME_RE.match(stem)
        if match is None:
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:  # pragma: no cover - raced with a delete
            continue
        scanned.append((
            Path(entry.path),
            None if tmp else match.group("id"),
            stat.st_size,
            stat.st_mtime,
        ))
    return scanned


def sweep_workspace(
    workspace_id: str,
    referenced: Set[str],
    *,
    ttl_seconds: int = ORPHAN_TTL_SECONDS,
) -> dict:
    """Remove this workspace's orphaned image files, then enforce the budget.

    A file goes only when BOTH hold: its id is not in *referenced*, and its
    mtime is older than *ttl_seconds*.  Stale ``.tmp`` leftovers of the store's
    own shape are orphaning by definition (never referenced) and follow the
    same TTL.  After that, if the workspace is still over
    :data:`MAX_WORKSPACE_IMAGE_BYTES`, the oldest unreferenced files are
    removed until it fits.

    A referenced file is NEVER deleted, not even to satisfy the budget.

    Exactly one INFO line is logged per sweep -- including a sweep that removed
    nothing, because a silent sweep and a broken sweep look identical in a log.

    Args:
        workspace_id: The workspace to sweep.
        referenced: Ids still referenced by that workspace's persisted
            histories (see :func:`referenced_ids_from_history`).
        ttl_seconds: Orphan age threshold.

    Returns:
        ``{'scanned_files', 'scanned_bytes', 'removed', 'freed_bytes'}``.

    Raises:
        ValueError: If *workspace_id* is not a usable path segment.
    """
    images_dir = _images_dir_path(workspace_id)
    referenced_ids = {image_id for image_id in (referenced or set())
                      if _valid_image_id(image_id)}
    now = time.time()

    scanned_files = 0
    scanned_bytes = 0
    removed = 0
    freed_bytes = 0
    kept_bytes = 0
    survivors: list = []

    for path, image_id, size, mtime in _scan_files(images_dir):
        scanned_files += 1
        scanned_bytes += size
        age = now - mtime
        if image_id is None or image_id not in referenced_ids:
            if age > ttl_seconds:
                try:
                    os.unlink(path)
                except OSError:  # pragma: no cover - raced with a delete
                    survivors.append((path, image_id, size, mtime))
                    kept_bytes += size
                    continue
                removed += 1
                freed_bytes += size
                continue
        survivors.append((path, image_id, size, mtime))
        kept_bytes += size

    # Budget pass: oldest unreferenced first.  A referenced file is never a
    # candidate, however old or large it is.
    if kept_bytes > MAX_WORKSPACE_IMAGE_BYTES:
        for path, image_id, size, mtime in sorted(survivors, key=lambda item: item[3]):
            if kept_bytes <= MAX_WORKSPACE_IMAGE_BYTES:
                break
            if image_id is not None and image_id in referenced_ids:
                continue
            try:
                os.unlink(path)
            except OSError:  # pragma: no cover - raced with a delete
                continue
            removed += 1
            freed_bytes += size
            kept_bytes -= size

    result = {
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "removed": removed,
        "freed_bytes": freed_bytes,
    }
    logger.info(
        "Image sweep workspace=%s: scanned %d file(s) %d byte(s), "
        "removed %d, freed %d byte(s), %d byte(s) kept (budget %d, ttl %ds)",
        workspace_id,
        scanned_files,
        scanned_bytes,
        removed,
        freed_bytes,
        kept_bytes,
        MAX_WORKSPACE_IMAGE_BYTES,
        ttl_seconds,
    )
    return result


def sweep_all_workspaces(
    referenced: Optional[Mapping[str, Iterable[str]]] = None,
    *,
    ttl_seconds: int = ORPHAN_TTL_SECONDS,
) -> dict:
    """Sweep every workspace that has an image directory.

    Args:
        referenced: Optional ``{workspace_id: ids}`` map -- normally built by
            the caller from each workspace's persisted histories.  A workspace
            absent from the map has no known references, so only its
            already-expired orphans are collected.
        ttl_seconds: Orphan age threshold, passed to every workspace sweep.

    Returns:
        ``{'workspaces': {workspace_id: result}, 'scanned_files',
        'scanned_bytes', 'removed', 'freed_bytes'}`` with the last four summed
        over every swept workspace.
    """
    referenced_map: Mapping[str, Iterable[str]] = referenced or {}
    totals = {
        "workspaces": {},
        "scanned_files": 0,
        "scanned_bytes": 0,
        "removed": 0,
        "freed_bytes": 0,
    }

    workspaces_root = vault_root() / "workspaces"
    try:
        entries = sorted(os.scandir(workspaces_root), key=lambda entry: entry.name)
    except OSError:
        logger.info("Image sweep: no workspaces directory under %s", workspaces_root)
        return totals

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:  # pragma: no cover - raced with a delete
            continue
        workspace_id = entry.name
        try:
            images_dir = _images_dir_path(workspace_id)
        except ValueError:
            # Reserved or malformed directory name: not a workspace this store
            # may touch.  Deliberately quiet -- sweep_all logs once per
            # workspace it actually sweeps.
            continue
        if not images_dir.is_dir():
            # Never sweep (or create) a workspace that stored no image.
            continue
        result = sweep_workspace(
            workspace_id,
            set(referenced_map.get(workspace_id, ()) or ()),
            ttl_seconds=ttl_seconds,
        )
        totals["workspaces"][workspace_id] = result
        for key in ("scanned_files", "scanned_bytes", "removed", "freed_bytes"):
            totals[key] += result[key]

    return totals
