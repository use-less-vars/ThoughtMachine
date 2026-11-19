// @vitest-environment jsdom
// --- MessageBubble.images.test.jsx ---
// Component under test: ../MessageBubble (default export).
//
// The transcript carries image METADATA only ({id, media_type, width, height}).
// The bytes are served by the backend from the durable route
// GET /api/session/{session_id}/image/{image_id} (cookie auth is
// SameSite=Strict, so a plain <img src> authenticates), and the browser's own
// copy of a just-pasted picture arrives as msg.localImagePreviews
// (utils/pastedImagePreviews). Therefore:
//
//   * a local preview, when present, wins (instant paint, no round trip);
//   * otherwise the bubble renders the DURABLE url -- a reload, another tab or
//     an evicted preview must still show the picture;
//   * the placeholder is the LAST resort: no preview AND no durable url, or the
//     durable image actually failed to load (onError).

import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, fireEvent } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import MessageBubble from '../MessageBubble'

afterEach(cleanup)

const SESSION = 'sess-1'
const META_A = { id: 'img-1', media_type: 'image/png', width: 40, height: 30 }
const META_B = { id: 'img-2', media_type: 'image/jpeg', width: 10, height: 10 }
const LOCAL_A = 'data:image/png;base64,AAAA'
const LOCAL_B = 'data:image/jpeg;base64,BBBB'
const DURABLE_A = `/api/session/${SESSION}/image/img-1`
const DURABLE_B = `/api/session/${SESSION}/image/img-2`

function userMsg(extra = {}) {
  return { role: 'user', content: 'look at this', ...extra }
}

function renderMsg(msg, sessionId = SESSION) {
  return render(<MessageBubble msg={msg} index={0} sessionId={sessionId} />)
}

function thumbs(container) {
  return container.querySelectorAll('[data-testid="message-image-thumb"]')
}

function placeholders(container) {
  return container.querySelectorAll('[data-testid="message-image-placeholder"]')
}

describe('MessageBubble attached images', () => {
  it('renders thumbnails from the browser-local previews', () => {
    const { container } = renderMsg(
      userMsg({ images: [META_A, META_B], localImagePreviews: [LOCAL_A, LOCAL_B] })
    )

    const rendered = thumbs(container)
    expect(rendered).toHaveLength(2)
    // The local copy wins over the durable route: no round trip for a picture
    // the browser already holds.
    expect(rendered[0]).toHaveAttribute('src', LOCAL_A)
    expect(rendered[1]).toHaveAttribute('src', LOCAL_B)
    expect(placeholders(container)).toHaveLength(0)
    // The text is still rendered alongside the images.
    expect(container.querySelector('.message-text')).toHaveTextContent('look at this')
  })

  it('renders from the DURABLE route when no local preview exists (reload, other tab)', () => {
    const { container } = renderMsg(userMsg({ images: [META_A, META_B] }))

    const rendered = thumbs(container)
    expect(rendered).toHaveLength(2)
    expect(rendered[0]).toHaveAttribute('src', DURABLE_A)
    expect(rendered[1]).toHaveAttribute('src', DURABLE_B)
    // This is the bug the durable route fixes: no placeholder just because the
    // browser-local copy is gone.
    expect(placeholders(container)).toHaveLength(0)
  })

  it('reuses a local preview across repeated renders of the same echo', () => {
    const msg = userMsg({ images: [META_A], localImagePreviews: [LOCAL_A] })
    const { container, rerender } = renderMsg(msg)
    expect(thumbs(container)[0]).toHaveAttribute('src', LOCAL_A)

    // The backend re-broadcasts the history: a fresh row object for the same
    // message must still paint, not fall back and not vanish.
    rerender(<MessageBubble msg={{ ...msg }} index={0} sessionId={SESSION} />)
    expect(thumbs(container)).toHaveLength(1)
    expect(thumbs(container)[0]).toHaveAttribute('src', LOCAL_A)
    expect(placeholders(container)).toHaveLength(0)
  })

  it('mixes a local preview and a durable url on a partially matched message', () => {
    const { container } = renderMsg(
      userMsg({ images: [META_A, META_B], localImagePreviews: [LOCAL_A, null] })
    )

    const rendered = thumbs(container)
    expect(rendered).toHaveLength(2)
    expect(rendered[0]).toHaveAttribute('src', LOCAL_A)
    expect(rendered[1]).toHaveAttribute('src', DURABLE_B)
    expect(placeholders(container)).toHaveLength(0)
  })

  it('shows a placeholder only when there is neither a preview nor a durable url', () => {
    // No session id: nothing can be addressed, so nothing can be rendered.
    const { container } = renderMsg(userMsg({ images: [META_A] }), null)

    expect(placeholders(container)).toHaveLength(1)
    expect(thumbs(container)).toHaveLength(0)
    expect(container.querySelector('.message-image-placeholder')).toHaveAttribute(
      'title',
      'image/png — image unavailable'
    )
  })

  it('shows the placeholder only AFTER the durable image fails to load', () => {
    const { container } = renderMsg(userMsg({ images: [META_A] }))

    const rendered = thumbs(container)
    expect(rendered).toHaveLength(1)
    expect(placeholders(container)).toHaveLength(0)

    fireEvent.error(rendered[0])

    expect(thumbs(container)).toHaveLength(0)
    expect(placeholders(container)).toHaveLength(1)
    expect(container.querySelector('.message-image-placeholder')).toHaveAttribute(
      'title',
      'image/png — image unavailable'
    )
  })

  it('renders no image markup for a message without images', () => {
    const { container } = renderMsg(userMsg())

    expect(container.querySelector('.message-images')).toBeNull()
    expect(thumbs(container)).toHaveLength(0)
    expect(placeholders(container)).toHaveLength(0)
  })
})
