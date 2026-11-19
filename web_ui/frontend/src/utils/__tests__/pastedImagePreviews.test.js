// --- pastedImagePreviews.test.js ---
// Module under test: ../pastedImagePreviews.
//
// The registry is the bridge between the ONLY copy of a pasted image's bytes
// (the browser's) and the server echo, which carries metadata only
// ({id, media_type, width, height}: the backend decodes the data URL once and
// stores the bytes in its ephemeral image_store). Properties pinned here:
//
//   * matching prefers the exact query text the image was sent with, then falls
//     back to media type + dimensions;
//   * a claimed preview is REMOVED, so a second paste of the same media type can
//     never show the first paste's pixels;
//   * the registry is bounded (MAX_PENDING_PREVIEWS) so a long-lived tab cannot
//     accumulate data URLs forever;
//   * the input array is never mutated.

import { describe, it, expect, beforeEach } from 'vitest'
import {
  MAX_PENDING_PREVIEWS,
  attachLocalImagePreviews,
  clearPendingImagePreviews,
  pendingPreviewCount,
  registerPastedImagePreviews,
} from '../pastedImagePreviews'

beforeEach(() => clearPendingImagePreviews())

const PNG = 'data:image/png;base64,AAAA'
const PNG2 = 'data:image/png;base64,BBBB'
const JPEG = 'data:image/jpeg;base64,CCCC'

function userMsg(images, content = 'hi') {
  return { role: 'user', content, images }
}

describe('pastedImagePreviews registry', () => {
  it('attaches the previews for a round trip and drains them', () => {
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
    expect(pendingPreviewCount()).toBe(0)
  })

  it('is a one-shot: a second echo gets no previews', () => {
    registerPastedImagePreviews([{ dataUrl: PNG, mediaType: 'image/png' }], 'hi')
    const images = [{ id: 'img-1', media_type: 'image/png' }]

    const first = attachLocalImagePreviews([userMsg(images)])
    expect(first[0].localImagePreviews).toEqual([PNG])

    const messages = [userMsg(images)]
    const second = attachLocalImagePreviews(messages)
    expect(second).toBe(messages) // unchanged array identity: nothing matched
    expect(second[0].localImagePreviews).toBeUndefined()
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

  it('ignores registrations that are not data URLs', () => {
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
