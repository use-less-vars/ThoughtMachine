// @vitest-environment jsdom
// --- QueryBar.images.test.jsx ---
// Component under test: ../QueryBar (default export).
//
// Pins the paste-images contract added in this slice. The numbers here are a
// deliberate MIRROR of web_ui/backend/image_ingest.py (4 images, 6 MiB per
// image decoded, 10 MiB total, png/jpeg/webp); the frontend rejects locally so
// the operator learns about a bad paste without a round trip, and the wire
// format is the backend's: a TOP-LEVEL `images` list on the command envelope,
// sibling of `query`/`config` (config is extra='forbid', so base64 must never
// travel inside it), each entry `{data_url, media_type, width, height}`.
//
// What is NOT covered here (see the slice report): the 10 MiB per-turn total
// limit (needs two multi-megabyte pastes in one turn), the post-await count
// re-check when several files arrive in ONE paste event, and the dimension
// probe -- jsdom never fires `load` on an <img>, so width/height legitimately
// stay 0 and are sent as such.

import React from 'react'
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import QueryBar from '../QueryBar'
import {
  clearPendingImagePreviews,
  pendingPreviewCount,
} from '../../utils/pastedImagePreviews'

afterEach(cleanup)
beforeEach(() => clearPendingImagePreviews())

// ── Helpers ────────────────────────────────────────────────────────────────

function pngFile(bytes = [1, 2, 3], name = 'shot.png', type = 'image/png') {
  return new File([new Uint8Array(bytes)], name, { type })
}

function setup(overrides = {}) {
  const sendCommand = vi.fn()
  render(
    <QueryBar
      sendCommand={sendCommand}
      status="IDLE"
      isRunning={false}
      config={{}}
      mode="default"
      sessionId="sess-1"
      sessionReady
      {...overrides}
    />
  )
  return { sendCommand }
}

function input() {
  return screen.getByPlaceholderText(/Enter your query/)
}

/* jsdom has no real clipboard, and FireEvent's ClipboardEvent plumbing differs
 * between versions: build a plain bubbling Event and hang clipboardData off it,
 * which is exactly what React's synthetic onPaste reads. */
function pasteItems(items) {
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clipboardData', { value: { items } })
  fireEvent(input(), event)
  return event
}

function fileItem(file) {
  return { kind: 'file', type: file.type, getAsFile: () => file }
}

function chips() {
  return document.querySelectorAll('[data-testid="query-image-thumb"]')
}

async function pasteOne(file) {
  pasteItems([fileItem(file)])
  await waitFor(() => expect(chips().length).toBeGreaterThan(0))
}

// ==========================================================================
// 1. Paste → chip
// ==========================================================================

describe('QueryBar pasted images — attaching', () => {
  it('renders a removable chip for a pasted PNG', async () => {
    setup()
    await pasteOne(pngFile())

    expect(screen.getByTestId('query-image-strip')).toBeInTheDocument()
    expect(chips()).toHaveLength(1)
    expect(chips()[0].getAttribute('src')).toMatch(/^data:image\/png;base64,/)
    expect(screen.queryByTestId('query-image-error')).not.toBeInTheDocument()
  })

  it('leaves a plain-text paste untouched (no chip, default not prevented)', () => {
    setup()
    const event = pasteItems([{ kind: 'string', type: 'text/plain', getAsFile: () => null }])

    expect(event.defaultPrevented).toBe(false)
    expect(chips()).toHaveLength(0)
    expect(screen.queryByTestId('query-image-strip')).not.toBeInTheDocument()
  })

  it('drops a chip when its remove button is clicked', async () => {
    setup()
    await pasteOne(pngFile())

    fireEvent.click(screen.getByLabelText('Remove attached image 1'))

    expect(chips()).toHaveLength(0)
    expect(screen.queryByTestId('query-image-strip')).not.toBeInTheDocument()
  })
})

// ==========================================================================
// 2. Local rejection — the backend's limits, enforced before the round trip
// ==========================================================================

