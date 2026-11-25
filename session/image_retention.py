"""Tombstone retention pass for the image references of a session transcript.

An image message that stays in ``Session.user_history`` forever carries its
image ids forever: every request that replays the history pays for them, and so
does every reader of the session file.  This module bounds that by TOMBSTONING:
the image is released by emptying ``message['images']`` while the message itself
stays in the transcript.  The message is NEVER dropped.  Its ``content``,
``role``, ``created_at``, ``seq`` and every other key stay byte-identical, so
the transcript still reads as the conversation it was and a reload still shows
the question that was asked -- only the attachment is gone.

This module never deletes or unlinks a file.  Dropping an id makes the bytes
*unreferenced*; reclaiming them stays with three gated paths, the only code
allowed to remove a stored image: ``agent.core.image_gc._unlink_over`` (via
``drop_session`` at session close), ``session.store._prune_session_images``
(via ``delete_session``) and ``agent.core.image_files.sweep_workspace``.  They
share one gate: a file goes only after
``agent.core.image_files.scan_workspace_references`` returns ``complete is
True`` and the id is not in the set it found.  A transcript the scan cannot
read therefore withholds reclamation rather than risk unlinking a live file.

Two policies decide what goes, applied in this order:

* a count policy -- the most recent ``TOMBSTONE_MAX_IMAGE_MESSAGES``
  image-bearing messages keep their images;
* a byte policy -- on what the count policy left, the oldest image-bearing
  messages are tombstoned until the session's live image bytes are at or under
  ``TOMBSTONE_MAX_IMAGE_BYTES``.

Neither numeral is derived, and neither is measured -- they are named here so
there is no doubt about what they are:

* ``TOMBSTONE_MAX_IMAGE_MESSAGES = 40`` is an ESTIMATE.  Nothing in the tree
  converts an image into a token budget: the only number is
  ``agent.core.token_counter.IMAGE_TOKEN_ESTIMATE`` (1024 tokens per image
  entry, agent/core/token_counter.py:18) and the history builders
  (``session.context_builder``) carry no token-budget constant to divide by, so
  40 x 1024 = ~40 960 tokens is an arithmetic illustration, not a bound.
* ``TOMBSTONE_MAX_IMAGE_BYTES = 32 MiB`` is a CHOSEN fraction, exactly
  ``agent.core.image_files.MAX_WORKSPACE_IMAGE_BYTES // 8`` (256 MiB // 8): it
  makes eight sessions at this ceiling fill the workspace budget exactly.  The
  8 is a choice, not a measurement.

What would replace them: a measured bound on the image tokens one turn replays,
from which the message count follows by division and the byte ceiling follows
the same measurement rather than a chosen divisor.  Until that evidence exists
they stay guesses, and they are changed only by whoever brings it -- never to
make a test pass.

Both are per MESSAGE, never per image: a message is tombstoned whole, because a
partly stripped message would render as a partially available attachment.

What this does not do
---------------------
Tombstoning makes an id UNREFERENCED; it does not reclaim the bytes.
``sweep_workspace`` runs per workspace on the server's periodic sweep tick
(there is no startup pass), and an unreferenced file is removed only once it is
older than ``agent.core.image_files.ORPHAN_TTL_SECONDS`` (24 h) -- or sooner,
with no age gate, if the workspace is over its 256 MiB
``MAX_WORKSPACE_IMAGE_BYTES`` budget.  So under budget, "tombstoned and nothing
shrank" is the expected behaviour for up to a day, not a defect.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

__all__ = [
    "TOMBSTONE_COUNT_KEY",
    "TOMBSTONE_MAX_IMAGE_BYTES",
    "TOMBSTONE_MAX_IMAGE_MESSAGES",
    "apply_image_retention",
]

#: How many most-recent image-bearing messages keep their images.
#: What this binds is TOKENS, not disk: `agent.core.token_counter.IMAGE_TOKEN_ESTIMATE`
#: charges a flat 1024 tokens per image entry, so 40 image-bearing messages cost
#: ~40_960 tokens of modelled budget. It is a GUESS, not a derivation: the tree has
#: no history token-budget constant to trace it to (the only session-layer cap is
#: byte-based, `session.size_bounding.SESSION_SIZE_CAP_BYTES`, which cannot see an
#: image byte at all). Replace with a derived number once such a budget exists.
TOMBSTONE_MAX_IMAGE_MESSAGES = 40

#: Per-session ceiling on live image bytes, derived from the control that actually
#: binds: the 256 MiB workspace budget in `image_files.MAX_WORKSPACE_IMAGE_BYTES`.
#: Eight sessions at this ceiling fill that budget exactly.
TOMBSTONE_MAX_IMAGE_BYTES = 32 * 1024 * 1024  # == MAX_WORKSPACE_IMAGE_BYTES // 8

#: Per-message audit marker. Integer count of images tombstoned in that message.
#: Never rendered as a number, never read by the UI; the adapters key on its
#: truthiness only, to say one placeholder sentence at render time.
TOMBSTONE_COUNT_KEY = 'images_tombstoned'


def _candidate_indices(user_history: List[Any]) -> List[int]:
    """Return the indices of the messages still holding images, oldest first.

    A message is a candidate only when it is a mapping, carries a non-empty
    ``images`` list, and is not already marked by a previous pass.  A marked
    message that somehow still carries images is left alone: the pass is not
    run twice over the same message.
    """
    indices: List[int] = []
    for index, message in enumerate(user_history):
        if not isinstance(message, dict):
            continue
        if message.get(TOMBSTONE_COUNT_KEY):
            continue
        images = message.get('images')
        if isinstance(images, list) and images:
            indices.append(index)
    return indices


def _without_images(user_history: List[Any], marked: Set[int]) -> List[Any]:
    """Return *user_history* with the ``images`` of every marked message emptied."""
    if not marked:
        return user_history
    interim = list(user_history)
    for index in marked:
        interim[index] = {**interim[index], 'images': []}
    return interim


def _live_sizes(workspace_id: str, user_history: List[Any]) -> Dict[str, int]:
    """Return the on-disk size of every image id still referenced by *history*.

    An id with no resolvable file, or a file that cannot be stat'ed, counts as
    zero bytes: retention is a best-effort bound, never a reason to fail a save.
    """
    # Imported here, not at module scope: ``agent.core.image_files`` imports
    # ``session``-adjacent modules, so a top-level import would be circular.
    from agent.core import image_files, image_gc

    sizes: Dict[str, int] = {}
    for image_id in image_gc.live_image_ids(user_history):
        path = image_files.image_path(workspace_id, image_id)
        if path is None:
            sizes[image_id] = 0
            continue
        try:
            sizes[image_id] = path.stat().st_size
        except OSError:
            sizes[image_id] = 0
    return sizes


def apply_image_retention(
    workspace_id: Optional[str],
    user_history: List[Dict[str, Any]],
    *,
    max_image_messages: int = TOMBSTONE_MAX_IMAGE_MESSAGES,
    max_bytes: int = TOMBSTONE_MAX_IMAGE_BYTES,
) -> List[Dict[str, Any]]:
    """Tombstone the images of the oldest image-bearing messages in *user_history*.

    The count policy runs first (keep the newest *max_image_messages*
    image-bearing messages), then the byte policy on what is left (drop the
    oldest survivors until the live image bytes fit *max_bytes*).

    Args:
        workspace_id: Workspace owning the session; falsy means "unknown", and
            an unknown workspace tombstones nothing (the byte policy could not
            measure anything, and the count policy is not needed to make a
            stored file smaller by a guess).
        user_history: The message dicts of the session.  A message that is
            tombstoned is shallow-copied; every other message is returned as the
            very same object.
        max_image_messages: How many most-recent image-bearing messages keep
            their images.
        max_bytes: Ceiling on the live image bytes of the session.

    Returns:
        The history to persist: the input list itself when nothing was
        tombstoned, otherwise a new list.  The input is never mutated.
    """
    try:
        if not workspace_id or not isinstance(user_history, list) or not user_history:
            return user_history

        # Imported here, not at module scope: ``agent.core`` imports ``session``
        # in the other direction, so a top-level import would be circular.
        from agent.core import image_gc

        candidates = _candidate_indices(user_history)
        if not candidates:
            return user_history

        # index -> ids that message contributed, for the messages being marked.
        marked: Dict[int, Set[str]] = {}

        # Count policy: keep the newest N image-bearing messages, tombstone the
        # excess from the oldest end.
        excess = len(candidates) - max_image_messages
        if excess > 0:
            for index in candidates[:excess]:
                marked[index] = image_gc.live_image_ids([user_history[index]])

        # Byte policy: whatever the count policy left, drop oldest-first until
        # the live image bytes are under the ceiling.
        live = _live_sizes(workspace_id, _without_images(user_history, set(marked)))
        live_bytes = sum(live.values())
        for index in candidates:
            if index in marked:
                continue
            if live_bytes <= max_bytes:
                break
            ids = image_gc.live_image_ids([user_history[index]])
            marked[index] = ids
            for image_id in ids:
                live_bytes -= live.pop(image_id, 0)

        if not marked:
            return user_history

        # Apply: empty the images list, record how many were removed, and leave
        # every other key of the message untouched.
        rewritten: List[Dict[str, Any]] = []
        for index, message in enumerate(user_history):
            if index in marked:
                copy = dict(message)
                copy['images'] = []
                copy[TOMBSTONE_COUNT_KEY] = len(message['images'])
                rewritten.append(copy)
            else:
                rewritten.append(message)
        return rewritten
    except Exception:
        logger.warning(
            "Image retention pass skipped for workspace %r: keeping the history "
            "exactly as it was", workspace_id, exc_info=True,
        )
        return user_history
