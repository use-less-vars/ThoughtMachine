/*
 * App.jsx
 *
 * Root component — layout + WebSocket lifecycle + event → store routing.
 *
 * Layout:
 *   ┌──────────────────────────────────────┐
 *   │            StatusBar                  │
 *   ├──────────────┬───────────────────────┤
 *   │  ConfigPanel │      ChatPanel         │
 *   │  (sidebar)   │    (conversation)      │
 *   ├──────────────┴───────────────────────┤
 *   │            QueryBar                   │
 *   └──────────────────────────────────────┘
 *
 * WebSocket events (mapped to store actions):
 *   state_changed         →  setStatus(msg.state) + setRunning(msg.is_running)
 *   tokens_updated        →  setTokens(msg.input, msg.output)
 *   context_updated       →  setContextLength(msg.context_length)
 *   conversation_changed  →  setHistory(msg.messages)
 *   config_changed        →  setConfig(msg.config)
 *   status_message        →  addStatusMessage(msg.text)
 */

import React, { useEffect, useRef, useCallback } from 'react'
import useStore from './store/useStore'
import ChatPanel from './components/ChatPanel'
import QueryBar from './components/QueryBar'
import StatusBar from './components/StatusBar'
import ConfigPanel from './components/ConfigPanel'
import './styles.css'

const WS_URL = `ws://${window.location.hostname}:8000/ws`

export default function App() {
  const wsRef = useRef(null)

  // ── Socket lifecycle ──────────────────────────────────────────────────
  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => console.log("✅ WS onopen")

    ws.onmessage = (event) => {
      console.log("📨 WS message:", event.data)
      try {
        const msg = JSON.parse(event.data)
        handleEvent(msg)
      } catch (err) {
        console.error('Failed to parse message:', event.data, err)
      }
    }

    ws.onclose = (e) => {
      console.log("❌ WS onclose code:", e.code, "reason:", e.reason, "wasClean:", e.wasClean)
      useStore.getState().addStatusMessage('Disconnected from agent server.')
      useStore.getState().setStatus('IDLE')
    }

    ws.onerror = (e) => {
      console.error("🔥 WS onerror", e)
      useStore.getState().addStatusMessage('WebSocket error — see console.')
    }

    return () => ws.close()
  }, [])

  // ── Event router (calls store actions directly) ───────────────────────
  function handleEvent(msg) {
    const store = useStore.getState()
    switch (msg.type) {
      case 'state_changed':
        store.setStatus(msg.state)
        store.setRunning(msg.is_running !== false)
        break
      case 'tokens_updated':
        store.setTokens(msg.input ?? 0, msg.output ?? 0)
        break
      case 'context_updated':
        store.setContextLength(msg.context_length ?? 0)
        break
      case 'conversation_changed':
        console.log("📨 Store before update:", useStore.getState().session.history)
        store.setHistory(msg.messages ?? [])
        console.log("📨 Store after update:", useStore.getState().session.history)
        break
      case 'config_changed':
        store.setConfig(msg.config)
        break
      case 'status_message':
        store.addStatusMessage(msg.text ?? '')
        break
      default:
        console.warn('Unknown event type:', msg.type)
    }
  }

  // ── sendCommand helper ────────────────────────────────────────────────
  const sendCommand = useCallback((command, payload = {}) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      useStore.getState().addStatusMessage('Cannot send — not connected.')
      return
    }
    ws.send(JSON.stringify({ command, ...payload }))
  }, [])

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="app-container">
      <StatusBar />
      <div className="app-main">
        <ConfigPanel sendCommand={sendCommand} />
        <ChatPanel />
      </div>
      <QueryBar sendCommand={sendCommand} />
    </div>
  )
}
