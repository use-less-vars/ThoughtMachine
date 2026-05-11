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

export default function QueryBar({ sendCommand, status, isRunning, config }) {
  const [query, setQuery] = useState('')

  const isIdle = status === 'IDLE'
  const isBusy = status === 'RUNNING'
  const isPaused = status === 'PAUSED'
  const isWaiting = status === 'WAITING_FOR_USER'

  const handleRun = () => {
    if (!query.trim()) return
    if (isIdle && isRunning) {
      // Agent thread still alive — continue session
      sendCommand('continue_session', { query: query.trim() })
    } else {
      // Fresh start — create new agent session
      sendCommand('start_session', { query: query.trim(), config })
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
      <input
        className="query-input"
        type="text"
        placeholder="Enter your query…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={isBusy || isPaused}
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
