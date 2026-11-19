// --- pastedImagePreviews.test.js ---
// Module under test: ../pastedImagePreviews.
//
// The registry is the browser's own copy of a pasted picture: the server echo
// carries metadata only ({id, media_type, width, height}) and serves the bytes
// itself from GET /api/session/{session_id}/image/{image_id} (durable route,
// rendered by MessageBubble as the fallback). Properties pinned here:
//
//   * matching prefers the exact query text the image was sent with, then falls
//     back to media type + dimensions;
//   * a preview is keyed on the SERVER image id and is REUSABLE: the backend
//     re-broadcasts the full history on every conversation_version bump, so the
//     same preview must satisfy every echo of the same message (the old
//     drain-once FIFO returned it exactly once and then lost the thumbnail);
//   * the by-id map is BOUNDED (MAX_REMEMBERED_PREVIEWS): oldest evicted first,
//     and an evicted object URL is revoked so the drop does not leak the blob;
//   * clearPendingImagePreviews() revokes and empties both structures;
//   * consumeImagePreview() is the explicit, opted-in destructive read;
//   * the input array is never mutated.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  MAX_PENDING_PREVIEWS,
  MAX_REMEMBERED_PREVIEWS,
  attachLocalImagePreviews,
  clearPendingImagePreviews,
  consumeImagePreview,
  pendingPreviewCount,
  registerPastedImagePreviews,
  rememberedPreviewCount,
} from '../pastedImagePreviews'

// jsdom does not implement object URLs; the registry only needs the revoker.
const originalRevoke = URL.revokeObjectURL

beforeEach(() => {
  URL.revokeObjectURL = vi.fn()
  clearPendingImagePreviews()
  URL.revokeObjectURL.mockClear()
})

afterEach(() => {
  if (originalRevoke === undefined) delete URL.revokeObjectURL
  else URL.revokeObjectURL = originalRevoke
})

const PNG = 'data:image/png;base64,AAAA'
const PNG2 = 'data:image/png;base64,BBBB'
const JPEG = 'data:image/jpeg;base64,CCCC'

function userMsg(images, content = 'hi') {
  return { role: 'user', content, images }
}

