/*
 * ChatPanel.jsx
 *
 * Renders a list of message bubbles with full Markdown support.
 * Supports roles: user, assistant, tool_call, tool_result, reasoning, system.
 * Each role gets a distinct CSS class and alignment.
 *
 * Props: messages — array of { role, content, reasoning_content? } objects
 */

import React, { useState, useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const TRUNCATE_LENGTH = 500

const ROLE_STYLE = {
  user:        { className: 'message-user',     label: 'You' },
  assistant:   { className: 'message-assistant', label: 'Assistant' },
  tool_call:   { className: 'message-tool-call', label: 'Tool Call' },
  tool_result: { className: 'message-tool-result', label: 'Tool Result' },
  system:      { className: 'message-system',    label: 'System' },
}

/* ── Truncatable text for long tool results ── */
function TruncatableContent({ text }) {
  const [expanded, setExpanded] = useState(false)
  if (!text || text.length <= TRUNCATE_LENGTH) {
    return <pre className="truncatable-content">{text}</pre>
  }
  return (
    <div>
      <pre className="truncatable-content">
        {expanded ? text : text.slice(0, TRUNCATE_LENGTH) + '…'}
      </pre>
      <button className="truncate-toggle" onClick={() => setExpanded(!expanded)}>
        {expanded ? '▲ Show less' : '▼ Show more'}
      </button>
    </div>
  )
}

/* ── Tool call display ── */
function ToolCallContent({ content }) {
  let parsed = null
  try {
    parsed = JSON.parse(content)
  } catch (e) {
    return <pre className="tool-call-raw">{content}</pre>
  }
  const { name, arguments: args } = parsed
  return (
    <details className="tool-call-details">
      <summary className="tool-call-summary">
        🛠️ Tool Call: <strong>{name}</strong>
      </summary>
      <div className="tool-call-body">
        {args ? (
          <pre className="tool-call-args">{JSON.stringify(args, null, 2)}</pre>
        ) : (
          <em>No arguments</em>
        )}
      </div>
    </details>
  )
}

/* ── Assistant message with optional reasoning block ── */
function AssistantContent({ msg }) {
  return (
    <>
      {msg.reasoning_content && (
        <details className="reasoning-block">
          <summary className="reasoning-summary">💭 Thinking</summary>
          <div className="reasoning-content">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {msg.reasoning_content}
            </ReactMarkdown>
          </div>
        </details>
      )}
      <div className="assistant-markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {msg.content}
        </ReactMarkdown>
      </div>
    </>
  )
}

/* ── Route content based on role ── */
function MessageContent({ msg }) {
  switch (msg.role) {
    case 'assistant':
      return <AssistantContent msg={msg} />
    case 'tool_call':
      return <ToolCallContent content={msg.content} />
    case 'tool_result':
      return <TruncatableContent text={msg.content} />
    case 'system':
      return <p className="system-text">{msg.content}</p>
    default:
      return <p className="message-text">{msg.content}</p>
  }
}

/* ── Single bubble ── */
function MessageBubble({ msg, index }) {
  const style = ROLE_STYLE[msg.role] || ROLE_STYLE.system
  return (
    <div className={`message ${style.className}`} key={index}>
      <div className="message-sender">{style.label}</div>
      <MessageContent msg={msg} />
    </div>
  )
}

/* ── Main panel ── */
export default function ChatPanel({ messages }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <div className="chat-panel">
      {(!messages || messages.length === 0) && (
        <div className="chat-empty">Send a message to start.</div>
      )}
      {messages && messages.map((msg, i) => (
        <MessageBubble key={i} msg={msg} index={i} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
