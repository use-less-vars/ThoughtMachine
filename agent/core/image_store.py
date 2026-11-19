"""
In-process CACHE of pasted-image bytes (the durable copy lives on disk).

A pasted screenshot reaches the backend as bytes, but the canonical message
dict only ever carries a *reference* to it::

    {"role": "user", "content": "why does this fail?", "images": ["3f2a...c1"]}

The bytes are held here, keyed by an opaque image id, and are resolved by the
provider adapter when the request is built.  Keeping them out of the message
dict means the transcript, the context builder and every token estimator stay
JSON-serialisable and cheap - a message prints (and is estimated) as its text
plus a short id, never as megabytes of base64.

Contract (a cache, not the source of truth)
------------------------------------------
* **The durable layer is** :mod:`agent.core.image_files`: every ingested image
  is written to ``<vault>/workspaces/<workspace_id>/images/<image_id>.<ext>``
  before it is put here, so a session switch, a reload or a process restart can
  re-read it.  This module is the *cache* in front of that: decoded bytes in
  memory, no disk I/O on the turn path.
* Nothing here is persisted, and nothing here may be treated as storage.  An
  entry leaving this table (eviction, :meth:`ImageStore.release`,
  :meth:`ImageStore.reconcile`) NEVER deletes a file -- the file is the durable
  copy and outlives the cache.
* Ids are supplied by the caller (``put(..., image_id=...)``), minted by
  :func:`agent.core.image_files.mint_image_id` -- random, not content-derived,
  so identical bytes in two workspaces are two unrelated ids.  ``put`` without
  an id still falls back to a content hash for callers that have no workspace
  (tests, legacy paths); no ingest path uses that fallback.
* Capacity is capped (``MAX_ENTRIES`` / ``MAX_BYTES``).  Insertion evicts the
  least recently used entries, preferring entries nothing references anymore.
* A miss is never fatal.  Evicted bytes, or a transcript replayed in a fresh
  process, resolve to ``None`` here; the adapter substitutes
  ``IMAGE_UNAVAILABLE_PLACEHOLDER`` so the model is told the screenshot can no
  longer be seen instead of the whole request failing.

All methods are thread-safe (a single ``RLock`` guards the entry table); the
store is used from the event loop and from tool worker threads.
"""
import hashlib
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "MAX_ENTRIES",
    "MAX_BYTES",
    "IMAGE_UNAVAILABLE_PLACEHOLDER",
    "ImageStore",
    "get_image_store",
    "put",
    "get",
    "release",
    "reconcile",
    "stats",
    "reset",
]

#: Maximum number of distinct images held at once.
MAX_ENTRIES = 32

#: Maximum total payload held at once (96 MiB).
MAX_BYTES = 96 * 1024 * 1024

#: Text substituted for an image whose bytes are no longer available.
IMAGE_UNAVAILABLE_PLACEHOLDER = "[screenshot — no longer available]"


