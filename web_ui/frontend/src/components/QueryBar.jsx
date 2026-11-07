/*
 * QueryBar.jsx
 *
 * Text input + a Run/Pause toggle plus separate Stop/Resume controls.
 *
 * Button logic:
 *   status      Button                 Command sent
 *   ─────────────────────────────────────────────────────────────
 *   RUNNING     ⏸ Pause                pause_session
 *   PAUSING     ⏸ Pausing… (disabled)  —
 *   otherwise   ▶ Run                  start_session (fresh session) /
 *                                      continue_session(query) (loaded session)
 *   not IDLE /  ⏹ Stop                 stop_session
 *   not connecting
 *   PAUSED      ▶ Resume               resume_session
 *
 * The Run button is disabled while the session is connecting and, when IDLE,
 * until the query is non-empty. Enter (without Shift) triggers the same toggle
 * from IDLE / WAITING_FOR_USER / PAUSED.
 *
 * Props:
 *   sendCommand(command, payload)
 *   status, isRunning, config, mode, sessionId, sessionReady
 */

import React, { useState, useRef } from 'react'

function QueryBar({ sendCommand, status, isRunning, config, mode, sessionId, sessionReady }) {
  const [query, setQuery] = useState('')
  const textareaRef = useRef(null)

  const isIdle = status === 'IDLE'
  const isBusy = status === 'RUNNING'
  const isPaused = status === 'PAUSED'
  const isPausing = status === 'PAUSING'
  const isWaiting = status === 'WAITING_FOR_USER'
  const isConnecting = !sessionReady && !sessionId // fresh tab waiting for session_loaded

  const handleRun = () => {
    if (!query.trim()) return
    console.log(`[DEBUG QueryBar handleRun] sessionId=${sessionId}, status=${status}, isRunning=${isRunning}, query="${query.trim().substring(0, 50)}"`)
    if (sessionId) {
      // Loaded session — continue with existing context, passing config
      sendCommand('continue_session', {
        query: query.trim(),
        session_id: sessionId,
        config: { ...(config ?? {}), mode }
      })
    } else {
      // Fresh start — create new agent session
      sendCommand('start_session', { query: query.trim(), config: { ...(config ?? {}), mode } })
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

  const handleStop = () => {
    sendCommand('stop_session', {})
  }

  const handleResume = () => {
    sendCommand('resume_session', {})
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
        {/* Toggle Run/Pause — always visible. Stop/Resume are separate controls. */}
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
            disabled={(isConnecting) || (!query.trim() && isIdle)}
          >
            {isConnecting ? 'Connecting…' : '▶ Run'}
          </button>
        )}
        {/* Stop — disabled while idle/connecting. Resume — enabled only when PAUSED. */}
        <button className="btn btn-stop" onClick={handleStop} disabled={isIdle || isConnecting}>
          ⏹ Stop
        </button>
        <button className="btn btn-resume" onClick={handleResume} disabled={!isPaused}>
          ▶ Resume
        </button>
      </div>
    </div>
  )
}

export default React.memo(QueryBar)
