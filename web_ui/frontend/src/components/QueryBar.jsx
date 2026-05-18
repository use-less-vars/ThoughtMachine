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

export default function QueryBar({ sendCommand, status, isRunning, config, sessionId }) {
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

  const handlePause = () => {
    sendCommand('pause_session', {})
  }

  const handleContinue = () => {
    sendCommand('continue_session', { query: query.trim() })
  }

  const handleStop = () => {
    sendCommand('stop_session', {})
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && (isIdle || isWaiting)) {
      e.preventDefault()
      handleRun()
    }
    // Enter while paused → resume + continue
    if (e.key === 'Enter' && !e.shiftKey && isPaused) {
      e.preventDefault()
      handleRun()
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
        {/* Run — visible when IDLE or WAITING_FOR_USER */}
        {(isIdle || isWaiting) && (
          <button
            className="btn btn-run"
            onClick={handleRun}
            disabled={!query.trim()}
          >
            ▶ Run
          </button>
        )}

        {/* Pause — visible when RUNNING */}
        {isBusy && (
          <button className="btn btn-pause" onClick={handlePause}>
            ⏸ Pause
          </button>
        )}

        {/* Continue/Resume — visible when PAUSED */}
        {isPaused && (
          <button className="btn btn-pause" onClick={handleContinue}>
            ▶ Resume
          </button>
        )}

        {/* Stop — visible when RUNNING, PAUSED, or WAITING_FOR_USER */}
        {(isBusy || isPaused || isWaiting) && (
          <button className="btn btn-stop" onClick={handleStop}>
            ⏹ Stop
          </button>
        )}
      </div>
    </div>
  )
}
