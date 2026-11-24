"""Per-session garbage collection for the ephemeral pasted-image store.

``agent.core.image_store`` is one process-wide, content-addressed table, so it
cannot be collected from a single session's transcript: the same screenshot
pasted in two tabs is ONE entry, and ``image_store.reconcile`` (which drops
every id the caller's transcript does not mention) would free the other tab's
image too.

This module keeps the missing piece of state -- which ids each session put into
the store -- and collects only what is BOTH:

* tracked by the session doing the collecting,
* no longer referenced by that session's (pruned) transcript, and
* not tracked by any OTHER live session.

Called from the two ends of a session's life: the turn that appends the images
(``Agent.process_query``) registers them, and every persistence of the session
(``session.store.FileSystemSessionStore.save_session``, which writes the
post-prune/post-cap history) collects the ones the transcript dropped.

The id sets are in-memory only, exactly like the bytes they protect: a restart
loses them along with the store's contents, and nothing here is persisted.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional, Set

from agent.core import image_store

logger = logging.getLogger(__name__)

__all__ = [
    "track_images",
    "tracked_ids",
    "drop_session",
    "live_image_ids",
    "warm_images",
    "gc_session",
    "reset",
]

#: session_id -> ids this session put into the store.
_sessions: Dict[str, Set[str]] = {}

#: session_id -> workspace that session belongs to, so a close can find the
#: directory holding the durable bytes.  Only a caller that knows the
#: workspace records anything here.  Lifetime is exactly ``_sessions``':
#: in-memory only, dropped with the session, cleared by :func:`reset`.
_workspaces: Dict[str, str] = {}

#: Guards ``_sessions`` (the image store has its own lock).
_lock = threading.RLock()


def _image_id(entry: Any) -> Optional[str]:
    """Return the image id carried by one ``images`` entry.

    Mirrors the adapter's resolver (``llm_providers.openai_compatible``): the
    canonical entry is the id string, a dict with ``id`` is tolerated because
    the frontend may hand over the richer upload descriptor, and the legacy
    ``image_id`` key is accepted so an old transcript still collects correctly.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("id", "image_id"):
            candidate = entry.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def track_images(
    session_id: Optional[str],
    images: Any,
    workspace_id: Optional[str] = None,
) -> None:
    """Record that *session_id* holds the images in *images* in the store.

    Idempotent: tracking the same id twice (a replayed turn, the same
    screenshot pasted again) leaves one entry.

    Args:
        session_id: Owning session; a falsy id tracks nothing.
        images: The turn's image metadata -- ids or ``{"id": ...}`` dicts.
            ``None``/empty is a no-op.
        workspace_id: Workspace owning the session, remembered only so that
            :func:`drop_session` can find the durable image directory when the
            session closes.  Default ``None`` records nothing, so a caller
            that does not know the workspace (or predates this parameter)
            keeps working and simply never unlinks anything.
    """
    if not session_id or not images:
        return
    ids = {image_id for image_id in (_image_id(entry) for entry in images) if image_id}
    if not ids:
        return
    with _lock:
        _sessions.setdefault(session_id, set()).update(ids)
        if workspace_id:
            _workspaces[session_id] = workspace_id


def tracked_ids(session_id: Optional[str]) -> Set[str]:
    """Return a copy of the ids currently tracked for *session_id*."""
    if not session_id:
        return set()
    with _lock:
        return set(_sessions.get(session_id, ()))


def drop_session(
    session_id: Optional[str],
    workspace_id: Optional[str] = None,
) -> None:
    """Stop tracking *session_id* and reclaim its unreferenced image bytes.

    Used when a session closes.  The ids the session put into the store are
    forgotten, and the durable files behind them are deleted -- but only the
    ones no persisted transcript of the workspace still mentions (see
    :func:`_unlink_over`, which deletes nothing at all when it cannot read
    every transcript).  Images the saved history still references must stay
    resolvable, so they are left alone.

    Args:
        session_id: Closing session; a falsy id does nothing.
        workspace_id: Workspace holding the durable files.  Explicit wins over
            the one recorded by :func:`track_images`; with neither, nothing is
            unlinked -- an unknown workspace is never guessed.
    """
    if not session_id:
        return
    with _lock:
        tracked = _sessions.pop(session_id, None)
        stored_workspace = _workspaces.pop(session_id, None)
    _unlink_over(workspace_id or stored_workspace, tracked or ())


