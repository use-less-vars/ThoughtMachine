// @vitest-environment jsdom
// --- MessageBubble.images.test.jsx ---
// Component under test: ../MessageBubble (default export).
//
// The transcript carries image METADATA only. The backend decodes each pasted
// data URL once (web_ui/backend/image_ingest.py), keeps the bytes in its
// process-wide ephemeral image_store and hands the rest of the stack
// {id, media_type, width, height} -- bridge._normalize_for_frontend copies user
// messages verbatim, so those dicts reach the bubble with no bytes attached.
// The only surviving copy of the pixels is the browser's own, re-attached as
// msg.localImagePreviews (utils/pastedImagePreviews) when the server echoes the
// message back. Therefore:

import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import MessageBubble from '../MessageBubble'

afterEach(cleanup)

const META_A = { id: 'img-1', media_type: 'image/png', width: 40, height: 30 }
const META_B = { id: 'img-2', media_type: 'image/jpeg', width: 10, height: 10 }
const LOCAL_A = 'data:image/png;base64,AAAA'
const LOCAL_B = 'data:image/jpeg;base64,BBBB'

function userMsg(extra = {}) {
  return { role: 'user', content: 'look at this', ...extra }
}

function renderMsg(msg) {
  return render(<MessageBubble msg={msg} index={0} />)
}

describe('MessageBubble attached images', () => {
  it('renders thumbnails from the browser-local previews', () => {
    const { container } = renderMsg(
      userMsg({ images: [META_A, META_B], localImagePreviews: [LOCAL_A, LOCAL_B] })
    )

    const thumbs = container.querySelectorAll('[data-testid="message-image-thumb"]')
    expect(thumbs).toHaveLength(2)
    expect(thumbs[0]).toHaveAttribute('src', LOCAL_A)
    expect(thumbs[1]).toHaveAttribute('src', LOCAL_B)
    expect(container.querySelectorAll('[data-testid="message-image-placeholder"]')).toHaveLength(0)
    // The text is still rendered alongside the images.
    expect(container.querySelector('.message-text')).toHaveTextContent('look at this')
  })

  it('falls back to a placeholder when no local copy is left (reload, other tab)', () => {
    const { container } = renderMsg(userMsg({ images: [META_A] }))

    expect(container.querySelectorAll('[data-testid="message-image-placeholder"]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-testid="message-image-thumb"]')).toHaveLength(0)
    expect(container.querySelector('.message-image-placeholder')).toHaveAttribute(
      'title',
      'image/png — preview not available in this tab'
    )
  })

  it('mixes a thumbnail and a placeholder for a partially matched message', () => {
    const { container } = renderMsg(
      userMsg({ images: [META_A, META_B], localImagePreviews: [LOCAL_A, null] })
    )

    expect(container.querySelectorAll('[data-testid="message-image-thumb"]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-testid="message-image-placeholder"]')).toHaveLength(1)
  })

  it('renders no image markup for a message without images', () => {
    const { container } = renderMsg(userMsg())

    expect(container.querySelector('.message-images')).toBeNull()
    expect(container.querySelectorAll('[data-testid="message-image-thumb"]')).toHaveLength(0)
    expect(container.querySelectorAll('[data-testid="message-image-placeholder"]')).toHaveLength(0)
  })
})
