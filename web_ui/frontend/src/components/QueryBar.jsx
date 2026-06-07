/*
 * QueryBar.jsx
 *
 * Text input + contextual action buttons.
 *
 * Button logic:
 *   status     Run | Pause | Stop         Command sent
 *   ────────────────────────────────────────────────────
 *   IDLE       ✓   |       |              start_session (fresh) /
 *                                            continue_session (if isRunning)
 *   RUNNING        | ✓    | ✓            pause_session / stop_session
 *   PAUSING    ⛔  |      |              single disabled Pause button
 *   PAUSED     ✓   |       | ✓            continue_session(query) / stop_session
 *   WAITING... ✓   |       | ✓            continue_session(query)
 *
 * Props:
 *   sendCommand(command, payload)
 *   status, isRunning, config
 */

import React, { useState, useRef } from 'react'

function QueryBar({ sendCommand, status, isRunning, config, sessionId }) {
  const [query, setQuery] = useState('')
  const textareaRef = useRef(null)

  const isIdle = status === 'IDLE'
  const isBusy = status === 'RUNNING'
  const isPaused = status === 'PAUSED'
  const isPausing = status === 'PAUSING'
  const isWaiting = status === 'WAITING_FOR_USER'

  const handleRun = () => {
    if (!query.trim()) return
    if (sessionId) {
      // Loaded session — continue with existing context, passing config
      sendCommand('continue_session', {
        query: query.trim(),
        session_id: sessionId,
        config: config ?? {}
      })
    } else {
      // Fresh start — create new agent session
      sendCommand('start_session', { query: query.trim(), config: config ?? {} })
    }
    setQuery('')  // Clear input after sending
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  const handleToggle = () => {
    if (isBusy) {
      sendCommand('pause_session', {})
    } else if (isPaused) {
      sendCommand('continue_session', { query: query.trim() })
      setQuery('')
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto'
      }
    } else {
      handleRun()
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && (isIdle || isWaiting || isPaused)) {
      e.preventDefault()
      handleToggle()
    }
  }

  // Debounced auto-resize using requestAnimationFrame to avoid layout thrashing
  const resizeRafRef = useRef(null)
  const handleResize = (e) => {
    if (resizeRafRef.current) return  // coalesce multiple events into one frame
    resizeRafRef.current = requestAnimationFrame(() => {
      resizeRafRef.current = null
      const el = textareaRef.current
      if (!el) return
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 200) + 'px'
    })
  }

  return (
    <div className="query-bar">
      <textarea
        ref={textareaRef}
        className="query-input"
        placeholder="Enter your query…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        onInput={handleResize}
        disabled={false}  /* Always writable — buttons control what's allowed */
        rows={1}
      />
      <div className="query-buttons">
        {/* Toggle Run/Pause — always visible. No Resume button. */}
        {isBusy ? (
          <button className="btn btn-pause" onClick={handleToggle}>
            ⏸ Pause
          </button>
        ) : isPausing ? (
            <button className="btn btn-pause" disabled>
              ⏸ Pausing…
            </button>
        ) : (
          <button
            className="btn btn-run"
            onClick={handleToggle}
            disabled={!query.trim() && isIdle}
          >
            ▶ Run
          </button>
        )}
      </div>
    </div>
  )
}

export default React.memo(QueryBar)