class ImageStore:
    """Bounded, id-keyed, in-memory image cache.

    Each entry is held as ``[data, media_type, refcount, last_used_seq]`` so
    that reconciliation can tell which images are still referenced by the live
    transcript and eviction can pick the least recently used unreferenced one.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES):
        """Create a store with explicit capacity limits.

        Args:
            max_entries: Maximum number of distinct images to retain.
            max_bytes: Maximum total payload size in bytes.
        """
        self._lock = threading.RLock()
        self._entries: Dict[str, List[Any]] = {}
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._total_bytes = 0
        self._seq = 0

    # ------------------------------------------------------------------
    # internals (callers must hold the lock)
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        """Return a monotonically increasing LRU sequence number."""
        self._seq += 1
        return self._seq

    def _drop(self, image_id: str) -> None:
        """Remove an entry, keeping the byte accounting consistent."""
        entry = self._entries.pop(image_id, None)
        if entry is not None:
            self._total_bytes -= len(entry[0])

    def _evict_until_fits(self, incoming_bytes: int) -> None:
        """Evict LRU entries until one more image of `incoming_bytes` fits.

        Unreferenced entries (``refcount == 0``) are preferred; within a class
        the least recently used one goes first.  Bounded by the entry count and
        the byte budget, whichever binds first.

        Eviction drops the in-memory copy ONLY.  The durable file written by
        ``agent.core.image_files.save_image`` is untouched: that file is the
        copy that survives the cache, so unlinking it here would delete the
        user's image exactly when the cache is under pressure.
        """
        while self._entries and (
            len(self._entries) + 1 > self._max_entries
            or self._total_bytes + incoming_bytes > self._max_bytes
        ):
            victim = min(
                self._entries.items(),
                key=lambda item: (item[1][2] > 0, item[1][3]),
            )[0]
            self._drop(victim)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def put(self, data: bytes, media_type: str, image_id: Optional[str] = None) -> str:
        """Store image bytes and return the id they are held under.

        Args:
            data: Raw image bytes.
            media_type: MIME type, e.g. ``image/png``.
            image_id: The id to key the entry by.  Supplied by the caller that
                minted it (and already wrote the durable copy with it); when
                omitted, a content hash is used so callers without a workspace
                still work.

        Returns:
            The image id, to be placed in a message's ``images`` list.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("image data must be bytes")
        payload = bytes(data)
        if image_id is None:
            image_id = hashlib.sha256(payload).hexdigest()[:16]
        elif not isinstance(image_id, str) or not image_id:
            raise ValueError("image_id must be a non-empty string")
        with self._lock:
            entry = self._entries.get(image_id)
            if entry is not None:
                # Same bytes pasted again: reuse the stored copy, touch the LRU.
                entry[3] = self._next_seq()
                return image_id
            self._evict_until_fits(len(payload))
            self._entries[image_id] = [payload, media_type or "image/png", 0, self._next_seq()]
            self._total_bytes += len(payload)
            return image_id

    def get(self, image_id: str) -> Optional[Tuple[bytes, str]]:
        """Return ``(data, media_type)`` for an id, or ``None`` on a miss.

        A hit counts as a use and refreshes the entry's LRU position.
        """
        with self._lock:
            entry = self._entries.get(image_id)
            if entry is None:
                return None
            entry[3] = self._next_seq()
            return entry[0], entry[1]

    def release(self, image_id: str) -> bool:
        """Drop an image outright (message deleted, conversation reset).

        Returns:
            True if an entry was removed.
        """
        with self._lock:
            if image_id not in self._entries:
                return False
            self._drop(image_id)
            return True

    def reconcile(self, live_ids: Iterable[str]) -> Dict[str, Any]:
        """Recompute reference counts from the ids still present in the transcript.

        Entries that nothing references are dropped, so a long conversation
        cannot grow the store without bound while still keeping alive every
        image the model may be asked about again.

        Args:
            live_ids: Image ids referenced by the current conversation.

        Returns:
            The resulting :meth:`stats` mapping.
        """
        live = [image_id for image_id in live_ids if image_id]
        live_set = set(live)
        with self._lock:
            for image_id in list(self._entries):
                if image_id not in live_set:
                    self._drop(image_id)
            for image_id in live:
                entry = self._entries.get(image_id)
                if entry is not None:
                    entry[2] += 1
                    entry[3] = self._next_seq()
            return self.stats()

    def stats(self) -> Dict[str, Any]:
        """Return a snapshot of the store's contents and capacity."""
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._total_bytes,
                "ids": list(self._entries),
                "max_entries": self._max_entries,
                "max_bytes": self._max_bytes,
            }

    def reset(self) -> None:
        """Drop every entry (used on shutdown and by tests)."""
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0
            self._seq = 0


_singleton: Optional[ImageStore] = None
_singleton_lock = threading.Lock()


def get_image_store() -> ImageStore:
    """Return the process-wide image store, creating it on first use."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ImageStore()
    return _singleton


def put(data: bytes, media_type: str, image_id: Optional[str] = None) -> str:
    """Module-level shortcut for :meth:`ImageStore.put` on the shared store."""
    return get_image_store().put(data, media_type, image_id=image_id)


def get(image_id: str) -> Optional[Tuple[bytes, str]]:
    """Module-level shortcut for :meth:`ImageStore.get` on the shared store."""
    return get_image_store().get(image_id)


def release(image_id: str) -> bool:
    """Module-level shortcut for :meth:`ImageStore.release` on the shared store."""
    return get_image_store().release(image_id)


def reconcile(live_ids: Iterable[str]) -> Dict[str, Any]:
    """Module-level shortcut for :meth:`ImageStore.reconcile` on the shared store."""
    return get_image_store().reconcile(live_ids)


def stats() -> Dict[str, Any]:
    """Module-level shortcut for :meth:`ImageStore.stats` on the shared store."""
    return get_image_store().stats()


def reset() -> None:
    """Module-level shortcut for :meth:`ImageStore.reset` on the shared store."""
    get_image_store().reset()
