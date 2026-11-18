/*
 * pastedImagePreviews.js
 *
 * Where a pasted image's BYTES can still be seen after it has been sent.
 *
 * The server deliberately keeps only metadata: ``image_ingest.ingest_incoming_images``
 * decodes the data URL once, stores the bytes in the process-wide ephemeral
 * ``agent.core.image_store`` and hands the rest of the stack
 * ``{id, media_type, width, height}``. Those dicts are what
 * ``conversation_changed`` carries back (bridge ``_normalize_for_frontend``
 * copies user messages verbatim), so the transcript has NO way to render a
 * thumbnail on its own.
 *
 * The only surviving copy of the picture is the one the browser read out of the
 * clipboard. This module is that copy's home: a bounded, module-level registry
 * filled when QueryBar submits, and drained when the server echoes the user
 * message back.
 *
 * Matching rules (deliberate, and the reason this is a FIFO queue rather than a
 * plain id map): the server mints the id, so the frontend cannot know it in
 * advance. A pending preview is claimed by the first message whose
 * ``media_type`` matches, preferring an exact query-text match (the text the
 * pasted image was sent with), and requiring width/height agreement when both
 * sides know them. Claimed entries are REMOVED, so a second paste of the same
 * media type can never show the first paste's pixels by accident.
 *
 * Nothing here is persisted: entries live in memory only, are capped at
 * MAX_PENDING_PREVIEWS, and are dropped once matched.
 */

/** Cap on unclaimed previews. Two full turns (2 x 4 images) is the useful
 *  window; beyond that the pictures are stale memory, not a pending echo. */
export const MAX_PENDING_PREVIEWS = 8

/** @type {Array<{dataUrl: string, mediaType: string, width: number, height: number, query: string}>} */
let pending = []

/**
 * Record the previews for a turn that is about to be sent.
 *
 * @param {Array<{dataUrl: string, mediaType: string, width?: number, height?: number}>} images
 * @param {string} query the text sent with them (the server substitutes
 *   '[Image attached]' for an empty query, so callers pass the text the server
 *   will store).
 */
export function registerPastedImagePreviews(images, query) {
  if (!Array.isArray(images) || images.length === 0) return
  const text = typeof query === 'string' ? query : ''
  for (const image of images) {
    if (!image || typeof image.dataUrl !== 'string' || !image.dataUrl.startsWith('data:')) continue
    pending.push({
      dataUrl: image.dataUrl,
      mediaType: String(image.mediaType || '').toLowerCase(),
      width: Number(image.width) || 0,
      height: Number(image.height) || 0,
      query: text,
    })
  }
  if (pending.length > MAX_PENDING_PREVIEWS) {
    pending = pending.slice(pending.length - MAX_PENDING_PREVIEWS)
  }
}

/** Drop every pending preview (tests, and abandoning the tab's draft). */
export function clearPendingImagePreviews() {
  pending = []
}

/** Number of unclaimed previews. Test/diagnostic helper. */
export function pendingPreviewCount() {
  return pending.length
}

function dimsAgree(entry, image) {
  const w = Number(image && image.width) || 0
  const h = Number(image && image.height) || 0
  if (!w && !h) return true // server did not report dimensions: media type decides
  if (!entry.width && !entry.height) return true // our own probe never finished: same
  return entry.width === w && entry.height === h
}

function takeAt(index) {
  return pending.splice(index, 1)[0]
}

function claim(image, content, requireSameQuery) {
  const mediaType = String((image && image.media_type) || '').toLowerCase()
  for (let i = 0; i < pending.length; i += 1) {
    const entry = pending[i]
    if (entry.mediaType !== mediaType) continue
    if (requireSameQuery && entry.query !== content) continue
    if (!dimsAgree(entry, image)) continue
    return takeAt(i)
  }
  return null
}

/**
 * Attach `localImagePreviews` (an array of data URLs, same order/length as
 * `images`, `null` where no local copy is left) to every user message that the
 * server reported images for.
 *
 * The input array and its message objects are never mutated; the module-level
 * registry IS drained, so call this exactly once per server echo (the
 * `conversation_changed` handler is the only caller).
 *
 * @param {Array<object>} messages
 * @returns {Array<object>} the same array when nothing matched, else a copy
 */
export function attachLocalImagePreviews(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return messages
  let out = messages
  for (let i = 0; i < messages.length; i += 1) {
    const msg = messages[i]
    if (!msg || msg.role !== 'user') continue
    const images = Array.isArray(msg.images) ? msg.images : []
    if (images.length === 0) continue
    if (Array.isArray(msg.localImagePreviews)) continue
    const content = typeof msg.content === 'string' ? msg.content : ''
    const previews = images.map((image) => {
      // First choice: a preview pasted with this very query text.
      const exact = claim(image, content, true)
      const entry = exact || claim(image, content, false)
      return entry ? entry.dataUrl : null
    })
    if (previews.every((p) => p === null)) continue // nothing local left: placeholder
    if (out === messages) out = messages.slice()
    out[i] = { ...msg, localImagePreviews: previews }
  }
  return out
}
