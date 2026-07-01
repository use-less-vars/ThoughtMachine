/*
 * ChatPanel.jsx
 *
 * Renders a list of message bubbles with full Markdown support.
 * Supports roles: user, assistant, tool_call, tool_result, reasoning, system.
 * Each role gets a distinct CSS class and alignment.
 *
 * Props: messages — array of { role, content, reasoning_content? } objects
 */

import React, { useState, useEffect, useRef, useCallback } from 'react'
import { MessageBubble } from './chat/MessageBubble'

/* ── Main panel ── */
function ChatPanel({ messages, loadMore, hasMore }) {
  const chatRef = useRef(null)
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true)

  // On scroll, determine if user is at bottom
  const handleScroll = useCallback(() => {
    const el = chatRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 20
    setShouldAutoScroll(atBottom)
  }, [])

  // After messages update, scroll if we were at bottom
  useEffect(() => {
    if (shouldAutoScroll && chatRef.current) {
      chatRef.current.scrollTop = chatRef.current.scrollHeight
    }
  }, [messages, shouldAutoScroll])

  const scrollToBottom = () => {
    if (chatRef.current) {
      chatRef.current.scrollTop = chatRef.current.scrollHeight
      setShouldAutoScroll(true)
    }
  }

  // Scroll to the previous user query above the current viewport
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
        setShouldAutoScroll(false)
        return
      }
    }
    // No user message above viewport, go to the first one (top of chat)
    if (userMessages.length > 0) {
      el.scrollTop = userMessages[0].offsetTop - 20
      setShouldAutoScroll(false)
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
      {!shouldAutoScroll && (
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
