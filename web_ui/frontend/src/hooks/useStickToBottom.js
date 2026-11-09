/*
 * useStickToBottom.js
 *
 * SINGLE SOURCE OF TRUTH for "stick to bottom" scrolling in the Web UI.
 *
 * `ChatPanel` and `WorkerOutputPanel` must both take their stick-to-bottom
 * behaviour from this hook.  They used to carry two near-identical inline
 * copies of the logic, and the copies drifted: ChatPanel gained the
 * reasoning-phase fixes and the worker panel never did, so a worker stream was
 * left behind.  The substring-based auto-scroll trigger that used to live in
 * `SessionTab.jsx` (deciding to scroll by inspecting message text) is being
 * retired in favour of this hook.
 *
 * Every non-obvious guard below exists because the naive version failed:
 *
 *   - The ResizeObserver watches the CONTENT (the child that grows), not the
 *     scroll container.  The container's own box rarely changes when a message,
 *     a code block or an image lands, so observing the container misses the
 *     async-expansion case entirely; observing the content catches it.
 *
 *   - User intent is tracked explicitly, because scroll POSITION alone cannot
 *     distinguish "the user scrolled up" from "we scrolled and layout shifted
 *     underneath us".  Reaching the bottom re-pins, moving away unpins, and
 *     wheel/touchmove raise the unpin earlier than a throttled scroll event
 *     would.
 *
 *   - A programmatic scroll is flagged, but the flag is also cleared on a timer.
 *     Suppressing every scroll event while our own scroll is in flight makes it
 *     impossible to scroll up during a stream (we glue on every commit, so the
 *     flag is almost always set); trusting every event instead makes the glue
 *     look like user intent and the view unpins itself.  See `handleScroll`.
 *
 *   - While the user is UNPINNED (reading history), growth above the viewport
 *     must not move what they are reading: an anchor element is snapshotted and
 *     its offset re-measured after the growth, and scrollTop is shifted by the
 *     delta.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

// Distance (px) from the bottom that still counts as "at the bottom".  Wide
// enough that a reader parked near the bottom edge is not thrown out of "pinned"
// by a one-line append, narrow enough that clearly read-up history unpins.
export const STICK_TOLERANCE_PX = 32

// How long a scroll WE caused keeps the "programmatic" flag set.  The flag is
// normally cleared by the scroll event itself, but a scroll assignment that does
// not actually move (already at the bottom) may never fire an event at all —
// without this timeout the stale flag would swallow the user's NEXT scroll and
// they could not unpin.  That is the classic bug in this area.
const PROGRAMMATIC_CLEAR_MS = 150

function _distanceFromBottom(el) {
  return (el.scrollHeight || 0) - (el.scrollTop || 0) - (el.clientHeight || 0)
}

// Offset of `node` from the top edge of `container`, measured in the same
// coordinate space (viewport-relative rects).  jsdom performs no layout, so
// every rect is 0 and this returns 0: the anchor restore then computes delta 0
// and leaves scrollTop alone instead of jumping.
function _offsetWithin(container, node) {
  if (!node || typeof container.getBoundingClientRect !== 'function') return 0
  if (typeof node.getBoundingClientRect !== 'function') return 0
  return node.getBoundingClientRect().top - container.getBoundingClientRect().top
}

/**
 * Owns stick-to-bottom scrolling for one scrollable container.
 *
 * All options are passed as a single plain object so a panel can hand over the
 * refs it already owns:
 *
 *   const stick = useStickToBottom({ containerRef: chatRef })
 *   <div ref={chatRef} onScroll={stick.handleScroll}>
 *
 * @param {object}   options
 * @param {object}   options.containerRef   REQUIRED. The scrollable element —
 *        the node that owns scrollTop / scrollHeight / clientHeight.
 * @param {object}   [options.contentRef]   The child that GROWS (the
 *        ResizeObserver target).  Optional: both panels render their rows as
 *        direct children of the scroll container, so when it is omitted the
 *        container itself is observed and the per-commit pass still sticks.
 * @param {boolean}  [options.enabled=true] When false the hook detaches and does
 *        nothing (panel hidden, or a panel that returns early before rendering
 *        its scroll div at all).
 * @param {number}   [options.threshold]    px window that still counts as "at
 *        the bottom"; defaults to STICK_TOLERANCE_PX.
 * @param {string}   [options.anchorSelector] CSS selector for the element kept
 *        visually stable while unpinned.  Default: the first element child of
 *        the observed content.  Its absolute position does not matter — only the
 *        change in its offset does.
 * @param {function} [options.onPinnedChange] Called with the new boolean whenever
 *        stickiness flips, for panels that render a "new events" affordance.
 *
 * @returns {{
 *   pinned: boolean,
 *   pinnedRef: {current: boolean},
 *   isAtBottomRef: {current: boolean},
 *   scrollToBottom: (force?: boolean) => void,
 *   handleScroll: () => void,
 * }}
 */
