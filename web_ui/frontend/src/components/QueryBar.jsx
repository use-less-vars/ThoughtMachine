/*
 * QueryBar.jsx
 *
 * Text input + contextual action buttons.
 *
 * Button logic:
 *   status     Run | Pause | Resume | Stop       Command sent
 *   ──────────────────────────────────────────────────────────
 *   IDLE       ✓   |       |        |            start_session (fresh) /
 *                                                  continue_session (if isRunning)
 *   RUNNING        | ✓    |        | ✓          pause_session / stop_session
 *   PAUSED         |       | ✓     | ✓          continue_session(query) / stop_session
 *   WAITING... ✓   |       |        | ✓          continue_session(query)
 *
 * Props:
 *   sendCommand(command, payload)
 *   status, isRunning, config
 */

import React, { useState } from 'react'

function QueryBar({ sendCommand, status, isRunning, config, sessionId }) {
  const [query, setQuery] = useState('')

  const isIdle = status === 'IDLE'
  const isBusy = status === 'RUNNING'
  const isPaused = status === 'PAUSED'
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
  }

  const handleToggle = () => {
    if (isBusy) {
      sendCommand('pause_session', {})
    } else if (isPaused) {
      sendCommand('continue_session', { query: query.trim() })
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

  return (
    <div className="query-bar">
      <textarea
        className="query-input"
        placeholder="Enter your query…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        onInput={(e) => {
          e.target.style.height = 'auto';
          e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px';
        }}
        disabled={isBusy || isPaused}
        rows={1}
      />
      <div className="query-buttons">
        {/* Toggle Run/Pause/Resume — always visible */}
        {isBusy ? (
          <button className="btn btn-pause" onClick={handleToggle}>
            ⏸ Pause
          </button>
        ) : isPaused ? (
          <button className="btn btn-run" onClick={handleToggle}>
            ▶ Resume
          </button>
        ) : (
          <button
            className="btn btn-run"
            onClick={handleToggle}
            disabled={!query.trim()}
          >
            ▶ Run
          </button>
        )}
      </div>
    </div>
  )
}

export default React.memo(QueryBar)