def _unlink_over(workspace_id: Optional[str], candidates: Iterable[Any]) -> int:
    """Delete the durable files of *candidates* that nothing references.

    The reference authority is the workspace's persisted transcripts: at close
    time the in-memory tracking is gone, so ``scan_workspace_references`` is
    what can still say whether an id is live anywhere in the workspace.

    Deletion is all-or-nothing per call.  If even one persisted transcript
    cannot be read, the scan's set may UNDER-count, so every candidate is kept
    rather than acting on a partial view.

    Args:
        workspace_id: Workspace whose image directory to collect; falsy means
            "unknown", which deletes nothing.
        candidates: Ids the closing session had tracked.

    Returns:
        The number of files actually unlinked.
    """
    if not workspace_id:
        return 0
    image_ids = {entry for entry in candidates or () if isinstance(entry, str) and entry}
    if not image_ids:
        return 0
    # Imported here, not at module scope: ``agent.core.image_files`` imports
    # this module at import time, so a top-level import would be circular.
    from agent.core import image_files

    try:
        referenced, complete = image_files.scan_workspace_references(workspace_id)
    except Exception:  # defensive only: the scan is documented never to raise
        logger.warning(
            "Skipping image collection for workspace %s: reference scan failed",
            workspace_id, exc_info=True,
        )
        return 0
    if not complete:
        logger.warning(
            "Keeping %d image file(s) in workspace %s: at least one persisted "
            "transcript could not be read, so an unreferenced id cannot be "
            "called dead",
            len(image_ids), workspace_id,
        )
        return 0
    removed = 0
    for image_id in image_ids - referenced:
        if image_files.delete_image(workspace_id, image_id):
            removed += 1
    return removed


def live_image_ids(history: Iterable[Any]) -> Set[str]:
    """Return every image id still referenced by *history*.

    Args:
        history: Iterable of message dicts (``Message`` is a dict subclass);
            each may carry ``images`` as ids or metadata dicts.  Anything that
            is not a message mapping, or whose ``images`` is not a list, is
            ignored rather than raising.
    """
    live: Set[str] = set()
    for message in history or ():
        if not isinstance(message, dict):
            continue
        entries = message.get("images")
        if not isinstance(entries, (list, tuple, set)):
            continue
        for entry in entries:
            image_id = _image_id(entry)
            if image_id:
                live.add(image_id)
    return live


def _ordered_image_ids(history: Iterable[Any]) -> List[str]:
    """Return the image ids *history* references, oldest first, deduplicated.

    :func:`live_image_ids` answers "what is referenced" as a set; warming needs
    the order too, because the store it fills is bounded and the newest turn is
    the one the next request is most likely to send again.  Message order and
    read order are identical to ``live_image_ids`` -- only the set is dropped.

    Args:
        history: Iterable of message dicts; anything that is not a mapping, or
            whose ``images`` is not a sequence, is ignored rather than raising.
    """
    ordered: List[str] = []
    seen: Set[str] = set()
    for message in history or ():
        if not isinstance(message, dict):
            continue
        entries = message.get('images')
        if not isinstance(entries, (list, tuple, set)):
            continue
        for entry in entries:
            image_id = _image_id(entry)
            if image_id and image_id not in seen:
                seen.add(image_id)
                ordered.append(image_id)
    return ordered