describe('pastedImagePreviews registry', () => {
  it('attaches the previews for a round trip', () => {
    registerPastedImagePreviews(
      [
        { dataUrl: PNG, mediaType: 'image/png', width: 0, height: 0 },
        { dataUrl: JPEG, mediaType: 'image/jpeg', width: 0, height: 0 },
      ],
      'hi'
    )
    expect(pendingPreviewCount()).toBe(2)

    const messages = [
      { role: 'user', content: 'older turn', images: [{ id: 'x', media_type: 'image/webp' }] },
      userMsg([
        { id: 'img-1', media_type: 'image/png', width: 40, height: 30 },
        { id: 'img-2', media_type: 'image/jpeg', width: 8, height: 8 },
      ]),
    ]
    const out = attachLocalImagePreviews(messages)

    expect(out).toHaveLength(2)
    expect(out[1].localImagePreviews).toEqual([PNG, JPEG])
    // Untouched messages and the input array itself are left alone.
    expect(out[0]).toBe(messages[0])
    expect(messages[1].localImagePreviews).toBeUndefined()
    // The registrations are gone from the pending queue, but the previews are
    // now remembered by server image id.
    expect(pendingPreviewCount()).toBe(0)
    expect(rememberedPreviewCount()).toBe(2)
  })

  it('REUSES a preview for every echo of the same image id (no longer one-shot)', () => {
    registerPastedImagePreviews([{ dataUrl: PNG, mediaType: 'image/png' }], 'hi')
    const images = [{ id: 'img-1', media_type: 'image/png' }]

    // First echo (user_query), then the re-broadcasts the backend sends for
    // tool_call / tool_result / turn / agent_responded.
    const first = attachLocalImagePreviews([userMsg(images)])
    expect(first[0].localImagePreviews).toEqual([PNG])

    for (let echo = 0; echo < 4; echo += 1) {
      const again = attachLocalImagePreviews([userMsg(images)])
      expect(again[0].localImagePreviews).toEqual([PNG])
    }
    // Still remembered: nothing was consumed.
    expect(rememberedPreviewCount()).toBe(1)
  })

  it('prefers the preview registered with the same query text', () => {
    registerPastedImagePreviews([{ dataUrl: PNG, mediaType: 'image/png' }], 'first ask')
    registerPastedImagePreviews([{ dataUrl: PNG2, mediaType: 'image/png' }], 'second ask')

    const out = attachLocalImagePreviews([
      userMsg([{ id: 'img-2', media_type: 'image/png' }], 'second ask'),
    ])

    expect(out[0].localImagePreviews).toEqual([PNG2])
    expect(pendingPreviewCount()).toBe(1) // the 'first ask' preview is still pending
  })

  it('requires the media type to agree', () => {
    registerPastedImagePreviews([{ dataUrl: PNG, mediaType: 'image/png' }], 'hi')
    const messages = [userMsg([{ id: 'img-1', media_type: 'image/jpeg' }], 'hi')]

    expect(attachLocalImagePreviews(messages)).toBe(messages)
    expect(pendingPreviewCount()).toBe(1)
    expect(rememberedPreviewCount()).toBe(0)
  })

  it('ignores non-user messages and messages without images', () => {
    registerPastedImagePreviews([{ dataUrl: PNG, mediaType: 'image/png' }], 'hi')
    const messages = [
      { role: 'assistant', content: 'hi', images: [{ id: 'a', media_type: 'image/png' }] },
      { role: 'user', content: 'hi' },
    ]

    expect(attachLocalImagePreviews(messages)).toBe(messages)
    expect(pendingPreviewCount()).toBe(1)
  })

  it(`caps pending previews at MAX_PENDING_PREVIEWS (${MAX_PENDING_PREVIEWS})`, () => {
    const many = Array.from({ length: MAX_PENDING_PREVIEWS + 4 }, (_, i) => ({
      dataUrl: `data:image/png;base64,${i}`,
      mediaType: 'image/png',
    }))
    registerPastedImagePreviews(many, 'hi')

    expect(pendingPreviewCount()).toBe(MAX_PENDING_PREVIEWS)
  })

  it(`caps the by-id map at MAX_REMEMBERED_PREVIEWS (${MAX_REMEMBERED_PREVIEWS}) and evicts oldest first`, () => {
    const total = MAX_REMEMBERED_PREVIEWS + 3
    for (let i = 0; i < total; i += 1) {
      registerPastedImagePreviews([{ dataUrl: `data:image/png;base64,${i}`, mediaType: 'image/png' }], 'hi')
      attachLocalImagePreviews([userMsg([{ id: `img-${i}`, media_type: 'image/png' }])])
    }

    // Bounded: the map never grows past its cap...
    expect(rememberedPreviewCount()).toBe(MAX_REMEMBERED_PREVIEWS)

    // ...the three OLDEST ids were evicted...
    for (const evicted of ['img-0', 'img-1', 'img-2']) {
      const messages = [userMsg([{ id: evicted, media_type: 'image/png' }])]
      expect(attachLocalImagePreviews(messages)).toBe(messages)
    }

    // ...and the newest id still resolves.
    const newest = attachLocalImagePreviews([
      userMsg([{ id: `img-${total - 1}`, media_type: 'image/png' }]),
    ])
    expect(newest[0].localImagePreviews).toEqual([`data:image/png;base64,${total - 1}`])
  })

  it('revokes the object URL of an entry evicted by the cap', () => {
    const total = MAX_REMEMBERED_PREVIEWS + 2
    for (let i = 0; i < total; i += 1) {
      registerPastedImagePreviews([{ dataUrl: `blob:pasted-${i}`, mediaType: 'image/png' }], 'hi')
      attachLocalImagePreviews([userMsg([{ id: `img-${i}`, media_type: 'image/png' }])])
    }

    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
    expect(URL.revokeObjectURL).toHaveBeenNthCalledWith(1, 'blob:pasted-0')
    expect(URL.revokeObjectURL).toHaveBeenNthCalledWith(2, 'blob:pasted-1')
  })

  it('clear() revokes every outstanding object URL and empties both structures', () => {
    registerPastedImagePreviews(
      [
        { dataUrl: 'blob:pending-a', mediaType: 'image/png' },
        { dataUrl: 'blob:pending-b', mediaType: 'image/png' },
        { dataUrl: PNG, mediaType: 'image/png' }, // data URL: nothing to revoke
      ],
      'hi'
    )
    attachLocalImagePreviews([userMsg([{ id: 'img-1', media_type: 'image/png' }])])
    expect(pendingPreviewCount()).toBe(2)
    expect(rememberedPreviewCount()).toBe(1)

    clearPendingImagePreviews()

    expect(pendingPreviewCount()).toBe(0)
    expect(rememberedPreviewCount()).toBe(0)
    // Two object URLs were outstanding: the one bound to img-1 and the one
    // still pending. The data URL holds no blob, so nothing is revoked for it.
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:pending-a')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:pending-b')
    // Nothing left to claim: the message keeps no local preview.
    const messages = [userMsg([{ id: 'img-1', media_type: 'image/png' }])]
    expect(attachLocalImagePreviews(messages)).toBe(messages)
  })

  it('consumeImagePreview() is the explicit destructive read', () => {
    registerPastedImagePreviews([{ dataUrl: PNG, mediaType: 'image/png' }], 'hi')
    attachLocalImagePreviews([userMsg([{ id: 'img-1', media_type: 'image/png' }])])

    expect(consumeImagePreview('img-1')).toBe(PNG)
    expect(consumeImagePreview('img-1')).toBeNull()
    expect(rememberedPreviewCount()).toBe(0)
  })

  it('ignores registrations that are not preview URLs', () => {
    registerPastedImagePreviews(
      [
        { dataUrl: 'https://example.invalid/a.png', mediaType: 'image/png' },
        { dataUrl: PNG, mediaType: 'image/png' },
        null,
      ],
      'hi'
    )
    expect(pendingPreviewCount()).toBe(1)
  })

  it('tolerates empty input', () => {
    expect(attachLocalImagePreviews([])).toEqual([])
    expect(attachLocalImagePreviews(undefined)).toBeUndefined()
    registerPastedImagePreviews([], 'hi')
    registerPastedImagePreviews(undefined, 'hi')
    expect(pendingPreviewCount()).toBe(0)
  })
})
