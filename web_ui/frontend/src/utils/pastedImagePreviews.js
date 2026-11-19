/*
 * pastedImagePreviews.js
 *
 * Where a pasted image's BYTES can be seen while the transcript is on screen.
 *
 * The server keeps the bytes (durable, per workspace, served by
 * ``GET /api/session/{session_id}/image/{image_id}``) and the transcript itself
 * carries metadata only: ``image_ingest.ingest_incoming_images`` decodes the
 * data URL once and hands the rest of the stack
 * ``{id, media_type, width, height}``. Those dicts are what
 * ``conversation_changed`` carries back (bridge ``_normalize_for_frontend``
 * copies user messages verbatim).
 *
 * This module is the browser's own copy: a bounded registry filled when QueryBar
 * submits, consulted when the server echoes the message back. It exists so a
 * freshly pasted picture paints instantly instead of after a round trip to the
 * durable route (and it keeps working if that route is slow).
 *
 * Two structures, both bounded:
 *
 *   * ``pending`` — registrations that have not met a server row yet, capped at
 *     MAX_PENDING_PREVIEWS and flushed oldest-first.
 *   * ``remembered`` — previews keyed by the SERVER image id, capped at
 *     MAX_REMEMBERED_PREVIEWS and evicted oldest-first. THIS is what makes a
 *     preview reusable: the backend re-broadcasts the FULL history on every
 *     ``conversation_version`` bump, so the same image id is claimed several
 *     times per turn. A lookup here never removes the entry, so every echo of
 *     the same message renders the same picture.
 *
 * Matching rules: the server mints the id, so the frontend cannot know it in
 * advance. An unmatched registration is bound to an image id by media type,
 * preferring an exact query-text match (the text the pasted image was sent
 * with), and requiring width/height agreement when both sides know them. Once
 * bound, the id is the key from then on — no further guessing.
 *
 * Nothing here is persisted, and nothing grows without bound: an evicted or
 * cleared entry that holds an object URL (``blob:``) is revoked with
 * ``URL.revokeObjectURL`` so dropping the reference does not leak the blob.
 */

/** Cap on registrations that have not met a server row yet. Two full turns
 *  (2 x 4 images) is the useful window; beyond that the pictures are stale
 *  memory, not a pending echo. */
export const MAX_PENDING_PREVIEWS = 8

/** Cap on remembered (by-server-id) previews. The server allows 4 images per
 *  turn, so 64 covers 16 pasted turns of scrollback — far more than fits on
 *  screen — while keeping the ceiling hard and the eviction (and its revoke)
 *  actually reachable. */
export const MAX_REMEMBERED_PREVIEWS = 64

/**
 * @typedef {{url: string, revocable: boolean, mediaType: string, width: number,
 *   height: number, query: string}} PreviewEntry
 */

/** Registrations waiting for their first server row. FIFO. @type {PreviewEntry[]} */
let pending = []

/** Server image id -> entry. Insertion order is the eviction order. @type {Map<string, PreviewEntry>} */
let remembered = new Map()

function isPreviewUrl(value) {
  return typeof value === 'string' && (value.startsWith('data:') || value.startsWith('blob:'))
}

function isObjectUrl(value) {
  return typeof value === 'string' && value.startsWith('blob:')
}

/** Release the blob behind an entry, if it owns one. */
function revokeEntry(entry) {
  if (!entry || !entry.revocable) return
  if (typeof URL === 'undefined' || typeof URL.revokeObjectURL !== 'function') return
  try {
    URL.revokeObjectURL(entry.url)
  } catch (e) {
    /* already revoked / not an object URL: nothing to do */
  }
}

function imageIdOf(image) {
  const raw = image ? image.id : null
  if (typeof raw === 'string' && raw.length > 0) return raw
  if (typeof raw === 'number' && Number.isFinite(raw)) return String(raw)
  return null
}

/** Evict oldest-first until the by-id map is inside its cap. */
function trimRemembered() {
  while (remembered.size > MAX_REMEMBERED_PREVIEWS) {
    const oldestId = remembered.keys().next().value
    const evicted = remembered.get(oldestId)
    remembered.delete(oldestId)
    // An evicted blob URL is gone for good — the bubble falls back to the
    // durable route for that message, so the thumbnail survives.
    revokeEntry(evicted)
  }
}

function remember(imageId, entry) {
  if (!imageId) return
  remembered.delete(imageId) // re-insert so insertion order stays "oldest first"
  remembered.set(imageId, entry)
  trimRemembered()
}

