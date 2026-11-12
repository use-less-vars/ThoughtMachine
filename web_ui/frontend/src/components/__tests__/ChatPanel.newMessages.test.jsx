// @vitest-environment jsdom
/*
 * ChatPanel.newMessages.test.jsx
 *
 * The two cases the SHARED suite (scrollBehaviour.suite.jsx) already holds both
 * panels to are the scroll POSITION ones: pinned follows the stream, unpinned
 * stays put. This file covers the other half of the ChatPanel contract, the one
 * only the chat list has:
 *
 *   - the 50px AT_BOTTOM_THRESHOLD window is the pin/unpin boundary: a reader
 *     exactly `threshold` px from the bottom still counts as pinned and is
 *     followed; ONE PIXEL further is unpinned and is not moved,
 *   - an append that lands while the reader is scrolled up raises the
 *     "New messages below" offer instead of scrolling them,
 *   - the offer is a real <button> (keyboard reachable) that jumps to the bottom,
 *     re-pins the view and removes itself,
 *   - scrolling back to the bottom dismisses the offer without a click,
 *   - a PREPEND (older history loaded above the fold) is NOT "new messages
 *     below" and must not raise the offer.
 *
 * jsdom performs no layout, so the container's metrics are stubbed and rAF is
 * replaced by a 0ms timer, exactly as the shared suite does — both helpers are
 * imported from it so the two files cannot drift.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChatPanel from '../ChatPanel'
import {
  stubScrollMetrics,
  flushFrames,
  SCROLL_HEIGHT,
  CLIENT_HEIGHT,
} from './scrollBehaviour.suite.jsx'

// Mirrors `AT_BOTTOM_THRESHOLD` in ChatPanel.jsx: the panel's historical ~50px
// "near the bottom" window, handed straight to `useStickToBottom` as `threshold`.
const AT_BOTTOM_THRESHOLD = 50
// scrollTop that puts the viewport exactly at the bottom of the stubbed box.
const BOTTOM_SCROLL_TOP = SCROLL_HEIGHT - CLIENT_HEIGHT
const OFFER = /New messages below/i

const USER = { role: 'user', content: 'why is the sky blue' }
const A1 = { role: 'assistant', content: 'because of rayleigh scattering' }
const A2 = { role: 'assistant', content: 'and short wavelengths scatter most' }
const OLDER = [
  { role: 'user', content: 'older question' },
  { role: 'assistant', content: 'older answer' },
]

const BASE = [USER, A1]
const APPENDED = [USER, A1, A2]
// A PREPEND: two older messages in front, the same tail OBJECT (A1) as BASE.
const PREPENDED = [...OLDER, USER, A1]

class MockResizeObserver {
  constructor(callback) {
    this.callback = callback
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

const panel = (messages) => <ChatPanel messages={messages} />

/**
 * Render the panel, give the layout-less container real metrics, then force the
 * post-stub commit (ChatPanel is memoised, so a fresh-but-equal array is used).
 * Leaves the view mounted at the bottom, i.e. pinned.
 */
async function mountPanel(messages = BASE) {
  const view = render(panel(messages))
  const el = document.querySelector('.chat-panel')
  expect(el).not.toBeNull()
  const metrics = stubScrollMetrics(el)
  view.rerender(panel([...messages]))
  await flushFrames()
  expect(metrics.scrollTop).toBe(metrics.scrollHeight)
  return { view, el, metrics }
}

/** Move the reader to `scrollTop` and let the hook read the new position. */
function scrollTo(el, metrics, scrollTop) {
  act(() => {
    metrics.setScrollTop(scrollTop)
    el.dispatchEvent(new Event('scroll'))
  })
}

const offer = (view) => view.queryByRole('button', { name: OFFER })

