// @vitest-environment jsdom
/*
 * useStickToBottom.test.jsx — the hook that owns stick-to-bottom scrolling.
 *
 * The panel-level suites (components/__tests__/*.scroll.test.jsx) check the two
 * panels through their DOM. This file checks the hook directly, because two of
 * its mechanisms are invisible from a panel: the anchor restore that keeps a
 * scrolled-up reader's text still while something ABOVE them grows, and the
 * programmatic-scroll flag. jsdom performs no layout, so every case below stubs
 * scrollHeight / clientHeight / scrollTop (and, for the anchor cases, the
 * bounding rects) — without that, an all-zero element looks "at the bottom" and
 * nothing would ever be exercised.
 *
 * LIMITATION (documented, not hidden): the programmatic flag's OUTCOME is not
 * black-box distinguishable — a scroll that ends up away from the bottom falls
 * through to the position-based decision whether or not the flag is armed, so
 * the user can always unpin. The tests below therefore assert the observable
 * facts only: the clear-timer leaves nothing pending, and the user's scroll is
 * never swallowed. `PROGRAMMATIC_CLEAR_MS` (150) is module-private, so it is
 * mirrored here rather than exported from the hook for the test's benefit.
 */
import React, { useRef } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import { useStickToBottom } from '../useStickToBottom'

const SCROLL_HEIGHT = 1000
const CLIENT_HEIGHT = 300
const PROGRAMMATIC_CLEAR_MS = 150

let roInstances = []

class MockResizeObserver {
  constructor(callback) {
    this.callback = callback
    this.observe = vi.fn()
    this.unobserve = vi.fn()
    this.disconnect = vi.fn()
    roInstances.push(this)
  }
}

// The hook's return value, captured on render so the tests can read `pinned`.
let api = null

function Host({ version = 0, bare = false }) {
  const containerRef = useRef(null)
  const stick = useStickToBottom({ containerRef })
  api = stick
  return (
    <div data-testid="container" ref={containerRef} onScroll={stick.handleScroll}>
      {bare ? null : <div data-testid="anchor" />}
      {bare ? null : <div data-testid="content">v{version}</div>}
    </div>
  )
}

const container = () => document.querySelector('[data-testid="container"]')
const anchor = () => document.querySelector('[data-testid="anchor"]')

/** Real-looking scroll metrics on a layout-less jsdom element. */
function stubMetrics(el, { scrollHeight = SCROLL_HEIGHT } = {}) {
  let scrollTop = 0
  let sh = scrollHeight
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => sh })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT_HEIGHT })
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (value) => {
      scrollTop = value
    },
  })
  return {
    get scrollTop() {
      return scrollTop
    },
    get scrollHeight() {
      return sh
    },
    setScrollTop(value) {
      scrollTop = value
    },
    setScrollHeight(value) {
      sh = value
    },
  }
}

function rect(top) {
  return { top, bottom: top + 40, left: 0, right: 100, width: 100, height: 40, x: 0, y: top, toJSON() {} }
}

/**
 * Model a real scroll box: the container's own rect stays put, while a child's
 * rect moves with the content — i.e. `anchorTop = docTop - scrollTop`. This is
 * what lets the anchor assertion be real rather than trivially true: if the hook
 * fails to compensate, the anchor's offset within the container changes.
 */
function stubScrollingRects(el, anchorEl, { containerTop = 0, anchorDocTop = 100 } = {}) {
  const state = { containerTop, anchorDocTop }
  el.getBoundingClientRect = () => rect(state.containerTop)
  anchorEl.getBoundingClientRect = () => rect(state.anchorDocTop - (el.scrollTop || 0))
  return state
}

function scrollAway(el, metrics, to = 120) {
  act(() => {
    metrics.setScrollTop(to)
    el.dispatchEvent(new Event('scroll'))
  })
}

