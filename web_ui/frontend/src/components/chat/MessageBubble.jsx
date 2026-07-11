/*
 * MessageBubble.jsx
 *
 * Shared message rendering components extracted from ChatPanel.
 * Used by both ChatPanel and WorkerOutputPanel.
 *
 * Supports roles: user, assistant, tool_call, tool_result, reasoning, system.
 * Each role gets a distinct CSS class and alignment.
 *
 * Exports: MessageBubble (default), MessageContent, CopyButton
 */

import React, { useState, memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/* ── Copy-to-clipboard button ──
 *
 * Uses onMouseDown + preventDefault so clicking never steals focus
 * from wherever the user is (the query bar, another field, etc.).
 */
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
    <button className="copy-btn" onMouseDown={(e) => e.preventDefault()} onClick={handleCopy} title="Copy to clipboard">
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
  summary:     { className: 'message-summary',           label: '📝 Summary' },
  final:       { className: 'message-final',            label: '🎯 Final' },
  question:    { className: 'message-question',          label: '❓ Question' },
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

/* ── Summary tool result (dark golden, full markdown, no truncation) ── */
function SummaryContent({ content }) {
  return (
    <div className="summary-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {content}
      </ReactMarkdown>
    </div>
  )
}

/* ── Tool call display ── */
function ToolCallContent({ content }) {
  const MAX_LINES = 5
  let parsed = null
  try {
    parsed = JSON.parse(content)
  } catch (e) {
    return <pre className="tool-call-raw">{content}</pre>
  }
  const { name, arguments: args } = parsed

  /* Pretty-print args and limit to MAX_LINES lines */
  let argsText = ''
  let truncated = false
  if (args) {
    const full = JSON.stringify(args, null, 2)
    const lines = full.split('\n')
    if (lines.length > MAX_LINES) {
      argsText = lines.slice(0, MAX_LINES).join('\n') + '\n...'
      truncated = true
    } else {
      argsText = full
    }
  }

  return (
    <details className="tool-call-details" open>
      <summary className="tool-call-summary">
        🛠️ Tool Call: <strong>{name}</strong>
      </summary>
      <div className="tool-call-body">
        {args ? (
          <pre className="tool-call-args">{argsText}</pre>
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
  /* Summary results: dark golden, full markdown, no truncation */
  if (msg.is_summary) {
    return <SummaryContent content={msg.content} />
  }

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
        /* Token warnings get a distinct yellow/orange banner */
        if (msg.content && (msg.content.toLowerCase().includes('token') || msg.content.includes('\u26a0\ufe0f'))) {
          return (
            <div style={{
              background: '#fff3cd',
              border: '1px solid #ffc107',
              color: '#856404',
              padding: '10px 14px',
              borderRadius: '8px',
              marginBottom: '8px',
              fontWeight: 500,
              fontSize: '0.9rem',
              lineHeight: '1.4',
            }}>
              {msg.content}
            </div>
          )
        }
        return <p className="system-text">{msg.content}</p>
      }
      return <p className="message-text">{msg.content}</p>
  }
}

/* ── Single bubble ── */
const MessageBubble = React.memo(
  function MessageBubble({ msg, index }) {
  /* ── System notifications are stored as 'user' role with is_system_notification flag ── */
  const effectiveRole = msg.is_final
    ? (msg.response_type === 'question' ? 'question' : 'final')
    : (msg.is_summary ? 'summary' : (msg.is_system_notification ? 'system' : msg.role))
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
  },
  (prev, next) =>
    prev.index === next.index &&
    prev.msg.content === next.msg.content &&
    prev.msg.role === next.msg.role &&
    prev.msg.reasoning_content === next.msg.reasoning_content &&
    prev.msg.is_system_notification === next.msg.is_system_notification &&
    prev.msg.is_final === next.msg.is_final &&
    prev.msg.is_summary === next.msg.is_summary &&
    prev.msg.response_type === next.msg.response_type
)

export { MessageBubble, MessageContent, CopyButton }
export default MessageBubble
