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

import threading
from typing import Any, Dict, Iterable, Optional, Set

from agent.core import image_store

__all__ = [
    "track_images",
    "tracked_ids",
    "drop_session",
    "live_image_ids",
    "gc_session",
    "reset",
]

#: session_id -> ids this session put into the store.
_sessions: Dict[str, Set[str]] = {}

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


def track_images(session_id: Optional[str], images: Any) -> None:
    """Record that *session_id* holds the images in *images* in the store.

    Idempotent: tracking the same id twice (a replayed turn, the same
    screenshot pasted again) leaves one entry.

    Args:
        session_id: Owning session; a falsy id tracks nothing.
        images: The turn's image metadata -- ids or ``{"id": ...}`` dicts.
            ``None``/empty is a no-op.
    """
    if not session_id or not images:
        return
    ids = {image_id for image_id in (_image_id(entry) for entry in images) if image_id}
    if not ids:
        return
    with _lock:
        _sessions.setdefault(session_id, set()).update(ids)


def tracked_ids(session_id: Optional[str]) -> Set[str]:
    """Return a copy of the ids currently tracked for *session_id*."""
    if not session_id:
        return set()
    with _lock:
        return set(_sessions.get(session_id, ()))


def drop_session(session_id: Optional[str]) -> None:
    """Stop tracking *session_id* without touching the store.

    Used when a session closes: the images its saved transcript still
    references must stay resolvable in this process, so they are left to the
    store's LRU cap rather than released here.
    """
    if not session_id:
        return
    with _lock:
        _sessions.pop(session_id, None)


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


def gc_session(session_id: Optional[str], history: Iterable[Any]) -> int:
    """Release this session's images that *history* no longer references.

    *history* must be the history as it will be persisted (i.e. AFTER the
    pruner and the size bound), because that is the transcript the process can
    still be asked to resolve images from.

    Ids still tracked by another session are never released: the store is
    content-addressed, so two sessions pasting the same screenshot share one
    entry and one session's collection must not blank the other's image.

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
