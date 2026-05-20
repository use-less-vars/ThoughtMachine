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
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/* ── Copy-to-clipboard button ── */
function CopyButton({ text, label }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch (e) {
      console.warn('Copy failed:', e)
    }
  }
  return (
    <button className="copy-btn" onClick={handleCopy} title="Copy to clipboard">
      {copied ? '✅' : label || '📋'}
    </button>
  )
}

const TRUNCATE_LENGTH = 500

const ROLE_STYLE = {
  user:        { className: 'message-user',             label: 'You' },
  assistant:   { className: 'message-assistant',       label: 'Assistant' },
  tool_call:   { className: 'message-tool-call',       label: 'Tool Call' },
  tool_result: { className: 'message-tool-result',     label: 'Tool Result' },
  final:       { className: 'message-final',            label: '🎯 Final' },
  system:      { className: 'message-system-as-user',  label: 'System' },
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
        <details className="reasoning-block" open>
          <summary className="reasoning-summary">
            💭 Thinking
            <CopyButton text={msg.reasoning_content} label="📋" />
          </summary>
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
  /* Final results render as full markdown (blueish, no truncation) */
  if (msg.is_final) {
    return (
      <div className="final-markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {msg.content}
        </ReactMarkdown>
      </div>
    )
  }
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
      /* System notification messages with role 'user' render as system style */
      if (msg.is_system_notification) {
        return <p className="system-text">{msg.content}</p>
      }
      return <p className="message-text">{msg.content}</p>
  }
}

/* ── Single bubble ── */
function MessageBubble({ msg, index }) {
  /* ── System notifications are stored as 'user' role with is_system_notification flag ── */
  const effectiveRole = msg.is_final ? 'final' : (msg.is_system_notification ? 'system' : msg.role)
  const style = ROLE_STYLE[effectiveRole] || ROLE_STYLE.system
  const copyText = msg.reasoning_content
    ? `${msg.reasoning_content}\n\n---\n\n${msg.content}`
    : msg.content
  return (
    <div className={`message ${style.className}`} key={index}>
      <div className="message-sender">
        {style.label}
        <CopyButton text={copyText} label="📋" />
      </div>
      <MessageContent msg={msg} />
    </div>
  )
}

/* ── Main panel ── */
function ChatPanel({ messages }) {
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

  return (
    <div className="chat-panel" ref={chatRef} onScroll={handleScroll}>
      {(!messages || messages.length === 0) && (
        <div className="chat-empty">Send a message to start.</div>
      )}
      {messages && messages.map((msg, i) => (
        <MessageBubble key={i} msg={msg} index={i} />
      ))}
    </div>
  )
}

export default React.memo(ChatPanel)