export function useStickToBottom({
  containerRef,
  contentRef = null,
  enabled = true,
  threshold = STICK_TOLERANCE_PX,
  anchorSelector = null,
  onPinnedChange = null,
} = {}) {
  // `pinned` is state so panels can re-render their affordances; the ref is the
  // authority read from stable callbacks (observer, listeners, effects) where a
  // closure over the state value would be stale.
  const [pinned, setPinnedState] = useState(true)
  const pinnedRef = useRef(true)
  const programmaticRef = useRef(false)
  const programmaticTimerRef = useRef(null)
  const anchorRef = useRef(null)
  const attachedElRef = useRef(null)
  const observerRef = useRef(null)
  const detachListenersRef = useRef(null)
  const touchStartYRef = useRef(null)

  const enabledRef = useRef(enabled)
  const onPinnedChangeRef = useRef(onPinnedChange)
  // Synced during render on purpose: the callbacks below are memoised (they are
  // handed to ResizeObserver and addEventListener), so they must read the CURRENT
  // option values through refs rather than a first-render closure.
  enabledRef.current = enabled
  onPinnedChangeRef.current = onPinnedChange

  const applyPinned = useCallback((next) => {
    if (pinnedRef.current === next) return
    pinnedRef.current = next
    setPinnedState(next)
    if (onPinnedChangeRef.current) onPinnedChangeRef.current(next)
  }, [])

  const markProgrammatic = useCallback(() => {
    programmaticRef.current = true
    if (programmaticTimerRef.current != null) clearTimeout(programmaticTimerRef.current)
    programmaticTimerRef.current = setTimeout(() => {
      programmaticRef.current = false
      programmaticTimerRef.current = null
    }, PROGRAMMATIC_CLEAR_MS)
  }, [])

  const glueToBottom = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    markProgrammatic()
    // scrollHeight, not scrollHeight - clientHeight: the browser clamps an
    // overshoot, and the existing panels/tests assert scrollTop === scrollHeight.
    el.scrollTop = el.scrollHeight
    applyPinned(true)
  }, [applyPinned, containerRef, markProgrammatic])

  const snapshotAnchor = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const scope = (contentRef && contentRef.current) || el
    const anchor = anchorSelector
      ? scope.querySelector(anchorSelector)
      : scope.firstElementChild
    if (!anchor) {
      anchorRef.current = null
      return
    }
    anchorRef.current = { el: anchor, offset: _offsetWithin(el, anchor) }
  }, [anchorSelector, containerRef, contentRef])

  const restoreAnchor = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const saved = anchorRef.current
    // No usable anchor (empty panel, node released by a re-render): take a fresh
    // snapshot and leave scrollTop alone rather than guessing a position.
    if (!saved || !saved.el || saved.el.isConnected === false) {
      snapshotAnchor()
      return
    }
    const delta = _offsetWithin(el, saved.el) - saved.offset
    if (delta !== 0) {
      // Something ABOVE the visible window grew or shrank (markdown reflow, a
      // late-loading code block, older messages prepended): shift scrollTop by
      // exactly the delta so the text under the reader's eyes does not jump.
      markProgrammatic()
      el.scrollTop = Math.max(0, el.scrollTop + delta)
    }
    snapshotAnchor()
  }, [containerRef, markProgrammatic, snapshotAnchor])

  const unpin = useCallback(() => {
    if (!enabledRef.current) return
    // Intent signal only — it must not fight the position-based decision in
    // handleScroll: a wheel back down at the bottom re-pins on the next scroll.
    if (!pinnedRef.current) return
    applyPinned(false)
    snapshotAnchor()
  }, [applyPinned, snapshotAnchor])

  const handleScroll = useCallback(() => {
    if (!enabledRef.current) return
    const el = containerRef.current
    if (!el) return
    const atBottom = _distanceFromBottom(el) <= threshold
    if (programmaticRef.current) {
      // Our own scroll.  Reading it as "the user scrolled up" is the classic
      // false-unpin; suppressing it outright is just as wrong, because while a
      // stream glues on every commit this flag is almost always set and a reader
      // could then never scroll up.  So: landing at/near the bottom confirms the
      // programmatic scroll, while a clear departure from the bottom can only
      // have been user intent.
      programmaticRef.current = false
      if (programmaticTimerRef.current != null) {
        clearTimeout(programmaticTimerRef.current)
        programmaticTimerRef.current = null
      }
      if (atBottom) {
        applyPinned(true)
        snapshotAnchor()
        return
      }
    }
    // The scroll POSITION is the authority: reaching the bottom re-pins, moving
    // away unpins.  wheel/touchmove only accelerate the unpin.
    applyPinned(atBottom)
    snapshotAnchor()
  }, [applyPinned, containerRef, snapshotAnchor, threshold])

  const scrollToBottom = useCallback((force = false) => {
    // force=true is the explicit "take me to the bottom" call — after sending a
    // message, on initial session load, or the ↓ button — and it must win even
    // while the reader is scrolled up.  force=false only follows a pinned view,
    // which keeps the auto-scroll paths from yanking a reader out of history.
    if (!force && !pinnedRef.current) return
    glueToBottom()
  }, [glueToBottom])

  const handleResize = useCallback(() => {
    if (!enabledRef.current) return
    // The observer fires AFTER layout, so both branches already see the new
    // metrics.  `> 1` skips the redundant write (and the programmatic flag it
    // would arm) when the view is already glued.
    const el = containerRef.current
    if (!el) return
    if (pinnedRef.current) {
      if (_distanceFromBottom(el) > 1) glueToBottom()
      return
    }
    restoreAnchor()
  }, [containerRef, glueToBottom, restoreAnchor])

  const detach = useCallback(() => {
    if (detachListenersRef.current) {
      detachListenersRef.current()
      detachListenersRef.current = null
    }
    if (observerRef.current) {
      observerRef.current.disconnect()
      observerRef.current = null
    }
    if (programmaticTimerRef.current != null) {
      clearTimeout(programmaticTimerRef.current)
      programmaticTimerRef.current = null
    }
    programmaticRef.current = false
    attachedElRef.current = null
  }, [])

  const attach = useCallback((el) => {
    attachedElRef.current = el
    const target = (contentRef && contentRef.current) || el
    // `typeof` guard, not a capability check: jsdom (and any non-browser host)
    // has no ResizeObserver, and a hook that throws on mount would take the whole
    // panel down.  Without the observer the per-commit pass below still keeps the
    // view stuck; only async expansion (images, highlighted code) is missed.
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(handleResize)
      observer.observe(target)
      observerRef.current = observer
    }
    const onWheel = (event) => {
      // Negative deltaY = scrolling up = revealing earlier content.
      if (event.deltaY < 0) unpin()
    }
    const onTouchStart = (event) => {
      const touch = event.touches && event.touches[0]
      touchStartYRef.current = touch ? touch.clientY : null
    }
    const onTouchMove = (event) => {
      const touch = event.touches && event.touches[0]
      const startY = touchStartYRef.current
      // Finger dragging DOWN scrolls the content up (reveals earlier history).
      if (touch && startY != null && touch.clientY > startY) unpin()
    }
    el.addEventListener('wheel', onWheel, { passive: true })
    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: true })
    detachListenersRef.current = () => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
    }
  }, [contentRef, handleResize, unpin])

  // Per-commit pass.  Deliberately no dependency array: this is the same
  // "DOM changed, re-measure" step the panels used to run from
  // useLayoutEffect(..., [messages]), and a caller-shaped deps array would change
  // length between renders, which React rejects.  It is not polling — it runs
  // once per commit and every branch exits on a cheap measurement.
  useLayoutEffect(() => {
    if (!enabled) {
      detach()
      return
    }
    const el = containerRef.current
    if (!el) return
    // Re-attach when the scroll element is REPLACED (panel reopened, or a first
    // render that returned early before rendering the scroll div at all): the
    // observer and the listeners belong to a specific element.
    if (attachedElRef.current !== el) {
      detach()
      attach(el)
    }
    if (pinnedRef.current) {
      if (_distanceFromBottom(el) > 1) glueToBottom()
      return
    }
    restoreAnchor()
  })

  // Unmount: no observer and no listeners may survive the component.
  useEffect(() => () => { detach() }, [detach])

  return {
    pinned,
    pinnedRef,
    // Alias of pinnedRef under the name both panels already use, so wiring them
    // up is a one-line swap of the ref they declared themselves.
    isAtBottomRef: pinnedRef,
    scrollToBottom,
    handleScroll,
  }
}
