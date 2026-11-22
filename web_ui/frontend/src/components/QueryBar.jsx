/*
 * QueryBar.jsx
 *
 * Text input + a Run/Pause toggle plus separate Stop/Resume controls.
 *
 * Button logic:
 *   status      Button                 Command sent
 *   ───────────────────────────────────────────────────────────────────────
 *   RUNNING     ⏸ Pause                pause_session
 *   PAUSING     ⏸ Pausing… (disabled)  —
 *   otherwise   ▶ Run                  start_session (fresh session) /
 *                                      continue_session(query) (loaded session)
 *   not IDLE /  ⏹ Stop                 stop_session
 *   not connecting
 *   PAUSED      ▶ Resume               resume_session
 *
 * The Run button is disabled while the session is connecting and, when IDLE,
 * until the query is non-empty OR at least one image is attached. Enter
 * (without Shift) triggers the same toggle from IDLE / WAITING_FOR_USER /
 * PAUSED.
 *
 * Pasted images (clipboard paste only, this slice):
 *   * a paste carrying image files is intercepted (Ctrl+V of text still works,
 *     untouched), validated against the same limits the backend enforces
 *     (web_ui/backend/image_ingest.py) so the user gets the rejection before a
 *     round trip, and shown as removable chips;
 *   * submitting sends the images as a TOP-LEVEL `images` list on the command
 *     envelope — never inside `config`, which is extra='forbid' — as
 *     `{data_url, media_type, width, height}`;
 *   * an image-only submit is legal: the server substitutes
 *     '[Image attached]' for the empty text.
 *
 * Props:
 *   sendCommand(command, payload)
 *   status, isRunning, config, mode, sessionId, sessionReady, workspaceId
 *
 * `workspaceId` is the workspace this tab belongs to (the loaded session's own
 * workspace, else the route's). A fresh session has no id yet, so it is the
 * only thing that can tell the server where a FIRST-turn pasted image belongs;
 * without it such an image stays cache-only and is gone after a reload
 * (backend: `_resolve_ingest_workspace_id` in web_ui/backend/server.py).
 */

import React, { useState, useRef } from 'react'
import { registerPastedImagePreviews } from '../utils/pastedImagePreviews'

/* Mirror of web_ui/backend/image_ingest.py. Kept in sync deliberately: the
 * frontend rejects locally with the same numbers the backend would reject
 * remotely, so the operator never loses a paste to a server round trip. */
const MAX_IMAGES = 4
const MAX_IMAGE_BYTES = 6 * 1024 * 1024           // per image, DECODED bytes
const MAX_TOTAL_IMAGE_BYTES = 10 * 1024 * 1024    // per turn, DECODED bytes
const ALLOWED_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/webp']
/* Same placeholder the server stores when a turn carries images but no text. */
const IMAGE_ONLY_QUERY_PLACEHOLDER = '[Image attached]'

function formatBytes(bytes) {
  if (!bytes) return '0 B'
  if (bytes >= 1024 * 1024) return `${Math.round((bytes / (1024 * 1024)) * 10) / 10} MB`
  return `${Math.round(bytes / 1024)} KB`
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    if (typeof FileReader === 'undefined') {
      reject(new Error('FileReader unavailable'))
      return
    }
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('read failed'))
    reader.readAsDataURL(file)
  })
}

/* Best-effort intrinsic size for the chip/metadata. Resolves only on a real
 * decode; an environment that never loads the data URL simply leaves 0x0,
 * which the backend coerces to "unknown" (_coerce_dimension). */
function probeImageDimensions(dataUrl) {
  return new Promise((resolve) => {
    if (typeof Image === 'undefined') {
      resolve({ width: 0, height: 0 })
      return
    }
    try {
      const img = new Image()
      img.onload = () => resolve({
        width: img.naturalWidth || img.width || 0,
        height: img.naturalHeight || img.height || 0,
      })
      img.onerror = () => resolve({ width: 0, height: 0 })
      img.src = dataUrl
    } catch (err) {
      resolve({ width: 0, height: 0 })
    }
  })
}

