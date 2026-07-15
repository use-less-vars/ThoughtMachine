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

/* ── Main panel ── */
function ChatPanel({ messages, loadMore, hasMore }) {
  const chatRef = useRef(null)
  const isAtBottomRef = useRef(true)
  const programmaticScrollRef = useRef(false)
  const prevCountRef = useRef(0)

  // ── Scroll handler — suppress updates during programmatic scroll ──
  const handleScroll = useCallback(() => {
    if (programmaticScrollRef.current) {
      // This scroll event was triggered by our own programmatic scroll;
      // reset the guard for the next user-initiated scroll.
      programmaticScrollRef.current = false
      return
    }
    const el = chatRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 20
    isAtBottomRef.current = atBottom
  }, [])

  // ── Programmatic scroll helper (sets guard flag) ──
  const scrollToBottom = useCallback(() => {
    const el = chatRef.current
    if (!el) return
    programmaticScrollRef.current = true
    el.scrollTop = el.scrollHeight
    isAtBottomRef.current = true
  }, [])

  // ── Auto-scroll on message append ──
  // Runs synchronously after DOM mutations (useLayoutEffect) but double-rAF
  // ensures async rendering (syntax highlighting, markdown) has settled.
  useLayoutEffect(() => {
    const currentCount = messages ? messages.length : 0
    const prevCount = prevCountRef.current
    prevCountRef.current = currentCount

    // Only auto-scroll when content was APPENDED (count increased),
    // user is at bottom, and container exists.
    if (
      !isAtBottomRef.current ||
      !chatRef.current ||
      currentCount <= prevCount ||
      currentCount === 0
    ) {
      return
    }

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (chatRef.current) {
          programmaticScrollRef.current = true
          chatRef.current.scrollTop = chatRef.current.scrollHeight
        }
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
          programmaticScrollRef.current = true
          el.scrollTop = el.scrollHeight
        }
      }
    })

    observer.observe(el)
    return () => observer.disconnect()
  }, [])

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
      {!isAtBottomRef.current && (
        <div className="scroll-nav-group">
          <button className="scroll-prev-btn" onClick={jumpToPrevQuery} title="Jump to previous query">
            ↑
          </button>
          <button className="scroll-bottom-btn" onClick={scrollToBottom} title="Scroll to bottom">
            ↓
          </button>
        </div>
      )}
    </div>
  )
}

export default React.memo(ChatPanel)