describe('QueryBar pasted images — rejection', () => {
  it('rejects an unsupported media type', async () => {
    setup()
    pasteItems([fileItem(pngFile([1], 'anim.gif', 'image/gif'))])

    const error = await screen.findByTestId('query-image-error')
    expect(error).toHaveTextContent(/Unsupported image type: image\/gif/)
    expect(error).toHaveAttribute('role', 'alert')
    expect(chips()).toHaveLength(0)
  })

  it('rejects an image over the 6 MiB per-image limit', async () => {
    setup()
    const oversized = new File([new Uint8Array(6 * 1024 * 1024 + 1)], 'huge.png', {
      type: 'image/png',
    })
    pasteItems([fileItem(oversized)])

    const error = await screen.findByTestId('query-image-error')
    expect(error).toHaveTextContent(/too large/)
    expect(chips()).toHaveLength(0)
  })

  it('rejects the fifth image (server cap is 4)', async () => {
    setup()
    for (let i = 0; i < 4; i += 1) {
      pasteItems([fileItem(pngFile([i + 1], `shot-${i}.png`))])
      await waitFor(() => expect(chips()).toHaveLength(i + 1))
    }

    pasteItems([fileItem(pngFile([99], 'fifth.png'))])

    const error = await screen.findByTestId('query-image-error')
    expect(error).toHaveTextContent(/at most 4 images/)
    expect(chips()).toHaveLength(4)
  })
})

// ==========================================================================
// 3. Wire format
// ==========================================================================

describe('QueryBar pasted images — submit envelope', () => {
  it('sends images as a top-level list, never inside config', async () => {
    const { sendCommand } = setup()
    await pasteOne(pngFile([1, 2, 3]))
    fireEvent.change(input(), { target: { value: 'what is this?' } })
    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    expect(sendCommand).toHaveBeenCalledTimes(1)
    const [command, payload] = sendCommand.mock.calls[0]
    expect(command).toBe('continue_session')
    expect(payload.query).toBe('what is this?')
    expect(payload.session_id).toBe('sess-1')
    expect(payload.images).toEqual([
      {
        data_url: 'data:image/png;base64,AQID',
        media_type: 'image/png',
        width: 0,
        height: 0,
      },
    ])
    expect(payload.config).not.toHaveProperty('images')
    expect(payload.config.mode).toBe('default')
    // The chip is cleared once the turn is sent.
    expect(chips()).toHaveLength(0)
    // The browser's own copy of the bytes was handed to the preview registry
    // (the server echo carries metadata only).
    expect(pendingPreviewCount()).toBe(1)
  })

  it('allows an image-only submit (server substitutes the placeholder)', async () => {
    const { sendCommand } = setup()
    await pasteOne(pngFile())

    const run = screen.getByRole('button', { name: /Run/ })
    expect(run).toBeEnabled()
    fireEvent.click(run)

    const [command, payload] = sendCommand.mock.calls[0]
    expect(command).toBe('continue_session')
    expect(payload.query).toBe('')
    expect(payload.images).toHaveLength(1)
  })

  it('sends no images key at all for a text-only turn', () => {
    const { sendCommand } = setup()
    fireEvent.change(input(), { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    const [, payload] = sendCommand.mock.calls[0]
    expect(payload).not.toHaveProperty('images')
  })

  it('puts images on start_session payloads too (fresh session)', async () => {
    const { sendCommand } = setup({ sessionId: null })
    await pasteOne(pngFile())
    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    const [command, payload] = sendCommand.mock.calls[0]
    expect(command).toBe('start_session')
    expect(payload.images).toHaveLength(1)
  })

  it('does nothing while the Run button is disabled (empty text, no images)', () => {
    const { sendCommand } = setup()
    const run = screen.getByRole('button', { name: /Run/ })
    expect(run).toBeDisabled()
    fireEvent.click(run)
    expect(sendCommand).not.toHaveBeenCalled()
  })
})