function QueryBar({ sendCommand, status, isRunning, config, mode, sessionId, sessionReady, workspaceId = null }) {
  const [query, setQuery] = useState('')
  const textareaRef = useRef(null)
  /* Attached images: {id, dataUrl, mediaType, size, width, height}. The ref
   * mirrors the state so a paste that awaits FileReader still sees the list as
   * it is NOW, not as it was when the handler was created. */
  const [images, setImages] = useState([])
  const imagesRef = useRef([])
  const nextImageIdRef = useRef(1)
  const [imageError, setImageError] = useState(null)

  const isIdle = status === 'IDLE'
  const isBusy = status === 'RUNNING'
  const isPaused = status === 'PAUSED'
  const isPausing = status === 'PAUSING'
  const isWaiting = status === 'WAITING_FOR_USER'
  const isConnecting = !sessionReady && !sessionId // fresh tab waiting for session_loaded

  const setImageList = (next) => {
    imagesRef.current = next
    setImages(next)
  }

  /* The envelope's images list. data_url is the ONLY place base64 travels, and
   * it dies at the backend boundary (image_ingest decodes it once). */
  const buildImageEnvelope = () => imagesRef.current.map((img) => ({
    data_url: img.dataUrl,
    media_type: img.mediaType,
    width: img.width || 0,
    height: img.height || 0,
  }))

  const clearDraft = () => {
    setQuery('')
    setImageList([])
    setImageError(null)
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  const addImageFile = async (file) => {
    const mediaType = String(file.type || '').toLowerCase()
    if (!ALLOWED_MEDIA_TYPES.includes(mediaType)) {
      setImageError(`⚠ Unsupported image type: ${mediaType || 'unknown'}. Paste a PNG, JPEG or WebP image.`)
      return
    }
    const size = Number(file.size) || 0
    if (size > MAX_IMAGE_BYTES) {
      setImageError(`⚠ Image is too large (${formatBytes(size)}). The limit is ${formatBytes(MAX_IMAGE_BYTES)} per image.`)
      return
    }
    if (imagesRef.current.length >= MAX_IMAGES) {
      setImageError(`⚠ You can attach at most ${MAX_IMAGES} images per message.`)
      return
    }
    const attachedBytes = imagesRef.current.reduce((sum, img) => sum + (img.size || 0), 0)
    if (attachedBytes + size > MAX_TOTAL_IMAGE_BYTES) {
      setImageError(`⚠ Attached images would exceed the ${formatBytes(MAX_TOTAL_IMAGE_BYTES)} total limit. Remove one first.`)
      return
    }
    let dataUrl
    try {
      dataUrl = await readFileAsDataUrl(file)
    } catch (err) {
      setImageError('⚠ The pasted image could not be read.')
      return
    }
    if (typeof dataUrl !== 'string' || !dataUrl.startsWith('data:')) {
      setImageError('⚠ The pasted image could not be read.')
      return
    }
    /* Re-check after the await: a second paste may have landed meanwhile. */
    if (imagesRef.current.length >= MAX_IMAGES) {
      setImageError(`⚠ You can attach at most ${MAX_IMAGES} images per message.`)
      return
    }
    const entry = {
      id: `paste-${nextImageIdRef.current}`,
      dataUrl,
      mediaType,
      size,
      width: 0,
      height: 0,
    }
    nextImageIdRef.current += 1
    setImageList([...imagesRef.current, entry])
    probeImageDimensions(dataUrl).then(({ width, height }) => {
      if (!width && !height) return
      setImageList(imagesRef.current.map((img) => (
        img.id === entry.id ? { ...img, width, height } : img
      )))
    })
  }

  const handlePaste = (e) => {
    const clipboard = e.clipboardData
    if (!clipboard) return
    const items = Array.from(clipboard.items || [])
    const files = items
      .filter((item) => item && item.kind === 'file')
      .map((item) => (typeof item.getAsFile === 'function' ? item.getAsFile() : null))
      .filter(Boolean)
    if (files.length === 0) return // plain-text paste: default behaviour is untouched
    e.preventDefault()
    setImageError(null)
    for (const file of files) {
      addImageFile(file)
    }
  }

  const handleRemoveImage = (id) => {
    setImageList(imagesRef.current.filter((img) => img.id !== id))
    setImageError(null)
  }

  const handleRun = () => {
    const trimmedQuery = query.trim()
    const hasImages = imagesRef.current.length > 0
    if (!trimmedQuery && !hasImages) return
    /* Hand the browser's own copy of the bytes to the preview registry: the
     * server echo carries metadata only, so this is the only way the sent
     * message can render thumbnails. Keyed by the text the server will store. */
    registerPastedImagePreviews(imagesRef.current, trimmedQuery || IMAGE_ONLY_QUERY_PLACEHOLDER)
    const imagesEnvelope = hasImages ? { images: buildImageEnvelope() } : {}
    console.log(`[DEBUG QueryBar handleRun] sessionId=${sessionId}, status=${status}, isRunning=${isRunning}, query="${trimmedQuery.substring(0, 50)}", images=${imagesRef.current.length}`)
    if (sessionId) {
      // Loaded session — continue with existing context, passing config
      sendCommand('continue_session', {
        query: trimmedQuery,
        session_id: sessionId,
        config: { ...(config ?? {}), mode },
        ...imagesEnvelope
      })
    } else {
      // Fresh start — create new agent session
      sendCommand('start_session', {
        query: trimmedQuery,
        config: { ...(config ?? {}), mode },
        /* A brand-new session cannot be named by id, so name the workspace it
         * is being started in: the server validates the id against the
         * registry before writing any pasted image into it. */
        ...(workspaceId ? { workspace_id: workspaceId } : {}),
        ...imagesEnvelope
      })
    }
    clearDraft()  // Clear input after sending
  }

  const handleToggle = () => {
    if (isBusy) {
      sendCommand('pause_session', {})
    } else if (isPaused) {
      const trimmedQuery = query.trim()
      const hasImages = imagesRef.current.length > 0
      if (hasImages) {
        registerPastedImagePreviews(imagesRef.current, trimmedQuery || IMAGE_ONLY_QUERY_PLACEHOLDER)
      }
      sendCommand('continue_session', {
        query: trimmedQuery,
        ...(hasImages ? { images: buildImageEnvelope() } : {}),
      })
      clearDraft()
    } else {
      handleRun()
    }
  }

  const handleStop = () => {
    sendCommand('stop_session', {})
  }

  const handleResume = () => {
    sendCommand('resume_session', {})
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && (isIdle || isWaiting || isPaused)) {
      e.preventDefault()
      handleToggle()
    }
  }

  // Debounced auto-resize using requestAnimationFrame to avoid layout thrashing
  const resizeRafRef = useRef(null)
  const handleResize = (e) => {
    if (resizeRafRef.current) return  // coalesce multiple events into one frame
    resizeRafRef.current = requestAnimationFrame(() => {
      resizeRafRef.current = null
      const el = textareaRef.current
      if (!el) return
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 200) + 'px'
    })
  }

  return (
    <>
      <div className="query-bar">
        {images.length > 0 && (
          <div className="query-image-strip" data-testid="query-image-strip">
            {images.map((img, i) => (
              <div className="query-image-chip" key={img.id}>
                <img
                  className="query-image-thumb"
                  src={img.dataUrl}
                  alt={`Attached image ${i + 1}`}
                  title={img.mediaType}
                  data-testid="query-image-thumb"
                />
                <button
                  type="button"
                  className="query-image-remove"
                  aria-label={`Remove attached image ${i + 1}`}
                  title="Remove image"
                  onClick={() => handleRemoveImage(img.id)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          className="query-input"
          placeholder="Enter your query…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onInput={handleResize}
          disabled={false}  /* Always writable — buttons control what's allowed */
          rows={1}
        />
        <div className="query-buttons">
          {/* Toggle Run/Pause — always visible. Stop/Resume are separate controls. */}
          {isBusy ? (
            <button className="btn btn-pause" onClick={handleToggle}>
              ⏸ Pause
            </button>
          ) : isPausing ? (
              <button className="btn btn-pause" disabled>
                ⏸ Pausing…
              </button>
          ) : (
            <button
              className="btn btn-run"
              onClick={handleToggle}
              disabled={(isConnecting) || (!query.trim() && images.length === 0 && isIdle)}
            >
              {isConnecting ? 'Connecting…' : '▶ Run'}
            </button>
          )}
          {/* Stop — disabled while idle/connecting. Resume — enabled only when PAUSED. */}
          <button className="btn btn-stop" onClick={handleStop} disabled={isIdle || isConnecting}>
            ⏹ Stop
          </button>
          <button className="btn btn-resume" onClick={handleResume} disabled={!isPaused}>
            ▶ Resume
          </button>
        </div>
      </div>
      {imageError && (
        <div className="query-image-error" role="alert" data-testid="query-image-error">
          {imageError}
        </div>
      )}
    </>
  )
}

export default React.memo(QueryBar)
