/*
 * SessionTab.jsx
 *
 * Self-contained session tab with its own WebSocket connection.
 * Each tab creates a separate bridge on the backend, enabling
 * independent session interaction per tab.
 *
 * Props:
 *   sessionId  — existing session to load, or null for a fresh session
 *   onClose    — called when the user or backend closes this tab
 *   onSessionSaved — called with (sessionId) so App can track new sessions
 *   onNewSession   — called with (sessionId) when a new session is created
 *
 * State (local — not in Zustand):
 *   status, history, tokensIn/Out, contextLength, isRunning, config
 *
 * Child components receive session state as props instead of reading
 * from the global store.
 */

import React, { useEffect, useRef, useState, useCallback } from 'react'
import ChatPanel from './ChatPanel'
import QueryBar from './QueryBar'
import StatusBar from './StatusBar'
import ConfigPanel from './ConfigPanel'

const WS_URL = `ws://${window.location.hostname}:8000/ws`

// ────────────────────────────────────────────────────────────────────────────
// Initial per-tab state
// ────────────────────────────────────────────────────────────────────────────
const INITIAL_STATE = {
  status: 'IDLE',
  history: [],
  tokensIn: 0,
  tokensOut: 0,
  contextLength: 0,
  isRunning: false,
  config: {
    temperature: 0.7,
    max_turns: 20,
    provider: 'openai',
    tools: [
      { name: 'bash', enabled: true },
      { name: 'file_read', enabled: false },
    ],
  },
}

// ────────────────────────────────────────────────────────────────────────────
// Component
// ────────────────────────────────────────────────────────────────────────────
export default function SessionTab({ sessionId, onClose, onNewSession, onSessionSaved, onRegister }) {
  const [state, setState] = useState(INITIAL_STATE)
  const wsRef = useRef(null)
  const closedRef = useRef(false)  // prevent double-close

  // ── Derived helpers ─────────────────────────────────────────────────────
  const update = useCallback((patch) => {
    setState((prev) => ({ ...prev, ...patch }))
  }, [])

  // ── WebSocket lifecycle ─────────────────────────────────────────────────
  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      console.log(`[SessionTab ${sessionId || 'new'}] WS onopen`)

      // Register sendCommand with parent (for save, etc.)
      onRegister?.({ sendCommand })

      // If we have a sessionId, load it; otherwise create a new session
      if (sessionId) {
        ws.send(JSON.stringify({ command: 'load_session', session_id: sessionId }))
      } else {
        ws.send(JSON.stringify({ command: 'new_session' }))
      }
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        handleEvent(msg)
      } catch (err) {
        console.error('[SessionTab] Failed to parse message:', event.data, err)
      }
    }

    ws.onclose = (e) => {
      console.log(`[SessionTab ${sessionId || '?'}] WS closed`, e.code, e.reason)
      if (!closedRef.current) {
        // Unexpected close — notify parent
        onClose?.()
      }
    }

    ws.onerror = (e) => {
      console.error(`[SessionTab ${sessionId || '?'}] WS error`, e)
    }

    return () => {
      closedRef.current = true
      ws.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])  // intentionally run once

  // ── Event router ─────────────────────────────────────────────────────────
  function handleEvent(msg) {
    switch (msg.type) {
      case 'state_changed':
        update({
          status: msg.state,
          isRunning: msg.is_running !== false,
        })
        break

      case 'tokens_updated':
        update({
          tokensIn: msg.input ?? 0,
          tokensOut: msg.output ?? 0,
        })
        break

      case 'context_updated':
        update({ contextLength: msg.context_length ?? 0 })
        break

      case 'conversation_changed':
        update({ history: msg.messages ?? [] })
        break

      case 'config_changed':
        update({ config: msg.config })
        break

      case 'status_message':
        update((prev) => ({
          history: [
            ...prev.history,
            { role: 'system', content: msg.text ?? '' },
          ],
        }))
        break

      case 'session_loaded':
        // Session data will arrive via conversation_changed + config_changed
        // If this is a new session, notify parent with the session_id
        if (msg.session_id && !sessionId) {
          onNewSession?.(msg.session_id)
        }
        break

      case 'session_saved':
        onSessionSaved?.()
        break

      case 'session_closed':
        closedRef.current = true
        onClose?.()
        break

      default:
        console.warn('[SessionTab] Unknown event type:', msg.type)
    }
  }

  // ── sendCommand — sends over this tab's WebSocket ──────────────────────
  const sendCommand = useCallback((command, payload = {}) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn('[SessionTab] Cannot send — WS not connected')
      return
    }
    ws.send(JSON.stringify({ command, ...payload }))
  }, [])

  // ── Render ───────────────────────────────────────────────────────────────
  // Pass per-tab state to children as props
  return (
    <div className="session-tab-content">
      <StatusBar
        status={state.status}
        tokensIn={state.tokensIn}
        tokensOut={state.tokensOut}
        contextLength={state.contextLength}
      />
      <div className="app-main">
        <ConfigPanel config={state.config} sendCommand={sendCommand} />
        <div className="app-center">
          <ChatPanel messages={state.history} />
        </div>
        {/* Session list is rendered by App, not per-tab */}
      </div>
      <QueryBar
        sendCommand={sendCommand}
        status={state.status}
        isRunning={state.isRunning}
        config={state.config}
      />
    </div>
  )
}
