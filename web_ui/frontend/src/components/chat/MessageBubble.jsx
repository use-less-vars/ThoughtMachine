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

/* ── Assistant message with optional reasoning block ──
 *
 * Streaming fast path: while a message is still being received (msg.is_streaming)
 * its text changes on every animation frame, and ReactMarkdown re-parses +
 * re-renders the whole document each time — that is the dominant cost of a live
 * turn. So during streaming we render plain pre-wrapped text and only switch to
 * ReactMarkdown once the message is final (markdown formatting of a half-written
 * document is wrong anyway: an unterminated ** or ``` flickers between states).
 */
function AssistantContent({ msg }) {
  const isStreaming = msg.is_streaming === true
  /* The thinking block defaults to open. A user can collapse it, and that
   * choice must survive the rest of the stream -- but NOT leak onto a
   * different message: ChatPanel keys bubbles by array index
   * (`messages.map((msg, i) => <MessageBubble key={i} .../>)`), so a load-more
   * prepend or a whole-list `conversation_changed` replace re-uses the same
   * component instance for a DIFFERENT message. Messages carry no id, so
   * identity is derived from content: while streaming, the reasoning text is
   * append-only, so a live continuation of the collapsed text is a prefix of
   * the new text. Anything that is not a prefix-extension is a different
   * message, and the block re-opens. */
  const [collapsedText, setCollapsedText] = useState(null)
  const reasoningText = msg.reasoning_content || ''
  const reasoningOpen = collapsedText === null || !reasoningText.startsWith(collapsedText)
  return (
    <>
      {msg.reasoning_content && (
        <details
          className="reasoning-block"
          open={reasoningOpen}
          onToggle={(e) => setCollapsedText(e.currentTarget.open ? null : reasoningText)}
        >
          <summary className="reasoning-summary">
            💭 Thinking
            <CopyButton text={msg.reasoning_content} label="📋" />
          </summary>
          <div className="reasoning-content">
            {isStreaming ? (
              <div style={{ whiteSpace: 'pre-wrap' }}>{msg.reasoning_content}</div>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {msg.reasoning_content}
              </ReactMarkdown>
            )}
          </div>
        </details>
      )}
      <div className="assistant-markdown">
        {isStreaming ? (
          <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {msg.content}
          </ReactMarkdown>
        )}
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
      return (
        <>
          <ToolCallContent content={msg.content} />
          {/* Paired result: the call and its outcome share ONE bubble now, so
              the outcome is rendered below the args.  Unpaired results never
              reach this path — they keep role 'tool_result'. */}
          {msg.result !== undefined && msg.result !== null && String(msg.result) !== ''
            ? <TruncatableContent text={String(msg.result)} />
            : null}
        </>
      )
    case 'tool_result':
      return <TruncatableContent text={msg.content} />
    case 'system':
      return <p className="system-text">{msg.content}</p>
    default:
      /* System notification messages with role 'user' render as system style */
      if (msg.is_system_notification) {
        /* Token warnings use the same muted style as other system notifications */
        if (msg.content && (msg.content.toLowerCase().includes('token') || msg.content.includes('⚠️'))) {
          return <p className="system-text">{msg.content}</p>
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
  const effectiveRole = (msg.is_final && !msg.is_system_notification)
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
    prev.msg.response_type === next.msg.response_type &&
    prev.msg.is_streaming === next.msg.is_streaming &&
    // Merged tool bubbles: content is intentionally UNCHANGED when a result
    // lands on an existing call bubble (content is first-insert authoritative),
    // so the fields that DO change on merge must be compared here or the
    // bubble would never re-render and the result would stay invisible.
    prev.msg.result === next.msg.result &&
    prev.msg.success === next.msg.success &&
    prev.msg.error === next.msg.error &&
    prev.msg.status === next.msg.status
)

export { MessageBubble, MessageContent, CopyButton }
export default MessageBubble
