/*
 * scrollBehaviour.suite.jsx — the SHARED stick-to-bottom regression suite.
 *
 * `ChatPanel` and `WorkerOutputPanel` used to carry two near-identical inline
 * copies of the stick-to-bottom logic, and the copies drifted (the worker panel
 * never got the streaming fixes). Both panels now take the behaviour from
 * `hooks/useStickToBottom`, and both prove it with THIS suite: each panel owns a
 * thin `*.scroll.test.jsx` that mounts the suite with its own harness, so the
 * two panels cannot drift again — either panel losing a case fails its file.
 *
 * The suite is deliberately harness-agnostic. What differs between the panels is
 * HOW they glue and how their content grows, so the harness supplies that:
 *
 *   element(step)      the panel element at growth `step` (0 = baseline).
 *   bumpElement(step,n) same CONTENT, but an element that is guaranteed to force
 *                      one extra commit. Both panels are memoised, so a plain
 *                      re-render with unchanged props BAILS OUT — and the
 *                      baseline commit must land AFTER the scroll metrics exist
 *                      (the mount commit ran against jsdom's layout-less
 *                      element, where nothing can be measured).
 *   container()        the scrollable element (the one owning scrollTop).
 *   growthDelta        px the stubbed scrollHeight grows by before the growth
 *                      commit. ChatPanel keeps 0: its OWN messages-signature
 *                      effect decides whether to follow the stream, and growing
 *                      the box would let the hook's per-commit glue mask a
 *                      broken detector. WorkerOutputPanel uses +400: it has no
 *                      such effect, the hook's per-commit glue is its only
 *                      writer, so the case is meaningless without real growth.
 *   skip               { [caseId]: reason } — registers `it.skip` with the reason
 *                      in the title instead of silently dropping the case.
 *   beforeEach         optional extra fixture reset (store/localStorage).
 *
 * jsdom performs no layout, so the container's metrics are stubbed
 * (scrollHeight 1000 / clientHeight 300 / writable scrollTop) and rAF is
 * replaced by a 0ms timer, which makes the panels' double-rAF glue deterministic.
 *
 * NOTE: this filename is `*.suite.jsx`, not `*.test.jsx`, so vitest's default
 * include glob does not collect it as a test file of its own.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'

export const SCROLL_HEIGHT = 1000
export const CLIENT_HEIGHT = 300

class MockResizeObserver {
  constructor(callback) {
    this.callback = callback
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

/**
 * Give the (layout-less) scroll container real-looking metrics.
 * Returns a handle: { scrollTop, scrollHeight, setScrollHeight(v), setScrollTop(v) }.
 */
export function stubScrollMetrics(el) {
  let scrollTop = 0
  let scrollHeight = SCROLL_HEIGHT
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight })
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
      return scrollHeight
    },
    setScrollTop(value) {
      scrollTop = value
    },
    setScrollHeight(value) {
      scrollHeight = value
    },
  }
}

/** Flush the panels' nested requestAnimationFrame callbacks (stubbed to 0ms). */
export async function flushFrames(rounds = 3) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
  }
}

/** Case registrars: plain `it`, or `it.skip` carrying the documented reason. */
function caseRegistrar(skip, id) {
  const reason = skip[id]
  if (!reason) return it
  return (title, fn) => it.skip(title + ' [SKIPPED: ' + reason + ']', fn)
}

/**
 * Register the shared cases for one panel.
 *
 * @param {object} harness see the file header.
 */
export function registerScrollBehaviour(harness) {
  const {
    name,
    element,
    bumpElement = null,
    container,
    growthDelta = 0,
    skip = {},
    beforeEach: harnessBeforeEach = null,
  } = harness

  const CASES = {
    pinned: 'keeps the view glued to the bottom while content grows (pinned)',
    unpinned: 'leaves a scrolled-up reader exactly where they are (unpinned)',
  }

  // Force one commit without changing what is on screen.
  const bump = (view, step, n) =>
    view.rerender(bumpElement ? bumpElement(step, n) : element(step))

  describe(name + ' — shared stick-to-bottom behaviour', () => {
    beforeEach(() => {
      if (harnessBeforeEach) harnessBeforeEach()
      // jsdom lacks ResizeObserver; the panels/hook construct one on mount.
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

    caseRegistrar(skip, 'pinned')(CASES.pinned, async () => {
      const view = render(element(0))
      const el = container()
      expect(el).not.toBeNull()
      const metrics = stubScrollMetrics(el)

      // Baseline commit now that the metrics exist.
      bump(view, 0, 1)
      await flushFrames()
      // A pinned panel sits at the bottom — this is the pre-existing assertion
      // (scrollTop === scrollHeight) the panels have always been held to.
      expect(metrics.scrollTop).toBe(metrics.scrollHeight)

      // Content grew (a streamed delta for ChatPanel, a new worker event for the
      // worker panel) and the reader was at the bottom, so the view must follow.
      metrics.setScrollHeight(metrics.scrollHeight + growthDelta)
      view.rerender(element(1))
      await flushFrames()
      // Pre-fix this froze at the old bottom (or was reset to 0) instead.
      expect(metrics.scrollTop).toBe(metrics.scrollHeight)
    })

    caseRegistrar(skip, 'unpinned')(CASES.unpinned, async () => {
      const view = render(element(0))
      const el = container()
      expect(el).not.toBeNull()
      const metrics = stubScrollMetrics(el)

      bump(view, 0, 1)
      await flushFrames()
      expect(metrics.scrollTop).toBe(metrics.scrollHeight)

      // The user scrolls up to read earlier content.
      act(() => {
        metrics.setScrollTop(120)
        el.dispatchEvent(new Event('scroll'))
      })

      // Growth arriving while the reader is up in history must not move them:
      // neither yanked to the bottom nor reset to the top.
      metrics.setScrollHeight(metrics.scrollHeight + growthDelta)
      view.rerender(element(1))
      await flushFrames()
      expect(metrics.scrollTop).toBe(120)
    })
  })
}