describe('ChatPanel — "New messages below" offer', () => {
  beforeEach(() => {
    // jsdom lacks ResizeObserver; the hook constructs one on mount.
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
    // Deterministic double-rAF: each frame is a 0ms macrotask.
    vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(() => cb(0), 0))
    vi.stubGlobal('cancelAnimationFrame', (id) => clearTimeout(id))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('follows an append and raises no offer while pinned', async () => {
    const { view, metrics } = await mountPanel()
    expect(offer(view)).toBeNull()

    metrics.setScrollHeight(metrics.scrollHeight + 100)
    view.rerender(panel(APPENDED))
    await flushFrames()

    expect(metrics.scrollTop).toBe(metrics.scrollHeight)
    expect(offer(view)).toBeNull()
  })

  it('still counts as pinned at exactly the threshold distance, and follows the append', async () => {
    const { view, el, metrics } = await mountPanel()
    // distance-from-bottom == AT_BOTTOM_THRESHOLD, i.e. the inclusive edge.
    scrollTo(el, metrics, BOTTOM_SCROLL_TOP - AT_BOTTOM_THRESHOLD)

    metrics.setScrollHeight(metrics.scrollHeight + 100)
    view.rerender(panel(APPENDED))
    await flushFrames()

    expect(metrics.scrollTop).toBe(metrics.scrollHeight)
    expect(offer(view)).toBeNull()
  })

  it('is unpinned one pixel past the threshold: no scroll, offer raised', async () => {
    const { view, el, metrics } = await mountPanel()
    // distance-from-bottom == AT_BOTTOM_THRESHOLD + 1, i.e. just outside.
    scrollTo(el, metrics, BOTTOM_SCROLL_TOP - AT_BOTTOM_THRESHOLD - 1)

    metrics.setScrollHeight(metrics.scrollHeight + 100)
    view.rerender(panel(APPENDED))
    await flushFrames()

    // The reader is not yanked to the bottom...
    expect(metrics.scrollTop).toBe(BOTTOM_SCROLL_TOP - AT_BOTTOM_THRESHOLD - 1)
    // ...they are offered the jump instead.
    expect(offer(view)).not.toBeNull()
  })

  it('does not raise the offer for a prepend (older history loaded above the fold)', async () => {
    const { view, el, metrics } = await mountPanel()
    scrollTo(el, metrics, 120)

    // Same tail object, higher count: an append by count alone, a prepend in fact.
    view.rerender(panel(PREPENDED))
    await flushFrames()

    expect(metrics.scrollTop).toBe(120)
    expect(offer(view)).toBeNull()
  })

  it('jumps to the bottom, re-pins and dismisses itself on click', async () => {
    const user = userEvent.setup()
    const { view, el, metrics } = await mountPanel()
    scrollTo(el, metrics, 120)
    view.rerender(panel(APPENDED))
    await flushFrames()

    const btn = offer(view)
    expect(btn).not.toBeNull()

    await user.click(btn)
    await flushFrames()

    expect(metrics.scrollTop).toBe(metrics.scrollHeight)
    expect(offer(view)).toBeNull()
  })

  it('is keyboard reachable and activates with Enter', async () => {
    const user = userEvent.setup()
    const { view, el, metrics } = await mountPanel()
    scrollTo(el, metrics, 120)
    view.rerender(panel(APPENDED))
    await flushFrames()

    const btn = offer(view)
    expect(btn).not.toBeNull()
    btn.focus()
    expect(document.activeElement).toBe(btn)

    await user.keyboard('{Enter}')
    await flushFrames()

    expect(metrics.scrollTop).toBe(metrics.scrollHeight)
    expect(offer(view)).toBeNull()
  })

  it('dismisses itself once the reader scrolls back to the bottom', async () => {
    const { view, el, metrics } = await mountPanel()
    scrollTo(el, metrics, 120)
    view.rerender(panel(APPENDED))
    await flushFrames()
    expect(offer(view)).not.toBeNull()

    // No click: the position authority pins the view and clears the offer.
    scrollTo(el, metrics, metrics.scrollHeight)
    await flushFrames()

    expect(offer(view)).toBeNull()
  })
})
