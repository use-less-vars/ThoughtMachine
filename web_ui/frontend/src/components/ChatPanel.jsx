/*
 * ChatPanel.jsx
 *
 * Renders a list of message bubbles with full Markdown support.
 * Supports roles: user, assistant, tool_call, tool_result, reasoning, system.
 * Each role gets a distinct CSS class and alignment.
 *
 * Props: messages — array of { role, content, reasoning_content? } objects
 *
 * Auto-scroll logic:
 *   - Tracks whether the user is "at the bottom" of the scroll container.
 *   - On message APPEND (count increases), if user was at bottom, scroll to
 *     new bottom via useLayoutEffect + double requestAnimationFrame.
 *   - Uses a programmaticScrollRef to suppress the onScroll handler during
 *     programmatic scroll events, preventing false "not at bottom" detection
 *     when async content (syntax highlighting, markdown) expands scrollHeight
 *     between the scroll assignment and the scroll event delivery.
 *   - A ResizeObserver on the container handles async content expansion
 *     while the user is at bottom, keeping the view anchored to the latest
 *     content even as images / code blocks load asynchronously.
 */

import React, { useEffect, useLayoutEffect, useRef, useCallback } from 'react'
import { MessageBubble } from './chat/MessageBubble'

// Fix 3a: "at bottom" window (~50px). Wider than the old 20px so a user
// reading near the bottom edge isn't considered "scrolled up" by every small
// append, while still auto-scrolling for the common reading-at-bottom case.
const AT_BOTTOM_THRESHOLD = 50

/* ── Main panel ── */
function ChatPanel({ messages, loadMore, hasMore, scrollToBottomKey = 0 }) {
  const chatRef = useRef(null)
  const isAtBottomRef = useRef(true)
  const programmaticScrollRef = useRef(false)
  const prevCountRef = useRef(0)
  // Fix 3a: last known scrollTop — used to preserve the user's reading
  // position when the messages list is replaced while scrolled up.
  const prevScrollTopRef = useRef(0)

  // ── Scroll handler — suppress updates during programmatic scroll ──
  const handleScroll = useCallback(() => {
    if (programmaticScrollRef.current) {
      // This scroll event was triggered by our own programmatic scroll;
      // reset the guard for the next user-initiated scroll.
      programmaticScrollRef.current = false
      // Update isAtBottomRef so it reflects the user's actual scroll position
      // even when this scroll event was triggered programmatically.
      const el = chatRef.current
      if (el) {
        isAtBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_THRESHOLD
        prevScrollTopRef.current = el.scrollTop
      }
      return
    }
    const el = chatRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_THRESHOLD
    isAtBottomRef.current = atBottom
    prevScrollTopRef.current = el.scrollTop
  }, [])

  // ── Centralized programmatic scroll helper ──
  // All direct scroll-to-bottom calls route through this function.
  // Pass force=true to scroll regardless of isAtBottomRef state.
  const scrollToBottomFn = (force = false) => {
    if (!force && !isAtBottomRef.current) return
    const el = chatRef.current
    if (el) {
      programmaticScrollRef.current = true
      el.scrollTop = el.scrollHeight
      isAtBottomRef.current = true
    }
  }

  // ── Auto-scroll on message append ──
  // Runs synchronously after DOM mutations (useLayoutEffect) but double-rAF
  // ensures async rendering (syntax highlighting, markdown) has settled.
  useLayoutEffect(() => {
    const currentCount = messages ? messages.length : 0
    const prevCount = prevCountRef.current
    prevCountRef.current = currentCount

    // No container — nothing to do.
    if (!chatRef.current || currentCount === 0) return

    // Fix 3a: content changed while the user is scrolled UP — preserve their
    // exact reading position. Browsers keep scrollTop when content is appended
    // below the fold, but conversation_changed REPLACES the whole list, so we
    // defensively restore the last known scrollTop from prevScrollTopRef.
    // This replaces the previous hard early-return and prevents scroll fights
    // between this effect and the ResizeObserver.
    if (!isAtBottomRef.current || currentCount <= prevCount) {
      chatRef.current.scrollTop = prevScrollTopRef.current
      return
    }

    // Content was APPENDED and user is at bottom — scroll to the new bottom.
    // Runs synchronously after DOM mutations (useLayoutEffect) but double-rAF
    // ensures async rendering (syntax highlighting, markdown) has settled.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        scrollToBottomFn()
      })
    })
  }, [messages])

  // ── ResizeObserver — keep anchored at bottom during async expansion ──
  useEffect(() => {
    const el = chatRef.current
    if (!el) return

    const observer = new ResizeObserver(() => {
      // Only intervene if user is at bottom and we're not already
      // in the middle of a programmatic scroll.
      if (isAtBottomRef.current && !programmaticScrollRef.current) {
        if (el.scrollHeight - el.scrollTop - el.clientHeight > 1) {
          // Content expanded below the fold while user was at bottom.
          scrollToBottomFn()
        }
      }
    })

    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // ── Force scroll to bottom when scrollToBottomKey changes ──
  // Fix 3a: the key is bumped on (a) context compaction/summary recovery
  // (R3, pre-existing wiring in SessionTab.handleEvent) and (b) user query
  // send (start_session / continue_session, wired in SessionTab.sendCommand).
  // Both are explicit user/system actions where force-scrolling is desired
  // EVEN IF the user is scrolled up — hence the removed isAtBottomRef guard
  // (previously it made this key a no-op whenever the user had scrolled up,
  // defeating the compaction intent).
  useEffect(() => {
    const el = chatRef.current
    if (!el) return
    if (scrollToBottomKey === 0) return  // initial value, no action

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        scrollToBottomFn(true)
      })
    })
  }, [scrollToBottomKey])

  // ── Scroll to previous user query (jump up) ──
  const jumpToPrevQuery = () => {
    const el = chatRef.current
    if (!el) return
    const userMessages = Array.from(el.querySelectorAll('.message-user'))
    // Walk backwards from the last user message
    for (let i = userMessages.length - 1; i >= 0; i--) {
      const msgEl = userMessages[i]
      if (msgEl.offsetTop + msgEl.offsetHeight < el.scrollTop + 10) {
        // This user message is above the current viewport — scroll to it
        el.scrollTop = msgEl.offsetTop - 20
        isAtBottomRef.current = false
        return
      }
    }
    // No user message above viewport, go to the first one (top of chat)
    if (userMessages.length > 0) {
      el.scrollTop = userMessages[0].offsetTop - 20
      isAtBottomRef.current = false
    }
  }

  return (
    <div className="chat-panel-wrapper">
      <div className="chat-panel" ref={chatRef} onScroll={handleScroll}>
        {(!messages || messages.length === 0) && (
          <div className="chat-empty">Send a message to start.</div>
        )}
        {messages && hasMore && loadMore && (
          <div className="load-more-container">
            <button className="load-more-btn" onClick={loadMore}>
              ← Load older messages
            </button>
          </div>
        )}
        {messages && messages.map((msg, i) => (
          <MessageBubble key={i} msg={msg} index={i} />
        ))}
      </div>
      <div className="scroll-nav-group">
        <button className="scroll-prev-btn" onClick={jumpToPrevQuery} title="Jump to previous query">
          ↑
        </button>
        <button className="scroll-bottom-btn" onClick={() => scrollToBottomFn(true)} title="Scroll to bottom">
          ↓
        </button>
      </div>
    </div>
  )
}

export default React.memo(ChatPanel)