beforeEach(() => {
  roInstances = []
  api = null
  vi.stubGlobal('ResizeObserver', MockResizeObserver)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('useStickToBottom — glue while pinned', () => {
  it('glues to the bottom on every commit, and follows the content growth', () => {
    const view = render(<Host version={0} />)
    const el = container()
    const metrics = stubMetrics(el)

    // A commit AFTER the metrics exist (the mount commit saw an all-zero box).
    view.rerender(<Host version={1} />)
    expect(metrics.scrollTop).toBe(SCROLL_HEIGHT)

    metrics.setScrollHeight(1400)
    view.rerender(<Host version={2} />)
    // Followed the growth instead of staying at the old bottom.
    expect(metrics.scrollTop).toBe(1400)
  })
})

describe('useStickToBottom — unpinning', () => {
  it('a scroll away from the bottom unpins, and growth then leaves the viewport alone', () => {
    const view = render(<Host version={0} />)
    const el = container()
    const metrics = stubMetrics(el)
    view.rerender(<Host version={1} />)
    expect(api.pinned).toBe(true)

    scrollAway(el, metrics, 120)
    expect(api.pinned).toBe(false)

    metrics.setScrollHeight(1400)
    view.rerender(<Host version={2} />)
    expect(metrics.scrollTop).toBe(120)
  })

  it('a wheel-up unpins before any scroll event arrives', () => {
    const view = render(<Host version={0} />)
    const el = container()
    stubMetrics(el)
    view.rerender(<Host version={1} />)
    expect(api.pinned).toBe(true)

    act(() => {
      // Negative deltaY = revealing earlier content.
      const wheel = new Event('wheel')
      Object.defineProperty(wheel, 'deltaY', { value: -1 })
      el.dispatchEvent(wheel)
    })
    expect(api.pinned).toBe(false)
  })
})

describe('useStickToBottom — anchor restoration while unpinned', () => {
  it('shifts scrollTop by exactly the anchor delta, keeping it fixed, then re-snapshots', () => {
    const view = render(<Host version={0} />)
    const el = container()
    const metrics = stubMetrics(el)
    const rects = stubScrollingRects(el, anchor(), { containerTop: 0, anchorDocTop: 220 })

    view.rerender(<Host version={1} />)
    expect(metrics.scrollTop).toBe(SCROLL_HEIGHT)

    // Unpin at scrollTop 120: the anchor sits 100px below the container's top.
    scrollAway(el, metrics, 120)
    expect(api.pinned).toBe(false)
    expect(anchor().getBoundingClientRect().top - el.getBoundingClientRect().top).toBe(100)

    // Something ABOVE the reader grew by 120px, then a commit landed.
    rects.anchorDocTop += 120
    view.rerender(<Host version={2} />)

    // scrollTop moved by exactly the delta, so the anchor's position within the
    // container is unchanged — the text under the reader's eyes did not jump.
    expect(metrics.scrollTop).toBe(120 + 120)
    expect(anchor().getBoundingClientRect().top - el.getBoundingClientRect().top).toBe(100)

    // The offset was re-snapshotted: an unchanged commit must not move again.
    view.rerender(<Host version={3} />)
    expect(metrics.scrollTop).toBe(240)
  })

  it('does nothing when the rects are all zero (the jsdom default)', () => {
    const view = render(<Host version={0} />)
    const el = container()
    const metrics = stubMetrics(el)
    view.rerender(<Host version={1} />)

    // No getBoundingClientRect stubs: every rect is 0, so _offsetWithin() is 0
    // and the delta is 0 — the hook must leave scrollTop alone instead of
    // jumping to a guessed position.
    scrollAway(el, metrics, 120)
    metrics.setScrollHeight(1400)
    view.rerender(<Host version={2} />)
    expect(metrics.scrollTop).toBe(120)
  })

  it('does nothing when there is no anchor element at all', () => {
    const view = render(<Host version={0} bare />)
    const el = container()
    const metrics = stubMetrics(el)
    view.rerender(<Host version={1} bare />)

    scrollAway(el, metrics, 120)
    metrics.setScrollHeight(1400)
    view.rerender(<Host version={2} bare />)
    expect(metrics.scrollTop).toBe(120)
  })
})

describe('useStickToBottom — programmatic-scroll flag', () => {
  it('clears itself on the timer, leaving nothing pending', () => {
    const view = render(<Host version={0} />)
    const el = container()
    const metrics = stubMetrics(el)

    vi.useFakeTimers()
    // Glue arms the guard: scrollTop is written and a clear-timer scheduled.
    act(() => {
      view.rerender(<Host version={1} />)
    })
    expect(metrics.scrollTop).toBe(SCROLL_HEIGHT)
    expect(vi.getTimerCount()).toBe(1)

    // Past PROGRAMMATIC_CLEAR_MS (150): the flag is gone and no timer lingers.
    act(() => {
      vi.advanceTimersByTime(PROGRAMMATIC_CLEAR_MS + 50)
    })
    expect(vi.getTimerCount()).toBe(0)

    // ...so the user's next scroll still unpins.
    act(() => {
      metrics.setScrollTop(120)
      el.dispatchEvent(new Event('scroll'))
    })
    expect(api.pinned).toBe(false)
  })

  it('never swallows a user scroll that arrives while the guard is still armed', () => {
    const view = render(<Host version={0} />)
    const el = container()
    const metrics = stubMetrics(el)

    vi.useFakeTimers()
    act(() => {
      view.rerender(<Host version={1} />)
    })
    // Flag armed (timer pending), and the user scrolls up anyway: a clear
    // departure from the bottom can only be user intent, so it must unpin.
    expect(vi.getTimerCount()).toBe(1)
    act(() => {
      metrics.setScrollTop(120)
      el.dispatchEvent(new Event('scroll'))
    })
    expect(api.pinned).toBe(false)
  })
})

describe('useStickToBottom — teardown', () => {
  it('disconnects the ResizeObserver and removes the listeners on unmount', () => {
    const { unmount } = render(<Host version={0} />)
    const el = container()

    expect(roInstances).toHaveLength(1)
    expect(roInstances[0].observe).toHaveBeenCalled()

    const removeListener = vi.spyOn(el, 'removeEventListener')
    unmount()

    expect(roInstances[0].disconnect).toHaveBeenCalled()
    const removed = removeListener.mock.calls.map((call) => call[0]).sort()
    expect(removed).toEqual(['touchmove', 'touchstart', 'wheel'])
  })
})