def warm_images(
    workspace_id: Optional[str],
    messages: Iterable[Any],
    *,
    budget: Optional[int] = None,
) -> int:
    """Restore the durable images *messages* names into the in-memory store.

    ``agent.core.image_store`` lives in the process and nothing else, so a
    restart starts it empty while the transcript -- and the UI, which reads the
    durable files in ``agent.core.image_files`` directly -- still shows the
    user's screenshots.  A miss then reaches the provider as
    ``IMAGE_UNAVAILABLE_PLACEHOLDER`` text, so the model answers about an image
    it never saw: the visible transcript and the actual request disagree.

    This is the read-side repair: the bytes the transcript still names are put
    back, so a pasted image survives the process that received it.  It only
    ever ADDS, and only for ids the caller's own transcript mentions, which is
    why it is not ``image_store.reconcile`` -- that one drops every id the
    caller does not mention, and with a content-addressed store shared by all
    sessions it would blank the image another conversation still shows.

    Warming runs newest first: the store is bounded and the newest turns are
    the ones a follow-up is most likely to resend, so they must not be the ones
    evicted to make room for an older screenshot.

    Never raises, and never turns a miss into a hit: bytes that are gone stay
    gone and the store's own miss path reports that honestly.

    Args:
        workspace_id: Workspace holding the durable files.  Falsy means
            "unknown", which warms nothing -- another workspace's directory is
            never guessed at.
        messages: Message dicts, read exactly as :func:`live_image_ids` reads
            them.
        budget: Maximum number of images to warm, defaulting to
            ``image_store.MAX_ENTRIES`` -- the most the store can hold at once,
            so a longer transcript cannot evict its own newest images while
            warming.

    Returns:
        The number of images actually put back into the store.
    """
    if not workspace_id:
        return 0
    image_ids = _ordered_image_ids(messages)
    if not image_ids:
        return 0
    limit = image_store.MAX_ENTRIES if budget is None else budget
    if limit <= 0:
        return 0
    # Imported here, not at module scope: ``agent.core.image_files`` imports
    # this module at import time, so a top-level import would be circular.
    from agent.core import image_files

    warmed = 0
    for image_id in reversed(image_ids):
        if warmed >= limit:
            break
        if image_store.get(image_id) is not None:
            continue
        try:
            data = image_files.load_image(workspace_id, image_id)
            if data is None:
                continue
            media_type = image_files.media_type_of(workspace_id, image_id)
            if not media_type:
                # ``image_files`` recovers the media type from the file name
                # only, so an unknown one means the file cannot say what it
                # holds.  Guessing a type here would send the provider a
                # mislabelled payload -- an error, or garbage decoded as an
                # image -- where an honest miss degrades to the placeholder
                # the rest of the path already handles.  The bytes are simply
                # dropped.
                logger.warning(
                    "Not warming image %s of workspace %s: its file names no "
                    "known image media type",
                    image_id, workspace_id,
                )
                continue
            image_store.put(data, media_type, image_id=image_id)
        except Exception:
            logger.warning(
                "Could not warm image %s of workspace %s into the store",
                image_id, workspace_id, exc_info=True,
            )
            continue
        warmed += 1
    return warmed


def gc_session(session_id: Optional[str], history: Iterable[Any]) -> int:
    """Release this session's images that *history* no longer references.

    *history* must be the history as it will be persisted (i.e. AFTER the
    pruner and the size bound), because that is the transcript the process can
    still be asked to resolve images from.

    Ids still tracked by another session are never released: the store is
    content-addressed, so two sessions pasting the same screenshot share one
    entry and one session's collection must not blank the other's image.

    Deliberately does NOT delete durable image files, and must not be made to:
    it runs inside ``save_session`` on EVERY turn, so (i) rescanning the
    workspace's session files here would cost O(all sessions) per turn, and
    (ii) the transcript holding the reference is the very file being written,
    so unlinking against it would race the authoritative reference.  Bytes are
    reclaimed at session close (:func:`drop_session`) or by the TTL sweep in
    ``agent.core.image_files`` -- never mid-turn.

    Args:
        session_id: Session being collected; a falsy id collects nothing.
        history: The session's post-prune message list.

    Returns:
        The number of store entries actually released.
    """
    if not session_id:
        return 0
    live = live_image_ids(history)
    with _lock:
        tracked = _sessions.get(session_id)
        if not tracked:
            return 0
        shared: Set[str] = set()
        for other_id, other_tracked in _sessions.items():
            if other_id != session_id:
                shared |= other_tracked
        dead = tracked - live - shared
        remaining = tracked & live
        if remaining:
            _sessions[session_id] = remaining
        else:
            _sessions.pop(session_id, None)
        released = 0
        for image_id in dead:
            if image_store.release(image_id):
                released += 1
        return released


def reset() -> None:
    """Forget every session (used on shutdown and by tests).

    Does NOT touch the image store: the caller owns that table and resets it
    itself, so a test can exercise one without clearing the other.
    """
    with _lock:
        _sessions.clear()
        _workspaces.clear()
