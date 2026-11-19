"""image_ingest.py -- the one place pasted-image data URLs stop travelling.

The WebSocket envelope may carry a TOP-LEVEL ``images`` list (a sibling of
``command``/``query``/``config``, never inside ``config``: ``SessionConfig`` is
``extra='forbid'`` and the envelope itself is an unvalidated dict)::

    {"command": "start_session", "query": "", "images": [
        {"data_url": "data:image/png;base64,...,", "media_type": "image/png",
         "width": 1280, "height": 720}]}

``ingest_incoming_images`` is the boundary: it parses and validates the
payloads, decodes them once, has the workspace's DURABLE store
(``agent.core.image_files``) mint an opaque image id and write the bytes,
then caches the same id and bytes in the
process-wide ``agent.core.image_store`` and returns METADATA ONLY
(``id``/``media_type``/``width``/``height``).  Nothing below this module
ever sees a ``data_url`` or base64 text -- the provider adapter resolves the id
back to bytes when it builds the wire request.

The id is minted here, once, and names BOTH layers, so the file on disk and the
cache entry are the same thing.  It is not derived from the content: identical
bytes pasted into two workspaces produce two ids and two files.

Rejection is total: a bad image anywhere in the list rejects the turn and
releases everything this call had already stored -- cache entry and durable
file -- so a partially-ingested payload cannot leak entries or files.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, List, Optional, Sequence, Tuple

from agent.core import image_files, image_store

__all__ = [
    "ALLOWED_MEDIA_TYPES",
    "IMAGE_ONLY_QUERY_PLACEHOLDER",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGES",
    "MAX_TOTAL_IMAGE_BYTES",
    "ingest_incoming_images",
]

#: Hard cap on images per turn.
MAX_IMAGES = 4

#: Hard cap on DECODED bytes per image (6 MiB).
MAX_IMAGE_BYTES = 6 * 1024 * 1024

#: Hard cap on the DECODED total across one turn (10 MiB = 10 * 1024 * 1024
#: bytes).  The per-image cap alone bounds nothing: MAX_IMAGES x MAX_IMAGE_BYTES
#: is 4 x 6 MiB = 24 MiB of decoded data, ~32 MiB of base64 in ONE envelope.
MAX_TOTAL_IMAGE_BYTES = 10 * 1024 * 1024

#: Media types the providers can actually consume.  ``image/svg+xml`` is
#: deliberately absent: it is a script-bearing document, not a bitmap, and no
#: supported provider accepts it inline.
ALLOWED_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp")

#: Text substituted for the query when the operator sent images with no text.
IMAGE_ONLY_QUERY_PLACEHOLDER = "[Image attached]"

#: Ingest is a boundary: a durability failure is logged here, never raised into
#: the WebSocket handler.
logger = logging.getLogger(__name__)


def _encoded_upper_bound(max_bytes: int) -> int:
    """Largest base64 text that can decode to ``max_bytes`` bytes.

    Checked BEFORE decoding so a hostile payload is rejected without ever
    being materialised as bytes.
    """
    return ((max_bytes + 2) // 3) * 4 + 8


def _coerce_dimension(value: Any) -> int:
    """Pass an int dimension through; anything else (incl. ``bool``) is 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _parse_data_url(data_url: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Split ``data:<mime>;base64,<payload>`` into ``(mime, payload, error)``."""
    if not isinstance(data_url, str):
        return None, None, "image payload must be a data URL string"
    if not data_url.startswith("data:"):
        return None, None, "image payload must be a data URL string"
    header, sep, payload = data_url.partition(",")
    if not sep:
        return None, None, "malformed data URL (no ',' separator)"
    body = header[len("data:"):]
    if not body.endswith(";base64"):
        return None, None, "image payload must be base64 encoded"
    media_type = body[: -len(";base64")].strip().lower()
    if not media_type:
        return None, None, "data URL carries no media type"
    return media_type, payload, None


def ingest_incoming_images(
    raw: Any,
    *,
    workspace_id: Optional[str] = None,
    max_images: int = MAX_IMAGES,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_total_bytes: int = MAX_TOTAL_IMAGE_BYTES,
    allowed_media_types: Sequence[str] = ALLOWED_MEDIA_TYPES,
) -> Tuple[List[dict], Optional[str]]:
    """Validate, persist and cache the envelope's ``images`` list.

    Args:
        raw: The raw top-level ``images`` value from the envelope.  ``None`` or
            an empty list is the no-images case.
        workspace_id: The workspace this turn belongs to -- the isolation
            boundary of the durable store.  Without one the bytes are only
            cached in this process (there is no directory to address), which
            degrades to the old ephemeral behaviour rather than rejecting the
            turn; every real ingest path passes it.
        max_images: Maximum number of images accepted in one turn.
        max_bytes: Maximum DECODED size of one image.
        max_total_bytes: Maximum DECODED size summed over every image of the
            turn.
        allowed_media_types: Media types accepted for this turn.

    Returns:
        ``(metadata, None)`` on success, where ``metadata`` is a list of
        ``{"id", "media_type", "width", "height"}`` dicts whose bytes
        are already in ``agent.core.image_store`` and, when a workspace was
        given, on disk under it; or ``([], reason)`` on rejection, in which
        case nothing this call stored is left behind.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, (list, tuple)):
        return [], "images must be a list"
    if not raw:
        return [], None
    if len(raw) > max_images:
        return [], f"Too many images: {len(raw)} (maximum {max_images} per message)"

    allowed = tuple(allowed_media_types)
    encoded_bound = _encoded_upper_bound(max_bytes)
    stored_ids: List[str] = []

    def _reject(reason: str) -> Tuple[List[dict], Optional[str]]:
        # Release everything this call already stored -- including the
        # duplicates of a repeated image (one put == one refcount) -- and drop
        # its durable copy, so a rejected turn leaves neither a cache entry nor
        # an orphan file behind.
        for image_id in stored_ids:
            try:
                image_store.release(image_id)
            except Exception:  # pragma: no cover - release never raises
                pass
            if workspace_id:
                try:
                    image_files.delete_image(workspace_id, image_id)
                except Exception:  # pragma: no cover - delete never raises
                    pass
        stored_ids.clear()
        return [], reason

    metadata: List[dict] = []
    total_bytes = 0
    for index, entry in enumerate(raw, start=1):
        if isinstance(entry, str):
            data_url = entry
            declared_media_type = None
            width = height = 0
        elif isinstance(entry, dict):
            data_url = entry.get("data_url")
            declared_media_type = entry.get("media_type")
            width = _coerce_dimension(entry.get("width"))
            height = _coerce_dimension(entry.get("height"))
        else:
            return _reject(f"Image {index}: payload must be a data URL string or an object")

        media_type, payload, error = _parse_data_url(data_url)
        if error:
            return _reject(f"Image {index}: {error}")
        if declared_media_type is not None:
            declared = str(declared_media_type).strip().lower()
            if declared != media_type:
                return _reject(
                    f"Image {index}: media_type {declared!r} does not match the data URL's {media_type!r}"
                )
        if media_type not in allowed:
            return _reject(
                f"Image {index}: unsupported media type {media_type!r} "
                f"(allowed: {', '.join(allowed)})"
            )
        if len(payload) > encoded_bound:
            return _reject(
                f"Image {index}: too large (maximum {max_bytes // (1024 * 1024)} MB)"
            )

        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return _reject(f"Image {index}: payload is not valid base64")
        if not data:
            return _reject(f"Image {index}: payload is empty")
        if len(data) > max_bytes:
            return _reject(
                f"Image {index}: too large ({len(data)} bytes, maximum {max_bytes} bytes)"
            )
        if total_bytes + len(data) > max_total_bytes:
            return _reject(
                "Images too large: "
                f"{(total_bytes + len(data)) // (1024 * 1024)} MB total "
                f"(maximum {max_total_bytes // (1024 * 1024)} MB per message)"
            )

        # ONE id names both layers: the file on disk and the cache entry.  The
        # durable store mints it, because it owns the file name.
        image_id: Optional[str] = None
        if workspace_id:
            try:
                image_id = image_files.save_image(workspace_id, media_type, data)
            except (OSError, ValueError) as exc:
                # Persisting is durability, not the turn itself: the in-memory
                # copy still serves this request, so a disk problem degrades to
                # the previous ephemeral behaviour instead of failing the user's
                # turn.
                logger.warning(
                    "Could not persist pasted image for workspace %r: %s",
                    workspace_id, exc,
                )
        if image_id is None:
            # No workspace (or the write failed): cache-only, still one id.
            image_id = image_files.mint_image_id()
        image_store.put(data, media_type, image_id=image_id)
        stored_ids.append(image_id)
        total_bytes += len(data)
        metadata.append({
            "id": image_id,
            "media_type": media_type,
            "width": width,
            "height": height,
        })

    return metadata, None