/**
 * Record the previews for a turn that is about to be sent.
 *
 * @param {Array<{dataUrl?: string, objectUrl?: string, mediaType: string, width?: number, height?: number}>} images
 * @param {string} query the text sent with them (the server substitutes
 *   '[Image attached]' for an empty query, so callers pass the text the server
 *   will store).
 */
export function registerPastedImagePreviews(images, query) {
  if (!Array.isArray(images) || images.length === 0) return
  const text = typeof query === 'string' ? query : ''
  for (const image of images) {
    if (!image) continue
    const url = typeof image.dataUrl === 'string'
      ? image.dataUrl
      : (typeof image.objectUrl === 'string' ? image.objectUrl : null)
    if (!isPreviewUrl(url)) continue
    pending.push({
      url,
      revocable: isObjectUrl(url),
      mediaType: String(image.mediaType || '').toLowerCase(),
      width: Number(image.width) || 0,
      height: Number(image.height) || 0,
      query: text,
    })
  }
  if (pending.length > MAX_PENDING_PREVIEWS) {
    const dropped = pending.slice(0, pending.length - MAX_PENDING_PREVIEWS)
    pending = pending.slice(pending.length - MAX_PENDING_PREVIEWS)
    dropped.forEach(revokeEntry)
  }
}

/**
 * Drop every preview — pending AND remembered — revoking the object URLs the
 * registry owns. Call on a session switch, on a tab reset, and in tests.
 */
export function clearPendingImagePreviews() {
  pending.forEach(revokeEntry)
  remembered.forEach(revokeEntry)
  pending = []
  remembered = new Map()
}

/** Number of registrations still waiting for a server row. Test/diagnostic helper. */
export function pendingPreviewCount() {
  return pending.length
}

/** Number of previews remembered by server image id. Test/diagnostic helper. */
export function rememberedPreviewCount() {
  return remembered.size
}

/**
 * Explicit DESTRUCTIVE read: take the preview for one server image id out of the
 * registry and hand it to the caller. The default path
 * (`attachLocalImagePreviews`) never does this — it leaves every entry in place
 * so repeated echoes keep rendering. Ownership of the value passes to the
 * caller, so an object URL is NOT revoked here.
 *
 * @param {string|number} imageId
 * @returns {string|null} the preview URL, or null when nothing is remembered
 */
export function consumeImagePreview(imageId) {
  const id = typeof imageId === 'number' ? String(imageId) : imageId
  if (typeof id !== 'string' || id.length === 0) return null
  const entry = remembered.get(id)
  if (!entry) return null
  remembered.delete(id)
  return entry.url
}

function dimsAgree(entry, image) {
  const w = Number(image && image.width) || 0
  const h = Number(image && image.height) || 0
  if (!w && !h) return true // server did not report dimensions: media type decides
  if (!entry.width && !entry.height) return true // our own probe never finished: same
  return entry.width === w && entry.height === h
}

/**
 * Find the preview for one server image row WITHOUT consuming it.
 *
 * Keyed by the server image id: once an id is bound, every later claim of the
 * same id returns the same entry (that is the whole point — the backend
 * re-broadcasts the history several times per turn).
 */
function claim(image, content, requireSameQuery) {
  const id = imageIdOf(image)
  if (id) {
    const bound = remembered.get(id)
    if (bound) return bound
  }
  const mediaType = String((image && image.media_type) || '').toLowerCase()
  for (let i = 0; i < pending.length; i += 1) {
    const entry = pending[i]
    if (entry.mediaType !== mediaType) continue
    if (requireSameQuery && entry.query !== content) continue
    if (!dimsAgree(entry, image)) continue
    if (id) {
      // The registration is now bound to the server id: it stops competing for
      // future rows, and the by-id map serves every later echo.
      pending.splice(i, 1)
      remember(id, entry)
    }
    // No server id: leave the registration in place, so a re-broadcast of the
    // same row matches it again instead of coming back empty.
    return entry
  }
  return null
}

/**
 * Attach `localImagePreviews` (an array of preview URLs, same order/length as
 * `images`, `null` where the registry has nothing) to every user message that
 * the server reported images for.
 *
 * NON-DESTRUCTIVE: the input array and its message objects are never mutated,
 * and the registry is never drained — calling this on every server echo is
 * expected, and the same preview is returned for every echo of the same image
 * id.
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
      return entry ? entry.url : null
    })
    if (previews.every((p) => p === null)) continue // nothing local: durable route
    if (out === messages) out = messages.slice()
    out[i] = { ...msg, localImagePreviews: previews }
  }
  return out
}
