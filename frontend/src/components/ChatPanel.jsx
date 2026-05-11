/*
 * ChatPanel.jsx
 *
 * Renders a list of message bubbles.
 * Supports roles: user, assistant, tool_call, tool_result, system.
 * Each role gets a distinct CSS class and alignment.
 *
 * Props: history — array of { role, content } objects
 */

import React, { useEffect, useRef } from 'react'

const ROLE_STYLE = {
  user:        { className: 'message-user',     label: 'You' },
  assistant:   { className: 'message-assistant', label: 'Assistant' },
  tool_call:   { className: 'message-tool-call', label: 'Tool Call' },
  tool_result: { className: 'message-tool-result', label: 'Tool Result' },
  system:      { className: 'message-system',    label: 'System' },
}

function MessageBubble({ msg, index }) {
  const style = ROLE_STYLE[msg.role] || ROLE_STYLE.system
  return (
    <div className={`message ${style.className}`} key={index}>
      <div className="message-sender">{style.label}</div>
      <div className="message-content">{msg.content}</div>
    </div>
  )
}

export default function ChatPanel({ history }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [history])

  return (
    <div className="chat-panel">
      {history.length === 0 && (
        <div className="chat-empty">
          No messages yet. Type a query below and press Run.
        </div>
      )}
      {history.map((msg, i) => (
        <MessageBubble key={i} msg={msg} index={i} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
