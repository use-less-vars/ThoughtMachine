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
 *   - ALL stick-to-bottom behaviour (at-bottom tracking, the programmatic-scroll
 *     guard, the ResizeObserver and the anchor restore used while the reader is
 *     scrolled up) comes from the shared `useStickToBottom` hook, so this panel
 *     and `WorkerOutputPanel` cannot drift apart again.
 *   - What stays here is only the part the hook cannot know about: deciding
 *     WHETHER a scroll is warranted, from the shape of the `messages` array
 *     (an append, or a streaming delta that grew the last message in place).
 *     That decision is then executed through `stick.scrollToBottom()`.
 */

import React, { useEffect, useLayoutEffect, useRef } from 'react'
import { MessageBubble } from './chat/MessageBubble'
import { useStickToBottom } from '../hooks/useStickToBottom'

// Fix 3a: "at bottom" window (~50px). Wider than the old 20px so a user
// reading near the bottom edge isn't considered "scrolled up" by every small
// append, while still auto-scrolling for the common reading-at-bottom case.
const AT_BOTTOM_THRESHOLD = 50

/* ── Main panel ── */
function ChatPanel({ messages, loadMore, hasMore, scrollToBottomKey = 0 }) {
  const chatRef = useRef(null)
  const prevCountRef = useRef(0)
  // Streaming deltas grow the LAST message in place (count unchanged), so the
  // last rendered content is tracked to detect "appended below the fold".
  const prevLastContentRef = useRef(null)

  // `threshold` keeps this panel's historical 50px "near the bottom" window;
  // the hook's own default is narrower.
  const stick = useStickToBottom({
    containerRef: chatRef,
    threshold: AT_BOTTOM_THRESHOLD,
  })

  // ── Auto-scroll on message append ──
  // Runs synchronously after DOM mutations (useLayoutEffect) but double-rAF
  // ensures async rendering (syntax highlighting, markdown) has settled.
  useLayoutEffect(() => {
    const currentCount = messages ? messages.length : 0
    const prevCount = prevCountRef.current
    prevCountRef.current = currentCount
    // A streaming delta keeps the count constant and only grows the last
    // message's text; treat that as an append so the view follows the stream.
    // The THINKING phase grows `reasoning_content` only (the answer body is
    // still empty), so the signature must cover both fields -- keyed on
    // `content` alone a reasoning-only delta looked like "no change" and the
    // view fell into the restore-scrollTop branch below, freezing the panel
    // while the model was thinking. The signature is private to this ref, so
    // the two-field shape is not observable anywhere else.
    const lastMsg = currentCount > 0 ? messages[currentCount - 1] : null
    const currentLastContent = currentCount > 0
      ? `${lastMsg?.content ?? ''}\u0000${lastMsg?.reasoning_content ?? ''}`
      : ''
    const prevLastContent = prevLastContentRef.current
    prevLastContentRef.current = currentLastContent
    const grewInPlace = currentCount === prevCount && currentCount > 0 &&
      currentLastContent !== prevLastContent

    // No container — nothing to do.
    if (!chatRef.current || currentCount === 0) return

    // Not warranted: the reader is scrolled up, or the list was REPLACED rather
    // than appended to.  Nothing to do here — while unpinned the hook restores the
    // reading anchor itself, and two writers of scrollTop would fight.
    if (!stick.isAtBottomRef.current || (currentCount <= prevCount && !grewInPlace)) {
      return
    }

    // Content was APPENDED and user is at bottom — follow it.  The hook performs
    // the write (it also arms the programmatic guard, which is what stops this
    // scroll from being read as the user scrolling up).  Kept on double-rAF: the
    // async rendering (markdown, syntax highlighting) settles over two frames.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        stick.scrollToBottom()
      })
    })
  }, [messages])

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
        stick.scrollToBottom(true)
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
        stick.isAtBottomRef.current = false
        return
      }
    }
    // No user message above viewport, go to the first one (top of chat)
    if (userMessages.length > 0) {
      el.scrollTop = userMessages[0].offsetTop - 20
      stick.isAtBottomRef.current = false
    }
  }

  return (
    <div className="chat-panel-wrapper">
      <div className="chat-panel" ref={chatRef} onScroll={stick.handleScroll}>
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
        <button className="scroll-bottom-btn" onClick={() => stick.scrollToBottom(true)} title="Scroll to bottom">
          ↓
        </button>
      </div>
    </div>
  )
}

export default React.memo(ChatPanel)
